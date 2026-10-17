"""Start a fresh local server and check its schemas. No UI or paid requests."""
import asyncio
import json
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REQUIRED = {"use_my_computer", "fast_session_status", "fast_session_end",
            "fast_capabilities", "fast_switch_window", "fast_navigate", "fast_web_search",
            "fast_find_files", "fast_desktop_inventory", "fast_desktop_arrange", "fast_desktop_undo",
            "fast_controls", "fast_click_control"}


async def main():
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "clickwarden_mcp.py")])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            missing = sorted(REQUIRED - names)
            if missing:
                raise RuntimeError("Missing tools: " + ", ".join(missing))
            response = await client.call_tool("fast_capabilities", {})
            assert all(c.type == "text" for c in response.content)
            capabilities = json.loads(response.content[0].text)
            assert capabilities["text_only"], "Keep CLICKWARDEN_TEXT_ONLY=1 for this project"
            print(json.dumps({"status": "passed", "tool_count": len(names),
                              "capabilities": capabilities}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
