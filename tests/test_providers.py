"""Provider settings, consent gate, honest identity, coordinate spaces and the settings logic."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clickwarden import configure, mydata, providers, vision
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


class TimeoutAndFreeEndpointTests(IsolatedConfig):
    def test_a_slow_first_response_is_not_cut_off_at_eight_seconds(self):
        client = VisionClient(settings=ready(timeout=25))
        self.assertEqual(client.timeout, 25.0)
        response = Mock(status_code=200)
        response.iter_lines.return_value = iter([])
        with patch.object(client._session, "post", return_value=response) as post:
            client.stream_chat("sys", "q", b"jpeg")
        self.assertEqual(post.call_args.kwargs["timeout"], (10.0, 25.0))
        client.close()

    def test_silence_longer_than_the_timeout_ends_the_call_with_that_number(self):
        import itertools
        import time
        from types import SimpleNamespace
        client = VisionClient(settings=ready(timeout=3))
        response = Mock(status_code=200)
        response.iter_lines.return_value = iter([b": keep-alive"])
        ticks = itertools.count(0, 100)  # every look at the clock is 100 s after the last
        fake_time = SimpleNamespace(monotonic=lambda: next(ticks), perf_counter=time.perf_counter,
                                    sleep=lambda seconds: None)
        with patch.object(client._session, "post", side_effect=lambda *a, **k: response),                 patch("clickwarden.vision.time", fake_time):
            response.iter_lines.side_effect = lambda: iter([b": keep-alive"])
            result = client.stream_chat("sys", "q", b"jpeg")
        self.assertIn("3 seconds", result.error or "")
        client.close()

    def test_timeout_comes_from_config_or_env_and_is_clamped(self):
        providers.save_config({"base_url": URL, "model": "m", "timeout_s": 45})
        self.assertEqual(providers.resolve().timeout, 45.0)
        with patch.dict("os.environ", {"CLICKWARDEN_TIMEOUT": "9999"}):
            self.assertEqual(providers.resolve().timeout, 120.0)
        with patch.dict("os.environ", {"CLICKWARDEN_TIMEOUT": "0.1"}):
            self.assertEqual(providers.resolve().timeout, 3.0)
        with patch.dict("os.environ", {"CLICKWARDEN_TIMEOUT": "soon"}):
            self.assertEqual(providers.resolve().timeout, 20.0)
        self.assertEqual(Settings().timeout, 20.0)

    def test_connection_test_is_more_patient_than_normal_actions(self):
        seen = {}

        class Recorder:
            def __init__(self, settings, timeout):
                seen["timeout"] = timeout

            def stream_chat(self, *args, **kwargs):
                return StreamResult(content="OK")

            def close(self):
                pass
        with patch("clickwarden.vision.VisionClient", Recorder):
            self.assertTrue(providers.test_connection(ready(timeout=8))["ok"])
        self.assertEqual(seen["timeout"], 30.0)

    def test_free_and_trial_endpoints_get_a_logging_warning_everywhere(self):
        for settings in (ready(base_url="https://integrate.api.nvidia.com/v1/chat/completions"),
                         Settings(base_url="https://openrouter.ai/api/v1/chat/completions",
                                  model="nvidia/nemotron-3.5-super-vl:free", api_key="k")):
            info = providers.disclosure(settings)
            self.assertTrue(info["notices"], settings.host)
            self.assertIn("FREE or TRIAL", configure.consent_question(settings))
            self.assertIn("FREE or TRIAL", providers.disclosure_text(settings))
        self.assertEqual(providers.disclosure(ready())["notices"], [])

    def test_timeout_form_validation(self):
        values = ConfigureLogicTests.values(self, api_key="k")
        for bad in ("2", "500", "soon"):
            self.assertTrue(any("Timeout" in e for e in configure.form_to_settings({**values, "timeout": bad})[1]), bad)
        settings, errors = configure.form_to_settings({**values, "timeout": "40"})
        self.assertEqual((errors, settings.timeout), ([], 40.0))
        self.assertEqual(configure.form_to_settings({**values, "timeout": ""})[0].timeout, 20.0)


class ThinkingModelTests(IsolatedConfig):
    def run_with(self, settings, outcomes):
        client = VisionClient(settings=settings)
        sent = []

        def fake(payload, headers, on_partial, t0):
            sent.append(payload["max_tokens"])
            return outcomes[min(len(sent), len(outcomes)) - 1]  # the last outcome repeats
        with patch.object(client, "_stream_once", side_effect=fake):
            first = client.stream_chat("s", "u", b"x", max_tokens=160)
            second = client.stream_chat("s", "u", b"x", max_tokens=160)
        client.close()
        return client, first, second, sent

    def test_a_thinking_model_that_runs_out_of_budget_is_retried_with_more_room(self):
        outcomes = [StreamResult(content="", completion_tokens=160), StreamResult(content='{"ok":1}', completion_tokens=600),
                    StreamResult(content='{"ok":2}', completion_tokens=590)]
        client, first, second, sent = self.run_with(ready(), outcomes)
        self.assertTrue(first.ok and first.reasoning_budget_grown)
        self.assertEqual(sent, [160, 640, 640])  # grown once, remembered for the next call
        self.assertTrue(second.ok and not second.reasoning_budget_grown)

    def test_empty_replies_without_proof_of_truncation_are_not_retried(self):
        client, first, _, sent = self.run_with(ready(), [StreamResult(content=""), StreamResult(content="")])
        self.assertFalse(first.ok)
        self.assertEqual(set(sent), {160})  # ordinary retries only, the budget never grows

    def test_budget_growth_has_a_ceiling(self):
        outcomes = [StreamResult(content="", completion_tokens=4096)] * 2
        _, _, _, sent = self.run_with(ready(), outcomes)
        self.assertLessEqual(max(sent), vision.MAX_TOKENS_CEILING)

    def test_activity_that_is_not_content_does_not_count_as_silence(self):
        import itertools
        import time
        from types import SimpleNamespace
        client = VisionClient(settings=ready(timeout=10))
        lines = [b': thinking', b'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
                 b'data: {"choices":[{"delta":{"content":"OK"}}]}', b"data: [DONE]"]
        response = Mock(status_code=200)
        response.iter_lines.side_effect = lambda: iter(lines)
        ticks = itertools.count(0, 6)  # six seconds between lines: never more than the 10 s limit apart
        fake_time = SimpleNamespace(monotonic=lambda: next(ticks), perf_counter=time.perf_counter,
                                    sleep=lambda seconds: None)
        with patch.object(client._session, "post", return_value=response), patch("clickwarden.vision.time", fake_time):
            result = client.stream_chat("s", "u", b"x")
        self.assertTrue(result.ok, result.error)
        client.close()

    def test_extra_request_fields_reach_the_payload_but_cannot_replace_core_fields(self):
        body = providers.clean_extra_body({"reasoning": {"enabled": False}, "model": "evil", "messages": [],
                                           "max_tokens": 9, "x" * 65: 1})
        self.assertEqual(body, {"reasoning": {"enabled": False}})
        self.assertEqual(providers.clean_extra_body({"big": "x" * 3000}), {})
        self.assertEqual(providers.clean_extra_body(["no"]), {})
        client = VisionClient(settings=ready(extra_body=body))
        with patch.object(client, "_stream_once", return_value=StreamResult(content="ok")) as once:
            client.stream_chat("s", "u", b"x")
        payload = once.call_args.args[0]
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertNotEqual(payload["model"], "evil")
        client.close()

    def test_extra_fields_come_from_config_and_env_and_validate_in_the_form(self):
        providers.save_config({"base_url": URL, "model": "m", "extra_body": {"reasoning": {"effort": "none"}}})
        self.assertEqual(providers.resolve().extra_body, {"reasoning": {"effort": "none"}})
        with patch.dict("os.environ", {"CLICKWARDEN_EXTRA_BODY": '{"reasoning": {"enabled": false}}'}):
            self.assertEqual(providers.resolve().extra_body["reasoning"], {"enabled": False})
        values = ConfigureLogicTests.values(self, api_key="k")
        good = configure.form_to_settings({**values, "extra_body": '{"reasoning": {"enabled": false}}'})
        self.assertEqual((good[1], good[0].extra_body), ([], {"reasoning": {"enabled": False}}))
        for bad in ("{oops", '["list"]', '{"model": "x"}', "{}"):
            self.assertTrue(any("Extra request fields" in e for e in
                                configure.form_to_settings({**values, "extra_body": bad})[1]), bad)


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

    def test_remote_save_requires_accepting_the_data_flow(self):
        errors = configure.save_form(self.values(api_key="k"), accepted=False)
        self.assertTrue(any("accept" in e and "vision.example.com" in e for e in errors))
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

    def test_single_value_boxes_refuse_code_samples_and_huge_pastes(self):
        accepts = configure.entry_accepts
        self.assertTrue(accepts("nvapi-AbC123_def-456", configure.KEY_MAX))
        self.assertTrue(accepts("nvapi-key\n", configure.KEY_MAX))      # a copy button's trailing newline
        self.assertTrue(accepts("  nvapi-key  ", configure.KEY_MAX))
        self.assertTrue(accepts("", configure.KEY_MAX))
        self.assertFalse(accepts("import os\nclient = OpenAI(api_key=os.environ['K'])", configure.KEY_MAX))
        self.assertFalse(accepts("two words", configure.KEY_MAX))
        self.assertFalse(accepts("a" * (configure.KEY_MAX + 1), configure.KEY_MAX))
        self.assertFalse(accepts("line1\nline2", configure.URL_MAX))

    def test_pasting_a_sample_script_into_the_key_box_is_refused_instantly(self):
        import base64
        import os
        import time
        import tkinter
        from tkinter import ttk
        blob = ("from openai import OpenAI\nclient = OpenAI(api_key=os.environ[\"NVIDIA_API_KEY\"])\n"
                "x = \"data:image/jpeg;base64," + base64.b64encode(os.urandom(9000)).decode() + "\"\n")
        seen = {}

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        def fake_mainloop(window):
            window.update()
            key_entry = next(w for w in walk(window) if isinstance(w, ttk.Entry) and str(w.cget("show")) == "•")
            key_entry.focus_force()
            window.clipboard_clear()
            window.clipboard_append(blob)
            started = time.monotonic()
            key_entry.event_generate("<<Paste>>")
            for _ in range(10):
                window.update()
            seen["seconds"], seen["held"] = time.monotonic() - started, key_entry.get()
            window.clipboard_clear()
            window.clipboard_append("nvapi-REALLY-A-KEY-123")
            key_entry.event_generate("<<Paste>>")
            window.update()
            seen["key"] = key_entry.get()
            window.destroy()
        with patch.object(tkinter.Tk, "mainloop", fake_mainloop):
            configure.launch()
        self.assertEqual(seen["held"], "")
        self.assertLess(seen["seconds"], 3)  # unfixed, a 12k-character paste held Tk for ~20 s
        self.assertEqual(seen["key"], "nvapi-REALLY-A-KEY-123")

    def test_app_icon_is_a_real_multi_size_ico_and_the_window_uses_it(self):
        from PIL import Image
        icon = configure.app_icon_path()
        self.assertIsNotNone(icon)
        with Image.open(icon) as image:
            self.assertGreaterEqual(set(image.info["sizes"]), {(16, 16), (32, 32), (48, 48), (256, 256)})
        import ctypes
        import tkinter
        seen = {}

        def window_icon(window):
            window.update()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
            return ctypes.windll.user32.SendMessageW(hwnd, 0x007F, 1, 0)  # WM_GETICON, ICON_BIG

        plain = tkinter.Tk()
        plain.withdraw()
        control = window_icon(plain)
        plain.destroy()

        def fake_mainloop(window):
            seen["icon"] = window_icon(window)
            window.destroy()
        with patch.object(tkinter.Tk, "mainloop", fake_mainloop),                 patch.object(configure, "claim_taskbar_identity") as claim:
            configure.launch()
        claim.assert_called_once()
        self.assertEqual(control, 0)          # a stock Tk window has no icon of its own (shows the Python one)
        self.assertNotEqual(seen["icon"], 0)  # the settings window carries the Clickwarden icon

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
    def test_confirmation_dialog_names_the_host_the_data_and_the_risks(self):
        text = configure.consent_question(ready())
        self.assertIn("send screenshots of your screen to vision.example.com", text)
        self.assertIn("What is sent:", text)
        self.assertIn("What can go wrong:", text)
        self.assertIn("Choose No", text)
        self.assertIn("Revoke consent", text)

    def test_saving_a_cloud_provider_asks_and_declining_saves_nothing(self):
        import tkinter
        from tkinter import messagebox, ttk
        calls = []

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        def drive(answer):
            def fake_mainloop(window):
                window.update()
                widgets = list(walk(window))
                combo = next(w for w in widgets if isinstance(w, ttk.Combobox))
                buttons = {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)}
                combo.set(providers.PRESETS["openrouter"]["label"])
                combo.event_generate("<<ComboboxSelected>>")
                window.update()
                entries = [w for w in widgets if isinstance(w, ttk.Entry) and str(w.cget("show")) == "\u2022"]
                entries[0].insert(0, "sk-test-key")
                with patch.object(messagebox, "askyesno", side_effect=lambda *a, **k: calls.append((a, k)) or answer), \
                        patch.object(messagebox, "showinfo"):
                    buttons["Save"].invoke()
                window.update()
                try:
                    window.destroy()
                except tkinter.TclError:
                    pass
            return fake_mainloop

        with patch.object(tkinter.Tk, "mainloop", drive(False)):
            configure.launch()
        self.assertEqual(providers.load_config(), {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("default"), "no")
        self.assertIn("openrouter.ai", calls[0][0][0])
        with patch.object(tkinter.Tk, "mainloop", drive(True)):
            configure.launch()
        self.assertEqual(providers.load_config()["consent"]["host"], "openrouter.ai")
        self.assertEqual(providers.resolve().problem, "")

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


class MyDataTests(IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        root = self.home / ".clickwarden"
        for name in ("sessions", "jobs", "layouts", "shots"):
            (root / name).mkdir(parents=True)
            (root / name / "a.txt").write_text("x" * 10)
        (self.home / "Desktop").mkdir()
        (self.home / "Desktop" / "Clickwarden.lnk").write_bytes(b"lnk")
        providers.save_config({"model": "m"})
        (providers.config_dir() / "key.dpapi").write_text("blob")

    def test_inventory_lists_every_stored_part_with_sizes(self):
        rows = {r["name"]: r for r in mydata.inventory(self.home)}
        self.assertTrue(all(r["exists"] for r in rows.values()), rows)
        self.assertEqual(rows["shots"]["files"], 1)
        self.assertIn("screen", mydata.ITEMS["shots"][1])

    def test_wipe_everything_keeps_the_shortcut_unless_asked(self):
        result = mydata.wipe_everything(False, self.home)
        self.assertEqual(result["errors"], {})
        rows = {r["name"]: r for r in mydata.inventory(self.home)}
        self.assertEqual([n for n, r in rows.items() if r["exists"]], ["shortcut"])
        self.assertFalse(providers.config_dir().exists())  # empty folders are tidied away
        self.assertFalse((self.home / ".clickwarden").exists())
        mydata.wipe_everything(True, self.home)
        self.assertFalse((self.home / "Desktop" / "Clickwarden.lnk").exists())
        self.assertTrue((self.home / "Desktop").exists())

    def test_deleting_one_item_leaves_the_rest_and_missing_is_not_an_error(self):
        result = mydata.delete(["key", "key"], self.home)
        self.assertEqual((result["deleted"], result["missing"], result["errors"]), (["key"], ["key"], {}))
        self.assertEqual(providers.load_key(), "")
        self.assertTrue((providers.config_dir() / "config.json").exists())
        self.assertEqual(mydata.delete(["../../etc"], self.home)["errors"], {"../../etc": "unknown item"})

    def test_links_are_removed_but_their_targets_are_never_followed(self):
        target = self.home / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        shutil.rmtree(self.home / ".clickwarden" / "shots")
        link = self.home / ".clickwarden" / "shots"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        self.assertEqual(mydata.inventory(self.home)[[r["name"] for r in mydata.inventory(self.home)].index("shots")]["files"], 0)
        mydata.delete(["shots"], self.home)
        self.assertTrue((target / "keep.txt").exists())
        self.assertFalse(link.exists())

    def test_cli_where_never_prints_the_key(self):
        (providers.config_dir() / "key.dpapi").write_text("sk-secret-value")
        text = json.dumps(mydata.inventory(self.home))
        self.assertNotIn("sk-secret-value", text)


class SkillDocsTests(unittest.TestCase):
    def test_skill_and_connect_guide_only_name_real_tools(self):
        tools = set(re.findall(r"^## `(\w+)\(", (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8"), re.M))
        self.assertIn("fast_click", tools)
        for path in ("skills/clickwarden/SKILL.md", "docs/CONNECT.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            named = set(re.findall(r"`((?:fast_|use_my)\w+)`", text))
            self.assertEqual(sorted(n for n in named if n not in tools and n != "use_my_computer"), [], path)
        skill = (ROOT / "skills" / "clickwarden" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: clickwarden\n"))
