# Handoff — X4 Neural Link bridge (continue the AI Influence build)

You are taking over **x4_neural_link**: a local Python bridge that lets X4: Foundations mods talk to the **Player2** desktop AI app to drive "AI Influence" — factions/NPCs that remember, reason over game state, and propose validated, whitelisted actions. **Read `ROADMAP.md` first** — it is the source of truth (✅/◐/OPEN status + dated verification snapshots, newest at the relevant dated sections).

## Paths & runtime
- **Single working copy (edit + run here):** this folder — `…\x4_ai_influence\x4_neural_link`. It is now the only copy; there is no separate F:→G: staged/live split. The bridge runs in place from wherever this folder lives (all paths are derived at runtime — `server.py` uses `Path(__file__).resolve().parents[1]`; the launchers use `Split-Path -Parent $MyInvocation.MyCommand.Path`). You can move or rename the folder and it still works.
- Bridge: `http://127.0.0.1:8713` (dashboard at `/dashboard`). Player2 app: `http://127.0.0.1:4315` (NPC API + chat; live API docs at `/docs`).
- Player2 chat model is currently **gpt-oss-120b (free)**. Keep LLM use free; the event-queue resolver is **stubbed by default** (set `config event_resolver="llm"` to enable the real Strategic-AI LLM resolver for a demo).

## Dev workflow (important)
- **`Deploy-And-Restart.bat` is run + watch mode (no deploy anymore).** Run it once, leave the window open. It compile-gates `bridge/*.py`, runs the bridge **directly from this folder**, then **watches `bridge/` + `config/` and auto-reloads in place on every edit**. A compile error keeps the previous bridge alive. Just edit files here; they go live automatically.
- **`Start-Neural-Link.bat`** is the no-watch one-shot launcher (same folder-relative behavior, no auto-reload).
- **Dashboard** (`dashboard/*.html/js/css`) is served by the bridge from this folder — edit here; a browser refresh picks it up (no restart).

## Architecture (3-layer)
X4 mod (Lua/MD via `djfhe_http`) → **bridge** (`8713`) → Player2 (`4315`). Bridge modules:
- `contracts.py` — request/response envelopes + validation (channels incl. `npc`; `target.mode:"npc"` routes to the NPC path).
- `server.py` — stdlib HTTP routes.
- `router.py` — coordinator; owns `MemoryStore` + `EventQueue` + `Player2Client`; `_resolve_events` is the LLM resolver.
- `player2_client.py` — Player2 **NPC API** (spawn/chat/NDJSON-responses/kill) + raw chat fallback. `npc_complete()` is the main, memory-aware path.
- `memory.py` — `MemoryStore` (SQLite, **save_id-scoped**): `turns` (raw rolling), `facts` (condensed, categorized core/significant/routine, **CORE verbatim survival + decay**), `npcs` (binding + X4 stats), `factions`, `relationships`. Plus `run_memory_selftest`, `run_memory_stress`, reset/clear, save index.
- `events.py` — `EventQueue` (green-light batched flush: buffer events → flush a batch each interval via the resolver → single drain lane = backpressure).
- `telemetry.py` — request/event/probe logging for the dashboard.

## Done & verified (see ROADMAP snapshots)
- NPC API path: clean replies; memory continuity (NPC remembers across turns via durable `npc_id` binding).
- 4-stage memory: raw turns → condense into categorized `facts` → rolling gist → decay. CORE (death/war/love/oath/…) survive **verbatim**; routine forgotten. `/api/memory/selftest` = 13/13. 100-NPC stress: 8000 turns → 806 retained, DB 0.33 MB.
- NPC X4 stats: `piloting/management/engineering/boarding/morale` (0–15 = 0–5 stars) + role/race/ship; injected into prompts; shown on dashboard.
- Event queue: 500-event flood drains via batched resolution; importance-5 priority preempt.
- Cache: indexed by save file (`/api/memory/saves`); reset per-save or all (`/api/memory/reset?save_id=…` / `?all=1`).
- `factions` + `relationships` tables + methods exist in `memory.py` — **storage only, NOT yet exposed via endpoints/dashboard.**

## NEXT — the influence engine (the actual goal)
Storing data is the substrate. The AI acts through the **Bannerlord-proven Player2 action contract** captured on
2026-06-30 with the local proxy DB at
`F:\DEV_ENV\AiInfluenceBannerlord\player2_proxy\runtime\player2_proxy.sqlite3`.

Observed reference behavior:
- Player2 returned `actions:["relation:main_hero,change:negative"]`; the Bannerlord mod applied a relationship drop.
- Player2 returned `actions:["attack:main_hero"]`; the Bannerlord mod prepared the NPC attack.

The X4 rule is now:
1. **Deterministic substrate** gathers facts, legal options, pressure scores, resources, cooldowns, X4 object IDs, and
   proof state. Scoring is advisory context only.
2. **Player2 owns intent**: voice, personality, doctrine, preference, route choice, offer acceptance, hostility,
   requests, and proposed actions.
3. **Bridge normalizes/audits** the Player2 response as `{response|reply, actions:[{type, params, description?,
   needs_confirm?}]}`. Failed/unparsed decisions defer; never math-fallback to an action.
4. **Deterministic validator/X4 executor** applies only whitelisted, legal, bounded effects, then records proof.

The scheduled strategic review should therefore be: substrate/advisory -> Player2 `decide(...)`/action proposal ->
validator -> incident/action dispatch -> X4 proof. Cadence remains slow (~10-60s hot, minutes broad) and never per tick.

**Build order:**
1. Reconcile old deterministic influence paths: route autonomous influence through the universal `decide(...)` adapter
   and remove index-0/math fallback as action authority.
2. Normalize the bridge action contract: every Player2 action becomes an object action with `type`, `params`,
   optional `description`, and optional `needs_confirm`.
3. Expand the whitelist in proof order: safe status/logbook/memory -> `relation_delta_limited` ->
   `threaten/attack_intent` -> `mission_offer` -> `trade_request` -> `temporary_diplomatic_flag` ->
   `faction_to_faction_proposal`.
4. Keep `strategic_state`, scoring, OPORD wargames, offer scoring, and economy/fleet reads as deterministic advisory
   and validator inputs, not final decision authority.
5. For each action type: add parser, audit row, validator, X4 dispatcher handler, dashboard visibility, selftest, and
   in-game proof before marking ✅.

## Gotchas (will save you hours)
- **Sandbox mount truncation:** freshly host-edited files can appear truncated in a sandbox view and break `cp`/`py_compile`. The **host file tools (Read/Edit/Write) are authoritative**; deploy host-side (the watcher's PowerShell robocopy reads complete host files). Trust the watcher's compile-gate, not a sandbox compile of a just-edited file.
- **Player2 raw chat quirk:** `/v1/chat/completions` with small `max_tokens` returns empty content (reasoning models burn the budget) — model-agnostic, Player2-side. Use the **NPC API** (clean replies); the bridge floors `max_tokens` + retries.
- **Joules/free:** keep the resolver stubbed during DB work; only call the LLM on the free model for explicit demos.
- **CORE facts are unbounded by design** (verbatim-forever) — a known failure mode at extreme longevity; eventually cap/merge old CORE into the gist. Not urgent.
- **Save scoping:** everything is keyed by `save_id`; new tables must be too, and must be covered by `reset_all` + `clear_save`.
- **X4-side mod doesn't exist yet** — this is the bridge only. The mod will POST events and poll `incidents`. Pin down which reads come from SirNukes Mod Support APIs / MD properties vs. are inferred by the bridge before writing ingest contracts.

## Verification discipline
Prove every change **live**: hit the endpoints + watch the dashboard. Use `/api/memory/selftest`, `/api/memory/stresstest?npcs=&turns=`, `/api/events/simulate?npcs=&events=` + `/api/events/state`. After each unit of work, update `ROADMAP.md` with ✅/◐ status + a dated snapshot (commands + results + any gotcha discovered).

## Reference docs
- `ROADMAP.md` (this repo) — source of truth.
- `Desktop/X4_AI_Influence_Blueprint2.md` — the full product spec (memory §13, actions §10, tiers §11, factions §12).
- `Desktop/Bringing Bannerlord Style AI Influence into X4 Foundations.md` — the influence-engine architecture (scoring core, decision contract, event→action mapping).
