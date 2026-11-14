import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from external_sync_cursor import CursorConflict, CursorLedger, SourceChanged


SID = "native-session"


def prompt(uid, text, *, origin="human"):
    return {
        "type": "user", "uuid": uid, "sessionId": SID,
        "timestamp": "2026-11-14T00:00:00Z",
        "origin": {"kind": origin}, "promptSource": "typed",
        "message": {"content": text},
    }


def answer(uid, text):
    return {
        "type": "assistant", "uuid": uid, "sessionId": SID,
        "timestamp": "2026-11-14T00:00:01Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "session.jsonl"
        self.source.touch()
        self.ledger = CursorLedger(self.root / "ledger.sqlite3")

    def append(self, *items):
        with self.source.open("ab") as stream:
            for item in items:
                stream.write(json.dumps(item).encode("utf-8") + b"\n")

    def start(self, **kwargs):
        return self.ledger.open_or_initialize(
            bot_id="LBAI-Main", session_id=SID, path=self.source, **kwargs
        )

    def test_initial_enable_starts_at_now(self):
        self.append(prompt("old", "Old private text"))
        cursor = self.start()
        batch = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(batch.events, ())
        self.append(prompt("new", "New text"), answer("a", "New answer"))
        batch = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual([e.content for e in batch.events], ["New text", "New answer"])

    def test_resumed_session_catches_up_since_enable_without_backfill(self):
        self.start()
        target = self.root / "resumed.jsonl"
        resumed = "resumed"
        old = {**prompt("old", "Private historical text"),
               "timestamp": "2000-01-01T00:00:00Z", "sessionId": resumed}
        new = {**prompt("new", "New native prompt"), "sessionId": resumed,
               "timestamp": datetime.now(timezone.utc).isoformat()}
        target.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in (old, new)))
        cursor = self.ledger.open_or_initialize(bot_id="LBAI-Main", session_id=resumed, path=target)
        batch = self.ledger.read_batch(cursor, path=target)
        self.assertEqual([event.content for event in batch.events], ["New native prompt"])

    def test_no_ack_replays_identical_events(self):
        cursor = self.start()
        self.append(prompt("one", "Hello"))
        a = self.ledger.read_batch(cursor, path=self.source)
        b = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(a, b)
        self.assertEqual([e.id for e in a.events], [e.id for e in b.events])
        self.assertEqual(self.start().offset, 0)

    def test_ack_advances_durably_and_cas_refuses_stale_ack(self):
        cursor = self.start()
        self.append(prompt("one", "Hello"))
        batch = self.ledger.read_batch(cursor, path=self.source)
        next_cursor = self.ledger.acknowledge(batch)
        self.assertEqual(self.start(), next_cursor)
        self.assertEqual(self.ledger.read_batch(next_cursor, path=self.source).events, ())
        with self.assertRaises(CursorConflict):
            self.ledger.acknowledge(batch)

    def test_echo_suppression_is_persistent_between_batches(self):
        cursor = self.start()
        self.append(prompt("openbot", "OPENBOT_BEGIN_AABBCCDDEEFF0011"))
        first = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(first.events, ())
        cursor = self.ledger.acknowledge(first)
        self.assertTrue(self.start().suppress_openbot_turn)
        self.append(answer("reply", "Must not echo"))
        second = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(second.events, ())
        cursor = self.ledger.acknowledge(second)
        self.append(prompt("terminal", "New independent question"))
        third = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual([e.content for e in third.events], ["New independent question"])
        self.assertFalse(third.next_suppress_openbot_turn)

    def test_partial_line_is_not_consumed(self):
        cursor = self.start()
        payload = json.dumps(prompt("one", "Wait until complete")).encode("utf-8")
        with self.source.open("ab") as stream:
            stream.write(payload)
        batch = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(batch.events, ())
        self.assertEqual(batch.next_offset, cursor.offset)
        with self.source.open("ab") as stream:
            stream.write(b"\n")
        batch = self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual([e.content for e in batch.events], ["Wait until complete"])

    def test_interleaved_openbot_reply_cannot_echo_after_restart(self):
        cursor = self.start()
        self.append(
            prompt("openbot", "OPENBOT_BEGIN_AABBCCDDEEFF0011"),
            prompt("native", "A separate prompt"),
        )
        self.ledger.acknowledge(self.ledger.read_batch(cursor, path=self.source))
        self.ledger = CursorLedger(self.root / "ledger.sqlite3")
        self.append(
            {**answer("echo", "Already present"), "parentUuid": "openbot"},
            {**answer("reply", "New reply"), "parentUuid": "native"},
        )
        batch = self.ledger.read_batch(self.start(), path=self.source)
        self.assertEqual([event.content for event in batch.events], ["New reply"])

    def test_invalid_json_fails_without_advancing(self):
        cursor = self.start()
        with self.source.open("ab") as stream:
            stream.write(b"not-json\n")
        with self.assertRaisesRegex(ValueError, "malformed transcript"):
            self.ledger.read_batch(cursor, path=self.source)
        self.assertEqual(self.start().offset, 0)

    def test_truncation_and_rotation_fail_closed(self):
        cursor = self.start()
        self.append(prompt("one", "Hello"))
        cursor = self.ledger.acknowledge(
            self.ledger.read_batch(cursor, path=self.source)
        )
        self.source.write_bytes(b"")
        with self.assertRaises(SourceChanged):
            self.ledger.read_batch(cursor, path=self.source)

        self.source.unlink()
        self.source.touch()
        with self.assertRaises(SourceChanged):
            self.ledger.read_batch(cursor, path=self.source)

    def test_symlink_source_rejected(self):
        symlink = self.root / "link.jsonl"
        symlink.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.ledger.open_or_initialize(
                bot_id="LBAI-Main", session_id=SID, path=symlink
            )

    def test_batch_is_bounded(self):
        cursor = self.start()
        self.append(prompt("one", "First"), prompt("two", "Second"))
        first = self.ledger.read_batch(cursor, path=self.source, limit=1)
        self.assertEqual(len(first.events), 1)
        cursor = self.ledger.acknowledge(first)
        second = self.ledger.read_batch(cursor, path=self.source, limit=1)
        self.assertEqual([e.content for e in second.events], ["Second"])


if __name__ == "__main__":
    unittest.main()
