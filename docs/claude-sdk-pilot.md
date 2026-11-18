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
Both profiles:
- `SDK_BRIDGE_TOKEN_SHA256` (required): the bridge holds only the hash; OpenBot holds the bearer.
- `SDK_BRIDGE_LEDGER` (absolute, required).
- `SDK_BRIDGE_HOST`: `127.0.0.1` (default) or `172.17.0.1`, the docker0 gateway, which the OpenBot container can reach. Anything else is refused.
- `SDK_BRIDGE_PORT` (8768; 1024-65535) and `SDK_BRIDGE_MODEL`.
- Caps: `SDK_BRIDGE_MAX_TURNS` (8; 1-500), `SDK_BRIDGE_MAX_BUDGET_USD` (2; 0.01-100), `SDK_BRIDGE_TIMEOUT_SECONDS` (600; 10-7200).
- **Concurrency.** `SDK_BRIDGE_MAX_CONCURRENT` (1; 1-4) sets how many live conversations run at once. Each conversation runs one run at a time, so an OpenBot Retry or a second tab gets 409 `conversation_busy`; over the cap gets 429 `bridge_busy`. In both cases no prompt is sent and no ledger row is written.
- **Failure codes.** `RUN_ERROR` carries a `code` naming what stopped the run: the SDK result subtype (`error_max_turns`, `error_max_budget_usd`, ...), `timeout`, `denied_session` or `workspace_pinned_file_changed`/`_missing`.
- `SDK_BRIDGE_DENY_SESSIONS`: session ids that are never resumed or adopted, such as the agent's live interactive session.
- `CLAUDE_CONFIG_DIR` is passed through, so the SDK uses that account's own sign-in where it already is.
- The bridge never passes `continue`, and it resumes only sessions it created and recorded in its ledger.

### `SDK_BRIDGE_PROFILE=pilot` (default)
Isolated: no settings, strict empty MCP, Read/Write/Glob/Agent limited to `SDK_BRIDGE_WORKSPACE`, plus the `word-counter` subagent. Agent-profile variables are refused.

### `SDK_BRIDGE_PROFILE=agent` (an existing agent's own setup)
- **Workspace.** `SDK_BRIDGE_WORKSPACE_ROOT` (required). Each conversation gets `<root>/<conversation-id>/`, a 16-hex id derived from the thread, so raw thread ids never reach paths. It is seeded once from `<root>/_template/`.
- **Template pins.** `SDK_BRIDGE_TEMPLATE_SHA256` (required): `<relative path>=<sha256>,...`.
  - The template must hold exactly those files, with no symlinks and no extras.
  - Hashes are checked before and after every seed.
  - The pinned paths inside the conversation folder are re-checked before **every** turn. A session that edited its own CLAUDE.md or deny list is refused, and the folder is never silently re-seeded. Other files and worktrees are ignored.
  - New conversation folders are made private: 0700 folders and 0600 files.
  - Run the unit with `UMask=0077`.
- **Title.** `SDK_BRIDGE_TITLE` (required) must contain `{id}`, for example `Vex (OpenBot {id})`, so a bridge session never takes the agent's own title. Watchdog duplicate guards match by title.
- **Setting sources.** `SDK_BRIDGE_SETTING_SOURCES` (`user,project`) must include `project`, so the workspace's CLAUDE.md and its `permissions.deny` load.
- **Extra settings.** `SDK_BRIDGE_SETTINGS` (optional absolute file) is layered on top as flag settings.
- **Tools.** `SDK_BRIDGE_TOOLS`: `preset` (all Claude Code tools, the default) or a comma list.
- **Permission mode.** `SDK_BRIDGE_PERMISSION_MODE`: `default`, `acceptEdits`, `dontAsk`, `plan` or `bypassPermissions`. Bypass also sets `allowDangerouslySkipPermissions`. Bypass removes prompts, not deny rules.
- **MCP.** `SDK_BRIDGE_STRICT_MCP` (0; 1 means no MCP servers) and `SDK_BRIDGE_CLAUDEAI_MCP` (0).
- **CLI.** `SDK_BRIDGE_CLAUDE_PATH` (optional absolute path). The default is the CLI bundled with the pinned SDK: 0.3.325 bundles Claude Code 2.1.325. The bundled CLI is recommended, because an auto-updated user install changes underneath the bridge. The CLI version reported at session start is logged.
- The system prompt is the `claude_code` preset, and there is no toy subagent.

Caps for full Vex (agreed with Main): `SDK_BRIDGE_MAX_TURNS=60`, `SDK_BRIDGE_MAX_BUDGET_USD=15`, `SDK_BRIDGE_TIMEOUT_SECONDS=1800`, `SDK_BRIDGE_MAX_CONCURRENT=1`.

## Tests
`npm test` in `claude-sdk/` runs offline tests with a fake `query`:
- auth;
- create then resume;
- text, tool and subagent events;
- replay and 409;
- uncertain not resent;
- session ownership;
- policy;
- host, cap and port bounds;
- profile validation;
- agent options (bypass flag, never `continue`, no toy subagent);
- titles;
- per-conversation seeding;
- template pins (changed file, extra file, unpinned, symlink);
- the CLI path;
- denied and foreign sessions refused, even when a ledger row names them;
- single flight per conversation, for new and resumed threads;
- the global concurrency cap;
- RUN_ERROR codes;
- the per-turn pinned-file check;
- folder modes.
