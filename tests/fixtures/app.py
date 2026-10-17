"""Disposable local fixture host: seeded HTML page + oracle state + focus stealer.

Importing this module has no side effects.  ``tests/live_baseline.py`` embeds a
``Fixture`` server in-process; the same file runs standalone for debugging:

    python tests/fixtures/app.py --serve --seed 1729
    python tests/fixtures/app.py --steal --seconds 6
"""

from __future__ import annotations

import argparse
import ctypes
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_ROOT))
    sys.path.insert(0, str(_ROOT / "tests"))
    from fixtures.layout import FIXTURE_VERSION, build_layout, layout_hash, render_page
else:
    from .layout import FIXTURE_VERSION, build_layout, layout_hash, render_page


class Fixture:
    """Serves one seeded fixture page and keeps the latest oracle state.

    The page POSTs its complete state after every recorded event; the server
    keeps the highest-sequence copy.  A live runner reads ``snapshot()``: that
    record of which element actually received input is the oracle.
    """

    def __init__(self, seed: int):
        self.spec = build_layout(seed)
        self.layout_hash = layout_hash(self.spec)
        self.page = render_page(self.spec)
        self._lock = threading.Lock()
        self._state = {"version": FIXTURE_VERSION, "seed": seed, "page_id": None,
                       "revision": -1, "sequence": -1, "counters": {}, "events": [],
                       "scroll": {}, "menu_open": False, "moved": False,
                       "rerendered": False, "dpr": None, "viewport": None}

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def merge(self, value) -> bool:
        """Accept a page report only when it is a new load or strictly newer.

        The page bumps ``revision`` for every state mutation (recorded events
        and async flags alike) and carries a per-load ``page_id``.  Stale or
        reordered HTTP deliveries must never roll fixture truth back.
        Returns whether the report was accepted.
        """
        if not isinstance(value, dict):
            return False
        with self._lock:
            current = self._state
            if value.get("page_id") != current.get("page_id"):
                self._state = value
                return True
            if value.get("revision", -1) > current.get("revision", -1):
                self._state = value
                return True
            return False

    def serve(self, host: str = "127.0.0.1", port: int = 0):
        fixture = self

        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body = fixture.page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    value = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    value = {}
                fixture.merge(value)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((host, port), Page)
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="clickwarden-fixture").start()
        return server, "http://%s:%d/" % (host, server.server_port)


def steal(seconds: float = 6.0, delay: float = 0.35,
          label: str = "Clickwarden focus stealer") -> dict:
    """Run a small window that deliberately takes the foreground, then exits.

    This is a real foreign window: the live runner uses it to prove Clickwarden
    refuses input after focus leaves the target instead of clicking into it.
    """
    import tkinter as tk
    from clickwarden.win32core import force_foreground

    root = tk.Tk()
    root.title(label)
    root.geometry("280x90+80+80")
    root.attributes("-topmost", True)
    tk.Label(root, text="Fixture focus stealer", font=("Segoe UI", 12)).pack(pady=18)
    result: dict = {}

    def grab():
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        hwnd = int(user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id())
        try:
            result.update(force_foreground(hwnd, title=label))
        except Exception as exc:  # pragma: no cover - live diagnostic
            result["error"] = str(exc)
        print("STEALER_READY hwnd=%d status=%s" % (hwnd, result.get("status", result.get("error"))),
              flush=True)
        root.after(max(100, int(seconds * 1000)), root.destroy)

    root.after(max(0, int(delay * 1000)), grab)
    root.mainloop()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Clickwarden disposable fixture host")
    parser.add_argument("--serve", action="store_true", help="serve the seeded page")
    parser.add_argument("--steal", action="store_true", help="run the focus-stealer window")
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args(argv)

    if args.steal:
        result = steal(seconds=args.seconds)
        return 0 if result.get("status") == "success" else 1
    if args.serve:
        fixture = Fixture(args.seed)
        server, url = fixture.serve(port=args.port)
        print(json.dumps({"url": url, "seed": args.seed,
                          "layout_hash": fixture.layout_hash}), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
