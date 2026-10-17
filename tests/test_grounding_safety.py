"""Regression fixtures: no actual desktop capture/input/provider calls."""
import copy
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from clickwarden.grounding import validate_grounding
from clickwarden.accessibility import inventory, validate_selection
from clickwarden.control import Cancelled
from clickwarden.engine import Engine
from clickwarden.frames import Frame
from clickwarden.vision import VisionClient, StreamResult
from test_sessions import make_session


def found(**overrides):
    return {'status': 'found', 'box': [10,20,30,40], 'confidence': 'high', **overrides}


def scan(**overrides):
    return {'status':'observed', 'target_hwnd':123, 'complete':True,
            'controls':[{'control_id':'1,2,3', 'name':'OK', 'role':'ControlType.Button',
                         'automation_id':'confirm', 'pid':55, 'enabled':True, 'box':[10,20,30,40]}],
            'hit':{'control_id':'1,2,3','matches':True,'point':[20,30]}, **overrides}


class GroundingSafetyTests(unittest.TestCase):
    def test_absent_and_ambiguous_override_even_plausible_coordinates(self):
        for status in ('absent','ambiguous'):
            result = validate_grounding(found(status=status, point=[20,30]),100,100)
            self.assertFalse(result['success'])
            self.assertEqual(result['grounding_status'],status)

    def test_boxes_and_confidence_fail_closed(self):
        values = [found(box=None), found(box=[1,2,3]), found(box=[0,0,0,10]),
                  found(box=[-1,0,20,20]), found(box=[0,0,101,20]),
                  found(box=[0,0,float('nan'),20]), found(box=[0,True,20,20]),
                  found(box=[0,0,float('inf'),20]), found(confidence='certain'),
                  found(status='FOUND'), found(point=[50,50]), found(point=[True,30]),
                  found(point=['20',30]), {'point':[20,30], 'confidence':'high'}]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(validate_grounding(value,100,100)['success'])

    def test_center_is_derived_from_box_not_independent_guess(self):
        result = validate_grounding(found(point=[11,21]),100,100)
        self.assertEqual(result['image_point'],[20,30])

    def test_absent_truncated_and_failed_stream_never_call_coordinate_callback(self):
        client = VisionClient(api_key='test')
        frame = Frame(b'jpeg',0,0,100,100,100,100,'precision',85)
        responses = [StreamResult(content=json.dumps(found(status='absent',point=[20,30]))),
                     StreamResult(content='{"status":"found","point":[20,30],'),
                     StreamResult(content=json.dumps(found()),error='interrupted')]
        try:
            for response in responses:
                callback = Mock()
                with patch.object(client,'stream_chat',return_value=response):
                    result = client.locate(frame,'missing download icon',on_point=callback)
                self.assertFalse(result['success'])
                callback.assert_not_called()
        finally:
            client._session.close()

    def test_mapping_handles_negative_origin_and_nonuniform_scale(self):
        client = VisionClient(api_key='test')
        frame = Frame(b'jpeg',-100,20,100,100,200,300,'precision',85)
        try:
            with patch.object(client,'stream_chat',return_value=StreamResult(content=json.dumps(found()))):
                result = client.locate(frame,'OK')
            self.assertEqual(result['point'],[-60,110])
            self.assertEqual(result['box'],[-80,80,-40,140])
            self.assertEqual(result['target_size'],[40,60])
        finally:
            client._session.close()

    def test_autonomous_click_requires_found_box_and_nonlow_confidence(self):
        client = VisionClient(api_key='test')
        frame = Frame(b'jpeg',0,0,100,100,100,100,'precision',85)
        try:
            for value in (found(status='absent'), found(confidence='low'), {'point':[20,30]}):
                with patch.object(client,'stream_chat',return_value=StreamResult(content=json.dumps({'action':'click',**value}))):
                    self.assertFalse(client.plan(frame,'goal',[])['success'])
            with patch.object(client,'stream_chat',return_value=StreamResult(content=json.dumps({'action':'click',**found()}))):
                self.assertEqual(client.plan(frame,'goal',[])['screen_point'],[20,30])
        finally:
            client._session.close()


class ClickSafetyTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(client=Mock(),hud=Mock())
        self.engine.safety = Mock()

    def test_high_confidence_wide_thin_row_still_zooms(self):
        loc = {'success':True,'confidence':'high','point':[120,30],
               'box':[20,20,220,40],'target_size':[200,20]}
        with patch.object(self.engine,'locate',return_value=loc) as locate, \
                patch.object(self.engine,'_dispatch_click') as dispatch, \
                patch('clickwarden.engine.virtual_screen',return_value=(0,0,1920,1080)):
            result = self.engine.smart_click('Thinking level')
        self.assertEqual(locate.call_count,2)
        dispatch.assert_called_once()
        self.assertFalse(result['task_verified'])
        self.assertEqual(result['action_status'],'dispatched')

    def test_zoom_absence_and_disjoint_box_never_inject(self):
        initial = {'success':True,'confidence':'high','point':[20,30],
                   'box':[10,20,30,40],'target_size':[20,20]}
        for zoom in ({'success':False,'grounding_status':'absent'},
                     {**initial,'point':[100,100],'box':[90,90,110,110]}):
            with patch.object(self.engine,'locate',side_effect=[initial,zoom]), \
                    patch.object(self.engine,'_dispatch_click') as dispatch, \
                    patch('clickwarden.engine.virtual_screen',return_value=(0,0,1920,1080)):
                result = self.engine.smart_click('OK')
            self.assertEqual(result['status'],'error')
            dispatch.assert_not_called()

    def test_repeated_failed_point_refuses_before_dispatch(self):
        loc = {'success':True,'confidence':'high','point':[20,30],
               'box':[10,20,30,40],'target_size':[20,20]}
        with patch.object(self.engine,'locate',return_value=loc), \
                patch.object(self.engine,'_dispatch_click') as dispatch, \
                patch('clickwarden.engine.virtual_screen',return_value=(0,0,1920,1080)):
            result = self.engine.smart_click('OK',avoid_point=[23,32])
        self.assertIn('repeated',result['error'])
        dispatch.assert_not_called()

    def test_native_point_outside_window_never_dispatches(self):
        with patch('clickwarden.engine.window_rect',return_value=(0,0,100,100)), \
                patch.object(self.engine,'_dispatch_click') as dispatch:
            result = self.engine.click_control_point([120,20],123)
        self.assertEqual(result['status'],'error')
        dispatch.assert_not_called()

    def test_valid_native_click_reaches_dispatch_with_real_trace(self):
        with patch('clickwarden.engine.window_rect',return_value=(0,0,100,100)), \
                patch.object(self.engine,'_dispatch_click') as dispatch:
            result = self.engine.click_control_point([20,30],123,button='right',clicks=2)
        self.assertEqual(result['status'],'success')
        self.assertEqual(dispatch.call_args.args[:4],(20,30,'right',2))
        self.assertEqual(dispatch.call_args.args[4].label,'native_click')
        self.assertFalse(result['task_verified'])


class AccessibilityTests(unittest.TestCase):
    def test_selection_requires_unchanged_enabled_hit_identity(self):
        before = scan()
        self.assertEqual(validate_selection(before,scan(),'1,2,3'),[20,30])
        variants = [scan(complete=False), scan(target_hwnd=456), scan(controls=[]),
                    scan(hit={'control_id':'1,2,3','matches':False,'point':[20,30]}),
                    scan(hit={'control_id':'other','matches':True,'point':[20,30]}),
                    scan(hit={'control_id':'1,2,3','matches':True,'point':[200,30]})]
        for field, value in [('box',[11,20,31,40]), ('enabled',False), ('name','Cancel'), ('pid',56)]:
            altered = scan()
            altered['controls'][0][field] = value
            variants.append(altered)
        duplicate = scan()
        duplicate['controls'].append(copy.deepcopy(duplicate['controls'][0]))
        variants.append(duplicate)
        for after in variants:
            with self.subTest(after=after), self.assertRaises(ValueError):
                validate_selection(before,after,'1,2,3')

    def test_native_probe_uses_argument_list_and_no_visible_window(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(scan()).encode(),b'')
        process.poll.return_value = 0
        with patch('clickwarden.accessibility.subprocess.Popen',return_value=process) as launch:
            result = inventory(123,name_filter='"; dangerous text')
        self.assertTrue(result['complete'])
        self.assertIsInstance(launch.call_args.args[0],list)
        self.assertEqual(launch.call_args.args[0][-1],'"; dangerous text')
        self.assertEqual(launch.call_args.kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)
        self.assertNotIn('shell',launch.call_args.kwargs)

    def test_hit_uses_a_validated_interior_point_when_the_center_fails(self):
        # Chromium's UIA hit test can intermittently resolve a point to the page
        # Document; a bounded interior point that resolves to the same control is
        # an acceptable click point, but it must still be inside the box.
        before = scan()
        after = scan()
        after['hit'] = {'control_id': '1,2,3', 'matches': True, 'point': [12, 22],
                        'fraction': [0.3, 0.3], 'candidates_tried': 3, 'center_ok': False,
                        'actual': {'role': 'ControlType.Button', 'box': [10, 20, 30, 40],
                                   'pid': 55}}
        self.assertEqual(validate_selection(before, after, '1,2,3'), [12, 22])

    def test_hit_candidate_outside_the_box_is_rejected(self):
        before = scan()
        after = scan()
        after['hit'] = {'control_id': '1,2,3', 'matches': True, 'point': [40, 60],
                        'fraction': [0.3, 0.3], 'candidates_tried': 2, 'center_ok': False,
                        'actual': {'role': 'ControlType.Button', 'box': [10, 20, 30, 40],
                                   'pid': 55}}
        with self.assertRaises(ValueError):
            validate_selection(before, after, '1,2,3')

    def test_refusal_names_the_actual_hit_evidence(self):
        before = scan()
        after = scan()
        after['hit'] = {'control_id': '1,2,3', 'matches': False, 'point': [20, 30],
                        'candidates_tried': 9, 'center_ok': False,
                        'actual': {'role': 'ControlType.Document', 'box': [0, 0, 500, 400],
                                   'name': 'page title', 'pid': 55}}
        with self.assertRaisesRegex(ValueError,
                                    'Control hit-test failed at every interior point'):
            validate_selection(before, after, '1,2,3')
        with self.assertRaisesRegex(ValueError, 'page title'):
            validate_selection(before, after, '1,2,3')

    def test_probe_hit_record_passes_candidate_metadata_through(self):
        process = Mock(returncode=0)
        record = scan()
        record['hit'].update(fraction=[0.5, 0.5], candidates_tried=1, center_ok=True)
        process.communicate.return_value = (json.dumps(record).encode(), b'')
        process.poll.return_value = 0
        with patch('clickwarden.accessibility.subprocess.Popen', return_value=process):
            result = inventory(123, selected_id='1,2,3')
        self.assertEqual(result['hit']['candidates_tried'], 1)
        self.assertTrue(result['hit']['center_ok'])

    def test_cancel_kills_only_probe_process(self):
        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired('probe',.05),(b'',b'')]
        calls = []
        def check():
            calls.append(1)
            if len(calls) >= 3:
                raise Cancelled('user')
        with patch('clickwarden.accessibility.subprocess.Popen',return_value=process), self.assertRaises(Cancelled):
            inventory(123,check=check)
        process.kill.assert_called_once()

    def test_provider_timeout_is_not_absence(self):
        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired('probe',.05),(b'',b'')]
        with patch('clickwarden.accessibility.subprocess.Popen',return_value=process), \
                patch('clickwarden.accessibility.time.monotonic',side_effect=[0,20]):
            result = inventory(123)
        self.assertFalse(result['complete'])
        self.assertEqual(result['stop_reason'],'provider_timeout')
        process.kill.assert_called_once()

    def test_native_click_single_use_and_zero_vision_calls(self):
        session = make_session()
        session.target_hwnd = 123
        session.input_check = Mock()
        session.point_check = Mock()
        session.engine.click_control_point.return_value = {'status':'success','task_verified':False}
        with patch('clickwarden.accessibility.inventory',side_effect=[scan(),scan()]):
            observed = session.controls()
            result = session.click_control(observed['snapshot_id'],'1,2,3')
        self.assertFalse(result['task_verified'])
        self.assertEqual(result['point_strategy'],'center')
        session.engine.click_control_point.assert_called_once_with([20,30],123,button='left',clicks=1)
        session.engine.inspect.assert_not_called()
        with self.assertRaises(ValueError):
            session.click_control(observed['snapshot_id'],'1,2,3')

    def test_native_click_reports_interior_fallback_strategy(self):
        # When Chromium resolves the exact centre to the page Document, a
        # validated interior point is used and the strategy is labelled; the
        # dispatched point is the validated fallback, not the failed centre.
        session = make_session()
        session.target_hwnd = 123
        session.input_check = Mock()
        session.point_check = Mock()
        session.engine.click_control_point.return_value = {'status':'success','task_verified':False}
        fallback = scan()
        fallback['hit'] = {'control_id':'1,2,3','matches':True,'point':[12,24],
                           'fraction':[0.3,0.3],'candidates_tried':4,'center_ok':False,
                           'actual':{'role':'ControlType.Button','box':[10,20,30,40],'pid':55}}
        with patch('clickwarden.accessibility.inventory',side_effect=[scan(),fallback]):
            observed = session.controls()
            result = session.click_control(observed['snapshot_id'],'1,2,3')
        self.assertEqual(result['point_strategy'],'interior_fallback')
        self.assertEqual(result['candidates_tried'],4)
        session.engine.click_control_point.assert_called_once_with([12,24],123,button='left',clicks=1)

    def test_own_panel_is_moved_before_native_hit_validation(self):
        session = make_session()
        session.target_hwnd = 123
        session.input_check = Mock()
        session.point_check = Mock()
        session.avoid_panel = Mock()
        session.engine.click_control_point.return_value = {'status':'success'}
        with patch('clickwarden.accessibility.inventory',return_value=scan()):
            observed = session.controls()
        def probe(*args,**kwargs):
            session.avoid_panel.assert_called_once_with([(20,30)])
            return scan()
        with patch('clickwarden.accessibility.inventory',side_effect=probe):
            session.click_control(observed['snapshot_id'],'1,2,3')

    def test_changed_control_never_reaches_input(self):
        session = make_session()
        session.target_hwnd = 123
        session.input_check = Mock()
        after = scan()
        after['controls'][0]['box'] = [40,50,60,70]
        with patch('clickwarden.accessibility.inventory',side_effect=[scan(),after]):
            observed = session.controls()
            with self.assertRaises(ValueError):
                session.click_control(observed['snapshot_id'],'1,2,3')
        session.engine.click_control_point.assert_not_called()

    def test_native_disappearance_requires_complete_unfiltered_readback(self):
        for complete, verified in ((True,True),(False,False)):
            session = make_session()
            session.target_hwnd = 123
            session.input_check = Mock()
            session.point_check = Mock()
            session.adopt_foreground = Mock()
            session.engine.click_control_point.return_value = {'status':'success','task_verified':False}
            with patch('clickwarden.accessibility.inventory',side_effect=[scan(),scan(),scan(controls=[],complete=complete)]) as probe:
                observed = session.controls(name_filter='OK')
                result = session.click_control(observed['snapshot_id'],'1,2,3',expect_gone=True)
            self.assertEqual(result['task_verified'],verified)
            self.assertEqual(probe.call_args.kwargs['name_filter'],'')

    def test_recovery_never_executes_model_guessed_shortcut(self):
        session = make_session()
        session.family = Mock(exe='unknown.exe')
        session.engine.inspect.return_value = {'answer':'{"keys":"ctrl,home"}'}
        with patch('clickwarden.sessions.enumerate_windows',return_value=[]):
            result = session.escalate('download',None)
        self.assertEqual(result['status'],'needs_attention')
        session.engine.inspect.assert_not_called()
        session.engine.hotkey.assert_not_called()

    def test_user_activity_resume_invalidates_native_snapshot(self):
        session = make_session()
        session.target_hwnd = 123
        session.family = Mock()
        session.point_check = Mock()
        with patch('clickwarden.sessions.foreground_hwnd',return_value=123), \
                patch('clickwarden.sessions.window_rect',return_value=(0,0,100,100)), \
                patch('clickwarden.accessibility.inventory',return_value=scan()):
            observed = session.run('controls',{})
            session.control.input_revision += 1
            session.control.pause('mouse activity')
            session.control.resume()
            result = session.run('click_control',{'snapshot_id':observed['snapshot_id'],'control_id':'1,2,3'})
        self.assertEqual(result['status'],'error')
        self.assertIn('snapshot',result['error'])
        session.engine.click_control_point.assert_not_called()

    def test_intervening_mutation_invalidates_native_snapshot(self):
        session = make_session()
        session.target_hwnd = 123
        session.family = Mock()
        session.engine.type.return_value = {'status':'success'}
        with patch('clickwarden.sessions.foreground_hwnd',return_value=123), \
                patch('clickwarden.sessions.window_rect',return_value=(0,0,100,100)), \
                patch('clickwarden.accessibility.inventory',return_value=scan()):
            observed = session.run('controls',{})
            session.run('type',{'text':'abc'})
            result = session.run('click_control',{'snapshot_id':observed['snapshot_id'],'control_id':'1,2,3'})
        self.assertEqual(result['status'],'error')
        session.engine.click_control_point.assert_not_called()

    def test_paused_native_observation_cannot_click(self):
        session = make_session()
        session.target_hwnd = 123
        session.family = Mock()
        session.control.pause('user')
        with patch('clickwarden.sessions.foreground_hwnd',return_value=123), \
                patch('clickwarden.sessions.window_rect',return_value=(0,0,100,100)), \
                patch('clickwarden.accessibility.inventory',return_value=scan()):
            observed = session.run('controls',{})
            self.assertEqual(observed['status'],'observed')
            result = session.run('click_control',{'snapshot_id':observed['snapshot_id'],'control_id':'1,2,3'})
        self.assertEqual(result['status'],'paused')
        session.engine.click_control_point.assert_not_called()

    def test_false_native_dispatch_never_runs_postcheck(self):
        session = make_session()
        session.target_hwnd = 123
        session.input_check = Mock()
        session.point_check = Mock()
        session.engine.click_control_point.return_value = {'status':'error'}
        with patch('clickwarden.accessibility.inventory',side_effect=[scan(),scan()]) as probe:
            observed = session.controls()
            result = session.click_control(observed['snapshot_id'],'1,2,3',expect_gone=True)
        self.assertEqual(result['status'],'error')
        self.assertEqual(probe.call_count,2)


if __name__ == '__main__':
    unittest.main()
