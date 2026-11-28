"""The OpenBot browser for Herdr agents, and the activity line, through the real bridge."""
import base64
import json
import re
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdr_browser  # noqa: E402
import openbot_browser_cli  # noqa: E402
from test_bridge import ONE_PIXEL_PNG, BridgeTests  # noqa: E402

RUN = "signed-run-assertion"
CLAUDE_WORKING = (Path(__file__).resolve().parent / "fixtures" / "screens" / "claude-running.txt").read_text()


class FakeOpenBot:
    """OpenBot's browser route: records what the gateway sent and answers like OpenBot."""

    def __init__(self, test):
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append(({k.lower(): v for k, v in self.headers.items()}, body))
                if self.headers.get("x-agent-fabric-credential") != "Bearer " + "Z" * 48 or body.get("run") != RUN:
                    answer = (401, {"error": "Not authorised."})
                elif body["action"] == "screenshot":
                    answer = (200, {"image": base64.b64encode(ONE_PIXEL_PNG).decode(), "url": "https://example.com/"})
                else:
                    answer = (200, {"title": "Example", "url": "https://example.com/", "text": "Hello from the page"})
                data = json.dumps(answer[1]).encode()
                self.send_response(answer[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/agent-fabric/browser"


class CallbackAddress(unittest.TestCase):
    def test_only_https_or_a_private_address(self):
        ok = ["https://bot.example.com/api/agent-fabric/browser", "http://127.0.0.1:3000/api/agent-fabric/browser",
              "http://100.67.1.2:3000/api/agent-fabric/browser"]
        bad = ["", "http://bot.example.com/api", "https://user:pw@bot.example.com/api", "ftp://x/y",
               "https://bot.example.com/api?x=1"]
        for value in ok:
            self.assertEqual(herdr_browser.callback_url(value), value)
        for value in bad:
            self.assertIsNone(herdr_browser.callback_url(value), value)


class BrowserThroughTheBridge(BridgeTests):
    """Reuses the bridge harness (test_bridge.py); only the tests below run from this class."""

    def setUp(self):
        super().setUp()
        self.openbot = FakeOpenBot(self)
        self.bridge.PORT = self.server.server_port
        patcher = patch.object(herdr_browser, "CALLBACK_URL", self.openbot.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent_did = []

    def fake_command(self, *args, session=None, **kwargs):
        if args[:2] == ("agent", "prompt") and "Browse the example page" in args[3]:
            prompt = args[3]
            turn = re.search(r"python3 \S+openbot_browser_cli\.py (\S+) navigate", prompt)
            self.agent_did.append(("hint", bool(turn)))
            if turn:
                pane = next(a for a in self.roster[session] if a["pane_id"] == args[2])
                path = Path(turn.group(1))
                path = path if path.is_absolute() else Path(pane["cwd"]) / path
                self.agent_did.append(("credential on disk", "Z" * 48 in path.read_text()))
                for argv in ([str(path), "navigate", "https://example.com/"], [str(path), "screenshot"]):
                    self.agent_did.append((argv[1], openbot_browser_cli.main(argv)))
                self.turn_file = path
        return super().fake_command(*args, session=session, **kwargs)

    def browse(self, forwarded=True):
        message = self.message("Browse the example page")
        if forwarded:
            message["forwardedProps"] = {"openbotRun": RUN}
        status, body = self.request("POST", "/ag-ui", message)
        self.assertEqual(status, 200)
        return self.events(body)

    def test_an_agent_browses_on_the_persons_openbot_computer(self):
        events = self.browse()
        self.assertIn(("hint", True), self.agent_did)
        self.assertIn(("credential on disk", False), self.agent_did)
        self.assertIn(("navigate", 0), self.agent_did)
        self.assertIn(("screenshot", 0), self.agent_did)
        # OpenBot got the signed run and the credential OpenBot itself presented, nothing else.
        headers, body = self.openbot.calls[0]
        self.assertEqual(body, {"run": RUN, "action": "navigate", "url": "https://example.com/"})
        self.assertEqual(headers["x-agent-fabric-credential"], "Bearer " + "Z" * 48)
        # The screenshot reached the reply through the existing outbox.
        reply = next(e for e in events if e.get("type") == "TEXT_MESSAGE_CONTENT")["delta"]
        self.assertIn("data:image/png;base64,", reply)
        # The turn is over: its file is gone and its key no longer works.
        self.assertFalse(self.turn_file.exists())
        self.assertFalse(herdr_browser._TURNS)

    def test_no_signed_run_means_no_browser(self):
        self.browse(forwarded=False)
        self.assertEqual(self.agent_did, [("hint", False)])
        self.assertEqual(self.openbot.calls, [])

    def test_unknown_keys_are_refused_without_reaching_openbot(self):
        status, body = self.request("POST", "/openbot-browser/" + "A" * 43, {"action": "read"}, auth=False)
        self.assertEqual(status, 404)
        self.assertEqual(self.openbot.calls, [])


class ActivityThroughTheBridge(BridgeTests):
    def setUp(self):
        super().setUp()
        self.bridge.ACTIVITY_SECONDS = 0.05

    def fake_command(self, *args, session=None, **kwargs):
        if args[:2] == ("agent", "read") and "visible" in args:
            return CLAUDE_WORKING
        if args[:2] == ("agent", "prompt"):
            time.sleep(0.4)
        return super().fake_command(*args, session=session, **kwargs)

    def test_progress_says_what_the_agent_is_doing_once_per_change(self):
        status, body = self.request("POST", "/ag-ui", self.message("Do some work"))
        lines = [e["value"] for e in self.events(body)
                 if e.get("name") == "agent-fabric.progress" and "line" in e.get("value", {})]
        self.assertEqual(lines, [{"state": "working", "line": "Listing files in current directory",
                                  "elapsedSeconds": 2}])

    def test_switched_off_keeps_the_bare_heartbeat(self):
        self.bridge.ACTIVITY_LINE = False
        self.bridge.HEARTBEAT_SECONDS = 0.1
        status, body = self.request("POST", "/ag-ui", self.message("Do some work"))
        progress = [e["value"] for e in self.events(body) if e.get("name") == "agent-fabric.progress"]
        self.assertTrue(progress)
        self.assertTrue(all("line" not in value for value in progress))


class InstallerPins(unittest.TestCase):
    def test_install_sh_pins_the_new_modules_as_they_are(self):
        import hashlib
        root = Path(__file__).resolve().parents[1]
        installer = (root / "install.sh").read_text()
        for module in ("activity_line.py", "herdr_browser.py", "openbot_browser_cli.py"):
            digest = hashlib.sha256((root / module).read_bytes()).hexdigest()
            self.assertIn(f'[{module}]="{digest}"', installer, f"update install.sh's pin for {module}")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(CallbackAddress))
    suite.addTests(loader.loadTestsFromTestCase(InstallerPins))
    for case in (BrowserThroughTheBridge, ActivityThroughTheBridge):
        for name in vars(case):
            if name.startswith("test_"):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
