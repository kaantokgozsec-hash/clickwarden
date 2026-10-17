"""Setup checklist and agent-connection helpers for the settings window.

Everything here is local and read-mostly. It never edits another program's configuration: it only builds
text for the user to copy, and (when the user presses the button) copies one skill file into their own
``.claude/skills`` folder. Nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import mydata, providers

PACKAGE = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE.parent


# ---------------------------------------------------------------- checklist ----

def checklist(config: dict | None = None) -> list[dict]:
    """Setup steps with live status. ``done`` is True, False, or None for "cannot be checked here"."""
    config = providers.load_config() if config is None else config
    settings = providers.resolve()
    has_endpoint = bool(settings.base_url and settings.model)
    key_ok = settings.local or bool(settings.api_key)
    consent = settings.local or providers.consent_given(settings, config)
    check = config.get("last_check") if isinstance(config.get("last_check"), dict) else {}
    check_ok = bool(check.get("ok") and check.get("host") == settings.host and check.get("model") == settings.model)
    return [
        {"id": "provider", "title": "Choose a vision provider and model", "tab": "provider",
         "done": has_endpoint,
         "detail": f"{settings.model} at {settings.host}" if has_endpoint else "Pick a provider on the Provider tab."},
        {"id": "key", "title": "Add your API key", "tab": "provider",
         "done": key_ok and has_endpoint,
         "detail": ("Local server: no key needed." if settings.local else
                    "A key is stored (encrypted for your Windows account)." if settings.api_key else
                    "Type your key into the key box yourself. Never give it to an AI agent.")},
        {"id": "consent", "title": "Accept or decline sending screenshots", "tab": "provider",
         "done": consent and has_endpoint,
         "detail": ("Local server: nothing leaves this computer." if settings.local else
                    f"You accepted sending screens to {settings.host}." if consent else
                    "You are asked when you press Save. Declining is fine; choose a local provider instead.")},
        {"id": "test", "title": "Run the safe test", "tab": "setup", "done": check_ok,
         "detail": (f"Passed on {check.get('at', '?')} ({check.get('model', '?')})." if check_ok else
                    f"The last run on {check.get('at', '?')} did not pass. Model answers can vary, so run it again."
                    if check and check.get("host") == settings.host and check.get("model") == settings.model else
                    "Opens a small test window and sends only that window's picture. Press Run test.")},
        {"id": "agent", "title": "Connect your AI agent", "tab": "connect", "done": None,
         "detail": "Copy the snippet for your agent on the Connect an agent tab. Clickwarden cannot check this step."},
    ]


def record_check(report: dict) -> dict:
    """Remember the outcome of the safe test (non-secret) so the checklist can show it."""
    settings = providers.resolve()
    entry = {"ok": bool(report.get("ok")), "host": settings.host, "model": settings.model,
             "at": time.strftime("%Y-%m-%d %H:%M"),
             "latency_ms": (report.get("locate") or {}).get("latency_ms")}
    config = providers.load_config()
    config["last_check"] = entry
    providers.save_config(config)
    return entry


# ----------------------------------------------------------- agent connection ----

def mcp_launch() -> dict:
    """The command an MCP client should run: python.exe (never pythonw) plus the launcher."""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").exists():
        python = python.with_name("python.exe")
    launcher = REPO_ROOT / "clickwarden_mcp.py"
    if launcher.is_file():  # a checkout: the launcher works from any working directory, no install needed
        return {"command": str(python), "args": [str(launcher)], "needs_install": False}
    return {"command": str(python), "args": ["-m", "clickwarden.server"], "needs_install": True}


def snippets(launch: dict | None = None) -> dict:
    launch = launch or mcp_launch()
    command, args = launch["command"], launch["args"]
    line = subprocess.list2cmdline([command, *args])
    def toml_string(value):  # single-quoted literal strings keep Windows paths readable; fall back if a quote is present
        return f"'{value}'" if "'" not in value else json.dumps(value)
    toml_args = ", ".join(toml_string(a) for a in args)
    return {
        "claude_code": f"claude mcp add clickwarden -- {line}",
        "codex": f"[mcp_servers.clickwarden]\ncommand = {toml_string(command)}\nargs = [{toml_args}]\n",
        "desktop": json.dumps({"mcpServers": {"clickwarden": {"command": command, "args": args}}}, indent=2),
    }


def verify_launch(launch: dict | None = None, timeout: float = 40.0) -> dict:
    """Can that Python import the server (dependencies installed)? Starts nothing and sends nothing."""
    launch = launch or mcp_launch()
    code = (f"import sys; sys.path.insert(0, {json.dumps(str(REPO_ROOT))}); "
            "import mcp, clickwarden.server; print('ok')")
    try:
        done = subprocess.run([launch["command"], "-c", code], capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "detail": f"{type(error).__name__}: {error}"}
    if done.returncode == 0 and "ok" in done.stdout:
        return {"ok": True, "detail": "The server code and its dependencies load."}
    tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["unknown error"]
    return {"ok": False, "detail": tail[0][:200] + " (try: pip install -e . in that Python)"}


# ---------------------------------------------------------------------- skill ----

def skill_source() -> str:
    return (PACKAGE / "assets" / "SKILL.md").read_text(encoding="utf-8")


def skill_target(home: Path | None = None) -> Path:
    return Path(home or Path.home()) / ".claude" / "skills" / "clickwarden" / "SKILL.md"


def skill_status(home: Path | None = None) -> str:
    target = skill_target(home)
    if not target.is_file():
        return "not installed"
    try:
        same = target.read_text(encoding="utf-8") == skill_source()
    except OSError:
        return "installed (cannot be read)"
    return "installed, up to date" if same else "installed, differs from this version"


def install_skill(overwrite: bool = False, home: Path | None = None) -> dict:
    """Copy the one skill file into the user's .claude/skills folder. Never follows links, never edits anything else."""
    target = skill_target(home)
    if mydata._is_link(target) or mydata._is_link(target.parent):  # symlink or junction
        return {"ok": False, "detail": "The skills folder contains a link; not writing through it.", "path": str(target)}
    if target.exists() and not overwrite and skill_status(home) != "installed, up to date":
        return {"ok": False, "needs_overwrite": True, "path": str(target),
                "detail": "A different Clickwarden skill file already exists."}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill_source(), encoding="utf-8", newline="\n")
    except OSError as error:
        return {"ok": False, "detail": f"{type(error).__name__}: {error}", "path": str(target)}
    return {"ok": True, "detail": "Installed.", "path": str(target)}


FIRST_DEMO_PROMPT = (
    "Use Clickwarden to type the words \"hello from clickwarden\" into the Notepad window I have open. "
    "Before you start, tell me which provider will receive my screenshots. Stop and ask me if anything "
    "unexpected appears. Do not choose providers, enter keys or accept data sharing for me."
)

AGENT_WARNING = ("Once connected, that agent can move your real mouse and keyboard and see what you show it "
                 "through Clickwarden's vision provider. Connect only agents you trust, and stay at the computer.")
