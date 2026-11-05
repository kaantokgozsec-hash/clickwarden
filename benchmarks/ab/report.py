"""Turn results.csv + meta.json into BENCHMARK.md and a one-line headline. Pure: no desktop, no network.

    python -m benchmarks.ab.report benchmarks\\results\\ab-20261105-1400

Every recorded run is used, failures included. Nothing is filtered or re-run here.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

from .environment import load_tasks
from .pricing import VISION_PRICE_SCENARIOS

NUMERIC = ("run", "success", "heavy_in_tokens", "heavy_out_tokens", "vision_in_tokens", "vision_out_tokens",
           "total_cost_usd", "wall_time_s", "steps", "heavy_uncached_in_tokens", "heavy_cache_write_tokens",
           "heavy_cache_read_tokens", "heavy_requests", "heavy_cost_usd", "vision_cost_usd", "vision_requests",
           "vision_usage_complete", "claimed_done", "false_success")
MODE_LABEL = {"baseline": "Native computer use", "clickwarden": "Through Clickwarden"}
MODEL_LABEL = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
               "claude-haiku-5-5": "Claude Haiku 5.5"}


def load_rows(csv_path: Path) -> list[dict]:
    with Path(csv_path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for key in NUMERIC:
            value = row.get(key, "")
            row[key] = float(value) if value not in ("", None) else 0.0
        row["tokens"] = row["heavy_in_tokens"] + row["heavy_out_tokens"] + row["vision_in_tokens"] + row["vision_out_tokens"]
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        n = len(rs)
        wins = sum(r["success"] for r in rs)
        cost = sum(r["total_cost_usd"] for r in rs)
        by_task = {}
        for r in rs:
            by_task.setdefault(r["task_id"], []).append(r["success"])
        out[mode] = {
            "runs": n, "successes": int(wins),
            "tasks": len(by_task), "tasks_all_runs_passed": sum(all(v) for v in by_task.values()),
            "tasks_majority_passed": sum(sum(v) * 2 > len(v) for v in by_task.values()),
            "mean_tokens": sum(r["tokens"] for r in rs) / n,
            "mean_heavy_in": sum(r["heavy_in_tokens"] for r in rs) / n,
            "mean_heavy_out": sum(r["heavy_out_tokens"] for r in rs) / n,
            "mean_vision_in": sum(r["vision_in_tokens"] for r in rs) / n,
            "mean_vision_out": sum(r["vision_out_tokens"] for r in rs) / n,
            "mean_cost": cost / n, "total_cost": cost,
            "cost_per_success": cost / wins if wins else None,
            "mean_heavy_cost": sum(r["heavy_cost_usd"] for r in rs) / n,
            "mean_vision_cost": sum(r["vision_cost_usd"] for r in rs) / n,
            "median_wall": statistics.median(r["wall_time_s"] for r in rs),
            "median_steps": statistics.median(r["steps"] for r in rs),
            "false_success": int(sum(r["false_success"] for r in rs)),
            "incomplete_vision_usage": int(sum(1 for r in rs if mode == "clickwarden" and not r["vision_usage_complete"])),
            "errors": int(sum(1 for r in rs if r.get("stop") == "error")),
        }
    return out


def _fmt_tokens(value: float) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def _cents(usd: float | None) -> str:
    return "n/a" if usd is None else f"{usd * 100:.2f}¢"


def comparison(summary: dict) -> dict | None:
    b, c = summary.get("baseline"), summary.get("clickwarden")
    if not b or not c or not b["mean_tokens"]:
        return None
    ratio = c["mean_tokens"] / b["mean_tokens"]
    cost_ratio = c["mean_cost"] / b["mean_cost"] if b["mean_cost"] else None
    return {"token_ratio": ratio, "token_change_pct": (ratio - 1) * 100,
            "cost_ratio": cost_ratio, "cost_change_pct": None if cost_ratio is None else (cost_ratio - 1) * 100}


def _change(pct: float) -> str:
    return f"{abs(pct):.0f}% fewer" if pct < 0 else f"{pct:.0f}% more"


def headline(summary: dict, meta: dict) -> str:
    b, c = summary.get("baseline"), summary.get("clickwarden")
    cmp_ = comparison(summary)
    if not b or not c or not cmp_:
        return "Incomplete: both modes need recorded runs before a comparison can be stated."
    cfg = meta.get("config", {})
    model = MODEL_LABEL.get(cfg.get("model"), cfg.get("model", "?"))
    vision = meta.get("vision", {}).get("model", "?")
    prices = meta.get("prices", {})
    free = not prices.get("vision_in") and not prices.get("vision_out")
    billed = prices.get("vision_label") or f"${prices.get('vision_in')}/${prices.get('vision_out')} per million tokens"
    tasks = c["tasks"]
    runs = cfg.get("runs", "?")
    return (f"{tasks} Windows tasks × {runs} runs, {model} + {vision}: "
            f"{c['successes']}/{c['runs']} successful runs through Clickwarden vs {b['successes']}/{b['runs']} with "
            f"native computer use; {_fmt_tokens(c['mean_tokens'])} vs {_fmt_tokens(b['mean_tokens'])} tokens per run "
            f"({_change(cmp_['token_change_pct'])}, vision-model tokens included); "
            f"{_cents(c['mean_cost'])} vs {_cents(b['mean_cost'])} per run"
            + (" (vision model on a free tier, billed at $0)." if free else f" (vision tokens billed at {billed})."))


def per_task(rows: list[dict]) -> list[dict]:
    out = []
    for task_id in sorted({r["task_id"] for r in rows}):
        for mode in ("baseline", "clickwarden"):
            rs = [r for r in rows if r["task_id"] == task_id and r["mode"] == mode]
            if not rs:
                continue
            tok = [r["tokens"] for r in rs]
            cost = [r["total_cost_usd"] for r in rs]
            out.append({"task_id": task_id, "mode": mode, "passed": int(sum(r["success"] for r in rs)), "n": len(rs),
                        "tok_med": statistics.median(tok), "tok_min": min(tok), "tok_max": max(tok),
                        "cost_med": statistics.median(cost), "cost_min": min(cost), "cost_max": max(cost),
                        "wall_med": statistics.median(r["wall_time_s"] for r in rs),
                        "steps_med": statistics.median(r["steps"] for r in rs)})
    return out


def rows_for_scenarios(c: dict) -> list[dict]:
    """Clickwarden's mean cost per run with its measured vision tokens re-priced at each scenario."""
    out = []
    for sc in VISION_PRICE_SCENARIOS:
        vision = (c["mean_vision_in"] * sc["in"] + c["mean_vision_out"] * sc["out"]) / 1e6
        per_run = c["mean_heavy_cost"] + vision
        out.append({**sc, "per_run": per_run, "vision_per_run": vision,
                    "per_success": per_run * c["runs"] / c["successes"] if c["successes"] else None})
    return out


def _check_summary(check: dict) -> str:
    kind = check["type"]
    if kind == "all":
        return "; ".join(_check_summary(c) for c in check["checks"])
    if kind == "file_text":
        return f"{check['path']} contains exactly “{check['equals']}”"
    if kind == "absent":
        return f"{check['path']} no longer exists"
    if kind == "dir_exists":
        return f"folder {check['path']} exists"
    if kind == "readonly":
        return f"{check['path']} read-only = {check['equals']}"
    if kind == "registry":
        return f"HKCU {check['name']} = {check['equals']}"
    if kind == "web_visit":
        return f"server received a visit to {check['path']}"
    if kind == "web_submit":
        return f"server received exactly one submission to {check['path']} with " + ", ".join(
            f"{k}={v}" for k, v in check["fields"].items())
    if kind == "web_click":
        return f"server received exactly one click, on “{check['target']}”"
    return kind


def write_report(out_dir: Path, tasks_file: Path | None = None) -> Path:
    out_dir = Path(out_dir)
    rows = load_rows(out_dir / "results.csv")
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    tasks = load_tasks(tasks_file or Path(__file__).with_name("tasks.yaml"))
    tasks = [t for t in tasks if t["id"] in set(meta.get("task_ids") or [t["id"] for t in tasks])]
    summary = summarize(rows)
    line = headline(summary, meta)
    (out_dir / "headline.txt").write_text(line + "\n", encoding="utf-8")
    cfg, hw, scr, prices = meta.get("config", {}), meta.get("hardware", {}), meta.get("screen", {}), meta.get("prices", {})
    vis, git = meta.get("vision", {}), meta.get("git", {})
    model = cfg.get("model", "?")
    L = []
    L += ["# Clickwarden A/B benchmark", "",
          f"> {line}", "",
          "The same Claude model did the same Windows tasks two ways: with Anthropic's own computer-use tool "
          "(it sees screenshots), and through Clickwarden's MCP tools (it sees only short text; Clickwarden reads "
          "the Windows accessibility tree first and asks a small vision model only when it needs to). Every run is "
          "reported below, failures included.", ""]
    L += ["## Setup", "",
          f"- **Heavy model:** `{model}` ({MODEL_LABEL.get(model, model)}), effort `{cfg.get('effort')}`, "
          f"{cfg.get('thinking', 'adaptive thinking')}. Prompt caching: {cfg.get('prompt_caching')}.",
          f"- **Native mode:** `{cfg.get('baseline_tool')}`, screenshots downscaled to "
          f"{'x'.join(map(str, cfg.get('baseline_screenshot', [])))} from the physical screen, "
          f"{cfg.get('baseline_post_action_delay_s')} s settle after each action before the next one. "
          "Clicks and keys go through Clickwarden's own Win32 input layer, so both modes press keys and click the same way.",
          f"- **Clickwarden mode:** Clickwarden {git.get('commit', '')[:10]} over MCP (stdio), fresh server per run. "
          f"The model got the server's instructions and the shipped agent skill in its system prompt. Not offered: "
          f"{', '.join('`%s`' % t for t in cfg.get('clickwarden_excluded_tools', []))} (durable jobs run in a separate "
          "process whose vision tokens could not be counted; the desktop-icon tools rearrange the real desktop).",
          f"- **Vision model:** `{vis.get('model')}` via {vis.get('host')} (coordinates: {vis.get('coord_space')}). "
          f"Priced at ${prices.get('vision_in', 0)}/${prices.get('vision_out', 0)} per million prompt/completion tokens"
          + (" because it is a free-tier model; its tokens are still counted in every token total." if not prices.get('vision_in') and not prices.get('vision_out') else "."),
          f"- **Same limits in both modes:** at most {cfg.get('max_turns')} model requests and {cfg.get('timeout_s'):.0f} s per run, "
          f"`max_tokens` {cfg.get('max_tokens')}. Identical task prompt and the same core system prompt.",
          f"- **Order:** {cfg.get('runs')} runs per task per mode; run 1 of every task, then run 2, then run 3, "
          "with the mode order alternating per task and run.",
          f"- **Machine:** {hw.get('cpu', '?')}, {hw.get('ram_gb', '?')} GB RAM, GPU: {hw.get('gpu', '?')}; "
          f"{hw.get('os', '?')} build {hw.get('os_build', '?')}; screen {scr.get('width')}x{scr.get('height')} at "
          f"{scr.get('scale_percent')}% scaling, {scr.get('monitors')} monitor(s).",
          f"- **Software:** Python {meta.get('python')}, anthropic SDK {meta.get('sdk', {}).get('anthropic')}, "
          f"git `{git.get('branch')}` @ `{git.get('commit', '')[:10]}`{' (uncommitted changes)' if git.get('dirty') else ''}, "
          f"tasks.yaml sha256 `{meta.get('tasks_sha256', '')[:16]}…`.",
          f"- **Run dates:** started {meta.get('started_at')} (UTC{meta.get('timezone', '')})"
          + (f", resumed {', '.join(meta['resumed_at'])}" if meta.get("resumed_at") else "") + ".", "",
          "**Token counts** for the heavy model are the sums of each API response's `usage` fields "
          "(`input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`, and `output_tokens`, which "
          "includes thinking). Vision tokens are the provider-reported `usage` that Clickwarden saves per session. "
          "No estimates.", "",
          f"**Prices** (USD per million tokens, checked {prices.get('heavy_checked')}): "
          + ", ".join(f"{k.replace('_', ' ')} ${v}" for k, v in (prices.get("heavy") or {}).items()) + ".", ""]
    L += ["## How success was decided", "",
          "Only by checking the end state: file contents and attributes on disk, registry values, or receipts "
          "logged by a local test web server (which page loaded, what a form submitted, which button was clicked). "
          "The model saying DONE never counts. A run that said DONE but failed the check is a *false success claim* "
          "and is counted below.", ""]
    L += ["## Tasks", "", "| Task | Difficulty | What the model was asked | Pass condition |", "|---|---|---|---|"]
    for t in tasks:
        prompt = " ".join(t["prompt"].split()).replace("|", "\\|")
        L.append(f"| `{t['id']}` | {t['difficulty']} | {prompt} | {_check_summary(t['check'])} |")
    L += ["", "## Results", "",
          "| | Successful runs | Tasks passed in every run | Mean tokens per run (heavy in / heavy out / vision) | Mean cost per run | Cost per successful run | Median wall time | Median actions | False success claims |",
          "|---|---|---|---|---|---|---|---|---|"]
    for mode in ("baseline", "clickwarden"):
        s = summary.get(mode)
        if not s:
            continue
        L.append(f"| {MODE_LABEL[mode]} | {s['successes']}/{s['runs']} | {s['tasks_all_runs_passed']}/{s['tasks']} | "
                 f"{_fmt_tokens(s['mean_tokens'])} ({_fmt_tokens(s['mean_heavy_in'])} / {_fmt_tokens(s['mean_heavy_out'])} / "
                 f"{_fmt_tokens(s['mean_vision_in'] + s['mean_vision_out'])}) | {_cents(s['mean_cost'])} | "
                 f"{_cents(s['cost_per_success'])} | {s['median_wall']:.0f} s | {s['median_steps']:.0f} | {s['false_success']} |")
    cmp_ = comparison(summary)
    if cmp_:
        L += ["", f"Clickwarden used **{_change(cmp_['token_change_pct'])} tokens** per run on average"
              + (f" and cost **{_change(cmp_['cost_change_pct'])}** per run" if cmp_["cost_change_pct"] is not None else "")
              + ". Read this together with the success rates above: a run that fails early can look cheap, which "
              "is why cost per *successful* run is listed too."]
    c, b = summary.get("clickwarden"), summary.get("baseline")
    if c and b:
        L += ["", "### What the vision model would cost on a paid endpoint", "",
              "The same measured vision tokens re-priced at published rates (USD per million prompt / completion "
              f"tokens, from OpenRouter's models API, {prices.get('vision_checked') or 'see pricing.py'}). Only the "
              "Nemotron rows are like-for-like; other models tokenize images differently, so their rows assume the "
              "same token count and are approximate. Native mode uses no vision model, so its cost does not change.", "",
              "| Vision price | $/M in / out | Clickwarden cost per run | Native cost per run | Clickwarden vs native | Clickwarden cost per successful run |",
              "|---|---|---|---|---|---|"]
        for sc in rows_for_scenarios(c):
            change = (sc["per_run"] / b["mean_cost"] - 1) * 100 if b["mean_cost"] else 0
            L.append(f"| {sc['label']}{'' if sc['same_model'] else ' *(approx.)*'} | ${sc['in']} / ${sc['out']} | "
                     f"{_cents(sc['per_run'])} | {_cents(b['mean_cost'])} | {_change(change)} | {_cents(sc['per_success'])} |")
    L += ["", "### Per task (median, with min–max over the runs)", "",
          "| Task | Mode | Passed | Tokens | Cost | Wall time | Actions |", "|---|---|---|---|---|---|---|"]
    for p in per_task(rows):
        L.append(f"| `{p['task_id']}` | {p['mode']} | {p['passed']}/{p['n']} | {_fmt_tokens(p['tok_med'])} "
                 f"({_fmt_tokens(p['tok_min'])}–{_fmt_tokens(p['tok_max'])}) | {_cents(p['cost_med'])} "
                 f"({_cents(p['cost_min'])}–{_cents(p['cost_max'])}) | {p['wall_med']:.0f} s | {p['steps_med']:.0f} |")
    failed = [r for r in rows if not r["success"]]
    L += ["", "## Failures", ""]
    if failed:
        L += ["Every failed run, as recorded. Transcripts are in `runs/<task>__<mode>__<run>.json`.", "",
              "| Task | Mode | Run | How it ended | Why it failed | Said DONE |", "|---|---|---|---|---|---|"]
        for r in sorted(failed, key=lambda r: (r["task_id"], r["mode"], r["run"])):
            reason = (r.get("failure_reason") or "").replace("|", "\\|")
            L.append(f"| `{r['task_id']}` | {r['mode']} | {int(r['run'])} | {r.get('stop')} | {reason} | "
                     f"{'yes' if r['claimed_done'] else 'no'} |")
    else:
        L.append("No run failed.")
    flags = []
    for mode, s in summary.items():
        if s["errors"]:
            flags.append(f"{s['errors']} {mode} run(s) ended with an API or harness error (counted as failures).")
        if s["incomplete_vision_usage"]:
            flags.append(f"{s['incomplete_vision_usage']} clickwarden run(s) have incomplete vision usage: the provider "
                         "did not report usage for every request, so their vision token counts are a lower bound.")
    L += ["", "## Data quality", ""] + ([f"- {f}" for f in flags] or ["- No API errors and no runs with missing vision usage."])
    L += ["", "## Trade-offs and limits of this benchmark", "",
          "- **Small sample:** " + f"{summary.get('clickwarden', {}).get('tasks', '?')} tasks × {cfg.get('runs')} runs on one machine. "
          "Differences of a run or two in success are within noise.",
          "- **Author-written tasks:** the task list was fixed before any run, but it was written by Clickwarden's author.",
          "- **Accessible targets help Clickwarden:** most targets here (Explorer, Settings, standard web pages) expose "
          "names in the Windows accessibility tree, which is Clickwarden's cheapest path. Apps that expose little "
          "(games, canvas apps, some Electron apps) push it onto the vision model and would narrow the gap.",
          "- **Native mode was not set up to be expensive:** screenshots were 1280x720 (a low-cost setting), the model "
          "asked for screenshots when it wanted them (none were attached automatically), and prompt caching was on.",
          "- **Free vision tier:** the vision model cost $0 here. Its tokens are in every total; a paid vision model would "
          "add its own price per token.",
          "- **Wall time** includes the vision provider's latency (free tiers are slow and variable) and Clickwarden's "
          "paced typing; it is reported, not optimized.", ""]
    L += ["## Reproduce", "", "```powershell",
          ".\\.venv\\Scripts\\python.exe -m benchmarks.ab.run --dry-run   # environment check, no API calls",
          f".\\.venv\\Scripts\\python.exe -m benchmarks.ab.run --model {model} --effort {cfg.get('effort')} --runs {cfg.get('runs')}"
          f" --vision-price-in {prices.get('vision_in', 0)} --vision-price-out {prices.get('vision_out', 0)}",
          "```", "", "Raw data: `results.csv` (one row per run), `meta.json` (setup), `runs/*.json` (transcripts; images not stored).", ""]
    path = out_dir / "BENCHMARK.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m benchmarks.ab.report <results folder>")
        return 2
    path = write_report(Path(argv[0]))
    print(path)
    print((Path(argv[0]) / "headline.txt").read_text(encoding="utf-8").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
