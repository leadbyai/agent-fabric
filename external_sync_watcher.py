"""Read opted-in Claude Code conversations and deliver them to OpenBot.

Run independently of the gateway. The config binds a single OpenBot Bot to a
specific Herdr pane and Claude config directory. The watcher never sends input
to Herdr or modifies a transcript.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from external_sync_cursor import CursorConflict, CursorLedger, SourceChanged


LOG = logging.getLogger(__name__)
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MAX_REQUEST_BYTES = 128_000


class SourceUnavailable(RuntimeError):
    """The bound source cannot currently be identified."""


class ImportFailed(RuntimeError):
    """The importer did not confirm every event."""


@dataclass(frozen=True)
class Binding:
    bot_id: str
    herdr_session: str
    pane_id: str
    claude_config_dir: Path
    cwd: Path
    native_session_id: str | None = None
    terminal_sender: str = "Derek (terminal)"


@dataclass(frozen=True)
class Source:
    session_id: str
    path: Path


def _process_start(proc: Path) -> str:
    return (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]


def resolve_claude(binding: Binding, *, herdr_bin: str = "herdr") -> Source:
    """Require the live foreground process and current PID metadata to agree."""
    try:
        result = subprocess.run(
            [herdr_bin, "--session", binding.herdr_session, "pane",
             "process-info", "--pane", binding.pane_id],
            capture_output=True, check=True, timeout=5,
        )
        info = json.loads(result.stdout)["result"]["process_info"]
        if info["pane_id"] != binding.pane_id:
            raise SourceUnavailable("Herdr returned a different pane")
        processes = [entry for entry in info["foreground_processes"]
                     if entry["name"] == "claude"]
        if len(processes) != 1:
            raise SourceUnavailable("pane has no unique foreground Claude process")
        pid = processes[0]["pid"]
        if type(pid) is not int or pid <= 0:
            raise SourceUnavailable("invalid foreground process identity")
        proc = Path("/proc") / str(pid)
        started = _process_start(proc)
        environment = (proc / "environ").read_bytes().split(b"\x00")
        configured = next((os.fsdecode(item.split(b"=", 1)[1])
                           for item in environment if item.startswith(b"CLAUDE_CONFIG_DIR=")),
                          Path.home() / ".claude")
        if Path(configured).resolve() != binding.claude_config_dir:
            raise SourceUnavailable("Claude config directory differs from binding")
        metadata = json.loads(
            (binding.claude_config_dir / "sessions" / f"{pid}.json").read_bytes()
        )
        sid = metadata["sessionId"]
        cwd = Path(metadata["cwd"])
        if (metadata["pid"] != pid or str(metadata["procStart"]) != started
                or _process_start(proc) != started
                or cwd != binding.cwd or not SESSION_ID.fullmatch(sid)):
            raise SourceUnavailable("live Claude session identity differs from binding")
        if binding.native_session_id is not None and sid != binding.native_session_id:
            raise SourceUnavailable("Claude session differs from the pinned native session")
        projects = binding.claude_config_dir / "projects"
        directory = projects / re.sub(r"[^a-zA-Z0-9-]", "-", str(cwd))
        if directory.resolve().parent != projects or directory.is_symlink():
            raise SourceUnavailable("Claude project directory is not authoritative")
        return Source(sid, directory / f"{sid}.jsonl")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError,
            KeyError, IndexError) as exc:
        raise SourceUnavailable("Claude session metadata is unavailable") from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ImportClient:
    def __init__(self, origin: str, secret: str):
        url = urlsplit(origin)
        try:
            loopback = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (url.username or url.password or url.path not in ("", "/")
                or url.query or url.fragment or not url.hostname
                or (url.scheme != "https" and not (url.scheme == "http" and loopback))):
            raise ValueError("OpenBot origin must be HTTPS or loopback HTTP")
        if len(secret) < 32 or not secret.isascii() or not secret.isprintable():
            raise ValueError("invalid import secret")
        self.url = origin.rstrip("/") + "/api/agent-fabric/import"
        self.secret = secret
        self.opener = build_opener(NoRedirect())

    def deliver(self, bot_id, events):
        # Record limits and HTTP limits are independent: a single Claude line
        # may be split into multiple assistant and tool-summary messages.
        pending = []
        accepted = set()
        for event in events:
            if len(pending) >= 20:
                accepted.update(self._post(bot_id, pending))
                pending = []
            pending.append(event)
            if len(self._body(bot_id, pending)) > MAX_REQUEST_BYTES:
                if len(pending) == 1:
                    raise ImportFailed("event exceeds the import request limit")
                accepted.update(self._post(bot_id, pending[:-1]))
                pending = [event]
        if pending:
            accepted.update(self._post(bot_id, pending))
        if accepted != {event.id for event in events}:
            raise ImportFailed("OpenBot did not acknowledge the complete batch")

    @staticmethod
    def _body(bot_id, events):
        wire_events = []
        for event in events:
            stamp = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ImportFailed("event timestamp is missing its timezone")
            kind, role = {
                "human": ("human_text", "human"),
                "assistant": ("agent_text", "agent"),
                "peer": ("agent_text", "agent"),
                "tool_summary": ("tool_summary", "agent"),
            }[event.kind]
            wire_events.append({
                **asdict(event), "kind": kind,
                "sender": {"role": role, "name": event.sender},
                "timestamp": stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
        return json.dumps(
            {"botId": bot_id, "events": wire_events},
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")

    def _post(self, bot_id, events):
        request = Request(
            self.url, method="POST", data=self._body(bot_id, events),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.secret},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                ids = json.loads(response.read(16_384))["acceptedIds"]
            if not isinstance(ids, list) or set(ids) != {event.id for event in events}:
                raise ImportFailed("OpenBot did not acknowledge the request")
            return set(ids)
        except (OSError, TypeError, KeyError, ValueError) as exc:
            raise ImportFailed("OpenBot import unavailable or invalid") from exc


def sync_source(ledger, client, binding, source, *, validate=None) -> bool:
    cursor = ledger.open_or_initialize(
        bot_id=binding.bot_id, session_id=source.session_id, path=source.path,
    )
    batch = ledger.read_batch(cursor, path=source.path, limit=20)
    if batch.next_offset == cursor.offset:
        return False
    if validate is not None and validate() != source:
        raise SourceUnavailable("Claude session changed while reading")
    if batch.events:
        events = tuple(
            replace(event, sender=binding.terminal_sender)
            if event.kind == "human" else event
            for event in batch.events
        )
        client.deliver(binding.bot_id, events)
    ledger.acknowledge(batch)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--herdr", default="herdr")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    cfg = json.loads(args.config.read_bytes())
    if cfg.get("enabled") is not True:
        return
    binding = Binding(
        cfg["botId"], cfg["herdrSession"], cfg["paneId"],
        Path(cfg["claudeConfigDir"]).resolve(), Path(cfg["cwd"]).resolve(),
        cfg.get("nativeSessionId"),
        cfg.get("terminalSender", "Derek (terminal)"),
    )
    if (not re.fullmatch(r"[\w.-]{1,128}", binding.bot_id, flags=re.ASCII)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", binding.herdr_session)
            or not re.fullmatch(r"w[0-9A-Za-z]+:p[0-9A-Za-z]+", binding.pane_id)
            or (binding.native_session_id is not None and
                (not isinstance(binding.native_session_id, str)
                 or not SESSION_ID.fullmatch(binding.native_session_id)))):
        parser.error("invalid Bot or Herdr binding")
    if (not isinstance(binding.terminal_sender, str)
            or not binding.terminal_sender.strip()
            or len(binding.terminal_sender) > 80):
        parser.error("terminal sender must be a name of 1 to 80 characters")
    client = ImportClient(cfg["openbotOrigin"], os.environ["AGENT_FABRIC_IMPORT_SECRET"])
    ledger = CursorLedger(args.ledger)

    def resolve():
        return resolve_claude(binding, herdr_bin=args.herdr)

    delay = 2
    while True:
        try:
            progressed = sync_source(ledger, client, binding, resolve(), validate=resolve)
            delay = 2
            if not progressed:
                time.sleep(delay)
        except (SourceUnavailable, SourceChanged, CursorConflict,
                ImportFailed, OSError, ValueError) as error:
            # Never log transcript text, credentials or response bodies.
            LOG.warning("Sync paused: %s", type(error).__name__)
            time.sleep(delay)
            delay = min(delay * 2, 30)


if __name__ == "__main__":
    main()
