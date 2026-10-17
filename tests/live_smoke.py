"""Opt-in live smoke test in a disposable window owned by this process.

Run: python tests/live_smoke.py
Uses the configured vision provider, sends only the test window's pixels,
and closes its window on completion. No existing documents are edited.
"""
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clickwarden.diagnostics import diagnose
from clickwarden.engine import Engine
from clickwarden.frames import capture_hwnd
from clickwarden.win32core import enumerate_windows, force_foreground, cursor_pos


def main():
    preflight = diagnose()
    if preflight["status"] != "ready":
        print(json.dumps(preflight))
        return 2
    root = tk.Tk()
    root.title("clickwarden isolated smoke test")
    root.geometry("640x360+200+200")
    root.attributes("-topmost", True)
    tk.Label(root, text="clickwarden test window", font=("Arial", 24)).pack(pady=15)
    tk.Label(root, text="Type the requested text into the field, then click Verify.").pack()
    entry = tk.Entry(root, font=("Arial", 18), width=32)
    entry.pack(pady=15)
    status = tk.StringVar(value="Waiting")
    click_count = tk.IntVar(value=0)
    expected = "clickwarden smoke"
    def verify():
        click_count.set(click_count.get() + 1)
        status.set("PASS" if entry.get() == expected else "FAIL")
    tk.Button(root, text="Verify", font=("Arial", 20),
              command=verify).pack()
    tk.Label(root, textvariable=status, font=("Arial", 24)).pack(pady=10)
    results = queue.Queue()
    report = {}
    original_cursor = cursor_pos()

    def run():
        engine = None
        try:
            hwnd = next(w["hwnd"] for w in enumerate_windows()
                        if w["title"] == "clickwarden isolated smoke test")
            if force_foreground(hwnd)["status"] != "success":
                raise RuntimeError("could not focus the test window")

            class TestEngine(Engine):
                def grab(self, *args, **kwargs):
                    captured = capture_hwnd(hwnd, pad=0, label="isolated smoke test")
                    if captured is None:
                        raise RuntimeError("test window unavailable")
                    return captured

            engine = TestEngine(motion="instant")
            # Exercise actual Unicode input, then verify it in the application.
            engine.type("A\U0001f600", expect=hwnd)
            engine.wait(0.2)  # SendInput queues events; let the GUI consume them.
            unicode_ok = entry.get() == "A\U0001f600"
            if not unicode_ok:
                raise RuntimeError(f"Unicode input mismatch: {entry.get()!r}")
            engine.hotkey("ctrl,a", expect=hwnd)
            engine.hotkey("backspace", expect=hwnd)
            engine.wait(0.15)
            result = engine.task(
                "In this test window, type 'clickwarden smoke' in the text field and click Verify. "
                "Finish only when PASS is displayed.", max_steps=6, verify=True,
            )
            report.update(unicode_roundtrip=unicode_ok, task=result,
                          actual_text=entry.get(), actual_status=status.get())
            before_clicks = click_count.get()
            click_result = engine.smart_click("the Verify button", verify=True, expect_hwnd=hwnd)
            engine.wait(0.15)
            report.update(smart_click=click_result, click_delivered=click_count.get() == before_clicks + 1)
            report["passed"] = (result["status"] == "completed" and status.get() == "PASS"
                                and click_result["status"] == "success" and report["click_delivered"])
        except Exception as exc:
            report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            if engine:
                try:
                    engine.move(*original_cursor)
                except Exception:
                    pass
            results.put(True)

    def poll():
        if not results.empty():
            root.destroy()
        else:
            root.after(100, poll)

    root.update()
    entry.focus_force()
    root.after(300, lambda: threading.Thread(target=run, daemon=True).start())
    root.after(100, poll)
    root.mainloop()
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
