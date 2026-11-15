---
name: vedun-install
description: Install vedun (local document knowledge base with dashboard, REST API and MCP server) on the user's computer and connect an AI agent to it. Use when the user asks to install, set up, update or reinstall vedun, or to connect Claude Code or another MCP client to it. Works on Windows, macOS and Linux with Docker or Podman.
---

# Install vedun

Goal: vedun runs at `http://127.0.0.1:8765`, the user can log in to the dashboard, and (if they want) their AI agent
is connected over MCP. Target users are often not programmers: explain each step in one plain sentence.

## Rules

- Ask with your host's interactive question tool (e.g. `AskUserQuestion`), recommended option first.
- Never ask for keys or passwords in the chat and never print them. The user types keys in their own terminal; for an
  unattended macOS/Linux install they may be exported in the environment of the install command.
- Antivirus warning or quarantine → stop, tell the user what was flagged, do not work around it.
- Do not change system security settings for the user. If a step needs one (e.g. PowerShell execution policy), show
  the command and let the user decide and run it.

## 1. Check prerequisites

Run `docker info` (or `podman info`) and `docker compose version`.
- Not installed → tell the user to install Docker Desktop (Windows/macOS: https://docs.docker.com/desktop/) or Docker
  Engine / Podman (Linux), then continue. Do not install it yourself.
- Installed but not running → ask them to start Docker Desktop (or `sudo systemctl start docker`).
- Check that port 8765 is free (or plan `--port`).

## 2. Ask the user (one round)

1. Install folder (default `~/vedun`, Windows `%USERPROFILE%\vedun`).
2. Dashboard language: English or Polish.
3. OpenRouter key (meaning-based search; https://openrouter.ai/keys): now, later, or not at all. Without it exact and
   full-text search still work. Typesafe key (optional reranker, https://typesafe.ai): same choice.
4. Connect Claude Code (if `claude` is on PATH) or print settings for another client.

## 3a. macOS / Linux

If the user has the keys ready, ask them to export them in your shell session themselves, or run the installer
interactively in their terminal. Unattended:

```bash
curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh -o /tmp/vedun-install.sh
bash /tmp/vedun-install.sh --yes --json --dir "$HOME/vedun" --lang en --connect print   # add --skip-openrouter etc.
```

Read stdout line by line (one JSON object per step). A line with `"status":"fail"` ends the run: show `message` and
`fix`. `--dry-run` first is a good idea when the user is unsure. The last line is `{"step":"done",…,"url":…}`.
The login is not in the JSON output: the user runs `vedun keys show-password` in their terminal.

## 3b. Windows (no installer script)

Run in PowerShell, in the install folder:

```powershell
mkdir $HOME\vedun; cd $HOME\vedun
iwr https://raw.githubusercontent.com/numikel/vedun/main/compose.release.yaml -OutFile compose.yaml
iwr https://raw.githubusercontent.com/numikel/vedun/main/scripts/vedun.ps1 -OutFile vedun.ps1
Unblock-File .\vedun.ps1
```

Offer the user to read `vedun.ps1` first (it is short). Then the user runs `.\vedun.ps1 setup` **in their own
terminal** (it asks for the keys with hidden input), and you run `.\vedun.ps1 start`. If PowerShell refuses to run
scripts, show `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and let the user decide.

## 4. Verify

- macOS/Linux: `vedun doctor --json` → no `"status":"fail"` lines. Windows: `.\vedun.ps1 status`.
- `curl -s http://127.0.0.1:8765/api/health` → `{"status":"ok",…}`.
- Ask the user to open the dashboard (`vedun open`) and log in.

## 5. Connect an agent

- Claude Code on macOS/Linux: the installer's `--connect claude` runs `claude mcp add` itself (the key then lives in
  Claude Code's config; say so).
- Otherwise give the user this command to run in their terminal, with the key from `vedun keys show-api-key`
  (Windows: `.\vedun.ps1 keys show`):
  `claude mcp add --scope user vedun --transport http http://127.0.0.1:8765/mcp --header "Authorization: Bearer <API key>"`
- Other clients: the dashboard page **MCP connection** (`/connect`) has ready-made configurations.
- After connecting, a new agent session sees the `kb_*` tools.

## 6. Finish

Tell the user: the dashboard address, where to put PDFs (`<folder>/pdf`, named `<doc_id>_<YYYYMM>.pdf`; see the
`vedun-add-documents` skill), and the everyday commands (`vedun start|stop|status|open|keys`, Windows `.\vedun.ps1 …`).
If anything failed, continue with the `vedun-doctor` skill.
