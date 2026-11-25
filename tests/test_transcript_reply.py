"""The reply fallback: a Claude Code agent's reply read from its own session transcript."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transcript_reply as tr  # noqa: E402

ID = "0123456789ABCDEF"
OTHER = "FEDCBA9876543210"


def prompt(marker=ID, text="Please check the build."):
    return {"type": "user", "isSidechain": False, "origin": {"kind": "human"},
            "message": {"role": "user", "content": f"[OpenBot pilot request {marker}]\n{text}\n\n"
                        f"enclose your final response between these exact lines:\n"
                        f"OPENBOT_BEGIN_{marker}\n<your response>\nOPENBOT_END_{marker}\n"}}


def says(text):
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def calls(tool, ident="toolu_1"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident, "name": tool,
                                                          "input": {}}]}}


def result(ident="toolu_1", content="ok"):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
                                                     "content": content}]}}


END = {"type": "system", "subtype": "turn_duration", "isMeta": False}


def marked(text, marker=ID):
    return f"OPENBOT_BEGIN_{marker}\n{text}\nOPENBOT_END_{marker}"


class Replies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"

    def write(self, *entries):
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")

    def test_the_marked_reply_without_its_markers(self):
        self.write(prompt(), says("Checking."), calls("Bash"), result(), says(marked("All green.")), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=False), "All green.")

    def test_a_reply_with_another_requests_marker_is_still_found(self):
        self.write(prompt(), says(marked("Done, the profile is set up.", OTHER)), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=False), "Done, the profile is set up.")

    def test_a_reply_with_no_markers_after_a_question_answered_mid_turn(self):
        self.write(prompt(), calls("AskUserQuestion", "toolu_q"), result("toolu_q", "User answered: yes"),
                   says("Recorded your approval; it is in place now."), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=False),
                         "Recorded your approval; it is in place now.")

    def test_an_image_bearing_prompt(self):
        image_note = {"type": "user", "isMeta": True,
                      "message": {"content": "[Image: original 1440x2915, displayed at 988x2000.]"}}
        self.write(prompt(text="Here is a screenshot."), calls("Read"), result(content=[{"type": "image"}]),
                   image_note, says("Looking at the layout."), calls("Bash", "toolu_2"), result("toolu_2"),
                   says(marked("The header scrolls because the composer grows.")), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=False),
                         "The header scrolls because the composer grows.")

    def test_two_requests_back_to_back_each_get_their_own_reply(self):
        self.write(prompt(ID, "first"), says(marked("First answer.", ID)), END,
                   prompt(OTHER, "second"), says(marked("Second answer.", OTHER)), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=True), "First answer.")
        self.assertEqual(tr.reply_for(self.path, OTHER, pane_idle=True), "Second answer.")

    def test_never_mid_turn(self):
        self.write(prompt(), says("Still working on it."))
        self.assertIsNone(tr.reply_for(self.path, ID, pane_idle=False))
        # Idle with no end record (the turn was interrupted): the last thing it said is the reply.
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=True), "Still working on it.")

    def test_a_later_turn_is_never_taken_for_the_reply(self):
        notification = {"type": "user", "isMeta": True, "message": {"content": "background task finished"}}
        self.write(prompt(), says(marked("The answer.")), END, notification, says("Background note."), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=True), "The answer.")

    def test_a_later_message_quoting_the_id_is_not_the_request(self):
        quote = {"type": "user", "message": {"content": f"Why did [OpenBot pilot request {ID}] fail?"}}
        self.write(prompt(), says(marked("Original answer.")), END, quote, says("Investigating."), END)
        self.assertEqual(tr.reply_for(self.path, ID, pane_idle=True), "Original answer.")

    def test_no_request_no_reply(self):
        self.write(prompt(OTHER), says("Other."), END)
        self.assertIsNone(tr.reply_for(self.path, ID, pane_idle=True))

    def test_a_giant_reply_is_cut_like_a_marked_one(self):
        self.write(prompt(), says(marked("\n".join(f"line {n}" for n in range(1000)))), END)
        reply = tr.reply_for(self.path, ID, pane_idle=False)
        self.assertTrue(reply.endswith(tr.CUT_NOTE))
        self.assertEqual(len(reply[: -len(tr.CUT_NOTE)].splitlines()), tr.MAX_REPLY_LINES)
        self.write(prompt(), says("x" * (tr.MAX_REPLY_CHARS * 2)), END)
        self.assertLessEqual(len(tr.reply_for(self.path, ID, pane_idle=False)),
                             tr.MAX_REPLY_CHARS + len(tr.CUT_NOTE))

    def test_unreadable_transcript_is_unavailable(self):
        with self.assertRaises(tr.Unavailable):
            tr.reply_for(Path(self.tmp.name) / "missing.jsonl", ID, pane_idle=True)


class Location(unittest.TestCase):
    """The pane's own transcript, found from its own process and never by searching."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proc = root / "proc"
        self.config = root / "config"
        self.cwd = "/work/my.project"
        self.sid = "11111111-2222-3333-4444-555555555555"
        (self.proc / "4242").mkdir(parents=True)
        (self.proc / "4242" / "environ").write_bytes(
            b"PATH=/usr/bin\0HOME=/home/agent\0CLAUDE_CONFIG_DIR=" + str(self.config).encode() + b"\0")
        (self.config / "sessions").mkdir(parents=True)
        (self.config / "sessions" / "4242.json").write_text(
            json.dumps({"pid": 4242, "sessionId": self.sid, "cwd": self.cwd}))
        self.expected = self.config / "projects" / "-work-my-project" / f"{self.sid}.jsonl"
        self.expected.parent.mkdir(parents=True)
        self.expected.write_text("")
        self.info = {"foreground_processes": [{"argv": ["claude", "--flag"], "name": "claude", "pid": 4242,
                                               "cwd": self.cwd}]}

    def test_from_the_process_config_dir_and_session_record(self):
        self.assertEqual(tr.transcript_path(self.info, proc=self.proc), self.expected)
        self.assertEqual(tr.transcript_path(self.info, self.sid, proc=self.proc), self.expected)

    def test_the_project_name_is_claude_codes(self):
        self.assertEqual(tr.project_dir_name("/home/a/projects/x.y_z"), "-home-a-projects-x-y-z")

    def test_not_a_claude_pane(self):
        codex = {"foreground_processes": [{"argv": ["codex"], "name": "codex", "pid": 4242, "cwd": self.cwd}]}
        with self.assertRaises(tr.Unavailable):
            tr.transcript_path(codex, proc=self.proc)

    def test_a_session_id_that_disagrees_with_herdrs_is_refused(self):
        with self.assertRaises(tr.Unavailable):
            tr.transcript_path(self.info, "99999999-2222-3333-4444-555555555555", proc=self.proc)

    def test_missing_or_symlinked_transcript(self):
        self.expected.unlink()
        with self.assertRaises(tr.Unavailable):
            tr.transcript_path(self.info, proc=self.proc)
        other = Path(self.tmp.name) / "elsewhere.jsonl"
        other.write_text("")
        os.symlink(other, self.expected)
        with self.assertRaises(tr.Unavailable):
            tr.transcript_path(self.info, proc=self.proc)

    def test_default_config_dir_is_home_dot_claude(self):
        (self.proc / "4242" / "environ").write_bytes(b"HOME=" + str(Path(self.tmp.name) / "home").encode() + b"\0")
        home_config = Path(self.tmp.name) / "home" / ".claude"
        (home_config / "sessions").mkdir(parents=True)
        (home_config / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": self.sid}))
        target = home_config / "projects" / "-work-my-project" / f"{self.sid}.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text("")
        self.assertEqual(tr.transcript_path(self.info, proc=self.proc), target)

    def test_unreadable_process(self):
        with self.assertRaises(tr.Unavailable):
            tr.transcript_path({"foreground_processes": [{"argv": ["claude"], "pid": 999, "cwd": "/x"}]},
                               proc=self.proc)


if __name__ == "__main__":
    unittest.main()
