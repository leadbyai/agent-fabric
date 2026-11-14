"""Mirror an explicitly bound native Hermes CLI/TUI conversation to OpenBot.

Use durable transcript rows as the read cursor. The gateways' in-memory replay
sequence resets on restart and cannot recover a truncated replay on its own.
This watcher opens the Hermes database read-only and never claims a session.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

from external_sync_events import ImportEvent, redact_content
from external_sync_watcher import ImportClient, ImportFailed, SourceUnavailable


def _lineage(db, root_id):
    root = db.execute(
        "SELECT id, source, COALESCE(created_source, source) AS origin, end_reason "
        "FROM sessions WHERE id=?", (root_id,),
    ).fetchone()
    if root is None or root["origin"] not in ("cli", "tui"):
        raise SourceUnavailable("binding does not identify a native Hermes session")
    origin = root["origin"]
    chain = [root["id"]]
    while root["end_reason"] == "compression":
        children = db.execute(
            "SELECT id, source, end_reason FROM sessions WHERE parent_session_id=? "
            "AND COALESCE(created_source, source)=? LIMIT 2",
            (chain[-1], origin),
        ).fetchall()
        if not children:
            break
        if len(children) != 1 or children[0]["id"] in chain or len(chain) >= 128:
            raise SourceUnavailable("ambiguous Hermes compression lineage")
        root = children[0]
        chain.append(root["id"])
    return chain


def _event(row, install_id, profile_id, root_id):
    content = row["content"]
    if not isinstance(content, str):
        return None
    cleaned = redact_content(content).strip()
    if not cleaned:
        return None
    uid = row["message_uid"]
    if not uid:
        raise SourceUnavailable("Hermes transcript row has no durable identity")
    key = json.dumps(["hermes", install_id, profile_id, root_id, uid], separators=(",", ":"))
    return ImportEvent(
        "afx-" + hashlib.sha256(key.encode()).hexdigest()[:32],
        uid, root_id, "human" if row["role"] == "user" else "assistant",
        "User (Hermes)" if row["role"] == "user" else "Hermes",
        datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat(), cleaned,
    )


def sync_hermes(source_path, ledger_path, client, *, bot_id, install_id, profile_id, root_id):
    """Advance the native row position only after all corresponding IDs are acknowledged."""
    binding = (bot_id, install_id, profile_id, root_id)
    source_path = source_path.resolve(strict=True)
    stat = source_path.stat()
    source_identity = f"{stat.st_dev}:{stat.st_ino}"
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        chain = _lineage(source, root_id)
        placeholders = ",".join("?" for _ in chain)
        tip = source.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id IN ({placeholders})",
            chain,
        ).fetchone()[0]
        with closing(sqlite3.connect(ledger_path)) as ledger, ledger:
            ledger.execute(
                "CREATE TABLE IF NOT EXISTS hermes_cursor ("
                "bot TEXT, install TEXT, profile TEXT, root TEXT, identity TEXT, offset INTEGER, "
                "PRIMARY KEY (bot, install, profile, root))"
            )
            ledger.execute(
                "INSERT OR IGNORE INTO hermes_cursor VALUES (?, ?, ?, ?, ?, ?)",
                (*binding, source_identity, tip),
            )
            identity, offset = ledger.execute(
                "SELECT identity, offset FROM hermes_cursor "
                "WHERE bot=? AND install=? AND profile=? AND root=?", binding,
            ).fetchone()
        if identity != source_identity:
            raise SourceUnavailable("Hermes transcript database was replaced")
        rows = source.execute(
            f"SELECT m.id, m.role, m.content, m.timestamp, m.message_uid FROM messages m "
            f"WHERE m.session_id IN ({placeholders}) AND m.id>? AND (m.active=1 OR m.compacted=1) "
            f"AND m._compressed_summary=0 AND m.role IN ('user','assistant') "
            f"AND m.display_kind IS NULL AND NOT EXISTS ("
            f"SELECT 1 FROM messages earlier WHERE earlier.session_id IN ({placeholders}) "
            f"AND earlier.message_uid=m.message_uid AND earlier.id<m.id) "
            f"ORDER BY m.id LIMIT 20", (*chain, offset, *chain),
        ).fetchall()
    if not rows:
        return False
    events = {}
    for row in rows:
        event = _event(row, install_id, profile_id, root_id)
        if event:
            events.setdefault(event.id, event)
    if events:
        client.deliver(bot_id, tuple(events.values()))
    with closing(sqlite3.connect(ledger_path)) as ledger, ledger:
        ledger.execute(
            "UPDATE hermes_cursor SET offset=? WHERE bot=? AND install=? AND profile=? "
            "AND root=? AND identity=? AND offset=?",
            (rows[-1]["id"], *binding, source_identity, offset),
        )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    if config.get("enabled") is not True:
        return
    args.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(args.ledger, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not args.ledger.is_file() or args.ledger.is_symlink():
            parser.error("Hermes cursor must be a regular file")
    else:
        os.close(descriptor)
    client = ImportClient(config["openbotOrigin"], os.environ["AGENT_FABRIC_IMPORT_SECRET"])
    delay = 2
    while True:
        try:
            progressed = sync_hermes(
                Path(config["hermesStateDb"]), args.ledger, client,
                bot_id=config["botId"], install_id=config["installId"],
                profile_id=config["profileId"], root_id=config["sessionId"],
            )
            delay = 2
            if not progressed:
                time.sleep(delay)
        except (OSError, sqlite3.DatabaseError, SourceUnavailable, ImportFailed, ValueError) as exc:
            logging.warning("Hermes sync paused: %s", type(exc).__name__)
            time.sleep(delay)
            delay = min(30, delay * 2)


if __name__ == "__main__":
    main()
