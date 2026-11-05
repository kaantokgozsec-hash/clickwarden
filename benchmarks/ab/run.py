r"""Run the A/B benchmark: every task x every mode x N runs, on the real desktop.

    .\.venv\Scripts\python.exe -m benchmarks.ab.run                    # full run (asks before starting)
    .\.venv\Scripts\python.exe -m benchmarks.ab.run --dry-run          # set up / check / tear down only, no API
    .\.venv\Scripts\python.exe -m benchmarks.ab.run --tasks t01_notepad_save --runs 1   # smoke test
    .\.venv\Scripts\python.exe -m benchmarks.ab.run --resume benchmarks\results\ab-20261105-1400

Needs ANTHROPIC_API_KEY in the environment (set it yourself), Clickwarden's vision provider configured with
`clickwarden configure`, Microsoft Edge, one monitor, and a quiet desktop: the runs move your real mouse and
keyboard. Ctrl+Alt+F12 stops after the current action; the interrupted run is not recorded, and --resume
continues where it stopped. Results: results.csv, runs/*.json transcripts, meta.json, BENCHMARK.md.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import agent, environment, pricing, report
from .webfixture import WebFixture

ROOT = Path(__file__).resolve().parents[2]
TASKS_FILE = Path(__file__).with_name("tasks.yaml")
MODES = ("baseline", "clickwarden")

CSV_FIELDS = [
    # the columns the benchmark brief asked for
    "task_id", "mode", "run", "success", "heavy_in_tokens", "heavy_out_tokens", "vision_in_tokens",
    "vision_out_tokens", "total_cost_usd", "wall_time_s", "steps", "failure_reason",
    # detail behind them
    "heavy_uncached_in_tokens", "heavy_cache_write_tokens", "heavy_cache_read_tokens", "heavy_requests",
    "heavy_cost_usd", "vision_cost_usd", "vision_requests", "vision_usage_complete", "stop", "claimed_done",
    "false_success", "check_detail", "model", "effort", "difficulty", "started_at",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def screen_info() -> dict:
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass
    dpi = user32.GetDpiForSystem() if hasattr(user32, "GetDpiForSystem") else 96
    return {"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1),
            "monitors": user32.GetSystemMetrics(80), "scale_percent": round(dpi * 100 / 96)}


def hardware() -> dict:
    script = ("$c=(Get-CimInstance Win32_Processor|Select -First 1).Name;"
              "$g=(Get-CimInstance Win32_VideoController|%{$_.Name}) -join '; ';"
              "$r=[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB);"
              "$o=(Get-CimInstance Win32_OperatingSystem);"
              "Write-Output (ConvertTo-Json @{cpu=$c.Trim();gpu=$g;ram_gb=$r;os=$o.Caption;os_build=$o.BuildNumber})")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=30, creationflags=environment.NO_WINDOW).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"os": platform.platform()}


def git_state() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain"))}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def start_abort_hotkey(abort: threading.Event) -> None:
    """Ctrl+Alt+F12 sets ``abort`` (same chord Clickwarden uses for its own emergency stop)."""
    user32 = ctypes.windll.user32

    def watch():
        while not abort.is_set():
            if all(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x11, 0x12, 0x7B)):
                abort.set()
                print("\n[abort] Ctrl+Alt+F12 pressed: stopping after the current action.", flush=True)
            time.sleep(0.05)

    threading.Thread(target=watch, daemon=True, name="ab-abort-hotkey").start()


def done_keys(csv_path: Path) -> set[tuple[str, str, int]]:
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return {(r["task_id"], r["mode"], int(r["run"])) for r in csv.DictReader(fh)}


def append_row(csv_path: Path, row: dict) -> None:
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def schedule(tasks: list[dict], runs: int, modes) -> list[tuple[int, dict, str]]:
    """Run 1 of every task, then run 2, ...; the mode order alternates per task and run (counterbalanced),
    so neither mode always goes first on a freshly set-up desktop or later in the day."""
    plan = []
    for run in range(1, runs + 1):
        for i, task in enumerate(tasks):
            order = list(modes) if (run + i) % 2 == 0 else list(reversed(modes))
            plan.extend((run, task, mode) for mode in order)
    return plan


def build_row(task: dict, mode: str, run: int, cfg: agent.Config, out: agent.RunOutcome, success: bool,
              detail: str, vision_price: tuple[float, float], started: str) -> dict:
    heavy_cost = pricing.heavy_cost(out.heavy, cfg.model)
    v = out.vision
    vision_cost = pricing.vision_cost(v["prompt_tokens"], v["completion_tokens"], *vision_price)
    if success:
        reason = ""
    elif out.error:
        reason = f"error: {out.error}"
    elif out.stop in ("end_turn", ""):
        reason = detail
    else:
        reason = f"{out.stop}: {detail}"
    return {
        "task_id": task["id"], "mode": mode, "run": run, "success": int(success),
        "heavy_in_tokens": out.heavy.total_input, "heavy_out_tokens": out.heavy.output,
        "vision_in_tokens": v["prompt_tokens"], "vision_out_tokens": v["completion_tokens"],
        "total_cost_usd": round(heavy_cost + vision_cost, 6), "wall_time_s": round(out.wall_s, 1),
        "steps": out.steps, "failure_reason": reason,
        "heavy_uncached_in_tokens": out.heavy.input, "heavy_cache_write_tokens": out.heavy.cache_write,
        "heavy_cache_read_tokens": out.heavy.cache_read, "heavy_requests": out.heavy.requests,
        "heavy_cost_usd": round(heavy_cost, 6), "vision_cost_usd": round(vision_cost, 6),
        "vision_requests": v["requests"], "vision_usage_complete": int(bool(v["usage_complete"])),
        "stop": out.stop, "claimed_done": int(out.claimed_done),
        "false_success": int(out.claimed_done and not success), "check_detail": detail,
        "model": cfg.model, "effort": cfg.effort, "difficulty": task["difficulty"], "started_at": started,
    }


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def vision_preflight() -> dict:
    """One request on a synthetic image (nothing from the screen) to prove the vision key and model work."""
    from clickwarden import providers
    from clickwarden.vision import VisionClient
    from benchmarks.vision_cost import BUTTON, draw_window, frame_of

    settings = providers.resolve()
    if settings.problem:
        return {"ok": False, "problem": settings.problem}
    client = VisionClient(settings=settings)
    try:
        result = client.locate(frame_of(draw_window()), "the Verify button")
    finally:
        client.close()
    point = result.get("point")
    inside = bool(point) and BUTTON[0] <= point[0] < BUTTON[2] and BUTTON[1] <= point[1] < BUTTON[3]
    return {"ok": bool(result.get("success")) and inside, "model": settings.model, "host": settings.host,
            "error": result.get("error"), "point": point,
            "tokens": [client.prompt_tokens, client.completion_tokens]}


async def heavy_preflight(client, model: str) -> dict:
    try:
        info = await client.models.retrieve(model)
        return {"ok": True, "model": info.id, "display_name": getattr(info, "display_name", "")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Clickwarden A/B benchmark (see benchmarks/ab/README.md)")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    p.add_argument("--modes", default=",".join(MODES))
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-turns", type=int, default=30, help="model requests per run (same in both modes)")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds per run (same in both modes)")
    p.add_argument("--screenshot", default="1280x720", help="baseline screenshot size")
    p.add_argument("--vision-price-in", type=float, default=0.0, help="USD per 1M vision prompt tokens")
    p.add_argument("--vision-price-out", type=float, default=0.0, help="USD per 1M vision completion tokens")
    p.add_argument("--out", default="", help="output folder (default benchmarks/results/ab-<time>)")
    p.add_argument("--resume", default="", help="continue an earlier output folder")
    p.add_argument("--dry-run", action="store_true", help="set up, check and tear down every task; no API calls")
    p.add_argument("--skip-vision-check", action="store_true")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation before starting")
    return p.parse_args(argv)


def select_tasks(all_tasks: list[dict], wanted: str) -> list[dict]:
    if not wanted:
        return all_tasks
    ids = [t.strip() for t in wanted.split(",") if t.strip()]
    by_id = {t["id"]: t for t in all_tasks}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"unknown task ids: {missing}")
    return [by_id[i] for i in ids]


def dry_run(tasks: list[dict]) -> int:
    web = WebFixture()
    web.start()
    env = environment.Environment(tasks, web=web)
    failures = 0
    try:
        for task in tasks:
            try:
                env.setup(task)
                ok, detail = env.check(task)
                status = "PROBLEM: check passes before the task was done" if ok else "ok"
                failures += ok
                print(f"  {task['id']:28s} setup ok, untouched state fails the check ({detail}) -> {status}")
            except Exception as exc:
                failures += 1
                print(f"  {task['id']:28s} SETUP FAILED: {exc}")
            finally:
                env.teardown()
    finally:
        env.teardown()
        web.stop()
    print("dry run:", "all tasks set up and torn down cleanly" if not failures else f"{failures} problem(s)")
    return 1 if failures else 0


async def main_async(args) -> int:
    import anthropic

    all_tasks = environment.load_tasks(TASKS_FILE)
    tasks = select_tasks(all_tasks, args.tasks)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if any(m not in MODES for m in modes):
        raise SystemExit(f"modes must be from {MODES}")
    width, height = (int(v) for v in args.screenshot.lower().split("x"))
    cfg = agent.Config(model=args.model, effort=args.effort, max_turns=args.max_turns, timeout_s=args.timeout,
                       screen_w=width, screen_h=height)
    if cfg.model not in pricing.HEAVY_PRICES:
        raise SystemExit(f"no price for {cfg.model} in benchmarks/ab/pricing.py")

    screen = screen_info()
    problems = environment.conflicts()
    if problems:
        print("Close these first (the benchmark closes them between runs):\n  - " + "\n  - ".join(problems))
        return 2
    if not environment.browser_path():
        print("Microsoft Edge is required for the browser tasks.")
        return 2
    if screen["monitors"] != 1:
        print(f"Warning: {screen['monitors']} monitors; the benchmark expects one.")

    if args.dry_run:
        return dry_run(tasks)

    client = anthropic.AsyncAnthropic(max_retries=4)
    heavy_check = await heavy_preflight(client, cfg.model)
    if not heavy_check["ok"]:
        print("Anthropic API check failed:", heavy_check["error"])
        print("Set ANTHROPIC_API_KEY in this terminal yourself, then rerun.")
        return 2
    vision_check = {"ok": None, "skipped": True}
    if "clickwarden" in modes and not args.skip_vision_check:
        vision_check = vision_preflight()
        if not vision_check["ok"]:
            print("Clickwarden vision check failed:", json.dumps(vision_check))
            print("Run `clickwarden configure` yourself to fix the provider or key, then rerun.")
            return 2

    out_dir = Path(args.resume or args.out or (ROOT / "benchmarks" / "results" / f"ab-{time.strftime('%Y%m%d-%H%M%S')}"))
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "results.csv"
    meta_path = out_dir / "meta.json"
    if args.resume and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["config"]["model"] != cfg.model or meta["tasks_sha256"] != file_sha256(TASKS_FILE):
            raise SystemExit("--resume needs the same model and tasks.yaml as the original run")
        meta.setdefault("resumed_at", []).append(time.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        from clickwarden import providers
        vs = providers.resolve()
        meta = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "timezone": time.strftime("%z"),
            "config": {"model": cfg.model, "effort": cfg.effort, "max_turns": cfg.max_turns,
                       "timeout_s": cfg.timeout_s, "max_tokens": cfg.max_tokens,
                       "baseline_screenshot": [width, height], "runs": args.runs, "modes": modes,
                       "prompt_caching": "automatic (top-level cache_control, 5-minute TTL), both modes",
                       "thinking": "adaptive (model default), both modes",
                       "baseline_tool": "computer_toolset_20260801",
                       "baseline_post_action_delay_s": 0.6,
                       "clickwarden_excluded_tools": sorted(agent.EXCLUDED_TOOLS)},
            "prices": {"heavy": pricing.HEAVY_PRICES[cfg.model], "heavy_checked": pricing.PRICES_CHECKED,
                       "vision_in": args.vision_price_in, "vision_out": args.vision_price_out},
            "vision": {"model": vs.model, "host": vs.host, "coord_space": vs.coord_space,
                       "local_only": getattr(vs, "local_only", None), "preflight": vision_check},
            "heavy_preflight": heavy_check, "screen": screen, "hardware": hardware(), "git": git_state(),
            "python": sys.version.split()[0],
            "sdk": {"anthropic": anthropic.__version__}, "tasks_sha256": file_sha256(TASKS_FILE),
            "task_ids": [t["id"] for t in tasks],
        }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    finished = done_keys(csv_path)
    plan = [step for step in schedule(tasks, args.runs, modes) if (step[1]["id"], step[2], step[0]) not in finished]
    print(f"\nHeavy model {cfg.model} (effort {cfg.effort}) | vision {meta['vision']['model']} @ {meta['vision']['host']}")
    print(f"Screen {screen['width']}x{screen['height']} at {screen['scale_percent']}% | {len(plan)} runs to do | output {out_dir}")
    print("The runs move your real mouse and keyboard. Do not touch them. Ctrl+Alt+F12 stops.")
    if not plan:
        print("Nothing left to run.")
    elif not args.yes and input("Type RUN to start: ").strip() != "RUN":
        print("Not started.")
        return 1

    abort = threading.Event()
    start_abort_hotkey(abort)
    web = WebFixture()
    web.start()
    env = environment.Environment(tasks, web=web)
    vision_price = (args.vision_price_in, args.vision_price_out)
    try:
        if plan:
            for second in range(5, 0, -1):
                print(f"  starting in {second}...", flush=True)
                time.sleep(1)
        for n, (run, task, mode) in enumerate(plan, 1):
            started = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                env.setup(task)
            except Exception as exc:
                print(f"[{n}/{len(plan)}] {task['id']} {mode} run {run}: setup failed ({exc}); not recorded")
                env.teardown()
                continue
            prompt = env.prompt(task)
            try:
                if mode == "baseline":
                    out = await agent.run_baseline(client, cfg, prompt, abort)
                else:
                    out = await agent.run_clickwarden(client, cfg, prompt, abort)
            except agent.Aborted:
                print(f"Stopped by the operator. Resume with: --resume {out_dir}")
                return 130
            time.sleep(1.5)  # let saves and page requests land before checking
            success, detail = env.check(task)
            env.teardown()
            row = build_row(task, mode, run, cfg, out, success, detail, vision_price, started)
            append_row(csv_path, row)
            (out_dir / "runs" / f"{task['id']}__{mode}__{run}.json").write_text(
                json.dumps({"row": row, "prompt": prompt, "vision": out.vision, "final_text": out.final_text,
                            "transcript": out.transcript}, indent=1, default=str), encoding="utf-8")
            tokens = row["heavy_in_tokens"] + row["heavy_out_tokens"] + row["vision_in_tokens"] + row["vision_out_tokens"]
            print(f"[{n}/{len(plan)}] {task['id']:26s} {mode:11s} run {run}: "
                  f"{'PASS' if success else 'FAIL'} {tokens:>8,} tok ${row['total_cost_usd']:.4f} "
                  f"{row['wall_time_s']:>6}s {row['steps']:>3} steps  {row['failure_reason'][:70]}", flush=True)
            if abort.is_set():
                print(f"Stopped by the operator. Resume with: --resume {out_dir}")
                return 130
    finally:
        env.teardown()
        web.stop()

    path = report.write_report(out_dir, tasks_file=TASKS_FILE)
    print("\nReport:", path)
    print("Headline:", (out_dir / "headline.txt").read_text(encoding="utf-8").strip())
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
