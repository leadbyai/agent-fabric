"""Durable, ACK-driven JSONL cursor for optional Agent Fabric transcript mirroring.

This module has no network or live-agent actions. It NEVER stores message
contents: only source position, native session identity and echo-guard state.
Call `acknowledge` only after OpenBot acknowledges every imported event in a
batch, or when a batch contained no eligible events.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import stat

from external_sync_events import ImportEvent, TranscriptState, classify_claude_records


MAX_JSONL_RECORD_BYTES = 512 * 1024


class SourceChanged(RuntimeError):
    """The source file was replaced or truncated; do not guess a new cursor."""


class CursorConflict(RuntimeError):
    """Another worker advanced this source; retry from its current cursor."""


@dataclass(frozen=True)
class Cursor:
    bot_id: str
    session_id: str
    device: int
    inode: int
    offset: int
    suppress_openbot_turn: bool


@dataclass(frozen=True)
class CandidateBatch:
    before: Cursor
    next_offset: int
    next_suppress_openbot_turn: bool
    events: tuple[ImportEvent, ...]


def _open_source(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("transcript source is not a regular file")
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _last_complete_record_boundary(source, size: int) -> int:
    """Do not start in the middle of an unfinished JSONL record."""
    if size == 0:
        return 0
    source.seek(size - 1)
    if source.read(1) == b"\n":
        return size
    pos = size - 1
    while pos > 0:
        pos -= 1
        source.seek(pos)
        if source.read(1) == b"\n":
            return pos + 1
    return 0


class CursorLedger:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.db_path.is_symlink():
            raise ValueError("cursor ledger may not be a symlink")
        if not self.db_path.exists():
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS transcript_cursor (
                    bot_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    offset INTEGER NOT NULL,
                    suppress_openbot_turn INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (bot_id, session_id)
                )"""
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def open_or_initialize(
        self, *, bot_id: str, session_id: str, path: Path, backfill: bool = False
    ) -> Cursor:
        if not bot_id or not session_id:
            raise ValueError("bot_id and session_id are required")
        # Verify the descriptor is a regular file and use the same identity
        # when choosing the starting boundary.
        source, info = _open_source(Path(path))
        with source:
            initial_offset = 0 if backfill else _last_complete_record_boundary(source, info.st_size)

        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO transcript_cursor "
                "(bot_id, session_id, device, inode, offset) VALUES (?, ?, ?, ?, ?)",
                (bot_id, session_id, info.st_dev, info.st_ino, initial_offset),
            )
            row = db.execute(
                "SELECT device, inode, offset, suppress_openbot_turn "
                "FROM transcript_cursor WHERE bot_id=? AND session_id=?",
                (bot_id, session_id),
            ).fetchone()
        cursor = Cursor(bot_id, session_id, row[0], row[1], row[2], bool(row[3]))
        if (cursor.device, cursor.inode) != (info.st_dev, info.st_ino):
            raise SourceChanged("transcript file identity changed")
        if info.st_size < cursor.offset:
            raise SourceChanged("transcript file was truncated")
        return cursor

    def read_batch(
        self, cursor: Cursor, *, path: Path, limit: int = 50
    ) -> CandidateBatch:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        source, info = _open_source(Path(path))
        with source:
            if (info.st_dev, info.st_ino) != (cursor.device, cursor.inode):
                raise SourceChanged("transcript was rotated or replaced")
            if info.st_size < cursor.offset:
                raise SourceChanged("transcript was truncated")

            source.seek(cursor.offset)
            records = []
            next_offset = cursor.offset
            for _ in range(limit):
                line = source.readline(MAX_JSONL_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_JSONL_RECORD_BYTES:
                    raise ValueError("oversized transcript record; refusing to advance")
                if not line.endswith(b"\n"):
                    break  # partial record: read it after the next append
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("malformed transcript record; refusing to advance") from error
                records.append(record)
                next_offset = source.tell()

        guard = TranscriptState(cursor.suppress_openbot_turn)
        events = tuple(
            classify_claude_records(records, session_id=cursor.session_id, state=guard)
        )
        return CandidateBatch(cursor, next_offset, guard.suppress_openbot_turn, events)

    def acknowledge(self, batch: CandidateBatch) -> Cursor:
        """Commit position only when the downstream import fully succeeded."""
        old = batch.before
        if batch.next_offset < old.offset:
            raise ValueError("cursor cannot move backwards")
        with self._connect() as db:
            updated = db.execute(
                "UPDATE transcript_cursor SET offset=?, suppress_openbot_turn=? "
                "WHERE bot_id=? AND session_id=? AND device=? AND inode=? "
                "AND offset=? AND suppress_openbot_turn=?",
                (
                    batch.next_offset,
                    int(batch.next_suppress_openbot_turn),
                    old.bot_id,
                    old.session_id,
                    old.device,
                    old.inode,
                    old.offset,
                    int(old.suppress_openbot_turn),
                ),
            ).rowcount
            if updated != 1:
                raise CursorConflict("cursor was advanced by another worker")
        return Cursor(
            old.bot_id, old.session_id, old.device, old.inode,
            batch.next_offset, batch.next_suppress_openbot_turn,
        )
