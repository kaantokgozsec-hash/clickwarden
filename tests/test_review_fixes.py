"""Regression tests for the 2026-10-16 review fixes. Offline: no input, capture or provider calls."""
import ctypes
import importlib
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from clickwarden import config, frames, input as inp, jobs, targets, win32core
from clickwarden.frames import Frame, Stabilizer, diff_ratio
from clickwarden.grounding import validate_grounding
from clickwarden.vision import _extract_json, is_pass

engmod = importlib.import_module("clickwarden.engine")


def jpeg_frame(left=0, top=0, w=100, h=100, color="white"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="JPEG")
    return Frame(out.getvalue(), left, top, w, h, w, h, "turbo", 60, fg_hwnd=1)


class VerdictParsingTests(unittest.TestCase):
    def test_pass_variants_models_actually_send(self):
        for answer in ("PASS", "PASS.", "pass", "**PASS**", " Pass!\n", "`PASS`"):
            self.assertTrue(is_pass(answer), answer)

    def test_anything_else_fails_closed(self):
        for answer in ("", None, "FAIL", "Fail.", "PASS? FAIL", "It looks like PASS",
                       "PASSED", "PASS - but the dialog shows FAIL"):
            self.assertFalse(is_pass(answer), answer)


class JsonSalvageTests(unittest.TestCase):
    def test_preamble_and_fence_are_tolerated(self):
        body = '{"status":"found","box":[1,1,9,9],"confidence":"high"}'
        for text in (body, "```json\n" + body + "\n```", "Here you go: " + body + " Done.",
                     "```\n" + body + "\n```"):
            self.assertEqual(_extract_json(text)["status"], "found")

    def test_salvage_never_bypasses_schema(self):
        parsed = _extract_json('Sure! {"status":"absent","description":"no such button"}')
        self.assertFalse(validate_grounding(parsed, 100, 100)["success"])
        with self.assertRaises(ValueError):
            _extract_json("no json here")


class DiffRatioTests(unittest.TestCase):
    def test_rounding_row_difference_is_compared_not_total_change(self):
        a = np.zeros((131, 240), dtype=np.int16)
        b = np.zeros((132, 240), dtype=np.int16)
        self.assertEqual(diff_ratio(a, b), 0.0)

    def test_different_regions_still_count_as_changed(self):
        self.assertEqual(diff_ratio(np.zeros((131, 240)), np.zeros((135, 240))), 1.0)


class StabilizerTests(unittest.TestCase):
    def test_focus_change_mid_capture_does_not_raise(self):
        good = jpeg_frame()
        calls = iter([RuntimeError("foreground changed during capture; capture again"), good, good])
        def fake_capture(**_):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(frames, "capture", side_effect=fake_capture):
            waited = Stabilizer(poll_ms=1, cap_ms=500).wait()
        self.assertLess(waited, 500)


class ChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = engmod.Engine(client=Mock(), hud=Mock())
        self.engine.safety = Mock()
        inp.foreground_guard.disarm()

    def tearDown(self):
        inp.foreground_guard.disarm()

    def test_after_frame_uses_the_baseline_region(self):
        before = jpeg_frame(left=-50, top=20, w=300, h=200)
        with patch.object(engmod, "capture", return_value=jpeg_frame(-50, 20, 300, 200)) as capture:
            self.assertFalse(self.engine._changed_since(before))
        self.assertEqual(capture.call_args.kwargs["roi"], (-50, 20, 300, 200))

    def test_baseline_is_taken_after_grounding_and_of_the_grounded_region(self):
        order = []
        region = {"left": 10, "top": 20, "width": 300, "height": 200}
        loc = {"success": True, "confidence": "high", "point": [50, 50], "box": [0, 0, 200, 100],
               "target_size": [200, 100], "region": region, "fg_hwnd": 1}
        def locate(*_, **__):
            order.append("locate")
            return dict(loc)
        def capture(**kwargs):
            order.append(("capture", kwargs.get("roi")))
            return jpeg_frame(10, 20, 300, 200)
        with patch.object(self.engine, "locate", side_effect=locate), \
                patch.object(engmod, "capture", side_effect=capture), \
                patch.object(self.engine, "_dispatch_click", side_effect=lambda *a, **k: order.append("click")), \
                patch.object(engmod, "Stabilizer"):
            result = self.engine.smart_click("wide button", verify=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(order[0], "locate")
        self.assertEqual(order[1], ("capture", (10, 20, 300, 200)))
        self.assertEqual(order[2], "click")


class RegionResolutionTests(unittest.TestCase):
    def test_auto_roi_never_becomes_a_title_search(self):
        engine = engmod.Engine(client=Mock(), hud=Mock())
        with patch.object(engine, "window_roi", return_value=None), \
                patch.object(engmod, "enumerate_windows",
                             return_value=[{"title": "AutoCAD 2026", "bounds": {"x": 0, "y": 0, "width": 9, "height": 9}}]) as windows:
            self.assertEqual(engine._resolve_region("precision", "auto"), ("precision", None))
        windows.assert_not_called()


class EnvTests(unittest.TestCase):
    def test_prefixed_settings_are_read_and_blank_values_fall_back(self):
        with patch.dict(os.environ, {"CLICKWARDEN_TURBO_WIDTH": "1000"}):
            self.assertEqual(config.env_int("TURBO_WIDTH", 1280), 1000)
        with patch.dict(os.environ, {"CLICKWARDEN_TURBO_WIDTH": "  "}):
            self.assertEqual(config.env_int("TURBO_WIDTH", 1280), 1280)
        with patch.dict(os.environ, {"WINFLAST_TURBO_WIDTH": "900"}):
            self.assertEqual(config.env_int("TURBO_WIDTH", 1280), 1280)
        with patch.dict(os.environ, {"CLICKWARDEN_HUD": "off"}):
            self.assertFalse(config.env_flag("HUD", True))


class CloakedWindowTests(unittest.TestCase):
    def test_cloaked_windows_are_not_listed(self):
        def enum(callback, _):
            for hwnd in (11, 22):
                callback(hwnd, 0)
            return True
        def text(hwnd, buf, n):
            ctypes.memmove(buf, ctypes.create_unicode_buffer("Window"), 14)
            return 6
        with patch.object(win32core.user32, "EnumWindows", side_effect=enum), \
                patch.object(win32core.user32, "IsWindowVisible", return_value=True), \
                patch.object(win32core.user32, "GetWindowTextLengthW", return_value=6), \
                patch.object(win32core.user32, "GetWindowTextW", side_effect=text), \
                patch.object(win32core, "window_rect", return_value=(0, 0, 500, 400)), \
                patch.object(win32core, "is_cloaked", side_effect=lambda h: h == 22):
            self.assertEqual([w["hwnd"] for w in win32core.enumerate_windows()], [11])

    def test_cloaked_window_above_target_is_not_masked(self):
        frame = jpeg_frame(w=100, h=100)
        family = Mock()
        family.contains.return_value = False
        with patch.object(targets.user32, "GetWindow", side_effect=[456, 0]), \
                patch.object(targets.user32, "IsWindowVisible", return_value=True), \
                patch.object(targets.user32, "IsIconic", return_value=False), \
                patch.object(targets, "is_cloaked", return_value=True), \
                patch("clickwarden.win32core.window_rect", return_value=(20, 20, 60, 60)):
            self.assertIs(targets.mask_foreign_windows(frame, 123, family), frame)


class FailsafeTests(unittest.TestCase):
    def test_disarmed_watcher_does_not_poll_and_armed_one_trips(self):
        watcher = inp.FailsafeWatcher(poll_ms=1)
        positions = Mock(return_value=(500, 500))
        with patch.object(inp, "cursor_xy", positions), \
                patch.object(inp, "virtual_screen", return_value=(0, 0, 1920, 1080)):
            watcher.start()
            try:
                time.sleep(0.08)
                self.assertEqual(positions.call_count, 0)
                positions.return_value = (1919, 0)
                watcher.arm()
                deadline = time.monotonic() + 1
                while not watcher.tripped and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(watcher.tripped)
            finally:
                watcher.stop()


class ReadJsonTests(unittest.TestCase):
    def test_transient_sharing_violation_is_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            jobs.atomic_json(path, {"ok": True})
            real = Path.read_text
            attempts = []
            def flaky(self, *args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError("sharing violation")
                return real(self, *args, **kwargs)
            with patch.object(Path, "read_text", flaky):
                self.assertEqual(jobs.read_json(path), {"ok": True})
            self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
