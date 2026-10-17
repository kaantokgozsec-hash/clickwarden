"""Focus-checked typing, target-centred scroll, NOT_VISIBLE inspection and usage accounting."""
import time
import unittest
from unittest.mock import Mock, patch

import importlib
from clickwarden import focus, input as inp, sessions, vision
engmod = importlib.import_module("clickwarden.engine")
from clickwarden.frames import Frame
from clickwarden.vision import VisionClient, StreamResult
from test_sessions import make_session


def frame():
    return Frame(b"jpeg", 0, 0, 200, 100, 200, 100, "precision", 85, fg_hwnd=123, fg_title="test")


def props(control_type, value_pattern=False, read_only=True, password=False):
    return {"control_type": control_type, "value_pattern": value_pattern,
            "read_only": read_only, "password": password, "pid": 7}


class FocusClassificationTests(unittest.TestCase):
    def test_text_targets_are_editable(self):
        self.assertIs(focus.classify(props(focus.EDIT, True, False)), True)
        self.assertIs(focus.classify(props(focus.EDIT)), True)  # no Value pattern: still a text box
        self.assertIs(focus.classify(props(focus.DOCUMENT, True, False)), True)
        self.assertIs(focus.classify(props(focus.COMBO_BOX, True, False)), True)
        self.assertIs(focus.classify(props(50033, password=True)), True)

    def test_clear_non_text_focus_is_refused(self):
        # Observed in Edge: page body = read-only Document, <button> = Button.
        self.assertIs(focus.classify(props(focus.DOCUMENT, True, True)), False)
        self.assertIs(focus.classify(props(focus.EDIT, True, True)), False)
        for kind in focus.NON_TEXT:
            self.assertIs(focus.classify(props(kind)), False)

    def test_uncertain_focus_stays_unknown(self):
        # contenteditable (Group), CAD/canvas panes, list type-ahead, select boxes.
        for kind in (50026, focus.PANE, 50032, 50007, 50024):
            self.assertIsNone(focus.classify(props(kind)))
        self.assertIsNone(focus.classify(props(focus.DOCUMENT)))
        self.assertIsNone(focus.classify(props(focus.COMBO_BOX, True, True)))

    def test_chromium_first_pane_answer_is_requeried(self):
        with patch.object(focus, "read_focused", side_effect=[props(focus.PANE), props(focus.EDIT, True, False)]) as read:
            verdict = focus.focused_editability()
        self.assertEqual(read.call_count, 2)
        self.assertEqual((verdict["editable"], verdict["role"]), (True, "edit"))

    def test_hung_or_failing_provider_is_unknown(self):
        with patch.object(focus, "read_focused", side_effect=lambda: time.sleep(.5) or props(focus.EDIT)):
            self.assertEqual(focus.focused_editability(timeout=.05)["source"], "uia_timeout")
        with patch.object(focus, "read_focused", side_effect=OSError("COM failure")):
            verdict = focus.focused_editability()
        self.assertIsNone(verdict["editable"])
        self.assertEqual(verdict["source"], "uia_error")
        with patch.object(focus, "read_focused", return_value={}):
            self.assertIsNone(focus.focused_editability()["editable"])


class FocusCheckedTypingTests(unittest.TestCase):
    def setUp(self):
        self.engine = engmod.Engine(client=Mock(), hud=Mock())
        self.engine.safety = Mock()
        inp.foreground_guard.disarm()
        self.guard = patch.object(engmod, "foreground_guard")
        self.guard.start()

    def tearDown(self):
        self.guard.stop()
        inp.foreground_guard.disarm()

    def type_with(self, editable, **kwargs):
        self.engine.focus_probe = Mock(return_value={"editable": editable, "role": "x"})
        with patch.object(engmod, "type_text", return_value={"elapsed_ms": 1}) as send, \
                patch.object(engmod, "input_hotkey") as enter:
            result = self.engine.type("Soviet Union", press_enter=True, **kwargs)
        return result, send, enter

    def test_refuses_without_input_when_focus_is_not_editable(self):
        result, send, enter = self.type_with(False)
        self.assertEqual(result["status"], "no_editable_focus")
        self.assertEqual(result["typed"], 0)
        send.assert_not_called()
        enter.assert_not_called()

    def test_override_and_unknown_focus_still_type(self):
        for editable, kwargs in ((False, {"allow_non_editable": True}), (None, {}), (True, {})):
            result, send, enter = self.type_with(editable, **kwargs)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["focus"]["editable"], editable)
            send.assert_called_once()
            enter.assert_called_once_with("enter")

    def test_refusal_is_unverified_session_evidence(self):
        self.assertEqual(sessions.evidence({"status": "no_editable_focus"}), "unverified")


class InspectAndLocatePromptTests(unittest.TestCase):
    def setUp(self):
        self.client = VisionClient()

    def tearDown(self):
        self.client._session.close()

    def inspect(self, text):
        with patch.object(self.client, "stream_chat", return_value=StreamResult(content=text)):
            return self.client.inspect(frame(), "Read the infobox capital")

    def test_not_visible_answer_is_flagged(self):
        self.assertIn("NOT_VISIBLE:", vision.INSPECT_SYSTEM)
        self.assertIs(self.inspect("NOT_VISIBLE: this is the Main Page, no infobox")["visible"], False)
        self.assertIs(self.inspect("**NOT_VISIBLE**: nothing")["visible"], False)
        self.assertIs(self.inspect("The capital is Moscow.")["visible"], True)
        self.assertFalse(vision.is_pass("NOT_VISIBLE: no dialog"))

    def test_failed_inspect_has_no_visibility_claim(self):
        with patch.object(self.client, "stream_chat", return_value=StreamResult(error="HTTP 503: x")):
            self.assertIsNone(self.client.inspect(frame(), "q")["visible"])

    def test_locate_asks_for_terse_found_json_with_small_budget(self):
        found = vision.LOCATE_SYSTEM.split('{{"status":"found"')[1]
        self.assertNotIn("description", found)
        reply = StreamResult(content='{"status":"found","target":"Search","box":[10,10,50,30],"confidence":"high"}')
        with patch.object(self.client, "stream_chat", return_value=reply) as chat:
            result = self.client.locate(frame(), "search box")
        self.assertTrue(result["success"])
        self.assertEqual(chat.call_args.kwargs["max_tokens"], vision.LOCATE_MAX_TOKENS)
        self.assertEqual(vision.LOCATE_MAX_TOKENS, 160 if vision.DISABLE_THINKING else 512)


class SessionScrollAndUsageTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.session.target_hwnd = 123
        self.session.family = Mock()
        self.patches = [patch("clickwarden.sessions.foreground_hwnd", return_value=123),
                        patch("clickwarden.sessions.window_rect", return_value=(0, 0, 400, 300))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        inp.foreground_guard.disarm()

    def test_scroll_without_point_targets_window_client_centre(self):
        self.session.engine.scroll.return_value = {"status": "success"}
        with patch("clickwarden.sessions.client_center", return_value=(210, 160)) as centre:
            self.session.run("scroll", {"clicks": -5, "x": None, "y": None})
        centre.assert_called_once_with(123)
        self.session.engine.scroll.assert_called_once_with(clicks=-5, x=210, y=160)

    def test_explicit_scroll_point_is_kept(self):
        self.session.engine.scroll.return_value = {"status": "success"}
        with patch("clickwarden.sessions.client_center") as centre:
            self.session.run("scroll", {"clicks": 3, "x": 5, "y": 6})
        centre.assert_not_called()
        self.session.engine.scroll.assert_called_once_with(clicks=3, x=5, y=6)

    def test_batch_scroll_and_typing_override(self):
        self.session.input_check = lambda: None
        for method in ("type", "scroll"):
            getattr(self.session.engine, method).return_value = {"status": "success"}
        with patch("clickwarden.sessions.client_center", return_value=(11, 22)):
            self.session.batch({"actions": [{"action": "scroll", "clicks": -2},
                                             {"action": "type", "text": "x", "allow_non_editable": True}]})
        self.session.engine.scroll.assert_called_once_with(-2, x=11, y=22)
        self.session.engine.type.assert_called_once_with("x", press_enter=False, wpm=0, allow_non_editable=True)
        with self.assertRaises(ValueError):
            self.session.batch({"actions": [{"action": "type", "text": "x", "allow_non_editable": "yes"}]})

    def test_scroll_without_client_area_refuses(self):
        with patch("clickwarden.sessions.client_center", return_value=None):
            result = self.session.run("scroll", {"clicks": -1, "x": None, "y": None})
        self.assertEqual(result["status"], "error")
        self.session.engine.scroll.assert_not_called()

    def test_tokens_reported_per_action_status_and_end(self):
        client = self.session.client

        def observe(**kwargs):
            client.total_requests += 1
            client.prompt_tokens += 1500
            client.completion_tokens += 40
            client.usage_reports += 1
            return {"success": True, "answer": "ok"}
        self.session.engine.inspect.side_effect = observe
        with patch.multiple(sessions, PRICE_IN=0.5, PRICE_OUT=2.0):
            result = self.session.run("inspect", {"query": "q"})
            status = self.session.snapshot()
        self.assertEqual(result["usage"], {"total_requests": 1, "prompt_tokens": 1500,
                                           "completion_tokens": 40, "est_cost_usd": 0.00083})
        self.assertEqual(self.session.ledger[-1]["prompt_tokens"], 1500)
        self.assertEqual((status["prompt_tokens"], status["completion_tokens"], status["est_cost_usd"]),
                         (1500, 40, 0.00083))
        self.assertIsNone(self.session.snapshot()["est_cost_usd"])  # no prices configured

    def test_actions_without_requests_carry_no_usage_block(self):
        self.session.engine.hotkey.return_value = {"status": "success"}
        result = self.session.run("hotkey", {"keys": "ctrl+l"})
        self.assertNotIn("usage", result)
        self.assertEqual(self.session.ledger[-1]["prompt_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
