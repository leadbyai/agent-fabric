"""Offline tests for session-only model and effort control. No real Herdr session or CLI is touched.

The fake panes reproduce what Claude Code 2.1.325 and codex-cli 0.169.0 did when driven through
Herdr (2026-11-14): Claude's `/model` menu wraps its cursor, clamps its effort slider, saves a
user-wide default on Enter over a model row, expands on Enter over "More models", and switches for
the session only on `s`; Codex switches for the session with `/model <slug> --reasoning=<level>`.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))

import model_control as mc  # noqa: E402

CLAUDE_MAIN = [("Default (recommended)", "Sonnet 5.5 · Efficient for routine tasks"),
               ("Opus 5.5", "For complex work and everyday tasks"),
               ("Fable 5.2", "For your toughest challenges"),
               ("Sonnet 5.5", "Most efficient for simpler tasks"),
               ("Haiku 5.5", "Fastest for quick answers")]
CLAUDE_MORE = [("Fable 5.1", "Most capable for your hardest and longest-running tasks"),
               ("Haiku 4.5", "Fastest for quick answers"),
               ("Opus 4.6", "Best for everyday, complex tasks")]
EFFORT_WORDS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "xHigh", "max": "Max"}
LEVELS = ["low", "medium", "high", "xhigh", "max"]

# Captured from Codex 0.169.0 (trimmed): its status line under the prompt.
CODEX_SCREEN = """  >_ OpenAI Codex (v0.169.0)
     /home/agent/work
› Ask Codex to do anything
  GPT-6.1-Sol default · /home/agent/work
  ← for agents · ? for shortcuts"""


def write_catalogues(home: Path) -> None:
    claude = home / ".claude" / "cache" / "model-catalog"
    claude.mkdir(parents=True)
    efforts = {"effort_levels": LEVELS, "default_effort": "medium"}
    models = [{"id": "claude-opus-5-5", "name": "Opus 5.5", "section": "main", "runtime": efforts},
              {"id": "claude-fable-5-2", "name": "Fable 5.2", "section": "main", "runtime": efforts},
              {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5", "section": "main", "runtime": efforts},
              {"id": "claude-haiku-5-5", "name": "Haiku 5.5", "section": "main", "runtime": efforts},
              {"id": "claude-fable-5-1", "name": "Fable 5.1", "section": "overflow",
               "runtime": {"effort_levels": LEVELS, "default_effort": "high"}},
              {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5", "section": "overflow", "runtime": None},
              {"id": "claude-opus-4-6", "name": "Opus 4.6", "section": "overflow",
               "runtime": {"effort_levels": ["low", "medium", "high", "max"], "default_effort": "high"}}]
    (claude / "a-cc.json").write_text(json.dumps({"version": 2, "catalog": {"config": {"models": models}}}))
    codex = home / ".codex"
    codex.mkdir()
    rows = [{"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "priority": 7,
             "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": e} for e in ["low", "medium", "high", "xhigh", "max"]]},
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 1,
             "default_reasoning_level": "low",
             "supported_reasoning_levels": [{"effort": e} for e in ["low", "medium", "high", "xhigh", "max", "ultra"]]},
            {"slug": "codex-auto-review", "display_name": "Auto review", "visibility": "hide", "priority": 11,
             "default_reasoning_level": "medium", "supported_reasoning_levels": []}]
    (codex / "models_cache.json").write_text(json.dumps({"client_version": "0.169.0", "models": rows}))


class FakeClaude:
    """Claude Code's prompt and `/model` menu, as far as the switch uses them."""

    def __init__(self, model="Opus 5.5", effort="medium"):
        self.model, self.effort = model, effort
        self.menu = None
        self.history = ["▝▜██████▀  Opus 5.5 · Claude Ultra"]
        self.saved_default = None  # set if anything saves a user-wide default
        self.typed = ""

    def rows(self):
        rows = [label for label, _ in CLAUDE_MAIN] + ["More models"]
        if self.menu and self.menu["expanded"]:
            rows += [label for label, _ in CLAUDE_MORE]
        return rows

    def screen(self):
        lines = list(self.history)
        if self.menu:
            lines += ["▔" * 40, "   Select model",
                      "   Switch between Claude models. Your pick becomes the default for new sessions."]
            number = 0
            descriptions = dict(CLAUDE_MAIN + CLAUDE_MORE)
            for index, label in enumerate(self.rows()):
                cursor = "❯" if index == self.menu["cursor"] else " "
                if label == "More models":
                    lines.append(f"   {cursor} {'▾' if self.menu['expanded'] else '▸'}   More models")
                    continue
                number += 1
                check = " ✔" if label == self.model else ""
                lines.append(f"   {cursor} {number}.  {label}{check}{' ' * 6}{descriptions[label]}")
            selected = self.rows()[self.menu["cursor"]]
            if selected not in ("More models", "Haiku 4.5"):
                lines.append(f"   ● {EFFORT_WORDS[self.menu['effort']]} effort ←/→ to adjust")
            lines.append("   Enter to set as default · s to use this session only · Esc to cancel")
        else:
            lines += [f"{' ' * 60}● {self.effort} · /effort", "─" * 40, f"❯ {self.typed}", "─" * 40]
        return "\n".join(lines)

    def styled(self):
        if self.menu:
            return self.screen()  # no prompt line while a menu is open
        typed = self.typed if self.typed else '\x1b[0m\x1b[2mTry "create a util logging.py that..."\x1b[0m'
        lines = list(self.history) + [f"{' ' * 60}● {self.effort} · /effort", "─" * 40, f"❯\u00a0{typed}\r", "─" * 40]
        return "\n".join(lines)

    def submit(self, text):
        if self.menu:
            raise AssertionError("typed into an open menu")
        if self.typed:
            raise AssertionError("submitted on top of an unsent draft")
        self.history.append(f"❯ {text}")
        if text == "/model":
            self.menu = {"cursor": self.rows().index(self.model) if self.model in self.rows() else 0,
                         "expanded": False, "effort": self.effort}
        else:
            raise AssertionError(f"unexpected command {text!r}")

    def keys(self, *names):
        for name in names:
            if not self.menu:
                if name == "Escape":
                    continue
                self.typed += name  # a stray key lands in the prompt
                continue
            rows, menu = self.rows(), self.menu
            if name == "Down":
                menu["cursor"] = (menu["cursor"] + 1) % len(rows)  # wraps
            elif name == "Up":
                menu["cursor"] = (menu["cursor"] - 1) % len(rows)
            elif name in ("Left", "Right"):
                at = LEVELS.index(menu["effort"]) + (1 if name == "Right" else -1)
                menu["effort"] = LEVELS[max(0, min(len(LEVELS) - 1, at))]  # clamps
            elif name == "Escape":
                self.history.append(f"  ⎿  Kept model as {self.model}")
                self.menu = None
            elif name == "Enter":
                if rows[menu["cursor"]] == "More models":
                    menu["expanded"] = not menu["expanded"]
                else:
                    self.saved_default = rows[menu["cursor"]]
                    self.menu = None
            elif name == "s":
                label = rows[menu["cursor"]]
                if label == "More models":
                    continue
                self.model, self.effort = label, menu["effort"]
                self.history.append(f"  ⎿  Set model to {label} for this session only with {self.effort} effort")
                self.menu = None
            else:
                raise AssertionError(f"unexpected key {name}")


class FakeCodex:
    def __init__(self):
        self.label, self.effort = "GPT-6.1-Sol", "default"
        self.commands = []
        self.config_written = False
        self.draft = ""

    def styled(self):
        # Real codex-cli 0.169 shape (Codex staging, 2026-11-15): ESC[0m ESC[1m›ESC[0m then a DIM hint,
        # the prompt directly above the "<model> <effort> · <dir>" footer.
        prompt = self.draft if self.draft else "\x1b[0m\x1b[2mAsk Codex to do anything\x1b[0m"
        return self.screen().replace("› Ask Codex to do anything", f"\x1b[0m\x1b[1m›\x1b[0m {prompt}")

    def screen(self):
        return CODEX_SCREEN.replace("GPT-6.1-Sol default", f"{self.label} {self.effort}")

    def keys(self, *names):
        raise AssertionError("Codex is switched with a typed command only")

    def submit(self, text):
        if self.draft:
            raise AssertionError("submitted on top of an unsent draft")
        self.commands.append(text)
        parts = text.split()
        slug = parts[1]
        self.label = {"gpt-6-luna": "GPT-6-Luna", "gpt-6.1-sol": "GPT-6.1-Sol"}[slug]
        self.effort = parts[2].split("=", 1)[1] if len(parts) > 2 else "medium"


def driver(kind):
    return mc.driver_for(kind, sleep=lambda _s: None, clock=iter(range(10 ** 6)).__next__)


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        write_catalogues(self.home)
        self.env = {"HOME": str(self.home)}

    def test_catalogues_come_from_each_cli_cache(self):
        claude = mc.catalogue_for("claude", self.env)
        self.assertEqual([m.id for m in claude][:4], ["claude-opus-5-5", "claude-fable-5-2",
                                                       "claude-sonnet-5-5", "claude-haiku-5-5"])
        self.assertEqual({m.section for m in claude[4:]}, {"more"})
        self.assertEqual(next(m for m in claude if m.label == "Haiku 4.5").efforts, [])
        codex = mc.catalogue_for("codex", self.env)
        # visibility "list" only, in the CLI's own order
        self.assertEqual([m.id for m in codex], ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(codex[0].efforts[-1], "ultra")

    def test_unknown_kinds_and_missing_caches_are_unsupported(self):
        with self.assertRaises(mc.Unsupported):
            mc.catalogue_for("gemini", self.env)
        with self.assertRaises(mc.Unsupported):
            mc.catalogue_for("codex", {"HOME": str(self.home / "nobody")})
        self.assertEqual(mc.provider_for("claude"), "anthropic")
        self.assertIsNone(mc.provider_for("opencode"))

    def test_validation(self):
        models = mc.catalogue_for("claude", self.env)
        with self.assertRaises(mc.InvalidChoice):
            mc.validate(models, "gpt-6-luna", None)
        with self.assertRaises(mc.InvalidChoice):
            mc.validate(models, "claude-opus-4-6", "xhigh")  # not offered on that model
        with self.assertRaises(mc.InvalidChoice):
            mc.validate(models, "claude-haiku-4-5-20251001", "low")  # takes no effort
        self.assertEqual(mc.validate(models, "claude-opus-5-5", "max")[1], "max")

    def test_claude_switch_is_session_only_through_the_menu(self):
        pane = FakeClaude()
        option, effort = mc.validate(mc.catalogue_for("claude", self.env), "claude-haiku-5-5", "high")
        state = driver("claude").switch(pane, option, effort)
        self.assertEqual((state.model, state.label, state.effort), ("claude-haiku-5-5", "Haiku 5.5", "high"))
        self.assertEqual((pane.model, pane.effort), ("Haiku 5.5", "high"))
        self.assertIsNone(pane.saved_default)  # never Enter on a model row
        self.assertIsNone(pane.menu)
        self.assertEqual(pane.typed, "")

    def test_claude_reaches_a_model_above_the_cursor_and_lowers_effort(self):
        pane = FakeClaude(model="Haiku 5.5", effort="max")
        option, effort = mc.validate(mc.catalogue_for("claude", self.env), "claude-opus-5-5", "low")
        state = driver("claude").switch(pane, option, effort)
        self.assertEqual((state.label, state.effort), ("Opus 5.5", "low"))
        self.assertIsNone(pane.saved_default)

    def test_claude_more_models_are_expanded_not_saved(self):
        pane = FakeClaude()
        option, effort = mc.validate(mc.catalogue_for("claude", self.env), "claude-opus-4-6", "max")
        state = driver("claude").switch(pane, option, effort)
        self.assertEqual((state.label, state.effort), ("Opus 4.6", "max"))
        self.assertIsNone(pane.saved_default)

    def test_claude_earlier_confirmation_is_not_mistaken_for_this_one(self):
        pane = FakeClaude()
        models = mc.catalogue_for("claude", self.env)
        driver("claude").switch(pane, *mc.validate(models, "claude-haiku-5-5", "high"))
        # The previous "Set model to Haiku 5.5" line is still on screen when the next switch starts.
        state = driver("claude").switch(pane, *mc.validate(models, "claude-opus-4-6", "max"))
        self.assertEqual((state.label, state.effort), ("Opus 4.6", "max"))

    def test_claude_switch_without_effort_keeps_the_current_one(self):
        pane = FakeClaude(effort="xhigh")
        option, _ = mc.validate(mc.catalogue_for("claude", self.env), "claude-fable-5-2", None)
        state = driver("claude").switch(pane, option, None)
        self.assertEqual((state.label, state.effort), ("Fable 5.2", "xhigh"))

    def test_claude_read_opens_and_closes_the_menu(self):
        pane = FakeClaude(model="Sonnet 5.5", effort="high")
        state = driver("claude").read(pane, mc.catalogue_for("claude", self.env))
        self.assertEqual((state.model, state.effort), ("claude-sonnet-5-5", "high"))
        self.assertIsNone(pane.menu)
        self.assertIsNone(pane.saved_default)

    def test_claude_unrecognised_screen_fails_closed(self):
        pane = FakeClaude()
        pane.submit = lambda text: pane.history.append("❯ /model")  # the menu never opens
        option, effort = mc.validate(mc.catalogue_for("claude", self.env), "claude-haiku-5-5", "high")
        with self.assertRaises(mc.Unverified):
            driver("claude").switch(pane, option, effort)
        self.assertEqual((pane.model, pane.typed), ("Opus 5.5", ""))

    def _user_menu(self):
        pane = FakeClaude()
        pane.submit("/model")  # a person has the menu open...
        pane.keys("Down", "Right")  # ...and has moved in it
        return pane, dict(pane.menu), list(pane.history)

    def test_regression_switch_leaves_a_users_open_menu_untouched(self):
        pane, menu, history = self._user_menu()
        option, _ = mc.validate(mc.catalogue_for("claude", self.env), "claude-haiku-5-5", None)
        with self.assertRaises(mc.NotIdle):
            driver("claude").switch(pane, option, None)
        self.assertEqual((pane.menu, pane.history, pane.saved_default), (menu, history, None))  # no Escape, nothing typed

    def test_regression_read_leaves_a_users_open_menu_untouched(self):
        pane, menu, history = self._user_menu()
        with self.assertRaises(mc.NotIdle):
            driver("claude").read(pane, mc.catalogue_for("claude", self.env))
        self.assertEqual((pane.menu, pane.history, pane.saved_default), (menu, history, None))

    def test_regression_unsent_draft_is_never_appended_submitted_or_cleared(self):
        models = mc.catalogue_for("claude", self.env)
        option, effort = mc.validate(models, "claude-haiku-5-5", "high")
        for act in (lambda p: driver("claude").read(p, models), lambda p: driver("claude").switch(p, option, effort)):
            pane = FakeClaude()
            pane.typed = "STAGE-DRAFT keep me"
            history = list(pane.history)
            with self.assertRaises(mc.NotIdle):
                act(pane)
            self.assertEqual((pane.typed, pane.history, pane.menu, pane.model), ("STAGE-DRAFT keep me", history, None, "Opus 5.5"))

    def test_unrecognised_prompt_is_refused_untouched(self):
        pane = FakeClaude()
        pane.styled = lambda: "no prompt line on this screen"
        history = list(pane.history)
        option, _ = mc.validate(mc.catalogue_for("claude", self.env), "claude-haiku-5-5", None)
        with self.assertRaises(mc.NotIdle):
            driver("claude").switch(pane, option, None)
        self.assertEqual((pane.history, pane.menu), (history, None))

    def test_codex_unsent_draft_is_refused_untouched(self):
        pane = FakeCodex()
        pane.draft = "half-typed request"
        option, effort = mc.validate(mc.catalogue_for("codex", self.env), "gpt-6-luna", "high")
        with self.assertRaises(mc.NotIdle):
            driver("codex").switch(pane, option, effort)
        self.assertEqual((pane.commands, pane.draft), ([], "half-typed request"))

    def test_composer_state_on_captured_claude_screens(self):
        rule = "─" * 119
        empty = f'{rule}\n❯\u00a0\x1b[0m\x1b[2mTry "create a util logging.py that..."\x1b[0m\r\n{rule}'
        draft = f"{rule}\n❯\u00a0STAGE-DRAFT keep me\r\n{rule}"
        multi = f"{rule}\n❯\u00a0first line\n  second line\n{rule}"
        self.assertEqual(mc.composer_state("claude", empty), "empty")
        self.assertEqual(mc.composer_state("claude", draft), "draft")
        self.assertIsNone(mc.composer_state("claude", multi))  # frame broken: not recognised, so refused
        self.assertEqual(mc.composer_state("claude", empty.replace("\x1b[2m", "")), "draft")  # unstyled: refused
        self.assertIsNone(mc.composer_state("gemini", empty))
        # Scrollback full of earlier prompt echoes does not hide the one framed, live prompt.
        history = "❯ /model\n  ⎿  Set model to Haiku 5.5 for this session only\n❯ /model\n  ⎿  Kept model as Haiku 5.5\n"
        self.assertEqual(mc.composer_state("claude", history + empty), "empty")

    def test_composer_state_on_codex_screens_anchors_to_the_footer(self):
        footer = "  GPT-6.1-Sol low · ~/work"
        hint = "\x1b[0m\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"
        self.assertEqual(mc.composer_state("codex", f"› earlier message\n{hint}\n{footer}"), "empty")
        self.assertEqual(mc.composer_state("codex", f"{hint}\n\n{footer}"), "empty")  # blank gap is fine
        self.assertEqual(mc.composer_state("codex", f"\x1b[1m›\x1b[0m Ask Codex to do anything\n{footer}"), "draft")  # same words, not dim
        self.assertEqual(mc.composer_state("codex", f"› fix the build\n{footer}"), "draft")
        # Shift+Enter at an empty prompt, then a marker: a bare › row, an indented row, then the footer.
        self.assertIsNone(mc.composer_state("codex", f"\x1b[1m›\x1b[0m \n    STAGE-MARK\n{footer}"))
        self.assertIsNone(mc.composer_state("codex", hint))  # no footer: not recognised, refused

    def test_repeated_claude_switches_without_clearing_scrollback(self):
        pane = FakeClaude()
        models = mc.catalogue_for("claude", self.env)
        for model_id, effort in (("claude-haiku-5-5", "high"), ("claude-sonnet-5-5", "low"), ("claude-haiku-5-5", None)):
            option, effort = mc.validate(models, model_id, effort)
            driver("claude").switch(pane, option, effort)
        self.assertEqual(pane.model, "Haiku 5.5")
        self.assertGreaterEqual(sum(1 for line in pane.history if line == "❯ /model"), 3)
        self.assertEqual(driver("claude").read(pane, models).label, "Haiku 5.5")
        self.assertIsNone(pane.saved_default)

    def test_claude_footer_effort_is_read_passively(self):
        self.assertEqual(mc.claude_footer_effort(FakeClaude(effort="xhigh").screen()), "xhigh")

    def test_codex_switch_is_one_session_only_command(self):
        pane = FakeCodex()
        models = mc.catalogue_for("codex", self.env)
        self.assertEqual(driver("codex").read(pane, models).effort, "low")  # "default" is the model's own
        option, effort = mc.validate(models, "gpt-6-luna", "high")
        state = driver("codex").switch(pane, option, effort)
        self.assertEqual(pane.commands, ["/model gpt-6-luna --reasoning=high"])
        self.assertEqual((state.model, state.effort), ("gpt-6-luna", "high"))

    def test_codex_unconfirmed_switch_is_unverified(self):
        pane = FakeCodex()
        pane.submit = lambda text: None  # the CLI ignores the command
        option, effort = mc.validate(mc.catalogue_for("codex", self.env), "gpt-6-luna", "high")
        with self.assertRaises(mc.Unverified):
            driver("codex").switch(pane, option, effort)


class ModelRouteTests(unittest.TestCase):
    """GET/PUT /sessions/<s>/agents/<pane>/model through the gateway's real handler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        write_catalogues(root)
        self.panes = {"w1:p1": FakeClaude(), "w1:p2": FakeCodex()}
        self.roster = [
            {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle", "cwd": str(root),
             "agent_session": {"value": "c-1"}},
            {"pane_id": "w1:p2", "agent": "codex", "agent_status": "idle", "cwd": str(root),
             "agent_session": {"value": "x-1"}},
            {"pane_id": "w1:p3", "agent": "gemini", "agent_status": "idle", "cwd": str(root)},
        ]
        env = {"HOME": str(root), "HERDR_SESSIONS": "Main", "OPENBOT_BRIDGE_TOKEN": "Z" * 48,
               "OPENBOT_BRIDGE_ALLOWED_ROOTS": str(root)}
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        spec = importlib.util.spec_from_file_location("bridge_model_test", PACKAGE_ROOT / "bridge.py")
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.bridge.command = self.fake_command
        mc.POLL_SECONDS = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.bridge.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fake_command(self, *args, session=None, **kwargs):
        self.assertEqual(session, "Main")
        if args[:2] == ("agent", "list"):
            return json.dumps({"result": {"agents": self.roster}})
        if args[:2] == ("agent", "get"):
            return json.dumps({"result": {"agent": next(a for a in self.roster if a["pane_id"] == args[2])}})
        pane = self.panes[args[2]]
        if args[:2] == ("agent", "read"):
            if args[3:] == ("--source", "visible", "--format", "ansi"):
                return pane.styled()  # the prompt check reads the styled screen
            self.assertEqual(args[3:], ("--source", "visible"))
            return pane.screen()
        if args[:2] == ("agent", "send-keys"):
            pane.keys(*args[3:])
            return "{}"
        if args[:2] == ("agent", "prompt"):
            self.assertEqual(len(args), 4)  # never --wait: a slash command starts no turn
            pane.submit(args[3])
            return "{}"
        raise AssertionError(f"Unexpected Herdr command {args}")

    def call(self, method, path, body=None, token="Z" * 48):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Authorization": "Bearer " + token} if token else {}
        data = None if body is None else json.dumps(body)
        if data is not None:
            headers["Content-Type"] = "application/json"
        con.request(method, path, body=data, headers=headers)
        response = con.getresponse()
        status, payload = response.status, json.loads(response.read() or b"{}")
        con.close()
        return status, payload

    def test_health_advertises_model_control(self):
        status, body = self.call("GET", "/health", token=None)
        self.assertEqual((status, body["model_control"]), (200, {"read": True, "switch": True}))

    def test_get_is_passive_for_claude_until_refresh(self):
        status, body = self.call("GET", "/sessions/Main/agents/w1:p1/model")
        self.assertEqual(status, 200)
        self.assertEqual((body["provider"], body["switchable"], body["scope"]), ("anthropic", True, "session"))
        self.assertIsNone(body["current"])  # not read yet, and no menu was opened
        self.assertEqual(self.panes["w1:p1"].history[-1], "▝▜██████▀  Opus 5.5 · Claude Ultra")
        status, body = self.call("GET", "/sessions/Main/agents/w1:p1/model?refresh=1")
        self.assertEqual(body["current"]["model"], "claude-opus-5-5")
        self.assertIsNone(self.panes["w1:p1"].menu)

    def test_put_switches_and_get_reports_it(self):
        status, body = self.call("PUT", "/sessions/Main/agents/w1:p1/model",
                                 {"model": "claude-sonnet-5-5", "effort": "high"})
        self.assertEqual((status, body["current"]["model"], body["current"]["effort"]),
                         (200, "claude-sonnet-5-5", "high"))
        self.assertIsNone(self.panes["w1:p1"].saved_default)
        status, body = self.call("GET", "/sessions/Main/agents/w1:p1/model")
        self.assertEqual(body["current"]["label"], "Sonnet 5.5")

    def test_codex_get_reads_the_status_line(self):
        status, body = self.call("GET", "/sessions/Main/agents/w1:p2/model")
        self.assertEqual((status, body["provider"], body["current"]["model"]), (200, "openai", "gpt-6.1-sol"))
        status, body = self.call("PUT", "/sessions/Main/agents/w1:p2/model", {"model": "gpt-6-luna"})
        self.assertEqual((status, body["current"]["model"]), (200, "gpt-6-luna"))

    def test_unsupported_kind_shows_provider_only(self):
        status, body = self.call("GET", "/sessions/Main/agents/w1:p3/model")
        self.assertEqual((status, body["provider"], body["switchable"], body["models"]),
                         (200, "google", False, []))
        status, body = self.call("PUT", "/sessions/Main/agents/w1:p3/model", {"model": "x"})
        self.assertEqual(status, 501)

    def test_busy_or_blocked_agents_are_not_switched(self):
        self.roster[0]["agent_status"] = "working"
        status, body = self.call("PUT", "/sessions/Main/agents/w1:p1/model", {"model": "claude-haiku-5-5"})
        self.assertEqual((status, body["error"]), (409, "agent_busy"))
        self.roster[0]["agent_status"] = "blocked"
        self.assertEqual(self.call("PUT", "/sessions/Main/agents/w1:p1/model", {"model": "claude-haiku-5-5"})[0], 409)
        lock = self.bridge.target_lock("Main", "w1:p2")
        with lock:  # a request is running on the pane
            self.assertEqual(self.call("PUT", "/sessions/Main/agents/w1:p2/model", {"model": "gpt-6-luna"})[0], 409)
        self.assertEqual(self.panes["w1:p1"].model, "Opus 5.5")
        self.assertEqual(self.panes["w1:p2"].commands, [])

    def test_invalid_choices_and_bodies(self):
        path = "/sessions/Main/agents/w1:p1/model"
        self.assertEqual(self.call("PUT", path, {"model": "gpt-6-luna"})[0], 422)
        self.assertEqual(self.call("PUT", path, {"model": "claude-opus-5-5", "effort": "ultra"})[0], 422)
        self.assertEqual(self.call("PUT", path, ["claude-opus-5-5"])[0], 400)
        self.assertEqual(self.call("PUT", path)[0], 400)
        self.assertEqual(self.call("PUT", "/sessions/Main/agents/w9:p9/model", {"model": "claude-opus-5-5"})[0], 404)

    def test_auth_scopes(self):
        path = "/sessions/Main/agents/w1:p1/model"
        self.assertEqual(self.call("GET", path, token=None)[0], 401)
        self.assertEqual(self.call("PUT", path, {"model": "claude-haiku-5-5"}, token="wrong" * 10)[0], 401)
        discovery = "D" * 48
        self.bridge.DISCOVERY_SHA256 = __import__("hashlib").sha256(discovery.encode()).hexdigest()
        self.assertEqual(self.call("GET", path, token=discovery)[0], 404)
        self.assertEqual(self.call("PUT", path, {"model": "claude-haiku-5-5"}, token=discovery)[0], 404)
        self.assertEqual(self.panes["w1:p1"].model, "Opus 5.5")

    def test_route_token_reaches_only_its_agent(self):
        token = "R" * 48
        self.bridge.ROUTE_HASHES = {("Main", "w1:p2"): __import__("hashlib").sha256(token.encode()).hexdigest()}
        self.assertEqual(self.call("GET", "/sessions/Main/agents/w1:p1/model", token=token)[0], 404)
        self.assertEqual(self.call("GET", "/sessions/Main/agents/w1:p2/model", token=token)[0], 200)

    def test_switch_can_be_turned_off(self):
        self.bridge.MODEL_SWITCH = False
        status, body = self.call("GET", "/sessions/Main/agents/w1:p2/model")
        self.assertEqual((body["switchable"], bool(body["reason"])), (False, True))
        self.assertEqual(self.call("PUT", "/sessions/Main/agents/w1:p2/model", {"model": "gpt-6-luna"})[0], 403)
        self.assertEqual(self.panes["w1:p2"].commands, [])


if __name__ == "__main__":
    unittest.main()
