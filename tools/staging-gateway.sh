#!/usr/bin/env bash
# A throwaway Herdr gateway for staging, beside (never instead of) the production gateway.
#
# Runs bridge.py from this checkout in the foreground, as the calling user, against a scratch Herdr
# session whose name starts with "staging-" (create it and its scratch panes first, never live
# agents). Everything is separate from production: its own port, bearer token, ledger, artifact
# directory and browser address. It never touches systemd, /etc/openbot-herdr-bridge or the
# production service. Ctrl-C stops it; nothing is left behind but the state directory you named.
#
#   tools/staging-gateway.sh --session staging-browser --port 18766 \
#     --token-file ~/staging/gateway.token --roots ~/staging/work \
#     --openbot-browser-url https://STAGING-OPENBOT/api/agent-fabric/browser \
#     --state-dir ~/staging/gateway-state [--bind 127.0.0.1]
#
# Register the Bots in the staging OpenBot with endpoint
# http://<bind>:<port>/sessions/<session>/agents/<pane>/ag-ui and the staging token.
set -euo pipefail
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SESSION="" PORT="" TOKEN_FILE="" ROOTS="" BROWSER_URL="" STATE_DIR="" BIND="127.0.0.1"
while (($#)); do
  [[ $# -ge 2 ]] || die "$1 needs a value"
  case "$1" in
    --session) SESSION="$2" ;;
    --port) PORT="$2" ;;
    --token-file) TOKEN_FILE="$2" ;;
    --roots) ROOTS="$2" ;;
    --openbot-browser-url) BROWSER_URL="$2" ;;
    --state-dir) STATE_DIR="$2" ;;
    --bind) BIND="$2" ;;
    *) die "Unknown option $1" ;;
  esac
  shift 2
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$SESSION" =~ ^staging-[A-Za-z0-9_.-]{1,48}$ ]] || die "--session must be a scratch Herdr session named staging-..."
[[ "$PORT" =~ ^[0-9]{4,5}$ && "$PORT" != 8765 && "$PORT" != 8766 ]] || die "--port must be a free port other than the production 8765/8766"
! (exec 3<>"/dev/tcp/$BIND/$PORT") 2>/dev/null || die "Port $PORT is already in use on $BIND"
[[ -f "$TOKEN_FILE" && $(tr -d '\r\n' < "$TOKEN_FILE" | wc -c) -ge 24 ]] || die "--token-file must hold a staging bearer token of 24+ characters"
[[ "$TOKEN_FILE" != /etc/openbot-herdr-bridge/* ]] || die "Use a staging token, not the production gateway's"
[[ -n "$ROOTS" ]] || die "--roots is required (scratch working directories)"
[[ -n "$STATE_DIR" && "$STATE_DIR" != /var/lib/openbot-herdr-gateway* ]] || die "--state-dir must be a scratch directory, not production state"
[[ -z "$BROWSER_URL" || "$BROWSER_URL" =~ ^https?:// ]] || die "--openbot-browser-url must be the staging OpenBot's browser route"
[[ "$(id -u)" != 0 ]] || die "Run as the staging user, not root"
mkdir -p -m 0700 "$STATE_DIR/artifacts"

printf 'Staging gateway: session=%s bind=%s:%s state=%s browser=%s\n' \
  "$SESSION" "$BIND" "$PORT" "$STATE_DIR" "${BROWSER_URL:-off}"
exec env -i PATH="$PATH" HOME="$HOME" \
  HERDR_SESSIONS="$SESSION" HERDR_SESSION="$SESSION" HERDR_AGENT_TARGET="" \
  HERDR_BIN="$(command -v herdr || echo herdr)" \
  OPENBOT_BRIDGE_HOST="$BIND" OPENBOT_BRIDGE_PORT="$PORT" \
  OPENBOT_BRIDGE_TOKEN_FILE="$TOKEN_FILE" \
  OPENBOT_BRIDGE_ALLOWED_ROOTS="$ROOTS" OPENBOT_BRIDGE_WORKDIR="" \
  OPENBOT_BRIDGE_ARTIFACT_DIR="$STATE_DIR/artifacts" OPENBOT_BRIDGE_ARTIFACT_MODE="inline" \
  OPENBOT_BRIDGE_BUSY_WAIT_SECONDS="600" OPENBOT_BRIDGE_RECOVERY_SECONDS="600" \
  OPENBOT_BRIDGE_LEDGER_PATH="$STATE_DIR/requests.sqlite3" \
  OPENBOT_BROWSER_CALLBACK_URL="$BROWSER_URL" \
  python3 -I -B "$HERE/bridge.py"
