import unittest

from external_sync_events import (
    TranscriptState,
    classify_claude_records,
    redact_content,
)


SID = "test-session"


def user(uid, text, *, source="typed", origin="human", **other):
    return {
        "type": "user",
        "uuid": uid,
        "timestamp": "2026-11-13T12:00:00Z",
        "sessionId": SID,
        "origin": {"kind": origin},
        "promptSource": source,
        "message": {"content": text},
        **other,
    }


def assistant(uid, blocks, **other):
    return {
        "type": "assistant",
        "uuid": uid,
        "timestamp": "2026-11-13T12:00:01Z",
        "sessionId": SID,
        "message": {"content": blocks},
        **other,
    }


class ClassifierTests(unittest.TestCase):
    def events(self, records, state=None):
        return list(classify_claude_records(records, session_id=SID, state=state or TranscriptState()))

    def test_human_prompt_and_agent_text(self):
        events = self.events([
            user("u1", "What is the status?"),
            assistant("a1", [{"type": "text", "text": "On track."}]),
        ])
        self.assertEqual([e.content for e in events], ["What is the status?", "On track."])
        self.assertEqual([e.sender for e in events], ["Derek (terminal)", "Agent"])
        self.assertTrue(all(e.id.startswith("afx-") for e in events))

    def test_ids_are_repeatable_and_part_index_separates_blocks(self):
        records = [assistant("a1", [{"type": "text", "text": "One"}, {"type": "text", "text": "Two"}])]
        first, second = self.events(records), self.events(records)
        self.assertEqual(first, second)
        self.assertNotEqual(first[0].id, first[1].id)

    def test_openbot_turn_and_reply_never_echo(self):
        state = TranscriptState()
        records = [
            user("u1", "[OpenBot pilot request ABCDEF012345]\nOPENBOT_BEGIN_ABCDEF012345"),
            assistant("a1", [{"type": "text", "text": "Already shown in OpenBot"}]),
        ]
        self.assertEqual(self.events(records, state), [])
        self.assertTrue(state.suppress_openbot_turn)
        next_events = self.events([user("u2", "New terminal question")], state)
        self.assertFalse(state.suppress_openbot_turn)
        self.assertEqual([e.content for e in next_events], ["New terminal question"])

    def test_state_survives_batch_boundaries(self):
        state = TranscriptState()
        self.events([user("u1", "OPENBOT_BEGIN_DEADBEEF12345678")], state)
        self.assertEqual(self.events([assistant("a1", [{"type": "text", "text": "Echo"}])], state), [])

    def test_raw_tool_results_and_thinking_are_excluded(self):
        events = self.events([
            {**user("u1", "secret"), "message": {"content": [{"type": "tool_result", "content": "SECRET"}]}},
            assistant("a1", [
                {"type": "thinking", "thinking": "PRIVATE"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "echo secret"}},
                {"type": "text", "text": "Completed"},
            ]),
        ])
        self.assertEqual([e.content for e in events], ["Ran shell command", "Completed"])

    def test_peer_metadata_scheduler_and_sidechain_are_excluded(self):
        events = self.events([
            user("u1", "other peer", origin="peer", isMeta=True),
            user("u2", "scheduled task", origin="task-notification"),
            user("u3", "child task", isSidechain=True),
            user("u4", "not typed", source="system"),
        ])
        self.assertEqual(events, [])

    def test_other_session_and_unidentified_records_fail_closed(self):
        events = self.events([
            {**user("u1", "wrong"), "sessionId": "other"},
            {**user("u2", "no-id"), "uuid": None},
            {**user("u3", "no-time"), "timestamp": None},
        ])
        self.assertEqual(events, [])

    def test_redaction(self):
        content = "hello Bearer secret-123 KEY_TOKEN=not-safe ghp_abcdefgh1234567890"
        cleaned = redact_content(content)
        self.assertNotIn("secret-123", cleaned)
        self.assertNotIn("not-safe", cleaned)
        self.assertNotIn("ghp_", cleaned)
        self.assertEqual(cleaned.count("[redacted]"), 3)

    def test_truncation_respects_byte_limit_and_utf8(self):
        content = "😃" * 2000
        cleaned = redact_content(content, max_bytes=128)
        self.assertLessEqual(len(cleaned.encode("utf-8")), 128)
        self.assertTrue(cleaned.endswith("(truncated)"))


if __name__ == "__main__":
    unittest.main()
