/*
 * LeadByAI Agent Fabric: one-time OpenBot roster importer.
 *
 * Paste this entire file into the developer console while signed in at
 * https://openbot.leadbyai.bot. It only calls the authenticated OpenBot API.
 * It does not call Herdr, send agent messages, test endpoints or change agents.
 *
 * This file deliberately does NOT implement continuous synchronization.
 */
(async () => {
  "use strict";

  if (window.location.origin !== "https://openbot.leadbyai.bot") {
    window.alert("Open https://openbot.leadbyai.bot and sign in first.");
    return;
  }

  const sources = [
    {
      label: "LBAI Admin",
      origin: "https://lbai-agents.leadbyai.bot",
      roster: [
        ["lbai", "w1:p2", "LBAI Main"],
        ["lbai", "w3:p1", "LBAI Website"],
        ["lbai", "w4:p1", "LBAI Blog Desk"],
        ["lbai", "w5:p1", "LBAI Distribution"],
        ["lbai", "w6:p1", "LBAI Quoting"],
        ["lbai", "w7:p1", "LBAI Connections"],
        ["lbai", "w8:p1", "LBAI Sales"],
        ["lbai", "wA:p1", "LBAI Voice Lead"],
        ["lbai", "wB:p1", "LBAI Voice App"],
        ["lbai", "wC:p1", "LBAI Voice QA"],
        ["3WM", "w1:p1", "3 Wise Men - GPT"],
        ["3WM", "w1:p2", "3 Wise Men - Claude"],
        ["3WM", "w1:p3", "3 Wise Men - Grok"],
      ],
    },
    {
      label: "Arcaiva",
      origin: "https://arcaiva-agents.leadbyai.bot",
      roster: [
        ["Arcaiva-Claude", "w1:p1", "PM - Arcaiva"],
        ["Arcaiva-Claude", "w2:p1", "Arcaiva DB"],
        ["Arcaiva-Claude", "w3:p1", "Arcaiva Pricing"],
        ["Arcaiva-Claude", "w4:p1", "Arcaiva Contractor-WIP"],
        ["Arcaiva-Claude", "w5:p1", "Arcaiva Container"],
        ["Arcaiva-Claude", "w6:p1", "Arcaiva Marketing"],
        ["Arcaiva-Claude", "w7:p1", "Archy Owner"],
        ["Arcaiva-Claude", "w8:p1", "AR Totals"],
        ["Arcaiva-Claude", "w9:p1", "Arcaiva Docs"],
        ["Arcaiva-Claude", "wA:p1", "Arcaiva Demo"],
        ["Arcaiva-Claude", "wC:p1", "Arcaiva Fixes"],
      ],
    },
  ];

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        `${path}: HTTP ${response.status}: ${payload.error || "request failed"}`,
      );
    }
    return payload;
  };

  let existing;
  try {
    const result = await api("/api/agents");
    if (!Array.isArray(result.agents)) {
      throw new Error("OpenBot's /api/agents response has an unexpected shape.");
    }
    existing = result.agents;
  } catch (error) {
    window.alert(`Read-only preflight failed: ${error.message}`);
    return;
  }

  const knownEndpoints = new Set(existing.map((agent) => agent.endpoint).filter(Boolean));
  const knownNames = new Set(existing.map((agent) => agent.name).filter(Boolean));
  const roster = sources.flatMap((source) =>
    source.roster.map(([session, pane, name]) => ({
      source,
      session,
      pane,
      name,
      endpoint: `${source.origin}/sessions/${session}/agents/${pane}/ag-ui`,
    })),
  );
  const skipped = roster.filter((agent) =>
    knownEndpoints.has(agent.endpoint) || knownNames.has(agent.name),
  );
  const pending = roster.filter((agent) => !skipped.includes(agent));

  if (pending.length === 0) {
    window.alert(`All ${roster.length} known agents already have OpenBot entries.`);
    return;
  }
  if (!window.confirm(
    `Import ${pending.length} private Bots into this OpenBot account?\n` +
    `${skipped.length} already exist by name or endpoint and will be left unchanged.\n\n` +
    "This does NOT send any messages to agents or modify their Herdr sessions.",
  )) return;

  const credentials = new Map();
  for (const source of sources.filter((s) => pending.some((a) => a.source === s))) {
    const input = window.prompt(
      `Paste the Agent Fabric bearer token for ${source.label}.\n` +
      "The token stays in this browser script and is sent only to your OpenBot server.",
    );
    if (!input || !input.trim()) {
      window.alert("Import cancelled; no new Bots were created.");
      return;
    }
    const trimmed = input.trim();
    credentials.set(source, /^Bearer\s+/i.test(trimmed) ? trimmed : `Bearer ${trimmed}`);
  }

  const created = [];
  const errors = [];
  for (const agent of pending) {
    try {
      const result = await api("/api/agents", {
        method: "POST",
        body: JSON.stringify({
          name: agent.name,
          title: agent.name,
          roleDescription:
            `Existing Herdr-managed agent in ${agent.session}. ` +
            "Follow the agent's existing project instructions and work in its current session.",
          visibility: "private",
          endpoint: agent.endpoint,
          auth: {
            header: "Authorization",
            value: credentials.get(agent.source),
          },
        }),
      });
      created.push({ name: agent.name, id: result.agent?.id });
      console.info(`[Agent Fabric] Created ${agent.name}`);
    } catch (error) {
      errors.push({ name: agent.name, reason: error.message });
      console.error(`[Agent Fabric] Failed ${agent.name}: ${error.message}`);
    }
  }
  credentials.clear();
  console.table({
    created: created.length,
    alreadyPresent: skipped.length,
    failed: errors.length,
    totalKnown: roster.length,
  });
  if (errors.length) console.table(errors);
  window.alert(
    `Agent Fabric import finished.\nCreated: ${created.length}\n` +
    `Already present: ${skipped.length}\nFailed: ${errors.length}\n` +
    "Refresh OpenBot's Bots page to see the imported roster.",
  );
})();
