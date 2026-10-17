"""
clickwarden.server — MCP server (stdio) exposing the fast engine.

    python -m clickwarden.server

Latency-first tools: ``fast_click`` (streaming, reticle), ``fast_batch``
and ``fast_task`` (warm autonomous loops). Results carry a ``latency`` block
so the caller can see exactly where time went.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Allow both `python -m clickwarden.server` and `python <path>/server.py`
# (MCP clients launch the file directly).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Without a package context the relative imports below raise ImportError;
    # PEP 366 lets a directly executed module declare its package.
    __package__ = "clickwarden"

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # older/newer packaging of the mcp SDK
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

from . import providers
from .engine import Engine
from .input import CornerAbort, ForegroundGuardError
from .vision import client as vision_client
from .jobs import JobManager
from .sessions import SessionManager
from .win32core import (
    ensure_dpi_aware, attach_desktop, cursor_pos, enumerate_windows, focus_window,
    virtual_screen, is_control_surface,
)

# --- process-level Win32 hygiene -------------------------------------------
DPI_MODE = ensure_dpi_aware()
attach_desktop()

try:  # instructions are shown to the connecting AI client before it calls any tool
    app = MCPServer("clickwarden", instructions=providers.agent_notice())
except TypeError:  # SDK versions without the instructions argument
    app = MCPServer("clickwarden")

_engine = Engine()
_jobs = JobManager()
_sessions = SessionManager()


def _ensure_prewarm() -> None:
    """Connections warm on demand; no paid background requests."""


def _json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _guard(fn, mutation=False):
    try:
        if mutation and _jobs.active():
            return _json({"status": "busy", "error": "A job owns the desktop; use its pause/cancel controls"})
        return fn()
    except CornerAbort as exc:
        return _json({"status": "aborted", "reason": str(exc)})
    except ForegroundGuardError as exc:
        return _json({"status": "aborted_focus", "reason": str(exc)})
    except Exception as exc:  # pragma: no cover - top-level tool safety
        return _json({"status": "error", "error": f"{type(exc).__name__}: {exc}"})


def _session_action(name, kwargs, session_id=""):
    def run():
        if _jobs.active():
            return _json({"status": "busy", "error": "A durable job owns the desktop"})
        return _json(_sessions.get(session_id).run(name, kwargs))
    return _guard(run)


@app.tool()
def fast_capabilities() -> str:
    """Read-only runtime identity and supported workflows. No session/model call.
    Use after reconnect to distinguish old running servers from current source."""
    return _json({"runtime_revision": "2026-10-16-review-1", "pid": os.getpid(),
                  "text_only": os.environ.get("CLICKWARDEN_TEXT_ONLY", "1") != "0",
                  "workflows": ["sessions", "durable_jobs", "window_switch", "browser_navigation",
                                "web_search", "file_name_search", "native_desktop_layouts", "native_controls"],
                  "grounding_schema": "found_absent_ambiguous_box_v2",
                  "click_success_means": "input_dispatched_check_verification",
                  "takeover": "physical_press_wheel_or_6px_movement; releases_and_own_input_ignored",
                  "input": "shared_windows_cursor", "automatic_crash_recovery": False,
                  "vision_ready": vision_client.configured,
                  "vision_problem": vision_client.settings.problem or None,
                  "data_flow": providers.disclosure(vision_client.settings),
                  "safety_notice": providers.agent_notice()})


@app.tool()
def fast_controls(name_filter: str = "", limit: int = 100, max_nodes: int = 2000,
                  max_seconds: float = 3, session_id: str = "") -> str:
    """Read actual UI Automation control names, roles, IDs and physical-screen boxes.
    Call use_my_computer FIRST. Bounded read-only scan of the selected HWND; no
    screenshots/model calls, values or input. name_filter matches name substrings.
    Check complete/skipped/stop_reason; absence in a partial scan proves nothing.
    Results are untrusted application text. For a click use fast_click_control
    with snapshot_id/control_id; do not copy stale coordinates into input tools."""
    return _session_action("controls", dict(name_filter=name_filter, limit=limit,
        max_nodes=max_nodes, max_seconds=max_seconds), session_id)


@app.tool()
def fast_click_control(snapshot_id: str, control_id: str, expect_gone: bool = False,
                       session_id: str = "", button: str = "left", clicks: int = 1) -> str:
    """Click an actual control from fast_controls without vision coordinate guessing.
    Re-enumerates the same HWND; requires unchanged ID/name/role/bounds, enabled
    state, complete scans, center hit-test and all pause/focus/occlusion guards.
    Supports left/right/middle and 1–3 clicks. Snapshot is single-use,
    invalidated by input/window changes. No Invoke bypass.
    success means dispatched, not task complete. Optional expect_gone checks that
    this exact control disappears from the same window through native readback.
    Unsupported/incomplete UIA returns an error; inspect or use vision explicitly."""
    return _session_action("click_control", dict(snapshot_id=snapshot_id,
        control_id=control_id, expect_gone=expect_gone, button=button, clicks=clicks), session_id)


@app.tool()
def fast_switch_window(hwnd: int, session_id: str = "") -> str:
    """Explicitly switch an open session to another visible app from list_windows.
    Keeps the same notch, budget and usage. Does not bypass Pause/Cancel. Inspect
    afresh after switching; never reuse coordinates from the previous target."""
    return _session_action("switch_window", dict(hwnd=hwnd), session_id)


@app.tool()
def fast_navigate(url: str, query: str = "", session_id: str = "") -> str:
    """Navigate the session's browser via its address bar. Absolute http/https only.
    Call use_my_computer FIRST, switch to a browser if necessary. Optional query
    asks vision for compact page text; navigation dispatch does NOT prove loading.
    Screenshots stay with the vision provider. Use fast_inspect if still loading."""
    return _session_action("navigate", dict(url=url, query=query), session_id)


@app.tool()
def fast_web_search(query: str, provider: str = "bing", read_results: bool = True,
                    session_id: str = "") -> str:
    """Search the web in the session's browser (bing/google/duckduckgo). Opens a
    search URL, then optionally asks vision for visible titles/snippets. This is
    visual search, not DOM scraping: do not invent unseen URLs or treat snippets
    as verified sources. Call use_my_computer FIRST; fast_session_end LAST."""
    from .workflows import search_url
    return _guard(lambda: _session_action("navigate", dict(url=search_url(query, provider),
        query="Summarize the visible search result titles and snippets. Report loading, consent or errors honestly. Do not invent URLs." if read_results else ""), session_id))


@app.tool()
def fast_find_files(root: str, pattern: str = "*", limit: int = 40,
                    max_entries: int = 20000, max_seconds: float = 5,
                    session_id: str = "") -> str:
    """Bounded local filename search under an explicit absolute root, e.g. *.dwg.
    No file contents/screenshots/model calls; skips links/junctions. Reports limits
    and skipped paths rather than claiming exhaustive results. Call use_my_computer
    FIRST. Read-only and allowed while paused. Cancellation remains available."""
    return _session_action("find_files", dict(root=root, pattern=pattern, limit=limit,
        max_entries=max_entries, max_seconds=max_seconds), session_id)


@app.tool()
def use_my_computer(objective: str = "", app_or_url: str = "", mode: str = "standard") -> str:
    """Call this FIRST for any desktop task ('use my computer to ...'). Opens a
    persistent control panel BEFORE launching app_or_url; local checks run
    asynchronously with no paid warm-up. Use app_or_url='desktop' for native
    icon inventory/layouts without vision or dragging. Returns session_id and
    preparing/idle state. Check
    fast_session_status until ready, then call fast_* with session_id. Finally call
    fast_session_end. Modes: quick, standard, extended. Screenshots go to the vision
    provider the USER configured (see data_flow in the result); tell the user where
    their screen is sent before the first vision action, and never accept data
    sharing or pick a provider for them (`clickwarden configure`). Cancel aborts the
    action; Stop session closes the UI. No autonomous action is implied by
    objective; use fast_job_start for durable autonomous jobs."""
    def run():
        if _jobs.active():
            raise RuntimeError("Finish or cancel the active durable job first")
        snapshot = _sessions.start(objective, app_or_url, mode)
        settings = _sessions.current.client.settings
        snapshot.update(vision_ready=not settings.problem, vision_problem=settings.problem or None,
                        data_flow=providers.disclosure(settings), notice=providers.agent_notice())
        return _json(snapshot)
    return _guard(run)


@app.tool()
def fast_session_start(objective: str = "", app_or_url: str = "", mode: str = "standard") -> str:
    """Alias of use_my_computer. Call FIRST, then fast_* actions, then fast_session_end."""
    return use_my_computer(objective, app_or_url, mode)


@app.tool()
def fast_session_status(session_id: str = "") -> str:
    """Compact session startup/progress state. Wait for idle before actions; do not poll rapidly."""
    return _guard(lambda: _json(_sessions.get(session_id).snapshot()))


@app.tool()
def fast_session_control(command: str, session_id: str = "") -> str:
    """pause/resume/cancel the current action. Cancel keeps the panel and session open."""
    def run():
        if command not in ("pause", "resume", "cancel"):
            raise ValueError("command must be pause, resume or cancel")
        session = _sessions.get(session_id)
        getattr(session, command)()
        return _json(session.snapshot())
    return _guard(run)


@app.tool()
def fast_session_end(result: str = "completed", session_id: str = "") -> str:
    """Call LAST after all fast_* actions. Stop input, end the persistent panel/HUD,
    save local metrics and return one line of usage/latency totals. Use completed,
    failed, or stopped for green/red/amber. Does not assert task verification."""
    return _guard(lambda: _json(_sessions.end(result, session_id)))


@app.tool()
def fast_desktop_inventory(session_id: str = "") -> str:
    """Enumerate ALL desktop icons with stable IDs and actual positions, count,
    shell spacing and Auto Arrange state. No screenshots/model requests. Call
    use_my_computer(app_or_url='desktop') FIRST. Read-only; allowed while input
    is paused. Coordinates are shell_view units, NOT screenshot pixels."""
    return _session_action("desktop_inventory",{},session_id)


@app.tool()
def fast_desktop_arrange(shape: str = "C", item_ids: list[str] | None = None,
                         bounds: list[int] | None = None, dry_run: bool = False,
                         session_id: str = "") -> str:
    """Arrange desktop items into C, U or grid in ONE call: enumerate, compute,
    checkpoint originals, position natively, and verify positions by native readback.
    Start a desktop session FIRST. Omitted item_ids means all icons. Bounds are
    [left,top,right,bottom] in shell_view units; omit for full view. dry_run plans
    without changes. Refuses Auto Arrange and stale state. Returns verified counts
    and a transaction_id for undo. No model/mouse; no claim of success without
    readback. Cancellation can leave a partial layout, with a saved undo record."""
    return _session_action("desktop_arrange",dict(shape=shape,item_ids=item_ids,bounds=bounds,dry_run=dry_run),session_id)


@app.tool()
def fast_desktop_undo(transaction_id: str, session_id: str = "") -> str:
    """Restore original icon positions from a native layout transaction and
    verify readback. Requires a desktop session. Refuses if desktop state changed
    since that transaction, preserving newer user work."""
    return _session_action("desktop_undo",dict(transaction_id=transaction_id),session_id)


# ---------------------------------------------------------------------------
# Environment / capture
# ---------------------------------------------------------------------------

@app.tool()
def get_screen_info() -> str:
    """Screen size, monitor layout, cursor position, DPI mode, vision status."""
    def _run():
        vx, vy, vw, vh = virtual_screen()
        x, y = cursor_pos()
        return _json({
            "virtual_screen": {"left": vx, "top": vy, "width": vw, "height": vh},
            "cursor": {"x": x, "y": y},
            "dpi_mode": DPI_MODE,
            "vision_configured": vision_client.configured,
            "model": vision_client.model,
            "data_destination": vision_client.settings.host or None,
            "hud": _engine._hud is not None,
            "motion": _engine.motion,
        })
    return _guard(_run)


def fast_screenshot(mode: str = "precision", monitor: int = 1, fresh: bool = True,
                    roi: str | list[int] = "", session_id: str = "") -> str:
    """
    Capture the screen as an optimized JPEG (precision: native Q85; turbo: 1280px Q80;
    roi: 'active_window' or a window-title substring). Returns the file path and timings.
    """
    def _run():
        _ensure_prewarm()
        session = _sessions.get(session_id)
        session.adopt_foreground()
        resolved_mode, resolved_roi = session.engine._resolve_region(mode, roi or None)
        frame = session.capture(mode=resolved_mode, roi=resolved_roi, fresh=fresh)
        from pathlib import Path
        from datetime import datetime
        shots = Path.home() / ".clickwarden" / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"shot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        path.write_bytes(frame.jpeg)
        return _json({"status": "success", "file_path": str(path), **frame.meta()})
    return _guard(_run)


# Screenshots are deliberately absent from the default MCP tool surface.
if os.environ.get("CLICKWARDEN_TEXT_ONLY", "1") == "0":
    app.tool()(fast_screenshot)


@app.tool()
def list_windows(only_visible: bool = True) -> str:
    """List top-level application windows with titles/bounds, excluding Clickwarden controls."""
    return _guard(lambda: _json([w for w in enumerate_windows(only_visible) if not is_control_surface(w)]))


@app.tool()
def focus_window_by_title(title_keyword: str, session_id: str = "") -> str:
    """Focus a window by title. During a session only its application family is allowed."""
    def run():
        current = _sessions.current
        if session_id or (current and not current.closed.is_set()):
            session = _sessions.get(session_id)
            if session.busy:
                raise RuntimeError("Wait for the active action to stop before focusing")
            if not session.family:
                raise RuntimeError("Session target is not ready")
            session.check()
            session.family.refresh()
            matches = [w for w in enumerate_windows() if title_keyword.casefold() in w["title"].casefold()
                       and session.family.contains(w["hwnd"]) and w["hwnd"] != session.panel_hwnd]
            if len(matches) != 1:
                raise ValueError("Title must uniquely identify a window in this session's application family")
            from .win32core import force_foreground
            return _json(force_foreground(matches[0]["hwnd"]))
        return _json(focus_window(title_keyword))
    return _guard(run, mutation=True)


# ---------------------------------------------------------------------------
# Grounded actions (the fast path)
# ---------------------------------------------------------------------------

@app.tool()
def fast_locate(description: str, mode: str = "precision", roi: str | list[int] = "",
                fresh: bool = True, session_id: str = "") -> str:
    """
    Ground a UI element with the Flash vision model and return native pixel coords.
    roi: 'active_window' crops to the focused window (native density, fewer tokens),
    or pass a title, 'x,y,w,h', or [x,y,w,h]. Call use_my_computer FIRST;
    actions share its persistent panel until fast_session_end.
    """
    return _session_action("locate", dict(description=description, mode=mode, roi=roi or None, fresh=fresh), session_id)


@app.tool()
def fast_click(description: str, button: str = "left", clicks: int = 1,
               mode: str = "precision", roi: str | list[int] = "", verify: bool = True,
               fresh: bool = True, hud: bool = True, session_id: str = "",
               expected_result: str = "") -> str:
    """
    Locate an element by description and click it after response validation and
    any ROI retry. ROI accepts 'x,y,w,h', [x,y,w,h], active_window or a title.
    Call use_my_computer FIRST, then fast_session_end LAST. Two observed misses
    escalate to supported protocol/window fallback; never a third identical click.
    Prefer fast_controls/fast_click_control for exposed controls. Absent/ambiguous
    vision targets refuse input. expected_result describes the visible postcondition
    for verification; vision_checked is a model assessment, not objective proof.
    success means dispatched; inspect verification/action_status/task_verified.
    """
    return _session_action("smart_click", dict(description=description, button=button, clicks=clicks,
                           mode=mode, roi=roi or None, verify=verify, fresh=fresh, hud=hud,
                           expected_result=expected_result), session_id)


@app.tool()
def fast_inspect(query: str = "What application is active and what is it showing?",
                 mode: str = "turbo", roi: str | list[int] = "", fresh: bool = True,
                 session_id: str = "") -> str:
    """Ask vision for compact text. ROI: title, active_window, 'x,y,w,h' or [x,y,w,h].
    Call use_my_computer FIRST; share its panel until fast_session_end. Read-only
    inspection remains available while input is paused; it never resumes input
    and returns observation-only data that must not be reused for later clicks."""
    return _session_action("inspect", dict(query=query, mode=mode, roi=roi or None, fresh=fresh), session_id)


@app.tool()
def fast_type(text: str, press_enter: bool = False, wpm: int = 0, allow_non_editable: bool = False,
              session_id: str = "") -> str:
    """Type text into the focused control via batched unicode SendInput.
    Call use_my_computer FIRST. wpm=0 is batched; wpm>0 adds cancellable delays.
    Newlines press Enter (CRLF counts once); they may submit a single-line field.
    Returns no_editable_focus without typing when focus is clearly a button, link
    or read-only page; allow_non_editable=true types anyway (keyboard-driven apps)."""
    return _session_action("type", dict(text=text, press_enter=press_enter, wpm=wpm,
                                        allow_non_editable=allow_non_editable), session_id)


@app.tool()
def fast_hotkey(keys: str, session_id: str = "") -> str:
    """Press a chord (ctrl+a or ctrl,a). Call use_my_computer FIRST."""
    return _session_action("hotkey", dict(keys=keys), session_id)


@app.tool()
def fast_scroll(clicks: int, x: int | None = None, y: int | None = None, session_id: str = "") -> str:
    """Call use_my_computer FIRST. Scroll wheel; negative scrolls down. Without x/y
    the pointer moves to the centre of the session's target window first."""
    return _session_action("scroll", dict(clicks=clicks, x=x, y=y), session_id)


@app.tool()
def fast_drag(start_x: int, start_y: int, end_x: int, end_y: int, session_id: str = "") -> str:
    """Call use_my_computer FIRST. Drag between points. outcome=dispatched only
    confirms input delivery; object movement remains unverified. For collections
    use fast_batch with drag actions; for desktop layouts use fast_desktop_arrange."""
    return _session_action("drag", dict(x1=start_x, y1=start_y, x2=end_x, y2=end_y), session_id)


@app.tool()
def fast_move(x: int, y: int, session_id: str = "") -> str:
    """Call use_my_computer FIRST. Move the shared pointer within its session."""
    return _session_action("move", dict(x=x, y=y), session_id)


# ---------------------------------------------------------------------------
# Multi-step
# ---------------------------------------------------------------------------

@app.tool()
def fast_batch(actions: str, verify_last: bool = False, session_id: str = "") -> str:
    """
    Run several grounded actions in one warm pipeline pass (no per-call overhead).
    Call use_my_computer FIRST, then fast_session_end LAST.
    actions: JSON array of objects, e.g.
      [{"action":"click","description":"search box"},
       {"action":"type","text":"lofi beats","press_enter":true},
       {"action":"wait","seconds":1}]
    Also supports {"action":"drag","start_x":10,"start_y":20,"end_x":30,"end_y":40}.
    Mouse move uses x/y; scroll preserves optional x/y; type preserves wpm;
    click supports button and clicks. Chords accept comma or plus separators.
    Drag batches are dispatched, not outcome-verified. Detailed results stay local.
    """
    def _run():
        _ensure_prewarm()
        try:
            parsed = json.loads(actions) if isinstance(actions, str) else actions
        except ValueError as exc:
            return _json({"status": "error", "error": f"invalid actions JSON: {exc}"})
        if not isinstance(parsed, list):
            return _json({"status": "error", "error": "actions must be a JSON array"})
        return _session_action("batch", dict(actions=parsed, verify_last=verify_last), session_id)
    return _guard(_run, mutation=True)


@app.tool()
def fast_task(task: str, max_steps: int = 6, mode: str = "precision",
              verify: bool = True, session_id: str = "") -> str:
    """
    Run an autonomous Plan->Act->Observe loop toward a goal, using streaming
    grounding and adaptive settling. Corner failsafe aborts instantly.
    Call use_my_computer FIRST, then fast_session_end LAST. For longer runs use
    fast_job_start outside an ad-hoc session.
    """
    return _session_action("task", dict(goal=task, max_steps=max_steps, mode=mode, verify=verify), session_id)


@app.tool()
def fast_job_start(goal: str, target_hwnd: int, max_steps: int = 60,
                   max_seconds: float = 1800, max_requests: int = 120,
                   completion_file: str = "", expected_text: str = "") -> str:
    """Start a persistent GUI job and return immediately. Target must be visible
    and foreground. A control panel provides Pause/Resume/Cancel; physical input
    pauses and Ctrl+Alt+F12 cancels. Uses the shared system cursor, with an animated
    target overlay. Screenshots go to the vision provider the user configured and accepted. Optional completion_file checks
    a new/modified nonempty artifact, with optional expected UTF-8 text.
    Read status at milestones; do not poll rapidly. One job at a time."""
    def run():
        if _sessions.current and not _sessions.current.closed.is_set():
            raise RuntimeError("End the ad-hoc session before starting a durable job")
        return _json(_jobs.start(goal, target_hwnd, max_steps, max_seconds,
                                max_requests, completion_file, expected_text))
    return _guard(run)


@app.tool()
def fast_job_status(job_id: str) -> str:
    """Compact text-only progress, usage and result. No screenshots or full logs."""
    return _guard(lambda: _json(_jobs.status(job_id)))


@app.tool()
def fast_job_list() -> str:
    """List up to twenty recent jobs with compact status."""
    return _guard(lambda: _json(_jobs.list()))


@app.tool()
def fast_job_cancel(job_id: str) -> str:
    """Request cancellation, including during a slow vision call. Confirm stopped via status."""
    return _guard(lambda: _json(_jobs.command(job_id, "cancel")))


@app.tool()
def fast_job_pause(job_id: str) -> str:
    """Pause input dispatch and retain the checkpoint and controls."""
    return _guard(lambda: _json(_jobs.command(job_id, "pause")))


@app.tool()
def fast_job_resume(job_id: str, target_hwnd: int = 0) -> str:
    """Resume with a fresh observation. If the app restarted, provide its new HWND.
    Budgets are cumulative; interrupted input is never blindly replayed."""
    def run():
        if _sessions.current and not _sessions.current.closed.is_set():
            raise RuntimeError("End the ad-hoc session before resuming a durable job")
        return _json(_jobs.resume(job_id, target_hwnd))
    return _guard(run)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.tool()
def fast_bench(session_id: str = "") -> str:
    """Call use_my_computer FIRST. Measure capture/vision latency on its target.
    Returns compact text and timings, accounted in session metrics."""
    return _session_action("inspect", dict(query="Briefly identify this application and its main view.",
                           mode="turbo", fresh=True), session_id)


@app.tool()
def hud_status(enable: bool = True) -> str:
    """Enable/disable the transparent HUD overlay (pulsing border + reticle)."""
    def _run():
        if _sessions.current and not _sessions.current.closed.is_set():
            raise RuntimeError("Session owns the HUD; use fast_session_end to close it")
        if _engine._hud is None:
            return _json({"status": "unavailable", "reason": "HUD failed to initialise"})
        if enable:
            _engine._hud.start()
        else:
            _engine._hud.shutdown()
        return _json({"status": "success", "hud_enabled": bool(enable)})
    return _guard(_run)


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
