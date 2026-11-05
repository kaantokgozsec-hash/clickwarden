"""Task environment: build each task's starting state, check its end state, and put everything back.

Everything the benchmark creates lives in %USERPROFILE%\\ClickwardenBench (deleted and recreated per run).
Registry values a task touches are snapshotted when the Environment is created and restored after every
run, so the machine ends the benchmark the way it started. Teardown closes Notepad, File Explorer folder
windows, Settings and the benchmark's own browser windows, so close your own windows of those apps first
(the runner's preflight refuses to start while they are open).
"""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

BENCH_DIR_NAME = "ClickwardenBench"
SETUP_KEYS = {"files", "open", "registry"}
CHECK_TYPES = {"file_text", "absent", "dir_exists", "readonly", "registry", "web_visit", "web_submit",
               "web_click", "all"}
OPEN_KINDS = {"notepad", "explorer", "browser"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def bench_root() -> Path:
    return Path.home() / BENCH_DIR_NAME


# ---------------------------------------------------------------------------
# Task list
# ---------------------------------------------------------------------------

def load_tasks(path: str | os.PathLike) -> list[dict]:
    """Load and validate tasks.yaml. Raises ValueError on anything the runner could not execute."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks.yaml has no tasks")
    seen = set()
    for task in tasks:
        tid = task.get("id")
        if not tid or tid in seen:
            raise ValueError(f"missing or duplicate task id: {tid!r}")
        seen.add(tid)
        for key in ("prompt", "check", "difficulty", "category"):
            if not task.get(key):
                raise ValueError(f"{tid}: missing {key}")
        setup = task.get("setup") or {}
        unknown = set(setup) - SETUP_KEYS
        if unknown:
            raise ValueError(f"{tid}: unknown setup keys {sorted(unknown)}")
        for item in setup.get("open") or []:
            if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in OPEN_KINDS:
                raise ValueError(f"{tid}: bad open entry {item!r}")
        _validate_check(tid, task["check"])
    return tasks


def _validate_check(tid: str, check: dict) -> None:
    if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
        raise ValueError(f"{tid}: unknown check {check!r}")
    if check["type"] == "all":
        if not check.get("checks"):
            raise ValueError(f"{tid}: 'all' check needs checks")
        for sub in check["checks"]:
            _validate_check(tid, sub)


def registry_entries(tasks: list[dict]) -> list[tuple[str, str]]:
    out = []
    for task in tasks:
        for reg in (task.get("setup") or {}).get("registry") or []:
            pair = (reg["key"], reg["name"])
            if pair not in out:
                out.append(pair)
    return out


# ---------------------------------------------------------------------------
# Windows helpers (only called during live runs)
# ---------------------------------------------------------------------------

def _windows(class_name: str | None = None) -> list[tuple[int, str, str]]:
    import win32gui
    found = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            if class_name is None or cls == class_name:
                found.append((hwnd, cls, win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def _close_windows(class_name: str, title_filter=None) -> int:
    import win32con
    import win32gui
    count = 0
    for hwnd, _, title in _windows(class_name):
        if title_filter is None or title_filter(title):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            count += 1
    return count


def _process_running(image: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture_output=True,
                         text=True, creationflags=NO_WINDOW).stdout
    return image.lower() in out.lower()


def _kill_image(image: str) -> None:
    subprocess.run(["taskkill", "/IM", image, "/F", "/T"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def broadcast_setting_change() -> None:
    """Tell Explorer and apps that theme/taskbar settings changed (WM_SETTINGCHANGE)."""
    user32 = ctypes.windll.user32
    result = ctypes.c_size_t()
    for area in ("ImmersiveColorSet", "TraySettings"):
        user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, ctypes.c_wchar_p(area), 0x0002, 1500, ctypes.byref(result))


def conflicts() -> list[str]:
    """Windows the teardown would close; the runner refuses to start while any are open."""
    problems = []
    if _process_running("notepad.exe"):
        problems.append("Notepad is running (the benchmark closes every Notepad window between runs)")
    folders = [t for _, _, t in _windows("CabinetWClass")]
    if folders:
        problems.append("File Explorer folder windows are open: " + ", ".join(folders[:5]))
    if _process_running("SystemSettings.exe"):
        problems.append("Windows Settings is open")
    return problems


def browser_path() -> Path | None:
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        path = Path(base) / "Microsoft/Edge/Application/msedge.exe"
        if base and path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def reg_read(key: str, name: str):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except FileNotFoundError:
        return None


def reg_write(key: str, name: str, value) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
        if value is None:
            try:
                winreg.DeleteValue(handle, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(handle, name, 0, winreg.REG_DWORD, int(value))


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class Environment:
    def __init__(self, tasks: list[dict], web=None, root: Path | None = None, live: bool = True,
                 settle_s: float = 1.5):
        self.root = Path(root) if root else bench_root()
        if self.root.name != BENCH_DIR_NAME:
            raise ValueError(f"refusing to manage a folder not named {BENCH_DIR_NAME}: {self.root}")
        self.web = web
        self.live = live
        self.settle_s = settle_s
        self.web_mark = 0
        self._procs: list[subprocess.Popen] = []
        self._profiles: list[str] = []
        # Snapshot every registry value any task touches, so teardown always restores the user's own values.
        self.registry_original = {pair: reg_read(*pair) for pair in registry_entries(tasks)} if live else {}

    # -- prompt ---------------------------------------------------------------
    def prompt(self, task: dict) -> str:
        return " ".join(task["prompt"].split()).replace("{bench}", str(self.root)).replace(
            "{web}", self.web.base_url if self.web else "")

    # -- setup ----------------------------------------------------------------
    def setup(self, task: dict) -> None:
        self.teardown()
        setup = task.get("setup") or {}
        self.root.mkdir(parents=True, exist_ok=True)
        for rel, content in (setup.get("files") or {}).items():
            target = self.root / rel
            if rel.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content or "", encoding="utf-8")
        if self.live:
            for reg in setup.get("registry") or []:
                reg_write(reg["key"], reg["name"], reg["value"])
            if setup.get("registry"):
                broadcast_setting_change()
        self.web_mark = self.web.mark() if self.web else 0
        if self.live:
            for item in setup.get("open") or []:
                kind, arg = next(iter(item.items()))
                getattr(self, "_open_" + kind)(arg)
            time.sleep(self.settle_s)

    def _open_notepad(self, rel: str) -> None:
        subprocess.Popen(["notepad.exe", str(self.root / rel)], creationflags=NO_WINDOW)
        self._wait(lambda: any("notepad" in c.lower() or Path(rel).name in t
                               for _, c, t in _windows()), "Notepad window")

    def _open_explorer(self, rel: str) -> None:
        folder = self.root / rel
        subprocess.Popen(["explorer.exe", str(folder)])
        self._wait(lambda: any(folder.name in t for _, _, t in _windows("CabinetWClass")), "Explorer window")

    def _open_browser(self, path: str) -> None:
        exe = browser_path()
        if not exe or not self.web:
            raise RuntimeError("Microsoft Edge and the web fixture are required for browser tasks")
        profile = tempfile.mkdtemp(prefix="cw-ab-edge-")
        self._profiles.append(profile)
        mark = self.web.mark()
        self._procs.append(subprocess.Popen(
            [str(exe), "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
             "--disable-sync", f"--user-data-dir={profile}", "--inprivate", "--start-maximized",
             "--new-window", self.web.base_url + path], creationflags=NO_WINDOW))
        self._wait(lambda: any(r["kind"] == "visit" and r["path"] == path for r in self.web.since(mark)),
                   "browser page load", timeout=20)
        self.web_mark = self.web.mark()  # the page load itself is not a task receipt

    @staticmethod
    def _wait(predicate, what: str, timeout: float = 12.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.25)
        raise RuntimeError(f"setup timed out waiting for {what}")

    # -- check ----------------------------------------------------------------
    def check(self, task: dict) -> tuple[bool, str]:
        return self._check(task["check"])

    def _check(self, check: dict) -> tuple[bool, str]:
        kind = check["type"]
        if kind == "all":
            for sub in check["checks"]:
                ok, reason = self._check(sub)
                if not ok:
                    return False, reason
            return True, "all checks passed"
        if kind == "file_text":
            path = self.root / check["path"]
            if not path.is_file():
                return False, f"{check['path']} does not exist"
            text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
            if text != check["equals"]:
                return False, f"{check['path']} contains {text[:60]!r}"
            return True, "file content matches"
        if kind == "absent":
            path = self.root / check["path"]
            return (not path.exists(), f"{check['path']} still exists" if path.exists() else "absent")
        if kind == "dir_exists":
            path = self.root / check["path"]
            return (path.is_dir(), "folder exists" if path.is_dir() else f"{check['path']} folder not found")
        if kind == "readonly":
            path = self.root / check["path"]
            if not path.exists():
                return False, f"{check['path']} does not exist"
            readonly = not os.stat(path).st_mode & stat.S_IWRITE
            ok = readonly == bool(check["equals"])
            return ok, f"{check['path']} read-only={readonly}"
        if kind == "registry":
            value = reg_read(check["key"], check["name"])
            ok = value == check["equals"]
            return ok, f"{check['name']}={value}"
        receipts = self.web.since(self.web_mark) if self.web else []
        if kind == "web_visit":
            ok = any(r["kind"] == "visit" and r["path"] == check["path"] for r in receipts)
            return ok, ("visited " + check["path"]) if ok else f"{check['path']} never loaded"
        if kind == "web_submit":
            submits = [r for r in receipts if r["kind"] == "submit" and r["path"] == check["path"]]
            if not submits:
                return False, f"no submission to {check['path']}"
            if len(submits) > 1:
                return False, f"{len(submits)} submissions to {check['path']} (expected exactly one)"
            fields = submits[0]["fields"]
            wrong = {k: fields.get(k) for k, v in check["fields"].items() if (fields.get(k) or "").strip() != v}
            return (not wrong, "submitted fields match" if not wrong else f"wrong fields: {wrong}")
        if kind == "web_click":
            clicks = [r["target"] for r in receipts if r["kind"] == "click"]
            if clicks == [check["target"]]:
                return True, "intended button clicked once"
            return False, f"clicks received: {clicks or 'none'}"
        raise ValueError(f"unknown check type {kind}")

    # -- teardown -------------------------------------------------------------
    def teardown(self) -> None:
        if self.live:
            for proc in self._procs:
                if proc.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            _kill_image("notepad.exe")
            _kill_image("SystemSettings.exe")
            _close_windows("#32770", lambda t: t.endswith(" Properties"))
            _close_windows("CabinetWClass")
            restored = False
            for (key, name), value in self.registry_original.items():
                if reg_read(key, name) != value:
                    reg_write(key, name, value)
                    restored = True
            if restored:
                broadcast_setting_change()
            time.sleep(0.8)
        self._procs.clear()
        for profile in self._profiles:
            shutil.rmtree(profile, ignore_errors=True)
        self._profiles.clear()
        self._remove_root()

    def _remove_root(self) -> None:
        if self.root.name != BENCH_DIR_NAME or not self.root.exists():
            return

        def make_writable(func, path, *_):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        hook = {"onexc": make_writable} if sys.version_info >= (3, 12) else {"onerror": make_writable}
        for attempt in range(5):
            try:
                shutil.rmtree(self.root, **hook)
                return
            except OSError:
                time.sleep(0.5 * (attempt + 1))  # Explorer can hold a handle for a moment after closing
        shutil.rmtree(self.root, ignore_errors=True)
