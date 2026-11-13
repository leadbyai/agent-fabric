#!/usr/bin/env bash
# Versioned installer for a side-by-side Herdr -> OpenBot gateway.
# Does NOT modify any existing Herdr session or openbot-herdr-pilot.service.
set -euo pipefail

RELEASE_VERSION="v0.5.1"
RELEASE_URL="https://raw.githubusercontent.com/leadbyai/agent-fabric/${RELEASE_VERSION}"
BRIDGE_SHA256="fae6cb09f3bfece64559b85db64a95136df7f23a50be356041dcd91446639680"
RUN_USER=""
HERDR_SESSIONS=""
UPLOAD_ROOTS=""
LEGACY_TARGET=""
LEGACY_WORKDIR=""
TOKEN_SOURCE=""
GATEWAY_PORT="8766"
SOURCE_DIR=""
SKIP_START=0

usage() {
  printf 'Usage: sudo bash install.sh --user USER --sessions NAME[,NAME] --roots DIR[:DIR] [--legacy-target NAME --legacy-workdir DIR] [--token-file PATH] [--port 8766] [--source-dir DIR] [--no-start]\n'
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --user|--sessions|--roots|--legacy-target|--legacy-workdir|--token-file|--port|--source-dir)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in
        --user) RUN_USER="$2" ;;
        --sessions) HERDR_SESSIONS="$2" ;;
        --roots) UPLOAD_ROOTS="$2" ;;
        --legacy-target) LEGACY_TARGET="$2" ;;
        --legacy-workdir) LEGACY_WORKDIR="$2" ;;
        --token-file) TOKEN_SOURCE="$2" ;;
        --port) GATEWAY_PORT="$2" ;;
        --source-dir) SOURCE_DIR="$2" ;;
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
[[ "$HERDR_SESSIONS" =~ ^[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*$ ]] || die "Invalid --sessions"
[[ -n "$UPLOAD_ROOTS" && "$UPLOAD_ROOTS" =~ ^[A-Za-z0-9_./,:-]+$ ]] || die "Invalid --roots"
[[ -z "$LEGACY_TARGET" || "$LEGACY_TARGET" =~ ^([a-z][a-z0-9_-]{0,31}|w[0-9A-Za-z]+:p[0-9A-Za-z]+)$ ]] || die "Invalid legacy target"
[[ -z "$LEGACY_WORKDIR" || "$LEGACY_WORKDIR" =~ ^[A-Za-z0-9_./-]+$ ]] || die "Invalid legacy workdir"
[[ -z "$LEGACY_TARGET" || -n "$LEGACY_WORKDIR" ]] || die "Specify --legacy-workdir with --legacy-target"
[[ -z "$LEGACY_WORKDIR" || -n "$LEGACY_TARGET" ]] || die "Specify --legacy-target with --legacy-workdir"
[[ -z "$TOKEN_SOURCE" || -f "$TOKEN_SOURCE" ]] || die "Token file does not exist"
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
fi

APP_DIR="/opt/openbot-herdr-bridge"
CONF_DIR="/etc/openbot-herdr-bridge"
UNIT_FILE="/etc/systemd/system/openbot-herdr-gateway.service"
[[ ! -L "$APP_DIR" && ! -L "$CONF_DIR" && ! -L "$UNIT_FILE" ]] || die "Managed destination cannot be a symlink"
if [[ -f "$UNIT_FILE" ]] && ! grep -q 'Managed by openbot-herdr-bridge installer' "$UNIT_FILE"; then
  die "Unmanaged service already exists at $UNIT_FILE"
fi
if [[ -d "$APP_DIR" && ! -f "$APP_DIR/bridge.py" ]]; then
  die "Unexpected contents at $APP_DIR; refusing to overwrite"
fi

install -d -m 0755 "$APP_DIR"
install -d -m 0700 "$CONF_DIR"
TEMP_FILE="$(mktemp "$APP_DIR/.bridge.py.XXXXXX")"
trap 'test ! -e "$TEMP_FILE" || rm -f -- "$TEMP_FILE"' EXIT
if [[ -n "$SOURCE_DIR" ]]; then
  cp -- "$SOURCE_DIR/bridge.py" "$TEMP_FILE"
else
  command -v curl >/dev/null || die "curl required"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 30 \
    "$RELEASE_URL/bridge.py" -o "$TEMP_FILE"
fi

if [[ "$BRIDGE_SHA256" != __BRIDGE_SHA256__ ]]; then
  printf '%s  %s\n' "$BRIDGE_SHA256" "$TEMP_FILE" | sha256sum -c - >/dev/null || die "Bridge SHA256 mismatch"
elif [[ -z "$SOURCE_DIR" ]]; then
  die "Release checksum not finalized; use a signed release or --source-dir"
fi
python3 -m py_compile "$TEMP_FILE" || die "Gateway code did not compile"
if [[ -f "$APP_DIR/bridge.py" ]]; then
  cp -p -- "$APP_DIR/bridge.py" "$APP_DIR/bridge.py.bak.$(date -u +%Y%m%dT%H%M%SZ)"
fi
install -m 0755 "$TEMP_FILE" "$APP_DIR/bridge.py"

if [[ -n "$TOKEN_SOURCE" ]]; then
  install -m 0600 "$TOKEN_SOURCE" "$CONF_DIR/bridge.token"
elif [[ ! -f "$CONF_DIR/bridge.token" ]]; then
  python3 -c 'import secrets,sys;sys.stdout.write(secrets.token_urlsafe(48)+"\n")' > "$CONF_DIR/bridge.token"
  chmod 0600 "$CONF_DIR/bridge.token"
fi
[[ $(wc -c < "$CONF_DIR/bridge.token") -ge 24 ]] || die "Existing bearer token too short"

CONFIG_TEMP="$(mktemp "$CONF_DIR/.config.env.XXXXXX")"
trap 'test ! -e "$CONFIG_TEMP" || rm -f -- "$CONFIG_TEMP"; test ! -e "$TEMP_FILE" || rm -f -- "$TEMP_FILE"' EXIT
{
  printf 'HERDR_SESSIONS=%s\n' "$HERDR_SESSIONS"
  printf 'HERDR_SESSION=%s\n' "${HERDR_SESSIONS%%,*}"
  printf 'HERDR_AGENT_TARGET=%s\n' "$LEGACY_TARGET"
  printf 'HERDR_BIN=%s\n' "$HERDR_EXEC"
  printf 'OPENBOT_BRIDGE_PORT=%s\n' "$GATEWAY_PORT"
  printf 'OPENBOT_BRIDGE_ALLOWED_ROOTS=%s\n' "$UPLOAD_ROOTS"
  printf 'OPENBOT_BRIDGE_WORKDIR=%s\n' "$LEGACY_WORKDIR"
  printf 'OPENBOT_BRIDGE_HOST=127.0.0.1\n'
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
    if curl -fsS --max-time 2 "http://127.0.0.1:$GATEWAY_PORT/health"; then
      printf '\n'
      HEALTHY=1
      break
    fi
    sleep 1
  done
  ((HEALTHY == 1)) || die "Gateway did not pass local health check"
  systemctl is-active --quiet openbot-herdr-gateway.service || die "Gateway failed to start"
else
  printf 'Prepared; run sudo systemctl enable --now openbot-herdr-gateway.service after review.\n'
fi
printf 'Gateway installed: %s, user=%s, sessions=%s, port=%s\n' "$RELEASE_VERSION" "$RUN_USER" "$HERDR_SESSIONS" "$GATEWAY_PORT"
printf 'Bearer token stored locally at %s (not printed).\n' "$CONF_DIR/bridge.token"
printf 'Existing pilot bridge and Herdr sessions were not modified.\n'
