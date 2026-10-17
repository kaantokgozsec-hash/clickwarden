"""Opt-in subprocess/HUD/job integration test using a disposable Tk window.

The first job is cancelled during vision; the second must create a verified
test artifact. No existing application documents are touched.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import win32gui
from clickwarden.jobs import JobManager, TERMINAL
from clickwarden.win32core import force_foreground


def main():
    temp = tempfile.TemporaryDirectory(prefix="clickwarden-job-test-")
    work = Path(temp.name)
    manager = JobManager(work / "jobs")
    artifact = work / "result.txt"
    root = tk.Tk()
    root.title("Clickwarden disposable job test")
    root.geometry("600x350+180+230")
    root.attributes("-topmost", True)
    tk.Label(root, text="Clickwarden job test", font=("Arial", 24)).pack(pady=20)
    entry = tk.Entry(root, font=("Arial", 18))
    entry.pack(pady=12)
    result = tk.StringVar(value="Waiting")
    def verify():
        if entry.get() == "clickwarden job":
            artifact.write_text("clickwarden job verified", encoding="utf-8")
            result.set("PASS")
        else:
            result.set("FAIL")
    tk.Button(root, text="Verify", command=verify, font=("Arial", 18)).pack()
    tk.Label(root, textvariable=result, font=("Arial", 22)).pack(pady=15)
    root.update()
    hwnd = win32gui.GetAncestor(root.winfo_id(), 2)
    entry.focus_force()
    force_foreground(hwnd)
    report = {}
    phase = "cancel"
    job = None
    started = time.monotonic()
    cancel_at = None
    pause_requested = False
    resumed = False

    def start():
        nonlocal job
        job = manager.start("Type 'clickwarden job' into the text field, click Verify, and finish when PASS is shown.",
                            hwnd, max_steps=12, max_seconds=100, max_requests=18,
                            completion_file=str(artifact), expected_text="clickwarden job verified")["job_id"]
        root.after(100, poll)

    def poll():
        nonlocal phase, job, cancel_at, pause_requested, resumed
        try:
            status = manager.status(job)
            if time.monotonic() - started > 130:
                manager.command(job, "cancel")
                raise RuntimeError("live test timed out")
            if phase == "cancel" and status.get("stage") == "Vision is choosing the next action" and cancel_at is None:
                if not pause_requested:
                    manager.command(job, "pause")
                    pause_requested = True
                elif resumed:
                    cancel_at = time.monotonic()
                    manager.command(job, "cancel")
            if status["status"] == "paused":
                # Do not override real user takeover. Only recover focus from fixture startup.
                if status.get("stage") == "Paused by user" and pause_requested:
                    report["pause_no_input"] = entry.get() == ""
                    manager.resume(job)
                    resumed = True
                elif "Target is not foreground" in status.get("stage", ""):
                    force_foreground(hwnd)
                    manager.resume(job)
                else:
                    raise RuntimeError("Physical user input paused the live test; leave it paused")
            if status["status"] in TERMINAL:
                if phase == "cancel":
                    report["cancellation"] = status
                    report.setdefault("cancel_latency_ms", round((time.monotonic() - cancel_at) * 1000) if cancel_at else None)
                    report["cancel_no_input"] = entry.get() == "" and not artifact.exists()
                    if status["status"] != "cancelled" or not report["cancel_no_input"]:
                        raise RuntimeError("Cancellation did not stop the first job cleanly")
                    if manager.processes[job].poll() is None:
                        root.after(100, poll)
                        return
                    phase = "complete"
                    force_foreground(hwnd)
                    entry.focus_force()
                    start()
                    return
                report["completion"] = status
                report["actual_text"] = entry.get()
                report["passed"] = (status["status"] == "completed" and result.get() == "PASS"
                                    and artifact.read_text() == "clickwarden job verified")
                if manager.processes[job].poll() is None:
                    root.after(100, poll)
                    return
                root.destroy()
                return
            root.after(100, poll)
        except Exception as exc:
            report.update(passed=False, error=str(exc), job_id=job, directory=str(work))
            if job:
                manager.command(job, "cancel")
            root.destroy()

    root.after(300, start)
    root.mainloop()
    for process in manager.processes.values():
        try:
            process.wait(timeout=4)
        except Exception:
            pass
    if not report.get("passed"):
        for folder in (work / "jobs").glob("*"):
            log = folder / "worker.log"
            if log.exists():
                report.setdefault("worker_logs", []).append(log.read_text(errors="replace")[-2500:])
    print(json.dumps(report, indent=2))
    temp.cleanup()
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
