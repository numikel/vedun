# Installer and launcher

| File | Platform | What |
|---|---|---|
| `install.sh` | macOS, Linux (bash 3.2+) | installer: checks Docker/Podman, creates the folder, stores the keys, starts vedun |
| `vedun.sh` | macOS, Linux | the `vedun` command (installed as `~/.local/bin/vedun`); the installer also uses it as a library |
| `vedun.ps1` | Windows (PowerShell 5.1 and 7) | launcher kept next to `compose.yaml`; there is no Windows installer (see the README) |

The installer saves [`../compose.release.yaml`](../compose.release.yaml) as `<install folder>/compose.yaml`. Folders
next to it: `data/` (knowledge base, profile `vedun.config.json`, reports), `pdf/`, `notes/`.

## install.sh

```
curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh | bash                  # interactive
curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh | bash -s -- --yes --json   # unattended
```

| Flag | Meaning |
|---|---|
| `--yes` | no questions, take the defaults; fails clearly if an answer is required |
| `--json` | one JSON object per step on stdout (`{"step","n","of","status","message",…}`, last line `{"step":"done",…}`); human text on stderr |
| `--dry-run` | print every change it would make, change nothing |
| `--dir <path>` | install folder (default `~/vedun`) |
| `--engine docker\|podman` | container engine (default: the one that runs; Docker first) |
| `--port <n>` | local port (default 8765) |
| `--lang en\|pl` | dashboard language (default from the system locale) |
| `--skip-openrouter`, `--skip-typesafe` | do not ask for these keys |
| `--dash-user <name>` | dashboard user (default `vedun`) |
| `--image <ref>` / `--from-source <path>` | another image / build the image from a local checkout |
| `--connect claude\|print\|none` | connect Claude Code, print the MCP settings, or nothing (default: ask; with `--yes`: print) |
| `--no-start` | install without starting |
| `--secret-store libsecret\|file\|prompt` | Linux only: where the keys are kept |

Keys for unattended runs come from the environment (`OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`), never from flags (flags
are visible to other users in the process list). Exit codes: 0 ok, 1 cancelled, 2 missing prerequisite, 3 a step failed.

Running it again on an existing installation offers update, new keys or reinstall; the API key is never replaced
without asking, because connected agents would lose access.

## The vedun command (macOS, Linux)

`vedun start | stop | status | logs [--tail N] | update | open | keys [openrouter|typesafe|api-key|password|show-api-key|show-password] | doctor | uninstall | version`,
with `--json`, `--dry-run` and `--yes`. `doctor` prints one line per check (engine, compose, folders, keys present,
port, health, container, image) with a fix for each problem. `uninstall` removes the container, the model cache, the
launcher, the PATH line, the install record and the keys; your folder only after a second question whose default is no.

The dashboard password and the API key are never written into `--json` output: agents read that output and keep it in
their transcripts. A person reads them in a terminal with `vedun keys show-password` / `show-api-key`.

## Where the keys are kept

| System | Store | Notes |
|---|---|---|
| Windows | DPAPI, current user (`%LOCALAPPDATA%\vedun\secrets\*.dpapi`) | only your Windows account can decrypt them |
| macOS | Keychain (`security`, service `vedun`) | the value goes to `security -i` on stdin, not on a command line |
| Linux desktop | Secret Service via `secret-tool` | install `libsecret-tools` (Debian/Ubuntu) or `libsecret` (Fedora, Arch) |
| Linux server | file store `~/.config/vedun/secrets/` (mode 600) or `prompt` (typed at every start) | chosen at install when no Secret Service runs |

`vedun start` reads the keys and hands them only to the `compose` child process. Keys already present in the
environment win, so a secrets injector works too: `kprun run vedun -- vedun start` (kprun:
[github.com/numikel/kprun](https://github.com/numikel/kprun)).

## Podman

`podman compose` must work (it uses `podman-compose` or a compose provider). Rootless Podman runs the service as
uid 0 inside the container, which maps to your own user on the host, so files in `data/` and `pdf/` stay yours.
Docker on Linux runs it under your uid; Docker Desktop (macOS, Windows) under the image's user.

## For AI agents

Drive the installer with `--yes --json` and the keys in the environment; parse one JSON object per line; treat a line
with `"status":"fail"` as the end and show its `message` and `fix` to the user. Check the result with
`vedun doctor --json`. Never ask the user to paste keys into the chat: ask them to run `vedun keys` in their terminal.

## Environment overrides (tests, unusual setups)

`VEDUN_RAW_BASE` (download base), `VEDUN_INSTALL_RECORD` (path of `install.json`), `VEDUN_BIN_DIR` (launcher folder),
`VEDUN_SECRET_SERVICE` (store service name), `VEDUN_START_TIMEOUT` (seconds), `NO_COLOR`; on Windows `VEDUN_PORT` and
`VEDUN_SECRET_DIR`. `COMPOSE_PROJECT_NAME` overrides the compose project name `vedun` (useful when a development copy
already runs under that name).
