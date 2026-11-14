"""Safe, read-only Claude Code transcript classification for Agent Fabric.

This module DOES NOT watch files, send HTTP requests, or write OpenBot messages.
An external watcher must explicitly opt a Bot in, resolve its native session,
and acknowledge successfully imported events before advancing its cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Iterable, Iterator


MAX_MESSAGE_BYTES = 4096

# Deliberately conservative: exclude obvious credential shapes even when they
# appear inside otherwise allowed human/assistant text. This is NOT a DLP system.
_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+={0,2}", re.IGNORECASE),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{10,}|"
        r"github_pat_[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
        r"AKIA[0-9A-Z]{16})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:[A-Za-z0-9_-]{8,}\.){2}[A-Za-z0-9_-]{8,}\b"
    ),  # JWT-like
    re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY)"
        r"\s*=\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\r\n]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:[a-f0-9]{40,}|[A-Za-z0-9+/]{48,}={0,2})\b"),
)

_OPENBOT_MARKER = re.compile(r"OPENBOT_BEGIN_[A-Z0-9]{8,40}", re.IGNORECASE)
_SAFE_TOOL_LABELS = {
    "Bash": "Ran shell command",
    "Read": "Read file",
    "Write": "Wrote file",
    "Edit": "Edited file",
    "Grep": "Searched files",
    "Glob": "Listed files",
    "Task": "Started delegated task",
    "Agent": "Started delegated task",
    "WebFetch": "Retrieved web page",
    "WebSearch": "Searched web",
}


@dataclass(frozen=True)
class ImportEvent:
    id: str
    source_uuid: str
    session_id: str
    kind: str
    sender: str
    timestamp: str
    content: str


@dataclass
class TranscriptState:
    """State must be persisted along with the watcher cursor after an ACK.

    True means the current Claude turn originated in OpenBot. Its response is
    already visible there and therefore must not be imported a second time.
    """

    suppress_openbot_turn: bool = False
    parent_guard: dict[str, bool] = field(default_factory=dict)


def redact_content(text: str, *, max_bytes: int = MAX_MESSAGE_BYTES) -> str:
    """Remove common credentials and bound the content sent to hosted storage."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    sanitized = text.replace("\x00", "")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[redacted]", sanitized)
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return sanitized
    suffix = "\n(truncated)"
    remaining = max_bytes - len(suffix.encode("utf-8"))
    if remaining <= 0:
        return ""
    return encoded[:remaining].decode("utf-8", errors="ignore") + suffix


def _message_id(session_id: str, source_uuid: str, part: str) -> str:
    key = f"claude|{session_id}|{source_uuid}|{part}".encode("utf-8")
    return "afx-" + hashlib.sha256(key).hexdigest()[:32]


def _user_text(record: dict[str, Any]) -> str | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def classify_claude_records(
    records: Iterable[dict[str, Any]],
    *,
    session_id: str,
    state: TranscriptState,
    include_tool_summaries: bool = True,
) -> Iterator[ImportEvent]:
    """Classify a consecutive JSONL slice without touching agent sessions.

    No backfill, file access, HTTP transmission or cursor advancement occurs
    here. The caller is responsible for the per-Bot opt-in and robust source
    session mapping. Records lacking stable IDs or timestamps fail closed.
    """
    if not session_id:
        raise ValueError("session_id is required")

    # Parent links identify interleaved replies within a read. The cursor also
    # carries the sequential guard for Claude records with no local parent.
    openbot_ancestors = state.parent_guard
    for record in records:
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        kind = record.get("type")
        uuid = record.get("uuid")
        timestamp = record.get("timestamp")
        if not isinstance(uuid, str) or not uuid or not isinstance(timestamp, str) or not timestamp:
            continue
        native_id = record.get("sessionId")
        if native_id is not None and native_id != session_id:
            continue

        parent = record.get("parentUuid")
        suppressed = state.suppress_openbot_turn
        if isinstance(parent, str):
            suppressed = openbot_ancestors.get(parent, suppressed)
        openbot_ancestors[uuid] = suppressed
        if len(openbot_ancestors) > 512:
            del openbot_ancestors[next(iter(openbot_ancestors))]

        if kind == "user":
            content = _user_text(record)
            if content is None:
                continue  # tool_result and attachments are never mirrored

            origin = record.get("origin")
            source = record.get("promptSource")
            if isinstance(origin, dict) and origin.get("kind") == "peer":
                yield ImportEvent(
                    id=_message_id(session_id, uuid, "peer"),
                    source_uuid=uuid,
                    session_id=session_id,
                    kind="peer",
                    sender="Peer agent",
                    timestamp=timestamp,
                    content=redact_content(content).strip() or "Received agent message",
                )
                continue
            if not isinstance(origin, dict) or origin.get("kind") != "human":
                continue
            if record.get("isMeta") or source not in ("typed", "queued"):
                continue

            if _OPENBOT_MARKER.search(content) or "[OpenBot pilot request " in content:
                state.suppress_openbot_turn = True
                openbot_ancestors[uuid] = True
                continue

            state.suppress_openbot_turn = False
            openbot_ancestors[uuid] = False
            cleaned = redact_content(content).strip()
            if cleaned:
                yield ImportEvent(
                    id=_message_id(session_id, uuid, "human"),
                    source_uuid=uuid,
                    session_id=session_id,
                    kind="human",
                    sender="Derek (terminal)",
                    timestamp=timestamp,
                    content=cleaned,
                )

        elif kind == "assistant" and not suppressed:
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_kind = block.get("type")
                if block_kind == "text" and isinstance(block.get("text"), str):
                    content = redact_content(block["text"]).strip()
                    event_kind = "assistant"
                elif block_kind == "tool_use" and include_tool_summaries:
                    tool_name = block.get("name")
                    content = _SAFE_TOOL_LABELS.get(tool_name, "Used tool")
                    if tool_name == "SendMessage":
                        content = "Sent a message to another agent"
                        event_kind = "peer"
                    else:
                        event_kind = "tool_summary"
                else:
                    continue  # thinking, raw tool input, other sensitive blocks
                if content:
                    yield ImportEvent(
                        id=_message_id(session_id, uuid, str(index)),
                        source_uuid=uuid,
                        session_id=session_id,
                        kind=event_kind,
                        sender="Agent",
                        timestamp=timestamp,
                        content=content,
                    )
