"""Offline tests for the A/B benchmark harness (benchmarks/ab). No input, no network, no desktop."""
from __future__ import annotations

import asyncio
import csv
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.ab import agent, computer, environment, pricing, report, run, webfixture  # noqa: E402

TASKS = environment.load_tasks(ROOT / "benchmarks" / "ab" / "tasks.yaml")
BY_ID = {t["id"]: t for t in TASKS}


class FakeIO:
    """Records what the baseline executor would send to clickwarden.input."""

    _BUTTON_FLAGS = {"left": (2, 4)}

    def __init__(self):
        self.calls = []
        self.cursor = (100, 100)

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record

    def cursor_xy(self):
        return self.cursor

    def _mouse_input(self, *args):
        return args


def fake_grab(region):
    if region:
        left, top, right, bottom = region
        return Image.new("RGB", (right - left, bottom - top), "white")
    return Image.new("RGB", (1920, 1080), "white")


def desktop():
    return computer.Desktop(1280, 720, backend=FakeIO(), grabber=fake_grab, post_action_delay=0)


class TaskFileTests(unittest.TestCase):
    def test_task_list_is_valid_and_fixed(self):
        self.assertEqual(len(TASKS), 12)
        self.assertEqual(len({t["id"] for t in TASKS}), 12)
        self.assertEqual({t["difficulty"] for t in TASKS}, {"easy", "medium", "hard"})

    def test_every_browser_path_is_served(self):
        for task in TASKS:
            for item in (task.get("setup") or {}).get("open") or []:
                if "browser" in item:
                    self.assertIsNotNone(webfixture.render(item["browser"]), task["id"])
        for path in ("/pricing/student", "/form", "/toolbar", "/orders", "/redeem", "/thanks"):
            self.assertIsNotNone(webfixture.render(path))

    def test_targets_exist_on_their_pages(self):
        self.assertIn(b"report('archive')", webfixture.render("/toolbar"))
        orders = webfixture.render("/orders")
        for oid in (b"5521", b"5512", b"5251"):
            self.assertIn(b"Confirm order #" + oid, orders)
        self.assertIn(b"Azerbaijan", webfixture.render("/form"))

    def test_bad_task_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "t.yaml"
            bad.write_text("tasks:\n  - {id: a, prompt: p, difficulty: easy, category: c, check: {type: nope}}\n")
            with self.assertRaises(ValueError):
                environment.load_tasks(bad)


class EnvironmentCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = webfixture.WebFixture()  # not started: receipts are recorded directly
        self.env = environment.Environment(TASKS, web=self.web, root=Path(self.tmp.name) / "ClickwardenBench",
                                           live=False)

    def tearDown(self):
        self.env.teardown()
        self.tmp.cleanup()

    def test_refuses_foreign_folder(self):
        with self.assertRaises(ValueError):
            environment.Environment(TASKS, root=Path(self.tmp.name) / "Documents", live=False)

    def assert_fails_then_passes(self, task_id, solve):
        task = BY_ID[task_id]
        self.env.setup(task)
        ok, _ = self.env.check(task)
        self.assertFalse(ok, f"{task_id} passes before anything was done")
        solve(self.env.root)
        ok, reason = self.env.check(task)
        self.assertTrue(ok, f"{task_id}: {reason}")

    def test_notepad_save(self):
        self.assert_fails_then_passes("t01_notepad_save", lambda r: (r / "note.txt").write_text(
            "Clickwarden benchmark 4417\r\n", encoding="utf-8"))

    def test_notepad_wrong_text_fails(self):
        task = BY_ID["t01_notepad_save"]
        self.env.setup(task)
        (self.env.root / "note.txt").write_text("Clickwarden benchmark 441", encoding="utf-8")
        ok, reason = self.env.check(task)
        self.assertFalse(ok)
        self.assertIn("441", reason)

    def test_rename(self):
        self.assert_fails_then_passes("t02_explorer_rename", lambda r: (r / "rename/draft.txt").rename(
            r / "rename/final_report.txt"))

    def test_new_folder(self):
        self.assert_fails_then_passes("t03_explorer_new_folder", lambda r: (r / "folders/Invoices_2026").mkdir())

    def test_move(self):
        self.assert_fails_then_passes("t04_explorer_move", lambda r: (r / "move/b.txt").rename(r / "move/archive/b.txt"))

    def test_move_by_copy_fails(self):
        task = BY_ID["t04_explorer_move"]
        self.env.setup(task)
        (self.env.root / "move/archive/b.txt").write_text("file b\n", encoding="utf-8")
        self.assertFalse(self.env.check(task)[0])

    def test_readonly(self):
        self.assert_fails_then_passes("t05_file_readonly", lambda r: os.chmod(r / "props/report.txt", stat.S_IREAD))

    def test_teardown_removes_readonly_files(self):
        self.env.setup(BY_ID["t05_file_readonly"])
        os.chmod(self.env.root / "props/report.txt", stat.S_IREAD)
        self.env.teardown()
        self.assertFalse(self.env.root.exists())

    def test_web_visit(self):
        self.assert_fails_then_passes("t08_browser_navigate",
                                      lambda r: self.web.record("visit", path="/pricing/student"))

    def test_form_needs_exactly_one_correct_submission(self):
        good = {"name": "Test User", "email": "test.user@example.com", "country": "Azerbaijan", "agree": "yes"}
        self.assert_fails_then_passes("t09_web_form", lambda r: self.web.record("submit", path="/form", fields=good))
        self.web.record("submit", path="/form", fields=good)
        ok, reason = self.env.check(BY_ID["t09_web_form"])
        self.assertFalse(ok)
        self.assertIn("2 submissions", reason)

    def test_form_without_agree_fails(self):
        task = BY_ID["t09_web_form"]
        self.env.setup(task)
        self.web.record("submit", path="/form", fields={"name": "Test User", "email": "test.user@example.com",
                                                        "country": "Azerbaijan"})
        self.assertFalse(self.env.check(task)[0])

    def test_small_icon_wrong_click_fails(self):
        task = BY_ID["t10_small_icon"]
        self.env.setup(task)
        self.web.record("click", target="delete")
        self.web.record("click", target="archive")
        ok, reason = self.env.check(task)
        self.assertFalse(ok)
        self.assertIn("delete", reason)

    def test_scroll_find(self):
        self.assert_fails_then_passes("t11_scroll_find", lambda r: self.web.record("click", target="confirm-5521"))

    def test_receipts_before_setup_do_not_count(self):
        self.web.record("click", target="confirm-5521")
        task = BY_ID["t11_scroll_find"]
        self.env.setup(task)
        self.assertFalse(self.env.check(task)[0])

    def test_redeem(self):
        self.assert_fails_then_passes("t12_notepad_to_browser",
                                      lambda r: self.web.record("submit", path="/redeem", fields={"code": "K7-Q2-93"}))

    def test_prompt_substitutes_bench_folder(self):
        prompt = self.env.prompt(BY_ID["t02_explorer_rename"])
        self.assertIn(str(self.env.root), prompt)
        self.assertNotIn("{bench}", prompt)


class KeyAndDesktopTests(unittest.TestCase):
    def test_chords(self):
        self.assertEqual(computer.chord("ctrl+s"), ["ctrl", "s"])
        self.assertEqual(computer.chord("Return"), ["enter"])
        self.assertEqual(computer.chord("super"), ["win"])
        self.assertEqual(computer.chord("alt+Tab"), ["alt", "tab"])
        self.assertEqual(computer.chord("F5"), ["f5"])
        self.assertEqual(computer.key_sequence("ctrl+a Delete"), [["ctrl", "a"], ["delete"]])
        with self.assertRaises(computer.ActionError):
            computer.chord("Hyper_Z")

    def test_coordinates_scale_to_physical_pixels(self):
        d = desktop()
        self.assertEqual((d.width, d.height), (1280, 720))
        d.execute("left_click", {"coordinate": [640, 360]})
        self.assertEqual(d.io.calls[-1], ("click", (960, 540), {"button": "left", "clicks": 1}))
        self.assertEqual(d.to_shot((960, 540)), (640, 360))

    def test_click_modifiers_are_released(self):
        d = desktop()
        d.execute("left_click", {"coordinate": [10, 10], "text": "ctrl+shift"})
        names = [c[0] for c in d.io.calls]
        self.assertEqual(names, ["key_down", "key_down", "click", "key_up", "key_up"])
        self.assertEqual([c[1][0] for c in d.io.calls if c[0] == "key_up"], ["shift", "ctrl"])

    def test_out_of_bounds_coordinate_is_refused(self):
        with self.assertRaises(computer.ActionError):
            desktop().execute("left_click", {"coordinate": [1281, 10]})

    def test_scroll_directions(self):
        d = desktop()
        d.execute("scroll", {"scroll_direction": "down", "scroll_amount": 5, "coordinate": [100, 100]})
        self.assertEqual(d.io.calls[-1], ("scroll", (-5, 150, 150), {}))
        d.execute("scroll", {"scroll_direction": "right", "scroll_amount": 2})
        self.assertIn(("key_down", ("shift",), {}), d.io.calls)

    def test_screenshot_and_zoom_fit_the_screenshot_size(self):
        d = desktop()
        shot = d.execute("screenshot", {})[0]
        self.assertEqual(shot["type"], "image")
        zoom = d.execute("zoom", {"region": [0, 0, 320, 90]})[0]
        self.assertEqual(zoom["source"]["media_type"], "image/png")
        self.assertEqual(d.screenshots, 2)

    def test_batch_stops_at_first_failure_and_answers_every_call(self):
        blocks = [SimpleNamespace(id="a", name="left_click", input={"coordinate": [5, 5]}),
                  SimpleNamespace(id="b", name="left_click", input={"coordinate": [9999, 5]}),
                  SimpleNamespace(id="c", name="type", input={"text": "never"})]
        results, executed = computer.run_batch(desktop(), blocks)
        self.assertEqual([r["tool_use_id"] for r in results], ["a", "b", "c"])
        self.assertTrue(all(r["toolset_name"] == "computer" for r in results))
        self.assertTrue(results[1]["is_error"])
        self.assertEqual(results[2]["content"], computer.NOT_EXECUTED)
        self.assertEqual(executed, 2)


class PricingTests(unittest.TestCase):
    def test_usage_sums_and_cost(self):
        u = pricing.HeavyUsage()
        u.add(SimpleNamespace(input_tokens=1000, cache_creation_input_tokens=4000, cache_read_input_tokens=0,
                              output_tokens=200))
        u.add(SimpleNamespace(input_tokens=500, cache_creation_input_tokens=1500, cache_read_input_tokens=4000,
                              output_tokens=None))
        self.assertEqual((u.input, u.cache_write, u.cache_read, u.output, u.requests), (1500, 5500, 4000, 200, 2))
        self.assertEqual(u.total_input, 11000)
        expected = (1500 * 4 + 5500 * 5 + 4000 * 0.2 + 200 * 20) / 1e6
        self.assertAlmostEqual(pricing.heavy_cost(u, "claude-opus-5-5"), expected)
        self.assertEqual(pricing.vision_cost(1_000_000, 0, 0.0, 0.0), 0.0)

    def test_unknown_model_has_no_silent_price(self):
        with self.assertRaises(KeyError):
            pricing.heavy_cost(pricing.HeavyUsage(), "some-other-model")


def usage(i=100, w=0, r=0, o=20):
    return SimpleNamespace(input_tokens=i, cache_creation_input_tokens=w, cache_read_input_tokens=r, output_tokens=o)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def run_loop(self, responses, cfg=None):
        client = SimpleNamespace(messages=FakeMessages(responses))
        executed = []

        async def execute(tool_uses, deadline):
            executed.extend(b.name for b in tool_uses)
            return [{"type": "tool_result", "tool_use_id": b.id, "content": "OK"} for b in tool_uses], len(tool_uses)

        out = asyncio.run(agent.agent_loop(client, cfg or agent.Config(), "system", [], "do it", execute,
                                           threading.Event()))
        return out, client.messages, executed

    def test_usage_steps_and_done_claim(self):
        call = SimpleNamespace(type="tool_use", id="t1", name="screenshot", input={}, toolset_name="computer")
        out, messages, executed = self.run_loop([
            SimpleNamespace(content=[call], stop_reason="tool_use", usage=usage(100, 5000, 0, 50)),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="DONE saved the file")],
                            stop_reason="end_turn", usage=usage(80, 300, 5000, 30))])
        self.assertEqual(out.stop, "end_turn")
        self.assertTrue(out.claimed_done)
        self.assertEqual(out.steps, 1)
        self.assertEqual(out.heavy.requests, 2)
        self.assertEqual(out.heavy.total_input, 100 + 5000 + 80 + 300 + 5000)
        self.assertEqual(executed, ["screenshot"])
        sent = messages.requests[0]
        self.assertEqual(sent["cache_control"], {"type": "ephemeral"})
        self.assertEqual(sent["output_config"], {"effort": "medium"})

    def test_turn_limit(self):
        call = SimpleNamespace(type="tool_use", id="t", name="wait", input={}, toolset_name="computer")
        responses = [SimpleNamespace(content=[call], stop_reason="tool_use", usage=usage()) for _ in range(3)]
        out, _, _ = self.run_loop(responses, agent.Config(max_turns=3))
        self.assertEqual(out.stop, "max_turns")
        self.assertFalse(out.claimed_done)

    def test_refusal_and_api_error_are_failures_not_crashes(self):
        out, _, _ = self.run_loop([SimpleNamespace(content=[], stop_reason="refusal", usage=usage())])
        self.assertEqual(out.stop, "refusal")
        out, _, _ = self.run_loop([])  # pop from empty list -> harness error
        self.assertEqual(out.stop, "error")


class ClickwardenModeTests(unittest.TestCase):
    def test_excluded_tools_and_stable_order(self):
        tools = [SimpleNamespace(name=n, description="d", input_schema={"type": "object"})
                 for n in ("fast_type", "fast_job_start", "use_my_computer", "fast_click", "hud_status")]
        out = agent.mcp_tools_to_anthropic(tools)
        self.assertEqual([t["name"] for t in out], ["fast_click", "fast_type", "use_my_computer"])

    def test_vision_usage_sums_sessions_and_flags_missing(self):
        records = {"a": {"prompt_tokens": 900, "completion_tokens": 40, "requests": 2, "usage_complete": True}}
        ended = {"b": {"prompt_tokens": 600, "completion_tokens": 20, "usage_complete": True}}
        total = agent.collect_vision_usage(["a", "b"], ended, reader=records.get)
        self.assertEqual((total["prompt_tokens"], total["completion_tokens"], total["requests"]), (1500, 60, 2))
        self.assertTrue(total["usage_complete"])
        total = agent.collect_vision_usage(["a", "c"], {}, reader=records.get)
        self.assertFalse(total["usage_complete"])

    def test_skill_text_has_no_front_matter(self):
        text = agent.skill_text()
        self.assertFalse(text.startswith("---"))
        self.assertIn("fast_controls", text)


class ScheduleAndReportTests(unittest.TestCase):
    def test_schedule_is_counterbalanced(self):
        plan = run.schedule(TASKS, 3, run.MODES)
        self.assertEqual(len(plan), 12 * 3 * 2)
        firsts = [plan[i][2] for i in range(0, len(plan), 2)]
        self.assertEqual(firsts.count("baseline"), firsts.count("clickwarden"))
        for i in range(0, len(plan), 2):
            self.assertEqual({plan[i][2], plan[i + 1][2]}, set(run.MODES))
            self.assertEqual(plan[i][1]["id"], plan[i + 1][1]["id"])

    def test_report_from_synthetic_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = []
            for task in ("t01_notepad_save", "t10_small_icon"):
                for r in (1, 2, 3):
                    rows.append({"task_id": task, "mode": "baseline", "run": r, "success": 1, "heavy_in_tokens": 90000,
                                 "heavy_out_tokens": 2000, "vision_in_tokens": 0, "vision_out_tokens": 0,
                                 "total_cost_usd": 0.10, "heavy_cost_usd": 0.10, "vision_cost_usd": 0, "wall_time_s": 60,
                                 "steps": 12, "failure_reason": "", "stop": "end_turn", "claimed_done": 1,
                                 "false_success": 0, "vision_usage_complete": 1})
                    ok = not (task == "t10_small_icon" and r == 2)
                    rows.append({"task_id": task, "mode": "clickwarden", "run": r, "success": int(ok),
                                 "heavy_in_tokens": 8000, "heavy_out_tokens": 800, "vision_in_tokens": 1100,
                                 "vision_out_tokens": 100, "total_cost_usd": 0.01, "heavy_cost_usd": 0.01,
                                 "vision_cost_usd": 0, "wall_time_s": 40, "steps": 6,
                                 "failure_reason": "" if ok else "clicks received: ['delete']", "stop": "end_turn",
                                 "claimed_done": 1, "false_success": int(not ok), "vision_usage_complete": 1})
            with (out / "results.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=run.CSV_FIELDS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in run.CSV_FIELDS})
            meta = {"config": {"model": "claude-opus-5-5", "effort": "medium", "runs": 3, "max_turns": 30,
                               "timeout_s": 300, "baseline_screenshot": [1280, 720]},
                    "vision": {"model": "nvidia/nemotron-3.5-super-vl:free", "host": "openrouter.ai"},
                    "prices": {"vision_in": 0, "vision_out": 0, "heavy": pricing.HEAVY_PRICES["claude-opus-5-5"]},
                    "task_ids": ["t01_notepad_save", "t10_small_icon"]}
            (out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            path = report.write_report(out)
            text = path.read_text(encoding="utf-8")
            headline = (out / "headline.txt").read_text(encoding="utf-8")
        self.assertIn("5/6 successful runs through Clickwarden vs 6/6", headline)
        self.assertIn("10.0k vs 92.0k tokens per run (89% fewer", headline)
        self.assertIn("1.00¢ vs 10.00¢ per run (vision model on a free tier", headline)
        self.assertIn("clicks received: ['delete']", text)
        self.assertIn("| Through Clickwarden | 5/6 | 1/2 |", text)
        self.assertIn("False success claims", text)


if __name__ == "__main__":
    unittest.main()
