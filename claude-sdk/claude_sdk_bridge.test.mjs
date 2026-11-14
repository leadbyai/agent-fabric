// Offline tests for the Claude SDK bridge (node --no-warnings --test). No SDK process or API is used.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { Ledger, bearerOk, createBridge, userText, workspacePolicy } from "./claude_sdk_bridge.mjs";

const TOKEN = "t".repeat(43);
const HASH = createHash("sha256").update(TOKEN).digest("hex");

function fakeQuery(state) {
  return ({ prompt, options }) => (async function* () {
    state.calls.push({ prompt, resume: options.resume ?? null, title: options.title ?? null });
    const sid = options.resume ?? state.forceSession ?? `S${++state.created}`;
    yield { type: "system", subtype: "init", session_id: sid };
    for (const ch of ["Hel", "lo"]) yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: ch } } };
    yield { type: "stream_event", parent_tool_use_id: "T1", event: { type: "content_block_delta", delta: { type: "text_delta", text: "SUBAGENT" } } };
    yield { type: "assistant", message: { content: [{ type: "tool_use", id: "T1", name: "Agent", input: { subagent_type: "word-counter" } }] } };
    yield { type: "assistant", parent_tool_use_id: "T1", message: { content: [{ type: "tool_use", id: "T2", name: "Read", input: {} }] } };
    yield { type: "user", parent_tool_use_id: "T1", message: { content: [{ type: "tool_result", tool_use_id: "T2", is_error: false }] } };
    if (state.fail) yield { type: "result", subtype: "error_during_execution", session_id: sid };
    else yield { type: "result", subtype: "success", session_id: sid };
  })();
}

async function setup(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "sdkb-"));
  const state = { calls: [], created: 0 };
  const ledger = new Ledger(path.join(dir, "ledger.sqlite3"));
  const handle = createBridge({ query: fakeQuery(state), ledger,
    config: { tokenSha256: HASH, workspace: dir, model: "m", maxTurns: 4, maxBudgetUsd: 1, timeoutSeconds: 30 } });
  const server = createServer((req, res) => handle(req, res));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;
  const post = (body, token = TOKEN) => fetch(base + "/ag-ui", { method: "POST", body: JSON.stringify(body),
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" } });
  const events = async (r) => (await r.text()).split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)));
  const run = (thread, runId, text = "hi") => ({ threadId: thread, runId, messages: [{ id: "m", role: "user", content: text }] });
  return { base, post, events, run, state, ledger };
}

test("health needs no auth; everything else does", async (t) => {
  const s = await setup(t);
  assert.equal((await fetch(s.base + "/health")).status, 200);
  assert.equal((await s.post(s.run("a", "r1"), "wrong")).status, 401);
  assert.equal((await fetch(s.base + "/requests/x")).status, 401);
  assert.equal(s.state.calls.length, 0);
});

test("first run creates a session, the next resumes it; text, tool and subagent events stream", async (t) => {
  const s = await setup(t);
  const ev1 = await s.events(await s.post(s.run("thread-1", "r1")));
  const types = ev1.map((e) => e.type);
  assert.deepEqual([types[0], types.at(-1)], ["RUN_STARTED", "RUN_FINISHED"]);
  assert.equal(ev1.filter((e) => e.type === "TEXT_MESSAGE_CONTENT").map((e) => e.delta).join(""), "Hello");  // subagent text not mixed in
  const tools = ev1.filter((e) => e.name === "agent-fabric.tool").map((e) => e.value);
  assert.deepEqual(tools.map((v) => [v.phase, v.subagent]), [["start", false], ["start", true], ["result", true]]);
  assert.equal(ev1.filter((e) => e.name === "agent-fabric.subagent").length, 1);
  assert.equal(ev1.some((e) => e.type?.startsWith("TOOL_CALL")), false);
  await s.events(await s.post(s.run("thread-1", "r2", "again")));
  assert.deepEqual(s.state.calls.map((c) => c.resume), [null, "S1"]);
  assert.match(s.state.calls[0].title, /^OpenBot thread-1 [0-9a-f]{10}$/);
  assert.equal(s.ledger.session("thread-1"), "S1");
});

test("replay returns the stored reply without a new turn; changed text is 409", async (t) => {
  const s = await setup(t);
  await s.events(await s.post(s.run("t", "r1", "hello")));
  const replay = await s.events(await s.post(s.run("t", "r1", "hello")));
  assert.equal(replay.find((e) => e.type === "TEXT_MESSAGE_CONTENT").delta, "Hello");
  assert.equal(s.state.calls.length, 1);
  assert.equal((await s.post(s.run("t", "r1", "different"))).status, 409);
  assert.equal(s.state.calls.length, 1);
});

test("a failed run is uncertain and never resent", async (t) => {
  const s = await setup(t);
  s.state.fail = true;
  const ev = await s.events(await s.post(s.run("t", "r1")));
  assert.equal(ev.at(-1).type, "RUN_ERROR");
  s.state.fail = false;
  const again = await s.events(await s.post(s.run("t", "r1")));
  assert.match(again.at(-1).message, /already uncertain/);
  assert.equal(s.state.calls.length, 1);
});

test("a session already owned by another thread is refused", async (t) => {
  const s = await setup(t);
  await s.events(await s.post(s.run("owner", "r1")));
  s.state.forceSession = "S1";
  const ev = await s.events(await s.post(s.run("intruder", "r1")));
  assert.equal(ev.at(-1).type, "RUN_ERROR");
  assert.equal(s.ledger.session("intruder"), null);
});

test("policy allows only workspace files and the named subagent", async () => {
  const p = workspacePolicy("/srv/ws");
  assert.equal((await p("Read", { file_path: "/srv/ws/a.txt" })).behavior, "allow");
  assert.equal((await p("Write", { file_path: "note.txt" })).behavior, "allow");
  for (const [tool, input] of [["Read", { file_path: "/etc/hostname" }], ["Write", { file_path: "/srv/ws/../x" }],
                               ["Read", { file_path: "/srv/wsx/a" }], ["Bash", { command: "id" }],
                               ["Agent", { subagent_type: "general-purpose" }]])
    assert.equal((await p(tool, input)).behavior, "deny", `${tool} ${JSON.stringify(input)}`);
  assert.equal((await p("Agent", { subagent_type: "word-counter" })).behavior, "allow");
});

test("helpers", () => {
  assert.equal(bearerOk(`Bearer ${TOKEN}`, HASH), true);
  assert.equal(bearerOk(`Bearer ${HASH}`, HASH), false);
  assert.equal(bearerOk(TOKEN, HASH), false);
  assert.equal(userText({ messages: [{ role: "user", content: [{ type: "text", text: "a" }, { type: "image" }] }] }), "a");
  assert.throws(() => userText({ messages: [] }));
});
