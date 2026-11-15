# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Copilot, Cursor, …) working with vedun: a local knowledge base for
technical documents with a dashboard, a REST API and an MCP server, shipped as one container image.

## Hard rules (always)

- **Never handle the user's secrets in the conversation.** Do not ask the user to paste API keys or passwords into the
  chat, do not print them, do not put them in files. Keys go into the OS secret store through `vedun keys` (macOS/Linux)
  or `.\vedun.ps1 setup` / `.\vedun.ps1 keys` (Windows), which the user runs in their own terminal, or into the
  environment for one command. No `.env` files.
- **If an antivirus or security tool blocks, quarantines or warns about a file or command, stop and tell the user**
  what was flagged and when. Never rewrite code to get past a scanner, never disable or probe security software.
- **Ask, don't guess, about anything the user will see or pay for** (install folder, language, which keys, connecting
  an agent). Use your host's interactive question tool when it has one (for example `AskUserQuestion` in Claude Code),
  with a recommended default first.
- **Do not delete the user's data.** The install folder (`data/`, `pdf/`, `notes/`) holds their knowledge base; remove
  it only when the user explicitly asks, after a second confirmation.
- Respect the terms of use of the documents being indexed: with embeddings on, document text is sent to the embedding
  API (OpenRouter by default).

## Tasks and the skill to follow

| The user wants to … | Follow |
|---|---|
| install vedun, connect an AI agent to it | [`skills/vedun-install/SKILL.md`](skills/vedun-install/SKILL.md) |
| add or update documents, fix rejected files, set up the profile for their numbering | [`skills/vedun-add-documents/SKILL.md`](skills/vedun-add-documents/SKILL.md) |
| find out why something does not work | [`skills/vedun-doctor/SKILL.md`](skills/vedun-doctor/SKILL.md) |
| answer questions from the documents | the MCP tools (`kb_lookup`, `kb_fulltext`, `kb_search`, `kb_verify_quote`, …); their descriptions say which to use |

To make the skills available in every project, copy the folders: `cp -r skills/* ~/.claude/skills/` (Claude Code) or
the equivalent folder of your agent.

## Working on the code

- Python 3.13, [uv](https://docs.astral.sh/uv/). Package `vedun/` (core library), `vedun/server/` (REST API, jobs,
  MCP), `vedun/web/` (dashboard: Jinja2 + HTMX, English msgids, Polish catalogue in `vedun/web/catalog_pl.py`).
- Setup and checks (the same as CI, `.github/workflows/ci.yml`):

  ```bash
  uv sync
  uv run ruff check . && uv run ruff format --check .
  uv run python -m pytest -q
  ```

  On Windows, if `uv run <tool>` fails with "Failed to canonicalize script path", use `.venv\Scripts\python.exe -m <tool>`.
- Tests never use the network or real standards: they run on an invented corpus ("ACME Safety Code",
  `tests/data/test_profile.json`). Keep it that way; do not add text from real standards to the repository.
- Nothing corpus-specific in code: document numbering, labels, citation formats, the source catalogue and the dashboard
  subtitle come from the profile (`vedun.config.json`, see `docs/reference.md`).
- Every user-visible string goes through `_()` / `t()`; add its Polish entry to `catalog_pl.py`
  (`tests/test_web_i18n.py` checks coverage).
- The HTTP layer opens the database read-only; only the job worker writes (`sync`). Do not run a host `sync` while
  the container runs.
- Security model (do not weaken): fail-closed auth, `Host` allow-list, cross-site guard (`X-Requested-With: vedun` on
  POST and `/api/search`), rate limit, audit log without content. See `docs/reference.md#security`.
- Installer scripts: `scripts/` (`install.sh`, `vedun.sh` for macOS/Linux, `vedun.ps1` for Windows). They must stay
  readable and conventional: no download-and-execute, no obfuscation, no changes to execution policy, registry or
  security settings. CI runs `bash -n`, shellcheck and the PowerShell parser on them.
- Commits: Conventional Commits (`feat(web): …`, `fix(server): …`), one logical change per commit.
