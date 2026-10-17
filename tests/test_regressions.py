"""Deterministic Windows tests: no real input, screenshots or provider requests."""
import importlib
import json
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from clickwarden import input as inp
from clickwarden import frames
from clickwarden.frames import Frame
from clickwarden.vision import VisionClient, StreamResult
from clickwarden.win32core import foreground_hwnd

engmod = importlib.import_module("clickwarden.engine")


def frame():
    return Frame(b"jpeg", -100, 20, 200, 100, 400, 200, "turbo", 80,
                 fg_hwnd=123, fg_title="test")


class InputTests(unittest.TestCase):
    def tearDown(self):
        inp.foreground_guard.disarm()

    def test_virtual_desktop_origin_and_flag(self):
        with patch.multiple(inp, _SCREEN_X=-1920, _SCREEN_Y=-1080,
                            _SCREEN_W=3840, _SCREEN_H=2160):
            self.assertEqual(inp._raw_abs_xy(-1920, -1080), (0, 0))
            self.assertEqual(inp._raw_abs_xy(1919, 1079), (65535, 65535))
            self.assertGreater(inp._raw_abs_xy(0, 0)[0], 32000)
        event = inp._mouse_input(0, 0, 0, inp.MOUSEEVENTF_ABSOLUTE)
        self.assertTrue(event.mi.dwFlags & inp.MOUSEEVENTF_VIRTUALDESK)

    def test_stale_dpi_ratio_resets(self):
        with patch.multiple(inp, _SCREEN_W=1920, _SCREEN_H=1080,
                            _RATIO_X=1.0, _RATIO_Y=1.0):
            inp.set_physical_screen(3840, 2160)
            self.assertEqual(inp._RATIO_X, 2)
            inp.set_physical_screen(1920, 1080)
            self.assertEqual((inp._RATIO_X, inp._RATIO_Y), (1, 1))

    def test_unicode_surrogates_in_both_typing_modes(self):
        for wpm in (0, 1000):
            with self.subTest(wpm=wpm), patch.object(inp, "_send") as send, \
                    patch.object(inp.time, "sleep"), patch.object(inp.failsafe, "check"):
                result = inp.type_text("A\U0001f600", wpm=wpm)
                events = [e for call in send.call_args_list for e in call.args]
                down = [e.ki.wScan for e in events if not e.ki.dwFlags & inp.KEYEVENTF_KEYUP]
                self.assertEqual(down, [65, 0xD83D, 0xDE00])
                self.assertEqual(result["typed"], 2)

    def test_bad_chord_does_not_press_ctrl(self):
        with patch.object(inp, "_send") as send:
            with self.assertRaises(ValueError):
                inp.hotkey("ctrl,not_a_key")
            send.assert_not_called()

    def test_drag_releases_button_on_abort(self):
        with patch.object(inp, "move_to"), patch.object(inp, "_send") as send, \
                patch.object(inp.failsafe, "check", side_effect=inp.CornerAbort()):
            with self.assertRaises(inp.CornerAbort):
                inp.drag(50, 50, 100, 100)
            self.assertEqual(send.call_args.args[0].mi.dwFlags, inp.MOUSEEVENTF_LEFTUP)

    def test_guard_rejects_another_window(self):
        inp.foreground_guard.arm(123)
        with patch("clickwarden.win32core.foreground_hwnd", return_value=456), \
                patch("clickwarden.win32core.window_title", return_value="other"):
            with self.assertRaises(inp.ForegroundGuardError):
                inp.foreground_guard.check()

    def test_null_foreground_is_zero(self):
        with patch("clickwarden.win32core.user32.GetForegroundWindow", return_value=None):
            self.assertEqual(foreground_hwnd(), 0)


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.client = VisionClient()

    def tearDown(self):
        self.client._session.close()

    def result(self, text):
        return patch.object(self.client, "stream_chat", return_value=StreamResult(content=text))

    def test_missing_or_invalid_grounding_is_not_origin(self):
        for response in ('{}', '[]', '{"point":null}', '{"point":[-1,0]}'):
            with self.subTest(response=response), self.result(response):
                self.assertFalse(self.client.locate(frame(), "target")["success"])

    def test_callback_waits_for_complete_validated_found_result(self):
        callback = Mock()

        def stream(*args, on_partial, **kwargs):
            on_partial('{"point":[10,2')
            callback.assert_not_called()
            on_partial('{"point":[10,25]')
            callback.assert_not_called()
            return StreamResult(content='{"status":"found","box":[0,15,20,35],"confidence":"high"}')

        with patch.object(self.client, "stream_chat", side_effect=stream):
            self.client.locate(frame(), "target", on_point=callback)
        callback.assert_called_once_with(-80, 70)

    def test_out_of_image_callback_never_fires(self):
        callback = Mock()

        def stream(*args, on_partial, **kwargs):
            on_partial('{"point":[999,25]}')
            return StreamResult(content='{"point":[999,25]}')

        with patch.object(self.client, "stream_chat", side_effect=stream):
            self.assertFalse(self.client.locate(frame(), "target", on_point=callback)["success"])
        callback.assert_not_called()

    def test_plan_rejects_invalid_click_points(self):
        for point in (None, [-1, 20], [200, 20], "12", [10]):
            with self.subTest(point=point), self.result(json.dumps({"action": "click", "point": point})):
                self.assertFalse(self.client.plan(frame(), "goal", [])["success"])

    def test_truncated_plan_is_never_salvaged_into_input(self):
        with self.result('{"action":"click","point":[12,34],"reason":"unfinished'):
            plan = self.client.plan(frame(), "goal", [])
        self.assertFalse(plan["success"])

    def test_plan_preserves_literal_backslashes(self):
        text = r"C:\new\test.txt"
        with self.result(json.dumps({"action": "type", "text": text})):
            self.assertEqual(self.client.plan(frame(), "goal", [])["text"], text)

    def test_thinking_field_is_sent_only_when_the_provider_opts_in(self):
        for opt_in, expected in ((False, None), (True, {"type": "disabled"})):
            client = VisionClient(api_key="test-key", api_url="https://provider.invalid/v1/chat/completions", model="m")
            client.settings.send_thinking_disabled = opt_in
            with patch.object(client, "_stream_once", return_value=StreamResult(content="ok")) as once:
                client.stream_chat("sys", "query", b"jpeg")
            self.assertEqual(once.call_args.args[0].get("thinking"), expected)
            client.close()

    def test_stream_closes_response_on_http_error(self):
        response = Mock(status_code=503, text="unavailable")
        with patch.object(self.client._session, "post", return_value=response):
            result = self.client._stream_once({}, {}, None, 0)
        self.assertIn("HTTP 503", result.error)
        response.close.assert_called_once()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = engmod.Engine(client=Mock(), hud=Mock())
        self.engine.safety = Mock()
        inp.foreground_guard.disarm()

    def tearDown(self):
        inp.foreground_guard.disarm()

    def test_verify_click_captures_with_quality_and_arms_failsafe(self):
        loc = {"success": True, "point": [50, 50], "box":[40,40,60,60], "fg_hwnd": 123}
        with patch.object(engmod, "capture", return_value=frame()) as capture, \
                patch.object(self.engine, "locate", return_value=loc), \
                patch.object(self.engine, "_dispatch_click"), \
                patch.object(engmod, "Stabilizer"), \
                patch.object(self.engine, "_changed_since", return_value=True):
            result = self.engine.smart_click("target", verify=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(capture.call_args.kwargs["quality"], 60)
        self.engine.safety.arm.assert_called_once()
        self.engine.safety.disarm.assert_called_once()

    def test_hud_and_failsafe_cleanup_on_error(self):
        with patch.object(self.engine, "locate", side_effect=RuntimeError("network")):
            with self.assertRaises(RuntimeError):
                self.engine.smart_click("target")
        self.engine.safety.disarm.assert_called_once()
        self.assertEqual(self.engine._hud.end.call_args.kwargs["color"], "red")

    def test_low_confidence_does_not_click(self):
        with patch.object(self.engine, "locate", return_value={
                "success": True, "confidence": "low", "point": [10, 20]}), \
                patch.object(self.engine, "_dispatch_click") as send:
            result = self.engine.smart_click("target")
        self.assertEqual(result["status"], "error")
        send.assert_not_called()

    def test_pinned_window_moved_during_grounding_does_not_click(self):
        with patch.object(engmod, "window_rect", side_effect=[(0, 0, 100, 100), (10, 0, 110, 100)]), \
                patch.object(inp.foreground_guard, "check"), \
                patch.object(self.engine, "locate", return_value={"success": True, "point": [50, 50], "box":[40,40,60,60]}), \
                patch.object(self.engine, "_dispatch_click") as send:
            result = self.engine.smart_click("target", expect_hwnd=123)
        self.assertEqual(result["status"], "error")
        send.assert_not_called()

    def test_task_rejects_focus_change_during_planning(self):
        self.engine.vision.plan.return_value = {"success": True, "action": "type", "text": "danger"}
        with patch.object(self.engine, "grab", return_value=frame()), \
                patch("clickwarden.win32core.foreground_hwnd", return_value=456), \
                patch("clickwarden.win32core.window_title", return_value="other"), \
                patch.object(engmod, "type_text") as send:
            result = self.engine.task("goal")
        self.assertEqual(result["status"], "aborted_focus")
        send.assert_not_called()

    def test_persistent_guard_is_respected_and_restored(self):
        self.engine.expect_foreground(123)
        with patch("clickwarden.win32core.foreground_hwnd", return_value=456), \
                patch("clickwarden.win32core.window_title", return_value="other"), \
                patch.object(engmod, "type_text") as send:
            with self.assertRaises(inp.ForegroundGuardError):
                self.engine.type("do not type")
        send.assert_not_called()
        self.assertEqual(inp.foreground_guard._hwnd, 123)

    def test_roi_is_intersected_not_shifted(self):
        with patch.object(frames, "virtual_screen", return_value=(0, 0, 1920, 1080)), \
                patch.object(frames, "_grab", return_value=Image.new("RGB", (50, 60))) as grab:
            frames.capture(roi=(-50, -40, 100, 100))
        grab.assert_called_once_with(0, 0, 50, 60)

    def test_offscreen_roi_fails_before_capture(self):
        with patch.object(frames, "virtual_screen", return_value=(0, 0, 1920, 1080)), \
                patch.object(frames, "_grab") as grab:
            with self.assertRaises(ValueError):
                frames.capture(roi=(3000, 0, 100, 100))
        grab.assert_not_called()


if __name__ == "__main__":
    unittest.main()
