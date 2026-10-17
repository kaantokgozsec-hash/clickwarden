"""Opt-in live control-panel callback test; never targets other applications."""
from pathlib import Path
import json
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clickwarden.control import Control
from clickwarden.worker import run_panel


class Fixture:
    def __init__(self):
        self.spec = {"goal": "Control-panel test — Pause, Resume, Cancel", "max_steps": 10, "max_requests": 10}
        self.control = Control(10)
        self.state = {"stage": "Testing operation controls", "status": "running"}
        self.done = threading.Event()
        self.watch = None

    def run(self):
        while not self.control.cancelled.wait(.05):
            if time.monotonic() >= self.control.deadline:
                self.control.cancel()
        self.state.update(stage="Cancelled", status="cancelled")
        self.done.set()

    def poll_commands(self):
        pass


def main():
    fixture = Fixture()
    report = {}
    def ready(root, buttons, hwnd):
        try:
            import win32gui
            from ctypes import windll, byref, wintypes
            report["compact_height"] = root.winfo_height()
            assert root.winfo_height() == 48
            assert root.overrideredirect()
            assert win32gui.GetWindowLong(hwnd, -20) & 0x08000000
            affinity = wintypes.DWORD()
            assert windll.user32.GetWindowDisplayAffinity(hwnd, byref(affinity)) and affinity.value == 0x11
            buttons["⌄"].invoke()
            root.update_idletasks()
            report["expanded_height"] = root.winfo_height()
            assert root.winfo_height() > 48
            buttons["Pause"].invoke()
            report["pause_button"] = fixture.control.paused.is_set()
            buttons["Resume"].invoke()
            report["resume_button"] = not fixture.control.paused.is_set()
            report["buttons_fit"] = all(b.winfo_rooty() + b.winfo_height() <= root.winfo_rooty() + root.winfo_height()
                                        for b in buttons.values())
            buttons["⌄"].invoke()
            root.update_idletasks()
            assert root.winfo_height() == 48
            buttons["Cancel"].invoke()
            report["cancel_button"] = fixture.control.cancelled.is_set()
        except Exception as exc:
            report["error"] = str(exc)
            fixture.control.cancel()
    run_panel(fixture, on_ready=ready)
    report["passed"] = all(report.get(k) for k in ("pause_button", "resume_button", "cancel_button", "buttons_fit"))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
