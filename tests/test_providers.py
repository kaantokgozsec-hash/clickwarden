"""Provider settings, consent gate, honest identity, coordinate spaces and the settings logic."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clickwarden import configure, providers, vision
from clickwarden.frames import Frame
from clickwarden.providers import Settings
from clickwarden.vision import StreamResult, VisionClient

ROOT = Path(__file__).resolve().parents[1]
URL = "https://vision.example.com/v1/chat/completions"


def frame(width=200, height=100):
    return Frame(b"jpeg", 0, 0, width, height, width, height, "precision", 85, fg_hwnd=1, fg_title="t")


def ready(**kwargs):
    return Settings(base_url=kwargs.pop("base_url", URL), model="m", api_key=kwargs.pop("api_key", "k"), **kwargs)


class IsolatedConfig(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = {"CLICKWARDEN_CONFIG_DIR": self.folder.name}
        for name in ("API_KEY", "API_URL", "VISION_MODEL", "ACCEPT_DATA_FLOW", "EXTRA_HEADERS", "COORD_SPACE",
                     "SEND_THINKING_DISABLED"):
            env["CLICKWARDEN_" + name] = ""
        patcher = patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReleaseHygieneTests(unittest.TestCase):
    def test_source_never_impersonates_another_client_or_reads_foreign_keys(self):
        forbidden = re.compile(r"opencode-cli|x-opencode|windows-computer-use|OPENCODE_GO_KEY|WINFLAST", re.I)
        offenders = [str(path.relative_to(ROOT)) for path in (ROOT / "clickwarden").rglob("*.py")
                     if forbidden.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_user_agent_is_honest(self):
        self.assertTrue(vision.USER_AGENT.startswith("clickwarden/"))
        client = VisionClient(settings=ready())
        self.assertEqual(client._session.headers["User-Agent"], vision.USER_AGENT)
        self.assertNotIn("x-opencode-session", {k.lower() for k in client._session.headers})
        client.close()


class ConsentTests(IsolatedConfig):
    def configure_remote(self, **config):
        providers.save_config({"base_url": URL, "model": "m", **config})
        providers.save_key("secret")

    def test_nothing_configured_names_the_fix(self):
        settings = providers.resolve()
        self.assertIn("clickwarden configure", settings.problem)
        self.assertFalse(VisionClient(settings=settings).configured)

    def test_remote_provider_needs_the_users_acceptance_for_that_host(self):
        self.configure_remote()
        settings = providers.resolve()
        self.assertIn("vision.example.com", settings.problem)
        self.assertIn("must not accept", settings.problem)
        client = VisionClient(settings=settings)
        self.assertFalse(client.configured)
        with patch.object(client._session, "post") as post:
            self.assertIn("accepted", client.stream_chat("s", "u", b"x").error)
        post.assert_not_called()

    def test_acceptance_is_bound_to_the_host_and_can_be_revoked_by_changing_it(self):
        config = {"base_url": URL, "model": "m"}
        providers.record_consent(config, "vision.example.com")
        self.configure_remote(consent=config["consent"])
        self.assertEqual(providers.resolve().problem, "")
        self.configure_remote(consent=config["consent"], base_url="https://other.example.org/v1/chat/completions")
        self.assertIn("other.example.org", providers.resolve().problem)

    def test_missing_key_is_reported_before_consent(self):
        providers.save_config({"base_url": URL, "model": "m"})
        self.assertIn("No API key", providers.resolve().problem)

    def test_local_server_needs_no_key_or_acceptance(self):
        providers.save_config({"base_url": "http://localhost:11434/v1/chat/completions", "model": "qwen2.5vl:7b"})
        settings = providers.resolve()
        self.assertEqual(settings.problem, "")
        client = VisionClient(settings=settings)
        self.assertTrue(client.configured)
        self.assertNotIn("Authorization", client._session.headers)
        client.close()

    def test_loopback_detection_cannot_be_fooled_by_lookalike_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/x", "http://[::1]:8080/"):
            self.assertTrue(providers.is_local(url), url)
        for url in ("https://localhost.evil.com/x", "http://127.0.0.1.evil.com/x", "https://example.com/localhost",
                    "http://user@evil.com/", ""):
            self.assertFalse(providers.is_local(url), url)

    def test_environment_acceptance_for_headless_use(self):
        self.configure_remote()
        with patch.dict("os.environ", {"CLICKWARDEN_ACCEPT_DATA_FLOW": "vision.example.com"}):
            self.assertEqual(providers.resolve().problem, "")
        with patch.dict("os.environ", {"CLICKWARDEN_ACCEPT_DATA_FLOW": "different.example.com"}):
            self.assertNotEqual(providers.resolve().problem, "")

    def test_key_is_encrypted_at_rest_and_round_trips(self):
        providers.save_key("sk-test-1234567890")
        stored = (Path(self.folder.name) / "key.dpapi").read_text(encoding="ascii")
        self.assertNotIn("sk-test", stored)
        self.assertEqual(providers.load_key(), "sk-test-1234567890")
        providers.save_key("")
        self.assertEqual(providers.load_key(), "")

    def test_explicit_constructor_arguments_bypass_the_saved_settings_gate(self):
        client = VisionClient(api_key="k", api_url=URL, model="m")
        self.assertTrue(client.configured)
        client.close()


class RequestShapeTests(unittest.TestCase):
    def sent(self, settings, **kwargs):
        client = VisionClient(settings=settings)
        with patch.object(client, "_stream_once", return_value=StreamResult(content="ok")) as once:
            client.stream_chat("sys", "query", b"jpeg", **kwargs)
        client.close()
        return once.call_args.args[0]

    def test_thinking_field_and_usage_option_are_opt_in_per_provider(self):
        plain = self.sent(ready())
        self.assertNotIn("thinking", plain)
        self.assertEqual(plain["stream_options"], {"include_usage": True})
        tuned = self.sent(ready(send_thinking_disabled=True, include_usage=False))
        self.assertEqual(tuned["thinking"], {"type": "disabled"})
        self.assertNotIn("stream_options", tuned)

    def test_configured_headers_are_sent_and_cleaned(self):
        client = VisionClient(settings=ready(headers=providers.clean_headers({"X-Title": "Clickwarden"})))
        self.assertEqual(client._session.headers["X-Title"], "Clickwarden")
        client.close()
        self.assertEqual(providers.clean_headers({"Bad Name": "x", "X-Ok": "line\r\nInjected: 1", "X-Fine": "yes",
                                                  "N": 5}), {"X-Fine": "yes"})
        self.assertEqual(providers.clean_headers(["not", "a", "dict"]), {})


class CoordinateSpaceTests(unittest.TestCase):
    def test_prompts_state_the_coordinate_space(self):
        client = VisionClient(settings=ready(coord_space="norm1000"))
        with patch.object(client, "stream_chat", return_value=StreamResult(content='{"status":"absent"}')) as chat:
            client.locate(frame(), "thing")
        self.assertIn("0-1000", chat.call_args.args[0])
        client.close()
        client = VisionClient(settings=ready())
        with patch.object(client, "stream_chat", return_value=StreamResult(content='{"status":"absent"}')) as chat:
            client.locate(frame(), "thing")
        self.assertIn("IMAGE pixels", chat.call_args.args[0])
        client.close()

    def test_norm1000_box_is_converted_before_validation(self):
        client = VisionClient(settings=ready(coord_space="norm1000"))
        reply = StreamResult(content='{"status":"found","target":"x","box":[100,200,300,600],"confidence":"high"}')
        with patch.object(client, "stream_chat", return_value=reply):
            result = client.locate(frame(200, 100), "x")
        self.assertTrue(result["success"])
        self.assertEqual(result["image_box"], [20, 20, 60, 60])
        self.assertEqual(result["image_point"], [40, 40])
        client.close()

    def test_to_pixels_leaves_bad_values_for_validation_to_reject(self):
        bad = {"box": [10, 20, 30, 4000], "point": ["1", 2]}
        self.assertEqual(vision.to_pixels(bad, 100, 100, "norm1000"), bad)
        data = {"box": [0, 0, 1000, 1000]}
        self.assertEqual(vision.to_pixels(data, 640, 480, "pixels"), data)
        self.assertEqual(vision.to_pixels(data, 640, 480, "norm1000")["box"], [0, 0, 640, 480])


class DisclosureTests(unittest.TestCase):
    def test_remote_disclosure_names_host_data_and_controls(self):
        info = providers.disclosure(ready())
        self.assertEqual(info["destination"], "vision.example.com")
        self.assertFalse(info["local_only"])
        self.assertTrue(any("screenshots" in item for item in info["sends"]))
        self.assertIn("Ctrl+Alt+F12 abort", info["controls"])
        text = providers.disclosure_text(ready())
        self.assertIn("vision.example.com", text)
        self.assertIn("open source", text)

    def test_local_disclosure_says_data_stays_on_the_computer(self):
        info = providers.disclosure(ready(base_url="http://localhost:11434/v1/chat/completions"))
        self.assertTrue(info["local_only"])
        self.assertIn("on this computer", info["summary"])

    def test_agent_notice_forbids_accepting_consent_or_typing_secrets(self):
        notice = providers.agent_notice()
        for phrase in ("Never accept data sharing", "credentials", "Cancel/Stop"):
            self.assertIn(phrase, notice)


class ConfigureLogicTests(IsolatedConfig):
    def values(self, **kwargs):
        base = {"preset": "custom", "base_url": URL, "model": "m", "api_key": "", "headers": "",
                "coord_space": "pixels", "send_thinking_disabled": False, "include_usage": True,
                "price_in": "", "price_out": ""}
        base.update(kwargs)
        return base

    def test_remote_save_requires_typing_the_host_to_accept(self):
        errors = configure.save_form(self.values(api_key="k"), accepted=False)
        self.assertTrue(any("Type vision.example.com" in e for e in errors))
        self.assertEqual(providers.load_config(), {})
        self.assertEqual(configure.save_form(self.values(api_key="k"), accepted=True), [])
        config = providers.load_config()
        self.assertEqual(config["consent"]["host"], "vision.example.com")
        self.assertEqual(providers.resolve().problem, "")
        self.assertNotIn("k", json.dumps(config).split('"'))  # key never lands in config.json

    def test_local_save_needs_no_acceptance_and_clears_old_consent(self):
        configure.save_form(self.values(api_key="k"), accepted=True)
        local = self.values(base_url="http://localhost:1234/v1/chat/completions")
        self.assertEqual(configure.save_form(local, accepted=False), [])
        self.assertNotIn("consent", providers.load_config())

    def test_validation_rejects_unsafe_or_malformed_input(self):
        cases = {"base_url": ["", "ftp://x.com/y", "http://remote.example.com/v1", "not a url"],
                 "model": [""], "headers": ["{bad", '["list"]', '{"Bad Name": "x"}', '{"X-A": "a\\r\\nB: c"}'],
                 "coord_space": ["degrees"], "price_in": ["-1", "abc"]}
        for field, bad_values in cases.items():
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    self.assertTrue(configure.form_to_settings(self.values(**{field: bad}))[1])

    def test_blank_key_keeps_the_stored_key(self):
        configure.save_form(self.values(api_key="first"), accepted=True)
        configure.save_form(self.values(api_key=""), accepted=True)
        self.assertEqual(providers.load_key(), "first")

    def test_prices_round_trip_and_describe_hides_the_key(self):
        configure.save_form(self.values(api_key="sk-unique-123", price_in="0.3", price_out="2.5"), accepted=True)
        config = providers.load_config()
        self.assertEqual((config["price_in"], config["price_out"]), (0.3, 2.5))
        summary = configure.describe()
        self.assertEqual(summary["api_key"], "stored")
        self.assertNotIn("sk-unique-123", json.dumps(summary))
        self.assertNotIn("sk-unique-123", (Path(self.folder.name) / "config.json").read_text(encoding="utf-8"))

    def test_set_key_from_terminal_stores_it_without_echo_or_consent(self):
        with patch("getpass.getpass", return_value="  nvapi-terminal-key-123  ") as ask:
            self.assertEqual(configure.main(["--set-key"]), 0)
        self.assertIn("hidden", ask.call_args.args[0])
        self.assertEqual(providers.load_key(), "nvapi-terminal-key-123")
        self.assertNotIn("consent", providers.load_config())  # consent still needs the window
        with patch("getpass.getpass", return_value="   "):
            self.assertEqual(configure.main(["--set-key"]), 1)
        self.assertEqual(providers.load_key(), "nvapi-terminal-key-123")

    def test_window_builds_and_closes_without_error(self):
        import tkinter
        with patch.object(tkinter.Tk, "mainloop", lambda self: (self.update(), self.destroy())):
            configure.launch()


class ConsentFlowTests(IsolatedConfig):
    def test_typed_host_must_match_exactly_not_just_be_nonempty(self):
        settings = ready()
        for typed in ("vision.example.com", " VISION.example.com ", "vision.example.com/"):
            self.assertTrue(configure.host_matches(typed, settings), typed)
        for typed in ("", "yes", "I agree", "example.com", "vision.example.com.evil.net", None):
            self.assertFalse(configure.host_matches(typed, settings), repr(typed))
        self.assertFalse(configure.host_matches("anything", Settings()))

    def test_revoking_consent_blocks_cloud_use_again(self):
        values = ConfigureLogicTests.values(self, api_key="k")
        self.assertEqual(configure.save_form(values, accepted=True), [])
        self.assertEqual(providers.resolve().problem, "")
        self.assertEqual(configure.describe()["data_sharing_accepted_for"], "vision.example.com")
        self.assertTrue(providers.revoke_consent())
        self.assertIn("not accepted", providers.resolve().problem)
        self.assertIsNone(configure.describe()["data_sharing_accepted_for"])
        self.assertFalse(providers.revoke_consent())

    def test_revoke_from_the_command_line(self):
        values = ConfigureLogicTests.values(self, api_key="k")
        configure.save_form(values, accepted=True)
        self.assertEqual(configure.main(["--revoke-consent"]), 0)
        self.assertNotIn("consent", providers.load_config())

    def test_notice_lists_what_can_go_wrong_before_what_is_sent(self):
        text = providers.disclosure_text(ready())
        self.assertLess(text.index("WHAT CAN GO WRONG"), text.index("WHAT LEAVES YOUR COMPUTER"))
        for phrase in ("clicks and types as you", "trick an AI agent", "cannot undo", "Ctrl+Alt+F12"):
            self.assertIn(phrase, text)

    def test_panel_destination_shows_host_or_local(self):
        text, colour = providers.panel_destination(ready())
        self.assertEqual(text, "Screens \u2192 vision.example.com")
        local, _ = providers.panel_destination(ready(base_url="http://localhost:1234/v1/chat/completions"))
        self.assertIn("stay on this computer", local)
        self.assertIn("No vision provider", providers.panel_destination(Settings())[0])


class PanelTests(unittest.TestCase):
    def make(self, **kwargs):
        from clickwarden.panel import NotchPanel
        calls = {name: 0 for name in ("pause", "resume", "cancel", "stop")}

        def bump(name):
            return lambda: calls.__setitem__(name, calls[name] + 1)
        try:
            panel = NotchPanel("test", bump("pause"), bump("resume"), bump("cancel"), bump("stop"), **kwargs)
        except Exception as exc:  # no usable desktop (headless runner) -> nothing to check here
            self.skipTest(f"panel needs an interactive desktop: {exc}")
        self.addCleanup(panel.root.destroy)
        return panel, calls

    def test_stop_is_large_red_and_always_visible(self):
        panel, calls = self.make(destination=("Screens \u2192 vision.example.com", "#ffcf70"))
        stop, cancel = panel.buttons["STOP"], panel.buttons["Cancel"]
        self.assertFalse(panel.expanded)  # visible on the collapsed bar
        self.assertGreaterEqual(int(str(stop.cget("font")).split()[-2] if " bold" in str(stop.cget("font")) else 12), 12)
        self.assertEqual(stop.cget("bg").lower(), "#c8283f")
        self.assertGreater(int(stop.cget("padx")), int(cancel.cget("padx")))
        stop.invoke()
        self.assertEqual(calls["stop"], 1)
        self.assertEqual(calls["cancel"], 0)

    def test_destination_label_is_on_the_collapsed_bar(self):
        panel, _ = self.make(destination=("Screens stay on this computer", "#65df99"))
        self.assertEqual(panel.destination_label.cget("text"), "Screens stay on this computer")
        self.assertEqual(panel.destination_label.cget("fg"), "#65df99")

    def test_job_panels_without_a_stop_callback_use_cancel_for_stop(self):
        from clickwarden.panel import NotchPanel
        called = []
        try:
            panel = NotchPanel("job", lambda: 0, lambda: 0, lambda: called.append("cancel"))
        except Exception as exc:
            self.skipTest(f"panel needs an interactive desktop: {exc}")
        self.addCleanup(panel.root.destroy)
        panel.buttons["STOP"].invoke()
        self.assertEqual(called, ["cancel"])


if __name__ == "__main__":
    unittest.main()
