"""Offline tests. No real Herdr session or production endpoint is touched."""

import http.client
import importlib.util
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
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
            self.replies[(session, pane_id)] = (
                f"OPENBOT_BEGIN_{marker}\nReply from {session}/{pane_id}\nOPENBOT_END_{marker}"
            )
            return '{"result":{"agent_status":"done"}}'
        if args[:2] == ("agent", "read"):
            return self.replies[(session, args[2])]
        raise AssertionError(f"Unexpected Herdr command {args}")

    def request(self, method, path, data=None, auth=True):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + "Z" * 48
        con.request(method, path, body=json.dumps(data) if data is not None else None,
                    headers=headers)
        response = con.getresponse()
        status, body = response.status, response.read().decode("utf-8")
        con.close()
        return status, body

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


if __name__ == "__main__":
    unittest.main()
