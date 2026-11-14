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
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import bridge
from request_ledger import RequestLedger

VERSION = "0.8.0-hermes.6"
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
ROUTE_RE = re.compile(r"/profiles/([a-z0-9][a-z0-9_-]{0,63})/ag-ui\Z")
SESSION_SOURCE = "gui"
CLIENT_NAME = "agent-fabric"
HEARTBEAT_SECONDS = 15.0
DENY_CHOICES = ("deny", "reject", "no", "cancel", "abort")
# Toolsets a GUI-source session gets from its client surface even when every platform toolset is empty.
# Read from Hermes when it names them; this is the fallback and is always required as well.
CLIENT_SURFACE_TOOLSETS = frozenset({"project", "desktop_ui", "catalog"})
MESSAGING_ENV = re.compile(
    r"(DISCORD|TELEGRAM|SLACK|WHATSAPP|SIGNAL|MATRIX|MATTERMOST|EMAIL|IMAP|SMTP|SMS|TWILIO|IMESSAGE|"
    r"BLUEBUBBLES|HOMEASSISTANT|HASS|WEBHOOK|DINGTALK|FEISHU|LARK|WECOM|WEIXIN|LINE|TEAMS|GOOGLE_CHAT|QQ)_[A-Z0-9_]+\Z")
CREDENTIAL_FIELD = re.compile(r"token|secret|password|api_key|webhook_url|signing", re.IGNORECASE)
ISOLATED_TOOLSET_PLATFORMS = ("cli", "tui", "gui")


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
        "bound_thread": os.environ.get("HERMES_BOUND_THREAD_ID", "").strip(),
        "bound_session": os.environ.get("HERMES_BOUND_SESSION_ID", "").strip(),
        "media_root": os.environ.get("HERMES_BRIDGE_MEDIA_ROOT", "").strip(),
        "follow_profile": os.environ.get("HERMES_FOLLOW_PROFILE_CONFIG", "1").strip() != "0",
        "isolated": os.environ.get("HERMES_ISOLATED_PROFILE", "0").strip() == "1",
    }


def check_bind_host(host: str) -> None:
    """Loopback, or a Tailscale (CGNAT 100.64.0.0/10) address. Never a wildcard or public address."""
    address = ipaddress.ip_address(host)
    if not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
        raise ValueError("Bind to 127.0.0.1 or this host's Tailscale 100.64.0.0/10 address only")


def _leaves(value, prefix=""):
    """(dotted key, value) for every scalar under a nested dict."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, f"{prefix}{key}.")
    else:
        yield prefix.rstrip("."), value


def dotenv_names(path: Path) -> set[str]:
    """Names set to a non-empty value in a KEY=VALUE file (comments and blank values ignored)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    names = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if value.strip().strip("'\"").strip():
            names.add(key.strip())
    return names


def isolated_profile_problems(config: dict, env_names: set[str], client_surfaces: frozenset[str]) -> list[str]:
    """Why an EFFECTIVE Hermes profile config is not safe to serve as an isolated shared profile ([] when it
    is): no messaging platform or platform credential, memory and the user profile off, no toolset on the
    session platforms (client-surface toolsets disabled too), and no MCP servers. This is capability
    isolation only; keeping the owner's files and credentials out of reach (a separate OS user or a
    container with nothing of the owner's mounted) is the deployer's job and is not checked here."""
    problems = []
    for where, section in (("platforms", config.get("platforms")),
                           ("gateway.platforms", (config.get("gateway") or {}).get("platforms"))):
        for name, entry in (section if isinstance(section, dict) else {}).items():
            entry = entry if isinstance(entry, dict) else {}
            if entry.get("enabled"):
                problems.append(f"{where}.{name} is enabled")
            if any(value and CREDENTIAL_FIELD.search(key) for key, value in _leaves(entry)):
                problems.append(f"{where}.{name} holds a credential")
    problems += [f"messaging credential {name} is set" for name in sorted(env_names) if MESSAGING_ENV.fullmatch(name)]
    memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
    problems += [f"memory.{key} must be false" for key in ("memory_enabled", "user_profile_enabled")
                 if memory.get(key) is not False]
    toolsets = config.get("platform_toolsets") if isinstance(config.get("platform_toolsets"), dict) else {}
    problems += [f"platform_toolsets.{name} must be set to []" for name in ISOLATED_TOOLSET_PLATFORMS
                 if toolsets.get(name) != []]
    problems += [f"platform_toolsets.{name} must be empty" for name, value in sorted(toolsets.items())
                 if name not in ISOLATED_TOOLSET_PLATFORMS and value not in ([], None)]
    disabled = set(((config.get("agent") or {}).get("disabled_toolsets")) or [])
    if missing := sorted(set(client_surfaces) - disabled):
        problems.append("agent.disabled_toolsets must include " + ", ".join(missing))
    if config.get("mcp_servers"):
        problems.append("mcp_servers must be empty")
    return problems


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

    def session_owner(self, profile_id: str, session_id: str) -> str | None:
        """The thread already mapped to this session, if any."""
        with self._open() as connection:
            row = connection.execute(
                "SELECT thread_id FROM thread_sessions WHERE profile_id=? AND session_id=? LIMIT 1",
                (profile_id, session_id)).fetchone()
            return row["thread_id"] if row else None

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


@contextmanager
def staged_images(cache: Path, attachments):
    """Hermes copies these disposable files into durable, content-addressed ingress on admission."""
    with ExitStack() as cleanup:
        images = []
        for _, mime, data in attachments:
            suffix = "." + bridge.image_file_type(data)[0]
            with tempfile.NamedTemporaryFile(dir=cache, prefix="afx-", suffix=suffix, delete=False) as file:
                cleanup.callback(Path(file.name).unlink, missing_ok=True)
                file.write(data)
            images.append({"path": file.name, "mime": mime})
        yield images


class HermesConnector:
    """The only code that imports Hermes. Tests replace it with a fake."""

    def __init__(self, hermes_home: str, agent_dir: str, *, dedicated_profile: str | None = None):
        self.root = Path(hermes_home)
        self.dedicated_profile = dedicated_profile
        if agent_dir and agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)

    def isolated_profile_problems(self) -> list[str]:
        """Check this process's own profile (HERMES_HOME) with Hermes' effective config, defaults included."""
        if Path(os.environ.get("HERMES_HOME", "")).resolve() != self.root.resolve():
            return ["HERMES_HOME must be the isolated profile's own home"]
        if self.root.resolve() == (Path.home() / ".hermes").resolve():
            return ["an isolated profile cannot be the default Hermes home"]
        from hermes_cli.config import load_config
        try:
            from toolsets import CLIENT_SURFACE_TOOLSETS as surfaces
        except ImportError:
            surfaces = frozenset()
        names = {key for key, value in os.environ.items() if value} | dotenv_names(self.root / ".env")
        return isolated_profile_problems(load_config(), names, frozenset(surfaces) | CLIENT_SURFACE_TOOLSETS)

    def profile_home(self, name: str) -> Path:
        return self.root if name in ("default", self.dedicated_profile) else self.root / "profiles" / name

    def discover(self, name: str):
        from hermes_cli.gateway_runtime import discover_gateway_endpoint  # never spawns
        return discover_gateway_endpoint(self.profile_home(name), timeout=3)

    def connect(self, endpoint):
        from hermes_cli.gateway_client import connect_gateway
        return connect_gateway(endpoint)

    def image_cache(self):
        from gateway.platforms.base import get_image_cache_dir
        return get_image_cache_dir()


async def list_profiles(connector, allowed: tuple[str, ...]) -> dict:
    """Served, allowed profiles with stable identity. Read-only (discover + describe/list)."""
    root = connector.discover("default")
    if root.endpoint is None:
        return {"online": False, "state": root.state, "reason": root.reason_code, "profiles": []}
    async with connector.connect(root.endpoint) as client:
        roster = await client.rpc("profiles.list")
    install_id = roster.get("install_id") or ""
    rows = [row for row in roster.get("profiles") or [] if isinstance(row, dict)]
    dedicated = getattr(connector, "dedicated_profile", None)
    if dedicated and dedicated in allowed and not any(row.get("name") == dedicated for row in rows):
        # A dedicated bridge serves its one home under its own route name. A standalone home outside
        # <root>/profiles/ calls itself "default" in its roster, so that row is this route.
        rows = [{**row, "name": dedicated, "display_name": dedicated} for row in rows if row.get("name") == "default"]
    profiles = []
    for row in rows:
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
        # The TUI and bridge can share a session. Only the client that submitted a
        # turn should observe or answer its controls. Unstamped frames cannot prove ownership.
        if admission != self.admission:
            return
        if kind == "approval.request" and payload.get("kind", "approval") == "approval":
            await self._deny(payload)
            return
        if kind == "clarify.request" or (kind == "approval.request" and payload.get("kind") == "clarify"):
            await self._clarify(payload)
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

    async def run(self, attachments=(), *, outbox: Path | None = None) -> str:
        receipt = await self.client.rpc("prompt.submit", session_id=self.session_id,
                                        input_id=self.input_id, text=self.text,
                                        **({"attachments": attachments} if attachments else {}))
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
        try:
            if self.failure:
                raise RuntimeError(f"Hermes turn failed: {self.failure}")
            if outbox is not None:
                screenshots = bridge.persist_reply_images("", outbox, mode="inline")
                if screenshots:
                    preview = ("\n\n" if self.final else "") + screenshots
                    self._text(preview)
                    self.final += preview
            return self.final
        finally:
            if self.started_text:
                self.emit({"type": "TEXT_MESSAGE_END", "messageId": self.message_id})


def session_title(profile_id: str, thread_id: str) -> str:
    """A title no other thread shares. Hermes treats a session title as an identity, so two threads
    with the same title prefix would otherwise be resolved to one session."""
    return f"OpenBot {thread_id[:12]} {stable_id('title', profile_id, thread_id, length=10)}"


async def create_params(client, profile_id: str, thread_id: str, *, follow_profile: bool) -> dict:
    """Optional session.create parameters, each sent only when the gateway advertises it:
    ``follow_profile_config=True`` (the session re-reads the profile's current model/provider sections
    each turn, so a profile model change applies without restarting the gateway) and a ``client``
    label unique to this thread, which the created session echoes in ``info.launch_request``."""
    try:
        described = await client.rpc("runtime.describe")
    except Exception:
        return {}
    parameters = (described.get("session_create") or {}).get("parameters") or []
    extra: dict = {}
    if follow_profile and "follow_profile_config" in parameters:
        extra["follow_profile_config"] = True
    if "client" in parameters:
        extra["client"] = {"name": CLIENT_NAME, "version": stable_id("launch", profile_id, thread_id, length=24)}
    return extra


def foreign_session(snapshot: dict, sent: dict) -> str | None:
    """Why a session.create result is not the session this request created (None when it is).
    Title-as-identity could otherwise hand back an existing TUI, messaging or older session."""
    info = snapshot.get("info") if isinstance(snapshot.get("info"), dict) else {}
    source = info.get("source")
    if source is not None and source != SESSION_SOURCE:
        return f"source {source!r}"
    if "client" in sent:
        launch = info.get("launch_request")
        if not isinstance(launch, dict) or launch.get("client") != sent["client"]:
            return "launch request does not match"
    return None


async def open_session(client, ledger: HermesLedger, profile_id: str, thread_id: str,
                       *, allow_native: bool = False, follow_profile: bool = True,
                       bound_session: str = "") -> tuple[str, bool]:
    """The Hermes session this OpenBot thread owns: resume ours, or create a new one.

    A thread an operator explicitly bound to an existing native session (origin 'bound',
    e.g. one a TUI also opens by id) is resumed only when native attachment is enabled
    (HERMES_ALLOW_NATIVE_ATTACH=1, off by default). Nothing here discovers or adopts other sessions."""
    if bound_session:
        snapshot = await client.rpc("session.resume", session_id=bound_session, omit_messages=True,
                                    defer_history=True)
        if (snapshot.get("stored_session_id") or snapshot.get("session_id")) != bound_session:
            raise PermissionError("Hermes returned a different session than the configured binding")
        return bound_session, False
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
    extra = await create_params(client, profile_id, thread_id, follow_profile=follow_profile)
    snapshot = await client.rpc("session.create", request_id=stable_id("create", profile_id, thread_id),
                                source=SESSION_SOURCE, title=session_title(profile_id, thread_id), **extra)
    session_id = snapshot.get("stored_session_id") or snapshot.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Hermes did not return a session id")
    if (why := foreign_session(snapshot, extra)) is not None:
        # Never adopt a session this request did not create (e.g. one resolved by its title).
        raise RuntimeError(f"Hermes returned a session this bridge did not create ({why})")
    owner = ledger.session_owner(profile_id, session_id)
    if owner is not None and owner != thread_id:
        # Never let two OpenBot threads share one Hermes session (their contexts would mix).
        raise RuntimeError("Hermes returned a session that already belongs to another OpenBot thread")
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
        media_root = self.config.get("media_root")
        if attachments and not media_root:
            self.json_response(400, {"error": "Attachments are not supported by the Hermes backend yet",
                                     "code": "attachments_unsupported"})
            return
        if len(attachments) > 10 or any(
            (kind := bridge.image_file_type(data)) is None or kind[1] != mime
            for _, mime, data in attachments
        ):
            self.json_response(400, {"error": "Hermes accepts up to 10 PNG, JPEG, or WebP images"})
            return
        thread_id = str(body.get("threadId") or uuid.uuid4())
        if self.config.get("bound_thread") and thread_id != self.config["bound_thread"]:
            self.json_response(404, {"error": "not_found"})
            return
        found = self.connector.discover(name)
        if found.endpoint is None or found.state != "ready":
            self.json_response(503, {"error": f"Hermes gateway for this profile is {found.state}",
                                     "code": found.reason_code or "not_ready"})
            return
        endpoint = found.endpoint
        run_id = str(body.get("runId") or uuid.uuid4())
        key = RequestLedger.key(endpoint.profile_id, "hermes", thread_id, run_id)
        fingerprint = RequestLedger.fingerprint(text, attachments)
        try:
            previous = self.ledger.reserve(key, endpoint.profile_id, "hermes", fingerprint, run_id)
        except ValueError:
            self.json_response(409, {"error": "Run identifier reused with different content"})
            return
        if not previous["created"]:
            self.start_stream(thread_id, run_id)
            if previous["state"] == "finished":
                self.send_reply(str(uuid.uuid4()), thread_id, run_id, previous["reply"] or "")
            else:
                self.send_event({"type": "RUN_ERROR", "message":
                                 f"Request {key} is already {previous['state']}. No duplicate prompt sent."})
            return
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
                                                         follow_profile=self.config.get("follow_profile", True),
                                                         bound_session=self.config.get("bound_session", ""))
                emit({"type": "CUSTOM", "name": "agent-fabric.progress",
                      "value": {"state": "submitted", "session": "created" if created else "resumed"}})
                with ExitStack() as cleanup:
                    images = cleanup.enter_context(staged_images(self.connector.image_cache(), attachments)) if attachments else ()
                    outbox = None
                    prompt = text
                    if media_root:
                        outbox = Path(cleanup.enter_context(tempfile.TemporaryDirectory(dir=media_root)))
                        prompt += (
                            "\n\nOpenBot screenshot delivery: To include a screenshot with the reply, "
                            f"save a PNG, JPEG, or WebP file directly in {outbox.as_posix()}/. "
                            "OpenBot attaches files from this folder automatically."
                        )
                    turn = Turn(client, emit, session_id=session_id,
                                input_id=stable_id("input", endpoint.profile_id, thread_id, run_id),
                                text=prompt, timeout=self.config["timeout"],
                                approval_wait=self.config["approval_wait"])
                    return await turn.run(images, outbox=outbox)

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
    if config["isolated"]:
        if config["allow_native"] or config["bound_thread"] or config["bound_session"] or config["media_root"]:
            sys.exit("An isolated profile never attaches to native sessions, pins a thread, or carries media")
        if len(config["profiles"]) != 1 or not os.environ.get("HERMES_HOME", "").strip():
            sys.exit("An isolated profile bridge serves exactly one profile: its own explicit HERMES_HOME")
    if config["bound_thread"] or config["bound_session"]:
        if (not config["bound_thread"] or not config["bound_session"] or not config["allow_native"]
                or len(config["profiles"]) != 1):
            sys.exit("A pinned bridge requires native attach, a thread, a session and one dedicated HERMES_HOME")
    if config["media_root"]:
        root = Path(config["media_root"])
        if (not config["bound_session"] or not root.is_absolute() or not root.is_dir()
                or root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077):
            sys.exit("Hermes screenshots require a pinned session and a private media directory owned by the bridge user")
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
    HermesHandler.connector = HermesConnector(
        config["hermes_home"], config["agent_dir"],
        dedicated_profile=config["profiles"][0] if config["bound_thread"] or config["isolated"] else None,
    )
    if config["isolated"] and (problems := HermesHandler.connector.isolated_profile_problems()):
        sys.exit("Refusing an isolated profile that is not isolated: " + "; ".join(problems))
    server = ThreadingHTTPServer((config["host"], config["port"]), HermesHandler)
    print(f"Agent Fabric Hermes bridge {VERSION} ready on {config['host']}:{config['port']} "
          f"| profiles={len(config['profiles'])} "
          f"| auth={'per-profile' if config['profile_hashes'] else 'bridge-wide'}"
          f"{' | isolated' if config['isolated'] else ''}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
