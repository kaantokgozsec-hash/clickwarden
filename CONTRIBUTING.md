# Contributing

Thanks for helping. Clickwarden drives people's real computers, so the bar for safety and honesty is
higher than for most projects.

## Ground rules

- **License:** by submitting a contribution you agree it is licensed under Apache-2.0, the same as
  the project (Apache-2.0 section 5). Only submit code you have the right to contribute and
  say if it came from elsewhere (and under what license).
- **No impersonation:** do not add code that makes requests look like they come from another
  company's software, and do not read other programs' credentials or configuration.
- **No secrets or private data:** no API keys, tokens, `.env` files, screenshots of real desktops,
  usernames or personal paths in code, tests, issues or logs.
- **Fail closed:** malformed, ambiguous or low-confidence model output must refuse to act.
- **Honest docs:** do not claim accuracy, speed or safety you have not measured.
- Keep the consent gate (`clickwarden/providers.py`) intact: nothing is sent to a remote host without
  the user's acceptance for that host.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\python -m unittest discover -s tests -v
```

The default suite is offline and sends no input. `tests/live_*.py` scripts are opt-in, use real
windows and may use provider quota; run them on a quiet desktop and say so in your pull request.

## Pull requests

Keep changes small and focused, add or update offline tests, and describe what you verified and
what you did not. For anything that changes what data leaves the machine, update `README.md`,
`SAFETY.md` and the disclosure text in the same pull request.

## Reporting security problems

Privately, as described in [SECURITY.md](SECURITY.md).
