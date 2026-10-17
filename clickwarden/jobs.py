"""Durable, text-only job API. Screenshots never enter job records or results."""
import ctypes
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes

from .win32core import enumerate_windows, pid_of_window, window_class

TERMINAL = {"completed", "cancelled", "failed", "needs_attention", "budget_reached", "interrupted"}
JOB_ROOT = Path(os.environ.get("CLICKWARDEN_JOB_DIR", str(Path.home() / ".clickwarden" / "jobs")))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        # Windows readers/AV may briefly open the destination without DELETE
        # sharing. Keep the old complete JSON until atomic replacement succeeds.
        for attempt in range(10):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(.005 * (attempt + 1), .04))
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path, default=None):
    # Another process (MCP server <-> worker) may be replacing the file; antivirus
    # and indexers can also hold it briefly. Retry those transient conditions.
    for attempt in range(10):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {} if default is None else default
        except (PermissionError, json.JSONDecodeError):
            if attempt == 9:
                raise
            time.sleep(min(.005 * (attempt + 1), .04))


def process_stamp(pid: int):
    """Creation time prevents a reused PID from being treated as our worker."""
    k = ctypes.windll.kernel32
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k.CloseHandle(handle)


def target_identity(hwnd: int):
    from .win32core import is_control_surface
    window = next((w for w in enumerate_windows() if w["hwnd"] == hwnd), None)
    if not window or is_control_surface(window):
        raise ValueError("target_hwnd must identify a visible window from list_windows")
    return {"hwnd": hwnd, "pid": pid_of_window(hwnd), "class": window_class(hwnd),
            "title": window["title"], "process_stamp": process_stamp(pid_of_window(hwnd))}


def public_status(state):
    keys = ("job_id", "status", "stage", "steps", "max_steps", "requests", "max_requests",
            "elapsed_seconds", "prompt_tokens", "completion_tokens", "usage_reports",
            "summary", "error", "verification", "input_mode", "usage_complete")
    return {k: state[k] for k in keys if k in state}


class JobManager:
    def __init__(self, root: Path = JOB_ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.processes = {}

    def folder(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("invalid job ID")
        path = self.root / job_id
        if not (path / "spec.json").exists():
            raise ValueError("unknown job ID")
        return path

    def alive(self, folder):
        info = read_json(folder / "process.json")
        return bool(info.get("stamp") and process_stamp(info["pid"]) == info["stamp"])

    def status(self, job_id):
        with self.lock:
            folder = self.folder(job_id)
            state = read_json(folder / "state.json")
            if state.get("status") not in TERMINAL and not self.alive(folder):
                # A freshly spawned interpreter has a brief startup interval.
                if time.time() - state.get("updated_at", 0) > 5:
                    state.update(status="interrupted", error="Worker stopped; resume re-observes before acting")
                    atomic_json(folder / "state.json", state)
            return public_status(state)

    def list(self):
        if not self.root.exists():
            return []
        folders = sorted(self.root.glob("*/spec.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.status(p.parent.name) for p in folders[:20]]

    def active(self):
        return next((s for s in self.list() if s["status"] not in TERMINAL), None)

    def start(self, goal: str, target_hwnd: int, max_steps=60, max_seconds=1800,
              max_requests=120, completion_file="", expected_text=""):
        with self.lock:
            if self.active():
                raise RuntimeError("A job is already active; pause, cancel or finish it first")
            if not goal.strip() or len(goal) > 8000:
                raise ValueError("goal must contain 1–8000 characters")
            if not (1 <= max_steps <= 1000 and 1 <= max_requests <= 2000
                    and math.isfinite(max_seconds) and 1 <= max_seconds <= 86400):
                raise ValueError("invalid step, request or time budget")
            target = target_identity(int(target_hwnd))
            job_id = uuid.uuid4().hex
            folder = self.root / job_id
            completion = {}
            if completion_file:
                path = Path(completion_file).expanduser().resolve()
                before = path.stat() if path.exists() else None
                completion = {"path": str(path), "expected_text": expected_text,
                              "before_mtime_ns": before.st_mtime_ns if before else None}
            spec = {"job_id": job_id, "goal": goal, "target": target,
                    "max_steps": int(max_steps), "max_seconds": float(max_seconds),
                    "max_requests": int(max_requests), "completion": completion}
            atomic_json(folder / "spec.json", spec)
            atomic_json(folder / "state.json", {"job_id": job_id, "status": "starting",
                        "steps": 0, "requests": 0, "max_steps": max_steps,
                        "max_requests": max_requests, "updated_at": time.time(),
                        "input_mode": "shared desktop; pauses on physical input"})
            self._launch(folder)
            return self.status(job_id)

    def _launch(self, folder):
        atomic_json(folder / "command.json", {"command": "run", "id": uuid.uuid4().hex})
        with (folder / "worker.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "clickwarden.worker", str(folder)],
                cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.processes[folder.name] = process
        atomic_json(folder / "process.json", {"pid": process.pid, "stamp": process_stamp(process.pid)})

    def command(self, job_id, command):
        if command not in ("pause", "resume", "cancel"):
            raise ValueError("unknown command")
        with self.lock:
            folder = self.folder(job_id)
            status = self.status(job_id)
            if status["status"] in TERMINAL:
                return status
            atomic_json(folder / "command.json", {"command": command, "id": uuid.uuid4().hex})
            return {"job_id": job_id, "status": f"{command}_requested"}

    def resume(self, job_id, target_hwnd: int = 0):
        with self.lock:
            folder = self.folder(job_id)
            self.status(job_id)  # reconcile a crashed worker before reading its checkpoint
            state = read_json(folder / "state.json")
            if self.alive(folder):
                return self.command(job_id, "resume")
            if state.get("status") not in ("paused", "interrupted", "needs_attention"):
                raise ValueError("Only paused/interrupted/needs_attention jobs can resume")
            active = self.active()
            if active and active["job_id"] != job_id:
                raise RuntimeError("Another job is active")
            spec = read_json(folder / "spec.json")
            if target_hwnd:
                spec["target"] = target_identity(target_hwnd)
                atomic_json(folder / "spec.json", spec)
            state.update(status="starting", stage="Resuming from a fresh observation", updated_at=time.time())
            state.pop("error", None)
            atomic_json(folder / "state.json", state)
            self._launch(folder)
            return public_status(state)
