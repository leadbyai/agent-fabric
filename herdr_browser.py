"""The OpenBot browser for Herdr agents (Claude Code, Codex), turn by turn.

OpenBot lets an Agent Fabric Bot drive a browser on the computer of the person it is working for
(openbot-custom `docs/BROWSER_DEFAULT.md`). OpenBot signs every person-started run
(`forwardedProps.openbotRun`). The gateway proves it is that Bot's endpoint by handing back, during
the turn, the credential OpenBot presented on the request. Nothing is issued per Bot, and no agent is
reconfigured.

Per turn:
1. `open_turn` keeps the signed run and the credential in this process's memory only, under a
   random 256-bit key, and writes `browser.json` (the gateway's local address and that key, never
   the credential) into the turn's private outbox.
2. The prompt tells the agent to run `openbot_browser_cli.py <outbox>/browser.json navigate <url>`
   (or `read`, `screenshot`).
3. The CLI posts to this gateway's `/openbot-browser/<key>`; `call` forwards to OpenBot with the
   signed run and the credential. A screenshot is saved into the outbox, where the bridge's
   existing reply-image code attaches it.
4. `close_turn` forgets the key and removes `browser.json` when the request ends, whatever happened.

On only when `OPENBOT_BROWSER_CALLBACK_URL` names OpenBot's browser route, e.g.
`https://bot.example.com/api/agent-fabric/browser`. OpenBot's signed run lasts 10 minutes.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

CALLBACK_URL = os.environ.get("OPENBOT_BROWSER_CALLBACK_URL", "").strip()
TURN_SECONDS = 600
MAX_SHOTS = 4
MAX_SHOT_BYTES = 2_000_000
TURN_FILE = "browser.json"
KEY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
PNG = b"\x89PNG\r\n\x1a\n"
# Sent on every call to OpenBot. Some edge proxies refuse urllib's default
# "Python-urllib/x.y" signature before the request reaches OpenBot.
USER_AGENT = "agent-fabric-openbot-browser/0.7.0"


@dataclass
class Turn:
    assertion: str
    credential: str
    outbox: Path | None
    expires: float
    shots: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_TURNS: dict[str, Turn] = {}
_GUARD = threading.Lock()


def callback_url(value: str | None = None) -> str | None:
    """The OpenBot route, if configured sensibly: https, or http to a private address."""
    value = CALLBACK_URL if value is None else value
    if not value:
        return None
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        return None
    if parts.scheme == "https":
        return value
    host = parts.hostname
    private = host in ("localhost", "127.0.0.1", "::1") or host.startswith(("100.", "10.", "192.168."))
    return value if parts.scheme == "http" and private else None


def enabled() -> bool:
    return callback_url() is not None


def open_turn(forwarded_props: object, credential: str | None, outbox: Path | None,
              gateway_address: str) -> Path | None:
    """Start this turn's browser; the turn file's path, or None when this run cannot browse."""
    if not enabled() or outbox is None or not credential or len(credential) > 4096:
        return None
    assertion = forwarded_props.get("openbotRun") if isinstance(forwarded_props, dict) else None
    if not isinstance(assertion, str) or not 1 <= len(assertion) <= 4096:
        return None
    key = secrets.token_urlsafe(32)
    turn_file = outbox / TURN_FILE
    descriptor = os.open(turn_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"endpoint": f"{gateway_address}/openbot-browser/{key}"}, handle)
    with _GUARD:
        _TURNS[key] = Turn(assertion, credential, outbox, time.monotonic() + TURN_SECONDS)
    return turn_file


def close_turn(turn_file: Path | None) -> None:
    if turn_file is None:
        return
    try:
        endpoint = json.loads(turn_file.read_text(encoding="utf-8")).get("endpoint", "")
    except (OSError, ValueError):
        endpoint = ""
    with _GUARD:
        _TURNS.pop(endpoint.rsplit("/", 1)[-1], None)
    turn_file.unlink(missing_ok=True)


def _turn(key: str) -> Turn | None:
    if not (40 <= len(key) <= 64) or not set(key) <= KEY_CHARS:
        return None
    with _GUARD:
        turn = _TURNS.get(key)
        if turn and time.monotonic() > turn.expires:
            _TURNS.pop(key, None)
            return None
        return turn


def _post(turn: Turn, payload: dict) -> tuple[int, dict]:
    url = callback_url()
    if url is None:
        return 503, {"error": "The OpenBot browser is not configured on this gateway."}
    request = urllib.request.Request(
        url,
        data=json.dumps({"run": turn.assertion, **payload}).encode("utf-8"),
        headers={"content-type": "application/json", "user-agent": USER_AGENT,
                 "x-agent-fabric-credential": turn.credential},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(65536) or b"{}")
        except ValueError:
            body = {}
        message = body.get("error") if isinstance(body.get("error"), str) else "OpenBot refused the browser call."
        return error.code, {"error": message}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 502, {"error": "Could not reach the OpenBot browser."}


def call(key: str, body: object) -> tuple[int, dict]:
    """One browser action for the turn that holds `key`."""
    turn = _turn(key)
    if turn is None:
        return 404, {"error": "This browser session has ended. Browsing works only during an OpenBot request."}
    if not isinstance(body, dict):
        return 400, {"error": "Invalid browser request."}
    action = body.get("action")
    if action == "navigate":
        url = body.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            return 400, {"error": "navigate needs a URL."}
        return _post(turn, {"action": "navigate", "url": url})
    if action == "read":
        return _post(turn, {"action": "read"})
    if action != "screenshot":
        return 400, {"error": "Use navigate, read or screenshot."}
    with turn.lock:
        if turn.shots >= MAX_SHOTS:
            return 429, {"error": f"At most {MAX_SHOTS} screenshots per reply."}
        status, result = _post(turn, {"action": "screenshot"})
        if status != 200:
            return status, result
        try:
            data = base64.b64decode(result.get("image", ""), validate=True)
        except (ValueError, TypeError):
            return 502, {"error": "OpenBot returned an unreadable screenshot."}
        if not data.startswith(PNG) or len(data) > MAX_SHOT_BYTES or turn.outbox is None:
            return 502, {"error": "OpenBot returned an unusable screenshot."}
        turn.shots += 1
        path = turn.outbox / f"openbot-browser-{turn.shots}.png"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return 200, {"saved": path.name, "url": result.get("url")}


def prompt_hint(turn_file: Path, shown_path: str, cli: Path) -> str:
    """What the agent is told. `shown_path` is the turn file as the agent should type it."""
    command = f"python3 {cli} {shown_path}"
    return (
        "For anything on the web, use the OpenBot browser. It runs on the person's own computer in "
        "OpenBot, where they can watch it: "
        f"`{command} navigate <url>` opens a page and prints its text, "
        f"`{command} read` prints the current page, and "
        f"`{command} screenshot` captures it and attaches the picture to your reply automatically. "
    )
