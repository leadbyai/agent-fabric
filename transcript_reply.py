"""A Claude Code agent's reply to one gateway request, read from that agent's own session transcript.

The gateway's first choice is the reply the agent marked on its screen (OPENBOT_BEGIN/END). That can
fail even when the agent answered correctly:
- the screen read loses lines after the pane is resized (a client of another size attached);
- the agent used another request's marker, or none at all;
- a question was answered on the agent's screen mid-turn, and the reply came without markers.

Claude Code writes every turn to `<config dir>/projects/<cwd as a name>/<session id>.jsonl`. This
module finds exactly that file for the pane's own process, never by searching: the config dir comes
from the process's environment, and the session id from Claude Code's own `sessions/<pid>.json`
record (or the id Herdr reports for the pane). In that file it finds the person's entry carrying this
request's id (`[OpenBot pilot request <id>]`) and returns the last assistant text of that turn, but
only once the turn has ended (a turn-end record follows it, or the caller saw the pane idle).

Codex panes use another format and are not read here; the caller keeps its current behaviour.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# The most of a transcript read from its end. A turn far larger than this is not looked for.
TAIL_BYTES = 16 * 1024 * 1024
# How far into a person's entry the request tag may start (after a client's short envelope).
PROMPT_HEAD = 300
# The same bound a marked reply has today (the screen read returns at most 200 lines), plus a byte cap.
MAX_REPLY_LINES = 200
MAX_REPLY_CHARS = 64 * 1024
CUT_NOTE = "\n\n(The rest of this reply is on the agent's own screen in Herdr.)"
MARKER_LINE = re.compile(r"^\s*OPENBOT_(?:BEGIN|END)_[A-Za-z0-9]+\s*$")
PROJECT_NAME = re.compile(r"[^A-Za-z0-9]")


class Unavailable(Exception):
    """The transcript cannot be found or read; the caller falls back to its current behaviour."""


def project_dir_name(cwd: str) -> str:
    """Claude Code's name for a working directory under `projects/`: every other character a dash."""
    return PROJECT_NAME.sub("-", cwd)


def _process_env(pid: int, proc: Path) -> dict[str, str]:
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError as exc:
        raise Unavailable("process environment unreadable") from exc
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        name, _, value = item.partition(b"=")
        if name in (b"CLAUDE_CONFIG_DIR", b"HOME"):
            env[name.decode()] = value.decode("utf-8", "replace")
    return env


def claude_process(process_info: dict) -> tuple[int, str] | None:
    """(pid, cwd) of the pane's foreground Claude Code process, or None when it is not Claude."""
    for process in process_info.get("foreground_processes") or []:
        if not isinstance(process, dict):
            continue
        argv = process.get("argv") or []
        name = os.path.basename(str(argv[0] if argv else process.get("name") or ""))
        if name == "claude" and isinstance(process.get("pid"), int) and isinstance(process.get("cwd"), str):
            return process["pid"], process["cwd"]
    return None


def transcript_path(process_info: dict, herdr_session_id: str | None = None,
                    proc: Path = Path("/proc")) -> Path:
    """The pane's own transcript file. Raises Unavailable for a non-Claude pane or anything missing."""
    found = claude_process(process_info)
    if found is None:
        raise Unavailable("not a Claude Code pane")
    pid, cwd = found
    env = _process_env(pid, proc)
    config = env.get("CLAUDE_CONFIG_DIR") or (env["HOME"] + "/.claude" if env.get("HOME") else "")
    if not config or not os.path.isabs(config):
        raise Unavailable("no config dir")
    config_dir = Path(config)
    session_id = herdr_session_id
    record_cwd = cwd
    record = config_dir / "sessions" / f"{pid}.json"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        if data.get("pid") == pid and isinstance(data.get("sessionId"), str):
            session_id = session_id or data["sessionId"]
            if data["sessionId"] != session_id:
                raise Unavailable("session ids disagree")
            if isinstance(data.get("cwd"), str):
                record_cwd = data["cwd"]
    except (OSError, ValueError):
        pass
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        raise Unavailable("no session id")
    path = config_dir / "projects" / project_dir_name(record_cwd) / f"{session_id}.jsonl"
    if path.is_symlink() or not path.is_file():
        raise Unavailable("transcript missing")
    return path


def _entries(path: Path) -> list[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError as exc:
        raise Unavailable("transcript unreadable") from exc
    if size > TAIL_BYTES:
        data = data.split(b"\n", 1)[-1]  # a partial first line
    entries = []
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _is_person(entry: dict) -> bool:
    """An entry that starts a turn: not a tool result, an image note or a side chain."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and content and all(
            isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
        return False
    return True


def _clean(text: str, marker_id: str) -> str:
    pair = re.search(rf"OPENBOT_BEGIN_{re.escape(marker_id)}\s*(.*?)\s*OPENBOT_END_{re.escape(marker_id)}",
                     text, flags=re.DOTALL)
    if pair:
        text = pair.group(1)
    return "\n".join(line for line in text.splitlines() if not MARKER_LINE.match(line)).strip()


def reply_for(path: Path, marker_id: str, pane_idle: bool) -> str | None:
    """The agent's reply to request `marker_id`, or None while there is none yet.

    None also when the turn has not ended: no turn-end record follows the request and the pane was not
    seen idle. Raises Unavailable when the file cannot be read."""
    tag = f"[OpenBot pilot request {marker_id}]"
    entries = _entries(path)
    # The request itself: the first person's entry that opens with this request's tag (the gateway
    # puts it on the prompt's first line; a client may wrap the prompt in a short envelope). A later
    # message that merely quotes the id is never taken for it.
    start = None
    for index, entry in enumerate(entries):
        if _is_person(entry) and tag in _text((entry.get("message") or {}).get("content"))[:PROMPT_HEAD]:
            start = index
            break
    if start is None:
        return None
    # Only the turn this request began: it ends at the first turn-end record or the next person's
    # entry. A later turn (a background task finishing, say) is never taken for the reply.
    last_text, ended = None, False
    for entry in entries[start + 1:]:
        if _is_person(entry) or (entry.get("type") == "system" and entry.get("subtype") == "turn_duration"):
            ended = True
            break
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            said = _text((entry.get("message") or {}).get("content")).strip()
            if said:
                last_text = said
    if not (ended or pane_idle) or last_text is None:
        return None
    reply = _clean(last_text, marker_id)
    if not reply:
        return None
    lines = reply.splitlines()
    if len(lines) > MAX_REPLY_LINES or len(reply) > MAX_REPLY_CHARS:
        reply = "\n".join(lines[:MAX_REPLY_LINES])[:MAX_REPLY_CHARS].rstrip() + CUT_NOTE
    return reply
