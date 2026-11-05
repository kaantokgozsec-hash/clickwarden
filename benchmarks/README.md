# Benchmarks

Offline-importable evaluation tooling for Clickwarden. Nothing here touches the
desktop by itself.

- `benchmarks/baseline.py` — outcome vocabulary, separate counters
  (`success` / `missing_target` / `declined` / `wrong_dispatch` / `unknown`)
  plus the `false_success` flag, timing summaries and the JSON baseline recorder.
- `benchmarks/vision_cost.py` — tokens, latency and grounding accuracy of your configured provider on
  synthetic images (window crop vs full screen, coordinate formats, reasoning on/off). Never touches the
  desktop; stops at the first rejected-key error.
- `benchmarks/ab/` — A/B benchmark: the same Claude model on 12 Windows tasks with native computer use vs
  through Clickwarden, counting heavy- and vision-model tokens from API usage. Unlike the files above, its
  runner drives the real desktop; see [ab/README.md](ab/README.md).
- `benchmarks/results/` — generated baseline reports (gitignored).

The WF-01 fixture failure suite lives in `tests/fixtures/` (seeded layout, page
host, receipt oracle) and is driven by the opt-in live runner
`tests/live_baseline.py`:

```powershell
.\.venv\Scripts\python.exe tests/live_baseline.py --seed 1729
```

It opens a disposable Edge/Chrome window, uses the shared cursor and sends no
vision requests. Run it only on a quiet desktop, never while someone is
working. Each attempt is scored from fixture-side receipt events; a screen
change or a caller "done" never counts as verified completion, receipts override
a tool's refusal/dispatch claim, and duplicate activation is not success. The
runner never auto-resumes a pause: any pause stops the suite and waits for an
explicit user resume.
