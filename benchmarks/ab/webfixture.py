"""Local web fixture for the browser tasks. Serves a few small pages on 127.0.0.1 and records receipts.

A receipt is the server-side record of what actually happened (a page visit, a form submission, a button
click). Task success for web tasks is decided from these receipts, never from what the model says.
Nothing here leaves the machine.
"""
from __future__ import annotations

import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STYLE = """<style>
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1c1c1c}
header{background:#24364b;color:#fff;padding:14px 28px;font-size:20px}
nav a{color:#fff;margin-right:18px;text-decoration:none;font-size:15px}
main{padding:24px 28px;max-width:900px}
.card{background:#fff;border:1px solid #d9dde3;border-radius:6px;padding:16px 20px;margin:12px 0}
label{display:block;margin:12px 0 4px}
input[type=text],input[type=email],select{width:320px;padding:6px;font-size:15px}
button{font-size:15px;padding:7px 16px;cursor:pointer}
.toolbar{display:flex;gap:2px;background:#e9ecf0;padding:3px;border:1px solid #c8cdd4;width:max-content}
.toolbar button{width:22px;height:22px;padding:2px;border:1px solid transparent;background:none}
.toolbar button:hover{border-color:#8aa2bd;background:#fff}
.order{display:flex;justify-content:space-between;align-items:center;background:#fff;border-bottom:1px solid #e1e4e8;padding:10px 14px}
</style>"""

NAV = ('<nav><a href="/">Home</a><a href="/products">Products</a><a href="/pricing">Pricing</a>'
       '<a href="/contact">Contact</a></nav>')

REPORT_JS = """<script>
function report(target){fetch('/event',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({target:target})});}
</script>"""

COUNTRIES = ["Argentina", "Australia", "Austria", "Azerbaijan", "Belgium", "Brazil", "Canada", "Georgia",
             "Germany", "Japan", "Kazakhstan", "Turkey", "United Kingdom", "United States"]

# 20 small toolbar icons: (id, accessible label, svg path)
ICONS = [
    ("new", "New", "M3 2h7l3 3v9H3z"), ("open", "Open", "M1 4h5l1 2h8v8H1z"),
    ("save", "Save", "M2 2h10l2 2v10H2zM5 2v4h6V2"), ("print", "Print", "M4 1h8v4H4zM2 5h12v6H2zM4 9h8v6H4z"),
    ("cut", "Cut", "M4 12a2 2 0 1 0 1 0L12 2M12 12a2 2 0 1 1-1 0L4 2"), ("copy", "Copy", "M5 5h9v9H5zM2 2h9v2"),
    ("paste", "Paste", "M4 3h8v12H4zM6 1h4v3H6z"), ("undo", "Undo", "M4 6h6a4 4 0 0 1 0 8H6M4 6l3-3M4 6l3 3"),
    ("redo", "Redo", "M12 6H6a4 4 0 0 0 0 8h4M12 6L9 3M12 6L9 9"), ("bold", "Bold", "M4 2h5a3 3 0 0 1 0 6H4zM4 8h6a3 3 0 0 1 0 6H4z"),
    ("archive", "Archive", "M1 2h14v4H1zM2 6h12v8H2zM6 9h4"), ("delete", "Delete", "M3 4h10M6 4V2h4v2M4 4l1 10h6l1-10"),
    ("flag", "Flag", "M3 1v14M3 2h9l-2 3 2 3H3"), ("tag", "Tag", "M1 1h7l7 7-7 7-7-7zM4 4h1"),
    ("search", "Search", "M7 7m-5 0a5 5 0 1 0 10 0a5 5 0 1 0-10 0M11 11l4 4"), ("mail", "Mail", "M1 3h14v10H1zM1 3l7 6 7-6"),
    ("calendar", "Calendar", "M1 3h14v12H1zM1 6h14M4 1v4M12 1v4"), ("lock", "Lock", "M3 7h10v8H3zM5 7V4a3 3 0 0 1 6 0v3"),
    ("star", "Star", "M8 1l2 5h5l-4 3 2 6-5-4-5 4 2-6-4-3h5z"), ("settings", "Settings", "M8 5a3 3 0 1 0 0 6a3 3 0 1 0 0-6M8 1v3M8 12v3M1 8h3M12 8h3"),
]

ORDER_IDS = [5101 + 7 * i for i in range(60)]
ORDER_IDS[29] = 5512   # decoy that looks like the target
ORDER_IDS[11] = 5251   # another decoy
ORDER_IDS[56] = 5521   # the target, near the bottom of a long page
ITEMS = ["USB-C cable", "Desk lamp", "Notebook A5", "Wireless mouse", "Monitor stand", "HDMI adapter",
         "Webcam", "Keyboard", "Headphones", "Power bank"]


def _page(title: str, body: str, script: str = "") -> bytes:
    return ("<!doctype html><html><head><meta charset='utf-8'><title>%s - Bench Store</title>%s</head>"
            "<body><header>Bench Store &nbsp; %s</header><main>%s</main>%s</body></html>"
            % (html.escape(title), STYLE, NAV, body, script)).encode("utf-8")


def render(path: str) -> bytes | None:
    if path == "/":
        return _page("Home", "<h1>Welcome to Bench Store</h1><div class='card'>A small local test site. "
                             "Use the menu above to browse.</div>")
    if path == "/products":
        rows = "".join("<div class='card'>%s</div>" % html.escape(item) for item in ITEMS)
        return _page("Products", "<h1>Products</h1>" + rows)
    if path == "/pricing":
        plans = [("Basic plan", "/pricing/basic", "$4 per month"), ("Student plan", "/pricing/student", "$2 per month"),
                 ("Team plan", "/pricing/team", "$12 per seat")]
        cards = "".join("<div class='card'><b>%s</b> - %s<br><a href='%s'>See details</a></div>" % (n, p, u)
                        for n, u, p in plans)
        return _page("Pricing", "<h1>Pricing</h1>" + cards)
    if path.startswith("/pricing/") and path.rsplit("/", 1)[1] in ("basic", "student", "team"):
        name = path.rsplit("/", 1)[1].capitalize()
        return _page(name + " plan", "<h1>%s plan details</h1><div class='card'>Everything in the %s plan.</div>"
                     % (name, name.lower()))
    if path == "/contact":
        return _page("Contact", "<h1>Contact</h1><div class='card'>bench-store@example.com</div>")
    if path == "/form":
        options = "".join("<option>%s</option>" % c for c in COUNTRIES)
        return _page("Registration", "<h1>Registration</h1><form class='card' method='post' action='/form'>"
                     "<label for='name'>Full name</label><input type='text' id='name' name='name'>"
                     "<label for='email'>Email</label><input type='email' id='email' name='email'>"
                     "<label for='country'>Country</label><select id='country' name='country'>"
                     "<option value=''>Choose a country</option>%s</select>"
                     "<label><input type='checkbox' name='agree' value='yes'> I agree to the terms</label>"
                     "<p><button type='submit'>Submit</button></p></form>" % options)
    if path == "/toolbar":
        buttons = "".join(
            "<button aria-label='%s' title='%s' onclick=\"report('%s')\"><svg width='16' height='16' "
            "viewBox='0 0 16 16' fill='none' stroke='#33475b' stroke-width='1.3'><path d='%s'/></svg></button>"
            % (label, label, ident, d) for ident, label, d in ICONS)
        return _page("Mail", "<h1>Inbox</h1><div class='toolbar' role='toolbar' aria-label='Message actions'>%s</div>"
                     "<div class='card'><b>Quarterly report</b><br>From: finance@example.com<br>"
                     "The quarterly numbers are attached.</div>" % buttons, REPORT_JS)
    if path == "/orders":
        rows = "".join(
            "<div class='order'><span>Order #%d &middot; %s</span><button onclick=\"report('confirm-%d')\">"
            "Confirm order #%d</button></div>" % (oid, ITEMS[i % len(ITEMS)], oid, oid)
            for i, oid in enumerate(ORDER_IDS))
        return _page("Orders", "<h1>Pending orders</h1>" + rows, REPORT_JS)
    if path == "/redeem":
        return _page("Redeem", "<h1>Redeem an access code</h1><form class='card' method='post' action='/redeem'>"
                     "<label for='code'>Access code</label><input type='text' id='code' name='code'>"
                     "<p><button type='submit'>Redeem</button></p></form>")
    if path == "/thanks":
        return _page("Thank you", "<h1>Thank you</h1><div class='card'>Your submission was received.</div>")
    return None


class WebFixture:
    """In-process HTTP server plus the receipt log the checkers read."""

    def __init__(self):
        self._lock = threading.Lock()
        self._receipts: list[dict] = []
        self.server = None
        self.base_url = ""

    def record(self, kind: str, **data) -> None:
        with self._lock:
            self._receipts.append({"kind": kind, "t": time.time(), **data})

    def mark(self) -> int:
        with self._lock:
            return len(self._receipts)

    def since(self, mark: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._receipts[mark:]]

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8"):
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                body = render(path)
                if body is None:
                    self._send(404 if path != "/favicon.ico" else 204)
                    return
                fixture.record("visit", path=path)
                self._send(200, body)

            def do_POST(self):
                path = urlsplit(self.path).path
                length = min(int(self.headers.get("Content-Length", 0) or 0), 65536)
                raw = self.rfile.read(length) if length else b""
                if path == "/event":
                    try:
                        target = str(json.loads(raw or b"{}").get("target", ""))[:64]
                    except ValueError:
                        target = ""
                    fixture.record("click", target=target)
                    self._send(204)
                    return
                if path in ("/form", "/redeem"):
                    fields = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8", "replace"),
                                                            keep_blank_values=True).items()}
                    fixture.record("submit", path=path, fields=fields)
                    self.send_response(303)
                    self.send_header("Location", "/thanks")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="ab-webfixture").start()
        self.base_url = "http://%s:%d" % (host, self.server.server_port)
        return self.base_url

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
