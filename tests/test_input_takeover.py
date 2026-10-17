"""Mouse/focus regressions: synthetic events and mocked Win32, no desktop input."""
import json
import unittest
from unittest.mock import Mock, patch

from clickwarden import input as inp
from clickwarden.control import Control, PhysicalInputWatch, WM_MOUSEMOVE
from clickwarden.hud import Hud
from clickwarden.input_identity import CLICKWARDEN_EXTRA_INFO
from clickwarden.jobs import target_identity
from test_sessions import make_session


class TakeoverTests(unittest.TestCase):
    def setUp(self):
        self.control = Control(60)
        self.watch = PhysicalInputWatch(self.control)
        self.watch.enabled = True
        self.watch.mouse_anchor = (100,100)

    def test_small_jitter_does_not_pause_but_accumulated_motion_does(self):
        for point in ((101,100),(102,101),(104,100),(105,101)):
            self.watch.mouse_event(0,WM_MOUSEMOVE,point)
        self.assertFalse(self.control.paused.is_set())
        self.assertEqual(self.control.input_revision,0)
        self.watch.mouse_event(0,WM_MOUSEMOVE,(106,100))
        self.assertTrue(self.control.paused.is_set())
        self.assertEqual(self.control.last_input['kind'],'mouse_move')

    def test_mouse_release_cannot_repause_after_resume(self):
        self.watch.mouse_event(0,0x201,(100,100))
        self.assertTrue(self.control.paused.is_set())
        with patch('clickwarden.control.cursor_pos',return_value=(100,100)):
            self.control.resume()
        revision = self.control.input_revision
        for release in (0x202,0x205,0x208,0x20C):
            self.watch.mouse_event(0,release,(100,100))
        self.assertFalse(self.control.paused.is_set())
        self.assertEqual(self.control.input_revision,revision)

    def test_keyboard_release_cannot_repause_after_resume(self):
        self.watch.keyboard_event(0,65,0x100)
        with patch('clickwarden.control.cursor_pos',return_value=(100,100)):
            self.control.resume()
        for release in (0x101,0x105):
            self.watch.keyboard_event(0,65,release)
        self.assertFalse(self.control.paused.is_set())

    def test_mouse_buttons_and_wheels_pause_immediately(self):
        for message in (0x201,0x204,0x207,0x20B,0x20A,0x20E):
            with self.subTest(message=message):
                self.control.paused.clear()
                self.watch.mouse_event(0,message,(100,100))
                self.assertTrue(self.control.paused.is_set())

    def test_own_marker_ignores_mouse_keyboard_even_without_os_injected_flag(self):
        self.watch.mouse_event(0,0x201,(900,900),CLICKWARDEN_EXTRA_INFO)
        self.watch.keyboard_event(0,65,0x100,CLICKWARDEN_EXTRA_INFO)
        self.assertFalse(self.control.paused.is_set())
        self.assertEqual(self.control.input_revision,0)
        self.watch.mouse_event(0,WM_MOUSEMOVE,(901,901))
        self.assertFalse(self.control.paused.is_set())
        self.watch.mouse_event(0,WM_MOUSEMOVE,(907,901))
        self.assertTrue(self.control.paused.is_set())

    def test_injected_motion_rebases_physical_cursor_baseline(self):
        self.watch.mouse_event(1,WM_MOUSEMOVE,(500,500))
        self.watch.mouse_event(0,WM_MOUSEMOVE,(501,500))
        self.assertFalse(self.control.paused.is_set())
        self.watch.mouse_event(0,WM_MOUSEMOVE,(506,500))
        self.assertTrue(self.control.paused.is_set())

    def test_resume_rebases_cursor_before_opening_input_gate(self):
        self.control.pause()
        with patch('clickwarden.control.cursor_pos',return_value=(700,700)) as position:
            self.control.resume()
        position.assert_called_once()
        self.watch.mouse_event(0,WM_MOUSEMOVE,(701,700))
        self.assertFalse(self.control.paused.is_set())

    def test_all_sendinput_events_carry_provenance_including_releases(self):
        delivered = []
        def send(n, events, size):
            delivered.extend(events[i].mi.dwExtraInfo if events[i].type == inp.INPUT_MOUSE
                             else events[i].ki.dwExtraInfo for i in range(n))
            return n
        with patch.object(inp,'SendInput',side_effect=send):
            inp._send(inp._mouse_input(1,2,0,inp.MOUSEEVENTF_MOVE),inp._vk_input(65))
            inp._send(inp._vk_input(65,up=True))
        self.assertEqual(delivered,[CLICKWARDEN_EXTRA_INFO]*3)

    def test_disabling_watcher_leaves_real_input_unblocked_and_unpaused(self):
        self.watch.enabled = False
        self.watch.mouse_event(0,0x201,(300,300))
        self.assertFalse(self.control.paused.is_set())


class OverlayTargetTests(unittest.TestCase):
    def test_overlay_mouse_activation_and_hit_test_never_take_focus(self):
        hud = Hud()
        self.assertEqual(hud._wndproc_impl(123,0x21,0,0),3)
        self.assertEqual(hud._wndproc_impl(123,0x84,0,0),-1)

    def test_startup_ignores_foreground_hud_and_adopts_actual_browser(self):
        session = make_session()
        session.app_or_url = 'https://www.google.com'
        session.initial_hwnd = 10
        session.engine._hud._hwnd = 20
        windows = [{'hwnd':10,'title':'ChatGPT'}, {'hwnd':20,'title':'clickwarden-hud'},
                   {'hwnd':30,'title':'Google - Google Chrome'}]
        with patch('clickwarden.sessions.virtual_screen',return_value=(0,0,1920,1080)), \
                patch('clickwarden.sessions.user32.IsProcessDPIAware',return_value=True), \
                patch('clickwarden.sessions.enumerate_windows',return_value=windows), \
                patch('clickwarden.sessions.foreground_hwnd',side_effect=[20,30]), \
                patch('clickwarden.sessions.pid_of_window',return_value=10), \
                patch('clickwarden.targets.process_parents',return_value={10:(0,'chatgpt.exe')}), \
                patch('clickwarden.sessions.launch_target'), \
                patch('clickwarden.sessions.TargetFamily'):
            session.prepare()
        self.assertEqual(session.target_hwnd,30)
        self.assertFalse(session.error)

    def test_explicit_switch_cannot_select_the_hud(self):
        session = make_session()
        with patch('clickwarden.sessions.enumerate_windows',return_value=[{'hwnd':20,'title':'clickwarden-hud'}]), \
                patch('clickwarden.sessions.user32.SetForegroundWindow') as focus, self.assertRaises(ValueError):
            session.switch_window(20)
        focus.assert_not_called()

    def test_durable_job_cannot_target_own_overlay(self):
        with patch('clickwarden.jobs.enumerate_windows',return_value=[{'hwnd':20,'title':'clickwarden-hud'}]), self.assertRaises(ValueError):
            target_identity(20)

    def test_window_inventory_excludes_control_surfaces(self):
        from clickwarden import server
        with patch('clickwarden.server.enumerate_windows',return_value=[{'hwnd':20,'title':'clickwarden-hud'},
                {'hwnd':21,'title':'Clickwarden — desktop controls'}, {'hwnd':30,'title':'Chrome'}]):
            self.assertEqual(json.loads(server.list_windows()),[{'hwnd':30,'title':'Chrome'}])


if __name__ == '__main__':
    unittest.main()
