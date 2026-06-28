const fmtTime = (ms) => {
  if (!ms) return "";
  return new Date(ms).toLocaleTimeString();
};

const statusClass = (value) => {
  if (value === true || value === "ok") return "ok";
  if (value === false || value === "degraded" || value === "invalid") return "bad";
  return "warn";
};

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[ch]));

const td = (text, cls = "") => `<td class="${cls}">${esc(text)}</td>`;

const idButton = (kind, id, label = id) => `
  <button type="button" class="linkBtn" data-kind="${esc(kind)}" data-id="${esc(id)}">${esc(label)}</button>
`;

let catalogCache = null;
let capabilityCache = null;

async function getJson(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}

function setJson(id, value) {
  document.getElementById(id).textContent = JSON.stringify(value, null, 2);
}

function renderCatalog(catalog) {
  document.getElementById("catalogBody").innerHTML = (catalog.endpoints || []).map((endpoint) => `
    <tr>
      ${td(endpoint.spec)}
      ${td(endpoint.method, endpoint.mutating ? "warn mono" : "ok mono")}
      ${td(endpoint.path, "mono")}
      ${td(endpoint.operation_id || "", "mono")}
      ${td(endpoint.capability?.class || "", capabilityClass(endpoint.capability?.class))}
      ${td(endpoint.summary || "")}
    </tr>
  `).join("");
}

function capabilityClass(value) {
  if (value === "safe_probe") return "ok";
  if (value === "destructive" || value === "costly_or_async" || value === "upload_external") return "bad";
  return "warn";
}

function renderCapabilities(matrix) {
  const counts = matrix.counts || {};
  document.getElementById("capabilityCounts").innerHTML = Object.keys(counts).sort().map((key) => `
    <span class="chip ${capabilityClass(key)}"><span>${esc(key)}</span><strong>${esc(counts[key])}</strong></span>
  `).join("");

  document.getElementById("capabilityBody").innerHTML = (matrix.endpoints || []).map((endpoint) => {
    const capability = endpoint.capability || {};
    return `
      <tr>
        ${td(capability.class || "", capabilityClass(capability.class))}
        ${td(capability.risk || "", capabilityClass(capability.class))}
        ${td(endpoint.spec)}
        ${td(endpoint.method, endpoint.mutating ? "warn mono" : "ok mono")}
        ${td(endpoint.path, "mono")}
        ${td(capability.reason || "")}
      </tr>
    `;
  }).join("");
}

function renderTelemetry(telemetry) {
  const counts = telemetry.counts || {};
  document.getElementById("requestTotal").textContent = counts.total || 0;
  document.getElementById("degradedTotal").textContent = counts.degraded || 0;
  document.getElementById("errorTotal").textContent = counts.errors || 0;

  const tables = telemetry.db_state?.tables || {};
  const dbRows = Object.values(tables).reduce((total, table) => total + (table.rows || 0), 0);
  document.getElementById("dbRows").textContent = dbRows;

  document.getElementById("requestsBody").innerHTML = (telemetry.requests || []).map((r) => `
    <tr>
      ${td(fmtTime(r.updated_ms))}
      <td>${idButton("request", r.request_id)}</td>
      ${td(r.source_mod)}
      ${td(r.channel)}
      ${td(r.status, statusClass(r.status))}
      ${td(r.latency_ms ? `${r.latency_ms} ms` : "")}
      ${td(r.error || "")}
    </tr>
  `).join("");

  document.getElementById("probesBody").innerHTML = (telemetry.player2_probes || []).map((p) => `
    <tr>
      ${td(fmtTime(p.ts_ms))}
      <td>${idButton("probe", p.id, p.name)}</td>
      ${td(`${p.method} ${p.path}`, "mono")}
      ${td(p.ok ? "yes" : "no", p.ok ? "ok" : "bad")}
      ${td(p.latency_ms ? `${p.latency_ms} ms` : "")}
      ${td(p.error || "")}
    </tr>
  `).join("");

  document.getElementById("eventsList").innerHTML = (telemetry.events || []).map((e) => `
    <div class="event">
      <div class="eventTop">
        <span>${esc(fmtTime(e.ts_ms))} · ${idButton("event", e.id, e.kind)}</span>
        <span class="${statusClass(e.status)}">${esc(e.status || "")}</span>
      </div>
      <div><span class="mono">${esc(e.request_id || "")}</span> ${esc(e.source_mod || "")} ${esc(e.channel || "")}</div>
      ${e.error ? `<div class="bad">${esc(e.error)}</div>` : ""}
    </div>
  `).join("");
}

let selectedNpcKey = null;
let selectedSave = null;

const tierClass = (tier) => ({ core: "ok", significant: "warn", routine: "" }[tier] || "");

function renderSaves(savesResp) {
  const list = (savesResp && savesResp.saves) || [];
  // Auto-select the most-recently-active save (instead of defaulting to "demo") so the dashboard
  // shows the live playthrough on load. Click another chip to override.
  if (!selectedSave && list.length) {
    selectedSave = list.reduce((a, b) => ((b.last_active_ms || 0) > (a.last_active_ms || 0) ? b : a)).save_id;
  }
  document.getElementById("savesStrip").innerHTML = list.map((s) => `
    <span class="saveChip ${s.save_id === selectedSave ? "saveSel" : ""}" data-save="${esc(s.save_id)}">
      <strong>${esc(s.save_id)}</strong>
      <span class="dim">${esc(s.npcs)} npc · ${esc(s.facts)} fact</span>
      <span class="saveReset" data-resetsave="${esc(s.save_id)}" title="Reset this save">✕</span>
    </span>
  `).join("") || `<span class="dim">No saves yet.</span>`;
}

// Grounded entityrole (md/Boarding.xml: marine/service, else crew). The raw skill number is
// deliberately not shown — it informs the persona, not the table.
function roleSkill(n) {
  return n.role || "—";
}

function renderMemory(memNpcs, memMetrics) {
  const npcs = (memNpcs.npcs || []).filter((n) => !selectedSave || n.save_id === selectedSave);
  const m = memMetrics || {};
  document.getElementById("npcTotal").textContent = m.npcs ?? npcs.length;
  document.getElementById("factTotal").textContent = m.facts ?? 0;

  const byTier = m.facts_by_tier || {};
  document.getElementById("memoryCounts").innerHTML = [
    ["npcs", m.npcs ?? npcs.length, ""],
    ["raw turns", m.turns ?? 0, ""],
    ["core", byTier.core || 0, "ok"],
    ["significant", byTier.significant || 0, "warn"],
    ["routine", byTier.routine || 0, ""],
  ].map(([k, v, cls]) => `<span class="chip ${cls}"><span>${esc(k)}</span><strong>${esc(v)}</strong></span>`).join("");

  document.getElementById("npcsBody").innerHTML = npcs.map((n) => `
    <tr class="rowBtn ${n.npc_key === selectedNpcKey ? "rowSel" : ""}" data-npc="${esc(n.npc_key)}">
      ${td(n.name || "(unnamed)")}
      ${td(n.faction_id || "")}
      ${td(roleSkill(n))}
      ${td(`${n.save_id || "—"} / ${n.game_id || "—"}`, "mono")}
      ${td(n.turns)}
      ${td(n.facts)}
      ${td(n.core_facts, n.core_facts ? "ok" : "")}
      ${td(n.npc_id || "(unbound)", "mono")}
    </tr>
  `).join("") || `<tr><td colspan="8" class="dim">No NPCs yet — send an NPC request to populate memory.</td></tr>`;
}

async function showNpc(npcKey) {
  selectedNpcKey = npcKey;
  const data = await getJson(`/api/memory/npc?npc_key=${encodeURIComponent(npcKey)}`);
  const npc = data.npc || {};
  document.getElementById("npcDetailTitle").textContent = `Memories — ${npc.name || npcKey}`;

  const skills = (() => { try { return JSON.parse(npc.skills || "{}"); } catch (e) { return {}; } })();
  const stars = (lvl) => { const f = Math.max(0, Math.min(5, Math.floor((+lvl || 0) / 3))); return "★".repeat(f) + "☆".repeat(5 - f); };
  const statBits = [];
  for (const [label, val] of [["role", npc.role], ["race", npc.race], ["class", npc.ship_class], ["ship", npc.ship_name], ["sector", npc.sector], ["faction", npc.faction_id]]) {
    if (val) statBits.push(`<span class="badge">${esc(label)}: ${esc(val)}</span>`);
  }
  // 0-15 crew stats only. "combined"/"combinedskill" (the engine's 0-100 overall) is excluded —
  // the raw number isn't useful to display; it colors the persona instead.
  const skillRows = Object.entries(skills)
    .filter(([k, v]) => v && k !== "combined" && k !== "combinedskill")
    .map(([k, v]) =>
      `<div class="skillRow"><span class="skillName">${esc(k)}</span><span class="stars">${stars(v)}</span><span class="dim">${esc(v)}/15</span></div>`).join("");
  document.getElementById("npcSummary").innerHTML = `
    ${statBits.length ? `<div class="statChips">${statBits.join("")}</div>` : `<div class="dim">No X4 stats attached yet (send them in target.stats).</div>`}
    ${skillRows ? `<div class="skillsBox">${skillRows}</div>` : ""}
    ${npc.summary ? `<div class="gist"><span class="label">Gist</span> ${esc(npc.summary)}</div>` : `<div class="dim">No condensed gist yet.</div>`}
  `;

  // SPEC 2a: the situated PersonaCard (archetype + AUTHORITY + concerns + can/cannot), consolidated on the sheet.
  renderPersonaCard(npc);

  // I5: persistent IDENTITY panel — the spec's "why bound?" + binding status. The identity survives reloads
  // even though X4's runtime component id does not (#99); fed by /api/identity via the npc's persistent_key.
  (async () => {
    const el = document.getElementById("npcIdentity");
    if (!el) return;
    const pkey = npc.persistent_key;
    if (!pkey) { el.innerHTML = `<div class="dim">No persistent identity yet — run identity backfill or a session rebind.</div>`; return; }
    try {
      const idr = await getJson(`/api/identity?persistent_npc_key=${encodeURIComponent(pkey)}`);
      if (!idr.ok) { el.innerHTML = ""; return; }
      const id = idr.identity || {};
      const ev = idr.evidence || [];
      const TIERS = ["faction-abstraction", "player-significant", "local-important", "background"];
      const tier = (id.importance_tier != null ? id.importance_tier : 3);
      const statusClass = ({ bound: "ok", tentative: "warn", ambiguous: "warn" })[id.status] || "dim";
      const conf = (id.identity_confidence != null) ? Math.round(id.identity_confidence * 100) + "%" : "—";
      const rt = (idr.bindings && idr.bindings[0] && idr.bindings[0].runtime_component_id) || npc.npc_id || "—";
      const collisions = idr.name_collisions || 0;
      const memN = (idr.memory_keys || []).length;
      const seenTs = (idr.bindings && idr.bindings[0] && idr.bindings[0].seen_at) || id.updated_at;
      const ago = (() => {
        const t = +seenTs; if (!t) return "—";
        const d = Math.max(0, Date.now() / 1000 - t);
        if (d < 60) return "moments ago"; if (d < 3600) return Math.floor(d / 60) + "m ago";
        if (d < 86400) return Math.floor(d / 3600) + "h ago"; return Math.floor(d / 86400) + "d ago";
      })();
      el.innerHTML = `
        <div class="identHead"><strong>Identity</strong>
          <span class="badge ${statusClass}">${esc(id.status || "session-only")}</span>
          <span class="badge">tier ${esc(tier)} · ${esc(TIERS[tier] || "")}</span>
          <span class="badge">conf ${esc(conf)}</span>
          ${collisions ? `<span class="badge warn">⚠ ${esc(collisions)} same-name collision${collisions > 1 ? "s" : ""}</span>` : ""}
        </div>
        <div class="identMeta dim">key <code>${esc(id.persistent_npc_key)}</code> · runtime <code>${esc(rt)}</code> · ${esc(memN)} memory link${memN === 1 ? "" : "s"} (cross-reload) · evidence ${esc(ev.length)} · last seen ${esc(ago)}</div>
        ${ev.length ? `<div class="identWhy"><span class="label">why bound?</span> ${ev.map((e) => `<span class="badge">${esc(e.evidence_type)}: ${esc(e.value)}${e.weight ? ` <span class="dim">+${esc(e.weight)}</span>` : ""}</span>`).join(" ")}</div>` : ""}
      `;
    } catch (e) { el.innerHTML = ""; }
  })();

  document.getElementById("npcFacts").innerHTML = (data.facts || []).map((f) => `
    <div class="fact ${tierClass(f.tier)}">
      <div class="factTop">
        <span class="badge ${tierClass(f.tier)}">${esc(f.tier)}</span>
        <span class="badge">${esc(f.category)}</span>
        <span class="badge">imp ${esc(f.importance)}</span>
        ${f.verbatim ? `<span class="badge ok">verbatim</span>` : ""}
      </div>
      <div class="factText">${esc(f.text)}</div>
    </div>
  `).join("") || `<div class="dim">No durable facts yet (condensation triggers once the raw window overflows).</div>`;

  // M7: memory-audit integrity view — durable count + the high-value turns NOT yet promoted (the
  // "talks a lot, stores few" gap, per-NPC; A4's auto-promotion keeps candidates small during play).
  try {
    const audit = await getJson("/v1/memory/audit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ npc_key: npcKey }) });
    const cands = audit.promotion_candidates || [];
    document.getElementById("npcAudit").innerHTML = `
      <div class="auditHead"><strong>Memory audit</strong> —
        <span class="ok">${esc(audit.durable_fact_count || 0)} durable</span> ·
        <span class="${audit.promotion_candidate_count ? "warn" : "dim"}">${esc(audit.promotion_candidate_count || 0)} not yet promoted</span></div>
      ${cands.map((c) => `
        <div class="fact ${tierClass(c.tier)}">
          <div class="factTop"><span class="badge ${tierClass(c.tier)}">${esc(c.tier)}</span><span class="badge">${esc(c.category)}</span><span class="badge">${esc(c.role)}</span></div>
          <div class="factText">${esc(c.text)}</div>
        </div>`).join("")}
    `;
  } catch (e) { const el = document.getElementById("npcAudit"); if (el) el.innerHTML = ""; }

  const turns = data.turns || [];
  document.getElementById("npcTurnsHead").style.display = turns.length ? "block" : "none";
  document.getElementById("npcTurns").innerHTML = turns.map((t) => `
    <div class="turn ${t.role === "assistant" ? "turnNpc" : "turnPlayer"}">
      <span class="turnRole">${esc(t.role)}</span> ${esc(t.text)}
    </div>
  `).join("");
}

// SPEC 2a — render the per-NPC PersonaCard + authority contract (the same card injected into the live chat),
// fetched on demand for the selected NPC and shown consolidated with the rest of the sheet.
async function renderPersonaCard(npc) {
  const el = document.getElementById("npcPersona");
  if (!el) return;
  el.innerHTML = `<div class="dim">Building role card…</div>`;
  let skills = {}; try { skills = JSON.parse(npc.skills || "{}"); } catch (e) {}
  const combined = skills.combined || skills.combinedskill;
  try {
    const r = await getJson("/v1/persona/card", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        save_id: npc.save_id || "", npc_name: npc.name, npc_short_name: npc.short_name,
        faction_id: npc.faction_id, role: npc.role, npc_skill: combined,
        ship_name: npc.ship_name, sector: npc.sector,
      }),
    });
    if (!r || !r.ok || !r.card) { el.innerHTML = ""; return; }
    const c = r.card;
    const li = (arr) => (arr || []).map((x) => `<li>${esc(x)}</li>`).join("");
    el.innerHTML = `
      <div class="personaCard">
        <div class="personaHead">
          <span class="badge role">${esc((c.archetype || "").replace(/_/g, " "))}</span>
          <span class="badge auth">authority: ${esc(c.authority_level || "—")}</span>
        </div>
        <div class="personaRole">${esc(c.role_descriptor || "")}</div>
        ${c.personality ? `<div class="personaLine"><span class="label">Temperament</span> ${esc(c.personality)}</div>` : ""}
        ${(c.current_concerns || []).length ? `<div class="personaLine"><span class="label">Concerns</span> ${esc(c.current_concerns.join("; "))}</div>` : ""}
        ${c.wants ? `<div class="personaLine"><span class="label">Wants</span> ${esc(c.wants)}</div>` : ""}
        ${c.knowledge_scope ? `<div class="personaLine"><span class="label">Knows</span> ${esc(c.knowledge_scope)}</div>` : ""}
        ${c.conversation_consequence ? `<div class="personaLine"><span class="label">Leads to</span> ${esc(c.conversation_consequence)}</div>` : ""}
        ${c.redirect_to ? `<div class="personaLine"><span class="label">Redirects to</span> ${esc(c.redirect_to)}</div>` : ""}
        <div class="personaCols">
          <div class="personaCol"><span class="label ok">Can</span><ul>${li(c.can_do)}</ul></div>
          <div class="personaCol"><span class="label warn">Cannot</span><ul>${li(c.cannot_do)}</ul></div>
        </div>
      </div>`;
  } catch (e) { el.innerHTML = ""; }
}

async function runMemorySelftest() {
  const btn = document.getElementById("memSelftestBtn");
  btn.disabled = true; btn.textContent = "Running...";
  try {
    const r = await getJson("/api/memory/selftest");
    setJson("detailJson", r);
    btn.textContent = r.ok ? `Self-test ✓ ${r.passed}/${r.total}` : `Self-test ✗ ${r.passed}/${r.total}`;
  } catch (err) {
    btn.textContent = "Self-test error";
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = "Run memory self-test"; }, 4000);
  }
}

function renderEvents(st) {
  const cfg = st.config || {};
  document.getElementById("eventCounts").innerHTML = [
    ["pending", st.pending || 0, (st.pending > 0 ? "warn" : "ok")],
    ["interval", `${cfg.flush_interval_s}s`, ""],
    ["batch", cfg.batch_size, ""],
    ["worker", cfg.worker_running ? "running" : "stopped", cfg.worker_running ? "ok" : "bad"],
    ["flushes", st.total_flushes || 0, ""],
    ["resolved", st.total_events_resolved || 0, ""],
  ].map(([k, v, cls]) => `<span class="chip ${cls}"><span>${esc(k)}</span><strong>${esc(v)}</strong></span>`).join("");

  document.getElementById("flushBody").innerHTML = (st.flushes || []).map((f) => `
    <tr>
      ${td(fmtTime(f.ts_ms))}
      ${td(f.reason)}
      ${td(f.batch_size)}
      ${td(f.coalesced)}
      ${td(f.latency_ms ? `${f.latency_ms} ms` : "")}
      ${td(f.ok ? "yes" : "no", f.ok ? "ok" : "bad")}
      ${td((f.resolution || "").slice(0, 200))}
    </tr>
  `).join("") || `<tr><td colspan="7" class="dim">No flushes yet — simulate events and watch the light cycle.</td></tr>`;
}

async function evtAction(path, btnId, label) {
  const btn = document.getElementById(btnId);
  btn.disabled = true; const prev = btn.textContent; btn.textContent = "...";
  try { await getJson(path); await refresh(); }
  finally { btn.disabled = false; btn.textContent = prev; }
}

const universeSave = () => selectedSave || "demo";
const relColor = (v) => { v = +v || 0; return v >= 20 ? "ok" : (v <= -20 ? "bad" : ""); };
const biasCell = (b, k) => (b && typeof b[k] === "number" ? td(Math.round(b[k] * 100)) : td(""));

function renderUniverse(factionsResp, relsResp, saveId) {
  document.getElementById("relTitle").textContent = "Relationships — " + saveId;
  const fs = (factionsResp && factionsResp.factions) || [];
  document.getElementById("factionsBody").innerHTML = fs.map((f) => {
    const b = f.biases || {};
    return `<tr>${td(f.faction_id)}${td(f.name || "")}${td(f.current_goal || "")}${td(f.mood || "")}${biasCell(b, "aggression")}${biasCell(b, "economic_focus")}${biasCell(b, "risk_tolerance")}${biasCell(b, "diplomacy")}</tr>`;
  }).join("") || `<tr><td colspan="8" class="dim">No factions for ${esc(saveId)} — click "Seed demo".</td></tr>`;
  const rs = (relsResp && relsResp.relationships) || [];
  document.getElementById("relationshipsBody").innerHTML = rs.map((r) => `
    <tr>${td(r.subject)}${td("→ " + r.object)}${td(r.trust, relColor(r.trust))}${td(r.fear, r.fear >= 20 ? "warn" : "")}${td(r.resentment, r.resentment >= 20 ? "bad" : "")}${td(r.debt)}${td(r.standing || "")}</tr>
  `).join("") || `<tr><td colspan="7" class="dim">No relationships yet.</td></tr>`;
}

// ---- Influence Log: every relationship change THIS MOD caused in-game ----
function renderInfluenceLog(resp, saveId) {
  const t = document.getElementById("influenceLogTitle");
  if (t) t.textContent = "Influence Log — mod-caused changes — " + saveId;
  const body = document.getElementById("influenceLogBody");
  if (!body) return;
  const rows = (resp && resp.entries) || [];
  body.innerHTML = rows.map((e) => {
    const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
    const oldv = (e.old_relation === null || e.old_relation === undefined) ? "—" : (+e.old_relation).toFixed(2);
    const newv = (e.new_relation === null || e.new_relation === undefined) ? "" : (+e.new_relation).toFixed(2);
    const cls = (+e.new_relation <= -0.2) ? "bad" : ((+e.new_relation >= 0.2) ? "ok" : "");
    return `<tr>${td(when)}${td(e.subject)}${td("→ " + e.object)}${td(oldv)}${td(newv, cls)}${td(e.standing || "", cls)}${td(e.source || "")}</tr>`;
  }).join("") || `<tr><td colspan="7" class="dim">No mod-caused changes yet — dispatch a relation change in-game.</td></tr>`;
}

// ---- Influence-engine substrate renderers (the DB, displayed for debugging) ----
const pct = (v) => (v === null || v === undefined || v === "" ? "" : Math.round((+v) * 100));
const pressCell = (v, warnAt = 50) => td(pct(v), (+v * 100 >= warnAt ? "warn" : ""));
const jlist = (a) => (Array.isArray(a) ? a.join(", ") : (a == null ? "" : String(a)));
// Render a small object as "k:v" pairs. Only treat fractional 0..1 numbers as
// percentages (e.g. a 0.8 shortage -> 80%); leave real quantities (duration_h:50) as-is.
const jshort = (o) => { try { return o == null ? "" : Object.entries(o).map(([k, v]) => `${k}:${(typeof v === "number" && v > 0 && v <= 1) ? Math.round(v * 100) + "%" : v}`).join(", "); } catch (e) { return ""; } };

function renderStrategic(resp, saveId) {
  document.getElementById("strategicTitle").textContent = "Strategic Pressures — " + saveId;
  const rows = (resp && resp.strategic_state) || [];
  document.getElementById("strategicBody").innerHTML = rows.map((s) => `
    <tr>${td(s.faction_id)}${pressCell(s.military_pressure)}${pressCell(s.economic_pressure)}${pressCell(s.logistics_stress)}${pressCell(s.recent_losses, 40)}${pressCell(s.territorial_pressure)}${pressCell(s.piracy_pressure)}${td(Math.round((+s.player_alignment || 0) * 100), (+s.player_alignment < 0 ? "bad" : "ok"))}</tr>
  `).join("") || `<tr><td colspan="8" class="dim">No strategic_state — click "Seed demo".</td></tr>`;
}

function renderIncidents(resp) {
  const rows = (resp && resp.incidents) || [];
  document.getElementById("incidentsTitle").textContent = `Incidents — pending actions (${rows.length})`;
  document.getElementById("incidentsBody").innerHTML = rows.map((i) => `
    <tr>${td(i.action_type, "mono")}${td(`${i.faction_id || "?"} → ${i.target || "?"}`)}${td(pct(i.confidence))}${td(i.priority)}${td(i.status, i.status === "pending" ? "warn" : (i.status === "applied" ? "ok" : ""))}${td((i.narrative || "").slice(0, 80))}</tr>
  `).join("") || `<tr><td colspan="6" class="dim">No incidents — the validator writes pending actions here.</td></tr>`;
}

function renderEconomy(resp) {
  const rows = (resp && resp.economy) || [];
  // #56: ware display names (#55 catalog) + per-faction captured-station count (audit the rollup against #54).
  const names = (resp && resp.ware_names) || {};
  const wl = (w) => names[w] || w;
  const lblNeeds = (arr) => (arr || []).map(wl).join(", ");
  const lblShort = (sh) => Object.entries(sh || {}).map(([w, v]) => `${wl(w)}:${Math.round((+v || 0) * 100)}%`).join(", ");
  document.getElementById("economyBody").innerHTML = rows.map((e) => `
    <tr>${td(e.faction_id)}${td(e.station_count != null ? e.station_count : "–", (+e.station_count > 0 ? "ok" : "dim"))}${td(pct(e.dependency_on_player))}${td(pct(e.production_health), (+e.production_health < 0.5 ? "bad" : ""))}${td(lblNeeds(e.key_needs))}${td(lblShort(e.shortages), "warn")}${td(e.market_status, e.market_status === "obstacle" ? "bad" : (e.market_status === "partner" ? "ok" : ""))}</tr>
  `).join("") || `<tr><td colspan="7" class="dim">No economy rows.</td></tr>`;
  const meta = (resp && resp.economy_meta) || {};
  const cap = document.getElementById("economyMeta");
  if (cap) cap.textContent = (meta.stations_captured || 0) + " stations captured · " + (meta.factions_covered || 0) + " factions";
}

function renderConflicts(resp) {
  const rows = (resp && resp.conflicts) || [];
  const losses = (resp && resp.losses) || {};
  document.getElementById("lossesChips").innerHTML = Object.entries(losses).map(([f, l]) =>
    `<span class="chip ${l.recent_losses >= 0.5 ? "bad" : "warn"}"><span>${esc(f)} losses</span><strong>${esc(Math.round(l.loss_total))}</strong></span>`).join("");
  document.getElementById("conflictsBody").innerHTML = rows.map((c) => `
    <tr>${td(`${c.faction_a} vs ${c.faction_b}`)}${td(c.status, c.status === "active" ? "bad" : "warn")}${td(pct(c.intensity))}${td(c.cause || "")}</tr>
  `).join("") || `<tr><td colspan="4" class="dim">No conflicts.</td></tr>`;
}

function renderSectors(resp) {
  const rows = (resp && resp.sectors) || [];
  document.getElementById("sectorsBody").innerHTML = rows.map((s) => `
    <tr>${td(s.sector_id, "mono")}${td(s.name || "")}${td(s.owner_faction || "")}${td(jlist(s.contested_by), s.contested_by && s.contested_by.length ? "warn" : "")}${td(pct(s.strategic_value))}${td(s.player_assets_present ? "yes" : "", s.player_assets_present ? "ok" : "")}</tr>
  `).join("") || `<tr><td colspan="6" class="dim">No sectors.</td></tr>`;
}

function renderFleets(resp) {
  const rows = (resp && resp.fleets) || [];
  document.getElementById("fleetsBody").innerHTML = rows.map((f) => `
    <tr>${td(f.faction_id || "")}${td(f.total_ships)}${td(f.fight, f.fight ? "warn" : "")}${td(f.trade)}${td(f.mine)}${td(f.build)}${td(f.capitals, f.capitals ? "ok" : "")}</tr>
  `).join("") || `<tr><td colspan="7" class="dim">No fleet data — reload X4 (~120s heartbeat).</td></tr>`;
}

function renderAgreements(resp) {
  const rows = (resp && resp.agreements) || [];
  document.getElementById("agreementsBody").innerHTML = rows.map((a) => `
    <tr>${td(`${a.party_a} ↔ ${a.party_b}`)}${td(a.type || "")}${td(a.status, a.status === "broken" ? "bad" : (a.status === "kept" ? "ok" : "warn"))}${td(jshort(a.terms))}</tr>
  `).join("") || `<tr><td colspan="4" class="dim">No agreements.</td></tr>`;
}

function renderWorldEvents(resp) {
  const rows = (resp && resp.world_events) || [];
  document.getElementById("worldEventsTitle").textContent = `World Events — durable history (${rows.length})`;
  document.getElementById("worldEventsBody").innerHTML = rows.map((e) => `
    <tr>${td(e.event_type, "mono")}${td(e.importance, e.importance >= 5 ? "bad" : (e.importance >= 3 ? "warn" : ""))}${td((e.summary || "").slice(0, 110))}${td(e.primary_faction || "")}${td(e.secondary_faction || "")}${td(e.sector_id || "")}</tr>
  `).join("") || `<tr><td colspan="6" class="dim">No world events.</td></tr>`;
}

function renderConversations(resp) {
  const rows = (resp && resp.conversations) || [];
  const title = document.getElementById("conversationsTitle");
  if (title) title.textContent = `Conversations — live chat transcript (${rows.length})`;
  const body = document.getElementById("conversationsBody");
  if (!body) return;
  body.innerHTML = rows.map((c) => {
    const when = c.created_at ? new Date(c.created_at * 1000).toLocaleTimeString() : "";
    const who = c.npc_name || c.faction_id || c.source_mod || "NPC";
    const player = c.player_name || "—";
    return `<tr>${td(when, "dim mono")}${td(player, "mono ok")}${td(who, "mono")}${td(c.prompt || "", "")}${td(c.reply || "", "warn")}${td(c.latency_ms != null ? c.latency_ms + "ms" : "", "dim")}</tr>`;
  }).join("") || `<tr><td colspan="6" class="dim">No conversations yet — send a chat message in-game (or POST /v1/request) and it'll appear here.</td></tr>`;
}

// ---- Player2 end-to-end pipeline stress (real prompts -> Player2 -> replies) ----
function renderP2(status) {
  const prog = document.getElementById("p2Progress");
  const counts = document.getElementById("p2Counts");
  if (!prog || !counts) return;
  const r = status.result;
  if (status.running) {
    const p = status.progress || {};
    prog.className = "warn";
    prog.textContent = `running: ${p.done}/${p.total} done · ok ${p.ok} · empty ${p.empty} · error ${p.error} · ${status.elapsed_s}s elapsed`;
  } else if (r && r.ok) {
    prog.className = "ok";
    prog.textContent = `done: ${r.calls} calls / ${r.threads} thread(s) in ${r.wall_s}s`;
  } else if (r) {
    prog.className = "bad";
    prog.textContent = `failed: ${r.error || ""}`;
  } else {
    prog.className = "dim";
    prog.textContent = "idle — run a stage to fire real prompts at Player2.";
  }
  if (r && r.ok) {
    counts.innerHTML = [
      ["success", `${Math.round(r.success_rate * 100)}%`, r.success_rate >= 0.99 ? "ok" : "bad"],
      ["ok", r.replies_ok, "ok"],
      ["empty", r.replies_empty, r.replies_empty ? "bad" : ""],
      ["errors", r.errors, r.errors ? "bad" : ""],
      ["throughput", `${r.throughput_per_min}/min`, ""],
      ["p50", `${r.latency_ms.p50} ms`, ""],
      ["p95", `${r.latency_ms.p95} ms`, r.latency_ms.p95 > 8000 ? "warn" : ""],
      ["max", `${r.latency_ms.max} ms`, r.latency_ms.max > 15000 ? "bad" : ""],
    ].map(([k, v, c]) => `<span class="chip ${c}"><span>${esc(k)}</span><strong>${esc(v)}</strong></span>`).join("");
    document.getElementById("p2RepliesBody").innerHTML = (r.sample_replies || []).map((s) =>
      `<tr>${td(s.i)}${td(`${s.latency_ms} ms`)}${td(s.reply)}</tr>`).join("") || `<tr><td colspan="3" class="dim">—</td></tr>`;
    document.getElementById("p2FailBody").innerHTML = (r.sample_failures || []).map((s) =>
      `<tr>${td(s.i)}${td(`${s.latency_ms} ms`)}${td(s.class, "bad")}${td(s.error)}</tr>`).join("") || `<tr><td colspan="4" class="dim">no failures</td></tr>`;
  }
}
let p2Poll = null;
function enableP2Btns(on) {
  ["p2s1", "p2s10", "p2s20", "p2s100"].forEach((id) => { const b = document.getElementById(id); if (b) b.disabled = !on; });
}
async function pollP2() {
  try {
    const s = await getJson("/api/player2/stress_status");
    renderP2(s);
    if (!s.running && p2Poll) { clearInterval(p2Poll); p2Poll = null; enableP2Btns(true); }
  } catch (e) { /* keep polling */ }
}
async function startP2(calls, threads) {
  enableP2Btns(false);
  try {
    const r = await getJson(`/api/player2/stress?calls=${calls}&threads=${threads}`);
    if (r.ok) {
      if (p2Poll) clearInterval(p2Poll);
      p2Poll = setInterval(pollP2, 1500);
      pollP2();
    } else {
      enableP2Btns(true);
      renderP2({ running: false, result: { ok: false, error: r.error } });
    }
  } catch (e) { enableP2Btns(true); }
}

const pct01 = (v) => (v == null || v === "") ? "" : Math.round(Number(v) * 100) + "%";

function renderSocial(resp, saveId) {
  const t = document.getElementById("socialTitle");
  if (t) t.textContent = "NPC Social Graph — " + saveId;
  const rows = (resp && resp.relations) || [];
  document.getElementById("socialBody").innerHTML = rows.map((r) => `
    <tr>
      ${td(r.subject_npc, "mono")}
      ${td(r.object_npc, "mono")}
      ${td(r.status, "ok")}
      ${td(r.relationship_type)}
      ${td(pct01(r.trust))}
      ${td(pct01(r.affection))}
      ${td(pct01(r.resentment))}
      ${td(pct01(r.loyalty))}
      ${td(pct01(r.rivalry))}
    </tr>
  `).join("") || `<tr><td colspan="9" class="dim">No NPC↔NPC social ties for ${esc(saveId)} yet.</td></tr>`;
}

function renderRumors(resp, saveId) {
  const t = document.getElementById("rumorTitle");
  if (t) t.textContent = "Rumors — " + saveId;
  const rows = (resp && resp.rumors) || [];
  document.getElementById("rumorBody").innerHTML = rows.map((r) => `
    <tr>
      ${td(r.text)}
      ${td(r.category)}
      ${td(r.origin_npc, "mono")}
      ${td(r.npc_key, "mono")}
      ${td(pct01(r.confidence))}
      ${td(r.hops)}
    </tr>
  `).join("") || `<tr><td colspan="6" class="dim">No rumors propagating in ${esc(saveId)} yet.</td></tr>`;
}

function renderPlayerRole(resp, saveId) {
  const t = document.getElementById("playerRoleTitle");
  if (t) t.textContent = "Player Role — " + saveId;
  const el = document.getElementById("playerRoleBody");
  if (!resp || resp.ok === false) { el.innerHTML = `<span class="dim">No role data for ${esc(saveId)}.</span>`; return; }
  const tags = (resp.role_tags || []).map((x) => `<span class="chip ok">${esc(x)}</span>`).join(" ") || "—";
  el.innerHTML = `
    <div><strong>Primary role:</strong> ${esc(resp.primary_role || "—")}</div>
    <div><strong>Tags:</strong> ${tags}</div>
    <div><strong>Faction friends:</strong> ${esc((resp.friends || []).join(", ") || "—")}</div>
    <div><strong>Faction threats:</strong> ${esc((resp.threats || []).join(", ") || "—")}</div>
    <div><strong>High economic dependency on player:</strong> ${esc((resp.high_dependency_factions || []).join(", ") || "—")}</div>
    <div><strong>Supplies enemies:</strong> ${resp.supplies_enemies ? "yes" : "no"} · <strong>Brokered deals:</strong> ${esc(resp.brokered_count || 0)}</div>
  `;
}

function renderBudgets(resp, saveId) {
  const t = document.getElementById("budgetTitle");
  if (t) t.textContent = "Faction Budgets — earned economy — " + saveId;
  const rows = (resp && resp.budgets) || [];
  const money = (v) => (v == null) ? "" : Math.round(Number(v)).toLocaleString();
  document.getElementById("budgetBody").innerHTML = rows.map((b) => `
    <tr>
      ${td(b.faction_id, "mono")}
      ${td(money(b.capacity))}
      ${td(money(b.spent), b.spent ? "warn" : "")}
      ${td(money(b.remaining), "ok")}
    </tr>
  `).join("") || `<tr><td colspan="4" class="dim">No economy-bearing factions for ${esc(saveId)} yet.</td></tr>`;
}

function renderLlmBudget(resp) {
  const el = document.getElementById("llmBudgetBody");
  if (!el) return;
  if (!resp || resp.ok === false) { el.innerHTML = `<span class="dim">No AI-power data.</span>`; return; }
  const budget = resp.budget ? esc(resp.budget) : "unlimited";
  const remaining = resp.remaining == null ? "—" : esc(resp.remaining);
  el.innerHTML = `
    <div><strong>Status:</strong> ${resp.killed ? '<span class="chip bad">PAUSED (kill switch on)</span>' : '<span class="chip ok">active</span>'}</div>
    <div><strong>Calls this session:</strong> ${esc(resp.calls || 0)}</div>
    <div><strong>Budget:</strong> ${budget} · <strong>Remaining:</strong> ${remaining}</div>
    <div class="dim">Controls: POST /v1/llm/budget_set {budget, killed, reset}</div>
  `;
}

async function refresh() {
  const uSave = universeSave();
  const post = (p) => getJson(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ save_id: uSave }) });
  const u = (p) => getJson(p + (p.includes("?") ? "&" : "?") + "save_id=" + encodeURIComponent(uSave));
  const [health, telemetry, memNpcs, memMetrics, evtState, saves, factions, rels,
         strategic, incidents, economy, conflicts, sectors, fleets, agreements, worldEvents, conversations, influenceLog, p2status,
         social, rumors, playerRole, budgets, llmBudget] = await Promise.all([
    getJson("/health"),
    getJson("/api/telemetry?limit=100"),
    getJson("/api/memory/npcs").catch(() => ({ npcs: [] })),
    getJson("/api/memory/metrics").catch(() => ({})),
    getJson("/api/events/state").catch(() => ({})),
    getJson("/api/memory/saves").catch(() => ({ saves: [] })),
    getJson("/api/factions?save_id=" + encodeURIComponent(uSave)).catch(() => ({ factions: [] })),
    getJson("/api/relationships?save_id=" + encodeURIComponent(uSave)).catch(() => ({ relationships: [] })),
    u("/api/strategic_state").catch(() => ({ strategic_state: [] })),
    u("/api/incidents").catch(() => ({ incidents: [] })),
    u("/api/economy").catch(() => ({ economy: [] })),
    u("/api/conflicts").catch(() => ({ conflicts: [], losses: {} })),
    u("/api/sectors").catch(() => ({ sectors: [] })),
    u("/api/fleets").catch(() => ({ fleets: [] })),
    u("/api/agreements").catch(() => ({ agreements: [] })),
    u("/api/world_events?limit=100").catch(() => ({ world_events: [] })),
    getJson("/api/conversations?limit=100").catch(() => ({ conversations: [] })),
    u("/api/influence_log?limit=50").catch(() => ({ entries: [] })),
    getJson("/api/player2/stress_status").catch(() => ({})),
    post("/v1/social/list").catch(() => ({ relations: [] })),
    post("/v1/rumor/list").catch(() => ({ rumors: [] })),
    post("/v1/player/role").catch(() => ({ ok: false })),
    post("/v1/economy/budget_list").catch(() => ({ budgets: [] })),
    post("/v1/llm/budget_status").catch(() => ({ ok: false })),
  ]);
  renderSaves(saves);
  renderMemory(memNpcs, memMetrics);
  renderEvents(evtState);
  renderUniverse(factions, rels, uSave);
  renderInfluenceLog(influenceLog, uSave);
  renderStrategic(strategic, uSave);
  renderIncidents(incidents);
  renderEconomy(economy);
  renderConflicts(conflicts);
  renderSectors(sectors);
  renderFleets(fleets);
  renderAgreements(agreements);
  renderWorldEvents(worldEvents);
  renderConversations(conversations);
  renderSocial(social, uSave);
  renderRumors(rumors, uSave);
  renderPlayerRole(playerRole, uSave);
  renderBudgets(budgets, uSave);
  renderLlmBudget(llmBudget);
  if (!p2Poll && p2status && (p2status.ok !== undefined)) renderP2(p2status);
  if (selectedNpcKey) showNpc(selectedNpcKey).catch(() => {});
  if (!catalogCache) catalogCache = await getJson("/api/player2/catalog");
  if (!capabilityCache) capabilityCache = await getJson("/api/player2/capabilities");

  document.getElementById("bridgeState").textContent = health.ok ? "online" : "offline";
  document.getElementById("bridgeState").className = statusClass(health.ok);
  document.getElementById("player2State").textContent = health.player2?.ok
    ? `${health.player2.client_version || "online"} / ${(health.player2.models || []).join(", ") || "no models"}`
    : "offline";
  document.getElementById("player2State").className = statusClass(Boolean(health.player2?.ok));

  renderTelemetry(telemetry);
  renderCapabilities(capabilityCache);
  renderCatalog(catalogCache);
  setJson("stateJson", {
    health,
    telemetry,
    catalog_documents: catalogCache.documents,
    capability_counts: capabilityCache.counts,
  });
}

async function runProbes() {
  const btn = document.getElementById("probeBtn");
  btn.disabled = true;
  btn.textContent = "Running...";
  try {
    await getJson("/api/player2/probes", { method: "POST" });
    await refresh();
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Player2 probes";
  }
}

async function showDetail(kind, id) {
  const path = {
    request: `/api/telemetry/request/${encodeURIComponent(id)}`,
    event: `/api/telemetry/event/${encodeURIComponent(id)}`,
    probe: `/api/telemetry/probe/${encodeURIComponent(id)}`,
  }[kind];
  if (!path) return;
  setJson("detailJson", await getJson(path));
}

document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("probeBtn").addEventListener("click", runProbes);
document.getElementById("memSelftestBtn").addEventListener("click", runMemorySelftest);
document.getElementById("evtSimBtn").addEventListener("click", () => evtAction("/api/events/simulate?npcs=500&events=1", "evtSimBtn"));
document.getElementById("evtFlushBtn").addEventListener("click", () => evtAction("/api/events/flush", "evtFlushBtn"));
document.getElementById("evtClearBtn").addEventListener("click", () => evtAction("/api/events/clear", "evtClearBtn"));
document.getElementById("seedUniverseBtn").addEventListener("click", () => evtAction("/api/universe/seed?save_id=" + encodeURIComponent(universeSave()), "seedUniverseBtn"));
document.getElementById("reviewBtn").addEventListener("click", async () => {
  const btn = document.getElementById("reviewBtn");
  btn.disabled = true; const prev = btn.textContent; btn.textContent = "Reviewing...";
  try {
    const r = await getJson("/api/strategic/review_all?save_id=" + encodeURIComponent(universeSave()));
    const decisions = (r.reviews || []).filter((x) => x.decision)
      .map((x) => `${x.faction_id} → ${x.decision.action}${x.decision.target ? " (" + x.decision.target + ")" : ""}`).join(" · ");
    btn.textContent = `cycle: ${r.decisions}/${r.factions} decided`;
    setJson("detailJson", r);
    await refresh();
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 5000);
  }
});

let stressPoll = null;
function renderStressStatus(s) {
  const el = document.getElementById("stressStatus");
  if (!el) return;
  if (s.running) {
    el.className = "warn";
    el.textContent = `stress running: ${s.params?.npcs} NPCs / ${s.params?.factions} factions — ${s.elapsed_s}s elapsed...`;
  } else if (s.result) {
    const r = s.result;
    const cls = r.ok ? "ok" : "bad";
    el.className = cls;
    const errs = (r.errors && r.errors.length) ? ` · errors: ${r.errors.length}` : "";
    el.textContent = r.ok
      ? `stress OK: ${r.n_npcs} NPCs, ${r.total_rows} rows, ${r.db_mb} MB, ${r.elapsed_s}s (max raw turns/NPC ${r.max_raw_turns_per_npc})${errs}`
      : `stress FAILED: ${r.error || JSON.stringify(r.errors)}`;
  } else {
    el.className = "dim";
    el.textContent = "";
  }
}
async function pollStress() {
  try {
    const s = await getJson("/api/universe/stress_status");
    renderStressStatus(s);
    if (!s.running) { clearInterval(stressPoll); stressPoll = null; document.getElementById("stressBtn").disabled = false; document.getElementById("stressBtn").textContent = "Stress 10k"; }
  } catch (e) { /* keep polling */ }
}
document.getElementById("stressBtn").addEventListener("click", async () => {
  const btn = document.getElementById("stressBtn");
  btn.disabled = true; btn.textContent = "Starting...";
  try {
    const r = await getJson("/api/universe/stress?npcs=10000&factions=100&turns=14");
    if (r.ok) {
      btn.textContent = "Running...";
      if (stressPoll) clearInterval(stressPoll);
      stressPoll = setInterval(pollStress, 2000);
      pollStress();
    } else {
      btn.disabled = false; btn.textContent = "Stress 10k";
      renderStressStatus({ running: false, result: { ok: false, error: r.error } });
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = "Stress 10k";
  }
});
document.getElementById("memResetAllBtn").addEventListener("click", () => {
  if (confirm("Reset the ENTIRE memory cache (all saves) and clear the event queue?")) {
    getJson("/api/memory/reset?all=1").then(() => { selectedSave = null; refresh(); });
  }
});
document.body.addEventListener("click", (event) => {
  const resetSave = event.target.closest("[data-resetsave]");
  if (resetSave) {
    event.stopPropagation();
    const sid = resetSave.dataset.resetsave;
    if (confirm(`Reset cache for save "${sid}"? This deletes its NPCs and memories.`)) {
      getJson(`/api/memory/reset?save_id=${encodeURIComponent(sid)}`).then(() => { if (selectedSave === sid) selectedSave = null; refresh(); });
    }
    return;
  }
  const saveChip = event.target.closest("[data-save]");
  if (saveChip) {
    const sid = saveChip.dataset.save;
    selectedSave = (selectedSave === sid) ? null : sid;
    refresh();
    return;
  }
  const npcRow = event.target.closest("[data-npc]");
  if (npcRow) {
    showNpc(npcRow.dataset.npc).catch((err) => {
      document.getElementById("npcFacts").textContent = String(err.stack || err);
    });
    return;
  }
  const btn = event.target.closest("[data-kind][data-id]");
  if (!btn) return;
  showDetail(btn.dataset.kind, btn.dataset.id).catch((err) => {
    document.getElementById("detailJson").textContent = String(err.stack || err);
  });
});

refresh().catch((err) => {
  document.getElementById("stateJson").textContent = String(err.stack || err);
});
setInterval(() => refresh().catch(() => {}), 3000);
pollStress();  // surface any in-progress or last-completed stress run on load

// ---- Grounded single-NPC immersion demo ----
function renderGrounded(s) {
  const st = document.getElementById("groundedStatus");
  if (!st) return;
  if (s.running) {
    st.className = "warn";
    st.textContent = `running: turn ${s.turn}/${s.total} · ${s.elapsed_s}s — real prompts to Player2 with full context injected`;
  } else if (s.result && s.result.ok) {
    st.className = "ok";
    st.textContent = `done — ${s.result.npc}, ${(s.result.transcript || []).length} turns`;
  } else if (s.result) {
    st.className = "bad";
    st.textContent = `failed: ${s.result.error || ""}`;
  } else {
    st.className = "dim";
    st.textContent = "idle — runs one richly-remembered NPC through a 5-turn conversation with the full situation briefing injected.";
  }
  const r = s.result;
  if (r && r.ok) {
    document.getElementById("groundedBriefing").textContent = r.briefing || "—";
    document.getElementById("groundedTranscript").innerHTML = (r.transcript || []).map((t) => `
      <div class="turn turnPlayer"><span class="turnRole">you</span> ${esc(t.player)}</div>
      <div class="turn turnNpc"><span class="turnRole">Voss</span> ${esc(t.reply)} <span class="dim">(${t.latency_ms} ms)</span></div>
    `).join("");
  }
}
let groundedPoll = null;
async function pollGrounded() {
  try {
    const s = await getJson("/api/grounded/status");
    renderGrounded(s);
    if (!s.running && groundedPoll) {
      clearInterval(groundedPoll); groundedPoll = null;
      const b = document.getElementById("groundedBtn"); b.disabled = false; b.textContent = "Run grounded conversation";
    }
  } catch (e) { /* keep polling */ }
}
document.getElementById("groundedBtn").addEventListener("click", async () => {
  const b = document.getElementById("groundedBtn"); b.disabled = true; b.textContent = "Running...";
  try {
    const r = await getJson("/api/grounded/run");
    if (r.ok) { if (groundedPoll) clearInterval(groundedPoll); groundedPoll = setInterval(pollGrounded, 1500); pollGrounded(); }
    else { b.disabled = false; b.textContent = "Run grounded conversation"; }
  } catch (e) { b.disabled = false; b.textContent = "Run grounded conversation"; }
});
pollGrounded();

document.getElementById("p2s1").addEventListener("click", () => startP2(1, 1));
document.getElementById("p2s10").addEventListener("click", () => startP2(10, 1));
document.getElementById("p2s20").addEventListener("click", () => startP2(20, 5));
document.getElementById("p2s100").addEventListener("click", () => startP2(100, 1));
document.getElementById("p2sClear").addEventListener("click", () => evtAction("/api/player2/stress_clear", "p2sClear"));
pollP2();  // surface any in-progress or last-completed Player2 pipeline run on load
