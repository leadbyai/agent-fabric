#!/usr/bin/env bash
# Agent Fabric, Hermes backend: additive user-level installer.
# Run AS the Hermes profiles' Unix user (no sudo). It installs a new user service next to the
# existing Hermes gateway. It never installs, restarts, stops or reconfigures Hermes, and never
# touches Hermes config, credentials, sessions or cron jobs.
#
#   bash install-hermes.sh --source-dir DIR --profiles NAME[,NAME] --bind IP [--port 8767]
#        [--hermes-home ~/.hermes] [--hermes-agent-dir DIR] [--python PATH]
#        [--profile-token-sha256 NAME=HEX ...]     # per-profile hashes: each token reaches one profile
#        [--token-sha256 HEX | --token-file PATH]   # bridge-wide: one token reaches every listed profile
#        [--timeout-seconds 900] [--no-start]
#   bash install-hermes.sh --uninstall        # rollback: stop, disable and remove the unit and files
set -euo pipefail

UNIT=agent-fabric-hermes.service
APP_DIR="$HOME/.local/share/agent-fabric-hermes/app"
CONF_DIR="$HOME/.config/agent-fabric-hermes"
STATE_DIR="$HOME/.local/state/agent-fabric-hermes"
UNIT_DIR="$HOME/.config/systemd/user"
SOURCE_DIR=""; PROFILES=""; BIND=""; PORT="8767"; HERMES_HOME_DIR="$HOME/.hermes"
AGENT_DIR=""; PY=""; TOKEN_SOURCE=""; TOKEN_SHA256=""; PROFILE_HASHES=(); TIMEOUT_SECONDS="900"; SKIP_START=0; UNINSTALL=0

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0"; }

while (($#)); do
  case "$1" in
    --source-dir|--profiles|--bind|--port|--hermes-home|--hermes-agent-dir|--python|--token-file|--token-sha256|--profile-token-sha256|--timeout-seconds)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in
        --source-dir) SOURCE_DIR="$2" ;; --profiles) PROFILES="$2" ;; --bind) BIND="$2" ;;
        --port) PORT="$2" ;; --hermes-home) HERMES_HOME_DIR="$2" ;; --hermes-agent-dir) AGENT_DIR="$2" ;;
        --python) PY="$2" ;; --token-file) TOKEN_SOURCE="$2" ;; --token-sha256) TOKEN_SHA256="$2" ;;
        --profile-token-sha256) PROFILE_HASHES+=("$2") ;; --timeout-seconds) TIMEOUT_SECONDS="$2" ;;
      esac
      shift 2 ;;
    --no-start) SKIP_START=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
done

[[ $EUID -ne 0 ]] || die "Run as the Hermes user, not root"
command -v systemctl >/dev/null || die "systemd required"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

if ((UNINSTALL)); then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload
  rm -rf "${APP_DIR:?}" "${CONF_DIR:?}"
  printf 'Removed %s, its app and its config. The ledger in %s is kept (delete it by hand if wanted).\n' "$UNIT" "$STATE_DIR"
  exit 0
fi

[[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/hermes_bridge.py" && -f "$SOURCE_DIR/bridge.py" && -f "$SOURCE_DIR/request_ledger.py" ]] \
  || die "--source-dir must contain hermes_bridge.py, bridge.py and request_ledger.py (a checkout of the reviewed commit)"
[[ "$PROFILES" =~ ^[a-z0-9][a-z0-9_-]{0,63}(,[a-z0-9][a-z0-9_-]{0,63})*$ ]] || die "Invalid --profiles"
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] && ((PORT >= 1024 && PORT <= 65535)) || die "Invalid --port"
[[ "$TIMEOUT_SECONDS" =~ ^[0-9]{2,4}$ ]] && ((TIMEOUT_SECONDS >= 30 && TIMEOUT_SECONDS <= 3600)) || die "Invalid --timeout-seconds"
[[ -z "$TOKEN_SHA256" || -z "$TOKEN_SOURCE" ]] || die "Use --token-sha256 or --token-file, not both"
[[ -z "$TOKEN_SHA256" || "$TOKEN_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--token-sha256 must be 64 lowercase hex characters"
if ((${#PROFILE_HASHES[@]})); then
  [[ -z "$TOKEN_SHA256" && -z "$TOKEN_SOURCE" ]] || die "Use --profile-token-sha256 or a bridge-wide token, not both"
fi
[[ "$BIND" =~ ^[0-9.]+$ ]] || die "--bind must be an IPv4 address (127.0.0.1 or this host's Tailscale address)"
if [[ "$BIND" != "127.0.0.1" ]]; then
  ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$BIND" || die "--bind $BIND is not an address of this host"
fi
if ss -Htln "sport = :$PORT" | grep -q .; then
  systemctl --user is-active --quiet "$UNIT" || die "Port $PORT is already in use"
fi
[[ -d "$HERMES_HOME_DIR" && -S "$HERMES_HOME_DIR/gateway.sock" ]] || die "No running Hermes gateway socket in $HERMES_HOME_DIR"
AGENT_DIR="${AGENT_DIR:-$HERMES_HOME_DIR/hermes-agent}"
PY="${PY:-$AGENT_DIR/venv/bin/python}"
[[ -x "$PY" ]] || die "Hermes Python not found: $PY"
IFS=, read -r -a profile_list <<< "$PROFILES"
for name in "${profile_list[@]}"; do
  [[ "$name" == default || -d "$HERMES_HOME_DIR/profiles/$name" ]] || die "Profile $name does not exist"
done
PROFILE_TOKENS=""
if ((${#PROFILE_HASHES[@]})); then
  declare -A seen_name=() seen_hash=()
  for entry in "${PROFILE_HASHES[@]}"; do
    [[ "$entry" =~ ^([a-z0-9][a-z0-9_-]{0,63})=([0-9a-f]{64})$ ]] || die "--profile-token-sha256 must be NAME=<64 lowercase hex>"
    name="${BASH_REMATCH[1]}"; digest="${BASH_REMATCH[2]}"
    [[ ",$PROFILES," == *",$name,"* ]] || die "--profile-token-sha256 names a profile that is not in --profiles"
    [[ -z "${seen_name[$name]:-}" ]] || die "--profile-token-sha256 given twice for one profile"
    [[ -z "${seen_hash[$digest]:-}" ]] || die "The same token hash is given for two profiles"
    seen_name[$name]=1; seen_hash[$digest]=1
    PROFILE_TOKENS+="$name $digest"$'\n'
  done
  for name in "${profile_list[@]}"; do
    [[ -n "${seen_name[$name]:-}" ]] || die "Profile $name has no --profile-token-sha256 (every listed profile needs one)"
  done
fi

# Preflight with Hermes' own read-only discovery (it never starts a gateway).
HERMES_HOME="$HERMES_HOME_DIR" "$PY" -E -s - "$AGENT_DIR" "$HERMES_HOME_DIR" "$PROFILES" <<'PY' || die "Preflight failed"
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli.gateway_runtime import discover_gateway_endpoint
import hermes_cli.gateway_client  # noqa: F401  (supported client must import)
home = Path(sys.argv[2])
for name in sys.argv[3].split(","):
    found = discover_gateway_endpoint(home if name == "default" else home / "profiles" / name, timeout=3)
    if found.endpoint is None or found.state != "ready":
        sys.exit(f"profile {name}: Hermes gateway is {found.state} ({found.reason_code}); not installing")
    if "session-authority-v1" not in found.endpoint.capabilities:
        sys.exit(f"profile {name}: gateway lacks session-authority-v1")
    print(f"preflight ok: {name} served (runtime protocol {found.endpoint.runtime_protocol})")
PY

umask 077
install -d -m 0700 "$APP_DIR" "$CONF_DIR" "$STATE_DIR" "$UNIT_DIR"
for file in hermes_bridge.py bridge.py request_ledger.py; do
  install -m 0600 "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
if [[ -n "$PROFILE_TOKENS" ]]; then
  # Per-profile scope replaces any bridge-wide credential, so no token reaches every profile.
  printf '# <profile> <sha256 of that profile'"'"'s bearer token>; managed by install-hermes.sh\n%s' "$PROFILE_TOKENS" \
    > "$CONF_DIR/profile-tokens.sha256"
  chmod 0600 "$CONF_DIR/profile-tokens.sha256"
  rm -f "$CONF_DIR/bridge.token.sha256"
  if [[ -f "$CONF_DIR/bridge.token" ]]; then shred -u "$CONF_DIR/bridge.token"; fi
  TOKEN_ENV="OPENBOT_BRIDGE_PROFILE_TOKENS_FILE=$CONF_DIR/profile-tokens.sha256"
elif [[ -n "$TOKEN_SHA256" ]]; then
  rm -f "$CONF_DIR/profile-tokens.sha256"
  printf '%s\n' "$TOKEN_SHA256" > "$CONF_DIR/bridge.token.sha256"
  chmod 0600 "$CONF_DIR/bridge.token.sha256"
  if [[ -f "$CONF_DIR/bridge.token" ]]; then shred -u "$CONF_DIR/bridge.token"; fi
  TOKEN_ENV="OPENBOT_BRIDGE_TOKEN_SHA256_FILE=$CONF_DIR/bridge.token.sha256"
else
  if [[ -n "$TOKEN_SOURCE" ]]; then
    [[ -f "$TOKEN_SOURCE" ]] || die "Token file does not exist"
    install -m 0600 "$TOKEN_SOURCE" "$CONF_DIR/bridge.token"
  elif [[ ! -s "$CONF_DIR/bridge.token" ]]; then
    "$PY" -E -s -c 'import secrets; print(secrets.token_urlsafe(48))' > "$CONF_DIR/bridge.token"
  fi
  chmod 0600 "$CONF_DIR/bridge.token"
  rm -f "$CONF_DIR/bridge.token.sha256" "$CONF_DIR/profile-tokens.sha256"
  TOKEN_ENV="OPENBOT_BRIDGE_TOKEN_FILE=$CONF_DIR/bridge.token"
fi

cat > "$UNIT_DIR/$UNIT" <<EOF
# Managed by agent-fabric install-hermes.sh. Additive: does not manage the Hermes gateway.
[Unit]
Description=Agent Fabric Hermes AG-UI bridge (additive; attaches to the running Hermes gateway)
After=hermes-gateway.service

[Service]
Type=simple
Environment=HERMES_HOME=$HERMES_HOME_DIR
Environment=HERMES_AGENT_DIR=$AGENT_DIR
Environment=HERMES_PROFILES=$PROFILES
Environment=OPENBOT_BRIDGE_HOST=$BIND
Environment=OPENBOT_BRIDGE_PORT=$PORT
Environment=$TOKEN_ENV
Environment=OPENBOT_BRIDGE_LEDGER_PATH=$STATE_DIR/ledger.sqlite3
Environment=OPENBOT_BRIDGE_TIMEOUT_SECONDS=$TIMEOUT_SECONDS
Environment=HERMES_APPROVAL_WAIT_SECONDS=0
Environment=HERMES_ALLOW_NATIVE_ATTACH=0
Environment=HERMES_FOLLOW_PROFILE_CONFIG=1
ExecStart=$PY -E -s $APP_DIR/hermes_bridge.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=default.target
EOF
chmod 0600 "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload
sha256sum "$APP_DIR"/*.py | sed "s#$APP_DIR/##"

if ((SKIP_START)); then
  printf 'Prepared. Start with: systemctl --user enable --now %s\n' "$UNIT"
  exit 0
fi
systemctl --user enable "$UNIT"
systemctl --user restart "$UNIT"
for _ in $(seq 1 20); do
  if curl -fsS -m 3 "http://$BIND:$PORT/health" >/dev/null 2>&1; then
    printf 'Agent Fabric Hermes bridge healthy on %s:%s. Rollback: bash install-hermes.sh --uninstall\n' "$BIND" "$PORT"
    exit 0
  fi
  sleep 1
done
systemctl --user disable --now "$UNIT" || true
die "Bridge did not become healthy; it was stopped and disabled (journalctl --user -u $UNIT)"
