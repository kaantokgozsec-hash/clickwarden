# Connecting Clickwarden to an AI agent

Read [SAFETY.md](../SAFETY.md) first. Clickwarden drives your real mouse and keyboard.

## The idea (and its honest limits)

Normal "computer use" sends every screenshot to the expensive model that is also planning the task.
Clickwarden splits the roles:

```
 Planner (Claude, Codex, any MCP client)      Clickwarden (this repo)            Vision model (you choose)
 decides WHAT to do, in text          ---->   captures the screen,        ---->  looks at the pixels,
 "click the Save button"                      checks, moves the mouse            answers "box at x,y"
 gets back a short text result        <----   returns compact text
```

- The planner **never receives screenshots**; it sends short text tool calls and reads short text
  results. Pixels go only to the cheap vision model you configured (or to a local model).
- Cost therefore depends on your vision model's price, not the planner's image-token price.
  **This is a design goal, not a measured guarantee.** A weak vision model can miss targets and
  cost retries, and the planner still pays for its own text turns. Measure it on your own tasks:
  sessions report token counts and, if you enter prices in the settings window, an estimated
  cost (`fast_session_end` returns the metrics).
- Task order comes from the planner (or you). Clickwarden does not plan; it executes one small
  grounded step at a time and refuses when it cannot find a target.

## 1. Install and configure (you, not the agent)

```powershell
git clone https://github.com/kaantokgozsec-hash/clickwarden
cd clickwarden
python -m venv .venv
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\clickwarden configure     # pick provider, type your key, accept or decline
```

Choosing the provider, typing the key and accepting data sharing are deliberately **human-only**
steps in that window. An agent must not do them for you. The window's **My data** tab shows
everything stored and lets you delete it at any time.

Check your provider on a harmless test window (sends only that window):

```powershell
.\.venv\Scripts\python scripts\provider_check.py
```

## 2. Register the MCP server with your agent

Use the full paths of your checkout.

**Claude Code**

```powershell
claude mcp add clickwarden -- C:\path\to\clickwarden\.venv\Scripts\python.exe C:\path\to\clickwarden\clickwarden_mcp.py
```

**Codex CLI** (`~/.codex/config.toml`)

```toml
[mcp_servers.clickwarden]
command = "C:\\path\\to\\clickwarden\\.venv\\Scripts\\python.exe"
args = ["C:\\path\\to\\clickwarden\\clickwarden_mcp.py"]
```

**Claude Desktop and most other clients** (`mcpServers` JSON)

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

Restart the client (and the MCP process) after changing settings. The agent should call
`fast_capabilities` once to confirm the server loaded and see where the screen would be sent.

## 3. Teach the agent the rules (the "skill")

The MCP server already sends the agent a safety notice when it connects. To add the full
operating procedure:

- **Claude Code:** copy [`skills/clickwarden`](../skills/clickwarden/SKILL.md) to
  `%USERPROFILE%\.claude\skills\clickwarden\` (or your project's `.claude/skills/`). The agent then
  loads it whenever you ask it to operate your computer.
- **Codex and other agents:** point them at [AGENTS.md](../AGENTS.md) (Codex reads it automatically
  from the repository root; elsewhere paste it into the agent's instructions).

## 4. Try a safe demo

1. Run `clickwarden configure` once and confirm the provider.
2. Open Notepad yourself and leave it empty.
3. Ask the agent:
   > Use Clickwarden to type the words "hello from clickwarden" into the Notepad window. Tell me
   > which provider receives my screen before you start. Stop if anything unexpected appears.
4. Watch the control panel: it shows `Screens → <host>` and a large red **STOP**. `Ctrl+Alt+F12`
   also ends everything.
5. Close Notepad without saving. Nothing else should have been touched.

Do not demo it on banking, mail, password managers or anything with other people's data.

## What the agent's session looks like

1. `use_my_computer` opens a session and the control panel (the panel must exist before apps launch).
2. `fast_controls` / `fast_click_control` for things Windows exposes (no vision needed, free);
   `fast_click`, `fast_type`, `fast_inspect` for the rest (one cheap vision call each).
3. `fast_session_end` closes it and returns usage numbers.

Full reference: [TOOLS.md](TOOLS.md).
