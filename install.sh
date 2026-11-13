#!/usr/bin/env bash
# Versioned installer for a side-by-side Herdr -> OpenBot gateway.
# Does NOT modify any existing Herdr session or openbot-herdr-pilot.service.
set -euo pipefail

RELEASE_VERSION="v0.7.1-rc1"
RELEASE_URL="https://raw.githubusercontent.com/leadbyai/agent-fabric/${RELEASE_VERSION}"
BRIDGE_SHA256="e60b8b5633da074ca5dd2b4e13b00a721b7c3b71d703121b87634de28dd4a216"
LEDGER_SHA256="e8d189feb578e2246d2af1bc75986534653ac3d3eefaaf8f1fa7f9edcbe5f008"
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

usage() {
  printf 'Usage: sudo bash install.sh --user USER --sessions NAME[,NAME] --roots DIR[:DIR] [--artifact-mode inline|signed_url --public-origin https://AGENT-HOST] [--legacy-target NAME --legacy-workdir DIR] [--token-file PATH] [--port 8766] [--busy-wait-seconds 600] [--recovery-seconds 600] [--source-dir DIR] [--no-start]\n'
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --user|--sessions|--roots|--legacy-target|--legacy-workdir|--token-file|--port|--busy-wait-seconds|--recovery-seconds|--source-dir|--public-origin|--artifact-mode)
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

if [[ -n "$TOKEN_SOURCE" ]]; then
  install -m 0600 "$TOKEN_SOURCE" "$CONF_DIR/bridge.token"
elif [[ ! -f "$CONF_DIR/bridge.token" ]]; then
  python3 -c 'import secrets,sys;sys.stdout.write(secrets.token_urlsafe(48)+"\n")' > "$CONF_DIR/bridge.token"
  chmod 0600 "$CONF_DIR/bridge.token"
fi
[[ $(wc -c < "$CONF_DIR/bridge.token") -ge 24 ]] || die "Existing bearer token too short"

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
  printf 'HERDR_SESSION=%s\n' "${HERDR_SESSIONS%%,*}"
  printf 'HERDR_AGENT_TARGET=%s\n' "$LEGACY_TARGET"
  printf 'HERDR_BIN=%s\n' "$HERDR_EXEC"
  printf 'OPENBOT_BRIDGE_PORT=%s\n' "$GATEWAY_PORT"
  printf 'OPENBOT_BRIDGE_ALLOWED_ROOTS=%s\n' "$UPLOAD_ROOTS"
  printf 'OPENBOT_BRIDGE_WORKDIR=%s\n' "$LEGACY_WORKDIR"
  printf 'OPENBOT_BRIDGE_HOST=127.0.0.1\n'
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
Environment=OPENBOT_BRIDGE_TOKEN_FILE=%d/bridge.token
LoadCredential=bridge.token:$CONF_DIR/bridge.token
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
# v0.7.1 migrates the request ledger schema on first start. Take a consistent
# online copy first (sqlite backup API; safe while the old gateway runs).
LEDGER_DB="$STATE_DIR/requests.sqlite3"
if [[ -f "$LEDGER_DB" && ! -L "$LEDGER_DB" ]]; then
  python3 -I -c 'import sqlite3,sys
src=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()' "$LEDGER_DB" "$LEDGER_DB.bak.$BACKUP_STAMP" \
    || die "Could not back up the request ledger; nothing restarted"
  chown "$RUN_USER:$(id -gn "$RUN_USER")" "$LEDGER_DB.bak.$BACKUP_STAMP"
  chmod 0600 "$LEDGER_DB.bak.$BACKUP_STAMP"
fi
systemctl daemon-reload
if ((SKIP_START == 0)); then
  systemctl enable openbot-herdr-gateway.service
  systemctl restart openbot-herdr-gateway.service
  HEALTHY=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$GATEWAY_PORT/health"; then
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
printf 'Gateway installed: %s, user=%s, sessions=%s, port=%s\n' "$RELEASE_VERSION" "$RUN_USER" "$HERDR_SESSIONS" "$GATEWAY_PORT"
printf 'Busy wait=%ss, recovery=%ss; run ledger stays on this server when either is enabled.\n' "$BUSY_WAIT_SECONDS" "$RECOVERY_SECONDS"
printf 'Bearer token stored locally at %s (not printed).\n' "$CONF_DIR/bridge.token"
printf 'Existing pilot bridge and Herdr sessions were not modified.\n'
