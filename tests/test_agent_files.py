import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_files  # noqa: E402
from request_ledger import RequestLedger  # noqa: E402

ID = "0b0f8a6e-3c1d-4e2f-9a8b-7c6d5e4f3a2b"
URL = "http://127.0.0.1:3001/api/agent-fabric/attachments"


class Sender:
    def __init__(self, mime="audio/mpeg"):
        self.calls = []
        self.mime = mime

    def __call__(self, url, credential, assertion, name, data):
        self.calls.append((url, credential, assertion, name, len(data)))
        return {"id": ID, "mimeType": self.mime}


class DeliverFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox = Path(self.tmp.name) / "outbox"
        self.outbox.mkdir(mode=0o700)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, reply="Here it is.", send=None, url=URL, credential="Bearer c", assertion="run", record=None):
        return agent_files.deliver_files(reply, self.outbox, url=url, credential=credential, assertion=assertion,
                                         log=self.logs.append, record=record, send=send or Sender())

    def test_an_mp3_becomes_a_link_line_and_is_recorded(self):
        (self.outbox / "narration.mp3").write_bytes(b"ID3" + b"\0" * 2000)
        sender, recorded = Sender(), []
        reply = self.deliver(send=sender, record=recorded.append)
        self.assertEqual(reply, f'Here it is.\n\n[narration.mp3](/api/attachments/{ID} "audio/mpeg")')
        self.assertEqual(sender.calls[0][:4], (URL, "Bearer c", "run", "narration.mp3"))
        self.assertEqual(recorded[0].size, 2003)
        self.assertEqual(len(recorded[0].sha256), 64)
        self.assertIn("sha256=", self.logs[0])

    def test_images_and_gateway_files_are_left_alone(self):
        (self.outbox / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.outbox / "browser.json").write_text("{}")
        (self.outbox / ".hidden").write_text("x")
        self.assertEqual(self.deliver(), "Here it is.")

    def test_credentials_are_refused_by_name_and_by_content(self):
        (self.outbox / ".env").write_text("TOKEN=1")
        (self.outbox / "auth.json").write_text("{}")
        (self.outbox / "notes.txt").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc")
        sender = Sender()
        reply = self.deliver(send=sender)
        self.assertEqual(sender.calls, [])
        self.assertIn("auth.json: looks like a credential", reply)
        self.assertIn("notes.txt: contains a private key", reply)
        self.assertNotIn(".env", [call[3] for call in sender.calls])

    def test_links_and_hard_links_are_refused(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("private")
        (self.outbox / "link.txt").symlink_to(secret)
        os.link(secret, self.outbox / "hard.txt")
        sender = Sender()
        reply = self.deliver(send=sender)
        self.assertEqual(sender.calls, [])
        self.assertIn("hard.txt: hard links are not sent", reply)
        self.assertIn("link.txt:", reply)

    def test_at_most_four_files_and_25_mb_each(self):
        (self.outbox / "big.wav").write_bytes(b"\0" * (agent_files.MAX_FILE_BYTES + 1))
        self.assertIn("big.wav: empty or larger than 25 MB", self.deliver())
        (self.outbox / "big.wav").unlink()
        for index in range(5):
            (self.outbox / f"{index}.mp3").write_bytes(b"ID3" + b"\0" * 10)
        sender = Sender()
        reply = self.deliver(send=sender)
        self.assertEqual(len(sender.calls), 4)
        self.assertIn("4.mp3: a reply can carry at most 4 files", reply)

    def test_no_route_or_no_signed_run_says_so_and_sends_nothing(self):
        (self.outbox / "narration.mp3").write_bytes(b"ID3")
        for kwargs in ({"url": None}, {"assertion": None}, {"credential": None}):
            sender = Sender()
            reply = self.deliver(send=sender, **kwargs)
            self.assertEqual(sender.calls, [])
            self.assertIn("narration.mp3: this conversation cannot receive files", reply)

    def test_an_openbot_refusal_is_reported_not_raised(self):
        (self.outbox / "narration.mp3").write_bytes(b"ID3")

        def refuse(*_):
            raise ValueError("This run does not belong to a conversation this Bot is in.")

        reply = self.deliver(send=refuse)
        self.assertIn("narration.mp3: This run does not belong", reply)

    def test_names_cannot_break_the_link_line(self):
        (self.outbox / 'a]"b.mp3').write_bytes(b"ID3")
        reply = self.deliver()
        self.assertIn(f'[a__b.mp3](/api/attachments/{ID} "audio/mpeg")', reply)


class HelperTests(unittest.TestCase):
    def test_files_url_sits_next_to_the_browser_route(self):
        self.assertEqual(agent_files.files_url("http://127.0.0.1:3001/api/agent-fabric/browser"), URL)
        self.assertIsNone(agent_files.files_url(None))
        self.assertIsNone(agent_files.files_url("http://127.0.0.1:3001/api/other"))

    def test_credential_names(self):
        for name in (".env", ".env.prod", "auth.json", "tls.pem", "x.key", "id_ed25519", "keybox.json", "bridge.token"):
            self.assertTrue(agent_files.looks_like_credential(name), name)
        for name in ("narration.mp3", "environment.md", "keynote.pdf"):
            self.assertFalse(agent_files.looks_like_credential(name), name)


class LedgerFileTests(unittest.TestCase):
    def test_a_delivered_file_is_recorded_without_its_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RequestLedger(str(Path(tmp) / "ledger.sqlite3"))
            ledger.record_file("k1", "narration.mp3", 14_000_000, "audio/mpeg", "ab" * 32, ID)
            self.assertEqual(ledger.files_for("k1"), [{"name": "narration.mp3", "size": 14_000_000,
                                                       "mime_type": "audio/mpeg", "sha256": "ab" * 32,
                                                       "attachment_id": ID}])


if __name__ == "__main__":
    unittest.main()
