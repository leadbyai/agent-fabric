# Claude Agent SDK bridge (pilot)

`claude-sdk/claude_sdk_bridge.mjs` serves AG-UI for **SDK-managed** Claude Code sessions, without Herdr.
Each OpenBot thread gets its own session, created through the official Claude Agent SDK
(`@anthropic-ai/claude-agent-sdk`) on the thread's first run and resumed on later runs. It does not
attach to an interactive session another process owns (see "Existing running sessions" below).

## Behaviour
- **Routes:** `GET /health` (no auth), `POST /ag-ui` (bearer), `GET /requests/<id>` (bearer).
- **Binding and auth:** loopback only (`127.0.0.1`). The bearer is checked against `SDK_BRIDGE_TOKEN_SHA256`;
  the bridge never stores the token.
- **Isolation:**
  - `settingSources: []`, so no user, project or local settings, CLAUDE.md or settings hooks load;
  - `strictMcpConfig: true` with `mcpServers: {}`, and claude.ai connectors off
    (`ENABLE_CLAUDEAI_MCP_SERVERS=false`);
  - a small built-in tool set (`tools`).
  - Doc: https://code.claude.com/docs/en/agent-sdk/claude-code-features.md ("Control filesystem settings
    with settingSources", "What settingSources does not control").
- **Authorization:**
  - `canUseTool` allows file tools only inside `SDK_BRIDGE_WORKSPACE`, plus one named subagent; everything
    else is denied.
  - Calls that are auto-approved never reach the callback (per the docs), so the tool set itself is
    restricted too.
  - Docs: https://code.claude.com/docs/en/agent-sdk/permissions.md and
    https://code.claude.com/docs/en/agent-sdk/user-input.md.
- **Idempotency:** a ledger (`node:sqlite`) keyed by H(thread, run).
  - A replay returns the stored reply without a new turn; the same run id with different text gets 409.
  - A failed run becomes `uncertain` and is never resent.
- **Session ownership:** the ledger maps thread → session, and a session already mapped to another
  thread is refused. New sessions get a title with a hash of the thread (`title` option).
- **Streaming:**
  - `includePartialMessages: true`. Main-session `text_delta` events become `TEXT_MESSAGE_CONTENT`;
    subagent text is not mixed in.
  - Tool calls and results are CUSTOM `agent-fabric.tool` events. A subagent spawn (`Agent`/`Task`) is
    also CUSTOM `agent-fabric.subagent`; subagent-internal calls carry `subagent: true`, via
    `parent_tool_use_id`.
  - AG-UI `TOOL_CALL_*` events are deliberately not used, because OpenBot would treat them as its own tools.
  - Docs: https://code.claude.com/docs/en/agent-sdk/streaming-output.md and
    https://code.claude.com/docs/en/agent-sdk/subagents.md.
- **Sessions:** the session id is taken from the `system`/`init` message; `resume` continues a session.
  Doc: https://code.claude.com/docs/en/agent-sdk/sessions.md.

## Existing running sessions (why this bridge does not attach to them)
- **Remote Control** is the supported way to drive an already-running interactive session, but only from
  the Claude apps (claude.ai/code, mobile). It needs a claude.ai subscription login and is not available
  with API keys. No API for third-party programs is documented on its page.
  Doc: https://code.claude.com/docs/en/remote-control.md.
- **SDK `resume`** does not attach to a running process. A `query()` runs its own Claude Code
  process that continues the session from its on-disk transcript; the process model itself is not
  documented, and resuming requires the session file on the same machine.
  Doc: https://code.claude.com/docs/en/agent-sdk/sessions.md.
- **Two writers:** resuming a session that another process already has open, without forking, produces
  two writers on one transcript. The Claude Code docs say messages from both interleave into one
  transcript. Doc: https://code.claude.com/docs/en/sessions.md ("Branch a session").
  - Forking (`forkSession`) avoids that, but it creates a *new* session; it does not control the running
    one.
- **Cross-session messages:** a running session has a per-session inbox socket for messages from other
  Claude Code sessions. Those arrive as messages subject to the receiver's own controls (they cannot
  approve anything), not as operator turns, and the page does not document a body format for third-party
  clients. Doc: https://code.claude.com/docs/en/cross-session-messaging.md.
- **Conclusion:** an existing interactive session can be driven only through Remote Control, from the
  Claude apps. Programmatic control needs an **exclusive-ownership handoff**:
  1. the interactive process ends cleanly at an idle point;
  2. a single SDK owner resumes the session;
  3. the old process is never restarted against it;
  4. returning to interactive use is a reverse handoff.
  - Without that, the result is two writers.

## Configuration
`SDK_BRIDGE_TOKEN_SHA256` (required), `SDK_BRIDGE_WORKSPACE` (absolute, required), `SDK_BRIDGE_LEDGER`
(absolute, required), `SDK_BRIDGE_PORT` (8768), `SDK_BRIDGE_MODEL`, `SDK_BRIDGE_MAX_TURNS` (8),
`SDK_BRIDGE_MAX_BUDGET_USD` (2), `SDK_BRIDGE_TIMEOUT_SECONDS` (600). `CLAUDE_CONFIG_DIR` is passed through
to the SDK, which relocates its config and transcripts.

## Tests
`npm test` in `claude-sdk/` runs offline tests with a fake `query`:
- auth;
- create then resume;
- text, tool and subagent events;
- replay and 409;
- uncertain not resent;
- session ownership;
- policy.
