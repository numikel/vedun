#!/usr/bin/env bash
# vedun installer for macOS and Linux (bash 3.2+). Interactive by default; AI agents use --yes --json.
#   curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh | bash -s -- --yes --json
# Keys for unattended runs come from the environment (OPENROUTER_API_KEY, TYPESAFE_API_KEY), never from flags.
# Other environment: VEDUN_RAW_BASE (download base), VEDUN_INSTALL_RECORD, VEDUN_BIN_DIR, VEDUN_SECRET_SERVICE, NO_COLOR.
set -euo pipefail

RAW_BASE="${VEDUN_RAW_BASE:-https://raw.githubusercontent.com/numikel/vedun/main}"
TOTAL=10
O_JSON=0 O_DRY=0 O_YES=0 O_DIR='' O_ENGINE='' O_PORT='' O_LANG='' O_SKIP_OR=0 O_SKIP_TS=0 O_USER=''
O_IMAGE='' O_SRC='' O_CONNECT='' O_NOSTART=0 O_STORE=''

usage() {
  cat <<'EOF'
vedun installer (macOS, Linux)

Usage: install.sh [options]        or: curl -fsSL <url>/install.sh | bash -s -- [options]

  --yes                 no questions, take the defaults (fails clearly if an answer is required)
  --json                one JSON object per step on stdout, human text on stderr
  --dry-run             show every change it would make, change nothing
  --dir <path>          install folder (default ~/vedun)
  --engine docker|podman
  --port <n>            local port (default 8765)
  --lang en|pl          dashboard language (default from the system locale)
  --skip-openrouter     no OpenRouter key (meaning-based search off, full-text search works)
  --skip-typesafe       no Typesafe key (optional reranker)
  --dash-user <name>    dashboard user name (default vedun)
  --image <ref>         use another image than ghcr.io/numikel/vedun
  --from-source <path>  build the image from a local checkout of vedun
  --connect claude|print|none   connect an AI agent (default: ask; with --yes: print)
  --no-start            install, but do not start the service
  --secret-store libsecret|file|prompt   Linux only: where the keys are kept
  --help

Keys for unattended runs: set OPENROUTER_API_KEY / TYPESAFE_API_KEY in the environment.
Exit codes: 0 ok, 1 cancelled, 2 missing prerequisite, 3 a step failed.
EOF
}

early_die() {
  printf 'x %s\n' "$1" >&2
  if [ $# -gt 1 ]; then printf '  -> %s\n' "$2" >&2; fi
  local m=${1//\"/\'}
  if [ "$O_JSON" = 1 ]; then printf '{"step":"args","status":"fail","message":"%s","exit":2}\n' "${m//\\//}"; fi
  exit 2
}
need_val() { if [ -z "${2:-}" ] || [ "${2#--}" != "$2" ]; then early_die "$1 needs a value." "See: install.sh --help"; fi; }

while [ $# -gt 0 ]; do
  case $1 in
    --yes | -y) O_YES=1 ;;
    --json) O_JSON=1 ;;
    --dry-run) O_DRY=1 ;;
    --dir) need_val "$1" "${2:-}" && O_DIR=$2 && shift ;;
    --engine) need_val "$1" "${2:-}" && O_ENGINE=$2 && shift ;;
    --port) need_val "$1" "${2:-}" && O_PORT=$2 && shift ;;
    --lang) need_val "$1" "${2:-}" && O_LANG=$2 && shift ;;
    --skip-openrouter) O_SKIP_OR=1 ;;
    --skip-typesafe) O_SKIP_TS=1 ;;
    --dash-user) need_val "$1" "${2:-}" && O_USER=$2 && shift ;;
    --image) need_val "$1" "${2:-}" && O_IMAGE=$2 && shift ;;
    --from-source) need_val "$1" "${2:-}" && O_SRC=$2 && shift ;;
    --connect) need_val "$1" "${2:-}" && O_CONNECT=$2 && shift ;;
    --no-start) O_NOSTART=1 ;;
    --secret-store) need_val "$1" "${2:-}" && O_STORE=$2 && shift ;;
    -h | --help) usage; exit 0 ;;
    *) early_die "Unknown option: $1" "See: install.sh --help" ;;
  esac
  shift
done
case ${O_ENGINE:-docker} in docker | podman) ;; *) early_die "--engine must be docker or podman." ;; esac
case ${O_LANG:-en} in en | pl) ;; *) early_die "--lang must be en or pl." ;; esac
case ${O_CONNECT:-print} in claude | print | none) ;; *) early_die "--connect must be claude, print or none." ;; esac
case ${O_STORE:-file} in libsecret | file | prompt) ;; *) early_die "--secret-store must be libsecret, file or prompt." ;; esac
case ${O_PORT:-8765} in '' | *[!0-9]*) early_die "--port must be a number." ;; esac
if [ "${O_PORT:-8765}" -lt 1 ] || [ "${O_PORT:-8765}" -gt 65535 ]; then early_die "--port must be 1-65535."; fi
[ -z "$O_IMAGE" ] || [ -z "$O_SRC" ] || early_die "Use either --image or --from-source, not both."
case $O_USER in *[!A-Za-z0-9._-]*) early_die "--dash-user may contain only letters, digits, dot, dash and underscore." ;; esac

# ---------- get the launcher (it is also our library) and the compose file ----------
TMPD=$(mktemp -d "${TMPDIR:-/tmp}/vedun-install.XXXXXX")
trap 'rm -rf "$TMPD"' EXIT
SRC_DIR=''
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  d=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  if [ -f "$d/vedun.sh" ] && [ -f "$d/../compose.release.yaml" ]; then SRC_DIR=$d; fi
fi
fetch() { # fetch <path in the repository> <destination>
  if [ -n "$SRC_DIR" ]; then cp "$SRC_DIR/../$1" "$2" && return 0; fi
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 2 "$RAW_BASE/$1" -o "$2" 2>/dev/null && return 0
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$RAW_BASE/$1" && return 0; fi
  printf 'x Could not download %s/%s\n  -> Check the internet connection (or set VEDUN_RAW_BASE) and run the installer again.\n' "$RAW_BASE" "$1" >&2
  if [ "$O_JSON" = 1 ]; then printf '{"step":"welcome","n":1,"of":10,"status":"fail","message":"download failed: %s"}\n' "$1"; fi
  exit 3
}
fetch scripts/vedun.sh "$TMPD/vedun.sh"
fetch compose.release.yaml "$TMPD/compose.yaml"
grep -q '^# vedun launcher' "$TMPD/vedun.sh" || early_die "The downloaded launcher is not the vedun launcher." "Check VEDUN_RAW_BASE."
# shellcheck source-path=SCRIPTDIR source=vedun.sh
VEDUN_LIB=1 . "$TMPD/vedun.sh"
JSON=$O_JSON DRY=$O_DRY YES=$O_YES
ui_init

STEP_N=0
step() {
  STEP_N=$1 CUR_STEP=$2
  hum ""
  hum "${C_ACC}${C_B}Step $1/$TOTAL ${S_DOT} $3${C_0}"
}
sreport() { # sreport <ok|skip|warn|fail> <message> [key=value ...]
  local st=$1 msg=$2
  shift 2
  if [ "$JSON" = 1 ]; then jobj "step=$CUR_STEP" "#n=$STEP_N" "#of=$TOTAL" "status=$st" "message=$msg" "$@"; fi
  case $st in ok) line_ok "$msg" ;; warn) line_warn "$msg" ;; fail) line_err "$msg" ;; *) line_dim "$msg" ;; esac
}
die() { # same as the launcher's die, plus the step number
  hum "${C_ERR}${S_ERR} $2${C_0}"
  if [ $# -gt 2 ]; then hum "  ${S_ARR} $3"; fi
  if [ "$JSON" = 1 ]; then jobj "step=${CUR_STEP:-welcome}" "#n=$STEP_N" "#of=$TOTAL" "status=fail" "message=$2" "fix=${3:-}" "#exit=$1"; fi
  exit "$1"
}
trap 'hum ""; die 1 "Cancelled." "Nothing after this point was changed; run the installer again to finish."' INT

# ---------- 1 welcome ----------
step 1 welcome "Welcome"
hum "  vedun = a private knowledge base for your PDFs: dashboard, search API and an MCP server for AI agents."
hum "  This installer checks Docker/Podman, creates a folder, stores the keys in your OS secret store and starts it."
[ "$DRY" = 1 ] && line_dim "dry run: nothing will be changed, every change is only printed"
OS=$(os_kind)
if [ "$OS" = other ]; then
  [ "$DRY" = 1 ] || die 2 "This installer is for macOS and Linux." "On Windows follow the Windows steps in the README."
  line_warn "not macOS or Linux: dry run only (on Windows follow the README)"
fi
if [ "$YES" = 0 ] && ! tty_ok; then
  die 2 "No terminal for the installer's questions (stdin is the script)." "Re-run with --yes (keys in OPENROUTER_API_KEY / TYPESAFE_API_KEY) for an unattended install."
fi
MODE=fresh
if [ -r "$RECORD" ]; then
  rec_load
  hum "  An installation exists in $R_DIR."
  if [ "$YES" = 1 ]; then MODE=update; else
    hum "  1 update (keep keys)   2 reconfigure keys   3 reinstall   4 cancel"
    ask "Choose" 1
    case $ASK_REPLY in 1) MODE=update ;; 2) MODE=keys ;; 3) MODE=reinstall ;; *) die 1 "Cancelled, nothing changed." ;; esac
  fi
fi
sreport ok "mode: $MODE" "mode=$MODE"

# ---------- 2 container engine ----------
step 2 engine "Container engine"
pick=''
if [ -n "$O_ENGINE" ]; then
  engine_usable "$O_ENGINE" && pick=$O_ENGINE
elif [ "$MODE" != fresh ] && engine_usable "$R_ENGINE"; then
  pick=$R_ENGINE
else
  for e in docker podman; do
    if engine_usable "$e"; then pick=$e && break; fi
  done
fi
if [ -z "$pick" ]; then
  e=${O_ENGINE:-docker}
  command -v "$e" >/dev/null 2>&1 || { command -v podman >/dev/null 2>&1 && [ -z "$O_ENGINE" ] && e=podman; }
  if command -v "$e" >/dev/null 2>&1; then
    if [ "$OS" = macos ]; then fix="Start Docker Desktop (or: podman machine start), wait until it runs, then run the installer again."
    else fix="Start it: sudo systemctl start docker (Podman: check 'podman info' and install podman-compose), then run the installer again."; fi
    die 2 "$e is installed but not usable ('$e info' or '$e compose version' fails)." "$fix"
  fi
  if [ "$OS" = macos ]; then fix="Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/ (or Podman: https://podman.io/docs/installation), start it, run the installer again."
  else fix="Install Docker Engine: https://docs.docker.com/engine/install/ (or Podman: https://podman.io/docs/installation), then run the installer again."; fi
  die 2 "No container engine found (vedun runs in Docker or Podman)." "$fix"
fi
R_ENGINE=$pick
if [ -n "$O_SRC" ]; then
  [ -f "$O_SRC/Dockerfile" ] || die 2 "--from-source: no Dockerfile in $O_SRC." "Point it at the root of a vedun checkout."
  O_SRC=$(cd "$O_SRC" && pwd)
fi
sreport ok "$R_ENGINE $("$R_ENGINE" version --format '{{.Server.Version}}' 2>/dev/null || echo '')" "engine=$R_ENGINE"

# ---------- 3 install folder ----------
step 3 folder "Install folder"
dflt=${O_DIR:-${R_DIR:-$HOME/vedun}}
if [ -n "$O_DIR" ] || [ "$MODE" = update ]; then ASK_REPLY=$dflt; else ask "Install folder" "$dflt"; fi
dir=$ASK_REPLY
case $dir in \~) dir=$HOME ;; \~/*) dir="$HOME/${dir#\~/}" ;; /* | [A-Za-z]:/*) ;; *) dir="$PWD/$dir" ;; esac
dir=${dir%/}
p=$dir
while [ ! -d "$p" ]; do p=$(dirname "$p"); done
[ -w "$p" ] || die 2 "Cannot create or write $dir ($p is not writable)." "Choose another folder with --dir <path>."
R_DIR=$dir
R_PORT=${O_PORT:-$R_PORT}
st=ok msg="folder $R_DIR, port $R_PORT"
if port_in_use "$R_PORT" && ! { [ "$MODE" != fresh ] && http_ok "http://127.0.0.1:$R_PORT/api/health"; }; then
  [ "$DRY" = 1 ] || die 2 "Port $R_PORT is already used by another program." "Choose a free port: run the installer with --port 8766"
  st=warn msg="$msg; port $R_PORT is busy: a real install would stop here (use --port <n>)"
fi
sreport "$st" "$msg" "dir=$R_DIR" "#port=$R_PORT"

# ---------- 4/5 optional keys ----------
# optional_key <NAME> <skip flag> <label> <what it does> <where to get it>
optional_key() {
  local n=$1 skip=$2 label=$3
  if [ "$skip" = 1 ]; then sreport skip "$label skipped (add it later: vedun keys)"; return 0; fi
  if [ "$MODE" = update ] && secret_get "$n" >/dev/null 2>&1; then sreport ok "$label kept"; return 0; fi
  if [ -n "${!n:-}" ]; then
    eval "NEW_$n=\${$n}"
    sreport ok "$label taken from the environment"
    return 0
  fi
  if [ "$YES" = 1 ]; then sreport skip "$label: not in the environment, skipped (add it later: vedun keys)"; return 0; fi
  hum "  $4"
  hum "  Get one at: ${C_ACC}$5${C_0}"
  ask_secret "$label (Enter = skip)"
  if [ -z "$ASK_REPLY" ]; then sreport skip "$label skipped (add it later: vedun keys)"; return 0; fi
  eval "NEW_$n=\$ASK_REPLY"
  sreport ok "$label entered"
}
# shellcheck disable=SC2034 # the NEW_<NAME> variables are read through eval in steps 6 and 9
NEW_OPENROUTER_API_KEY='' NEW_TYPESAFE_API_KEY=''
step 4 openrouter "OpenRouter key (optional)"
optional_key OPENROUTER_API_KEY "$O_SKIP_OR" "OpenRouter key" \
  "It makes embeddings for meaning-based search; exact and full-text search work without it." "https://openrouter.ai/keys"
step 5 typesafe "Typesafe key (optional)"
optional_key TYPESAFE_API_KEY "$O_SKIP_TS" "Typesafe key" \
  "An optional reranker that orders search results better." "https://typesafe.ai"

# ---------- 6 secrets ----------
step 6 secrets "API key, dashboard password, secret store"
if [ "$OS" = macos ]; then
  R_STORE=keychain
  [ -z "$O_STORE" ] || line_dim "--secret-store is for Linux; macOS uses the Keychain"
  command -v security >/dev/null 2>&1 || die 2 "The macOS 'security' tool is missing." "Install the Xcode command line tools: xcode-select --install"
elif [ -n "$O_STORE" ]; then
  R_STORE=$O_STORE
  [ "$R_STORE" != libsecret ] || libsecret_ok || die 2 "--secret-store libsecret: secret-tool or a running Secret Service is missing." \
    "Install it (Debian/Ubuntu: sudo apt install libsecret-tools; Fedora: sudo dnf install libsecret; Arch: sudo pacman -S libsecret) or use --secret-store file."
elif [ "$MODE" != fresh ] && [ -n "$R_STORE" ] && [ "$R_STORE" != keychain ]; then
  :
elif libsecret_ok; then
  R_STORE=libsecret
else
  line_warn "No Secret Service found (secret-tool missing or no desktop keyring, e.g. a headless server)."
  if [ "$YES" = 1 ]; then R_STORE="file"; else
    hum "  a  install libsecret and run the installer again"
    hum "  b  keep the keys in files readable only by you ($FILE_SECRETS_DIR, chmod 600)"
    hum "  c  store nothing: you type the keys at every 'vedun start'"
    ask "Choose a, b or c" b
    case $ASK_REPLY in
      a | A) die 2 "Install libsecret, then run the installer again." "Debian/Ubuntu: sudo apt install libsecret-tools gnome-keyring; Fedora: sudo dnf install libsecret; Arch: sudo pacman -S libsecret" ;;
      c | C) R_STORE=prompt ;;
      *) R_STORE="file" ;;
    esac
  fi
fi
[ "$R_STORE" != file ] || line_warn "keys go to plain files readable only by you: $FILE_SECRETS_DIR (chmod 600)"
[ "$R_STORE" != prompt ] || line_warn "keys are not stored: write down the API key and password shown at the end"

OLD_KEY=$(secret_get VEDUN_API_KEY 2>/dev/null || true)
OLD_PASS=$(secret_get VEDUN_DASH_PASSWORD 2>/dev/null || true)
NEW_VEDUN_API_KEY='' NEW_VEDUN_DASH_PASSWORD='' ROTATED=0
if [ -z "$OLD_KEY" ]; then
  NEW_VEDUN_API_KEY=$(gen_api_key)
elif [ "$MODE" = keys ] || [ "$MODE" = reinstall ]; then
  line_warn "A new API key disconnects every AI agent that uses the current one."
  if confirm "Create a new API key?" N; then NEW_VEDUN_API_KEY=$(gen_api_key) && ROTATED=1; fi
fi
if [ -z "$OLD_PASS" ]; then
  NEW_VEDUN_DASH_PASSWORD=$(gen_password)
elif [ "$MODE" = keys ] || [ "$MODE" = reinstall ]; then
  if confirm "Create a new dashboard password?" N; then NEW_VEDUN_DASH_PASSWORD=$(gen_password); fi
fi
DASH_USER=${O_USER:-$(secret_get VEDUN_DASH_USER 2>/dev/null || echo vedun)}
API_KEY=${NEW_VEDUN_API_KEY:-$OLD_KEY}
DASH_PASS=${NEW_VEDUN_DASH_PASSWORD:-$OLD_PASS}
# shellcheck disable=SC2034 # read through eval below
NEW_VEDUN_DASH_USER=$DASH_USER
for n in $SECRET_NAMES; do
  eval "v=\${NEW_$n:-}"
  if [ -n "$v" ] && [ "$R_STORE" != prompt ]; then secret_set "$n" "$v"; fi
done
v='' st=ok
case $R_STORE in file | prompt) st=warn ;; esac
sreport "$st" "keys ready in the $(store_name "$R_STORE")" "secret_store=$R_STORE" "#api_key_rotated=$([ $ROTATED = 1 ] && echo true || echo false)"

# ---------- 7 dashboard language ----------
step 7 language "Dashboard language"
CONF="$R_DIR/data/vedun.config.json"
LANG_SET=''
if [ -z "$O_LANG" ] && [ "$MODE" = update ] && [ -s "$CONF" ]; then
  sreport ok "kept: $(sed -n 's/.*"ui_language"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONF" | head -n 1)"
else
  loc="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
  if [ "$OS" = macos ] && { [ -z "$loc" ] || [ "$loc" = C ]; }; then loc=$(defaults read -g AppleLocale 2>/dev/null || true); fi
  case $loc in pl*) dl=pl ;; *) dl=en ;; esac
  if [ -n "$O_LANG" ]; then LANG_SET=$O_LANG; else
    ask "Dashboard language (en/pl)" "$dl"
    case $ASK_REPLY in pl | PL) LANG_SET=pl ;; *) LANG_SET=en ;; esac
  fi
  sreport ok "dashboard language: $LANG_SET" "lang=$LANG_SET"
fi

# ---------- 8 files ----------
step 8 files "Files and the vedun command"
# write_config <file> <lang>: set ui_language, keep every other key of an existing file
write_config() {
  if [ "$DRY" = 1 ]; then dry "set ui_language=$2 in $1 (other keys kept)"; return 0; fi
  if [ ! -s "$1" ] || tr -d ' \t\r\n' <"$1" | grep -q '^{}$'; then
    printf '{"ui_language": "%s"}\n' "$2" >"$1.tmp"
  elif grep -q '"ui_language"' "$1"; then
    sed 's/"ui_language"[[:space:]]*:[[:space:]]*"[^"]*"/"ui_language": "'"$2"'"/' "$1" >"$1.tmp"
  else
    awk -v l="$2" '!d && (i = index($0, "{")) > 0 { $0 = substr($0, 1, i) "\"ui_language\": \"" l "\", " substr($0, i + 1); d = 1 } { print }' "$1" >"$1.tmp"
  fi
  mv "$1.tmp" "$1"
}
for d in "$R_DIR" "$R_DIR/data" "$R_DIR/pdf" "$R_DIR/notes"; do
  if [ "$DRY" = 1 ]; then [ -d "$d" ] || dry "create folder $d"; else mkdir -p "$d"; fi
done
if [ "$DRY" = 1 ]; then dry "write $R_DIR/compose.yaml"; else
  if [ -f "$R_DIR/compose.yaml" ] && ! cmp -s "$TMPD/compose.yaml" "$R_DIR/compose.yaml"; then cp "$R_DIR/compose.yaml" "$R_DIR/compose.yaml.bak"; fi
  cp "$TMPD/compose.yaml" "$R_DIR/compose.yaml"
fi
[ -z "$LANG_SET" ] || write_config "$CONF" "$LANG_SET"
if [ "$DRY" = 1 ]; then dry "install the vedun command as $BIN_DIR/vedun (chmod 755)"; else
  mkdir -p "$BIN_DIR"
  cp "$TMPD/vedun.sh" "$BIN_DIR/vedun.tmp" && chmod 755 "$BIN_DIR/vedun.tmp" && mv "$BIN_DIR/vedun.tmp" "$BIN_DIR/vedun"
fi
if [ -n "$O_SRC" ]; then R_SOURCE=$O_SRC R_IMAGE=vedun:local; elif [ -n "$O_IMAGE" ]; then R_SOURCE=image R_IMAGE=$O_IMAGE; fi
on_path=1
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) on_path=0 ;; esac
if [ "$on_path" = 0 ] && [ -z "$R_PROFILE" ]; then
  case ${SHELL:-} in
    */zsh) prof="$HOME/.zshrc" ;;
    */bash) if [ "$OS" = macos ]; then prof="$HOME/.bash_profile"; else prof="$HOME/.bashrc"; fi ;;
    *) prof="$HOME/.profile" ;;
  esac
  pline="export PATH=\"$BIN_DIR:\$PATH\" $PROFILE_MARK"
  line_warn "$BIN_DIR is not on your PATH yet. Add this line to $prof:"
  hum "      $pline"
  if [ "$YES" = 0 ] && confirm "Add it to $prof for you?" N; then
    if [ "$DRY" = 1 ]; then dry "append the line above to $prof"; else printf '\n%s\n' "$pline" >>"$prof"; fi
    R_PROFILE=$prof
    line_ok "added; open a new terminal for it to work"
  fi
fi
rec_write
sreport ok "installed: $R_DIR/compose.yaml, $BIN_DIR/vedun, $RECORD" "record=$RECORD" "launcher=$BIN_DIR/vedun" "#on_path=$([ $on_path = 1 ] && echo true || echo false)"

# ---------- 9 start ----------
step 9 start "Start vedun"
URL=$(service_url)
if [ "$O_NOSTART" = 1 ]; then
  sreport skip "not started (--no-start): run 'vedun start' when you are ready"
else
  if [ -n "$O_SRC" ]; then
    hum "  building the image from $O_SRC (a few minutes)"
    if [ "$DRY" = 1 ]; then dry "run: $R_ENGINE build -t vedun:local \"$O_SRC\""
    else "$R_ENGINE" build -t vedun:local "$O_SRC" >&2 || die 3 "Building the image failed." "Read the build output above; fix it and run the installer again."; fi
  fi
  hum "  starting; the first start downloads the image (can take a few minutes)"
  # the keys of this run exist only in memory: give them to the compose child, not to the installer's environment
  (
    for n in $SECRET_NAMES; do
      eval "v=\${NEW_$n:-}"
      if [ -n "$v" ]; then export "$n=$v"; fi
    done
    do_up
  )
  wait_health "$START_TIMEOUT" || die 3 "vedun did not answer on $URL/api/health within $START_TIMEOUT s." "See why: vedun logs --tail 100, then vedun doctor"
  if [ "$DRY" = 1 ]; then sreport ok "dry run: vedun would start at $URL" "url=$URL"; else sreport ok "vedun is running: $URL" "url=$URL"; fi
fi

# ---------- 10 connect an AI agent ----------
step 10 connect "Connect an AI agent"
mode=$O_CONNECT
if [ -z "$mode" ]; then
  if [ "$YES" = 1 ] || ! command -v claude >/dev/null 2>&1; then mode=print; else
    line_warn "Connecting Claude Code stores the vedun API key in Claude Code's own config file."
    if confirm "Connect Claude Code to vedun now?" Y; then mode=claude; else mode=print; fi
  fi
fi
if [ "$mode" = claude ] && ! command -v claude >/dev/null 2>&1; then
  line_warn "the 'claude' command is not on PATH; printing the settings instead"
  mode=print
fi
if [ "$mode" = claude ]; then
  if [ "$DRY" = 1 ]; then
    dry "run: claude mcp add --scope user vedun --transport http $URL/mcp --header \"Authorization: Bearer <API key>\""
  else
    if claude mcp get vedun >/dev/null 2>&1; then
      if [ "$ROTATED" = 1 ] || [ "$MODE" = fresh ]; then claude mcp remove --scope user vedun >/dev/null 2>&1 || true; else mode=kept; fi
    fi
    if [ "$mode" = claude ]; then
      # the key is on this command line for a moment: the claude CLI has no stdin option for headers
      claude mcp add --scope user vedun --transport http "$URL/mcp" --header "Authorization: Bearer $API_KEY" >/dev/null ||
        die 3 "claude mcp add failed." "Run it yourself later; the settings are on the dashboard page $URL/connect"
    fi
  fi
  sreport ok "Claude Code connected (user scope)" "connect=$mode"
elif [ "$mode" = print ]; then
  hum "  Any MCP client (put your API key in VEDUN_API_KEY, or paste it in place of \${VEDUN_API_KEY}):"
  hum "    {\"mcpServers\":{\"vedun\":{\"type\":\"http\",\"url\":\"$URL/mcp\",\"headers\":{\"Authorization\":\"Bearer \${VEDUN_API_KEY}\"}}}}"
  hum "  Ready snippets for other clients: $URL/connect   ${S_DOT}   the API key: vedun keys show-api-key"
  hum "  kprun users (https://kprun.michalsk.pl): kprun mcp -e vedun --bearer VEDUN_API_KEY $URL/mcp"
  sreport ok "connection settings printed" "connect=print" "mcp_url=$URL/mcp"
else
  sreport skip "no agent connected (--connect none); settings: $URL/connect" "connect=none"
fi

# ---------- final screen ----------
hum ""
if [ "$DRY" = 1 ]; then
  hum "${C_OK}${C_B}${S_OK} dry run finished: nothing was changed${C_0} (a real run ends with the dashboard address and how to see the login)"
  if [ "$JSON" = 1 ]; then jobj step=done status=ok "url=$URL" "dir=$R_DIR" "#dry_run=true"; fi
  exit 0
fi
hum "${C_OK}${C_B}${S_OK} vedun is installed${C_0}"
hum "  Dashboard   ${C_ACC}$URL${C_0}   (vedun open)"
if [ "$R_STORE" = prompt ]; then
  # nothing is stored, so this screen is the only place the keys exist: show them even under --json
  hum "  User        $DASH_USER"
  hum "  Password    ${C_B}$DASH_PASS${C_0}   (shown once)"
  hum "  API key     ${C_B}$API_KEY${C_0}   (shown once; you type these at every vedun start)"
else
  if [ -n "$NEW_VEDUN_DASH_PASSWORD" ] && [ "$JSON" = 0 ]; then
    hum "  User        $DASH_USER"
    hum "  Password    ${C_B}$DASH_PASS${C_0}"
  fi
  # --json output is read by agents and lands in their transcripts: the login is shown on request, in a terminal
  hum "  The keys are stored in the $(store_name "$R_STORE"). Show the login: vedun keys show-password; change keys: vedun keys"
fi
hum "  Put your PDFs in $R_DIR/pdf and press Sync in the dashboard (the first sync takes the whole folder)."
hum "  Commands: vedun help   ${S_DOT}   Already use kprun (https://kprun.michalsk.pl)? 'kprun run vedun -- vedun start' works too."
API_KEY='' DASH_PASS=''
if [ "$JSON" = 1 ]; then jobj step=done status=ok "url=$URL" "dir=$R_DIR"; fi
exit 0
