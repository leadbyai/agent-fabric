"""Files a Herdr agent hands back to the person (audio first), through OpenBot's file route.

Images in the per-request outbox keep their existing path (`persist_reply_images`). Every other
regular file there is sent to OpenBot's `/api/agent-fabric/attachments`, with the signed run OpenBot
put on this request (`forwardedProps.openbotRun`) and the credential OpenBot presented to this
gateway — the same proof the OpenBot browser uses. OpenBot stores the file in the conversation and
checks access on every fetch; the reply carries only a short link line per file:

    [narration.mp3](/api/attachments/<uuid> "audio/mpeg")

Refused before anything is sent: links (O_NOFOLLOW), hard links (st_nlink > 1), anything that is not
a regular file, names that look like credentials, and files holding a private-key PEM header. One
journal line and one ledger row per file: name, size, type and sha256 — never the contents.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

MAX_FILES = 4
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
# Gateway-owned files that live in the outbox and are never deliverables.
RESERVED = {"browser.json"}
UUID = re.compile(r"[0-9a-fA-F-]{36}\Z")
TYPE = re.compile(r"[a-z0-9.+-]{1,60}/[a-z0-9.+-]{1,80}\Z")
PRIVATE_KEY = re.compile(rb"-----BEGIN ((RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----")
CLAIMS = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/mp4", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".weba": "audio/webm",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".pdf": "application/pdf", ".zip": "application/zip", ".csv": "text/csv", ".md": "text/markdown",
    ".txt": "text/plain", ".json": "application/json",
}


@dataclass
class Delivered:
    name: str
    size: int
    mime_type: str
    sha256: str
    attachment_id: str


def files_url(browser_url: str | None) -> str | None:
    """OpenBot's file route, next to its browser route (`…/agent-fabric/browser` → `…/attachments`)."""
    if not browser_url:
        return None
    parts = urlsplit(browser_url)
    if not parts.path.endswith("/browser"):
        return None
    return urlunsplit(parts._replace(path=parts.path[: -len("/browser")] + "/attachments"))


def looks_like_credential(name: str) -> bool:
    base = name.lower().replace("\\", "/").rsplit("/", 1)[-1]
    return (
        base.startswith(".env") or base in ("auth.json", "credentials.json", ".netrc", ".pgpass")
        or base.endswith((".pem", ".key", ".p12", ".pfx"))
        or re.fullmatch(r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?", base) is not None
        or "keybox" in base or "bridge.token" in base
    )


def deliverable_names(outbox_fd: int) -> list[str]:
    """Outbox entries this module handles: not images, not gateway files, not hidden."""
    with os.scandir(outbox_fd) as entries:
        names = sorted(entry.name for entry in entries)
    return [name for name in names
            if Path(name).suffix.lower() not in IMAGE_EXTENSIONS
            and name not in RESERVED and not name.startswith(".") and not name.endswith(".tmp")]


def read_safely(outbox_fd: int, name: str) -> bytes:
    if name in (".", "..") or "/" in name or "\n" in name or "\r" in name or len(name) > 255:
        raise ValueError("unsafe file name")
    if looks_like_credential(name):
        raise ValueError("looks like a credential")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=outbox_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_nlink > 1:
            raise ValueError("hard links are not sent")
        if not 0 < info.st_size <= MAX_FILE_BYTES:
            raise ValueError("empty or larger than 25 MB")
        data = handle.read(MAX_FILE_BYTES + 1)
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise ValueError("empty or larger than 25 MB")
    if PRIVATE_KEY.search(data[: 64 * 1024]):
        raise ValueError("contains a private key")
    return data


def upload(url: str, credential: str, assertion: str, name: str, data: bytes,
           timeout: float = 120) -> dict:
    body = json.dumps({
        "run": assertion,
        "name": name,
        "mimeType": CLAIMS.get(Path(name).suffix.lower(), "application/octet-stream"),
        "data": base64.b64encode(data).decode("ascii"),
    }).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "x-agent-fabric-credential": credential,
                 "user-agent": "agent-fabric-herdr-gateway"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read(65536) or b"{}").get("error")
        except ValueError:
            message = None
        raise ValueError(message if isinstance(message, str) else f"OpenBot refused it ({error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValueError("could not reach OpenBot") from error
    if not (isinstance(answer, dict) and isinstance(answer.get("id"), str) and UUID.match(answer["id"])
            and isinstance(answer.get("mimeType"), str) and TYPE.match(answer["mimeType"])):
        raise ValueError("OpenBot gave an unexpected answer")
    return answer


def link_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\r\n\"]", "_", name).strip() or "file"
    return cleaned[:160]


def deliver_files(reply: str, outbox: Path, *, url: str | None, credential: str | None,
                  assertion: str | None, log: Callable[[str], None],
                  record: Callable[[Delivered], None] | None = None,
                  send: Callable[..., dict] = upload) -> str:
    """The reply with one link line per delivered file, and a plain note for any that was not."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    outbox_fd = os.open(outbox, flags)
    try:
        if outbox.is_symlink() or outbox.parent.is_symlink():
            raise RuntimeError("Reply file directory is unsafe")
        names = deliverable_names(outbox_fd)
        if not names:
            return reply
        links: list[str] = []
        notes: list[str] = []
        total = 0
        for name in names:
            if len(links) >= MAX_FILES:
                notes.append(f"{name}: a reply can carry at most {MAX_FILES} files")
                continue
            try:
                data = read_safely(outbox_fd, name)
                if total + len(data) > MAX_TOTAL_BYTES:
                    raise ValueError("the reply's files would pass 50 MB")
                if not (url and credential and assertion):
                    raise ValueError("this conversation cannot receive files")
                answer = send(url, credential, assertion, name, data)
            except (OSError, ValueError) as error:
                notes.append(f"{name}: {error}")
                log(f"agent file refused name={json.dumps(name)} reason={error}")
                continue
            total += len(data)
            delivered = Delivered(name, len(data), answer["mimeType"], hashlib.sha256(data).hexdigest(),
                                  answer["id"])
            log(f"agent file sent name={json.dumps(name)} bytes={delivered.size} "
                f"type={delivered.mime_type} sha256={delivered.sha256} id={delivered.attachment_id}")
            if record:
                record(delivered)
            links.append(f'[{link_name(name)}](/api/attachments/{delivered.attachment_id} "{delivered.mime_type}")')
    finally:
        os.close(outbox_fd)
    parts = [reply.rstrip()] if reply.strip() else []
    if notes:
        parts.append("Not attached:\n" + "\n".join(f"- {note}" for note in notes))
    if links:
        parts.append("\n".join(links))
    return "\n\n".join(parts)
