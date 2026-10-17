"""One isolated worker process per durable job, with a visible control panel."""
import ctypes
import json
from pathlib import Path
import queue
import sys
import threading
import time

from . import input as inp
from .control import Control, Paused, Cancelled, BudgetExceeded, PhysicalInputWatch
from .engine import Engine
from .frames import capture_hwnd, diff_ratio
from .jobs import atomic_json, read_json, process_stamp, TERMINAL
from .vision import VisionClient, is_pass
from .win32core import foreground_hwnd, pid_of_window, window_class, window_rect, user32


class Attention(RuntimeError):
    pass


class Runner:
    def __init__(self, folder: Path, client=None, engine=None):
        self.folder = folder
        self.spec = read_json(folder / "spec.json")
        self.state = read_json(folder / "state.json")
        self.history = read_json(folder / "history.json", [])
        self.elapsed_before = self.state.get("elapsed_seconds", 0)
        self.started = time.monotonic()
        self.control = Control(max(0, self.spec["max_seconds"] - self.elapsed_before))
        self.client = client or VisionClient()
        self.engine = engine or Engine(client=self.client, motion="instant")
        self.client.before_request = self.before_request
        self.base_usage = {k: self.state.get(k, 0) for k in
                           ("requests", "prompt_tokens", "completion_tokens", "usage_reports")}
        self.pending = None
        self.command_id = None
        self.done = threading.Event()
        self.watch = None
        self.latencies = []
        self.lock = threading.RLock()
        self.expected_hwnd = 0
        self.expected_rect = None
        if self.state.get("pending_action"):
            self.history.append("Interrupted during an input action; it may be partially applied. Re-observe; do not replay blindly.")
        self.state.pop("error", None)

    def before_request(self):
        self.control.check()
        if self.base_usage["requests"] + self.client.total_requests >= self.spec["max_requests"]:
            raise BudgetExceeded("Vision request budget reached")

    def save(self, **changes):
        with self.lock:
            self.state.update(changes, updated_at=time.time(),
                              elapsed_seconds=round(self.elapsed_before + time.monotonic() - self.started, 1))
            self.state["requests"] = self.base_usage["requests"] + self.client.total_requests
            for key in ("prompt_tokens", "completion_tokens", "usage_reports"):
                self.state[key] = self.base_usage[key] + getattr(self.client, key, 0)
            self.state["usage_complete"] = self.state["usage_reports"] == self.state["requests"]
            atomic_json(self.folder / "state.json", self.state)

    def stage(self, message, status="running"):
        self.save(stage=message, status=status)
        if self.engine._hud:
            self.engine._hud.notice(message + " | Ctrl+Alt+F12 cancels", "amber" if status == "paused" else "cyan")

    def poll_commands(self):
        command = read_json(self.folder / "command.json")
        if command.get("id") == self.command_id:
            return
        self.command_id = command.get("id")
        if command.get("command") == "cancel":
            self.control.cancel()
        elif command.get("command") == "pause":
            self.control.pause()
        elif command.get("command") == "resume":
            self.control.resume()

    def check(self):
        self.control.check()
        self.engine.safety.check()

    def input_check(self):
        self.check()
        if foreground_hwnd() != self.expected_hwnd:
            self.control.pause("Focus changed; refocus the target and resume")
            self.control.check()
        if self.expected_rect and window_rect(self.expected_hwnd) != self.expected_rect:
            self.control.pause("Window moved; resume to capture its new position")
            self.control.check()
        inp.foreground_guard.check()

    def select_target(self):
        target = self.spec["target"]
        root = target["hwnd"]
        if (pid_of_window(root) != target["pid"] or window_class(root) != target["class"]
                or process_stamp(target["pid"]) != target["process_stamp"]):
            raise Attention("Target application closed or restarted; resume with its new window ID")
        fg = foreground_hwnd()
        owner = fg
        for _ in range(16):
            if owner == root:
                return fg
            owner = int(user32.GetWindow(owner, 4) or 0) if owner else 0  # GW_OWNER
            if not owner:
                break
        self.control.pause("Target is not foreground; focus it and click Resume")
        self.control.check()

    def call_vision(self, method, *args):
        # An interrupted response is discarded. Never start another request until
        # the old request has drained; cancellation can still exit the process.
        while self.pending and self.pending.is_alive():
            self.check()
            self.pending.join(0.05)
        self.check()
        result = queue.Queue()
        generation = self.control.generation
        def run():
            try:
                result.put((True, method(*args)))
            except Exception as exc:
                result.put((False, exc))
        self.pending = threading.Thread(target=run, daemon=True, name="clickwarden-vision-request")
        self.pending.start()
        while True:
            self.check()
            if generation != self.control.generation:
                raise Paused("Discarding a response captured before user intervention")
            try:
                ok, value = result.get(timeout=0.05)
                if not ok:
                    raise value
                if value.get("latency_ms") is not None:
                    self.latencies.append(value["latency_ms"])
                return value
            except queue.Empty:
                pass

    def verify_completion(self, frame):
        check = self.spec.get("completion", {})
        if check:
            path = Path(check["path"])
            if not path.is_file() or path.stat().st_size == 0:
                raise Attention("Completion file is missing or empty")
            if path.stat().st_mtime_ns == check.get("before_mtime_ns"):
                raise Attention("Completion file has not changed since the job started")
            if check.get("expected_text"):
                if path.stat().st_size > 10_000_000:
                    raise Attention("Completion text check is limited to 10 MB files")
                # A non-UTF-8 file must yield needs_attention, not a crashed job.
                if check["expected_text"] not in path.read_text(encoding="utf-8", errors="replace"):
                    raise Attention("Completion file does not contain the expected text")
            return "file_checked"
        self.stage("Checking the visible result")
        verdict = self.call_vision(self.client.inspect, frame,
            "Independently check this goal: " + self.spec["goal"] +
            ". Reply exactly PASS only if the screenshot demonstrates completion. Otherwise reply FAIL.")
        if not verdict.get("success") or not is_pass(verdict.get("answer")):
            raise Attention("Separate visual completion check did not pass")
        return "vision_checked (not a drawing/geometry audit)"

    def execute(self, plan):
        action = plan.get("action")
        self.input_check()
        if action in ("click", "double_click"):
            x, y = plan["screen_point"]
            self.engine.hud_reticle(x, y)
            self.wait(0.25)  # show the artificial pointer heading to its target
            self.input_check()
            inp.click(x, y, button=plan.get("button", "left"),
                      clicks=2 if action == "double_click" else 1, human=False)
            self.engine.hud_flash(x, y)
        elif action == "type":
            inp.type_text(plan.get("text", ""))
            if plan.get("press_enter"):
                self.input_check()
                inp.hotkey("enter")
        elif action == "hotkey":
            inp.hotkey(plan.get("keys", ""))
        elif action == "scroll":
            inp.scroll(max(-20, min(20, int(plan.get("clicks", -3)))))
        elif action == "wait":
            self.wait(max(0.1, min(30, float(plan.get("seconds", 1)))))
        else:
            raise Attention(f"Unsupported action: {action}")

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.05, max(0, end - time.monotonic())))

    def run(self):
        previous_signature = None
        repeats = 0
        previous_frame = None
        self.engine.hud_begin(status="Preparing job | Ctrl+Alt+F12 cancels")
        self.engine.safety.arm()
        try:
            if self.watch:
                self.watch.enabled = True
            with inp.dispatch_guard(self.input_check):
                while True:
                    try:
                        self.check()
                        if self.state.get("steps", 0) >= self.spec["max_steps"]:
                            raise BudgetExceeded("Step budget reached")
                        self.expected_hwnd = self.select_target()
                        inp.foreground_guard.arm(self.expected_hwnd)
                        rect = window_rect(self.expected_hwnd)
                        self.expected_rect = rect
                        self.stage("Looking at the target window")
                        frame = capture_hwnd(self.expected_hwnd, pad=0, max_width=1600)
                        if frame is None:
                            raise Attention("Target window cannot be captured")
                        self.stage("Vision is choosing the next action")
                        plan = self.call_vision(self.client.plan, frame, self.spec["goal"], self.history[-12:])
                        self.input_check()
                        if window_rect(self.expected_hwnd) != rect:
                            self.control.pause("Window moved; resume to capture its new position")
                            self.control.check()
                        if not plan.get("success"):
                            raise Attention(plan.get("error") or "Vision planning failed")
                        action = plan.get("action")
                        if action == "done":
                            self.stage("Verifying completion")
                            fresh = capture_hwnd(self.expected_hwnd, pad=0, max_width=1600)
                            if fresh is None:
                                raise Attention("Cannot verify the final window state")
                            verification = self.verify_completion(fresh)
                            self.check()
                            self.save(status="completed", stage="Finished", verification=verification,
                                      summary=plan.get("summary", "Goal completed")[:500])
                            break
                        if action == "fail":
                            raise Attention(plan.get("reason", "Vision reported a blocker"))
                        signature = json.dumps({k: plan.get(k) for k in
                            ("action", "screen_point", "text", "keys", "clicks")}, sort_keys=True)
                        unchanged = (previous_frame is not None and
                            diff_ratio(previous_frame.thumbnail(240), frame.thumbnail(240)) < 0.003)
                        repeats = repeats + 1 if signature == previous_signature and unchanged else 0
                        if repeats >= 2 and action != "wait":
                            raise Attention("Repeated action with no visible progress")
                        previous_signature, previous_frame = signature, frame
                        self.stage(f"Step {self.state.get('steps', 0) + 1}: {action}")
                        # Persist intent before input. A restart re-observes; it never replays this record.
                        self.save(pending_action={"action": action, "reason": plan.get("reason", "")[:180]})
                        self.execute(plan)
                        self.history.append(f"{action}: {plan.get('reason', '')[:180]}")
                        self.history = self.history[-100:]
                        atomic_json(self.folder / "history.json", self.history)
                        self.save(steps=self.state.get("steps", 0) + 1, pending_action=None)
                        self.wait(0.2)
                    except Paused as exc:
                        self.stage(str(exc), status="paused")
                        while self.control.paused.is_set():
                            if self.control.cancelled.wait(0.05):
                                raise Cancelled("Cancelled by user")
                            if time.monotonic() >= self.control.deadline:
                                raise BudgetExceeded("Time budget reached while paused")
                        previous_frame, previous_signature, repeats = None, None, 0
                        self.history.append("User intervened; re-observe current state. Previous action may be partial.")
        except (Cancelled, inp.CornerAbort) as exc:
            self.save(status="cancelled", stage="Stopped", error=str(exc))
        except BudgetExceeded as exc:
            self.save(status="budget_reached", stage="Budget reached", error=str(exc))
        except (Attention, inp.ForegroundGuardError) as exc:
            self.save(status="needs_attention", stage="Needs attention", error=str(exc)[:500])
        except Exception as exc:
            self.save(status="failed", stage="Stopped", error=f"{type(exc).__name__}: {exc}"[:500])
        finally:
            if self.watch:
                self.watch.enabled = False
            inp.foreground_guard.disarm()
            self.engine.safety.disarm()
            if self.latencies:
                values = sorted(self.latencies)
                self.save(vision_p50_ms=values[len(values) // 2],
                          vision_p95_ms=values[min(len(values) - 1, int(len(values) * .95))])
            self.engine.hud_end(color="green" if self.state.get("status") == "completed" else "amber",
                                status=self.state.get("stage", "Stopped"))
            self.done.set()


def run_panel(runner, on_ready=None):
    from .panel import NotchPanel
    try:
        from .providers import panel_destination
        destination = panel_destination(runner.client.settings)
    except Exception:
        destination = None
    panel = NotchPanel(runner.spec["goal"], runner.control.pause,
                       runner.control.resume, runner.control.cancel, destination=destination)
    root, buttons, panel_hwnd = panel.root, panel.buttons, panel.hwnd
    watch = PhysicalInputWatch(runner.control).start()
    runner.watch = watch
    threading.Thread(target=runner.run, daemon=True, name="clickwarden-job").start()
    if on_ready:
        root.after(100, lambda: on_ready(root, buttons, panel_hwnd))

    def poll():
        try:
            runner.poll_commands()
            state = dict(runner.state)
            message = state.get("stage", "Starting")
            if runner.control.cancelled.is_set() and not runner.done.is_set():
                message = "Cancelling… no further input will be dispatched"
            panel.update(message, f"Steps {state.get('steps', 0)}/{runner.spec['max_steps']} • "
                         f"Vision calls {state.get('requests', 0)}/{runner.spec['max_requests']}",
                         runner.control.paused.is_set())
            if runner.done.is_set():
                watch.stop()
                root.after(1600, root.destroy)
                return
            root.after(50, poll)
        except Exception:
            runner.control.cancel()
            watch.stop()
            root.destroy()
    root.after(50, poll)
    root.mainloop()


def main():
    folder = Path(sys.argv[1]).resolve()
    runner = Runner(folder)
    # Only one worker may own the interactive desktop, even across MCP servers.
    k = ctypes.windll.kernel32
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.ReleaseMutex.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = k.CreateMutexW(None, False, "Local\\ClickwardenDesktopWorker")
    owned = bool(mutex) and k.WaitForSingleObject(mutex, 3000) in (0, 0x80)
    try:
        if not owned:
            raise RuntimeError("Another Clickwarden worker already owns the desktop")
        run_panel(runner)
    except Exception as exc:
        runner.control.cancel()
        runner.save(status="failed", error=f"Control panel unavailable: {exc}")
    finally:
        if not runner.done.is_set():
            runner.control.cancel()
            runner.done.wait(2)
        if owned:
            k.ReleaseMutex(mutex)
        if mutex:
            k.CloseHandle(mutex)


if __name__ == "__main__":
    main()
