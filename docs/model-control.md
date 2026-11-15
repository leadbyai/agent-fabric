# Model and effort control

**Status:** `feat/model-control`, not released. Built on `a328c9a` (`feat/hermes-isolated-shared-profile`), which extends the live gateway commit `8b451cd`.

OpenBot's composer can show which model an agent runs on, and let a Bot's owner or an admin switch
its model and reasoning effort. The gateway does the reading and switching; OpenBot decides who may.

## Always for this session only

| CLI (verified) | How the gateway switches | Saves a default? |
| --- | --- | --- |
| Claude Code 2.1.325 | Opens `/model`, moves to the model, sets effort with ←/→, presses `s` ("use this session only") | No |
| Codex 0.169.0 | Types `/model <slug> --reasoning=<level>` | No |

Claude Code's typed `/model x` and `/effort x` **also save the choice as the Unix user's default for
every new session**, so the gateway never uses them, and never presses Enter on a model row (Enter there
saves a default). Enter is pressed only on "More models", where it expands the list.

A restarted agent therefore returns to whatever its host is configured for. That host configuration
stays the source of truth.

## Where the lists come from

Each CLI's own model cache, in the home of the user the gateway runs as. There is no model table in this repo.

- Claude Code: newest `~/.claude/cache/model-catalog/*.json` (`CLAUDE_CONFIG_DIR` honoured). The main list and "More models" are both offered.
- Codex: `~/.codex/models_cache.json` (`CODEX_HOME` honoured); models with `visibility: "list"`, in the CLI's order.

If a cache is missing, the model is shown as unavailable (`switchable: false` with a reason). Nothing is guessed.

## API

`GET /sessions/<s>/agents/<pane>/model[?refresh=1]` returns:

```json
{"agent": "claude", "provider": "anthropic", "switchable": true, "reason": null, "scope": "session",
 "current": {"model": "claude-opus-5-5", "label": "Opus 5.5", "effort": "medium", "observed_at": "…Z"},
 "models": [{"id": "claude-opus-5-5", "label": "Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"],
             "default_effort": "medium", "section": "main"}]}
```

- **Codex:** read passively from its status line.
- **Claude Code:** reading the model means opening and closing its menu, which holds the pane for about 1 s. So:
  - a plain GET returns the model last read or set here, plus the effort shown beside the prompt;
  - `?refresh=1` does the menu read. OpenBot asks for this when someone opens the picker.
  - The cache follows the agent's CLI session id, so a restarted agent is never shown its old model.
- **Other kinds:** the provider is reported (for example `gemini` → `google`), with `switchable: false`.

`PUT` with `{"model": "<id>", "effort": "<level>"}` (effort optional). Responses:

| Status | When |
| --- | --- |
| 200 | Switched. Body as GET, with `current` as confirmed on screen |
| 400 | Body missing, too large or not a JSON object |
| 403 | `OPENBOT_BRIDGE_MODEL_SWITCH=off` on this gateway |
| 404 | Unknown route, or the credential cannot reach this agent (discovery credentials never can) |
| 409 | The agent is working or blocked, or a request holds its pane |
| 422 | The agent does not offer that model, or that model does not take that effort |
| 501 | This agent kind cannot be switched, or its model cache is missing |
| 502 | The CLI did not confirm the switch. Menus were closed with Esc and nothing was retried |

The same credentials as `/ag-ui` apply: a bridge-wide token, or a per-agent token for its own route only.
`GET /health` reports `"model_control": {"read": true, "switch": <bool>}` so clients can tell an upgraded gateway.

## Safety

- Switches only while Herdr reports the agent `idle` or `done`, holding the same per-pane lock as prompts.
  A message arriving during a switch waits, or is refused as busy, exactly as for a running request.
- Herdr keeps reporting `idle` while the menu is open, so a switch never looks like "needs you".
- Every step re-reads the screen. The menu's cursor wraps, so key presses are never counted blind.
- A screen the driver does not recognise ends the attempt: Esc closes the menu, and a 502 says so.
  A menu a person already has open is never touched.
- Logs say which route switched; the model chosen is not logged.

## Measured (2026-11-14, laptop, real CLIs through this gateway)

- Claude Code read with refresh: 0.8 s. Switches 1.2–1.9 s, including "More models" (Fable 5.1).
- Codex switch: 0.6–2.6 s.
- `~/.claude/settings.json` and `~/.codex/config.toml` were unchanged by every switch.

## Turning it off

Set `OPENBOT_BRIDGE_MODEL_SWITCH=off` in the gateway's `config.env`: reads still work and switches return 403.

## Idle, empty prompt only

Nothing is typed or pressed unless the agent sits at an idle, **empty** prompt. Herdr reports an agent as
idle even when its user has typed an unsent draft, so the gateway also reads the pane's styled screen
(`agent read --format ansi`):

- **Claude Code**: the live prompt is the single `❯` line framed by a rule directly above and below it.
  Earlier `❯ /model` echoes in the scrollback are not framed, so repeated switches work. A multi-line
  draft breaks the frame and is refused.
- **Codex**: the live prompt is the nearest `›` line above the `<model> <effort> · <dir>` footer, and every
  row between them must be blank. A multi-line draft (even one whose first line is blank) leaves a
  continuation row there and is refused.
- Text after the prompt glyph counts as empty only when it is drawn as a hint (dim, SGR 2, or the
  grey palette). The same words typed by a person are not dim, so they count as a draft.
- An open menu, a draft, or any screen these rules cannot place returns **409 `agent_not_ready`** with
  nothing typed or pressed. A menu the user already had open is never closed; only a menu the gateway
  opened itself is closed with Esc.
