"""Persistent, visible sessions for ad-hoc MCP action chains. No image results."""
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from . import input as inp
from .config import env_float
from .providers import load_config
from .control import Control, Cancelled, Paused, BudgetExceeded, PhysicalInputWatch
from .engine import Engine
from .jobs import atomic_json
from .targets import TargetFamily, assert_unoccluded
from .vision import VisionClient, is_pass
from .win32core import (foreground_hwnd, window_rect, cursor_pos, virtual_screen, client_center,
                        enumerate_windows, force_foreground, pid_of_window, user32, kernel32,
                        is_control_surface)

PRESETS = {"standard": (100, 1800, 160), "quick": (20, 300, 40), "extended": (500, 14400, 1000)}
# Optional USD per million tokens for the configured vision model; 0 = not reported.
_SAVED = load_config()
PRICE_IN = env_float("PRICE_IN", float(_SAVED.get("price_in") or 0.0))
PRICE_OUT = env_float("PRICE_OUT", float(_SAVED.get("price_out") or 0.0))


def estimated_cost(prompt_tokens, completion_tokens):
    if not (PRICE_IN or PRICE_OUT):
        return None
    return round((prompt_tokens * PRICE_IN + completion_tokens * PRICE_OUT) / 1e6, 6)


def evidence(result, observation=False):
    if observation or result.get("outcome")=="observed":
        return "observed"
    if result.get("status") in ("verified","success") and result.get("verification") in (
            "native_position_readback","vision_checked","window_title_checked","window_identity_readback",
            "native_control_disappearance","native_text_readback"):
        return result["verification"]
    if result.get("status") == "success":
        return "dispatched_unverified"
    return "unverified"


def launch_target(value):
    """Use the registered application handler, never a shell command string."""
    value = value.strip()
    if value.lower() == "steam":
        value = "steam://open/main"
    os.startfile(value)


class VisionProxy:
    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        original = getattr(self.session.client, name)
        if name not in ("locate", "inspect", "plan"):
            return original
        def call(*args, **kwargs):
            return self.session.vision_call(original, *args, **kwargs)
        return call


class Session:
    def __init__(self, objective="", app_or_url="", mode="standard", client=None):
        if mode not in PRESETS:
            raise ValueError("mode must be quick, standard or extended")
        self.id = uuid.uuid4().hex
        self.objective, self.app_or_url = objective, app_or_url
        self.max_actions, self.max_seconds, self.max_requests = PRESETS[mode]
        self.started = self.last_action = time.monotonic()
        self.client = client or VisionClient()
        self.base_usage = {key:getattr(self.client,key,0) for key in ("total_requests","prompt_tokens","completion_tokens","usage_reports")}
        self.engine = Engine(client=VisionProxy(self), motion="instant")
        self.raw_grab = self.engine.grab
        self.engine.grab = self.capture
        self.engine.session_active = True
        self.control = Control(self.max_seconds)
        self.ready, self.prepared, self.closed = threading.Event(), threading.Event(), threading.Event()
        self.operation_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.stopping = False
        self.panel_error = None
        self.panel_hwnd = 0
        self.watch = None
        self.family = None
        self.initial_hwnd = foreground_hwnd()
        self.target_hwnd = 0
        self.expected_rect = None
        self.stage = "Preparing session"
        self.steps = []
        self.actions = self.grounded = self.fallbacks = self.misses = 0
        self.failed_points = {}
        self.control_snapshot = None
        self.latencies = []
        self.failures = {}
        self.pending = None
        self.busy = False
        self.read_only = False
        self.ledger = []
        self.active_seconds = self.vision_seconds = 0.0
        self.prepared_at = None
        self.first_productive_at = None
        self.desktop_target = app_or_url.casefold() == "desktop"
        self.error = ""
        self.result = ""
        self.path = Path.home() / ".clickwarden" / "sessions" / (self.id + ".json")
        self.client.before_request = self.before_request

    def before_request(self):
        self.check()
        if self.usage("total_requests") >= self.max_requests:
            raise BudgetExceeded("Session request budget reached")

    def check(self):
        if self.stopping or self.closed.is_set():
            raise Cancelled("Session stopped")
        if self.panel_error or not self.ready.is_set():
            raise Cancelled("Session panel unavailable")
        self.control.check(ignore_pause=self.read_only)
        if not self.read_only:
            self.engine.safety.check()

    def usage(self,key):
        return getattr(self.client,key,0)-self.base_usage[key]

    def usage_since(self, start):
        """Requests/tokens spent since *start* (a dict of usage() values)."""
        spent = {key: self.usage(key) - start[key] for key in start}
        spent["est_cost_usd"] = estimated_cost(spent["prompt_tokens"], spent["completion_tokens"])
        return spent

    def scroll_point(self):
        """Wheel at the target's client centre, not wherever the shared cursor rests."""
        point = client_center(self.target_hwnd) if self.target_hwnd else None
        if point is None:
            raise ValueError("Target window has no client area to scroll; pass x and y")
        return point

    def input_check(self):
        if self.read_only:
            raise RuntimeError("Input is forbidden during a read-only observation")
        self.check()
        if not self.family or not self.family.contains(foreground_hwnd()):
            self.control.pause("Focus left the target application; refocus it and Resume")
            self.control.check()
        if foreground_hwnd() != self.target_hwnd or window_rect(self.target_hwnd) != self.expected_rect:
            self.control.pause("Target window changed; Resume to observe it again")
            self.control.check()

    def point_check(self, x, y):
        self.input_check()
        self.avoid_panel([(x,y)])
        assert_unoccluded(x, y, self.target_hwnd, self.family, forbidden=(self.panel_hwnd,))

    def avoid_panel(self, points):
        """Move our own control surface away before input, without activating it."""
        if not self.panel_hwnd:
            return
        l,t,r,b=window_rect(self.panel_hwnd)
        def overlaps(left,top):
            return any(left-24 <= x <= left+(r-l)+24 and top-24 <= y <= top+(b-t)+24 for x,y in points)
        if not overlaps(l,t):
            return
        vx,vy,vw,vh=virtual_screen()
        options=[(vx+12,vy+12),(vx+vw-(r-l)-12,vy+12),
                 (vx+12,vy+vh-(b-t)-12),(vx+vw-(r-l)-12,vy+vh-(b-t)-12)]
        destination=next((p for p in options if not overlaps(*p)),None)
        if destination is None:
            raise RuntimeError("No safe place for controls around these targets")
        import win32gui
        win32gui.SetWindowPos(self.panel_hwnd,0,*destination,0,0,0x0010|0x0004|0x0001)  # NOACTIVATE|NOZORDER|NOSIZE
        self.ledger.append({"action":"panel_relocated","offset":round(time.monotonic()-self.started,3)})

    def capture(self, *args, **kwargs):
        if self.read_only:
            self.check()
            if window_rect(self.target_hwnd) != self.expected_rect:
                raise RuntimeError("Target moved during observation; request a fresh observation")
        else:
            self.input_check()
        l, t, r, b = self.expected_rect
        if kwargs.get("roi") is None:
            kwargs["roi"] = (l, t, r-l, b-t)
        else:
            x, y, w, h = kwargs["roi"]
            left, top, right, bottom = max(l,x), max(t,y), min(r,x+w), min(b,y+h)
            if right <= left or bottom <= top:
                raise ValueError("ROI does not overlap the session target window")
            kwargs["roi"] = (left,top,right-left,bottom-top)
        frame = self.raw_grab(*args, **kwargs)
        from .targets import mask_foreign_windows
        return mask_foreign_windows(frame, self.target_hwnd, self.family)

    def cancel(self):
        self.control.cancel()
        self.stage = "Action cancelled — session remains open"

    def pause(self):
        self.control.pause()

    def resume(self):
        self.control.resume()

    def stop(self, result="stopped"):
        self.result = result
        self.stopping = True
        self.control.cancel()
        self.stage = result

    def snapshot(self):
        return {"session_id": self.id, "panel": "closed" if self.closed.is_set() else "visible",
                "status": "stopping" if self.stopping else "needs_attention" if self.error else "paused" if self.control.paused.is_set() else
                          "preparing" if not self.prepared.is_set() else "busy" if self.busy else "idle",
                "target_hwnd": self.target_hwnd, "last_action": self.stage,
                "error": self.error, "actions": self.actions, "requests": self.usage("total_requests"),
                "prompt_tokens": self.usage("prompt_tokens"), "completion_tokens": self.usage("completion_tokens"),
                "est_cost_usd": estimated_cost(self.usage("prompt_tokens"), self.usage("completion_tokens")),
                "pause_reason": self.control.reason if self.control.paused.is_set() else "",
                "last_physical_input": self.control.last_input}

    def metrics(self):
        values = sorted(self.latencies)
        outcomes={key:0 for key in ("native_text_readback","native_position_readback","native_control_disappearance","vision_checked","window_title_checked","window_identity_readback","observed","dispatched_unverified","unverified")}
        for entry in self.ledger:
            if entry["action"]=="batch_steps":
                for result in entry["results"]:
                    outcomes[evidence(result)] += 1
            elif entry["action"]!="batch" and "outcome" in entry:
                outcomes[entry["outcome"]] += 1
        return {**self.snapshot(), "result": self.result, "objective": self.objective,
                "outcomes":outcomes, "grounding_misses":self.misses,
                "wall_seconds": round(time.monotonic() - self.started, 2),
                "usage_complete": self.usage("usage_reports") == self.usage("total_requests"),
                "startup_ms": round((self.prepared_at-self.started)*1000,1) if self.prepared_at else None,
                "first_productive_ms": round((self.first_productive_at-self.started)*1000,1) if self.first_productive_at else None,
                "active_seconds": round(self.active_seconds,3), "vision_wait_seconds":round(self.vision_seconds,3),
                "outside_action_seconds":round(max(0,time.monotonic()-self.started-self.active_seconds),3),
                "vision_p50_ms": values[len(values)//2] if values else None,
                "vision_p95_ms": values[min(len(values)-1, int(len(values)*.95))] if values else None,
                "grounded_actions": self.grounded, "fallback_actions": self.fallbacks,
                "misses": self.misses, "last_steps": self.steps[-3:]}

    def save(self):
        with self.metrics_lock:
            atomic_json(self.path, {**self.metrics(), "ledger":self.ledger})

    def vision_call(self, method, *args, **kwargs):
        started=time.monotonic()
        try:
            return self._vision_call(method,*args,**kwargs)
        finally:
            self.vision_seconds += time.monotonic()-started

    def _vision_call(self, method, *args, **kwargs):
        if self.pending and self.pending.is_alive():
            raise RuntimeError("Previous vision request is draining; wait before another action")
        token = self.control
        generation = token.generation
        read_only = self.read_only
        results = queue.Queue()
        def request():
            try:
                results.put((True, method(*args, **kwargs)))
            except Exception as exc:
                results.put((False, exc))
        self.pending = threading.Thread(target=request, daemon=True, name="clickwarden-session-vision")
        self.pending.start()
        while True:
            self.check()
            if token is not self.control or (not read_only and generation != token.generation):
                raise Paused("Discarding stale observation after user intervention")
            try:
                ok, value = results.get(timeout=.05)
            except queue.Empty:
                continue
            self.check()
            if not ok:
                raise value
            latency = value.get("latency_ms") if isinstance(value, dict) else value.total_ms
            if latency is not None:
                self.latencies.append(latency)
            return value

    def prepare(self):
        """Called only after the panel has painted and hooks are installed."""
        try:
            # Startup has no input probe or paid preflight; first useful work
            # establishes provider health. Geometry checks remain local.
            self.control.check(ignore_pause=True)
            vx, vy, vw, vh = virtual_screen()
            if vw <= 0 or vh <= 0 or not user32.IsProcessDPIAware():
                raise RuntimeError("DPI or desktop geometry self-check failed")
            if not self.ready.is_set() or self.stopping:
                raise Cancelled("Session panel unavailable")
            if self.desktop_target:
                from .desktop import LayoutTransactions
                data = LayoutTransactions().inventory()
                self.target_hwnd = data["hwnd"]
                self.family = TargetFamily(self.target_hwnd)
                self.stage = "Desktop ready — native inventory and layouts available"
                return
            before = {w["hwnd"] for w in enumerate_windows()}
            from .targets import process_parents
            initial_exe = process_parents().get(pid_of_window(self.initial_hwnd), (0, ""))[1].casefold()
            reuse_browser = self.app_or_url.lower().startswith(("https://", "http://")) and initial_exe in (
                "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe")
            if self.app_or_url:
                self.stage = "Opening " + self.app_or_url[:70]
                self.check()
                launch_target(self.app_or_url)
            deadline = time.monotonic() + 10
            while True:
                self.control.check(ignore_pause=True)
                if self.stopping:
                    raise Cancelled("Session stopped")
                windows = [w for w in enumerate_windows() if not self.is_control_surface(w)]
                fg = foreground_hwnd()
                candidates = [w for w in windows if w["hwnd"] == fg and
                              (not self.app_or_url or fg != self.initial_hwnd or fg not in before
                               or reuse_browser
                               or self.app_or_url.casefold() in w["title"].casefold())]
                if not candidates and not self.app_or_url:
                    candidates = [w for w in windows if w["hwnd"] == self.initial_hwnd]
                if not candidates and self.app_or_url:
                    candidates = [w for w in windows if w["hwnd"] not in before]
                if candidates:
                    self.target_hwnd = candidates[0]["hwnd"]
                    self.family = TargetFamily(self.target_hwnd)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("No target window appeared; focus the app and start a new session")
                time.sleep(.05)
            self.stage = "Ready — waiting for an action"
        except Exception as exc:
            self.error = str(exc)[:300]
            self.stage = "Self-check: " + self.error
        finally:
            self.prepared_at = time.monotonic()
            self.prepared.set()
            self.save()

    def is_control_surface(self, window):
        """Never adopt an overlay/control surface as the application target."""
        hwnd = window['hwnd']
        hud_hwnd = getattr(self.engine._hud, '_hwnd', None)
        return (hwnd == self.panel_hwnd or
                (isinstance(hud_hwnd, int) and hwnd == hud_hwnd) or
                is_control_surface(window))

    def run(self, name, kwargs):
        if not self.prepared.is_set():
            return {**self.snapshot(), "status": "preparing", "message": "Call session status after startup finishes"}
        if self.error:
            return {"status": "error", "error": self.error}
        if not self.operation_lock.acquire(False):
            return {"status": "busy", "error": "An action is already running"}
        call_started = time.monotonic()
        entry = {"action":name,"started_offset":round(call_started-self.started,3),"status":"interrupted"}
        start_usage = {key: self.usage(key) for key in ("total_requests", "prompt_tokens", "completion_tokens")}
        self.read_only = name in ("inspect","locate","desktop_inventory","find_files","controls") or (name=="desktop_arrange" and kwargs.get("dry_run",False))
        generation = self.control.generation
        input_revision = self.control.input_revision
        try:
            if self.pending and self.pending.is_alive():
                return {"status": "draining", "message": "Cancelled provider request still draining"}
            if self.control.paused.is_set() and not self.read_only:
                raise Paused(self.control.reason)
            self.control.cancelled.clear()
            if not self.read_only:
                self.control.generation += 1
            self.check()
            if not self.read_only and name != "click_control":
                self.control_snapshot = None
            if name == "click_control" and self.control_snapshot and (
                    self.control_snapshot['generation'] != generation or
                    self.control_snapshot['input_revision'] != input_revision):
                self.control_snapshot = None
            if self.actions >= self.max_actions:
                raise BudgetExceeded("Session action budget reached")
            independent = name in ("switch_window", "find_files")
            if not independent:
                self.family.refresh()
            fg = foreground_hwnd()
            native = name.startswith("desktop_")
            if native and not self.desktop_target:
                raise ValueError("Start a desktop session with app_or_url='desktop' for native desktop operations")
            if not independent and not native and not self.read_only and (fg == self.panel_hwnd or not self.family.contains(fg)):
                raise Paused("Focus the target application and retry")
            if not independent and not native and fg != self.panel_hwnd and self.family.contains(fg):
                self.target_hwnd = fg
            if not independent:
                self.expected_rect = window_rect(self.target_hwnd)
            self.busy = True
            self.actions += 1  # reserve before input, including interrupted attempts
            self.stage = name + ": " + str(kwargs.get("description", kwargs.get("query", "")))[:100]
            self.engine.gate = self.check if self.read_only else self.input_check
            if not self.read_only:
                self.engine.safety.arm()
                inp.foreground_guard.arm(self.target_hwnd)
            with inp.dispatch_guard(self.input_check, self.point_check):
                kwargs = dict(kwargs)
                if name in ("locate", "smart_click", "inspect"):
                    if not kwargs.get("roi") or kwargs.get("roi") in ("active_window", "auto"):
                        l, t, r, b = self.expected_rect
                        kwargs["roi"] = (l, t, r-l, b-t)
                    kwargs["fresh"] = True
                if name == "scroll" and kwargs.get("x") is None and kwargs.get("y") is None:
                    kwargs["x"], kwargs["y"] = self.scroll_point()
                if name == "switch_window":
                    value = self.switch_window(**kwargs)
                elif name == "controls":
                    value = self.controls(**kwargs)
                elif name == "click_control":
                    value = self.click_control(**kwargs)
                elif name == "find_files":
                    from .workflows import find_files
                    value = find_files(check=self.check, **kwargs)
                elif name == "navigate":
                    value = self.navigate(**kwargs)
                elif native:
                    from .desktop import LayoutTransactions
                    transactions = LayoutTransactions()
                    if name=="desktop_inventory":
                        value={"status":"observed", **transactions.inventory()}
                    elif name=="desktop_arrange":
                        value=transactions.arrange(check=self.check,**kwargs)
                    elif name=="desktop_undo":
                        value=transactions.undo(check=self.check,**kwargs)
                    else:
                        raise ValueError("Unknown desktop operation")
                elif name == "batch":
                    value = self.batch(kwargs)
                elif name == "task":
                    kwargs["max_steps"] = min(kwargs.get("max_steps", 6), self.max_actions - self.actions + 1)
                    value = self.engine.task(**kwargs)
                    self.actions += max(0, len(value.get("log", [])) - 1)
                elif name == "smart_click":
                    value = self.click_with_fallback(kwargs)
                elif name == "drag":
                    self.avoid_panel([(kwargs["x1"],kwargs["y1"]),(kwargs["x2"],kwargs["y2"])])
                    value = self.engine.drag(**kwargs)
                else:
                    value = getattr(self.engine, name)(**kwargs)
            productive = (value.get("status") in ("success","verified","observed","planned","completed")
                          or (value.get("status") is None and value.get("success") is True))
            if self.first_productive_at is None and productive and value.get("success",True):
                self.first_productive_at = time.monotonic()
            if self.read_only:
                value.update(observation_only=True,reusable_for_input=False,
                             user_activity_during_observation=input_revision!=self.control.input_revision or generation!=self.control.generation)
            entry.update(status=value.get("status","observed"),verification=value.get("verification","unverified"))
            entry["outcome"]=evidence(value,self.read_only)
            value.setdefault("outcome",entry["outcome"])
            spent = self.usage_since(start_usage)
            if spent["total_requests"]:
                value["usage"] = spent
            self.steps.append({"action": name, "status": value.get("status", value.get("success")),
                               "strategy": value.get("strategy", "grounded" if name == "smart_click" else "direct")})
            self.stage = f"{name}: {value.get('status', 'done')}"
            return value
        except (Cancelled, Paused, BudgetExceeded) as exc:
            self.stage = str(exc)
            entry.update(status=type(exc).__name__.lower(),error=str(exc),outcome="unverified")
            return {"status": type(exc).__name__.lower(), "error": str(exc), "session_id": self.id}
        except Exception as exc:
            self.stage = str(exc)[:200]
            entry.update(status="error",error=self.stage,outcome="unverified")
            return {"status": "error", "error": self.stage}
        finally:
            duration=time.monotonic()-call_started
            self.active_seconds += duration
            spent = self.usage_since(start_usage)
            entry.update(duration_seconds=round(duration,3),requests=spent["total_requests"],
                         prompt_tokens=spent["prompt_tokens"],completion_tokens=spent["completion_tokens"])
            self.ledger.append(entry)
            self.read_only = False
            self.busy = False
            self.last_action = time.monotonic()
            inp.foreground_guard.disarm()
            self.engine.safety.disarm()
            try:
                self.save()
            finally:
                self.operation_lock.release()

    def switch_window(self, hwnd):
        """Explicit target change; never infer a new app from accidental focus."""
        if type(hwnd) is not int or hwnd <= 0 or hwnd == self.panel_hwnd:
            raise ValueError("Choose a visible application HWND from list_windows")
        matches = [w for w in enumerate_windows() if w["hwnd"] == hwnd and not self.is_control_surface(w)]
        if not matches:
            raise ValueError("Target window is no longer visible; list windows again")
        candidate = TargetFamily(hwnd)
        self.check()
        user32.SetForegroundWindow(hwnd)
        if not self._await_foreground(hwnd):
            # Windows foreground lock: this process does not own the
            # foreground, so a bare SetForegroundWindow is refused.  Use the
            # same tested helper typing uses; it sends only a tagged ALT
            # gesture (ignored by the takeover watcher) and never types text
            # into the application.  If focus still cannot be taken, pause and
            # wait for an explicit resume.
            force_foreground(hwnd, title=matches[0]["title"], attempts=2)
            if not self._await_foreground(hwnd):
                self.control.pause("Windows refused focus; focus the chosen window and Resume")
                raise Paused(self.control.reason)
        self.check()
        candidate.refresh()
        if not candidate.contains(hwnd):
            raise RuntimeError("Target process changed during switch; list windows again")
        rect = window_rect(hwnd)
        self.family, self.target_hwnd, self.expected_rect = candidate, hwnd, rect
        self.desktop_target = False
        self.engine.cache.invalidate()
        self.failures.clear()
        self.failed_points.clear()
        return {"status": "success", "target_hwnd": hwnd, "title": matches[0]["title"],
                "verification": "window_identity_readback", "fresh_observation_required": True}

    def _await_foreground(self, hwnd, timeout=.6):
        """Wait for the explicit switch to land; a physical pause aborts it."""
        deadline = time.monotonic() + timeout
        while foreground_hwnd() != hwnd:
            self.check()
            if time.monotonic() >= deadline:
                return False
            time.sleep(.03)
        return True

    def navigate(self, url, query=""):
        from .workflows import BROWSERS, web_url, wait_for_content_change
        url = web_url(url)
        if self.family.exe not in BROWSERS:
            raise ValueError("Switch to a supported browser window before navigation")
        self.input_check()
        def sample():
            pixels = self.engine.grab(mode="turbo", fresh=True, roi=self.expected_rect_roi()).thumbnail(240)
            # Ignore the top third, including the address bar whose own edit
            # would otherwise count as a loaded page.
            return pixels[pixels.shape[0]//3:]
        before = sample() if query else None
        self.engine.hotkey("ctrl,l")
        self.input_check()
        self.engine.type(url, press_enter=True)
        self.engine.cache.invalidate()
        # Dispatch is not evidence that the destination loaded.
        result = {"status": "success", "url": url, "verification": "unverified",
                  "next": "Use fast_inspect to read the loaded page or search results"}
        if query:
            self.stage = "Waiting for browser content before reading"
            result["readiness"] = wait_for_content_change(before, sample, self.input_check)
            self.input_check()
            self.stage = "Reading visible browser content"
            result["observation"] = self.engine.inspect(query, fresh=True, roi=self.expected_rect_roi())
        return result

    def batch(self, kwargs):
        actions = kwargs["actions"]
        # Validate the complete schema before an early valid action can mutate.
        required={"click":("description",),"double_click":("description",),"type":("text",),
                  "hotkey":("keys",),"scroll":("clicks",),"wait":(),
                  "move":("x","y"),
                  "drag":("start_x","start_y","end_x","end_y")}
        if not isinstance(actions,list) or not actions:
            raise ValueError("Batch must contain at least one action")
        for item in actions:
            if not isinstance(item,dict) or item.get("action") not in required:
                raise ValueError("Unsupported or malformed batch action")
            kind=item["action"]
            if any(key not in item for key in required[kind]):
                raise ValueError("Missing batch fields for "+kind)
            if kind in ('drag','move') and any(type(item[k]) is not int for k in required[kind]):
                raise ValueError("Mouse coordinates must be integers")
            if kind in ("type","hotkey","click","double_click") and not isinstance(item[required[kind][0]],str):
                raise ValueError("Text, descriptions and hotkeys must be strings")
            if kind=='type':
                item['text'].encode('utf-16-le')
                if type(item.get('wpm',0)) is not int or item.get('wpm',0)<0:
                    raise ValueError('wpm must be a non-negative integer')
                if type(item.get('press_enter',False)) is not bool:
                    raise ValueError('press_enter must be a boolean')
                if type(item.get('allow_non_editable',False)) is not bool:
                    raise ValueError('allow_non_editable must be a boolean')
            if kind=='scroll':
                if type(item['clicks']) is not int or not -1000 <= item['clicks'] <= 1000:
                    raise ValueError('Scroll clicks must be an integer between -1000 and 1000')
                if (item.get('x') is None) != (item.get('y') is None):
                    raise ValueError('Provide both scroll x and y, or neither')
                if item.get('x') is not None and any(type(item[k]) is not int for k in ('x','y')):
                    raise ValueError('Scroll coordinates must be integers')
            if kind in ('click','double_click'):
                if item.get('button','left') not in ('left','right','middle'):
                    raise ValueError('Unknown mouse button')
                clicks=item.get('clicks',2 if kind=='double_click' else 1)
                if type(clicks) is not int or clicks not in (1,2,3):
                    raise ValueError('Use 1–3 clicks')
            if kind=="wait":
                seconds=item.get("seconds",1)
                if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:
                    raise ValueError("Wait duration must be a finite nonnegative number")
            if kind=="hotkey":
                inp.validate_hotkey(item['keys'])
        if len(actions) > self.max_actions - self.actions + 1:
            raise BudgetExceeded("Batch exceeds remaining action budget")
        results = []
        # Retain dispatched steps even if a later step is cancelled or fails.
        self.ledger.append({"action":"batch_steps","results":results})
        for index, item in enumerate(actions):
            self.input_check()
            if index:
                self.actions += 1
            kind = item.get("action", "click")
            if kind in ("click", "double_click"):
                value = self.click_with_fallback(dict(description=item.get("description", ""),
                    roi=item.get("roi", self.expected_rect_roi()), verify=item.get("verify", True),
                    expected_result=item.get("expected_result", ""),
                    button=item.get('button','left'),
                    clicks=item.get('clicks',2 if kind == 'double_click' else 1)))
            elif kind == "wait":
                self.engine.wait(min(10, max(0, float(item.get("seconds", 1)))))
                value = {"status": "success", "action":"wait", "outcome":"observed"}
            elif kind == "type":
                value = self.engine.type(item.get("text", ""), press_enter=item.get("press_enter", False),
                                         wpm=item.get('wpm',0),
                                         **({'allow_non_editable': True} if item.get('allow_non_editable') else {}))
            elif kind == "hotkey":
                value = self.engine.hotkey(item.get("keys", ""))
            elif kind == "scroll":
                x, y = (item['x'], item['y']) if item.get('x') is not None else self.scroll_point()
                value = self.engine.scroll(item['clicks'],x=x,y=y)
            elif kind == 'move':
                value = self.engine.move(item['x'],item['y'])
            elif kind == "drag":
                self.avoid_panel([(item["start_x"],item["start_y"]),(item["end_x"],item["end_y"])])
                value = self.engine.drag(int(item["start_x"]),int(item["start_y"]),int(item["end_x"]),int(item["end_y"]))
                value.update(verification="unverified",outcome="dispatched")
            else:
                raise ValueError("Unsupported batch action: " + str(kind))
            results.append(value)
            self.steps.append({"action":kind,"status":value.get("status"),"strategy":"batch"})
            self.stage=f"Batch {index+1}/{len(actions)}: {kind} — {value.get('outcome',value.get('status'))}"
            if value.get("status") != "success":
                break
        return {"status": "completed" if all(v.get("status") == "success" for v in results) else "failed",
                "steps":len(results),"dispatched":sum(v.get("outcome")=="dispatched" for v in results),
                "verification":"unverified" if any(v.get("verification")=="unverified" for v in results) else "mixed",
                "last_results":results[-3:]}

    def expected_rect_roi(self):
        l, t, r, b = self.expected_rect
        return l, t, r-l, b-t

    def click_with_fallback(self, kwargs):
        kwargs = dict(kwargs)
        expected_result = kwargs.pop("expected_result", "")
        description = kwargs["description"]
        key = description.strip().casefold()
        count = self.failures.get(key, 0)
        if count >= 2:
            return {"status": "needs_attention", "error": "Two grounded misses; fallback exhausted. No third click."}
        if key in self.failed_points:
            kwargs["avoid_point"] = self.failed_points[key]
        result = self.engine.smart_click(**kwargs)
        self.grounded += 1
        missed = result.get("status") != "success" or result.get("latency", {}).get("changed") is False
        # A visible change alone does not prove the intended control activated.
        if not missed and kwargs.get("verify", True):
            self.adopt_foreground()
            condition = expected_result or ("a visible response attributable to activating " + description)
            check = self.engine.inspect("Check the following expected state: " + condition +
                ". Reply exactly PASS only if visible evidence supports it; otherwise FAIL. "
                "Do not assume the click succeeded. A hover/tooltip alone is not evidence.", roi=self.expected_rect_roi())
            missed = not (check.get("success", True) and is_pass(check.get("answer")))
        if not missed:
            self.failures.pop(key, None)
            self.failed_points.pop(key, None)
            result["verification"]="vision_checked" if kwargs.get("verify",True) else "unverified"
            result["action_status"]="visually_checked" if kwargs.get("verify",True) else "dispatched"
            result["task_verified"]=False
            return result
        self.misses += 1
        self.failures[key] = count + 1
        if result.get("clicked_point"):
            self.failed_points[key] = result["clicked_point"]
        if count + 1 < 2:
            return {**result, "status": "retry_needed", "message": "No confirmed progress; next miss triggers fallback"}
        return self.escalate(description, kwargs.get("roi"))

    def controls(self, name_filter="", limit=100, max_nodes=2000, max_seconds=3):
        from .accessibility import inventory
        params = dict(name_filter=name_filter, limit=limit, max_nodes=max_nodes, max_seconds=max_seconds)
        generation, revision = self.control.generation, self.control.input_revision
        result = inventory(self.target_hwnd, check=self.check, **params)
        token = uuid.uuid4().hex
        self.control_snapshot = dict(token=token, result=result, params=params,
                                     generation=generation, input_revision=revision)
        return {**result, "snapshot_id": token}

    def click_control(self, snapshot_id, control_id, expect_gone=False, button='left', clicks=1):
        from .accessibility import inventory, validate_selection
        if type(expect_gone) is not bool:
            raise ValueError("expect_gone must be a boolean")
        if button not in ('left', 'right', 'middle') or type(clicks) is not int or clicks not in (1, 2, 3):
            raise ValueError('Use left/right/middle button and 1–3 clicks')
        snapshot = self.control_snapshot
        self.control_snapshot = None  # single-use, including failed attempts
        if not snapshot or snapshot['token'] != snapshot_id:
            raise ValueError("No current control snapshot; call fast_controls first")
        if snapshot['result'].get('target_hwnd') != self.target_hwnd:
            raise ValueError("Target changed; call fast_controls again")
        self.input_check()
        candidates = [c for c in snapshot['result'].get('controls',[]) if c.get('control_id') == control_id]
        if not snapshot['result'].get('complete') or len(candidates) != 1:
            raise ValueError('A complete, unambiguous control observation is required')
        box = candidates[0].get('box')
        if (not isinstance(box,list) or len(box)!=4 or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in box) or
                box[2]<=box[0] or box[3]<=box[1]):
            raise ValueError('Invalid native control geometry')
        # Our movable panel must get out of the way BEFORE the UIA hit-test.
        # This does not hide/ignore foreign windows or bypass fresh validation.
        self.avoid_panel([(math.floor((box[0]+box[2])/2),math.floor((box[1]+box[3])/2))])
        fresh = inventory(self.target_hwnd, selected_id=control_id, check=self.input_check, **snapshot['params'])
        point = validate_selection(snapshot['result'], fresh, control_id)
        self.point_check(*point)
        result = self.engine.click_control_point(point, self.target_hwnd, button=button, clicks=clicks)
        result['control_id'] = control_id
        hit = fresh.get('hit') or {}
        result['point_strategy'] = 'center' if hit.get('center_ok', True) else 'interior_fallback'
        if hit.get('center_ok') is False:
            result['candidates_tried'] = hit.get('candidates_tried')
        if result.get('status') == 'success' and expect_gone:
            self.engine.wait(.12)
            self.adopt_foreground()
            # Query the same HWND; do not mistake a newly adopted window for
            # proof that a control vanished from the original application.
            old_hwnd = fresh['target_hwnd']
            if self.target_hwnd != old_hwnd:
                result.update(postcondition='unverified_window_changed')
                return result
            params = {**snapshot['params'], 'name_filter': '', 'limit': 500}
            after = inventory(old_hwnd, check=self.input_check, **params)
            gone = (after.get('complete') is True and not any(
                c.get('control_id') == control_id for c in after.get('controls', [])))
            result.update(postcondition='control_absent' if gone else 'not_confirmed',
                          verification='native_control_disappearance' if gone else 'unverified',
                          task_verified=gone)
        return result

    def adopt_foreground(self):
        self.check()
        self.family.refresh()
        fg = foreground_hwnd()
        if fg == self.panel_hwnd or not self.family.contains(fg):
            raise Paused("Focus left target application")
        self.target_hwnd, self.expected_rect = fg, window_rect(fg)
        inp.foreground_guard.arm(fg)

    def escalate(self, description, roi):
        self.stage = "Trying supported protocol and window fallback"
        # Keyboard recovery requires an app adapter with an explicit mapping.
        # A model saying an arbitrary shortcut is 'known' is not such a mapping.
        if self.family.exe == "steam.exe" and "friend" in description.casefold():
            self.input_check()
            launch_target("steam://open/friends")
            self.fallbacks += 1
            for _ in range(20):
                self.check()
                self.family.refresh()
                found = [w for w in enumerate_windows() if "friend" in w["title"].casefold() and self.family.contains(w["hwnd"])]
                if found:
                    return {"status": "success", "strategy": "steam_protocol", "verification": "window_title_checked"}
                time.sleep(.1)
        self.family.refresh()
        matches = [w for w in enumerate_windows() if description.casefold() in w["title"].casefold() and self.family.contains(w["hwnd"])]
        if len(matches) == 1:
            self.check()
            outcome = force_foreground(matches[0]["hwnd"])
            self.fallbacks += 1
            return {**outcome, "strategy": "window_title", "verification": "focus_only"}
        return {"status": "needs_attention", "error": "Two misses; supported protocol/window fallback did not confirm success. Enumerate native controls or change strategy."}

    def panel(self):
        """Tk is created, used and destroyed exclusively on this thread."""
        root = None
        mutex = None
        owned = False
        try:
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            mutex = kernel32.CreateMutexW(None, False, "Local\\ClickwardenDesktopWorker")
            owned = bool(mutex) and kernel32.WaitForSingleObject(mutex, 0) in (0, 0x80)
            if not owned:
                raise RuntimeError("Another session or job owns the desktop")
            from .panel import NotchPanel
            try:
                from .providers import panel_destination
                destination = panel_destination(self.client.settings)
            except Exception:  # a custom client without settings still gets a panel
                destination = None
            panel = NotchPanel(self.objective, self.pause, self.resume, self.cancel, self.stop, destination)
            root = panel.root
            self.panel_hwnd = panel.hwnd
            self.watch = PhysicalInputWatch(self.control).start()
            self.watch.enabled = True
            self.engine.hud_begin(status="Session starting")
            self.ready.set()
            self.prepare_thread=threading.Thread(target=self.prepare, daemon=True, name="clickwarden-session-prepare")
            self.prepare_thread.start()
            def poll():
                if self.stopping:
                    # Input gates are closed before any UI disappears. Wait for action cleanup.
                    if self.busy:
                        panel.update("Stopping — releasing input")
                        root.after(50, poll)
                        return
                    color = "green" if self.result in ("success", "completed") else "red" if self.result in ("failed", "error") else "amber"
                    panel.update(self.result, color={"green":"#65df99", "red":"#ff7585", "amber":"#ffcf70"}[color])
                    self.engine.session_active = False
                    self.engine.hud_end(color=color, status=self.result)
                    root.after(450, root.destroy)
                    return
                idle = int(time.monotonic() - self.last_action)
                state = self.control.reason if self.control.paused.is_set() else self.stage
                tail = "\n".join(f"{s['action']}: {s['status']} ({s['strategy']})" for s in self.steps[-3:])
                panel.update(state, (f"Idle — waiting ({idle}s)\n" if not self.busy and self.prepared.is_set() else "") + tail, self.control.paused.is_set())
                root.after(100, poll)
            root.after(100, poll)
            root.mainloop()
        except Exception as exc:
            self.panel_error = str(exc)
            self.error = self.panel_error
        finally:
            self.stopping = True
            self.control.cancel()
            if self.watch:
                self.watch.stop()
            if root:
                try:
                    root.destroy()
                except Exception:
                    pass
            if self.engine._hud:
                self.engine._hud.shutdown()
            self.ready.set()
            # Keep the mutex until the cancelled action can no longer dispatch.
            while self.busy:
                time.sleep(.02)
            if owned:
                kernel32.ReleaseMutex(mutex)
            if mutex:
                kernel32.CloseHandle(mutex)
            self.closed.set()
            self.save()

    def panel_thread(self):
        try:
            self.panel()
        finally:
            # Tk callback reference cycles must be collected by their owner thread.
            import gc
            gc.collect()


class SessionManager:
    def __init__(self):
        self.current = None
        self.lock = threading.Lock()
        self.client = None

    def start(self, objective="", app_or_url="", mode="standard"):
        with self.lock:
            if self.current and not self.current.closed.is_set():
                raise RuntimeError("A session is open; end it before starting another")
            old = self.current
            if self.client is None or (old and ((old.pending and old.pending.is_alive()) or
                    (getattr(old,"prepare_thread",None) and old.prepare_thread.is_alive()))):
                self.client = VisionClient()
            session = self.current = Session(objective, app_or_url, mode, client=self.client)
            session.ui_thread = threading.Thread(target=session.panel_thread, daemon=True, name="clickwarden-session-panel")
            session.ui_thread.start()
        if not session.ready.wait(3):
            session.stop("Panel startup timed out")
            raise RuntimeError("Session panel did not become ready")
        if session.panel_error:
            raise RuntimeError(session.panel_error)
        return session.snapshot()

    def get(self, session_id=""):
        session = self.current
        if not session or session.closed.is_set() or (session_id and session_id != session.id):
            raise ValueError("No matching open session; call use_my_computer first")
        return session

    def end(self, result="completed", session_id=""):
        session = self.get(session_id)
        session.stop(result)
        session.closed.wait(2)
        if session.closed.is_set():
            session.ui_thread.join(1)
        session.save()
        metrics = session.metrics()
        summary = (f"{metrics['actions']} actions, {metrics['requests']} requests, "
                   f"{metrics['prompt_tokens']}/{metrics['completion_tokens']} prompt/completion tokens"
                   + (f" (~${metrics['est_cost_usd']})" if metrics['est_cost_usd'] is not None else "") + ", "
                   f"vision p50/p95 {metrics['vision_p50_ms']}/{metrics['vision_p95_ms']} ms, "
                   f"{metrics['wall_seconds']}s, {metrics['grounded_actions']} grounded / "
                   f"{metrics['fallback_actions']} fallback, {metrics['grounding_misses']} grounding misses; "
                   f"{metrics['outcomes']['native_position_readback']} native-verified / "
                   f"{metrics['outcomes']['vision_checked']} vision-checked / "
                   f"{metrics['outcomes']['dispatched_unverified']} dispatched-unverified")
        return {"session_id": session.id, "status": "ended" if session.closed.is_set() else "stopping",
                "summary": summary, "outcomes":metrics["outcomes"], "usage_complete": metrics["usage_complete"],
                "prompt_tokens": metrics["prompt_tokens"], "completion_tokens": metrics["completion_tokens"],
                "est_cost_usd": metrics["est_cost_usd"]}
