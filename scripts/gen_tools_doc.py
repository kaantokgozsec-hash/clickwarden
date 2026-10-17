"""Generate docs/TOOLS.md from the live MCP tool list so it cannot drift from the code.

    python scripts/gen_tools_doc.py
"""
import asyncio
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from clickwarden.server import app  # noqa: E402


def schema_of(tool):
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}


async def main():
    tools = sorted(await app.list_tools(), key=lambda t: t.name)
    out = ["# MCP tool reference", "",
           "Generated from the server's own tool list (`python scripts/gen_tools_doc.py`). Every tool that acts",
           "needs an open session: call `use_my_computer` first, pass its `session_id`, call `fast_session_end`",
           "last. Read [SAFETY.md](../SAFETY.md) first: these tools operate a real desktop.", ""]
    for tool in tools:
        schema = schema_of(tool)
        props, required = schema.get("properties", {}), set(schema.get("required", []))
        args = ", ".join(f"{name}{'' if name in required else '?'}" for name in props)
        out += [f"## `{tool.name}({args})`", "", " ".join((tool.description or "").split()), ""]
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "TOOLS.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(len(tools), "tools documented")


asyncio.run(main())
