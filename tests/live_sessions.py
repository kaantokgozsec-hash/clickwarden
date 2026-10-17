"""Opt-in actual panel/session test using only an owned disposable window."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clickwarden.sessions import SessionManager
from clickwarden.win32core import enumerate_windows, force_foreground, user32


def main():
    root = tk.Tk()
    root.title("clickwarden session smoke")
    root.geometry("660x380+140+150")
    root.attributes("-topmost",True)
    value = tk.StringVar(value="Waiting for Verify")
    tk.Label(root,text="SESSION TEST",font=("Arial",24)).pack(pady=30)
    button = tk.Button(root,text="Verify",font=("Arial",24), command=lambda:value.set("PASS — Verified"))
    button.pack(pady=20)
    tk.Label(root,textvariable=value,font=("Arial",20)).pack()
    report={}
    manager=SessionManager()
    def run():
        try:
            target=next(w["hwnd"] for w in enumerate_windows() if w["title"]=="clickwarden session smoke")
            force_foreground(target)
            def launch(name):
                session=manager.current
                report["panel_before_launch"]=bool(session.ready.is_set() and user32.IsWindowVisible(session.panel_hwnd))
                assert report["panel_before_launch"]
                force_foreground(target)
            t=time.monotonic()
            with patch("clickwarden.sessions.launch_target",side_effect=launch):
                started=manager.start("Click Verify and confirm PASS", "clickwarden session smoke")
                report["entry_ms"]=round((time.monotonic()-t)*1000)
                session=manager.get(started["session_id"])
                assert session.prepared.wait(30), "Preparation timed out"
            report["startup"]=session.snapshot()
            assert not session.error, session.error
            report["ready_ms"]=session.metrics()["startup_ms"]
            assert session.client.total_requests == 0, "Startup must not make a model call"
            affinity=wintypes.DWORD()
            user32.GetWindowDisplayAffinity.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
            assert user32.GetWindowDisplayAffinity(session.panel_hwnd,ctypes.byref(affinity))
            report["capture_affinity"]=affinity.value
            assert affinity.value==0x11
            session.cancel()
            time.sleep(.15)
            report["cancel_keeps_panel"]=bool(user32.IsWindowVisible(session.panel_hwnd) and not session.closed.is_set())
            assert report["cancel_keeps_panel"]
            force_foreground(target)
            result=session.run("smart_click",{"description":"the Verify button", "verify":True})
            report["click"]=result
            time.sleep(.2)
            report["actual_result"]=value.get()
            assert report["actual_result"]=="PASS — Verified", report
            session.pause()
            observation=session.run("inspect",{"query":"Does the test window say PASS? Reply PASS or FAIL."})
            report["paused_observation"]={"success":observation.get("success"),"answer":observation.get("answer"),
                                          "observation_only":observation.get("observation_only")}
            assert observation.get("success") and observation.get("observation_only"), observation
            assert session.control.paused.is_set(), "Observation must not resume input"
            from clickwarden.win32core import window_rect
            l,t,r,b=window_rect(session.panel_hwnd)
            session.avoid_panel([((l+r)//2,(t+b)//2)])
            report["panel_moved_off_target"]=window_rect(session.panel_hwnd)!=(l,t,r,b)
            assert report["panel_moved_off_target"]
            assert user32.IsWindowVisible(session.panel_hwnd)
            report["end"]=manager.end("completed")
            assert session.closed.is_set()
            client=session.client
            force_foreground(target)
            second=manager.start("Read-only return")
            returning=manager.get(second["session_id"])
            assert returning.prepared.wait(5)
            assert not returning.error, returning.error
            report["returning_ready_ms"]=returning.metrics()["startup_ms"]
            report["connection_reused"]=returning.client is client
            assert returning.client is client
            assert returning.usage("total_requests")==0
            manager.end("completed")
        except Exception as exc:
            report["error"]=str(exc)
        finally:
            if manager.current and not manager.current.closed.is_set():
                manager.end("failed")
            print(json.dumps(report,ensure_ascii=True),flush=True)
            root.after(0,root.destroy)
    root.after(400,lambda:threading.Thread(target=run,daemon=True).start())
    root.mainloop()
    return 1 if "error" in report else 0


if __name__=="__main__":
    raise SystemExit(main())
