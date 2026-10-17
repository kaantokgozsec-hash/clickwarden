"""
clickwarden.frames — the optimized frame pipeline.

Blueprint §3 in code. A desktop grab plus encode costs ~28 ms end-to-end when
kept in memory; the old PNG-to-disk path cost ~85 ms and produced 1.15 MB files.

Output is always a ``Frame``: JPEG bytes plus the exact screen-space mapping of
what the VLM saw, so any returned (x, y) can be converted to a physical click
with one linear transform — no guesswork, no subpixel drift.

Three modes (matching the blueprint):
  * precision — native resolution, JPEG Q85 (default; exact 1:1 pixels)
  * turbo     — scaled to <=1280px wide, JPEG Q80 (macro navigation)
  * roi       — only a region of the screen, native density (foveated)
"""

from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass, field

import mss
import numpy as np
from PIL import Image

from .config import env_int
from .win32core import virtual_screen

DEFAULT_QUALITY = env_int("JPEG_QUALITY", 85)
TURBO_QUALITY = 80
TURBO_WIDTH = env_int("TURBO_WIDTH", 1280)

_tls = threading.local()


def _mss() -> mss.MSS:
    """Thread-local MSS instance (MSS is not safe to share across threads)."""
    inst = getattr(_tls, "mss", None)
    if inst is None:
        inst = mss.MSS()
        _tls.mss = inst
    return inst


def _grab(left: int, top: int, width: int, height: int) -> Image.Image:
    region = {"left": left, "top": top, "width": width, "height": height}
    shot = _mss().grab(region)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    jpeg: bytes
    left: int
    top: int
    width: int      # pixel width of the JPEG (what the model sees)
    height: int
    screen_w: int   # native pixel width of the captured region
    screen_h: int
    mode: str
    quality: int
    grab_ms: float = 0.0
    encode_ms: float = 0.0
    label: str = ""
    fg_hwnd: int = 0        # foreground window when the frame was taken (focus guard)
    fg_title: str = ""
    _thumb: np.ndarray | None = field(default=None, repr=False)

    @property
    def scale_x(self) -> float:
        return self.screen_w / self.width if self.width else 1.0

    @property
    def scale_y(self) -> float:
        return self.screen_h / self.height if self.height else 1.0

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Map a model-space coordinate to a physical screen coordinate."""
        sx = int(round(self.left + x * self.scale_x))
        sy = int(round(self.top + y * self.scale_y))
        return sx, sy

    def clip(self, x: float, y: float) -> tuple[int, int]:
        """Map + clamp to the captured region so we can never click garbage."""
        sx, sy = self.to_screen(x, y)
        return (
            max(self.left, min(self.left + self.screen_w - 1, sx)),
            max(self.top, min(self.top + self.screen_h - 1, sy)),
        )

    @property
    def total_ms(self) -> float:
        return round(self.grab_ms + self.encode_ms, 1)

    def meta(self) -> dict:
        return {
            "mode": self.mode,
            "left": self.left, "top": self.top,
            "width": self.width, "height": self.height,
            "native_width": self.screen_w, "native_height": self.screen_h,
            "jpeg_kb": round(len(self.jpeg) / 1024, 1),
            "quality": self.quality,
            "grab_ms": round(self.grab_ms, 1),
            "encode_ms": round(self.encode_ms, 1),
            "label": self.label,
            "foreground": {"hwnd": self.fg_hwnd, "title": self.fg_title},
        }

    def thumbnail(self, width: int = 320) -> np.ndarray:
        """Small grayscale array for change detection / stabilization checks."""
        if self._thumb is None:
            img = Image.open(io.BytesIO(self.jpeg))
            h = max(2, int(img.height * width / img.width))
            self._thumb = np.asarray(img.convert("L").resize((width, h), Image.BILINEAR),
                                     dtype=np.int16)
        return self._thumb


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def encode_jpeg(img: Image.Image, quality: int = DEFAULT_QUALITY,
                optimize: bool = False) -> tuple[bytes, float]:
    buf = io.BytesIO()
    t0 = time.perf_counter()
    img.save(buf, format="JPEG", quality=quality, optimize=optimize, progressive=False)
    return buf.getvalue(), (time.perf_counter() - t0) * 1000


def capture(
    monitor: int = 0,
    roi: tuple[int, int, int, int] | None = None,
    mode: str = "precision",
    quality: int | None = None,
    turbo_width: int = TURBO_WIDTH,
    max_width: int | None = None,
    label: str = "",
) -> Frame:
    """Capture the desktop into an optimized Frame.

    monitor:    0 = whole virtual desktop; 1 = primary monitor (mss indexing).
    roi:        (left, top, width, height) in physical pixels; overrides monitor.
    mode:       'precision' | 'turbo' | 'roi' (roi implies precision density).
    max_width:  optional hard cap (used to bound ROI crops on huge windows);
                applies at native density, unlike turbo_width which is tied to
                the turbo mode and also lowers JPEG quality.
    """
    t0 = time.perf_counter()

    if roi is None:
        if monitor >= 0:
            try:
                mon = _mss().monitors[monitor]
            except IndexError:
                mon = _mss().monitors[0]
            left, top, width, height = mon["left"], mon["top"], mon["width"], mon["height"]
        else:
            vx, vy, vw, vh = virtual_screen()
            left, top, width, height = vx, vy, vw, vh
    else:
        left, top, width, height = roi
        # clamp ROI to the virtual desktop
        vx, vy, vw, vh = virtual_screen()
        right, bottom = min(left + width, vx + vw), min(top + height, vy + vh)
        left = max(vx, left)
        top = max(vy, top)
        width, height = right - left, bottom - top

    if width <= 0 or height <= 0:
        raise ValueError("capture region does not intersect the virtual desktop")

    from .win32core import foreground_hwnd, window_title
    fg = foreground_hwnd()
    fg_title = window_title(fg) if fg else ""

    img = _grab(left, top, width, height)
    grab_ms = (time.perf_counter() - t0) * 1000
    screen_w, screen_h = img.width, img.height

    if foreground_hwnd() != fg:
        raise RuntimeError("foreground changed during capture; capture again")

    q = quality if quality is not None else (TURBO_QUALITY if mode == "turbo" else DEFAULT_QUALITY)

    limit = turbo_width if mode == "turbo" else None
    if max_width is not None:
        limit = min(limit, max_width) if limit else max_width
    if limit is not None and img.width > limit:
        new_h = max(2, int(round(img.height * limit / img.width)))
        img = img.resize((limit, new_h), Image.BILINEAR)

    jpeg, encode_ms = encode_jpeg(img, q)

    return Frame(
        jpeg=jpeg, left=left, top=top,
        width=img.width, height=img.height,
        screen_w=screen_w, screen_h=screen_h,
        mode=mode, quality=q,
        grab_ms=grab_ms, encode_ms=encode_ms, label=label,
        fg_hwnd=fg, fg_title=fg_title,
    )


def capture_window(title_keyword: str, pad: int = 8,
                   mode: str = "roi") -> Frame | None:
    """ROI-capture a window by title (foveated mode). None if not found."""
    from .win32core import enumerate_windows
    keyword = title_keyword.lower()
    best = None
    for win in enumerate_windows():
        if keyword in win["title"].lower():
            b = win["bounds"]
            area = b["width"] * b["height"]
            if best is None or area > best[0]:
                best = (area, b)
    if best is None:
        return None
    b = best[1]
    vx, vy, vw, vh = virtual_screen()
    left = max(vx, b["x"] - pad)
    top = max(vy, b["y"] - pad)
    width = min(b["width"] + pad * 2, vx + vw - left)
    height = min(b["height"] + pad * 2, vy + vh - top)
    return capture(roi=(left, top, width, height), mode=mode,
                   label=f"window:{title_keyword}")


def capture_hwnd(hwnd: int, pad: int = 6, max_width: int | None = None,
                 label: str = "") -> Frame | None:
    """ROI-capture a specific window by HWND, native density.

    Unlike foreground-based cropping this cannot be fooled by invisible service
    windows (e.g. GameInputServiceWindow) that steal the foreground: the caller
    names the window explicitly, so grounding always sees the intended pixels.
    """
    from .win32core import window_rect, window_title
    vx, vy, vw, vh = virtual_screen()
    l, t, r, b = window_rect(hwnd)
    if r - l <= 8 or b - t <= 8:
        return None
    l, t = max(l, vx), max(t, vy)
    r, b = min(r, vx + vw), min(b, vy + vh)
    if r - l <= 8 or b - t <= 8:  # window is (almost) entirely off the desktop
        return None
    left = max(vx, l - pad)
    top = max(vy, t - pad)
    width = min(r - l + pad * 2, vx + vw - left)
    height = min(b - t + pad * 2, vy + vh - top)
    return capture(roi=(left, top, width, height), mode="precision",
                   max_width=max_width, label=label or f"hwnd:{window_title(hwnd)}")


def pin_window(hwnd: int, x: int = 40, y: int = 40) -> dict:
    """Park a window at a fixed screen position so repeated captures stay valid.

    Windows (especially UWP apps in remote sessions) can reposition a window on
    activation. Grounding against a stale frame then clicks the wrong pixels.
    Pinning removes that whole class of failure.
    """
    from .win32core import window_rect, user32
    import ctypes as _c
    import time as _t

    user32.SetWindowPos(_c.c_void_p(int(hwnd)), _c.c_void_p(-1),  # HWND_TOPMOST
                        int(x), int(y), 0, 0,
                        0x0001 | 0x0010 | 0x0040)  # NOSIZE | NOACTIVATE | SHOWWINDOW
    _t.sleep(0.25)
    l, t, r, b = window_rect(hwnd)
    return {"hwnd": int(hwnd), "bounds": {"x": l, "y": t,
                                          "width": r - l, "height": b - t}}


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def diff_ratio(a: np.ndarray, b: np.ndarray, threshold: int = 12) -> float:
    """Fraction of pixels that changed noticeably between two thumbnails.

    Thumbnails of the same region can differ by a row or column when the source
    frames were encoded at different scales (precision vs turbo); compare their
    common area instead of reporting a total change. Genuinely different shapes
    (another region or window size) still count as fully changed.
    """
    if a.shape != b.shape:
        if a.ndim != b.ndim or any(abs(x - y) > 2 for x, y in zip(a.shape, b.shape)):
            return 1.0
        rows, cols = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
        a, b = a[:rows, :cols], b[:rows, :cols]
    return float(np.mean(np.abs(a - b) > threshold))


class Stabilizer:
    """Wait only as long as the screen actually needs to settle.

    Replaces the old engine's fixed ``sleep(1.0)`` after every action: poll a
    cheap thumbnail every ~70 ms and return as soon as two consecutive frames
    agree (or the cap elapses).
    """

    def __init__(self, monitor: int = 1, poll_ms: float = 70.0, cap_ms: float = 2500.0,
                 settle: float = 0.004):
        self.monitor = monitor
        self.poll_s = poll_ms / 1000.0
        self.cap_s = cap_ms / 1000.0
        self.settle = settle

    def wait(self) -> float:
        """Returns the waited ms."""
        t0 = time.perf_counter()
        prev: np.ndarray | None = None
        stable_frames = 0
        while (time.perf_counter() - t0) < self.cap_s:
            try:
                frame = capture(monitor=self.monitor, mode="turbo", quality=60)
            except RuntimeError:
                # Focus moved mid-grab (e.g. the click opened a dialog). That is
                # "not settled yet", never a reason to lose an already-sent click.
                prev, stable_frames = None, 0
                time.sleep(self.poll_s)
                continue
            cur = frame.thumbnail(240)
            if prev is not None:
                ratio = diff_ratio(prev, cur)
                if ratio <= self.settle:
                    stable_frames += 1
                    if stable_frames >= 1:
                        break
                else:
                    stable_frames = 0
            prev = cur
            time.sleep(self.poll_s)
        return round((time.perf_counter() - t0) * 1000, 1)
