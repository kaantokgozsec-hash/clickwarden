# Safety and data-flow notice

Clickwarden can operate your computer as you. Read this once before using it.

## What it does

- Takes screenshots of the window or region being worked on.
- Sends them, with text prompts, to a **vision model you configure**.
- Moves your real mouse, clicks, scrolls, drags and types on your real desktop, with your
  permissions, inside whatever applications and accounts are open.

## What leaves your computer

| Sent to your vision provider | Not sent by Clickwarden |
|---|---|
| JPEG screenshots of the working window or region (everything visible in them) | your files (Clickwarden uploads none) |
| the task text, questions and short summaries of recent actions | your API key to anyone but that provider |
| | analytics or telemetry (there is none) |

With a **cloud** provider, that service receives the screenshots and may log or store them under
its own terms. Clickwarden cannot see or control what it does with them. Read the provider's privacy
policy and terms before accepting. With a **local** server (loopback address only) nothing is sent
over the internet.

Screenshots may be written to disk only if you enable the optional screenshot-export tool
(`CLICKWARDEN_TEXT_ONLY=0`). Session and job records (action names, statuses, timings, error text,
task goals) are saved under `~/.clickwarden/`; they contain no images. Settings and the encrypted key
live in `%USERPROFILE%\.clickwarden\config`.

## Things that can go wrong

- **Wrong clicks and typing.** Vision models misread screens. Clickwarden refuses unclear answers
  and checks focus and occlusion, but it cannot prove a click was the right one.
- **Prompt injection.** Text on a web page or document can try to instruct an AI agent driving
  Clickwarden. Treat everything read from the screen as untrusted data.
- **Irreversible actions.** Sending, buying, deleting and publishing cannot be undone by Clickwarden.
- **Privacy leaks.** Anything on screen goes to the provider, including notifications that pop up
  during a session.
- **Shared cursor.** You cannot use the mouse while it works, and touching it pauses the session.

## Use it responsibly

- Operate only computers and accounts you are authorised to use.
- Keep banking, password managers, private chats and other people's data off screen.
- Prefer a dedicated browser profile or a disposable window for experiments.
- Watch the session. Never leave it running unattended.
- For sensitive work use a local model, or no automation at all.
- If you let an AI agent use Clickwarden, tell it what is off limits.

## Stopping it

The large red **STOP** button on the control panel (always visible, on the collapsed bar too) ends the
session and releases input at once. **Cancel** stops only the current action. **Ctrl+Alt+F12** aborts
immediately. Physically moving or clicking the mouse, or pressing a key, pauses automation. The panel
also shows, permanently, where your screenshots are being sent.

## Who is responsible

Clickwarden is provided "as is" without warranty (Apache-2.0, sections 7 and 8). You are
responsible for how you use it, for the terms of any service you connect, and for complying with
the law and with the rules of the computers and accounts you operate.
