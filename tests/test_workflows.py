import tempfile
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import Mock, patch

from clickwarden.control import Cancelled
from clickwarden.workflows import find_files, search_url, web_url, wait_for_content_change
from test_sessions import make_session


class WorkflowTests(unittest.TestCase):
    def test_page_readiness_requires_content_change_and_stability(self):
        before = np.zeros((20,20), dtype=np.int16)
        after = np.full((20,20), 100, dtype=np.int16)
        with patch('clickwarden.workflows.time.sleep'), \
             patch('clickwarden.workflows.time.monotonic', side_effect=[0, .1,.2,.3,.4,.5,.6,.7,.8]):
            result = wait_for_content_change(before, Mock(return_value=after), Mock())
        self.assertEqual(result['hint'], 'content_changed_and_settled')
        self.assertFalse(result['page_load_verified'])

    def test_page_readiness_does_not_call_unchanged_page_loaded(self):
        before = np.zeros((20,20), dtype=np.int16)
        with patch('clickwarden.workflows.time.sleep'), \
             patch('clickwarden.workflows.time.monotonic', side_effect=[0,.1,.2,9,9]):
            result = wait_for_content_change(before, Mock(return_value=before), Mock())
        self.assertEqual(result['hint'], 'wait_limit')
        self.assertFalse(result['content_changed'])

    def test_page_wait_cancel_stops_before_capture(self):
        sample = Mock()
        with self.assertRaises(Cancelled):
            wait_for_content_change(np.zeros((2,2)), sample, Mock(side_effect=Cancelled('stop')))
        sample.assert_not_called()

    def test_cancelled_batch_retains_completed_dispatches(self):
        s = make_session()
        s.input_check = Mock(side_effect=[None, Cancelled('stop')])
        s.engine.type.return_value = {'status':'success','verification':'unverified'}
        with self.assertRaises(Cancelled):
            s.batch({'actions':[{'action':'type','text':'first'},{'action':'type','text':'second'}]})
        self.assertEqual(s.metrics()['outcomes']['dispatched_unverified'],1)
        self.assertEqual(len(s.ledger[0]['results']),1)
        self.assertEqual(s.engine.type.call_count,1)

    def test_search_encodes_query_as_data(self):
        url = search_url('a&b #日本\nsecond')
        self.assertIn('q=a%26b+%23', url)
        self.assertIn('%0A', url)
        with self.assertRaises(ValueError):
            search_url('ok', 'unknown')

    def test_navigation_rejects_protocols_credentials_and_keystroke_injection(self):
        for url in ('javascript:alert(1)', 'file:///C:/a', 'https://u:p@example.com',
                    'https://example.com\nhello', 'https://', ' example.com'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                web_url(url)
        self.assertEqual(web_url('https://example.com/?q=a%20b'), 'https://example.com/?q=a%20b')

    def test_native_search_metadata_bounds_and_case(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'drawing.DWG').write_text('never read')
            (root/'other.txt').write_text('ignored')
            result = find_files(folder, '*.dwg')
            self.assertEqual(len(result['matches']), 1)
            self.assertFalse(result['content_read'])
            self.assertTrue(result['complete'])
            limited = find_files(folder, '*', limit=1)
            self.assertTrue(limited['truncated'])
            self.assertFalse(limited['complete'])
            bounded = find_files(folder, '*', max_entries=1)
            self.assertEqual(bounded['scanned'], 1)
            self.assertEqual(bounded['stop_reason'], 'entry_limit')

    def test_search_cancels_before_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Cancelled):
                find_files(folder, check=Mock(side_effect=Cancelled('stop')))

    def test_search_refuses_unbounded_or_relative_arguments(self):
        with self.assertRaises(ValueError):
            find_files('.')
        with tempfile.TemporaryDirectory() as folder:
            for kwargs in ({'limit':0}, {'max_entries':0}, {'max_seconds':float('nan')}, {'pattern':'../*'}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    find_files(folder, **kwargs)

    def test_paused_file_search_does_not_need_live_target(self):
        s = make_session()
        s.family = None
        s.control.pause()
        with tempfile.TemporaryDirectory() as folder:
            result = s.run('find_files', {'root':folder})
        self.assertEqual(result['status'], 'observed')
        self.assertTrue(s.control.paused.is_set())
        s.engine.type.assert_not_called()

    def test_switch_updates_target_and_invalidates_old_observations(self):
        s = make_session()
        s.target_hwnd = 1
        s.family = Mock()
        candidate = Mock()
        with patch('clickwarden.sessions.enumerate_windows', return_value=[{'hwnd':42,'title':'Second app'}]), \
             patch('clickwarden.sessions.TargetFamily', return_value=candidate), \
             patch('clickwarden.sessions.foreground_hwnd', return_value=42), \
             patch('clickwarden.sessions.window_rect', return_value=(0,0,500,500)), \
             patch('clickwarden.sessions.user32.SetForegroundWindow') as focus:
            result = s.run('switch_window', {'hwnd':42})
        self.assertEqual(result['status'], 'success')
        self.assertIs(s.family, candidate)
        self.assertEqual(s.target_hwnd,42)
        s.engine.cache.invalidate.assert_called_once()
        focus.assert_called_once_with(42)

    def test_paused_switch_never_focuses(self):
        s = make_session()
        s.control.pause()
        with patch('clickwarden.sessions.user32.SetForegroundWindow') as focus:
            result = s.run('switch_window', {'hwnd':42})
        self.assertEqual(result['status'], 'paused')
        focus.assert_not_called()

    def test_switch_focus_denied_preserves_old_target(self):
        s = make_session()
        s.check = Mock()
        old = s.family = Mock()
        s.target_hwnd = 1
        with patch('clickwarden.sessions.enumerate_windows', return_value=[{'hwnd':42,'title':'Second'}]), \
             patch('clickwarden.sessions.TargetFamily'), \
             patch('clickwarden.sessions.foreground_hwnd', return_value=1), \
             patch('clickwarden.sessions.user32.SetForegroundWindow'), \
             patch('clickwarden.sessions.force_foreground') as recovery, \
             patch('clickwarden.sessions.time.monotonic', side_effect=[0, 1, 2, 3]):
            from clickwarden.control import Paused
            with self.assertRaises(Paused):
                s.switch_window(42)
        self.assertIs(s.family,old)
        self.assertEqual(s.target_hwnd,1)
        recovery.assert_called_once()

    def test_switch_recovers_from_foreground_lock_with_tagged_gesture(self):
        # The foreground lock refuses a bare SetForegroundWindow when another
        # process owns the foreground; the tagged-ALT helper is then used once,
        # and the switch succeeds without weakening any check.
        s = make_session()
        s.check = Mock()
        candidate = Mock()
        with patch('clickwarden.sessions.enumerate_windows', return_value=[{'hwnd':42,'title':'Second'}]), \
             patch('clickwarden.sessions.TargetFamily', return_value=candidate), \
             patch('clickwarden.sessions.foreground_hwnd', side_effect=[1, 42]), \
             patch('clickwarden.sessions.window_rect', return_value=(0,0,500,500)), \
             patch('clickwarden.sessions.user32.SetForegroundWindow'), \
             patch('clickwarden.sessions.force_foreground') as recovery, \
             patch('clickwarden.sessions.time.monotonic', side_effect=[0, 1, 2]), \
             patch('clickwarden.sessions.time.sleep'):
            result = s.switch_window(42)
        self.assertEqual(result['status'],'success')
        self.assertEqual(s.target_hwnd,42)
        recovery.assert_called_once()

    def test_navigation_requires_browser_and_does_not_claim_page_loaded(self):
        s = make_session()
        s.input_check = Mock()
        s.family = Mock(exe='notepad.exe')
        with self.assertRaises(ValueError):
            s.navigate('https://example.com')
        s.engine.hotkey.assert_not_called()
        s.family.exe = 'msedge.exe'
        result = s.navigate('https://example.com')
        self.assertEqual(result['verification'], 'unverified')
        s.engine.type.assert_called_once_with('https://example.com', press_enter=True)


if __name__ == '__main__':
    unittest.main()
