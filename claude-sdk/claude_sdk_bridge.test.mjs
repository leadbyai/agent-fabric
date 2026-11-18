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

// ------------------------------------------------------------------ agent profile (0.2.0)
import { mkdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync, existsSync } from "node:fs";
import { configFromEnv, conversationId, conversationWorkspace, sdkOptions, sessionTitle } from "./claude_sdk_bridge.mjs";

const BASE = { SDK_BRIDGE_TOKEN_SHA256: HASH, SDK_BRIDGE_LEDGER: "/srv/l.sqlite3" };
const sha = (s) => createHash("sha256").update(s).digest("hex");
const TEMPLATE = { "CLAUDE.md": "template", ".claude/settings.json": '{"permissions":{"deny":["Bash(*git*push*)"]}}' };
const PINS = Object.fromEntries(Object.entries(TEMPLATE).map(([f, c]) => [f, sha(c)]));
const PIN_ENV = Object.entries(PINS).map(([f, h]) => `${f}=${h}`).join(",");
const AGENT = { ...BASE, SDK_BRIDGE_PROFILE: "agent", SDK_BRIDGE_WORKSPACE_ROOT: "/srv/ws", SDK_BRIDGE_TITLE: "Vex (OpenBot {id})",
  SDK_BRIDGE_PERMISSION_MODE: "bypassPermissions", SDK_BRIDGE_TEMPLATE_SHA256: PIN_ENV };

function agentTemplate() {
  const root = mkdtempSync(path.join(os.tmpdir(), "sdkroot-"));
  mkdirSync(path.join(root, "_template", ".claude"), { recursive: true });
  for (const [f, c] of Object.entries(TEMPLATE)) writeFileSync(path.join(root, "_template", f), c);
  return root;
}

test("host is limited to loopback or the docker0 gateway; caps and port are bounded", () => {
  assert.equal(configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w" }).host, "127.0.0.1");
  assert.equal(configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w", SDK_BRIDGE_HOST: "172.17.0.1" }).host, "172.17.0.1");
  for (const h of ["0.0.0.0", "100.113.154.85", "::", "localhost"])
    assert.throws(() => configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w", SDK_BRIDGE_HOST: h }), /SDK_BRIDGE_HOST/);
  for (const [k, v] of [["SDK_BRIDGE_MAX_TURNS", "0"], ["SDK_BRIDGE_MAX_TURNS", "2.5"], ["SDK_BRIDGE_MAX_BUDGET_USD", "1000"],
                        ["SDK_BRIDGE_TIMEOUT_SECONDS", "abc"], ["SDK_BRIDGE_PORT", "80"]])
    assert.throws(() => configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w", [k]: v }), new RegExp(k));
  const c = configFromEnv({ ...AGENT, SDK_BRIDGE_MAX_TURNS: "60", SDK_BRIDGE_MAX_BUDGET_USD: "15", SDK_BRIDGE_TIMEOUT_SECONDS: "1800" });
  assert.deepEqual([c.maxTurns, c.maxBudgetUsd, c.timeoutSeconds], [60, 15, 1800]);
});

test("pilot refuses agent settings; agent profile needs a root, a title with {id} and project settings", () => {
  assert.throws(() => configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w", SDK_BRIDGE_PERMISSION_MODE: "bypassPermissions" }), /PROFILE=agent/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_TITLE: "Vex" }), /\{id\}/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_WORKSPACE_ROOT: "rel" }), /WORKSPACE_ROOT/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_WORKSPACE: "/w" }), /WORKSPACE_ROOT/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_SETTING_SOURCES: "user" }), /include project/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_SETTING_SOURCES: "user,project,policy" }), /SETTING_SOURCES/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_PERMISSION_MODE: "yolo" }), /PERMISSION_MODE/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_SETTINGS: "rel.json" }), /SDK_BRIDGE_SETTINGS/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_STRICT_MCP: "yes" }), /STRICT_MCP/);
  const c = configFromEnv({ ...AGENT, SDK_BRIDGE_DENY_SESSIONS: " a , b " });
  assert.deepEqual(c.agent.settingSources, ["user", "project"]);
  assert.deepEqual(c.denySessions, ["a", "b"]);
  assert.equal(c.agent.tools, "preset");
});

test("agent options: bypass needs the explicit flag, no toy subagent, never continue, no settings unless given", () => {
  const c = configFromEnv(AGENT);
  const o = sdkOptions(c, "/srv/ws/abc", { PATH: "/bin", CLAUDE_CONFIG_DIR: "/home/a/.claude-a" });
  assert.equal(o.permissionMode, "bypassPermissions");
  assert.equal(o.allowDangerouslySkipPermissions, true);
  assert.equal(o.continue, false);
  assert.equal(o.agents, undefined);
  assert.equal(o.canUseTool, undefined);
  assert.equal(o.settings, undefined);
  assert.equal(o.cwd, "/srv/ws/abc");
  assert.deepEqual(o.settingSources, ["user", "project"]);
  assert.deepEqual(o.tools, { type: "preset", preset: "claude_code" });
  assert.deepEqual(o.systemPrompt, { type: "preset", preset: "claude_code" });
  assert.equal(o.env.CLAUDE_CONFIG_DIR, "/home/a/.claude-a");
  assert.equal(o.env.ENABLE_CLAUDEAI_MCP_SERVERS, "false");
  const d = sdkOptions(configFromEnv({ ...AGENT, SDK_BRIDGE_PERMISSION_MODE: "default", SDK_BRIDGE_SETTINGS: "/s.json",
    SDK_BRIDGE_TOOLS: "Read,Grep", SDK_BRIDGE_STRICT_MCP: "1" }), "/x", {});
  assert.equal(d.allowDangerouslySkipPermissions, undefined);
  assert.equal(d.settings, "/s.json");
  assert.deepEqual(d.tools, ["Read", "Grep"]);
  assert.deepEqual([d.strictMcpConfig, d.mcpServers], [true, {}]);
  const p = sdkOptions(configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w" }), "/w", {});
  assert.deepEqual([p.continue, p.settingSources, p.permissionMode], [false, [], "default"]);
});

test("titles always carry the conversation id, never the bare agent name", () => {
  const c = configFromEnv(AGENT);
  const t = sessionTitle(c, "thread-xyz");
  assert.match(t, /^Vex \(OpenBot [0-9a-f]{8}\)$/);
  assert.notEqual(t, "Vex");
  assert.notEqual(sessionTitle(c, "other"), t);
});

test("each conversation gets its own workspace seeded once from the template", () => {
  const root = agentTemplate();
  const a = conversationWorkspace(root, "thread-a", PINS), b = conversationWorkspace(root, "thread-b", PINS);
  assert.notEqual(a, b);
  assert.equal(path.dirname(a), root);
  assert.equal(path.basename(a), conversationId("thread-a"));
  assert.equal(readFileSync(path.join(a, ".claude", "settings.json"), "utf8"), TEMPLATE[".claude/settings.json"]);
  mkdirSync(path.join(a, "worktree", "src"), { recursive: true });            // extra files and worktrees are fine
  writeFileSync(path.join(a, "worktree", "src", "x.txt"), "work");
  assert.equal(conversationWorkspace(root, "thread-a", PINS), a);
  assert.equal(statSync(a).mode & 0o777, 0o700);
  assert.equal(statSync(path.join(a, ".claude")).mode & 0o777, 0o700);
  assert.equal(statSync(path.join(a, "CLAUDE.md")).mode & 0o777, 0o600);
  writeFileSync(path.join(a, "CLAUDE.md"), "edited in conversation");        // a pinned file edited by the session
  assert.throws(() => conversationWorkspace(root, "thread-a", PINS), /CLAUDE\.md changed/);
  assert.equal(readFileSync(path.join(a, "CLAUDE.md"), "utf8"), "edited in conversation");   // never silently re-seeded
  writeFileSync(path.join(a, "CLAUDE.md"), TEMPLATE["CLAUDE.md"]);
  rmSync(path.join(a, ".claude", "settings.json"));
  assert.throws(() => conversationWorkspace(root, "thread-a", PINS), /settings\.json is missing/);
  assert.equal(conversationWorkspace(root, "../../etc", PINS).startsWith(root + path.sep), true);  // raw ids never reach paths
});

test("a missing template or a template with a symlink is refused", () => {
  const empty = mkdtempSync(path.join(os.tmpdir(), "sdkroot-"));
  assert.throws(() => conversationWorkspace(empty, "t", PINS), /template is missing/);
  const root = agentTemplate();
  symlinkSync("/etc/hostname", path.join(root, "_template", "link"));
  assert.throws(() => conversationWorkspace(root, "t", PINS), /symbolic links/);
  assert.equal(existsSync(path.join(root, conversationId("t"))), false);
});

test("the template is seeded only when its files exactly match the pinned hashes", () => {
  const changed = agentTemplate();
  writeFileSync(path.join(changed, "_template", ".claude", "settings.json"), '{"permissions":{"deny":[]}}');
  assert.throws(() => conversationWorkspace(changed, "t", PINS), /settings\.json does not match/);
  const extra = agentTemplate();
  writeFileSync(path.join(extra, "_template", ".claude", "settings.local.json"), "{}");
  assert.throws(() => conversationWorkspace(extra, "t", PINS), /differ from the pinned set/);
  const fine = agentTemplate();
  assert.throws(() => conversationWorkspace(fine, "t", {}), /not pinned/);
  for (const r of [changed, extra, fine]) assert.equal(existsSync(path.join(r, conversationId("t"))), false);
  for (const bad of ["CLAUDE.md=abc", "/etc/x=" + "a".repeat(64), "../x=" + "a".repeat(64), `${PIN_ENV},CLAUDE.md=${"b".repeat(64)}`, ""])
    assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_TEMPLATE_SHA256: bad }), /TEMPLATE_SHA256/);
  assert.deepEqual(configFromEnv(AGENT).agent.templatePins, PINS);
});

test("the CLI defaults to the SDK's bundled one; an explicit path must be absolute", () => {
  assert.equal(sdkOptions(configFromEnv(AGENT), "/x", {}).pathToClaudeCodeExecutable, undefined);
  assert.equal(sdkOptions(configFromEnv({ ...AGENT, SDK_BRIDGE_CLAUDE_PATH: "/opt/c/claude" }), "/x", {}).pathToClaudeCodeExecutable, "/opt/c/claude");
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_CLAUDE_PATH: "claude" }), /CLAUDE_PATH/);
  assert.throws(() => configFromEnv({ ...BASE, SDK_BRIDGE_WORKSPACE: "/w", SDK_BRIDGE_CLAUDE_PATH: "/c" }), /PROFILE=agent/);
});

test("agent runs use the conversation workspace and title; denied and foreign sessions are refused", async (t) => {
  const root = agentTemplate();
  const dir = mkdtempSync(path.join(os.tmpdir(), "sdkb-"));
  const seen = [];
  let force = null, n = 0;
  const query = ({ options }) => (async function* () {
    seen.push(options);
    const sid = options.resume ?? force ?? `A${++n}`;
    yield { type: "system", subtype: "init", session_id: sid };
    yield { type: "result", subtype: "success", session_id: sid };
  })();
  const config = { ...configFromEnv({ ...AGENT, SDK_BRIDGE_WORKSPACE_ROOT: root, SDK_BRIDGE_DENY_SESSIONS: "LIVE" }) };
  const ledger = new Ledger(path.join(dir, "ledger.sqlite3"));
  const handle = createBridge({ query, config, ledger });
  const server = createServer((req, res) => handle(req, res));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const post = async (thread, run) => (await (await fetch(`http://127.0.0.1:${server.address().port}/ag-ui`, { method: "POST",
    headers: { authorization: `Bearer ${TOKEN}` }, body: JSON.stringify({ threadId: thread, runId: run,
      messages: [{ role: "user", content: "hi" }] }) })).text()).split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)));
  assert.equal((await post("t1", "r1")).at(-1).type, "RUN_FINISHED");
  assert.equal(seen[0].cwd, path.join(root, conversationId("t1")));
  assert.match(seen[0].title, /^Vex \(OpenBot [0-9a-f]{8}\)$/);
  assert.equal(seen[0].continue, false);
  await post("t1", "r2");
  assert.equal(seen[1].resume, "A1");
  force = "LIVE";                                    // SDK hands back the live interactive session
  assert.equal((await post("t2", "r1")).at(-1).type, "RUN_ERROR");
  assert.equal(ledger.session("t2"), null);
  ledger.map("t3", "LIVE");                          // even a ledger row cannot make the bridge resume it
  const before = seen.length;
  assert.equal((await post("t3", "r1")).at(-1).type, "RUN_ERROR");
  assert.equal(seen.length, before);
});

// ------------------------------------------------------------------ review fixes (single flight, global cap, codes)
async function gatedBridge(t, extra = {}) {
  const root = agentTemplate();
  const dir = mkdtempSync(path.join(os.tmpdir(), "sdkb-"));
  const gates = [], seen = [];
  let n = 0, result = "success";
  const query = ({ options }) => (async function* () {
    seen.push(options);
    const sid = options.resume ?? `G${++n}`;
    yield { type: "system", subtype: "init", session_id: sid };
    await new Promise((r) => gates.push(r));                 // held until the test releases it
    yield { type: "result", subtype: result, session_id: sid };
  })();
  const config = configFromEnv({ ...AGENT, SDK_BRIDGE_WORKSPACE_ROOT: root, ...extra });
  const ledger = new Ledger(path.join(dir, "ledger.sqlite3"));
  const handle = createBridge({ query, config, ledger });
  const server = createServer((req, res) => handle(req, res));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const post = (thread, run) => fetch(`http://127.0.0.1:${server.address().port}/ag-ui`, { method: "POST",
    headers: { authorization: `Bearer ${TOKEN}` }, body: JSON.stringify({ threadId: thread, runId: run,
      messages: [{ role: "user", content: "hi" }] }) });
  const events = async (r) => (await r.text()).split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)));
  const waitFor = async (k) => { for (let i = 0; i < 200 && gates.length < k; i++) await new Promise((r) => setTimeout(r, 5)); };
  return { root, post, events, seen, gates, waitFor, ledger, setResult: (r) => { result = r; } };
}

test("single flight: a second run on a busy conversation is refused before any SDK call", async (t) => {
  const g = await gatedBridge(t, { SDK_BRIDGE_MAX_CONCURRENT: "4" });
  const first = g.post("t1", "r1");                          // new thread, first run in flight
  await g.waitFor(1);
  const retry = await g.post("t1", "r2");                    // e.g. OpenBot Retry mints a new runId
  assert.equal(retry.status, 409);
  assert.equal((await retry.json()).error, "conversation_busy");
  assert.equal(g.seen.length, 1);
  g.gates.shift()();
  assert.equal((await g.events(await first)).at(-1).type, "RUN_FINISHED");
  const resumed = g.post("t1", "r3");                        // existing thread: busy again while resuming
  await g.waitFor(1);
  assert.equal((await g.post("t1", "r4")).status, 409);
  assert.deepEqual(g.seen.map((o) => o.resume ?? null), [null, "G1"]);
  g.gates.shift()();
  await g.events(await resumed);
  assert.equal(g.ledger.get(createHash("sha256").update(JSON.stringify(["t1", "r2"])).digest("hex")), null);  // refused run left no ledger row
  const after = await g.events(await (async () => { const p = g.post("t1", "r2"); await g.waitFor(1); g.gates.shift()(); return p; })());
  assert.equal(after.at(-1).type, "RUN_FINISHED");           // and can run once the conversation is free
});

test("global cap: with the default of one, another conversation is refused while one runs", async (t) => {
  const g = await gatedBridge(t);
  const first = g.post("a", "r1");
  await g.waitFor(1);
  const other = await g.post("b", "r1");
  assert.equal(other.status, 429);
  assert.equal((await other.json()).error, "bridge_busy");
  assert.equal(g.seen.length, 1);
  g.gates.shift()();
  await g.events(await first);
  const next = g.post("b", "r1");
  await g.waitFor(1); g.gates.shift()();
  assert.equal((await g.events(await next)).at(-1).type, "RUN_FINISHED");
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_MAX_CONCURRENT: "5" }), /MAX_CONCURRENT/);
  assert.throws(() => configFromEnv({ ...AGENT, SDK_BRIDGE_MAX_CONCURRENT: "0" }), /MAX_CONCURRENT/);
  assert.equal(configFromEnv(AGENT).maxConcurrent, 1);
});

test("RUN_ERROR names the cap or guard that stopped the run; a changed pinned file stops the next turn", async (t) => {
  const g = await gatedBridge(t);
  g.setResult("error_max_turns");
  const r = g.post("t", "r1"); await g.waitFor(1); g.gates.shift()();
  const err = (await g.events(await r)).at(-1);
  assert.deepEqual([err.type, err.code], ["RUN_ERROR", "error_max_turns"]);
  assert.match(err.message, /error_max_turns/);
  g.setResult("success");
  writeFileSync(path.join(g.root, conversationId("t"), ".claude", "settings.json"), '{"permissions":{"deny":[]}}');
  const before = g.seen.length;
  const blocked = (await g.events(await g.post("t", "r2"))).at(-1);
  assert.deepEqual([blocked.type, blocked.code], ["RUN_ERROR", "workspace_pinned_file_changed"]);
  assert.equal(g.seen.length, before);                       // no prompt reached the SDK
});
