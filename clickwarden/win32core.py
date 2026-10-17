"""
clickwarden.win32core — Win32 session, DPI and virtual-screen substrate.

Every other module builds on this. The two critical Windows nuances handled here:

1. *Desktop session isolation*: background threads spawned by MCP runners can land
   on a non-interactive window station, where GetForegroundWindow / SendInput fail
   silently. ``attach_desktop()`` binds the calling thread to WinSta0\\Default.

2. *DPI awareness*: if the process is DPI-virtualized, every coordinate we capture
   or click is scaled, producing systematic misclicks. ``ensure_dpi_aware()``
   promotes the process to per-monitor-v2 awareness at startup (best effort).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from .input_identity import CLICKWARDEN_EXTRA_INFO

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ctypes defaults to 32-bit integers; handles and LPARAM are pointer-sized.
for _name, _args, _result in (
    ("GetForegroundWindow", [], wintypes.HWND),
    ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
    ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
    ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    ("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
    ("IsIconic", [wintypes.HWND], wintypes.BOOL),
    ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
    ("BringWindowToTop", [wintypes.HWND], wintypes.BOOL),
    ("OpenDesktopW", [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
    ("SetThreadDesktop", [wintypes.HANDLE], wintypes.BOOL),
    ("CloseDesktop", [wintypes.HANDLE], wintypes.BOOL),
    ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
    ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
):
    _fn = getattr(user32, _name)
    _fn.argtypes, _fn.restype = _args, _result

# --- window styles used by the HUD -----------------------------------------
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def is_control_surface(window: dict) -> bool:
    return window.get('title', '').casefold() in ('clickwarden-hud', 'clickwarden — desktop controls')

DESKTOP_ALL_ACCESS = 0x01FF


# ---------------------------------------------------------------------------
# Session / DPI setup
# ---------------------------------------------------------------------------

def attach_desktop() -> bool:
    """Bind the calling thread to the interactive WinSta0\\Default desktop.

    Safe to call repeatedly and from any thread. Returns True on success.
    """
    try:
        hdesk = user32.OpenDesktopW("Default", 0, False, DESKTOP_ALL_ACCESS)
        if hdesk:
            if user32.SetThreadDesktop(hdesk):
                # Keep the handle while the calling thread is using this desktop.
                return True
            user32.CloseDesktop(hdesk)
    except Exception as exc:  # pragma: no cover - defensive
        sys.stderr.write(f"[clickwarden] attach_desktop warning: {exc}\n")
    return False


def ensure_dpi_aware() -> str:
    """Best-effort per-monitor-v2 DPI awareness. Returns the API used."""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


# ---------------------------------------------------------------------------
# Virtual screen geometry (physical pixels)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Monitor:
    index: int          # mss monitor index (1-based) or 0 for the full virtual screen
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width, self.top + self.height)


def virtual_screen() -> tuple[int, int, int, int]:
    """Physical bounds of the full virtual desktop: (left, top, width, height)."""
    x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if w == 0 or h == 0:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
    return x, y, w, h


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def is_key_down(vk: int) -> bool:
    """True while *vk* is physically held (GetAsyncKeyState high bit)."""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


is_key_toggled = is_key_down  # backward-compatible alias; this never checked toggle state


# ---------------------------------------------------------------------------
# Windows / foreground app
# ---------------------------------------------------------------------------

FOREGROUND_PID = wintypes.DWORD()


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


_DWMWA_CLOAKED = 14
_dwm_get_attribute = None


def is_cloaked(hwnd: int) -> bool:
    """True for DWM-cloaked windows: visible by flag but not actually on screen.

    Suspended UWP apps, windows on other virtual desktops and several shell
    hosts report IsWindowVisible=True while being cloaked. Treating them as real
    windows lists phantom targets and paints mask boxes over the real target.
    """
    global _dwm_get_attribute
    try:
        if _dwm_get_attribute is None:
            fn = ctypes.windll.dwmapi.DwmGetWindowAttribute
            fn.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            fn.restype = ctypes.c_long
            _dwm_get_attribute = fn
        value = wintypes.DWORD()
        if _dwm_get_attribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            return False
        return bool(value.value)
    except (OSError, AttributeError):
        return False


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client-area-free window bounds: (left, top, right, bottom)."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def client_center(hwnd: int) -> tuple[int, int] | None:
    """Screen point at the centre of the window's client area, or None if empty."""
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right <= 0 or rect.bottom <= 0:
        return None
    pt = wintypes.POINT(rect.right // 2, rect.bottom // 2)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def enumerate_windows(only_visible: bool = True) -> list[dict]:
    """Top-level windows with titles and bounds (clamped to the virtual screen)."""
    out: list[dict] = []
    vx, vy, vw, vh = virtual_screen()
    vrect = (vx, vy, vx + vw, vy + vh)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if only_visible and (not user32.IsWindowVisible(hwnd) or is_cloaked(hwnd)):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        l, t, r, b = window_rect(hwnd)
        w, h = r - l, b - t
        if w <= 4 or h <= 4:
            return True
        # clamp to virtual screen so we never crop outside capture bounds
        cl, ct = max(l, vrect[0]), max(t, vrect[1])
        cr, cb = min(r, vrect[2]), min(b, vrect[3])
        if cr - cl <= 4 or cb - ct <= 4:
            return True
        out.append({
            "hwnd": int(hwnd),
            "title": title,
            "bounds": {"x": cl, "y": ct, "width": cr - cl, "height": cb - ct},
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def pid_of_window(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def wait_for_window(pid: int | None = None, exclude_hwnd: int = 0,
                    timeout_s: float = 3.0, poll_ms: float = 60.0) -> dict | None:
    """Wait for a new visible window to appear (e.g. a save/open dialog).

    Returns the longest-titled window not in *exclude_hwnd* that belongs to
    *pid*, or None on timeout. This is what makes dialog-driven flows reliable:
    the next grounding is then guaranteed to target the dialog instead of a
    stale frame captured before it opened.
    """
    import time as _time

    deadline = _time.perf_counter() + timeout_s
    while _time.perf_counter() < deadline:
        candidates = [
            w for w in enumerate_windows()
            if w["hwnd"] != exclude_hwnd
            and w["title"].lower() not in ("program manager", "windows input experience")
            and (pid is None or pid_of_window(w["hwnd"]) == pid)
        ]
        if candidates:
            # dialogs carry descriptive titles; pick the longest
            return max(candidates, key=lambda w: len(w["title"]))
        _time.sleep(poll_ms / 1000.0)
    return None


def focus_window(title_keyword: str, settle_ms: float = 40.0) -> dict:
    """Bring a window to the foreground *reliably* and verify it happened.

    Plain ``SetForegroundWindow`` is refused by Windows when the caller is not
    the foreground process (focus-stealing protection), which silently sends
    the next click/keystroke to the wrong application — the most dangerous
    failure mode in desktop automation. The AttachThreadInput dance temporarily
    merges our input queue with the foreground thread's, after which the swap
    is permitted; we then verify with GetForegroundWindow and report honestly.
    """
    import time as _time

    keyword = title_keyword.lower()

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    match = {"hwnd": 0, "title": ""}

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or is_cloaked(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if keyword in title.lower():
            match["hwnd"] = int(hwnd)
            match["title"] = title
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not match["hwnd"]:
        return {"status": "error", "message": f"No visible window matching '{title_keyword}'"}

    return force_foreground(match["hwnd"], title=match["title"], settle_ms=settle_ms)


def force_foreground(hwnd: int, title: str = "", settle_ms: float = 40.0,
                     attempts: int = 4) -> dict:
    """SetForegroundWindow that survives real-world Windows foreground locks.

    Two hostile conditions exist in the wild:
      * focus-stealing protection (calling process != foreground process), and
      * invisible helper windows (e.g. MSI's ``GameInputServiceWindow``) that
        silently own the foreground, so a naive SetForegroundWindow fails with
        "access denied" and every keystroke disappears into the void.

    We fight both: AttachThreadInput merges input queues where permitted, and a
    synthetic ALT is the documented gesture that clears the foreground lock.
    """
    import time as _time

    hwnd = int(hwnd)
    last_error = ""

    alt = 0x12  # VK_MENU
    keyup = 0x0002

    for attempt in range(max(1, attempts)):
        if foreground_hwnd() == hwnd:
            return {"status": "success", "focused": title or window_title(hwnd),
                    "hwnd": hwnd, "already": True, "attempts": attempt + 1}

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _time.sleep(0.12)

        fg = foreground_hwnd()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        our_thread = kernel32.GetCurrentThreadId()

        attached = []
        try:
            for other in {fg_thread, our_thread} - {target_thread}:
                if other:
                    if user32.AttachThreadInput(target_thread, other, True):
                        attached.append(other)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            if foreground_hwnd() != hwnd:
                # gesture unlock: an ALT press is what Windows accepts as
                # "the user is interacting", defeating the foreground lock
                user32.keybd_event(alt, 0, 0, CLICKWARDEN_EXTRA_INFO)
                user32.keybd_event(alt, 0, keyup, CLICKWARDEN_EXTRA_INFO)
                _time.sleep(0.02)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
        finally:
            for other in attached:
                try:
                    user32.AttachThreadInput(target_thread, other, False)
                except Exception:
                    pass

        _time.sleep(settle_ms / 1000.0)
        if foreground_hwnd() == hwnd:
            return {"status": "success", "focused": title or window_title(hwnd),
                    "hwnd": hwnd, "verified": True, "attempts": attempt + 1}
        last_error = last_error or "another process holds the foreground lock"
        _time.sleep(0.08)

    return {
        "status": "error",
        "focused": title or window_title(hwnd),
        "hwnd": hwnd,
        "verified": False,
        "message": f"Windows refused the focus swap after {attempts} attempts "
                   f"({last_error})",
    }


def foreground_info() -> dict:
    hwnd = foreground_hwnd()
    return {"hwnd": hwnd, "title": window_title(hwnd) if hwnd else "",
            "class": window_class(hwnd) if hwnd else ""}
