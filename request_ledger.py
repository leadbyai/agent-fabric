"""Small crash-safe run ledger for an existing Herdr gateway.

The database intentionally stores a request fingerprint, not the user's prompt
or attachment bytes. A run is reserved before the non-transactional Herdr CLI
call, so a restart can leave an uncertain run but can never silently resend it.

v0.7.1: a request is recorded as ``queued`` as soon as it is accepted, before
it waits for a busy agent. States:

  queued     accepted, waiting for the agent; prompt NOT sent
  submitted  Herdr prompt is being sent / agent is working
  finished   reply stored
  uncertain  failure after submission; the prompt may have been sent
  failed     definitely never sent (wait expired, agent changed, client left,
             gateway restarted while queued); ``reason`` says why. A retry with
             the same run identifier may be queued again.
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

    SCHEMA = """
                CREATE TABLE IF NOT EXISTS requests (
                  key TEXT PRIMARY KEY,
                  session TEXT NOT NULL,
                  pane TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  marker TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN
                    ('queued', 'submitted', 'finished', 'uncertain', 'failed')),
                  reply TEXT,
                  updated_at INTEGER NOT NULL,
                  queued_at INTEGER,
                  reason TEXT
                )
            """

    def _initialize(self) -> None:
        with self._open() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()
            if row is not None and "'queued'" not in row["sql"]:
                self._migrate_v070(connection)
            connection.execute(self.SCHEMA)
        os.chmod(self.path, 0o600)

    def _migrate_v070(self, connection: sqlite3.Connection) -> None:
        """Rebuild the v0.6/0.7.0 table: SQLite cannot widen a CHECK in place.

        Atomic: on any error the transaction rolls back and the old table stays.
        Old gateway code can still read the migrated table (rollback-safe);
        it treats queued/failed rows as "already submitted" and fails closed.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(self.SCHEMA.replace("EXISTS requests", "EXISTS requests_v071"))
            connection.execute("""
                INSERT INTO requests_v071
                  (key, session, pane, fingerprint, marker, state, reply, updated_at)
                SELECT key, session, pane, fingerprint, marker, state, reply, updated_at
                FROM requests
            """)
            before = connection.execute("SELECT count(*) FROM requests").fetchone()[0]
            after = connection.execute("SELECT count(*) FROM requests_v071").fetchone()[0]
            if before != after:
                raise sqlite3.DatabaseError("ledger migration row count mismatch")
            connection.execute("DROP TABLE requests")
            connection.execute("ALTER TABLE requests_v071 RENAME TO requests")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def key(session: str, pane: str, thread_id: str, run_id: str) -> str:
        fields = json.dumps([session, pane, thread_id, run_id], separators=(",", ":"))
        return hashlib.sha256(fields.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(text: str, attachments: list[tuple[str, str, bytes]]) -> str:
        fields = [text, [(name, mime, hashlib.sha256(data).hexdigest())
                         for name, mime, data in attachments]]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode("utf-8")).hexdigest()

    def enqueue(self, key: str, session: str, pane: str,
                fingerprint: str, marker: str) -> dict:
        """Durably accept a request before it waits. Returns the row + ``created``.

        A previous ``failed`` row (provably never sent) is re-queued; any other
        existing row is returned unchanged so the caller can replay or refuse.
        """
        now = int(time.time())
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            if row is not None and row["fingerprint"] != fingerprint:
                raise ValueError("Run identifier was reused for different message content")
            if row is None:
                connection.execute("""
                    INSERT INTO requests
                      (key, session, pane, fingerprint, marker, state, updated_at, queued_at)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """, (key, session, pane, fingerprint, marker, now, now))
                created = True
            elif row["state"] == "failed":
                connection.execute("""
                    UPDATE requests SET state='queued', marker=?, reply=NULL, reason=NULL,
                      updated_at=?, queued_at=? WHERE key=? AND state='failed'
                """, (marker, now, now, key))
                created = True
            else:
                created = False
            record = dict(connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone())
            record["created"] = created
            return record

    def reserve(self, key: str, session: str, pane: str,
                fingerprint: str, marker: str) -> dict:
        """Mark a request ``submitted`` immediately before the Herdr prompt.

        ``created`` is True when this call owns the submission: a new row, or
        this request's own ``queued`` row (same marker) moving to submitted.
        """
        now = int(time.time())
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT OR IGNORE INTO requests
                  (key, session, pane, fingerprint, marker, state, updated_at)
                VALUES (?, ?, ?, ?, ?, 'submitted', ?)
            """, (key, session, pane, fingerprint, marker, now))
            inserted = connection.execute("SELECT changes()").fetchone()[0] == 1
            promoted = False
            if not inserted:
                promoted = connection.execute("""
                    UPDATE requests SET state='submitted', updated_at=?
                    WHERE key=? AND state='queued' AND marker=? AND fingerprint=?
                """, (now, key, marker, fingerprint)).rowcount == 1
            row = connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
            assert row is not None
            record = dict(row)
            if record["fingerprint"] != fingerprint:
                raise ValueError("Run identifier was reused for different message content")
            record["created"] = inserted or promoted
            return record

    def fail(self, key: str, reason: str) -> None:
        """Record that a QUEUED request was never sent. Never touches submitted rows."""
        with self._open() as connection:
            connection.execute("""
                UPDATE requests SET state='failed', reason=?, updated_at=?
                WHERE key=? AND state='queued'
            """, (reason[:300], int(time.time()), key))

    def fail_abandoned_queue(self, reason: str = "gateway_restarted_before_submit") -> int:
        """At startup: no waiter survives a restart, so every ``queued`` row is
        definitely unsent. Mark them failed (durable, visible) instead of losing them."""
        with self._open() as connection:
            return connection.execute("""
                UPDATE requests SET state='failed', reason=?, updated_at=?
                WHERE state='queued'
            """, (reason, int(time.time()))).rowcount

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

    def queued(self) -> list[dict]:
        with self._open() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT key AS id, session, pane, queued_at, updated_at FROM requests
                WHERE state='queued' ORDER BY queued_at ASC LIMIT 100
            """).fetchall()]

    def recent_failures(self, within_seconds: int = 86400) -> list[dict]:
        with self._open() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT key AS id, session, pane, reason, updated_at FROM requests
                WHERE state='failed' AND updated_at>=? ORDER BY updated_at DESC LIMIT 100
            """, (int(time.time()) - within_seconds,)).fetchall()]

    def trim_finished(self, older_than_seconds: int = 7 * 86400) -> None:
        # Never trim queued/submitted/uncertain records: those are duplicate guards.
        # Failed rows were never sent, so they age out like finished ones.
        with self._open() as connection:
            connection.execute("DELETE FROM requests WHERE state IN ('finished', 'failed') AND updated_at<?",
                               (int(time.time()) - older_than_seconds,))
