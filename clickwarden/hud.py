"""
clickwarden.hud — the Codex-style transparent overlay.

A WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW window that
covers the virtual desktop and paints:

  * a slow pulsing border in cyan (working) / amber (attention) / green (done),
  * an animated target reticle at the click point (expanding ripple),
  * a transient confirmation flash after the click lands.

Design notes for latency: the border mask is precomputed once in NumPy, so a
pulse frame is a single vectorized alpha modulation into the BGRA buffer handed
straight to ``UpdateLayeredWindow`` — no GDI text/geometry each frame. Only the
thin edge band is recomputed while the border pulses (the interior is transparent),
the pulse runs at ~30 fps and only ripples/pointer motion use ~60 fps. The timer
stops itself when idle, so the overlay costs 0% CPU when the agent is off.
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .win32core import (
    user32, WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT,
    virtual_screen, WS_EX_NOACTIVATE,
)

gdi32 = ctypes.windll.gdi32

# typed bindings — HWNDs/DCs must go through c_void_p or 64-bit values overflow
_UpdateLayeredWindow = user32.UpdateLayeredWindow
_UpdateLayeredWindow.restype = wintypes.BOOL
_UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
]

_CreateCompatibleDC = gdi32.CreateCompatibleDC
_CreateCompatibleDC.restype = ctypes.c_void_p
_CreateCompatibleDC.argtypes = [ctypes.c_void_p]

_CreateDIBSection = gdi32.CreateDIBSection
_CreateDIBSection.restype = ctypes.c_void_p
_CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]

_SelectObject = gdi32.SelectObject
_SelectObject.restype = ctypes.c_void_p
_SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]

# HWND-taking calls MUST be typed or 64-bit handles get truncated to 32-bit ints
# (this is what silently broke SetTimer -> no WM_TIMER -> overlay never painted).
_SetTimer = user32.SetTimer
_SetTimer.restype = ctypes.c_size_t
_SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]

_KillTimer = user32.KillTimer
_KillTimer.restype = wintypes.BOOL
_KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]

_DestroyWindow = user32.DestroyWindow
_DestroyWindow.restype = wintypes.BOOL
_DestroyWindow.argtypes = [ctypes.c_void_p]

_ShowWindow = user32.ShowWindow
_ShowWindow.restype = wintypes.BOOL
_ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

_IsWindowVisible = user32.IsWindowVisible
_IsWindowVisible.restype = wintypes.BOOL
_IsWindowVisible.argtypes = [ctypes.c_void_p]

_GetMessageW = user32.GetMessageW
_GetMessageW.restype = ctypes.c_int
_GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]

_PostQuitMessage = user32.PostQuitMessage
_PostQuitMessage.restype = None
_PostQuitMessage.argtypes = [ctypes.c_int]

WM_CLOSE = 0x0010

_PostMessageW = user32.PostMessageW
_PostMessageW.restype = wintypes.BOOL
_PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]

_SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
_SetWindowDisplayAffinity.restype = wintypes.BOOL
_SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wintypes.DWORD]

# UpdateLayeredWindow plumbing
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x00000011

BI_RGB = 0
DIB_RGB_COLORS = 0

# palette (BGRA)
CYAN = (255, 214, 10)      # (B, G, R)
AMBER = (0, 176, 255)
GREEN = (110, 255, 60)
RED = (70, 70, 255)


@dataclass
class Color:
    b: int
    g: int
    r: int


COLORS = {"cyan": CYAN, "amber": AMBER, "green": GREEN, "red": RED}


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _to_cfunc(callable_obj):
    """Convert a Python callable to a WNDPROC-compatible pointer.

    Uses LONG_PTR (c_ssize_t) so HWND/WPARAM/LPARAM values pass through without
    overflow on 64-bit Windows.
    """
    return ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT,
        ctypes.c_size_t, ctypes.c_ssize_t,
    )(callable_obj)


class _State:
    """Renderable state shared between engine threads and the HUD thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.visible = False
        self.linger_until = 0.0
        self.color = "cyan"
        self.border = True
        self.reticle_pos: tuple[int, int] | None = None
        self.reticle_started = 0.0
        self.flash_pos: tuple[int, int] | None = None
        self.flash_color = "green"
        self.flash_started = 0.0
        self.bump = 0  # animation generation counter
        self.status: str = "clickwarden: active"
        self.pointer_from = None
        self.pointer_to = None
        self.pointer_started = 0.0


class Hud:
    """Singleton overlay. All methods are safe to call from any thread."""

    REFRESH_MS = 16  # ~60 fps while a ripple/flash/pointer animates
    PULSE_REFRESH_MS = 33  # ~30 fps is plenty for the slow border pulse
    IDLE_REFRESH_MS = 250
    RIPPLE_MS = 300.0
    FLASH_MS = 420.0
    BORDER_PX = 3

    def __init__(self, excluded_from_capture: bool = True):
        self._state = _State()
        self.excluded_from_capture = excluded_from_capture
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = 0
        self._shown = False
        self._bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._enabled = True
        self._last_anim_ms = 0.0
        self._tick_ms = self.REFRESH_MS
        self._painted = False
        self._border_strips: list = []
        self._dirty: list = []  # (rows, cols) slices painted outside the edge band

    # -- public API --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> "Hud":
        if self._thread and self._thread.is_alive():
            return self
        self._enabled = True
        self._ready.clear()
        self._shown = False
        self._thread = threading.Thread(target=self._run, name="clickwarden-hud", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self

    def shutdown(self) -> None:
        """Stop the overlay thread and destroy the window."""
        self._enabled = False
        hwnd = self._hwnd
        if hwnd:
            try:
                _PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def begin(self, color: str = "cyan", status: str = "clickwarden: working") -> None:
        with self._state.lock:
            self._state.visible = True
            self._state.border = True
            self._state.color = color
            self._state.status = status
            self._state.linger_until = time.perf_counter() + 3600.0
            self._state.bump += 1

    def end(self, color: str = "green", status: str = "clickwarden: done",
            linger_ms: float = 450.0) -> None:
        with self._state.lock:
            self._state.color = color
            self._state.status = status
            self._state.visible = True
            self._state.linger_until = time.perf_counter() + linger_ms / 1000.0
            self._state.bump += 1

    def reticle(self, x: int, y: int, color: str | None = None) -> None:
        with self._state.lock:
            now = time.perf_counter()
            self._state.pointer_from = self._pointer_position(now) or (x, y)
            self._state.pointer_to = (x, y)
            self._state.pointer_started = now
            self._state.reticle_pos = (x, y)
            self._state.reticle_started = time.perf_counter()
            if color:
                self._state.color = color
            self._state.bump += 1

    def notice(self, status: str, color: str = "cyan") -> None:
        with self._state.lock:
            self._state.status = status
            self._state.color = color
            self._state.linger_until = time.perf_counter() + 3600

    def _pointer_position(self, now: float):
        st = self._state
        if st.pointer_to is None:
            return None
        start = st.pointer_from or st.pointer_to
        t = min(1.0, max(0.0, (now - st.pointer_started) / 0.06))
        ease = 1 - (1 - t) ** 3
        return tuple(round(a + (b - a) * ease) for a, b in zip(start, st.pointer_to))

    def flash(self, x: int, y: int, color: str = "green") -> None:
        with self._state.lock:
            self._state.flash_pos = (x, y)
            self._state.flash_color = color
            self._state.flash_started = time.perf_counter()
            self._state.bump += 1

    # -- thread body -------------------------------------------------------

    def _run(self) -> None:
        from .win32core import attach_desktop
        attach_desktop()
        try:
            self._create_window()
        except Exception as exc:  # pragma: no cover - HUD is best-effort
            import sys
            sys.stderr.write(f"[clickwarden] HUD disabled ({exc})\n")
            self._enabled = False
            self._ready.set()
            return

        # ensure the window never appears in our own screenshots
        if self.excluded_from_capture:
            try:
                _SetWindowDisplayAffinity(self._hwnd, WDA_EXCLUDEFROMCAPTURE)
            except Exception:
                pass

        self._ready.set()

        # Paint and show immediately (do not wait for the first WM_TIMER): the
        # reticle must be available the instant an action starts, and a stuck
        # timer can never leave the overlay silently invisible again.
        try:
            self._present()
        except Exception:
            pass

        _SetTimer(self._hwnd, 1, self.IDLE_REFRESH_MS, None)
        self._tick_ms = self.IDLE_REFRESH_MS

        _DispatchMessage = user32.DispatchMessageW
        _DispatchMessage.restype = ctypes.c_ssize_t
        _DispatchMessage.argtypes = [ctypes.c_void_p]

        msg = wintypes.MSG()
        while self._enabled:
            got = _GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got <= 0:
                break
            _DispatchMessage(ctypes.byref(msg))

        _KillTimer(self._hwnd, 1)
        if self._hwnd:
            _DestroyWindow(self._hwnd)

    def _create_window(self) -> None:
        from ctypes import wintypes as wt

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._wndproc = _to_cfunc(self._wndproc_impl)

        class_name = f"ClickwardenHud_{id(self)}_{time.time_ns()}"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        wc_struct = WNDCLASSW(
            0, ctypes.cast(self._wndproc, ctypes.c_void_p), 0, 0,
            ctypes.c_void_p(hinst), None, None, None, None, class_name,
        )
        register = user32.RegisterClassW
        register.restype = wt.ATOM
        register.argtypes = [ctypes.POINTER(WNDCLASSW)]
        register(ctypes.byref(wc_struct))  # fine if already registered

        CreateWindowExW = user32.CreateWindowExW
        CreateWindowExW.restype = wintypes.HWND
        CreateWindowExW.argtypes = [
            ctypes.c_ulong, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]

        vx, vy, vw, vh = virtual_screen()
        self._bounds = (vx, vy, vw, vh)
        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        WS_POPUP = 0x80000000
        self._hwnd = CreateWindowExW(
            ctypes.c_ulong(ex_style), class_name, "clickwarden-hud",
            ctypes.c_ulong(WS_POPUP),
            vx, vy, vw, vh, None, None, ctypes.c_void_p(hinst), None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed for HUD overlay "
                          f"(last error {ctypes.windll.kernel32.GetLastError()})")

        self._init_buffers(vw, vh)

    def _init_buffers(self, w: int, h: int) -> None:
        # DIB section we render into
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        ppv = ctypes.c_void_p()
        screen_dc = user32.GetDC(None)
        mem_dc = _CreateCompatibleDC(screen_dc)
        hbitmap = _CreateDIBSection(mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                    ctypes.byref(ppv), ctypes.c_void_p(None), 0)
        user32.ReleaseDC(None, screen_dc)
        if not hbitmap or not ppv.value:
            raise OSError("CreateDIBSection failed")
        _SelectObject(mem_dc, hbitmap)

        self._mem_dc = mem_dc
        self._hbitmap = hbitmap
        self._w, self._h = w, h
        self._buf_addr = ppv.value
        self._buf = np.ctypeslib.as_array(
            (ctypes.c_uint8 * (w * h * 4)).from_address(ppv.value)
        ).reshape(h, w, 4)

        self._build_border(w, h)

        # --- reticle kernels ----------------------------------------------
        self._reticle_dot = self._make_disc(2.6, 1.0)
        # Compact arrow; its tip is the exact input point.
        cursor = Image.new("L", (16, 22))
        ImageDraw.Draw(cursor).polygon([(0, 0), (0, 16), (4, 12), (9, 21),
                                      (12, 19), (8, 11), (15, 11)], fill=255)
        self._cursor_mask = np.asarray(cursor, dtype=np.float32) / 255
        self._banner_key = None
        self._banner = None

    def _build_border(self, w: int, h: int) -> None:
        """Precompute the border glow, keeping only the edge band that is nonzero.

        The full-desktop mask is zero beyond ~2.2x the border width, so per-frame
        work is limited to four thin strips instead of every desktop pixel
        (~2M pixels x 4 channels at 1080p, ~8M at 4K) — that full-frame pass
        competed with capture/encode for CPU and the GIL during every action.
        """
        border = max(3, int(self.BORDER_PX * (1 + w / 1920)))
        xs = np.arange(w, dtype=np.float32)[None, :]
        ys = np.arange(h, dtype=np.float32)[:, None]
        d_edge = np.minimum(np.minimum(xs, (w - 1) - xs), np.minimum(ys, (h - 1) - ys))
        alpha = np.clip((border * 2 - d_edge) / (border * 2), 0.0, 1.0) ** 2.2
        # keep the very outer line crisp: full alpha within first 2 px
        alpha = np.where(d_edge < 2, 1.0, alpha) * (d_edge < border * 2.2)
        band = max(1, min(h // 2, w // 2, int(math.ceil(border * 2.2)) + 1))
        regions = ((slice(0, band), slice(0, w)), (slice(h - band, h), slice(0, w)),
                   (slice(band, h - band), slice(0, band)),
                   (slice(band, h - band), slice(w - band, w)))
        self._border_strips = [(rows, cols, alpha[rows, cols].copy()) for rows, cols in regions]
        self._dirty = []

    @staticmethod
    def _make_disc(radius_px: float, soft: float) -> np.ndarray:
        n = int(radius_px * 2 + 4)
        yy, xx = np.mgrid[0:n, 0:n]
        c = (n - 1) / 2.0
        d = np.hypot(xx - c, yy - c)
        return np.clip((radius_px - d) / max(soft * radius_px, 0.8), 0.0, 1.0).astype(np.float32)

    # -- window proc -------------------------------------------------------

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        if msg == 0x0021:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE: never take app focus
        if msg == 0x0084:  # WM_NCHITTEST
            return -1  # HTTRANSPARENT
        WM_TIMER = 0x0113
        WM_DESTROY = 0x0002
        WM_CLOSE = 0x0010
        if msg == WM_TIMER:
            self._render()
            return 0
        if msg == WM_CLOSE:
            _DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- rendering ---------------------------------------------------------

    def _render(self) -> None:
        st = self._state
        now = time.perf_counter()

        with st.lock:
            color = COLORS.get(st.color, CYAN)
            border_on = st.visible and now < st.linger_until
            reticle = st.reticle_pos if st.reticle_pos and \
                (now - st.reticle_started) < self.RIPPLE_MS / 1000 else None
            ripple_age = now - st.reticle_started
            flash = st.flash_pos if st.flash_pos and \
                (now - st.flash_started) < self.FLASH_MS / 1000 else None
            flash_age = now - st.flash_started
            pointer = self._pointer_position(now) if border_on and now-st.pointer_started < .75 else None
            status = st.status

        if not border_on and reticle is None and flash is None:
            if self._painted:
                self._buf.fill(0)
                self._dirty = []
                self._present()
                self._painted = False
            self._set_tick(self.IDLE_REFRESH_MS)
            return  # nothing to paint — collapse to near-zero cost

        animating = reticle is not None or flash is not None or pointer is not None
        self._set_tick(self.REFRESH_MS if animating else self.PULSE_REFRESH_MS)
        self._paint_frame(self._buf, border_on, now, color, reticle, ripple_age,
                          flash, flash_age, pointer, status)
        self._present()
        self._painted = True

    def _set_tick(self, ms: int) -> None:
        if self._tick_ms != ms:
            self._tick_ms = ms
            _SetTimer(self._hwnd, 1, ms, None)

    def _paint_frame(self, buf, border_on, now, color, reticle, ripple_age,
                     flash, flash_age, pointer, status) -> None:
        if border_on:
            pulse = 0.5 + 0.5 * np.sin(now * 2.6)          # 0..1, ~2.4 rad/s
            border_a = (0.28 + 0.30 * pulse)
            # Erase last frame's reticle/pointer/flash, then refresh the edge band.
            # Everything else is already transparent, so no full-desktop pass.
            for rows, cols in self._dirty:
                buf[rows, cols] = 0
            self._dirty = []
            for rows, cols, alpha in self._border_strips:
                a = alpha * border_a
                region = buf[rows, cols]
                region[:, :, 0] = (a * color[0]).astype(np.uint8)
                region[:, :, 1] = (a * color[1]).astype(np.uint8)
                region[:, :, 2] = (a * color[2]).astype(np.uint8)
                region[:, :, 3] = (a * 255).astype(np.uint8)
        else:
            buf.fill(0)
            self._dirty = []

        if reticle is not None:
            self._paint_reticle(buf, reticle, ripple_age, color)
        if flash is not None:
            self._paint_flash(buf, flash, flash_age, COLORS.get(self._state.flash_color, GREEN))
        if pointer:
            x, y = pointer[0] - self._bounds[0], pointer[1] - self._bounds[1]
            self._blit(buf, self._cursor_mask, (x + 1, y + 1), (10, 15, 20))
            self._blit(buf, self._cursor_mask, (x, y), color)
        if border_on:
            self._paint_notice(buf, status, color)

    def _paint_notice(self, buf, status, color):
        key = (status, color)
        if self._banner_key != key:
            banner = Image.new("RGBA", (500, 46), (0, 0, 0, 0))
            draw = ImageDraw.Draw(banner)
            draw.rounded_rectangle((0, 0, 499, 45), 14, fill=(15, 22, 32, 245),
                                   outline=(color[2], color[1], color[0], 255), width=2)
            try:
                font = ImageFont.truetype("segoeui.ttf", 15)
            except OSError:
                font = ImageFont.load_default()
            draw.text((16, 12), status[:65], font=font, fill=(240, 245, 250, 255))
            rgba = np.array(banner, dtype=np.uint16)
            bgra = rgba[:, :, [2, 1, 0, 3]]
            bgra[:, :, :3] = bgra[:, :, :3] * bgra[:, :, 3:4] // 255
            self._banner = bgra.astype(np.uint8)
            self._banner_key = key
        width = min(500, self._w - 20)
        buf[12:58, 10:10 + width] = self._banner[:, :width]

    def _paint_reticle(self, buf, pos, age_s: float, color) -> None:
        x, y = pos
        ox, oy = self._bounds[0], self._bounds[1]
        cx, cy = x - ox, y - oy

        # expanding ripple ring
        t = min(age_s / (self.RIPPLE_MS / 1000.0), 1.0)
        radius = 4 + 8 * (1 - (1 - t) ** 2)
        width = 1.0
        fade = (1 - t) ** 1.5

        n = int(radius + width + 4) * 2
        yy, xx = np.mgrid[0:n, 0:n]
        c = (n - 1) / 2.0
        d = np.hypot(xx - c, yy - c)
        ring = np.clip((width - np.abs(d - radius)) / width, 0.0, 1.0).astype(np.float32) * fade
        self._blit(buf, ring, (cx - c, cy - c), color)

        # solid center dot (fades slower)
        dot_a = max(0.0, 1.0 - t * 0.55)
        self._blit(buf, self._reticle_dot * dot_a, (cx - 3, cy - 3), color)

    def _paint_flash(self, buf, pos, age_s: float, color) -> None:
        t = min(age_s / (self.FLASH_MS / 1000.0), 1.0)
        a = (1 - t) ** 1.2
        x, y = pos
        ox, oy = self._bounds[0], self._bounds[1]
        self._blit(buf, self._reticle_dot * a * 1.4, (x - ox - 3, y - oy - 3), color)

    def _blit(self, buf, mask: np.ndarray, top_left: tuple[float, float], color) -> None:
        h, w = buf.shape[:2]
        x0, y0 = int(round(top_left[0])), int(round(top_left[1]))
        mh, mw = mask.shape
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        cw = min(mw - sx0, w - dx0)
        ch = min(mh - sy0, h - dy0)
        if cw <= 0 or ch <= 0:
            return
        m = np.clip(mask[sy0:sy0 + ch, sx0:sx0 + cw], 0, 1)
        region = buf[dy0:dy0 + ch, dx0:dx0 + cw].astype(np.int32)
        alpha = (m * 255).astype(np.int32)[:, :, None]
        src = np.array(color, dtype=np.int32).reshape(1, 1, 3)
        inv = (255 - alpha)
        rgb = (region[:, :, :3] * inv + src * alpha) // 255
        buf[dy0:dy0 + ch, dx0:dx0 + cw, :3] = rgb.astype(np.uint8)
        a_old = region[:, :, 3]
        a_new = a_old + ((alpha[:, :, 0] * (255 - a_old)) // 255)
        buf[dy0:dy0 + ch, dx0:dx0 + cw, 3] = a_new.astype(np.uint8)
        self._dirty.append((slice(dy0, dy0 + ch), slice(dx0, dx0 + cw)))

    def _present(self) -> None:
        x, y = self._bounds[0], self._bounds[1]
        size = wintypes.SIZE()
        size.cx, size.cy = self._w, self._h
        src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        _UpdateLayeredWindow(
            self._hwnd, None, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(size),
            self._mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
        )
        if not self._shown:
            _ShowWindow(self._hwnd, 4)  # SW_SHOWNOACTIVATE
            self._shown = True


# module-level handle
hud = Hud()
