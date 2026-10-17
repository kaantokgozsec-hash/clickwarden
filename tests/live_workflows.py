"""Opt-in fresh-MCP acceptance using two disposable apps and temporary files.

No vision requests, screenshots, user document changes or external navigation.
"""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def fixture(title, output, x):
    import tkinter as tk
    root = tk.Tk()
    root.title(title)
    root.geometry(f"430x180+{x}+240")
    tk.Label(root, text="Disposable Clickwarden workflow test").pack(pady=12)
    entry = tk.Entry(root, width=35)
    entry.pack(pady=12)
    entry.focus_set()
    def save():
        Path(output).write_text(json.dumps({"text": entry.get()}), encoding="utf-8")
        root.after(60, save)
    root.after(60, save)
    root.after(45000, root.destroy)
    root.mainloop()


async def main():
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "clickwarden_mcp.py")])
    report = {}
    children = []
    with tempfile.TemporaryDirectory(prefix="clickwarden-workflows-") as folder:
        work = Path(folder)
        (work / "sample.DWG").write_text("test fixture", encoding="utf-8")
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                async def call(name, **kwargs):
                    response = await client.call_tool(name, kwargs)
                    assert not getattr(response, "is_error", getattr(response, "isError", False)), response
                    assert all(c.type == "text" for c in response.content)
                    return json.loads(response.content[0].text)
                sid = None
                try:
                    caps = await call("fast_capabilities")
                    assert caps["runtime_revision"] == "2026-10-15-input-5"
                    report["runtime_revision"] = caps["runtime_revision"]
                    tools = {t.name for t in (await client.list_tools()).tools}
                    assert {"fast_web_search", "fast_navigate", "fast_switch_window", "fast_find_files"} <= tools
                    start = await call("use_my_computer", objective="Disposable multi-app acceptance", app_or_url="desktop")
                    sid = start["session_id"]
                    for _ in range(20):
                        state = await call("fast_session_status", session_id=sid)
                        if state["status"] != "preparing":
                            break
                        await asyncio.sleep(.2)
                    assert state["status"] == "idle", state
                    titles = ["Clickwarden fixture " + uuid.uuid4().hex[:8] for _ in range(2)]
                    for i, title in enumerate(titles):
                        children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                            "--fixture", title, str(work / f"state{i}.json"), str(180 + i*480)],
                            creationflags=subprocess.CREATE_NO_WINDOW))
                    windows = []
                    for _ in range(20):
                        windows = await call("list_windows")
                        if isinstance(windows, dict):
                            windows = windows.get("windows", [])
                        if all(any(w["title"] == title for w in windows) for title in titles):
                            break
                        await asyncio.sleep(.2)
                    handles = [next(w["hwnd"] for w in windows if w["title"] == title) for title in titles]
                    for i, hwnd in enumerate(handles):
                        switched = await call("fast_switch_window", hwnd=hwnd, session_id=sid)
                        assert switched["status"] == "success", switched
                        assert switched["fresh_observation_required"]
                        typed = await call("fast_type", text=f"fixture-{i}", session_id=sid)
                        assert typed["status"] == "success", typed
                        await asyncio.sleep(.2)
                        assert json.loads((work / f"state{i}.json").read_text())["text"] == f"fixture-{i}"
                    report["cross_process_switch_and_type"] = True
                    await call("fast_session_control", command="pause", session_id=sid)
                    refused = await call("fast_switch_window", hwnd=handles[0], session_id=sid)
                    assert refused["status"] == "paused", refused
                    found = await call("fast_find_files", root=str(work), pattern="*.dwg", session_id=sid)
                    assert len(found["matches"]) == 1 and found["complete"], found
                    assert (await call("fast_session_status", session_id=sid))["status"] == "paused"
                    report["paused_search_and_switch_refusal"] = True
                    state = await call("fast_session_status", session_id=sid)
                    assert state["requests"] == 0
                    report["vision_requests"] = state["requests"]
                finally:
                    if sid:
                        report["end"] = await call("fast_session_end", session_id=sid, result="completed")
                    for child in children:
                        if child.poll() is None:
                            child.terminate()
                        child.wait(timeout=5)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture":
        fixture(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        asyncio.run(main())
