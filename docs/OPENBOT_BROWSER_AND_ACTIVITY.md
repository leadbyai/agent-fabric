# Herdr gateway: OpenBot browser and Bot activity line

Two additions to the Herdr gateway. Neither changes any agent's configuration.

## 1. Bot activity line (`activity_line.py`)
**On by default.** While a request runs, the gateway reads the agent's visible screen every 2 seconds. When what the agent is doing changes, it sends:

```json
{"type": "CUSTOM", "name": "agent-fabric.progress",
 "value": {"state": "working", "line": "Listing files in current directory", "elapsedSeconds": 2}}
```

- **Claude Code:** the agent's own narration ("● I'll run ls next"), or the step in progress ("Reading 1 file…").
- **Codex:** the reasoning heading on its status line, else its latest action ("• Ran pytest" becomes "Running pytest").
- **Redaction:** secret-looking values, URL credentials, `--token` style flags and long tokens are cut, and lines are capped at 100 characters.
- **Traffic:** the seconds travel separately, so events go out only when the words change, plus the usual 15-second heartbeat.
- **Compatibility:** an OpenBot that doesn't know `line` ignores it.
- **Off switch:** `OPENBOT_ACTIVITY_LINE=0` restores the bare heartbeat.

The parser is tested against real Claude Code 2.1.326 and Codex 0.169.0 screens (`tests/fixtures/screens`).

## 2. OpenBot browser for Herdr agents (`herdr_browser.py`, `openbot_browser_cli.py`)
**On when `OPENBOT_BROWSER_CALLBACK_URL` is set** (installer: `--openbot-browser-url https://OPENBOT-HOST/api/agent-fabric/browser`). Needs OpenBot with leadbyai/openbot-custom#69.

For each person-started run (`forwardedProps.openbotRun`):
1. The gateway keeps the signed run and the `Authorization` value OpenBot sent, in memory only, under a random 256-bit key.
2. It writes `browser.json` into the request's private outbox. The file holds the gateway's local address and that key, never the credential.
3. The prompt tells the agent to run `python3 /opt/openbot-herdr-bridge/openbot_browser_cli.py <outbox>/browser.json navigate <url>` (or `read`, `screenshot`).
4. The CLI calls the gateway's `POST /openbot-browser/<key>`, and the gateway calls OpenBot with the signed run and `x-agent-fabric-credential`.
5. OpenBot drives the browser on **the person's own OpenBot computer**, where they can watch it in the Computer panel.
6. A screenshot (at most 4) is saved in the outbox, and the existing reply-image code attaches it.
7. When the request ends, the key is forgotten and `browser.json` is deleted.

**Limits:**
- OpenBot's signed run lasts 10 minutes; browser calls after that are refused.
- Codex sessions in a sandbox without network access can't reach the gateway, even on loopback. Codex Bots run with full access today.
- No browsing in group chats or in Bot-to-Bot runs: OpenBot refuses them.

## Install
`install.sh` installs the three new files with pinned checksums, and backs them up and restores them like the others. `tests/test_herdr_browser.py` fails if a pin drifts from its file.

**Keep the fixed `request_ledger.py` (b1359fbc) wherever it is installed.**

## Tests
- `tests/test_activity_line.py`: real screens, shapes, redaction.
- `tests/test_herdr_browser.py`: the whole path through the real bridge against a fake OpenBot, including that the credential never touches disk and that the turn's key dies with the request. Also the activity events, and the off switch.
