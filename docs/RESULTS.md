# Measured results

Small, reproducible measurements, with their limits stated next to them. They are not a benchmark of task success;
see [ARCHITECTURE.md §9](ARCHITECTURE.md#9-evaluation) for the evaluation plan.

**Setup for all vision numbers:** one Windows 11 machine; vision model `nvidia/nemotron-3.5-super-vl:free` on
OpenRouter; reasoning off (`{"reasoning": {"enabled": false}}`) and the 0–1000 coordinate grid unless a row says
otherwise.

## 1. Vision benchmark on synthetic images (2026-11-05)

How it was run: `python benchmarks/vision_cost.py`. The test window (heading `PROVIDER CHECK 4417` and a `Verify`
button) is drawn in memory, so the button's true position is known exactly and nothing from the desktop is captured
or sent. 25 requests, none rate-limited. Raw data:
[benchmarks/published/vision_cost_20261105_125630.json](../benchmarks/published/vision_cost_20261105_125630.json).

| Condition | Passed | Tokens per call (in + out) | Median latency | Median distance from button centre |
|---|---|---|---|---|
| Window only, 560×340 (how Clickwarden sends it) | **8/8** | 459 + 35 | **1.70 s** | **3 px** (range 1–4) |
| Read the window's text | 4/4 | 381 + 12 | 1.55 s | n/a |
| Same window inside a 1920×1080 screen | 6/6 | 2,228 + 36 | 2.73 s | 8 px |
| Coordinates requested as image pixels | **0/4** | 429 + 35 | 1.76 s | every answer rejected |
| Reasoning on | 2/3 | 459–918 + 800–1,807 | 6.47 s (max 9.3 s) | 3 px |

What this shows:

- **Window crop vs full screen:** sending only the window used 4.9× fewer image tokens, was 1.6× faster and
  landed closer to the centre (3 px vs 8 px).
- **Reasoning off vs on:** 29× fewer answer tokens and 3.8× faster, with 8/8 passes against 2/3.
- **Wrong coordinate format fails safe:** this model answers on a 0–1000 grid. When its answers were read as
  pixels, the validator rejected all of them, so no wrong click would have been sent.
- **A defect found by this run:** one reasoning-mode failure carried no error message. Empty replies are now
  reported with a reason (commit `7b41cea`).

Limits: one model, one synthetic layout, a plain white window; 25 requests in total. It says nothing about
cluttered real applications or end-to-end task success.

## 2. Cost of one "look" (computed from section 1)

Image cost at the paid OpenRouter price of the same model ($0.15 input / $0.50 output per million tokens; the run
itself used the free tier), compared with what Claude Opus 5.5 charges merely to receive the same image
($4 per million input tokens; Claude counts an image as `ceil(w/28) × ceil(h/28)` tokens, per Anthropic's vision
documentation). Prices read 2026-11-05.

| Image | Clickwarden (vision model, paid price) | Claude Opus 5.5 receiving the image | Ratio |
|---|---|---|---|
| Window 560×340 | $0.000086 | $0.00104 (260 image tokens) | ~12× |
| Screen 1920×1080 | $0.00035 | $0.0108 (2,691 image tokens) | ~31× |

Limits: image cost only. The planner still reads Clickwarden's short text result, and the two models tokenise
images differently, so the ratios are indicative, not a per-task saving.

## 3. Live demos (2026-10-18, small n)

| Task | Vision requests | Vision tokens (in + out) | Outcome |
|---|---|---|---|
| Type a phrase into Notepad | 2 | 1,190 + 110 | Correct text, confirmed by the window title, after fixing a typing defect |
| Skip a track in YouTube Music (Chrome) | 5 | 8,957 + 160 | Track changed (confirmed by a follow-up read); UI Automation exposed no button there, so vision did the work |

The Notepad run exposed a real defect: in the current Notepad, typing a burst of characters, or one every
1–3 ms, dropped and repeated characters. Typing now sends one character every 20 ms and reads the text back
through UI Automation (about 23 ms, no model call); a mismatch is reported as `typed_mismatch`, never as success.

## 4. What building it with an AI assistant cost

The project was built in one long AI-assisted session (Claude Code; Claude Opus 5.5 and Claude Sonnet 5.5) from
2026-10-16 to 2026-11-05. The figure below is computed from the per-request usage that Claude Code records,
priced at Anthropic's published API rates on 2026-11-05. It is an API-equivalent estimate; the session ran on a
subscription.

| | |
|---|---|
| Requests | 426 |
| Tokens re-read from cache | 138.5 M |
| Tokens written to cache | 2.53 M |
| Output tokens (thinking included) | 484 k |
| API-equivalent cost | about $36 (cache reads 44 %, cache writes 40 %, output 16 %) |

Most of the cost was carrying a very long conversation (a median of 250 k tokens of context per request), not the
work itself.
