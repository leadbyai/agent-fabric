#!/usr/bin/env bash
# Versioned installer for a side-by-side Herdr -> OpenBot gateway.
# Does NOT modify any existing Herdr session or openbot-herdr-pilot.service.
set -euo pipefail

RELEASE_VERSION="v0.7.0-rc4"
RELEASE_URL="https://raw.githubusercontent.com/leadbyai/agent-fabric/${RELEASE_VERSION}"
BRIDGE_SHA256="cd842baca6927da3d07094ff2dda31cefe040424eba76ef4a4c46b8333c06240"
LEDGER_SHA256="c675b60a26a462690361951e6569848831f3efecf15577bb34dcb71babf2f49d"
RUN_USER=""
HERDR_SESSIONS=""
UPLOAD_ROOTS=""
LEGACY_TARGET=""
LEGACY_WORKDIR=""
TOKEN_SOURCE=""
GATEWAY_PORT="8766"
BUSY_WAIT_SECONDS="0"
RECOVERY_SECONDS="0"
SOURCE_DIR=""
PUBLIC_ORIGIN=""
ARTIFACT_MODE="inline"
SKIP_START=0
BIND_HOST="127.0.0.1"
TOKEN_SHA256=""
ROUTE_HASHES=()
EXPECT_BRIDGE_SHA256=""
DENY_SESSIONS=""
DISCOVERY_SHA256=""

usage() {
  printf 'Usage: sudo bash install.sh --user USER --sessions NAME[,NAME] --roots DIR[:DIR] [--artifact-mode inline|signed_url --public-origin https://AGENT-HOST] [--legacy-target NAME --legacy-workdir DIR] [--token-file PATH] [--port 8766] [--busy-wait-seconds 600] [--recovery-seconds 600] [--source-dir DIR [--expect-bridge-sha256 HEX]] [--bind 127.0.0.1|TAILSCALE-IP] [--token-sha256 HEX | --route-token-sha256 SESSION/PANE=HEX ...] [--sessions auto --deny-sessions NAME[,NAME]] [--discovery-token-sha256 HEX] [--no-start]\n'
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Backend choice. herdr (default) is the unchanged Herdr gateway install below.
#   --backend hermes  delegates to install-hermes.sh (run as the Hermes user, no sudo); every other
#                     option is passed through unchanged.
#   --backend both    runs the Herdr install, then install-hermes.sh as --user with the options
#                     given as --hermes-<option> (e.g. --hermes-profiles NAME --hermes-bind IP).
BACKEND="herdr"
HERMES_ARGS=()
REST_ARGS=()
while (($#)); do
  case "$1" in
    --backend) (($# >= 2)) || die "Missing value for --backend"; BACKEND="$2"; shift 2 ;;
    --hermes-no-start|--hermes-uninstall) HERMES_ARGS+=("--${1#--hermes-}"); shift ;;
    --hermes-*) (($# >= 2)) || die "Missing value for $1"; HERMES_ARGS+=("--${1#--hermes-}" "$2"); shift 2 ;;
    *) REST_ARGS+=("$1"); shift ;;
  esac
done
set -- ${REST_ARGS[@]+"${REST_ARGS[@]}"}
HERMES_INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install-hermes.sh"
case "$BACKEND" in
  herdr) ((${#HERMES_ARGS[@]} == 0)) || die "--hermes-* options need --backend both" ;;
  hermes)
    [[ -f "$HERMES_INSTALLER" ]] || die "install-hermes.sh not found next to install.sh"
    exec bash "$HERMES_INSTALLER" "$@" ;;
  both) [[ -f "$HERMES_INSTALLER" ]] || die "install-hermes.sh not found next to install.sh" ;;
  *) die "--backend must be herdr, hermes or both" ;;
esac

while (($#)); do
  case "$1" in
    --user|--sessions|--roots|--legacy-target|--legacy-workdir|--token-file|--port|--busy-wait-seconds|--recovery-seconds|--source-dir|--public-origin|--artifact-mode|--bind|--token-sha256|--route-token-sha256|--expect-bridge-sha256|--deny-sessions|--discovery-token-sha256)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in
        --user) RUN_USER="$2" ;;
        --sessions) HERDR_SESSIONS="$2" ;;
        --roots) UPLOAD_ROOTS="$2" ;;
        --legacy-target) LEGACY_TARGET="$2" ;;
        --legacy-workdir) LEGACY_WORKDIR="$2" ;;
        --token-file) TOKEN_SOURCE="$2" ;;
        --port) GATEWAY_PORT="$2" ;;
        --busy-wait-seconds) BUSY_WAIT_SECONDS="$2" ;;
        --recovery-seconds) RECOVERY_SECONDS="$2" ;;
        --source-dir) SOURCE_DIR="$2" ;;
        --public-origin) PUBLIC_ORIGIN="$2" ;;
        --artifact-mode) ARTIFACT_MODE="$2" ;;
        --bind) BIND_HOST="$2" ;;
        --token-sha256) TOKEN_SHA256="$2" ;;
        --route-token-sha256) ROUTE_HASHES+=("$2") ;;
        --expect-bridge-sha256) EXPECT_BRIDGE_SHA256="$2" ;;
        --deny-sessions) DENY_SESSIONS="$2" ;;
        --discovery-token-sha256) DISCOVERY_SHA256="$2" ;;
      esac
      shift 2 ;;
    --no-start) SKIP_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run with sudo"
[[ "$RUN_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Specify a valid --user"
id -u "$RUN_USER" >/dev/null 2>&1 || die "User $RUN_USER does not exist"
[[ "$GATEWAY_PORT" =~ ^[0-9]{4,5}$ ]] || die "Invalid port"
((GATEWAY_PORT >= 1024 && GATEWAY_PORT <= 65535)) || die "Invalid port"
[[ "$BUSY_WAIT_SECONDS" =~ ^[0-9]{1,4}$ ]] || die "Invalid busy wait"
[[ "$RECOVERY_SECONDS" =~ ^[0-9]{1,4}$ ]] || die "Invalid recovery wait"
((BUSY_WAIT_SECONDS <= 3600 && RECOVERY_SECONDS <= 3600)) || die "Waits must be at most 3600 seconds"
[[ "$HERDR_SESSIONS" =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || die "Invalid --sessions"
[[ -z "$DENY_SESSIONS" || "$DENY_SESSIONS" =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || die "Invalid --deny-sessions"
if [[ "$HERDR_SESSIONS" == auto ]]; then
  [[ -z "$LEGACY_TARGET" ]] || die "--legacy-target cannot be combined with --sessions auto"
fi
[[ -z "$DISCOVERY_SHA256" || "$DISCOVERY_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--discovery-token-sha256 must be 64 lowercase hex characters"
[[ -n "$UPLOAD_ROOTS" && "$UPLOAD_ROOTS" =~ ^[A-Za-z0-9_./,:-]+$ ]] || die "Invalid --roots"
[[ -z "$LEGACY_TARGET" || "$LEGACY_TARGET" =~ ^([a-z][a-z0-9_-]{0,31}|w[0-9A-Za-z]+:p[0-9A-Za-z]+)$ ]] || die "Invalid legacy target"
[[ -z "$LEGACY_WORKDIR" || "$LEGACY_WORKDIR" =~ ^[A-Za-z0-9_./-]+$ ]] || die "Invalid legacy workdir"
[[ -z "$LEGACY_TARGET" || -n "$LEGACY_WORKDIR" ]] || die "Specify --legacy-workdir with --legacy-target"
[[ -z "$LEGACY_WORKDIR" || -n "$LEGACY_TARGET" ]] || die "Specify --legacy-target with --legacy-workdir"
[[ -z "$TOKEN_SOURCE" || -f "$TOKEN_SOURCE" ]] || die "Token file does not exist"
[[ -z "$PUBLIC_ORIGIN" || "$PUBLIC_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{2,5})?$ ]] || die "--public-origin must be a bare HTTPS origin"
[[ "$ARTIFACT_MODE" = "inline" || "$ARTIFACT_MODE" = "signed_url" ]] || die "--artifact-mode must be inline or signed_url"
[[ "$ARTIFACT_MODE" != "signed_url" || -n "$PUBLIC_ORIGIN" ]] || die "signed_url mode requires --public-origin"
command -v curl >/dev/null || die "curl required"
command -v systemctl >/dev/null || die "systemd required"
# Bind: loopback (behind a private tunnel) or this host's own Tailscale (100.64.0.0/10) address.
if [[ "$BIND_HOST" != "127.0.0.1" ]]; then
  [[ "$BIND_HOST" =~ ^100\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]] && (( BASH_REMATCH[1] >= 64 && BASH_REMATCH[1] <= 127 )) \
    || die "--bind must be 127.0.0.1 or a Tailscale 100.64.0.0/10 address"
  ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$BIND_HOST" || die "--bind $BIND_HOST is not an address of this host"
fi
# Credentials: the bridge-wide token file (default), a bridge-wide SHA-256, or per-agent SHA-256s.
[[ -z "$TOKEN_SHA256" || "$TOKEN_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--token-sha256 must be 64 lowercase hex characters"
(( (${#TOKEN_SOURCE} > 0) + (${#TOKEN_SHA256} > 0) + (${#ROUTE_HASHES[@]} > 0) <= 1 )) \
  || die "Use one of --token-file, --token-sha256 or --route-token-sha256"
ROUTE_TOKENS=""
if ((${#ROUTE_HASHES[@]})); then
  declare -A seen_route=() seen_hash=()
  for entry in "${ROUTE_HASHES[@]}"; do
    [[ "$entry" =~ ^([A-Za-z0-9][A-Za-z0-9_-]{0,63})/(w[0-9A-Za-z]+:p[0-9A-Za-z]+)=([0-9a-f]{64})$ ]] \
      || die "--route-token-sha256 must be SESSION/PANE=<64 lowercase hex>"
    route="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"; digest="${BASH_REMATCH[3]}"
    [[ "$HERDR_SESSIONS" == auto || ",$HERDR_SESSIONS," == *",${BASH_REMATCH[1]},"* ]] || die "--route-token-sha256 names a session that is not in --sessions"
    [[ ",$DENY_SESSIONS," != *",${BASH_REMATCH[1]},"* ]] || die "--route-token-sha256 names a denied session"
    [[ -z "${seen_route[$route]:-}" ]] || die "--route-token-sha256 given twice for one route"
    [[ -z "${seen_hash[$digest]:-}" ]] || die "The same token hash is given for two routes"
    [[ "$digest" != "$DISCOVERY_SHA256" ]] || die "The discovery token must differ from every agent token"
    seen_route[$route]=1; seen_hash[$digest]=1
    ROUTE_TOKENS+="$route $digest"$'\n'
  done
fi
[[ -z "$EXPECT_BRIDGE_SHA256" || "$EXPECT_BRIDGE_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--expect-bridge-sha256 must be 64 lowercase hex characters"
[[ -z "$EXPECT_BRIDGE_SHA256" || -n "$SOURCE_DIR" ]] || die "--expect-bridge-sha256 needs --source-dir"
[[ -n "$EXPECT_BRIDGE_SHA256" ]] && BRIDGE_SHA256="$EXPECT_BRIDGE_SHA256"
[[ -z "$DISCOVERY_SHA256" || "$DISCOVERY_SHA256" != "$TOKEN_SHA256" ]] || die "The discovery token must differ from the bridge-wide token"

IFS=: read -r -a root_list <<< "$UPLOAD_ROOTS"
for path in "${root_list[@]}"; do
  [[ "$path" = /* && -d "$path" ]] || die "Upload root must be an existing absolute directory: $path"
done
if [[ -n "$LEGACY_WORKDIR" ]]; then
  [[ "$LEGACY_WORKDIR" = /* && -d "$LEGACY_WORKDIR" ]] || die "Legacy workdir must exist"
fi

RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -d "$RUN_HOME" ]] || die "No home directory for $RUN_USER"
HERDR_EXEC="$RUN_HOME/.local/bin/herdr"
[[ -x "$HERDR_EXEC" ]] || die "Herdr binary not executable at $HERDR_EXEC"
if [[ -n "$SOURCE_DIR" ]]; then
  [[ -f "$SOURCE_DIR/bridge.py" ]] || die "Missing bridge.py in source directory"
  [[ -f "$SOURCE_DIR/request_ledger.py" ]] || die "Missing request_ledger.py in source directory"
fi

APP_DIR="/opt/openbot-herdr-bridge"
CONF_DIR="/etc/openbot-herdr-bridge"
STATE_DIR="/var/lib/openbot-herdr-gateway"
ARTIFACT_DIR="$STATE_DIR/artifacts"
UNIT_FILE="/etc/systemd/system/openbot-herdr-gateway.service"
[[ ! -L "$APP_DIR" && ! -L "$CONF_DIR" && ! -L "$STATE_DIR" && ! -L "$UNIT_FILE" ]] || die "Managed destination cannot be a symlink"
if [[ -f "$UNIT_FILE" ]] && ! grep -q 'Managed by openbot-herdr-bridge installer' "$UNIT_FILE"; then
  die "Unmanaged service already exists at $UNIT_FILE"
fi
if [[ -d "$APP_DIR" && ! -f "$APP_DIR/bridge.py" ]]; then
  die "Unexpected contents at $APP_DIR; refusing to overwrite"
fi

install -d -m 0755 "$APP_DIR"
install -d -m 0700 "$CONF_DIR"
install -d -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" -m 0700 "$STATE_DIR"
install -d -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" -m 0700 "$ARTIFACT_DIR"
TEMP_FILE="$(mktemp "$APP_DIR/.bridge.py.XXXXXX")"
TEMP_LEDGER="$(mktemp "$APP_DIR/.request_ledger.py.XXXXXX")"
trap 'test ! -e "$TEMP_FILE" || rm -f -- "$TEMP_FILE"; test ! -e "$TEMP_LEDGER" || rm -f -- "$TEMP_LEDGER"' EXIT
if [[ -n "$SOURCE_DIR" ]]; then
  cp -- "$SOURCE_DIR/bridge.py" "$TEMP_FILE"
  cp -- "$SOURCE_DIR/request_ledger.py" "$TEMP_LEDGER"
else
  command -v curl >/dev/null || die "curl required"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 30 \
    "$RELEASE_URL/bridge.py" -o "$TEMP_FILE"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 30 \
    "$RELEASE_URL/request_ledger.py" -o "$TEMP_LEDGER"
fi

if [[ "$BRIDGE_SHA256" != __BRIDGE_SHA256__ && "$LEDGER_SHA256" != __LEDGER_SHA256__ ]]; then
  printf '%s  %s\n' "$BRIDGE_SHA256" "$TEMP_FILE" | sha256sum -c - >/dev/null || die "Bridge SHA256 mismatch"
  printf '%s  %s\n' "$LEDGER_SHA256" "$TEMP_LEDGER" | sha256sum -c - >/dev/null || die "Ledger SHA256 mismatch"
elif [[ -z "$SOURCE_DIR" ]]; then
  die "Release checksum not finalized; use a signed release or --source-dir"
fi
python3 -m py_compile "$TEMP_FILE" "$TEMP_LEDGER" || die "Gateway code did not compile"
BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
if [[ -f "$APP_DIR/bridge.py" ]]; then
  cp -p -- "$APP_DIR/bridge.py" "$APP_DIR/bridge.py.bak.$BACKUP_STAMP"
fi
if [[ -f "$APP_DIR/request_ledger.py" ]]; then
  cp -p -- "$APP_DIR/request_ledger.py" "$APP_DIR/request_ledger.py.bak.$BACKUP_STAMP"
fi
install -m 0755 "$TEMP_FILE" "$APP_DIR/bridge.py"
install -m 0644 "$TEMP_LEDGER" "$APP_DIR/request_ledger.py"

CREDENTIAL_ENV='OPENBOT_BRIDGE_TOKEN_FILE=%d/bridge.token'
CREDENTIAL_LOAD="bridge.token:$CONF_DIR/bridge.token"
if [[ -n "$ROUTE_TOKENS" || -n "$TOKEN_SHA256" ]]; then
  # Hash-only: the gateway never holds a bearer token; any old token file is removed.
  if [[ -n "$ROUTE_TOKENS" ]]; then
    printf '# <session>/<pane> <sha256 of that agent'"'"'s bearer token>; managed by install.sh\n%s' "$ROUTE_TOKENS" \
      > "$CONF_DIR/route-tokens.sha256"
    chmod 0600 "$CONF_DIR/route-tokens.sha256"
    rm -f "$CONF_DIR/bridge.token.sha256"
    CREDENTIAL_ENV='OPENBOT_BRIDGE_ROUTE_TOKENS_FILE=%d/route-tokens.sha256'
    CREDENTIAL_LOAD="route-tokens.sha256:$CONF_DIR/route-tokens.sha256"
  else
    printf '%s\n' "$TOKEN_SHA256" > "$CONF_DIR/bridge.token.sha256"
    chmod 0600 "$CONF_DIR/bridge.token.sha256"
    rm -f "$CONF_DIR/route-tokens.sha256"
    CREDENTIAL_ENV='OPENBOT_BRIDGE_TOKEN_SHA256_FILE=%d/bridge.token.sha256'
    CREDENTIAL_LOAD="bridge.token.sha256:$CONF_DIR/bridge.token.sha256"
  fi
  if [[ -f "$CONF_DIR/bridge.token" ]]; then shred -u "$CONF_DIR/bridge.token"; fi
else
  if [[ -n "$TOKEN_SOURCE" ]]; then
    install -m 0600 "$TOKEN_SOURCE" "$CONF_DIR/bridge.token"
  elif [[ ! -f "$CONF_DIR/bridge.token" ]]; then
    python3 -c 'import secrets,sys;sys.stdout.write(secrets.token_urlsafe(48)+"\n")' > "$CONF_DIR/bridge.token"
    chmod 0600 "$CONF_DIR/bridge.token"
  fi
  [[ $(wc -c < "$CONF_DIR/bridge.token") -ge 24 ]] || die "Existing bearer token too short"
fi

DISCOVERY_UNIT_LINES=""
if [[ -n "$DISCOVERY_SHA256" ]]; then
  printf '%s\n' "$DISCOVERY_SHA256" > "$CONF_DIR/discovery-token.sha256"
  chmod 0600 "$CONF_DIR/discovery-token.sha256"
  DISCOVERY_UNIT_LINES=$'\nEnvironment=OPENBOT_BRIDGE_DISCOVERY_TOKEN_SHA256_FILE=%d/discovery-token.sha256'
  DISCOVERY_UNIT_LINES+=$'\n'"LoadCredential=discovery-token.sha256:$CONF_DIR/discovery-token.sha256"
else
  rm -f "$CONF_DIR/discovery-token.sha256"
fi
CONFIG_TEMP="$(mktemp "$CONF_DIR/.config.env.XXXXXX")"
trap 'test ! -e "$CONFIG_TEMP" || rm -f -- "$CONFIG_TEMP"; test ! -e "$TEMP_FILE" || rm -f -- "$TEMP_FILE"; test ! -e "$TEMP_LEDGER" || rm -f -- "$TEMP_LEDGER"' EXIT
if [[ -f "$CONF_DIR/config.env" ]]; then
  cp -p -- "$CONF_DIR/config.env" "$CONF_DIR/config.env.bak.$BACKUP_STAMP"
fi
if [[ -f "$UNIT_FILE" ]]; then
  cp -p -- "$UNIT_FILE" "$UNIT_FILE.bak.$BACKUP_STAMP"
fi
{
  printf 'HERDR_SESSIONS=%s\n' "$HERDR_SESSIONS"
  if [[ "$HERDR_SESSIONS" == auto ]]; then printf 'HERDR_SESSION=\n'; else printf 'HERDR_SESSION=%s\n' "${HERDR_SESSIONS%%,*}"; fi
  [[ -z "$DENY_SESSIONS" ]] || printf 'HERDR_SESSIONS_DENY=%s\n' "$DENY_SESSIONS"
  printf 'HERDR_AGENT_TARGET=%s\n' "$LEGACY_TARGET"
  printf 'HERDR_BIN=%s\n' "$HERDR_EXEC"
  printf 'OPENBOT_BRIDGE_PORT=%s\n' "$GATEWAY_PORT"
  printf 'OPENBOT_BRIDGE_ALLOWED_ROOTS=%s\n' "$UPLOAD_ROOTS"
  printf 'OPENBOT_BRIDGE_WORKDIR=%s\n' "$LEGACY_WORKDIR"
  printf 'OPENBOT_BRIDGE_HOST=%s\n' "$BIND_HOST"
  printf 'OPENBOT_BRIDGE_BUSY_WAIT_SECONDS=%s\n' "$BUSY_WAIT_SECONDS"
  printf 'OPENBOT_BRIDGE_RECOVERY_SECONDS=%s\n' "$RECOVERY_SECONDS"
  printf 'OPENBOT_BRIDGE_ARTIFACT_DIR=%s\n' "$ARTIFACT_DIR"
  printf 'OPENBOT_BRIDGE_ARTIFACT_MODE=%s\n' "$ARTIFACT_MODE"
  printf 'OPENBOT_BRIDGE_PUBLIC_ORIGIN=%s\n' "$PUBLIC_ORIGIN"
  if (( BUSY_WAIT_SECONDS > 0 || RECOVERY_SECONDS > 0 )); then
    printf 'OPENBOT_BRIDGE_LEDGER_PATH=%s/requests.sqlite3\n' "$STATE_DIR"
  fi
} > "$CONFIG_TEMP"
install -m 0600 "$CONFIG_TEMP" "$CONF_DIR/config.env"

cat > "$UNIT_FILE" <<EOF
# Managed by openbot-herdr-bridge installer ($RELEASE_VERSION)
[Unit]
Description=OpenBot multi-agent Herdr gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$RUN_HOME
EnvironmentFile=$CONF_DIR/config.env
Environment=$CREDENTIAL_ENV
LoadCredential=$CREDENTIAL_LOAD$DISCOVERY_UNIT_LINES
ExecStart=/usr/bin/python3 $APP_DIR/bridge.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
StateDirectory=openbot-herdr-gateway
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"
systemctl daemon-reload
if ((SKIP_START == 0)); then
  systemctl enable openbot-herdr-gateway.service
  systemctl restart openbot-herdr-gateway.service
  HEALTHY=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 2 "http://$BIND_HOST:$GATEWAY_PORT/health"; then
      printf '\n'
      HEALTHY=1
      break
    fi
    sleep 1
  done
  if ((HEALTHY != 1)) || ! systemctl is-active --quiet openbot-herdr-gateway.service; then
    if [[ -f "$APP_DIR/bridge.py.bak.$BACKUP_STAMP" &&
          -f "$CONF_DIR/config.env.bak.$BACKUP_STAMP" &&
          -f "$UNIT_FILE.bak.$BACKUP_STAMP" ]]; then
      cp -p -- "$APP_DIR/bridge.py.bak.$BACKUP_STAMP" "$APP_DIR/bridge.py"
      cp -p -- "$CONF_DIR/config.env.bak.$BACKUP_STAMP" "$CONF_DIR/config.env"
      cp -p -- "$UNIT_FILE.bak.$BACKUP_STAMP" "$UNIT_FILE"
      if [[ -f "$APP_DIR/request_ledger.py.bak.$BACKUP_STAMP" ]]; then
        cp -p -- "$APP_DIR/request_ledger.py.bak.$BACKUP_STAMP" "$APP_DIR/request_ledger.py"
      fi
      systemctl daemon-reload
      systemctl restart openbot-herdr-gateway.service || true
      die "Gateway failed health check; attempted restoration of previous code, config and service"
    fi
    die "Gateway failed health check; no previous deployment available to restore"
  fi
else
  printf 'Prepared; run sudo systemctl enable --now openbot-herdr-gateway.service after review.\n'
fi
if [[ -n "$EXPECT_BRIDGE_SHA256" ]]; then
  printf 'Gateway installed: reviewed source build (bridge.py %s), user=%s, sessions=%s, bind=%s, port=%s\n' \
    "${EXPECT_BRIDGE_SHA256:0:16}" "$RUN_USER" "$HERDR_SESSIONS" "$BIND_HOST" "$GATEWAY_PORT"
else
  printf 'Gateway installed: %s, user=%s, sessions=%s, bind=%s, port=%s\n' "$RELEASE_VERSION" "$RUN_USER" "$HERDR_SESSIONS" "$BIND_HOST" "$GATEWAY_PORT"
fi
printf 'Busy wait=%ss, recovery=%ss; run ledger stays on this server when either is enabled.\n' "$BUSY_WAIT_SECONDS" "$RECOVERY_SECONDS"
if [[ -n "$ROUTE_TOKENS" ]]; then
  printf 'Per-agent token hashes stored at %s (%s routes); no bearer token is stored on this server.\n' \
    "$CONF_DIR/route-tokens.sha256" "${#ROUTE_HASHES[@]}"
elif [[ -n "$TOKEN_SHA256" ]]; then
  printf 'Bridge-wide token hash stored at %s; no bearer token is stored on this server.\n' "$CONF_DIR/bridge.token.sha256"
else
  printf 'Bearer token stored locally at %s (not printed).\n' "$CONF_DIR/bridge.token"
fi
printf 'Existing pilot bridge and Herdr sessions were not modified.\n'

if [[ "$BACKEND" == both ]]; then
  # Additive Hermes bridge, installed as the same user's own systemd --user service.
  runuser -u "$RUN_USER" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$RUN_USER")" \
    bash "$HERMES_INSTALLER" "${HERMES_ARGS[@]}"
fi
