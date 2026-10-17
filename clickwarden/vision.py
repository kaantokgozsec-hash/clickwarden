"""
clickwarden.vision — the Flash vision middleman.

This is where the seconds live, so it is engineered relentlessly:

* **On-demand keep-alive session.** ``requests.Session`` reuses connections;
  no paid startup preflight or recurring idle pings.

* **Streaming, validated grounding.** Coordinates are exposed only after a
  complete found result and bounding box validate. Partial output never authorizes input.

* **Token-tight prompts.** Same schema as the blueprint, minimal system text.

Requires the same two headers the previous engine sent (User-Agent + session id);
the endpoint rejects requests without them (HTTP 403 / MissingSessionID).
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from ._version import __version__
from .config import env_flag, env_int
from .grounding import validate_grounding
from .providers import Settings, resolve

# Identifies this program honestly. Clickwarden never presents itself as another client.
USER_AGENT = f"clickwarden/{__version__}"

# Reasoning output costs seconds and can exhaust max_tokens before an answer appears.
# Providers that accept a ``thinking: {"type": "disabled"}`` field can be told to skip
# it (opt-in per provider in ``clickwarden configure``); CLICKWARDEN_THINKING=1 keeps reasoning.
DISABLE_THINKING = not env_flag("THINKING", False)

# ---------------------------------------------------------------------------
# Prompts (kept terse — every token is latency)
# ---------------------------------------------------------------------------

LOCATE_SYSTEM = (
    "Locate only a visibly present target in this {w}x{h} image. {coords} Do not assume the requested control exists. If missing or indistinguishable "
    'reply {{"status":"absent|ambiguous","description":"<reason>"}}. '
    "For a unique visible target return its tight clickable bounds, including the text control "
    "or glyph, not a neighboring row or its whole panel. Right/bottom edges are exclusive. "
    'Reply ONLY JSON: {{"status":"found","target":"<short name>","box":[left,top,right,bottom],'
    '"confidence":"high|medium|low"}}'
)

# A found box is ~40 tokens; reasoning (CLICKWARDEN_THINKING=1) needs the old headroom.
LOCATE_MAX_TOKENS = env_int("LOCATE_MAX_TOKENS", 160 if DISABLE_THINKING else 512)
NOT_VISIBLE = "NOT_VISIBLE"

COORDS_PIXELS = "Coordinates are IMAGE pixels, origin top-left."
COORDS_NORM1000 = ("Coordinates are normalized to a 0-1000 grid on both axes (0,0 top-left, 1000,1000 "
                   "bottom-right), not pixels.")


def coord_note(space: str) -> str:
    return COORDS_NORM1000 if space == "norm1000" else COORDS_PIXELS


def to_pixels(data: dict, width: int, height: int, space: str) -> dict:
    """Convert a model's box/point from a 0-1000 grid to image pixels (no-op for pixels).

    Malformed values are left untouched so validate_grounding still rejects them.
    """
    if space != "norm1000" or not isinstance(data, dict):
        return data
    out = dict(data)
    box = out.get("box")
    if (isinstance(box, list) and len(box) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) for v in box) and
            all(0 <= v <= 1000 for v in box)):
        left, top, right, bottom = box
        out["box"] = [min(width - 1, math.floor(left * width / 1000)),
                      min(height - 1, math.floor(top * height / 1000)),
                      min(width, math.ceil(right * width / 1000)),
                      min(height, math.ceil(bottom * height / 1000))]
    point = out.get("point")
    if (isinstance(point, list) and len(point) == 2 and
            all(type(v) in (int, float) and math.isfinite(v) for v in point) and
            all(0 <= v <= 1000 for v in point)):
        out["point"] = [math.floor(point[0] * width / 1000), math.floor(point[1] * height / 1000)]
    return out

INSPECT_SYSTEM = (
    "You are a precise desktop inspector. Answer about the {w}x{h} screenshot in "
    "1-3 sentences. Name the active app, visible controls and state. No preamble. "
    "If the question asks about something not shown in the screenshot, start with "
    "'NOT_VISIBLE:' and say briefly what is shown instead; never answer from other content."
)

PLAN_SYSTEM = (
    "You are an autonomous Windows GUI agent. The image is {w}x{h}; {coords} "
    "Goal: {task}\nRecent actions:\n{history}\n"
    "Decide the single next action and reply with ONLY this JSON schema:\n"
    '{{"action":"click|double_click|type|hotkey|scroll|wait|done|fail",'
    '"status":"found|absent|ambiguous","target":"<visible control>","box":[left,top,right,bottom],'
    '"confidence":"high|medium|low",'
    '"button":"left|right","text":"...","press_enter":true,'
    '"keys":"ctrl,t","clicks":-3,"seconds":1,"reason":"<why>","summary":"<if done>"}}\n'
    "For clicks include found status, confidence and tight clickable bounds in those coordinates; "
    "right/bottom edges exclusive. Never invent a missing control. Use wait/fail when absent "
    "or ambiguous. Prefer done only when visible evidence supports the goal."
)

# ---------------------------------------------------------------------------
# Streaming plumbing
# ---------------------------------------------------------------------------

_POINT_RE = re.compile(r'"point"\s*:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]')
_ACTION_RE = re.compile(r'"action"\s*:\s*"([a-z_]+)"')
_KEYS_RE = re.compile(r'"keys"\s*:\s*"([^"]+)"')
_TEXT_RE = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
_REASON_RE = re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"')
_CLICKS_RE = re.compile(r'"clicks"\s*:\s*(-?\d+)')
_SECONDS_RE = re.compile(r'"seconds"\s*:\s*(\d+)')
_CONF_RE = re.compile(r'"confidence"\s*:\s*"(high|medium|low)"')
_BUTTON_RE = re.compile(r'"button"\s*:\s*"(left|right)"')
_ENTER_RE = re.compile(r'"press_enter"\s*:\s*(true|false)')
_SUMMARY_RE = re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _extract_json(text: str):
    """Parse the JSON object in a model reply.

    Accepts a bare object, a fenced ```json block, or an object surrounded by a
    short preamble/epilogue. This only recovers the *syntax*; every caller still
    runs the strict schema validation, so a salvaged reply never bypasses
    found/absent/ambiguous or box checks. Raises ValueError when nothing parses.
    """
    blob = text.strip()
    if "```" in blob:
        inner = blob.split("```")[1]
        if inner.lstrip().lower().startswith("json"):
            inner = inner.lstrip()[4:]
        blob = inner.strip()
    try:
        return json.loads(blob)
    except ValueError:
        start, end = blob.find("{"), blob.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(blob[start:end + 1])


_VERDICT_RE = re.compile(r"[A-Za-z]+")


def is_pass(answer: str | None) -> bool:
    """True only when a PASS/FAIL check reply clearly says PASS.

    Models often answer "PASS.", "**PASS**" or "Pass" even when told to reply
    exactly PASS; treating those as failures produced false misses. Anything
    that mentions FAIL, or does not start with PASS, is still a failure.
    """
    words = _VERDICT_RE.findall(answer or "")
    if not words or words[0].upper() != "PASS":
        return False
    return not any(word.upper() == "FAIL" for word in words)


def _json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


@dataclass
class StreamResult:
    content: str = ""
    ttft_ms: float | None = None
    first_point_ms: float | None = None
    total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    error: str | None = None
    retries: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.content) and not self.error


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class VisionClient:
    """Thread-safe, keep-alive client for the Flash vision model."""

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 api_url: str | None = None, timeout: float | None = None,
                 settings: Settings | None = None):
        """Settings come from ``clickwarden configure`` unless the caller passes them.

        Passing model/api_key/api_url (or settings) means the caller has chosen the
        endpoint, so the user-consent gate used for saved settings does not apply.
        """
        if settings is None:
            if model is None and api_key is None and api_url is None:
                settings = resolve()
            else:
                settings = Settings.explicit(model, api_key, api_url)
        self.settings = settings
        self.model = settings.model
        self.api_url = settings.base_url
        self.timeout = float(timeout if timeout is not None else settings.timeout)
        self.api_key = settings.api_key
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **settings.headers,
        })
        if self.api_key:
            self._session.headers["Authorization"] = f"Bearer {self.api_key}"
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self._session.mount("https://", adapter)
        self._prewarm_thread: threading.Thread | None = None
        self._prewarm_stop = threading.Event()
        self.total_requests = 0
        self.last_latency_ms = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_reports = 0
        self.before_request: Callable[[], None] | None = None

    # -- session lifecycle -------------------------------------------------

    @property
    def configured(self) -> bool:
        s = self.settings
        return not s.problem and bool(self.api_key or s.local)

    def prewarm(self) -> float:
        """Open/verify the TLS connection so the next user request skips setup."""
        t0 = time.perf_counter()
        if not self.configured:
            return 0.0
        try:
            self._session.post(
                self.api_url,
                json={"model": self.model, "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1, "stream": False},
                timeout=20,
            )
        except requests.RequestException:
            pass
        return round((time.perf_counter() - t0) * 1000, 1)

    def start_prewarmer(self, interval_s: float = 40.0) -> None:
        """Compatibility no-op: paid model calls are never sent just to stay warm.

        Requests reuse the Session connection on demand. Explicit prewarm() is
        still available as a diagnostic, but no recurring request is scheduled.
        """

    def close(self) -> None:
        self._prewarm_stop.set()
        try:
            self._session.close()
        except Exception:
            pass

    # -- core call ---------------------------------------------------------

    def stream_chat(
        self,
        system: str,
        user_text: str,
        jpeg: bytes,
        on_partial: Callable[[str], None] | None = None,
        max_tokens: int = 400,
        temperature: float = 0.0,
        disable_thinking: bool | None = None,
    ) -> StreamResult:
        """Stream a vision completion; ``on_partial`` sees the growing text buffer.

        The callback fires on every SSE delta so the orchestrator can act the
        millisecond a coordinate parse succeeds.
        """
        if not self.configured:
            return StreamResult(error=self.settings.problem or
                                "No vision provider is configured. Ask the user to run `clickwarden configure`.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," +
                                   __import__("base64").b64encode(jpeg).decode("ascii")}},
                ]},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if self.settings.include_usage:
            payload["stream_options"] = {"include_usage": True}
        if self.settings.send_thinking_disabled and (
                disable_thinking if disable_thinking is not None else DISABLE_THINKING):
            payload["thinking"] = {"type": "disabled"}
        headers = {}

        # The endpoint occasionally ends a 200 stream with no content at all
        # (transient upstream). One cheap retry recovers it — observed recovery
        # is immediate, and the alternative is a 4-second wasted action.
        t0 = time.perf_counter()
        for attempt in range(2):
            result = self._stream_once(payload, headers, on_partial, t0)
            result.retries = attempt
            transient = result.error and (result.error.startswith(("ReadTimeout:", "ConnectTimeout:", "ConnectionError:"))
                                            or result.error.startswith(("HTTP 502:", "HTTP 503:", "HTTP 504:")))
            if result.content or (result.error and not transient):
                return result
            if attempt == 0:
                time.sleep(0.12)
        return result

    def _stream_once(self, payload: dict, headers: dict,
                     on_partial: Callable[[str], None] | None,
                     t0: float) -> StreamResult:
        result = StreamResult(model=self.model)
        resp = None
        try:
            if self.before_request:
                self.before_request()
            with self._lock:  # serialize session use; keep-alive pool stays hot
                self.total_requests += 1
                resp = self._session.post(self.api_url, json=payload, headers=headers,
                                          stream=True, timeout=(min(self.timeout, 10.0), self.timeout))
            if resp.status_code != 200:
                result.error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                result.total_ms = (time.perf_counter() - t0) * 1000
                return result

            buf: list[str] = []
            last_content = time.monotonic()
            for raw in resp.iter_lines():
                if time.monotonic() - last_content > self.timeout:
                    raise requests.ReadTimeout(f"No model content for {self.timeout:g} seconds")
                if not raw or not raw.startswith(b"data: "):
                    continue
                chunk = raw[6:]
                if chunk == b"[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                    usage = event.get("usage")
                    if usage:
                        result.prompt_tokens = int(usage.get("prompt_tokens", 0))
                        result.completion_tokens = int(usage.get("completion_tokens", 0))
                    delta = event["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                piece = delta.get("content")
                if not piece:
                    continue
                if result.ttft_ms is None:
                    result.ttft_ms = (time.perf_counter() - t0) * 1000
                buf.append(piece)
                last_content = time.monotonic()
                text = "".join(buf)
                if on_partial is not None:
                    on_partial(text)

            result.content = "".join(buf)
            result.total_ms = (time.perf_counter() - t0) * 1000
            self.last_latency_ms = result.total_ms
        except requests.RequestException as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.total_ms = (time.perf_counter() - t0) * 1000
        finally:
            if result.prompt_tokens or result.completion_tokens:
                self.prompt_tokens += result.prompt_tokens
                self.completion_tokens += result.completion_tokens
                self.usage_reports += 1
            if resp is not None:
                resp.close()
        return result

    # -- grounding ---------------------------------------------------------

    def locate(
        self,
        frame,
        description: str,
        on_point: Callable[[int, int], None] | None = None,
        max_tokens: int = LOCATE_MAX_TOKENS,
    ) -> dict:
        """Find *description* on the frame; return native screen coords.

        ``on_point`` runs only after the complete found result and box validate.
        """
        system = LOCATE_SYSTEM.format(w=frame.width, h=frame.height, coords=coord_note(self.settings.coord_space))
        user = f"Locate: {description}. JSON only."
        state: dict[str, Any] = {"fired": False, "raw": None, "first_ms": None}
        t0 = time.perf_counter()

        def _on_partial(text: str) -> None:
            if state["fired"]:
                return
            m = _POINT_RE.search(text)
            if m and 0 <= int(m.group(1)) < frame.width and 0 <= int(m.group(2)) < frame.height:
                state["fired"] = True
                state["raw"] = [int(m.group(1)), int(m.group(2))]
                state["first_ms"] = (time.perf_counter() - t0) * 1000

        res = self.stream_chat(system, user, frame.jpeg, on_partial=_on_partial,
                               max_tokens=max_tokens)
        if not res.ok:
            return {"success": False, "error": res.error,
                    "latency_ms": round(res.total_ms)}

        parsed: dict[str, Any] = {}
        try:
            parsed = _extract_json(res.content)
        except (ValueError, TypeError):
            return {"success": False, "error": "Incomplete or invalid grounding JSON",
                    "latency_ms": round(res.total_ms)}

        parsed = to_pixels(parsed, frame.width, frame.height, self.settings.coord_space)
        validated = validate_grounding(parsed, frame.width, frame.height)
        if not validated["success"]:
            return {**validated, "latency_ms": round(res.total_ms)}
        rx, ry = validated["image_point"]
        native = frame.clip(rx, ry)
        l, t, r, b = validated["image_box"]
        box = [*frame.to_screen(l, t), *frame.to_screen(r, b)]
        if not (box[0] <= native[0] < box[2] and box[1] <= native[1] < box[3]):
            return {"success": False, "error": "mapped box has no safe interior",
                    "latency_ms": round(res.total_ms)}
        if on_point is not None:
            on_point(*native)
        return {
            **validated,
            "target": parsed.get("target", description),
            "point": list(native),
            "image_point": [rx, ry],
            "confidence": parsed.get("confidence", "medium"),
            "box": box,
            "target_size": [box[2]-box[0], box[3]-box[1]],
            "description": parsed.get("description", ""),
            "latency_ms": round(res.total_ms),
            "ttft_ms": round(res.ttft_ms) if res.ttft_ms else None,
            "first_point_ms": round(state["first_ms"]) if state["first_ms"] else None,
            "near_edge": self._near_edge(frame, rx, ry),
        }

    @staticmethod
    def _near_edge(frame, x: int, y: int, margin_ratio: float = 0.035) -> bool:
        """True when a point hugs the frame border — a likely clipping artefact."""
        mx = max(10, int(frame.width * margin_ratio))
        my = max(10, int(frame.height * margin_ratio))
        return x < mx or y < my or x > frame.width - mx or y > frame.height - my

    # -- inspection --------------------------------------------------------

    def inspect(self, frame, query: str, max_tokens: int = 260) -> dict:
        system = INSPECT_SYSTEM.format(w=frame.width, h=frame.height)
        res = self.stream_chat(system, query, frame.jpeg, max_tokens=max_tokens)
        answer = res.content.strip()
        return {
            "success": res.ok,
            "answer": answer,
            # Model assessment: False means the asked-about content was not on screen.
            "visible": not answer.lstrip("*_ ").upper().startswith(NOT_VISIBLE) if res.ok else None,
            "latency_ms": round(res.total_ms),
            "error": res.error,
        }

    # -- planning ----------------------------------------------------------

    def plan(self, frame, task: str, history: list[str], max_tokens: int = 320) -> dict:
        system = PLAN_SYSTEM.format(
            w=frame.width, h=frame.height, task=task, coords=coord_note(self.settings.coord_space),
            history="\n".join(f"- {h}" for h in history[-6:]) or "none",
        )
        res = self.stream_chat(system, "Decide the next action. JSON only.", frame.jpeg,
                               max_tokens=max_tokens)
        if not res.ok:
            return {"success": False, "error": res.error, "latency_ms": round(res.total_ms)}

        try:
            data = _extract_json(res.content)
        except (ValueError, TypeError):
            return {"success": False, "error": "Incomplete or invalid plan JSON",
                    "latency_ms": round(res.total_ms)}

        if not isinstance(data, dict):
            return {"success": False, "error": "plan must be a JSON object"}
        if data.get("action") not in ("click", "double_click", "type", "hotkey", "scroll", "wait", "done", "fail"):
            return {"success": False, "error": "unsupported plan action"}

        data = to_pixels(data, frame.width, frame.height, self.settings.coord_space)
        data["success"] = True
        data["latency_ms"] = round(res.total_ms)
        if data.get("action") in ("click", "double_click"):
            validated = validate_grounding(data, frame.width, frame.height)
            if not validated['success'] or validated.get('confidence') == 'low':
                return {"success": False, "error": validated.get('error', 'Low-confidence click plan')}
            data['screen_point'] = list(frame.clip(*validated['image_point']))
            l, t, r, b = validated['image_box']
            data['screen_box'] = [*frame.to_screen(l,t), *frame.to_screen(r,b)]
        if "press_enter" in data:
            data["press_enter"] = str(data["press_enter"]).lower() == "true"
        if "clicks" in data:
            try:
                data["clicks"] = int(data["clicks"])
            except (TypeError, ValueError):
                data.pop("clicks", None)
        if "seconds" in data:
            try:
                data["seconds"] = int(data["seconds"])
            except (TypeError, ValueError):
                data.pop("seconds", None)
        return data


# Module-level singleton used by the engine/server.
client = VisionClient()
