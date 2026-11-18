---
name: vedun-doctor
description: Diagnose and fix a vedun installation (local document knowledge base in Docker/Podman) — the dashboard does not open, the MCP tools are missing, search returns nothing or only full-text results, a sync fails, or the container is unhealthy. Use whenever the user reports that vedun does not work as expected.
---

# vedun doctor

Work from evidence: run the checks, read the output, then propose one fix at a time and verify it.

## 1. Collect the facts

- macOS/Linux: `vedun doctor --json` (one JSON line per check with `status` and `fix`) and `vedun status`.
- Windows: `.\vedun.ps1 status` in the install folder, plus `docker info`, `docker compose ls`.
- Always: `curl -s http://127.0.0.1:8765/api/health` and the last log lines: `vedun logs --tail 100`
  (Windows: `docker compose -f compose.yaml logs --tail 100` in the install folder).

Never print or ask for keys while diagnosing: `status` and `doctor` only say whether a key is set.

## 2. Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker info` fails | Docker Desktop / the daemon is not running | start it, wait until it runs, `vedun start` |
| port 8765 in use by another program | another service | free it, or reinstall with `--port 8766` (Windows: `$env:VEDUN_PORT`) |
| every route answers 503, health is `ok` | the dashboard login (`VEDUN_DASH_USER`, `VEDUN_DASH_PASSWORD`) is missing | `vedun keys` / `.\vedun.ps1 setup`, then `vedun start` (keys are passed only at start) |
| only `/api` or `/mcp` answer 503, the dashboard opens | no key could open them: no `VEDUN_API_KEY` and no active client key | create a key on the dashboard page **MCP connection**, or set the master key with `vedun keys api-key` |
| 403 `insufficient_scope` for the agent or a script | a client key without that permission | create a key with the permission on **MCP connection** (MCP for agents, REST read / write for scripts) |
| 401 in the browser or for the agent | wrong login / old, revoked or expired key | `vedun keys show-password` / `show-api-key` (Windows `keys show`) in the user's terminal; reconnect the agent after a key rotation |
| MCP tools missing in the agent | agent not connected, or connected before a key rotation | see the `vedun-install` skill, step 5; start a new agent session |
| search answers but `degraded` / full-text only | no OpenRouter key, or the daily budget (`VEDUN_DAILY_BUDGET_USD`, default 3 USD) is used up | add the key with `vedun keys openrouter`, or wait for the next UTC day |
| results not reranked (`rerank_error`) | no Typesafe key (optional) | add it with `vedun keys typesafe`, or ignore |
| a PDF is missing after sync | file name does not match `<doc_id>_<YYYYMM>.pdf` / `doc_id_pattern`, or conversion failed (file in `pdf/.incoming/rejected/`) | see the `vedun-add-documents` skill |
| sync very slow or killed | long PDF, little memory for Docker | give Docker more memory (Docker Desktop settings, 8–16 GB); `fast_table_documents` for table-heavy documents |
| container `unhealthy`, log says the database has an older schema | upgraded image over an old database | `vedun stop`, then run the migration in the container: `docker compose run --rm vedun python -m vedun migrate`, then `vedun start` |
| Linux: permission denied on `data/` or `pdf/` | folder owned by another user | `vedun doctor` shows which folder; fix ownership (`sudo chown -R $USER <folder>`) after the user agrees |
| antivirus quarantined or blocked a vedun file | security software | stop, tell the user what was flagged and when; do not work around it |

## 3. Fix and verify

Propose the fix in one sentence, ask before anything that changes the system or deletes data, apply it, then run the
same check again and show that it passes. If the cause is still unclear after the checks, collect `vedun doctor
--json` and the last 100 log lines (they contain no keys or document text) and suggest opening an issue at
https://github.com/numikel/vedun/issues.
