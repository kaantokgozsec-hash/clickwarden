"""Cancellation and physical-input takeover for cooperative desktop jobs."""
import ctypes
import threading
import time
from ctypes import wintypes

from .win32core import user32, kernel32, cursor_pos
from .input_identity import CLICKWARDEN_EXTRA_INFO

WM_MOUSEMOVE = 0x0200
MOUSE_PRESSES = {0x0201, 0x0204, 0x0207, 0x020B}
MOUSE_WHEELS = {0x020A, 0x020E}
KEY_PRESSES = {0x0100, 0x0104}


class Cancelled(RuntimeError):
    pass


class Paused(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Control:
    def __init__(self, max_seconds: float):
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.reason = ""
        self.deadline = time.monotonic() + max_seconds
        self.generation = 0
        self.input_revision = 0
        self.last_input = None
        self.rebase_input = None

    def cancel(self):
        self.cancelled.set()

    def pause(self, reason="Paused by user"):
        if not self.paused.is_set():
            self.reason = reason
            self.generation += 1
            self.paused.set()

    def resume(self):
        if self.rebase_input:
            self.rebase_input()
        self.generation += 1
        self.paused.clear()

    def check(self, ignore_pause=False):
        if self.cancelled.is_set():
            raise Cancelled("Cancelled by user")
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("Time budget reached")
        if self.paused.is_set() and not ignore_pause:
            raise Paused(self.reason)


class PhysicalInputWatch:
    """Observe real input, ignoring LL*_INJECTED events from SendInput.

    Never swallows input. Ctrl+Alt+F12 cancels. Real presses/wheel or accumulated
    movement of at least six physical pixels pauses; release events and jitter
    do not immediately re-pause a just-resumed session.
    """
    def __init__(self, control: Control):
        self.control = control
        self.ready = threading.Event()
        self.error = None
        self.thread_id = 0
        self.enabled = False
        self.mouse_anchor = None
        self.move_threshold = 6
        self.control.rebase_input = self.rebase

    def rebase(self):
        self.mouse_anchor = cursor_pos()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="clickwarden-takeover")
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            raise RuntimeError(self.error or "physical input watcher did not start")
        return self

    def stop(self):
        self.enabled = False
        if self.control.rebase_input == self.rebase:
            self.control.rebase_input = None
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)

    def mouse_event(self, flags, message=0x0201, point=None, extra=0):
        if flags & 1 or extra == CLICKWARDEN_EXTRA_INFO:
            # Own movement changes the true cursor baseline. Compare subsequent
            # physical movement against this landing, never the pre-click point.
            if point is not None:
                self.mouse_anchor = tuple(point)
            return
        if not self.enabled:
            return
        if message == WM_MOUSEMOVE:
            if point is None:
                return
            point = tuple(point)
            if self.mouse_anchor is None:
                self.mouse_anchor = point
                return
            distance = max(abs(point[0]-self.mouse_anchor[0]), abs(point[1]-self.mouse_anchor[1]))
            if distance < self.move_threshold:
                return
            reason, kind = f"Physical mouse moved {distance}px — paused", "mouse_move"
        elif message in MOUSE_PRESSES:
            reason, kind = "Physical mouse button pressed — paused", "mouse_button"
        elif message in MOUSE_WHEELS:
            reason, kind = "Physical mouse wheel used — paused", "mouse_wheel"
        else:
            # In particular, releasing the Resume button must not re-pause.
            return
        if point is not None:
            self.mouse_anchor = tuple(point)
        self.control.input_revision += 1
        self.control.last_input = {"kind": kind, "time": time.monotonic()}
        self.control.pause(reason)

    def keyboard_event(self, flags, vk, message, extra=0):
        if flags & 0x10 or extra == CLICKWARDEN_EXTRA_INFO or message not in KEY_PRESSES:
            return
        self.control.input_revision += 1
        self.control.last_input = {"kind": "key_press", "time": time.monotonic()}
        if (vk == 0x7B
                and user32.GetAsyncKeyState(0x11) & 0x8000
                and user32.GetAsyncKeyState(0x12) & 0x8000):
            self.control.cancel()
        elif self.enabled:
            self.control.pause("Physical keyboard key pressed — paused")

    def _run(self):
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                     ctypes.c_size_t, ctypes.c_ssize_t)
        class Mouse(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.c_size_t)]
        class Key(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.c_size_t)]
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def mouse(code, message, ptr):
            if code >= 0 and self.enabled:
                event = ctypes.cast(ptr, ctypes.POINTER(Mouse)).contents
                self.mouse_event(event.flags, message, (event.pt.x, event.pt.y), event.extra)
            return user32.CallNextHookEx(None, code, message, ptr)

        def keyboard(code, message, ptr):
            if code >= 0:
                event = ctypes.cast(ptr, ctypes.POINTER(Key)).contents
                self.keyboard_event(event.flags, event.vk, message, event.extra)
            return user32.CallNextHookEx(None, code, message, ptr)

        callbacks = (HOOKPROC(mouse), HOOKPROC(keyboard))
        hooks = []
        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            module = kernel32.GetModuleHandleW(None)
            hooks = [user32.SetWindowsHookExW(kind, cb, module, 0)
                     for kind, cb in zip((14, 13), callbacks)]
            if not all(hooks):
                raise OSError("Could not install physical-input hooks; job will not run")
            self.rebase()
            self.ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = str(exc)
            self.control.cancel()
            self.ready.set()
        finally:
            for hook in hooks:
                if hook:
                    user32.UnhookWindowsHookEx(hook)
