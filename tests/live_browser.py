"""Opt-in Edge acceptance in a temporary profile; screenshots go only to vision.

Serves a local fixture, navigates through Clickwarden, then reads a public Bing
search. Does not use the user's browser profile or accounts.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class Page(BaseHTTPRequestHandler):
    hits = set()
    def do_GET(self):
        self.hits.add(self.path)
        body = ("<title>Clickwarden browser fixture</title><body style='font:40px sans-serif;padding:80px'>"
                "CLICKWARDEN BROWSER PASS<br>Local navigation fixture</body>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass


async def main():
    import win32process
    edge = next((Path(p) / "Microsoft/Edge/Application/msedge.exe" for p in
                 (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""))
                 if (Path(p) / "Microsoft/Edge/Application/msedge.exe").is_file()), None)
    if edge is None:
        raise RuntimeError("Edge is required for this opt-in test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    report = {}
    child = None
    with tempfile.TemporaryDirectory(prefix="clickwarden-edge-test-") as profile:
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "clickwarden_mcp.py")])
        try:
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
                        start = await call("use_my_computer", objective="Isolated browser acceptance", app_or_url="desktop")
                        sid = start["session_id"]
                        for _ in range(20):
                            state = await call("fast_session_status", session_id=sid)
                            if state["status"] != "preparing":
                                break
                            await asyncio.sleep(.2)
                        assert state["status"] == "idle", state
                        child = subprocess.Popen([str(edge), "--no-first-run", "--no-default-browser-check",
                            "--disable-background-mode", f"--user-data-dir={profile}", "--inprivate", "--new-window", url + "/initial"],
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        target = None
                        for _ in range(40):
                            windows = await call("list_windows")
                            if isinstance(windows, dict):
                                windows = windows.get("windows", [])
                            target = next((w["hwnd"] for w in windows if
                                win32process.GetWindowThreadProcessId(w["hwnd"])[1] == child.pid), None)
                            if target:
                                break
                            await asyncio.sleep(.3)
                        assert target, "No browser window belonging to the isolated process appeared"
                        switched = await call("fast_switch_window", hwnd=target, session_id=sid)
                        assert switched["status"] == "success", switched
                        navigation = await call("fast_navigate", url=url + "/proof", session_id=sid)
                        assert navigation["status"] == "success", navigation
                        for _ in range(20):
                            if "/proof" in Page.hits:
                                break
                            await asyncio.sleep(.2)
                        assert "/proof" in Page.hits, "Browser never requested the navigation fixture"
                        observed = await call("fast_inspect", query="Read the large status heading on this page. Reply only that heading.", session_id=sid)
                        assert observed.get("success") and "CLICKWARDEN BROWSER PASS" in observed.get("answer", ""), observed
                        report["navigation_http_readback"] = True
                        report["fixture_vision_readback"] = True
                        search = await call("fast_web_search", query="Python official documentation", read_results=True, session_id=sid)
                        assert search["status"] == "success", search
                        results = search["observation"]
                        assert results.get("success"), results
                        report["search_observation"] = results.get("answer", "")
                        report["readiness"] = search["readiness"]
                        report["vision_requests"] = (await call("fast_session_status", session_id=sid))["requests"]
                    finally:
                        if sid:
                            report["end"] = await call("fast_session_end", session_id=sid, result="completed")
        finally:
            server.shutdown()
            server.server_close()
            if child and child.poll() is None:
                # Only the still-running process we launched and its own tree.
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                child.wait(timeout=5)
            await asyncio.sleep(.5)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
