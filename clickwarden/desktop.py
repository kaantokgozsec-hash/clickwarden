"""Native desktop inventory and verified layout transactions. No vision or mouse.

Coordinates are IFolderView coordinates, never inferred screenshot coordinates.
COM objects stay on the calling thread and are released before CoUninitialize.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

from .jobs import atomic_json, read_json


class DesktopView:
    def __init__(self):
        import win32com.client
        import pythoncom
        from win32com.shell import shell
        self.dispatch = win32com.client.Dispatch("Shell.Application").Windows().FindWindowSW(0, 0, 8, 0, 1)
        self.browser = self.dispatch._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider).QueryService(
            shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
        self.view = self.browser.QueryActiveShellView()
        self.folder_view = self.view.QueryInterface(shell.IID_IFolderView)
        self.folder = self.folder_view.GetFolder(shell.IID_IShellFolder)
        self.hwnd = self.view.GetWindow()
        self.pidls = {}

    def snapshot(self):
        import win32gui
        from .jobs import process_stamp
        from .win32core import pid_of_window
        f = self.folder_view
        items = []
        self.pidls = {}
        for index in range(f.ItemCount(2)):  # SVGIO_ALLVIEW
            pidl = f.Item(index)
            # IFolderView exposes a child ITEMID; IShellFolder expects a PIDL sequence.
            parsing = self.folder.GetDisplayNameOf([pidl], 0x8000)
            identity = hashlib.sha256(parsing.encode("utf-8")).hexdigest()[:24]
            if identity in self.pidls:
                raise RuntimeError("Duplicate shell identity; refusing ambiguous layout")
            self.pidls[identity] = pidl
            items.append({"id": identity, "name": self.folder.GetDisplayNameOf([pidl], 0),
                          "position": list(f.GetItemPosition(pidl))})
        spacing = list(f.GetSpacing(0, 0))
        pid = pid_of_window(self.hwnd)
        data = {"coordinate_space": "shell_view", "hwnd": self.hwnd,
                "process_stamp": process_stamp(pid), "bounds": list(win32gui.GetClientRect(self.hwnd)),
                "auto_arrange": bool(f.GetAutoArrange()), "spacing": spacing, "items": items,
                "count": len(items), "source": "IFolderView"}
        data["revision"] = revision(data)
        return data

    def position(self, identity, point):
        # pywin32's plural wrapper is unimplemented. Its singular wrapper invokes
        # the supported SelectAndPositionItems COM API, one item per cancellation gate.
        self.folder_view.SelectAndPositionItem(self.pidls[identity], tuple(point), 0x80)  # SVSI_POSITIONITEM

    def close(self):
        self.folder = self.folder_view = self.view = self.browser = self.dispatch = None


@contextmanager
def connect():
    import pythoncom
    pythoncom.CoInitialize()
    view = None
    try:
        view = DesktopView()
        yield view
    finally:
        if view:
            view.close()
        pythoncom.CoUninitialize()


def revision(snapshot):
    value = {k: snapshot[k] for k in ("hwnd", "process_stamp", "bounds", "spacing", "auto_arrange")}
    value["items"] = sorted((i["id"], i["position"]) for i in snapshot["items"])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def plan_layout(snapshot, shape="C", item_ids=None, bounds=None):
    """Build a deterministic, spaced layout in native view coordinates."""
    if snapshot["auto_arrange"]:
        raise ValueError("Auto Arrange is enabled; disable it explicitly before positioning icons")
    items = snapshot["items"]
    if item_ids is not None:
        if not item_ids or len(set(item_ids)) != len(item_ids):
            raise ValueError("item_ids must be nonempty and unique")
        by_id = {i["id"]: i for i in items}
        if any(i not in by_id for i in item_ids):
            raise ValueError("An item no longer exists")
        items = [by_id[i] for i in item_ids]
    if not items:
        raise ValueError("No desktop items to arrange")
    shape = shape.upper()
    if shape not in ("C", "U", "GRID"):
        raise ValueError("shape must be C, U or grid")
    l, t, r, b = bounds or snapshot["bounds"]
    sl, st, sr, sb = snapshot["bounds"]
    if not (sl <= l < r <= sr and st <= t < b <= sb):
        raise ValueError("Layout bounds must stay inside the desktop view")
    sx, sy = snapshot["spacing"]
    if sx <= 0 or sy <= 0:
        raise ValueError("Shell returned invalid spacing")
    # Existing native anchor preserves shell grid phase; never fit pitch from vision.
    anchor = min(snapshot["items"], key=lambda i: (i["position"][1], i["position"][0]))["position"]
    x0 = l + (anchor[0] - l) % sx
    y0 = t + (anchor[1] - t) % sy
    cols = int((r - sx - x0) // sx) + 1
    rows = int((b - sy - y0) // sy) + 1
    if cols < 2 or rows < 2:
        raise ValueError("Desktop bounds are too small")
    if shape == "C":
        slots = [(x,0) for x in range(cols-1,-1,-1)] + [(0,y) for y in range(1,rows)] + [(x,rows-1) for x in range(1,cols)]
    elif shape == "U":
        slots = [(0,y) for y in range(rows)] + [(x,rows-1) for x in range(1,cols)] + [(cols-1,y) for y in range(rows-2,-1,-1)]
    else:
        slots = [(x,y) for y in range(rows) for x in range(cols)]
    if len(items) > len(slots):
        raise ValueError(f"Layout has room for {len(slots)} items, but {len(items)} were requested")
    if shape != "GRID" and len(items) > 1:
        slots = [slots[round(i*(len(slots)-1)/(len(items)-1))] for i in range(len(items))]
    else:
        slots = slots[:len(items)]
    ordered = sorted(items, key=lambda i: (i["name"].casefold(), i["id"]))
    moves = [{"id": item["id"], "before": item["position"], "after": [int(x0+x*sx), int(y0+y*sy)]}
             for item,(x,y) in zip(ordered,slots)]
    selected = {i["id"] for i in items}
    for move in moves:
        for other in snapshot["items"]:
            if other["id"] not in selected and abs(move["after"][0]-other["position"][0]) < sx and abs(move["after"][1]-other["position"][1]) < sy:
                raise ValueError("Layout would overlap an unselected item; choose different bounds")
    return {"shape":shape, "revision":snapshot["revision"], "moves":moves, "coordinate_space":"shell_view"}


class LayoutTransactions:
    def __init__(self, root=None, connector=connect):
        self.root = Path(root or Path.home() / ".clickwarden"/"layouts")
        self.connector = connector

    def inventory(self):
        with self.connector() as view:
            return view.snapshot()

    def arrange(self, shape, check, item_ids=None, bounds=None, dry_run=False):
        with self.connector() as view:
            check()
            before = view.snapshot()
            plan = plan_layout(before, shape, item_ids, bounds)
            if dry_run:
                return {"status":"planned", **plan, "count":len(plan["moves"])}
            return self.apply(view, before, plan, check)

    def apply(self, view, before, plan, check):
        if before["auto_arrange"]:
            raise ValueError("Auto Arrange must be disabled before positioning")
        moving={m["id"] for m in plan["moves"]}
        sx,sy=before["spacing"]
        for move in plan["moves"]:
            for other in before["items"]:
                if other["id"] not in moving and abs(move["after"][0]-other["position"][0])<sx and abs(move["after"][1]-other["position"][1])<sy:
                    raise ValueError("A target position is occupied by an item outside this transaction")
        # All identities/geometry must still match immediately before the transaction.
        if view.snapshot()["revision"] != before["revision"]:
            raise RuntimeError("Desktop changed during planning; observe again")
        transaction_id = uuid.uuid4().hex
        path = self.root/(transaction_id+".json")
        record = {"id":transaction_id, "status":"prepared", "before":before, "plan":plan,
                  "applied":[], "created_at":time.time()}
        atomic_json(path,record)  # recovery information must exist BEFORE any changes
        error = None
        try:
            for move in plan["moves"]:
                check()
                record["pending"] = move["id"]
                atomic_json(path,record)
                view.position(move["id"], move["after"])
                record["applied"].append(move["id"])
            record.pop("pending",None)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        # Read-only reconciliation still runs after cancellation; never auto-undo
        # against a human who just took control of the desktop.
        try:
            after = view.snapshot()
            actual = {i["id"]:i["position"] for i in after["items"]}
            verified = [m["id"] for m in plan["moves"] if actual.get(m["id"]) == m["after"]]
            unchanged = [m["id"] for m in plan["moves"] if actual.get(m["id"]) == m["before"] and m["before"] != m["after"]]
            other = [m["id"] for m in plan["moves"] if m["id"] not in verified and m["id"] not in unchanged]
            selected = {m["id"] for m in plan["moves"]}
            collateral = [i["id"] for i in before["items"] if i["id"] not in selected and actual.get(i["id"]) != i["position"]]
            collateral += list(set(actual)-{i["id"] for i in before["items"]})
            record.update(after=after, verified=verified, unchanged=unchanged, unverified=other, collateral=collateral)
            status = "verified" if len(verified)==len(plan["moves"]) and not error and not collateral else "needs_attention"
        except Exception as exc:
            status = "unverified"
            record["verification_error"] = str(exc)
            record["unverified"] = [m["id"] for m in plan["moves"]]
        record.update(status=status,error=error)
        atomic_json(path,record)
        return {"status":status,"transaction_id":transaction_id,"requested":len(plan["moves"]),
                "verified":len(record.get("verified",[])),"unchanged":len(record.get("unchanged",[])),
                "unverified":len(record.get("unverified",[])),"collateral_changes":len(record.get("collateral",[])),
                "error":error or record.get("verification_error"),
                "verification":"native_position_readback" if "after" in record else "unavailable",
                "undo_available":"after" in record}

    def undo(self, transaction_id, check):
        if len(transaction_id)!=32 or any(c not in "0123456789abcdef" for c in transaction_id):
            raise ValueError("Invalid transaction ID")
        record = read_json(self.root/(transaction_id+".json"))
        if not record or "after" not in record:
            raise ValueError("No reconciled transaction available for safe undo")
        with self.connector() as view:
            check()
            current = view.snapshot()
            if current["revision"] != record["after"]["revision"]:
                raise ValueError("Desktop changed since this transaction; undo refused to preserve newer work")
            originals={i["id"]:i["position"] for i in record["before"]["items"]}
            moves=[{"id":i["id"],"before":i["position"],"after":originals[i["id"]]}
                   for i in current["items"] if i["id"] in originals and originals[i["id"]]!=i["position"]]
            return self.apply(view,current,{"shape":"undo","revision":current["revision"],"moves":moves},check)
