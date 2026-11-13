# Agent Fabric v0.6.0 — bounded busy-agent queue and timeout recovery

This release builds on v0.5.1 (prose-wrap repair). The running v0.5.0 gateways are
unmodified until an operator installs a tagged release.

## What is fixed

- A request arriving while its Herdr pane is `working` waits for that agent
  to reach `idle` or `done` instead of immediately returning HTTP 409.
- Multiple OpenBot requests to one pane are serviced in FIFO order within the
  running gateway. Existing Herdr work is not interrupted.
- The AG-UI response stays alive with `CUSTOM` progress events, so OpenBot's
  byte-activity watchdog can distinguish waiting from a silent agent.
- Herdr `agent prompt` is submitted **once**. If its `--wait` step times out,
  the bridge can inspect the existing transcript for its unique reply markers;
  it never retries the prompt automatically.
- A local SQLite run ledger remembers completed replies or uncertain submissions
  keyed by session, pane, OpenBot thread ID and run ID. Transport retries with
  the same IDs cannot submit a duplicate, including across gateway restarts.
- The authenticated `GET /requests/<id>` endpoint returns completion state and
  reply; `/attention` lists unresolved requests without including their prompts.

## Boundaries and behavior

- **Opt-in:** `--busy-wait-seconds` and `--recovery-seconds` default to `0`,
  preserving the older behavior until the canary is approved.
- A request **waiting to be submitted** is held by the active AG-UI stream,
  not yet written to SQLite. Waiting requests are canceled on a disconnected
  stream or gateway restart; they have *not* been submitted to Herdr.
- Waiting is capped at 3,600 seconds, and the gateway admits at most 64
  queued HTTP requests. At the configured deadline it returns `RUN_ERROR`
  that explicitly states **no prompt was submitted**.
- Once the ledger reserves a run for submission, a crash can leave that run
  `uncertain`. This is intentional: a nontransactional Herdr command can have
  reached the agent before a network or process failure. Inspect Herdr and
  reconcile rather than silently repeating work.
- Duplicate protection is strongest for retries with the **same run ID**.
  A *new* run ID is a new request, even if its text resembles an earlier one.
  A future OpenBot-specific continuation hook is needed to make all browser
  reconnects and background notifications fully durable.
- The ledger stores reply text, opaque IDs and a SHA-256 fingerprint of
  submitted input. It does not store the user's prompt or attachment bytes.
  Its directory is owner-only and SQLite files are mode `0600`.
- The bridge preserves its `/ag-ui`, per-session/per-pane routes, authentication,
  file handling and live roster. It never launches, stops or restarts agents.

## Release process

1. Merge the separately tested formatting PR, then review this stacked PR.
2. Run the offline bridge test suite, compile both Python files, validate
   `install.sh`, and verify the embedded SHA-256 checksums.
3. Tag the merged sources `v0.6.0` **only after** the installer checksum matches.
4. Canary **one low-impact Herdr pane** first, with e.g. `--busy-wait-seconds 600`
   and `--recovery-seconds 600`. Keep Cloudflare routing, existing token,
   selected session list, upload roots and port unchanged.
5. Confirm: normal request; busy-to-idle queued request; queued expiry with
   no submission; injected Herdr timeout without a second prompt; attachment
   transfer; stale-state rejection; and a safe operator rollback.
6. Roll out to LBAI Main, 3 Wise Men and Arcaiva only after canary evidence.

The installer backs up the previous bridge, ledger module (if present),
configuration and systemd unit. If its restarted service fails the local
health check, it attempts to restore the previous code/config/unit and restart
the old gateway. No Herdr session or agent is restarted by this installer.

**Do not run `v0.6.0` installer commands until the matching release tag exists.**
