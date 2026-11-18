#!/usr/bin/env bash
# vedun launcher for macOS and Linux (bash 3.2+): start, stop, status, logs, update, upgrade,
# open, keys, doctor, uninstall.
# The installer copies this file to ~/.local/bin/vedun and also sources it as a library (VEDUN_LIB=1).
# Environment overrides: VEDUN_INSTALL_RECORD (path of install.json), VEDUN_BIN_DIR (launcher folder, default
# ~/.local/bin), VEDUN_SECRET_SERVICE (secret store service name, default vedun), VEDUN_START_TIMEOUT (seconds to
# wait for /api/health, default 300), NO_COLOR (plain output).
# Secrets already present in the environment win over the secret store, so `kprun run vedun -- vedun start` works.
set -euo pipefail

VEDUN_VERSION="0.2.0"
SERVICE="${VEDUN_SECRET_SERVICE:-vedun}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
RECORD="${VEDUN_INSTALL_RECORD:-$CONFIG_HOME/vedun/install.json}"
if [ -n "${VEDUN_INSTALL_RECORD:-}" ]; then BIN_DIR="${VEDUN_BIN_DIR:-$(dirname "$RECORD")/bin}"; else BIN_DIR="${VEDUN_BIN_DIR:-$HOME/.local/bin}"; fi
FILE_SECRETS_DIR="$CONFIG_HOME/$SERVICE/secrets"
SECRET_NAMES="VEDUN_API_KEY VEDUN_DASH_USER VEDUN_DASH_PASSWORD OPENROUTER_API_KEY TYPESAFE_API_KEY"
REQUIRED_SECRETS="VEDUN_API_KEY VEDUN_DASH_USER VEDUN_DASH_PASSWORD"
DEFAULT_IMAGE="ghcr.io/numikel/vedun:0.2"
IMAGE_LINE="${DEFAULT_IMAGE##*:}" # the minor line this launcher belongs to: `vedun upgrade` moves to it
START_TIMEOUT="${VEDUN_START_TIMEOUT:-300}"
PROFILE_MARK="# added by the vedun installer"
JSON=0 DRY=0 YES=0
R_DIR='' R_ENGINE='' R_PORT=8765 R_SOURCE=image R_IMAGE='' R_STORE='' R_PROFILE=''

# ---------- output ----------
ui_init() {
  local fd=1
  [ "$JSON" = 1 ] && fd=2
  C_ACC='' C_OK='' C_WARN='' C_ERR='' C_DIM='' C_B='' C_0=''
  if [ -t "$fd" ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    C_ACC=$'\033[36m' C_OK=$'\033[32m' C_WARN=$'\033[33m' C_ERR=$'\033[31m' C_DIM=$'\033[2m' C_B=$'\033[1m' C_0=$'\033[0m'
  fi
  case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *UTF-8* | *utf-8* | *UTF8* | *utf8*) S_OK='✓' S_WARN='!' S_ERR='✗' S_ARR='→' S_DOT='·' ;;
    *) S_OK='[ok]' S_WARN='[warn]' S_ERR='[fail]' S_ARR='->' S_DOT='-' ;;
  esac
}
ui_init
hum() { if [ "$JSON" = 1 ]; then printf '%s\n' "$*" >&2; else printf '%s\n' "$*"; fi; }
line_ok() { hum "  ${C_OK}${S_OK}${C_0} $*"; }
line_warn() { hum "  ${C_WARN}${S_WARN}${C_0} $*"; }
line_err() { hum "  ${C_ERR}${S_ERR}${C_0} $*"; }
line_dim() { hum "    ${C_DIM}$*${C_0}"; }
# dry <action>: print it and collect it for the "actions" list of the next JSON line
DRY_ACTIONS=''
dry() {
  hum "  ${C_ACC}[dry-run]${C_0} would $*"
  json_q "$*" && DRY_ACTIONS="$DRY_ACTIONS,$JQ"
}
# dry_fields -> DF1 DF2: the extra JSON fields of a line under --dry-run, empty otherwise (jobj skips empty
# arguments; strings instead of an array because "${a[@]}" of an empty array fails under set -u in bash 3.2)
dry_fields() {
  DF1='' DF2=''
  if [ "$DRY" = 1 ]; then DF1="#dry_run=true" DF2="#actions=[${DRY_ACTIONS#,}]"; fi
  DRY_ACTIONS=''
}
# die <exit code> <what happened> [what to do next]
die() {
  local code=$1
  hum "${C_ERR}${S_ERR} $2${C_0}"
  if [ $# -gt 2 ]; then hum "  ${S_ARR} $3"; fi
  if [ "$JSON" = 1 ]; then jobj "command=${CUR_STEP:-vedun}" "status=fail" "message=$2" "next=${3:-}" "#code=$code"; fi
  exit "$code"
}

# json_q <string> -> JQ = the string as a quoted JSON value (no subshell)
json_q() {
  # pattern and replacement quoted: bash 5.2 patsub_replacement would otherwise eat backslashes
  local s=$1 bs=\\ q=\" nl=$'\n' cr=$'\r' tab=$'\t'
  s=${s//"$bs"/"$bs$bs"}
  s=${s//"$q"/"$bs$q"}
  s=${s//"$nl"/"${bs}n"}
  s=${s//"$cr"/"${bs}r"}
  s=${s//"$tab"/"${bs}t"}
  JQ="\"$s\""
}
json_str() { json_q "$1" && printf '%s' "$JQ"; }
# jobj key=value ... ; a key starting with '#' takes a raw JSON value (number, boolean, object)
jobj() {
  local out='' a k v
  for a in "$@"; do
    [ -n "$a" ] || continue
    k=${a%%=*} v=${a#*=}
    if [ "${k#\#}" != "$k" ]; then
      json_q "${k#\#}" && out="$out,$JQ:$v"
    else
      json_q "$k" && k=$JQ && json_q "$v" && out="$out,$k:$JQ"
    fi
  done
  printf '{%s}\n' "${out#,}"
}
# report <step> <ok|skip|warn|fail> <message> [key=value ...]: one JSON line in --json mode, a human line always
report() {
  local step=$1 st=$2 msg=$3
  shift 3
  dry_fields
  if [ "$JSON" = 1 ]; then jobj "command=$step" "status=$st" "message=$msg" "$@" "$DF1" "$DF2"; fi
  case $st in
    ok) line_ok "$msg" ;;
    warn) line_warn "$msg" ;;
    fail) line_err "$msg" ;;
    *) line_dim "$msg" ;;
  esac
}

# ---------- prompts (always via /dev/tty: stdin may be the script itself under `curl | bash`) ----------
tty_ok() { (exec </dev/tty) 2>/dev/null; }
need_tty() { tty_ok || die 2 "No terminal to ask: $1" "Re-run with --yes (and the keys in the environment) for an unattended run."; }
# ask <question> <default> -> ASK_REPLY (the default under --yes)
ask() {
  ASK_REPLY=$2
  [ "$YES" = 1 ] && return 0
  need_tty "$1"
  local v=''
  printf '  %s %s[%s]%s: ' "$1" "$C_DIM" "$2" "$C_0" >/dev/tty
  IFS= read -r v </dev/tty || true
  [ -n "$v" ] && ASK_REPLY=$v
  return 0
}
# ask_secret <question> -> ASK_REPLY, input not echoed
ask_secret() {
  need_tty "$1"
  local v=''
  printf '  %s: ' "$1" >/dev/tty
  IFS= read -rs v </dev/tty || true
  printf '\n' >/dev/tty
  ASK_REPLY=$v
}
# confirm <question> <Y|N>: 0 = yes. Under --yes the default answer is taken.
confirm() {
  local d=$2 hint='[y/N]' v
  [ "$d" = Y ] && hint='[Y/n]'
  if [ "$YES" = 1 ]; then [ "$d" = Y ]; return; fi
  need_tty "$1"
  printf '  %s %s ' "$1" "$hint" >/dev/tty
  IFS= read -r v </dev/tty || v=''
  case $v in
    [yY] | [yY][eE][sS] | [tT] | [tT][aA][kK]) return 0 ;;
    '') [ "$d" = Y ] ;;
    *) return 1 ;;
  esac
}

# ---------- random values (cryptographic RNG of the OS) ----------
gen_api_key() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
gen_password() {
  local p=''
  while [ ${#p} -lt 20 ]; do p="$p$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"; done
  printf '%s' "${p:0:20}"
}

# ---------- install record ----------
json_unstr() {
  local s=$1 bs=\\ q=\" ph=$'\001'
  s=${s//"$bs$bs"/"$ph"}
  s=${s//"$bs$q"/"$q"}
  printf '%s' "${s//"$ph"/"$bs"}"
}
# rec_get <key> [file]: value of a flat one-key-per-line JSON file written by rec_write
rec_get() {
  local f=${2:-$RECORD} line v
  [ -r "$f" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      *"\"$1\":"*)
        v=${line#*\""$1"\":}
        v=${v#"${v%%[![:space:]]*}"}
        v=${v%,}
        case $v in \"*\") v=${v#\"} && v=$(json_unstr "${v%\"}") ;; esac
        printf '%s\n' "$v"
        return 0
        ;;
    esac
  done <"$f"
  return 1
}
rec_load() {
  [ -r "$RECORD" ] || die 2 "vedun is not installed here (no $RECORD)." \
    "Install it: curl -fsSL https://raw.githubusercontent.com/numikel/vedun/main/scripts/install.sh | bash"
  R_DIR=$(rec_get dir) || die 3 "The install record $RECORD has no \"dir\"." "Re-run the installer to repair it."
  R_ENGINE=$(rec_get engine || echo docker)
  R_PORT=$(rec_get port || echo 8765)
  R_SOURCE=$(rec_get source || echo image)
  R_IMAGE=$(rec_get image || true)
  R_STORE=$(rec_get secret_store || default_store)
  R_PROFILE=$(rec_get path_profile || true)
}
rec_write() {
  local f=$RECORD body
  body="{
  \"dir\": $(json_str "$R_DIR"),
  \"engine\": $(json_str "$R_ENGINE"),
  \"port\": $R_PORT,
  \"source\": $(json_str "$R_SOURCE"),"
  [ -n "$R_IMAGE" ] && body="$body
  \"image\": $(json_str "$R_IMAGE"),"
  [ -n "$R_PROFILE" ] && body="$body
  \"path_profile\": $(json_str "$R_PROFILE"),"
  body="$body
  \"secret_store\": $(json_str "$R_STORE"),
  \"version\": 1
}"
  if [ "$DRY" = 1 ]; then dry "write the install record $f (no secrets in it)"; return 0; fi
  mkdir -p "$(dirname "$f")"
  printf '%s\n' "$body" >"$f.tmp" && mv "$f.tmp" "$f"
}

# ---------- secret store ----------
os_kind() {
  case $(uname -s) in
    Darwin) echo macos ;;
    Linux) echo linux ;;
    *) echo other ;;
  esac
}
default_store() { if [ "$(os_kind)" = macos ]; then echo keychain; else echo libsecret; fi; }
store_name() {
  case $1 in
    keychain) echo "macOS Keychain" ;;
    libsecret) echo "Secret Service (libsecret)" ;;
    file) echo "file store $FILE_SECRETS_DIR" ;;
    *) echo "nowhere (asked at every start)" ;;
  esac
}
# libsecret_ok: secret-tool installed and a Secret Service answers on the session bus
libsecret_ok() {
  command -v secret-tool >/dev/null 2>&1 || return 1
  local err
  err=$(secret-tool lookup service "$SERVICE-probe" account probe 2>&1 >/dev/null) || true
  [ -z "$err" ]
}
secret_get() {
  local kind=${R_STORE:-$(default_store)}
  case $kind in
    keychain) security find-generic-password -s "$SERVICE" -a "$1" -w 2>/dev/null ;;
    libsecret) secret-tool lookup service "$SERVICE" account "$1" 2>/dev/null ;;
    file) [ -s "$FILE_SECRETS_DIR/$1" ] && cat "$FILE_SECRETS_DIR/$1" ;;
    *) return 1 ;;
  esac
}
# secret_set <NAME> <value>: the value travels on stdin of the store tool, never on a command line
secret_set() {
  local kind=${R_STORE:-$(default_store)} n=$1 v=$2
  if [ "$DRY" = 1 ]; then dry "store $n in the $(store_name "$kind") (service $SERVICE)"; return 0; fi
  case $kind in
    keychain)
      case $v in *'"'* | *\\* | *$'\n'*) die 3 "$n contains a quote, backslash or line break." "Check the value and try again." ;; esac
      # `security -i` reads the command from stdin, so the value is not visible in the process list
      printf 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' "$SERVICE" "$n" "$v" | security -i >/dev/null 2>&1 || true
      [ "$(secret_get "$n" || true)" = "$v" ] || die 3 "Could not save $n in the macOS Keychain." "Unlock the login keychain (Keychain Access) and try again."
      ;;
    libsecret)
      printf '%s' "$v" | secret-tool store --label "vedun $n" service "$SERVICE" account "$n" ||
        die 3 "Could not save $n in the Secret Service." "Unlock the keyring (log in to the desktop session) or use --secret-store file."
      ;;
    file)
      (umask 077 && mkdir -p "$FILE_SECRETS_DIR" && printf '%s' "$v" >"$FILE_SECRETS_DIR/$n")
      chmod 700 "$FILE_SECRETS_DIR" 2>/dev/null || true
      chmod 600 "$FILE_SECRETS_DIR/$n" 2>/dev/null || true
      ;;
    *) return 0 ;;
  esac
}
secret_del() {
  local kind=${R_STORE:-$(default_store)}
  if [ "$DRY" = 1 ]; then dry "delete $1 from the $(store_name "$kind")"; return 0; fi
  case $kind in
    keychain) security delete-generic-password -s "$SERVICE" -a "$1" >/dev/null 2>&1 || true ;;
    libsecret) secret-tool clear service "$SERVICE" account "$1" 2>/dev/null || true ;;
    file) rm -f "$FILE_SECRETS_DIR/$1" ;;
  esac
}
# has_secret <NAME>: set in the environment or in the store
has_secret() { [ -n "${!1:-}" ] || secret_get "$1" >/dev/null 2>&1; }
# export_secrets: fill the environment of THIS process (and so of its children) from the store
export_secrets() {
  local n v
  for n in $SECRET_NAMES; do
    if [ -z "${!n:-}" ] && v=$(secret_get "$n" 2>/dev/null); then export "$n=$v"; fi
  done
}

# ---------- container engine ----------
engine_usable() { command -v "$1" >/dev/null 2>&1 && "$1" info >/dev/null 2>&1 && "$1" compose version >/dev/null 2>&1; }
compose_env() {
  export VEDUN_PORT="$R_PORT" VEDUN_HOST_ROOT="$R_DIR/data" VEDUN_HOST_PDF_DIR="$R_DIR/pdf"
  if [ "$R_ENGINE" = podman ]; then
    export VEDUN_UID=0 VEDUN_GID=0
  elif [ "$(os_kind)" = linux ]; then
    VEDUN_UID=$(id -u) VEDUN_GID=$(id -g)
    export VEDUN_UID VEDUN_GID
  fi
  if [ -n "$R_IMAGE" ]; then export VEDUN_IMAGE="$R_IMAGE"; fi
}
compose() {
  if [ "$DRY" = 1 ]; then dry "run: $R_ENGINE compose --project-directory \"$R_DIR\" -f \"$R_DIR/compose.yaml\" $*"; return 0; fi
  if [ "$JSON" = 1 ]; then
    "$R_ENGINE" compose --project-directory "$R_DIR" -f "$R_DIR/compose.yaml" "$@" >&2
  else
    "$R_ENGINE" compose --project-directory "$R_DIR" -f "$R_DIR/compose.yaml" "$@"
  fi
}
service_url() { echo "http://127.0.0.1:$R_PORT"; }
http_ok() {
  if command -v curl >/dev/null 2>&1; then curl -fsS -m 4 -o /dev/null "$1" 2>/dev/null; else wget -q -T 4 -O /dev/null "$1" 2>/dev/null; fi
}
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
# wait_health <seconds>
wait_health() {
  local t=$1 i=0
  [ "$DRY" = 1 ] && { dry "wait up to ${t}s for $(service_url)/api/health"; return 0; }
  while [ "$i" -lt "$t" ]; do
    if http_ok "$(service_url)/api/health"; then return 0; fi
    if [ $((i % 30)) -eq 0 ] && [ "$i" -gt 0 ]; then line_dim "still starting (${i}s; the first start downloads the image)"; fi
    sleep 3
    i=$((i + 3))
  done
  return 1
}
container_state() {
  local id
  id=$("$R_ENGINE" compose --project-directory "$R_DIR" -f "$R_DIR/compose.yaml" ps -a -q vedun 2>/dev/null | head -n 1) || true
  [ -n "$id" ] || { echo "absent"; return 0; }
  "$R_ENGINE" inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id" 2>/dev/null || echo "unknown"
}
image_ref() {
  if [ -n "$R_IMAGE" ]; then echo "$R_IMAGE"; return 0; fi
  local d
  # shellcheck disable=SC2016 # matches the literal ${VEDUN_IMAGE:-...} text of compose.yaml
  d=$(sed -n 's/.*\${VEDUN_IMAGE:-\([^}]*\)}.*/\1/p' "$R_DIR/compose.yaml" 2>/dev/null | head -n 1) || true
  echo "${d:-$DEFAULT_IMAGE}"
}

# ensure_secrets: required keys present (environment or store), else ask for them (or exit 2 under --yes)
ensure_secrets() {
  local n missing=''
  for n in $REQUIRED_SECRETS; do has_secret "$n" || missing="$missing $n"; done
  missing=${missing# }
  [ -z "$missing" ] && return 0
  if [ "$R_STORE" = prompt ]; then
    [ "$YES" = 1 ] && die 2 "Keys are not stored (secret store: prompt) and --yes forbids asking." "Put $missing in the environment, e.g. kprun run vedun -- vedun start."
    hum "  Enter the vedun keys (they are kept in memory only):"
    for n in $SECRET_NAMES; do
      [ -n "${!n:-}" ] && continue
      if [ "$n" = VEDUN_DASH_USER ]; then ask "$n" vedun; else ask_secret "$n (empty = skip)"; fi
      if [ -n "$ASK_REPLY" ]; then export "$n=$ASK_REPLY"; fi
    done
    return 0
  fi
  [ "$YES" = 1 ] && die 2 "Missing keys in the $(store_name "$R_STORE"): $missing" "Run: vedun keys"
  line_warn "Missing keys: $missing"
  for n in $missing; do
    case $n in
      VEDUN_API_KEY) keys_api_key ;;
      VEDUN_DASH_USER) ask "Dashboard user name" vedun && secret_set VEDUN_DASH_USER "$ASK_REPLY" ;;
      VEDUN_DASH_PASSWORD) keys_password ;;
    esac
  done
}
# do_up: start or recreate the service with the secrets in the child environment only
do_up() {
  compose_env
  ensure_secrets
  (export_secrets && compose up -d)
}

# ---------- commands ----------
cmd_start() {
  CUR_STEP=start
  rec_load
  engine_usable "$R_ENGINE" || [ "$DRY" = 1 ] || die 2 "$R_ENGINE is not running." "Start Docker Desktop / the $R_ENGINE service, then: vedun start"
  do_up
  if ! wait_health "$START_TIMEOUT"; then
    die 3 "vedun did not answer on $(service_url)/api/health within $START_TIMEOUT s." "See what happened: vedun logs --tail 100, then vedun doctor"
  elif [ "$DRY" = 1 ]; then
    report start ok "dry run: vedun would start at $(service_url)" "url=$(service_url)"
  else
    report start ok "vedun is running: $(service_url)" "url=$(service_url)"
  fi
}
cmd_stop() {
  CUR_STEP=stop
  rec_load
  compose stop
  report stop ok "vedun stopped"
}
cmd_status() {
  CUR_STEP=status
  rec_load
  local st='absent' api=down n sj='' yn
  if engine_usable "$R_ENGINE"; then st=$(container_state); fi
  http_ok "$(service_url)/api/health" && api=ok
  for n in $SECRET_NAMES; do
    if has_secret "$n"; then yn=true; else yn=false; fi
    sj="$sj,\"$n\":$yn"
  done
  local state=${st%% *} health=${st#* }
  case $state in running) ;; absent) state=missing ;; *) state=stopped ;; esac
  if [ "$health" = "$st" ] || [ -z "$health" ]; then health=none; fi
  if [ "$JSON" = 1 ]; then
    jobj command=status "status=$([ "$api" = ok ] && echo ok || echo warn)" "state=$state" "health=$health" "api=$api" "url=$(service_url)" "dir=$R_DIR" "#secrets={${sj#,}}"
    return 0
  fi
  hum "${C_B}vedun${C_0} ${S_DOT} $(service_url)"
  hum "  container   $state, health $health"
  hum "  /api/health $api"
  hum "  folder      $R_DIR"
  hum "  keys        $(echo "${sj#,}" | sed 's/"//g; s/:true/: yes/g; s/:false/: no/g; s/,/, /g')"
}
cmd_logs() {
  rec_load
  compose logs -f --tail "${TAIL:-100}"
}
cmd_update() {
  CUR_STEP=update
  rec_load
  if [ "$R_SOURCE" = image ]; then
    compose_env
    compose pull
  else
    if [ "$DRY" = 1 ]; then dry "run: $R_ENGINE build -t vedun:local \"$R_SOURCE\""; else "$R_ENGINE" build -t vedun:local "$R_SOURCE" >&2; fi
  fi
  do_up
  wait_health "$START_TIMEOUT" || die 3 "vedun did not come back after the update." "Run: vedun logs --tail 100"
  report update ok "vedun updated and running: $(service_url)" "url=$(service_url)"
  upgrade_hint
}
cmd_upgrade() {
  CUR_STEP=upgrade
  rec_load
  [ "$R_SOURCE" = image ] || die 2 "This install builds vedun from source ($R_SOURCE)." "Get the new code there, then: vedun update"
  [ -z "$R_IMAGE" ] || die 2 "The image is set by VEDUN_IMAGE ($R_IMAGE)." "Change that variable instead."
  local cur new f="$R_DIR/compose.yaml"
  cur=$(image_ref)
  new="${cur%:*}:$IMAGE_LINE"
  if [ "$cur" = "$new" ]; then
    report upgrade ok "Already on the $IMAGE_LINE line; vedun update gets its fixes." "image=$cur"
    return 0
  fi
  hum "  Upgrade $cur -> $new. What changes in $IMAGE_LINE:"
  hum "    - keys for AI clients and scripts are created on the dashboard page 'MCP connection' (permissions, expiry)"
  hum "    - the dashboard opens even without the master API key"
  hum "  Release notes: https://github.com/numikel/vedun/releases"
  if [ "$YES" != 1 ]; then confirm "Upgrade now?" N || die 1 "Upgrade cancelled." "Nothing was changed."; fi
  if [ "$DRY" = 1 ]; then
    dry "set the image in $f to $new"
  else
    sed "s#\${VEDUN_IMAGE:-[^}]*}#\${VEDUN_IMAGE:-$new}#" "$f" >"$f.upgrade" && mv "$f.upgrade" "$f"
    grep -q "VEDUN_IMAGE:-$new}" "$f" || die 3 "Could not set the image in $f." "Edit its image line by hand to $new."
  fi
  compose_env
  compose pull
  do_up
  wait_health "$START_TIMEOUT" || die 3 "vedun did not come back after the upgrade." "Run: vedun logs --tail 100"
  report upgrade ok "vedun upgraded to $IMAGE_LINE and running: $(service_url)" "image=$new" "url=$(service_url)"
}
# upgrade_hint: after an update, say when a newer minor line exists (moving to it is a separate, explained step)
upgrade_hint() {
  local cur
  if [ "$R_SOURCE" != image ] || [ -n "$R_IMAGE" ]; then return 0; fi
  cur=$(image_ref)
  cur=${cur##*:}
  case $cur in
    0.[0-9]*) if [ "$cur" != "$IMAGE_LINE" ]; then hum "  vedun $IMAGE_LINE is available: vedun upgrade (it shows what changes first)"; fi ;;
  esac
  return 0
}
cmd_open() {
  rec_load
  local u
  u="$(service_url)/"
  if [ "$DRY" = 1 ]; then dry "open $u in the browser"; return 0; fi
  if [ "$(os_kind)" = macos ]; then open "$u"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$u" >/dev/null 2>&1 & else hum "Open $u in your browser."; fi
}

keys_api_key() {
  line_warn "A new API key disconnects every AI agent that uses the old one (Claude Code, other MCP clients)."
  confirm "Create a new API key?" N || { line_dim "API key unchanged"; return 0; }
  local k
  k=$(gen_api_key)
  secret_set VEDUN_API_KEY "$k"
  report keys ok "New API key stored. Update your agents: vedun keys show-api-key, or the dashboard page /connect."
}
keys_password() {
  local p
  if [ "$YES" = 1 ]; then p=$(gen_password); else
    ask_secret "New dashboard password (empty = generate one)"
    p=${ASK_REPLY:-$(gen_password)}
  fi
  secret_set VEDUN_DASH_PASSWORD "$p"
  # --json output is read by agents and lands in their transcripts: the password stays out of it
  if [ "$DRY" = 0 ] && [ "$JSON" = 0 ]; then hum "  Dashboard password: ${C_B}$p${C_0}  (stored in the $(store_name "$R_STORE"))"; fi
  report keys ok "Dashboard password changed"
}
# keys_optional <NAME> <label>: OPENROUTER/TYPESAFE; under --yes the value comes from the environment
keys_optional() {
  local v
  if [ "$YES" = 1 ]; then v=${!1:-}; else ask_secret "$2 (empty = remove)"; v=$ASK_REPLY; fi
  if [ -n "$v" ]; then secret_set "$1" "$v" && report keys ok "$2 stored"; else secret_del "$1" && report keys skip "$2 removed (not set)"; fi
}
cmd_keys() {
  CUR_STEP=keys
  rec_load
  local what=${1:-} changed=1
  if [ -z "$what" ]; then
    [ "$YES" = 1 ] && die 2 "Say which key: vedun keys openrouter|typesafe|api-key|password|show-api-key|show-password"
    hum "  1 OpenRouter key   2 Typesafe key   3 new API key   4 dashboard password   5 show API key   6 show login"
    ask "Choose" 1
    case $ASK_REPLY in 1) what=openrouter ;; 2) what=typesafe ;; 3) what="api-key" ;; 4) what=password ;; 5) what="show-api-key" ;; 6) what="show-password" ;; *) die 1 "Cancelled." ;; esac
  fi
  case $what in
    openrouter) keys_optional OPENROUTER_API_KEY "OpenRouter key" ;;
    typesafe) keys_optional TYPESAFE_API_KEY "Typesafe key" ;;
    api-key) keys_api_key ;;
    password) keys_password ;;
    show-api-key)
      changed=0
      [ "$JSON" = 1 ] && die 2 "show-api-key never prints into --json output." "Run it without --json in a terminal."
      hum "  VEDUN_API_KEY: $(secret_get VEDUN_API_KEY || echo '(not stored)')"
      ;;
    show-password)
      changed=0
      [ "$JSON" = 1 ] && die 2 "show-password never prints into --json output." "Run it without --json in a terminal."
      hum "  Dashboard user: $(secret_get VEDUN_DASH_USER || echo vedun)"
      hum "  Dashboard password: $(secret_get VEDUN_DASH_PASSWORD || echo '(not stored)')"
      ;;
    *) die 2 "Unknown key '$what'." "Use: openrouter, typesafe, api-key, password, show-api-key, show-password" ;;
  esac
  if [ "$changed" = 1 ] && http_ok "$(service_url)/api/health"; then
    line_dim "applying: recreating the service with the new keys"
    do_up
  fi
}

# doctor: one ok/warn/fail line per check, each with a fix
DOC_OK=0 DOC_FAIL=0 DOC_WARN=0
check() { # check <name> <ok|warn|fail> <message> [fix]
  [ "$2" = ok ] && DOC_OK=$((DOC_OK + 1))
  [ "$2" = fail ] && DOC_FAIL=$((DOC_FAIL + 1))
  [ "$2" = warn ] && DOC_WARN=$((DOC_WARN + 1))
  if [ "$JSON" = 1 ]; then jobj "check=$1" "status=$2" "message=$3" "fix=${4:-}"; fi
  case $2 in ok) line_ok "$3" ;; warn) line_warn "$3" ;; *) line_err "$3" ;; esac
  if [ -n "${4:-}" ] && [ "$2" != ok ]; then line_dim "$S_ARR $4"; fi
}
cmd_doctor() {
  local e st n d
  if [ -r "$RECORD" ]; then rec_load; check record ok "install record $RECORD"; else
    check record fail "no install record at $RECORD" "Run the installer again."
    R_ENGINE=docker
  fi
  e=$R_ENGINE
  if command -v "$e" >/dev/null 2>&1; then check engine ok "$e installed"; else check engine fail "$e not found" "Install Docker Desktop (macOS) or Docker Engine / Podman (Linux)."; fi
  if "$e" info >/dev/null 2>&1; then check engine_running ok "$e is running"; else check engine_running fail "$e is not running" "Start Docker Desktop or: sudo systemctl start docker"; fi
  if "$e" compose version >/dev/null 2>&1; then check compose ok "$e compose available"; else check compose fail "$e compose missing" "Install the compose plugin (docker-compose-plugin / podman-compose)."; fi
  if [ -n "$R_DIR" ]; then
    for d in "$R_DIR" "$R_DIR/data" "$R_DIR/pdf"; do
      if [ -d "$d" ] && [ -w "$d" ]; then check folder ok "$d writable"; else check folder fail "$d missing or not writable" "mkdir -p \"$d\" (or re-run the installer)"; fi
    done
    if [ -d "$R_DIR/notes" ]; then check folder ok "$R_DIR/notes exists"; else check folder warn "$R_DIR/notes missing" "mkdir -p \"$R_DIR/notes\""; fi
    if [ -r "$R_DIR/compose.yaml" ]; then check compose_file ok "$R_DIR/compose.yaml"; else check compose_file fail "$R_DIR/compose.yaml missing" "Re-run the installer (choose update)."; fi
    for n in $SECRET_NAMES; do
      if has_secret "$n"; then check "secret_$n" ok "$n set: yes"
      else case " $REQUIRED_SECRETS " in
        *" $n "*) check "secret_$n" fail "$n set: no" "vedun keys" ;;
        *) check "secret_$n" warn "$n set: no (optional)" "vedun keys" ;;
      esac; fi
    done
  fi
  if http_ok "$(service_url)/api/health"; then
    check port ok "port $R_PORT used by vedun"
    check api ok "$(service_url)/api/health answers"
  else
    if port_in_use "$R_PORT"; then check port fail "port $R_PORT is used by another program" "Free it, or re-run the installer with --port <n>."; else check port ok "port $R_PORT free"; fi
    check api warn "$(service_url)/api/health does not answer" "vedun start"
  fi
  if [ -n "$R_DIR" ] && "$e" info >/dev/null 2>&1; then
    st=$(container_state)
    case $st in
      "running healthy" | "running ") check container ok "container: $st" ;;
      running*) check container warn "container: $st" "vedun logs --tail 100" ;;
      *) check container warn "container: $st" "vedun start" ;;
    esac
    if "$e" image inspect "$(image_ref)" >/dev/null 2>&1; then check image ok "image $(image_ref) present"; else check image warn "image $(image_ref) not downloaded yet" "vedun update"; fi
  fi
  local sum=ok
  if [ "$DOC_WARN" -gt 0 ]; then sum=warn; fi
  if [ "$DOC_FAIL" -gt 0 ]; then sum=fail; fi
  if [ "$JSON" = 1 ]; then jobj check=summary "status=$sum" "#ok=$DOC_OK" "#warn=$DOC_WARN" "#fail=$DOC_FAIL"; fi
  [ "$DOC_FAIL" -eq 0 ] || exit 3
}

cmd_uninstall() {
  CUR_STEP=uninstall
  rec_load
  local n f
  hum "${C_B}Uninstall vedun${C_0}: container, model cache volume, launcher, PATH line, install record and stored keys."
  # --yes answers this first question; the data folder below stays a separate question whose default is no
  [ "$YES" = 1 ] || confirm "Remove them?" N || die 1 "Cancelled, nothing removed."
  if [ -r "$R_DIR/compose.yaml" ] && { [ "$DRY" = 1 ] || engine_usable "$R_ENGINE"; }; then compose_env && compose down -v --remove-orphans; fi
  f="$BIN_DIR/vedun"
  if [ -f "$f" ] && grep -q '^# vedun launcher' "$f"; then
    if [ "$DRY" = 1 ]; then dry "delete $f"; else rm -f "$f"; fi
  fi
  if [ -n "$R_PROFILE" ] && [ -f "$R_PROFILE" ] && grep -qF "$PROFILE_MARK" "$R_PROFILE"; then
    if [ "$DRY" = 1 ]; then dry "remove the vedun PATH line from $R_PROFILE"; else
      grep -vF "$PROFILE_MARK" "$R_PROFILE" >"$R_PROFILE.vedun-tmp" || true
      cat "$R_PROFILE.vedun-tmp" >"$R_PROFILE" && rm -f "$R_PROFILE.vedun-tmp"
    fi
  fi
  for n in $SECRET_NAMES; do secret_del "$n"; done
  if [ "$DRY" = 1 ]; then dry "delete $RECORD"; else
    rm -f "$RECORD"
    rmdir "$FILE_SECRETS_DIR" "$(dirname "$FILE_SECRETS_DIR")" "$(dirname "$RECORD")" 2>/dev/null || true
  fi
  report uninstall ok "vedun removed (your folder $R_DIR is still there)"
  # a damaged install record must never turn this into `rm -rf` of the home folder or of something that is not ours
  case ${R_DIR%/} in '' | / | "${HOME%/}") return 0 ;; esac
  grep -q '^name: vedun' "$R_DIR/compose.yaml" 2>/dev/null || return 0
  if confirm "ALSO delete your folder $R_DIR with the knowledge base, PDFs and notes? This cannot be undone." N; then
    if [ "$DRY" = 1 ]; then dry "delete $R_DIR"; else rm -rf "$R_DIR"; fi
    report uninstall ok "deleted $R_DIR"
  fi
}

usage() {
  cat <<EOF
vedun $VEDUN_VERSION - local document knowledge base (dashboard + API + MCP server)

Usage: vedun <command> [--json] [--dry-run] [--yes]

  start        start the service and wait until it answers
  stop         stop the service
  status       container state, health, URL and which keys are set
  logs         follow the service log (--tail N, default 100)
  update       get the new image of the same line (or rebuild from source) and restart
  upgrade      move to the newest minor line: shows what changes, asks first
  open         open the dashboard in the browser
  keys [what]  change keys: openrouter, typesafe, api-key, password; show them: show-api-key, show-password
  doctor       check the installation, with a fix for every problem
  uninstall    remove vedun (your data folder only after a second question)
  help | version

  --json     one JSON object per line on stdout (human text goes to stderr)
  --dry-run  show what would change, change nothing
  --yes      no questions: take the defaults (keys from the environment)
EOF
}

main() {
  local cmd='' arg=''
  while [ $# -gt 0 ]; do
    case $1 in
      --json) JSON=1 ;;
      --dry-run) DRY=1 ;;
      --yes | -y) YES=1 ;;
      --tail) TAIL=${2:?--tail needs a number}; shift ;;
      -h | --help) cmd=help ;;
      -*) ui_init; die 2 "Unknown option $1" "See: vedun help" ;;
      *) if [ -z "$cmd" ]; then cmd=$1; else arg=$1; fi ;;
    esac
    shift
  done
  ui_init
  case ${cmd:-help} in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    update) cmd_update ;;
    upgrade) cmd_upgrade ;;
    open) cmd_open ;;
    keys) cmd_keys "$arg" ;;
    doctor) cmd_doctor ;;
    uninstall) cmd_uninstall ;;
    version | --version) if [ "$JSON" = 1 ]; then jobj command=version status=ok "version=$VEDUN_VERSION"; else echo "vedun $VEDUN_VERSION"; fi ;;
    help) usage ;;
    *) die 2 "Unknown command '$cmd'." "See: vedun help" ;;
  esac
}

if [ "${VEDUN_LIB:-0}" != 1 ]; then main "$@"; fi
