import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from clickwarden.control import Control, Cancelled, Paused, BudgetExceeded, PhysicalInputWatch
from clickwarden.hud import Hud
from clickwarden import input as inp
from clickwarden.jobs import JobManager, atomic_json, public_status, read_json
from clickwarden.vision import VisionClient
from clickwarden.worker import Runner, Attention


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / ("a" * 32)
        self.spec = {"job_id": "a" * 32, "goal": "test", "target": {"hwnd": 123},
                     "max_steps": 10, "max_seconds": 60, "max_requests": 5, "completion": {}}
        atomic_json(self.folder / "spec.json", self.spec)
        atomic_json(self.folder / "state.json", {"job_id": "a" * 32, "status": "starting",
                    "steps": 0, "requests": 0, "updated_at": time.time()})
        client = Mock(total_requests=0, prompt_tokens=0, completion_tokens=0, usage_reports=0)
        self.runner = Runner(self.folder, client=client, engine=Mock())

    def tearDown(self):
        self.temp.cleanup()

    def test_cancel_interrupts_slow_vision_without_dispatch(self):
        release = threading.Event()
        finished = threading.Event()
        errors = []
        def call():
            try:
                self.runner.call_vision(lambda: (release.wait(3), {"success": True})[1])
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(.06)
        t = time.monotonic()
        self.runner.control.cancel()
        self.assertTrue(finished.wait(.4))
        self.assertLess(time.monotonic() - t, .4)
        self.assertIsInstance(errors[0], Cancelled)
        release.set()
        thread.join()

    def test_pause_resume_invalidates_pending_plan(self):
        release = threading.Event()
        errors = []
        def call():
            try:
                self.runner.call_vision(lambda: (release.wait(3), {"success": True})[1])
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(.06)
        self.runner.control.pause("takeover")
        self.runner.control.resume()
        thread.join(.4)
        release.set()
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], Paused)

    def test_request_budget_counts_prior_attempts(self):
        self.runner.base_usage["requests"] = 4
        self.runner.client.total_requests = 1
        with self.assertRaises(BudgetExceeded):
            self.runner.before_request()

    def test_completion_requires_new_or_modified_file(self):
        path = self.root / "output.txt"
        path.write_text("expected", encoding="utf-8")
        self.runner.spec["completion"] = {"path": str(path), "before_mtime_ns": path.stat().st_mtime_ns,
                                           "expected_text": "expected"}
        with self.assertRaises(Attention):
            self.runner.verify_completion(None)
        self.runner.spec["completion"]["before_mtime_ns"] = None
        self.assertEqual(self.runner.verify_completion(None), "file_checked")
        self.runner.spec["completion"]["expected_text"] = "missing"
        with self.assertRaises(Attention):
            self.runner.verify_completion(None)

    def test_text_status_excludes_logs_images_and_goals(self):
        status = public_status({"job_id": "a", "status": "running", "log": ["long"],
                                "jpeg": b"secret", "file_path": "shot.jpg", "goal": "private"})
        self.assertEqual(status, {"job_id": "a", "status": "running"})

    def test_job_ids_cannot_escape_storage(self):
        with self.assertRaises(ValueError):
            JobManager(self.root).status("../other")

    def test_dead_worker_is_interrupted_not_completed(self):
        state = read_json(self.folder / "state.json")
        state["updated_at"] = 0
        atomic_json(self.folder / "state.json", state)
        with patch.object(JobManager, "alive", return_value=False):
            self.assertEqual(JobManager(self.root).status("a" * 32)["status"], "interrupted")

    def test_cancel_writes_control_message_without_waiting_for_worker(self):
        manager = JobManager(self.root)
        with patch.object(manager, "alive", return_value=True):
            result = manager.command("a" * 32, "cancel")
        self.assertEqual(result["status"], "cancel_requested")
        self.assertEqual(read_json(self.folder / "command.json")["command"], "cancel")

    def test_old_checkpoint_preserves_usage_on_resume(self):
        state = read_json(self.folder / "state.json")
        state.update(requests=3, prompt_tokens=100, completion_tokens=20)
        atomic_json(self.folder / "state.json", state)
        runner = Runner(self.folder, client=self.runner.client, engine=Mock())
        runner.client.total_requests = 1
        runner.client.prompt_tokens = 50
        runner.save()
        self.assertEqual(runner.state["requests"], 4)
        self.assertEqual(runner.state["prompt_tokens"], 150)


class ControlTests(unittest.TestCase):
    def test_physical_input_pauses_but_injected_input_does_not(self):
        control = Control(60)
        watch = PhysicalInputWatch(control)
        watch.enabled = True
        watch.mouse_event(1)
        watch.keyboard_event(0x10, 65, 0x100)
        self.assertFalse(control.paused.is_set())
        watch.mouse_event(0)
        self.assertTrue(control.paused.is_set())
        control.resume()
        watch.keyboard_event(0, 65, 0x100)
        self.assertTrue(control.paused.is_set())

    def test_emergency_hotkey_cancels_even_while_paused(self):
        control = Control(60)
        watch = PhysicalInputWatch(control)
        with patch("clickwarden.control.user32.GetAsyncKeyState", return_value=0x8000):
            watch.keyboard_event(0, 0x7B, 0x100)
        self.assertTrue(control.cancelled.is_set())

    def test_input_cancel_blocks_down_but_allows_release(self):
        def cancel():
            raise Cancelled()
        with patch.object(inp, "SendInput", return_value=1) as send, inp.dispatch_guard(cancel):
            with self.assertRaises(Cancelled):
                inp._send(inp._vk_input(0x11))
            inp._send(inp._vk_input(0x11, up=True))
        self.assertEqual(send.call_count, 1)

    def test_time_budget_includes_pauses(self):
        c = Control(0)
        c.pause()
        with self.assertRaises(BudgetExceeded):
            c.check()

    def test_no_idle_provider_requests(self):
        client = VisionClient(api_key="test")
        with patch.object(client, "prewarm") as warm:
            client.start_prewarmer(.001)
            warm.assert_not_called()
        self.assertIsNone(client._prewarm_thread)
        client.close()

    def test_provider_usage_event_without_choices_is_counted(self):
        client = VisionClient(api_key="test")
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
            b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3}}',
            b'data: [DONE]']
        with patch.object(client._session, "post", return_value=response):
            result = client._stream_once({}, {}, None, time.perf_counter())
        self.assertEqual((result.content, client.prompt_tokens, client.completion_tokens), ("ok", 12, 3))
        self.assertEqual(client.total_requests, 1)
        response.close.assert_called_once()
        client.close()

    def test_screenshot_tool_is_absent_in_default_mcp(self):
        server = importlib.import_module("clickwarden.server")
        names = [t.name for t in asyncio.run(server.app.list_tools())]
        self.assertNotIn("fast_screenshot", names)
        self.assertIn("fast_job_cancel", names)


class HudTests(unittest.TestCase):
    def test_artificial_pointer_eases_between_targets(self):
        hud = Hud()
        hud._state.pointer_from = (0, 0)
        hud._state.pointer_to = (100, 100)
        hud._state.pointer_started = 1
        self.assertEqual(hud._pointer_position(1), (0, 0))
        self.assertEqual(hud._pointer_position(1.03), (88, 88))
        self.assertEqual(hud._pointer_position(2), (100, 100))

    def test_cursor_blending_does_not_overflow_or_wrap(self):
        hud = Hud()
        buf = np.zeros((2, 2, 4), dtype=np.uint8)
        hud._blit(buf, np.ones((2, 2)) * 1.4, (0, 0), (255, 200, 100))
        self.assertEqual(buf[0, 0].tolist(), [255, 200, 100, 255])


if __name__ == "__main__":
    unittest.main()
