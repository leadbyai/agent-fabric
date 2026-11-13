"""Offline tests for the Hermes backend. No real Hermes gateway is touched."""

import asyncio
import contextlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))
import hermes_bridge  # noqa: E402

TOKEN = "t" * 32
PROFILE_ID = "/home/example/.hermes/profiles/pilot"


class FakeClient:
    """Canonical-WS stand-in: records RPCs and plays scripted events after prompt.submit."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.events = asyncio.Queue()

    async def rpc(self, method, **params):
        self.gateway.calls.append((method, params))
        if method == "profiles.list":
            return {"install_id": "install-1", "profiles": [
                {"name": "default", "display_name": "Default"},
                {"name": "pilot", "display_name": "Pilot Bot"},
                {"name": "other", "display_name": "Other"}]}
        if method == "session.create":
            self.gateway.created += 1
            sid = f"S{self.gateway.created}"
            self.gateway.sessions[sid] = []
            return {"stored_session_id": sid, "session_id": "live-" + sid}
        if method == "session.resume":
            sid = params["session_id"]
            if sid not in self.gateway.sessions:
                raise RuntimeError("not_found")
            return {"stored_session_id": sid, "subscription_id": "sub"}
        if method == "prompt.submit":
            sid, input_id = params["session_id"], params["input_id"]
            admission = "A-" + input_id[:8]
            if input_id not in self.gateway.inputs:
                self.gateway.inputs.add(input_id)
                self.gateway.sessions[sid].append(params["text"])
            for kind, payload in self.gateway.script(sid, admission):
                await self.events.put({"method": "event", "params": {
                    "session_id": sid, "type": kind, "payload": payload, "admission_id": admission}})
            return {"admission_id": admission}
        if method in ("approval.respond", "clarify.respond"):
            return {"status": "ok"}
        raise AssertionError(f"unexpected rpc {method}")


class FakeGateway:
    def __init__(self, state="ready"):
        self.state = state
        self.calls, self.sessions, self.inputs, self.created = [], {}, set(), 0
        self.script = lambda sid, adm: [("message.delta", {"text": "Hello "}),
                                        ("message.complete", {"text": "Hello there", "status": "complete"})]

    def discover(self, name):
        if self.state != "ready" or name == "absent":
            return SimpleNamespace(state="absent", endpoint=None, reason_code=None)
        pid = PROFILE_ID if name == "pilot" else f"/home/example/.hermes/profiles/{name}"
        return SimpleNamespace(state="ready", reason_code=None,
                               endpoint=SimpleNamespace(profile_id=pid, instance_id="run-1"))

    def profile_home(self, name):
        return Path("/home/example/.hermes/profiles") / name

    @contextlib.asynccontextmanager
    async def _connect(self):
        yield FakeClient(self)

    def connect(self, endpoint):
        return self._connect()


class HermesBridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = FakeGateway()
        self.ledger = hermes_bridge.HermesLedger(str(Path(tmp.name) / "ledger.sqlite3"))
        handler = type("H", (hermes_bridge.HermesHandler,), {
            "config": {"token": TOKEN, "profiles": ("pilot",), "timeout": 30, "approval_wait": 0,
                       "allow_native": False},
            "connector": self.gateway, "ledger": self.ledger,
            "log_message": lambda *a: None})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        data = response.read().decode()
        return response.status, data

    def run_turn(self, text="hi", thread="thread-1", run="run-1", profile="pilot"):
        return self.request("POST", f"/profiles/{profile}/ag-ui", {
            "threadId": thread, "runId": run,
            "messages": [{"id": "m1", "role": "user", "content": text}]})

    @staticmethod
    def events(data):
        return [json.loads(line[5:]) for line in data.splitlines() if line.startswith("data:")]

    def test_health_needs_no_auth_but_everything_else_does(self):
        self.assertEqual(self.request("GET", "/health", token="wrong")[0], 200)
        self.assertEqual(self.request("GET", "/agents", token="wrong")[0], 401)
        self.assertEqual(self.run_turn.__func__(self, profile="pilot")[0], 200)
        self.assertEqual(self.request("POST", "/profiles/pilot/ag-ui", {}, token="wrong")[0], 401)

    def test_agents_lists_only_allowed_profiles_with_stable_identity(self):
        status, data = self.request("GET", "/agents")
        self.assertEqual(status, 200)
        listing = json.loads(data)
        self.assertEqual([p["name"] for p in listing["profiles"]], ["pilot"])
        pilot = listing["profiles"][0]
        self.assertEqual((pilot["install_id"], pilot["profile_id"]), ("install-1", PROFILE_ID))
        self.assertEqual(pilot["display_name"], "Pilot Bot")
        self.assertEqual(pilot["ag_ui_path"], "/profiles/pilot/ag-ui")

    def test_profile_not_allowed_is_refused_without_contacting_hermes(self):
        self.assertEqual(self.run_turn(profile="other")[0], 404)
        self.assertEqual(self.gateway.calls, [])

    def test_round_trip_streams_text_and_creates_one_gui_session(self):
        status, data = self.run_turn()
        self.assertEqual(status, 200)
        events = self.events(data)
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "RUN_STARTED")
        self.assertEqual(types[-1], "RUN_FINISHED")
        text = "".join(e.get("delta", "") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        self.assertEqual(text, "Hello there")
        creates = [p for m, p in self.gateway.calls if m == "session.create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["source"], "gui")
        self.assertNotIn("unattended", [k for m, p in self.gateway.calls for k in p])

    def test_same_thread_reuses_its_session(self):
        self.run_turn(run="r1")
        self.run_turn(run="r2", text="again")
        self.assertEqual(self.gateway.created, 1)
        self.assertEqual(self.gateway.sessions["S1"], ["hi", "again"])
        self.assertIn("session.resume", [m for m, _ in self.gateway.calls])

    def test_retried_run_is_replayed_not_resubmitted(self):
        self.run_turn()
        before = len([m for m, _ in self.gateway.calls if m == "prompt.submit"])
        status, data = self.run_turn()
        self.assertEqual(status, 200)
        after = len([m for m, _ in self.gateway.calls if m == "prompt.submit"])
        self.assertEqual(before, after)
        self.assertIn("Hello there", data)

    def test_reused_run_id_with_new_text_is_refused(self):
        self.run_turn()
        self.assertEqual(self.run_turn(text="different")[0], 409)

    def test_approval_is_denied_and_surfaced(self):
        self.gateway.script = lambda sid, adm: [
            ("approval.request", {"prompt_id": "p1", "execution_generation": 3, "kind": "approval",
                                  "tool_name": "terminal", "choices": ["once", "session", "deny"]}),
            ("message.complete", {"text": "ok", "status": "complete"})]
        status, data = self.run_turn()
        self.assertEqual(status, 200)
        responses = [p for m, p in self.gateway.calls if m == "approval.respond"]
        self.assertEqual(responses, [{"session_id": "S1", "execution_generation": 3,
                                      "prompt_id": "p1", "choice": "deny"}])
        self.assertIn("agent-fabric.approval", data)
        self.assertIn("denied", data)

    def test_tool_calls_are_custom_events_not_agui_tool_calls(self):
        self.gateway.script = lambda sid, adm: [
            ("tool.start", {"tool_name": "web_search"}), ("tool.complete", {"tool_name": "web_search"}),
            ("message.complete", {"text": "done", "status": "complete"})]
        data = self.run_turn()[1]
        self.assertNotIn("TOOL_CALL", data)
        self.assertEqual(data.count("agent-fabric.tool"), 2)

    def test_other_sessions_events_are_ignored(self):
        def script(sid, adm):
            return [("message.delta", {"text": "LEAK"}),
                    ("message.complete", {"text": "fine", "status": "complete"})]
        original = FakeClient.rpc

        async def rpc(client, method, **params):
            result = await original(client, method, **params)
            if method == "prompt.submit":
                # A foreign session's event arrives first on the same connection.
                await client.events.put({"method": "event", "params": {
                    "session_id": "FOREIGN", "type": "message.delta", "payload": {"text": "LEAK"}}})
            return result
        self.gateway.script = lambda sid, adm: [("message.complete", {"text": "fine", "status": "complete"})]
        FakeClient.rpc = rpc
        self.addCleanup(setattr, FakeClient, "rpc", original)
        data = self.run_turn()[1]
        self.assertNotIn("LEAK", data)
        self.assertIn("fine", data)

    def test_failed_turn_marks_uncertain_and_never_resends(self):
        self.gateway.script = lambda sid, adm: [("message.complete", {"text": "", "status": "error",
                                                                     "failure_reason": "provider"})]
        data = self.run_turn()[1]
        self.assertIn("RUN_ERROR", data)
        again = self.run_turn()[1]
        self.assertIn("already uncertain", again)
        self.assertEqual(len([m for m, _ in self.gateway.calls if m == "prompt.submit"]), 1)

    def test_gateway_not_ready_is_503_and_nothing_is_started(self):
        self.gateway.state = "absent"
        self.assertEqual(self.run_turn()[0], 503)
        self.assertEqual(self.gateway.calls, [])

    def test_attachments_are_refused_clearly(self):
        status, data = self.request("POST", "/profiles/pilot/ag-ui", {
            "threadId": "t", "runId": "r", "messages": [{"id": "m", "role": "user", "content": [
                {"type": "text", "text": "see"},
                {"type": "binary", "mimeType": "image/png", "data": "iVBORw0KGgo="}]}]})
        self.assertIn(status, (400,))

    def test_bound_native_session_needs_opt_in(self):
        self.gateway.sessions["NATIVE"] = []
        self.ledger.map_thread(PROFILE_ID, "bound-thread", "NATIVE", origin="bound")
        data = self.run_turn(thread="bound-thread")[1]
        self.assertIn("RUN_ERROR", data)
        self.assertEqual(self.gateway.sessions["NATIVE"], [])
        self.server.RequestHandlerClass.config["allow_native"] = True
        data = self.run_turn(thread="bound-thread", run="r2")[1]
        self.assertIn("RUN_FINISHED", data)
        self.assertEqual(self.gateway.sessions["NATIVE"], ["hi"])
        self.assertEqual(self.gateway.created, 0)

    def test_hash_only_bearer(self):
        import hashlib
        real = "r" * 43
        config = {"token": "", "token_sha256": hashlib.sha256(real.encode()).hexdigest()}
        self.assertTrue(hermes_bridge.bearer_ok("Bearer " + real, config))
        self.assertFalse(hermes_bridge.bearer_ok("Bearer " + real + "x", config))
        self.assertFalse(hermes_bridge.bearer_ok("", config))
        self.assertFalse(hermes_bridge.bearer_ok(real, config))  # missing "Bearer "
        self.assertFalse(hermes_bridge.bearer_ok("Bearer " + config["token_sha256"], config))  # the hash is not the token
        self.assertFalse(hermes_bridge.bearer_ok("Bearer anything", {"token": "", "token_sha256": ""}))

    def test_hash_only_config_over_http(self):
        import hashlib
        real = "s" * 43
        self.server.RequestHandlerClass.config = {**self.server.RequestHandlerClass.config,
                                                  "token": "", "token_sha256": hashlib.sha256(real.encode()).hexdigest()}
        self.assertEqual(self.request("GET", "/agents", token=real)[0], 200)
        self.assertEqual(self.request("GET", "/agents", token=TOKEN)[0], 401)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", "/agents")
        self.assertEqual(conn.getresponse().status, 401)

    def test_bind_host_policy(self):
        hermes_bridge.check_bind_host("127.0.0.1")
        hermes_bridge.check_bind_host("100.100.1.2")
        for bad in ("0.0.0.0", "192.168.1.5", "8.8.8.8"):
            with self.assertRaises(ValueError):
                hermes_bridge.check_bind_host(bad)


if __name__ == "__main__":
    unittest.main()
