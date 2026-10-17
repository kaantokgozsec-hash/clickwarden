"""
clickwarden.input — hardware-level Win32 input via SendInput.

Why not pyautogui: each pyautogui call carries interpreter overhead, its own pause
timer, and (with FAILSAFE) a corner polling loop. We instead build *batched*
INPUT records and hand them to SendInput in one syscall, then verify the return
count. A click becomes move+down+up in a single call (~1-2 ms), so input is never
the bottleneck.

Also provided:
  * ``corner_failsafe_watch`` — the pyautogui FAILSAFE replacement for a daemon:
    a 4 ms polling thread that trips when the cursor enters any screen corner.
  * easing-based pointer motion (``move_to(..., human=True)``) for natural paths.

Coordinates are absolute virtual-desktop pixels (DPI-aware; see win32core).
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

from .config import env_flag, env_float
from .win32core import user32, kernel32, virtual_screen
from .input_identity import CLICKWARDEN_EXTRA_INFO

# ---------------------------------------------------------------------------
# INPUT structures
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

WHEEL_DELTA = 120

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


SendInput = user32.SendInput
SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SendInput.restype = wintypes.UINT

_SCREEN_X, _SCREEN_Y, _SCREEN_W, _SCREEN_H = 0, 0, 0, 0
_dispatch_state = threading.local()


@contextmanager
def dispatch_guard(check, point_check=None):
    """Per-thread cancellation/takeover check at every input batch boundary."""
    previous = getattr(_dispatch_state, "check", None)
    previous_point = getattr(_dispatch_state, "point_check", None)
    _dispatch_state.check = check
    _dispatch_state.point_check = point_check
    try:
        yield
    finally:
        _dispatch_state.check = previous
        _dispatch_state.point_check = previous_point


def check_click_point(x, y):
    check = getattr(_dispatch_state, "point_check", None)
    if check:
        check(x, y)
    elif getattr(foreground_guard, "_hwnd", 0):
        from .targets import assert_unoccluded
        assert_unoccluded(x, y, foreground_guard._hwnd)


def _refresh_screen_metrics() -> None:
    """Measure the cursor coordinate space (must run AFTER DPI awareness is set).

    If this runs while the process is still DPI-virtualized, the screen reads as
    1536x864 on a 1920x1080 desktop and every injected coordinate is off by 25%.
    """
    global _SCREEN_X, _SCREEN_Y, _SCREEN_W, _SCREEN_H
    from .win32core import ensure_dpi_aware
    ensure_dpi_aware()
    _SCREEN_X, _SCREEN_Y, _SCREEN_W, _SCREEN_H = virtual_screen()
    if _SCREEN_W <= 0 or _SCREEN_H <= 0:
        _SCREEN_W, _SCREEN_H = 1920, 1080


_refresh_screen_metrics()

# Failsafe: if the framebuffer turns out to be larger than the cursor space
# (stale virtualization), scale clicks by the measured ratio.
_SCREEN_PHYS_W = 0
_SCREEN_PHYS_H = 0
_RATIO_X = 1.0
_RATIO_Y = 1.0
_CALIBRATED = True  # no-op until a physical size is supplied


def _raw_abs_xy(x: int, y: int) -> tuple[int, int]:
    """Map raw pixel coords (already in cursor space) to SendInput absolute space."""
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)):
        raise ValueError('Coordinates must be finite numbers')
    if not (_SCREEN_X <= x < _SCREEN_X + _SCREEN_W and _SCREEN_Y <= y < _SCREEN_Y + _SCREEN_H):
        raise ValueError('Point is outside the virtual desktop')
    nx = int(round((x - _SCREEN_X) * 65535 / max(_SCREEN_W - 1, 1)))
    ny = int(round((y - _SCREEN_Y) * 65535 / max(_SCREEN_H - 1, 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def set_physical_screen(width: int, height: int) -> None:
    """Tell the input layer the real capture (framebuffer) size, e.g. 1920x1080.

    If the cursor space is smaller (stale DPI virtualization), this arms the
    ratio failsafe so physical-space clicks are scaled into cursor space.
    """
    global _SCREEN_PHYS_W, _SCREEN_PHYS_H, _CALIBRATED, _RATIO_X, _RATIO_Y
    _SCREEN_PHYS_W, _SCREEN_PHYS_H = int(width), int(height)
    _RATIO_X = _RATIO_Y = 1.0
    _CALIBRATED = True
    if _SCREEN_PHYS_W > _SCREEN_W or _SCREEN_PHYS_H > _SCREEN_H:
        _RATIO_X = _SCREEN_PHYS_W / _SCREEN_W
        _RATIO_Y = _SCREEN_PHYS_H / _SCREEN_H
        _CALIBRATED = False


def calibrate(force: bool = False) -> dict:
    """Verify/measure the capture->cursor coordinate transform."""
    global _CALIBRATED
    if _CALIBRATED and not force:
        return {"calibrated": True, "ratio_x": round(_RATIO_X, 4),
                "ratio_y": round(_RATIO_Y, 4), "screen_space": (_SCREEN_W, _SCREEN_H)}
    _CALIBRATED = True
    return {"calibrated": True, "ratio_x": round(_RATIO_X, 4),
            "ratio_y": round(_RATIO_Y, 4), "screen_space": (_SCREEN_W, _SCREEN_H),
            "physical_space": (_SCREEN_PHYS_W, _SCREEN_PHYS_H)}


def _raw_move(x: int, y: int) -> None:
    """Inject a move with raw cursor-space coordinates (no scaling)."""
    nx, ny = _raw_abs_xy(x, y)
    _send(_mouse_input(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))


def _corrected_abs_xy(x: int, y: int) -> tuple[int, int]:
    """Scale physical-space coordinates into the cursor space, then normalize."""
    if _RATIO_X != 1.0 or _RATIO_Y != 1.0:
        x = int(round(x / _RATIO_X)) if _RATIO_X else x
        y = int(round(y / _RATIO_Y)) if _RATIO_Y else y
    return _raw_abs_xy(x, y)


def _mouse_input(dx: int, dy: int, data: int, flags: int) -> INPUT:
    if flags & MOUSEEVENTF_ABSOLUTE:
        flags |= MOUSEEVENTF_VIRTUALDESK
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data, flags, 0, 0))


def _send(*inputs: INPUT) -> int:
    # Key/button releases must still be delivered during cancellation.
    releasing = all(
        (e.type == INPUT_KEYBOARD and e.ki.dwFlags & KEYEVENTF_KEYUP)
        or (e.type == INPUT_MOUSE and e.mi.dwFlags in
            (MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP))
        for e in inputs
    )
    check = getattr(_dispatch_state, "check", None)
    if check and not releasing:
        check()
    if not releasing and getattr(foreground_guard, '_hwnd', 0):
        foreground_guard.check()
    n = len(inputs)
    for event in inputs:
        if event.type == INPUT_MOUSE:
            event.mi.dwExtraInfo = CLICKWARDEN_EXTRA_INFO
        elif event.type == INPUT_KEYBOARD:
            event.ki.dwExtraInfo = CLICKWARDEN_EXTRA_INFO
    arr = (INPUT * n)(*inputs)
    sent = SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:  # pragma: no cover - surface real Win32 failures
        error = kernel32.GetLastError()
        # A partial batch may stop between down and up. Release only keys/buttons
        # actually pressed by its delivered prefix, even if cancellation is set.
        held = {}
        for event in inputs[:sent]:
            if event.type == INPUT_KEYBOARD:
                key = ('key', event.ki.wVk, event.ki.wScan,
                       event.ki.dwFlags & ~KEYEVENTF_KEYUP)
                if event.ki.dwFlags & KEYEVENTF_KEYUP:
                    held.pop(key, None)
                else:
                    held[key] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                        event.ki.wVk, event.ki.wScan, event.ki.dwFlags | KEYEVENTF_KEYUP, 0, CLICKWARDEN_EXTRA_INFO))
            elif event.type == INPUT_MOUSE:
                for down, up in _BUTTON_FLAGS.values():
                    if event.mi.dwFlags & down:
                        held[('mouse', down)] = _mouse_input(0, 0, 0, up)
                    if event.mi.dwFlags & up:
                        held.pop(('mouse', down), None)
        cleanup = ''
        if held:
            try:
                _send(*reversed(list(held.values())))
            except OSError as exc:
                cleanup = f'; release also failed: {exc}'
        raise OSError(f"SendInput injected {sent}/{n} events (last error {error}){cleanup}")
    return sent


def mouse_move_abs(x: int, y: int) -> None:
    """Move the cursor to physical-screen (x, y), correcting for DPI virtualization."""
    failsafe.check()
    nx, ny = _corrected_abs_xy(x, y)
    _send(_mouse_input(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))


def move_to(x: int, y: int, human: bool = True, max_ms: float = 90.0) -> float:
    """Move the pointer to (x, y). Returns the elapsed ms.

    human=True interpolates a short eased path so motion looks organic (and
    hover states behave), capped by *max_ms* so latency stays bounded.
    """
    _refresh_screen_metrics()
    _corrected_abs_xy(x, y)  # validate before any movement
    t0 = time.perf_counter()
    cx, cy = cursor_xy()
    dx, dy = x - cx, y - cy
    dist = math.hypot(dx, dy)
    if not human or dist < 6:
        mouse_move_abs(x, y)
        return (time.perf_counter() - t0) * 1000

    # duration scales with distance, bounded to keep the action loop snappy
    duration = min(max_ms, max(25.0, dist * 0.12)) / 1000.0
    steps = max(3, int(duration * 550))
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 3  # ease-out cubic
        mouse_move_abs(int(cx + dx * e), int(cy + dy * e))
        remaining = t0 + duration * t - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
    mouse_move_abs(x, y)  # guarantee the exact landing (easing can end 1-3px short)
    return (time.perf_counter() - t0) * 1000


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left", clicks: int = 1,
          human: bool = True, settle_ms: float = 0.0) -> dict:
    """Move + click in one or two batched SendInput calls. Returns timing info."""
    if button not in _BUTTON_FLAGS:
        raise ValueError(f"unknown button '{button}'")
    if clicks not in (1, 2, 3):
        raise ValueError("clicks must be 1, 2, or 3")
    down, up = _BUTTON_FLAGS[button]

    t0 = time.perf_counter()
    check_click_point(x, y)
    move_ms = move_to(x, y, human=human)
    check_click_point(x, y)

    if clicks >= 2:
        ms = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        nx, ny = _corrected_abs_xy(x, y)
        events = []
        for _ in range(min(clicks, 3)):
            events.append(_mouse_input(nx, ny, 0, ms))
            events.append(_mouse_input(0, 0, 0, down))
            events.append(_mouse_input(0, 0, 0, up))
        _send(*events)
    else:
        _send(_mouse_input(0, 0, 0, down), _mouse_input(0, 0, 0, up))

    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)
    return {
        "x": x, "y": y, "button": button, "clicks": clicks,
        "move_ms": round(move_ms, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def drag(x1: int, y1: int, x2: int, y2: int, human: bool = True) -> dict:
    t0 = time.perf_counter()
    check_click_point(x1, y1)
    check_click_point(x2, y2)
    move_to(x1, y1, human=human)
    check_click_point(x1, y1)
    ms = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    cx, cy = x1, y1
    dist = math.hypot(x2 - x1, y2 - y1)
    steps = max(4, min(28, int(dist / 24)))
    try:
        _send(_mouse_input(0, 0, 0, MOUSEEVENTF_LEFTDOWN))
        started = time.perf_counter()
        duration = min(.25, max(.06, dist / 3000)) if human else .04
        for i in range(1, steps + 1):
            failsafe.check()
            t = i / steps
            px, py = int(cx + (x2 - cx) * t), int(cy + (y2 - cy) * t)
            check_click_point(px, py)
            nx, ny = _corrected_abs_xy(px, py)
            _send(_mouse_input(nx, ny, 0, ms))
            remaining = started + duration * t - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        check_click_point(x2, y2)
    finally:
        _send(_mouse_input(0, 0, 0, MOUSEEVENTF_LEFTUP))
    return {"from": [x1, y1], "to": [x2, y2],
            "total_ms": round((time.perf_counter() - t0) * 1000, 1)}


def scroll(clicks: int, x: int | None = None, y: int | None = None) -> dict:
    if type(clicks) is not int or not -1000 <= clicks <= 1000:
        raise ValueError('clicks must be an integer between -1000 and 1000')
    if (x is None) != (y is None):
        raise ValueError('Provide both x and y, or neither')
    if not clicks:
        return {'clicks': 0, 'at': None}
    target = (x, y) if x is not None else cursor_xy()
    check_click_point(*target)
    if x is not None and y is not None:
        move_to(x, y, human=False)
    check_click_point(*target)
    delta = int(clicks) * WHEEL_DELTA
    _send(_mouse_input(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL))
    return {"clicks": clicks, "at": [x, y] if x is not None else None}


def cursor_xy() -> tuple[int, int]:
    from .win32core import cursor_pos
    return cursor_pos()


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def _vk_input(vk: int, up: bool = False) -> INPUT:
    # Dedicated navigation keys (rather than their numeric-keypad counterparts).
    extended = vk in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                      0x2D, 0x2E, 0x2C, 0x5B, 0x5C, 0xA3, 0xA5}
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return INPUT(type=INPUT_KEYBOARD, ki=ki)


def _scan_input(scan: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    ki = KEYBDINPUT(0, scan, flags, 0, 0)
    return INPUT(type=INPUT_KEYBOARD, ki=ki)


VK = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL,
    "lctrl": 0xA2, "rctrl": 0xA3, "lshift": 0xA0, "rshift": 0xA1,
    "shift": VK_SHIFT,
    "alt": VK_MENU, "menu": VK_MENU,
    "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "comma": 0xBC, "period": 0xBE,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "pgup": 0x21, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "capslock": 0x14, "printscreen": 0x2C,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def key_down(key: str) -> None:
    _send(_vk_input(_resolve_vk(key)))


def key_up(key: str) -> None:
    _send(_vk_input(_resolve_vk(key), up=True))


def _resolve_vk(key: str) -> int:
    k = key.strip().lower()
    if k in VK:
        return VK[k]
    if len(k) == 1:
        res = user32.VkKeyScanW(ctypes.c_wchar(k))
        vk = res & 0xFF
        if vk == 0xFF:
            raise ValueError(f"no virtual key for '{key}'")
        return vk
    raise ValueError(f"unknown key '{key}'")


def validate_hotkey(keys: str | list[str]):
    """Resolve the whole chord without sending input; shared with batch preflight."""
    if isinstance(keys, str):
        keys = keys.split(',') if ',' in keys else keys.split('+') if len(keys) > 1 else [keys]
    if not isinstance(keys, (list, tuple)) or any(not isinstance(k, str) or not k.strip() for k in keys):
        raise ValueError('Provide a non-empty chord such as ctrl+a or ctrl,a')
    seq = [k.strip() for k in keys]
    if not seq:
        raise ValueError("no keys given")
    # Validate the whole chord before pressing any modifiers.
    vks = [_resolve_vk(k) for k in seq]
    if len(set(vks)) != len(vks):
        raise ValueError('Duplicate keys in chord')
    return seq, vks


def hotkey(keys: str | list[str]) -> dict:
    """Press a chord, accepting ctrl,shift,t or ctrl+shift+t."""
    seq, vks = validate_hotkey(keys)
    failsafe.check()
    pressed = []
    try:
        for vk in vks:
            pressed.append(vk)
            _send(_vk_input(vk))
    finally:
        failures = []
        for vk in reversed(pressed):
            try:
                _send(_vk_input(vk, up=True))
            except OSError as exc:
                failures.append(str(exc))
        if failures:
            raise OSError('Key release failed: ' + '; '.join(failures))
    return {"keys": seq}


def _input_wait(seconds):
    """Keep slow typing cancellable without waiting a whole character interval."""
    end = time.perf_counter() + seconds
    while True:
        check = getattr(_dispatch_state, 'check', None)
        if check:
            check()
        failsafe.check()
        foreground_guard.check()
        remaining = end - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(.02, remaining))


DEFAULT_TYPE_DELAY_MS = 20.0


def type_text(text: str, wpm: int = 0) -> dict:
    """Type unicode text via scan-coded Unicode events.

    wpm=0 types one character at a time with a short cancellable gap (20 ms by default; set
    CLICKWARDEN_TYPE_BATCH=1 for bounded one-syscall batches that never split surrogate pairs).
    Newlines use Enter; CRLF is one newline. wpm>0 sets the pace explicitly.
    """
    if not isinstance(text, str):
        raise ValueError('text must be a string')
    if type(wpm) is not int or wpm < 0:
        raise ValueError('wpm must be a non-negative integer')
    # Validate before input so malformed Unicode never leaves a partial string.
    text.encode('utf-16-le')
    t0 = time.perf_counter()
    if not text:
        return {"typed": 0, "elapsed_ms": 0.0}

    normalized = text.replace('\r\n', '\n').replace('\r', '\n')

    def character_events(ch):
        if ch == '\n':
            return [_vk_input(VK['enter']), _vk_input(VK['enter'], up=True)]
        raw = ch.encode('utf-16-le')
        events = []
        for i in range(0, len(raw), 2):
            code = int.from_bytes(raw[i:i + 2], "little")
            events.append(INPUT(type=INPUT_KEYBOARD,
                                ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
            events.append(INPUT(type=INPUT_KEYBOARD,
                                ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        return events

    if wpm == 0 and env_flag("TYPE_BATCH", False):
        events = []
        for ch in normalized:
            next_events = character_events(ch)
            if len(events) + len(next_events) > 1000:
                failsafe.check()
                foreground_guard.check()
                _send(*events)
                events = []
            events.extend(next_events)
        if events:
            failsafe.check()
            foreground_guard.check()
            _send(*events)
    else:
        # Default pace (wpm=0): modern apps such as the new Notepad read each Unicode key lazily and
        # drop or repeat characters when they arrive faster than ~20 ms apart (measured: bursts and
        # 1-3 ms gaps corrupt the text). CLICKWARDEN_TYPE_BATCH=1 restores one-syscall bursts for
        # apps that cope; CLICKWARDEN_TYPE_DELAY_MS tunes the gap.
        delay = 60.0 / (wpm * 5) if wpm else max(0.0, env_float("TYPE_DELAY_MS", DEFAULT_TYPE_DELAY_MS)) / 1000.0
        for index, ch in enumerate(normalized):
            failsafe.check()
            foreground_guard.check()
            _send(*character_events(ch))
            if index + 1 < len(normalized):
                _input_wait(delay)

    return {"typed": len(text), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


# ---------------------------------------------------------------------------
# Failsafe
# ---------------------------------------------------------------------------

class CornerAbort(RuntimeError):
    """Raised when the physical mouse enters a screen corner during automation."""


class FailsafeWatcher:
    """4 ms polling thread: trips when the cursor is slammed into any corner.

    Replaces pyautogui.FAILSAFE with a mechanism that works even though we no
    longer route input through pyautogui (and thus owned its corner check).
    """

    CORNER_PX = 4

    def __init__(self, poll_ms: float = 4.0):
        self.poll_interval = poll_ms / 1000.0
        self._stop = threading.Event()
        self._armed = threading.Event()
        self._tripped = threading.Event()
        self._thread: threading.Thread | None = None
        self._monitor = 0.0

    def start(self) -> "FailsafeWatcher":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="clickwarden-failsafe", daemon=True)
        self._thread.start()
        return self

    def arm(self) -> None:
        self._tripped.clear()
        self._armed.set()

    def disarm(self) -> None:
        self._armed.clear()

    def stop(self) -> None:
        self._stop.set()

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    def check(self) -> None:
        """Raise if the failsafe tripped since the last arm()."""
        if self._tripped.is_set():
            raise CornerAbort("Mouse entered a screen corner — automation aborted.")

    @staticmethod
    def _corners() -> tuple[tuple[int, int], ...]:
        vx, vy, vw, vh = virtual_screen()
        return ((vx, vy), (vx + vw - 1, vy),
                (vx, vy + vh - 1), (vx + vw - 1, vy + vh - 1))

    def _run(self) -> None:
        # The watcher lives for the whole process (an Engine is built at import),
        # so it must cost nothing while disarmed: block on the event instead of
        # waking 250x/second. Corners are re-read per arming so monitor layout
        # changes between actions are honoured.
        while not self._stop.is_set():
            if not self._armed.wait(0.25):
                continue
            corners = self._corners()
            while self._armed.is_set() and not self._stop.is_set():
                x, y = cursor_xy()
                if any(abs(x - cx) <= self.CORNER_PX and abs(y - cy) <= self.CORNER_PX
                       for cx, cy in corners):
                    self._tripped.set()
                    self._armed.clear()
                    break
                time.sleep(self.poll_interval)


class ForegroundGuardError(RuntimeError):
    """Raised when an action would inject input into the wrong application."""


class ForegroundGuard:
    """Refuse to inject input unless the expected window owns the foreground.

    This is the safety net against the classic desktop-agent catastrophe: a
    grounding runs while app A is focused, the action lands after focus moved to
    app B, and keystrokes (or clicks) go into the wrong application. Every
    destructive action passes through ``check()`` first.
    """

    def arm(self, hwnd: int, title: str = "") -> None:
        self._hwnd = int(hwnd) if hwnd else 0
        self._title = title

    def disarm(self) -> None:
        self._hwnd = 0
        self._title = ""

    def check(self, force: bool = False) -> dict:
        """Return the current foreground info; raise if it doesn't match."""
        from .win32core import foreground_hwnd, window_title

        fg = foreground_hwnd()
        info = {"hwnd": fg, "title": window_title(fg) if fg else ""}
        expected = getattr(self, "_hwnd", 0)
        if force or not expected:
            return info
        if fg == expected:
            return info
        raise ForegroundGuardError(
            f"foreground changed: expected '{self._title or expected}' (hwnd {expected}), "
            f"but '{info['title']}' (hwnd {fg}) is active — refusing to inject input"
        )


foreground_guard = ForegroundGuard()

failsafe = FailsafeWatcher()
