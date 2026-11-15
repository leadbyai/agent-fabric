"""Model and reasoning-effort control for existing Herdr agents (Agent Fabric).

Reads which models an agent's own CLI offers, which one it is running and how hard it thinks, and
switches them, always FOR THIS SESSION ONLY:

- Claude Code: typed `/model x` and `/effort x` also save the choice as the Unix user's default for
  every new session (verified on Claude Code 2.1.325). So the switch goes through the `/model` menu
  and its "s to use this session only" key, which saves nothing. Enter is never pressed on a model
  row (Enter there saves a default); it is pressed only on the "More models" row, where it expands.
- Codex: `/model <slug> --reasoning=<level>` is already session-only (verified on codex-cli 0.169.0).

A restart therefore returns the agent to whatever its host is configured for.

Where the lists come from: each CLI's own model cache in the agent user's home, never a table here.
Claude Code: `~/.claude/cache/model-catalog/*.json` (newest). Codex: `~/.codex/models_cache.json`.

Fail closed: nothing is typed or pressed unless the pane sits at an idle, EMPTY prompt (no unsent
draft, no menu open; told apart from a dim placeholder hint by the screen's styling), otherwise the
request is refused untouched (NotIdle, HTTP 409). Once this module has opened a menu, any screen it
does not recognise ends the attempt (only ITS menu is closed with Esc) and raises; a menu the user
already had open is never touched. Nothing is retried, and a switch is reported only once the CLI
has confirmed it.
Terminal I/O is passed in (`Pane`), so this module never runs Herdr itself and is testable offline.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

# Weakest first, in the spelling OpenBot's own picker uses on the wire.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Who serves each Herdr agent kind. A kind missing here is shown without a provider.
PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "copilot": "github",
    "cursor": "cursor",
    "grok": "xai",
    "kimi": "moonshot",
    "qwen": "alibaba",
    "amp": "sourcegraph",
    "droid": "factory",
}

SWITCHABLE_KINDS = ("claude", "codex")
POLL_SECONDS = 0.25
SETTLE_SECONDS = 6.0
MAX_MOVES = 32


class Unsupported(Exception):
    """This agent kind, CLI version or host cannot be driven (HTTP 501)."""


class InvalidChoice(ValueError):
    """The model or effort is not one this agent offers (HTTP 422)."""


class Unverified(RuntimeError):
    """The CLI did not confirm the switch; the menu this module opened was closed (HTTP 502)."""


class NotIdle(Exception):
    """The pane is not at an idle, empty prompt (a draft is typed, a menu is open, or the screen is not
    recognised); nothing was typed or pressed (HTTP 409). Not a RuntimeError on purpose: callers map
    RuntimeError to "Herdr unavailable"."""


class Pane(Protocol):
    def screen(self) -> str: ...  # the visible terminal text
    def styled(self) -> str: ...  # the same screen with ANSI styling (dim placeholder vs typed draft)
    def keys(self, *names: str) -> None: ...  # Herdr key names: Up, Down, Left, Right, Enter, Escape, s
    def submit(self, text: str) -> None: ...  # type a line and press Enter


@dataclass
class ModelOption:
    id: str
    label: str
    efforts: list[str] = field(default_factory=list)
    default_effort: str | None = None
    section: str = "main"  # "main", or "more" for a CLI's "More models" list


@dataclass
class ModelState:
    model: str | None  # catalogue id, or None when the CLI names a model the catalogue lacks
    label: str
    effort: str | None


def provider_for(kind: str) -> str | None:
    return PROVIDERS.get(kind)


def ordered_efforts(levels) -> list[str]:
    offered = {level for level in levels if isinstance(level, str)}
    return [level for level in EFFORT_ORDER if level in offered]


# ---------------------------------------------------------------- catalogues


def _home(environ: dict) -> Path:
    return Path(environ.get("HOME") or os.path.expanduser("~"))


def claude_catalogue(environ: dict | None = None) -> list[ModelOption]:
    environ = os.environ if environ is None else environ
    base = Path(environ.get("CLAUDE_CONFIG_DIR") or _home(environ) / ".claude")
    files = sorted((base / "cache" / "model-catalog").glob("*.json"),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise Unsupported("Claude Code has not cached its model list on this host yet")
    data = json.loads(files[0].read_text(encoding="utf-8"))
    rows = (((data.get("catalog") or {}).get("config") or {}).get("models")) or []
    models = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("name"), str):
            continue
        runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
        models.append(ModelOption(
            id=row["id"], label=row["name"],
            efforts=ordered_efforts(runtime.get("effort_levels") or []),
            default_effort=runtime.get("default_effort") if isinstance(runtime.get("default_effort"), str) else None,
            section="main" if row.get("section") == "main" else "more"))
    if not models:
        raise Unsupported("Claude Code's cached model list is empty or unreadable")
    return models


def codex_catalogue(environ: dict | None = None) -> list[ModelOption]:
    environ = os.environ if environ is None else environ
    path = Path(environ.get("CODEX_HOME") or _home(environ) / ".codex") / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Unsupported("Codex has not cached its model list on this host yet") from exc
    rows = [row for row in data.get("models") or [] if isinstance(row, dict)
            and row.get("visibility") == "list" and isinstance(row.get("slug"), str)]
    rows.sort(key=lambda row: row.get("priority") if isinstance(row.get("priority"), int) else 1 << 30)
    models = [ModelOption(
        id=row["slug"], label=str(row.get("display_name") or row["slug"]),
        efforts=ordered_efforts(level.get("effort") for level in row.get("supported_reasoning_levels") or []
                                if isinstance(level, dict)),
        default_effort=row.get("default_reasoning_level") if isinstance(row.get("default_reasoning_level"), str) else None)
        for row in rows]
    if not models:
        raise Unsupported("Codex's cached model list is empty or unreadable")
    return models


def catalogue_for(kind: str, environ: dict | None = None) -> list[ModelOption]:
    if kind == "claude":
        return claude_catalogue(environ)
    if kind == "codex":
        return codex_catalogue(environ)
    raise Unsupported(f"Model switching is not available for {kind or 'this'} agent")


def validate(models: list[ModelOption], model_id: object, effort: object) -> tuple[ModelOption, str | None]:
    if not isinstance(model_id, str) or not model_id:
        raise InvalidChoice("model is required")
    chosen = next((option for option in models if option.id == model_id), None)
    if chosen is None:
        raise InvalidChoice("This agent does not offer that model")
    if effort is None:
        return chosen, None
    if not isinstance(effort, str) or effort not in chosen.efforts:
        raise InvalidChoice("That model does not take that effort")
    return chosen, effort


# ---------------------------------------------------------------- screens

_ROW = re.compile(r"^\s*(?P<cursor>❯)?\s*(?:(?P<number>\d+)\.\s+(?P<label>.+?)(?P<check>\s✔)?(?:\s{2,}.*)?|(?P<more>[▸▾])\s+More models)\s*$")
_CLAUDE_EFFORT = re.compile(r"^\s*\S\s+(?P<label>Low|Medium|High|xHigh|Max)\s+effort\b")
_CLAUDE_DONE = re.compile(r"Set model to (?P<label>.+?) for this session only(?: with (?P<effort>\w+) effort)?\s*$")
_CLAUDE_FOOTER_EFFORT = re.compile(r"[○◐●◉◈]\s+(?P<effort>low|medium|high|xhigh|max)\s+·\s+/effort")
_CODEX_FOOTER = re.compile(
    r"^\s*(?P<label>\S.*?) (?P<effort>none|minimal|low|medium|high|xhigh|max|ultra|default) · ")


def claude_menu(screen: str) -> dict | None:
    """The open `/model` menu: rows in screen order, which has the cursor, which is current, and
    the effort shown; None when the menu is not on screen."""
    lines = screen.splitlines()
    try:
        start = max(i for i, line in enumerate(lines) if line.strip() == "Select model")
    except ValueError:
        return None
    rows, cursor, current, effort, expanded = [], None, None, None, False
    for line in lines[start + 1:]:
        match = _ROW.match(line)
        if match:
            if match.group("more"):
                label, expanded = "More models", match.group("more") == "▾"
            else:
                label = match.group("label").strip()
                if label.startswith("Default (recommended)"):
                    label = "Default (recommended)"
            if match.group("cursor"):
                cursor = len(rows)
            if match.group("check"):
                current = label
            rows.append(label)
            continue
        found = _CLAUDE_EFFORT.match(line)
        if found:
            effort = found.group("label").lower()
    if not rows or cursor is None:
        return None
    return {"rows": rows, "cursor": cursor, "current": current, "effort": effort, "expanded": expanded}


def claude_confirmation(screen: str) -> tuple[str, str | None] | None:
    """The CLI's reply to the last `/model` command, when it confirms a session-only switch."""
    lines = screen.splitlines()
    echoes = [i for i, line in enumerate(lines) if line.strip() == "❯ /model"]
    if not echoes:
        return None
    for line in lines[echoes[-1] + 1: echoes[-1] + 4]:
        match = _CLAUDE_DONE.search(line)
        if match:
            effort = match.group("effort")
            return match.group("label").strip(), effort.lower() if effort else None
    return None


def claude_footer_effort(screen: str) -> str | None:
    """The effort Claude Code shows beside its prompt ("● high · /effort"), read without a menu."""
    found = None
    for line in screen.splitlines():
        match = _CLAUDE_FOOTER_EFFORT.search(line)
        if match:
            found = match.group("effort")
    return found


def codex_footer(screen: str) -> tuple[str, str] | None:
    found = None
    for line in screen.splitlines():
        match = _CODEX_FOOTER.match(line)
        if match:
            found = (match.group("label").strip(), match.group("effort"))
    return found


# ---------------------------------------------------------------- the prompt (composer)

# The glyph each CLI draws at the start of its input line. Claude Code 2.1.325: "❯" + NBSP between two
# horizontal rules. Codex: "›" (newer) or "▌" (older). An empty prompt shows either nothing or a hint
# drawn DIM (SGR 2) or in the bright-black/grey palette; anything else after the glyph is a draft.
COMPOSER_GLYPHS = {"claude": ("❯",), "codex": ("›", "▌")}
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OTHER_ESC = re.compile(r"\x1b(?:\[[0-9;:?]*[A-Za-ln-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z])")
_RULE = re.compile(r"^\s*[─━]{8,}\s*$")


def _faint(params: list[str], faint: bool) -> bool:
    """Track whether text is drawn as a hint: SGR 2 (dim), 90 (bright black) or 38;5;8/244-247."""
    i = 0
    while i < len(params):
        code = params[i]
        if code in ("", "0", "22", "39"):
            faint = False if code in ("", "0", "22") else faint
        elif code in ("2", "90"):
            faint = True
        elif code == "38" and i + 2 < len(params) and params[i + 1] == "5":
            faint = params[i + 2] in ("8", "240", "241", "242", "243", "244", "245", "246", "247")
            i += 2
        elif code == "38" and i + 4 < len(params) and params[i + 1] == "2":
            i += 4
        i += 1
    return faint


def _typed_text(styled_line: str) -> str:
    """The visible characters of *styled_line* that are NOT drawn as a hint."""
    line = _OTHER_ESC.sub("", styled_line)
    out, faint, pos = [], False, 0
    for match in _SGR.finditer(line):
        if not faint:
            out.append(line[pos:match.start()])
        faint = _faint(match.group(1).replace(":", ";").split(";"), faint)
        pos = match.end()
    if not faint:
        out.append(line[pos:])
    return "".join(out)


def composer_state(kind: str, styled: str) -> str | None:
    """"empty", "draft", or None when this module cannot locate the live prompt unambiguously.

    Claude Code: the live prompt is the ONE glyph line framed by a rule directly above and below it
    (earlier `❯ /model` echoes in the scrollback are not framed, so repeated switches still work; a
    multi-line draft breaks the frame and is refused). Codex: the live prompt is the nearest glyph line
    above the status footer, and every row between them must be blank (a multi-line draft, even one
    whose first line is blank, leaves continuation rows there and is refused)."""
    glyphs = COMPOSER_GLYPHS.get(kind)
    if not glyphs:
        return None
    lines = styled.replace("\r", "").split("\n")
    plain = [_OTHER_ESC.sub("", _SGR.sub("", line)) for line in lines]
    is_prompt = [line.lstrip().startswith(glyphs) for line in plain]
    if kind == "claude":
        framed = [i for i in range(1, len(plain) - 1)
                  if is_prompt[i] and _RULE.match(plain[i - 1]) and _RULE.match(plain[i + 1])]
        if len(framed) != 1:
            return None
        i = framed[0]
    else:
        footers = [i for i, line in enumerate(plain) if _CODEX_FOOTER.match(line)]
        if not footers:
            return None
        i = footers[-1] - 1
        while i >= 0 and not plain[i].replace("\u00a0", " ").strip():
            i -= 1  # blank rows between the prompt and the footer are fine
        if i < 0 or not is_prompt[i]:
            return None  # a continuation row (or anything else) sits above the footer: refuse
    raw = lines[i]
    glyph_at = min(raw.find(g) for g in glyphs if raw.find(g) >= 0)
    after = next(g for g in glyphs if raw.startswith(g, glyph_at))
    rest = _typed_text(raw[glyph_at + len(after):])
    return "empty" if not rest.replace("\u00a0", " ").strip() else "draft"


def require_empty_prompt(pane: Pane, kind: str) -> None:
    """Refuse (NotIdle) unless the pane is at an empty prompt. Reads only; never types or presses."""
    state = composer_state(kind, pane.styled())
    if state == "draft":
        raise NotIdle("The agent has unsent text at its prompt; nothing was changed")
    if state != "empty":
        raise NotIdle("The agent's prompt is not recognisably idle and empty; nothing was changed")


# ---------------------------------------------------------------- drivers


def _wait(pane: Pane, test: Callable[[str], object], sleep, clock, seconds: float = SETTLE_SECONDS):
    deadline = clock() + seconds
    while True:
        screen = pane.screen()
        result = test(screen)
        if result:
            return result
        if clock() >= deadline:
            return None
        sleep(POLL_SECONDS)


class ClaudeDriver:
    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock

    def _close(self, pane: Pane) -> None:
        for _ in range(3):
            if claude_menu(pane.screen()) is None:
                return
            pane.keys("Escape")
            self.sleep(POLL_SECONDS)

    def _ready(self, pane: Pane) -> None:
        """Checks only, before anything is typed: no menu open (the user's own is left alone) and an
        empty prompt. Raises NotIdle; the caller must not close anything when this raises."""
        if claude_menu(pane.screen()) is not None:
            raise NotIdle("A Claude Code menu is already open in this pane; it was left as it is")
        require_empty_prompt(pane, "claude")

    def _open(self, pane: Pane) -> dict:
        """Type `/model` and wait for the menu. Call only after _ready(); the menu is then ours."""
        pane.submit("/model")
        menu = _wait(pane, claude_menu, self.sleep, self.clock)
        if menu is None:
            raise Unverified("Claude Code did not open its model menu")
        return menu

    def read(self, pane: Pane, models: list[ModelOption]) -> ModelState:
        """Open the menu, read the checked model and the effort, close it (prints "Kept model")."""
        self._ready(pane)  # raises before anything is typed; nothing of ours to close then
        try:
            menu = self._open(pane)
        finally:
            self._close(pane)  # only ever our own menu: _ready() proved none was open before
        label = menu["current"] or "Default (recommended)"
        option = next((m for m in models if m.label == label), None)
        return ModelState(option.id if option else None, label, menu["effort"])

    def _move_to(self, pane: Pane, label: str) -> dict:
        for _ in range(MAX_MOVES):
            menu = claude_menu(pane.screen())
            if menu is None:
                raise Unverified("Claude Code's model menu closed unexpectedly")
            if label in menu["rows"] and menu["rows"][menu["cursor"]] == label:
                return menu
            target = menu["rows"].index(label) if label in menu["rows"] else None
            if target is None:
                raise Unverified(f"{label} is not in Claude Code's model menu")
            pane.keys("Down" if target > menu["cursor"] else "Up")
            self.sleep(POLL_SECONDS / 2)
        raise Unverified("Could not reach the model in Claude Code's menu")

    def _set_effort(self, pane: Pane, wanted: str, levels: list[str]) -> None:
        for _ in range(MAX_MOVES):
            menu = claude_menu(pane.screen())
            if menu is None or menu["effort"] is None:
                raise Unverified("Claude Code's menu shows no effort for this model")
            if menu["effort"] == wanted:
                return
            if menu["effort"] not in EFFORT_ORDER or wanted not in levels:
                raise Unverified("Claude Code shows an effort this module does not know")
            pane.keys("Right" if EFFORT_ORDER.index(wanted) > EFFORT_ORDER.index(menu["effort"]) else "Left")
            self.sleep(POLL_SECONDS / 2)
        raise Unverified("Could not set the effort in Claude Code's menu")

    def switch(self, pane: Pane, option: ModelOption, effort: str | None) -> ModelState:
        self._ready(pane)  # raises before anything is typed; nothing of ours to close then
        try:
            menu = self._open(pane)
            if option.label not in menu["rows"]:
                if option.section != "more":
                    raise Unverified(f"{option.label} is not in Claude Code's model menu")
                menu = self._move_to(pane, "More models")
                if not menu["expanded"]:
                    pane.keys("Enter")  # on "More models" Enter only expands the list
                    if _wait(pane, lambda s: (claude_menu(s) or {}).get("expanded"), self.sleep, self.clock) is None:
                        raise Unverified("Claude Code did not expand More models")
            self._move_to(pane, option.label)
            if effort:
                self._set_effort(pane, effort, option.efforts)
            menu = claude_menu(pane.screen())
            if menu is None or menu["rows"][menu["cursor"]] != option.label:
                raise Unverified("Claude Code's menu moved before the switch")
            pane.keys("s")

            def confirmed(screen: str):
                # Only this request's line counts: the menu must have closed, and the line must name
                # the requested model (an earlier switch's line stays on screen until it scrolls).
                if claude_menu(screen) is not None:
                    return None
                found = claude_confirmation(screen)
                if found and found[0] == option.label and (not effort or found[1] == effort):
                    return found
                return None

            result = _wait(pane, confirmed, self.sleep, self.clock)
        except Exception:
            self._close(pane)
            raise
        if result is None:
            self._close(pane)
            raise Unverified("Claude Code did not confirm the session-only switch")
        return ModelState(option.id, result[0], result[1])


class CodexDriver:
    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock

    @staticmethod
    def state_from(screen: str, models: list[ModelOption]) -> ModelState | None:
        footer = codex_footer(screen)
        if footer is None:
            return None
        label, effort = footer
        option = next((m for m in models if m.label == label or m.id == label.lower()), None)
        if effort == "default":
            effort = option.default_effort if option else None
        return ModelState(option.id if option else None, label, effort)

    def read(self, pane: Pane, models: list[ModelOption]) -> ModelState:
        state = self.state_from(pane.screen(), models)
        if state is None:
            raise Unverified("Codex's status line is not on screen")
        return state

    def switch(self, pane: Pane, option: ModelOption, effort: str | None) -> ModelState:
        require_empty_prompt(pane, "codex")  # never append to an unsent draft
        command = f"/model {option.id}" + (f" --reasoning={effort}" if effort else "")
        pane.submit(command)

        def settled(screen: str):
            state = self.state_from(screen, [option])
            if state and state.model == option.id and (effort is None or state.effort == effort):
                return state
            return None

        state = _wait(pane, settled, self.sleep, self.clock)
        if state is None:
            raise Unverified("Codex did not show the requested model and effort")
        return state


def driver_for(kind: str, **kwargs):
    if kind == "claude":
        return ClaudeDriver(**kwargs)
    if kind == "codex":
        return CodexDriver(**kwargs)
    raise Unsupported(f"Model switching is not available for {kind or 'this'} agent")


def describe(kind: str, models: list[ModelOption] | None, state: ModelState | None, *,
             switchable: bool, reason: str | None, observed_at: str | None) -> dict:
    """The JSON shape of GET/PUT /sessions/<s>/agents/<pane>/model."""
    return {
        "agent": kind,
        "provider": provider_for(kind),
        "switchable": switchable,
        "reason": reason,
        "scope": "session",
        "current": None if state is None else {**asdict(state), "observed_at": observed_at},
        "models": [asdict(option) for option in models or []],
    }
