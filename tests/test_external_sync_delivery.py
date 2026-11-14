"""Exercise real HTTP delivery and durable offsets using synthetic sources."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from external_sync_cursor import CursorLedger
from external_sync_hermes import sync_hermes
from external_sync_watcher import Binding, ImportClient, ImportFailed, Source, sync_source
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
                "message_uid, active, _compressed_summary, display_kind);"
                "INSERT INTO sessions VALUES ('first','cli',NULL,NULL,NULL);"
                "INSERT INTO sessions VALUES ('unrelated','gui','gui',NULL,NULL);"
                "INSERT INTO messages VALUES (1,'first','user','pre-opt-in',100,'uid-1',1,0,NULL);"
            )
        self.assertFalse(sync_hermes(file, ledger, self.client, **binding))
        with contextlib.closing(sqlite3.connect(file)) as db, db:
            db.executescript(
                "UPDATE sessions SET end_reason='compression' WHERE id='first';"
                "INSERT INTO sessions VALUES ('next','cli','cli',NULL,'first');"
                "INSERT INTO messages VALUES (2,'first','user','a question',102,'uid-2',1,0,NULL);"
                "INSERT INTO messages VALUES (3,'next','user','a question',102,'uid-2',1,0,NULL);"
                "INSERT INTO messages VALUES (4,'unrelated','user','other',104,'uid-4',1,0,NULL);"
                "INSERT INTO messages VALUES (5,'next','assistant','an answer',105,'uid-5',1,0,NULL);"
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
