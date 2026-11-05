# A/B benchmark: native computer use vs Clickwarden

The same Claude model does the same 12 Windows tasks two ways, 3 runs each:

| | `baseline` | `clickwarden` |
|---|---|---|
| What the model sees | screenshots (1280x720) | short text results only |
| Tools | Anthropic `computer_toolset_20260801` | Clickwarden's MCP tools (27 of 36; jobs and desktop-icon tools left out) |
| Who finds things on screen | the model itself | Windows UI Automation first, then Clickwarden's vision model |
| Tokens counted | heavy model `usage` | heavy model `usage` **plus** the vision model's `usage` |

Same in both: model, effort, adaptive thinking, prompt caching, task prompt, core system prompt, max 30 model
requests and 300 s per run, and the Win32 input layer (baseline clicks go through `clickwarden.input` too).
Success is decided only from the end state (files, registry, local web-server receipts), never from the model
saying DONE.

## Before you run it

0. **Install** the extra dependencies: `.\.venv\Scripts\pip install -e ".[bench]"` (Anthropic SDK, PyYAML).
1. **Anthropic API key.** Set it yourself in the terminal you run from (it is never stored by the harness):
   `$env:ANTHROPIC_API_KEY = "<your key>"` in PowerShell.
2. **Vision provider.** Configured with `clickwarden configure` (yours: OpenRouter, a `:free` model). Free
   OpenRouter models have a daily request cap; the full run needs roughly 100–250 vision requests. The runner
   sends one synthetic test image first and stops if the key is rejected.
3. **Quiet desktop.** Close Notepad, File Explorer folder windows and Settings (the runner refuses to start
   otherwise; it closes those between runs). One monitor, 100% scaling recommended. Keep private windows
   closed: screenshots go to Anthropic (baseline) and the vision provider (clickwarden).
4. **Optional, reduces noise:** in Notepad's settings, set *When Notepad starts* to *Start new session and
   discard unsaved changes*, so leftover tabs from one run don't reappear in the next.
5. **Commit first** so `meta.json` records a clean git commit for the code that produced the numbers.

What it changes on your machine: it creates and deletes `%USERPROFILE%\ClickwardenBench`, opens Notepad,
Explorer, Settings and a throwaway Edge profile, and two tasks change *Choose your mode* and *Show seconds in
system tray clock*. Your original values for both are read at start and written back after every run.

## Commands

```powershell
.\.venv\Scripts\python.exe -m benchmarks.ab.run --dry-run                       # no API: set up, check, tear down each task
.\.venv\Scripts\python.exe -m benchmarks.ab.run --tasks t01_notepad_save --runs 1   # paid smoke test, 2 runs
.\.venv\Scripts\python.exe -m benchmarks.ab.run                                 # the full benchmark: 12 x 2 x 3 = 72 runs
.\.venv\Scripts\python.exe -m benchmarks.ab.run --resume benchmarks\results\ab-<time>   # continue after a stop
.\.venv\Scripts\python.exe -m benchmarks.ab.report benchmarks\results\ab-<time>         # rebuild the report
```

**Ctrl+Alt+F12** stops after the current action. The interrupted run is not recorded; `--resume` redoes it.
Don't touch the mouse or keyboard during clickwarden runs: Clickwarden pauses on physical input by design, and
that run would fail.

Options worth knowing: `--model claude-sonnet-5-5`, `--effort low|medium|high`, `--vision-price-in/out` (USD
per million tokens, if you switch to a paid vision model).

## Output (`benchmarks/results/ab-<time>/`, git-ignored)

- `results.csv`: one row per run. The first 12 columns are the brief's (`task_id, mode, run, success,
  heavy_in_tokens, heavy_out_tokens, vision_in_tokens, vision_out_tokens, total_cost_usd, wall_time_s, steps,
  failure_reason`); the rest split input into uncached / cache write / cache read and record how each run ended.
- `BENCHMARK.md`: setup, task list, summary and per-task tables (median, min–max), every failure, trade-offs.
- `headline.txt`: the one-line result.
- `meta.json`: models, prices, limits, hardware, git commit, tasks.yaml hash.
- `runs/*.json`: per-run transcripts (tool calls, results, usage per request; screenshots are not saved).

To publish, copy `results.csv`, `BENCHMARK.md`, `meta.json` and `runs/` into the repo deliberately.

## Rules this harness keeps

- The task list (`tasks.yaml`) was fixed before any run. `--resume` refuses a changed task file.
- Every run is reported; the report never filters or re-runs anything.
- Token counts come from API `usage` fields only. A clickwarden run whose vision provider did not report usage
  for every request is flagged in the report as a lower bound.
- Mode order alternates per task and run, so neither mode always runs first.
