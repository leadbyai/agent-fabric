"""Small crash-safe run ledger for an existing Herdr gateway.

The database intentionally stores a request fingerprint, not the user's prompt
or attachment bytes. A run is reserved before the non-transactional Herdr CLI
call, so a restart can leave an uncertain run but can never silently resend it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


class RequestLedger:
    def __init__(self, path: str):
        destination = Path(path)
        if not destination.is_absolute() or destination.is_symlink():
            raise ValueError("Request ledger must be a non-symlink absolute path")
        if not destination.parent.is_dir() or destination.parent.is_symlink():
            raise ValueError("Request ledger directory must already exist")
        self.path = str(destination)
        self._initialize()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._open() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                  key TEXT PRIMARY KEY,
                  session TEXT NOT NULL,
                  pane TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  marker TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('submitted', 'finished', 'uncertain')),
                  reply TEXT,
                  updated_at INTEGER NOT NULL
                )
            """)
        os.chmod(self.path, 0o600)

    @staticmethod
    def key(session: str, pane: str, thread_id: str, run_id: str) -> str:
        fields = json.dumps([session, pane, thread_id, run_id], separators=(",", ":"))
        return hashlib.sha256(fields.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(text: str, attachments: list[tuple[str, str, bytes]]) -> str:
        fields = [text, [(name, mime, hashlib.sha256(data).hexdigest())
                         for name, mime, data in attachments]]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode("utf-8")).hexdigest()

    def reserve(self, key: str, session: str, pane: str,
                fingerprint: str, marker: str) -> dict:
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute("""
                INSERT OR IGNORE INTO requests
                  (key, session, pane, fingerprint, marker, state, updated_at)
                VALUES (?, ?, ?, ?, ?, 'submitted', ?)
            """, (key, session, pane, fingerprint, marker, int(time.time())))
            row = connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            assert row is not None
            record = dict(row)
            if record["fingerprint"] != fingerprint:
                raise ValueError("Run identifier was reused for different message content")
            record["created"] = inserted.rowcount == 1
            return record

    def get(self, key: str) -> dict | None:
        with self._open() as connection:
            row = connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def finish(self, key: str, reply: str) -> None:
        with self._open() as connection:
            connection.execute("""
                UPDATE requests SET state='finished', reply=?, updated_at=? WHERE key=?
            """, (reply, int(time.time()), key))

    def uncertain(self, key: str) -> None:
        with self._open() as connection:
            connection.execute("""
                UPDATE requests SET state='uncertain', updated_at=? WHERE key=?
                  AND state='submitted'
            """, (int(time.time()), key))

    def unresolved(self) -> list[dict]:
        with self._open() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT key AS id, session, pane, state, updated_at FROM requests
                WHERE state IN ('submitted', 'uncertain')
                ORDER BY updated_at ASC LIMIT 100
            """).fetchall()]

    def trim_finished(self, older_than_seconds: int = 7 * 86400) -> None:
        # Never trim submitted/uncertain records: those are duplicate guards.
        with self._open() as connection:
            connection.execute("DELETE FROM requests WHERE state='finished' AND updated_at<?",
                               (int(time.time()) - older_than_seconds,))
