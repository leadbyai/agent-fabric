"""Offline tests for the Hermes backend. No real Hermes gateway is touched."""

import asyncio
import base64
import contextlib
import http.client
import json
import re
import sys
import tempfile
import threading
import unittest
import unittest.mock
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
        if method == "runtime.describe":
            if self.gateway.describe_params is None:
                raise RuntimeError("unknown_method")
            return {"session_create": {"parameters": self.gateway.describe_params}}
        if method == "session.create":
            title = params.get("title")
            launch = {"source": params.get("source"),
                      "launch_request": {k: v for k, v in params.items() if k != "request_id"}}
            if title in self.gateway.titles:  # Hermes resolves a title as an identity
                sid = self.gateway.titles[title]
                return {"stored_session_id": sid, "session_id": "live-" + sid, "info": self.gateway.info[sid]}
            if self.gateway.force_session:
                return {"stored_session_id": self.gateway.force_session, "session_id": "live-x", "info": launch}
            self.gateway.created += 1
            sid = f"S{self.gateway.created}"
            self.gateway.sessions[sid] = []
            self.gateway.titles[title] = sid
            self.gateway.info[sid] = launch
            return {"stored_session_id": sid, "session_id": "live-" + sid, "info": launch}
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
        self.describe_params = ["model", "follow_profile_config", "request_id", "client"]
        self.isolated_problems = []
        self.titles, self.force_session, self.info = {}, None, {}
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

    def isolated_profile_problems(self):
        return list(self.isolated_problems)

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

    def test_shared_session_only_handles_controls_from_its_own_admission(self):
        async def check():
            events = []
            client = FakeClient(self.gateway)
            turn = hermes_bridge.Turn(client, events.append, session_id="native", input_id="input",
                                      text="hello", timeout=30, approval_wait=0)
            turn.admission = "ours"
            foreign = {"prompt_id": "foreign", "execution_generation": 1, "choices": ["deny"]}
            await turn.handle("approval.request", "tui", foreign)
            await turn.handle("clarify.request", None, foreign)
            await turn.handle("message.complete", "tui", {"text": "TUI message"})
            self.assertEqual(self.gateway.calls, [])
            self.assertEqual(events, [])
            self.assertIsNone(turn.final)
            await turn.handle("approval.request", "ours", foreign)
            self.assertEqual([method for method, _ in self.gateway.calls], ["approval.respond"])

        asyncio.run(check())

    def test_other_sessions_events_are_ignored(self):
        def script(sid, adm):
            return [("message.delta", {"text": "LEAK"}),
                    ("message.complete", {"text": "fine", "status": "complete"})]
        original = FakeClient.rpc

        async def rpc(fake, method, **params):  # not "client": that is also a session.create parameter
            result = await original(fake, method, **params)
            if method == "prompt.submit":
                # A foreign session's event arrives first on the same connection.
                await fake.events.put({"method": "event", "params": {
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

    def test_pinned_screenshot_roundtrip_and_replay(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "5XcAAAAASUVORK5CYII="
        )
        media = Path(self.ledger.path).parent / "media"
        cache = media / "cache"
        cache.mkdir(parents=True)
        self.gateway.image_cache = lambda: cache
        self.gateway.sessions["NATIVE"] = []
        self.server.RequestHandlerClass.config.update(
            bound_thread="pinned", bound_session="NATIVE", allow_native=True, media_root=str(media),
        )

        def reply(sid, admission):
            params = self.gateway.calls[-1][1]
            assert params["attachments"][0]["mime"] == "image/png"
            assert Path(params["attachments"][0]["path"]).read_bytes() == png
            path = Path(re.search(r"directly in (.*?)/\. OpenBot", params["text"])[1])
            (path / "reply.png").write_bytes(png)
            return [("message.delta", {"text": "I read it"}),
                    ("message.complete", {"text": "I read it", "status": "complete"})]

        self.gateway.script = reply
        body = {"threadId": "pinned", "runId": "media", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Read the image"},
            {"type": "image", "source": {"type": "data", "mimeType": "image/png",
                                         "value": base64.b64encode(png).decode()}},
        ]}]}
        route = "/profiles/pilot/ag-ui"
        for _ in range(2):
            status, data = self.request("POST", route, body)
            self.assertEqual(status, 200)
            self.assertIn("RUN_FINISHED", data)
            self.assertIn("data:image/png;base64,", data)
        body["messages"][0]["content"][1]["source"]["value"] = base64.b64encode(png + b"changed").decode()
        self.assertEqual(self.request("POST", route, body)[0], 409)
        self.assertEqual(len(self.gateway.sessions["NATIVE"]), 1)
        self.assertEqual(list(media.iterdir()), [cache])
        self.assertEqual(list(cache.iterdir()), [])

    def test_pinned_bridge_accepts_only_its_exact_thread_and_existing_session(self):
        self.gateway.sessions["NATIVE"] = []
        self.server.RequestHandlerClass.config.update(
            bound_thread="bound-thread", bound_session="NATIVE", allow_native=True,
        )
        self.assertEqual(self.run_turn(thread="other-thread")[0], 404)
        self.assertEqual(self.gateway.calls, [])
        self.assertIn("RUN_FINISHED", self.run_turn(thread="bound-thread")[1])
        self.assertEqual(self.gateway.sessions["NATIVE"], ["hi"])
        del self.gateway.sessions["NATIVE"]
        self.assertIn("RUN_ERROR", self.run_turn(thread="bound-thread", run="r2")[1])
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

    def test_profile_hash_file_parsing(self):
        import hashlib
        a, b = (hashlib.sha256(x.encode()).hexdigest() for x in ("a" * 43, "b" * 43))
        parsed = hermes_bridge.parse_profile_hashes(f"# comment\npilot {a}\n\nother {b.upper()}  # trailing\n")
        self.assertEqual(parsed, {"pilot": a, "other": b})
        for bad in (f"pilot {a}\npilot {b}\n",     # profile listed twice
                    f"pilot {a}\nother {a}\n",     # one token for two profiles
                    f"pilot {a[:-1]}\n",            # not a sha256
                    f"Pilot! {a}\n",                # not a profile name
                    f"pilot {a} extra\n"):
            with self.assertRaises(ValueError):
                hermes_bridge.parse_profile_hashes(bad)

    def test_per_profile_scope_function(self):
        import hashlib
        config = {"profiles": ("pilot", "other"), "token": "", "token_sha256": "",
                  "profile_hashes": {"pilot": hashlib.sha256(b"A" * 43).hexdigest(),
                                     "other": hashlib.sha256(b"B" * 43).hexdigest(),
                                     "hidden": hashlib.sha256(b"C" * 43).hexdigest()}}
        self.assertEqual(hermes_bridge.bearer_scope("Bearer " + "A" * 43, config), ("pilot",))
        self.assertEqual(hermes_bridge.bearer_scope("Bearer " + "B" * 43, config), ("other",))
        self.assertEqual(hermes_bridge.bearer_scope("Bearer " + "C" * 43, config), ())  # profile not exposed
        self.assertEqual(hermes_bridge.bearer_scope("Bearer " + config["profile_hashes"]["pilot"], config), ())
        self.assertEqual(hermes_bridge.bearer_scope("A" * 43, config), ())
        legacy = {"profiles": ("pilot", "other"), "token": TOKEN, "token_sha256": "", "profile_hashes": {}}
        self.assertEqual(hermes_bridge.bearer_scope("Bearer " + TOKEN, legacy), ("pilot", "other"))

    def test_per_profile_tokens_over_http(self):
        import hashlib
        tok_pilot, tok_other = "p" * 43, "o" * 43
        self.server.RequestHandlerClass.config = {
            **self.server.RequestHandlerClass.config, "token": "", "token_sha256": "",
            "profiles": ("pilot", "other"),
            "profile_hashes": {"pilot": hashlib.sha256(tok_pilot.encode()).hexdigest(),
                               "other": hashlib.sha256(tok_other.encode()).hexdigest()}}
        # Discovery shows only the token's own profile.
        for token, name in ((tok_pilot, "pilot"), (tok_other, "other")):
            status, data = self.request("GET", "/agents", token=token)
            self.assertEqual(status, 200)
            self.assertEqual([p["name"] for p in json.loads(data)["profiles"]], [name])
        self.assertEqual(self.request("GET", "/agents", token=TOKEN)[0], 401)  # old bridge-wide token is gone
        # A token cannot drive another profile; Hermes is never contacted for it.
        body = {"threadId": "t-x", "runId": "r-x", "messages": [{"id": "m", "role": "user", "content": "hi"}]}
        self.gateway.calls.clear()
        self.assertEqual(self.request("POST", "/profiles/other/ag-ui", body, token=tok_pilot)[0], 404)
        self.assertEqual(self.gateway.calls, [])
        status, data = self.request("POST", "/profiles/pilot/ag-ui", body, token=tok_pilot)
        self.assertEqual(status, 200)
        self.assertIn("RUN_FINISHED", [e["type"] for e in self.events(data)])
        # Request status is visible only within the owning profile's scope.
        key = hermes_bridge.RequestLedger.key(PROFILE_ID, "hermes", "t-x", "r-x")
        self.assertEqual(self.request("GET", f"/requests/{key}", token=tok_pilot)[0], 200)
        self.assertEqual(self.request("GET", f"/requests/{key}", token=tok_other)[0], 404)

    def creates(self):
        return [params for method, params in self.gateway.calls if method == "session.create"]

    def test_new_sessions_follow_profile_config_when_advertised(self):
        self.assertEqual(self.run_turn(thread="fp-1")[0], 200)
        self.assertEqual(len(self.creates()), 1)
        self.assertIs(self.creates()[0].get("follow_profile_config"), True)
        self.assertEqual(self.creates()[0]["source"], "gui")
        # A resumed thread is not re-created and keeps its original session settings.
        self.assertEqual(self.run_turn(thread="fp-1", run="run-2")[0], 200)
        self.assertEqual(len(self.creates()), 1)

    def test_follow_profile_config_omitted_when_not_advertised(self):
        for params in ([], None):  # gateway lists other parameters only, or has no runtime.describe
            self.gateway.describe_params = params
            self.gateway.calls.clear()
            thread = f"fp-none-{params is None}"
            self.assertEqual(self.run_turn(thread=thread)[0], 200)
            self.assertNotIn("follow_profile_config", self.creates()[0])

    def test_follow_profile_config_can_be_disabled(self):
        self.server.RequestHandlerClass.config = {**self.server.RequestHandlerClass.config,
                                                  "follow_profile": False}
        self.assertEqual(self.run_turn(thread="fp-off")[0], 200)
        self.assertNotIn("follow_profile_config", self.creates()[0])
        self.assertIn("client", self.creates()[0])  # the launch label is independent of following

    def test_threads_with_a_shared_prefix_get_their_own_sessions(self):
        # Same first 12 characters: the titles must still differ, or Hermes would merge the threads.
        self.assertEqual(self.run_turn(thread="openbot-roll-a")[0], 200)
        self.assertEqual(self.run_turn(thread="openbot-roll-b")[0], 200)
        titles = [c["title"] for c in self.creates()]
        self.assertEqual(len(set(titles)), 2)
        self.assertTrue(all(t.startswith("OpenBot openbot-roll ") for t in titles))
        self.assertEqual(self.gateway.created, 2)
        self.assertNotEqual(self.ledger.thread_session(PROFILE_ID, "openbot-roll-a")[0],
                            self.ledger.thread_session(PROFILE_ID, "openbot-roll-b")[0])

    def test_session_owned_by_another_thread_is_refused(self):
        self.assertEqual(self.run_turn(thread="owner")[0], 200)
        taken = self.ledger.thread_session(PROFILE_ID, "owner")[0]
        self.gateway.force_session = taken
        self.gateway.titles.clear()
        prompts_before = sum(1 for m, _ in self.gateway.calls if m == "prompt.submit")
        status, data = self.run_turn(thread="intruder", run="r-i")
        self.assertEqual(status, 200)
        self.assertIn("RUN_ERROR", [e["type"] for e in self.events(data)])
        self.assertIsNone(self.ledger.thread_session(PROFILE_ID, "intruder"))
        self.assertEqual(sum(1 for m, _ in self.gateway.calls if m == "prompt.submit"), prompts_before)

    def seed_existing(self, thread, info):
        """An existing session that owns the title this thread would get (title-as-identity)."""
        self.gateway.sessions["OLD"] = ["earlier conversation"]
        self.gateway.titles[hermes_bridge.session_title(PROFILE_ID, thread)] = "OLD"
        self.gateway.info["OLD"] = info

    def test_client_label_is_sent_and_echo_is_verified(self):
        self.assertEqual(self.run_turn(thread="label-1")[0], 200)
        client = self.creates()[0]["client"]
        self.assertEqual(client["name"], "agent-fabric")
        self.assertEqual(client["version"], hermes_bridge.stable_id("launch", PROFILE_ID, "label-1", length=24))

    def test_title_resolved_tui_session_is_refused(self):
        self.seed_existing("t-tui", {"source": "tui", "launch_request": {"source": "tui"}})
        status, data = self.run_turn(thread="t-tui", run="r1")
        self.assertIn("RUN_ERROR", [e["type"] for e in self.events(data)])
        self.assertIsNone(self.ledger.thread_session(PROFILE_ID, "t-tui"))
        self.assertEqual(self.gateway.sessions["OLD"], ["earlier conversation"])  # nothing submitted

    def test_title_resolved_gui_session_from_another_launch_is_refused(self):
        self.seed_existing("t-gui", {"source": "gui", "launch_request": {
            "source": "gui", "client": {"name": "agent-fabric", "version": "somebody-else"}}})
        status, data = self.run_turn(thread="t-gui", run="r1")
        self.assertIn("RUN_ERROR", [e["type"] for e in self.events(data)])
        self.assertIsNone(self.ledger.thread_session(PROFILE_ID, "t-gui"))
        self.assertEqual(self.gateway.sessions["OLD"], ["earlier conversation"])

    def test_without_client_support_only_the_source_is_checked(self):
        self.gateway.describe_params = ["follow_profile_config"]
        self.assertEqual(self.run_turn(thread="old-gw")[0], 200)
        self.assertNotIn("client", self.creates()[0])
        self.seed_existing("old-gw-tui", {"source": "tui"})
        status, data = self.run_turn(thread="old-gw-tui", run="r1")
        self.assertIn("RUN_ERROR", [e["type"] for e in self.events(data)])

    def test_foreign_session_helper(self):
        sent = {"client": {"name": "agent-fabric", "version": "v"}}
        ok = {"info": {"source": "gui", "launch_request": {"client": {"name": "agent-fabric", "version": "v"}}}}
        self.assertIsNone(hermes_bridge.foreign_session(ok, sent))
        self.assertIsNone(hermes_bridge.foreign_session({}, {}))  # nothing to compare on an old gateway
        self.assertIsNotNone(hermes_bridge.foreign_session({"info": {"source": "cli"}}, {}))
        self.assertIsNotNone(hermes_bridge.foreign_session({"info": {"source": "gui"}}, sent))

    def test_bind_host_policy(self):
        hermes_bridge.check_bind_host("127.0.0.1")
        hermes_bridge.check_bind_host("100.100.1.2")
        for bad in ("0.0.0.0", "192.168.1.5", "8.8.8.8"):
            with self.assertRaises(ValueError):
                hermes_bridge.check_bind_host(bad)


if __name__ == "__main__":
    unittest.main()


class IsolatedProfileTests(unittest.TestCase):
    """HERMES_ISOLATED_PROFILE=1: a shared profile with no messaging, memory, tools or native attach."""

    SAFE = {
        "platforms": {}, "gateway": {"standalone": True, "platforms": {}},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "platform_toolsets": {"cli": [], "tui": [], "gui": []},
        "agent": {"disabled_toolsets": ["project", "desktop_ui", "catalog"]},
        "mcp_servers": {},
    }

    def problems(self, env=(), **override):
        config = json.loads(json.dumps(self.SAFE))
        config.update(override)
        return hermes_bridge.isolated_profile_problems(config, set(env), hermes_bridge.CLIENT_SURFACE_TOOLSETS)

    def test_safe_profile_passes(self):
        self.assertEqual(self.problems(), [])

    def test_enabled_messaging_platforms_are_refused(self):
        self.assertEqual(self.problems(platforms={"discord": {"enabled": True}}), ["platforms.discord is enabled"])
        self.assertEqual(self.problems(gateway={"platforms": {"telegram": {"enabled": True}}}),
                         ["gateway.platforms.telegram is enabled"])

    def test_platform_credential_on_disk_is_refused_even_when_disabled(self):
        self.assertEqual(self.problems(platforms={"discord": {"enabled": False, "token": "x"}}),
                         ["platforms.discord holds a credential"])
        self.assertEqual(self.problems(platforms={"webhook": {"enabled": False, "extra": {"port": 1}}}), [])

    def test_messaging_credentials_in_the_environment_are_refused_by_name(self):
        self.assertEqual(self.problems(env={"DISCORD_BOT_TOKEN", "PATH", "HOME"}),
                         ["messaging credential DISCORD_BOT_TOKEN is set"])

    def test_memory_must_be_explicitly_off(self):
        self.assertEqual(self.problems(memory={}),
                         ["memory.memory_enabled must be false", "memory.user_profile_enabled must be false"])
        self.assertEqual(self.problems(memory={"memory_enabled": False, "user_profile_enabled": True}),
                         ["memory.user_profile_enabled must be false"])

    def test_session_platform_toolsets_must_be_explicitly_empty(self):
        self.assertEqual(self.problems(platform_toolsets={"cli": ["all"], "tui": []}),
                         ["platform_toolsets.cli must be set to []", "platform_toolsets.gui must be set to []"])
        self.assertEqual(self.problems(platform_toolsets={"cli": [], "tui": [], "gui": [], "cron": ["all"]}),
                         ["platform_toolsets.cron must be empty"])

    def test_client_surface_toolsets_must_be_disabled(self):
        self.assertEqual(self.problems(agent={"disabled_toolsets": ["project"]}),
                         ["agent.disabled_toolsets must include catalog, desktop_ui"])

    def test_mcp_servers_are_refused(self):
        self.assertEqual(self.problems(mcp_servers={"files": {"command": "x"}}), ["mcp_servers must be empty"])

    def test_dotenv_names_ignores_comments_and_blank_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("A=1\n# B=2\nC=\nexport D='x'\nE=\"\"\n\nnoequals\n", encoding="utf-8")
            self.assertEqual(hermes_bridge.dotenv_names(env), {"A", "D"})
            self.assertEqual(hermes_bridge.dotenv_names(Path(tmp) / "missing"), set())

    def test_connector_checks_only_its_own_home(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict("os.environ", {"HERMES_HOME": "/elsewhere"}):
            connector = hermes_bridge.HermesConnector(tmp, "", dedicated_profile="shared")
            self.assertEqual(connector.isolated_profile_problems(), ["HERMES_HOME must be the isolated profile's own home"])
            self.assertEqual(connector.profile_home("shared"), Path(tmp))

    def start(self, **env):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            base = {"PATH": "/usr/bin:/bin", "OPENBOT_BRIDGE_TOKEN": TOKEN, "HERMES_PROFILES": "shared",
                    "HERMES_ISOLATED_PROFILE": "1", "HERMES_HOME": tmp,
                    "OPENBOT_BRIDGE_LEDGER_PATH": str(Path(tmp) / "ledger.sqlite3")}
            base.update(env)
            code = f"import sys; sys.path.insert(0, {str(PACKAGE_ROOT)!r}); import hermes_bridge; hermes_bridge.main()"
            done = subprocess.run([sys.executable, "-I", "-c", code],
                                  cwd=PACKAGE_ROOT, env={k: v for k, v in base.items() if v is not None},
                                  capture_output=True, text=True, timeout=30)
            return done.returncode, done.stderr

    def test_isolated_profile_never_attaches_pins_or_carries_media(self):
        for extra in ({"HERMES_ALLOW_NATIVE_ATTACH": "1"}, {"HERMES_BOUND_THREAD_ID": "t", "HERMES_BOUND_SESSION_ID": "s"},
                      {"HERMES_BRIDGE_MEDIA_ROOT": "/tmp"}):
            code, err = self.start(**extra)
            self.assertNotEqual(code, 0)
            self.assertIn("never attaches to native sessions", err)

    def test_isolated_profile_serves_exactly_one_explicit_home(self):
        for extra in ({"HERMES_PROFILES": "shared,other"}, {"HERMES_HOME": None}):
            code, err = self.start(**extra)
            self.assertNotEqual(code, 0)
            self.assertIn("exactly one profile", err)


class _Harness(unittest.TestCase):
    """The HermesBridgeTests server and helpers without re-running its tests."""
    setUp = HermesBridgeTests.setUp
    request = HermesBridgeTests.request
    run_turn = HermesBridgeTests.run_turn
    events = staticmethod(HermesBridgeTests.events)


class IsolatedHandlerTests(_Harness):
    """An isolated bridge: gateway-enforced empty toolsets, no live profile re-read, and a per-run re-check."""

    def setUp(self):
        super().setUp()
        self.gateway.describe_params = ["model", "follow_profile_config", "request_id", "client", "toolsets"]
        self.server.RequestHandlerClass.config = {**self.server.RequestHandlerClass.config, "isolated": True,
                                                  "follow_profile": True}

    def test_sessions_are_created_with_no_toolsets_and_no_profile_follow(self):
        self.assertIn("RUN_FINISHED", self.run_turn()[1])
        creates = [p for m, p in self.gateway.calls if m == "session.create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["toolsets"], [])
        self.assertNotIn("follow_profile_config", creates[0])
        self.assertIn("client", creates[0])

    def test_gateway_that_cannot_enforce_isolation_is_refused(self):
        for params in (["model", "client"], ["model", "toolsets"], None):
            self.gateway.describe_params = params
            data = self.run_turn(thread=f"t-{params}", run=f"r-{params}")[1]
            self.assertIn("RUN_ERROR", data)
        self.assertEqual([m for m, p in self.gateway.calls if m == "session.create"], [])

    def test_failed_isolation_recheck_refuses_before_hermes(self):
        self.gateway.isolated_problems = ["memory.memory_enabled must be false"]
        status, data = self.run_turn()
        self.assertEqual(status, 503)
        self.assertIn("isolation_check_failed", data)
        self.assertEqual(self.gateway.calls, [])

    def test_bound_native_session_is_never_resumed(self):
        self.ledger.map_thread(PROFILE_ID, "bound-thread", "NATIVE", origin="bound")
        data = self.run_turn(thread="bound-thread")[1]
        self.assertIn("RUN_ERROR", data)
        self.assertNotIn("NATIVE", [p.get("session_id") for m, p in self.gateway.calls if m == "prompt.submit"])


class DedicatedListingTests(_Harness):
    """A dedicated (pinned or isolated) bridge lists its one home under its route name."""

    def test_standalone_home_listed_under_its_route_name(self):
        self.gateway.dedicated_profile = "solo"
        self.server.RequestHandlerClass.config = {**self.server.RequestHandlerClass.config, "profiles": ("solo",)}
        listing = json.loads(self.request("GET", "/agents")[1])
        self.assertEqual([(p["name"], p["display_name"], p["ag_ui_path"]) for p in listing["profiles"]],
                         [("solo", "solo", "/profiles/solo/ag-ui")])

    def test_roster_that_names_the_route_is_unchanged(self):
        self.gateway.dedicated_profile = "pilot"
        listing = json.loads(self.request("GET", "/agents")[1])
        self.assertEqual([(p["name"], p["display_name"]) for p in listing["profiles"]], [("pilot", "Pilot Bot")])


class IsolatedGuardExtraTests(unittest.TestCase):
    SAFE = IsolatedProfileTests.SAFE
    problems = IsolatedProfileTests.problems

    def test_plugins_memory_provider_and_focus_posture_are_refused(self):
        self.assertEqual(self.problems(plugins={"enabled": ["files"]}), ["plugins.enabled must be empty"])
        self.assertEqual(self.problems(plugins={"enabled": []}), [])
        self.assertEqual(self.problems(memory={"memory_enabled": False, "user_profile_enabled": False, "provider": "honcho"}),
                         ["memory.provider must be empty"])
        self.assertEqual(self.problems(agent={"disabled_toolsets": ["project", "desktop_ui", "catalog"], "coding_context": "focus"}),
                         ["agent.coding_context must not be focus"])

    def test_more_messaging_credentials_are_refused_by_name(self):
        self.assertEqual(self.problems(env={"API_SERVER_KEY", "MSGRAPH_CLIENT_SECRET", "YUANBAO_TOKEN", "RELAY_TOKEN"}),
                         ["messaging credential API_SERVER_KEY is set", "messaging credential MSGRAPH_CLIENT_SECRET is set",
                          "messaging credential RELAY_TOKEN is set", "messaging credential YUANBAO_TOKEN is set"])


class IsolatedConnectorTests(unittest.TestCase):
    """The connector reads Hermes' effective config itself and never trusts a fallback."""

    def fake_hermes(self, config):
        import types
        cfg_mod = types.ModuleType("hermes_cli.config"); cfg_mod.load_config = lambda: config
        pkg = types.ModuleType("hermes_cli"); pkg.config = cfg_mod
        ts = types.ModuleType("toolsets"); ts.CLIENT_SURFACE_TOOLSETS = frozenset({"project", "desktop_ui", "catalog"})
        return unittest.mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.config": cfg_mod, "toolsets": ts})

    def connector(self, tmp):
        home = Path(tmp) / "isolated"; home.mkdir()
        return hermes_bridge.HermesConnector(str(home), "", dedicated_profile="shared"), home

    def test_effective_safe_config_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            connector, home = self.connector(tmp)
            with self.fake_hermes(dict(IsolatedProfileTests.SAFE)), \
                    unittest.mock.patch.dict("os.environ", {"HERMES_HOME": str(home), "HOME": tmp}, clear=True):
                self.assertEqual(connector.isolated_profile_problems(), [])

    def test_fallback_config_is_never_trusted(self):
        class FailedConfigRead(dict):
            def __init__(self, data, *, error):
                super().__init__(data); self.error = error
        with tempfile.TemporaryDirectory() as tmp:
            connector, home = self.connector(tmp)
            with self.fake_hermes(FailedConfigRead(IsolatedProfileTests.SAFE, error=ValueError("bad yaml"))), \
                    unittest.mock.patch.dict("os.environ", {"HERMES_HOME": str(home), "HOME": tmp}, clear=True):
                self.assertEqual(connector.isolated_profile_problems(),
                                 ["config.yaml could not be read, and a fallback config is never trusted"])

    def test_any_home_inside_the_default_hermes_home_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes" / "profiles" / "shared"; home.mkdir(parents=True)
            connector = hermes_bridge.HermesConnector(str(home), "", dedicated_profile="shared")
            with unittest.mock.patch.dict("os.environ", {"HERMES_HOME": str(home), "HOME": tmp}, clear=True):
                self.assertEqual(connector.isolated_profile_problems(),
                                 ["an isolated profile cannot live in the default Hermes home"])
