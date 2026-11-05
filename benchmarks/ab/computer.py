"""Baseline mode: carry out ``computer_toolset_20260801`` member calls on the real desktop.

Screenshots are captured at the physical resolution and downscaled to ``width`` x ``height`` (1280x720 by
default, a low-cost setting Anthropic recommends for desktop tasks). The model's coordinates are in that
screenshot space and are scaled back to physical pixels here. Input goes through Clickwarden's own Win32
input layer (``clickwarden.input``), so both benchmark modes press keys and click the same way; only
perception and planning differ.
"""
from __future__ import annotations

import base64
import io
import time

from PIL import Image

NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

# xdotool-style key names the model uses -> clickwarden.input key names
KEY_ALIASES = {
    "return": "enter", "kp_enter": "enter", "enter": "enter",
    "super": "win", "super_l": "win", "super_r": "win", "cmd": "win", "meta": "win", "windows": "win", "win": "win",
    "control": "ctrl", "control_l": "ctrl", "control_r": "ctrl", "ctrl": "ctrl",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "option": "alt",
    "escape": "esc", "esc": "esc", "backspace": "backspace", "delete": "delete", "del": "delete",
    "page_down": "pagedown", "next": "pagedown", "pagedown": "pagedown",
    "page_up": "pageup", "prior": "pageup", "pageup": "pageup",
    "minus": "-", "equal": "=", "plus": "=", "period": ".", "comma": ",", "slash": "/", "backslash": "\\",
    "semicolon": ";", "apostrophe": "'", "grave": "`", "bracketleft": "[", "bracketright": "]",
    "space": "space", "tab": "tab", "home": "home", "end": "end", "insert": "insert",
    "up": "up", "down": "down", "left": "left", "right": "right",
}


class ActionError(RuntimeError):
    pass


def chord(text: str) -> list[str]:
    """'ctrl+shift+Escape' -> ['ctrl', 'shift', 'esc'] in clickwarden.input names."""
    text = (text or "").strip()
    if not text:
        raise ActionError("empty key name")
    keys = []
    for part in text.split("+"):
        name = part.strip()
        if not name:
            raise ActionError(f"bad key chord {text!r}")
        low = name.lower()
        if low in KEY_ALIASES:
            keys.append(KEY_ALIASES[low])
        elif len(name) == 1 or (low.startswith("f") and low[1:].isdigit()):
            keys.append(low)
        else:
            raise ActionError(f"unsupported key {name!r}")
    return keys


def key_sequence(text: str) -> list[list[str]]:
    """'ctrl+a Delete' (space separated) -> [['ctrl','a'], ['delete']]."""
    return [chord(part) for part in (text or "").split()] or [chord(text)]


def fit(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Scale ``size`` down to fit inside ``box``, keeping the aspect ratio."""
    w, h = size
    scale = min(box[0] / w, box[1] / h, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


class Desktop:
    """The baseline 'computer'. ``backend`` is clickwarden.input (live) or a fake in tests."""

    def __init__(self, width: int = 1280, height: int = 720, backend=None, grabber=None,
                 post_action_delay: float = 0.6):
        if backend is None:
            from clickwarden import input as backend  # noqa: PLW0127 - live Win32 input
            from clickwarden.win32core import ensure_dpi_aware
            ensure_dpi_aware()
        self.io = backend
        self.grab = grabber or self._grab_mss
        self.post_action_delay = post_action_delay
        first = self.grab(None)
        self.phys_w, self.phys_h = first.size
        self.width, self.height = fit((self.phys_w, self.phys_h), (width, height))
        self.sx = self.phys_w / self.width
        self.sy = self.phys_h / self.height
        self.screenshots = 0

    # -- capture ----------------------------------------------------------------
    @staticmethod
    def _grab_mss(region):
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]
            if region:
                left, top, right, bottom = region
                mon = {"left": mon["left"] + left, "top": mon["top"] + top,
                       "width": max(1, right - left), "height": max(1, bottom - top)}
            shot = sct.grab(mon)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    @staticmethod
    def _image_block(image: Image.Image) -> dict:
        buf = io.BytesIO()
        image.save(buf, "PNG", optimize=False)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(buf.getvalue()).decode("ascii")}}

    def screenshot(self) -> dict:
        self.screenshots += 1
        return self._image_block(self.grab(None).resize((self.width, self.height), Image.LANCZOS))

    def zoom(self, region) -> dict:
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ActionError("region must be [x0, y0, x1, y1]")
        x0, y0 = self.to_screen(region[:2])
        x1, y1 = self.to_screen(region[2:])
        if x1 <= x0 or y1 <= y0:
            raise ActionError("region must have positive width and height")
        crop = self.grab((x0, y0, x1, y1))
        self.screenshots += 1
        return self._image_block(crop.resize(fit(crop.size, (self.width, self.height)), Image.LANCZOS))

    # -- coordinates --------------------------------------------------------------
    def to_screen(self, xy) -> tuple[int, int]:
        if not isinstance(xy, (list, tuple)) or len(xy) != 2:
            raise ActionError("coordinate must be [x, y]")
        x, y = xy
        if not (0 <= x <= self.width and 0 <= y <= self.height):
            raise ActionError(f"coordinate {list(xy)} is outside the {self.width}x{self.height} screenshot")
        return (min(self.phys_w - 1, round(x * self.sx)), min(self.phys_h - 1, round(y * self.sy)))

    def to_shot(self, xy) -> tuple[int, int]:
        return round(xy[0] / self.sx), round(xy[1] / self.sy)

    # -- actions ------------------------------------------------------------------
    def _with_modifiers(self, text, fn):
        mods = chord(text) if text else []
        pressed = []
        try:
            for key in mods:
                self.io.key_down(key)
                pressed.append(key)
            return fn()
        finally:
            for key in reversed(pressed):
                self.io.key_up(key)

    def _click(self, inp, button="left", clicks=1):
        if inp.get("coordinate") is not None:
            x, y = self.to_screen(inp["coordinate"])
        else:
            x, y = self.io.cursor_xy()
        self._with_modifiers(inp.get("text"), lambda: self.io.click(x, y, button=button, clicks=clicks))

    def execute(self, name: str, inp: dict, deadline: float | None = None):
        """Run one member call. Returns a list of content blocks for the tool_result."""
        inp = inp or {}
        if name == "screenshot":
            return [self.screenshot()]
        if name == "zoom":
            return [self.zoom(inp.get("region"))]
        if name == "cursor_position":
            x, y = self.to_shot(self.io.cursor_xy())
            return [{"type": "text", "text": f"X={x}, Y={y}"}]
        if name in ("left_click", "right_click", "middle_click"):
            self._click(inp, button=name.split("_")[0])
        elif name == "double_click":
            self._click(inp, clicks=2)
        elif name == "triple_click":
            self._click(inp, clicks=3)
        elif name == "left_click_drag":
            x1, y1 = self.to_screen(inp.get("start_coordinate"))
            x2, y2 = self.to_screen(inp.get("coordinate"))
            self._with_modifiers(inp.get("text"), lambda: self.io.drag(x1, y1, x2, y2))
        elif name == "mouse_move":
            x, y = self.to_screen(inp.get("coordinate"))
            self.io.move_to(x, y)
        elif name in ("left_mouse_down", "left_mouse_up"):
            down, up = self.io._BUTTON_FLAGS["left"]
            self.io._send(self.io._mouse_input(0, 0, 0, down if name.endswith("down") else up))
        elif name == "scroll":
            amount = int(inp.get("scroll_amount") or 3)
            direction = inp.get("scroll_direction")
            if direction not in ("up", "down", "left", "right"):
                raise ActionError("scroll_direction must be up, down, left or right")
            at = self.to_screen(inp["coordinate"]) if inp.get("coordinate") is not None else (None, None)
            clicks = amount if direction in ("up", "left") else -amount
            text = inp.get("text") or ""
            if direction in ("left", "right"):
                text = "+".join(filter(None, [text, "shift"]))
            self._with_modifiers(text, lambda: self.io.scroll(clicks, *at))
        elif name == "type":
            text = inp.get("text")
            if not isinstance(text, str):
                raise ActionError("type needs text")
            self.io.type_text(text)
        elif name == "key":
            repeat = max(1, min(100, int(inp.get("repeat") or 1)))
            for _ in range(repeat):
                for keys in key_sequence(inp.get("text")):
                    self.io.hotkey(keys)
        elif name == "hold_key":
            keys = chord(inp.get("text"))
            duration = self._bounded(inp.get("duration"), deadline)
            pressed = []
            try:
                for key in keys:
                    self.io.key_down(key)
                    pressed.append(key)
                time.sleep(duration)
            finally:
                for key in reversed(pressed):
                    self.io.key_up(key)
        elif name == "wait":
            time.sleep(self._bounded(inp.get("duration"), deadline))
            return [{"type": "text", "text": "OK"}]
        else:
            raise ActionError(f"unknown computer action {name!r}")
        if self.post_action_delay:
            time.sleep(self.post_action_delay)
        return [{"type": "text", "text": "OK"}]

    @staticmethod
    def _bounded(duration, deadline) -> float:
        value = max(0.0, min(float(duration or 1), 300.0))
        if deadline is not None:
            value = min(value, max(0.0, deadline - time.monotonic()))
        return value


def block_input(block) -> dict:
    """A tool_use block's input as a plain dict (the SDK gives typed models for toolset members)."""
    value = getattr(block, "input", None)
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return dict(value)


def run_batch(desktop: Desktop, blocks, deadline: float | None = None) -> tuple[list[dict], int]:
    """Execute a turn's computer tool_use blocks in order, stopping at the first failure.

    Returns (tool_result blocks, number of actions executed). Every tool_use gets exactly one result,
    each echoing toolset_name, as the API requires.
    """
    results, executed, failed = [], 0, False
    for block in blocks:
        base = {"type": "tool_result", "tool_use_id": block.id, "toolset_name": "computer"}
        if failed:
            results.append({**base, "is_error": True, "content": NOT_EXECUTED})
            continue
        try:
            content = desktop.execute(block.name, block_input(block), deadline)
            executed += 1
            results.append({**base, "content": content})
        except Exception as exc:  # report to the model, stop the batch
            failed = True
            executed += 1
            results.append({**base, "is_error": True, "content": f"Error: {type(exc).__name__}: {exc}"})
    return results, executed
