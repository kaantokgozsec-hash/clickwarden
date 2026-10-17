"""Session acceptance checks without real input or provider requests."""
import io
import asyncio
import importlib
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import requests
from clickwarden.sessions import Session, SessionManager
from clickwarden import targets, input as inp
from clickwarden.engine import Engine
from clickwarden.frames import Frame
from clickwarden.vision import VisionClient, StreamResult
from clickwarden.control import Cancelled
from clickwarden.jobs import atomic_json, read_json


def make_session():
    with patch("clickwarden.sessions.Engine") as engine:
        session = Session(client=Mock(total_requests=0, prompt_tokens=0, completion_tokens=0, usage_reports=0))
    session.ready.set()
    session.prepared.set()
    session.save = Mock()
    return session


class SessionTests(unittest.TestCase):
    def test_mcp_session_protocol_and_roi_schema(self):
        server = importlib.import_module("clickwarden.server")
        tools = {t.name:t for t in asyncio.run(server.app.list_tools())}
        self.assertIn("FIRST", tools["use_my_computer"].description)
        self.assertIn("LAST", tools["fast_session_end"].description)
        for name in ("fast_click", "fast_locate", "fast_inspect", "fast_type", "fast_hotkey", "fast_batch", "fast_task"):
            tool = tools[name]
            schema = getattr(tool,"input_schema",getattr(tool,"inputSchema",{}))
            self.assertIn("session_id",schema["properties"])
        schema = getattr(tools["fast_click"],"input_schema",getattr(tools["fast_click"],"inputSchema",{}))
        self.assertEqual({v["type"] for v in schema["properties"]["roi"]["anyOf"]}, {"string","array"})

    def test_process_family_includes_siblings_but_not_launcher(self):
        tree = {10:(1,"app.exe"), 11:(10,"helper.exe"), 12:(10,"helper.exe"), 1:(0,"launcher.exe")}
        with patch.object(targets,"pid_of_window",side_effect=lambda hwnd:hwnd), patch.object(targets,"process_stamp",side_effect=lambda pid:100+pid), patch.object(targets,"process_parents",return_value=tree):
            family = targets.TargetFamily(10)
            family.refresh()
            self.assertTrue(family.contains(11))
            self.assertTrue(family.contains(12))
            self.assertFalse(family.contains(1))
            self.assertFalse(family.contains(99))

    def test_foreign_overlay_pixels_are_masked_without_moving_window(self):
        image=Image.new("RGB",(100,100),"white")
        output=io.BytesIO()
        image.save(output,format="JPEG")
        frame=Frame(output.getvalue(),0,0,100,100,100,100,"precision",85)
        family=Mock()
        family.contains.return_value=False
        with patch.object(targets.user32,"GetWindow",side_effect=[456,0]), patch.object(targets.user32,"IsWindowVisible",return_value=True), patch.object(targets.user32,"IsIconic",return_value=False), patch.object(targets.user32,"GetWindowDisplayAffinity",return_value=False), patch("clickwarden.win32core.window_rect",return_value=(20,20,60,60)):
            masked=targets.mask_foreign_windows(frame,123,family)
        pixels=Image.open(io.BytesIO(masked.jpeg))
        self.assertLess(max(pixels.getpixel((40,40))),50)
        self.assertGreater(min(pixels.getpixel((5,5))),240)
        self.assertEqual(masked.to_screen(30,30),(30,30))

    def test_checkpoint_retries_windows_reader_sharing_conflict(self):
        import os
        replace = os.replace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            atomic_json(path, {"old": True})
            attempts = []
            def busy_once(src, dst):
                attempts.append(1)
                if len(attempts) == 1:
                    self.assertEqual(read_json(path), {"old": True})
                    raise PermissionError("sharing violation")
                replace(src, dst)
            with patch("clickwarden.jobs.os.replace",side_effect=busy_once):
                atomic_json(path, {"new":True})
            self.assertEqual(read_json(path), {"new":True})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_cancel_keeps_session_open_and_stops_pending_vision(self):
        session = make_session()
        started, release = threading.Event(), threading.Event()
        caught = []
        def request():
            started.set()
            release.wait(2)
            return {"success": True}
        def run():
            try:
                session.vision_call(request)
            except Exception as exc:
                caught.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        started.wait(1)
        session.cancel()
        thread.join(.4)
        release.set()
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(caught[0], Cancelled)
        self.assertFalse(session.closed.is_set())
        self.assertFalse(session.stopping)
        self.assertEqual(session.snapshot()["panel"], "visible")

    def test_stop_closes_gate_before_panel_closes(self):
        session = make_session()
        session.stop("failed")
        self.assertFalse(session.closed.is_set())
        with self.assertRaises(Cancelled):
            session.check()

    def test_launch_never_happens_before_panel_ready(self):
        session = make_session()
        session.ready.clear()
        session.app_or_url = "steam"
        with patch("clickwarden.sessions.launch_target") as launch:
            session.prepare()
        launch.assert_not_called()
        self.assertIn("panel", session.error)

    def test_two_misses_escalate_and_no_third_click(self):
        session = make_session()
        session.engine.smart_click.return_value = {"status": "error"}
        session.escalate = Mock(return_value={"status": "needs_attention"})
        self.assertEqual(session.click_with_fallback({"description": "friends"})["status"], "retry_needed")
        session.click_with_fallback({"description": "friends"})
        session.click_with_fallback({"description": "friends"})
        self.assertEqual(session.engine.smart_click.call_count, 2)
        session.escalate.assert_called_once()

    def test_protocol_after_no_known_keyboard_path(self):
        session = make_session()
        session.family = Mock(exe="steam.exe")
        session.engine.inspect.return_value = {"answer": '{"keys":""}'}
        session.input_check = Mock()
        with patch("clickwarden.sessions.launch_target") as launch, patch("clickwarden.sessions.enumerate_windows", return_value=[{"title":"Friends List", "hwnd":12}]):
            result = session.escalate("friends", None)
        launch.assert_called_once_with("steam://open/friends")
        self.assertEqual(result["strategy"], "steam_protocol")

    def test_explicit_roi_formats_and_invalid_values(self):
        engine = Engine(hud=Mock())
        for roi in ("-20,30,240,240", [-20, 30, 240, 240], (-20, 30, 240, 240)):
            self.assertEqual(engine._resolve_region("turbo", roi), ("precision", (-20,30,240,240)))
        for roi in ("1,2,3", [1,2,-1,2], [True,2,3,4], "a,b,c,d"):
            with self.assertRaises(ValueError):
                engine._resolve_region("precision", roi)

    def test_small_icon_crop_maps_coordinates_within_five_pixels(self):
        image = Image.new("RGB", (240,240), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle((110,110,129,129), fill="red")
        output = io.BytesIO()
        image.save(output, format="JPEG")
        frame = Frame(output.getvalue(), 500, 200, 240, 240, 240, 240, "precision", 85)
        client = VisionClient(api_key="test")
        with patch.object(client, "stream_chat", return_value=StreamResult(content='{"status":"found","box":[110,110,130,130],"confidence":"high"}')):
            result = client.locate(frame, "20px red icon")
        self.assertEqual(result["point"], [620,320])  # tests transform, not model accuracy

    def test_occlusion_blocks_before_injection(self):
        family = Mock()
        family.contains.return_value = False
        with patch.object(targets.user32, "WindowFromPoint", return_value=456), patch.object(targets, "root_window", side_effect=lambda h:h), patch.object(inp, "_send") as send:
            with inp.dispatch_guard(lambda: None, lambda x,y: targets.assert_unoccluded(x,y,123,family)):
                with self.assertRaises(inp.ForegroundGuardError):
                    inp.click(20,30, human=False)
        send.assert_not_called()

    def test_zoom_retries_at_native_density_once(self):
        engine = Engine(hud=Mock())
        engine.safety = Mock()
        responses = [{"success":True,"point":[600,300],"box":[590,290,610,310],"confidence":"medium"},
                     {"success":True,"point":[603,302],"box":[593,292,613,312],"confidence":"high"}]
        with patch.object(engine, "locate", side_effect=responses) as locate, patch.object(engine,"_dispatch_click"), patch("clickwarden.engine.virtual_screen", return_value=(0,0,1920,1080)):
            result = engine.smart_click("friends icon")
        self.assertEqual(locate.call_count,2)
        self.assertEqual(locate.call_args.kwargs["roi"], (480,180,240,240))
        self.assertEqual(result["clicked_point"],[603,302])

    def test_stalled_stream_single_retry_and_read_timeout(self):
        client = VisionClient(api_key="test")
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"OK"}}]}', b'data: [DONE]']
        with patch.object(client._session,"post",side_effect=[requests.ReadTimeout("stall"),response]) as post:
            result=client.stream_chat("test","test",b"jpeg")
        self.assertTrue(result.ok)
        self.assertEqual(result.retries,1)
        self.assertEqual(client.total_requests,2)
        self.assertEqual(post.call_args.kwargs["timeout"],(10.0,20.0))  # connect cap, then the 20 s default read/stall limit

    def test_hud_does_not_end_between_session_actions(self):
        hud = Mock()
        engine = Engine(hud=hud)
        engine.session_active = True
        engine.hud_end()
        hud.end.assert_not_called()
        hud.notice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
