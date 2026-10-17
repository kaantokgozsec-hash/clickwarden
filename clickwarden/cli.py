"""
clickwarden.cli — one-shot command line for the engine.

    clickwarden info
    clickwarden shot --mode turbo --out shot.jpg
    clickwarden locate "the YouTube skip button"
    clickwarden do "the search box"           # locate + click + reticle
    clickwarden inspect "what is playing?"
    clickwarden type "lofi beats" --enter
    clickwarden hotkey ctrl,t
    clickwarden task "pause the music"
    clickwarden batch '[{"action":"click","description":"search box"},{"action":"type","text":"jazz","press_enter":true}]'
    clickwarden bench
    clickwarden warm                          # pre-open the TLS connection
    clickwarden configure                     # choose the vision provider and review data sharing
    clickwarden configure --show              # print non-secret settings
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clickwarden",
                                     description="Ultra-low-latency Windows computer use")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="screen + engine status")
    sub.add_parser("warm", help="pre-open the vision TLS connection")
    p_conf = sub.add_parser("configure", help="choose the vision provider, key and data-sharing consent")
    p_conf.add_argument("--show", action="store_true", help="print non-secret settings and exit")
    p_doctor = sub.add_parser("doctor", help="inspect environment and optionally probe pointer input")
    p_doctor.add_argument("--input", action="store_true", help="move the cursor, verify, and restore it")
    p_job = sub.add_parser("job", help="start/control durable desktop jobs")
    job_commands = p_job.add_subparsers(dest="job_command", required=True)
    job_start = job_commands.add_parser("start")
    job_start.add_argument("goal")
    job_start.add_argument("--hwnd", type=int, required=True)
    job_start.add_argument("--max-steps", type=int, default=60)
    job_start.add_argument("--max-seconds", type=float, default=1800)
    job_start.add_argument("--max-requests", type=int, default=120)
    job_start.add_argument("--completion-file", default="")
    job_start.add_argument("--expected-text", default="")
    job_commands.add_parser("list")
    for name in ("status", "pause", "resume", "cancel"):
        command = job_commands.add_parser(name)
        command.add_argument("job_id")
        if name == "resume":
            command.add_argument("--hwnd", type=int, default=0)

    p_shot = sub.add_parser("shot", help="capture the screen to JPEG")
    p_shot.add_argument("--mode", default="precision", choices=["precision", "turbo"])
    p_shot.add_argument("--monitor", type=int, default=1)
    p_shot.add_argument("--roi", default="", help="'active_window' or window-title substring")
    p_shot.add_argument("--out", default="")

    p_loc = sub.add_parser("locate", help="ground an element to coordinates")
    p_loc.add_argument("description")
    p_loc.add_argument("--mode", default="precision", choices=["precision", "turbo", "auto"])
    p_loc.add_argument("--roi", default="")

    p_do = sub.add_parser("do", help="locate + click (the fast primitive)")
    p_do.add_argument("description")
    p_do.add_argument("--button", default="left", choices=["left", "right", "middle"])
    p_do.add_argument("--clicks", type=int, default=1)
    p_do.add_argument("--mode", default="precision", choices=["precision", "turbo", "auto"])
    p_do.add_argument("--roi", default="")
    p_do.add_argument("--verify", action="store_true")
    p_do.add_argument("--no-hud", action="store_true")

    p_ins = sub.add_parser("inspect", help="ask the vision model about the screen")
    p_ins.add_argument("query", nargs="?", default="What application is active and what is shown?")
    p_ins.add_argument("--mode", default="turbo", choices=["precision", "turbo", "auto"])

    p_type = sub.add_parser("type", help="type text")
    p_type.add_argument("text")
    p_type.add_argument("--enter", action="store_true")
    p_type.add_argument("--wpm", type=int, default=0)

    p_hk = sub.add_parser("hotkey", help="press a chord")
    p_hk.add_argument("keys")

    p_task = sub.add_parser("task", help="autonomous loop toward a goal")
    p_task.add_argument("goal")
    p_task.add_argument("--max-steps", type=int, default=6)
    p_task.add_argument("--mode", default="precision", choices=["precision", "turbo"])

    p_batch = sub.add_parser("batch", help="run a JSON array of grounded actions")
    p_batch.add_argument("actions")
    p_batch.add_argument("--verify-last", action="store_true")

    p_win = sub.add_parser("windows", help="list top-level windows")
    p_win.add_argument("--all", action="store_true")

    sub.add_parser("bench", help="measure the live pipeline")

    p_hud = sub.add_parser("hud", help="toggle the overlay")
    p_hud.add_argument("state", choices=["on", "off"])

    args = parser.parse_args(argv)

    if args.cmd == "job":
        from .jobs import JobManager
        manager = JobManager()
        try:
            if args.job_command == "start":
                result = manager.start(args.goal, args.hwnd, args.max_steps, args.max_seconds,
                                       args.max_requests, args.completion_file, args.expected_text)
            elif args.job_command == "list":
                result = manager.list()
            elif args.job_command == "status":
                result = manager.status(args.job_id)
            elif args.job_command == "resume":
                result = manager.resume(args.job_id, args.hwnd)
            else:
                result = manager.command(args.job_id, args.job_command)
            _print(result)
            return 0
        except Exception as exc:
            _print({"status": "error", "error": str(exc)})
            return 1

    if args.cmd == "configure":
        from .configure import main as configure_main
        return configure_main(["--show"] if args.show else [])

    if args.cmd == "doctor":
        from .diagnostics import diagnose
        report = diagnose(probe_input=args.input)
        _print(report)
        return 0 if report["status"] == "ready" else 1

    # import lazily so --help is instant
    if args.cmd == "warm":
        from .vision import client
        ms = client.prewarm()
        _print({"status": "warm", "prewarm_ms": ms, "configured": client.configured})
        return 0 if client.configured else 1

    if args.cmd == "info":
        from .win32core import cursor_pos, ensure_dpi_aware, attach_desktop, virtual_screen
        from .vision import client
        dpi = ensure_dpi_aware()
        attach_desktop()
        vx, vy, vw, vh = virtual_screen()
        x, y = cursor_pos()
        _print({
            "virtual_screen": {"left": vx, "top": vy, "width": vw, "height": vh},
            "cursor": {"x": x, "y": y},
            "dpi_mode": dpi,
            "vision_configured": client.configured,
            "model": client.model,
        })
        return 0

    if args.cmd == "windows":
        from .win32core import attach_desktop, enumerate_windows
        attach_desktop()
        _print(enumerate_windows(only_visible=not args.all))
        return 0

    from .engine import engine

    if args.cmd == "shot":
        engine.monitor = args.monitor
        if args.roi:
            resolved_mode, roi = engine._resolve_region(args.mode, args.roi)
        else:
            resolved_mode, roi = args.mode, None
        frame = engine.grab(mode=resolved_mode, roi=roi, fresh=True)
        if args.out:
            with open(args.out, "wb") as fh:
                fh.write(frame.jpeg)
        _print({"status": "success", "file": args.out or None, **frame.meta()})
        return 0

    if args.cmd == "locate":
        _print(engine.locate(args.description, mode=args.mode, roi=args.roi or None, fresh=True))
        return 0

    if args.cmd == "do":
        res = engine.smart_click(args.description, button=args.button, clicks=args.clicks,
                                 mode=args.mode, roi=args.roi or None,
                                 verify=args.verify, hud=not args.no_hud)
        _print(res)
        return 0 if res.get("status") == "success" else 1

    if args.cmd == "inspect":
        _print(engine.inspect(args.query, mode=args.mode, fresh=True))
        return 0

    if args.cmd == "type":
        _print(engine.type(args.text, press_enter=args.enter, wpm=args.wpm))
        return 0

    if args.cmd == "hotkey":
        _print(engine.hotkey(args.keys))
        return 0

    if args.cmd == "task":
        _print(engine.task(args.goal, max_steps=args.max_steps, mode=args.mode))
        return 0

    if args.cmd == "batch":
        try:
            actions = json.loads(args.actions)
        except ValueError as exc:
            _print({"status": "error", "error": f"invalid JSON: {exc}"})
            return 1
        _print(engine.batch(actions, verify_last=args.verify_last))
        return 0

    if args.cmd == "hud":
        if engine._hud is None:
            _print({"status": "unavailable"})
            return 1
        if args.state == "on":
            engine._hud.start()
            engine._hud.begin(color="cyan", status="clickwarden: hud test")
            time.sleep(1.2)
            engine._hud.end()
            _print({"status": "success", "hud_enabled": True})
        else:
            engine._hud.shutdown()
            _print({"status": "success", "hud_enabled": False})
        return 0

    if args.cmd == "bench":
        from . import frames as frames_mod
        from .input import move_to
        from .vision import client
        out: dict = {}
        out["prewarm_ms"] = client.prewarm()

        frame = frames_mod.capture(monitor=1, mode="precision")
        out["capture"] = frame.meta()

        t0 = time.perf_counter()
        loc = engine.locate("the Windows taskbar clock", fresh=True)
        out["locate_full_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["locate"] = {k: loc.get(k) for k in
                         ("success", "point", "latency_ms", "ttft_ms",
                          "press_ready_ms", "confidence")}

        t0 = time.perf_counter()
        move_to(960, 540, human=False)
        out["move_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        _print(out)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
