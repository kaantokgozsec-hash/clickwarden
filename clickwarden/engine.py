"""
clickwarden.engine — the action orchestrator.

Ties the layers into the tight loop from the blueprint:

    capture (28 ms) -> flash vision (streaming, ~2.4-3.0 s warm)
        -> HUD reticle -> SendInput (batched, ~40 ms) -> stabilizer

The two latency decisions the architecture is built around:

1. ``locate(on_point=...)`` exposes coordinates after response validation. ``smart_click``
   waits for the validated response and any ROI retry before dispatching input.

2. Post-action waiting is adaptive (``Stabilizer``): we poll a 240 px grayscale
   thumbnail and proceed as soon as two frames agree, instead of sleeping 1 s.

Every vision call is preceded by a failsafe gate: if the user slams the mouse
into a screen corner, the action is never dispatched.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from functools import wraps

from . import frames as frames_mod
from .config import env_flag, env_int
from .frames import Frame, Stabilizer, capture, capture_window, diff_ratio
from .input import click as input_click, drag as input_drag, hotkey as input_hotkey, \
    scroll as input_scroll, type_text, failsafe, foreground_guard, CornerAbort, \
    ForegroundGuardError
from .focus import focused_editability
from .vision import client as vision_client, VisionClient
from .win32core import enumerate_windows, foreground_hwnd, virtual_screen, window_rect

HUD_ENABLED = env_flag("HUD", True)

# ROI crops are downscaled above this width so huge (even maximized) windows
# still fit a small prefill budget while keeping native density for the model.
ROI_MAX_WIDTH = env_int("ROI_MAX_WIDTH", 1600)

_ACTION_LOCK = threading.RLock()
_ACTION_STATE = threading.local()


def desktop_action(fn):
    """Serialize desktop mutations and scope the HUD, abort watcher and guard."""
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with _ACTION_LOCK:
            outer = not getattr(_ACTION_STATE, "depth", 0)
            saved_guard = (getattr(foreground_guard, "_hwnd", 0),
                           getattr(foreground_guard, "_title", ""))
            _ACTION_STATE.depth = getattr(_ACTION_STATE, "depth", 0) + 1
            show_hud = kwargs.get("hud", True)
            try:
                if outer:
                    self.safety.arm()
                    if show_hud:
                        self.hud_begin(status=f"clickwarden: {fn.__name__}")
                result = fn(self, *args, **kwargs)
                if outer and show_hud:
                    ok = result.get("status") in ("success", "completed")
                    color = ("cyan" if ok and result.get("task_verified") is False else
                             "green" if ok else "amber" if result.get("status") == "max_steps_reached" else "red")
                    self.hud_end(color=color)
                return result
            except Exception:
                if outer and show_hud:
                    self.hud_end(color="red", status="clickwarden: stopped")
                raise
            finally:
                foreground_guard.arm(*saved_guard)
                _ACTION_STATE.depth -= 1
                if outer:
                    self.safety.disarm()
    return wrapped


# ---------------------------------------------------------------------------
# Frame cache — re-use a fresh capture across locate/verify calls
# ---------------------------------------------------------------------------

class FrameCache:
    def __init__(self, ttl_s: float = 8.0):
        self.ttl_s = ttl_s
        self._frame: Frame | None = None
        self._ts = 0.0
        self._key: tuple = ()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Frame | None:
        with self._lock:
            if (self._frame is not None and self._key == key
                    and (time.perf_counter() - self._ts) < self.ttl_s):
                return self._frame
        return None

    def put(self, key: tuple, frame: Frame) -> None:
        with self._lock:
            self._frame, self._ts, self._key = frame, time.perf_counter(), key

    def invalidate(self) -> None:
        with self._lock:
            self._frame = None

    def snapshot(self) -> Frame | None:
        with self._lock:
            if self._frame is not None and (time.perf_counter() - self._ts) < self.ttl_s:
                return self._frame
        return None


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ActionTrace:
    label: str
    capture_ms: float = 0.0
    vision_ms: float = 0.0
    ttft_ms: float = 0.0
    input_ms: float = 0.0
    settle_ms: float = 0.0
    total_ms: float = 0.0
    jpeg_kb: float = 0.0
    mode: str = ""
    point: tuple[int, int] | None = None
    changed: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "capture_ms": round(self.capture_ms, 1),
            "vision_ms": round(self.vision_ms, 1),
            "ttft_ms": round(self.ttft_ms, 1),
            "input_ms": round(self.input_ms, 1),
            "settle_ms": round(self.settle_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "jpeg_kb": round(self.jpeg_kb, 1),
            "mode": self.mode,
            "point": list(self.point) if self.point else None,
            "changed": self.changed,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, client: VisionClient | None = None, hud=None, monitor: int = 1,
                 motion: str = "human"):
        self.vision = client or vision_client
        self.monitor = monitor
        self.motion = motion  # 'human' | 'instant'
        self.safety = failsafe
        self.safety.start()
        self.cache = FrameCache()
        self.focus_probe = focused_editability
        self._hud = hud
        if hud is None and HUD_ENABLED:
            try:
                from .hud import hud as _h
                self._hud = _h
            except Exception:
                self._hud = None

    # -- plumbing ----------------------------------------------------------

    def hud_begin(self, status: str = "clickwarden: working", color: str = "cyan") -> None:
        if self._hud is not None:
            try:
                self._hud.start()
                self._hud.begin(color=color, status=status)
            except Exception:
                pass

    def hud_end(self, color: str = "green", status: str = "clickwarden: done") -> None:
        if getattr(self, "session_active", False):
            if self._hud is not None:
                self._hud.notice(status, color)
            return
        if self._hud is not None:
            try:
                self._hud.end(color=color, status=status)
            except Exception:
                pass

    def hud_reticle(self, x: int, y: int, color: str | None = None) -> None:
        if self._hud is not None:
            try:
                self._hud.reticle(x, y, color=color)
            except Exception:
                pass

    def hud_flash(self, x: int, y: int, color: str = "green") -> None:
        if self._hud is not None:
            try:
                self._hud.flash(x, y, color=color)
            except Exception:
                pass

    def gate(self) -> None:
        """Failsafe gate — raise CornerAbort if the user aborted."""
        self.safety.check()

    def wait(self, seconds: float) -> None:
        deadline = time.perf_counter() + max(0, seconds)
        while True:
            self.gate()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    @desktop_action
    def move(self, x: int, y: int) -> dict:
        from .input import move_to, check_click_point
        self.gate()
        check_click_point(x, y)
        self.hud_reticle(x, y)
        ms = move_to(x, y, human=(self.motion == "human"))
        return {"status": "success", "x": x, "y": y, "move_ms": round(ms, 1)}

    # -- capture -----------------------------------------------------------

    def grab(self, mode: str = "precision", roi: tuple | None = None,
             fresh: bool = False, label: str = "",
             max_width: int | None = None, quality: int | None = None) -> Frame:
        key = (mode, roi, self.monitor, max_width, quality)
        if not fresh:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        frame = capture(monitor=self.monitor, roi=roi,
                        mode=mode if roi is None else ("precision" if mode == "roi" else mode),
                        max_width=max_width, label=label, quality=quality)
        self.cache.put(key, frame)
        return frame

    def window_roi(self, pad: int = 10,
                   max_width: int = ROI_MAX_WIDTH) -> tuple[int, int, int, int] | None:
        """Crop box for the foreground window, whatever its size.

        Maximized windows still pay off: a 1920px window at the ROI cap costs
        ~60% of the full-screen image tokens, and every crop keeps native pixel
        density for the model. ``pad`` is dropped when the window already hugs
        the screen edge (otherwise we'd crop into the taskbar/other monitors
        with no information gain).
        """
        hwnd = foreground_hwnd()
        if not hwnd:
            return None
        l, t, r, b = window_rect(hwnd)
        vx, vy, vw, vh = virtual_screen()
        l, t = max(l, vx), max(t, vy)
        r, b = min(r, vx + vw), min(b, vy + vh)
        w, h = r - l, b - t
        if w <= 32 or h <= 32:
            return None
        near_edge = w >= vw * 0.97 or h >= vh * 0.97
        p = 0 if near_edge else pad
        left = max(vx, l - p)
        top = max(vy, t - p)
        return (left, top, min(w + p * 2, vx + vw - left), min(h + p * 2, vy + vh - top))

    # -- vision-grounded actions -------------------------------------------

    def locate(self, description: str, mode: str | None = None, fresh: bool = False,
               roi: str | tuple | None = None, on_point: Callable | None = None,
               auto_roi: bool = False, expect_hwnd: int | None = None) -> dict:
        """Ground *description* on screen and return native screen coordinates.

        auto_roi:    try the focused window first (native density, fewer prefill
                     tokens); re-ground full-screen if the point is unusable.
        expect_hwnd: ground against THIS window's pixels. Immune to invisible
                     service windows that steal the foreground.
        """
        auto = bool(auto_roi) or mode == "auto" or (isinstance(roi, str) and roi == "auto")
        wroi = self.window_roi() if auto else None

        if expect_hwnd and roi is None:
            from .frames import capture_hwnd
            frame = capture_hwnd(expect_hwnd, max_width=ROI_MAX_WIDTH,
                                 label=f"locate:{description[:40]}")
            if frame is None:
                return {"success": False,
                        "error": f"window {expect_hwnd} has no capturable rect"}
            wroi = None
        elif wroi is not None:
            frame = self.grab(mode="precision", roi=wroi, fresh=fresh,
                              max_width=ROI_MAX_WIDTH, label=f"locate:{description[:40]}")
        else:
            resolved_mode, resolved_roi = self._resolve_region(mode, roi)
            frame = self.grab(mode=resolved_mode, roi=resolved_roi, fresh=fresh,
                              label=f"locate:{description[:40]}")

        t0 = time.perf_counter()
        res = self._locate_on_frame(frame, description, on_point)
        res["capture_ms"] = round(frame.total_ms, 1)
        res["mode"] = frame.mode
        res["jpeg_kb"] = round(len(frame.jpeg) / 1024, 1)
        res["region"] = {"left": frame.left, "top": frame.top,
                         "width": frame.screen_w, "height": frame.screen_h}
        res["fg_hwnd"] = frame.fg_hwnd
        res["fg_title"] = frame.fg_title

        retried = False
        if wroi is not None and self._roi_uncertain(res, frame):
            retried = True
            self.gate()
            full = self.grab(mode="precision", roi=None, fresh=True,
                             label=f"locate-full:{description[:32]}")
            full_res = self._locate_on_frame(full, description, on_point)
            full_res["capture_ms"] = round(full.total_ms, 1)
            full_res["mode"] = full.mode
            full_res["jpeg_kb"] = round(len(full.jpeg) / 1024, 1)
            full_res["region"] = {"left": full.left, "top": full.top,
                                  "width": full.screen_w, "height": full.screen_h}
            full_res["fg_hwnd"] = full.fg_hwnd
            full_res["fg_title"] = full.fg_title
            full_res["roi_retry"] = {"roi_latency_ms": res.get("latency_ms"),
                                     "roi_point": res.get("point")}
            res = full_res

        res["auto_roi"] = wroi is not None
        res["fallback_used"] = retried
        res["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return res

    @staticmethod
    def _roi_uncertain(res: dict, frame: Frame) -> bool:
        """Should an ROI grounding be re-tried on the full screen?

        Deliberately conservative: a retry costs a full second vision round-trip,
        so it only fires for real failures. Edge proximity and the mild
        "medium/high" confidence band are normal for edge-anchored UI (taskbar,
        title bars, docks) and must not trigger a retry of an already-correct
        point.
        """
        if not res.get("success"):
            return True
        return res.get("confidence") == "low"

    def _locate_on_frame(self, frame: Frame, description: str,
                         on_point: Callable | None) -> dict:
        """Run the grounding request against one captured frame."""
        first_point: dict[str, Any] = {}

        def _cb(sx: int, sy: int) -> None:
            if first_point or on_point is None:
                return
            first_point["ts"] = time.perf_counter()
            on_point(sx, sy)

        t0 = time.perf_counter()
        res = self.vision.locate(frame, description, on_point=_cb if on_point else None)
        if first_point:
            res["press_ready_ms"] = round((first_point["ts"] - t0) * 1000, 1)
        return res

    @desktop_action
    def smart_click(self, description: str, button: str = "left", clicks: int = 1,
                    mode: str | None = None, verify: bool = False, fresh: bool = True,
                    roi: str | tuple | None = None, hud: bool = True,
                    expect_hwnd: int | None = None,
                    avoid_point: list | tuple | None = None) -> dict:
        """Locate *description* and click it — the core one-call primitive.

        mode='auto' tries the focused window first and re-grounds full-screen if
        the target isn't inside it, so ROI speed never causes a wrong click.
        expect_hwnd pins grounding to one window's pixels and additionally
        rejects any point that falls outside that window's bounds.
        """
        trace = ActionTrace(label=f"click:{description[:40]}")
        t_start = time.perf_counter()
        self.gate()
        expected_rect = None
        if expect_hwnd:
            foreground_guard.arm(expect_hwnd)
            foreground_guard.check()
            expected_rect = window_rect(expect_hwnd)

        auto_roi = (mode == "auto") or (isinstance(roi, str) and roi == "auto")
        if hud:
            self.hud_begin(status=f"clickwarden: {description[:48]}")

        located = self.locate(description, mode=mode, fresh=fresh, roi=roi,
                              on_point=None, auto_roi=auto_roi, expect_hwnd=expect_hwnd)
        # Change detection compares the region that was grounded, not the zoom crop.
        base_region = located.get("region")
        # A single native-density zoom, before dispatch, for uncertain/small glyphs.
        if located.get("success") and (located.get("confidence") != "high"
                or not located.get("target_size")
                or min(located["target_size"]) < 64
                or avoid_point is not None
                or any(word in description.lower() for word in ("icon", "glyph", "tiny", "small"))
                or located.get("sample_spread", 0) > 12):
            self.gate()
            zx, zy = located["point"]
            vx, vy, vw, vh = virtual_screen()
            left, top = max(vx, zx - 120), max(vy, zy - 120)
            right, bottom = min(vx + vw, zx + 120), min(vy + vh, zy + 120)
            if expect_hwnd:
                wl, wt, wr, wb = expected_rect
                left, top, right, bottom = max(left, wl), max(top, wt), min(right, wr), min(bottom, wb)
            if right > left and bottom > top:
                zoom = self.locate(description, mode="precision", fresh=True,
                                   roi=(left, top, right-left, bottom-top))
                zoom["zoom_retry"] = True
                zoom["fallback_used"] = True
                # A zoom must remain in the original candidate's neighborhood.
                # It may refine the box; it must not jump to a different row.
                old_box, new_box = located.get("box"), zoom.get("box")
                if zoom.get("success") and old_box and new_box and not (
                        max(old_box[0], new_box[0]) < min(old_box[2], new_box[2]) and
                        max(old_box[1], new_box[1]) < min(old_box[3], new_box[3])):
                    zoom = {"success": False, "error": "zoom contradicted original target box"}
                located = zoom
        if located.get("region"):
            self._sync_from_region(located["region"])
        trace.capture_ms = located.get("capture_ms", 0.0)
        trace.vision_ms = located.get("latency_ms", 0.0)
        trace.ttft_ms = located.get("ttft_ms") or 0.0
        trace.jpeg_kb = located.get("jpeg_kb", 0.0)
        trace.mode = located.get("mode", "")

        if not located.get("success") or located.get("confidence") == "low":
            trace.error = located.get("error") or "grounding failed or confidence is low"
            trace.total_ms = (time.perf_counter() - t_start) * 1000
            if hud:
                self.hud_end(color="red", status="clickwarden: not found")
            return {"status": "error", "element": description,
                    "error": trace.error, "latency": trace.as_dict()}

        sx, sy = located["point"]
        box = located.get("box")
        if not box or not (box[0] <= sx < box[2] and box[1] <= sy < box[3]):
            if hud:
                self.hud_end(color="red", status="clickwarden: no safe box")
            return {"status": "error", "error": "No validated clickable box", "element": description}
        if avoid_point is not None and max(abs(sx-avoid_point[0]), abs(sy-avoid_point[1])) <= 6:
            if hud:
                self.hud_end(color="red", status="clickwarden: repeated point refused")
            return {"status": "error", "error": "Refusing repeated failed point; change strategy",
                    "element": description, "clicked_point": None}
        trace.point = (sx, sy)

        # Teach the input layer the true framebuffer space before injecting
        if located.get("region"):
            self._sync_from_region(located["region"])

        # Pinned-window sanity: a point outside the requested window can only be
        # a grounding error (or a stale frame) — never click it.
        if expect_hwnd:
            l, t, r, b = window_rect(expect_hwnd)
            if (l, t, r, b) != expected_rect or not (l <= sx < r and t <= sy < b):
                trace.error = (f"grounded point {[sx, sy]} lies outside window "
                               f"{expect_hwnd} bounds ({l},{t})-({r},{b})")
                trace.total_ms = (time.perf_counter() - t_start) * 1000
                if hud:
                    self.hud_end(color="red", status="clickwarden: off-target")
                return {"status": "error", "element": description,
                        "error": trace.error, "latency": trace.as_dict()}

        if hud:
            self.hud_reticle(sx, sy)
            if getattr(self, "session_active", False):
                self.wait(.06)
        self.gate()
        # Baseline for change detection is taken immediately before input. A
        # baseline from before the multi-second vision call would count any
        # unrelated animation during grounding as a "visual change".
        pre_frame = self._region_snapshot(base_region) if verify else None
        try:
            self._dispatch_click(sx, sy, button, clicks, trace,
                                 expected_fg=(expect_hwnd or located.get("fg_hwnd", 0),
                                              located.get("fg_title", "")))
        except ForegroundGuardError as exc:
            trace.error = str(exc)
            trace.total_ms = (time.perf_counter() - t_start) * 1000
            if hud:
                self.hud_end(color="red", status="clickwarden: focus stolen")
            return {"status": "aborted_focus", "element": description,
                    "error": trace.error, "latency": trace.as_dict()}

        if verify and pre_frame is not None:
            settle = Stabilizer(monitor=self.monitor)
            trace.settle_ms = settle.wait()
            trace.changed = self._changed_since(pre_frame)
        if hud:
            self.hud_flash(sx, sy, color="cyan")
            self.hud_end(color="cyan", status="clickwarden: click sent")

        trace.total_ms = (time.perf_counter() - t_start) * 1000
        return {
            "status": "success",
            "action": "smart_click",
            "element": description,
            "clicked_point": list(trace.point),
            "target_box": box,
            "action_status": "dispatched",
            "verification": "visual_change" if trace.changed is True else "unverified",
            "task_verified": False,
            "confidence": located.get("confidence"),
            "model_description": located.get("description"),
            "target": located.get("target"),
            "mode": trace.mode,
            "fallback_used": located.get("fallback_used", False),
            "latency": trace.as_dict(),
        }

    @desktop_action
    def click_control_point(self, point, expect_hwnd: int, button: str = 'left', clicks: int = 1) -> dict:
        """Dispatch a freshly revalidated UIA point through all existing input guards."""
        self.gate()
        l, t, r, b = window_rect(expect_hwnd)
        x, y = point
        if not (l <= x < r and t <= y < b):
            return {"status": "error", "error": "Native control point outside target window"}
        trace = ActionTrace(label="native_click")
        self.hud_reticle(x, y)
        self.gate()
        self._dispatch_click(x, y, button, clicks, trace, expected_fg=(expect_hwnd, ""))
        return {"status": "success", "action_status": "dispatched", "clicked_point": [x, y],
                "button": button, "clicks": clicks, "strategy": "uia_bounds",
                "verification": "unverified", "task_verified": False}

    def _dispatch_click(self, x: int, y: int, button: str, clicks: int,
                        trace: ActionTrace, expected_fg: tuple[int, str] | None = None) -> None:
        t0 = time.perf_counter()
        try:
            if expected_fg and expected_fg[0]:
                foreground_guard.arm(*expected_fg)
                foreground_guard.check()   # raises before anything is injected
            input_click(x, y, button=button, clicks=clicks,
                        human=(self.motion == "human"))
            trace.input_ms = (time.perf_counter() - t0) * 1000
            self.cache.invalidate()
        finally:
            foreground_guard.disarm()

    @staticmethod
    def _sync_physical_space(frame: Frame) -> None:
        """Refresh the desktop metrics; frame dimensions may describe only a crop."""
        from .input import _refresh_screen_metrics, set_physical_screen
        _refresh_screen_metrics()
        _, _, width, height = virtual_screen()
        set_physical_screen(width, height)

    def _sync_from_region(self, region: dict) -> None:
        """Refresh desktop geometry; an ROI is never a desktop-size measurement."""
        from .input import _refresh_screen_metrics, set_physical_screen
        _refresh_screen_metrics()
        _, _, width, height = virtual_screen()
        set_physical_screen(width, height)

    def expect_foreground(self, hwnd: int, title: str = "") -> None:
        """Arm the focus guard for subsequent text/hotkey injection."""
        foreground_guard.arm(hwnd, title)

    def clear_foreground(self) -> None:
        foreground_guard.disarm()

    def guard_foreground(self) -> None:
        """Raise ForegroundGuardError unless focus matches the armed window."""
        foreground_guard.check()

    @desktop_action
    def type(self, text: str, press_enter: bool = False, wpm: int = 0,
             expect: int | None = None, allow_non_editable: bool = False) -> dict:
        """Type into the focused control.

        expect: an HWND that must own the foreground first. Pass the window you
        actually intend to type into — without it, keystrokes go wherever focus
        happens to be, which is exactly how agents type into the wrong app.
        A UIA read refuses only when focus is clearly not a text target (button,
        link, read-only page body); unknown focus (canvas, CAD panes) still types.
        """
        self.gate()
        if expect:
            from .win32core import force_foreground
            fg = force_foreground(expect)
            if fg.get("status") != "success":
                raise ForegroundGuardError(
                    f"could not focus the intended window for typing: {fg.get('message')}")
            foreground_guard.arm(expect, fg.get("focused", ""))
        elif not getattr(foreground_guard, "_hwnd", 0):
            fg = foreground_hwnd()
            foreground_guard.arm(fg, "")
        try:
            foreground_guard.check()
            focus = self.focus_probe()
            if focus.get("editable") is False and not allow_non_editable:
                return {"status": "no_editable_focus", "action": "type", "typed": 0, "focus": focus,
                        "message": "Focused control does not accept text; focus a text field first "
                                   "or pass allow_non_editable=true for keyboard-driven apps"}
            foreground_guard.check()
            t0 = time.perf_counter()
            info = type_text(text, wpm=wpm)
            if press_enter:
                self.gate()
                foreground_guard.check()
                input_hotkey("enter")
            self.cache.invalidate()
        finally:
            foreground_guard.disarm()
        return {"status": "success", "action": "type", "typed": len(text),
                "press_enter": press_enter, "focus": focus, "elapsed_ms": info["elapsed_ms"],
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @desktop_action
    def hotkey(self, keys: str, expect: int | None = None) -> dict:
        """Press a chord, optionally after re-asserting that *expect* is focused."""
        self.gate()
        if expect:
            from .win32core import force_foreground
            fg = force_foreground(expect)
            if fg.get("status") != "success":
                raise ForegroundGuardError(
                    f"could not focus the intended window for hotkey: {fg.get('message')}")
            foreground_guard.arm(expect)
        foreground_guard.check()
        t0 = time.perf_counter()
        info = input_hotkey(keys)
        self.cache.invalidate()
        return {"status": "success", "action": "hotkey", "keys": info["keys"],
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @desktop_action
    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> dict:
        self.gate()
        t0 = time.perf_counter()
        input_scroll(clicks, x, y)
        self.cache.invalidate()
        return {"status": "success", "action": "scroll", "clicks": clicks,
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @desktop_action
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        self.gate()
        t0 = time.perf_counter()
        input_drag(x1, y1, x2, y2, human=(self.motion == "human"))
        self.cache.invalidate()
        return {"status": "success", "action": "drag", "from": [x1, y1], "to": [x2, y2],
                "outcome":"dispatched", "verification":"unverified",
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def inspect(self, query: str, mode: str | None = None, fresh: bool = True,
                roi: str | tuple | None = None, expect_hwnd: int | None = None) -> dict:
        if expect_hwnd:
            from .frames import capture_hwnd
            frame = capture_hwnd(expect_hwnd, max_width=ROI_MAX_WIDTH, label="inspect")
            if frame is None:
                return {"success": False, "error": f"window {expect_hwnd} has no rect"}
        else:
            resolved_mode, resolved_roi = self._resolve_region(mode, roi)
            frame = self.grab(mode=resolved_mode, roi=resolved_roi, fresh=fresh)
        res = self.vision.inspect(frame, query)
        res["capture_ms"] = round(frame.total_ms, 1)
        res["jpeg_kb"] = round(len(frame.jpeg) / 1024, 1)
        return res

    # -- batch & task loops --------------------------------------------------

    @desktop_action
    def batch(self, actions: list[dict], verify_last: bool = False) -> dict:
        """Run grounded actions back to back on one warm pipeline.

        Each item: {"action": "click|type|hotkey|scroll|wait",
                    "description": "...", "text": "...", "keys": "...",
                    "clicks": n, "seconds": n, "verify": bool}
        """
        t0 = time.perf_counter()
        log: list[dict] = []
        for i, item in enumerate(actions):
            self.gate()
            kind = (item.get("action") or "click").lower()
            verify = bool(item.get("verify")) or (verify_last and i == len(actions) - 1)
            if kind in ("click", "double_click"):
                res = self.smart_click(
                    item.get("description", ""),
                    button=item.get("button", "left"),
                    clicks=2 if kind == "double_click" else int(item.get("clicks", 1)),
                    mode=item.get("mode"), verify=verify, fresh=True, roi=item.get("roi"),
                    hud=item.get("hud", True),
                )
            elif kind == "type":
                res = self.type(item.get("text", ""), press_enter=bool(item.get("press_enter")))
            elif kind == "hotkey":
                res = self.hotkey(item.get("keys", ""))
            elif kind == "scroll":
                res = self.scroll(int(item.get("clicks", -3)), item.get("x"), item.get("y"))
            elif kind == "wait":
                sec = min(10, float(item.get("seconds", 1)))
                self.wait(sec)
                res = {"status": "success", "action": "wait", "seconds": sec}
            else:
                res = {"status": "error", "error": f"unknown action {kind}"}
            log.append(res)
            if res.get("status") != "success":
                return {"status": "failed", "step": i + 1, "log": log,
                        "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return {"status": "completed", "steps": len(log), "log": log,
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    @desktop_action
    def task(self, goal: str, max_steps: int = 6, mode: str = "precision",
             verify: bool = True) -> dict:
        """Autonomous Plan->Act->Observe loop with adaptive settling."""
        t0 = time.perf_counter()
        history: list[str] = []
        log: list[dict] = []
        self.hud_begin(status=f"clickwarden: {goal[:48]}")

        try:
            for step in range(1, max_steps + 1):
                self.gate()
                frame = self.grab(mode=mode, fresh=True, label=f"task:{goal[:30]}")
                plan = self.vision.plan(frame, goal, history)
                if not plan.get("success"):
                    self.hud_end(color="red", status="clickwarden: plan failed")
                    return {"status": "error", "error": plan.get("error"),
                            "step": step, "log": log,
                            "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

                action = (plan.get("action") or "").lower()
                reason = plan.get("reason", "")
                entry: dict[str, Any] = {"step": step, "action": action, "reason": reason,
                                         "vision_ms": plan.get("latency_ms")}

                if action == "done":
                    entry["summary"] = plan.get("summary", "Task completed.")
                    log.append(entry)
                    self.hud_end(color="green", status="clickwarden: done")
                    return {"status": "completed", "steps": step,
                            "summary": entry["summary"], "log": log,
                            "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
                if action == "fail":
                    log.append(entry)
                    self.hud_end(color="red", status="clickwarden: blocked")
                    return {"status": "failed", "steps": step, "reason": reason, "log": log,
                            "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

                if not frame.fg_hwnd:
                    raise ForegroundGuardError("capture had no foreground window; refusing input")
                foreground_guard.arm(frame.fg_hwnd, frame.fg_title)
                foreground_guard.check()
                # Unmasked same-region baseline taken just before input (see smart_click).
                pre = self._region_snapshot({"left": frame.left, "top": frame.top,
                                             "width": frame.screen_w, "height": frame.screen_h}) if verify else None
                if action in ("click", "double_click"):
                    pt = plan.get("screen_point")
                    if not pt:
                        log.append({**entry, "error": "no point"})
                        continue
                    x, y = int(pt[0]), int(pt[1])
                    self.hud_reticle(x, y)
                    self.gate()
                    t_in = time.perf_counter()
                    input_click(x, y, button=plan.get("button", "left"),
                                clicks=2 if action == "double_click" else 1,
                                human=(self.motion == "human"))
                    entry["input_ms"] = round((time.perf_counter() - t_in) * 1000, 1)
                    entry["point"] = [x, y]
                    history.append(f"clicked {x},{y} ({reason})")
                elif action == "type":
                    self.gate()
                    info = type_text(plan.get("text", ""))
                    if plan.get("press_enter"):
                        self.gate()
                        foreground_guard.check()
                        input_hotkey("enter")
                    entry["typed"] = len(plan.get("text", ""))
                    entry["input_ms"] = info["elapsed_ms"]
                    history.append(f"typed {plan.get('text', '')[:24]!r} ({reason})")
                elif action == "hotkey":
                    self.gate()
                    input_hotkey(plan.get("keys", ""))
                    entry["keys"] = plan.get("keys")
                    history.append(f"hotkey {plan.get('keys')} ({reason})")
                elif action == "scroll":
                    self.gate()
                    input_scroll(int(plan.get("clicks", -3)))
                    entry["clicks"] = plan.get("clicks", -3)
                    history.append(f"scroll {plan.get('clicks', -3)} ({reason})")
                elif action == "wait":
                    sec = min(5, int(plan.get("seconds", 1)))
                    self.wait(sec)
                    entry["seconds"] = sec
                    history.append(f"wait {sec}s ({reason})")
                else:
                    log.append({**entry, "error": f"unknown action {action}"})
                    continue

                self.cache.invalidate()
                foreground_guard.disarm()
                if verify:
                    settle = Stabilizer(monitor=self.monitor)
                    entry["settle_ms"] = settle.wait()
                    entry["changed"] = self._changed_since(pre) if pre is not None else None
                log.append(entry)

            self.hud_end(color="amber", status="clickwarden: step cap")
            return {"status": "max_steps_reached", "steps": max_steps, "log": log,
                    "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        except CornerAbort:
            self.hud_end(color="red", status="clickwarden: aborted")
            return {"status": "aborted", "reason": "corner failsafe tripped", "log": log,
                    "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        except ForegroundGuardError as exc:
            return {"status": "aborted_focus", "error": str(exc), "log": log}

    # -- helpers -------------------------------------------------------------

    def _resolve_region(self, mode: str | None, roi: str | tuple | None):
        """Turn the generic (mode, roi) request into concrete capture settings."""
        if isinstance(roi, str) and "," in roi:
            try:
                roi = tuple(int(v.strip()) for v in roi.split(","))
            except ValueError:
                raise ValueError("Pixel ROI must be x,y,width,height") from None
        if isinstance(roi, (list, tuple)):
            if len(roi) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in roi) or roi[2] <= 0 or roi[3] <= 0:
                raise ValueError("Pixel ROI must contain four integers and positive width/height")
            roi = tuple(roi)
        if isinstance(roi, str):
            if roi in ("active_window", "auto"):
                # "auto" with no usable foreground window falls back to the full
                # frame; it must never be treated as a title such as "AutoCAD".
                roi = self.window_roi()
            elif roi == "none":
                roi = None
            elif roi:
                for win in enumerate_windows():
                    if roi.lower() in win["title"].lower():
                        b = win["bounds"]
                        pad = 10
                        roi = (b["x"] - pad, b["y"] - pad, b["width"] + pad * 2,
                               b["height"] + pad * 2)
                        break
                else:
                    raise ValueError("No window matches the requested ROI")
        if roi is not None:
            return "precision", roi
        if mode in ("precision", "turbo", "auto", None):
            if mode == "auto":
                # auto: turbo framing now, with auto-ROI fallback handled by
                # locate()/smart_click(); a focused window is tried first.
                auto = self.window_roi()
                if auto is not None:
                    return "precision", auto
                return "turbo", None
            return (mode or "precision"), None
        return "precision", None

    def _region_snapshot(self, region: dict | None = None) -> Frame | None:
        """Cheap unmasked turbo capture of *region* (or the monitor) for diffs."""
        try:
            if region:
                return capture(roi=(region["left"], region["top"], region["width"], region["height"]),
                               mode="turbo", quality=60)
            return capture(monitor=self.monitor, mode="turbo", quality=60)
        except Exception:
            return None

    def _changed_since(self, before: Frame) -> bool | None:
        """Compare a fresh turbo thumbnail of the SAME region against *before*.

        Comparing a window crop with a full-monitor capture (as sessions did)
        always differs in shape, so every click looked like a visual change.
        """
        try:
            after = capture(roi=(before.left, before.top, before.screen_w, before.screen_h),
                            mode="turbo", quality=60)
            return diff_ratio(before.thumbnail(240), after.thumbnail(240)) > 0.003
        except Exception:
            return None


engine = Engine()
