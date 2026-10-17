# Clickwarden

Windows computer use for AI agents: capture the screen, ask a vision model where things are,
and drive your real mouse and keyboard, with a visible control panel and hard stops.
It exposes a text-only MCP server and a CLI. Open source, Apache-2.0.

> ## ⚠️ Read this before you run it
>
> **Clickwarden is invasive by design.** While a session runs it moves your real cursor,
> clicks and types on your real desktop, **as you**, in whatever application is in front.
> A mistake, a wrong click or a malicious web page can send messages, delete files, spend
> money or change settings in accounts you are signed in to.
>
> **Your screen leaves your computer.** Screenshots of the working window are sent to the
> vision provider **you** choose. Everything visible in those pixels (private messages,
> names, balances, one-time codes) goes to that service, which may store it under its own
> privacy policy. Pick a **local** model (Ollama, LM Studio) to keep it all on your machine.
>
> **You are responsible for what you automate.** Use it only on computers and accounts you
> are allowed to operate. Keep it away from banking, password managers and other people's
> private data. Do not leave a session unattended. Details: [SAFETY.md](SAFETY.md).
>
> Status: **beta (v0.1.0)**. Windows 10/11 only. No warranty (see [LICENSE](LICENSE)).

## About this project (the honest version)

I am Kaan, a second-year computer engineering student, and this is a **hobby project**. I built it
by "vibe coding": directing AI coding assistants and testing the results, not by writing every line
myself or having it professionally reviewed. AI agents that operate a computer ("computer use") are a
very recent topic, and I wanted to learn how they work and contribute something open instead of just
watching. I have run the offline test suite and tried it on my own machine, but that is all the
assurance there is.

So please treat it as an experiment: read the code, expect rough edges, do not trust it with anything
you cannot afford to lose, and tell me what is wrong. Issues, corrections and criticism are welcome.
Product and service names mentioned here (OpenAI, Google Gemini, NVIDIA, OpenRouter, Ollama, LM Studio, Windows)
belong to their owners; Clickwarden is not affiliated with or endorsed by any of them.

## What you can inspect and control

- **You choose the provider and accept the data flow.** Nothing is sent until you run
  `clickwarden configure`, read the notice and accept it by **typing the provider's host name**
  (a tick box is too easy to click through). Acceptance is bound to that host; change the
  host and you must accept again. `clickwarden configure --revoke-consent` (or the *Revoke
  consent* button) withdraws it. Loopback servers need no acceptance because nothing leaves
  the machine.
- **You always see where your screen goes.** The control panel permanently shows
  `Screens → <host>` (or `Screens stay on this computer`) next to a large red **STOP** button.
- **An AI agent cannot do that for you.** Choosing a provider, entering the key and accepting
  data sharing happen only in the settings window, never through the MCP tools.
- **Your key is stored encrypted** with Windows DPAPI (not in the repo, not in plain text).
- **Honest identity:** requests carry `User-Agent: clickwarden/<version>`. Clickwarden does not
  pretend to be another company's client.
- **You can read all of it.** About 7k lines of Python, no binaries, no telemetry. The only network client is the vision
  provider connection in `clickwarden/vision.py` (`git grep "requests"` to check); the browser you
  drive naturally talks to whatever sites you visit.

## How it works

```
capture the window -> vision model (your choice) returns a box -> safety checks -> SendInput
                                  \-> overlay + panel show what it is about to do
```

Prefer exposed controls before pixels: `fast_controls` / `fast_click_control` read the Windows
UI Automation tree and click a control only after re-reading and hit-testing it. Vision is the
fallback for things that expose no controls.

## Quick start

Requires Windows, Python 3.11+ (developed and tested on 3.14).

```powershell
git clone https://github.com/kaantokgozsec-hash/clickwarden
cd clickwarden
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\clickwarden configure
```

`clickwarden configure` opens a window where you pick a provider, enter the model and key, read
exactly what will be sent, and accept or decline. Paste only the key itself into the key box (the window refuses code samples and huge pastes). If pasting is a
problem, `clickwarden configure --set-key` takes the key in the terminal with hidden input.
`clickwarden configure --show` prints the
non-secret settings. Then try:

```powershell
.\.venv\Scripts\clickwarden doctor
.\.venv\Scripts\clickwarden inspect "What application is in front?"   # one screenshot goes to your provider
```

### Use it from an MCP client

Add a server entry to your client's MCP configuration (the format varies by client):

```json
{
  "mcpServers": {
    "clickwarden": {
      "command": "C:\\path\\to\\clickwarden\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\clickwarden\\clickwarden_mcp.py"]
    }
  }
}
```

The server sends the connecting AI a safety notice and every session start repeats where the
screen is sent. Tool reference: [docs/TOOLS.md](docs/TOOLS.md). AI agents should also read
[AGENTS.md](AGENTS.md). Restart the MCP process after changing settings.

## Providers and local models

Any server that accepts OpenAI-style `chat/completions` with image input can work. The settings
window has starting presets (Ollama, LM Studio, OpenRouter, Gemini's OpenAI-compatible endpoint,
OpenAI, NVIDIA's API catalog, custom). **The maintainer has not tested these presets or any particular model**; model
names change, so choose a vision-capable model yourself.

**Can local vision models run this on their machine? Yes, in principle.** Point it at
`http://localhost:11434/v1/chat/completions` (Ollama) or LM Studio's local server, load a vision
model, and no screenshot leaves your computer. Practical notes:

- Accuracy is the real limit. Clicking needs precise boxes; small models can be sloppy. Clickwarden
  validates every answer and refuses to click on absent, ambiguous or malformed results, so a weak
  model costs time and retries, not stray clicks. Try `clickwarden locate "the Start button"` first.
- Some models answer on a 0-1000 grid instead of pixels (some Qwen-VL versions do). Set
  *Coordinate format* to `norm1000` in the settings window if clicks land in the wrong place.
- Local servers can be slow on large screenshots; the window-crop and Turbo modes help.
- Reasoning models may spend their token budget before answering; keep thinking off
  (`CLICKWARDEN_THINKING=0`, default) or raise `CLICKWARDEN_LOCATE_MAX_TOKENS`.

## Built-in safety controls

| Control | What it does |
|---|---|
| Panel **STOP** (large, red, always visible) | ends the session and releases input at once |
| Panel **Cancel** | stops the current action; the session stays open |
| **Ctrl+Alt+F12** | abort immediately |
| Physical mouse or keyboard use | pauses automation until you press Resume |
| Foreground guard | refuses input if focus moved off the target window |
| Occlusion check | refuses to click a point covered by another window |
| Budgets | per-session limits on actions, requests and time |
| Fail-closed grounding | absent / ambiguous / out-of-bounds model answers never produce a click |
| Focus-checked typing | refuses to type when focus is clearly not a text field |

These reduce risk; they do not remove it. Vision "verification" is a model's opinion, not proof.

## Known limits

- One shared Windows cursor: you cannot use the mouse while it works. It is **not** a
  background or invisible controller.
- It cannot (and should not) operate the secure desktop, UAC prompts or other users' sessions.
- Applications with elevated privileges may reject input from a normal process.
- Tested mainly on Windows 11 with one developer's setup. Expect rough edges.

## Repository map

`clickwarden/vision.py` provider client · `providers.py` settings, consent, key storage ·
`configure.py` settings window · `input.py` Win32 input and guards · `engine.py` actions ·
`sessions.py` MCP sessions · `accessibility.py` + `uia_probe.ps1` UI Automation reads ·
`grounding.py` fail-closed answer validation · `server.py` MCP tools · `tests/` offline suite.

Preview the panel without running anything: `python scripts/preview_panel.py`.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The default suite sends no input and makes no network calls. Files named `tests/live_*.py` are
opt-in, drive real windows and may use your provider quota; run them on a quiet desktop.

## License and contributing

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Developed with the assistance of AI
coding tools; review the code before trusting it with your desktop.
