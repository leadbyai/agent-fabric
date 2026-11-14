#!/usr/bin/env python3
"""Authenticated AG-UI gateway to an existing Hermes Agent gateway (Agent Fabric, Hermes backend).

Runs on the Hermes server as the profiles' own Unix user, with Hermes' own Python, and attaches to
the ALREADY RUNNING Hermes gateway through Hermes' supported local client
(``hermes_cli.gateway_runtime.discover_gateway_endpoint`` + ``hermes_cli.gateway_client``): a
one-use ticket from the private control socket, then the loopback canonical WebSocket.

It never starts, stops, restarts or reconfigures Hermes, never attaches to sessions it did not
create (TUI, messaging, cron, subagent), and never auto-approves. Each OpenBot thread gets its own
new Hermes session (source ``gui``); the thread -> session mapping and per-run idempotency are kept
in a private SQLite ledger, and prompts carry a deterministic ``input_id`` so a retried run is not
admitted twice.

A permanent agent is a served Hermes *profile*. Its stable identity is the Hermes install id plus
the profile id (the profile's home). The gateway's ``instance_id`` changes per gateway run and is
reported only as runtime state.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import bridge
from request_ledger import RequestLedger

VERSION = "0.8.0-hermes.3"
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
ROUTE_RE = re.compile(r"/profiles/([a-z0-9][a-z0-9_-]{0,63})/ag-ui\Z")
SESSION_SOURCE = "gui"
HEARTBEAT_SECONDS = 15.0
DENY_CHOICES = ("deny", "reject", "no", "cancel", "abort")


def env_config() -> dict:
    token = os.environ.get("OPENBOT_BRIDGE_TOKEN", "")
    token_file = os.environ.get("OPENBOT_BRIDGE_TOKEN_FILE", "").strip()
    if token_file and not token:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    token_sha256 = os.environ.get("OPENBOT_BRIDGE_TOKEN_SHA256", "").strip().lower()
    sha_file = os.environ.get("OPENBOT_BRIDGE_TOKEN_SHA256_FILE", "").strip()
    if sha_file and not token_sha256:
        token_sha256 = Path(sha_file).read_text(encoding="utf-8").strip().lower()
    profile_tokens_file = os.environ.get("OPENBOT_BRIDGE_PROFILE_TOKENS_FILE", "").strip()
    profile_hashes = parse_profile_hashes(Path(profile_tokens_file).read_text(encoding="utf-8")) \
        if profile_tokens_file else {}
    return {
        "token": token,
        "token_sha256": token_sha256,
        "profile_hashes": profile_hashes,
        "host": os.environ.get("OPENBOT_BRIDGE_HOST", "127.0.0.1").strip(),
        "port": int(os.environ.get("OPENBOT_BRIDGE_PORT", "8767")),
        "hermes_home": os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")).strip(),
        "agent_dir": os.environ.get("HERMES_AGENT_DIR", "").strip(),
        "profiles": tuple(dict.fromkeys(p.strip() for p in
                          os.environ.get("HERMES_PROFILES", "").split(",") if p.strip())),
        "ledger": os.environ.get("OPENBOT_BRIDGE_LEDGER_PATH", "").strip(),
        "timeout": int(os.environ.get("OPENBOT_BRIDGE_TIMEOUT_SECONDS", "900")),
        "approval_wait": int(os.environ.get("HERMES_APPROVAL_WAIT_SECONDS", "0")),
        "allow_native": os.environ.get("HERMES_ALLOW_NATIVE_ATTACH", "0").strip() == "1",
        "follow_profile": os.environ.get("HERMES_FOLLOW_PROFILE_CONFIG", "1").strip() != "0",
    }


def check_bind_host(host: str) -> None:
    """Loopback, or a Tailscale (CGNAT 100.64.0.0/10) address. Never a wildcard or public address."""
    address = ipaddress.ip_address(host)
    if not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
        raise ValueError("Bind to 127.0.0.1 or this host's Tailscale 100.64.0.0/10 address only")


class HermesLedger(RequestLedger):
    """Run idempotency (inherited) plus the OpenBot thread -> Hermes session map."""

    def _initialize(self) -> None:
        super()._initialize()
        with self._open() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS thread_sessions (
                  profile_id TEXT NOT NULL,
                  thread_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  origin TEXT NOT NULL DEFAULT 'created' CHECK(origin IN ('created', 'bound')),
                  created_at INTEGER NOT NULL,
                  PRIMARY KEY (profile_id, thread_id)
                )
            """)

    def thread_session(self, profile_id: str, thread_id: str) -> tuple[str, str] | None:
        """(session_id, origin) for this thread; origin 'created' = the bridge made it,
        'bound' = an operator bound the thread to an existing native Hermes session."""
        with self._open() as connection:
            row = connection.execute(
                "SELECT session_id, origin FROM thread_sessions WHERE profile_id=? AND thread_id=?",
                (profile_id, thread_id)).fetchone()
            return (row["session_id"], row["origin"]) if row else None

    def map_thread(self, profile_id: str, thread_id: str, session_id: str, origin: str = "created") -> None:
        with self._open() as connection:
            connection.execute("""
                INSERT INTO thread_sessions (profile_id, thread_id, session_id, origin, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, thread_id) DO UPDATE SET session_id=excluded.session_id,
                  origin=excluded.origin
            """, (profile_id, thread_id, session_id, origin, int(time.time())))


def parse_profile_hashes(text: str) -> dict[str, str]:
    """Per-profile token hashes: one ``<profile> <sha256-hex>`` per line; ``#`` starts a comment.
    Each token is scoped to exactly one profile, so a hash may appear only once."""
    hashes: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not PROFILE_RE.fullmatch(parts[0]) or not re.fullmatch(r"[0-9a-f]{64}", parts[1].lower()):
            raise ValueError(f"profile token file line {number}: expected '<profile> <64-hex sha256>'")
        name, digest = parts[0], parts[1].lower()
        if name in hashes:
            raise ValueError(f"profile token file line {number}: profile listed twice")
        if digest in hashes.values():
            raise ValueError(f"profile token file line {number}: the same token hash is used for two profiles")
        hashes[name] = digest
    return hashes


def bearer_scope(header: str, config: dict) -> tuple[str, ...]:
    """Profiles the presented bearer may use (empty tuple = unauthorized).
    With per-profile hashes a token reaches only its own profile; a single bridge-wide token or
    hash (the older configuration) reaches every exposed profile."""
    import hmac
    profiles = tuple(config.get("profiles") or ())
    hashes: dict = config.get("profile_hashes") or {}
    if not hashes:
        return profiles if bearer_ok(header, config) else ()
    if not header.startswith("Bearer ") or len(header) > 4096:
        return ()
    digest = hashlib.sha256(header[len("Bearer "):].encode("utf-8")).hexdigest()
    matched = ()
    for name, expected in hashes.items():  # compare against every entry: no early exit
        if hmac.compare_digest(digest, expected) and name in profiles:
            matched = (name,)
    return matched


def bearer_ok(header: str, config: dict) -> bool:
    """Timing-safe bearer check. With a hash-only config the bridge never holds the token itself:
    it compares SHA-256 of the presented bearer with the stored hex digest."""
    import hmac
    if not header.startswith("Bearer ") or len(header) > 4096:
        return False
    presented = header[len("Bearer "):]
    expected_hash = config.get("token_sha256") or ""
    if expected_hash:
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, expected_hash)
    token = config.get("token") or ""
    return bool(token) and hmac.compare_digest(presented, token)


def stable_id(*parts: str, length: int = 32) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:length]


class HermesConnector:
    """The only code that imports Hermes. Tests replace it with a fake."""

    def __init__(self, hermes_home: str, agent_dir: str):
        self.root = Path(hermes_home)
        if agent_dir and agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)

    def profile_home(self, name: str) -> Path:
        return self.root if name == "default" else self.root / "profiles" / name

    def discover(self, name: str):
        from hermes_cli.gateway_runtime import discover_gateway_endpoint  # never spawns
        return discover_gateway_endpoint(self.profile_home(name), timeout=3)

    def connect(self, endpoint):
        from hermes_cli.gateway_client import connect_gateway
        return connect_gateway(endpoint)


async def list_profiles(connector, allowed: tuple[str, ...]) -> dict:
    """Served, allowed profiles with stable identity. Read-only (discover + describe/list)."""
    root = connector.discover("default")
    if root.endpoint is None:
        return {"online": False, "state": root.state, "reason": root.reason_code, "profiles": []}
    async with connector.connect(root.endpoint) as client:
        roster = await client.rpc("profiles.list")
    install_id = roster.get("install_id") or ""
    profiles = []
    for row in roster.get("profiles") or []:
        name = row.get("name")
        if not isinstance(name, str) or name not in allowed:
            continue
        found = connector.discover(name)
        endpoint = found.endpoint
        profiles.append({
            "name": name,
            "display_name": row.get("display_name") or name,
            "install_id": install_id,
            "profile_id": endpoint.profile_id if endpoint else str(connector.profile_home(name)),
            "state": found.state,
            "instance_id": endpoint.instance_id if endpoint else None,
            "ag_ui_path": f"/profiles/{name}/ag-ui",
        })
    return {"online": True, "install_id": install_id, "profiles": profiles}


class Turn:
    """One OpenBot run against one Hermes session; emits AG-UI events through ``emit``."""

    def __init__(self, client, emit, *, session_id: str, input_id: str, text: str,
                 timeout: float, approval_wait: float):
        self.client, self.emit = client, emit
        self.session_id, self.input_id, self.text = session_id, input_id, text
        self.timeout, self.approval_wait = timeout, approval_wait
        self.admission: str | None = None
        self.held: list[tuple[str, str | None, dict]] = []
        self.streamed = ""
        self.message_id = str(uuid.uuid4())
        self.started_text = False
        self.final: str | None = None
        self.failure: str | None = None

    def _text(self, delta: str) -> None:
        if not delta:
            return
        if not self.started_text:
            self.emit({"type": "TEXT_MESSAGE_START", "messageId": self.message_id, "role": "assistant"})
            self.started_text = True
        self.streamed += delta
        self.emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": self.message_id, "delta": delta})

    async def _deny(self, payload: dict) -> None:
        choices = [c for c in payload.get("choices") or [] if isinstance(c, str)]
        name = payload.get("tool_name") or payload.get("command") or "an action"
        self.emit({"type": "CUSTOM", "name": "agent-fabric.approval",
                   "value": {"prompt_id": payload.get("prompt_id"), "tool": str(name)[:200],
                             "choices": choices[:10], "decision": "deny"}})
        self._text(f"\n\n[Agent Fabric: Hermes asked for approval of {str(name)[:120]}. "
                   "Approvals are not available from OpenBot yet, so it was denied.]\n\n")
        if self.approval_wait > 0:
            await asyncio.sleep(self.approval_wait)
        deny = next((c for c in choices if c.lower() in DENY_CHOICES), None)
        if deny is None:
            return  # No deny choice offered: Hermes' own approval timeout resolves it as deny.
        await self.client.rpc("approval.respond", session_id=self.session_id,
                              execution_generation=payload.get("execution_generation"),
                              prompt_id=payload.get("prompt_id"), choice=deny)

    async def _clarify(self, payload: dict) -> None:
        self.emit({"type": "CUSTOM", "name": "agent-fabric.clarify",
                   "value": {"prompt_id": payload.get("prompt_id"), "decision": "skipped"}})
        await self.client.rpc("clarify.respond", session_id=self.session_id,
                              execution_generation=payload.get("execution_generation"),
                              prompt_id=payload.get("prompt_id"), answer="")

    async def handle(self, kind: str, admission: str | None, payload: dict) -> None:
        # Session-blocking controls are answered whichever admission raised them.
        if kind == "approval.request" and payload.get("kind", "approval") == "approval":
            await self._deny(payload)
            return
        if kind == "clarify.request" or (kind == "approval.request" and payload.get("kind") == "clarify"):
            await self._clarify(payload)
            return
        if admission and admission != self.admission:
            return
        if kind == "message.delta":
            text = payload.get("text") or payload.get("delta") or ""
            if isinstance(text, str):
                self._text(text)
        elif kind == "tool.start":
            # CUSTOM, not AG-UI TOOL_CALL_*: OpenBot would treat those as tools for it to run.
            self.emit({"type": "CUSTOM", "name": "agent-fabric.tool",
                       "value": {"state": "started", "tool": str(payload.get("tool_name")
                                                                 or payload.get("name") or "tool")[:120]}})
        elif kind == "tool.complete":
            self.emit({"type": "CUSTOM", "name": "agent-fabric.tool",
                       "value": {"state": "completed", "tool": str(payload.get("tool_name")
                                                                   or payload.get("name") or "tool")[:120]}})
        elif kind == "message.complete":
            text = payload.get("text") or payload.get("content") or ""
            text = text if isinstance(text, str) else ""
            if not self.streamed:
                self._text(text)
            elif text.startswith(self.streamed):
                self._text(text[len(self.streamed):])
            status = payload.get("status")
            if status in ("error", "failed") or payload.get("error"):
                self.failure = str(payload.get("failure_reason") or payload.get("error") or status)[:300]
            self.final = text or self.streamed

    async def run(self) -> str:
        receipt = await self.client.rpc("prompt.submit", session_id=self.session_id,
                                        input_id=self.input_id, text=self.text)
        self.admission = (receipt or {}).get("admission_id")
        if not self.admission:
            raise RuntimeError("Hermes did not admit the prompt")
        for kind, admission, payload in self.held:
            await self.handle(kind, admission, payload)
        deadline = time.monotonic() + self.timeout
        last_beat = time.monotonic()
        while self.final is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes did not finish within the bridge timeout")
            try:
                event = await asyncio.wait_for(self.client.events.get(), min(remaining, HEARTBEAT_SECONDS))
            except asyncio.TimeoutError:
                self.emit({"type": "CUSTOM", "name": "agent-fabric.progress", "value": {"state": "working"}})
                last_beat = time.monotonic()
                continue
            if isinstance(event, Exception):
                raise RuntimeError("Lost the Hermes gateway connection") from event
            params = event.get("params", {}) if isinstance(event, dict) else {}
            if params.get("session_id") != self.session_id:
                continue
            kind, payload = params.get("type"), params.get("payload") or {}
            admission = params.get("admission_id") or payload.get("admission_id")
            await self.handle(kind, admission, payload)
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                self.emit({"type": "CUSTOM", "name": "agent-fabric.progress", "value": {"state": "working"}})
                last_beat = time.monotonic()
        if self.started_text:
            self.emit({"type": "TEXT_MESSAGE_END", "messageId": self.message_id})
        if self.failure:
            raise RuntimeError(f"Hermes turn failed: {self.failure}")
        return self.final


async def follow_profile_params(client) -> dict:
    """``follow_profile_config=True`` when the gateway advertises it for session.create: the session
    then re-reads the profile's current model/provider sections each turn (Hermes' own "Bot Chat"
    behaviour), so a profile model change applies without restarting the gateway."""
    try:
        described = await client.rpc("runtime.describe")
    except Exception:
        return {}
    parameters = (described.get("session_create") or {}).get("parameters") or []
    return {"follow_profile_config": True} if "follow_profile_config" in parameters else {}


async def open_session(client, ledger: HermesLedger, profile_id: str, thread_id: str,
                       *, allow_native: bool = False, follow_profile: bool = True) -> tuple[str, bool]:
    """The Hermes session this OpenBot thread owns: resume ours, or create a new one.

    A thread an operator explicitly bound to an existing native session (origin 'bound',
    e.g. one a TUI also opens by id) is resumed only when native attachment is enabled
    (HERMES_ALLOW_NATIVE_ATTACH=1, off by default). Nothing here discovers or adopts other sessions."""
    mapping = ledger.thread_session(profile_id, thread_id)
    known = None
    if mapping:
        known, origin = mapping
        if origin == "bound" and not allow_native:
            raise PermissionError("This thread is bound to a native Hermes session; native attach is disabled")
    if known:
        try:
            snapshot = await client.rpc("session.resume", session_id=known, omit_messages=True,
                                        defer_history=True)
            return snapshot.get("stored_session_id") or known, False
        except Exception as exc:  # A pruned/deleted session: start a new one for this thread.
            if "not_found" not in str(exc) or mapping[1] == "bound":
                raise
    extra = await follow_profile_params(client) if follow_profile else {}
    snapshot = await client.rpc("session.create", request_id=stable_id("create", profile_id, thread_id),
                                source=SESSION_SOURCE, title=f"OpenBot {thread_id[:12]}", **extra)
    session_id = snapshot.get("stored_session_id") or snapshot.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Hermes did not return a session id")
    ledger.map_thread(profile_id, thread_id, session_id)
    return session_id, True


class HermesHandler(bridge.Handler):
    server_version = f"AgentFabricHermes/{VERSION}"
    config: dict = {}
    connector: HermesConnector | None = None
    ledger: HermesLedger | None = None

    def scope(self) -> tuple[str, ...]:
        return bearer_scope(self.headers.get("Authorization", ""), self.config)

    def profile_id_scope(self, scope: tuple[str, ...]) -> set[str]:
        return {str(self.connector.profile_home(name)) for name in scope}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.json_response(200, {"ok": True, "version": VERSION, "mode": "hermes"})
            return
        scope = self.scope()
        if not scope:
            self.json_response(401, {"error": "unauthorized"})
            return
        if path == "/agents":
            try:
                listing = asyncio.run(list_profiles(self.connector, scope))
            except Exception as exc:
                self.json_response(503, {"error": "Hermes gateway unavailable", "detail": type(exc).__name__})
                return
            self.json_response(200, {"version": VERSION, "backend": "hermes", **listing})
            return
        if path.startswith("/requests/"):
            key = path.removeprefix("/requests/")
            record = self.ledger.get(key) if re.fullmatch(r"[a-f0-9]{64}", key) else None
            if record is not None and record["session"] not in self.profile_id_scope(scope):
                record = None  # another profile's request: indistinguishable from unknown
            if record is None:
                self.json_response(404, {"error": "not_found"})
            else:
                self.json_response(200, {"id": key, "state": record["state"],
                                         "reply": record["reply"] if record["state"] == "finished" else None})
            return
        self.json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:
        scope = self.scope()
        if not scope:
            self.json_response(401, {"error": "unauthorized"})
            return
        match = ROUTE_RE.fullmatch(urlsplit(self.path).path)
        if not match or match.group(1) not in scope:
            self.json_response(404, {"error": "Unknown or not allowed Hermes profile"})
            return
        name = match.group(1)
        try:
            body = json.loads(self.read_request_body())
            text, attachments = bridge.parse_user_message(body)
        except bridge.PayloadTooLarge as exc:
            self.json_response(413, {"error": str(exc), "code": "request_too_large"})
            return
        except (ValueError, json.JSONDecodeError, TypeError, TimeoutError) as exc:
            self.json_response(400, {"error": str(exc)})
            return
        if attachments:
            self.json_response(400, {"error": "Attachments are not supported by the Hermes backend yet",
                                     "code": "attachments_unsupported"})
            return
        found = self.connector.discover(name)
        if found.endpoint is None or found.state != "ready":
            self.json_response(503, {"error": f"Hermes gateway for this profile is {found.state}",
                                     "code": found.reason_code or "not_ready"})
            return
        endpoint = found.endpoint
        thread_id = str(body.get("threadId") or uuid.uuid4())
        run_id = str(body.get("runId") or uuid.uuid4())
        key = RequestLedger.key(endpoint.profile_id, "hermes", thread_id, run_id)
        fingerprint = RequestLedger.fingerprint(text, [])
        previous = self.ledger.get(key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                self.json_response(409, {"error": "Run identifier reused with different content"})
                return
            self.start_stream(thread_id, run_id)
            if previous["state"] == "finished":
                self.send_reply(str(uuid.uuid4()), thread_id, run_id, previous["reply"] or "")
            else:
                self.send_event({"type": "RUN_ERROR", "message":
                                 f"Request {key} is already {previous['state']}. No duplicate prompt sent."})
            return
        self.ledger.reserve(key, endpoint.profile_id, "hermes", fingerprint, run_id)
        self.start_stream(thread_id, run_id)
        detached = {"value": False}

        def emit(event: dict) -> None:
            if detached["value"]:
                return
            try:
                self.send_event(event)
            except (BrokenPipeError, ConnectionResetError):
                detached["value"] = True  # The turn continues; the ledger keeps the result.

        async def work() -> str:
            async with self.connector.connect(endpoint) as client:
                session_id, created = await open_session(client, self.ledger, endpoint.profile_id, thread_id,
                                                         allow_native=self.config.get("allow_native", False),
                                                         follow_profile=self.config.get("follow_profile", True))
                emit({"type": "CUSTOM", "name": "agent-fabric.progress",
                      "value": {"state": "submitted", "session": "created" if created else "resumed"}})
                turn = Turn(client, emit, session_id=session_id,
                            input_id=stable_id("input", endpoint.profile_id, thread_id, run_id),
                            text=text, timeout=self.config["timeout"],
                            approval_wait=self.config["approval_wait"])
                return await turn.run()

        try:
            reply = asyncio.run(work())
        except Exception as exc:
            self.ledger.uncertain(key)
            self.log_message("Hermes run failed: %s", type(exc).__name__)
            emit({"type": "RUN_ERROR", "message": f"Hermes run did not complete ({type(exc).__name__}). "
                                                  f"No duplicate prompt will be sent; check /requests/{key}."})
            return
        self.ledger.finish(key, reply)
        emit({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})


def main() -> None:
    try:
        config = env_config()
    except ValueError as exc:
        sys.exit(str(exc))
    if config["profile_hashes"]:
        if config["token"] or config["token_sha256"]:
            sys.exit("Configure per-profile token hashes or a bridge-wide token, not both")
        unknown = sorted(set(config["profile_hashes"]) - set(config["profiles"]))
        if unknown:
            sys.exit("OPENBOT_BRIDGE_PROFILE_TOKENS_FILE names profiles that are not in HERMES_PROFILES")
    elif config["token_sha256"]:
        if config["token"]:
            sys.exit("Configure either a token or a token SHA-256, not both")
        if not re.fullmatch(r"[0-9a-f]{64}", config["token_sha256"]):
            sys.exit("OPENBOT_BRIDGE_TOKEN_SHA256(_FILE) must be a 64-character hex SHA-256")
    elif not config["token"] or len(config["token"]) < 24:
        sys.exit("Set OPENBOT_BRIDGE_PROFILE_TOKENS_FILE (per-profile SHA-256 hashes), "
                 "OPENBOT_BRIDGE_TOKEN(_FILE) to a random secret of at least 24 characters, "
                 "or OPENBOT_BRIDGE_TOKEN_SHA256(_FILE) to the SHA-256 of one")
    try:
        check_bind_host(config["host"])
    except ValueError as exc:
        sys.exit(str(exc))
    if not config["profiles"] or any(not PROFILE_RE.fullmatch(p) for p in config["profiles"]):
        sys.exit("Set HERMES_PROFILES to the comma-separated profiles to expose")
    if not config["ledger"]:
        sys.exit("Set OPENBOT_BRIDGE_LEDGER_PATH to a private absolute path")
    if not 30 <= config["timeout"] <= 3600:
        sys.exit("OPENBOT_BRIDGE_TIMEOUT_SECONDS must be between 30 and 3600")
    if not 0 <= config["approval_wait"] <= 600:
        sys.exit("HERMES_APPROVAL_WAIT_SECONDS must be between 0 and 600")
    os.umask(0o077)
    HermesHandler.config = config
    HermesHandler.ledger = HermesLedger(config["ledger"])
    HermesHandler.ledger.trim_finished()
    HermesHandler.connector = HermesConnector(config["hermes_home"], config["agent_dir"])
    server = ThreadingHTTPServer((config["host"], config["port"]), HermesHandler)
    print(f"Agent Fabric Hermes bridge {VERSION} ready on {config['host']}:{config['port']} "
          f"| profiles={len(config['profiles'])} "
          f"| auth={'per-profile' if config['profile_hashes'] else 'bridge-wide'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
