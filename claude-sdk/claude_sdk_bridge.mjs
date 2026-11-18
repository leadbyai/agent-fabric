// Agent Fabric: private AG-UI bridge to Claude Agent SDK sessions (pilot).
//
// Each OpenBot thread gets its own SDK-managed Claude Code session, created on the thread's first run
// and resumed on later runs (the thread -> session map is in the ledger). It never attaches to an
// interactive session that another process owns: never `continue`, never a session it did not create,
// never a session on the deny list. Hash-only bearer; binds 127.0.0.1 or the docker0 gateway only.
//
// Routes:  GET /health (no auth) | POST /ag-ui (bearer) | GET /requests/<id> (bearer)
//
// Env (both profiles): SDK_BRIDGE_TOKEN_SHA256 (64 hex, required), SDK_BRIDGE_HOST (127.0.0.1 | 172.17.0.1),
// SDK_BRIDGE_PORT (8768), SDK_BRIDGE_LEDGER (required), SDK_BRIDGE_MODEL, SDK_BRIDGE_MAX_TURNS (8),
// SDK_BRIDGE_MAX_BUDGET_USD (2), SDK_BRIDGE_TIMEOUT_SECONDS (600), SDK_BRIDGE_DENY_SESSIONS (comma list of
// session ids that must never be resumed or adopted, e.g. the agent's live interactive session).
// SDK_BRIDGE_PROFILE=pilot (default): isolated, no settings, workspace-only tools; SDK_BRIDGE_WORKSPACE.
// SDK_BRIDGE_PROFILE=agent: an existing agent's own setup, one workspace per conversation:
//   SDK_BRIDGE_WORKSPACE_ROOT (required; <root>/_template/ is copied to <root>/<conversation>/ on first run),
//   SDK_BRIDGE_TEMPLATE_SHA256 (required: "<relative path>=<sha256>,..." naming EXACTLY the template's files;
//   checked before every seed), SDK_BRIDGE_CLAUDE_PATH (optional absolute CLI path; default: the SDK's bundled CLI),
//   SDK_BRIDGE_TITLE (required, must contain {id}, e.g. "Vex (OpenBot {id})"), SDK_BRIDGE_SETTING_SOURCES
//   (user,project), SDK_BRIDGE_SETTINGS (optional extra settings file), SDK_BRIDGE_TOOLS ("preset" or a list),
//   SDK_BRIDGE_PERMISSION_MODE (default | acceptEdits | dontAsk | plan | bypassPermissions),
//   SDK_BRIDGE_STRICT_MCP (0), SDK_BRIDGE_CLAUDEAI_MCP (0).
// CLAUDE_CONFIG_DIR is passed through, so the SDK uses that account's own sign-in where it already is;
// the bridge never reads or copies credentials.
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import { cpSync, existsSync, lstatSync, readFileSync, readdirSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export const VERSION = "0.2.0-sdk-agent";
export const ALLOWED_HOSTS = ["127.0.0.1", "172.17.0.1"];
export const PERMISSION_MODES = ["default", "acceptEdits", "dontAsk", "plan", "bypassPermissions"];
const SETTING_SOURCES = ["user", "project", "local"];
const LIMITS = { maxTurns: [1, 500, true], maxBudgetUsd: [0.01, 100, false], timeoutSeconds: [10, 7200, true] };
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

// ------------------------------------------------------------------------------------------ workspace
/** Short, path-safe conversation id derived from the OpenBot thread (raw thread ids never reach paths). */
export const conversationId = (thread) => stableId("conversation", thread).slice(0, 16);

/** Relative paths of every file in a plain tree; symlinks and special files are refused. */
function plainFiles(dir, base = dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name), st = lstatSync(p);
    if (st.isSymbolicLink()) throw new Error("Workspace template must not contain symbolic links");
    if (st.isDirectory()) out.push(...plainFiles(p, base));
    else if (st.isFile()) out.push(path.relative(base, p).split(path.sep).join("/"));
    else throw new Error("Workspace template may hold only files and folders");
  }
  return out;
}

/** The template must hold exactly the pinned files, each with its pinned SHA-256. */
export function verifyTemplate(template, pins) {
  const files = plainFiles(template).sort(), wanted = Object.keys(pins).sort();
  if (JSON.stringify(files) !== JSON.stringify(wanted)) throw new Error("Workspace template files differ from the pinned set");
  for (const f of files)
    if (createHash("sha256").update(readFileSync(path.join(template, f))).digest("hex") !== pins[f])
      throw new Error(`Workspace template file ${f} does not match its pinned hash`);
}

/** The conversation's own workspace under <root>, seeded from <root>/_template/ the first time. */
export function conversationWorkspace(root, thread, pins) {
  const dir = path.join(root, conversationId(thread));
  if (!existsSync(dir)) {
    const template = path.join(root, "_template");
    if (!existsSync(template) || !lstatSync(template).isDirectory()) throw new Error("Workspace template is missing");
    if (!pins || !Object.keys(pins).length) throw new Error("Workspace template hashes are not pinned");
    verifyTemplate(template, pins);
    cpSync(template, dir, { recursive: true, errorOnExist: true, force: false });
    verifyTemplate(dir, pins);   // what was copied is exactly what was checked
  } else if (lstatSync(dir).isSymbolicLink() || !lstatSync(dir).isDirectory()) throw new Error("Conversation workspace is not a folder");
  return dir;
}

/** Session title for a new conversation. Agent titles always carry the id, so never equal the bare agent name. */
export function sessionTitle(config, thread) {
  if ((config.profile ?? "pilot") === "pilot") return `OpenBot ${thread.slice(0, 12)} ${stableId("title", thread).slice(0, 10)}`;
  return config.agent.title.replaceAll("{id}", conversationId(thread).slice(0, 8));
}

// ------------------------------------------------------------------------------------------ bridge
/** SDK options for one turn, before resume/title. Pure, so the profiles can be tested without the SDK. */
export function sdkOptions(config, cwd, env = process.env) {
  const common = { model: config.model, cwd, continue: false, includePartialMessages: true,
    maxTurns: config.maxTurns, maxBudgetUsd: config.maxBudgetUsd };
  if ((config.profile ?? "pilot") === "pilot") return { ...common,
    env: { ...env, ENABLE_CLAUDEAI_MCP_SERVERS: "false" },
    settingSources: [],
    strictMcpConfig: true,
    mcpServers: {},
    tools: ["Read", "Write", "Glob", "Agent"],
    permissionMode: "default",
    canUseTool: workspacePolicy(cwd),
    agents: { "word-counter": { description: "Counts the words in one workspace file.",
      prompt: "Read the single file you are given and reply with only the number of words in it.", tools: ["Read"] } } };
  const a = config.agent;
  return { ...common,
    env: { ...env, ENABLE_CLAUDEAI_MCP_SERVERS: a.claudeAiMcp ? "true" : "false" },
    settingSources: a.settingSources,           // "project" loads the workspace's CLAUDE.md and deny list
    ...(a.settings ? { settings: a.settings } : {}),
    ...(a.claudePath ? { pathToClaudeCodeExecutable: a.claudePath } : {}),
    systemPrompt: { type: "preset", preset: "claude_code" },
    tools: a.tools === "preset" ? { type: "preset", preset: "claude_code" } : a.tools,
    permissionMode: a.permissionMode,
    ...(a.permissionMode === "bypassPermissions" ? { allowDangerouslySkipPermissions: true } : {}),
    ...(a.strictMcp ? { strictMcpConfig: true, mcpServers: {} } : {}) };
}

export function createBridge({ query, config, ledger }) {
  const denied = new Set(config.denySessions ?? []);
  const workspaceFor = (thread) => (config.profile ?? "pilot") === "pilot" ? config.workspace
    : conversationWorkspace(config.agent.workspaceRoot, thread, config.agent.templatePins);
  let reportedVersion = null;

  async function runTurn(thread, text, emit, signal) {
    const known = ledger.session(thread);
    if (known && denied.has(known)) throw new Error("Refusing to resume a denied session");
    const options = { ...sdkOptions(config, workspaceFor(thread)), abortController: signal,
      ...(known ? { resume: known } : { title: sessionTitle(config, thread) }) };
    const messageId = randomUUID();
    let started = false, reply = "", sessionId = known;
    for await (const m of query({ prompt: text, options })) {
      if (m.type === "system" && m.subtype === "init") {
        sessionId = m.session_id;
        if (denied.has(sessionId)) throw new Error("SDK returned a denied session");
        if (m.claude_code_version && m.claude_code_version !== reportedVersion) {
          reportedVersion = m.claude_code_version;
          console.log(`Claude Code CLI version ${reportedVersion}`);
        }
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

const list = (v) => String(v ?? "").split(",").map((x) => x.trim()).filter(Boolean);
const envName = (k) => "SDK_BRIDGE_" + k.replace(/[A-Z]/g, (c) => "_" + c).toUpperCase();
const AGENT_KEYS = ["SDK_BRIDGE_WORKSPACE_ROOT", "SDK_BRIDGE_TEMPLATE_SHA256", "SDK_BRIDGE_CLAUDE_PATH", "SDK_BRIDGE_TITLE", "SDK_BRIDGE_SETTINGS", "SDK_BRIDGE_SETTING_SOURCES",
  "SDK_BRIDGE_TOOLS", "SDK_BRIDGE_PERMISSION_MODE", "SDK_BRIDGE_STRICT_MCP", "SDK_BRIDGE_CLAUDEAI_MCP"];

export function configFromEnv(env = process.env) {
  const host = (env.SDK_BRIDGE_HOST || "127.0.0.1").trim();
  if (!ALLOWED_HOSTS.includes(host)) throw new Error(`SDK_BRIDGE_HOST must be one of ${ALLOWED_HOSTS.join(", ")}`);
  const profile = (env.SDK_BRIDGE_PROFILE || "pilot").trim();
  if (!["pilot", "agent"].includes(profile)) throw new Error("SDK_BRIDGE_PROFILE must be pilot or agent");
  const config = {
    tokenSha256: (env.SDK_BRIDGE_TOKEN_SHA256 || "").trim().toLowerCase(),
    host, profile,
    port: Number(env.SDK_BRIDGE_PORT || 8768),
    ledger: env.SDK_BRIDGE_LEDGER || "",
    model: env.SDK_BRIDGE_MODEL || undefined,
    maxTurns: Number(env.SDK_BRIDGE_MAX_TURNS || 8),
    maxBudgetUsd: Number(env.SDK_BRIDGE_MAX_BUDGET_USD || 2),
    timeoutSeconds: Number(env.SDK_BRIDGE_TIMEOUT_SECONDS || 600),
    denySessions: list(env.SDK_BRIDGE_DENY_SESSIONS),
  };
  if (!/^[0-9a-f]{64}$/.test(config.tokenSha256)) throw new Error("SDK_BRIDGE_TOKEN_SHA256 must be a 64-character hex SHA-256");
  if (!path.isAbsolute(config.ledger)) throw new Error("SDK_BRIDGE_LEDGER must be an absolute path");
  if (!Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) throw new Error("SDK_BRIDGE_PORT must be 1024-65535");
  for (const [k, [lo, hi, integer]] of Object.entries(LIMITS)) {
    const v = config[k];
    if (!Number.isFinite(v) || v < lo || v > hi || (integer && !Number.isInteger(v)))
      throw new Error(`${envName(k)} must be ${integer ? "an integer" : "a number"} from ${lo} to ${hi}`);
  }
  if (profile === "pilot") {
    const stray = AGENT_KEYS.filter((k) => env[k] !== undefined && env[k] !== "");
    if (stray.length) throw new Error(`${stray.join(", ")} need SDK_BRIDGE_PROFILE=agent`);
    config.workspace = env.SDK_BRIDGE_WORKSPACE || "";
    if (!path.isAbsolute(config.workspace)) throw new Error("SDK_BRIDGE_WORKSPACE must be an absolute path");
    return config;
  }
  if (env.SDK_BRIDGE_WORKSPACE) throw new Error("The agent profile uses SDK_BRIDGE_WORKSPACE_ROOT, not SDK_BRIDGE_WORKSPACE");
  const workspaceRoot = env.SDK_BRIDGE_WORKSPACE_ROOT || "";
  if (!path.isAbsolute(workspaceRoot)) throw new Error("SDK_BRIDGE_WORKSPACE_ROOT must be an absolute path");
  const templatePins = {};
  for (const item of list(env.SDK_BRIDGE_TEMPLATE_SHA256)) {
    const at = item.lastIndexOf("="), file = item.slice(0, at).trim(), hash = item.slice(at + 1).trim().toLowerCase();
    if (at < 1 || !/^[0-9a-f]{64}$/.test(hash) || path.isAbsolute(file) || file.split("/").includes("..") || file in templatePins)
      throw new Error("SDK_BRIDGE_TEMPLATE_SHA256 entries must be <relative path>=<64-hex sha256>, each path once");
    templatePins[file] = hash;
  }
  if (!Object.keys(templatePins).length) throw new Error("SDK_BRIDGE_TEMPLATE_SHA256 is required for the agent profile");
  const claudePath = env.SDK_BRIDGE_CLAUDE_PATH || undefined;
  if (claudePath !== undefined && !path.isAbsolute(claudePath)) throw new Error("SDK_BRIDGE_CLAUDE_PATH must be an absolute path");
  const title = (env.SDK_BRIDGE_TITLE || "").trim();
  if (!title.includes("{id}")) throw new Error("SDK_BRIDGE_TITLE must contain {id}, so a bridge session never takes the agent's own title");
  const settings = env.SDK_BRIDGE_SETTINGS || undefined;
  if (settings !== undefined && !path.isAbsolute(settings)) throw new Error("SDK_BRIDGE_SETTINGS must be an absolute path");
  const settingSources = env.SDK_BRIDGE_SETTING_SOURCES === undefined ? ["user", "project"] : list(env.SDK_BRIDGE_SETTING_SOURCES);
  if (settingSources.some((s) => !SETTING_SOURCES.includes(s))) throw new Error("SDK_BRIDGE_SETTING_SOURCES: user, project, local only");
  if (!settingSources.includes("project")) throw new Error("SDK_BRIDGE_SETTING_SOURCES must include project (workspace CLAUDE.md and deny list)");
  const rawTools = (env.SDK_BRIDGE_TOOLS || "preset").trim();
  const permissionMode = (env.SDK_BRIDGE_PERMISSION_MODE || "default").trim();
  if (!PERMISSION_MODES.includes(permissionMode)) throw new Error(`SDK_BRIDGE_PERMISSION_MODE must be one of ${PERMISSION_MODES.join(", ")}`);
  const flag = (k) => { const v = (env[k] || "0").trim(); if (!["0", "1"].includes(v)) throw new Error(`${k} must be 0 or 1`); return v === "1"; };
  config.agent = { workspaceRoot, templatePins, claudePath, title, settings, settingSources, tools: rawTools === "preset" ? "preset" : list(rawTools),
    permissionMode, strictMcp: flag("SDK_BRIDGE_STRICT_MCP"), claudeAiMcp: flag("SDK_BRIDGE_CLAUDEAI_MCP") };
  return config;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = configFromEnv();
  const { query } = await import("@anthropic-ai/claude-agent-sdk");
  const handle = createBridge({ query, config, ledger: new Ledger(config.ledger) });
  createServer((req, res) => handle(req, res).catch(() => { try { res.end(); } catch {} }))
    .listen(config.port, config.host, () => console.log(`Agent Fabric Claude SDK bridge ${VERSION} on ${config.host}:${config.port}`
      + ` profile=${config.profile}`
      + (config.agent ? ` mode=${config.agent.permissionMode} sources=${config.agent.settingSources.join("+")}` : "")
      + ` caps=${config.maxTurns} turns/$${config.maxBudgetUsd}/${config.timeoutSeconds}s denied=${config.denySessions.length}`));
}
