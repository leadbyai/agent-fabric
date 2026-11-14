"""Exercise real HTTP delivery and durable offsets using synthetic sources."""

import contextlib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from external_sync_cursor import CursorLedger
from external_sync_backfill import backfill, _timestamp
from external_sync_hermes import sync_hermes
from external_sync_watcher import (
    Binding, ImportClient, ImportFailed, Source, SourceUnavailable, resolve_claude, sync_source,
)
from test_external_sync_cursor import SID, answer, prompt


SECRET = "synthetic-secret-" * 3


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.received = []
        self.stored = {}
        self.partial = False
        test = self

        class Recorder(BaseHTTPRequestHandler):
            def do_POST(self):
                test.assertEqual(self.path, "/api/agent-fabric/import")
                test.assertEqual(self.headers["Authorization"], "Bearer " + SECRET)
                batch = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.received.append(batch)
                events = batch["events"][:1] if test.partial else batch["events"]
                for event in events:
                    previous = test.stored.setdefault(event["id"], event)
                    test.assertEqual(previous, event)
                body = json.dumps({"acceptedIds": [event["id"] for event in events]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Recorder))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        self.client = ImportClient(f"http://127.0.0.1:{server.server_port}", SECRET)

    def test_partial_ack_replays_identical_ids_and_suppresses_openbot_echo(self):
        file = self.root / "test.jsonl"
        file.write_bytes(b"")
        binding = Binding("bot-fixture", "staging", "w1:p1", self.root, self.root)
        source = Source(SID, file)
        ledger_path = self.root / "cursor.db"
        ledger = CursorLedger(ledger_path)
        self.assertFalse(sync_source(ledger, self.client, binding, source))
        records = [
            prompt("human", "A staging prompt"),
            answer("reply", "A staging reply"),
            prompt("openbot", "OPENBOT_BEGIN_AABBCCDDEEFF0011"),
            answer("echo", "Already in OpenBot"),
        ]
        file.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
        self.partial = True
        with self.assertRaises(ImportFailed):
            sync_source(ledger, self.client, binding, source)
        self.partial = False
        restarted = CursorLedger(ledger_path)
        self.assertTrue(sync_source(restarted, self.client, binding, source))
        self.assertFalse(sync_source(restarted, self.client, binding, source))
        self.assertEqual(self.received[0], self.received[1])
        self.assertEqual([e["kind"] for e in self.stored.values()], ["human_text", "agent_text"])
        self.assertEqual({e["session_id"] for e in self.stored.values()}, {SID})

    def test_hermes_compression_and_replay_keep_one_conversation(self):
        file = self.root / "hermes.db"
        ledger = self.root / "cursor.db"
        binding = {"bot_id": "hermes-bot", "install_id": "install",
                   "profile_id": "profile", "root_id": "first"}
        with contextlib.closing(sqlite3.connect(file)) as db, db:
            db.executescript(
                "CREATE TABLE sessions(id, source, created_source, end_reason, parent_session_id);"
                "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id, role, content, timestamp, "
                "message_uid, active, _compressed_summary, display_kind, compacted);"
                "INSERT INTO sessions VALUES ('first','cli',NULL,NULL,NULL);"
                "INSERT INTO sessions VALUES ('unrelated','gui','gui',NULL,NULL);"
                "INSERT INTO messages VALUES (1,'first','user','pre-opt-in',100,'uid-1',1,0,NULL,0);"
            )
        self.assertFalse(sync_hermes(file, ledger, self.client, **binding))
        with contextlib.closing(sqlite3.connect(file)) as db, db:
            db.executescript(
                "UPDATE sessions SET end_reason='compression' WHERE id='first';"
                "INSERT INTO sessions VALUES ('next','cli','cli',NULL,'first');"
                "INSERT INTO messages VALUES (2,'first','user','a question',102,'uid-2',0,0,NULL,1);"
                "INSERT INTO messages VALUES (3,'next','user','a question',102,'uid-2',1,0,NULL,0);"
                "INSERT INTO messages VALUES (4,'unrelated','user','other',104,'uid-4',1,0,NULL,0);"
                "INSERT INTO messages VALUES (5,'next','assistant','an answer',105,'uid-5',1,0,NULL,0);"
                "INSERT INTO messages VALUES (6,'next','user','pre-opt-in',100,'uid-1',1,0,NULL,0);"
            )
        self.partial = True
        with self.assertRaises(ImportFailed):
            sync_hermes(file, ledger, self.client, **binding)
        self.partial = False
        self.assertTrue(sync_hermes(file, ledger, self.client, **binding))
        self.assertFalse(sync_hermes(file, ledger, self.client, **binding))
        self.assertEqual(self.received[0], self.received[1])
        self.assertEqual([e["content"] for e in self.stored.values()], ["a question", "an answer"])
        self.assertEqual({e["session_id"] for e in self.stored.values()}, {"first"})

    def test_historical_preview_then_apply_is_resumable_and_deduplicated(self):
        file = self.root / "history.jsonl"
        binding = Binding("bot-fixture", "staging", "w1:p1", self.root, self.root, SID, "Terminal")
        source = Source(SID, file)
        records = [
            {**prompt("old", "Historical prompt"), "timestamp": "2026-11-13T23:59:00Z"},
            {**answer("old-reply", "Historical reply"), "timestamp": "2026-11-13T23:59:01Z"},
            {**prompt("remote", "OPENBOT_BEGIN_AABBCCDDEEFF0011"),
             "timestamp": "2026-11-13T23:59:02Z"},
            {**answer("echo", "Stored elsewhere"), "timestamp": "2026-11-13T23:59:03Z"},
            prompt("live", "After cutoff"),
        ]
        file.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
        start = _timestamp("2026-11-13T00:00:00Z")
        stop = _timestamp("2026-11-14T00:00:00Z")
        self.assertEqual(backfill(binding, source, self.root / "preview.db", start, stop), (2, True))
        self.assertEqual(self.stored, {})
        ledger = self.root / "apply.db"
        self.partial = True
        with self.assertRaises(ImportFailed):
            backfill(binding, source, ledger, start, stop, client=self.client)
        self.partial = False
        self.assertEqual(backfill(binding, source, ledger, start, stop, client=self.client), (2, True))
        self.assertEqual(backfill(binding, source, ledger, start, stop, client=self.client), (0, True))
        self.assertEqual([e["content"] for e in self.stored.values()],
                         ["Historical prompt", "Historical reply"])
        self.assertEqual(next(iter(self.stored.values()))["sender"]["name"], "Terminal")

    def test_new_binding_can_import_its_history_without_rewinding_live_cursor(self):
        file = self.root / "new.jsonl"
        records = [prompt("old", "Existing turn"), answer("reply", "Existing answer")]
        file.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
        source = Source(SID, file)
        ledger = CursorLedger(self.root / "new.db")
        binding = Binding("fixture", "staging", "w1:p1", self.root, self.root,
                          SID, "Terminal", True)
        self.assertTrue(sync_source(ledger, self.client, binding, source))
        self.assertFalse(sync_source(CursorLedger(self.root / "new.db"), self.client, binding, source))
        self.assertEqual(len(self.stored), 2)

    def test_resolver_follows_current_session_and_rejects_reused_pid(self):
        config = self.root / "claude"
        (config / "sessions").mkdir(parents=True)
        process = self.root / "proc" / "42"
        process.mkdir(parents=True)
        (process / "stat").write_text("42 (claude) " + " ".join(["0"] * 19 + ["97"]))
        (process / "environ").write_bytes(f"CLAUDE_CONFIG_DIR={config}\0".encode())
        metadata = config / "sessions" / "42.json"
        binding = Binding("fixture", "staging", "w1:p1", config, Path("/scratch"))
        envelope = {"result": {"process_info": {
            "pane_id": "w1:p1", "foreground_processes": [{"name": "claude", "pid": 42}],
        }}}
        with patch("external_sync_watcher.subprocess.run") as run, patch(
            "external_sync_watcher.Path", side_effect=lambda path: (
                self.root / "proc" if path == "/proc" else Path(path)
            )
        ):
            run.return_value.stdout = json.dumps(envelope)
            for prefix in ("a", "b"):
                sid = f"{prefix}0000000-0000-0000-0000-000000000000"
                metadata.write_text(json.dumps({
                    "sessionId": sid, "pid": 42, "procStart": "97", "cwd": str(binding.cwd),
                }))
                self.assertEqual(resolve_claude(binding).session_id, sid)
            pinned = replace(binding, native_session_id=sid)
            self.assertEqual(resolve_claude(pinned).session_id, sid)
            with self.assertRaisesRegex(SourceUnavailable, "pinned native session"):
                resolve_claude(replace(pinned, native_session_id="a" + sid[1:]))
            (process / "stat").write_text("42 (claude) " + " ".join(["0"] * 19 + ["98"]))
            with self.assertRaises(SourceUnavailable):
                resolve_claude(binding)
