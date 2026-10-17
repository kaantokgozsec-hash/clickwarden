"""Input boundary regressions. All OS input is mocked."""
import unittest
from unittest.mock import patch

from clickwarden import input as inp
from clickwarden.control import Cancelled


class InputPolishTests(unittest.TestCase):
    def tearDown(self):
        inp.foreground_guard.disarm()

    def test_partial_click_releases_delivered_button(self):
        calls = []
        def send(n, events, size):
            calls.append([events[i].mi.dwFlags for i in range(n)])
            return 1
        with patch.object(inp, 'SendInput', side_effect=send), self.assertRaises(OSError):
            inp._send(inp._mouse_input(0,0,0,inp.MOUSEEVENTF_LEFTDOWN),
                      inp._mouse_input(0,0,0,inp.MOUSEEVENTF_LEFTUP))
        self.assertEqual(calls, [[inp.MOUSEEVENTF_LEFTDOWN, inp.MOUSEEVENTF_LEFTUP],
                                 [inp.MOUSEEVENTF_LEFTUP]])

    def test_partial_unicode_releases_only_unpaired_delivered_key(self):
        calls = []
        def send(n, events, size):
            calls.append([(events[i].ki.wScan, events[i].ki.dwFlags) for i in range(n)])
            return 3 if len(calls) == 1 else n
        with patch.dict('os.environ', {'CLICKWARDEN_TYPE_BATCH': '1'}), \
                patch.object(inp, 'SendInput', side_effect=send), self.assertRaises(OSError):
            inp.type_text('ab')
        self.assertEqual(calls[1], [(98, inp.KEYEVENTF_UNICODE | inp.KEYEVENTF_KEYUP)])

    def test_zero_dispatch_does_not_release_unpressed_keys(self):
        with patch.object(inp, 'SendInput', return_value=0) as send, self.assertRaises(OSError):
            inp._send(inp._vk_input(65))
        self.assertEqual(send.call_count, 1)

    def test_focus_is_checked_between_each_chord_press(self):
        inp.foreground_guard.arm(123)
        with patch.object(inp.foreground_guard, 'check', side_effect=[None, inp.ForegroundGuardError('changed')]), \
                patch.object(inp, 'SendInput', side_effect=lambda n,*_:n) as send, self.assertRaises(inp.ForegroundGuardError):
            inp.hotkey('ctrl+a')
        self.assertEqual(send.call_count, 3)  # ctrl down, a up, ctrl up

    def test_release_failure_still_attempts_remaining_modifiers(self):
        delivered = []
        def send(event):
            delivered.append((event.ki.wVk, bool(event.ki.dwFlags & inp.KEYEVENTF_KEYUP)))
            if event.ki.wVk == 65 and event.ki.dwFlags & inp.KEYEVENTF_KEYUP:
                raise OSError('release failure')
        with patch.object(inp, '_send', side_effect=send), self.assertRaises(OSError):
            inp.hotkey('ctrl+shift+a')
        self.assertEqual(delivered[-3:], [(65, True), (inp.VK_SHIFT, True), (inp.VK_CONTROL, True)])

    def test_extended_keys_on_both_press_and_release(self):
        for key in ('home','end','delete','left','up','rctrl','ralt','win'):
            for up in (False, True):
                event = inp._vk_input(inp.VK[key], up=up)
                self.assertTrue(event.ki.dwFlags & inp.KEYEVENTF_EXTENDEDKEY)
                self.assertEqual(bool(event.ki.dwFlags & inp.KEYEVENTF_KEYUP), up)

    def test_invalid_chords_fail_before_input(self):
        for keys in ('ctrl++a','ctrl,','ctrl,control','', ['ctrl', 1]):
            with self.subTest(keys=keys), patch.object(inp,'_send') as send, self.assertRaises(ValueError):
                inp.hotkey(keys)
            send.assert_not_called()

    def test_scroll_checks_current_cursor_for_foreign_window(self):
        with patch.object(inp,'cursor_xy',return_value=(100,200)), \
                patch.object(inp,'check_click_point',side_effect=inp.ForegroundGuardError('covered')) as check, \
                patch.object(inp,'_send') as send, self.assertRaises(inp.ForegroundGuardError):
            inp.scroll(-3)
        check.assert_called_once_with(100,200)
        send.assert_not_called()

    def test_scroll_rechecks_after_moving_and_preserves_signed_delta(self):
        with patch.object(inp,'move_to') as move, patch.object(inp,'check_click_point') as check, \
                patch.object(inp,'_send') as send:
            inp.scroll(-3,10,20)
        self.assertEqual(check.call_count,2)
        move.assert_called_once_with(10,20,human=False)
        self.assertEqual(send.call_args.args[0].mi.mouseData, (-360) & 0xFFFFFFFF)

    def test_invalid_scroll_refuses_before_movement(self):
        for args in ((1,10,None),(1,None,20),(float('nan'),None,None),(1001,None,None)):
            with patch.object(inp,'move_to') as move, patch.object(inp,'_send') as send, self.assertRaises(ValueError):
                inp.scroll(*args)
            move.assert_not_called()
            send.assert_not_called()

    def test_drag_checks_intermediate_coverage_and_releases_on_refusal(self):
        with patch.object(inp,'move_to'), patch.object(inp,'check_click_point',
                side_effect=[None,None,None,inp.ForegroundGuardError('foreign mid-path')]), \
                patch.object(inp,'_send') as send, self.assertRaises(inp.ForegroundGuardError):
            inp.drag(50,50,100,100)
        self.assertEqual(send.call_args.args[0].mi.dwFlags,inp.MOUSEEVENTF_LEFTUP)

    def test_invalid_coordinates_do_not_clamp_to_screen_corner(self):
        with patch.multiple(inp,_SCREEN_X=-100,_SCREEN_Y=0,_SCREEN_W=200,_SCREEN_H=100):
            for point in ((-101,10),(100,10),(0,-1),(0,100),(float('nan'),0),(True,0)):
                with self.subTest(point=point), self.assertRaises(ValueError):
                    inp._raw_abs_xy(*point)
            self.assertEqual(inp._raw_abs_xy(-100,0),(0,0))

    def test_slow_typing_wait_is_immediately_cancellable(self):
        with inp.dispatch_guard(lambda: (_ for _ in ()).throw(Cancelled())), \
                patch.object(inp.time,'sleep') as sleep, self.assertRaises(Cancelled):
            inp._input_wait(12)
        sleep.assert_not_called()

    def test_bad_text_or_cadence_is_rejected_before_input(self):
        for text,wpm in ((None,0),('a',-1),('a',float('nan')),('\ud800',0)):
            with self.subTest(text=repr(text),wpm=wpm), patch.object(inp,'_send') as send, self.assertRaises((ValueError,UnicodeError)):
                inp.type_text(text,wpm)
            send.assert_not_called()

    def test_newlines_use_enter_once_for_crlf_in_both_modes(self):
        for wpm in (0, 600):
            with patch.object(inp,'_send') as send, patch.object(inp,'_input_wait'):
                inp.type_text('a\r\nb\nc\rd',wpm)
            events = [e for call in send.call_args_list for e in call.args]
            enters = [e for e in events if e.ki.wVk == inp.VK['enter'] and not e.ki.dwFlags & inp.KEYEVENTF_KEYUP]
            self.assertEqual(len(enters),3)
            self.assertFalse(any(e.ki.wScan in (10,13) for e in events))

    def test_batch_boundary_does_not_split_surrogate_pair(self):
        with patch.dict('os.environ', {'CLICKWARDEN_TYPE_BATCH': '1'}), patch.object(inp,'_send') as send:
            inp.type_text('a'*499+'😀'+'b')
        batches = [[e.ki.wScan for e in call.args if not e.ki.dwFlags & inp.KEYEVENTF_KEYUP]
                   for call in send.call_args_list]
        self.assertEqual(len(batches),2)
        self.assertEqual(batches[1],[0xD83D,0xDE00,98])

    def test_keyboard_batch_uses_same_plus_chord_validation(self):
        from test_sessions import make_session
        session = make_session()
        session.input_check = lambda: None
        session.engine.hotkey.return_value = {'status':'success'}
        session.batch({'actions':[{'action':'hotkey','keys':'ctrl+a'}]})
        session.engine.hotkey.assert_called_once_with('ctrl+a')

    def test_move_rejects_covered_target_before_moving(self):
        from clickwarden.engine import Engine
        from unittest.mock import Mock
        engine = Engine(client=Mock(),hud=Mock())
        engine.safety = Mock()
        with patch.object(inp,'check_click_point',side_effect=inp.ForegroundGuardError('covered')), \
                patch.object(inp,'move_to') as move, self.assertRaises(inp.ForegroundGuardError):
            engine.move(100,100)
        move.assert_not_called()

    def test_batch_preserves_mouse_targets_and_typing_speed(self):
        from test_sessions import make_session
        session = make_session()
        session.input_check = lambda: None
        for method in ('type','scroll','move'):
            getattr(session.engine,method).return_value = {'status':'success'}
        session.batch({'actions':[{'action':'type','text':'hi','wpm':300},
            {'action':'scroll','clicks':-2,'x':10,'y':20},{'action':'move','x':30,'y':40}]})
        session.engine.type.assert_called_once_with('hi',press_enter=False,wpm=300)
        session.engine.scroll.assert_called_once_with(-2,x=10,y=20)
        session.engine.move.assert_called_once_with(30,40)

    def test_bad_later_batch_parameters_refuse_whole_chain(self):
        from test_sessions import make_session
        for invalid in ({'action':'type','text':'later','wpm':-1},
                        {'action':'scroll','clicks':2,'x':10},
                        {'action':'click','description':'OK','button':'bad'}):
            session = make_session()
            with self.assertRaises(ValueError):
                session.batch({'actions':[{'action':'type','text':'first'},invalid]})
            session.engine.type.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class DefaultTypingPaceTests(unittest.TestCase):
    def test_default_typing_is_paced_one_character_at_a_time(self):
        with patch.object(inp, '_send') as send, patch.object(inp, '_input_wait') as wait:
            inp.type_text('abc')
        self.assertEqual([len(call.args) for call in send.call_args_list], [2, 2, 2])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [0.02, 0.02])

    def test_pace_is_tunable_and_batching_is_opt_in(self):
        with patch.dict('os.environ', {'CLICKWARDEN_TYPE_DELAY_MS': '35'}), \
                patch.object(inp, '_send'), patch.object(inp, '_input_wait') as wait:
            inp.type_text('ab')
        self.assertEqual(wait.call_args_list[0].args[0], 0.035)
        with patch.dict('os.environ', {'CLICKWARDEN_TYPE_BATCH': '1'}), \
                patch.object(inp, '_send') as send, patch.object(inp, '_input_wait') as wait:
            inp.type_text('abc')
        self.assertEqual(send.call_count, 1)
        wait.assert_not_called()
