"""Opt-in WF-01 baseline: reproducible failure suite over a disposable fixture.

Runs one local Edge/Chrome page fixture through a fresh text-only Clickwarden MCP
connection.  Every interaction uses native UIA control observation and guarded
input; there are no vision requests.  Fixture-side events decide the outcome:

    python tests/live_baseline.py [--seed 1729] [--chrome]

The runner records which element actually received input and writes a baseline
report (seed, layout hash, environment, model, strategy, timings, failures and
separate counters) under ``benchmarks/results/``.

Safety policy: the runner never auto-resumes.  A focus pause and later physical
input can share the same preserved pause reason, and there is no client-side
atomic check that input cannot arrive between a decision and a resume, so any
pause stops this suite and waits for an explicit user resume.  Because of that,
the focus-stealer scenario runs last.  Receipt evidence always overrides a
tool's dispatch/refusal claim: input that actually reached the wrong control is
counted wrong even if the tool reported a refusal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from benchmarks.baseline import BaselineRecorder, completion_claim  # noqa: E402
from fixtures import app as fixture_app  # noqa: E402
from fixtures import oracle  # noqa: E402
from fixtures.layout import FIXTURE_VERSION, build_layout  # noqa: E402

REFUSED_STATUSES = ("paused", "aborted_focus", "aborted", "cancelled")
REFUSAL_MARKERS = ("hit-test failed", "Control missing or ambiguous", "Control changed",
                   "Control is disabled", "scan incomplete", "Click blocked",
                   "Target window changed", "No current control snapshot", "changed since observation")


def read_line(process, timeout=6.0):
    box = queue.Queue()

    def reader():
        try:
            box.put(process.stdout.readline())
        except Exception:
            box.put("")

    threading.Thread(target=reader, daemon=True).start()
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        return ""


def browser_path(name):
    executable = ("Google/Chrome/Application/chrome.exe" if name == "chrome"
                  else "Microsoft/Edge/Application/msedge.exe")
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        path = Path(base) / executable
        if path.is_file():
            return path
    return None


def browser_file_version(path) -> str:
    """Best-effort file version of the browser executable; empty when unknown."""
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return ""


def leaf_error(exc) -> str:
    """Unwrap ExceptionGroup nesting so reports name the underlying failure."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return "%s: %s" % (type(exc).__name__, exc)


class LiveBaseline:
    def __init__(self, seed, browser="edge"):
        self.seed = seed
        self.browser = browser
        self.spec = build_layout(seed)
        self.fixture = fixture_app.Fixture(seed)
        self.recorder = None
        self.sid = ""
        self.client = None
        self.browser_hwnd = 0
        self.stealer = None
        self.takeover = False
        self.cleaned = False

    # -- fixture truth --------------------------------------------------------
    def state(self):
        return self.fixture.snapshot()

    def events(self, marker):
        return self.state()["events"][marker:]

    def counter(self, target):
        return self.state()["counters"].get(target, 0)

    async def settle(self, predicate, timeout=4.0):
        deadline = time.monotonic() + timeout
        while True:
            value = self.state()
            if predicate(value):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(.05)

    async def quiet(self, stable_seconds=0.4, timeout=3.0):
        """Wait until the fixture stops emitting events.

        Smooth scrolling emits a tail of scroll events after the wheel; without
        draining it, those events would leak into the next scenario's receipt
        window and be misattributed (e.g. make an absent-target scan look like a
        wrong dispatch).
        """
        deadline = time.monotonic() + timeout
        last = len(self.state()["events"])
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(.05)
            count = len(self.state()["events"])
            if count != last:
                last = count
                stable_since = time.monotonic()
                continue
            if time.monotonic() - stable_since >= stable_seconds:
                return True
        return len(self.state()["events"]) == last

    # -- MCP helpers ----------------------------------------------------------
    async def call(self, name, **kwargs):
        response = await self.client.call_tool(name, kwargs)
        assert not getattr(response, "is_error", getattr(response, "isError", False)), response
        assert all(c.type == "text" for c in response.content)
        return json.loads(response.content[0].text)

    async def observed(self, name):
        for _ in range(8):
            value = await self.call("fast_controls", name_filter=name, session_id=self.sid,
                                    max_seconds=5, limit=400)
            if value.get("complete") and any(c["name"] == name for c in value["controls"]):
                return value, [c for c in value["controls"] if c["name"] == name]
            await asyncio.sleep(.25)
        return value, []

    def score(self, name, intended, *, result, marker, expect, deliberate=False,
              target_observed=None, claimed_success=None, effect_ok=None, refused=None,
              duration_ms=None, detail="", expected_count=1, kinds=("click",)):
        """Record exactly one structured verdict for one scenario.

        ``claimed_success`` defaults to an explicit outcome-completion claim
        (``benchmarks.baseline.completion_claim``), never to a bare dispatch
        acknowledgement.  Fixture receipts override both the dispatch and the
        refusal claim inside the oracle.  ``kinds`` selects which receipt kinds
        count for this scenario so unrelated event tails cannot be attributed.
        """
        status = result.get("status") if isinstance(result, dict) else None
        error = str(result.get("error", "")) if isinstance(result, dict) else ""
        claimed_dispatch = status == "success"
        if refused is None:
            refused = status in REFUSED_STATUSES or any(m in error for m in REFUSAL_MARKERS)
        if claimed_success is None:
            claimed_success = completion_claim(result)
        received = [e.get("target") for e in self.events(marker)
                    if e.get("kind") in kinds]
        verdict = oracle.classify(
            intended=intended, received=received, dispatched=claimed_dispatch,
            refused=refused, target_observed=target_observed,
            claimed_success=claimed_success, effect_ok=effect_ok,
            expected_count=expected_count, screen_changed=bool(received),
            claimed_by=name, detail=detail or error)
        detail_text = detail or error
        if isinstance(result, dict) and result.get("point_strategy"):
            detail_text = ("%s; point_strategy=%s" % (detail_text, result["point_strategy"]))
            if result.get("candidates_tried"):
                detail_text += " candidates_tried=%s" % result["candidates_tried"]
        self.recorder.record(name, verdict, expected=expect, deliberate=deliberate,
                             duration_ms=duration_ms, detail=detail_text)
        if verdict["outcome"] != expect:
            raise AssertionError({"scenario": name, "expected": expect, "verdict": verdict,
                                  "result": result})
        return verdict

    async def click_control(self, name, started=None, receipt_timeout=2.0):
        """Observe, click and wait for the receipt; duration covers the whole step."""
        start = time.perf_counter() if started is None else started
        snapshot, items = await self.observed(name)
        assert len(items) == 1, {"observed": name, "count": len(items), "scan": snapshot}
        marker = len(self.state()["events"])
        result = await self.call("fast_click_control", snapshot_id=snapshot["snapshot_id"],
                                 control_id=items[0]["control_id"], session_id=self.sid)
        self.ensure_not_user_takeover(result)
        await self.after_dispatch(marker, timeout=receipt_timeout)
        return marker, result, (time.perf_counter() - start) * 1000

    async def after_dispatch(self, marker, timeout=2.0):
        await self.settle(lambda s: len(s["events"]) > marker, timeout=timeout)

    def ensure_not_user_takeover(self, result):
        """A physical pause is a real takeover: stop, never resume it.

        Guard refusals (occlusion, failsafe, stale snapshot) are recorded as
        declined by the caller and do not stop the suite.
        """
        if (self.stealer is None and isinstance(result, dict)
                and result.get("status") == "paused"):
            self.takeover = True
            raise RuntimeError("session paused during an action: " +
                               str(result.get("error") or result.get("reason") or result))

    async def dispatch_at(self, x, y, started=None, receipt_timeout=2.0):
        """Dispatch a coordinate click (same-point drag) and wait for the receipt."""
        start = time.perf_counter() if started is None else started
        marker = len(self.state()["events"])
        result = await self.call("fast_drag", start_x=int(x), start_y=int(y),
                                 end_x=int(x), end_y=int(y), session_id=self.sid)
        self.ensure_not_user_takeover(result)
        await self.after_dispatch(marker, timeout=receipt_timeout)
        return marker, result, (time.perf_counter() - start) * 1000

    def center(self, box):
        return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2

    async def check_takeover(self):
        status = await self.call("fast_session_status", session_id=self.sid)
        if status.get("status") == "paused":
            self.takeover = True
            raise RuntimeError("session paused (physical input or focus): " +
                               str(status.get("pause_reason", "")))

    # -- scenarios ------------------------------------------------------------
    async def run_scenarios(self):
        """Run the seeded matrix; exactly one structured result per scenario.

        Setup failures, refusals, timeouts and skipped dependencies are recorded
        as counted outcomes (never as bare failures).  A real user takeover
        stops the run immediately.
        """
        scenario = {item["id"]: item for item in self.spec["scenarios"]}
        steps = (
            ("tiny_click", scenario["tiny_click"]["expect"],
             lambda: self.st_tiny_click(scenario["tiny_click"])),
            ("tiny_injected_wrong", scenario["tiny_injected_wrong"]["expect"],
             lambda: self.st_tiny_injected_wrong(scenario["tiny_injected_wrong"])),
            ("duplicate_label", scenario["duplicate_label"]["expect"],
             lambda: self.st_duplicate_label(scenario["duplicate_label"])),
            ("delayed_menu_open", scenario["delayed_menu_open"]["expect"],
             lambda: self.st_delayed_menu_open(scenario["delayed_menu_open"])),
            ("delayed_menu_item", scenario["delayed_menu_item"]["expect"],
             lambda: self.st_delayed_menu_item(scenario["delayed_menu_item"])),
            ("rerender", scenario["rerender"]["expect"],
             lambda: self.st_rerender(scenario["rerender"])),
            ("rerender_replacement", scenario["rerender_replacement"]["expect"],
             lambda: self.st_rerender_replacement(scenario["rerender_replacement"])),
            ("mover_fresh", scenario["mover_fresh"]["expect"],
             lambda: self.st_mover_fresh(scenario["mover_fresh"])),
            ("mover_stale", scenario["mover_stale"]["expect"],
             lambda: self.st_mover_stale(scenario["mover_stale"])),
            ("overlay_refusal", scenario["overlay_refusal"]["expect"],
             lambda: self.st_overlay_refusal(scenario["overlay_refusal"])),
            ("nested_scroll", scenario["nested_scroll"]["expect"],
             lambda: self.st_nested_scroll(scenario["nested_scroll"])),
            ("missing_target", scenario["missing_target"]["expect"],
             lambda: self.st_missing_target(scenario["missing_target"])),
            # Last: this scenario leaves the session paused and is never resumed
            # automatically (see the module docstring).
            ("focus_steal_refusal", "declined", self.st_focus_steal_refusal),
        )
        for name, expected, step in steps:
            try:
                await self.quiet()
                await step()
            except Exception as exc:
                if self.takeover:
                    raise RuntimeError("stopped at %s after user takeover" % name) from exc
                self.recorder.failure(name, "%s: %s" % (type(exc).__name__, exc))
                if not self.recorder.recorded(name):
                    self.recorder.record(
                        name,
                        oracle.unresolved(scenario.get(name, {}).get("target", ""),
                                          "scenario did not complete: %s" % exc),
                        expected=expected, detail="setup failure")
            if self.takeover:
                raise RuntimeError("stopped at %s after user takeover" % name)

    async def st_tiny_click(self, scenario):
        """Native click on one adjacent tiny target; the neighbors must stay untouched."""
        started = time.perf_counter()
        marker, result, duration = await self.click_control("tiny-20", started=started)
        self.score("tiny_click", "tiny-20", result=result, marker=marker, duration_ms=duration,
                   expect=scenario["expect"], expected_count=1)
        assert [self.counter(f"tiny-{size}") for size in (16, 32, 64)] == [0, 0, 0], self.state()
        assert self.counter("tiny-20") == 1, self.state()
        await self.check_takeover()

    async def st_tiny_injected_wrong(self, scenario):
        """Aim at tiny-16, dispatch at tiny-64; the screen changes yet the oracle
        must record a wrong dispatch (this is the deliberately injected claim)."""
        started = time.perf_counter()
        snapshot, items = await self.observed("tiny-64")
        assert len(items) == 1, items
        x, y = self.center(items[0]["box"])
        marker, result, duration = await self.dispatch_at(x, y, started=started)
        self.score("tiny_injected_wrong", "tiny-16", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], deliberate=True,
                   claimed_success=True, expected_count=1,
                   detail="deliberately dispatched at the tiny-64 neighbor")
        assert self.counter("tiny-64") == 1 and self.counter("tiny-16") == 0, self.state()
        await self.check_takeover()

    async def st_duplicate_label(self, scenario):
        """Both controls are called "Save"; clicking the left one must not touch the right."""
        started = time.perf_counter()
        snapshot, items = await self.observed("Save")
        assert len(items) == 2, {"observed_save": items}
        wanted = min(items, key=lambda item: item["box"][0])
        marker = len(self.state()["events"])
        result = await self.call("fast_click_control", snapshot_id=snapshot["snapshot_id"],
                                 control_id=wanted["control_id"], session_id=self.sid)
        self.ensure_not_user_takeover(result)
        await self.after_dispatch(marker)
        duration = (time.perf_counter() - started) * 1000
        self.score("duplicate_label", "save-left", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], expected_count=1)
        assert self.counter("save-left") == 1 and self.counter("save-right") == 0, self.state()
        await self.check_takeover()

    async def st_delayed_menu_open(self, scenario):
        """The menu appears only after the seeded delay."""
        started = time.perf_counter()
        marker, result, _ = await self.click_control("menu-open", started=started)
        opened = await self.settle(lambda s: s.get("menu_open") is True, timeout=4)
        duration = (time.perf_counter() - started) * 1000
        self.score("delayed_menu_open", "menu-open", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], expected_count=1,
                   effect_ok=opened)
        assert opened, "delayed menu did not open"
        await self.check_takeover()

    async def st_delayed_menu_item(self, scenario):
        started = time.perf_counter()
        marker, result, duration = await self.click_control("item-beta", started=started)
        self.score("delayed_menu_item", "item-beta", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], expected_count=1)
        assert self.counter("item-beta") == 1, self.state()
        await self.check_takeover()

    async def st_rerender(self, scenario):
        """The original node is replaced by a new runtime identity."""
        started = time.perf_counter()
        marker, result, _ = await self.click_control("rerender", started=started)
        replaced = await self.settle(lambda s: s.get("rerendered") is True, timeout=3)
        duration = (time.perf_counter() - started) * 1000
        self.score("rerender", "rerender", result=result, marker=marker, duration_ms=duration,
                   expect=scenario["expect"], expected_count=1, effect_ok=replaced)
        assert replaced, "button did not rerender"
        await self.check_takeover()

    async def st_rerender_replacement(self, scenario):
        started = time.perf_counter()
        marker, result, duration = await self.click_control("rerender-2", started=started)
        self.score("rerender_replacement", "rerender-2", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], expected_count=1)
        assert self.counter("rerender-2") == 1, self.state()
        await self.check_takeover()

    async def st_mover_fresh(self, scenario):
        """Click after the move; the decoy in the old slot must not react."""
        started = time.perf_counter()
        moved = await self.settle(lambda s: s.get("moved") is True, timeout=5)
        assert moved, "mover did not move"
        marker, result, duration = await self.click_control("mover", started=started)
        self.score("mover_fresh", "mover", result=result, marker=marker, duration_ms=duration,
                   expect=scenario["expect"], expected_count=1)
        assert self.counter("mover") == 1 and self.counter("mover-decoy") == 0, self.state()
        await self.check_takeover()

    async def st_mover_stale(self, scenario):
        """Dispatch where a stale observation of the mover would point (the decoy
        now occupies it); the decoy reacts but the oracle still counts it wrong."""
        started = time.perf_counter()
        snapshot, items = await self.observed("mover-decoy")
        assert len(items) == 1, items
        x, y = self.center(items[0]["box"])
        marker, result, duration = await self.dispatch_at(x, y, started=started)
        self.score("mover_stale", "mover", result=result, marker=marker, duration_ms=duration,
                   expect=scenario["expect"], deliberate=True, claimed_success=True,
                   expected_count=1, detail="deliberately dispatched at the stale mover position")
        assert self.counter("mover-decoy") == 1, self.state()
        await self.check_takeover()

    async def st_overlay_refusal(self, scenario):
        """The covered control's center hit-tests to the overlay, so the guard
        must refuse before any input reaches the fixture."""
        started = time.perf_counter()
        snapshot, items = await self.observed("covered")
        assert len(items) == 1, items
        marker = len(self.state()["events"])
        result = await self.call("fast_click_control", snapshot_id=snapshot["snapshot_id"],
                                 control_id=items[0]["control_id"], session_id=self.sid)
        duration = (time.perf_counter() - started) * 1000
        self.score("overlay_refusal", "covered", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], target_observed=True,
                   refused=True)
        assert len(self.events(marker)) == 0, self.state()
        await self.check_takeover()

    async def st_focus_steal_refusal(self):
        """Our own foreign window takes the foreground; the next click must be
        refused with no dispatch.

        The session is deliberately left paused afterwards: this scenario is
        last and the runner never auto-resumes (see the module docstring).
        """
        self.stealer = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "fixtures" / "app.py"),
             "--steal", "--seconds", "15"],
            creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        try:
            ready = read_line(self.stealer, timeout=8)
            assert ready.startswith("STEALER_READY"), {"stealer": ready}
            stealer_hwnd = int(ready.split("hwnd=")[1].split()[0]) if "hwnd=" in ready else 0
            import win32gui
            foreground_owned = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if stealer_hwnd and win32gui.GetForegroundWindow() == stealer_hwnd:
                    foreground_owned = True
                    break
                await asyncio.sleep(.1)
            assert foreground_owned, {"stealer": ready,
                                      "foreground": win32gui.GetForegroundWindow()}
            started = time.perf_counter()
            snapshot, items = await self.observed("Save")
            assert len(items) == 2, {"observed_save": items}
            wanted = min(items, key=lambda item: item["box"][0])
            marker = len(self.state()["events"])
            result = await self.call("fast_click_control", snapshot_id=snapshot["snapshot_id"],
                                     control_id=wanted["control_id"], session_id=self.sid)
            duration = (time.perf_counter() - started) * 1000
            status = await self.call("fast_session_status", session_id=self.sid)
            detail = ("runner-owned focus stealer owned the foreground; pause_reason=%s"
                      % status.get("pause_reason", ""))
            self.score("focus_steal_refusal", "save-left", result=result, marker=marker,
                       duration_ms=duration, expect="declined", target_observed=True,
                       refused=True, detail=detail)
            assert len(self.events(marker)) == 0, self.state()
        finally:
            self.stop_stealer()

    async def st_nested_scroll(self, scenario):
        """A wheel over the inner region scrolls the inner region, not the outer
        one, and the fixture records the inner as the receiver."""
        started = time.perf_counter()
        snapshot, items = await self.observed("scroll-inner")
        assert len(items) == 1, items
        x, y = self.center(items[0]["box"])
        marker = len(self.state()["events"])
        result = await self.call("fast_scroll", clicks=-3, x=x, y=y, session_id=self.sid)
        self.ensure_not_user_takeover(result)
        scrolled = await self.settle(lambda s: s["scroll"].get("scroll-inner", 0) > 0, timeout=3)
        duration = (time.perf_counter() - started) * 1000
        self.score("nested_scroll", "scroll-inner", result=result, marker=marker,
                   duration_ms=duration, expect=scenario["expect"], effect_ok=scrolled,
                   expected_count=None, kinds=("scroll",))
        assert scrolled and self.state()["scroll"].get("scroll-outer", 0) == 0, self.state()
        await self.check_takeover()

    async def st_missing_target(self, scenario):
        """A complete scan finds nothing; no input may be dispatched."""
        started = time.perf_counter()
        ghost = "missing-%d-%s" % (self.seed, uuid.uuid4().hex[:6])
        marker = len(self.state()["events"])
        result = await self.call("fast_controls", name_filter=ghost, session_id=self.sid,
                                 max_seconds=5, limit=400)
        duration = (time.perf_counter() - started) * 1000
        assert result.get("complete") is True, result
        assert not [c for c in result["controls"] if c["name"] == ghost], result
        self.score("missing_target", ghost, result={"status": "absent"}, marker=marker,
                   expect=scenario["expect"], target_observed=False, duration_ms=duration,
                   kinds=("click", "scroll"))
        assert len(self.events(marker)) == 0, self.state()

    # -- lifecycle ------------------------------------------------------------
    def stop_stealer(self):
        if self.stealer and self.stealer.poll() is None:
            self.stealer.kill()
        if self.stealer:
            try:
                self.stealer.communicate(timeout=5)
            except Exception:
                pass
        self.stealer = None

    async def run(self):
        report_path = ROOT / "benchmarks" / "results" / ("wf01-live-%d-%s.json" % (
            self.seed, time.strftime("%Y%m%d-%H%M%S")))
        server, url = self.fixture.serve()
        browser = browser_path(self.browser)
        assert browser, f"a local {self.browser} installation is required"
        child = None
        report = None
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "clickwarden_mcp.py")],
            env={**os.environ, "CLICKWARDEN_TEXT_ONLY": "1"})
        try:
            with tempfile.TemporaryDirectory(prefix="clickwarden-baseline-") as profile:
                try:
                    async with stdio_client(params) as (reader, writer):
                        async with ClientSession(reader, writer) as client:
                            self.client = client
                            await client.initialize()
                            try:
                                caps = await self.call("fast_capabilities")
                                screen = await self.call("get_screen_info")
                                self.recorder = BaselineRecorder(
                                    seed=self.seed, layout_hash=self.fixture.layout_hash,
                                    fixture_version=FIXTURE_VERSION, suite="wf01-live",
                                    model=screen.get("model"), strategy="native_uia",
                                    screen={"virtual_screen": screen.get("virtual_screen"),
                                            "dpi_mode": screen.get("dpi_mode"),
                                            "vision_configured": screen.get("vision_configured")},
                                    apps={"browser": self.browser, "fixture": "v%d" % FIXTURE_VERSION},
                                    runtime={"runtime_revision": caps.get("runtime_revision"),
                                             "pid": caps.get("pid")})
                                self.sid = (await self.call("use_my_computer",
                                    objective="WF-01 disposable fixture baseline",
                                    app_or_url="desktop"))["session_id"]
                                for _ in range(25):
                                    status = await self.call("fast_session_status", session_id=self.sid)
                                    if status["status"] != "preparing":
                                        break
                                    await asyncio.sleep(.2)
                                assert status["status"] == "idle", status
                                version = browser_file_version(browser)
                                if version:
                                    self.recorder.environment["apps"]["browser_version"] = version
                                child = subprocess.Popen(
                                    [str(browser), "--no-first-run", "--no-default-browser-check",
                                     "--disable-background-mode", f"--user-data-dir={profile}",
                                     "--inprivate", "--window-size=1100,880",
                                     "--window-position=140,120", "--new-window", url],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                                import win32process
                                self.browser_hwnd = 0
                                page = {}
                                for _ in range(40):
                                    windows = await self.call("list_windows")
                                    self.browser_hwnd = next((w["hwnd"] for w in windows if
                                        win32process.GetWindowThreadProcessId(w["hwnd"])[1] == child.pid), 0)
                                    page = self.state()
                                    if self.browser_hwnd and page.get("page_id"):
                                        break
                                    await asyncio.sleep(.25)
                                assert self.browser_hwnd and page.get("page_id"), \
                                    "local fixture page did not load"
                                self.recorder.environment["screen"]["page_dpr"] = page.get("dpr")
                                self.recorder.environment["screen"]["page_viewport"] = page.get("viewport")
                                self.recorder.environment["screen"]["page_visual_scale"] = page.get("visual_scale")
                                switched = await self.call("fast_switch_window",
                                                           hwnd=self.browser_hwnd, session_id=self.sid)
                                assert (switched["status"] == "success"
                                        and switched["fresh_observation_required"]), switched
                                await self.run_scenarios()
                                report = self.finish(report_path)
                            finally:
                                await self.cleanup()
                finally:
                    if child and child.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                        child.wait(timeout=5)
                    await asyncio.sleep(.4)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            note = leaf_error(exc)
            if self.recorder:
                self.recorder.failure("run", note)
                report = self.finish(report_path, error=note)
            else:
                report = {"passed": False, "seed": self.seed, "error": note,
                          "report": str(report_path)}
        finally:
            server.shutdown()
            server.server_close()
        print(json.dumps({k: report[k] for k in ("passed", "seed", "counters", "failures",
                                                 "report", "error") if k in report},
                         ensure_ascii=False), flush=True)
        return 0 if report.get("passed") else 1

    def finish(self, report_path, error=""):
        checks = {
            "scenarios": len(self.recorder.steps),
            "mismatches": sum(1 for s in self.recorder.steps
                              if s["expected"] and s["actual"] != s["expected"]),
            "unintended_wrong_dispatch": sum(1 for s in self.recorder.steps
                                             if s["actual"] == "wrong_dispatch" and not s["deliberate"]),
            "unintended_false_success": sum(1 for s in self.recorder.steps
                                            if s["false_success"] and not s["deliberate"]),
            "deliberate_detected": sum(1 for s in self.recorder.steps
                                       if s["deliberate"] and s["actual"] == s["expected"]),
        }
        passed = (not self.recorder.failures and not error and checks["mismatches"] == 0
                  and checks["unintended_wrong_dispatch"] == 0
                  and checks["unintended_false_success"] == 0
                  and checks["deliberate_detected"] >= 2
                  and checks["scenarios"] == len(self.spec["scenarios"]) + 1)  # + focus_steal_refusal
        extra = {"checks": checks, "passed": passed}
        if error:
            extra["error"] = error
        path = self.recorder.write(report_path, extra=extra)
        summary = {"passed": passed, "seed": self.seed, "report": str(path),
                   "counters": self.recorder.report()["counters"], "checks": checks,
                   "failures": list(self.recorder.failures)}
        if error:
            summary["error"] = error
        return summary

    async def cleanup(self):
        if self.cleaned:
            return
        self.cleaned = True
        self.stop_stealer()
        if self.sid and self.client:
            try:
                await self.client.call_tool("fast_session_end", {"session_id": self.sid,
                                                                 "result": "stopped"})
            except Exception:
                pass
        self.sid = ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clickwarden WF-01 live baseline")
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--chrome", action="store_true", help="use Chrome instead of Edge")
    args = parser.parse_args(argv)
    baseline = LiveBaseline(seed=args.seed, browser="chrome" if args.chrome else "edge")
    return asyncio.run(baseline.run())


if __name__ == "__main__":
    raise SystemExit(main())
