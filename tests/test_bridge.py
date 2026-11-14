"""Offline tests. No real Herdr session or production endpoint is touched."""

import base64
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "5XcAAAAASUVORK5CYII="
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))
BRIDGE_PATH = PACKAGE_ROOT / "release" / "bridge.py"
if not BRIDGE_PATH.is_file():
    BRIDGE_PATH = PACKAGE_ROOT / "bridge.py"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "demo").mkdir()
        (root / "pricing").mkdir()
        (root / "other").mkdir()
        self.roster = {
            "Main": [
                {"pane_id": "wA:p1", "name": "demo", "terminal_title_stripped": "Demo",
                 "agent_status": "done", "cwd": str(root / "demo")},
                {"pane_id": "w3:p1", "name": "pricing", "terminal_title_stripped": "Pricing",
                 "agent_status": "idle", "cwd": str(root / "pricing")},
            ],
            "Other": [{"pane_id": "w1:p2", "terminal_title_stripped": "Other Agent",
                       "agent_status": "working", "cwd": str(root / "other")}],
        }
        self.prompts = []
        self.replies = {}
        env = {
            "HERDR_SESSION": "Main",
            "HERDR_SESSIONS": "Main,Other,Review",
            "HERDR_AGENT_TARGET": "demo",
            "OPENBOT_BRIDGE_TOKEN": "Z" * 48,
            "OPENBOT_BRIDGE_ALLOWED_ROOTS": str(root),
            "OPENBOT_BRIDGE_WORKDIR": str(root / "demo"),
            "OPENBOT_BRIDGE_ARTIFACT_DIR": str(root / "artifacts"),
            "OPENBOT_BRIDGE_PUBLIC_ORIGIN": "https://lbai-agents.leadbyai.bot",
            "OPENBOT_BRIDGE_ARTIFACT_MODE": "inline",
        }
        with patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("bridge_test", BRIDGE_PATH)
            self.bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bridge)
        self.bridge.command = self.fake_command
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.bridge.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fake_command(self, *args, session=None, **kwargs):
        if args[:2] == ("session", "list"):
            if getattr(self, "session_list_fails", False):
                raise RuntimeError("server_not_running")
            running = getattr(self, "running", ["Main", "Other"])
            return json.dumps({"sessions": [{"name": n, "running": n in running}
                                            for n in ["Main", "Other", "Review", "Secret", "New"]]})
        if session == "Review":
            raise RuntimeError("server_not_running")
        agents = self.roster.get(session, [])
        if args[:2] == ("agent", "list"):
            return json.dumps({"result": {"agents": agents}})
        if args[:2] == ("agent", "get"):
            target = args[2]
            found = next((a for a in agents if a["pane_id"] == target or a.get("name") == target), None)
            if not found:
                raise RuntimeError("No such agent")
            return json.dumps({"result": {"agent": found}})
        if args[:2] == ("agent", "prompt"):
            pane_id, prompt = args[2], args[3]
            self.prompts.append((session, pane_id, prompt))
            marker = re.search(r"OPENBOT_BEGIN_([A-F0-9]{16})", prompt).group(1)
            if "Create a screenshot" in prompt:
                current = next(a for a in agents if a["pane_id"] == pane_id)
                outbox = Path(current["cwd"]) / ".openbot-outbox" / marker
                (outbox / "screen.png").write_bytes(ONE_PIXEL_PNG)
                response = "Created screenshot."
                if "without marker" not in prompt:
                    response += "\nOPENBOT_IMAGE: screen.png"
            else:
                response = f"Reply from {session}/{pane_id}"
            self.replies[(session, pane_id)] = (
                f"OPENBOT_BEGIN_{marker}\n{response}\nOPENBOT_END_{marker}"
            )
            return '{"result":{"agent_status":"done"}}'
        if args[:2] == ("agent", "read"):
            return self.replies[(session, args[2])]
        raise AssertionError(f"Unexpected Herdr command {args}")

    def request(self, method, path, data=None, auth=True, raw=False, token=None):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + (token or "Z" * 48)
        con.request(method, path, body=json.dumps(data) if data is not None else None,
                    headers=headers)
        response = con.getresponse()
        status, payload = response.status, response.read()
        con.close()
        return status, payload if raw else payload.decode("utf-8")

    @staticmethod
    def message(text, file=None):
        content = [{"type": "text", "text": text}]
        if file:
            content.append({"type": "text", "text": f'Attached file "test.csv":\n\n{file}'})
        return {"threadId": "t1", "runId": "r1", "messages": [{"role": "user", "content": content}]}

    def test_discovery_shows_offline_session_and_new_agents(self):
        self.roster["Main"].append({"pane_id": "wD:p2", "agent_status": "blocked",
                                    "terminal_title_stripped": "QA", "cwd": self.tmp.name})
        status, body = self.request("GET", "/agents")
        self.assertEqual(status, 200)
        sessions = json.loads(body)["sessions"]
        self.assertEqual([s["online"] for s in sessions], [True, True, False])
        self.assertEqual(sessions[0]["agents"][-1]["title"], "QA")
        status, body = self.request("GET", "/attention")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["blocked"][0]["pane_id"], "wD:p2")
        self.assertEqual(json.loads(body)["offline_sessions"], ["Review"])

    def test_legacy_route_still_works(self):
        status, body = self.request("POST", "/ag-ui", self.message("Hello demo"))
        self.assertEqual(status, 200)
        self.assertIn("Reply from Main/wA:p1", body)
        self.assertEqual(self.prompts[-1][1], "wA:p1")

    def test_per_agent_route_and_attachment_workdir(self):
        status, body = self.request("POST", "/sessions/Main/agents/w3:p1/ag-ui",
                                    self.message("Read this CSV", "name,amount\nSample,42f\n"))
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        inbox = Path(self.tmp.name) / "pricing" / ".openbot-inbox"
        self.assertEqual(len(list(inbox.glob("*/1-test.csv"))), 1)
        self.assertFalse((Path(self.tmp.name) / "demo" / ".openbot-inbox").exists())

    def test_stopped_or_unknown_agent_never_gets_prompt(self):
        status, body = self.request("POST", "/sessions/Review/agents/w1:p1/ag-ui",
                                    self.message("Hello"))
        self.assertEqual(status, 503)
        status, body = self.request("POST", "/sessions/Other/agents/w9:p1/ag-ui",
                                    self.message("Hello"))
        self.assertEqual(status, 404)
        self.assertEqual(self.prompts, [])

    def test_busy_agent_is_not_prompted(self):
        status, body = self.request("POST", "/sessions/Other/agents/w1:p2/ag-ui",
                                    self.message("Hello"))
        self.assertEqual(status, 409)
        self.assertEqual(self.prompts, [])

    def test_unauthorized_calls_rejected(self):
        self.assertEqual(self.request("GET", "/agents", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/ag-ui", self.message("Hello"), auth=False)[0], 401)
        self.assertEqual(self.prompts, [])

    def use_route_tokens(self):
        import hashlib
        self.tok_pricing, self.tok_other = "P" * 43, "O" * 43
        self.bridge.TOKEN = ""
        self.bridge.ROUTE_HASHES = {
            ("Main", "w3:p1"): hashlib.sha256(self.tok_pricing.encode()).hexdigest(),
            ("Other", "w1:p2"): hashlib.sha256(self.tok_other.encode()).hexdigest()}

    def test_route_token_file_parsing(self):
        import hashlib
        a, b = (hashlib.sha256(x.encode()).hexdigest() for x in ("a", "b"))
        parsed = self.bridge.parse_route_hashes(f"# c\nMain/w3:p1 {a}\nOther/w1:p2 {b.upper()} # x\n")
        self.assertEqual(parsed, {("Main", "w3:p1"): a, ("Other", "w1:p2"): b})
        for bad in (f"Main/w3:p1 {a}\nMain/w3:p1 {b}\n", f"Main/w3:p1 {a}\nOther/w1:p2 {a}\n",
                    f"Main {a}\n", f"Main/notapane {a}\n", f"Main/w3:p1 {a[:-1]}\n"):
            with self.assertRaises(ValueError):
                self.bridge.parse_route_hashes(bad)

    def test_credential_scope_function(self):
        import hashlib
        scope = self.bridge.credential_scope
        routes = {("Main", "w3:p1"): hashlib.sha256(b"P" * 43).hexdigest()}
        self.assertEqual(scope("Bearer " + "P" * 43, route_hashes=routes), frozenset({("Main", "w3:p1")}))
        self.assertIsNone(scope("Bearer " + "Q" * 43, route_hashes=routes))
        self.assertIsNone(scope("Bearer " + routes[("Main", "w3:p1")], route_hashes=routes))  # hash is not the token
        self.assertIsNone(scope("P" * 43, route_hashes=routes))
        wide = hashlib.sha256(b"W" * 43).hexdigest()
        self.assertEqual(scope("Bearer " + "W" * 43, token_sha256=wide), self.bridge.ALL_ROUTES)
        self.assertIsNone(scope("Bearer " + "W" * 44, token_sha256=wide))
        self.assertEqual(scope("Bearer " + "Z" * 48, token="Z" * 48), self.bridge.ALL_ROUTES)
        self.assertIsNone(scope("Bearer " + "Z" * 47, token="Z" * 48))
        self.assertIsNone(scope("", token=""))

    def test_route_token_reaches_only_its_agent(self):
        self.use_route_tokens()
        self.assertEqual(self.request("GET", "/agents")[0], 401)  # bridge-wide token no longer accepted
        status, body = self.request("GET", "/agents", token=self.tok_pricing)
        self.assertEqual(status, 200)
        sessions = json.loads(body)["sessions"]
        self.assertEqual([(s["name"], [a["pane_id"] for a in s["agents"]]) for s in sessions],
                         [("Main", ["w3:p1"])])
        # Another agent's route, and the legacy route, are refused without prompting.
        self.assertEqual(self.request("POST", "/sessions/Main/agents/wA:p1/ag-ui", self.message("x"),
                                      token=self.tok_pricing)[0], 404)
        self.assertEqual(self.request("POST", "/sessions/Other/agents/w1:p2/ag-ui", self.message("x"),
                                      token=self.tok_pricing)[0], 404)
        self.assertEqual(self.request("POST", "/ag-ui", self.message("x"), token=self.tok_pricing)[0], 404)
        self.assertEqual(self.request("GET", "/agent", token=self.tok_pricing)[0], 404)
        self.assertEqual(self.prompts, [])
        status, body = self.request("POST", "/sessions/Main/agents/w3:p1/ag-ui", self.message("Hello"),
                                    token=self.tok_pricing)
        self.assertEqual(status, 200)
        self.assertIn("Reply from Main/w3:p1", body)
        self.assertEqual([(s, p) for s, p, _ in self.prompts], [("Main", "w3:p1")])

    def test_route_token_scopes_request_status_and_attention(self):
        self.use_route_tokens()
        self.bridge.LEDGER = self.bridge.RequestLedger(str(Path(self.tmp.name) / "scoped.sqlite3"))
        key = self.bridge.RequestLedger.key("Main", "w3:p1", "t1", "r1")
        self.request("POST", "/sessions/Main/agents/w3:p1/ag-ui", self.message("Hello"), token=self.tok_pricing)
        self.assertEqual(self.request("GET", f"/requests/{key}", token=self.tok_pricing)[0], 200)
        self.assertEqual(self.request("GET", f"/requests/{key}", token=self.tok_other)[0], 404)
        status, body = self.request("GET", "/attention", token=self.tok_other)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["offline_sessions"], [])  # Review is outside this token's scope

    def use_auto(self, deny=("Secret",)):
        self.bridge.AUTO_SESSIONS = True
        self.bridge.SESSIONS_DENY = frozenset(deny)
        self.bridge._auto_cache.update(at=0.0, sessions=None)

    def test_auto_mode_serves_running_sessions_minus_deny(self):
        self.running = ["Main", "Other", "Secret", "New"]
        self.roster["New"] = [{"pane_id": "w2:p1", "agent": "claude", "agent_status": "idle",
                               "terminal_title_stripped": "New Agent", "cwd": self.tmp.name}]
        self.roster["Secret"] = [{"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle",
                                  "terminal_title_stripped": "Hidden", "cwd": self.tmp.name}]
        self.use_auto()
        status, body = self.request("GET", "/agents")
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(listing["session_mode"], "auto")
        self.assertEqual([s["name"] for s in listing["sessions"]], ["Main", "Other", "New"])  # stopped Review absent
        # A denied session is never routed, even with the bridge-wide token.
        self.assertEqual(self.request("POST", "/sessions/Secret/agents/w1:p1/ag-ui", self.message("x"))[0], 404)
        self.assertEqual(self.prompts, [])
        status, body = self.request("POST", "/sessions/New/agents/w2:p1/ag-ui", self.message("Hello"))
        self.assertEqual(status, 200)
        self.assertIn("Reply from New/w2:p1", body)

    def test_auto_mode_failed_session_list_is_503_not_empty(self):
        self.use_auto()
        self.session_list_fails = True
        self.assertEqual(self.request("GET", "/agents")[0], 503)

    def test_configured_mode_still_honours_deny(self):
        self.bridge.SESSIONS_DENY = frozenset({"Other"})
        status, body = self.request("GET", "/agents")
        self.assertNotIn("Other", [s["name"] for s in json.loads(body)["sessions"]])
        self.assertEqual(self.request("POST", "/sessions/Other/agents/w1:p2/ag-ui", self.message("x"))[0], 404)

    def test_discovery_credential_lists_but_never_prompts(self):
        import hashlib
        disc = "D" * 43
        self.bridge.DISCOVERY_SHA256 = hashlib.sha256(disc.encode()).hexdigest()
        self.bridge.LEDGER = self.bridge.RequestLedger(str(Path(self.tmp.name) / "disc.sqlite3"))
        status, body = self.request("GET", "/agents", token=disc)
        self.assertEqual(status, 200)
        names = {s["name"]: [a["pane_id"] for a in s["agents"]] for s in json.loads(body)["sessions"]}
        self.assertEqual(names["Main"], ["wA:p1", "w3:p1"])
        self.assertEqual(self.request("GET", "/attention", token=disc)[0], 200)
        for method, path in (("POST", "/sessions/Main/agents/w3:p1/ag-ui"), ("POST", "/ag-ui"),
                             ("GET", "/agent"), ("GET", "/requests/" + "a" * 64)):
            self.assertEqual(self.request(method, path, self.message("x") if method == "POST" else None,
                                          token=disc)[0], 404, path)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.request("GET", "/agents")[0], 200)  # the bridge-wide token still works

    def test_discovery_reports_agent_kind(self):
        self.roster["Main"][0]["agent"] = "claude"
        self.roster["Main"][1]["agent"] = "hermes"
        body = json.loads(self.request("GET", "/agents")[1])
        main = next(s for s in body["sessions"] if s["name"] == "Main")
        self.assertEqual([a["agent"] for a in main["agents"]], ["claude", "hermes"])

    def test_bind_host_policy(self):
        self.bridge.check_bind_host("127.0.0.1")
        self.bridge.check_bind_host("100.100.1.2")
        for bad in ("0.0.0.0", "192.168.1.5", "8.8.8.8"):
            with self.assertRaises(ValueError):
                self.bridge.check_bind_host(bad)

    def test_multi_page_instruction_reaches_herdr_without_truncation(self):
        prompt = "Please complete the following checklist.\n" + ("A" * 12000)
        status, body = self.request("POST", "/ag-ui", self.message(prompt))
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        self.assertIn(prompt, self.prompts[-1][2])

    def test_utf8_large_instruction_is_staged_without_truncation(self):
        # A four-byte character exceeds Linux's per-argument safe size, and
        # must be passed as a private file rather than rejected or truncated.
        prompt = "🚆" * ((self.bridge.MAX_PROMPT_BYTES // 4) + 1)
        status, body = self.request("POST", "/ag-ui", self.message(prompt))
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        self.assertNotIn(prompt, self.prompts[-1][2])
        self.assertIn("full-user-instructions.txt", self.prompts[-1][2])
        staged = list((Path(self.tmp.name) / "demo" / ".openbot-inbox").glob(
            "*/1-full-user-instructions.txt"
        ))
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_text(), prompt)

    def test_screenshot_sent_from_openbot_reaches_correct_agent(self):
        message = {"threadId": "t1", "runId": "r1", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe the screenshot"},
            {"type": "image", "source": {
                "type": "data", "mimeType": "image/png",
                "value": base64.b64encode(ONE_PIXEL_PNG).decode("ascii"),
            }, "metadata": {"filename": "capture.png"}},
        ]}]}
        status, body = self.request("POST", "/sessions/Main/agents/w3:p1/ag-ui", message)
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        paths = list((Path(self.tmp.name) / "pricing" / ".openbot-inbox").glob(
            "*/1-capture.png"
        ))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), ONE_PIXEL_PNG)
        self.assertIn("1-capture.png", self.prompts[-1][2])

    def test_screenshot_from_agent_is_embedded_in_authenticated_chat(self):
        status, body = self.request("POST", "/ag-ui", self.message("Create a screenshot"))
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        self.assertIn("data:image/png;base64,", body)
        self.assertIn(base64.b64encode(ONE_PIXEL_PNG).decode("ascii"), body)
        self.assertNotIn("https://lbai-agents.leadbyai.bot/artifacts/", body)
        # Inline mode exposes no unauthenticated public artifact endpoint.
        self.assertEqual(self.request("GET", "/artifacts/" + "x" * 43 + ".png", auth=False)[0], 404)

    def test_screenshot_is_auto_attached_without_marker_over_agui(self):
        status, body = self.request("POST", "/ag-ui",
                                    self.message("Create a screenshot without marker"))
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", body)
        self.assertIn("data:image/png;base64,", body)
        self.assertNotIn("OPENBOT_IMAGE:", body)

    def test_opt_in_capability_url_mode_is_explicit_and_retrievable(self):
        self.bridge.ARTIFACT_MODE = "signed_url"
        status, body = self.request("POST", "/ag-ui", self.message("Create a screenshot"))
        self.assertEqual(status, 200)
        match = re.search(r"https://lbai-agents.leadbyai.bot(/artifacts/[A-Za-z0-9_-]+\.png)", body)
        self.assertIsNotNone(match)
        artifact_path = match.group(1)
        status, raw = self.request("GET", artifact_path, auth=False, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(raw, ONE_PIXEL_PNG)
        self.assertEqual(self.request("GET", "/artifacts/" + "x" * 43 + ".png", auth=False)[0], 404)

    def test_agent_must_not_export_other_paths_as_screenshots(self):
        root = Path(self.tmp.name)
        outbox = root / "demo" / ".openbot-outbox" / "MARKER123"
        outbox.mkdir(parents=True)
        (outbox / "escape.png").symlink_to(root / "other" / "secret.png")
        with self.assertRaises(RuntimeError):
            self.bridge.persist_reply_images("OPENBOT_IMAGE: escape.png", outbox)
        self.assertFalse((root / "other" / "secret.png").exists())

    def test_indented_image_declaration_is_embedded(self):
        # Two live Blog Desk replies used 2 and then 4 spaces; indentation is
        # now irrelevant because the per-request outbox drives image delivery.
        outbox = Path(self.tmp.name) / "demo" / ".openbot-outbox" / "INDENT123"
        outbox.mkdir(parents=True)
        (outbox / "1-image.png").write_bytes(ONE_PIXEL_PNG)
        for spaces in (0, 2, 4, 8):
            with self.subTest(spaces=spaces):
                reply = "Image copied.\n" + " " * spaces + "OPENBOT_IMAGE: 1-image.png\n"
                rendered = self.bridge.persist_reply_images(reply, outbox)
                self.assertNotIn("OPENBOT_IMAGE:", rendered)
                self.assertEqual(rendered.count("![Screenshot](data:image/png;base64,"), 1)

    def test_outbox_image_with_spaces_in_filename_is_auto_attached(self):
        outbox = Path(self.tmp.name) / "demo" / ".openbot-outbox" / "NOMARKER123"
        outbox.mkdir(parents=True)
        (outbox / "Screenshot May 2026.png").write_bytes(ONE_PIXEL_PNG)
        rendered = self.bridge.persist_reply_images("Screenshot saved.", outbox)
        self.assertIn("Screenshot saved.", rendered)
        self.assertIn("![Screenshot](data:image/png;base64,", rendered)

    def test_more_than_four_outbox_images_are_rejected(self):
        outbox = Path(self.tmp.name) / "demo" / ".openbot-outbox" / "OVERFLOW123"
        outbox.mkdir(parents=True)
        for index in range(5):
            (outbox / f"{index}.png").write_bytes(ONE_PIXEL_PNG)
        with self.assertRaisesRegex(RuntimeError, "more than 4 screenshots"):
            self.bridge.persist_reply_images("Too many", outbox)

    def test_image_declaration_inside_code_indent_is_not_interpreted(self):
        # Without a saved image, a literal Markdown example stays as text.
        outbox = Path(self.tmp.name) / "demo" / ".openbot-outbox" / "CODE123"
        outbox.mkdir(parents=True)
        reply = "Example:\n    OPENBOT_IMAGE: not-a-file.png"
        self.assertEqual(self.bridge.persist_reply_images(reply, outbox), reply)

    def test_nul_character_has_distinct_error_and_is_not_submitted(self):
        status, body = self.request("POST", "/ag-ui", self.message("Hello\x00world"))
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_nul_character")
        self.assertEqual(self.prompts, [])

    def test_request_body_limit_is_explicit_413_not_ambiguous_400(self):
        original_limit = self.bridge.MAX_BODY
        try:
            self.bridge.MAX_BODY = 100
            status, body = self.request("POST", "/ag-ui", self.message("A" * 500))
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["code"], "request_too_large")
            self.assertEqual(self.prompts, [])
        finally:
            self.bridge.MAX_BODY = original_limit

    def test_reflows_wrapped_prose_without_merging_paragraphs(self):
        original = (
            "One change I'd push for: don't name Microsoft or Copilot in the fake chat. Microsoft hasn't said what caused the\n"
            "outage. A made-up chat between a Microsoft engineer\n"
            "and Copilot suggests an AI agent caused it, which we can't back up. It would also break our rule against running\n"
            "down named vendors, on a public post with\n"
            "LeadByAI's name on it.\n\n"
            "Instead, I'd label it as satire: an on-call engineer at a very large software company and their AI assistant. The\n"
            "assistant breaks things with total confidence,\n"
            "the engineer calls it out, and it answers with the same mistake again. Everyone\n"
            "will know who we mean. Right after the chat, we\n"
            "state plainly that the real cause hasn't been disclosed."
        )
        expected = (
            "One change I'd push for: don't name Microsoft or Copilot in the fake chat. Microsoft hasn't said what caused the "
            "outage. A made-up chat between a Microsoft engineer "
            "and Copilot suggests an AI agent caused it, which we can't back up. It would also break our rule against running "
            "down named vendors, on a public post with LeadByAI's name on it.\n\n"
            "Instead, I'd label it as satire: an on-call engineer at a very large software company and their AI assistant. The "
            "assistant breaks things with total confidence, "
            "the engineer calls it out, and it answers with the same mistake again. Everyone "
            "will know who we mean. Right after the chat, we "
            "state plainly that the real cause hasn't been disclosed."
        )
        self.assertEqual(self.bridge.normalize_prose_wrapping(original), expected)
        self.assertEqual(self.bridge.normalize_prose_wrapping(expected), expected)

    def test_preserves_markdown_and_intentional_line_breaks(self):
        message = (
            "# Release notes\n"
            "- First item has quite a lot of text which wraps visually when the window is small\n"
            "  and is an indented continuation of the list\n"
            "- Second item is preserved\n\n"
            "| Key | Value |\n| --- | --- |\n| a | b |\n\n"
            "```python\n"
            "def handle_text():\n"
            "    return 'A relatively long line that should never be reflowed because it belongs to code'\n"
            "```\n\n"
            "> This quote is a deliberate long line with a single newline before the next line\n"
            "> It remains a quote\n\n"
            "Engineer: This dialogue line is intentionally separate from the next one.\n"
            "Agent: You're right, I should leave this conversation formatted exactly as written.\n\n"
            "This is a deliberately long line with an explicit Markdown hard line break.  \n"
            "The next line must remain separate.\n\n"
            "Short verse\nLike poetry\nStays apart"
        )
        self.assertEqual(self.bridge.normalize_prose_wrapping(message), message)

    def test_preserves_json_logs_and_unfenced_commands(self):
        message = (
            '{"message":"A long JSON record with all sorts of content and spacing"}\n'
            '{"level":"warning","code":409}\n\n'
            '2026-11-12 ERROR the first process had a failure that must be inspected\n'
            '2026-11-12 INFO second process resumed normally\n\n'
            'sudo systemctl status openbot-herdr-gateway.service\n'
            'sudo journalctl -u openbot-herdr-gateway.service'
        )
        self.assertEqual(self.bridge.normalize_prose_wrapping(message), message)

    def test_agent_reply_is_normalized_before_ag_ui_stream(self):
        wrapped = (
            "This message has a deliberately lengthy initial line that wraps awkwardly halfway through a single\n"
            "sentence and leaves a strange short final line in the OpenBot interface."
        )
        with patch.object(self.bridge, "extract_reply", return_value=wrapped):
            status, body = self.request("POST", "/ag-ui", self.message("Hi"))
        self.assertEqual(status, 200)
        events = [json.loads(line.removeprefix("data: ")) for line in body.splitlines()
                  if line.startswith("data: ")]
        content = next(event for event in events if event.get("type") == "TEXT_MESSAGE_CONTENT")
        self.assertEqual(content["delta"], wrapped.replace("\n", " "))

    @staticmethod
    def events(body):
        return [json.loads(line.removeprefix("data: ")) for line in body.splitlines()
                if line.startswith("data: ")]

    def enable_bounded_queue(self, seconds=1):
        self.bridge.BUSY_WAIT_SECONDS = seconds
        self.bridge.HEARTBEAT_SECONDS = 0.03
        self.bridge.STATE_POLL_SECONDS = 0.03

    def test_busy_agent_waits_then_delivers_same_turn(self):
        self.enable_bounded_queue()
        agent = self.roster["Main"][0]
        agent["agent_status"] = "working"

        def finish_previous_work():
            time.sleep(0.14)
            agent["agent_status"] = "idle"

        finishing = threading.Thread(target=finish_previous_work)
        finishing.start()
        try:
            status, body = self.request("POST", "/ag-ui", self.message("Deliver when ready"))
        finally:
            finishing.join(timeout=2)
        events = self.events(body)
        self.assertEqual(status, 200)
        self.assertTrue(any(event.get("name") == "agent-fabric.progress" and
                            event["value"]["state"] == "queued" for event in events))
        self.assertEqual(events[-1]["type"], "RUN_FINISHED")
        self.assertEqual(len(self.prompts), 1)

    def test_busy_agent_wait_expires_without_submitting(self):
        self.enable_bounded_queue(seconds=0.12)
        self.roster["Main"][0]["agent_status"] = "working"
        status, body = self.request("POST", "/ag-ui", self.message("Never delivered"))
        self.assertEqual(status, 200)  # AG-UI errors use RUN_ERROR after stream starts.
        events = self.events(body)
        self.assertEqual(events[-1]["type"], "RUN_ERROR")
        self.assertIn("No prompt submitted", events[-1]["message"])
        self.assertEqual(self.prompts, [])

    def test_blocked_agent_is_not_queued_or_prompted(self):
        self.enable_bounded_queue()
        self.roster["Main"][0]["agent_status"] = "blocked"
        status, body = self.request("POST", "/ag-ui", self.message("Needs approval"))
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1]["type"], "RUN_ERROR")
        self.assertEqual(self.prompts, [])

    def test_replaced_agent_is_not_prompted_after_queue_wait(self):
        self.enable_bounded_queue()
        agent = self.roster["Main"][0]
        agent["agent_status"] = "working"
        agent["agent_session"] = {"value": "original"}

        def replace_occupant():
            time.sleep(0.12)
            agent["agent_session"] = {"value": "replacement"}
            agent["agent_status"] = "idle"

        replacing = threading.Thread(target=replace_occupant)
        replacing.start()
        try:
            status, body = self.request("POST", "/ag-ui", self.message("Wrong process must not receive this"))
        finally:
            replacing.join(timeout=2)
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1]["type"], "RUN_ERROR")
        self.assertIn("session changed", self.events(body)[-1]["message"])
        self.assertEqual(self.prompts, [])

    def test_timed_out_submission_reconciles_without_duplicate_prompt(self):
        self.bridge.RECOVERY_SECONDS = 1
        self.bridge.HEARTBEAT_SECONDS = 0.02
        base = self.fake_command

        def timeout_once(*args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                base(*args, **kwargs)  # Prompt WAS submitted.
                raise RuntimeError('Herdr command failed: {"code":"timeout","message":"timed out waiting for agent status"}')
            return base(*args, **kwargs)

        self.bridge.command = timeout_once
        status, body = self.request("POST", "/ag-ui", self.message("Submit once"))
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1]["type"], "RUN_FINISHED")
        self.assertEqual(len(self.prompts), 1)

    def test_ambiguous_timeout_never_retries_submitted_prompt(self):
        self.bridge.RECOVERY_SECONDS = 0.12
        self.bridge.HEARTBEAT_SECONDS = 0.02
        self.bridge.STATE_POLL_SECONDS = 0.02
        base = self.fake_command

        def timeout_without_correlated_reply(*args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                base(*args, **kwargs)  # One submission; pretend result is not visible.
                raise RuntimeError('timed out waiting for agent status')
            if args[:2] == ("agent", "read"):
                return "No correlated answer in recent terminal output"
            return base(*args, **kwargs)

        self.bridge.command = timeout_without_correlated_reply
        status, body = self.request("POST", "/ag-ui", self.message("Do not retry"))
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1]["type"], "RUN_ERROR")
        self.assertIn("could not be correlated", self.events(body)[-1]["message"])
        self.assertEqual(len(self.prompts), 1)

    def test_early_idle_status_waits_for_correlated_reply_before_next_prompt(self):
        """A successful Herdr --wait return may precede its reply marker."""
        self.enable_bounded_queue(seconds=2)
        self.bridge.RECOVERY_SECONDS = 1
        submitted = threading.Event()
        allow_reply = threading.Event()
        recorded = {}
        base = self.fake_command

        def early_idle(*args, **kwargs):
            if args[:2] == ("agent", "prompt") and "Message one" in args[3]:
                self.prompts.append((kwargs.get("session"), args[2], args[3]))
                marker = re.search(r"OPENBOT_BEGIN_([A-F0-9]{16})", args[3]).group(1)
                recorded["first_reply"] = (
                    f"OPENBOT_BEGIN_{marker}\nTEST A COMPLETE\nOPENBOT_END_{marker}"
                )
                submitted.set()
                return '{"result":{"agent_status":"done"}}'
            if args[:2] == ("agent", "read") and not allow_reply.is_set():
                return "First reply has not appeared in Herdr yet"
            if args[:2] == ("agent", "read") and len(self.prompts) == 1:
                return recorded["first_reply"]
            return base(*args, **kwargs)

        self.bridge.command = early_idle
        results = {}

        def send(number, text):
            body = self.message(text)
            body["runId"] = f"run{number}"
            body["threadId"] = f"thread{number}"
            results[number] = self.request("POST", "/ag-ui", body)

        first = threading.Thread(target=send, args=(1, "Message one"))
        second = threading.Thread(target=send, args=(2, "Message two"))
        first.start()
        self.assertTrue(submitted.wait(timeout=2))
        second.start()
        try:
            time.sleep(0.15)
            self.assertEqual(len(self.prompts), 1,
                             "Second prompt must not bypass uncorrelated first request")
            allow_reply.set()
            first.join(timeout=3)
            second.join(timeout=3)
        finally:
            allow_reply.set()
            first.join(timeout=3)
            second.join(timeout=3)

        self.assertEqual([results[i][0] for i in (1, 2)], [200, 200])
        self.assertTrue(all(self.events(results[i][1])[-1]["type"] == "RUN_FINISHED"
                            for i in (1, 2)))
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("TEST A COMPLETE", results[1][1])

    def test_stream_disconnect_after_submission_does_not_abandon_ledger(self):
        """A browser that cancels its stream cannot abort submitted Herdr work."""
        self.bridge.LEDGER = self.bridge.RequestLedger(
            str(Path(self.tmp.name) / "requests.sqlite3"))
        self.bridge.HEARTBEAT_SECONDS = 0.02
        original = self.fake_command

        def slow_command(*args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                time.sleep(0.10)
            return original(*args, **kwargs)

        self.bridge.command = slow_command
        original_send_event = self.bridge.Handler.send_event

        def disconnect_when_reserved(handler, payload):
            if payload.get("name") == "agent-fabric.request":
                raise BrokenPipeError("browser closed the AG-UI stream")
            return original_send_event(handler, payload)

        with patch.object(self.bridge.Handler, "send_event", new=disconnect_when_reserved), \
             patch.object(self.bridge.Handler, "send_reply", side_effect=BrokenPipeError):
            status, _ = self.request("POST", "/ag-ui", self.message("One task"))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.prompts), 1)
        key = self.bridge.LEDGER.key("Main", "wA:p1", "t1", "r1")
        record = self.bridge.LEDGER.get(key)
        self.assertEqual(record["state"], "finished")
        self.assertIn("Reply from Main/wA:p1", record["reply"])

    def test_completed_request_is_replayed_without_resubmitting(self):
        ledger_file = str(Path(self.tmp.name) / "requests.sqlite3")
        self.bridge.LEDGER = self.bridge.RequestLedger(ledger_file)
        self.enable_bounded_queue()
        request = self.message("One submission, multiple transport attempts")
        first_status, first_body = self.request("POST", "/ag-ui", request)
        second_status, second_body = self.request("POST", "/ag-ui", request)
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(self.events(first_body)[-1]["type"], "RUN_FINISHED")
        self.assertEqual(self.events(second_body)[-1]["type"], "RUN_FINISHED")
        self.assertEqual(len(self.prompts), 1)
        key = self.bridge.LEDGER.key("Main", "wA:p1", "t1", "r1")
        self.assertEqual(self.bridge.LEDGER.get(key)["state"], "finished")
        self.assertEqual(Path(ledger_file).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request("GET", "/requests/" + key, auth=False)[0], 401)
        # Reinstantiate from disk: a restarted gateway still must not resend.
        self.bridge.LEDGER = self.bridge.RequestLedger(ledger_file)
        third_status, third_body = self.request("POST", "/ag-ui", request)
        self.assertEqual(third_status, 200)
        self.assertEqual(self.events(third_body)[-1]["type"], "RUN_FINISHED")
        self.assertEqual(len(self.prompts), 1)

    def test_uncertain_request_uses_stored_run_id_to_prevent_duplicate(self):
        self.enable_bounded_queue()
        self.bridge.LEDGER = self.bridge.RequestLedger(str(Path(self.tmp.name) / "requests.sqlite3"))
        self.bridge.RECOVERY_SECONDS = 0
        base = self.fake_command

        def timed_out(*args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                base(*args, **kwargs)
                raise RuntimeError('timed out waiting for agent status')
            return base(*args, **kwargs)

        self.bridge.command = timed_out
        request = self.message("Never submit a second time")
        status, body = self.request("POST", "/ag-ui", request)
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1]["type"], "RUN_ERROR")
        status, body = self.request("POST", "/ag-ui", request)
        self.assertEqual(status, 200)
        self.assertIn("No duplicate prompt sent", self.events(body)[-1]["message"])
        self.assertEqual(len(self.prompts), 1)
        key = self.bridge.LEDGER.key("Main", "wA:p1", "t1", "r1")
        status, data = self.request("GET", "/requests/" + key)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["state"], "uncertain")
        self.assertIsNone(json.loads(data)["reply"])
        status, attention = self.request("GET", "/attention")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(attention)["unresolved_requests"][0]["id"], key)

    def test_same_run_identifier_with_different_input_is_rejected(self):
        self.enable_bounded_queue()
        self.bridge.LEDGER = self.bridge.RequestLedger(str(Path(self.tmp.name) / "requests.sqlite3"))
        self.assertEqual(self.request("POST", "/ag-ui", self.message("Original"))[0], 200)
        status, body = self.request("POST", "/ag-ui", self.message("Different text"))
        self.assertEqual(status, 409)
        self.assertIn("reused", body)
        self.assertEqual(len(self.prompts), 1)

    def test_two_messages_to_same_agent_are_serialized(self):
        self.enable_bounded_queue(seconds=3)
        started = threading.Event()
        original = self.fake_command

        def slow_first(*args, **kwargs):
            if args[:2] == ("agent", "prompt") and "Message one" in args[3]:
                started.set()
                time.sleep(0.18)
            return original(*args, **kwargs)

        self.bridge.command = slow_first
        results = {}

        def send(index, text):
            body = self.message(text)
            body["runId"] = "r" + str(index)
            results[index] = self.request("POST", "/ag-ui", body)

        first = threading.Thread(target=send, args=(1, "Message one"))
        second = threading.Thread(target=send, args=(2, "Message two"))
        first.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            second.start()
            second.join(timeout=4)
        finally:
            first.join(timeout=4)
        self.assertEqual([results[i][0] for i in (1, 2)], [200, 200])
        self.assertTrue(all(self.events(results[i][1])[-1]["type"] == "RUN_FINISHED"
                            for i in (1, 2)))
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("Message one", self.prompts[0][2])
        self.assertIn("Message two", self.prompts[1][2])


if __name__ == "__main__":
    unittest.main()
