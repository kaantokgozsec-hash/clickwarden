"""Process-family membership and point-level desktop input checks."""
import ctypes
from ctypes import wintypes

from .win32core import user32, kernel32, pid_of_window, is_cloaked
from .jobs import process_stamp


def process_parents():
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("Cannot enumerate target process family")
    result = {}
    try:
        entry = Entry(size=ctypes.sizeof(Entry))
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            result[entry.pid] = (entry.parent, entry.exe)
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return result


class TargetFamily:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.pid = pid_of_window(hwnd)
        self.stamp = process_stamp(self.pid)
        if not self.pid or not self.stamp:
            raise ValueError("Target process is unavailable")
        self.members = {self.pid: self.stamp}
        self.exe = process_parents().get(self.pid, (0, ""))[1].lower()
        # Steam UI windows can belong to sibling web-helper processes. Promote
        # only this known application tree, never an arbitrary launcher parent.
        if self.exe == "steamwebhelper.exe":
            tree = process_parents()
            parent = self.pid
            for _ in range(16):
                parent = tree.get(parent, (0, ""))[0]
                if not parent:
                    break
                if tree.get(parent, (0, ""))[1].lower() == "steam.exe":
                    self.pid, self.stamp, self.exe = parent, process_stamp(parent), "steam.exe"
                    self.refresh()
                    break

    def refresh(self):
        if process_stamp(self.pid) != self.stamp:
            raise RuntimeError("Target process exited; start a new session")
        tree = process_parents()
        # Never adopt a shared launcher/browser parent: only root + descendants.
        members = {self.pid: self.stamp}
        for pid in tree:
            current, seen = pid, set()
            while current in tree and current not in seen:
                if current == self.pid:
                    stamp = process_stamp(pid)
                    if stamp and stamp >= self.stamp:
                        members[pid] = stamp
                    break
                seen.add(current)
                current = tree[current][0]
        self.members = members

    def contains(self, hwnd):
        pid = pid_of_window(hwnd)
        return bool(pid in self.members and process_stamp(pid) == self.members[pid])


def root_window(hwnd):
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    return int(user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT


def assert_unoccluded(x, y, expected_hwnd, family=None, forbidden=()):
    """Check the actual window receiving the button, including child controls."""
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    hit = int(user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)
    allowed = hit and root_window(hit) not in forbidden and (family.contains(hit) if family else root_window(hit) == root_window(expected_hwnd))
    if not allowed:
        from .input import ForegroundGuardError
        raise ForegroundGuardError(f"Click blocked: foreign window {root_window(hit) if hit else 0} covers ({x}, {y})")


def mask_foreign_windows(frame, target_hwnd, family):
    """Hide visible foreign-window rectangles above the target in the image.

    This does not reveal covered target pixels. Point checks still refuse input
    there. Do not tamper with another app's display-affinity or visibility.
    """
    import io
    import math
    from dataclasses import replace
    from PIL import Image, ImageDraw
    from .win32core import window_rect
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    boxes = []
    seen = set()
    hwnd = int(user32.GetWindow(target_hwnd, 3) or 0)  # GW_HWNDPREV: above target
    while hwnd and hwnd not in seen:
        seen.add(hwnd)
        if (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not is_cloaked(hwnd)
                and not family.contains(hwnd)):
            affinity = wintypes.DWORD()
            excluded = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)) and affinity.value == 0x11
            if not excluded:
                l, t, r, b = window_rect(hwnd)
                l, t = max(l, frame.left), max(t, frame.top)
                r, b = min(r, frame.left+frame.screen_w), min(b, frame.top+frame.screen_h)
                if r > l and b > t:
                    boxes.append((math.floor((l-frame.left)/frame.scale_x), math.floor((t-frame.top)/frame.scale_y),
                                  math.ceil((r-frame.left)/frame.scale_x), math.ceil((b-frame.top)/frame.scale_y)))
        hwnd = int(user32.GetWindow(hwnd, 3) or 0)
    if not boxes:
        return frame
    image = Image.open(io.BytesIO(frame.jpeg)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill="#242424")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=frame.quality)
    return replace(frame, jpeg=output.getvalue(), _thumb=None)
