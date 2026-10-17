"""Opt-in mouse/keyboard acceptance through fresh Clickwarden text-only MCP.

Only a disposable local page/profile; objective event readback, no vision.
Never resumes physical takeover. Always closes its own session/browser.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


HTML = r'''<!doctype html><meta charset="utf-8"><title>Clickwarden input fixture</title>
<style>body{font:20px sans-serif;padding:40px}button,textarea{font:20px sans-serif;margin:12px}
#drag{width:170px;height:70px}#wheel{height:120px;overflow:auto;border:2px solid;width:420px}</style>
<h1>Disposable input test</h1><button id="click">Record click</button><br>
<button id="tiny" aria-label="Tiny target" style="width:20px;height:20px;padding:0;font-size:10px">+</button>
<textarea id="edit" aria-label="Test editor" rows="3" cols="30"></textarea><br>
<button id="drag">Drag test</button>
<div id="wheel" role="region" aria-label="Scroll test" tabindex="0"><div style="height:800px">Scroll here</div></div>
<script>
const state={sequence:0,clicks:0,tiny:0,context:0,double:0,drags:0,wheels:0,text:'',scroll:0};
function report(){state.sequence++;state.text=edit.value;state.scroll=wheel.scrollTop;
fetch('/state',{method:'POST',body:JSON.stringify(state)})}
click.onclick=()=>{state.clicks++;report()};
tiny.onclick=()=>{state.tiny++;report()};
click.oncontextmenu=e=>{e.preventDefault();state.context++;report()};
click.ondblclick=()=>{state.double++;report()};
let inputTimer;
edit.oninput=()=>{clearTimeout(inputTimer);inputTimer=setTimeout(report,30)};
wheel.onwheel=()=>{state.wheels++;setTimeout(report,100)};
let start=null;drag.onmousedown=e=>{start=[e.clientX,e.clientY]};
document.onmouseup=e=>{if(start&&Math.hypot(e.clientX-start[0],e.clientY-start[1])>20){state.drags++;report()}start=null};
report();
setInterval(report,250); // retry readback if an HTTP report was delayed/lost
</script>'''


class Page(BaseHTTPRequestHandler):
    state = {}
    lock = threading.Lock()

    def do_GET(self):
        body = HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with Page.lock:
            if value['sequence'] > Page.state.get('sequence', -1):
                Page.state = value
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


async def main():
    import win32process
    browser_name = 'chrome' if '--chrome' in sys.argv else 'edge'
    executable = 'Google/Chrome/Application/chrome.exe' if browser_name == 'chrome' else 'Microsoft/Edge/Application/msedge.exe'
    edge = next((Path(p) / executable for p in
        (os.environ.get('ProgramFiles(x86)', ''), os.environ.get('ProgramFiles', ''), os.environ.get('LOCALAPPDATA', ''))
        if (Path(p) / executable).is_file()), None)
    assert edge, f'{browser_name} is required'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report, child, sid = {'browser':browser_name}, None, None
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / 'clickwarden_mcp.py')],
        env={**os.environ, 'CLICKWARDEN_TEXT_ONLY': '1'})
    try:
        with tempfile.TemporaryDirectory(prefix='clickwarden-input-test-') as profile:
            try:
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()

                        async def call(name, **kwargs):
                            response = await client.call_tool(name, kwargs)
                            assert not getattr(response, 'is_error', getattr(response, 'isError', False)), response
                            assert all(c.type == 'text' for c in response.content)
                            value = json.loads(response.content[0].text)
                            if isinstance(value, dict):
                                assert value.get('status') not in ('error', 'paused', 'cancelled'), value
                            return value

                        async def observed(name):
                            for _ in range(3):
                                value = await call('fast_controls', name_filter=name, session_id=sid,
                                                   max_seconds=5, limit=250)
                                assert value.get('complete'), value
                                items = [c for c in value['controls'] if c['name'] == name]
                                if items:
                                    break
                                await asyncio.sleep(.4)
                            if not items:
                                debug = await call('fast_controls', session_id=sid, limit=250, max_seconds=5)
                                print(json.dumps({'fixture_controls':debug}), flush=True)
                            assert len(items) == 1, value
                            return value, items[0]

                        async def click_native(name, **options):
                            snapshot, item = await observed(name)
                            result = await call('fast_click_control', snapshot_id=snapshot['snapshot_id'],
                                                control_id=item['control_id'], session_id=sid, **options)
                            assert result['status'] == 'success', result
                            return item

                        async def proof(key, expected):
                            for _ in range(20):
                                if Page.state.get(key) == expected:
                                    return
                                await asyncio.sleep(.1)
                            raise AssertionError({'expected': {key: expected}, 'actual': Page.state})

                        try:
                            report['runtime'] = (await call('fast_capabilities'))['runtime_revision']
                            sid = (await call('use_my_computer', objective='Disposable mouse and keyboard acceptance',
                                              app_or_url='desktop'))['session_id']
                            for _ in range(20):
                                status = await call('fast_session_status', session_id=sid)
                                if status['status'] != 'preparing':
                                    break
                                await asyncio.sleep(.2)
                            assert status['status'] == 'idle', status
                            child = subprocess.Popen([str(edge), '--no-first-run', '--no-default-browser-check',
                                '--disable-background-mode', f'--user-data-dir={profile}', '--inprivate',
                                '--window-size=1000,850', '--window-position=160,140', '--new-window',
                                f'http://127.0.0.1:{server.server_port}/'], creationflags=subprocess.CREATE_NO_WINDOW)
                            hwnd = None
                            for _ in range(30):
                                windows = await call('list_windows')
                                hwnd = next((w['hwnd'] for w in windows if
                                    win32process.GetWindowThreadProcessId(w['hwnd'])[1] == child.pid), None)
                                if hwnd and Page.state:
                                    break
                                await asyncio.sleep(.3)
                            assert hwnd and Page.state, 'Local fixture did not open'
                            await call('fast_switch_window', hwnd=hwnd, session_id=sid)
                            await click_native('Record click')
                            await proof('clicks', 1)
                            report['native_click'] = True
                            await click_native('Record click', button='right')
                            await proof('context', 1)
                            await click_native('Record click', clicks=2)
                            await proof('double', 1)
                            await proof('clicks', 3)
                            report['right_and_double_click'] = True
                            for count in range(1,4):
                                await click_native('Tiny target')
                                await proof('tiny',count)
                            report['tiny_target_three_clicks'] = True
                            await click_native('Test editor')
                            text = 'Name — Türkçe 你好 😀\nsecond line'
                            await call('fast_type', text=text, session_id=sid)
                            await proof('text', text)
                            await call('fast_hotkey', keys='ctrl+a', session_id=sid)
                            await call('fast_type', text='replacement', session_id=sid)
                            await proof('text', 'replacement')
                            report['unicode_and_selection'] = True
                            await call('fast_hotkey', keys='home', session_id=sid)
                            await call('fast_hotkey', keys='delete', session_id=sid)
                            await proof('text', 'eplacement')
                            report['navigation_keys'] = True
                            snapshot, item = await observed('Drag test')
                            l,t,r,b = item['box']; x,y = int((l+r)/2),int((t+b)/2)
                            await call('fast_drag', start_x=x, start_y=y, end_x=x+90, end_y=y,
                                       session_id=sid)
                            await proof('drags', 1)
                            report['drag'] = True
                            snapshot, item = await observed('Scroll test')
                            l,t,r,b = item['box']; x,y = int((l+r)/2),int((t+b)/2)
                            await call('fast_scroll', clicks=-3, x=x, y=y, session_id=sid)
                            await asyncio.sleep(.3)
                            assert Page.state['wheels'] > 0 and Page.state['scroll'] > 0, Page.state
                            report['scroll'] = True
                            state = await call('fast_session_status', session_id=sid)
                            assert state['status'] == 'idle' and state['requests'] == 0, state
                            report.update(passed=True, vision_requests=0)
                        finally:
                            if sid:
                                await client.call_tool('fast_session_end', {'session_id':sid,
                                    'result':'completed' if report.get('passed') else 'failed'})
            finally:
                if child and child.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    child.wait(timeout=5)
                await asyncio.sleep(.5)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        report.update(passed=False, error=str(exc))
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
