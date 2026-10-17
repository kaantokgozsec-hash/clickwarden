"""WF-01 regressions: reproducible fixture baseline, fixture-side oracle, counters.

Offline only: no desktop input, screenshots, provider requests or real windows.
The live companion is tests/live_baseline.py (opt-in).
"""
import json
from pathlib import Path
import tempfile
import time
import unittest

from benchmarks.baseline import (OUTCOMES, BaselineRecorder, OutcomeCounters,
                                 completion_claim, timing_summary)
from fixtures import oracle
from fixtures.app import Fixture
from fixtures.layout import FIXTURE_VERSION, TINY_GAP, TINY_SIZES, build_layout, layout_hash, render_page
from clickwarden.control import Control


class LayoutDeterminismTests(unittest.TestCase):
    def test_same_seed_reproduces_layout_hash_and_expected_results(self):
        first, second = build_layout(1729), build_layout(1729)
        self.assertEqual(first, second)
        self.assertEqual(layout_hash(first), layout_hash(second))
        self.assertEqual([s["expect"] for s in first["scenarios"]],
                         [s["expect"] for s in second["scenarios"]])
        self.assertEqual(render_page(first), render_page(second))

    def test_different_seed_changes_seeded_geometry(self):
        values = {layout_hash(build_layout(seed)) for seed in (1, 2, 3, 4, 5, 6)}
        self.assertEqual(len(values), 6)

    def test_required_failure_features_are_present_and_adjacent(self):
        spec = build_layout(11)
        tiny = spec["tiny"]
        self.assertEqual([item["w"] for item in tiny], list(TINY_SIZES))
        for left, right in zip(tiny, tiny[1:]):
            self.assertEqual(right["x"] - (left["x"] + left["w"]), TINY_GAP)
        self.assertEqual(len({item["id"] for item in tiny}), len(tiny))

        saves = [item for item in spec["targets"] if item["label"] == "Save"]
        self.assertEqual(len(saves), 2)
        self.assertEqual(len({item["id"] for item in saves}), 2)
        self.assertGreater(saves[1]["x"], saves[0]["x"] + saves[0]["w"])

        self.assertGreaterEqual(spec["menu"]["delay_ms"], 300)
        self.assertGreaterEqual(len(spec["menu"]["items"]), 2)
        self.assertNotEqual(spec["rerender"]["initial"]["id"], spec["rerender"]["replacement"]["id"])
        self.assertNotEqual(spec["mover"]["box_a"], spec["mover"]["box_b"])
        self.assertEqual(spec["mover"]["decoy"]["x"], spec["mover"]["box_a"]["x"])
        self.assertEqual(spec["mover"]["decoy"]["y"], spec["mover"]["box_a"]["y"])
        overlay, covered = spec["overlay"]["box"], spec["overlay"]["covered"]
        self.assertLess(overlay["w"], covered["w"])
        self.assertEqual((overlay["x"], overlay["y"]), (covered["x"], covered["y"]))
        inner, outer = spec["scroll"]["inner"], spec["scroll"]["outer"]
        self.assertGreaterEqual(inner["w"], 0)
        self.assertGreaterEqual(inner["h"], 0)
        self.assertLessEqual(inner["h"], outer["h"])

    def test_scenarios_reference_real_targets_and_outcomes(self):
        spec = build_layout(23)
        ids = {item["id"] for item in spec["targets"]}
        deliberate = 0
        for scenario in spec["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                self.assertIn(scenario["expect"], OUTCOMES)
                if scenario.get("dispatch"):
                    self.assertIn(scenario["dispatch"], ids)
                if scenario["expect"] != "missing_target":
                    self.assertIn(scenario["target"], ids)
                if scenario.get("deliberate"):
                    deliberate += 1
        self.assertGreaterEqual(deliberate, 2)

    def test_layout_rejects_bad_seed(self):
        for seed in (-1, 2**31, 1.5, "7", None):
            with self.assertRaises(ValueError):
                build_layout(seed)

    def test_page_carries_revision_and_display_metadata(self):
        # The stale-report merge and the environment record depend on these
        # page-side fields; this guards the template against silent removal.
        page = render_page(build_layout(3))
        for token in ("page_id", "revision", "devicePixelRatio", "visualViewport"):
            self.assertIn(token, page)
        self.assertIn("state.revision++", page)


class OracleTests(unittest.TestCase):
    def test_wrong_click_is_counted_wrong_even_when_the_screen_changes(self):
        verdict = oracle.classify(intended="tiny-16", received=["tiny-20"],
                                  dispatched=True, claimed_success=True,
                                  screen_changed=True)
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertTrue(verdict["false_success"])
        self.assertNotEqual(verdict["outcome"], "success")
        self.assertIn("tiny-20", verdict["reason"])

    def test_screen_change_alone_never_promotes_to_success(self):
        verdict = oracle.classify(intended="tiny-16", received=[], dispatched=True,
                                  claimed_success=True, screen_changed=True)
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertTrue(verdict["false_success"])

    def test_success_requires_the_intended_target_as_sole_recipient(self):
        good = oracle.classify(intended="save-left", received=["save-left"], dispatched=True)
        self.assertEqual(good["outcome"], "success")
        self.assertFalse(good["false_success"])
        extra = oracle.classify(intended="save-left", received=["save-left", "save-right"],
                                dispatched=True)
        self.assertEqual(extra["outcome"], "wrong_dispatch")

    def test_receipts_override_a_refusal_claim(self):
        # The reviewer's reproducer: the tool reported aborted_focus, but the
        # fixture received a click on the wrong control.  That is wrong input,
        # never a safe refusal.
        verdict = oracle.classify(intended="tiny-16", received=["tiny-20"],
                                  dispatched=False, refused=True)
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertNotEqual(verdict["outcome"], "declined")
        self.assertTrue(verdict["evidence_of_dispatch"])
        self.assertIn("receipts prove input was delivered", verdict["reason"])

    def test_receipts_override_a_no_dispatch_claim_for_the_intended_target(self):
        verdict = oracle.classify(intended="save-left", received=["save-left"], dispatched=False)
        self.assertEqual(verdict["outcome"], "success")

    def test_duplicate_activation_of_the_intended_target_is_not_success(self):
        verdict = oracle.classify(intended="tiny-20", received=["tiny-20", "tiny-20"],
                                  dispatched=True, expected_count=1)
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertIn("observed 2", verdict["reason"])
        loose = oracle.classify(intended="scroll-inner", received=["scroll-inner"] * 3,
                                dispatched=True, expected_count=None, effect_ok=True)
        self.assertEqual(loose["outcome"], "success")
        self.assertEqual(loose["receipt_counts"], {"scroll-inner": 3})
        self.assertEqual(set(verdict["receipt_counts"]), {"tiny-20"})

    def test_missing_target_requires_a_complete_observation(self):
        absent = oracle.classify(intended="ghost", received=[], dispatched=False,
                                 target_observed=False)
        self.assertEqual(absent["outcome"], "missing_target")
        partial = oracle.classify(intended="ghost", received=[], dispatched=False,
                                  target_observed=None)
        self.assertEqual(partial["outcome"], "unknown")

    def test_declined_is_distinct_from_missing(self):
        verdict = oracle.classify(intended="tiny-16", received=[], dispatched=False,
                                  refused=True, target_observed=True,
                                  detail="focus left the target")
        self.assertEqual(verdict["outcome"], "declined")
        self.assertFalse(verdict["false_success"])

    def test_effect_failure_is_unverified_even_though_the_target_was_clicked(self):
        verdict = oracle.classify(intended="checkbox", received=["checkbox"],
                                  dispatched=True, effect_ok=False, claimed_success=True)
        self.assertNotEqual(verdict["outcome"], "success")
        self.assertEqual(verdict["outcome"], "unknown")
        self.assertTrue(verdict["false_success"])

    def test_verdict_from_receipts_uses_only_events_after_the_marker(self):
        events = [{"target": "save-right", "kind": "click"},
                  {"target": "save-left", "kind": "click"}]
        verdict = oracle.verdict_from_receipts("duplicate_label", "save-left", events, 1,
                                               dispatched=True)
        self.assertEqual(verdict["outcome"], "success")
        verdict = oracle.verdict_from_receipts("duplicate_label", "save-left", events, 0,
                                               dispatched=True)
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertIn("save-right", verdict["reason"])

    def test_unresolved_is_a_counted_unknown(self):
        verdict = oracle.unresolved("menu-open", "menu did not open")
        self.assertEqual(verdict["outcome"], "unknown")
        self.assertFalse(verdict["false_success"])

    def test_planner_done_is_not_completion(self):
        verdict = oracle.classify(intended="download", received=[], dispatched=True,
                                  claimed_success=True, claimed_by="planner_done",
                                  detail="model said the task is complete")
        self.assertEqual(verdict["outcome"], "wrong_dispatch")
        self.assertTrue(verdict["false_success"])


class CompletionClaimTests(unittest.TestCase):
    def test_dispatch_acknowledgements_are_not_completion_claims(self):
        click = {"status": "success", "action_status": "dispatched",
                 "verification": "unverified", "task_verified": False}
        batch = {"status": "completed", "steps": 3, "dispatched": 3,
                 "verification": "unverified", "last_results": []}
        self.assertFalse(completion_claim(click))
        self.assertFalse(completion_claim(batch))

    def test_explicit_outcome_claims_are_recognised(self):
        task = {"status": "completed", "steps": 4, "summary": "Task completed."}
        job = {"status": "completed", "stage": "Finished", "summary": "done",
               "verification": "vision_checked (not a drawing/geometry audit)"}
        verified = {"status": "success", "task_verified": True}
        self.assertTrue(completion_claim(task))
        self.assertTrue(completion_claim(job))
        self.assertTrue(completion_claim(verified))
        for value in ("completed", None, [], {"status": "success"}):
            with self.subTest(value=value):
                self.assertFalse(completion_claim(value))

    def test_entry_point_claims_are_scored_against_fixture_receipts(self):
        # Real result shapes from the four execution paths.  Dispatch
        # acknowledgements must not be flagged as false success, while an
        # explicit task/job completion claim is checked against the fixture.
        shapes = {
            "fast_click_control": {"status": "success", "action_status": "dispatched",
                                   "verification": "unverified", "task_verified": False},
            "fast_batch": {"status": "completed", "steps": 3, "dispatched": 3,
                           "verification": "unverified"},
            "fast_task": {"status": "completed", "steps": 4, "summary": "Task completed."},
            "fast_job_start": {"status": "completed", "stage": "Finished",
                               "verification": "vision_checked (not a drawing/geometry audit)"},
        }
        false_success_expected = {"fast_click_control": False, "fast_batch": False,
                                  "fast_task": True, "fast_job_start": True}
        for entry, result in shapes.items():
            with self.subTest(entry=entry):
                verdict = oracle.classify(
                    intended="tiny-16", received=["tiny-20"],
                    dispatched=result.get("status") in ("success", "completed"),
                    claimed_success=completion_claim(result), screen_changed=True)
                self.assertEqual(verdict["outcome"], "wrong_dispatch")
                self.assertEqual(verdict["false_success"], false_success_expected[entry])


class CounterTests(unittest.TestCase):
    def test_missing_declined_wrong_and_success_have_separate_counters(self):
        counters = OutcomeCounters()
        counters.add("missing_target")
        counters.add("declined")
        counters.add("wrong_dispatch")
        counters.add("success")
        counters.add("success", false_success=True)
        counted = counters.as_dict()
        self.assertEqual(counted["outcomes"], {"success": 2, "missing_target": 1,
                                               "declined": 1, "wrong_dispatch": 1,
                                               "unknown": 0})
        self.assertEqual(counted["flags"], {"false_success": 1})
        self.assertEqual(counted["total"], 5)

    def test_unknown_outcomes_are_rejected(self):
        counters = OutcomeCounters()
        with self.assertRaises(ValueError):
            counters.add("probably_fine")


class FixtureStateTests(unittest.TestCase):
    @staticmethod
    def page(page_id, revision, **overrides):
        value = {"page_id": page_id, "revision": revision, "sequence": revision,
                 "counters": {}, "events": [], "scroll": {}, "menu_open": False,
                 "moved": False, "rerendered": False}
        value.update(overrides)
        return value

    def test_reordered_reports_cannot_roll_back_state(self):
        fixture = Fixture(5)
        self.assertTrue(fixture.merge(self.page("p1", 4, menu_open=True)))
        self.assertFalse(fixture.merge(self.page("p1", 3, menu_open=False)))
        self.assertIs(fixture.snapshot()["menu_open"], True)

    def test_equal_revision_reports_are_rejected(self):
        fixture = Fixture(5)
        fixture.merge(self.page("p1", 2, moved=True))
        self.assertFalse(fixture.merge(self.page("p1", 2, moved=False)))
        self.assertIs(fixture.snapshot()["moved"], True)

    def test_flag_only_updates_with_a_new_revision_are_accepted(self):
        fixture = Fixture(5)
        fixture.merge(self.page("p1", 1))
        self.assertTrue(fixture.merge(self.page("p1", 2, menu_open=True)))
        self.assertIs(fixture.snapshot()["menu_open"], True)

    def test_new_page_load_replaces_the_previous_record(self):
        fixture = Fixture(5)
        fixture.merge(self.page("p1", 9, rerendered=True))
        self.assertTrue(fixture.merge(self.page("p2", 0)))
        self.assertIs(fixture.snapshot()["rerendered"], False)
        self.assertEqual(fixture.snapshot()["page_id"], "p2")


class RunnerPolicyTests(unittest.TestCase):
    def test_focus_pause_reason_can_hide_later_physical_input(self):
        # Control.pause() keeps the first reason while already paused, so a
        # focus reason is not evidence that no physical input arrived.  This is
        # exactly why the live runner must not resume from a reason string.
        control = Control(60)
        control.pause("Focus left the target application; refocus it and Resume")
        control.input_revision += 1
        control.last_input = {"kind": "mouse_move", "time": time.monotonic()}
        control.pause("Physical mouse moved 40px — paused")  # ignored: already paused
        self.assertEqual(control.reason,
                         "Focus left the target application; refocus it and Resume")
        self.assertIsNotNone(control.last_input)

    def test_live_runner_never_auto_resumes(self):
        source = (Path(__file__).resolve().parent / "live_baseline.py").read_text(encoding="utf-8")
        self.assertNotIn('command="resume"', source)
        self.assertNotIn("command='resume'", source)


class RecorderTests(unittest.TestCase):
    def make_recorder(self):
        spec = build_layout(7)
        return spec, BaselineRecorder(seed=spec["seed"], layout_hash=layout_hash(spec),
                                      fixture_version=FIXTURE_VERSION, suite="wf01",
                                      model="fixture-model", strategy="native_uia",
                                      screen={"virtual_screen": "test"},
                                      apps={"browser": "fixture"},
                                      runtime={"runtime_revision": "test-rev"})

    def test_report_records_seed_versions_timings_and_failures(self):
        spec, recorder = self.make_recorder()
        verdict = oracle.classify(intended="tiny-20", received=["tiny-20"], dispatched=True)
        recorder.record("tiny_click", verdict, duration_ms=12.5, expected="success")
        recorder.record("missing", oracle.classify(intended="ghost", received=[],
                                                   dispatched=False, target_observed=False),
                        duration_ms=3.0)
        recorder.record("wrong", oracle.classify(intended="tiny-16", received=["tiny-20"],
                                                 dispatched=True, claimed_success=True),
                        duration_ms=9.5, deliberate=True)
        recorder.failure("delayed_menu_item", "item never appeared")
        report = recorder.report()

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["seed"], spec["seed"])
        self.assertEqual(report["layout_hash"], layout_hash(spec))
        self.assertEqual(report["fixture_version"], FIXTURE_VERSION)
        self.assertEqual(report["model"], "fixture-model")
        self.assertEqual(report["runtime"], {"runtime_revision": "test-rev"})
        self.assertEqual(report["counters"]["outcomes"],
                         {"success": 1, "missing_target": 1, "declined": 0,
                          "wrong_dispatch": 1, "unknown": 0})
        self.assertEqual(report["counters"]["flags"]["false_success"], 1)
        self.assertEqual(report["timings"]["count"], 3)
        self.assertEqual(report["timings"]["p95_ms"], 12.5)
        self.assertEqual(report["failures"], [{"step": "delayed_menu_item",
                                               "error": "item never appeared"}])
        self.assertTrue(report["steps"][2]["deliberate"])
        self.assertTrue(report["steps"][2]["false_success"])
        self.assertFalse(report["steps"][0]["false_success"])
        self.assertIn("python", report["environment"])
        self.assertEqual(report["environment"]["apps"], {"browser": "fixture"})

    def test_recorded_tracks_one_result_per_scenario(self):
        _, recorder = self.make_recorder()
        self.assertFalse(recorder.recorded("tiny_click"))
        recorder.record("tiny_click", oracle.classify(intended="tiny-20", received=["tiny-20"],
                                                      dispatched=True))
        self.assertTrue(recorder.recorded("tiny_click"))

    def test_unresolved_outcomes_are_counted(self):
        _, recorder = self.make_recorder()
        recorder.record("delayed_menu_item", oracle.unresolved("item-beta", "menu never opened"),
                        expected="success", detail="setup failure")
        report = recorder.report()
        self.assertEqual(report["counters"]["outcomes"]["unknown"], 1)
        self.assertEqual(report["steps"][0]["actual"], "unknown")
        self.assertEqual(report["steps"][0]["expected"], "success")

    def test_report_round_trips_through_json_file(self):
        _, recorder = self.make_recorder()
        recorder.record("tiny_click", oracle.classify(intended="tiny-20",
                                                      received=["tiny-20"], dispatched=True),
                        duration_ms=1.0)
        with tempfile.TemporaryDirectory() as folder:
            path = recorder.write(Path(folder) / "nested" / "baseline.json",
                                  extra={"checks": {"mismatches": 0}, "passed": True})
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written, recorder.last_report)
        self.assertTrue(written["passed"])
        self.assertEqual(written["checks"], {"mismatches": 0})
        self.assertEqual(written["counters"]["outcomes"]["success"], 1)

    def test_timing_summary_never_invents_samples(self):
        self.assertEqual(timing_summary([]), {"count": 0, "p50_ms": None, "p95_ms": None,
                                              "max_ms": None, "total_ms": 0.0})
        values = timing_summary([4, 1, 3, 2])
        self.assertEqual(values["count"], 4)
        self.assertEqual(values["max_ms"], 4.0)
        self.assertEqual(values["total_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
