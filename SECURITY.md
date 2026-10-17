# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue. Use GitHub's
**Security → Report a vulnerability** form on this repository. Include what you found, how to reproduce it
and the version (`pip show clickwarden`). I will acknowledge
reports as soon as I can; this is a one-person hobby project, so there is no guaranteed response time.

## Supported versions

Only the latest release receives fixes while the project is in beta (0.x).

## What is in scope

- Input being sent to the wrong window, or guards (focus, occlusion, takeover, Cancel/Stop) failing.
- The consent gate being bypassed so screenshots are sent without the user's acceptance.
- API-key exposure (logs, files, MCP results, error messages).
- Unsafe handling of model output (a malformed answer producing input, path or command execution).
- Header or URL handling that allows request injection.

## Threat model and honest limits

- The API key is encrypted with Windows DPAPI for your user account. That protects it from other
  Windows users and from someone copying the disk. **It does not protect it from other programs
  running as you.**
- Clickwarden trusts the vision provider you configure with your screenshots; it cannot make a
  provider's retention or logging safe.
- Text visible on screen is untrusted and can attempt prompt injection against an AI agent that
  drives Clickwarden. Safeguards in this repository are fail-closed checks on model output, not a
  defence against a compromised or malicious agent.
- The MCP server has no authentication of its own: any MCP client that can launch the process can
  use it. Only register it with clients you trust.
- Clickwarden has no sandbox. It runs with your privileges.
