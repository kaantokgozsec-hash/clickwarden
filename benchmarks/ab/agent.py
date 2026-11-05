"""The two agent loops. Same model, same effort, same limits, same caching; only the tools differ.

* baseline:    Claude with Anthropic's computer-use toolset; it sees screenshots.
* clickwarden: Claude with Clickwarden's MCP tools; it never sees pixels. Clickwarden's own vision model
               runs inside the MCP server, and its tokens are read from Clickwarden's session records.

Heavy-model token counts come only from each response's ``usage`` field.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .computer import Desktop, block_input, run_batch
from .pricing import HeavyUsage

ROOT = Path(__file__).resolve().parents[2]

SYSTEM_CORE = (
    "You are operating a Windows 11 computer to complete one task for an automated benchmark. "
    "The user has already approved every action this task needs, and nobody is available to answer "
    "questions, so do not ask for confirmation. Stay within the task: do not open unrelated apps or "
    "websites and do not change anything the task does not mention. When you have finished, reply with "
    "a line starting with DONE followed by one sentence on what you did. If you cannot finish, reply with "
    "a line starting with FAILED and the reason."
)
BASELINE_NOTE = ("You control the computer with the computer tools. Screenshots are {w}x{h} pixels; give "
                 "coordinates in that pixel space.")
CLICKWARDEN_NOTE = ("You control the computer only through the Clickwarden tools; you never see screenshots. "
                    "The benchmark operator configured Clickwarden's vision provider and accepted its data "
                    "flow beforehand, so there is no user to inform about it.")

# Not offered in clickwarden mode: durable jobs run in a separate worker process whose vision tokens are
# not in the session records (so their cost could not be counted), and the desktop-icon tools rearrange
# the user's real desktop, which no task needs.
EXCLUDED_TOOLS = frozenset({"fast_job_start", "fast_job_status", "fast_job_list", "fast_job_pause",
                            "fast_job_resume", "fast_job_cancel", "fast_desktop_arrange", "fast_desktop_undo",
                            "hud_status"})


class Aborted(RuntimeError):
    """The operator pressed the abort hotkey; the run is not recorded."""


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_turns: int = 30
    timeout_s: float = 300.0
    max_tokens: int = 16000
    screen_w: int = 1280
    screen_h: int = 720
    request_timeout_s: float = 180.0


@dataclass
class RunOutcome:
    heavy: HeavyUsage = field(default_factory=HeavyUsage)
    steps: int = 0
    stop: str = ""
    final_text: str = ""
    error: str = ""
    wall_s: float = 0.0
    vision: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0,
                                                  "usage_complete": True, "sessions": []})
    transcript: list = field(default_factory=list)

    @property
    def claimed_done(self) -> bool:
        return self.final_text.lstrip().upper().startswith("DONE")


def _text_of(content) -> str:
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip()


def _loggable(results: list[dict]) -> list[dict]:
    """Tool results for the transcript: images replaced by a marker, long text cut."""
    out = []
    for r in results:
        content = r.get("content")
        if isinstance(content, list):
            content = [("[image]" if c.get("type") == "image" else c.get("text", "")[:4000]) for c in content]
        elif isinstance(content, str):
            content = content[:4000]
        out.append({"id": r["tool_use_id"], "is_error": bool(r.get("is_error")), "content": content})
    return out


async def agent_loop(client, cfg: Config, system: str, tools: list[dict], prompt: str, execute, abort) -> RunOutcome:
    """Generic tool loop. ``execute(tool_uses, deadline) -> (tool_result blocks, executed count)``."""
    out = RunOutcome()
    messages = [{"role": "user", "content": prompt}]
    start = time.monotonic()
    deadline = start + cfg.timeout_s
    try:
        for _ in range(cfg.max_turns):
            if abort.is_set():
                raise Aborted()
            if time.monotonic() >= deadline:
                out.stop = "timeout"
                break
            response = await client.messages.create(
                model=cfg.model, max_tokens=cfg.max_tokens, system=system, tools=tools, messages=messages,
                output_config={"effort": cfg.effort}, cache_control={"type": "ephemeral"},
                timeout=cfg.request_timeout_s)
            out.heavy.add(response.usage)
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            entry = {"turn": out.heavy.requests, "stop_reason": response.stop_reason,
                     "usage": out.heavy.per_request[-1], "text": _text_of(response.content)[:2000],
                     "calls": [{"id": b.id, "name": b.name, "input": block_input(b)} for b in tool_uses]}
            out.transcript.append(entry)
            if response.stop_reason == "refusal":
                out.stop = "refusal"
                out.final_text = entry["text"]
                break
            if not tool_uses:
                out.final_text = entry["text"]
                out.stop = "end_turn" if response.stop_reason == "end_turn" else str(response.stop_reason)
                break
            if abort.is_set():
                raise Aborted()
            results, executed = await execute(tool_uses, deadline)
            out.steps += executed
            entry["results"] = _loggable(results)
            messages.append({"role": "user", "content": results})
        else:
            out.stop = "max_turns"
    except Aborted:
        raise
    except Exception as exc:  # API or harness error: the run counts as a failure with this reason
        out.stop = "error"
        out.error = f"{type(exc).__name__}: {str(exc)[:300]}"
    out.wall_s = time.monotonic() - start
    return out


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------

async def run_baseline(client, cfg: Config, prompt: str, abort, desktop: Desktop | None = None) -> RunOutcome:
    desktop = desktop or Desktop(cfg.screen_w, cfg.screen_h)
    system = SYSTEM_CORE + "\n\n" + BASELINE_NOTE.format(w=desktop.width, h=desktop.height)
    tools = [{"type": "computer_toolset_20260801"}]

    async def execute(tool_uses, deadline):
        computer = [b for b in tool_uses if getattr(b, "toolset_name", None) == "computer"]
        results, executed = run_batch(desktop, computer, deadline)
        for b in tool_uses:
            if b not in computer:
                results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": f"Unknown tool {b.name}"})
        return results, executed

    out = await agent_loop(client, cfg, system, tools, prompt, execute, abort)
    out.transcript.append({"screenshots": desktop.screenshots, "screen": [desktop.width, desktop.height]})
    return out


# ---------------------------------------------------------------------------
# clickwarden
# ---------------------------------------------------------------------------

def skill_text() -> str:
    """The agent skill shipped with Clickwarden, without its YAML front matter."""
    text = (ROOT / "skills" / "clickwarden" / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    tools = []
    for tool in sorted(mcp_tools, key=lambda t: t.name):  # stable order keeps the prompt cache valid
        if tool.name in EXCLUDED_TOOLS:
            continue
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {"type": "object"}
        tools.append({"name": tool.name, "description": (tool.description or "")[:4000], "input_schema": schema})
    return tools


def session_record(session_id: str) -> dict | None:
    path = Path.home() / ".clickwarden" / "sessions" / f"{session_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


async def run_clickwarden(client, cfg: Config, prompt: str, abort) -> RunOutcome:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "clickwarden_mcp.py")],
                                   env={**os.environ, "CLICKWARDEN_TEXT_ONLY": "1"})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as mcp:
            init = await mcp.initialize()
            listed = await mcp.list_tools()
            tools = mcp_tools_to_anthropic(listed.tools)
            names = {t["name"] for t in tools}
            instructions = getattr(init, "instructions", None) or ""
            system = "\n\n".join(filter(None, [SYSTEM_CORE, CLICKWARDEN_NOTE,
                                               "Clickwarden server instructions:\n" + instructions,
                                               "Clickwarden agent skill:\n" + skill_text()]))
            sessions: list[str] = []
            ended: dict[str, dict] = {}

            async def call(name, args, timeout):
                result = await asyncio.wait_for(mcp.call_tool(name, args), timeout=timeout)
                text = "\n".join(getattr(c, "text", "") for c in (result.content or []))
                is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
                return text, is_error

            async def execute(tool_uses, deadline):
                results, executed = [], 0
                for block in tool_uses:
                    base = {"type": "tool_result", "tool_use_id": block.id}
                    if block.name not in names:
                        results.append({**base, "is_error": True, "content": f"Unknown tool {block.name}"})
                        continue
                    timeout = max(30.0, deadline - time.monotonic() + 30.0)
                    try:
                        text, is_error = await call(block.name, block_input(block), timeout)
                    except asyncio.TimeoutError:
                        text, is_error = f"Tool {block.name} timed out", True
                    executed += 1
                    data = _parse(text)
                    sid = data.get("session_id")
                    if sid and sid not in sessions:
                        sessions.append(sid)
                    if block.name == "fast_session_end" and sid:
                        ended[sid] = data
                    results.append({**base, "content": text or "(empty result)", **({"is_error": True} if is_error else {})})
                return results, executed

            out = await agent_loop(client, cfg, system, tools, prompt, execute, abort)
            # Close any session the model left open so Clickwarden saves its usage record.
            for sid in sessions:
                if sid not in ended:
                    try:
                        text, _ = await call("fast_session_end", {"result": "stopped", "session_id": sid}, 30)
                        ended[sid] = _parse(text)
                    except Exception as exc:  # record what we could not close
                        ended[sid] = {"error": str(exc)}
            out.vision = collect_vision_usage(sessions, ended)
            out.transcript.append({"tools_offered": sorted(names), "sessions": sessions})
            return out


def collect_vision_usage(sessions: list[str], ended: dict[str, dict], reader=session_record) -> dict:
    """Sum Clickwarden's vision usage over every session the run opened.

    Prefers the saved session record; falls back to the numbers fast_session_end returned. If neither
    exists for a session, usage_complete is False and the report flags the run.
    """
    total = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0, "usage_complete": True, "sessions": []}
    for sid in sessions:
        record = reader(sid) or ended.get(sid) or {}
        if "prompt_tokens" not in record:
            total["usage_complete"] = False
            total["sessions"].append({"id": sid, "missing": True})
            continue
        total["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(record.get("completion_tokens") or 0)
        total["requests"] += int(record.get("requests") or 0)
        if record.get("usage_complete") is False:
            total["usage_complete"] = False
        total["sessions"].append({"id": sid, "prompt_tokens": record.get("prompt_tokens"),
                                  "completion_tokens": record.get("completion_tokens"),
                                  "requests": record.get("requests")})
    return total
