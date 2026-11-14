# External chat sync — Agent Fabric read and delivery side

This is the first code checkpoint for `openbot-custom` PR #17's approved design.

`external_sync_events.py` classifies **synthetic or already-authorized** Claude
Code JSONL records into deterministic, sanitized import candidates. It is
read-only, has no network calls, and cannot prompt/restart an agent.

Current scope:

- Human terminal messages and agent text responses; simple tool **names only**.
- Deterministic `afx-` event IDs for downstream OpenBot deduplication.
- Fail-closed provenance checking and per-turn suppression for OpenBot-originated
  exchanges, including across batch boundaries.
- Obvious credential redaction and a 4 KiB byte cap per event.
- Claude peer messages with redacted text and a generic sender label. Outbound
  peer calls expose an activity summary only. Subagent transcripts, tool
  inputs/outputs and thinking stay out of the mirror.
- A separate SQLite cursor helper that stores **positions only**, reads bounded
  complete JSONL records, refuses rotated/truncated sources, and commits its
  cursor and echo-suppression state only after downstream ACK.

## Run in isolated staging

Both watchers are separate from the gateway. Each config enables one explicitly
bound staging Bot. Use a scratch source and inject a dedicated importer secret
as `AGENT_FABRIC_IMPORT_SECRET` through the staging environment.

Claude Code example:

```json
{
  "enabled": true,
  "botId": "agent_staging_fixture",
  "herdrSession": "external-sync-staging",
  "paneId": "w1:p1",
  "claudeConfigDir": "/path/to/scratch/claude",
  "cwd": "/path/to/scratch/project",
  "openbotOrigin": "http://127.0.0.1:3102"
}
```

Run `python3 external_sync_watcher.py --config claude.json --ledger claude.sqlite3`
as the scratch Herdr user. It resolves foreground PID and kernel start time
before reading the transcript. Its first scan starts at the current end.

For Hermes, use the same `enabled`, `botId` and `openbotOrigin` keys, plus
`hermesStateDb`, `installId`, `profileId` and the native CLI/TUI `sessionId`.
Run `python3 external_sync_hermes.py --config hermes.json --ledger hermes.sqlite3`
against a scratch profile. The watcher follows a unique compression continuation
and uses durable transcript message IDs so gateway replay resets do not create
duplicates. Bind a dedicated native conversation for mirroring. OpenBot-created
Hermes sessions are outside this source.

## Required before production

1. Per-Bot explicit opt-in (default OFF), using a synthetic staging Bot first.
2. Authoritative pane → foreground PID → Claude session ID → JSONL validation,
   including PID reuse, in-app `/resume`, truncation and file rotation.
3. Durable cursor and suppression state with **advance only after import ACK**;
   handle network interruption, retry, restart and batched partial acceptance.
4. A model-less, locked, authenticated OpenBot import route in a separately
   reviewed overlay image, using deterministic IDs without generating prompts.
5. A clear default Live Session channel and visible UI entry point; prove that
   context continuity is actual native-session continuity, not just copied text.
6. Bounded staging acceptance: direct Herdr message appears without OpenBot
   input, survives refresh/restart, no echoes, no secrets, unchanged normal
   OpenBot/Herdr and screenshot behavior.

**Important:** Redaction is defense in depth, not a guarantee that arbitrary
customer data is safe for hosted CopilotKit storage. Keep sync disabled unless
Derek has explicitly approved the Bot and its data scope. No production use.

Run tests:

```bash
python3 -m unittest discover -s tests -p 'test_external_sync_*.py'
```
