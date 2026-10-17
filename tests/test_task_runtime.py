"""Outcome and speed regressions; no user desktop mutations or model requests."""
import copy
from contextlib import contextmanager
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from clickwarden.desktop import LayoutTransactions, plan_layout, revision
from clickwarden.control import Cancelled, Paused
from test_sessions import make_session


class FakeView:
    def __init__(self):
        self.data={"hwnd":12,"process_stamp":100,"bounds":[0,0,640,500],
                   "spacing":[80,80],"auto_arrange":False,"coordinate_space":"shell_view",
                   "items":[{"id":str(i),"name":str(i),"position":[10,10+i*80]} for i in range(6)]}
        self.calls=[]
        self.ignore=False

    def snapshot(self):
        value=copy.deepcopy(self.data)
        value["revision"]=revision(value)
        return value

    def position(self,identity,point):
        self.calls.append(identity)
        if not self.ignore:
            next(i for i in self.data["items"] if i["id"]==identity)["position"]=list(point)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.view=FakeView()
        @contextmanager
        def connector():
            yield self.view
        self.tx=LayoutTransactions(self.temp.name,connector)

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_uses_native_facts_without_moving(self):
        data=self.tx.inventory()
        self.assertEqual(data["spacing"],[80,80])
        self.assertEqual(len(data["items"]),6)
        self.assertEqual(self.view.calls,[])

    def test_dry_run_does_not_write_or_move(self):
        result=self.tx.arrange("C",lambda:None,dry_run=True)
        self.assertEqual(result["status"],"planned")
        self.assertEqual(self.view.calls,[])
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_layout_verified_and_undo_restores_exact_originals(self):
        original=self.view.snapshot()
        result=self.tx.arrange("C",lambda:None)
        self.assertEqual(result["status"],"verified")
        self.assertEqual(result["verified"],6)
        self.assertEqual(len(self.view.calls),6)
        undo=self.tx.undo(result["transaction_id"],lambda:None)
        self.assertEqual(undo["status"],"verified")
        self.assertEqual(self.view.snapshot()["revision"],original["revision"])

    def test_successful_com_call_is_not_verified_movement(self):
        self.view.ignore=True
        result=self.tx.arrange("U",lambda:None)
        self.assertEqual(result["status"],"needs_attention")
        self.assertGreater(result["unchanged"],0)

    def test_partial_cancellation_reconciles_and_does_not_auto_undo(self):
        calls=[]
        def check():
            calls.append(1)
            if len(calls)==4:
                raise Cancelled("human takeover")
        result=self.tx.arrange("C",check)
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(len(self.view.calls),2)
        self.assertIn("Cancelled",result["error"])
        self.assertTrue(result["undo_available"])
        self.assertEqual(self.tx.undo(result["transaction_id"],lambda:None)["status"],"verified")

    def test_checkpoint_exists_before_first_position(self):
        original=self.view.position
        def position(identity,point):
            files=list(Path(self.temp.name).glob("*.json"))
            self.assertEqual(len(files),1)
            self.assertIn('"before"',files[0].read_text())
            original(identity,point)
        self.view.position=position
        self.tx.arrange("C",lambda:None)

    def test_undo_refuses_newer_user_changes(self):
        result=self.tx.arrange("C",lambda:None)
        self.view.data["items"][0]["position"]=[100,100]
        count=len(self.view.calls)
        with self.assertRaisesRegex(ValueError,"changed"):
            self.tx.undo(result["transaction_id"],lambda:None)
        self.assertEqual(len(self.view.calls),count)

    def test_auto_arrange_and_bad_ids_refuse_before_mutation(self):
        self.view.data["auto_arrange"]=True
        with self.assertRaisesRegex(ValueError,"Auto Arrange"):
            self.tx.arrange("C",lambda:None)
        self.view.data["auto_arrange"]=False
        with self.assertRaises(ValueError):
            self.tx.arrange("C",lambda:None,item_ids=["missing"])
        self.assertEqual(self.view.calls,[])

    def test_shapes_fit_and_have_no_duplicate_targets(self):
        for shape in ("C","U","grid"):
            plan=plan_layout(self.view.snapshot(),shape)
            points=[tuple(m["after"]) for m in plan["moves"]]
            self.assertEqual(len(set(points)),6)
            self.assertTrue(all(0<=x<640 and 0<=y<500 for x,y in points))

    def test_bad_shape_bounds_and_id_traversal_refuse(self):
        with self.assertRaises(ValueError):
            self.tx.arrange("X",lambda:None)
        with self.assertRaises(ValueError):
            self.tx.arrange("C",lambda:None,bounds=[-10,0,640,500])
        with self.assertRaises(ValueError):
            self.tx.undo("../file",lambda:None)


class RuntimeTests(unittest.TestCase):
    def test_drag_counts_are_not_verified_successes(self):
        s=make_session()
        s.ledger=[{"action":"batch_steps","results":[{"status":"success","verification":"unverified"} for _ in range(18)]}]
        outcomes=s.metrics()["outcomes"]
        self.assertEqual(outcomes["dispatched_unverified"],18)
        self.assertEqual(outcomes["native_position_readback"],0)
        self.assertEqual(outcomes["vision_checked"],0)

    def test_observation_works_while_paused_and_does_not_resume_input(self):
        s=make_session()
        s.family=Mock()
        s.target_hwnd=123
        s.engine.inspect.return_value={"success":True,"answer":"observed"}
        s.control.pause("human")
        with patch("clickwarden.sessions.foreground_hwnd",return_value=123),patch("clickwarden.sessions.window_rect",return_value=(0,0,600,400)):
            result=s.run("inspect",{"query":"what changed"})
        self.assertEqual(result["answer"],"observed")
        self.assertIsNotNone(s.first_productive_at)
        self.assertTrue(result["observation_only"])
        self.assertFalse(result["reusable_for_input"])
        self.assertTrue(s.control.paused.is_set())
        with self.assertRaises(Paused):
            s.input_check()

    def test_observation_gate_never_dispatches_input(self):
        s=make_session()
        s.read_only=True
        s.control.pause()
        s.check()
        with self.assertRaisesRegex(RuntimeError,"forbidden"):
            s.input_check()
        s.cancel()
        with self.assertRaises(Cancelled):
            s.check()

    def test_read_only_vision_survives_physical_pause(self):
        s=make_session()
        s.read_only=True
        def inspect():
            s.control.pause("mouse moved")
            return {"success":True,"answer":"fresh observation"}
        result=s.vision_call(inspect)
        self.assertTrue(result["success"])
        self.assertTrue(s.control.paused.is_set())

    def test_startup_has_no_model_preflight_or_pointer_probe(self):
        s=make_session()
        s.initial_hwnd=123
        s.control.pause("user is using the mouse")
        with patch("clickwarden.sessions.enumerate_windows",return_value=[{"hwnd":123,"title":"test"}]),patch("clickwarden.sessions.foreground_hwnd",return_value=123),patch("clickwarden.sessions.TargetFamily"),patch("clickwarden.sessions.inp.move_to") as move:
            s.prepare()
        self.assertEqual(s.error,"")
        move.assert_not_called()
        s.client.stream_chat.assert_not_called()

    def test_batch_validates_all_actions_before_dispatch(self):
        s=make_session()
        s.input_check=Mock()
        with self.assertRaises(ValueError):
            s.batch({"actions":[{"action":"type","text":"test"},{"action":"drag","start_x":1}]})
        s.engine.type.assert_not_called()

    def test_drag_batch_runs_in_one_call_and_reports_unverified(self):
        s=make_session()
        s.input_check=Mock()
        s.avoid_panel=Mock()
        s.engine.drag.return_value={"status":"success","outcome":"dispatched","verification":"unverified"}
        result=s.batch({"actions":[{"action":"drag","start_x":10,"start_y":20,"end_x":100,"end_y":200} for _ in range(18)]})
        self.assertEqual(result["steps"],18)
        self.assertEqual(result["dispatched"],18)
        self.assertEqual(result["verification"],"unverified")
        self.assertLessEqual(len(result["last_results"]),3)

    def test_reused_client_usage_is_per_session(self):
        s=make_session()
        s.base_usage={"total_requests":5,"prompt_tokens":100,"completion_tokens":10,"usage_reports":5}
        s.client.total_requests=7
        s.client.prompt_tokens=160
        s.client.completion_tokens=15
        s.client.usage_reports=7
        self.assertEqual(s.metrics()["requests"],2)
        self.assertEqual(s.metrics()["prompt_tokens"],60)

    def test_metrics_write_error_does_not_leave_action_lock_held(self):
        s=make_session()
        s.control.pause()
        s.save.side_effect=OSError("disk full")
        with self.assertRaises(OSError):
            s.run("type",{"text":"never sent"})
        self.assertFalse(s.operation_lock.locked())


if __name__=="__main__":
    unittest.main()
