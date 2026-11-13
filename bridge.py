#!/usr/bin/env python3
"""Authenticated AG-UI gateway to existing Herdr-managed coding agents.

Version 0.7.1 records every accepted request durably as `queued` before it
waits, so a gateway restart or expired wait leaves a truthful `failed` record
instead of a silently lost message, and strips inline data-URI images from the
AG-UI history before parsing so screenshot-heavy chats stay under the limit.
Version 0.7.0 added long instructions and bidirectional image handoff.
Python standard library only. It does not launch, stop, or resume Herdr agents.
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
import hmac
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from request_ledger import RequestLedger


TARGET = os.environ.get("HERDR_AGENT_TARGET", "").strip()
MACHINE = os.environ.get("HERDR_MACHINE", "").strip()
SESSION = os.environ.get("HERDR_SESSION", "").strip()
SESSIONS = tuple(dict.fromkeys(s.strip() for s in
    os.environ.get("HERDR_SESSIONS", SESSION).split(",") if s.strip()))
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH", "").strip()
HERDR_BIN = os.environ.get("HERDR_BIN", "herdr")
TOKEN_FILE = os.environ.get("OPENBOT_BRIDGE_TOKEN_FILE", "").strip()
TOKEN = os.environ.get("OPENBOT_BRIDGE_TOKEN", "")
if TOKEN_FILE and not TOKEN:
    # systemd LoadCredential provides this path without leaking the bearer
    # token into process arguments or unit files.
    TOKEN = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
HOST = os.environ.get("OPENBOT_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPENBOT_BRIDGE_PORT", "8765"))
TIMEOUT_MS = int(os.environ.get("OPENBOT_BRIDGE_TIMEOUT_MS", "120000"))
BUSY_WAIT_SECONDS = int(os.environ.get("OPENBOT_BRIDGE_BUSY_WAIT_SECONDS", "0"))
RECOVERY_SECONDS = int(os.environ.get("OPENBOT_BRIDGE_RECOVERY_SECONDS", "0"))
LEDGER_PATH = os.environ.get("OPENBOT_BRIDGE_LEDGER_PATH", "").strip()
LEDGER: RequestLedger | None = None
HEARTBEAT_SECONDS = 15.0
STATE_POLL_SECONDS = 2.0
SHUTTING_DOWN = threading.Event()
SHUTDOWN_GRACE_SECONDS = 5.0
VERSION = "0.7.1"
WORKDIR = os.environ.get("OPENBOT_BRIDGE_WORKDIR", "").strip()
ALLOWED_ROOTS = tuple(s.strip() for s in
    os.environ.get("OPENBOT_BRIDGE_ALLOWED_ROOTS", "").split(":") if s.strip())
AGENT_LOCKS: dict[tuple[str, str], threading.Lock] = {}
LOCKS_GUARD = threading.Lock()
WAIT_QUEUES: dict[tuple[str, str], deque[object]] = {}
WAIT_QUEUES_GUARD = threading.Lock()
GLOBAL_SLOTS = threading.BoundedSemaphore(8)
WAITING_SLOTS = threading.BoundedSemaphore(64)
# OpenBot may inline up to 32 MiB of recent attachments; base64 plus history
# makes a full AG-UI request larger than those original bytes.
MAX_BODY = 64 * 1024 * 1024
# Bytes on the wire before history data-URI images are stripped. Only the
# stripped body (<= MAX_BODY) is ever held in memory.
MAX_RAW_BODY = 512 * 1024 * 1024
# Inline base64 images shorter than this are left untouched.
DATA_URI_STRIP_MIN = 4096
# The Herdr CLI prompt is an OS argument. Longer text is saved as a private
# file for the agent to read, rather than being rejected or truncated.
MAX_PROMPT_BYTES = 64 * 1024
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_TOTAL_FILES = 24 * 1024 * 1024
MAX_FILES = 4
ARTIFACT_DIR = os.environ.get("OPENBOT_BRIDGE_ARTIFACT_DIR", "").strip()
PUBLIC_ORIGIN = os.environ.get("OPENBOT_BRIDGE_PUBLIC_ORIGIN", "").strip().rstrip("/")
ARTIFACT_MODE = os.environ.get("OPENBOT_BRIDGE_ARTIFACT_MODE", "inline").strip()
ARTIFACT_TTL_SECONDS = int(os.environ.get("OPENBOT_BRIDGE_ARTIFACT_TTL_SECONDS", "2592000"))
ARTIFACT_STORAGE_BYTES = int(os.environ.get("OPENBOT_BRIDGE_ARTIFACT_STORAGE_BYTES", str(2 * 1024 ** 3)))
MAX_OUTBOUND_IMAGES = 4
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INLINE_IMAGES_TOTAL = 4 * 1024 * 1024
# Image delivery is determined by validated files in the private per-request
# outbox, never by the model's whitespace or Markdown formatting. Strip any
# legacy declaration after finding actual images in the outbox.
OUTBOUND_IMAGE_RE = re.compile(r"(?m)^[ \t]*OPENBOT_IMAGE:[^\r\n]*$")
OUTBOUND_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ARTIFACT_RE = re.compile(r"/artifacts/([A-Za-z0-9_-]{32,64})\.(png|jpg|webp)\Z")


class PayloadTooLarge(ValueError):
    """A safely bounded but too-large request, reported as HTTP 413."""


class DataUriStripper:
    """Streaming filter: drop large `data:image/...;base64,` payloads from JSON bytes.

    OpenBot resends the whole conversation on every AG-UI run. Screenshots this
    gateway returned inline (Markdown data-URI images in assistant messages)
    would otherwise accumulate until every later turn in that chat hit the body
    limit. The gateway only acts on the latest user turn, whose real attachments
    arrive as AG-UI DataSource parts (bare base64, no `data:` prefix) and are not
    touched. A long run is replaced with `OPENBOT_OMITTED`; JSON stays valid
    because base64 characters and the JSON escape `\\/` never contain quotes.
    Works across arbitrary chunk boundaries.
    """
    PREFIX = b"data:image"
    HEADER = re.compile(rb"data:image(?:/|\\/)[A-Za-z0-9.+-]{1,40};base64,")
    PARTIAL_HEADER = re.compile(rb"data:image(?:\\(?:/[A-Za-z0-9.+-]{0,40}(?:;(?:b(?:a(?:s(?:e(?:6(?:4)?)?)?)?)?)?)?)?|/[A-Za-z0-9.+-]{0,40}(?:;(?:b(?:a(?:s(?:e(?:6(?:4)?)?)?)?)?)?)?)?")
    B64 = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    PLACEHOLDER = b"OPENBOT_OMITTED"

    def __init__(self, min_run: int = DATA_URI_STRIP_MIN):
        self.min_run = min_run
        self.buffer = b""
        self.in_run = False
        self.kept = bytearray()   # first bytes of the current run, until min_run
        self.dropping = False
        self.pending_escape = False
        self.stripped = 0

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        data = self.buffer + data
        self.buffer = b""
        i = 0
        n = len(data)
        while i < n:
            if self.in_run:
                if self.pending_escape:
                    self.pending_escape = False
                    if data[i] == 0x2F:  # JSON-escaped "/" inside base64
                        self._run_byte(b"\\/", out)
                        i += 1
                        continue
                    self._end_run(out)
                    out += b"\\"
                    continue
                byte = data[i]
                if byte in self.B64:
                    self._run_byte(data[i:i + 1], out)
                    i += 1
                    continue
                if byte == 0x5C:  # backslash: may start "\/"
                    self.pending_escape = True
                    i += 1
                    continue
                self._end_run(out)
                continue
            j = data.find(self.PREFIX, i)
            if j < 0:
                # Keep a tail that could be the start of a split prefix.
                keep = max(i, n - (len(self.PREFIX) - 1))
                out += data[i:keep]
                self.buffer = data[keep:]
                return bytes(out)
            out += data[i:j]
            match = self.HEADER.match(data, j)
            if match is None:
                if self.PARTIAL_HEADER.fullmatch(data, j):
                    # Header may be split across chunks; wait for more bytes.
                    self.buffer = data[j:]
                    return bytes(out)
                out += data[j:j + 1]
                i = j + 1
                continue
            out += match.group(0)
            i = match.end()
            self.in_run = True
            self.kept = bytearray()
            self.dropping = False
        return bytes(out)

    def _run_byte(self, piece: bytes, out: bytearray) -> None:
        if self.dropping:
            return
        self.kept += piece
        if len(self.kept) >= self.min_run:
            self.dropping = True
            self.stripped += 1
            self.kept = bytearray()
            out += self.PLACEHOLDER

    def _end_run(self, out: bytearray) -> None:
        if not self.dropping:
            out += self.kept
        self.kept = bytearray()
        self.in_run = False
        self.dropping = False

    def finish(self) -> bytes:
        out = bytearray()
        if self.in_run:
            if self.pending_escape:
                self.pending_escape = False
                self._end_run(out)
                out += b"\\"
            else:
                self._end_run(out)
        out += self.buffer
        self.buffer = b""
        return bytes(out)
OPENBOT_TEXT_FILE = re.compile(r'\AAttached file "([^"\r\n]{1,160})":\r?\n\r?\n([\s\S]*)\Z')
TEXT_MIMES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".log": "text/plain",
}
PANE_RE = re.compile(r"w[0-9A-Za-z]+:p[0-9A-Za-z]+\Z")
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
AGENT_ROUTE = re.compile(r"/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/agents/(w[0-9A-Za-z]+:p[0-9A-Za-z]+)/ag-ui\Z")


def command(*args: str, session: str | None = None, wait_seconds: float = 15) -> str:
    cmd = [HERDR_BIN]
    if MACHINE:
        cmd += ["--machine", MACHINE]
    elif session and not SOCKET_PATH:
        cmd += ["--session", session]
    cmd += list(args)
    env = os.environ.copy()
    if session and not MACHINE:
        env["HERDR_SESSION"] = session
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=wait_seconds,
        env=env
    )
    if result.returncode != 0:
        # CLI stderr may contain user data; do not put it in HTTP responses.
        reason = (result.stderr or "").strip().splitlines()
        brief = reason[0][:160] if reason else "no details"
        raise RuntimeError(f"Herdr command failed (exit={result.returncode}): {brief}")
    return result.stdout


def agent_record(session: str, target: str) -> dict:
    output = command("agent", "get", target, session=session)
    data = json.loads(output)
    agent = data.get("result", {}).get("agent", {})
    if not isinstance(agent, dict) or not agent:
        raise RuntimeError("Herdr did not return an agent record")
    return agent


def normalized_state(agent: dict) -> str:
    status = agent.get("agent_status") or agent.get("status")
    if isinstance(status, dict):
        status = status.get("state") or status.get("status")
    if not isinstance(status, str):
        raise RuntimeError("Herdr returned no recognizable agent status; inspect agent get")
    return status.lower()


def target_state() -> str:
    """Backward-compatible single-target status used by /agent."""
    return normalized_state(agent_record(SESSION, TARGET))


def list_agents(session: str) -> list[dict]:
    payload = json.loads(command("agent", "list", session=session))
    agents = payload.get("result", {}).get("agents", [])
    if not isinstance(agents, list):
        raise RuntimeError("Invalid Herdr agent list")
    return [a for a in agents if isinstance(a, dict) and
            PANE_RE.fullmatch(str(a.get("pane_id", "")))]


def public_agent_info(session: str, agent: dict) -> dict:
    pane = agent["pane_id"]
    return {"session": session, "pane_id": pane,
            "name": agent.get("name") or "",
            "title": agent.get("terminal_title_stripped") or agent.get("name") or pane,
            "status": normalized_state(agent),
            "cwd": agent.get("foreground_cwd") or agent.get("cwd") or "",
            "ag_ui_path": f"/sessions/{session}/agents/{pane}/ag-ui"}


def live_target(session: str, pane_id: str) -> dict:
    """Allow only currently recognized agents in an explicitly allowed session."""
    if session not in SESSIONS or not PANE_RE.fullmatch(pane_id):
        raise LookupError("Unknown agent route")
    for agent in list_agents(session):
        if agent["pane_id"] == pane_id:
            return agent
    raise LookupError("No live Herdr agent in this pane")


def target_lock(session: str, pane_id: str) -> threading.Lock:
    key = (session, pane_id)
    with LOCKS_GUARD:
        if key not in AGENT_LOCKS:
            AGENT_LOCKS[key] = threading.Lock()
        return AGENT_LOCKS[key]


def waiting_queue(session: str, pane_id: str) -> deque[object]:
    key = (session, pane_id)
    with WAIT_QUEUES_GUARD:
        if key not in WAIT_QUEUES:
            WAIT_QUEUES[key] = deque()
        return WAIT_QUEUES[key]


def last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            text = ""
        if text.strip():
            return text.strip()
    raise ValueError("No text user message was provided")


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:96].strip(".")
    return name or "attachment.bin"


def openbot_text_attachment(text: str) -> tuple[str, str, bytes] | None:
    """Rebuild a *derived UTF-8 copy* of OpenBot's textual file rendering.

    OpenBot 1.0.2 was observed to send a second text part of exactly this form:
    `Attached file "test.csv":\n\nname,amount\nSample,42f\n` (50 chars).
    This is NOT an original binary upload or a byte-for-byte representation.
    """
    if not text.startswith('Attached file "'):
        return None
    match = OPENBOT_TEXT_FILE.fullmatch(text)
    if match is None:
        raise ValueError("Unsupported OpenBot text attachment wrapper")
    name = safe_filename(match.group(1))
    mime = TEXT_MIMES.get(Path(name).suffix.lower())
    if mime is None:
        raise ValueError("OpenBot rendered an unsupported file type as text; use a binary attachment handoff")
    content = match.group(2)
    if "\x00" in content:
        raise ValueError("OpenBot text attachment contains a null character")
    data = content.encode("utf-8")
    if not data or len(data) > MAX_FILE_SIZE:
        raise ValueError("OpenBot text attachment is empty or exceeds the 8 MiB pilot limit")
    return name, mime + "; source=openbot-derived-text", data


def parse_user_message(body: dict) -> tuple[str, list[tuple[str, str, bytes]]]:
    """Parse only the latest user turn, preserving media and rejecting unknown sources.

    Inline AG-UI 1.0 DataSource media is the only supported attachment transport.
    Never silently omit a file or request the bridge to fetch arbitrary URLs.
    """
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ValueError("AG-UI messages must be an array")
    user = next((msg for msg in reversed(body["messages"])
                 if isinstance(msg, dict) and msg.get("role") == "user"), None)
    if user is None:
        raise ValueError("No user message was provided")
    content = user.get("content", "")
    if isinstance(content, str):
        text = content.strip()
        if not text:
            raise ValueError("No text user message was provided")
        return text, []
    if not isinstance(content, list):
        raise ValueError("Unsupported AG-UI message content")

    texts: list[str] = []
    attachments: list[tuple[str, str, bytes]] = []
    size_total = 0
    for part_index, part in enumerate(content):
        if not isinstance(part, dict):
            raise ValueError("Malformed AG-UI content part")
        kind = part.get("type")
        if kind == "text":
            if not isinstance(part.get("text"), str):
                raise ValueError("Malformed text content part")
            derived = openbot_text_attachment(part["text"]) if part_index > 0 else None
            if derived is not None:
                if len(attachments) >= MAX_FILES:
                    raise ValueError(f"At most {MAX_FILES} files per message are supported")
                size_total += len(derived[2])
                if size_total > MAX_TOTAL_FILES:
                    raise ValueError("Total attachment bytes exceed the 24 MiB limit")
                attachments.append(derived)
            else:
                texts.append(part["text"])
            continue
        if kind not in ("image", "audio", "video", "document"):
            raise ValueError(f"Unsupported AG-UI content part: {str(kind)[:40]}")
        if len(attachments) >= MAX_FILES:
            raise ValueError(f"At most {MAX_FILES} files per message are supported")
        source = part.get("source")
        if not isinstance(source, dict) or source.get("type") != "data":
            raise ValueError("File references/URLs are not supported yet; send inline AG-UI data")
        encoded = source.get("value")
        if not isinstance(encoded, str) or len(encoded) > 4 * MAX_FILE_SIZE // 3 + 8:
            raise ValueError("Attachment exceeds the 8 MiB pilot limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid attachment base64") from exc
        if not data or len(data) > MAX_FILE_SIZE:
            raise ValueError("Attachment exceeds the 8 MiB pilot limit or is empty")
        size_total += len(data)
        if size_total > MAX_TOTAL_FILES:
            raise ValueError("Total attachment bytes exceed the 24 MiB limit")
        mime = source.get("mimeType", "application/octet-stream")
        if not isinstance(mime, str) or len(mime) > 120 or "\n" in mime:
            raise ValueError("Invalid attachment MIME type")
        metadata = part.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        name = metadata.get("filename") or metadata.get("fileName") or "attachment.bin"
        if not isinstance(name, str):
            name = "attachment.bin"
        name = safe_filename(name)
        attachments.append((name, mime, data))
    text = "\n".join(texts).strip()
    if not text and not attachments:
        raise ValueError("No text or attachment was provided")
    return text or "Please examine the attached files.", attachments


def redacted_request_shape(body: object) -> dict:
    """Report field *structure* for troubleshooting, never content or secrets.

    OpenBot may store composer files in its own backend rather than placing
    inline AG-UI parts in the request. This lets us identify the handoff path
    without writing raw messages, data URLs, bearer tokens, or filenames to logs.
    """
    if not isinstance(body, dict):
        return {"body": "non-object"}

    def keys(obj: object) -> list[str]:
        if not isinstance(obj, dict):
            return []
        return sorted(str(key)[:40] for key in obj)[:24]

    def field_kind(obj: object) -> dict:
        if isinstance(obj, dict):
            return {"kind": "object", "keys": keys(obj)}
        if isinstance(obj, list):
            return {"kind": "array", "items": len(obj)}
        if isinstance(obj, str):
            return {"kind": "string", "characters": len(obj)}
        return {"kind": type(obj).__name__}

    messages = body.get("messages")
    user = next((msg for msg in reversed(messages)
                 if isinstance(msg, dict) and msg.get("role") == "user"), None) \
        if isinstance(messages, list) else None
    result = {"body_keys": keys(body),
              "message_count": len(messages) if isinstance(messages, list) else 0,
              "latest_user_keys": keys(user)}
    if not isinstance(user, dict):
        return result

    content = user.get("content")
    result["content"] = field_kind(content)
    if isinstance(content, list):
        parts = []
        for part in content[:12]:
            if not isinstance(part, dict):
                parts.append({"kind": "non-object"})
                continue
            kind = part.get("type")
            source = part.get("source")
            info = {"part_keys": keys(part),
                    "part_type": kind if kind in ("text", "image", "audio", "video", "document") else "other",
                    "source": field_kind(source),
                    "metadata": field_kind(part.get("metadata"))}
            if isinstance(source, dict):
                source_kind = source.get("type")
                info["source_type"] = source_kind if source_kind in ("data", "url", "file") else "other"
                info["source_value"] = field_kind(source.get("value"))
            if isinstance(part.get("text"), str):
                info["text_characters"] = len(part["text"])
            parts.append(info)
        result["parts"] = parts
    for key in ("attachments", "files", "forwardedProps", "metadata", "context", "state"):
        if key in body:
            result["body_" + key] = field_kind(body[key])
        if key in user:
            result["user_" + key] = field_kind(user[key])
    return result


def agent_workdir(workdir: str, *, dynamic: bool = False) -> Path:
    """Resolve the agent's trusted root, never a path from a model or HTTP body."""
    if not workdir:
        raise ValueError("Agent has no known working directory for attachments")
    root = Path(workdir).expanduser()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("The configured agent working directory is unavailable")
    if dynamic:
        if not ALLOWED_ROOTS:
            raise ValueError("Set OPENBOT_BRIDGE_ALLOWED_ROOTS to enable per-agent uploads")
        canonical = root.resolve(strict=True)
        if not any(canonical.is_relative_to(Path(path).resolve(strict=True))
                   for path in ALLOWED_ROOTS):
            raise ValueError("Agent working directory is outside allowed upload roots")
    return root


def private_marker_directory(root: Path, name: str, marker_id: str) -> Path:
    parent = root / name
    if parent.is_symlink():
        raise ValueError(f"{name} cannot be a symlink")
    parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    marker_dir = parent / marker_id
    marker_dir.mkdir(mode=0o700)
    return marker_dir


def stage_attachments(attachments: list[tuple[str, str, bytes]],
                      marker_id: str, workdir: str, *, dynamic: bool = False) -> list[str]:
    """Write into the selected agent's workdir, never a supplied HTTP path."""
    if not attachments:
        return []
    root = agent_workdir(workdir, dynamic=dynamic)
    target = private_marker_directory(root, ".openbot-inbox", marker_id)
    written: list[Path] = []
    try:
        for i, (name, mime, data) in enumerate(attachments, 1):
            filename = f"{i}-{safe_filename(name)}"
            path = target / filename
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            written.append(path)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # Only paths and metadata go to the agent prompt, never file contents.
        return [f".openbot-inbox/{marker_id}/{i}-{safe_filename(name)} ({mime}; {len(data)} bytes)"
                for i, (name, mime, data) in enumerate(attachments, 1)]
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        target.rmdir()
        raise


def prepare_outbox(marker_id: str, workdir: str, *, dynamic: bool = False) -> Path:
    root = agent_workdir(workdir, dynamic=dynamic)
    return private_marker_directory(root, ".openbot-outbox", marker_id)


def image_file_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "jpg", "image/jpeg"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def persist_reply_images(reply: str, outbox: Path) -> str:
    """Automatically attach images placed in this request's private outbox.

    Default inline mode places image bytes within OpenBot's already-authenticated
    conversation (AG-UI AssistantMessage.content remains a string). Opt-in
    signed_url mode is available for deployments approving capability links.
    Legacy OPENBOT_IMAGE lines are removed when an actual image is available;
    arbitrary model-generated indentation cannot suppress delivery.
    """
    published: list[Path] = []
    previews: list[str] = []
    total_inline = 0
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        outbox_fd = os.open(outbox, directory_flags)
        if outbox.is_symlink() or outbox.parent.is_symlink():
            os.close(outbox_fd)
            raise RuntimeError("Screenshot directory is unsafe")
        # Both the directory enumeration and file opens use the same dir_fd,
        # preventing a swapped ancestor from redirecting the file reads.
        try:
            with os.scandir(outbox_fd) as entries:
                names = sorted(entry.name for entry in entries
                               if Path(entry.name).suffix.lower() in OUTBOUND_IMAGE_EXTENSIONS)
            if not names:
                return reply
            if len(names) > MAX_OUTBOUND_IMAGES:
                raise RuntimeError(f"Outbox contains more than {MAX_OUTBOUND_IMAGES} screenshots")
            if ARTIFACT_MODE == "signed_url" and (not ARTIFACT_DIR or not PUBLIC_ORIGIN):
                raise RuntimeError("Screenshot delivery is not configured on the Agent Fabric gateway")
            artifact_root: Path | None = None
            if ARTIFACT_MODE == "signed_url":
                artifact_root = Path(ARTIFACT_DIR)
                if not artifact_root.is_absolute() or artifact_root.is_symlink():
                    raise RuntimeError("Artifact directory is unsafe")
                artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(artifact_root, 0o700)
            for name in names:
                if (name in (".", "..") or ".." in name or name.startswith(".") or
                        len(name) > 255 or "\n" in name or "\r" in name):
                    raise RuntimeError("Unsafe screenshot filename")
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(name, flags, dir_fd=outbox_fd)
                with os.fdopen(fd, "rb") as handle:
                    metadata = os.fstat(handle.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or not (0 < metadata.st_size <= MAX_FILE_SIZE):
                        raise RuntimeError("Screenshot must be a regular file of at most 8 MiB")
                    data = handle.read(MAX_FILE_SIZE + 1)
                kind = image_file_type(data)
                if kind is None or len(data) > MAX_FILE_SIZE:
                    raise RuntimeError("Screenshot is not a supported PNG, JPEG, or WebP image")
                extension, mime = kind
                if ARTIFACT_MODE == "inline":
                    total_inline += len(data)
                    if (len(data) > MAX_INLINE_IMAGE_BYTES or
                            total_inline > MAX_INLINE_IMAGES_TOTAL):
                        raise RuntimeError(
                            "Screenshot is too large for private chat storage. "
                            "Compress it to at most 2 MiB per image, 4 MiB total."
                        )
                    url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                    previews.append(f"![Screenshot]({url})")
                    continue
                assert artifact_root is not None
                # Remove only bridge-generated images that have passed their
                # configured retention; never evict an unexpired screenshot.
                used = 0
                for existing in artifact_root.iterdir():
                    if not ARTIFACT_RE.fullmatch("/artifacts/" + existing.name) or existing.is_symlink():
                        continue
                    info = existing.stat()
                    if time.time() - info.st_mtime > ARTIFACT_TTL_SECONDS:
                        existing.unlink(missing_ok=True)
                    elif stat.S_ISREG(info.st_mode):
                        used += info.st_size
                if used + len(data) > ARTIFACT_STORAGE_BYTES:
                    raise RuntimeError("Screenshot storage is full; no unexpired images were removed")
                secret = secrets.token_urlsafe(32)
                destination = artifact_root / f"{secret}.{extension}"
                write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    write_flags |= os.O_NOFOLLOW
                fd = os.open(destination, write_flags, 0o600)
                published.append(destination)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                url = f"{PUBLIC_ORIGIN}/artifacts/{destination.name}"
                previews.append(f"![Screenshot]({url})\n\n[Open screenshot]({url})")
        finally:
            os.close(outbox_fd)
        prose = OUTBOUND_IMAGE_RE.sub("", reply).strip()
        return (prose + "\n\n" if prose else "") + "\n\n".join(previews)
    except (OSError, ValueError) as exc:
        for path in published:
            path.unlink(missing_ok=True)
        raise RuntimeError("Could not safely deliver the agent screenshot") from exc
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise


def extract_reply(transcript: str, marker_id: str) -> str:
    # The prompt itself contains the sentinels. Take the LAST complete pair.
    begin = f"OPENBOT_BEGIN_{marker_id}"
    end = f"OPENBOT_END_{marker_id}"
    parts = list(re.finditer(re.escape(begin) + r"\s*(.*?)\s*" + re.escape(end),
                             transcript, flags=re.DOTALL))
    if not parts:
        raise RuntimeError(
            "Could not correlate the agent's answer with the request. "
            "Check the Herdr pane; the prompt may already have been submitted. "
            "Do not retry automatically."
        )
    text = parts[-1].group(1).strip()
    if (not text or text == "<your response>" or
            "OPENBOT_BEGIN_" in text or "OPENBOT_END_" in text):
        raise RuntimeError("Agent reply could not be safely extracted; inspect Herdr")
    return text


def normalize_prose_wrapping(text: str) -> str:
    """Undo apparent terminal-style wrapping in ordinary prose only.

    Herdr may return literal newlines in what is visually one paragraph. OpenBot
    then wraps those already-broken lines again. Only reflow sufficiently long,
    clearly prose-like runs; keep Markdown layout and ambiguous text verbatim.
    """
    if "\n" not in text:
        return text

    structural = re.compile(
        r"^(?:\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|(?:-{3,}|\*{3,}|_{3,})\s*$)"
        r"|\s{0,3}(?:\|.*\||<[/!A-Za-z]|::\s*$|\d{4}-\d{2}-\d{2}\s)"
        r"|\s{0,3}(?:sudo |\$ |>>> |curl |git |python\d? |systemctl )|\s{4,}|\t)"
    )
    speaker = re.compile(r'^\s{0,3}[A-Z][\w .-]{0,24}:\s')
    fence = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
    output: list[str] = []
    pending: list[str] = []
    fence_char = ""
    fence_length = 0
    in_structured_block = False

    def flush() -> None:
        if (len(pending) > 1 and len(pending[0]) >= 60 and
                all(len(re.findall(r"[A-Za-z]+", line)) >= 4 for line in pending)):
            output.append(" ".join(line.strip() for line in pending))
        else:
            output.extend(pending)
        pending.clear()

    for line in text.split("\n"):
        trimmed = line.strip()
        marker = fence.match(line)
        if fence_char:
            output.append(line)
            if marker and marker.group(1)[0] == fence_char and len(marker.group(1)) >= fence_length:
                fence_char = ""
            continue
        if marker:
            flush()
            fence_char, fence_length = marker.group(1)[0], len(marker.group(1))
            output.append(line)
            continue
        if not trimmed:
            flush()
            output.append(line)
            in_structured_block = False
            continue
        if (in_structured_block or structural.match(line) or speaker.match(line) or
                trimmed.endswith(":") or line.endswith(("  ", "\\")) or
                any(c in line for c in "{}") or
                len(re.findall(r"[A-Za-z]+", line)) < 4):
            flush()
            output.append(line)
            if structural.match(line) and re.match(r'^\s{0,3}(?:[-*+]\s|\d+[.)]\s)', line):
                in_structured_block = True
            continue
        if pending and re.search(r'[.!?]["\']?$', pending[-1].strip()) and trimmed[:1].isupper():
            # A standalone sentence followed by another capitalized line may
            # be an intentional paragraph break without an empty line.
            flush()
        pending.append(line)
    flush()
    return "\n".join(output)


class Handler(BaseHTTPRequestHandler):
    server_version = f"OpenBotHerdrBridge/{VERSION}"

    def log_message(self, format_string: str, *args: object) -> None:
        # Deliberately never logs request bodies, authorization, or agent output.
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(),
                                          format_string % args))

    def authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        return hmac.compare_digest(value, "Bearer " + TOKEN)

    def json_response(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.json_response(200, {"ok": True, "version": VERSION, "mode": "multi-agent",
                                     "busy_wait_enabled": BUSY_WAIT_SECONDS > 0})
            return
        # Public capability links do not carry the gateway's broad bearer
        # credential. Possession of a 256-bit random path is the only grant.
        artifact = ARTIFACT_RE.fullmatch(path)
        if artifact:
            if ARTIFACT_MODE != "signed_url" or not ARTIFACT_DIR:
                self.json_response(404, {"error": "not_found"})
                return
            location = Path(ARTIFACT_DIR) / f"{artifact.group(1)}.{artifact.group(2)}"
            try:
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(location, flags)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_SIZE or
                            time.time() - info.st_mtime > ARTIFACT_TTL_SECONDS):
                        self.json_response(404, {"error": "not_found"})
                        return
                    data = handle.read(MAX_FILE_SIZE + 1)
                kind = image_file_type(data)
                if not kind or kind[0] != artifact.group(2):
                    self.json_response(404, {"error": "not_found"})
                    return
            except OSError:
                self.json_response(404, {"error": "not_found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", kind[1])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "inline")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        if path.startswith("/requests/") and LEDGER is not None:
            key = path.removeprefix("/requests/")
            if not re.fullmatch(r"[a-f0-9]{64}", key):
                self.json_response(404, {"error": "not_found"})
                return
            record = LEDGER.get(key)
            if record is None:
                self.json_response(404, {"error": "not_found"})
            else:
                self.json_response(200, {"id": key, "state": record["state"],
                                         "reason": record.get("reason"),
                                         "queued_at": record.get("queued_at"),
                                         "updated_at": record["updated_at"],
                                         "reply": record["reply"] if record["state"] == "finished" else None})
            return
        if path in ("/agents", "/attention"):
            sessions = []
            for session in SESSIONS:
                try:
                    agents = [public_agent_info(session, a) for a in list_agents(session)]
                    sessions.append({"name": session, "online": True, "agents": agents})
                except (RuntimeError, OSError, subprocess.TimeoutExpired,
                        ValueError, json.JSONDecodeError):
                    # A stopped session remains visible without being started.
                    sessions.append({"name": session, "online": False, "agents": []})
            if path == "/attention":
                self.json_response(200, {"blocked": [a for s in sessions for a in s["agents"]
                                                    if a["status"] == "blocked"],
                                         "offline_sessions": [s["name"] for s in sessions
                                                              if not s["online"]],
                                         "unresolved_requests": LEDGER.unresolved() if LEDGER else [],
                                         "queued_requests": LEDGER.queued() if LEDGER else [],
                                         "recent_failed_requests":
                                             LEDGER.recent_failures() if LEDGER else []})
            else:
                self.json_response(200, {"version": VERSION, "sessions": sessions})
            return
        if path == "/agent" and TARGET:
            try:
                self.json_response(200, {"target": TARGET, "machine": MACHINE or "local",
                                         "state": target_state()})
            except Exception as exc:
                self.json_response(503, {"error": str(exc)})
            return
        self.json_response(404, {"error": "not_found"})

    def send_event(self, payload: dict) -> None:
        message = "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"
        self.wfile.write(message.encode("utf-8"))
        self.wfile.flush()

    def start_stream(self, thread_id: str, run_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        self.send_event({"type": "RUN_STARTED", "threadId": thread_id,
                         "runId": run_id})

    def send_progress(self, state: str) -> None:
        # AG-UI CUSTOM is a valid event but does not become assistant prose.
        # The upstream OpenBot stream watchdog observes bytes, not just text.
        self.send_event({"type": "CUSTOM", "name": "agent-fabric.progress",
                         "value": {"state": state}})

    def send_submitted_event(self, payload: dict) -> None:
        """Continue tracking a submitted run if its browser stream closes.

        Once Herdr has been invoked, dropping the stream must not abandon the
        work or release this pane's lock before the result is reconciled.
        Waiting (not yet submitted) requests still stop on a broken stream.
        """
        if getattr(self, "_stream_detached", False):
            return
        try:
            self.send_event(payload)
        except (BrokenPipeError, ConnectionResetError):
            self._stream_detached = True

    def send_submitted_progress(self, state: str) -> None:
        self.send_submitted_event({"type": "CUSTOM", "name": "agent-fabric.progress",
                                   "value": {"state": state}})

    def send_reply(self, msg_id: str, thread_id: str, run_id: str,
                   response: str) -> None:
        self.send_event({"type": "TEXT_MESSAGE_START", "messageId": msg_id,
                         "role": "assistant"})
        self.send_event({"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id,
                         "delta": response})
        self.send_event({"type": "TEXT_MESSAGE_END", "messageId": msg_id})
        self.send_event({"type": "RUN_FINISHED", "threadId": thread_id,
                         "runId": run_id})

    def prompt_with_liveness(self, session: str, pane_id: str, prompt: str,
                             marker_id: str) -> str:
        """Submit exactly once. On an ambiguous timeout, observe; never retry."""
        args = ("agent", "prompt", pane_id, prompt, "--wait", "--until", "idle",
                "--until", "done", "--until", "blocked", "--timeout", str(TIMEOUT_MS))
        timeout_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            submitted = pool.submit(command, *args, session=session,
                                    wait_seconds=TIMEOUT_MS / 1000 + 15)
            while True:
                try:
                    submitted.result(timeout=HEARTBEAT_SECONDS)
                    break
                except FutureTimeout:
                    self.send_submitted_progress("working")
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    timeout_error = exc
                    break

        if timeout_error is not None:
            message = str(timeout_error).lower()
            if (RECOVERY_SECONDS <= 0 or
                    not ("timed out waiting for agent status" in message or
                         isinstance(timeout_error, subprocess.TimeoutExpired))):
                raise timeout_error

        # A successful Herdr --wait return does not guarantee the response
        # markers are already visible in recent-unwrapped. Poll for them even
        # without an explicit Herdr timeout; otherwise a fast status change
        # can mark the request uncertain and release the pane to the next run.
        deadline = time.monotonic() + RECOVERY_SECONDS
        while True:
            if normalized_state(agent_record(session, pane_id)) == "blocked":
                raise RuntimeError(f"Agent {pane_id} needs an approval or answer in Herdr")
            try:
                transcript = command("agent", "read", pane_id,
                                     "--source", "recent-unwrapped", "--lines", "200",
                                     session=session, wait_seconds=30)
                return normalize_prose_wrapping(extract_reply(transcript, marker_id))
            except RuntimeError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Submitted agent reply could not be correlated "
                        "within the recovery window. Inspect Herdr before retrying; "
                        "automatic resubmission is disabled."
                    ) from exc
            self.send_submitted_progress("recovering")
            time.sleep(min(STATE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    def read_request_body(self) -> bytes:
        """Read bounded JSON bodies using Content-Length or HTTP chunked framing.

        Wire bytes are bounded by MAX_RAW_BODY and streamed through
        DataUriStripper; only the stripped body (<= MAX_BODY) is kept.
        """
        encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        content_length = self.headers.get("Content-Length")
        self.log_message("AGUI request framing: Content-Length=%r Transfer-Encoding=%r Limit=%d",
                         content_length, encoding, MAX_BODY)
        self.connection.settimeout(30)
        stripper = DataUriStripper()
        kept: list[bytes] = []
        kept_total = 0

        def keep(piece: bytes) -> None:
            nonlocal kept_total
            kept_total += len(piece)
            if kept_total > MAX_BODY:
                raise PayloadTooLarge("Request exceeds the 64 MiB AG-UI limit")
            kept.append(piece)

        def done() -> bytes:
            keep(stripper.finish())
            if stripper.stripped:
                self.log_message("AGUI history: omitted %d inline data-URI image(s)",
                                 stripper.stripped)
            return b"".join(kept)

        if encoding:
            if encoding != "chunked" or content_length is not None:
                raise ValueError("Unsupported or ambiguous request framing")
            total = 0
            while True:
                length_line = self.rfile.readline(1025)
                if not length_line or len(length_line) > 1024:
                    raise ValueError("Invalid chunk header")
                try:
                    size = int(length_line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise ValueError("Invalid chunk size") from None
                if size < 0 or total + size > MAX_RAW_BODY:
                    raise PayloadTooLarge("Request exceeds the 64 MiB AG-UI limit")
                if size == 0:
                    # Consume permitted trailers so the framing stays well-formed.
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(1025)
                        trailer_bytes += len(trailer)
                        if not trailer or len(trailer) > 1024 or trailer_bytes > 8192:
                            raise ValueError("Invalid request trailers")
                        if trailer in (b"\r\n", b"\n"):
                            return done()
                remaining = size
                while remaining:
                    piece = self.rfile.read(min(remaining, 1024 * 1024))
                    if not piece:
                        raise ValueError("Truncated or malformed request chunk")
                    remaining -= len(piece)
                    keep(stripper.feed(piece))
                if self.rfile.read(2) != b"\r\n":
                    raise ValueError("Truncated or malformed request chunk")
                total += size

        try:
            size = int(content_length or "0")
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if size > MAX_RAW_BODY:
            raise PayloadTooLarge("Request exceeds the 64 MiB AG-UI limit")
        if size <= 0:
            raise ValueError("Invalid request size")
        remaining = size
        while remaining:
            piece = self.rfile.read(min(remaining, 1024 * 1024))
            if not piece:
                raise ValueError("Incomplete request body")
            remaining -= len(piece)
            keep(stripper.feed(piece))
        return done()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        match = AGENT_ROUTE.fullmatch(path)
        legacy = path == "/ag-ui" and bool(TARGET)
        if not match and not legacy:
            self.json_response(404, {"error": "not_found"})
            return
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        if SHUTTING_DOWN.is_set():
            self.json_response(503, {"error": "Gateway is restarting; no prompt submitted. Resend shortly.",
                                     "code": "gateway_restarting"})
            return
        if match:
            session, requested_pane = match.groups()
            try:
                agent = live_target(session, requested_pane)
            except LookupError:
                self.json_response(404, {"error": "agent_not_found"})
                return
            except (RuntimeError, OSError, subprocess.TimeoutExpired,
                    ValueError, json.JSONDecodeError):
                self.json_response(503, {"error": "Herdr session unavailable"})
                return
            target = requested_pane
            workdir = str(agent.get("foreground_cwd") or agent.get("cwd") or "")
            dynamic = True
        else:
            session = SESSION
            target = TARGET
            try:
                agent = agent_record(session, target)
            except (RuntimeError, OSError, subprocess.TimeoutExpired,
                    ValueError, json.JSONDecodeError):
                self.json_response(503, {"error": "Legacy agent unavailable"})
                return
            workdir = WORKDIR
            dynamic = False
        pane_id = str(agent.get("pane_id") or target)
        if not PANE_RE.fullmatch(pane_id):
            self.json_response(503, {"error": "Herdr did not identify the target pane"})
            return
        try:
            body = json.loads(self.read_request_body())
            # Key names, field types and lengths only; no user content or tokens.
            self.log_message("AGUI request shape: %s",
                             json.dumps(redacted_request_shape(body), separators=(",", ":")))
            user_text, attachments = parse_user_message(body)
            if "\x00" in user_text:
                self.json_response(400, {
                    "error": "User message contains an invalid NUL character",
                    "code": "invalid_nul_character",
                })
                return
            prompt_bytes = len(user_text.encode("utf-8"))
        except PayloadTooLarge as exc:
            self.json_response(413, {"error": str(exc), "code": "request_too_large",
                                     "max_bytes": MAX_BODY})
            return
        except (ValueError, json.JSONDecodeError, TypeError, TimeoutError) as exc:
            self.json_response(400, {"error": str(exc)})
            return

        thread_id = str(body.get("threadId") or uuid.uuid4())
        run_id = str(body.get("runId") or uuid.uuid4())
        msg_id = str(uuid.uuid4())
        request_key = LEDGER.key(session, pane_id, thread_id, run_id) if LEDGER else ""
        input_digest = LEDGER.fingerprint(user_text, attachments) if LEDGER else ""
        if LEDGER:
            previous = LEDGER.get(request_key)
            if previous and previous["state"] != "failed":
                if previous["fingerprint"] != input_digest:
                    self.json_response(409, {"error": "Run identifier reused with different content"})
                    return
                self.start_stream(thread_id, run_id)
                if previous["state"] == "finished":
                    self.send_reply(msg_id, thread_id, run_id, previous["reply"] or "")
                else:
                    self.send_event({"type": "RUN_ERROR", "message":
                                     f"Request {request_key} is already {previous['state']}. "
                                     "No duplicate prompt sent. Inspect Herdr or check /requests/ID."})
                return

        wait_enabled = BUSY_WAIT_SECONDS > 0
        if wait_enabled:
            if not WAITING_SLOTS.acquire(blocking=False):
                self.json_response(503, {"error": "Bridge is at queued-request capacity"})
                return
        elif not GLOBAL_SLOTS.acquire(blocking=False):
            self.json_response(503, {"error": "Bridge is at concurrent run capacity"})
            return
        lock = target_lock(session, pane_id)
        if not wait_enabled and not lock.acquire(blocking=False):
            GLOBAL_SLOTS.release()
            self.json_response(409, {"error": "Agent is already handling an OpenBot request"})
            return

        marker_id = secrets.token_hex(8).upper()
        begin = f"OPENBOT_BEGIN_{marker_id}"
        end = f"OPENBOT_END_{marker_id}"
        have_lock = not wait_enabled
        have_slot = not wait_enabled
        stream_started = False
        ledger_reserved = False
        ledger_queued = False
        fail_reason = "not_submitted"
        queue_ticket: object | None = None
        pane_queue = waiting_queue(session, pane_id) if wait_enabled else None
        last_progress = 0.0

        def check_shutdown() -> None:
            if SHUTTING_DOWN.is_set():
                raise RuntimeError("Gateway is restarting; this queued request was not submitted. "
                                   "Please resend it.")

        def progress_if_due(state: str) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= HEARTBEAT_SECONDS:
                self.send_progress(state)
                last_progress = now

        try:
            if wait_enabled:
                self.start_stream(thread_id, run_id)
                stream_started = True
                if LEDGER:
                    try:
                        queued = LEDGER.enqueue(request_key, session, pane_id,
                                                input_digest, marker_id)
                    except ValueError as exc:
                        raise RuntimeError(str(exc)) from exc
                    if not queued["created"]:
                        # A concurrent identical run owns this key.
                        if queued["state"] == "finished":
                            self.send_reply(msg_id, thread_id, run_id, queued["reply"] or "")
                        else:
                            self.send_event({"type": "RUN_ERROR", "message":
                                             f"Request {request_key} is already {queued['state']}. "
                                             "No duplicate prompt sent."})
                        return
                    ledger_queued = True
                    self.send_event({"type": "CUSTOM", "name": "agent-fabric.request",
                                     "value": {"id": request_key, "state": "queued"}})
                deadline = time.monotonic() + BUSY_WAIT_SECONDS
                # Explicit FIFO: lock acquisition alone is not guaranteed fair.
                queue_ticket = object()
                with WAIT_QUEUES_GUARD:
                    assert pane_queue is not None
                    pane_queue.append(queue_ticket)
                while True:
                    with WAIT_QUEUES_GUARD:
                        if pane_queue[0] is queue_ticket and lock.acquire(blocking=False):
                            pane_queue.popleft()
                            have_lock = True
                            queue_ticket = None
                            break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Agent is still busy; waiting expired. No prompt submitted.")
                    check_shutdown()
                    progress_if_due("queued")
                    time.sleep(min(STATE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

            current_agent = agent_record(session, pane_id)
            state = normalized_state(current_agent)
            # Don't route to a process that replaced the pane occupant after
            # discovery. Herdr's native session ref exists for most Claude panes.
            original_ref = agent.get("agent_session", {}).get("value") if isinstance(
                agent.get("agent_session"), dict) else None
            current_ref = current_agent.get("agent_session", {}).get("value") if isinstance(
                current_agent.get("agent_session"), dict) else None
            if original_ref and current_ref and original_ref != current_ref:
                if wait_enabled:
                    raise RuntimeError("Agent changed while request was waiting. No prompt submitted.")
                self.json_response(409, {"error": "Agent changed while request was preparing"})
                return
            while wait_enabled and state == "working":
                if time.monotonic() >= deadline:
                    raise RuntimeError("Agent is still working; waiting expired. No prompt submitted.")
                check_shutdown()
                progress_if_due("queued")
                time.sleep(min(STATE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                current_agent = agent_record(session, pane_id)
                updated_ref = current_agent.get("agent_session", {}).get("value") if isinstance(
                    current_agent.get("agent_session"), dict) else None
                if original_ref and updated_ref and original_ref != updated_ref:
                    raise RuntimeError("Agent session changed while queued. No prompt submitted.")
                state = normalized_state(current_agent)
            if state not in ("idle", "done") and not wait_enabled:
                self.json_response(409, {"error": f"Agent is {state}, no prompt submitted"})
                return
            if state not in ("idle", "done"):
                raise RuntimeError(f"Agent is {state}. No prompt submitted; inspect Herdr.")
            if wait_enabled:
                while not GLOBAL_SLOTS.acquire(blocking=False):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Bridge is still at capacity; waiting expired. No prompt submitted.")
                    check_shutdown()
                    progress_if_due("waiting_for_capacity")
                    time.sleep(min(STATE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                have_slot = True
                # A native Herdr operator could have started the pane while we
                # waited for capacity; revalidate before prompt submission.
                latest_agent = agent_record(session, pane_id)
                latest_ref = latest_agent.get("agent_session", {}).get("value") if isinstance(
                    latest_agent.get("agent_session"), dict) else None
                if original_ref and latest_ref and original_ref != latest_ref:
                    raise RuntimeError("Agent session changed while queued. No prompt submitted.")
                state = normalized_state(latest_agent)
                if state not in ("idle", "done"):
                    raise RuntimeError(f"Agent became {state} while queued. No prompt submitted.")
            try:
                long_text = prompt_bytes > MAX_PROMPT_BYTES
                staged_items = list(attachments)
                if long_text:
                    staged_items.append(("full-user-instructions.txt", "text/plain",
                                         user_text.encode("utf-8")))
                staged = stage_attachments(staged_items, marker_id, workdir,
                                           dynamic=dynamic)
                outbox = prepare_outbox(marker_id, workdir, dynamic=dynamic)
            except (ValueError, OSError) as exc:
                if stream_started:
                    raise RuntimeError(f"Could not prepare attachment: {exc}") from exc
                self.json_response(422, {"error": str(exc)})
                return
            if not stream_started:
                self.start_stream(thread_id, run_id)
                stream_started = True
            if long_text:
                user_text = (
                    "The complete user instruction is in the attached full-user-instructions.txt "
                    "file below. Read the entire file before doing any work; do not "
                    "answer from this summary alone."
                )
            attachment_instructions = ""
            if staged:
                attachment_instructions = (
                    "\n\nThe user provided these files in your working directory. "
                    "Read them with your normal file tools as needed; the files are not "
                    "included verbatim in this prompt. A file marked "
                    "source=openbot-derived-text is a reconstructed UTF-8 text copy, "
                    "not guaranteed to be byte-for-byte identical to the upload:\n" +
                    "\n".join(f"- {path}" for path in staged)
                )
            prompt = (
                f"[OpenBot pilot request {marker_id}]\n{user_text}"
                f"{attachment_instructions}\n\n"
                "For this integration test, enclose your final response between "
                "these exact lines, with no formatting around them:\n"
                f"{begin}\n<your response>\n{end}\n"
                "To return up to four screenshots, save actual PNG, JPEG, or WebP files "
                f"into .openbot-outbox/{marker_id}/. The gateway automatically attaches "
                "the image files in that directory. No OPENBOT_IMAGE declaration or "
                "special reply formatting is needed. This is optional; never claim a "
                "screenshot was attached unless you saved the image. Keep the response concise."
            )
            if wait_enabled:
                check_shutdown()
            if LEDGER:
                try:
                    reserved = LEDGER.reserve(request_key, session, pane_id,
                                              input_digest, marker_id)
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
                if not reserved["created"]:
                    if reserved["state"] == "finished":
                        self.send_reply(msg_id, thread_id, run_id, reserved["reply"] or "")
                    else:
                        self.send_event({"type": "RUN_ERROR", "message":
                                         f"Request {request_key} is already submitted or uncertain. "
                                         "No duplicate prompt sent."})
                    return
                ledger_reserved = True
                self.send_submitted_event({"type": "CUSTOM", "name": "agent-fabric.request",
                                           "value": {"id": request_key, "state": "submitted"}})
            response = self.prompt_with_liveness(session, pane_id, prompt, marker_id)
            response = persist_reply_images(response, outbox)
            if LEDGER:
                LEDGER.finish(request_key, response)
                ledger_reserved = False
            self.send_reply(msg_id, thread_id, run_id, response)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            fail_reason = str(exc) or type(exc).__name__
            # A protocol run failure is not proof the Herdr prompt was not sent.
            if ledger_reserved and LEDGER:
                LEDGER.uncertain(request_key)
            try:
                self.send_event({"type": "RUN_ERROR", "message": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            fail_reason = "client_disconnected_before_submit"
            if ledger_reserved and LEDGER:
                LEDGER.uncertain(request_key)
            pass
        finally:
            if ledger_queued and LEDGER:
                # No-op unless the row is still 'queued' (never submitted).
                try:
                    LEDGER.fail(request_key, fail_reason)
                except Exception as exc:  # noqa: BLE001 - startup recovery still covers it
                    self.log_message("ledger fail() error: %s", type(exc).__name__)
            if queue_ticket is not None and pane_queue is not None:
                with WAIT_QUEUES_GUARD:
                    pane_queue.remove(queue_ticket)
            if have_lock:
                lock.release()
            if have_slot:
                GLOBAL_SLOTS.release()
            if wait_enabled:
                WAITING_SLOTS.release()


def main() -> None:
    global LEDGER
    if TARGET and not re.fullmatch(r"(?:w[0-9A-Za-z]+:p[0-9A-Za-z]+|[a-z][a-z0-9_-]{0,31})", TARGET):
        sys.exit("HERDR_AGENT_TARGET must be an existing agent name or pane ID")
    if not SESSIONS or any(not SESSION_RE.fullmatch(s) for s in SESSIONS):
        sys.exit("Set HERDR_SESSIONS to one or more named Herdr sessions")
    if TARGET and SESSION not in SESSIONS:
        sys.exit("HERDR_SESSION must belong to HERDR_SESSIONS when using legacy /ag-ui")
    if not TOKEN or len(TOKEN) < 24:
        sys.exit("Set OPENBOT_BRIDGE_TOKEN to a random secret of at least 24 characters")
    if HOST != "127.0.0.1":
        sys.exit("Bridge must bind to 127.0.0.1; use a private proxy/tunnel")
    if MACHINE and (SESSION or SOCKET_PATH):
        sys.exit("Use HERDR_MACHINE OR local HERDR_SESSION/SOCKET_PATH, not both")
    if (MACHINE or SOCKET_PATH) and len(SESSIONS) != 1:
        sys.exit("Multi-session routing requires local Herdr sessions (no machine/socket override)")
    for root in ALLOWED_ROOTS:
        if not Path(root).is_absolute() or not Path(root).is_dir():
            sys.exit("Every OPENBOT_BRIDGE_ALLOWED_ROOTS entry must be an existing absolute directory")
    if TIMEOUT_MS < 1000 or TIMEOUT_MS > 600000:
        sys.exit("OPENBOT_BRIDGE_TIMEOUT_MS must be between 1000 and 600000")
    if BUSY_WAIT_SECONDS < 0 or BUSY_WAIT_SECONDS > 3600:
        sys.exit("OPENBOT_BRIDGE_BUSY_WAIT_SECONDS must be between 0 and 3600")
    if RECOVERY_SECONDS < 0 or RECOVERY_SECONDS > 3600:
        sys.exit("OPENBOT_BRIDGE_RECOVERY_SECONDS must be between 0 and 3600")
    if BUSY_WAIT_SECONDS and not LEDGER_PATH:
        sys.exit("Enable a persistent OPENBOT_BRIDGE_LEDGER_PATH before enabling busy waiting")
    if LEDGER_PATH:
        os.umask(0o077)
        LEDGER = RequestLedger(LEDGER_PATH)
        abandoned = LEDGER.fail_abandoned_queue()
        if abandoned:
            print(f"Ledger: marked {abandoned} request(s) failed "
                  "(queued at previous shutdown, never submitted)", flush=True)
        LEDGER.trim_finished()
    if ARTIFACT_DIR:
        directory = Path(ARTIFACT_DIR)
        if not directory.is_absolute() or directory.is_symlink():
            sys.exit("OPENBOT_BRIDGE_ARTIFACT_DIR must be a non-symlink absolute directory")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if ARTIFACT_MODE not in ("inline", "signed_url"):
        sys.exit("OPENBOT_BRIDGE_ARTIFACT_MODE must be inline or signed_url")
    origin = urlsplit(PUBLIC_ORIGIN)
    if PUBLIC_ORIGIN and (origin.scheme != "https" or not origin.hostname or
                          origin.username is not None or origin.password is not None or
                          origin.path not in ("", "/") or origin.query or origin.fragment):
        sys.exit("OPENBOT_BRIDGE_PUBLIC_ORIGIN must be an HTTPS origin without path/query")
    if ARTIFACT_TTL_SECONDS < 3600 or ARTIFACT_TTL_SECONDS > 90 * 86400:
        sys.exit("OPENBOT_BRIDGE_ARTIFACT_TTL_SECONDS must be between one hour and 90 days")
    if ARTIFACT_STORAGE_BYTES < MAX_FILE_SIZE:
        sys.exit("OPENBOT_BRIDGE_ARTIFACT_STORAGE_BYTES must allow at least one screenshot")
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def on_sigterm(signum: int, frame: object) -> None:
        # Tell queued waiters (they answer RUN_ERROR + ledger 'failed'), refuse
        # new runs, then stop. Requests already submitted are unaffected by this
        # and remain 'submitted' in the ledger, exactly as before.
        if SHUTTING_DOWN.is_set():
            return
        SHUTTING_DOWN.set()
        print("Shutdown requested: refusing new runs, releasing queued requests", flush=True)

        def stop() -> None:
            deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
            while time.monotonic() < deadline:
                if LEDGER is not None:
                    if not LEDGER.queued():
                        break
                else:
                    with WAIT_QUEUES_GUARD:
                        if not any(WAIT_QUEUES.values()):
                            break
                time.sleep(0.1)
            server.shutdown()

        threading.Thread(target=stop, daemon=True).start()

    signal.signal(signal.SIGTERM, on_sigterm)
    print(f"Bridge ready on http://{HOST}:{PORT}/agents | "
          f"sessions={','.join(SESSIONS)} | legacy_target={TARGET or 'disabled'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
