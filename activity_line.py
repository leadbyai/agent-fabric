"""One short line saying what a Herdr agent is doing right now (OpenBot's "Bot activity", Summary level).

Read from the pane's visible screen while a request runs, and sent only on that run's own AG-UI
stream (a CUSTOM `agent-fabric.progress` event, which OpenBot shows in place of "Thinking" and does
not store as a message). Shapes taken from real screens (tests/fixtures/screens):

- Claude Code 2.1: a live spinner line, "✢ Zesting… (2s · ↓ 10 tokens)", whose word is decoration.
  Above it, the agent's own narration ("● I'll run ls as soon as it finishes.") or a step in
  progress ("  Reading 1 file…"). A finished turn reads "✻ Worked for 3s · done".
- Codex 0.169: a status line, "• Working (14s • esc to interrupt)", where "Working" becomes the
  model's reasoning heading when it writes one. Above it, narration ("• I'll read note.txt…") or an
  action ("• Ran sleep 15", "• Explored" over "└ Read note.txt").

Everything is redacted before it leaves (secret-looking values, credentials in URLs, long tokens)
and cut to 100 characters.
"""
from __future__ import annotations

import re

MAX_LINE = 100
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
TIME = r"((?:\d+h\s*)?(?:\d+m\s*)?\d+s)"
CLAUDE_SPINNER = re.compile(r"^\s*[·✢✳✶✻✽*]\s+\S[^(]*…\s*\(" + TIME + r"\b")
CODEX_STATUS = re.compile(r"^\s*•\s+(.+?)\s+\(" + TIME + r"\s*•\s*esc to interrupt\)", re.IGNORECASE)
CODEX_ACTION = re.compile(r"^(Ran|Edited|Updated|Added|Deleted|Applied|Searched|Called|Read|Listed)\b\s*(.*)$")
CODEX_DETAIL = re.compile(r"^\s*└\s+(Read|List|Search|Ran|Edit|Glob)\b\s*(.*)$")

SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:pass(?:word)?|secret|token|api[_-]?key|credential|cookie)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
FLAG_SECRET = re.compile(r"(?i)(--?(?:password|token|secret|api-key|auth)[= ])\S+")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
# sshpass -p SECRET / -pSECRET (mysql and friends; also hides a harmless -p8080, which is fine).
SSHPASS = re.compile(r"(?i)\b(sshpass\s+-p)\s*\S+")
ATTACHED_P = re.compile(r"(?<=\s)(-p)(?=\S)\S+")
# curl -u user:pass, --user user:pass, -U user:pass: keep the user, hide the password.
USER_PASS = re.compile(r"(?i)((?:^|\s)(?:-u|-U|--user|--proxy-user)(?:\s+|=))([^\s:]+):\S+")

VERBS = {
    "ran": "Running", "edited": "Editing", "updated": "Editing", "added": "Adding", "deleted": "Deleting",
    "applied": "Applying", "searched": "Searching", "called": "Using", "read": "Reading", "listed": "Listing",
    "list": "Listing", "search": "Searching", "edit": "Editing", "glob": "Finding",
}
# Codex's default status word says no more than "Thinking" does.
CODEX_PLAIN = {"working", "thinking"}


def redact(text: str) -> str:
    text = URL_USERINFO.sub(r"\1…@", text)
    text = BEARER.sub(r"\1 …", text)
    text = FLAG_SECRET.sub(r"\1…", text)
    text = SSHPASS.sub(r"\1 …", text)
    text = USER_PASS.sub(r"\1\2:…", text)
    text = ATTACHED_P.sub(r"\1…", text)
    text = SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}…", text)
    return LONG_TOKEN.sub("…", text)


def tidy(text: str) -> str:
    text = redact(re.sub(r"\s+", " ", text).strip().strip("`*_").strip())
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1].rstrip() + "…"


def seconds(took: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)([hms])", took):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def with_time(text: str, took: str) -> tuple[str, int]:
    return tidy(text), seconds(took)


def _claude(lines: list[str], at: int, took: str) -> tuple[str, int]:
    for line in reversed(lines[:at]):
        stripped = line.strip()
        if not stripped or stripped.startswith(("⎿", "❯", "─")):
            continue
        if stripped.startswith(("●", "⏺")):
            return with_time(stripped[1:], took)
        if line.startswith("  ") and stripped.endswith("…"):
            return with_time(stripped, took)
    return with_time("Thinking", took)


def _codex(lines: list[str], at: int, heading: str, took: str) -> tuple[str, int]:
    if heading.lower().rstrip("…. ") not in CODEX_PLAIN:
        return with_time(heading, took)
    detail: str | None = None
    for line in reversed(lines[:at]):
        stripped = line.strip()
        found = CODEX_DETAIL.match(line)
        if found and detail is None:
            detail = f"{VERBS.get(found.group(1).lower(), found.group(1))} {found.group(2)}"
            continue
        if not stripped.startswith("•"):
            continue
        said = stripped[1:].strip()
        if said == "Explored":
            return with_time(detail or "Exploring the code", took)
        action = CODEX_ACTION.match(said)
        if action:
            return with_time(f"{VERBS.get(action.group(1).lower(), action.group(1))} {action.group(2)}", took)
        return with_time(said, took)
    return with_time("Thinking", took)


def summary(screen: str) -> tuple[str, int] | None:
    """(what it is doing, seconds into the turn), or None when the screen shows no turn in progress.

    The seconds travel separately so the gateway sends an event only when the words change; OpenBot
    counts the time itself."""
    lines = [ANSI.sub("", line).rstrip() for line in screen.splitlines()]
    for index in range(len(lines) - 1, -1, -1):
        codex = CODEX_STATUS.match(lines[index])
        if codex:
            return _codex(lines, index, codex.group(1), codex.group(2))
        claude = CLAUDE_SPINNER.match(lines[index])
        if claude:
            return _claude(lines, index, claude.group(1))
    return None


def summary_line(screen: str) -> str | None:
    """The line as a person reads it: "Reading note.txt · 8s"."""
    found = summary(screen)
    if found is None:
        return None
    text, took = found
    minutes, secs = divmod(took, 60)
    hours, minutes = divmod(minutes, 60)
    clock = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "") + f"{secs}s"
    return f"{text} · {clock}"
