// Agent Fabric: private AG-UI bridge to Claude Agent SDK sessions (pilot).
//
// Each OpenBot thread gets its own SDK-managed Claude Code session, created on the thread's first run
// and resumed on later runs (the thread -> session map is in the ledger). It never attaches to an
// interactive session that another process owns. Loopback bind only; hash-only bearer.
//
// Routes:  GET /health (no auth) | POST /ag-ui (bearer) | GET /requests/<id> (bearer)
//
// Env: SDK_BRIDGE_TOKEN_SHA256 (64 hex, required), SDK_BRIDGE_PORT (8768), SDK_BRIDGE_WORKSPACE (required,
// absolute), SDK_BRIDGE_LEDGER (required), SDK_BRIDGE_MODEL, SDK_BRIDGE_MAX_TURNS (8),
// SDK_BRIDGE_MAX_BUDGET_USD (2), SDK_BRIDGE_TIMEOUT_SECONDS (600). CLAUDE_CONFIG_DIR is passed through.
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export const VERSION = "0.1.0-sdk-pilot";
const MAX_BODY = 1024 * 1024;
const sha256 = (s) => createHash("sha256").update(s).digest("hex");
export const stableId = (...parts) => sha256(JSON.stringify(parts)).slice(0, 32);

// ------------------------------------------------------------------------------------------ ledger
export class Ledger {
  constructor(file) {
    this.db = new DatabaseSync(file);
    this.db.exec(`create table if not exists requests (key text primary key, thread text, fingerprint text,
                    state text, reply text, updated_at integer);
                  create table if not exists threads (thread text primary key, session_id text, created_at integer);`);
  }
  get(key) { return this.db.prepare("select * from requests where key = ?").get(key) ?? null; }
  reserve(key, thread, fingerprint) {
    const r = this.db.prepare(`insert or ignore into requests (key, thread, fingerprint, state, updated_at)
                               values (?, ?, ?, 'submitted', ?)`).run(key, thread, fingerprint, Date.now());
    return r.changes === 1;
  }
  finish(key, reply) { this.db.prepare("update requests set state='finished', reply=?, updated_at=? where key=?").run(reply, Date.now(), key); }
  uncertain(key) { this.db.prepare("update requests set state='uncertain', updated_at=? where key=?").run(Date.now(), key); }
  session(thread) { return this.db.prepare("select session_id from threads where thread = ?").get(thread)?.session_id ?? null; }
  owner(sessionId) { return this.db.prepare("select thread from threads where session_id = ?").get(sessionId)?.thread ?? null; }
  map(thread, sessionId) { this.db.prepare("insert into threads (thread, session_id, created_at) values (?, ?, ?)").run(thread, sessionId, Date.now()); }
}

// ------------------------------------------------------------------------------------------ policy
export function workspacePolicy(workspace, allowedSubagents = ["word-counter"]) {
  const root = path.resolve(workspace);
  const inside = (p) => typeof p === "string" && (path.resolve(root, p) + path.sep).startsWith(root + path.sep);
  return async (toolName, input) => {
    let ok = false;
    if (["Read", "Write", "Edit"].includes(toolName)) ok = inside(input.file_path);
    else if (toolName === "Glob") ok = input.path === undefined || inside(input.path);
    else if (toolName === "Agent" || toolName === "Task") ok = allowedSubagents.includes(input.subagent_type);
    return ok ? { behavior: "allow", updatedInput: input }
              : { behavior: "deny", message: `Not permitted by the bridge policy: ${toolName}.` };
  };
}

export function bearerOk(header, expectedHash) {
  if (typeof header !== "string" || !header.startsWith("Bearer ") || header.length > 4096) return false;
  const a = Buffer.from(sha256(header.slice(7)), "hex"), b = Buffer.from(expectedHash, "hex");
  return a.length === b.length && timingSafeEqual(a, b);
}

/** The latest user message text of an AG-UI RunAgentInput (string or text parts). */
export function userText(body) {
  const msgs = Array.isArray(body?.messages) ? body.messages : [];
  const last = [...msgs].reverse().find((m) => m?.role === "user");
  if (!last) throw new Error("No user message");
  const c = last.content;
  const text = typeof c === "string" ? c : Array.isArray(c) ? c.filter((p) => p?.type === "text").map((p) => p.text).join("\n") : "";
  if (!text.trim()) throw new Error("Empty user message");
  return text;
}

// ------------------------------------------------------------------------------------------ bridge
export function createBridge({ query, config, ledger }) {
  const policy = workspacePolicy(config.workspace);
  const baseOptions = () => ({
    model: config.model,
    cwd: config.workspace,
    env: { ...process.env, ENABLE_CLAUDEAI_MCP_SERVERS: "false" },
    settingSources: [],
    strictMcpConfig: true,
    mcpServers: {},
    tools: ["Read", "Write", "Glob", "Agent"],
    permissionMode: "default",
    canUseTool: policy,
    agents: { "word-counter": { description: "Counts the words in one workspace file.",
      prompt: "Read the single file you are given and reply with only the number of words in it.", tools: ["Read"] } },
    includePartialMessages: true,
    maxTurns: config.maxTurns,
    maxBudgetUsd: config.maxBudgetUsd,
  });

  async function runTurn(thread, text, emit, signal) {
    const known = ledger.session(thread);
    const options = { ...baseOptions(), abortController: signal,
      ...(known ? { resume: known } : { title: `OpenBot ${thread.slice(0, 12)} ${stableId("title", thread).slice(0, 10)}` }) };
    const messageId = randomUUID();
    let started = false, reply = "", sessionId = known;
    for await (const m of query({ prompt: text, options })) {
      if (m.type === "system" && m.subtype === "init") {
        sessionId = m.session_id;
        if (!known) {
          if (ledger.owner(sessionId)) throw new Error("SDK returned a session another thread owns");
          ledger.map(thread, sessionId);
        } else if (sessionId !== known) throw new Error("Resumed session id changed");
        emit({ type: "CUSTOM", name: "agent-fabric.progress", value: { state: "submitted", session: known ? "resumed" : "created" } });
      }
      if (m.type === "stream_event" && !m.parent_tool_use_id && m.event?.type === "content_block_delta" && m.event.delta?.type === "text_delta") {
        if (!started) { emit({ type: "TEXT_MESSAGE_START", messageId, role: "assistant" }); started = true; }
        emit({ type: "TEXT_MESSAGE_CONTENT", messageId, delta: m.event.delta.text });
        reply += m.event.delta.text;
      }
      if (m.type === "assistant") {
        for (const b of m.message?.content ?? []) {
          if (b.type !== "tool_use") continue;
          const sub = Boolean(m.parent_tool_use_id);
          // CUSTOM events, not AG-UI TOOL_CALL_*: OpenBot would treat those as tools for it to run.
          emit({ type: "CUSTOM", name: "agent-fabric.tool", value: { phase: "start", tool: b.name, id: b.id, subagent: sub,
            parent: m.parent_tool_use_id ?? null } });
          if (b.name === "Agent" || b.name === "Task") emit({ type: "CUSTOM", name: "agent-fabric.subagent",
            value: { phase: "start", id: b.id, subagent_type: b.input?.subagent_type ?? null } });
        }
      }
      if (m.type === "user" && Array.isArray(m.message?.content)) {
        for (const b of m.message.content) if (b.type === "tool_result") emit({ type: "CUSTOM", name: "agent-fabric.tool",
          value: { phase: "result", id: b.tool_use_id, error: Boolean(b.is_error), subagent: Boolean(m.parent_tool_use_id) } });
      }
      if (m.type === "result" && m.subtype !== "success") throw new Error(`SDK result ${m.subtype}`);
    }
    if (started) emit({ type: "TEXT_MESSAGE_END", messageId });
    return reply.trim();
  }

  return async function handle(req, res) {
    const url = new URL(req.url, "http://localhost");
    const json = (code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };
    if (req.method === "GET" && url.pathname === "/health") return json(200, { ok: true, version: VERSION, mode: "claude-sdk" });
    if (!bearerOk(req.headers.authorization, config.tokenSha256)) return json(401, { error: "unauthorized" });
    if (req.method === "GET" && url.pathname.startsWith("/requests/")) {
      const r = ledger.get(url.pathname.slice("/requests/".length));
      return r ? json(200, { id: r.key, state: r.state, reply: r.state === "finished" ? r.reply : null }) : json(404, { error: "not_found" });
    }
    if (req.method !== "POST" || url.pathname !== "/ag-ui") return json(404, { error: "not_found" });
    let raw = "";
    for await (const chunk of req) { raw += chunk; if (raw.length > MAX_BODY) return json(413, { error: "request_too_large" }); }
    let body, text;
    try { body = JSON.parse(raw); text = userText(body); } catch (e) { return json(400, { error: String(e.message) }); }
    const thread = String(body.threadId || randomUUID()), run = String(body.runId || randomUUID());
    const key = sha256(JSON.stringify([thread, run])), fingerprint = sha256(text);
    const previous = ledger.get(key);
    const stream = () => res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "close" });
    let detached = false;
    const emit = (ev) => { if (!detached) { try { res.write(`data: ${JSON.stringify(ev)}\n\n`); } catch { detached = true; } } };
    res.on("close", () => { detached = true; });
    if (previous) {
      if (previous.fingerprint !== fingerprint) return json(409, { error: "Run identifier reused with different content" });
      stream(); emit({ type: "RUN_STARTED", threadId: thread, runId: run });
      if (previous.state === "finished") {
        const id = randomUUID();
        emit({ type: "TEXT_MESSAGE_START", messageId: id, role: "assistant" });
        emit({ type: "TEXT_MESSAGE_CONTENT", messageId: id, delta: previous.reply ?? "" });
        emit({ type: "TEXT_MESSAGE_END", messageId: id });
        emit({ type: "RUN_FINISHED", threadId: thread, runId: run });
      } else emit({ type: "RUN_ERROR", message: `Request ${key} is already ${previous.state}. No duplicate prompt sent.` });
      return res.end();
    }
    if (!ledger.reserve(key, thread, fingerprint)) return json(409, { error: "Run already in progress" });
    stream(); emit({ type: "RUN_STARTED", threadId: thread, runId: run });
    emit({ type: "CUSTOM", name: "agent-fabric.request", value: { id: key, state: "submitted" } });
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), config.timeoutSeconds * 1000);
    try {
      const reply = await runTurn(thread, text, emit, abort);
      ledger.finish(key, reply);
      emit({ type: "RUN_FINISHED", threadId: thread, runId: run });
    } catch (e) {
      ledger.uncertain(key);  // never resent automatically
      emit({ type: "RUN_ERROR", message: `Run did not complete (${e?.name ?? "Error"}). No duplicate prompt will be sent; check /requests/${key}.` });
    } finally { clearTimeout(timer); res.end(); }
  };
}

export function configFromEnv(env = process.env) {
  const config = {
    tokenSha256: (env.SDK_BRIDGE_TOKEN_SHA256 || "").trim().toLowerCase(),
    host: "127.0.0.1",
    port: Number(env.SDK_BRIDGE_PORT || 8768),
    workspace: env.SDK_BRIDGE_WORKSPACE || "",
    ledger: env.SDK_BRIDGE_LEDGER || "",
    model: env.SDK_BRIDGE_MODEL || undefined,
    maxTurns: Number(env.SDK_BRIDGE_MAX_TURNS || 8),
    maxBudgetUsd: Number(env.SDK_BRIDGE_MAX_BUDGET_USD || 2),
    timeoutSeconds: Number(env.SDK_BRIDGE_TIMEOUT_SECONDS || 600),
  };
  if (!/^[0-9a-f]{64}$/.test(config.tokenSha256)) throw new Error("SDK_BRIDGE_TOKEN_SHA256 must be a 64-character hex SHA-256");
  if (!path.isAbsolute(config.workspace)) throw new Error("SDK_BRIDGE_WORKSPACE must be an absolute path");
  if (!path.isAbsolute(config.ledger)) throw new Error("SDK_BRIDGE_LEDGER must be an absolute path");
  return config;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = configFromEnv();
  const { query } = await import("@anthropic-ai/claude-agent-sdk");
  const handle = createBridge({ query, config, ledger: new Ledger(config.ledger) });
  createServer((req, res) => handle(req, res).catch(() => { try { res.end(); } catch {} }))
    .listen(config.port, config.host, () => console.log(`Agent Fabric Claude SDK bridge ${VERSION} on ${config.host}:${config.port}`));
}
