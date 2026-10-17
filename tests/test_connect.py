"""Setup checklist, agent-connection snippets, skill install and the safe self-check."""
import json
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from clickwarden import connect, providers

ROOT = Path(__file__).resolve().parents[1]
URL = "https://vision.example.com/v1/chat/completions"


class Isolated(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(shutil.rmtree, self.home, True)
        env = {"CLICKWARDEN_CONFIG_DIR": self.folder}
        for name in ("API_KEY", "API_URL", "VISION_MODEL", "ACCEPT_DATA_FLOW", "EXTRA_HEADERS", "COORD_SPACE",
                     "SEND_THINKING_DISABLED", "EXTRA_BODY", "TIMEOUT"):
            env["CLICKWARDEN_" + name] = ""
        patcher = patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def steps(self):
        return {s["id"]: s for s in connect.checklist()}


class ChecklistTests(Isolated):
    def test_fresh_install_has_nothing_done_and_agent_step_is_unknowable(self):
        steps = self.steps()
        self.assertEqual([steps[k]["done"] for k in ("provider", "key", "consent", "test")], [False] * 4)
        self.assertIsNone(steps["agent"]["done"])

    def test_remote_provider_needs_key_consent_and_a_passing_test(self):
        providers.save_config({"base_url": URL, "model": "m"})
        self.assertEqual([self.steps()[k]["done"] for k in ("provider", "key", "consent")], [True, False, False])
        providers.save_key("sk-test")
        config = providers.load_config()
        providers.record_consent(config, "vision.example.com")
        providers.save_config(config)
        self.assertEqual([self.steps()[k]["done"] for k in ("provider", "key", "consent", "test")],
                         [True, True, True, False])
        connect.record_check({"ok": True, "locate": {"latency_ms": 123}})
        self.assertTrue(self.steps()["test"]["done"])

    def test_local_provider_needs_no_key_or_consent(self):
        providers.save_config({"base_url": "http://localhost:11434/v1/chat/completions", "model": "llava"})
        steps = self.steps()
        self.assertTrue(steps["key"]["done"] and steps["consent"]["done"])
        self.assertIn("nothing leaves", steps["consent"]["detail"])

    def test_a_pass_for_another_model_or_host_does_not_count(self):
        providers.save_config({"base_url": URL, "model": "m"})
        connect.record_check({"ok": True})
        providers.save_config({**providers.load_config(), "model": "other"})
        self.assertFalse(self.steps()["test"]["done"])

    def test_a_failed_test_is_recorded_as_not_done(self):
        providers.save_config({"base_url": URL, "model": "m"})
        connect.record_check({"ok": False})
        self.assertFalse(self.steps()["test"]["done"])

    def test_checklist_never_contains_the_key(self):
        providers.save_config({"base_url": URL, "model": "m"})
        providers.save_key("sk-very-secret")
        self.assertNotIn("sk-very-secret", json.dumps(connect.checklist()))
        self.assertNotIn("sk-very-secret", json.dumps(providers.load_config()))


class SnippetTests(unittest.TestCase):
    LAUNCH = {"command": r"C:\Program Files\Python 3\python.exe",
              "args": [r"C:\My Tools\click'warden\clickwarden_mcp.py"], "needs_install": False}

    def test_all_three_snippets_round_trip_paths_with_spaces_and_quotes(self):
        text = connect.snippets(self.LAUNCH)
        parsed = tomllib.loads(text["codex"])["mcp_servers"]["clickwarden"]
        self.assertEqual((parsed["command"], parsed["args"]), (self.LAUNCH["command"], self.LAUNCH["args"]))
        desktop = json.loads(text["desktop"])["mcpServers"]["clickwarden"]
        self.assertEqual((desktop["command"], desktop["args"]), (self.LAUNCH["command"], self.LAUNCH["args"]))
        self.assertTrue(text["claude_code"].startswith("claude mcp add clickwarden -- "))
        self.assertIn('"C:\\Program Files\\Python 3\\python.exe"', text["claude_code"])

    def test_launch_uses_python_exe_not_pythonw_and_the_checkout_launcher(self):
        with patch.object(connect.sys, "executable", r"C:\Python\pythonw.exe"), \
                patch.object(connect.Path, "exists", return_value=True):
            self.assertEqual(Path(connect.mcp_launch()["command"]).name, "python.exe")
        launch = connect.mcp_launch()
        self.assertTrue(launch["args"][0].endswith("clickwarden_mcp.py"))
        self.assertFalse(launch["needs_install"])

    def test_without_a_checkout_launcher_it_asks_for_an_install(self):
        with patch.object(connect.Path, "is_file", return_value=False):
            launch = connect.mcp_launch()
        self.assertEqual(launch["args"], ["-m", "clickwarden.server"])
        self.assertTrue(launch["needs_install"])

    def test_verify_launch_reports_missing_dependencies_plainly(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nModuleNotFoundError: No module named 'mcp'")
        with patch.object(connect.subprocess, "run", return_value=failed):
            result = connect.verify_launch(SnippetTests.LAUNCH)
        self.assertFalse(result["ok"])
        self.assertIn("No module named 'mcp'", result["detail"])
        self.assertIn("pip install", result["detail"])
        with patch.object(connect.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")):
            self.assertTrue(connect.verify_launch(SnippetTests.LAUNCH)["ok"])

    def test_nothing_in_the_snippets_is_a_secret_or_a_data_flow_acceptance(self):
        joined = " ".join(connect.snippets().values()) + connect.FIRST_DEMO_PROMPT
        for forbidden in ("API_KEY", "ACCEPT_DATA_FLOW", "nvapi", "sk-"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("Do not choose providers", connect.FIRST_DEMO_PROMPT)
        self.assertIn("which provider will receive my screenshots", connect.FIRST_DEMO_PROMPT)


class SkillInstallTests(Isolated):
    def test_packaged_skill_matches_the_repo_copy_and_is_valid(self):
        repo_copy = (ROOT / "skills" / "clickwarden" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(connect.skill_source(), repo_copy)
        self.assertTrue(repo_copy.startswith("---\nname: clickwarden\n"))
        self.assertIn("never choose a provider", repo_copy.lower().replace("\n", " "))

    def test_install_writes_exactly_one_file_and_is_idempotent(self):
        self.assertEqual(connect.skill_status(self.home), "not installed")
        result = connect.install_skill(home=self.home)
        self.assertTrue(result["ok"], result)
        written = [p for p in self.home.rglob("*") if p.is_file()]
        self.assertEqual([p.name for p in written], ["SKILL.md"])
        self.assertEqual(connect.skill_status(self.home), "installed, up to date")
        self.assertTrue(connect.install_skill(home=self.home)["ok"])

    def test_a_different_existing_file_is_never_replaced_without_overwrite(self):
        target = connect.skill_target(self.home)
        target.parent.mkdir(parents=True)
        target.write_text("my own edits", encoding="utf-8")
        result = connect.install_skill(home=self.home)
        self.assertEqual((result["ok"], result.get("needs_overwrite")), (False, True))
        self.assertEqual(target.read_text(encoding="utf-8"), "my own edits")
        self.assertEqual(connect.skill_status(self.home), "installed, differs from this version")
        self.assertTrue(connect.install_skill(overwrite=True, home=self.home)["ok"])
        self.assertEqual(connect.skill_status(self.home), "installed, up to date")

    def test_it_never_writes_through_a_link(self):
        outside = self.home / "outside"
        outside.mkdir()
        skills = self.home / ".claude" / "skills"
        skills.mkdir(parents=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(skills / "clickwarden"), str(outside)], check=True,
                       capture_output=True)
        result = connect.install_skill(home=self.home)
        self.assertFalse(result["ok"])
        self.assertEqual(list(outside.iterdir()), [])


class SelfCheckTests(unittest.TestCase):
    def test_wrapper_and_module_exist_and_scripts_stays_thin(self):
        self.assertTrue((ROOT / "clickwarden" / "selfcheck.py").is_file())
        wrapper = (ROOT / "scripts" / "provider_check.py").read_text(encoding="utf-8")
        self.assertIn("clickwarden.selfcheck", wrapper)
        self.assertLess(len(wrapper.splitlines()), 20)

    def test_unconfigured_provider_reports_a_problem_without_opening_a_window(self):
        from clickwarden import selfcheck
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
                "CLICKWARDEN_CONFIG_DIR": folder, "CLICKWARDEN_API_KEY": "", "CLICKWARDEN_API_URL": "",
                "CLICKWARDEN_VISION_MODEL": ""}), patch.object(selfcheck.tk, "Tk") as window:
            report = selfcheck.run()
        self.assertFalse(report["ok"])
        self.assertTrue(report["problem"])
        window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
