"""The Summary activity line, read from real Claude Code 2.1 and Codex 0.169 screens."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activity_line as al  # noqa: E402

SCREENS = Path(__file__).resolve().parent / "fixtures" / "screens"


def screen(name: str) -> str:
    return (SCREENS / f"{name}.txt").read_text(encoding="utf-8")


class RealScreens(unittest.TestCase):
    def test_claude_code(self):
        self.assertEqual(al.summary_line(screen("claude-reading")), "Reading 1 file… · 3s")
        self.assertEqual(al.summary_line(screen("claude-running")), "Listing files in current directory · 2s")

    def test_codex(self):
        self.assertEqual(al.summary_line(screen("codex-starting")), "Thinking · 2s")
        self.assertEqual(al.summary_line(screen("codex-explored")), "Reading note.txt · 8s")

    def test_nothing_while_idle_or_finished(self):
        for name in ("claude-finished", "claude-idle-background", "codex-finished"):
            self.assertIsNone(al.summary_line(screen(name)), name)


class Shapes(unittest.TestCase):
    def test_codex_reasoning_heading_wins(self):
        text = "• Ran git status\n• Inspecting the bridge (1m 12s • esc to interrupt)\n› Ask Codex"
        self.assertEqual(al.summary_line(text), "Inspecting the bridge · 1m12s")

    def test_codex_action_and_narration(self):
        self.assertEqual(al.summary_line("• Ran pytest -q\n  └ (no output)\n• Working (5s • esc to interrupt)"),
                         "Running pytest -q · 5s")
        self.assertEqual(al.summary_line("• I'll check the logs next.\n• Working (5s • esc to interrupt)"),
                         "I'll check the logs next. · 5s")

    def test_claude_narration_over_its_continuation_lines(self):
        text = ("● Here is the plan:\n  1. Read the file\n  2. Fix it\n"
                "✶ Pondering… (41s · ↓ 1.2k tokens · thought for 9s)\n  ⎿  Tip: something")
        self.assertEqual(al.summary_line(text), "Here is the plan: · 41s")

    def test_terminal_colours_are_ignored(self):
        self.assertEqual(al.summary_line("\x1b[1m• Working\x1b[0m (3s • esc to interrupt)"), "Thinking · 3s")


class Redaction(unittest.TestCase):
    def test_secrets_never_leave(self):
        cases = {
            "● Running curl -H 'Authorization: Bearer abcdefghijklmnop' https://x": "abcdefghijklmnop",
            "● export API_KEY=sk-live-1234567890": "sk-live-1234567890",
            "● Cloning https://derek:hunter2@github.com/x.git": "hunter2",
            "● Running deploy --token s3cr3tvalue": "s3cr3tvalue",
            "● Using ghp_0123456789abcdefghijklmnopqrstuvwxyz": "ghp_0123456789abcdef",
        }
        for line, secret in cases.items():
            shown = al.summary_line(line + "\n✢ Zesting… (2s · ↓ 10 tokens)")
            self.assertIsNotNone(shown)
            self.assertNotIn(secret, shown, line)

    def test_long_lines_are_cut(self):
        shown = al.summary_line("● " + "word " * 60 + "\n✢ Zesting… (2s)")
        self.assertLessEqual(len(shown), al.MAX_LINE + len(" · 2s"))


if __name__ == "__main__":
    unittest.main()
