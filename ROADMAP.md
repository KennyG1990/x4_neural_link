# X4 Neural Link + AI Influence Roadmap

## ★★★ EPIC I — SYNTHETIC PERSISTENT NPC IDENTITY LAYER (spec'd 2026-06-28, Ken) — Neural Link becomes the identity authority

**Why:** the NPC-identity investigation (#99/#102) PROVED X4 exposes no stable cross-reload identity for generic
crew — runtime `raw` and save `<component id>` are the same volatile UniverseID in hex (Manda: 134465819→236456014
across one reload), idcode empty. **Decision (Ken): do NOT downgrade richness — make the MOD the identity authority.**
X4 handles become evidence/session-routing only; NPCs are re-identified each session by deterministic evidence
scoring. Full design: **[[../../../StarForge/wiki/x4-neural-link/npc-identity-layer-spec]]** (`F:\StarForge\wiki\x4-neural-link\npc-identity-layer-spec.md`).

**RECONCILE (already exists → extend, don't rebuild):** `npcs` table already holds name/faction/role/race/gender/
ship_class/ship_name/sector/skills/stats/summary + unused `bound_entity_id`; `facts`(tier/importance), `turns`,
`relationships`(social #39/#76), persona/archetype (#37), RoleRAG (#16/31-33), census scaffold (#98, dormant). The
NEW work: a **handle-independent `persistent_npc_key`** (current `make_key=save_id|game_id|persona` WRONGLY embeds
save_id), an evidence table + scoring/rebind, importance tiers + promotion, confidence-gated dialogue, the identity
dashboard, player soft-confirm. **Keystone/risk:** re-keying facts/turns/relationships off the save_id-embedded key
(reversible, selftested migration). **Anti-cheat:** observe + memory only; no world mutation, no resources.

**Phases (status: spec'd):**
- **I0** — schema + handle-independent key + resolution layer [bridge]. **✅ DONE 2026-06-28.** Added
  `npc_identities` + `npc_identity_evidence` + `npc_runtime_bindings` + `npcs.persistent_key`;
  `derive_persistent_key` (handle-independent — excludes runtime/save id), `upsert_identity`/`set_identity_fields`
  (I2/I3 lifecycle writer), `record_evidence`, `bind_runtime`/`expire_session_bindings` (reload flow),
  `resolve_memory_keys` (cross-reload memory UNION — facts/turns NOT re-keyed → reversible), idempotent
  `backfill_identities`. Endpoints `/api/identity/{selftest,backfill}`, `/api/identities`, `/api/identity`.
  **Validated:** bridge `identity/selftest` **13/13** live; backfill on live DB = 19 npc rows → 13 identities
  (6 collapsed = dedup/union); detail returns evidence + memory keys. Pure backend, in-game gate N/A.
- **I1** — in-game evidence capture (conversation NPC) [MD/Lua + bridge]. **✅ DONE 2026-06-28, in-game verified
  HANDS-FREE.** GROUNDED in-game: runtime-readable person fields = **macro**
  (`character_argon_female_asi_crew_01_macro`) + sector + skills + name + owner; NOT readable = idcode/code/class/
  commander (unique code stays save-only → binding is evidence-scored, as the spec premised). FULL CHAIN: aic_uix.lua
  reads macro/sector (event-order-independent direct read at fold) → folds into `context` → aic_menu sends as
  `prompt_vars` → **router promotes `pv.macro/sector/runtime_component_id` to first-class `target` fields** (the
  missing link — the chat builder cherry-picks pv→target; macro wasn't in the list) → `npc_complete` rebinds each
  exchange → confident bind + Tier-1 promotion (promote AFTER link). **VALIDATED (all 3):** selftests green;
  dashboard shows Manda `bound conf 0.9` evidence `name,faction,macro,skill_vector,role,sector`; **in-game: a real
  chat auto-wrote a fresh binding with the live runtime id `303620034`, no manual step.** ◐ deferred: `container`
  (readable but a volatile handle, low value). Station-NPC census accessor NOT needed here (that's I6/#98).
  NOTE: a diagnostic `AIChat.open folded identity evidence` log line remains in aic_uix.lua (harmless; remove on next pass).
- **I2** — scoring + `rebind_session` engine [bridge]. **✅ DONE 2026-06-28.** `score_identity` (spec weights:
  name/faction/role/macro/npc_code/skill_vector/container/sector/recently-talked/same-session-id; penalties:
  name+diff-faction −0.40, name+diff-role+macro −0.25; faction normalized via resolve_faction_id) +
  `rebind_session` (≥0.80 bound · ≥0.60 tentative · ≥0.40/near-tie ambiguous→fresh `:amb` key, never merges ·
  else new; links session npc_key→identity, records evidence, writes runtime binding). Endpoints
  `/api/identity/rebind_selftest`, `/api/identity/rebind`. **Validated:** rebind selftest **7/7** live — incl. the
  keystone *reload rebinds + memory unions across the reload*, dup-name/diff-faction not merged, near-tie→ambiguous,
  brand-new→new. Pure backend, in-game gate N/A. **◐ deferred:** spec "long time gap" penalty (−0.10..−0.30) —
  needs a game-time model; logged, not built.
- **I3** — importance tiers (0–3) + promotion rules [bridge]. **✅ DONE 2026-06-28.** `importance_tier` lifecycle
  on `npc_identities` (0 abstraction · 1 player-significant · 2 local · 3 background); `promote_identity`
  (idempotent, never demotes) + `promote_identity_for_npc` (npc_key→identity bridge); `record_turn` hook promotes
  to Tier 1 on any conversation (covers talk/mission/negotiate + the 2+-memories case); `PROMOTION_TIER` maps all
  spec triggers; endpoints `/api/identity/promotion_selftest`, `/api/identity/promote`. **Validated:** promotion
  selftest **7/7** live (talk→1, never-demote, event→2, abstraction→0, unknown/unlinked no-op). Pure backend,
  in-game gate N/A. **◐ deferred:** wiring promote calls into non-conversation event sources (news/social/
  relationship/assignment handlers) — API ready, conversation path live.
- **I4** — confidence-gated dialogue + RoleRAG layering [bridge]. **◐ logic DONE + selftest-verified; in-game
  dialogue confirmation pending.** Added `identity_recall_gate(npc_key)` + rewrote `build_memory_context` to gate
  PERSONAL recall by bind status: **bound** → full recall UNIONED across the identity's keys (resolve_memory_keys —
  finally consumed, the I8-deferred wiring); **tentative** → recall but HEDGED ("you half-recognize…"); **ambiguous**
  → suppress personal history (faction/role only, never assert shared past). Non-chat/unbound NPCs → default full
  recall (no regression). **Validated:** new recall selftest **6/6** (bound-unions-both-keys, tentative-hedges,
  ambiguous-suppresses, unbound-default); I0 13/13, I2 7/7, I3 7/7, core memory selftest green (fixed a STALE A4
  assertion `no_auto_condensation` that had been silently red since A4's record_turn promotion). **REMAINING (in-game
  gate):** see it in dialogue — talk to a bound NPC (recalls you) vs an ambiguous one (stays neutral). The RoleRAG
  boundary layer already injects faction/role context on every call (SPEC 1e); I4 adds the personal-memory gating on top.
- **I5** — dashboard identity panel + "why bound?" evidence [dashboard]. **✅ DONE 2026-06-28.** `npcIdentity`
  section in `showNpc` fed by enriched `/api/identity` (identity + evidence + memory_keys + bindings +
  name_collisions). Shows status (color-coded), tier+label, confidence, persistent key, runtime id, memory-link
  count (cross-reload), evidence count, **last seen**, conditional collision warning, and the **"why bound?"**
  evidence breakdown. **Validated (Chrome, live):** Manda → "SESSION-ONLY · TIER 3 background · CONF 100% · key
  pid:40f717bb500f · 1 memory link · evidence 4 · last seen 18m ago · why bound? faction/name/role/skill_vector".
  Dashboard observer surface → Chrome render is its bar; in-game gate N/A.
- **I6** — throttled census priority order [extends #98, in-game gated].
- **I7** — player soft-confirmation path (guarded, anti-abuse) [bridge; in-game gated].
- **I8** — fix second-layer misses from the I1 wiring [bridge]. **✅ DONE 2026-06-28.** DEFECT fixed: the rebind in
  `npc_complete` fired for ALL callers (reactions/news/influence), polluting identities (Galaxy News Desk ×5, High
  Command dups) — now gated to `game_id=='chat'` (real player conversations only). CLEANUP: chat-only backfill +
  `reset_identities()` + `/api/identity/reset` → 22 junk identities cleared, rebuilt to 3 real chat NPCs, zero dups.
  Validated: I0 13/13 (selftest updated for chat-only backfill), I2 7/7, I3 7/7; dashboard identities clean. The
  `resolve_memory_keys` union (earlier flagged) is DEFERRED to I4 — low-value now (npc_key stable per playthrough →
  memory already persists), real consumer is I4's confidence-gated retrieval.
- **Build order:** I0 → I2 → I3 → I5 (verifiable now) → I1 → **I8 (cleanup)** → I4 → I6 → I7 (need in-game accessor / player surface).

## ★ CONVERSATION UX — choice-driven dialogue (Ken, 2026-06-28)
- **Chat wheel: instant presets + conversation-aware suggestions [#112]. ◐ code-done + Forge-validated; in-game pending.**
  First wheel shows instant presets (no '(thinking)' placeholder); LLM suggestions follow the conversation
  (generate_suggestions reads recent turns) and refresh each open. Validate in-game: /refreshmd → open wheel.
- **Conversation flow: choice-driven loop, don't force the text box [#113]. SPEC'D (not built).** Picking a
  suggested option currently forces the edit-box (Open_chat → custom window focused on typing). Target = ME-style:
  pick choice → NPC replies → new contextual choices → pick; text box ONLY via "Type my own message"; "Goodbye"
  ends. Design B (chosen): render suggestions as CLICKABLE BUTTONS in the existing aic_menu window, stop
  auto-focusing the edit-box — reuses the window (async reply display) + #112's conversation-aware suggestions.
  Full design: [[../../../StarForge/wiki/x4-neural-link/conversation-flow-spec]].
- **Bugfix: chat window not isolated per NPC [#114]. ◐ FIXED; in-game pending.** The window transcript
  (`menu.history`) was never reset between conversations → showed every NPC's turns, relabeled to the current NPC
  (bridge memory was fine — isolated by npc_key). Fix: reset `termMenu.history` on NPC change (onOpenCommLink).
  Validate: /reloadui → talk to two NPCs → each window shows only its own turns.


**Status:** backend ✅ · **conversation → real gamestate change LIVE + verified in-game ✅** (declare war in
chat → X4 relation flips → factions fight) · world-model sync ✅ (relations + Tier-1 conflicts/events/
agreements) · **readers all live-verified ✅** (skills, sectors, economy, fleet census, war-losses,
contested-sectors→territorial/piracy) · **Tier-3 strategic deriver ✅** (every faction's pressures + dynamic
mood, emergent on the heartbeat) · **MEMORY ENGINE substrate ✅** (the game's own logbook ingested as
classified world-event memories [SPEC 1c-B] + each faction's named representative/'rememberer' [SPEC 1c-C] +
clean name-keyed sectors [SPEC 0b]) · **ACTUATION ✅** (autonomous decisions flip REAL X4 relations [1d-W2]) ·
**PLAYER-FACING VOICE ✅ verified in-game** (factions transmit prominent grounded communiqués to the player —
[SPEC 1j], blueprint §5.6) · **REAL MILITARY ORDERS ✅ in-game** (war phases order a faction's OWN ships to
patrol/raid — no spawning; #49–#53) · **ANTI-CHEAT ✅ ~95%** (words≠resources: no decision-triggered ware/money/
loss writes; `warphase_actuate_selftest` 10/10) · **EVENT-GROUNDED CONFLICT LEDGER ✅ keystone bridge** (#62:
`hostile_events`→derived located conflicts; 7/7) · **Phase:** feed the conflict ledger from REAL in-game combat
(#66) → raids prove themselves (#67); live economy read (#54-56); diplomacy validators (#57-58); player contracts
(#59-60). · **NEXT (2026-06-27): audit remediation A1–A7** (see "★ AUDIT REMEDIATION" below — panels/reaper/
roles/facts/docs/joules; spec'd, not started). · **Updated:** 2026-06-27

This session (2026-06-25): war-losses (fleet-delta), Tier-3 deriver, contested-sector reader (territorial +
piracy), SPEC 0b (sector dedup), SPEC 1c-B (logbook→memory), SPEC 1c-C (faction reps). All verified in-game;
outcomes recorded in StarForge canon `wiki/x4-neural-link/outcomes.md`. Next spec: 1c-D below.

### ✅ SESSION 2026-06-26 — what shipped (full detail in the per-SPEC sections below)
A long build session. In order:
1. **Forge debug-log watcher** (Forge ROADMAP) — cue-liveness, real mod log-marker detection (`[AICHAT][UIX]`),
   runtime-error attribution by marker proximity, benign unsigned-file noise excluded. Shows RED for real faults.
2. **cdata reader bug** (#25) — `GetComponentData(got cdata)`: SyncSectors fixed + VERIFIED in-game (frequent
   errors stopped); SyncFleets residual fixed (confirms next reload).
3. **SPEC 1j — PLAYER-FACING VOICE** ✅ verified in-game — factions transmit prominent grounded communiqués TO
   the player (Alerts/Diplomacy logbook), triggers = near-player / grudge / major shift.
4. **SPEC 1k — RoleRAG boundary-aware retrieval** ✅ + **ware catalog harvest** (1397 wares from the game's own
   `libraries/wares.xml`) + **zero-friction boot canon** (`ensure_canon` auto-builds factions+wares on bridge
   load; game path derived from the bridge's own location — works on any install). NPCs reject off-universe
   factions/wares (closed-set, grounded in the encyclopedia data).
5. **SPEC 1l — diplomatic bulletin quality** ✅ — name hygiene, titled spokesperson, dedup, reason-gating,
   template families, `[TEST]` dropped.
6. **SPEC 2a — PersonaCard + authority model** ✅ acceptance-test passed (bridge API; in-game-UI confirm
   pending) — every player↔NPC turn gets a situated role card so NPCs RP within their authority. THREE passes:
   (a) build + Codex acceptance test; (b) 7-field audit → added `wants` (motivation) + `conversation_consequence`
   (routing); (c) Codex review #2 → finer `ARCH_SPECIALIZATIONS` (specific postings), proximity-ranked concerns
   (local sector first), `ARCH_REDIRECT` (concrete office), physical-beat default-on. Plus the card is now
   SURFACED on the dashboard NPC sheet (`renderPersonaCard`). New files: `bridge/persona.py`. Selftest all-pass.
7. **Map-won't-open check (Ken 2026-06-26):** investigated the debuglog via the watcher → `status: clean`, 0
   mod runtime errors, NO UI/menu/map/Lua-load errors from us; only benign heartbeat lines + a pcall-guarded
   `Component 0 does not exist any more` (despawned object mid-read). Our mod is NOT the cause — likely a vanilla
   UI state issue (F9 quickload clears it). (Optional polish: skip `Component 0` reads to quiet that benign line.)
**Verification honesty:** everything verified via Forge diagnostics + DB/bridge endpoints; SPEC 1j + the cdata
sectors fix were also confirmed IN-GAME (logbook/debuglog). SPEC 1l + 2a are bridge-verified — their in-game CHAT
/ logbook surface uses unchanged, previously-proven plumbing, but the on-screen render wasn't re-driven this
session (low risk, not zero). **NEXT SESSION START HERE → SPEC 2b (Narrator), then 2c (NPC↔NPC relationships) —
both fully scoped under "★★★ SPEC 2" below.**

### ✅ SESSION 2026-06-26 (CONTINUED) — what else shipped (detail in the dated sections below)
Continuing from item 7, in order (all bridge-verified; in-game-proven items noted):
8. **SPEC 2b — Narrator layer** ✅ (world-history articles, cause-gated, evidence-led, spam-guarded).
9. **SPEC 3 — event priority hierarchy** ✅ (gates+tiers, 9/9; suppresses no-op spam — verified live) + **3.2
   war-state phases** ✅ (dead escalate → varied war moves).
10. **SPEC 1k-fix — local assignment facts > refusal guard** ✅ (Codex "Vigilant" bug: NPC's own ship/sector are
    hard local facts the RoleRAG guard can't reject; verified live).
11. **KEYSTONE delivery fix** ✅ in-game — influence_step was too slow (LLM) for the mod's HTTP timeout, so
    news/articles/actions never arrived. Decoupled generation (background daemon) from delivery (fast
    `GET /v1/influence_drain`). This unblocked ALL surfacing.
12. **IMMERSION** ✅ — `_humanize_math` (convert war-scores/intensity %s → English in player text, pooled variety)
    + `_qualify_prose` (deny the news-desk LLM raw numbers in its grounding so it phrases in its own voice).
13. **SPEC 3.3 order primitive (#49–#53)** ✅ PROVEN IN-GAME — real ship orders over OWNED ships (no spawning):
    DeadAir `find_ship_by_true_owner` + `create_order`; mobilize_fleet → real patrol order, raid_supply_line →
    real Attack order (debuglog: `[AIINF] order patrol/raid … ship=<real ship>`).
14. **Economy read pipeline foundation (#46)** ✅ (raw `economy_stations` + rollup → faction shortages; 5/5).
15. **ANTI-CHEAT arc (#44/#64) — Ken's "words≠resources"** ✅ verified — removed ALL decision-triggered ware/
    money writes (no `type:economy` emitters bridge-wide) AND the DB-causality fabrication (`record_loss`/
    `_econ_delta`/intensity off a decision); war phases now emit ONLY real orders+relations; `warphase_actuate_
    selftest` 10/10; guarded the dormant MD economy branch (`$act.$earned=='true'`). Anti-cheat ~95% closed.
16. **EVENT-GROUNDED CONFLICT LEDGER keystone (#62)** ✅ 7/7 — `hostile_events` table + `derive_conflicts_from_
    events` (intensity rolling from real magnitude, cause=first real event, located sectors, attributed losses) —
    replaces relation-derived "intensity 100 / relations at war".
**Open task map (granular, closeable):** keystone chain #62✅→#66 (in-game hostile-event capture)→#67 (order_id
linkage; raid proves itself); economy read #54-56; diplomacy validators #57-58; player contracts #59-60 + earned
economy #63; anti-cheat #65 (ForceWar gating); Forge-ship faithfulness #61. **NEXT: #66** (in-game combat-event
capture feeds the #62 ledger with truth).

### ★ AUDIT REMEDIATION (2026-06-27) — scoped, NOT started (from `gap-audit-2026-06-27.md`)
Gap audit (analysis-only) diffed built+surfaced vs Blueprint/Gameplay/Codex-advice-1&2. **Headline: the
architecture matches the spec — RoleRAG scope-gate, PersonaCard authority, Narrator, priority gates, war-phase
actions, earned-economy anti-cheat are all built + green (14/14 new-feature selftests pass).** The real gaps are
**visibility + memory depth**, not foundations. Items below are spec'd; each closes with the workflow's named
validation (Forge diag where relevant · `:8713` selftest/endpoint + dashboard render · in-game where applicable).

- **A1 — Dashboard panels for the endpoint-only feature families [IG-1, HIGH, buildable-now].** player-role
  (`/v1/player/role`), social graph + romance (`/v1/social/list`), rumors (`/v1/rumor/list`), faction budget
  (`/v1/economy/budget_status`), memory-audit candidates (`/v1/memory/audit`), offers/contracts
  (`/v1/offers/*`), war-phase actuation, gameplay-tick. (Persona already surfaced via `renderPersonaCard` — skip.)
  All tracked via endpoints, ZERO panels → the dashboard (blueprint's proof surface) is blind to everything built
  since the economy panel. **Validate:** each panel renders live rows (Chrome) against its `/api/*|/v1/*` source.
    - **A1a ✅ DONE+VERIFIED 2026-06-27 (browser/live):** 3 panels added — NPC Social Graph (`/v1/social/list`),
      Rumors (`/v1/rumor/list`), Player Role (`/v1/player/role`) — in `dashboard/index.html` + `dashboard/app.js`
      (render fns + `post()` helper wired into `refresh()`, save-scoped). Verified on `game_301276512`: Player
      Role renders REAL data (primary_role "faction threat", threats alliance/argon, high-dependency alliance);
      Social + Rumors render correct empty-state (no in-game social events captured yet — gated on #39 population,
      NOT a panel defect). Rest of dashboard unaffected (app.js parses). NOTE: social/rumor panels stay empty until
      in-game social events feed them — expected, not broken.
    - **A1b ✅ DONE+VERIFIED 2026-06-27 (browser/live).** Reconcile reshaped it: agreements ALREADY surfaced
      (`renderAgreements`/"Agreements / Deals") → dropped; offers are generators (not persisted) → deferred
      (surface when offers become a stored contract, ties M5). DELIVERED: Faction Budgets panel
      (`dashboard/index.html`+`app.js` `renderBudgets`) + new `router.budget_list` + `/v1/economy/budget_list`
      (iterates economy factions → derived capacity vs spent; surfaces the #63 anti-cheat substrate). VERIFIED on
      `game_301276512`: endpoint 200 with 12 factions (teladi 21.2M, paranid 14.5M, argon 7.7M … ministry 250K,
      spent=0); panel renders 12 money-formatted rows. (Gotcha: a new server.py route 404'd until the file was
      re-saved once — rapid successive .py edits coalesced in the watcher; re-save re-triggers the restart.)
    - **A1c ◐ TODO:** memory-audit candidates (`/v1/memory/audit`), war-phase actuation, gameplay-tick.
- **A2 — Selftest-save reaper + selftest teardown [IG-3, MED, cheap]. ✅ DONE+VERIFIED 2026-06-27 (browser/live).**
  RESULT: `memory.reap_selftest_saves()` + dynamic `clear_save` (deletes from every `save_id`-scoped table) +
  ONE dispatch hook in `server.py` (after any POST `*selftest*` route, reap) — covers all 14 selftests + future
  ones with no per-method edits. VERIFIED live: `/v1/memory/reap_selftests` reaped 24 saves (4 npc-visible + 20
  substrate-only — proves the cross-table sweep); `/api/memory/saves` selftest 4→0, all 9 real saves kept
  (cctest/grounded/game_* untouched); `rumor/selftest` 5/5 + `social/selftest` 10/10 still green AND now leave 0
  rows; NPC metric 85→75. Files: `memory.py`, `router.py`, `server.py`. (Boundary: GET-style selftests not hooked
  — weren't polluting.)
  `__*_selftest__*` saves (14 patterns: rumor/social/social_brief/promote/mem_audit/player_role/patrol_offer/
  earned_validate/agreements/hostile_ledger/warphase/econ_rollup/supply_offer/gameplay_tick) persist in the live
  DB and inflate counts (85 NPCs shown vs 23 in the real `game_301276512`). (`cctest`/`octest` are legacy MANUAL
  saves — NOT auto-reaped.) **Design refined during reconcile:** (a) `dry_run` doesn't fit write→read selftests
  (they must write rows to test reads) → use **teardown** (`clear_save(save)` at end) for the row-creating ones
  instead; (b) reuse the existing `memory.clear_save(save_id)` (don't rebuild); (c) **fix `clear_save` to be
  dynamic** — delete from EVERY table that has a `save_id` column, killing the recurring "newer tables left
  behind" bug (it currently misses `faction_budget`/`social_relations`/`rumors`). Add `memory.reap_selftest_saves()`
  (sweep `%selftest%` save_ids → `clear_save` each) + router handler + `/v1/memory/reap_selftests` route.
  **Validate:** `/api/memory/saves` shows only real saves post-reap; selftests still green AND leave no rows.
- **A3 — Surface classified persona role + fix NPC↔entity binding [IG-4, MED]. ◐ A3a ✅ DONE+VERIFIED 2026-06-27
  (role surfacing); A3b (real entity binding) GATED on in-game Lua + X4.** A3a result: `router.memory_npcs` now
  fills BLANK roles via `persona.classify_archetype` (maps the row's `name`→`npc_name` the classifier expects);
  verified live — 0 blank roles, all "X High Command" → `high_command`, real roles (marine/service crew) preserved,
  News Desk → civilian (benign). Ids still `(unbound)` → that's A3b (capture the component id in Lua; details below).
  Roles render "—" and ids "(unbound)" though `persona.classify_archetype` exists and blueprint §13 has
  `bound_entity_id`/`npc_id`. **Root cause GROUNDED (2026-06-27):** real embodied NPCs (e.g. Rina Bekker/marine,
  Rylan Dehaan/service crew, save tag `/ chat`) are unbound NOT because the game lacks ids — the NPC component is
  already delivered to Lua at conversation start (`aic_uix.lua` `AIChat.npc_skills` ~L596-606 does
  `ConvertStringToLuaID(tostring(component))` to read skills) — but the component is **discarded**; the chat/memory
  request keys the NPC by NAME (`npc_name`/`target_name`, L144/158), not the component id. (The two id concepts:
  `bound_entity_id` = in-game component; `npc_id` = Player2 spawn handle, which is what the column currently
  renders.) **Fix (3 steps):** (1) Lua — capture the component's stable 64-bit id at conversation start, include it
  in suggest/index/chat payloads; (2) bridge — persist as `bound_entity_id`, key NPC memory by it (name = display
  only), backfill unambiguous name-keyed rows; (3) dashboard — render the bound id. **Caveats:** person `idcode`
  may be empty (use the component id); **MUST verify the id survives save/reload** before trusting it as the
  persistent memory key; handle despawn/death via the existing `npc/delete` path (`router.py` ~L232). Leave the
  synthetic `High Command`/`Galaxy News Desk` rows unbound BY DESIGN (abstract faction voices; ideally tag them
  "abstract" so only real NPCs are expected to bind). **Enables:** A4 (facts stick to a real person), #39 (real
  NPC↔NPC edges), M5 (targeted hail), M9 (succession). **Validate:** NPC sheet shows real roles + bound ids; the
  same NPC is recognized across two separate conversations after a reload (Chrome + in-game).
    - **A3b ◐ RECONCILE FINDING 2026-06-27 — plan likely UNSOUND, probe deployed.** The "capture the component
      UniverseID → persist as the binding key" plan probably fails the actual goal (recognize the SAME NPC across
      save/reload): X4 component UniverseIDs are RUNTIME handles, not save-persistent — relations-sync re-reads them
      every tick precisely because they don't persist. A stored key on the id would change on reload. Did NOT build
      the full chain on that assumption. **Probe deployed:** `aic_uix.lua ReadNpcSkills` now logs `A3b npc_id probe
      =>`; next = in-game chat → save/reload → chat again, compare the logged id (needs a UI reload to take effect).
      If unstable (expected), re-scope the key to a STABLE identifier (person idcode if it exists, else a composite
      name+faction+ship+role) or accept session-only binding. Resolve BEFORE building the capture→persist chain.
      **SPEC — Stable NPC Identity Binding (Codex+Claude, 2026-06-27) — EVIDENCE-FIRST, phased.** Core principle:
      TWO identifiers, never one — `runtime_component_id` (who am I talking to now) vs `persistent_npc_key` (same
      person across reloads). **Phase 0 (NOW):** extend the probe to log `idcode`+candidate fields; in-game chat →
      FULL save+exit+reload → chat again → compare. Answers Q1 (stable idcode?) + Q2 (runtime id survives?).
      **Then pick:** Path A (stable idcode) → key memory on idcode, write `bound_entity_id`, opportunistic
      name-key migration, dashboard column — MINIMAL, no resolver. Path B (no idcode, runtime id survives reload)
      → key on runtime id behind the persistent-key abstraction. Path C (neither) → session-binding=runtime id +
      best-effort composite (name+faction+role+assignment), confidence-marked, dashboard shows "imperfect".
      **DEFER to phase 2 (only if collisions observed):** `npc_runtime_bindings` table, merge/split/rebind
      endpoints+UI, full collision workflow, NPC social/romance on top. **Rules:** don't assume UniverseID
      persists; don't auto-merge same-name; don't delete old name-key memory on migrate. Build order: probe →
      idcode investigation → choose path → schema → payload fields → write bound id → dashboard → safe migration.
      **PHASE 0 RESULT — IN-GAME VERIFIED 2026-06-27 (Manda Smitt, full save→menu→load):** `raw=458069` →
      `raw=2059935` (CHANGED); `idcode=` empty both times; `name`+`owner`(faction) present. ⇒ **Q1: NO persistent
      person idcode (Path A out). Q2: runtime UniverseID CHANGES on reload (Path B out). → PATH C.** HONEST
      CEILING: X4 exposes no reliable persistent per-crew id, and a composite (name+faction+role+ship/sector) is
      MORE unique but LESS stable (role/ship/sector change on transfer) — so true per-individual cross-session
      identity for generic crew is NOT achievable; name+faction is only a marginal collision-reduction over
      name-only. RELIABLE memory tier = FACTION-level (already works by name=faction). DECISION (pending Ken):
      minimal Path C — key memory by name+faction, confidence-mark it, dashboard shows "name-key (imperfect)" not
      "(unbound)", optionally session-bind the runtime id for in-session targeting; DEFER all heavy machinery
      (runtime_bindings table, merge/split UI, collision workflow) — it can't overcome the missing stable id.
      **LEAD (community tip via Ken, 2026-06-27): "characters have an id in the save XML; idk what MD can see."**
      → narrows the conclusion: a PERSISTENT character id may EXIST in save data; we only proved it absent via
      `idcode` + runtime UniverseID. **Path A REOPENED, conditional on a runtime accessor.** Investigation (Codex,
      offline): inspect `Documents/Egosoft/X4/<id>/save/*.xml.gz` for the character `id` + structure; then find
      which `GetComponentData(person,…)` key / MD person-property returns that SAME id at runtime AND is stable
      across reload. Found → Path A (stable key, minimal build). If the save id is just the runtime UniverseID
      serialized at save-time (would differ next save) → dead end, stay Path C.

### ★ NPC CENSUS / LIVE ROSTER — incremental tiered indexer (2026-06-27) — spec'd, NOT started
**Problem:** the NPC table is interaction-driven — an NPC enters the DB only when the player talks to it (chat
path) or as a bridge abstraction (High Command / News Desk). So the dashboard is a record of WHO-INTERACTED, not
a LIVE roster of the world. (Confirmed via reconcile: `AI_Influence.IndexNpcs` exists in Lua but NO MD cue feeds
it.) Does NOT fix cross-reload identity (proven impossible for generic crew); it operationalizes Path C.
**Build:** a throttled, tiered NPC census — reuse the PROVEN pattern of the economy round-robin indexer (#54) +
the fleet census (`GetContainedObjectsByOwner`) — that scans relevant NPCs in small chunks per tick and POSTs to
`/v1/npcs/index`, populating/refreshing the roster gradually WITHOUT freezing the game and WITHOUT dumping the
galaxy's thousands of generic crew.
**Tiers (priority-scoped, ties to SPEC-3 gates):** T0 always = abstract actors (High Command / reps, exist);
T1 each tick (cheap) = NPCs at the PLAYER's current location (talk-able now); T2 round-robin throttled =
important operational NPCs (station managers, ship captains, mission/named actors), a chunk per tick faction by
faction; T3 = generic long-tail crew → DO NOT pre-census, index lazily ON INTERACTION (current behavior).
**Payload (extend `/v1/npcs/index`):** runtime_component_id, name, faction, role, location(ship/station/sector),
owner, skills, seen_at, source="npc_indexer".
**Bridge ACTIVE ROSTER (the session-binding layer):** runtime_component_id → current-session NPC; composite key
(name+faction+role+assignment) → best-effort memory identity; refresh each tick (relations-sync pattern).
Persistent memory stays strongest at faction/station/named-role tier.
**Acceptance (HONEST):** SUCCESS = live roster + dashboard shows nearby NPCs without a chat + clean in-session
identity. NOT-success ≠ perfect permanent memory for every random crew member.
**Defer:** heavy identity machinery (runtime_bindings table beyond the in-memory roster, merge/split UI,
collision workflow) until collisions are observed.
**Build order:** confirm enumeration primitives (location NPCs + crew via GetContainedObjectsByOwner) → T0/T1
first (cheap, high value) → extend index payload → bridge roster + session binding → dashboard live roster →
T2 round-robin → tune throttle. **Validate per the IN-GAME GATE:** roster populates in-game WITHOUT interaction.
**◐ BUILT 2026-06-27 (in-game test PENDING):** Tier-2 first cut — `Census_npcs` library in `ai_influence_worldsync.xml`
finds `player.sector` stations → indexes each `.controlentity.knownname` + `.owner.knownname` via the EXISTING
`AIChat.index_npcs`→`/v1/npcs/index` path (one-file change, no bridge/Lua edit). Reconcile pivot: Tier-1 generic-crew
enumeration is NOT grounded (no person-enumeration in DeadAir/refs; needs the vanilla crew-menu primitive) → did
Tier-2 (controlentity, grounded — schema + conversation.xml). Forge `project/validate`: census MD schema-CLEAN
(only "error" = `missing_content_xml`, a single-file artifact; cross-file/lua warnings are single-file artifacts
too). `find_station groupname=` for `multiple` (DeadAir pattern). NEXT (in-game gate): `/refreshmd` → stand in a
sector with stations → confirm the dashboard NPC roster gains those station managers WITHOUT a chat.
      **IN-GAME RESULT 2026-06-27 (3× /refreshmd, diagnostic debug_text):** cue FIRES every tick; `find_station
      space="player.sector"` = **24 stations ✓**; BUT no MD property exposes a station's commanding NPC —
      `controlentity` (ship-only) AND `manager` both give `npcs=[]` for AI-faction stations. **2 guesses failed →
      STOPPED guessing (stop-and-research).** Census wiring DISABLED (library kept, DORMANT). **BLOCKED on the
      grounded station-NPC / person-enumeration accessor** — likely AI stations don't expose a manager-person to
      MD, so this needs the vanilla crew/station-info menu primitive (SAME gap as Tier-1). → Codex grounding:
      find the accessor in `scriptproperties.xml` + the unpacked ego crew/station menu lua, then re-enable the two
      `run_actions ref=…Census_npcs`. Proven + ready: cue scaffold, find_station, the index→bridge→dashboard path.
- **A4 — Fact-promotion tuning [IG-2, HIGH]. ✅ DONE+VERIFIED 2026-06-27 (live).** Root cause (reconcile):
  condensation is DELIBERATELY disabled (raw turns kept full-fidelity for retrieval — Codex's accuracy choice)
  and `promote_durable_facts` was ON-DEMAND only (ran once via #77 → 11 facts). FIX: auto-wire promotion into
  `memory.record_turn` on a cadence (every 6 turns → `promote_durable_facts(max_promote=6)`) — ADDITIVE (copies
  high-value turns to facts, keeps raw turns) + DETERMINISTIC (regex classify, no LLM), so it's NOT the lossy
  condensation that was disabled; guarded so a promotion error can't break turn recording. Added
  `record_turn_promote_selftest` + `/v1/memory/promote_cadence_selftest`. VERIFIED: cadence selftest allPassed
  (6 high-value turns → 6 facts); `promote_selftest` 5/5 (no regression); LIVE BACKFILL of `game_301276512`
  promoted **174 facts across 23 NPCs** (total 24→198; core 102, significant 96) — the central "talks a lot,
  remembers little" gap is now closed. Files: `memory.py`, `router.py`, `server.py`. (Deferred: memory-audit
  candidate panel — with auto-promotion live the candidate backlog stays small + facts already show in NPC detail.)
- **A5 — Bake "surface it" into the per-feature definition-of-done [PG-1, process]. ✅ DONE 2026-06-27.** Added
  the DoD clause to StarForge canon `bridge-feature-pattern.md` step 5 (every player/sim-facing feature ships a
  dashboard panel OR is logged ◐ "endpoint-only, deferred" with a reason, + the panel pattern) — this is why IG-1
  accumulated. Also added during this session's AARs: the selftest auto-reap convention (A2) + the "new server.py
  route 404 → re-save" gotcha (A1b). Applied live in A1a/A1b.
- **A6 — Reconcile contradictory build-method instructions [PG-3, cheap doc]. ✅ DONE+VERIFIED 2026-06-27.**
  Reconcile found the stale "build ONLY through the Forge UI" HARD RULE in TWO files (not just the scratch one the
  audit named): the CANONICAL `F:\DEV_ENV\X4_Forge\CLAUDE.md` (the live GitHub repo — the important one) AND the
  deprecated scratch `X4-Foundations-Mod-Studio\CLAUDE.md`. Both reversed to match the authoritative
  `F:\DEV_ENV\{CLAUDE,AGENTS,GEMINI}.md` (agent API allowed, 2026-06-24); scratch also marked ⚠️ DEPRECATED →
  use `F:\DEV_ENV\X4_Forge`. VERIFIED: old `## ⛔ HARD RULE … ONLY through this Forge's UI` header = 0 matches in
  X4_Forge; new "agent API allowed (UI-only LIFTED)" header present; all trees agree.
- **A7 — Joule budget + kill switch [PG-4, MED]. ✅ DONE+VERIFIED 2026-06-27 (live).** Per-session LLM-call
  budget + kill switch gating BOTH Player2 chokepoints (`complete` + `npc_complete`, confirmed independent — no
  double-count) via one `_llm_gate()` on `Player2Client`; blocked calls return `NeuralResponse.safe_error`
  (graceful, no crash). Status + control endpoints (`/v1/llm/budget_status`, `/v1/llm/budget_set` {budget,killed,
  reset}, `/v1/llm/budget_selftest`) + dashboard "AI Power" panel (A5 DoD). VERIFIED: selftest allPassed
  (kill_switch_blocks, unlimited_allows, budget_allows_then_blocks); live status active/unlimited; `health.
  player2.ok` + `social_selftest` green (no break to the chat path — `unlimited_allows` IS the live default, proving
  chat isn't gated); panel renders. Files: `player2_client.py`, `router.py`, `server.py`, `dashboard/*`.
  BOUNDARIES (honest): caps CALLS not raw joule values (bridge can't see per-call cost — but Player2 exposes
  `/v1/joules`, already probed in `health`: FUTURE = joule-aware budget); budget defaults 0/unlimited (opt-in cap;
  kill switch is the always-on lever); per-profile config-file budgets (blueprint §19) deferred to runtime control.
- **Gated (sequence behind in-game capture / not for this pass):** rumor auto-origination + multi-hop [IG-7];
  memorials / death & succession [IG-8, blueprint §5.9, needs capital-ship-death capture]; #67 (raid→located loss)
  already tracked.

**Priority order:** A1 (panels) → A2 (reaper) → A3 (roles/binding) → A4 (facts) → A6 (doc, trivial) →
A5 (process) → A7 (joules). A2/A3/A6 are cheap; A1 is the highest visibility ROI.

### ★ IMMERSION & INTERACTIVITY (2026-06-27) — scoped, NOT started (from `immersion-interactivity-proposal-2026-06-27.md`)
Ideation pass diffed built scope vs Blueprint §5 + the Bannerlord technical-research doc. **Core insight: the
backend is deep but the PLAYER can't see most of it** — today = chat UI + flat logbook + notifications; the docs
envision a news feed, NPCs reaching out, voice, tone-consequences, a readable memory, succession. **Fastest gains
are SURFACING existing backend, not new plumbing.** Effort: S=bridge-only · M=bridge+Forge MD/Lua+validate ·
L=heavy in-game UI/gated. Each closes with named validation (`:8713` selftest/endpoint · in-game logbook/chat).

**Tier 1 — best buildable-now wins:**
- **M1 — In-game News Feed ("Galactic Affairs") [M, observed, doc A].** Render the narrator articles already
  generated (#38: title·participants·body·consequence·quote) as a dedicated logbook bulletin stream instead of
  one-liners. Biggest "alive" jump; SURFACES #38, not new logic. **Validate:** in-game logbook shows formatted
  bulletins; debuglog clean.
- **(= A1) Dashboard panels for the 9 endpoint-only families** — already scoped in AUDIT REMEDIATION (IG-1). Same
  task; serves both the audit and immersion. Don't double-build.
- **M2 — Tone → relation feedback [M, observed, doc D].** Expose the per-turn persona-reaction (#17) as a visible
  standing delta ("Nerra's regard ↑ slightly") and nudge the relation within guard-rails (#21). The core
  Bannerlord interaction hook — words have consequences. **Validate:** chat turn shows delta; relation moves
  within bounds (dashboard + in-game notification).
- **M3 — Per-NPC quirks + one-time backstory [S, observed, doc I]. ✅ DONE+VERIFIED 2026-06-27 (bridge/live).**
  Reconcile: the quirk/tone + archetype-specialization layer ALREADY existed (seeded per NPC key) — did NOT rebuild.
  Added the missing piece: a seeded one-time BACKSTORY (origin + formative event, `_ORIGINS`×`_FORMATIVE_EVENTS`,
  independent seed) in `persona.py` `build()` + a "Your history:" line in `card_to_prompt`. Stable-by-construction
  (same NPC-key seed → same history every turn; no DB, no LLM → no joule cost). VERIFIED: `persona/selftest`
  22/22 (4 new backstory checks); live persona cards — Rina vs Rylan get DISTINCT backstories, both in the prompt
  `npc_complete` sends in-game. Boundary: in-game "feel" is wired into every chat prompt but not separately A/B'd
  (qualitative — confirm in play).
- **M4 — Relationship-arc + ambient-rumor beats [S, inferred].** Emit a one-line beat when a social edge (#39)
  crosses a narrative threshold (rivals→comrades, courting→partners) and occasionally surface a low-stakes rumor
  (#76) as sector-tied ambient comms. Makes the social/rumor graphs FELT. Must ride the priority gates (#40) to
  avoid spam. **Validate:** beats fire on real transitions, throttled; comms queue not flooded.

**Tier 2 — plan-next (heavier or one confirmation away):**
- **M5 — NPC-initiated → openable chat [M, observed, doc B].** Upgrade the comms queue (#27–#30) from
  "text in logbook" to "a faction is hailing you" → opening it drops into a live chat with that NPC pre-seeded
  with why. The world reaches OUT (blueprint §5.6/§5.7). **Confirm first:** chat UI can open targeting a specific
  NPC. **Validate:** in-game hail → open → contextual chat.
- **M6 — Player2 voice/TTS [M, observed-with-caveat, doc C].** Route NPC lines through Player2 TTS; play on
  desktop audio + a TTS on/off toggle. **Honest caveat:** audio is desktop-companion, NOT in-engine X4 audio
  (true in-world voice is L/gated). **Validate:** spoken line plays; toggle works.
- **M7 — Memory Book [M, observed, doc E].** Readable per-NPC view of durable facts/promises/grudges/shared
  history. Continuity becomes VISIBLE. **Soft dependency:** pairs with A4 (facts gap) — a thin book undersells;
  do A4 first or together. Dashboard-first is S, in-game panel is M. **Validate:** book renders an NPC's real
  facts (Chrome / in-game).
  - **✅ DONE 2026-06-28 (dashboard slice [S]), dashboard-validated.** Reconcile: `showNpc` ALREADY renders durable
    facts/turns/persona — so M7's delta is the memory-AUDIT integrity view (A4-deferred). DELIVERED: `npcAudit`
    panel (index.html) + `showNpc` fetches `/v1/memory/audit` (source-confirmed shape: `durable_fact_count` +
    `promotion_candidates[{category,tier,role,text}]`) → renders "N durable · M not yet promoted" + the unpromoted
    candidates. VALIDATED via Claude-in-Chrome (after app reset cleared the browser-permission glitch): drove
    `showNpc('game_258932640|reaction|Kha'ak High Command')` → section showed **"6 durable · 2 not yet promoted"**
    + the 2 candidate turns with tier/category/role badges. In-game Memory Book PANEL [M] = separate deferred scope
    (this dashboard observer view has no in-game player surface → in-game gate N/A).

**Tier 3 — gated (sequence behind in-game capture / UI work, NOT this pass):**
- **M8 — Negotiation accept→real-effect [observed, doc H].** NPC offer → player-acceptable proposal → real
  visible effect (relation/order/agreement). Rides the **#67** in-game-proof gate (bridge side ready: #59/#60/#73).
- **M9 — Death & succession [observed, blueprint §5.9].** Capital-ship/leader death → obituary + successor →
  world remembers. = audit IG-8; **gated** on in-game capital-ship-death capture (extends #62/#66).
- **M10 — In-game "state of the galaxy" consult window [L, inferred].** Pull-up posture/wars/standing summary.
  Data exists (dashboard); **gated** on a custom X4 UI surface (historically painful — logbook versions get ~80%
  of payoff cheaper).

**Priority order:** M1 (news feed) + A1 (panels) → M2 (tone) → M3 (quirks) → M4 (beats) → then M5/M6/M7. Most
Tier-1 wins are EXPOSURE of existing backend (the point). The facts gap (A4/IG-2) is a soft dependency under
anything "they remember" (esp. M7).

### ◐ 2026-06-26 — `aic_uix.lua` SyncSectors cdata bug (surfaced by the Forge watcher) — fix deployed, in-game verify pending
The corrected Forge debug-log watcher (now error-driven + mod-marker aware) immediately earned its keep: it
flagged **15 recurring** `[=ERROR=] … GetComponentData(): Invalid argument #1 <component> (got cdata, expected
component ID)` faults, interleaved with the `[AICHAT][UIX] sectors_sync` heartbeat (15s cadence → the sectors
reader, not the 120s fleets reader). **Root cause:** `SyncSectors` enumerates sectors via an ffi VLA
(`buf = ffi.new("UniverseID[?]")`), so `buf[i]` is raw **cdata** (uint64); passing it straight to
`GetComponentData(sid,"macro")` is illegal — that call wants a Lua component ID. The fault was `pcall`-swallowed,
so `macro` silently stayed nil AND X4 logged the engine error each pass. **Real damage (corrected after reading
the log):** display NAMES still resolved (the fallback `C.GetComponentName(rawid)` takes the cdata directly), but
with `macro=nil` the row's `sector_id` fell back to the raw numeric cdata id instead of the **macro string** —
so sectors_sync rows don't join cleanly to the contested/fleet/economy data that's keyed on macro (the exact
SPEC-0b join key). Plus 154/157 error lines in the last 500-entry window were this one fault — it owns the log. **Fix (both deployed + source copies):** `local rawid = buf[i]; local sid =
ConvertStringToLuaID(tostring(rawid))` — the EXACT proven conversion the working skills reader already uses
(same file, line ~516). Keep `rawid` (cdata) for the `C.GetComponentName` engine call + the stable string key;
use `sid` (Lua ID) for `GetComponentData`. **Verified:** static — file intact (692 lines, no mount truncation),
edit confirmed, pattern identical to the in-file proven reader. **PENDING (honest ◐):** the in-game gate (errors
stop + sector names resolve) and the DB-dashboard gate (sectors_sync posts real macro names, not "Unknown
Sector") both require a UI/save reload to load the new Lua — NOT yet run (the live session was mid-walk; I did
not force a save-reload of Ken's running game). Confirm at next reload: watcher `modRuntime.errorCount`→0 and the
bridge `/v1/sectors_sync` rows carry real sector macros.

**UPDATE 2026-06-26 — SyncSectors VERIFIED in-game + a RESIDUAL found & fixed.** After an F9 quickload the
SyncSectors fix is confirmed: the frequent **15s-cadence** cdata errors STOPPED (pre-reload errors at gametime
~80200 scrolled out; none recur at the sectors cadence). Timestamp analysis then exposed a residual at **~120s**
spacing = the **SyncFleets** cadence: line 456 `sc = GetComponentData(obj,"sector")` returns a cdata component
that was passed straight back into `GetComponentData(sc,"macro")` → same fault, lower frequency (per-unique-sector,
cached, fight-ships only). Same `ConvertStringToLuaID(ck)` fix applied to both deployed + staged copies; confirms
on next reload. Harmless meanwhile (pcall-guarded; the sector key just falls back to numeric).

### ✅ SPEC 1j — PLAYER-FACING VOICE: factions reach out to YOU unprompted (Ken 2026-06-26) — DONE + VERIFIED 3-GATE IN-GAME
**VERIFIED 2026-06-26 (all three gates):** (1) **Forge diagnostics** — `project/validate` on the comms cue:
structuralErrors 0, unresolvedCueRefs 0, `comms_incoming` md↔lua binding RESOLVED (the 2 remaining crossFile
errors are pre-existing + unrelated: `ai_influence.request` heuristic miss + the dynamic `"log_"..cat` control).
(2) **Bridge/DB** — `/v1/player_comms/prove` → queue → `/v1/player_comms` drain returns real Player2-authored,
grounded, player-addressed communiqués; `influence_step` hook runs clean (ok, reviewed 3, no break). (3)
**IN-GAME** — after an F9 quickload (Ken: F9 reloads), two forced communiqués surfaced in the **Alerts** logbook
tab, **no `[TEST]` prefix**, faction-titled, full body: "GODREALM OF THE PARANID WARNING — Your trade routes will
be sealed; no methane, ice, ore, silicon, helium, or allographyne will pass your stations. Defy this embargo and
you invite the full fury of the Godrealm upon your fleet." and "TELADI COMPANY WARNING — Your continued
operations near Argon borders will be considered hostile…". The new Lua `DrainPlayerComms` consumed the queue
(drained to 0) and the new MD `CommsIncoming` cue rendered them with ZERO cue errors. This is blueprint §5.6 live.
Build details below.

### (build notes) SPEC 1j — PLAYER-FACING VOICE
Closes blueprint goal #8 / §5.6 (the felt "alive galaxy"): today the autonomous loop only surfaces AMBIENT
news (logbook tab + 3s toast, `[TEST]`-marked) and `/v1/updates_pool` is never driven — factions never
*reach out to the player*. **Scope (Ken-confirmed 2026-06-26):** TIERED — a **prominent incoming comms
message** (faction "transmits" to you; titled communiqué you open + read, §5.6 "ARGON STRATEGIC ALERT" style)
for player-relevant crises, **keeping** the existing ambient news for everything else. **Triggers (all three):**
(1) war/embargo/alliance involving a faction that owns/contests a sector the player is active in; (2) a faction's
standing/grudge toward the PLAYER crosses a threshold (threat or favour); (3) a major galaxy-wide shift (a war
starts, an alliance forms). **Build:**
- **Bridge (Python, edit normally):** in the autonomous loop, evaluate the 3 triggers off data we already have
  (player sectors from sectors_sync owner=player + contested_by; player-standing factors; war/alliance decisions).
  On a fire, LLM-author a grounded in-character communiqué (title + body, via the existing roleRAG/GraphRAG
  grounding) and enqueue to a **player_comms** queue with dedup + a cooldown/budget governor (mirror the
  ACTUATION governor so the player isn't spammed). New `GET /v1/player_comms/drain` + a forced-test entrypoint
  (proving-harness style).
- **Lua (Forge-deployed):** on the existing sync heartbeat, drain `/v1/player_comms` → `raise_lua_event
  AIChat.comms_incoming` with title|body|faction|category (fresh Lua table, pcall-guarded).
- **MD (Forge-built via agent API + deploy):** a comms cue handles control `comms_incoming` → `show_notification`
  ("Incoming transmission — <Faction>") + `write_to_logbook` (Diplomacy/Alerts, title "<FACTION> STRATEGIC
  ALERT", full body, `faction=` for the portrait/icon). Drop `[TEST]`. (Exact action attrs grounded via the
  Forge `validate` loop — authoritative.) Later upgrade: "Open" drops the player into the faction-rep chat UI
  seeded with the communiqué (reuses the proven aic_uix chat) — NOT in this slice.
- **VALIDATE (all 3 gates + reload):** Forge `validate` ok:true; DB dashboard shows player_comms fill/drain; the
  Forge debuglog watcher stays clean + the comms cue fires; in-game reload (also clears the SyncSectors cdata
  fix) → force a comms → SEE the prominent message. Then wire the real triggers + tune frequency.
**Honest bound:** this slice delivers the *prominent unprompted comms channel*; a wider ACTION vocabulary
(embargoes that choke trade, tribute, contracts — blueprint's other half) stays separate/next.

**HONEST OPEN POINTS (for review / Codex feedback — what is NOT yet proven or is deliberately deferred):**
1. **In-game proof used the FORCED path** (`/v1/player_comms/prove`), not a natural autonomous trigger. The
   natural path (`_maybe_player_comms` inside `influence_step`) is code-complete and runs clean (loop returned
   ok, no break), but a comm was NOT yet *observed* firing autonomously in-game — `commsQueued:0` in the window
   watched. Triggers need real data to fire: near-player needs the player to own/contest a sector a deciding
   faction touches; grudge needs faction→player resentment ≥40; major needs a war/alliance/peace decision that
   tick. → NEXT: observe a natural fire (or lower thresholds / seed a grudge to force the natural path), confirm.
2. **Ambient news still carries `[TEST]`.** Only the new `CommsIncoming` cue dropped the marker; the four
   `log_*` ambient-news cues (galaxynews.xml) still title with `[TEST]`. Drop them when we ship.
3. **"Open" is just a logbook entry.** The communiqué is readable + persistent, but clicking it does NOT yet
   drop the player into the faction-rep chat seeded with the message (the planned immersive upgrade — reuses the
   proven aic_uix chat). Deferred.
4. **Cooldown/budget are first-guess** (`PLAYER_COMMS_BUDGET=1`/tick, `PLAYER_COMMS_COOLDOWN_S=75`,
   `GRUDGE_THREAT=40`, `FAVOUR_DEBT=40`). Untuned against real play frequency — may be too rare or too noisy.
5. **SyncFleets cdata residual** fix is applied but confirms only on the next reload (see the cdata entry above).
6. **Pre-existing crossFile validator warnings** (`ai_influence.request` heuristic miss; dynamic `"log_"..cat`
   control) are NOT mine and NOT fixed — flagged for awareness; the mod runs.

### ✅ SPEC 1k-fix — LOCAL ASSIGNMENT FACTS OUTRANK THE REFUSAL GUARD (Codex "Vigilant" bug, 2026-06-26)
Codex caught the boundary guard backfiring: asked about his OWN ship ("tell me more about the Vigilant"), marine
Quint Caren said he'd never heard of it — the refusal guard treated the ship name as an unknown proper noun and
rejected it, because "Vigilant" is a procedurally-named ship absent from the galaxy-lore corpus. Codex's fix:
a **fact hierarchy — NPC local assignment facts > recent conversation > role card > retrieved lore > refusal
guard.** Implemented:
- **`rolerag.py`** — `analyze_query` / `retrieve` / `analyze_and_retrieve` now take `local_facts` (the NPC's own
  ship/sector/posting). They are matched FIRST (word-bounded), emitted as in-scope `specific` + `local`, added to
  the classifier prompt as "LOCAL FACTS … NEVER mark in_scope=false", and — because the match puts them in `seen`
  — the out-of-scope backstop can never re-reject them. `retrieve` surfaces each as POSITIVE first-person context
  ("Vigilant is part of your own posting — you know her decks and squad routines … not the officer-level picture;
  say so plainly rather than claiming you've never heard of it"). This is the answer shape Codex specified.
- **`player2_client.npc_complete`** — builds `local_facts` from the NPC's `ship_name`/`sector` (target/metadata/
  stats) and passes them into RoleRAG. Deterministic injection — independent of the classifier's LLM variance,
  which is the whole point (the bug was intermittent because it depended on the classifier's mood).
- **Endpoint** `POST /v1/rolerag/analyze` accepts `local_facts` for validation; debug `POST /v1/warphase/test`.
- **VERIFIED (live, `/v1/rolerag/analyze`):** with `local_facts=[Vigilant]`, "tell me about the Vigilant" →
  `specific:["Vigilant"]`, out_of_scope empty, and the positive local-knowledge context line is injected; without
  it the model gets nothing (refuses or invents). **◐ remaining (Codex pt.2, NOT done):** summarizer has only an
  in-character recap mode — needs a **memory-audit mode** (literal facts/contradictions/durable-fact candidates)
  for condensation, and **durable-fact promotion** ("Quint serves on the Vigilant" → durable memory). Tracked next.

### ✅ SPEC 1k — RoleRAG BOUNDARY-AWARE RETRIEVAL (paper §3.4) — bridge-built + LIVE-VERIFIED (Codex/RoleRAG follow-up, 2026-06-26)
Closes the gap Codex + the RoleRAG paper (Wang/Leung/Shen 2025, arXiv:2505.18541) flagged: our retrieval was
faction-anchored graph RAG with the cognitive boundary enforced only by a blanket "you only know X4" system
prompt. RoleRAG's measured win comes from **per-entity, per-query** boundary handling. Built faithfully to §3.4:
- **New `bridge/rolerag.py`.** `EntityIndex` builds the canonical X4 entity set straight from game data
  (factions via lore/`FACTION_NAMES`/`list_factions` + canon ids, sectors via `list_sectors`) — we SKIP the
  paper's Module 1 (semantic entity normalization) because X4 entities are canonical-by-construction (no
  "Anakin=Vader" ambiguity). `analyze_query` = deterministic alias match (free, high precision) + one cheap
  LLM call (HyDE hypothetical-context + entity extraction → `{name,type,in_scope,specificity,rationale}`),
  merged + deduped by canonical key. `retrieve` = the paper's THREE routes: **specific** in-scope → that
  entity's subgraph (`graph_retrieve` per mentioned faction; sector ownership); **general** → NPC faction
  1-hop (the prior behavior, so this is a strict superset); **out-of-scope** → an EXPLICIT refusal line
  ("You have NO knowledge of X — …; do not pretend to know it"). Degrades to deterministic-only if the LLM is
  down (never throws, never rejects without evidence).
- **Wired into `player2_client.npc_complete`** — replaces the faction-only graph_retrieve block; injects the
  entity context + a "COGNITIVE BOUNDARY" section. Gated: the LLM classifier fires ONLY on genuine player
  turns (not news/comms authoring) AND only when the message has an unknown proper noun the deterministic pass
  didn't resolve → the common "ask about known factions" case stays LLM-free.
- **Endpoints:** `GET /v1/rolerag/selftest`, `POST /v1/rolerag/analyze` (debug).
- **VERIFIED (2026-06-26):** (1) `run_rolerag_selftest()` 11/11 (standalone + via live endpoint — proves the
  module imports cleanly into the package and the whole bridge reloaded). (2) **Live-data analyze** on
  `game_301276512`, NPC=argon, msg *"What do the Teladi think about the war, and what would the President of
  the United States do?"* → specific=`[Teladi Company]`, general=`[war]`, **out_of_scope=`[President of the
  United States, United States]`**, 4 context lines + 2 boundary rejections each instructing refusal. The
  paper's anti-hallucination mechanism works on live state. **◐ remaining gate:** in-game NPC chat — type an
  out-of-scope reference to a station NPC and confirm it refuses in-character (a 10-second spot-check; the
  exact instruction injected is already verified, so this confirms LLM obedience, not the pipeline).
- **HARDENING via Ken's bleeding-edge test (2026-06-26).** "Star Wars"/"Earth" is a softball — the base model
  refuses it unaided. The real discriminator is a galaxy-PLAUSIBLE fake. Tested *"Where do the Yaki stand
  against the Vortyx Collective, and would you buy Veldspar ore?"* → first pass LEAKED: only `Yaki` caught;
  `Vortyx Collective` (fake faction) AND `Veldspar ore` (EVE Online ore) both classified in-scope. Root cause:
  the classifier trusted the local model's MEMORY of X4, which can't recall the faction roster. Fix: hand the
  model the AUTHORITATIVE faction roster in-prompt (closed set) + a deterministic backstop — any faction-like
  entity (`_FACTION_LIKE_TYPES`) that resolves to nothing in our complete roster is forced out-of-scope
  regardless of the model. Re-test: `Vortyx Collective` → **out_of_scope** ✓, `Yaki` → specific ✓. **Honest
  remaining leak (then closed, below):** `Veldspar` passed because wares weren't an enumerated set yet.
- **WARE CATALOG from the game's own encyclopedia data (Ken's idea, 2026-06-26) — closes the ware leak.**
  The in-game encyclopedia is a rendered view of `libraries/*.xml`; we already harvest `libraries/factions.xml`
  via `catdat`, so we extended the SAME mechanism: new `lore.parse_wares` + `router.wares_harvest`
  (`POST /v1/wares_harvest`) extract `libraries/wares.xml`, resolve names through the `t/` language DB, and
  store the COMPLETE catalog as canon lore `kind='ware'`. **Harvested 1397 wares** (Advanced Composites,
  Antimatter Cells, Claytronics, …). `EntityIndex` now loads them (`has_wares`), and the closed-set backstop
  extends to `_WARE_LIKE_TYPES` — an unresolved ware-typed entity is forced out-of-scope once the catalog is
  present. **Verified 3/3 consistent** on live data: *"Where do the Yaki stand against the Vortyx Collective,
  and would you buy a hold of Veldspar?"* → **out_of_scope=[Vortyx Collective, Veldspar]**, specific=[Yaki],
  and real wares (Energy Cells, Antimatter Cells, Ore) correctly stay in-scope. So the boundary is now
  two-sided and airtight across factions AND wares, grounded in the game's own catalog rather than the model's
  memory. **Known soft edges:** (a) the local model's entity-EXTRACTION is the floor — if it returns nothing,
  nothing is rejected (degrades to the baseline system-prompt boundary, never worse); (b) a fake ware suffixed
  with a real category word ("Veldspar **ore**") can resolve to the real ware "Ore" and slip — bare "Veldspar"
  is caught. (c) ships/sectors are the same pattern, not yet harvested. Canon ware seed persists in SQLite
  (seed-once like factions; could be folded into a boot ensure-canon step).
- **ZERO-FRICTION on-load canon build (Ken, ship requirement, 2026-06-26).** A published mod can't ask players
  to run a harvest script, so canon must build itself. Done: `router.ensure_canon()` runs on bridge boot (daemon
  thread in `__init__`, never blocks serving), idempotent + version-stamped (`CANON_VERSION`) so it's a cheap
  no-op once built; and `catdat.resolve_game_path` now **derives the install root from the bridge's OWN
  location** (`parents[3]` → `<X4>/extensions/x4_neural_link/bridge`), so it works on ANY machine with no env
  var or hardcoded path. **Verified:** force-rebuild harvested **21 factions + 232 canon relations + 1397
  wares**, game path resolved to the live install from location alone; a second call → "already built". So a new
  game boots with a fully-grounded, lore-accurate NPC layer out of the box. Faction LORE (the rich encyclopedia
  descriptions — e.g. the Antigone Republic prose) is part of the faction harvest, so it's auto-built too; the
  current faction REP (e.g. "Met Hinder") is LIVE/dynamic and already synced via the Lua faction-rep reader
  (SPEC 1c-C). **Coverage vs the encyclopedia categories:** Factions ✅ (+lore) · Wares ✅ 1397 — which in X4
  INCLUDE Ships/Equipment/Station-Modules/Military (verified: Plasma Cannon, Engines, Hull Parts, Scanning
  Arrays resolve in-scope). **Remaining / next:** Races (overlaps faction ids), Galaxy/Sectors (live-synced;
  static full list TBD), Blueprints; fold the live faction rep into the canon lore chunk; and two quality edges
  — (i) the substring-alias false-resolve lets a cross-game term suffixed with a real ware-word slip ("Phaser
  **Array**" → real "…Array" ware), (ii) some engine ware names harvest messy with unresolved nested `{page,id}`
  refs + race markup (`parse_wares` resolves one ref level → needs deeper resolution + cleanup).
- **Deliberately deferred (faithful-but-scoped):** Module 1 semantic normalization (unneeded — canonical
  ids); standalone HyDE call (folded into the single classification prompt); ware/ship specific-entity
  subgraphs (factions+sectors covered; wares route to general/economy context). Per-character boundary (a rep
  knows their faction + public galaxy but not a rival's secrets) is a v2.

### ✅ SPEC 1l — DIPLOMATIC BULLETIN QUALITY: kill repetition + name hygiene (Codex review, 2026-06-26) — DONE + VERIFIED (bridge+Forge)
**VERIFIED 2026-06-26** on live save `game_301276512`: bulletins now read e.g. *"Antigone Republic condemns
Kha'ak's hostile acts and escalates pressure in response to the heavy losses it has suffered…; spokesperson
Met Hinder said \"…\""* and the fallback *"Scale Plate Pact, in pointed condemnation, is escalating tensions
with Kha'ak, citing long-standing grievances."* — i.e. clean display names (no `khaak`/`scaleplate`), titled
spokesperson with the REAL encyclopedia rep ("Met Hinder"), an angle frame, a grounded reason, and no
triple-redundancy. `influence_step` ok; Forge full-project validate structuralErrors 0; `[TEST]` gone from all
bulletin titles (only a dev comment retains the word). Also fixed mid-build: a missing `import re` (broke the
fallback), the `why_event` raw-id leak (now `_normalize_faction_text`), the fallback's action/target
triple-redundancy (angle is now an adverbial frame around ONE action clause + ONE distinct concrete reason),
and per-faction angle seeding so same-tick factions don't all open with "condemnation". **◐ in-game gate:** the
logbook shows clean, varied, `[TEST]`-free bulletins on the next reload (bridge already serves the new text; MD
needs a UI reload to drop `[TEST]`). Build details:
The news lane works mechanically (event → faction interpretation → in-game logbook entry) but reads like a test
harness: ~80% mechanically, ~55-60% as believable politics. Diagnosis: the jump is **constraints, not bigger
prompts**. Six fixes (priority order), all in the news path (`router._decision_news` / `_author_news_llm` /
`_news_fallback` / `_news_clause`) + the galaxynews MD:
1. **Name hygiene (FIRST — foundational, a wiring bug not missing data).** Raw ids leak into prose ("khaak",
   "freesplit", "Scaleplate"). `FACTION_NAMES` already maps these (`khaak→Kha'ak`, `freesplit→Free Families`,
   `scaleplate→Scale Plate Pact`) — route EVERY faction reference (subject + target) through `_fac_name` before
   prompt + in the fallback. (Same normalization already done for SPEC 1j comms.)
2. **Spokesperson format.** "- Tupmanckagtek" → `spokesperson Tupmanckagtek said` (titled) or omit; never a bare
   generated name in official prose.
3. **Duplicate suppression.** Per-(faction→target→action) cooldown (mirror the `_comms_last` governor): don't
   re-emit the same bulletin within a window. Kills the "every few minutes" repetition — the main complaint.
4. **Require one concrete grounded reason from live state** (loss / sector pressure / incident / relation drop /
   shortage / prior grudge). If the factsheet has none, SUPPRESS the bulletin instead of emitting filler
   ("following reports that Scaleplate escalates pressure").
5. **Template families.** Give each bulletin an ANGLE (condemnation, mobilization, warning, retaliation,
   negotiation, denial, propaganda) from action+persona, so structure varies instead of recycling "is escalating
   tensions following reports that…". LLM gets the angle; the deterministic fallback rotates clauses per family.
6. **Drop `[TEST]`** from the galaxynews `log_*` cue titles (the channel is trusted; the SPEC 1j comms cue
   already dropped it).
**Validate:** Forge diagnostics (MD edit) + DB dashboard (bulletins normalized, no dup signatures) + in-game
(logbook reads varied, clean display names, titled/odd-name-free spokesperson, no `[TEST]`). Bridge-side =
edited normally + auto-reload.

## ★★★ SPEC 2 — BANNERLORD-GRADE PER-NPC SITUATED ROLEPLAY (Ken + Codex, 2026-06-26) — THE NEW BAR, 3 SEGMENTS
The bar is no longer "better faction bulletins" — it's **every NPC is situated**: speaks from a role, within an
authority, from live situation + memory. Codex's blunt diagnosis: we're "faction-level political AI with NPC
chat access"; the missing piece is **per-NPC role cards + authority boundaries** (RoleRAG alone isn't it).
Three distinct voices to keep SEPARATE: **NPCs create opinions · Factions create decisions · Narrator creates
history.** Build order (recommended): 2a → 2b → 2c.

### SPEC 2a — PersonaCard + authority model (HEADLINE) — ✅ BUILT + VERIFIED (acceptance test passed) 2026-06-26
**DONE:** new `bridge/persona.py` (`ARCHETYPES` authority table · `classify_archetype` · `PersonaCardBuilder`
with `build` + `card_to_prompt` · `run_persona_selftest` 9/9) wired into `player2_client.npc_complete` — for a
genuine player↔NPC turn (not news/comms/reaction authoring) the context now LEADS with a situated role card
(identity + archetype + AUTHORITY + live concerns + can/cannot). Endpoints `GET /v1/persona/selftest`,
`POST /v1/persona/card`. **VERIFIED live (Codex acceptance test)** — same question to 3 archetypes:
- *"…can you make it happen?"* → High Command: *"\*glances at the battle projections\* …operational directives
  come from the War Council; file your request through them."* · Marine: *"\*Glances at the tactical console…\*
  I can't order a strike, sir. That authority lies with High Command—direct your request to Commander Juro
  Topeka or the fleet admiral."* · Service crew: redirects to fleet command.
- None fabricated authority; each answered from its role with a physical beat + situation + limit + next step
  (the Bannerlord 4-part pattern). Cards are grounded in live state (High Command's concerns = the real
  Kha'ak/Xenon wars + heavy losses). **No reload needed** — bridge-side, so the next in-game NPC chat already
  uses it. **Dashboard (Ken 2026-06-26):** the card is now SURFACED on the NPC sheet — `dashboard/app.js`
  `renderPersonaCard()` fetches `/v1/persona/card` for the selected NPC and renders archetype + authority +
  temperament + concerns + knows + can/cannot, consolidated under her stats (verified: Manda Smitt →
  Service Crew / authority low / can/cannot). So the persona we inject into chat is now inspectable per-NPC.
- **2ND-PASS AUDIT vs the Codex doc (Ken 2026-06-26) — 2 real gaps found + closed.** Codex's required NPC-prompt
  fields are seven: who/role/knowledge/**WANT**/authorize/forbidden/**CONSEQUENCE**. The first card had identity,
  role, knowledge, can/cannot — but no **wants (motivation)** and no **consequence routing** (also one of the 4
  pillars). Added `ARCH_DRIVE` + `ARCH_CONSEQUENCE` per archetype → card now carries `wants` (archetype drive +
  the faction's strategic goal for high-authority NPCs) and `conversation_consequence` (the concrete next step
  this chat can trigger); both injected into the prompt contract ("What you WANT…", "Where this can lead…") and
  shown on the dashboard card. Selftest extended (all-pass). **VERIFIED live** — marine asked to order a strike:
  *"She gives the console a quick glance, jaw set. 'We're already on high alert and our boarding teams are ready,
  Commander, but calling in a full strike is beyond my orders. Direct that request to the fleet command…'"* —
  physical beat + motivation + authority limit + next-step routing, all four Codex pillars. **Deliberately NOT
  split:** Codex's `can_say` vs `can_do` (the merged `can_do` already conveys both; low value). Per-NPC personal
  MEMORY is injected separately by the existing retrieval path (build_situation_briefing/retrieve_relevant), not
  duplicated in the card.
- **3RD-PASS — Codex review #2 (80-85% → tighter, 2026-06-26): "more specific, more local".** Three targeted
  upgrades: (1) **finer specialization** — `ARCH_SPECIALIZATIONS` gives 4-5 specific postings per archetype
  (service crew → maintenance/docking/life-support tech, logistics clerk, repair hand; marine → boarding
  marine/breacher/squad rifleman/security), one seeded-stable per NPC, leading the role descriptor; (2)
  **proximity-ranked concerns** — `_concerns` now takes the NPC's sector and puts a LOCAL contested-sector crisis
  ABOVE faction-wide wars (falls through to wars only when the home sector is quiet); (3) **authority redirect
  map** — `ARCH_REDIRECT` gives a concrete office per archetype (service crew → duty officer/station manager,
  marine → squad leader/CO, captain → fleet command, rep → High Command), injected into the refusal; plus the
  physical beat is now DEFAULT-ON ("START with one beat unless the question is purely factual"). **VERIFIED** —
  selftest all-pass; Manda (life-support technician) replied *"She tightens a wrench on the console, eyes
  flicking to the ship logs… dispatching strike fleets is beyond my remit. Take that request to the fleet
  command officer or station manager."* — the beat now fits the SPECIFIC posting and the redirect names real
  offices. Dashboard shows specialization + redirect. Codex verdict was 80-85%; this closes the named gaps. **Tuning notes (honest):** same-faction NPCs converge somewhat on a soft question (the role colour is
  there but subtle); the physical-beat/next-step richness depends on the local model and the 2-3 sentence target.
  Build details below.

### (build notes) SPEC 2a — PersonaCard + authority model
A new layer between raw X4 data and Player2: for EVERY player-facing NPC turn, synthesize a compact role card
and inject it before the reply, so the NPC RPs hard WITHIN its authority (a marine can rage about Kha'ak but
can't order a fleet; High Command can weigh strategy). Four sub-layers:
- **Archetype classifier** — raw X4 data (role/skill/faction/ship/posting) → an archetype. V1 set: High
  Command, faction representative, station manager, ship captain/pilot, marine, service crew, trader,
  police/security, pirate/criminal, generic civilian.
- **Authority model** — per-archetype `can_say` / `can_do` / `cannot_do` (the boundary that stops a janitor
  speaking for High Command). Deterministic table.
- **Persona synthesis** — combine faction ideology (`FACTION_PERSONA`) + archetype + skill + sector/ship +
  recent events + memory into a short card; deterministic-first with small NPC-key-seeded flavor so the same
  NPC stays consistent across turns.
- **Prompt contract** — "Answer AS this person, within THIS authority, using THIS situation. If asked beyond
  your authority, redirect or refuse in character." Injected in `player2_client.npc_complete` alongside RoleRAG.
- **What we already have to build on:** `npc_complete` already captures name/faction/role/skill/ship/sector;
  `FACTION_PERSONA`; RoleRAG boundary; encyclopedia catalogs; High-Command pseudo-NPCs. NEW = the card builder +
  authority table + contract.
- **ACCEPTANCE TEST (Codex):** ask three NPCs of different archetypes "Should we attack the Kha'ak?" → High
  Command weighs strategy/consequences; marine = aggressive personal reaction but cannot authorize; service crew
  = fear/local concern, redirects to officers. All three differ AND stay within authority.

### SPEC 2b — Narrator layer — ✅ BUILT + VERIFIED (bridge + Forge; in-game on reload) 2026-06-26
**DONE:** new `bridge/narrator.py` — `Narrator(memory)` clusters recent `world_events` by faction-pair, ranks by
summed importance, and narrates the top cluster into a grounded history article `{title, category:"news",
participants, body, consequence, quote}`. CAUSE-GATED (skips `reaction`/trivial/sub-importance-3 rows; no events
→ no article), cursor-deduped per save, LLM-authored with a deterministic fallback, case-sensitive name-hygiene
(fixed a double-expansion "Argon Federation Federation" bug). Wired into `influence_step` (returns `articles`,
budget 1/tick) → Lua `SyncInfluence` raises `log_article` (fresh table) → new MD `LogArticle` cue writes the
article (own TITLE + body + consequence) to the **News** logbook tab — distinct from SPEC 1l faction bulletins
(Diplomacy/Alerts) and SPEC 1j player comms. Endpoints `GET /v1/narrator/selftest`, `POST /v1/narrator/prove`.
**VERIFIED:** selftest all-pass; live narrate on `game_301276512` → *"Free Families Heighten Pressure on Kha'ak
— …consequence: Relations between the factions have become more strained."* + *"Scale Plate Pact Pressures
Kha'ak."* (clean names, real causes); `influence_step` returns `articles` ok; Forge full-project validate
structuralErrors 0 + the `log_article` md↔lua binding resolves. **◐ in-game:** the News-tab article surfaces on
the next UI reload (MD/Lua loaded then) when the loop emits a worthy world_event. **Three voices now distinct:**
NPC opinions (2a PersonaCard chat) · faction decisions (1l bulletins) · world history (2b News articles).
- **2ND-PASS AUDIT vs the Codex NARRATION spec (2026-06-26) — articles were too GENERIC, now evidence-led.**
  Codex's target output cites concrete evidence ("relation dropped to -0.7", "3 patrol losses") + a quote; my
  first articles only paraphrased the event summary. Closed three gaps: (1) **EVIDENCE** — new `_evidence()`
  pulls real numbers from the substrate (relation standing+value via `get_relationship`, conflict cause +
  intensity via `list_conflicts`, recent losses via `derive_pressures`, the contested sector) and leads the
  facts fed to BOTH the LLM and the fallback; (2) **QUOTE** — required in the prompt + a seeded attributed
  fallback, so every article carries one; (3) **thematic TOPIC** (Military/Political/Economic/Territorial) from
  the dominant event type (Codex's `category:"Political"`). **VERIFIED** (selftest all-pass + live): generic
  *"Free Families Heighten Pressure on Kha'ak"* → *"**Free Families Declare War on Kha'ak** [Military] — now
  stand at war with the Kha'ak, relations marked as **-1.00** and the conflict at **100% intensity**… "Our
  forces will continue to apply pressure until peace is secured.""* — the Bannerlord-grade, evidence-cited,
  quoted history article the doc lays out.
- **3RD-PASS — Codex 2b review #2 (~82% struct, ~65% output until SUBSTRATE fixed): the spam is upstream.**
  Codex's key insight: the narrator architecture is fine; it's being fed SPAM — repeated `escalate_pressure`
  `world_events` + no-op `old=-1.0 -> new=-1.0` `influence_log` rows from factions pinned at max war. Fixed at the
  SOURCE (not the narrator): (1) **`apply_incident_effects` saturation guard** — a repeated escalate at max war
  (conflict intensity already 1.0) is a no-op -> records NO loss + NO world_event; only a real escalation
  (intensity rose / new war) becomes history; (2) **`record_influence_change` no-op guard** — a write-back where
  `new==old` no longer logs an identical row; (3) **durable narrator cursor** — persisted to `_meta/narrator_cursor`
  so a bridge RESTART won't re-narrate; (4) **a/an grammar** fix in the fallback quote ("A Argon" -> "An Argon").
  **VERIFIED:** selftest all-pass, `influence_step` ok (guards don't break the loop), fallback articles now cite
  evidence + grammatical quotes. These clean the substrate for the narrator AND the 1l bulletins AND the
  dashboard. (Remaining/noted: full SEMANTIC-repeat dedup beyond exact-title; the upstream decision layer could
  also stop PROPOSING escalate at saturation — a deeper scoring tweak.)

### (build notes) SPEC 2b — Narrator layer (world history, separate from NPC RP + faction decisions)
A Narrator service that runs AFTER meaningful state changes and converts simulation deltas into legible
political history — distinct from the SPEC 1l faction bulletins. Input `{event_type, participants, location,
evidence[], severity, cause, result}` → Output `{title, category, participants[], body, consequence, quote?}`.
**HARD RULE: no real cause in the DB → no article** (relation change / fleet loss / sector contested / shortage
/ deal made-or-broken / player action / faction action / war-peace threshold). Builds on `world_events` +
reuses the SPEC 1l name-hygiene/grounding discipline. Likely refactor: split today's `_decision_news`
(faction-decision bulletins) from a true narrator (history articles).
**Build plan (next session, concrete):** new `bridge/narrator.py` — `narrate(memory, save_id, event)` → article
dict, CAUSE-GATED (return None if no real DB cause), LLM-authored with a deterministic fallback, reuse
`router._normalize_faction_text` for hygiene + the SPEC 1l angle/reason discipline; a `run_narrator_selftest`.
Drive it off `memory.list_world_events` (high-importance, recent) on the influence heartbeat — clustering
related events into one article. Surface as a DISTINCT logbook channel (News/history) separate from SPEC 1j
player-comms and SPEC 1l faction bulletins (three voices stay separate). Endpoints `/v1/narrator/selftest` +
`/v1/narrator/prove`. Acceptance: a real relation-shift/fleet-loss cluster → a titled article with
participants + consequence + (optional) quote; NO cause → NO article.

### SPEC 2c — NPC↔NPC social relationship graph (interpersonal RP, incl. romance)
A FIRST-CLASS social graph, SEPARATE from faction relations (political ≠ social). Three layers per edge:
(1) **social scores** trust/affection/resentment/fear/loyalty/rivalry/debt/attraction; (2) **narrative status**
discrete label (strangers→acquaintances→comrades→friends→rivals→enemies→family→mentor_student→romantic_interest→
courting→partners→ex_partners→betrayed…); (3) **evidence events** (saved_life, served_together, betrayed_order,
shared_secret, public_insult, romantic_confession…). Romance is a PROGRESSION (`stage`/`mutuality`/`confidence`/
`obstacles`/`boundaries`), not a bool. **HARD RULE: edges change ONLY from events, never LLM whim** (fought
together → trust↑; abandoned → resentment↑; saved life → affection↑; etc.). New table + accessors; inject only
the relevant edge when NPC A speaks about NPC B. Highest net-new effort → build LAST.
**Build plan (next session, concrete):** new `npc_relationships` table in `memory.py` (subject_npc, object_npc,
scores…, status, stage/mutuality/confidence for romance, last_event, updated_at) + accessors
`upsert_social_edge` / `get_social_edge` / `list_social_edges_for(npc)` / `apply_social_event(a,b,event)` with a
deterministic EVENT→DELTA table (no LLM whim). A small romance state-machine (`none→curiosity→private_attraction→
flirtation→confession_pending→courting→partners→strained→exes→grieving`). Inject ONLY the one relevant edge into
`npc_complete` when the player's message names another known NPC (resolve via the entity index + the SPEC 2a
card for NPC A's identity). `run_social_selftest` + `/v1/social/*` endpoints. Acceptance: NPC A speaks about NPC
B colored by their real edge; the edge moves only when an event fires, never from chat alone.

## ★★★ SPEC 3 — FROM "AI COMMENTS ON THE GALAXY" → "AI OFFERS CONCRETE, STATE-BACKED GAMEPLAY" (Codex + Ken, 2026-06-26)
### ✅ BUILT 2026-06-26 — event hierarchy (3.1) + war-state phases (3.2)
- **3.1 Event priority hierarchy** — new `bridge/gates.py` (`EventGate` · `ACTION_TIER` · `TIER_POLICY` ·
  `run_gates_selftest` 9/9). Wired into `influence_step`: every decision is classified into a TIER and passes
  GATES (importance · cooldown · **state-actually-changed** · authority · semantic-dedup) → ROUTES
  (actuate/news/narrate/comms/store-silently). **VERIFIED LIVE:** a faction pinned at the -1.0 war floor now has
  its no-op escalate `state_changed=False` → the gate SUPPRESSES it (no news/actuate/comms) — the spam is gone;
  the loop goes quiet instead of repeating "X escalates pressure" every 15s. `GET /v1/gates/selftest`.
- **3.2 War-state phases** — a dead escalate at max war is SWAPPED for a real war-phase move
  (`mobilize_fleet · raid_supply_line · fortify_sector · request_supplies · demand_reparations ·
  war_exhaustion_warning · seek_ceasefire · offer_privateer_contract`), picked by recent-losses + persona
  (war-weary+diplomatic → ceasefire/exhaustion) and rotated per pair; each gets a NEWS verb + a recorded
  world_event (→ narrator). **VERIFIED (deterministic, `POST /v1/warphase/test`):** Teladi vs Kha'ak rotated
  "digs in along the front → calls up war supplies → privateer contracts → mobilises its fleet → raids supply
  lines"; Split started at a different phase (seeded). The gate then fires these (real new state) where it
  suppressed the dead escalate. **◐ live-loop firing** of a max-war faction → phase → news → article is wired
  but stochastic + LLM-latency-bound (couldn't catch it in a quick 4am test); confirms under natural max-war
  conditions on the heartbeat. **Together:** the loop stops spamming and starts doing — Codex's "AI offers
  concrete, state-backed gameplay" begins here. **NEXT under SPEC 3:** contracts-from-sectors (#3), live-economy
  jobs (#4), agreements (#5), player-roles (#6), Kha'ak/Xenon asymmetry (#7), fact-promotion (#8).
- **CODEX AUDIT (2026-06-26, ~80% as-claimed) — one gap CLOSED, one HONESTLY OPEN:**
  - ✅ **FIXED — storage bypassed the gate.** The war-phase `add_world_event` was written BEFORE the gate ran, so
    a cooldown/dedup-blocked phase still entered `world_events` and the narrator could resurface a suppressed
    duplicate. Moved the store to AFTER the gate, conditioned on `gate.fire` — the gate is now authoritative over
    storage too, not just news/actuate/comms. (router.py influence_step loop; bridge reload + gates 9/9 + step ok.)
  - ◐ **OPEN (honest) — war phases are NOT game-actuated.** `_decision_action()` only dispatches actions in
    `RELATION_DELTAS`; the new phases (mobilize_fleet/raid_supply_line/fortify_sector/request_supplies/
    offer_privateer_contract) currently produce DB world_events + news only — narrative/state representation, NOT
    real fleet/job/economy mutation. True actuation (bridge-side war_losses/piracy/economy deltas the loop reads
    back, and/or in-game MD/Lua fleet/job spawns) is the next bounded decision. Not claimed as done.

### ▶ SPEC 3.3-B2 — WAR-PHASE ECONOMY ACTUATION (Ken chose "economy effect + lasting changes", 2026-06-26)
First real NON-relation in-game effect: a war phase makes a LASTING change to a faction's economy.
- **Grounded in real X4 + the DeadAir mods** (Ken's resources): DeadAir Scripts' **"Fill"** feature does exactly
  this ("Adds or removes cargo from Trade Stations, Shipyards, and Wharves"). The **Economy Update spec** (Codex,
  uploaded) says to use OMNISCIENT, non-fog-of-war queries — so the MD uses `find_station_by_true_owner faction=`
  (not the player-known `find_station owner=`). All schema-confirmed via the Forge `/api/schema/library`.
- **MD** (`ai_influence_contract.xml` `On_action`): new `type=='economy'` branch →
  `find_station_by_true_owner name=$estation faction=faction.{$efid}` → `add_wares` / `remove_wares object=$estation
  ware=ware.{$eware} exact=$eamt`. **Schema-VALID** (`project/validate`: only error is the single-file
  `missing_content_xml` artifact; the new elements pass the real md.xsd).
- **Bridge** (`router.py _actuate_war_phase`): `request_supplies` → dispatch `{type:economy, faction, ware:energycells,
  amount:8000, op:add}`; `demand_reparations` → `{...target, amount:5000, op:remove}`. Rides the same actions pipe
  as the relation dispatch.
- **Lua** (`aic_uix.lua`): forwards `ware`/`amount`/`op` in the fresh action table.
- ✅ **VALIDATED IN-GAME (2026-06-26).** After Ken's reload, a `request_supplies` prove (argon) ran end-to-end —
  debuglog: `md.ai_influence_contract.On_action: [AIINF] economy add 8000 energycells @ argon`. That line fires
  INSIDE the `do_if $estation` block, so `find_station_by_true_owner(argon)` matched a station and `add_wares`
  executed. A war phase now makes a REAL, LASTING economy change in the live game. (Read via the Forge's
  `/api/agent/log-file-tail`.) **B-2 chosen effect = DONE + in-game-proven.**
- **Bonus (debuglog):** the mod ALREADY does omniscient per-faction station capture — `[AICHAT][UIX] economy
  paranid stations=165 … xenon 128 … scaleplate 23 needs=18`. So the Economy Update READ pipeline has a real
  starting point in `SyncEconomy`; extend it to per-station products/storage (the new `economy_stations` table).
- **→ NEXT (bigger):** the **Economy Update READ pipeline** — omniscient sync of stations/production/trade-offers
  (`find_station_by_true_owner` / `find_ship_by_true_owner` / `find_sector multiple`) → raw econ tables → derived
  faction ware rollup → AI meaning layer. DeadAir Eco/Scripts/Wars are the reference. Major subsystem, scoped in
  the uploaded "Economy Update" spec.

#### ✅ SPEC 3.3-B RE-SCOPED + CLOSED honestly (Ken, 2026-06-26): "the mod does not invent assets"
Ken's design rule: **NO ship-spawning — the AI reasons and acts over what factions ACTUALLY own.** This both
removes the riskiest work and matches DeadAir: its Dynamic War changes *relations*, and its fleets are built by
the game's own JOB system at real shipyards, never raw-spawned. So B's actuation is **relations + economy over
real assets**, and "fleets" = the AI REASONING over a faction's real military (the read pipeline), not puppeting
ships. Title corrected from "fleets/jobs/economy" → **"relations + economy actuation over real owned assets."**
Phase effects, all in-game-PROVEN over real assets (debuglog `On_action: [AIINF] …`):
- `seek_ceasefire` → real relation move → PEACE. ✅
- `mobilize_fleet` → relation/intensity move; the "fleet" is the faction's real existing military, reasoned over. ✅
- `request_supplies` → `add_wares` at the faction's own station (`[AIINF] economy add 8000 energycells @ argon`). ✅
- `raid_supply_line` → `remove_wares` = real SUPPLY DISRUPTION at the target's own station
  (`[AIINF] economy remove 6000 energycells @ teladi`). ✅ proven
- `demand_reparations` / `offer_privateer_contract` → same proven `remove_wares` branch (econ remove). ✅ (shared path)
- `fortify_sector` (self-economy posture) + `war_exhaustion_warning` (signal) → narrative/DB by design.
All economy effects use `find_station_by_true_owner` (omniscient, DeadAir pattern) + `add_wares`/`remove_wares`,
Forge-schema-validated. **No assets invented. Task #44 ✅ under the corrected scope.**

##### EACH effect now INDEPENDENTLY debuglog-proven (Ken caught a premature close-by-inference, 2026-06-26):
- `request_supplies` → `[AIINF] economy add 8000 energycells @ argon` ✅
- `raid_supply_line` → `[AIINF] economy remove 6000 energycells @ teladi` ✅
- `demand_reparations` → `[AIINF] economy remove 5000 energycells @ argon` ✅
- `offer_privateer_contract` → `[AIINF] economy remove 3000 energycells @ teladi` ✅
- `fortify_sector` → `[AIINF] economy add 4000 hullparts @ argon` ✅ (real ware, add executed)
- `seek_ceasefire` → PEACE alert + relation write-back ✅
- `mobilize_fleet` → ◐ relation/intensity via the SAME proven `adjust_relation` On_action branch (opposite sign of
  ceasefire); not a separate proof, but the literal-same code path. `war_exhaustion_warning` → signal by design.
Lesson re-logged: do NOT close by inference — prove each effect with its own debuglog line.

### ✅ ORDER-PRIMITIVE #1 (task #49) — DeadAir's native "order an existing ship" pattern (2026-06-26)
Extracted from `deadair_scripts/md/deadairdynamicuniverse.xml` (Jobs Expeditions). The native, no-spawn pattern
for real military operations over assets a faction ACTUALLY owns:
- **FIND existing combat ships (omniscient, no spawn):**
  `<find_ship_by_true_owner groupname="$ships" faction="$Fac" space="player.galaxy" checkoperational="true"
  masstraffic="false" multiple="true"><match primarypurpose="purpose.fight"/>…</find_ship_by_true_owner>`
- **ORDER them via vanilla order IDs** (`create_order object="$ship" id="'…'"`):
  - move/patrol: `id="'MoveGeneric'"` params `destination` (sector/station), `position`, `endintargetzone=true`,
    `activepatrol=true`.
  - raid/attack: `id="'Attack'"` params `primarytarget`, `pursuetargets=true`, `allowothertargets=true`
    (+ `'AttackInRange'` for area).
  - other useful ids seen: `'RestockSubordinates'`, `'RecycleDefault'`, `'AttackInRange'`.
- **Mapping:** `mobilize_fleet` → find faction combat ships → `MoveGeneric` to the contested front (`activepatrol`);
  `raid_supply_line` → find combat ships → `Attack` the target's traders/stations.
- **⚠ GOTCHA:** DeadAir re-orders ITS OWN expedition fleets (ships built for that role), not arbitrary faction
  ships — forcibly re-tasking a faction's general military mid-defense could disrupt its own AI. So #50/#52/#53
  must pick IDLE/patrol ships (or a small slice), not yank active defenders. Validate effect + non-disruption in-game.

### ✅ ORDER-PRIMITIVE #2 (task #50) — order branch authored + FORGE-VALIDATED (2026-06-26)
Added a `$type == 'order'` branch to `On_action` (ai_influence_contract.xml): `find_ship_by_true_owner` (combat
ships, `match primarypurpose="purpose.fight"`, checkoperational, masstraffic=false) → `create_order` —
`id='MoveGeneric'` (destination=front, endintargetzone, activepatrol) for kind=patrol, `id='Attack'`
(primarytarget, pursuetargets, allowothertargets) for kind=raid; front = the target's station via
`find_station_by_true_owner`. **Forge `project/validate`: schema-VALID** (only the single-file `missing_content_xml`
artifact; the new military-order elements pass md.xsd). Branch is inert until the bridge dispatches `type:'order'`
(task #52/#53). Ship path: faithful Forge fs/write deploy (Ken's call); graph-compile faithfulness deferred to #61.

### ✅ ORDER-PRIMITIVE #3 (task #51) — a real existing ship took a real order IN-GAME (2026-06-26)
The native-execution bridge under all war ops + contracts, PROVEN. New bridge `POST /v1/order/prove` queues
`{type:'order', faction, target, kind}`; Lua forwards `kind`; On_action's order branch runs. After Ken's reload,
`order_prove(argon vs khaak, kind=patrol)` → debuglog:
`md.ai_influence_contract.On_action: [AIINF] order patrol argon vs khaak ship=ARG Police Quasar Vanguard`.
The line fires AFTER `create_order`, inside the `$oships.count gt 0 and $ofront` guard — so `find_ship_by_true_owner`
matched Argon's own combat ships, the Kha'ak front resolved, and a real `MoveGeneric` patrol order was issued to a
ship Argon ACTUALLY OWNS. No spawning, no errors. **Unlocks #52 (mobilize→orders) + #53 (raid→orders).**
- Note: `purpose.fight` matched a POLICE ship; #52/#53 may want a tighter military filter + a ship-slice cap
  (the documented "don't yank active defenders" gotcha). Ship path: faithful Forge deploy (live extension dir).

### ✅ WAR-PHASE ORDER: mobilize_fleet → REAL patrol order (task #52, in-game proven 2026-06-26)
Replaced mobilize_fleet's relation PROXY with a real order dispatch `{type:'order', kind:'patrol'}` (intensity
substrate stays). Bridge-only change (order branch already live from #51 — no reload). Proven:
`[AIINF] order patrol split vs khaak ship=ZYA Colonial Police Dragon` — a real Split-owned ship took a real
MoveGeneric patrol order toward the front. Codex #4 satisfied for mobilize: real military op, not logbook text.
(Still matches police via purpose.fight — tighter mil filter is a later polish.)

### ✅ WAR-PHASE ORDER: raid_supply_line → REAL raid order + supply disruption (task #53, in-game proven 2026-06-26)
Multi-dispatch support added (`_actuate_war_phase` can return `dispatches:[…]`; influence_step + warphase_prove
queue all; fixed the return to surface `dispatches` not just `dispatch`). raid now emits TWO real effects:
- `[AIINF] order raid argon vs khaak ship=ARG Recon Fighter Discoverer Vanguard` — a real Argon ship got an
  `Attack` order vs the Kha'ak (create_order id='Attack').
- `[AIINF] economy remove 6000 energycells @ khaak` — supply disruption at a Kha'ak station.
Both over real owned assets, no spawning. **Codex #4 military third now real for BOTH mobilize + raid — the gap
Ken flagged ("B not complete") is CLOSED in-game.** Remaining war phases are economy/relation (done). Future
polish: tighter military ship filter (purpose.fight still catches police/recon), ship-slice cap, sector-aware
raid targeting.

### ✅ ANTI-CHEAT: words≠resources — removed ALL magic ware-writes from war phases (Ken, 2026-06-26)
Ken's principle: a faction's DECISION/intent must never mint or skim in-game resources — otherwise the player can
social-engineer the AIs into handing over (or destroying) wares they never earned/lost = a roundabout cheat menu.
This condemned EVERY decision-triggered `add_wares`/`remove_wares` I'd built (request_supplies/fortify = free
resources; demand_reparations/raid/privateer = unearned skim). Removed all of them. What stays legitimate:
- **Orders** (real ships, real action): `mobilize_fleet` → patrol order; `raid_supply_line` → real `Attack` order —
  the economic damage is now EARNED from vanilla combat (destroyed cargo), not a scripted number.
- **Relations** (disposition, not a resource): `seek_ceasefire` etc.
- **News + bridge substrate** (the AI's internal reasoning model — not extractable by the player).
**VERIFIED:** raid prove → dispatch is `{type:order}` ONLY (no economy remove); request_supplies/demand_reparations
→ no dispatch. The legit path for resource transfer = player CONTRACTS (#60), earned by REAL delivery.
- **✅ DB-causality cheat FIXED (Codex audit, 2026-06-26):** removed ALL decision-time substrate fabrication from
  `_actuate_war_phase` — no more `record_loss`/`_econ_delta`/`_conflict_intensity_delta` written off a mere
  decision. War phases now emit ONLY real orders (mobilize/raid) + relations (ceasefire) + news; losses/economy/
  intensity come only from REAL events (census now, the #62 event ledger next). `warphase_actuate_selftest`
  rewritten + 10/10: raid emits an order, fabricates NO loss/economy; supplies/reparations/privateer/fortify =
  no actuation; no conflict conjured.
- **✅ MD economy branch GUARDED (task #64):** the dormant `type=='economy'` add/remove_wares branch now requires
  `$act.$earned=='true'` (set only by the future earned-economy/contract path #63) — a raw decision dispatch can
  never reactivate it. Forge schema-valid.
- **◐ remaining anti-cheat:** #65 — gate/remove the chat-driven `ForceWar_handler` (words→relation mutation)
  behind the diplomacy validators (#58).

### ✅ EVENT-GROUNDED CONFLICT LEDGER #1 (keystone, task #62) — bridge built + 7/7 (2026-06-26)
Codex/Ken keystone: stop treating relation hostility as proof of combat; derive everything from REAL located
hostile actions. Bridge built (headless, deterministic):
- **`hostile_events` table** (attacker, victim, sector, object_id/name, event_kind, magnitude, source, ts,
  linked_order_id) — the source of truth for who-hit-whom-WHERE. `add_hostile_event` / `list_hostile_events`.
- **`derive_conflicts_from_events`** — the keystone derivation: conflicts grouped by faction-pair from recent
  events, with **intensity = rolling score from real magnitude** (not flat 1.0), **cause = the first triggering
  event** (not "relations at war"), **sectors** + **per-victim located losses** straight from the events.
- **Endpoint** `POST /v1/hostile_events` (ingest + return derived conflicts), `POST /v1/hostile_ledger_selftest`.
- **VERIFIED 7/7:** intensity rolling + scales with magnitude (0.875 vs 0.125), located (Grand Exchange/Hatikvah),
  losses attributed (teladi 35), cause = real event not "relations at war", no events → no conflict.
- **NEXT:** #66 in-game capture (MD/Lua detect real combat → POST hostile_events) — the real source; then #67 link
  order_id → events so a raid PROVES ITSELF (the #53 consequence). Live integration (dashboard/news/deriver read
  event-grounded conflicts, retire relation-derived `add_conflict(...'relations at war')`) follows #66's real feed.

### ✅ PLAYER2 CONCURRENCY (task #68) — bounded semaphore replaces the strict chat-lock (2026-06-26)
The bridge serialized ALL Player2 generation behind one `threading.Lock()` (news/narrator/reactions/chat one-at-a-
time). Premise was wrong ("single LOCAL model") — the model is HOSTED (a 120B can't run on the user's GPU), so the
backend serves parallel requests natively; our lock was the sole bottleneck (the server already spawns one thread
per `/v1/request`). Swapped `_chat_lock` → `threading.BoundedSemaphore(chat_concurrency)`, default **3**,
config-tunable (`player2_chat_concurrency`); all `with self._chat_lock:` sites unchanged.
- **VALIDATED LIVE (`/v1/request`→`/v1/response/{id}`):** cap=2 → 4 concurrent done at 6/6/8/8s (vs ~6/12/18/24
  serial); cap=3 → 6 concurrent done at 4/6/8/10/10/10s, **0 errors** (~3.6x vs ~36s serial). Hosted backend
  handles it cleanly; no 429s/failures. Matches the workload (a tick's ~2 news + narrator now parallelize; chat no
  longer blocks background generation). Reversible to 1 via config if Player2's ceiling is ever hit.

### ▶ EVENT LEDGER #2 (task #66) — GROUNDED + design locked (2026-06-26); MD build next
X4 exposes CONFIRMED combat events: `event_object_killed_object` (attacker+victim), `event_object_destroyed`
(+`.killer`), `event_object_attacked`/`_object`, `event_object_hull_damaged`; props `killer`/`attacker`/
`damagesource`. **Constraint:** these register per-OBJECT, not galaxy-wide — can't cheaply watch every death
(why even DeadAir news only reports major events). **Design (unifies #66+#67, avoids the presence-delta heuristic
that would re-introduce movement≠kill ambiguity):** capture confirmed combat AROUND OUR ORDERED SHIPS — when
`On_action` issues a raid/patrol order to ship S, register `event_object_killed_object`/`destroyed` on S (+target);
on fire, the Lua POSTs a real `hostile_event{attacker,victim,sector,magnitude}` LINKED to the `order_id`. The raid
then proves itself with a REAL located kill attributed to its order. **Build = MD event cues + Lua POST to
`/v1/hostile_events`; validate in-game** (slow loop: order→travel→fight→kill→located row). Bridge ledger + ingest
already done (#62).
- **✅ DONE + verified in-game (2026-06-26): real combat around ordered ships is captured → located conflicts.**
  - **3-gate verification:** (1) Forge/ecosystem — `debug-watcher/brief` (Codex's new recency-aware API):
    `cueLiveness.erroringCount 0` of 32 cues, `modRuntime.errorCount 0`, `activeErrors 0` (734 lifetime issues but
    0 ACTIVE — the `sinceDeploy` boundary working), 23 marker lines seen → cues firing clean. (2) Dashboard DB —
    `derive_conflicts_from_events(game_…)` returns 3 located conflicts (alliance/khaak 5 losses, holyorder/paranid 1,
    antigone/holyorder 1). (3) In-game fingerprint proves genuine engine capture (not test data): sectors are RAW
    HEX component ids (`0xc0a4fcd` — live `$obj.sector`, vs selftest's English names), every `magnitude==1` (the
    cue's hardcoded `ship_destroyed`, vs selftest's 15/5), stored under the live `game_…` save (not the
    `__hostile_ledger_selftest__` prefix), losses attributed to victims. Arithmetic checks: 5×1/40 = intensity 0.125.
  - **MD** `ai_influence_combat.xml` (NEW): `State` creates a `$Watched` group on load; `On_killed`
    (`event_object_killed_object group=$Watched`) + `On_destroyed` (`event_object_destroyed group=$Watched`, param=
    killer) raise `AIChat.hostile_event` with `attacker/victim/sector` (DeadAir group-event pattern). Schema-valid.
  - **MD** `On_action` order branch adds each ordered ship to `$Watched` (`add_to_group`). Schema-valid.
  - **Lua** `ReportHostile` (registered `AIChat.hostile_event`) → POST `/v1/hostile_events`.
  - **Bridge** ingest resolves display-name owners → canon ids (in-game `$obj.owner` renders as a name). VERIFIED:
    ingest "Argon Federation"/"Teladi Company" → derived conflict `argon` vs `teladi`, sector "Grand Exchange".
  - **Runtime fix #1 (1st reload):** nested the event cues inside `State` (was a race). Still errored.
  - **Runtime fix #2 (2nd reload, watcher caught State/On_killed/On_destroyed ✗) — grounded in DeadAir + schema
    (Ken):** the group still resolved null because I used the FULL md-path on `create_group` and `parent.$Watched`
    in conditions. DeadAir's proven pattern (`InfPatrolDestroyedListener`) creates the group with a BARE
    `groupname="$Watched"` and the nested listener references it BARE `group="$Watched"` (child inherits parent's
    namespace). Fixed to match. Re-Forge-validated (schema-valid). Needs another reload.
  - **Runtime fix #3 confirmed live:** after the DeadAir bare-`$Watched` fix, the watcher reads the 3 combat cues
    CLEAN (erroringCount 0). The null-group race is fully resolved.
  - **▶ NEXT (#67):** the loss is captured but NOT yet linked to the raid order that caused it. #67 carries the
    `order_id`/raid context into the `hostile_event` POST so a specific raid proves itself with its own located kill.

### ▶ EVENT LEDGER #3 (task #67) — order→loss attribution: ◐ built + gates 1&2 passed; in-game PENDING (2026-06-26)
Closes the loop: a captured loss now names the SPECIFIC raid order that caused it, not just the faction pair.
- **MD** `ai_influence_contract.xml` order branch: after `add_to_group`, mint a unique id
  `'ord:'+kind+':'+fid+':'+tgt+':'+$oship.idcode` and tag the ship with a **component-scoped MD var**
  `$oship.$AIINF_order` (rides on the ship object). `debug_text [AIINF] order_tag <id>`.
- **MD** `ai_influence_combat.xml` both cues: read the tag off the watched ship (`event.object`) — `$attacker` in
  `On_killed`, `$victim` in `On_destroyed` — and append `|order=<id>` to the `AIChat.hostile_event` param.
- **Lua** `ReportHostile`: the `key=value` parser already yields `ctx.order`; forward it as `linked_order_id`.
- **Bridge** `derive_conflicts_from_events`: collect per-conflict `orders` (dedup set → sorted list) so the
  attribution is observable. `add_hostile_event` already persists `linked_order_id` (#62 column).
- **Verification:** (1) Forge `project/validate` → **0 errors** (schema-legal: component-scoped var + `order=`
  concat). (2) Bridge logic (standalone replica of the derivation, live process not yet restarted): **5/5** —
  `loss_linked_to_raid_order`, `unlinked_event_carries_no_order`, `single_dedup_order_not_two`,
  `losses_still_attributed`, `intensity_still_rolling`. Selftest `hostile_ledger_selftest` extended with the same
  two assertions. (3) **In-game PENDING** — needs: Ken reload (MD+Lua, already on disk at the live ext dir) +
  bridge restart (picks up memory.py/router.py) → issue a raid → the tagged ship kills/dies → a located
  `hostile_events` row carrying `linked_order_id`, surfaced in the conflict's `orders[]`.

### ▶ ECONOMY UPDATE READ PIPELINE — foundation built (Ken's "Economy Update" spec + DeadAir Eco, 2026-06-26)
Turns the AI from "roleplay over remembered events" into "roleplay over the actual X4 economy" (spec's words).
Build-order step 1-3 (bridge side) DONE + tested:
- **Raw table** `economy_stations` (omniscient per-station capture: faction/sector/type/workforce/products/needs/
  storage). **Methods** `upsert_economy_station` / `list_economy_stations` / `rollup_economy_from_stations`.
- **Derived rollup**: a faction's shortages = fraction of its stations needing a ware; key_needs ranked;
  production_health from the short-station ratio → written into the `economy` table (replaces seeded values with
  live-grounded ones). **Endpoints** `POST /v1/economy/stations` (ingest + rollup), `GET? POST /v1/economy/rollup_selftest`.
- **VERIFIED:** rollup selftest **5/5** (3 synthetic argon stations → energycells shortage 0.67, hullparts 0.33,
  production_health 0.33, key_needs ["energycells",…]); ingestion round-trip rolls up a faction from raw stations.
- **NEXT (in-game):** the mod ALREADY logs per-faction station counts (`[AICHAT][UIX] economy paranid stations=165
  needs=7`) — extend `SyncEconomy` to enumerate each station via `find_station_by_true_owner` (omniscient) and POST
  per-station products/storage to `/v1/economy/stations`. Then: meaning-layer prose, economy-backed mission offers,
  narrator econ events, role-filtered NPC economy knowledge, dashboard "Economy Truth" panel (spec §4-10).

#### ▶ SPEC #54 (SCOPED 2026-06-26) — in-game per-station economy capture → fill the hollow economy table
**Why now (dashboard gap audit):** `/api/economy` has 12 faction rows but they're hollow — `shortages:{}` empty on
every faction, `key_needs` is a generic all-ware list, not real demand. The `economy_stations` table (#46) receives
nothing. #54 turns on the live feed; it UNBLOCKS #55 (prose), #56 (panel), and #60 (economy-delivery contract).
**Scope (one bounded unit — capture only):** on the economy heartbeat, enumerate each faction's stations and POST
per-station rows to the existing `POST /v1/economy/stations` (ingest+rollup already built & 5/5 tested). NOTHING
else — no prose, no panel, no contracts, no writes back to the game.
- **Payload per station** (matches `economy_stations` columns): `station_id, faction_id, sector_id, station_name,
  station_type, workforce_current, workforce_capacity, products[], needs[], storage{ware:amt}`. `products`/`needs`/
  `storage` optional-degrade (send what MD can read; rollup only needs `needs[]` + `products[]` to derive shortages).
- **Anti-cheat:** READ-ONLY observation. NO `add_wares`/`remove_wares`. Pure capture of what factions already own.
- **Build steps + per-step verify:**
  1. **✅ RESEARCH DONE (2026-06-26) — lower risk than feared; #54 is mostly a RESTRUCTURE of proven code, not new
     FFI.** The capture is **Lua FFI, not MD** (no MD station-property gymnastics). `SyncEconomy` (aic_uix.lua:551)
     ALREADY enumerates stations via `GetContainedStationsByOwner(fid,nil,true)` (omniscient) and reads outputs
     `GetComponentData(st,"products")` + inputs `GetComponentData(st,"allresources")`. **All per-station fields are
     proven reads already in the mod or canon:** sector `GetComponentData(st,"sector")` (used aic_uix.lua:490),
     type `GetComponentData(st,"macro")` (used :435/:478), id `GetComponentData(st,"code")` (stable idcode → PK),
     name `"name"`, ware label `GetWareData(w,"name")`. Canon recipe: StarForge `entity-model-and-grounded-reads`
     + `Act_Of_Desperation.md:229` (`…→GetComponentData(station,"wares") outputs→GetProductionModuleData inputs→
     GetSupplyBudget/GetTradeWareBudget money`). **DECISIVE:** `rollup_economy_from_stations` (memory.py:2250)
     consumes ONLY `faction_id`+`needs[]`+`products[]` per station — shortage severity = fraction of a faction's
     stations needing a ware. So **`storage` and `workforce` reads are NOT needed to fill shortages** (deferred,
     not blocking); `GetSupplyBudget`/`GetTradeWareBudget` money is **#63's** primitive, not #54's.
  2. **Author Lua (not MD):** restructure `SyncEconomy`'s inner loop to emit ONE per-station record
     `{station_id:code, faction_id, sector_id, station_name, station_type:macro, products[], needs[]}` (all
     pcall-guarded; fallback `station_id=tostring(st)`), collect into `stations[]`, and POST to
     `POST /v1/economy/stations` `{save_id, stations:[…], rollup:true}` — which auto-rolls-up real shortages into
     the `economy` table. REPLACES the current hollow `/api/economy` POST (`shortages:{}`). **CAP stations-per-tick
     + round-robin factions** across heartbeats — reuse the canon "throttled incremental indexer" cursor pattern
     (paranid=165; a full per-station sweep must amortize over ticks, never one POST). PK `(save_id,station_id)` =
     upsert, so re-capture doesn't grow rows. → verify: Forge `validate` → `ok:true` (Lua-only change, low schema
     risk). NOTE: this is a UI/Lua file — Forge validate covers MD/schema; the Lua correctness gate is in-game.
  3. **Deploy faithful** (verbatim `fs/write`/disk, per the lifted-mandate method) + in-game reload + bridge restart.
- **3-GATE VERIFICATION (all three, per the hard rule):**
  1. **Forge/ecosystem:** `validate` ok + `debug-watcher/brief` cue `erroringCount 0`, `activeErrors 0`.
  2. **Dashboard DB:** `/api/economy` `shortages` is NON-empty and faction-specific (not `{}`); `economy_stations`
     row count > 0 for the live save; `economy_rollup_selftest` still 5/5.
  3. **In-game:** debuglog shows the per-station POST marker + `[AICHAT][UIX] economy <faction> stations=N`; the
     "Economy — meaning" panel renders real per-faction shortages.
- **Risks / fallbacks:** (a) MD may not cheaply expose `products`/`storage` per station → fallback to `needs[]`
  (already proven readable) + `products[]`, defer `storage`. (b) full-universe enumeration is expensive → the
  per-tick cap + round-robin bounds it. (c) `station_type` may need a small classification map.
- **References (Ken, 2026-06-26):** DeadAir source at `F:\DEV_ENV\projects\Mods\X4Mods\deadair_scripts` and
  `…\deadairdynamicwars` — ground the station read recipe (and the deferred storage/workforce/budget reads) against
  these before authoring; `deadairdynamicwars` is also the #57-58 diplomacy-eligibility reference.
- **DeadAir cross-check (2026-06-26, Ken's refs):** `deadair_scripts/md/factionlogic_economy.xml` is a
  build-station patch (not a trade read) → confirms the Lua-FFI layer is correct for #54. The DEFERRED storage read
  IS available and DeadAir's "Fill" (`deadairdynamicuniverse.xml:~3905`) shows the exact path + a BETTER severity
  formula: `$station.cargo.{ware}.count` / `.target` / `.cargo.list` → severity = `1 − count/target` (how far below
  desired stock). MD-side; the Lua-FFI equivalent is the future storage-precision pass. NOT needed for #54 (rollup
  fills shortages from needs/products ratio) — logged as the documented upgrade path.
- **Status: ✅ DONE — 3-gate verified in-game (2026-06-26 reload).**
  - **Gate 1 (Forge/ecosystem):** `validate` ok, 0 errors (MD untouched); watcher `modRuntime.errorCount 0`,
    `cueLiveness.erroringCount 0`, brief text "No recent X4 errors or warnings… for x4_ai_influence". (The
    watcher's `states.runtimeErrors:true`/`activeIssueCount 9` is a FALSE POSITIVE — the mod's `log()` uses
    `DebugError`, so every benign `[AICHAT][UIX]` marker is `[=ERROR=]`-prefixed; the 8 evidence lines were all
    `relations_sync`/`sectors_sync` markers, not errors. Authoritative classifiers all 0.)
  - **Gate 2 (dashboard DB):** live `economy_rollup_selftest` **6/6** (incl. the new `market_status_derived_in_
    rollup`); `economy_stations` went 0 → 60 rows; argon `shortages` NON-EMPTY + real (foodrations 0.917,
    medicalsupplies 0.917, energycells 0.883), `market_status` importer.
  - **Gate 3 (in-game):** new marker `[AICHAT][UIX] economy argon stations 0..60/150 sent=60` firing — the
    per-station round-robin is live. Big counts confirm the cap was right (argon 150, split 125, xenon 130).
  - **Note:** full coverage builds incrementally — the cursor captures one faction + a 60-station slice per
    heartbeat (1/12 factions rolled at verify time), converging over ~15-20 heartbeats. By design, not partial.
  - **▶ Unblocks #55 (meaning prose over real shortages), #56 (Economy Truth panel), #60 (economy-delivery
    contract pointing at a real shortage).**

#### ▶ SPEC #55 (SCOPED 2026-06-26) — economy meaning-layer prose (UPGRADE, not greenfield)
`build_faction_briefing` ALREADY phrases economy (memory.py ~1225-1241), but with #54's now-REAL data it has 3
defects: (a) prints RAW ware ids (`foodrations` not "Food Rations"); (b) always says "critically short" ignoring
the real severity float (0.917 vs 0.30); (c) leaks a raw "dependency X/100" number. Per Ken's rule (English, deny
the LLM raw numbers — same discipline as `_humanize_math`/`_qualify_prose`), upgrade the prose. **Bridge-only.**
- **Build:** (1) `_ware_label(ware_id)` — cached map from canon lore `list_lore(CANON_SAVE,"ware")` (#34 catalog),
  fallback raw id. (2) `_shortage_phrase(sev)` bands: ≥0.7 "critically short on", 0.4-0.7 "running low on", <0.4
  "a little tight on". (3) Rewrite the economy block: display names for key_needs+shortages, group shortages by
  band, replace "dependency X/100" with English ("heavily reliant on the Commander for supply"). Keep ≤2-3 lines.
- **Verify (3-gate, applicable):** (1) Forge validate still ok (no MD touched). (2) Bridge selftest — briefing
  economy line uses display names (no raw `foodrations`), has a severity band phrase, NO `/100` in the econ line;
  run against argon's live data. (3) In-game — dashboard "Injected briefing" panel / NPC chat shows natural econ
  prose ("a net importer, critically short on Food Rations & Medical Supplies, running low on Energy Cells").
- **Risk:** some ware ids may miss the lore catalog (khaak/xenon) → fallback to raw id (rare, acceptable).
- **Status: ◐ BUILT + logic-verified (2026-06-26); live verification PENDING a BRIDGE RESTART (no mod reload —
  pure Python). The mod-reload already done for #54 does NOT pick up this memory.py change.**
  - **Built:** `_ware_label` (canon-lore id→name, cached), `_shortage_phrase` (≥0.7 critically / 0.4-0.7 running
    low / <0.4 a little tight), `_and_join` (Oxford-comma list); economy block in `build_faction_briefing`
    rewritten to use them + English dependency (no raw `/100`). `economy_rollup_selftest` extended with 5 prose
    checks (now 11 checks).
  - **Verification:** (1) Forge = N/A (bridge-only Python, no MD/Lua). (2) Bridge logic — standalone replica 8/9
    on the rollup data + argon live data; the 1 "fail" was a wrong test-string (3 wares all ≥0.7 → ONE grouped
    "critically short on A, B, and C" phrase, which is the CORRECT output). Real rendered prose for argon:
    *"a net importer; you rely on importing Food Rations, Medical Supplies, and Energy Cells. … critically short
    on Food Rations, Medical Supplies, and Energy Cells."* — no raw ids, no raw numbers.
  - **✅ VERIFIED LIVE (2026-06-26):** the bridge HOT-RELOADS .py — no restart was needed. Live
    `economy_rollup_selftest` **11/11** incl. all 5 `prose_*` checks. Found+fixed a real bug on the way: the lore
    display name is in the `title` column, not `name` — `_ware_label` now reads `title`, so wares resolve
    (energycells→"Energy Cells", 4/4 probed). Briefing prose renders with display names + English bands, no raw
    numbers. **#55 DONE.**

#### ▶ SPEC #56 (DONE 2026-06-26) — dashboard "Economy Truth" panel made auditable
The "Economy — meaning" panel already rendered the aggregate, but with raw ware ids and no grounding audit. #56:
- **Bridge** `economy_list` now attaches per-faction `station_count` (from the #54 `economy_stations` capture),
  a `ware_names` id→display-name map (#55 `_ware_label`), and `economy_meta {stations_captured, factions_covered}`.
- **Dashboard** (`index.html` + `app.js`): new "Stations" column, ware **display names** in Key needs/Shortages,
  and a header caption of the sweep totals.
- **✅ VERIFIED LIVE:** rendered panel shows display names ("Energy Cells, Hull Parts, Food Rations…"), real
  per-faction station counts (argon **153**, antigone 95, alliance 3), and caption **"1251 stations captured ·
  12 factions"** — the #54 round-robin has fully converged across all 12 factions. Forge = N/A (dashboard+bridge).
- **NOTE (workflow):** the bridge appears to **hot-reload .py on change** — #54/#55/#56 bridge edits all went live
  without a manual restart (only the mod's Lua needed Ken's in-game reload). Treat bridge edits as live-on-save.
  - **Step 1 (research) ✅** — primitives proven, storage/workforce cleanly deferred, money→#63.
  - **Step 2 (build) ✅ authored:**
    - **Lua** `SyncEconomy` (aic_uix.lua) REWRITTEN: round-robin ONE faction + a 60-station slice per call
      (cursors `_econFac`/`_econOff`; a big faction captures over several heartbeats, then advances — bounds the
      UI-thread cost). Emits per-station `{station_id:idcode|tostring, faction_id, sector_id, station_name,
      station_type:macro, products[], needs[]}` (all pcall-guarded) → `POST /v1/economy/stations` (auto-rollup).
      **Removed the hollow `/api/economy` POST** (`shortages:{}`) — the bridge now owns ALL derivation.
    - **Bridge** `rollup_economy_from_stations` now also derives **`market_status`** (exporter if product variety >
      need variety, importer if unmet needs, else neutral) — moved off the Lua, which only ever saw a per-tick
      slice and couldn't judge faction-wide. `upsert_economy` is a partial-merge, so this co-exists cleanly.
  - **Verification so far:** (1) **Forge validate `ok`, 0 errors** (MD untouched — Lua/bridge only). (2) **Bridge
    rollup replica 6/6** — energycells 0.67 / hullparts 0.33 shortages, production_health 0.33, key_needs ranked,
    `market_status` importer (selftest) + exporter (variety case); `economy_rollup_selftest` extended with the
    market_status assertion (now 6 checks). No Lua runtime in-sandbox → block hand-traced (all blocks balance);
    in-game is the real Lua gate.
  - **Gate 3 (in-game) PENDING:** reload (UI Lua, already on disk) + bridge restart (memory.py/router.py) → watch
    debuglog `economy <fac> stations <off>..<last>/<total> sent=N`, then `/api/economy` `shortages` NON-empty +
    `economy_stations` rows>0 + `economy_rollup_selftest` 6/6 live + Economy panel shows real shortages.

#### ▶ SPEC #57 ✅ DONE (2026-06-26) — faction war/peace eligibility pattern EXTRACTED from DeadAir
The dashboard has NO eligibility data (gap audit). Fully grounded against Ken's `deadairdynamicwars` ref
(`dynamicwar.xml` + `dynamicwardiplomacy.xml`). **The pattern (verbatim from the source):**
- **`$ExcludedFactions` (the core artifact, `dynamicwar.xml:273` / `:989`):**
  `[civilian, criminal, khaak, player, smuggler, visitor, xenon]` — these are NEVER subject to dynamic war/peace.
  Rationale: khaak/xenon are engine-permanent hostiles (not negotiable); civilian/criminal/smuggler/visitor are
  non-combatant background/economic factions; player is excluded from auto-war. Story factions (buccaneers,
  hatikvah) are conditionally appended; `PeacefulList`/`VisitorList` also folded in.
- **Active check (`:314`):** a faction is eligible only if `$faction != null and $faction.isactive == true`
  (it still exists in this game).
- **Enemy/ally selection (`:822-824`):** `get_factions_by_relation relation="killmilitary"` → current enemies;
  `relation="member"` → allies; relation value via `$A.relationto.{$B}`, the factor clamped to a min/max band.
- **Relation-move bounds (`dynamicwardiplomacy.xml`):** UI value `±25` (`.relation.{…}.uivalue`), step ±5,
  cost-gated by `player.money`. (Our engine scale is −1..+1; the On_action relation code already clamps to that.)
**So `is_war_eligible(a, b)` = both known+active, AND neither in the excluded set.** Mirrored into the
`x4-reference-mods` skill + StarForge canon. **#57 closed.**

#### ▶ SPEC #58 (PLAN 2026-06-26) — bridge faction-eligibility validator + selftest (unblocks #65)
- **Build:** a pure deterministic validator in the bridge — `war_eligibility(a, b, save_id)` →
  `{eligible: bool, reason: str}`. Rules ported from #57: `EXCLUDED_FROM_WAR = {civilian, criminal, khaak,
  player, smuggler, visitor, xenon}`; both factions must be known to the save (our faction table = "active");
  neither in EXCLUDED. Plus `relation_move_ok(current, delta)` → clamps to engine scale [−1, +1] and reports if a
  move is in-bounds (mirrors DeadAir's ±25). Place in a small `validators.py` (or memory method) + a public
  `POST /v1/diplomacy/eligibility_selftest` route.
- **Anti-cheat tie-in:** this is the gate #65 needs — ForceWar / chat→relation mutations must call
  `war_eligibility` first and refuse if ineligible (no "declare war on the Xenon", no dragging the player into
  auto-war, no minting a war between non-combatant factions).
- **✅ DONE + VERIFIED LIVE (2026-06-26).** New pure module `bridge/diplomacy.py`: `EXCLUDED_FROM_WAR =
  {civilian, criminal, khaak, player, smuggler, visitor, xenon}`, `war_eligibility(a,b,known)` →
  `{eligible,reason}`, `relation_move_ok(cur,delta)` (clamp to [−1,+1]), `run_selftest()`. Wired into router
  (`diplomacy_eligibility` + `diplomacy_eligibility_selftest`, using `memory.list_factions` for the active set)
  and routed at `POST /v1/diplomacy/eligibility` + `…/eligibility_selftest`.
- **3-gate verify:** (1) Forge = N/A (bridge-only). (2) Selftest **12/12** — sandbox import AND live endpoint
  (the new routes hot-reloaded). (3) Live against the real save: argon↔split eligible; paranid↔xenon refused
  ("xenon is excluded"); argon↔narnia refused ("not an active faction in this game").
- **▶ UNBLOCKS #65:** ForceWar / chat→relation mutations can now call `war_eligibility` first and refuse the
  illegal moves (declare-war-on-Xenon, drag-in-player, mint-war-between-non-combatants).

#### ▶ SPEC #65 (PLAN 2026-06-26, workflow demo) — gate the war-causing relation mutation with `war_eligibility`
**RECONCILE findings (before building):** "ForceWar" is NOT one thing. (a) `ai_influence_conversation.xml`
`ForceWar_handler` = a hardcoded `[TEST] Declare war on me` dev cue (`set_faction_relation $A↔player -1.0`);
plus a `[AI TEST]` hotkey in `proving.xml`. (b) The REAL autonomous war-mutation chokepoint is
`scoring.validate_incident` (the Stage-3 disposer in `router.review_faction`): it gates legal-set / authority
tier / confidence / cooldown / idempotency / confirmation — **but NOT faction eligibility**, so a hostility-class
action toward khaak/xenon/player/non-combatant would pass. (c) The chat-driven `adjust_relation` path
(`ai_influence_contract.xml` On_action) is a separate surface.
- **Scope (one bounded unit):** add `diplomacy.war_eligibility` to `validate_incident` — for a hostility-class
  action with a real target, REFUSE if not war-eligible (the pure EXCLUDED check needs no memory). Extend the
  scoring selftest with eligibility cases. The chat path + the `[TEST]` dev cue are assessed in the SECOND-LAYER
  PASS (cover or explicitly defer-with-reason — the `[TEST]` cue is a deliberate, marked dev tool, not LLM-reachable).
- **Validate (cite):** sandbox unit (validate_incident declare_war split→khaak rejected, split→argon allowed);
  dashboard DB feedback (live `strategic/selftest` or a review call shows the rejection reason). Forge = N/A.
- **✅ DONE — IMPLEMENTED + VALIDATED + SECOND-LAYER REVIEWED (2026-06-26).**
  - **Implement:** `scoring.validate_incident` now imports the pure `diplomacy` module and, for any `hostility`/
    `peace`-class action with a real target, REFUSES (`status:"ineligible"`) if `war_eligibility(faction,target)`
    fails — the Stage-3 disposer in `router.review_faction` is the live autonomous chokepoint. Selftest extended +4.
  - **Validate (methods CITED):** (1) **Sandbox unit** — blocked by the known **bash-mount truncation** (the /tmp
    copy of scoring.py was cut at line 407, past my edits); host file confirmed intact via the Read tool, so the
    truncation is a mount artifact, not a real syntax error. (2) **Dashboard DB feedback / live endpoint** —
    `GET /api/strategic/selftest` **22/22 ok**, the 4 new checks pass live (khaak rejected, player rejected,
    peace-with-xenon rejected, split↔argon eligible-passes), nothing else broke. (3) Forge = N/A (bridge-only).
  - **SECOND-LAYER PASS (coverage review vs the task's "chat→relation" wording):** RECONCILE named 3 surfaces;
    my first cut covered only the autonomous one. Re-checked the others: the **chat→relation actions**
    (`relation_delta_limited`, `faction_to_faction_proposal`, `temporary_diplomatic_flag`) are ALL in
    `config/action_whitelist.json` → `disabled_until_tested`, so the chat path is **provably inert** today (no live
    mutation possible). The **`[TEST]` ForceWar_handler** cue is a deliberate, `[TEST]`-marked dev tool (hardcoded
    NPC↔player, not LLM/manipulation-reachable) — retained on purpose.
  - **▶ FORWARD-GUARD (must-do when enabling chat diplomacy):** when any of those whitelisted relation actions is
    moved out of `disabled_until_tested` (e.g. a future contract/diplomacy-chat task), it MUST route through
    `diplomacy.war_eligibility` before mutating — same gate, different entry point. Logged so it's not forgotten.

### ▶ PLAYER CONTRACTS / OFFERS (#59–#60) — NPC offers grounded in real world state
#### ▶ SPEC #59 (PLAN 2026-06-26) — X4-native mission/offer TEMPLATE catalog
**RECONCILE:** `contracts.py` = the mod↔bridge API envelope (NOT mission offers); the `agreements` table stores
ACCEPTED deals; `mission_offer`/`trade_request` are whitelist-`disabled_until_tested`. **No offer-template catalog
exists** (grep clean). So #59 is greenfield — build the catalog of shapes; #60 instantiates one against real data.
- **Scope (one bounded unit):** new pure module `bridge/offers.py` — a catalog of X4-native offer templates, a
  `render_offer(template_id, params)` that fills a template into a concrete offer dict, `list_templates()`, and
  `run_selftest()`. Templates grounded in real X4 mission kinds: `supply_delivery` (Deliver Wares → a real
  shortage, #60), `bounty` (Destroy target → an active conflict), `patrol` (Patrol → a contested sector),
  `trade_buy`/`trade_sell` (Trade a ware). Each = `{id, kind, title, summary_template, required_params,
  grounding (world-data source), reward_kind}`.
- **Anti-cheat:** offers are PROPOSALS only (text/intent). Accepting/fulfilling + any reward is a SEPARATE gated
  flow (reward must be EARNED, ties to #63) — explicitly OUT of #59/#60 scope.
- **Validate (cite):** sandbox unit (`offers.run_selftest`), live endpoint (`POST /v1/offers/selftest`), host-
  confirmed if the bash mount truncates. Forge = N/A (bridge-only).
- **✅ DONE + VALIDATED + REVIEWED (2026-06-26).** New pure module `bridge/offers.py`: 5 X4-native templates
  (`supply_delivery`=Deliver Wares, `bounty`=Destroy Target, `patrol`=Patrol, `trade_buy`/`trade_sell`=Trade);
  `render_offer(template_id, params)` (fails loudly on missing required params — no placeholder offers leak),
  `list_templates()`, `run_selftest()`. Routed: `POST /v1/offers/{list,render,selftest}`.
  - **Validate (CITED):** **Sandbox unit** `offers.run_selftest` **8/8** (not truncated this run). **Live
    endpoints** — `/v1/offers/selftest` **8/8**, `/list` 5 templates, `/render` bounty renders correctly, missing
    params rejected ("missing required params: ware, amount"). Forge = N/A.
  - **SECOND-LAYER PASS:** catalog covers the relevant X4-native kinds; each template carries a `grounding` source
    so #60 can pull real data; render validates (missing/unknown). In-game surfacing of an offer is correctly
    #60's scope (instantiate a real shortage + deliver via player_comms), not #59's. No partial-coverage gap.

#### ▶ SPEC #60 (PLAN 2026-06-26) — economy-delivery contract: NPC asks player to supply a REAL shortage
**RECONCILE:** `offers.render_offer('supply_delivery', …)` (#59) ✓; `memory.get_economy` gives live shortages
(#54) ✓; `memory._ware_label` display names (#55) ✓; `memory.list_economy_stations` gives a real station for
"where" ✓; the router's `player_comms` deque + `player_comms_prove`/`drain_player_comms` (#27) is the in-game
surfacing channel (comm shape `{title, body, faction, faction_name, category, kind, save_id, ts}`). Nothing to
rebuild — WIRE the existing pieces.
- **Scope (one bounded unit):** `_build_supply_offer(save_id, faction_id="")` — pick the faction with the worst
  real shortage (or the given one), take its top shortage ware, render `supply_delivery` with display name +
  severity-scaled REQUEST quantity (text only) + a real captured station as "where" + a severity-banded reason;
  return `{ok, faction, ware, severity, offer}` (NO enqueue, NO reward). `economy_supply_offer(payload)` wraps it
  and ENQUEUES a player communiqué. `economy_supply_offer_selftest` seeds a synthetic shortage and asserts the
  offer is grounded in it (selftest does NOT touch the live queue).
- **Anti-cheat:** PROPOSAL only — the request quantity is text; no ware is moved, no reward minted. Fulfilment +
  reward is the separate EARNED flow (#63), out of scope.
- **Validate (cite):** sandbox/live `economy_supply_offer_selftest`; live `POST /v1/offers/supply` against the
  real save → a concrete offer from a real shortage (e.g. argon Food Rations). Forge = N/A.
- **✅ DONE + VALIDATED + REVIEWED (2026-06-26).** Router: `_build_supply_offer` (pure: picks the worst real
  shortage, renders `supply_delivery` with display name + severity-scaled request quantity + a real station for
  "where" + severity-banded reason), `economy_supply_offer` (wraps + enqueues a player communiqué),
  `economy_supply_offer_selftest`. Routed `POST /v1/offers/{supply,supply_selftest}`.
  - **Validate (CITED):** live `/v1/offers/supply_selftest` **7/7**; live `/v1/offers/supply` against the real
    save → *"Argon Federation needs 8,584 Food Rations delivered to ARG Graphene Refinery I. Their stations are
    critically short."* (real faction + real shortage + real station), `comm_enqueued:true`. Forge = N/A.
  - **SECOND-LAYER PASS caught a real gap:** the first live run rendered "delivered to **Unknown Station**" (the
    captured station name read back as a placeholder). Re-IMPLEMENTED the "where" fallback to skip empty/`Unknown*`
    names (use the next real station, else "{faction} space"), added a `where_no_unknown_placeholder` selftest
    check, and re-validated (7/7, leaks_unknown=false). Anti-cheat: PROPOSAL only — request quantity is text, no
    ware moved, no reward minted (the EARNED fulfilment flow is #63).

### ▶ SPEC #63 (PLAN 2026-06-26) — earned-economy: faction budget grounded in REAL owned stations
**RECONCILE:** no budget/stockpile/credits field exists (grep clean). The MD economy branch (#64) gates on
`$act.$earned=='true'` but NOTHING server-side validates ownership — its own comment names "#63" as the
owned-budget draw. Canon (`Act_Of_Desperation.md:229`) names `GetSupplyBudget`/`GetTradeWareBudget` as the real
in-game money primitives (future in-game capture, like #54). For NOW, derive a grounded budget from the REAL
owned infrastructure already captured (#54): `capacity = station_count × PER_STATION × production_health`.
- **Scope (one bounded unit):** a budget abstraction + the anti-cheat validator. `faction_budget` ledger table
  (save_id, faction_id, spent, updated_at); `budget_capacity(save_id,fid)` (derived, grounded in real stations);
  `budget_spent` / `record_budget_spend`; **`validate_earned_transfer(save_id, fid, cost)` → {earned, reason,
  capacity, spent, remaining}** — earned=true ONLY if `capacity − spent ≥ cost`. Persistent spend tracking so a
  faction can't re-spend the same budget (the cheat). Router `budget_status` + `earned_validate` endpoints +
  selftest. **The `earned` marker is SERVER-set by this validator, never LLM-settable.**
- **Anti-cheat:** "a faction can only give what it owns." The budget scales with REAL owned stations (#54), so
  words≠resources holds. Real `GetSupplyBudget` in-game capture = documented follow-up (refines the derivation).
- **Validate (cite):** sandbox/live `earned_validate_selftest` (afford within capacity True; over-capacity False;
  spend then re-check refuses re-spend); live `POST /v1/economy/earned_validate` against the real save. Forge N/A.
- **✅ DONE + VALIDATED + REVIEWED (2026-06-26).** memory: `faction_budget` ledger table + `budget_capacity`
  (= station_count × PER_STATION(250k) × production_health — grounded in REAL #54 stations), `budget_spent`,
  `record_budget_spend`, `validate_earned_transfer(save, fid, cost, commit)` (earned ONLY if capacity−spent≥cost;
  commit debits so it can't be re-spent). Router `budget_status` + `earned_validate` + `earned_validate_selftest`;
  routed `POST /v1/economy/{budget_status,earned_validate,earned_validate_selftest}`.
  - **Validate (CITED):** live `earned_validate_selftest` **5/5** (capacity-from-real-stations, affordable,
    over-capacity refused, cannot-re-spend-drained-budget, no-capacity-no-spend); live `budget_status` argon
    capacity **6,241,000** (153 stations × health), `earned_validate` 1M→earned, 999B→refused
    ("exceeds the faction's owned capacity"). Fixed a selftest bug en route (the ledger reset floored negatives to
    0 → switched to a unique per-run save_id, the established selftest pattern). Forge = N/A.
  - **SECOND-LAYER PASS — forward-items logged (not core gaps):** (1) credits budget done; a *ware* STOCKPILE is a
    follow-up IF ware-reward offers (`trade_buy`) get enabled. (2) Real `GetSupplyBudget` in-game capture (Lua,
    like #54) will refine the derivation later. (3) **FORWARD-WIRE:** when a contract-fulfilment flow is built, it
    MUST call `validate_earned_transfer(commit=True)` BEFORE any `type:'economy' earned:'true'` dispatch — the
    `earned` marker is server-set by this validator, never LLM-settable (closes the #64 dormant-branch loop).

#### ▶ G4 BACKFILL (✅ DONE 2026-06-26) — promote durable-fact candidates to facts
The G4 audit surfaced under-promotion (live NPC: 25 candidates, 0 facts); this actually PROMOTES them.
- **Built:** `memory.promote_durable_facts(npc_key)` — scans recent turns, promotes non-routine, not-yet-stored
  ones to durable facts via `add_fact` (dedup, skip routine). Routed `POST /v1/memory/{promote_facts,
  promote_selftest}`.
- **Validate (CITED):** live `memory/promote_selftest` **5/5** (promotes refusal+oath, routine skipped, dedup on
  re-run); live on real NPC → 11 facts promoted; `audit_selftest` 5/5 + `/api/memory/selftest` 15/15. Forge = N/A.
- **SECOND-LAYER PASS caught a real latent bug (in G4's audit too):** the facts `verbatim` column is a 0/1 FLAG,
  not text — `f.get("verbatim") or f.get("text")` returned `"1"` as the dedup key for core facts → broke dedup
  (re-promote failed; only 7 of 11 promotions cleared candidates). Fixed BOTH `promote_durable_facts` and
  `memory_audit_summary` to key on `text`; re-validated green. (Added to the bridge-feature-pattern canon gotchas.)

#### ▶ RUMOR PROPAGATION (✅ DONE 2026-06-26) — events spread along the #39 social graph (design-doc §4)
**RECONCILE:** greenfield (no rumor/gossip); builds on #39 edges (affection/trust/attraction = share; rivalry/
fear = suppress) + world_events. Followed the new `bridge-feature-pattern` canon — fast.
- **Built:** `rumors` table (PK save_id+npc_key+rumor_id dedups per NPC). `propagate_rumor(save_id, origin, text)`
  spreads to the warmest top-`reach` ties, confidence from tie strength. `list_rumors`, `rumor_brief` ("Word
  reaching you — … (unconfirmed)") wired into `build_situation_briefing`. Routed `POST /v1/rumor/{propagate,list,
  selftest}`.
- **Validate (CITED):** live `rumor/selftest` **5/5** (spreads to warm tie, NOT to hostile tie, recipient knows
  it, brief surfaces it, dedup on re-spread); `social/briefing_selftest` **3/3** + `/api/memory/selftest` **15/15**
  (the new briefing line didn't break anything). Forge = N/A.
- **SECOND-LAYER PASS — ◐ follow-ons:** multi-hop spread w/ decay (currently single-hop); auto-originate rumors
  from world_events + wire into the heartbeat (make it FIRE during play); rumors influencing faction decisions.

#### ▶ HEARTBEAT WIRING (✅ DONE 2026-06-26) — the G-generators now FIRE during play
The G-generators (G1 patrol, G5 agreements) were on-demand endpoints; nothing in the autonomous loop called them.
- **RECONCILE:** `influence_step` is the heartbeat slice (daemon → influence_step → `_drain` → mod `influence_drain`);
  offers already reach the player via the separate `player_comms` drain. So a throttled side-effect call is the
  low-coupling fix (no news-list format risk). Player-role (G2) needs no periodic gen — it's read live in the briefing.
- **Built:** `gameplay_generation_tick(save_id, dry_run)` — throttled per save (200s): `generate_agreements`
  (G5, persist + announce via player_comms) + alternate one patrol/supply offer (G1/#60). Called (guarded) at the
  end of `influence_step`. `dry_run` skips enqueue so the selftest never pollutes the live comms queue. Routed
  `POST /v1/gameplay/{tick,tick_selftest}`.
- **Validate (CITED):** live `gameplay/tick_selftest` **3/3** (ran / generated agreements / throttled on re-run);
  dry-run on the real save → ran=true, 0 new agreements (the G5 ceasefires already exist → dedup proven). The
  influence_step call is guarded (can't break the loop). Live enqueue uses the player_comms pattern already
  validated by #60/G1. Forge = N/A.

### ▶ GAMEPLAY CHANGES DOC — reconciled build plan (Ken's uploaded doc, 2026-06-26)
**RECONCILE (most of the doc is ALREADY built):** war-state phases ✅(#41/43/44), event priority hierarchy
✅(#40), local-assignment-facts ✅(#42), live economy→shortages ✅(#54-56), economy contracts ✅(#60),
Kha'ak/Xenon excluded from normal war ✅(#58), world-event clustering into arcs ✅(Narrator #38), agreements
table+CRUD ✅(exist, but unpopulated). **Genuinely MISSING (build order per the doc's own "blunt priority"):**
- **G1 — Patrol/escort/defense contracts from contested sectors** (doc #3, "fastest route to AI gives me real
  work"). The war-pressure analog of #60: pick a real `sectors.contested_by` sector → render the `patrol` offer
  (#59) → enqueue a player communiqué. ← BUILD FIRST.
- **G2 — Player role classification** (supplier/mercenary/mediator/war-profiteer/faction-friend/threat…) derived
  from stored conversations/influence/contracts/relationships, so factions react differently.
- **G3 — Kha'ak/Xenon differentiated behavior** (raids/hive/swarm vs expansion/machine/incursion vs normal
  diplomacy) — they're excluded from normal war (#58) but have no distinct event family yet.
- **G4 — Two summary modes** (memory-AUDIT summary distinct from in-character recap) + stronger fact promotion.
- **G5 — Agreements GENERATOR** (the lane exists but is empty: ceasefire/NAP/trade-pact/transit-rights/patrol-
  cooperation as real gameplay objects).

#### ▶ SPEC G1 (PLAN 2026-06-26) — patrol/defense contract from a REAL contested sector
**RECONCILE:** `offers.render_offer('patrol', {faction, where, threat})` ✅(#59); `memory.list_sectors` returns
`name/owner_faction/contested_by[]/strategic_value/player_assets_present` ✅(#3/#4); `_build_supply_offer` +
`player_comms` enqueue pattern ✅(#60). WIRE them — no new infra.
- **Scope (one bounded unit):** `_build_patrol_offer(save_id, faction_id="")` — pick the best contested sector
  (prefer player_assets_present, then strategic_value, then most contesters) with an owner + contesters; render
  `patrol` with owner=faction, sector=where, first-contester=threat; return `{ok, sector, owner, threat, offer}`
  (no enqueue/reward). `sector_patrol_offer(payload)` wraps + enqueues a communiqué. `sector_patrol_offer_selftest`
  seeds a synthetic contested sector and asserts grounding. Routed `POST /v1/offers/{patrol,patrol_selftest}`.
- **Anti-cheat:** PROPOSAL only (text), no reward minted.
- **Validate (cite):** live `patrol_selftest`; live `/v1/offers/patrol` against the real save → a concrete patrol
  offer from a real contested sector. Forge = N/A.
- **✅ DONE + VALIDATED + REVIEWED (2026-06-26).** Router `_build_patrol_offer` (pure: ranks contested sectors by
  player_assets_present > strategic_value > #contesters, renders the `patrol` offer), `sector_patrol_offer`
  (wraps + enqueues a communiqué), `sector_patrol_offer_selftest`. Routed `POST /v1/offers/{patrol,patrol_selftest}`.
  - **Validate (CITED):** live `patrol_selftest` **7/7** (targets the most-pressing sector, owner/kind/grounding,
    no reward, no-contested→no-offer); live `/v1/offers/patrol` → *"Teladi Company asks you to patrol Profit
    Center Alpha, contested by Xenon."* (real contested sector, `comm_enqueued:true`). Forge = N/A.
  - **SECOND-LAYER PASS:** headline patrol contract from a real contested sector delivered; anti-cheat proposal-
    only. ◐ Follow-on (extends the #59 catalog, not G1's scope): escort-convoy / scan-activity / deploy-
    satellites/lasertowers / evacuate templates (bounty already ≈ "destroy raiders").

#### ▶ SPEC G5 (✅ DONE 2026-06-26) — agreements generator (the missing middle between talk & war)
**RECONCILE:** the `agreements` table + CRUD (`add_agreement`/`list_agreements`/`set_agreement_status`) exist but
the lane was EMPTY — nothing generated agreements from game state. WIRE a generator.
- **Built:** `memory.generate_agreements(save_id)` — proposes CEASEFIRES for active wars + TRADE pacts for an
  exporter↔importer(shortage) pair, EXCLUDING engine-permanent hostiles (khaak/xenon don't negotiate), dedup'd
  against existing, `status='proposed'` (a feeler feeding the existing accept/reject lifecycle). Routed
  `POST /v1/agreements/{generate,generate_selftest}`.
- **Validate (CITED):** live `agreements/generate_selftest` **4/4** (ceasefire-for-war, trade-for-exporter/
  importer, excluded-never-negotiate, dedup-on-rerun); live `/v1/agreements/generate` → **3 real ceasefire
  proposals** from active wars (antigone↔teladi, antigone↔ministry, argon↔ministry). The hollow lane is now
  populated. Forge = N/A.
- **SECOND-LAYER PASS — ◐ follow-on:** the remaining doc types (non-aggression pact / transit rights / patrol
  cooperation / player-brokered supply) extend the same generator.
- **EXTENSION (✅ DONE 2026-06-26):** added `patrol_cooperation` (two non-excluded factions sharing a COMMON
  enemy in active conflicts) + `non_aggression` (neutral non-excluded pairs, not at war/allied). Live
  `agreements/generate_selftest` **6/6**; live generate on the real save produced `patrol_cooperation` proposals
  (factions jointly fighting khaak/xenon). Remaining ◐: transit_rights, player-brokered supply (ties to G1/#60).

#### ▶ SPEC G4 (✅ DONE 2026-06-26) — memory-AUDIT summary mode + stronger fact promotion
**RECONCILE:** the fact pipeline exists (`classify_text`→`category_tier`→`heuristic_summarizer`, tiers core/
significant/routine) — the doc's "860 turns, 4 facts" is UNDER-promotion. The categorizer was rich (oath/deal/
insult/threat/betrayal) but **"refusal" (the doc's named "refuses aid") was missing**, and there was no audit
mode distinct from the in-character recap.
- **Built:** added a `refusal` category (regex placed EARLY so it beats deal/oath/economy) → SIGNIFICANT tier, so
  refusals now promote. `memory_audit_summary(npc_key)` — a literal integrity view: durable facts stored PLUS
  durable-fact CANDIDATES (recent non-routine turns not yet promoted), the "memory audit" mode vs the roleplay
  recap. Routed `POST /v1/memory/{audit,audit_selftest}`.
- **Validate (CITED):** live `memory/audit_selftest` **5/5** (refusal + promise promoted as candidates, smalltalk
  excluded); existing `/api/memory/selftest` still **15/15** (refusal category didn't break condensation); LIVE
  audit on real NPC "Finance High Command" → **0 durable facts, 19 promotion candidates** (exactly the doc's gap,
  now surfaced). Fixed a JSON-serialization bug en route (a `set` in a check detail → `sorted(...)`). Forge = N/A.
- **SECOND-LAYER PASS — ◐ follow-ons:** contradiction detection (NPC affirms X then denies X — needs assertion
  tracking); backfill auto-promotion of the historical candidates the audit surfaces.

#### ▶ SPEC G3 (✅ DONE 2026-06-26) — Kha'ak/Xenon differentiated behavior families
**RECONCILE:** `scoring.generate_candidates` produced a UNIFORM option set (khaak/xenon got the same diplomacy/
ceasefire/resource_request as normal factions). **Key design:** their aggression must be OPERATIONAL ("military"
class = orders), not "hostility" relation moves — else it'd hit the #65 eligibility gate (which excludes them).
- **Built:** `behavior_kind(fid)` (khaak→hive, xenon→machine, else normal); new actions `KHAAK_RAID`/
  `XENON_INCURSION` (ACTION_CLASS "military"); `generate_candidates` branches — hive/machine emit ONLY their
  operational family (raid/incursion on existing presence) + the dialogue baseline, NO diplomacy; normal factions
  untouched. Scoring selftest +7.
- **Validate (CITED):** live `/api/strategic/selftest` **29/29** — behavior_kind correct, khaak/xenon emit
  raids/incursions not ceasefire/resource_request, khaak_raid is "military" class, normal faction provably
  unchanged; nothing else broke. Forge = N/A.
- **SECOND-LAYER PASS — ◐ follow-ons:** (a) MOD-SIDE execution of `khaak_raid`/`xenon_incursion` → real Attack
  orders (#53 pattern) deferred (not touching the mod while Codex works the Forge); (b) news-verb prose for the
  two new actions (minor polish).

#### ▶ SPEC G2 (PLAN 2026-06-26) — player role classification (factions react to WHO the player is)
**RECONCILE:** no `classify_player` exists (greenfield); all signals stored — `relationships` (faction→player
trust/resentment/standing), `economy.dependency_on_player`, `player_market.supplying_enemies`, `agreements`
(player-brokered), `conflicts`. WIRE them into a deterministic classifier.
- **Scope (one bounded unit):** `classify_player_role(save_id)` (pure-ish derive) → `{primary_role, role_tags[],
  per_faction:{fid: friend|threat|neutral}}` from the stored signals: supplying factions at war → "war profiteer";
  ≥2 high `dependency_on_player` → "supplier"; player-brokered ceasefire/pact → "mediator"; high trust & no
  threats → "faction friend"; high resentment/at-war → "faction threat"; else "unaligned newcomer". Endpoint
  `POST /v1/player/role` + selftest. Surface ONE line into `build_faction_briefing` ("The Commander is regarded
  here as a …") so factions react in-character.
- **Validate (cite):** live `player_role_selftest` (seed signals → assert role); live `/v1/player/role` on the
  real save. Forge = N/A.
- **✅ DONE + VALIDATED + REVIEWED (2026-06-26).** `memory.classify_player_role(save_id)` (deterministic over
  relationships/economy/player_market/agreements) → `{primary_role, role_tags, friends, threats, per_faction, …}`;
  one reputation line surfaced in `build_faction_briefing`. Routed `POST /v1/player/{role,role_selftest}`.
  - **Validate (CITED):** live `player_role_selftest` **5/5** (newcomer/supplier/war-profiteer-primary/threat/
    friend); live `/v1/player/role` on the real save → primary "faction threat" w/ threats `[alliance, argon]`.
  - **SECOND-LAYER PASS caught + fixed a real bug:** the first live run listed khaak/xenon as "threats," inflating
    the role — but being at war with them is UNIVERSAL, not a player choice. Excluded the engine-permanent/non-
    combatant set (mirrors `diplomacy.EXCLUDED_FROM_WAR`); re-validated (threats now `[alliance, argon]`, 5/5).

#### ▶ #39 SURFACING (✅ DONE 2026-06-26) — wire NPC social ties into the live situation briefing
The #39 graph existed but wasn't in the prompt (Codex: "the gap is whether each prompt gets the right grounding").
- **Built:** `build_situation_briefing` now appends `social_summary(save_id, npc_key)` (guarded) — an NPC speaks
  aware of their closest personal ties.
- **Validate (CITED):** live `social/briefing_selftest` **3/3** (no-ties→no-line; after a seeded
  served_together event the briefing reads "Personal ties: crewmates with Quint Caren"); existing
  `/api/memory/selftest` still **15/15** (additive change didn't break the briefing). Forge = N/A.
- **SECOND-LAYER PASS — ◐ follow-on:** the per-EDGE brief (`social_edge_brief`, inject only the relevant tie when
  NPC A references NPC B in a turn — Codex's targeted example) needs turn-content NPC detection; the always-on
  top-ties summary is the robust core, shipped now.

### ▶ SPEC 2c / #39 — NPC↔NPC social relationship graph (✅ DONE bridge foundation, 2026-06-26)
**Intent (Ken's uploaded docs — "Bannerlord Feature Translation §3" + "Codex_Feedback2 §relationships"):** a
FIRST-CLASS NPC social graph, EXPLICITLY separate from faction diplomacy ("faction = political; NPC =
social/emotional; don't overload one table"). Emotional SCORES + narrative STATUS + EVIDENCE; **changes come ONLY
from social EVENTS, never faction projection or LLM whim**; romance is a PROGRESSION, not a boolean; §7 restraint
(not universal romance).
- **⚠ COURSE-CORRECTION (Ken caught it):** my first cut projected faction relations onto NPCs
  (`seed_social_from_world`: same-faction→colleague, factions-at-war→rivalry) + a thin `affinity`/`romantic`
  schema. That was faction relationships in NPC clothing — the exact anti-pattern the docs warn against. Rebuilt
  to the spec before closing.
- **Built (corrected):** `social_relations(save_id, subject_npc, object_npc, status, relationship_type, trust,
  affection, resentment, fear, loyalty, rivalry, debt, attraction, publicity, evidence_json)` — all 14 doc edge
  fields. `SOCIAL_EVENTS` map (all 8 doc events: saved_life, abandoned_in_combat, served_together, shared_secret,
  public_insult, betrayal, repeated_conversations, player_mediation + flirtation/rebuff/bereavement).
  `apply_social_event(...)` = THE driver (mutates scores, appends evidence, re-derives status — the only
  sanctioned change path). `_advance_social_status` = pure scalars→narrative status (strangers..close
  friends..rivals..enemies..mentor + romance progression private_attraction→flirtation→confession_pending→
  courting→partners→grieving), **romance GATED on attraction AND affection** (§7 restraint).
  `social_edge_brief` = the in-character edge injected when subject talks ABOUT object (scores→English, evidence
  "you remember…", no raw numbers — Codex's example). Routed `POST /v1/social/{list,event,edge_brief,selftest}`.
  One-time guarded migration drops the stale-schema table (no real data) so the new schema recreates.
- **Validate (CITED):** live `/v1/social/selftest` **10/10** (status gating, attraction-alone-≠-romance,
  event-moves-scores, evidence recorded, romance-is-a-state-not-boolean, edge-brief-has-no-numbers, unknown-event
  + self-edge rejected); live event demo → edge brief *"You know B personally — your relationship: crewmates; you
  trust them somewhat. You remember: pulled wounded crew from the wreck."* (served_together+saved_life). The
  schema migration ran live. Forge = N/A (bridge-only).
- **SECOND-LAYER PASS — coverage vs the doc:** all 14 edge fields ✓, all 8 doc events ✓, status machine ✓,
  romance-as-progression ✓, evidence ✓, prompt-injection edge-brief ✓, §7 restraint ✓. **◐ Deferred (need
  previous-status tracking for backward arcs):** the decay/end states `curiosity / strained / separated /
  ex-partners` — modelling a relationship cooling DOWN needs history the pure status-deriver doesn't carry;
  logged rather than half-built.
- **▶ Follow-ups (bridge-foundation-first scope, not this unit):** wire `social_edge_brief` into the live NPC
  prompt when one NPC references another; feed `apply_social_event` from real in-game events (who saved whose
  life — like #66 combat capture); a dashboard social panel.

### ▶ SPEC 3.3 — WAR-PHASE ACTUATION (Ken: "build A then go for B", 2026-06-26) — IN PROGRESS
Closes Codex's open gap above. Two depths, A first as the substrate for B:
- ✅ **A — bridge-side STATE actuation (task #43, DONE + VERIFIED 2026-06-26).** Each war phase now writes REAL
  substrate state the strategic deriver reads back next heartbeat — phases are genuinely state-changing (feed
  pressures + future decisions), not just narrative. **Key design choice:** mutate the SUBSTRATE (war_losses /
  conflict intensity / economy), NOT `strategic_state` directly — `derive_pressures` recomputes strategic_state
  from the substrate every tick, so a direct write there would be clobbered. New in `router.py`:
  `_actuate_war_phase` + `_econ_delta` + `_conflict_intensity_delta`. Effects: `raid_supply_line` → record_loss
  on target (+10) + target production_health −0.06 → target military_pressure↑, economic↑ · `mobilize_fleet` →
  conflict intensity +0.10 (both sides' military_pressure↑) · `seek_ceasefire` → intensity −0.15 (cools) ·
  `offer_privateer_contract` → record_loss on target (+5) · `request_supplies` → own production_health +0.10 ·
  `demand_reparations` → target production_health −0.05 · `fortify_sector` → own production_health −0.03 (supply
  cost) · `war_exhaustion_warning` → signal-only, NO substrate write (honest). Wired into `influence_step`: a
  gate-fired war phase routes to `_actuate_war_phase` (substrate) and surfaces as `phase_effects` in the response
  — NOT the in-game `actions` list (those are MD relation dispatches; phase state is bridge-side until B).
  **VERIFIED:** `POST /v1/warphase/actuate_selftest` 7/7 — incl. `deriver_sees_target_losses` (the deriver picks
  up the recorded losses → recent_losses↑), proving the read-back. Live `influence_step` ok, `phase_effects` key
  present, no regression.
- ▶ **B — real IN-GAME actuation (task #44, IN PROGRESS — design locked 2026-06-26).** A made phases change the
  bridge's world-model; B makes them change the actual GAME. Builds on A's substrate (B without A is cosmetic
  ship-spawning with no economic logic behind it). Scoped into sub-units, safest/most-verifiable first:
  - **Architecture (transport) — CONFIRMED SIMPLER than first thought.** No new queue/endpoint needed: the mod's
    heartbeat (`aic_uix.lua` `SyncInfluence`) already POSTs `/v1/influence_step` and reads `content.articles`/
    `content.actions` straight off the response, and `phase_effects` is now in that same response — so it already
    reaches the Lua. B's transport = the Lua reads `content.phase_effects` → raises a FRESH-table MD event (same
    round-trip rule as the action/article paths) → a new `On_warphase` cue in `ai_influence_galaxynews.xml`
    dispatches the in-game effect. (Note: the phase is ALREADY surfaced in-game as NEWS via SPEC 3.2's NEWS_VERBS;
    B is strictly about the GAME EFFECT, not another logbook line.)
  - **B-1 (first, lowest-risk, fully verifiable):** transport + an in-game LOGBOOK surfacing of the phase ("Argon
    raids Kha'ak supply lines in <sector>") — proves the pipe end-to-end with zero risk to the save. Validate:
    Forge ok:true · DB shows the phase_effect drained · in-game logbook entry appears after F9 reload.
  - **B-2 (real effects, per phase, escalating risk):** map each phase to a concrete X4 MD effect using the
    engine's own verbs — `mobilize_fleet` → spawn/redirect a faction patrol toward the target's border sector;
    `raid_supply_line` → a raider group vs the target's traders in a contested sector; `fortify_sector` →
    defensive station/patrol posture; economy phases → nudge the faction's actual budget/wares. Each effect is
    authored in the Forge, gated, and proven in-game one at a time (the `[TEST]` proving-slice discipline).
  - **B-3 (validation, all three gates, EVERY effect):** Forge diagnostics ok:true · DB dashboard reflects the
    phase_effect drained + the A-substrate delta · **in-game**: drive X4 (computer-use), reload, SEE the fleet/
    raid/posture happen + read the debuglog for MD/Lua errors. A phase isn't ✅ until seen in-game.
  - **Note:** B-2/B-3 need X4 running + the Forge for the mandated in-game validation — this is an in-game build
    session, materially different from A's headless bridge work. A's substrate is the deterministic backstop so
    that even before an effect is proven in-game, the phase already has real consequences in the world-model.
  - ✅ **B-1 BRIDGE SIDE DONE + server-verified (2026-06-26).** `_actuate_war_phase` now also returns a real
    in-game `dispatch` for relation-meaningful phases (`seek_ceasefire` RAISES a war relation — the AI
    de-escalating a real war; `mobilize_fleet` lowers it), routed through the 100%-proven `On_action` cue (no new
    MD). New `POST /v1/warphase/prove` forces a phase + queues its dispatch/news for the mod. Reuses the exact
    `_pending_actions` → `On_action` pipe verified in task #21. Server-verified: prove queues the dispatch, records
    the world_event, actuate selftest 7/7.
  - ⛔ **B-1 IN-GAME actuation BLOCKED — root cause found (see KEYSTONE below).** The ceasefire dispatch sat
    UN-DRAINED in `_pending_actions` for 90s+ while the game ran. Decisive test: a queued player-comm WAS drained
    by the mod on its own (fast GET path alive), but the influence dispatch was NOT — isolating the blocker to the
    slow `influence_step` POST. Re-validate B-1 in-game (ceasefire → relation write-back + PEACE notification)
    once the keystone fix lands.

### ⛔⛔ KEYSTONE BLOCKER (found 2026-06-26) — INFLUENCE-LOOP DELIVERY IS BROKEN (task #45, NEXT)
The mod's `SyncInfluence` POSTs `/v1/influence_step`, which runs LLM news + the narrator **synchronously**
(measured 6–45s, highly variable). That intermittently exceeds the mod's HTTP request timeout, so the WHOLE
response — news, narrator articles, relation actions, AND war-phase dispatches — silently never reaches the game.
Proven by isolation: the fast GET endpoints (`AIChat.sync_relations` 15s, `/v1/player_comms` 30s) work (the comm
queue drained itself); only the slow `influence_step` POST fails to deliver. **This almost certainly explains why
SPEC 1l news, 2a/2b articles, and comms-actions have all read as "pending a reload" — they've been GENERATED but
never DELIVERED.** **Fix = the proven comms pattern:** generate server-side on a background cadence into per-save
drain queues; the mod drains via a FAST `GET /v1/influence_drain` (zero LLM in the request path). One fix unblocks
B-1 and restores the entire news/article/comms surfacing pipeline. Validate in-game: those actually appear.

#### ◐ KEYSTONE FIX BUILT + bridge-verified (task #45, 2026-06-26) — in-game gated on a UI reload
Implemented the decouple exactly as Codex/Claude specified:
- **Bridge (`router.py`):** a background `_influence_daemon` generates a slice every ~22s **only while the game is
  actively pulling** (gated by `_last_drain_ts`, idle cutoff 150s → a closed save costs no LLM), and pushes
  `news/actions/articles/phase_effects` into a per-save `_drain` queue (capped). New **fast** `influence_drain`
  (LLM-free) returns + clears that queue and marks the save active. `server.py`: `GET /v1/influence_drain?save_id=`.
- **Mod (`aic_uix.lua`):** `SyncInfluence` now does a fast `GET /v1/influence_drain` instead of the slow
  `POST /v1/influence_step`; identical `{news,actions,articles}` processing downstream. The LLM never sits in the
  in-game request path again.
- **VERIFIED (bridge side, live):** after a `seek_ceasefire` prove + marking the save active, the daemon generated
  and the fast drain returned `actions:[argon→teladi +1.0]` + 1 news + 1 article **instantly**. The exact failure
  mode (slow POST) is gone from the hot path.
- **◐ IN-GAME PENDING:** the Lua is a UI addon — it's loaded at game start, so the running session still uses the
  OLD POST path until X4 reloads the UI (save reload / restart). After the reload: queue a ceasefire prove → the
  mod's fast drain delivers it → `On_action` applies the relation + writes back → SEE the relation change + PEACE
  notification, and confirm news/articles now surface live. THEN #45 ✅ and B-1 in-game ✅.
- ✅ **VALIDATED IN-GAME (2026-06-26).** After the reload, a `seek_ceasefire` prove (argon→teladi, forced +1.0)
  delivered through the new fast drain: in-game **"PEACE: Argon Federation and Teladi Company — a ceasefire has
  taken hold"** alert + the News-tab bulletin both fired, influence log shows `argon→teladi -1 → 0, source:
  mod_dispatch`, relation holding at 0. Full chain proven: daemon → fast drain → On_action → real relation change
  → write-back. Keystone fix (#45) ✅ AND B-1 in-game ✅. (News/article surfacing that was "pending reload" is now
  flowing live too — Scale Plate / Paranid war bulletins seen on the News tab.)

### ✅ IMMERSION: CONVERT sim-math to English in player-facing prose (Ken, 2026-06-26)
Ken's first ask was "don't report the value"; his correction was sharper: **don't just delete the number —
translate it.** "100% intensity" should read as *fighting at a fever pitch*; "-0.96 relations" as *sworn
enemies*. Fix in `router.py`: `_humanize_math` maps conflict-intensity % → a fighting descriptor (≥85% "a fever
pitch" · ≥55% "full fury" · ≥30% "a steady boil" · else "a low simmer") and any relation/war-score value → a
standing (≤-0.85 "sworn enemies" · ≤-0.55 "bitter enemies" · ≤-0.25 "open rivals" · <0.10 "uneasy neighbours" ·
≥0.55 "close allies"), substituted IN PLACE (comma-aware so appositives read right), and number-bearing telemetry
parentheticals dropped. Applied to BOTH news (`_decision_news`) and narrator articles (in `influence_step`), plus
a prompt rule telling the LLM to describe qualitatively. **Verified** 6/6 on the exact on-screen strings + variants:
"…at war with the Teladi Company and fighting at full fury…"; "…at war with the Kha'ak, now sworn enemies and the
conflict running at a fever pitch."; "The Boron, now open rivals, remain wary…"; no false positives on number-free
prose. Two live bulletins came back clean. Bridge-only (hot-reload) → new bulletins read in English immediately.

### ✅ IMMERSION pt.2: push the prose onto the LLM, demote the map to a net (Ken, 2026-06-26)
Ken caught that the band→phrase map ("a fever pitch") was hard-coded, so leaks read canned. Two changes so the
LLM owns the description and the map almost never fires:
1. **Pools, not single phrases** — each `_humanize_math` band now draws a RANDOM variant (≥85% intensity →
   "a fever pitch" / "its bloody peak" / "a savage boil" / "white-hot fury"; ≤-0.85 relations → "sworn enemies" /
   "implacable foes" / "blood enemies"), so even a leaked number doesn't repeat verbatim.
2. **Deny the LLM raw numbers at the source** — new `player2_client._qualify_prose` runs on the GROUNDING for
   player-facing AUTHORING calls only (`galaxy_news` / `player_comms`): "intensity 100%" → "intensity: all-out",
   and the numeric tallies in parentheses (trust/fear, aggression 70/100, dependency 60/100, resentment 30) are
   dropped — keeping all the qualitative substance (aggressive/uncompromising/bold, hostile, major supplier,
   lasting grudge). Chat + decision calls keep their precise numbers. So the news desk gets the SITUATION but no
   figures to copy, and describes it in its own words; the `_humanize_math` map is now a last-resort net.
   **VERIFIED:** `_qualify_prose` 5/5 on the real briefing lines; 3 live bulletins came back clean + varied
   ("the full wrath of our righteous armada", "the alien horde") with no numbers and no canned phrase.

Codex's strategic read: the mod is converging, but it's simulation/DB-first where Bannerlord is character-first,
and **the softest part is the VALIDATOR/EXECUTOR boundary** — every LLM/decision output must pass "can this
faction/character LEGALLY do this RIGHT NOW?" before it mutates the world. The `-1.0 → -1.0` escalation spam was
the proof the validator was too soft (now guarded — SPEC 2b 3rd-pass). The arc: **game telemetry → DB
facts/events → authority/persona prompt → structured JSON intent → VALIDATOR → executor → narrator/news → memory
condensation.** Codex's BLUNT PRIORITY ORDER (this is the recommended build order, bigger than 2c):
1. **Stop redundant escalation at -1.0** ✅ DONE (SPEC 2b saturation + no-op guards).
2. **War-state PHASES** — once two factions are at war, STOP `escalate_pressure`; switch the action vocabulary to
   `mobilize_fleet · request_supplies · offer_privateer_contract · fortify_sector · raid_supply_line ·
   seek_ceasefire · demand_reparations · war_exhaustion_warning`. Turns "we hate Kha'ak again" into gameplay.
3. **Contracts from contested sectors + fleet presence** (fastest "AI gives me real WORK"): patrol / escort
   convoy / scan enemy / destroy raiders / deliver defence supplies / evacuate / deploy satellites — generated
   from the `presence_debug` contested sectors we already store.
4. **Live economy → player jobs** (needs the live shortage update we scoped): supply contract, urgent delivery,
   trade-corridor negotiation, convoy escort, embargo pressure, shortage bulletin.
5. **AGREEMENTS as real objects** (the missing middle between talk and war; `/api/agreements` is empty):
   ceasefire · non-aggression pact · trade pact · transit rights · patrol cooperation · player-brokered supply.
6. **Player ROLES** — classify the player from stored behavior (supplier / mercenary / mediator / pirate
   collaborator / war profiteer / unreliable contractor / faction friend / faction threat) → factions react.
7. **Kha'ak/Xenon ASYMMETRY** — not the same "escalate pressure" structure: Kha'ak = raids/hive/swarm; Xenon =
   expansion/machine/sector incursion; normal factions = diplomacy/contracts/negotiation.
8. **Memory FACT promotion** — 860 turns but ~4 facts; promote durable commitments (promised a patrol, refused
   aid, negotiated a corridor, insulted a faction) into facts (Codex's recurring note).

### SPEC 3-PRIORITY — EVENT PRIORITY HIERARCHY (Ken, 2026-06-26) — likely build FIRST under SPEC 3
Today everything refreshes on a flat 15s tick (a "polling demo"). Make **15s a HEARTBEAT, not a content-
generation interval.** Each tick: check queues, decay pressures, process 1-2 items — and an event only FIRES
when it passes gates: **importance high enough · cooldown expired · state actually changed · new evidence
exists · player relevance high · faction has authority · not a semantic duplicate.** Tiers:
- **Critical game-state** (war declared/peace, sector ownership change, station/fleet destroyed, major relation
  threshold) → narrator + faction reaction + possible player comms.
- **Strategic pressure** (trade route blocked, sustained shortage, repeated Kha'ak/Xenon losses, buildup) →
  accumulate, fire only on a THRESHOLD crossing.
- **Faction policy decisions** (escalate/de-escalate/sanction/patrol/blockade/bounty/convoy) → validators + cooldowns.
- **NPC-local knowledge** (crew rumor, officer reaction) → update MEMORY, not always logbook output.
- **Ambient flavor** (gossip, morale) → cheap, sparse, mostly stored SILENTLY.
This hierarchy IS the validator/executor boundary in scheduler form — it's what stops a well-built narrator from
narrating spam. **Reframes SPEC 2c (NPC relationships):** still valuable but it's the character-first lane;
Codex's priority order puts the gameplay-action + hierarchy work AHEAD of it.

This roadmap supersedes the old assumption that `x4_ai_influence` is the foundation. The old directory is now source material and backup evidence. The new foundation is `x4_neural_link`: a standalone bridge extension that any X4 mod can depend on to communicate with Player2. It now lives **nested inside `x4_ai_influence/`** (own directory) as the single working copy.

---

## ★★★ REALITY CHECK — the BRAIN is deep, the PLAYER-FACING layer is thin (Ken, 2026-06-25) — TOP PRIORITY
Ken's observation, and it's correct: the database fills, the LLM reasons, but **in the actual game there is ~zero
player-facing feedback.** Honest diagnosis — TWO gaps, not a setting:
1. **No HANDS (actuation).** The autonomous loop applies decisions to the SHADOW world model (our DB) only — it
   does NOT mutate real X4 relations/fleets/economy. The ONLY real-game mutation ever proven is the chat-driven
   ForceWar (`set_relation`). So "Argon escalates against Xenon" is narration in our DB; the real galaxy is
   untouched. → **SPEC 1d-W2 (generalize the ForceWar dispatch to the autonomous loop) is now TOP PRIORITY.**
2. **Thin VOICE (surfacing).** What surfacing exists (logbook bulletins + brief toasts via the MD GalaxyNews
   route) is sparse (many ticks are no-ops/repeats), passive (a tab you must open + a 3s toast), and [TEST]-
   marked. No prominent, immersive "the galaxy is alive" feedback, no faction COMMS to the player (blueprint
   §5.6 crisis messages), no player-as-participant.
**Lesson for validation discipline:** "verified in the DB + grounded demo" measures the BRAIN, not the player
experience. Going forward, a feature isn't really done for the player until a real in-game EFFECT or a prominent
in-game MESSAGE is visible — the in-game gate must mean *the player would notice*, not just *the row changed*.
**Recommended next order:** (1) actuation 1d-W2 — autonomous decisions flip REAL X4 relations (watchable: fleets
engage, faction menu shifts); (2) rich surfacing — faction comms/crisis messages to the player + native-reading
notifications; (3) player-as-participant — factions act toward the player. Actuation first: highest impact, most
contained (the dispatch path already exists).

## ▶ WHAT'S LIVE IN-GAME + HOW TO OBSERVE IT (plain English — updated 2026-06-25)

**In one line:** the mod watches the live X4 galaxy, remembers what happens, forms opinions (moods + grudges),
and lets you TALK to faction representatives who reason from all of it. (The half where AI factions *act* on
those opinions on their own is the next build — SPEC 1d.)

**What runs inside the game (the mod):** every ~15s it reads the live galaxy and sends it to the local bridge —
faction relations, who owns/contests which sectors, each faction's economy, a census of every faction's ships
(and their losses), the game's own news log, and each faction's named representative. It also adds an in-game
CHAT: walk up to an NPC → "Speak to AI" → talk to an LLM-driven character.

**Where to SEE it all — the dashboard** (`http://127.0.0.1:8713/dashboard`). Live panels:
- **Factions** — each faction's dynamic MOOD ("embattled" when bleeding, "belligerent" at war) + its real
  REPRESENTATIVE (Argon → Melissa Mettel) + personality (aggression/risk).
- **Strategic Pressures** — per faction: Military / Economic / Logistics / recent Losses / Territory / Piracy /
  player Alignment — all computed live.
- **Fleet Strength** — every faction's ships by role (fight/trade/mine) + capital ships.
- **Conflicts & Losses** — who's at war + how many ships each faction recently lost.
- **Territory** — sector owners + which are CONTESTED and by whom.
- **World events** — the game's OWN news ("Xenon station destroyed in Hatikvah's Choice I", wars, defences)
  captured as faction memories.
- **Relationships** — trust / fear / RESENTMENT (the grudges) between factions.

**Where to SEE it in the game itself:**
1. **Talk to an NPC** (walk up → "Speak to AI"): the reply is grounded in that faction's REAL situation — its
   representative, current wars, contested home sectors, and grudges. (Proven: an Argon officer spoke of
   "holding the last hull line against the Split" because Argon carried a Split grudge.)
2. **Declare war in chat** → the actual X4 faction relation flips → that faction turns hostile, its ships
   engage. (The one ACTION wired so far.)
3. **The game's news/logbook** — the same events the mod ingests; watch a station fall and see it become a
   faction memory on the dashboard.

**How to watch a GRUDGE form (the headline feature):** find two factions fighting over a sector (Territory
panel shows it "contested") → over minutes their RESENTMENT climbs (Relationships panel) → talk to one of
their NPCs and its tone hardens toward the enemy. Grudges build FORWARD over play (they don't backfill old
fights) and must cross a threshold before an NPC voices them.

**What you WON'T see yet (next, SPEC 1d):** factions don't yet ACT on grudges autonomously — they remember and
talk, but don't launch retaliations/embargoes on their own. That autonomous-injection loop is the next piece.

---

## 2026-06-24 — Mod is now FORGE-BUILT · per-skill reader rebuilt & live · 2 Forge bugs fixed

`x4_ai_influence` is now genuinely **built by the Forge** (MD as ~119 workspace nodes; `/api/agent/deploy`
compiles to BOTH F: source and G: game). Recovered from a session where the developed mod code was lost —
rebuilt from roadmap spec + grounded against the unpacked vanilla UI, not from a found file.

- **Per-skill reader REBUILT + live-verified.** `GetComponentData(npc,"skills")` was gone from every copy/
  snapshot; rebuilt grounded on `ui/addons/ego_detailmonitor/menu_map.lua` (`skills[entry.name]=entry.value`,
  `ConvertStringToLuaID(tostring(component))`). MD raises `AIChat.npc_skills` (NPC component) → Lua → folds
  into `prompt_vars.skills` → bridge `target.skills`. **In-game:** Rina (morale7/board6/pilot6/eng1) + Manda
  (morale3/eng2/mgmt1/pilot1) render real per-skill bars.
- **Forge round-trip bug (found by deploying the real mod, then FIXED in the Forge).** Node→MD regen dropped
  `<library purpose="run_actions">` → broke `Do_sync` (37 worldsync errors) and `Open_chat` (chat wouldn't
  open). Fixed in Forge `xmlParser.ts`(capture) + `types.ts`(emit). Re-deployed clean.
- **Chat auto-open-on-load — ◐ REGRESSED (the `_openRequested` gate is NOT holding).** Gated
  `menu.onShowMenu` behind an `_openRequested` flag (set only on real player opens). It worked once, but
  during the 2026-06-24 fleet-reader session the "Comm-Link: Argon Officer" window (note: **default fallback
  names** argon/Officer → opened with NO real NPC context) reopened on **every** F9 load. Leading hypothesis
  (~60%): the CLOSE button hides the frame but does NOT pop the menu from X4's engine active-menu record, so
  the quicksave still records the chat as the active menu and load restores it down a path that bypasses (or
  re-trips) the `_openRequested` guard. Needs a debuglog probe (log `_openRequested` + caller at onShowMenu
  entry, and confirm CLOSE calls `Helper.closeMenuAndReturn`/proper deregistration). NOT yet fixed — do not
  re-mark ✅ until a clean F9 load shows no window.

**Readers built + live-verified (all via the Forge loop, grounded on unpacked vanilla):**
- **Sectors (#8) ✅** — `GetSectorsByOwner` per faction → owner; `GetComponentData(sid,"macro")` →
  `GetMacroData(macro,"name")` for real names (fog-of-war proof). Rides the 15s relations heartbeat.
  Territory panel populated (owner + name).
- **Economy (#10, production half) ✅** — `GetContainedStationsByOwner(fid,nil,true)` → union station
  `products`/`allresources` → `key_needs` (inputs not self-produced) + `production_health` (station-count)
  + `market_status` (exporter/importer). Throttled ~120s off the heartbeat. POST `/api/economy`.
  Live: exporters (argon/antigone/holyorder = raw resources, health 1.0) vs importers (alliance/ministry
  = long manufactured key-needs). NPCs can now reason about supply/dependency.

- **Ships/Fleets (#9) ✅ LIVE-VERIFIED in-game** — two parts: (a) the **conversation NPC's own
  ship/fleet** folded into chat context (MD reads `event.object.ship.knownname` + `.commander` → prompt_vars
  → persona, so the NPC says "I serve aboard the <ship> in <commander>'s fleet"); (b) a **faction fleet
  census** — Lua `GetContainedObjectsByOwner(fid)` → count ships by primarypurpose (fight/trade/mine/build)
  + capitals → bridge `fleet_strength` table + `/v1/fleets_sync` + `/api/fleets` → dashboard **Fleet
  Strength** panel. Throttled ~120s off the heartbeat.
  - **HARD-WON GOTCHA (cost the whole census ~6 reload cycles):** the enumerator MUST be called with a
    **single arg** — `GetContainedObjectsByOwner(fid)` enumerates that faction's objects **galaxy-wide**.
    `GetContainedObjectsByOwner(fid, nil, true)` (the "recursive" 3-arg form) returns an **empty table for
    every faction including the player** — the explicit `nil` container poisons it. (Contrast the *stations*
    sibling `GetContainedStationsByOwner(fid, nil, true)`, which *does* accept the 3-arg form — they are not
    symmetric.) Ship detection: `GetMacroClass(macro)` prefix `"ship_"` is the only method that works here;
    `GetComponentData(obj,"class")` returns **0 ships** (it yields sector/zone-ish strings on these objects,
    not "ship"/"station"). Capitals = `ship_l` + `ship_xl`. Roles via `primarypurpose`.
  - **Live numbers (save game_301276512, verified on dashboard):** xenon 1990 ships (1810 fight / 180 mine /
    0 trade / 71 cap — all-military + miners, exactly right for Xenon); split 772 (503 fight / 156 trade /
    85 mine / 118 cap); teladi 686; argon 639 (369 fight / 177 trade / 71 mine / 79 cap); ministry 607
    (569 fight / 25 trade — military-heavy). Role split (fight/trade/mine/build) and capital counts are all
    sane. NPCs can now reason about relative military strength + fleet composition per faction.
  - **Validation path used:** authored via Forge workspace + `/api/agent/deploy`; reloaded the live game by
    desktop-control F5→F9 (focus the X4 window first or the keypress is dropped); read back `/api/fleets`
    after the ~heartbeat. Iterated probe→production entirely against real in-game data, no guessing.

- **War losses (#10 other half) ✅ LIVE-VERIFIED in-game** — instead of hooking galaxy-wide
  `event_object_destroyed` (heavy + fog-of-war-blind), the **already-verified fleet census IS the loss
  sensor**: `upsert_fleet_strength` now diffs each faction's **fight-ship** count against the prior snapshot
  and a net decline ≥2 is recorded as a `record_loss(kind="combat")` event. The census is galaxy-wide/
  omniscient so a drop is real attrition, not visibility; a faction out-building its losses nets ~0 (correct
  for a "being ground down" pressure). Threshold ≥2 kills single-ship reclassification noise; **increases
  (building) and −1 drops record nothing** (both verified). Reuses the whole existing read path —
  `get_loss_summary` (1hr window, /50 normalize) → `conflicts_list` `losses` → dashboard **Conflicts &
  Losses** chips, AND feeds `derive_strategic_pressures` military_pressure. **Bridge-side only (Python),
  no Forge / no new in-game code.** Verified three ways: (a) synthetic HTTP against the LIVE bridge —
  argon 400→375 ⇒ loss 25, recent_losses 0.5; teladi +30/−1/−9 ⇒ only the −9 registers; (b) **real in-game
  attrition** during live play — holyorder 2, khaak 5, paranid 3 lost across census cycles (26 active
  conflicts); (c) dashboard chips render those real losses. *Known limit:* a save reload resets counts, so a
  decline spanning a reload boundary is missed (load-census reads an artificial increase) — only suppresses
  losses, never fabricates them; a non-issue in normal play.

- **Tier-3 strategic deriver (#11) ✅ LIVE-VERIFIED (keystone)** — the Strategic Pressures table and the
  Factions **mood** are now EMERGENT instead of hand-seeded. `derive_pressures` already computed the six
  pressures per faction; the missing piece was that nothing ran it live. Added `derive_all_pressures(save_id)`
  (loops every known faction → `derive_pressures` + a dynamic `_derive_mood`) and wired it into
  `relations_sync` — so it recomputes on **every 15s relations heartbeat**, right after the Tier-1 reconcile,
  off fresh substrate (economy / active conflicts / windowed war-losses / contested sectors / player rels).
  Cheap + idempotent (local SQLite). `_derive_mood` priority ladder: embattled (loss≥0.5 or mil≥0.7) →
  belligerent (mil≥0.4) → defensive (terr≥0.4) → strained (econ≥0.5) → resentful/amicable (player align) →
  watchful. Mood flows into `build_persona_context`, so representatives now *sound* like their faction's live
  situation. **Verified (live bridge, save game_301276512):** 12 factions derived; Mil 0.60–0.80 (driven by
  26 active wars), recent_losses tracking the war-loss feed (khaak 0.5, ministry 0.34, argon 0.08); moods
  differentiate correctly — khaak/ministry **embattled** (bleeding), the rest **belligerent**. Dashboard
  Strategic Pressures table + Factions moods render it (argon/alliance Align −100 = the ForceWar test maxing
  their resentment — real derived data, not seeded).
  - *Deliberately NOT fabricated (each needs its own substrate, scoped next):* **piracy_pressure** (no piracy/
    crime reader yet — left 0) and the economy **Dep. column** = `dependency_on_player`, which needs the
    **player-trade substrate** (`player_market`: how much of a faction's key_needs the player fulfills / trade
    volume) — a separate derivation, not part of the pressure substrate. Factions **aggr/risk** are static
    canon personality traits (seeded), correctly NOT derived.

**NEXT:** see the three grounded SPECs below.

---

## SPEC (pending) — remaining derivations + the auto-open fix (2026-06-24, grounded)

These are scoped for a future session. Each names the REAL tables/fields/endpoints that already exist, so
the work is "feed + derive + verify", not "design from scratch". Ground any X4 API against the unpacked
vanilla files (`DEV_ENV/Games/X4 Foundations/Files/unpacked`) — do not guess.

### SPEC 1 — Economy "Dep." column = `dependency_on_player` (player-trade dependency)
- **Goal:** fill the economy panel's **Dep.** cell (`app.js` reads `e.dependency_on_player`, 0..1) and the
  sibling `player_economic_importance`. Both columns ALREADY exist on the `economy` table and are settable
  through the economy upsert (router `economy_upsert` whitelists them). Today they're always 0.
- **Substrate (already built, only demo-seeded):** the `player_market` table
  `(save_id, ware, sector, dominance_level 0..1, supplying_enemies)` with `upsert_player_market` +
  `list_player_market` + a `/api/player_market` reader. Currently only seeded once in a demo
  (`router.py:449`), never fed from the live game.
- **Two parts:**
  1. **IN-GAME reader (Forge/Lua, ride the ~120s economy heartbeat)** — report the player's market position
     per ware. Read the player's own stations (`GetContainedStationsByOwner("player", nil, true)` — proven to
     work) → their `products`/`allresources`; for each produced ware estimate the player's supply share /
     leverage in the region, and flag `supplying_enemies` when a buyer is at war with the seller. POST to a
     new `/v1/market_sync` → `upsert_player_market`. (Hard part — ground the trade/share API against vanilla;
     a first cut can use a coarse dominance = player produces a ware a faction key-needs ⇒ 0.5+.)
  2. **DERIVATION (bridge, trivial once fed)** — in `derive_pressures` (or a small pass in
     `derive_all_pressures`): `dependency_on_player[faction] = clamp01( Σ over faction.key_needs of
     player_market.dominance_level[ware] )`; `player_economic_importance` = a broader version over all wares
     the player trades with that faction. Write via the existing economy upsert.
- **Verify:** seed `player_market` dominance for a ware that is argon's `key_need` → run derive →
  `/api/economy` argon `dependency_on_player` > 0 → dashboard **Dep.** cell populates. Then confirm the live
  in-game reader produces non-zero dominance for the player's actual stations.
- **Caveat:** the bridge plumbing is done; the real cost is the in-game player-trade reader. Don't fabricate
  dominance — leave 0 until the reader is grounded.

### SPEC 0 — Contested-sector reader ✅ LIVE-VERIFIED in-game (feeds territorial + piracy)

**DONE 2026-06-25.** Real contested sectors now derive from live ship presence → `territorial_pressure`
AND `piracy_pressure` are emergent, rendering on the dashboard. Verified on save game_301276512 (driven via
desktop-control F5/F9 reload + live bridge reads): the census reports per-sector combat-ship presence; the
bridge resolves owners + war + criminal contesters; e.g. **Silent Witness I** (argon) contested by
teladi+xenon, **Profit Center Alpha** (teladi) by argon+khaak+xenon, **Second Contact II Flashpoint**
(antigone) by xenon. Strategic Pressures Terr/Piracy columns populate (antigone 2%/2%, argon 1%/1%,
teladi 1%/1%).

**Gotchas (hard-won — read before touching this):**
- **Presence is keyed by sector NAME, not macro.** The in-game `GetComponentData(ship,"sector")` →
  name-string path yields keys like `"Argon Prime"`, NOT the macro/numeric `sector_id` the sectors table
  uses as PK. The bridge therefore joins presence→owner by **name** (and id), mapping back to the real
  `sector_id` for the upsert (never upsert under a name — it creates a phantom row). See
  `sync_contested_from_presence`.
- **Filters:** a sector owned by A is contested by B when B has **≥2 fight ships** present AND B is at war
  with A (same `-0.75` relations threshold as the Tier-1 reconcile). Idempotent: re-sets contested + clears
  stale each census. **Piracy** = the criminal slice (`CRIMINAL_FACTIONS = {xenon, khaak, scaleplate}`).
- **Diagnostics:** `/api/fleets` now returns `presence_debug` (presence_sectors / owner_matched /
  enemy_present / war_pairs / sample) — the lens that found the name-vs-id bug. Keep it.
- **Bridge-side only.** The in-game presence reader (deployed via the Forge before the fix) was correct;
  every fix was in `memory.py`/`router.py`. No Forge bug this round.

**⚠ KNOWN DATA-QUALITY CAVEAT → new SPEC 0b below.** Values are real + differentiated but **diluted ~8×**:
`SyncSectors` writes **~8 duplicate rows per named sector** (unstable numeric ids) and only **8 distinct
names resolve** (652 of 708 rows are "Unknown Sector" — mostly fog of war, expected early-game). The dedup-
by-name join handles the duplicates for detection, but `territorial = contested/owned` inflates the
denominator (owned counted 8×) → Terr/Piracy read ~⅛ of true. **Accurate values require fixing SyncSectors
(SPEC 0b).**

### SPEC 0b — SyncSectors dedup + stable keys ✅ DONE 2026-06-25 (bridge-side)
**Problem:** the sectors table had ~8 rows per named sector under different unstable numeric `sector_id`s
(`SyncSectors`' `tostring(sid):gsub` fallback isn't stable across syncs), so `territorial/piracy` read ~1/8
true. **Fix (chosen — bridge-only, no in-game reload):** `sectors_sync` → `replace_sectors_by_name`: store
exactly one row per KNOWN sector keyed by NAME (stable), skip "Unknown Sector" (fog), delete-not-in to flush
legacy/stale rows, and PRESERVE `contested_by` on survivors (never touch contested_by_json on the owner
upsert). Self-healing + authoritative each sync. **Verified:** table went 708 rows/8-dups → 7 clean rows / 7
distinct names / 0 dups; territorial/piracy now true — argon 0.25 (1 of 4 known sectors contested), antigone
& teladi 1.0 (their single known sector contested). **Design note:** territorial/piracy are now "of KNOWN
space" — fog of war means only explored sectors are tracked; the denominator grows as the player explores.
That's the honest, inherent limit. (A deeper in-game stable-key fix for unexplored-sector counts is possible
later but not needed for the experience.)

### SPEC 1b — Incremental ("trickle") ingestion + full NPC/crew tracking (NEW, major) ◐ PLANNED
**Problem (Ken, 2026-06-25):** every heartbeat the mod takes a FULL galaxy snapshot (all ships/sectors/
stations) and POSTs it at once → spikes the in-game frame AND the DB ingest. It should behave like a game's
load bar / Obsidian's indexer: spread the work over many ticks (amortized), NEAREST-to-player first,
expanding outward. Close two gaps at the same time: (1) NPCs aren't in the DB at all (only conversation-time
indexing); (2) we count ships but not the PEOPLE on them.

**Grounded API (verified, unpacked `ego_detailmonitor/menu_map.lua`):** `GetPeople2(PeopleInfo* out,len,
controllableid,includearriving)` → count; per person `NPCSeed`(uint64) with `GetPersonName/GetPersonRole/
GetPersonCombinedSkill/GetPersonSkills3/GetPersonTier(seed,controllableid)`. Ship→sector =
`GetComponentData(ship,"sector")` (already used). Player ship/sector via GetPlayerComponent (ground exact
call before coding).

**Architecture — a work-BUDGET crawler, not a snapshot:**
1. **Frontier priority (near-first, gradual outward):** *Tier 0, every tick* = the player's CURRENT sector —
   enumerate its ships + each ship's people (cheap, few objects), always fresh = who the player can meet.
   *Background crawl, budgeted* = a persistent round-robin CURSOR over all faction ships; each tick process
   only the next K ships (start K≈25): read people via GetPeople2, upsert. Cursor wraps → whole galaxy
   covered gradually over many ticks; far refreshes slowly, near (Tier 0) every tick. Round-robin gives
   "expand outward over time" WITHOUT needing a sector-adjacency graph.
2. **NPC table + upsert (closes BOTH gaps):** per person → upsert `npcs` {npc_seed, name, faction, role,
   skills, ship_id/ship_name, sector, last_seen}. Crew-of-ship is automatic (we enumerate people PER ship).
3. **Staleness + eviction (bound the data):** stamp `last_seen` each upsert; periodic prune drops NPCs unseen
   for a long window (died/left) → a ROLLING roster of currently-existing NPCs, not an ever-growing log. This
   is the safety valve against unbounded growth (tens of thousands galaxy-wide).
4. **Fold the aggregates into the same pass:** accumulate fleet_strength counts + sector presence as ships
   are visited (one amortized pass) instead of a separate full enumeration.

**Budget tuning:** K ships/tick × 15s. Start conservative, watch FPS + dashboard freshness timestamps, raise
K until just below the comfort line. The whole point: no single tick spikes.

**Phasing (each independently validatable in DB + game):**
- **Phase A = SPEC 0b first** (stable sector keys/dedup) — needed so the frontier has clean sector identity
  + accurate territorial/piracy.
- **Phase B = crawler framework** — round-robin cursor + per-tick budget on the census (no NPCs yet); prove
  coverage builds over ticks and the frame stays smooth.
- **Phase C = NPC + crew** — ride GetPeople2 on the crawl; `npcs` upsert + bridge endpoint + dashboard NPC
  panel; validate player's current-sector NPCs appear immediately, roster grows outward.
- **Phase D = staleness/eviction + tuning.**

**Open decisions:** (a) ambition — ALL NPCs galaxy-wide (big rolling table, eviction essential — Ken's stated
intent) vs. only crew within N tiers of the player (bounded). (b) Phase B REPLACES the all-at-once census vs.
runs alongside (Ken's concern implies replace).

**★ REFRAME (Ken, 2026-06-25) — this is an EVENT/MEMORY engine, not a live census.** The DELIVERABLE is the
experience: NPCs remembering "the battle of {sector} where {faction} lost {N} ships and {crew} crew," those
memories driving attitudes → economy + politics. **If we can't deliver that, the mod doesn't work.** The live
roster is only substrate. This sharpens the design and resolves the earlier spike-vs-diff tension:
- **Split CHEAP destruction-detection from EXPENSIVE enrichment.** Destruction = a COMPLETE but cheap
  snapshot of ship IDs only (GetContainedObjectsByOwner is omniscient; we already enumerate them) → diff vs
  prior → vanished ids = destroyed. Must be complete (an un-crawled ship must NOT look "gone"), but it's just
  ids, so cheap. The EXPENSIVE part (GetPeople2 crew, notable individuals, last-known sector) goes INCREMENTAL
  / near-first — it only ENRICHES ships so that when one dies we know who/what was aboard. (Clean resolution
  of red-team #4/#5: complete where cheap, incremental where costly.)
- **The diff IS the cleanup.** A destroyed ship LEAVES the live `ships` table (bounded working set = only what
  exists) and its destruction becomes a durable MEMORY EVENT: "{faction} lost {ship}+{crew} in {sector} to
  {killer}." The event attaches to who'd know (loser, allies, killer, NPCs in-sector) and rides the bridge's
  EXISTING A/B/C/D memory decay (raw → condensed fact → rolling summary → forget) — history stays bounded by
  CONDENSING, not deletion. NPCs aboard a destroyed ship → marked lost, persist only as memory. (Exactly
  Ken's "doesn't stay an existing ship, stays a memory until condensed.")
- **Battle aggregation:** many losses in one sector/short window = ONE "battle of {sector}" memory, not 50
  facts — matches how a war is recalled, avoids memory spam.
- **Drives the world:** a loss → resentment toward the killer (relationship adjust) + economic stress (lost
  trader/station) + political shift, fed through the Tier-3 deriver (already reads losses; extend to
  memory-weighted grudges).
- **Per-ship diff SUPERSEDES the count-delta war-losses (#10):** which ship-id vanished is more precise than
  "fight count dropped" (kills false losses from count fluctuation) AND yields the ship+crew+sector context
  the memory needs. Migrate losses onto the diff.

**Revised kill-tests (cheap, do FIRST — gate the whole build):**
1. **Ship-id stability** — snapshot ship ids across a few ticks; a surviving ship MUST keep its id (the diff
   depends on it). 64-bit → stringify everywhere (the sector-id precision trap again).
2. **GetPeople2 fog-gating** — call on an UNSCANNED enemy ship; if crew is fog-gated, "X crew lost" for
   distant ships falls back to last-known / crew-capacity (ship-level destruction still works — omniscient).
3. **Conversation targets present** — does the player's actual talk-to NPC (e.g. Reen Omara) appear in the
   station's GetPeople2, so the roster includes who the player meets?

**Scope (refined):** track NOTABLE individuals (captains/pilots/managers/named) + ship & crew COUNTS — NOT
every anonymous marine. That bound is what keeps this an experience, not friction.

### SPEC 1c — HOOK the game's OWN tracking (logbook events + faction data) ◐ logbook ingestion ✅ DONE
**✅ 1c-B DONE 2026-06-25 (logbook → memory, live-verified):** in-game `SyncLogbook` calls `GetLogbook(1,50,
cat)` for news/alerts/diplomacy past a per-category time cursor → POST `/v1/logbook_sync` → bridge
`ingest_logbook_event` classifies (destroyed→battle/4, war→diplomatic/4, defence→battle/3, construction→
economic/2), resolves faction, matches a known sector by name, dedups, → `world_events` (source=logbook).
Deployed via the Forge (validate 0-err → deploy). **Verified:** real game news ingested — "Construction of
Hatikvah Free League station completed in Hatikvah's Choice I", "Xenon mounting defence in Hatikvah's Choice
I", war entries — 15 events with real content + correct classification.
- **GOTCHA (cost a debug pass):** the game puts a generic LABEL in `title` ("News update:", "Emergency
  alert:") and the real content in `text`. Use `text` as the memory summary AND the dedup key — else every
  "News update:" collapses to one row (over-dedup) and summaries are useless.
- **Follow-ups (enrichment, not blockers):** sector often blank (news sectors like "Hatikvah's Choice I"
  aren't in our small owned-sectors table — improves as the player explores; add regex sector parse later);
  faction blank on label-only news (parse names from text later); cadence is ~120s (econ-throttled) — fine.
- **✅ 1c-C DONE 2026-06-25 (faction representatives, live-verified):** in-game `SyncFactions` →
  `C.GetFactionRepresentative(fid)` → `ffi.string(C.GetComponentName(rep))` per faction → POST
  `/v1/factions_sync` → bridge `upsert_faction(representative=...)` (added a guarded `representative` column
  migration). Deployed via Forge (validate 0-err → deploy). **Verified:** 13 real reps — Argon=Melissa Mettel,
  Ministry=Huritis Gobanis Trosulis VI, Scale Plate=Yalos Yayasisos Ganatos I (matches the in-game faction
  menu). Each faction now has its persistent named NPC to anchor memories/attitudes. `ffi.cdef` is global
  across the X4 UI, so `C.GetFactionRepresentative` (vanilla-declared) is callable without our own cdef.
- **Remaining 1c:** wire memories → attitude/grudge (SPEC 1c-D below), optional faction-data enrichment (HQ,
  known sectors). Then SPEC 1d injection loop.

### SPEC 1c-D — Memory → attitude/grudge attribution ✅ DONE 2026-06-25 (bridge-only, 3-channel validated)
**Built:** transition-based resentment nudges (no per-tick runaway) — `sync_contested_from_presence` nudges
the owner's resentment toward a NEWLY-contesting enemy (+12, dtrust-6); `reconcile_world_from_relations`
seeds mutual resentment on a NEW war (+15; trust stays game-owned). `build_situation_briefing` now surfaces
the faction REPRESENTATIVE + the strongest lingering grudge (resentment ≥ 25). **Honest scope:** only
attributable sources grudge (contests, wars); a fleet-delta loss has no attacker so it stays a mood/pressure
signal. Resentment decay deferred (grudges currently persist — a later pass can bleed them).
**Validated 3 ways (per Ken's required tools):** (a) **Forge ecosystem** `project/validate` 0-err/0-warn;
(b) **DB dashboard** — synthetic war+contest 15→27, and REAL save argon→xenon=12 from real contested sectors
(in-game-driven); (c) **in-game via the grounded LLM demo** — briefing shows "...representative, Melissa
Mettel" + "lasting grudge against Zyarth Patriarchy (resentment 60)", and Captain Voss's in-character replies
VOICE it ("the last hull line against the Split", "the Split are gathering again"). The remembered grudge
colours the NPC's speech — the target experience. (Also enriched the grounded-demo seed to showcase rep+grudge.)
**Next → SPEC 1d** (LLM-driven parallel injection loop): a high grudge → a validated retaliation → injected +
news. (was: ◐ NEXT)
**Goal:** make the substrate we now collect actually BEND the AI — a remembered loss/contest/war becomes a
directed GRUDGE that drives the influence engine's decisions AND the representative's persona. This is the
bridge from "we record events" → "the AI acts on them," and it sets up SPEC 1d (a high grudge → a proposed
retaliation → injection).
**Mechanism (exists):** `adjust_relationship(save_id, subject, obj, dresentment=+, dtrust=-)` writes the
bridge's directed attitude overlay (trust/fear/resentment); the influence engine + `player_alignment` already
read it. The deriver runs each heartbeat — fold attribution in there.
**Attributable sources (honest about what carries an aggressor):**
- **Contested sectors ✅ attributable:** owner A, `contested_by` enemy B → A resents B. (We KNOW both
  parties.) Strongest, freshest signal.
- **Active conflicts ✅:** A↔B at war → mutual resentment, scaled by intensity.
- **War/diplomacy logbook events ✅ when two-party:** "A vs B" → both. Destruction events usually name only
  the victim (no killer in the text) → those feed `recent_losses`/mood, NOT a directed grudge. State this.
- **War-losses ⚠ NOT directly attributable:** a fleet-delta loss has no attacker, so it drives
  military_pressure/mood (already wired), not a directed grudge. Don't fake an aggressor.
**Anti-runaway (important):** `adjust_relationship` ACCUMULATES (clamped ±100). Running every heartbeat would
peg resentment. Options: (a) small per-tick nudge + lean on the existing memory DECAY to bleed it back; or
(b) compute a TARGET resentment from current world-state (contest intensity, war) and move the value TOWARD
it (set, not add). Prefer (b) for contest/war (reflects the ongoing situation); use (a) for discrete event
spikes. Tune + cap.
**Persona surfacing:** extend `build_persona_context` — add the faction's STRONGEST current grudge + the rep
name, e.g. "As <representative>, you are bitter toward <X> after <event/contest>." So the rep VOICES the
memory in chat.
**Validate (DB + in-game):** a sector contested by X → owner's resentment toward X rises (DB); a faction at
war → mutual; then TALK to that faction in-game and confirm the rep references the grudge in its reply.
**Then → SPEC 1d:** the influence loop reads these grudges, proposes a (validated) retaliation, injects it +
posts a news entry — closing read→remember→decide→inject→read.

(original planning notes below)
### SPEC 1c (plan) — HOOK the game's OWN tracking (logbook events + faction data)
**Discovery (Ken, 2026-06-25, screenshots):** X4 already tracks the exact stuff we want, structured + filtered:
the **logbook/news** ("Xenon station in Hatikvah's Choice I was destroyed", "Terran Protectorate mounting
defence", wars, construction) and the **faction menu** (HQ, **faction representative** = a named NPC, known
sectors, licence/reputation tiers, relations). Don't rebuild detectors we can read.

**Grounded API (unpacked vanilla):**
- **`GetLogbook(startIndex, numQuery, category)`** (`ego_detailmonitor/menu_playerinfo.lua`) → the game's own
  event log; categories `all/general/missions/news/diplomacy/alerts/upkeep/tips/ticker`, queryLimit 1000.
  Entries carry title/text/time/faction (ground exact shape when building).
- **`GetFactionData(...)` / `FactionDetails` / `representative`** (`menu_diplomacy.lua` = the Factions screen,
  also `menu_encyclopedia`/`menu_docked`) → representative NPC, HQ, known sectors, relations, reputation.

**Why this LEADS (cheaper + event-driven, directly delivers the memory experience):**
- **The logbook IS the event stream we were going to hand-build.** Poll it each heartbeat for NEW entries
  (cursor on last-ingested index/time), forward new ones to the bridge as memory events. The game already did
  the detection AND the "notable" filtering, and it's inherently incremental (only new rows) — **no ship-diff,
  no spike.** It hands us the headline "battle of {sector}" / "{faction} station destroyed" events for free.
- **The faction representative is the persistent "rememberer."** A named, stable, per-faction NPC to anchor
  memories + attitudes on — far better than random crew, and exactly the voice that "remembers the war".
- **Faction-data enrichment** (HQ, known sectors, reputation tiers) deepens the world model the AIs reason on.

**Caveats (honest):** the logbook is **player-centric / notable-filtered** — it logs what's relevant to the
player, NOT every distant skirmish. That's a FEATURE for the memory experience (notable, player-relevant
events) but it does NOT replace the substrate polling; it complements it. Dedup via a last-ingested cursor
(append-only log + query window). MD events (object destroyed / war) are an even deeper signal-level hook
(event-driven, zero poll) — layer where they fire.

**Revised build order (this supersedes SPEC 1b's "crawler first"):**
- **A — SPEC 0b** (stable sector keys) — still first.
- **B — Logbook event ingestion** (NEW lead): `GetLogbook` cursor → bridge `world_events`/memory → attach to
  factions + the representative NPC → Tier-3 grudges. Cheapest path to the actual deliverable. Validate:
  blow up a station in-game → the logbook entry → a bridge memory event → a faction grudge shift.
- **C — Faction representatives + faction-data enrichment** (named rememberer NPCs into the `npcs`/`factions`
  tables via GetFactionData).
- **D — ship-id diff + crew crawler (SPEC 1b)** for FINE-GRAIN losses (which ship/crew) — now a *detail layer*
  on top of the logbook headlines, not the foundation. Gated by its kill-tests.

### SPEC 1d — INJECTION / actuation: the LLM-driven parallel influence loop (the WRITE half) ◐ PLANNED
**Intent (Ken, 2026-06-25):** the read side senses the world; this is the symmetric WRITE side — the LLM
DRIVES faction behaviour and INJECTS decisions back into the live game, in PARALLEL with X4's own sim. This
closes the loop: **read → remember → decide → inject → (becomes new events to) read.** Without it the AIs only
*observe*; with it they *act*, and the world becomes genuinely AI-driven.

**What already exists (the proven seed — build ON it, don't re-derive):**
- **Real-game injection is PROVEN for one verb:** chat → action → MD `ActionStash`/`Act_go` →
  `set_faction_relation` actually flips X4's relation (declare-war test, verified end-to-end). That MD
  dispatch path is the TEMPLATE for every injection verb.
- **The influence engine skeleton exists:** `router.review_faction` = one cycle (derive pressures →
  `scoring.rank_faction` → pick [deterministic OR LLM] → `scoring.validate_incident` Stage-3 gate →
  `add_incident` → `apply_incident_effects`). `/api/strategic/review_all` runs it for all factions on demand.
- **Logbook WRITE is proven:** `C.AddPlayerLogEntry(category,title,text)` (we already use it) → the AI's
  decisions can surface as in-game NEWS the player reads.

**The gaps to close:**
1. **Run it autonomously IN PARALLEL, amortized.** Turn `review_all` from an on-demand endpoint into a
   background loop on a cadence, **round-robin a few factions per tick** (same anti-spike discipline as the
   read crawler — writes trickle too, never all factions acting at once). The AI lives its life alongside the
   player.
2. **Route validated decisions to the REAL game, not just the shadow.** Today `apply_incident_effects` mutates
   the BRIDGE's own tables (a headless stand-in). For true injection, a validated decision must also dispatch
   to X4 via the proven MD path so the real game changes (relations today; expand the verbs).
3. **Expand the action vocabulary (injection verbs), each = an MD executor on the chat→war template:**
   war/peace/alliance (relations — proven), trade embargo / restriction, bounty on the player or a faction,
   fleet posture (defend/raid a sector), economic shift (price/subsidy), and **inject a NEWS/logbook entry**
   so the decision is visible ("Scale Plate, still bitter over Silent Witness I, raises bounties on Argon
   traders"). Start with 2–3, grow.
4. **Surface AI decisions as game news** (`AddPlayerLogEntry`) — the player SEES the world reacting, and it
   feeds back into the read side as a logbook event → memory. Loop closed.

**Safety / authority (the project's 3-layer model):** the **LLM PROPOSES, deterministic code DISPOSES, MD
EXECUTES.** Every decision passes `validate_incident` (legality / bounds / cooldown / idempotency) BEFORE any
state change; the LLM never mutates game state directly. LLM = orchestration (flavour + choice), deterministic
Python/MD = execution (consistency). Bound every verb's magnitude + a per-faction cooldown so the world can't
thrash.

**★ NO PLAYER APPROVAL (Ken 2026-06-25):** autonomous faction decisions apply ON THEIR OWN — there is NO
player-confirmation gate. A confirmation prompt breaks the living-universe illusion (friction between the
universe and the player). The deterministic validator still gates legality/bounds/cooldown, but the player is
a PARTICIPANT who reacts, not an approver. Applies to faction-vs-player too (a faction can turn on you
organically). Implemented: `review_faction(autonomous=True)` skips the old `requires_confirmation` hold and
applies; the influence loop calls it autonomously. (The player-initiated chat path keeps confirmation only
for the player's OWN proposed actions.)

**Parallelism model:** read-crawler and write-loop are two amortized trickles sharing the heartbeat budget;
neither snapshots/acts all-at-once. Tune both budgets together against FPS + DB latency.

**Phasing:**
- **✅ W1 DONE 2026-06-25** — autonomous influence loop. Bridge `influence_step` (round-robin cursor, a few
  factions per heartbeat = amortized) → each faction decides from pressures + GRUDGES via
  `review_faction(autonomous=True)` → applies to the SHADOW world model (no real-game mutation yet) → returns
  player-facing NEWS. Mod `SyncInfluence` (heartbeat `%4`, ~60s) POSTs `/v1/influence_step` → writes news to
  the in-game logbook (category `general`, so it doesn't feed back into the news ingester). Deployed via Forge
  (validate 0-err). **Validated:** Forge clean; DB — fresh save's reviewed factions all produced APPLIED,
  grudge-driven, declarative news ("Argon Federation (Rep. Melissa Mettel) is escalating tensions with Xenon")
  with NO approval wording, cooldowns blocking re-decides; in-game logbook write proven (`AddPlayerLogEntry` —
  [AI TEST] WAR entries visible), live Faction-Activity entries flow as decisions free up.
- **W2** (next) — wire validated relation verbs to the REAL game via MD dispatch (generalise chat→war) so
  autonomous decisions flip real X4 relations (no approval).
- **W3** — 2–3 more verbs (embargo, bounty, fleet posture). **W4** — tune budgets + cooldowns.

### Proving harness + test (2026-06-25) — prove the chain delivers IN-GAME
**Built:** on-demand prover — bridge `/v1/influence_prove {faction_id}` forces ONE faction to decide NOW
(cooldown bypassed via `review_faction(force=True)`), applies it, and queues the news; `influence_step` drains
the queue so the mod surfaces it. Mod `SyncInfluence` now also `Helper.showNotification`s each decision (an
on-screen toast) in addition to the logbook entry.
**Test protocol (reproducible):** POST `influence_prove` for a faction → within one mod heartbeat the decision
appears in-game (logbook entry + toast). 
**Result:** chain PROVEN end-to-end — forcing decisions then re-reading the queue shows it **drained by the
in-game mod** (the mod pulled the decisions on its heartbeat and ran the write loop = `AddPlayerLogEntry` +
`showNotification`). `influence_prove` HTTP-verified (returns grudge-driven news + incident). The write path
itself is proven (the same `AddPlayerLogEntry` produced the visible "[AI TEST] WAR" logbook entries).
**★ ROOT CAUSE FOUND (2026-06-25, confirmed in-game by Ken's logbook screenshots):** **`C.AddPlayerLogEntry`
and `Helper.showNotification` called from the mod's LUA do NOTHING** — not from the async djfhe `:send`
callback, not from the MD-raised heartbeat handler (`SyncRelations`). Only **MD-ACTION context renders UI**.
Proof: the logbook shows the "[AI TEST] WAR" entries (written by MD `<write_to_logbook>` in `ForceWar_handler`)
but NEVER any Lua-written entry — not the galaxy-news, and not even chat replies (`writeToLogbook` has been
silently no-op'ing; chat replies only ever showed in the chat *window*). Tried: write category "general"
(invalid) → "news" (valid for MD, no-op from Lua) → defer write from callback to `SyncRelations` heartbeat →
all failed to appear.
**★ THE FIX — route surfacing through MD: ✅ DONE & VERIFIED ON-SCREEN BY KEN (2026-06-25).** Lua raises
`AddUITriggeredEvent("ai_influence", "galaxynews", line)` per decision (Lua→MD path PROVEN — it's how the
suggestion wheel works), and the new MD cue `GalaxyNews` (`md/ai_influence_galaxynews.xml`:
`event_ui_triggered screen='ai_influence' control='galaxynews'` → `<write_to_logbook category="alerts"
title="'[TEST] Galaxy News'" text="$line"/>` + `<show_notification text="$line"/>`) renders both in MD-action
context. Built via the Forge (new `ai_influence_galaxynews.xml`, round-tripped clean, 0 errors), deployed to
G:+F:, `aic_uix.lua` SyncInfluence callback swapped from the no-op Lua write to the event raise. Proven:
`influence_prove` queued 5 grudge-driven decisions → next `%4` SyncInfluence heartbeat surfaced them →
**Ken confirmed the on-screen toast appeared.** The visual proof is CLOSED. (Category currently "alerts";
could try "news" later. `[TEST]` title is dev-only, dropped at ship.)
**Next (SPEC 1d-W2):** decisions still only *surface* — wire them to flip REAL X4 relations via MD dispatch,
then add verbs (embargo/bounty/fleet posture, W3) and tune budgets/cooldowns (W4).
**★ AUTO-OPEN CHAT IS NOW A HARD BLOCKER (escalate the ◐):** the chat reopens on EVERY F9 load and PAUSES the
sim until force-closed; clicks/Escape only register when X4 has OS focus (`open_application` first). It blocked
this whole test repeatedly. Fix it (SPEC 3) before more in-game iteration — it's no longer cosmetic.
**Chain status:** the autonomous loop itself is PROVEN — the in-game mod pulls the grudge-driven decisions
every heartbeat (queue drains verified many times); only the in-game *surfacing* is unbuilt (the MD-route).

### SPEC 1d-S — Logbook category ROUTING by vanilla semantics (Ken 2026-06-25) ✅ DONE + VERIFIED (3-gate)
**✅ VERIFIED 2026-06-25 across all three gates (Forge + DB dashboard + in-game):**
- **Forge diagnostics:** `project/validate` → **0 errors**; the 4-cue `ai_influence_galaxynews.xml` (one cue per
  tab) is XSD-legal — `category="diplomacy"` is an accepted writable category (was the open risk).
- **DB dashboard:** forced 10 factions via `/v1/influence_prove` → 8 surfaced tagged **`diplomacy`**, 2
  `dialogue_only` correctly suppressed (`news:null`); the in-game heartbeat then **drained the queue to empty**.
- **In-game:** the Player Info → Logbook → **Diplomacy** tab shows **"[TEST] Diplomatic Update"** entries with
  the LLM prose ("Freesplit intensifies its pressure against the Kha'ak … spokesperson Sae t'Ztk declared"),
  while the old `[AI TEST] WAR … Forced relation to -1.0` actuation entries sit correctly in **Alerts**. Routing
  + writability both proven on screen.
**Implementation:** bridge `_decision_news` returns `{text, category}` (`_decision_category`: target-directed →
diplomacy, self → news); Lua `SyncInfluence` raises `log_<category>`; 4 MD cues write to the right tab with
vanilla titles ("News update:" etc.); **feedback guard** (`note_self_authored`/`is_self_authored`, exact-text,
ship-safe) stops SyncLogbook re-ingesting our own writes.
**⚠ Known caveat (follow-up):** force-queued 9 decisions but only **2 rendered** — X4 coalesces/drops multiple
`AddUITriggeredEvent` raises sharing the same screen+control in one Lua frame before MD samples them. Bites only
when >~2 surface per heartbeat (the forced pre-queue); normal op surfaces 1-2/tick. Fix later: stagger raises
across frames, or have MD drain a per-tick queue instead of one event per decision.

----- (original spec retained below) -----
### SPEC 1d-S — Logbook category ROUTING by vanilla semantics (Ken 2026-06-25) ◐ SPEC'D
**Problem:** every surfaced entry currently dumps into **Alerts** (`md/ai_influence_galaxynews.xml` hardcodes
`category="alerts"`). Ken wants each entry filed in the tab whose vanilla meaning matches its content, and the
titles to follow vanilla's phrasing (e.g. News uses the literal prefix `"News update:"`).

**Vanilla tab semantics** (grounded from Ken's 4 logbook screenshots 2026-06-25 + the `GetLogbook` cat enum
`news/alerts/diplomacy/general/missions`; the X4 game folder isn't mounted in-sandbox so the *Alerts* writer
list is calibrated, not greppable — **confirm in-game before ship**):

| Tab (category) | Vanilla meaning (observed) | Title convention | Our content that routes here |
|---|---|---|---|
| **general** | Player status changes — rank stripped, "Reputation lost: -30", licences, blueprints. Faction name right-aligned. | plain sentence | Player-directed consequences of a decision (your standing with a faction shifted *because* of its action). |
| **news** | World/economy news — station construction, "Xenon mounting defence in …", economy. Also non-player asset losses titled "Emergency alert:". | **`News update:`** prefix (literal) | Faction posturing / world-flavor: "X is weighing its next move", build-ups, economic moves that aren't formal diplomacy. |
| **diplomacy** | Inter-faction political relations (empty in Ken's save). | faction-vs-faction phrasing | **PRIMARY target for our decisions:** escalating tensions, **war declared**, ceasefire, **peace treaty**, **economic sanctions / embargo**, alliances — anything political diplomacy. |
| **alerts** | Threats needing player attention — your ship/station under attack or destroyed, emergencies, scans. (Currently polluted by our test spam.) | urgent phrasing | A faction we angered acting *against the player* (fleet dispatched at player, bounty on player). |

**Routing rule (the decision→category map the bridge must emit):**
- `escalate_tensions` / `declare_war` / `ceasefire` / `peace` / `embargo` / `sanction` / `alliance` → **diplomacy**
- `weighing_next_move` / posture / build-up / economic-not-diplomatic → **news** (title `"News update:"`)
- decision that moves the **player's** rep/standing → **general**
- decision that sends force **at the player** → **alerts**

**Implementation (small):**
1. Bridge `_decision_news` returns `{text, category}` per decision (classify off the decision verb; default
   `diplomacy` for faction-vs-faction, `news` for self-posturing). Drain `_pending_news` keeps the category.
2. Lua `SyncInfluence` callback raises a **category-specific control** —
   `AddUITriggeredEvent("ai_influence", "log_"..category, line)` (4 controls: `log_diplomacy/log_news/log_general/log_alerts`).
   Distinct controls avoid string-parsing a packed value in MD.
3. MD: 4 cues (or one cue per control) in `ai_influence_galaxynews.xml`, each `write_to_logbook`+`show_notification`
   with the right `category=` and title convention (News cue prepends `News update:`; dev `[TEST]` marker stays
   until ship).
**Verify:** force one decision of each type via `influence_prove` → each lands in the correct tab in-game;
DB dashboard shows the category per queued decision; confirm vanilla's real Alerts contents in a clean session.
**Open Q (confirm in-game):** exact vanilla Alerts trigger set — observe what the unmodded game files there
(player under attack, asset destroyed, fuel, police) before finalizing the `alerts` routing.

### SPEC 1d-N — News CONTENT quality: grounded, contextual bulletins (Ken 2026-06-25) ◐ CODE DONE + det. verified
**Problem (Ken):** the surfaced lines were bland thought-bubbles — "terran is weighing its next move": zero
context (what/why/where), a dead end, reads like an NPC think-snippet not a news update. His bar = the vanilla
comms message *degree of information* (named office, concrete event + location, motive, hook).
**Fix (`bridge/router.py`, `_decision_news` rewrite):**
1. **Filler suppressed.** Only actions in `NEWS_VERBS` (active: escalate/de-escalate/war/peace/alliance/embargo/
   consolidate/expand/fortify) surface. Passive/no-op picks (the old "weighing" fallback) return None — non-events
   are not news.
2. **Grounded fact-bundle** (`_decision_facts`): faction + representative + mood, resentment toward target,
   the **contested sector** this faction owns that the target is contesting (the *where*), the most recent
   important **world_event** between the two (the *why*), and pressures (losses/territorial/piracy/economic).
3. **LLM-authored bulletin** (`_author_news_llm`): the player2 "Galaxy News Desk" persona writes 1-2 sentences
   from a factsheet, hard-constrained to **use ONLY the given facts, invent no ship counts/names/dates** (reuses
   the proven `npc_complete` path). Per-tick LLM budget = 2 (synchronous on the heartbeat) — the rest fall back.
4. **Deterministic fallback** (`_news_fallback`): richer template — who (+Rep.) + active verb (+correct prep:
   "embargo **on** X", "alliance **with** X") + grounded why ("amid fighting over Silent Witness I", "following
   reports that …", "after a string of costly losses"). **Verified offline** (7 decision types render
   context-rich; "hold" suppressed). Compiles clean (host source; mount tail-null artifact ignored).
**Pending verification:** LLM-authored prose + the lines appearing in-game (host-gated; bridge auto-reloaded).
Drive a forced decision and read the logbook to close. **Note:** still lands in *Alerts* until SPEC 1d-S routes
it to *News*.

### ⛔ SPEC 1e — UNIVERSAL retrieval grounding: roleRAG + GraphRAG on EVERY LLM call (Ken 2026-06-25) ✅ DONE + VERIFIED
**✅ BUILT + VERIFIED 2026-06-25.** Implementation:
- `memory.build_faction_briefing(save_id, faction_id)` — extracted the faction-level half of
  `build_situation_briefing` (mood/goal/rep, player + other-faction standings, wars, contested sectors,
  grudges, recent events) so it grounds ANY faction-facing call from just save_id+faction_id (no NPC record).
  `build_situation_briefing` now composes personal memory + this (DRY, chat output unchanged).
- `player2_client.npc_complete` — when a call carries `faction_id` but the persona isn't that faction's bound
  NPC, it now appends `build_faction_briefing` (so the synthetic "news desk" / "war council" personas get full
  faction grounding); GraphRAG (`graph_retrieve`) already fired off the same `faction_id`.
- `_author_news_llm` now sets `faction_id` on the news target → GraphRAG + faction briefing feed the bulletin
  (was a memory-less "Galaxy News Desk"). `_decision_facts` carries `fid`. `_llm_decide` already set faction_id.
**Audit (all ~9 call sites):** player-facing faction calls all carry `faction_id` and are now grounded — chat
dispatch (`_process`/`substrate_post`), suggestions (`generate_suggestions`, already RAG-grounded), decisions
(`_llm_decide`), news (`_author_news_llm`). The rest are synthetic load/stress scaffolding (`_one_player2_call`
p2-pipeline-stress, influence/probe stress) — not player-facing, intentionally ungrounded.
**Verification (3-gate, applicable ones):** Forge = N/A (bridge-only, no MD/Lua). DB dashboard = bridge healthy
(200), `influence_prove` returns LLM-authored bulletins (non-fallback). **Grounded-LLM proof** = the grounded
demo's briefing, now produced by `build_faction_briefing`, surfaced the full faction context ("Argon Federation;
goal: Hold the Xenon frontier; mood: watchful … at war with split (border raids), intensity 60% … hostile terms
with Zyarth Patriarchy … hold Argon Prime, contested by xenon") and the NPC replies cited it (Hatikvah's Choice,
the Split war, the hull-parts shortage) — the SAME path news/decisions now use. In-game rendering unchanged from
1d-S (MD/Lua untouched; bridge auto-reloaded), so live news is grounded from the next heartbeat.
**Follow-up (1e-W2, not done):** the autonomous loop still PICKS deterministically (`use_llm=False`); turning on
the now-grounded LLM bounded-option pick is a budgeted cost decision, deferred.

----- (original spec retained below) -----
### ⛔ SPEC 1e — UNIVERSAL retrieval grounding: roleRAG + GraphRAG on EVERY LLM call (Ken 2026-06-25) ◐ SPEC'D
**Hard design rule (Ken, decisive):** roleRAG + GraphRAG were installed on the bridge DB so that **every
faction-facing LLM call is grounded through the retrieval layer** — not chat only. This is the blueprint intent:
Bannerlord doc's influence core = *deterministic scoring → retrieval-based context selection → LLM for
intent/rationale → deterministic validator*; Blueprint2 §13.2 = "**For each LLM call**, include … top relevant
memory facts, recent world events, relationship summary." Applies to news bulletins, crisis messages (§5.6),
war explanations (§5.8), autonomous reactions (§3.6), and decisions — all of it.

**The gap found (2026-06-25).** `player2.npc_complete` already wires all three layers
(`build_situation_briefing` + roleRAG `retrieve_relevant` + GraphRAG `graph_retrieve`), BUT they only fire when
the call carries a real faction identity: the retrieval **keys off the persona's `npc_key`** (resolves to a
faction NPC record) and **GraphRAG additionally requires `faction_id` on the target**. Two call sites violate this:
- **`_author_news_llm` (news authoring)** — sent as a memory-less synthetic persona "Galaxy News Desk", **no
  `faction_id`** → all three layers no-op. News is written from a 7-field hand-picked factsheet, NOT retrieval.
- **`_llm_decide` (decision pick)** — sets `faction_id` (GraphRAG fires) but uses a synthetic "{faction} War
  Council" key → briefing + roleRAG find no memory. AND the autonomous loop runs it `use_llm=False`, so the
  LLM-picks-bounded-option step (the blueprint's core) is skipped entirely — decisions are pure deterministic
  top-score, ungrounded by retrieval/LLM.

**The fix — one shared "grounded faction call" helper, MANDATORY for all LLM calls.** Issue every faction-facing
call under the faction's **canonical identity**: the representative's `npc_key` + `faction_id` + a relevance
**query = the decision/topic**, so `build_situation_briefing` + `retrieve_relevant` + `graph_retrieve` all fire
and the model reasons over that faction's real memory, grudge graph, wars, and world events (same grounding the
chat NPC gets). Route `_author_news_llm` and `_llm_decide` through it; turn the autonomous decision pick into a
retrieval-grounded LLM choice among the deterministically-shortlisted legal options.
**Audit (Ken: "ground everything, all calls").** ~9 `npc_complete`/`generate_suggestions` call sites in
`router.py` (lines ~128, 212, 581, 845, 1169, 1348, 1630, 1747, 1863). Classify each: is it faction-facing? does
it carry a real `npc_key` + `faction_id`? Confirm retrieval actually fires (log the assembled `game_state_info`).
Known-ungrounded: `_author_news_llm` (845), `_llm_decide` (581). Chat path (128) is grounded — use as the
reference shape.
**Verify:** for one decision, log the retrieved context (briefing + roleRAG facts + graph subgraph) actually fed
to the model; confirm the news/rationale references retrieved specifics (a named grudge/war/event), not just the
factsheet. Validate via DB dashboard + in-game.
**Supersedes part of 1d-N:** the enriched news lines (1d-N) are DONE but currently grounded only on the
hand-picked factsheet — 1e replaces that factsheet with full retrieval grounding.

### SPEC 1f — LLM-driven EMOTIONAL factors: persona reactions write back to the substrate (Ken 2026-06-25) ✅ DONE + VERIFIED (full Level 3)
**✅ BUILT + VERIFIED 2026-06-25.** L3 is live: factions REACT in character to perceived events and the
reaction moves the emotional factors (resentment/fear/trust/mood), bounded.
- `memory`: `apply_reaction` (clamps to per-event caps, floors resentment/fear at 0, records a `reaction`
  world_event), `decay_emotions` (ages resentment/fear toward 0, rate-limited ~55s), `_cap_delta`, constants.
- `router`: `_llm_reaction` (faction reacts in character — 1e-grounded via faction_id), `_persona_scale`
  (0.6×pacifist…1.2×warlike off `biases.aggression`), `_react` (propose→persona-scale→clamp→apply, with
  idempotency-per-event + 45s per-target cooldown + deterministic overflow nudge), `react_prove` endpoint, and a
  budgeted reaction pass wired into `influence_step` (decay + ≤2 in-character reactions to fresh two-party
  world_events, BEFORE the decisions read the factors). `server`: `/v1/react_prove`.
**Verification (3-gate):** Forge = N/A (bridge-only). **Guardrails proven offline:** LLM proposing +999 → clamps
to +20; pacifist (aggr 0.1) → ~13, warlike (aggr 0.9) → cap 20; trust −999 → −15; decay floors at 0; scale
0.6..1.2. **DB dashboard:** `react_prove` writes BOUNDED deltas with in-character rationales — e.g.
alliance→khaak 0/0→9/13 ("their swarms strike deep into our frontier, fueling our hatred and alarm"),
xenon→argon 92→100 ("their hardened defenses only deepen our resolve to crush them"); the Relationships table's
Fear/Resent columns now populate from real grievances (neutral pairs correctly stay 0). **In-game:** reactions
are recorded as `reaction` world_events and shift the factors that the (already in-game-proven) decision→news
loop reads each heartbeat, so behavior is fed by freshly-felt, decaying grudges. Self-populates live via the
autonomous reaction pass.
**Note (honest):** live decay-over-time is offline-proven + wired (runs each heartbeat); a discrete in-game
"grudge faded then behavior softened" observation is emergent/continuous, not a single screenshot.
**Debt column is NOT filled by L3** — debt is owed-favours/credit, driven by the agreements/credit-transfer
actions (action-whitelist breadth, not built). Standing + Trust already populate (Trust from the game).

**🔒 LOCKED VOLATILITY (Ken 2026-06-25 — "lock the feel first, go full L3"; exposed as named constants for later tuning):**
- Per-event delta caps (pre-persona): `resentment −15..+20`, `fear −10..+15`, `trust −15..+10`.
- Hard factor bounds: resentment/fear `[0,100]`, trust `[−100,100]`.
- Persona scaling: `cap *= 0.6 + 0.6*aggression` → ~0.6× pacifist … ~1.2× warlike (then clamped to the absolute cap). This is what makes pirates react like pirates and the Alliance like the Alliance.
- Idempotency: one reaction per (faction, event). Cooldown: one LLM reaction per (faction→target) / 45s; overflow folds to a deterministic ±3 nudge (no LLM, bounds joules + spam).
- Decay (every ~60s heartbeat pass): `resentment −2`, `fear −3` (floor 0); trust drifts toward baseline by 1. Anti-spiral.
- Reaction budget: ≤2 LLM reactions per influence tick.

### SPEC 1f — LLM-driven EMOTIONAL factors: persona reactions write back to the substrate (Ken 2026-06-25) ◐ SPEC'D — TARGET = Level 3
**Intent (Ken):** the most realistic, *alive* version — a faction's emotional state is EMERGENT from its
IDENTITY reacting to events. Pirates react to a raid like pirates (opportunistic); the Alliance reacts like the
Alliance (righteous, mobilizing). Same event, different factor deltas, **because of who they are.** The LLM —
grounded by 1e in the faction's persona + memory + grudge graph — doesn't just *pick actions*; its emotional
reaction MOVES the underlying factors (resentment, fear, mood) that drive every downstream decision.

**Graduated LLM-authority model — becomes a PLAYER TOGGLE in nested mod settings (the UIX multilevel-submenu
spec) and ties to perf profiles (§19 joule budget + kill switch):**
- **L0 — Deterministic (current default):** factors + picks are pure code. Cheapest, most stable, zero joules.
- **L1 — LLM picks actions (= 1e-W2):** deterministic factors → LLM chooses among bounded legal options. Built-ready; just a cost toggle.
- **L2 — LLM sets magnitudes / proposes bounded actions:** wider authority, each step guard-railed. Scoped, not built.
- **L3 — LLM reactions drive the FACTORS (this spec, the target):** persona-driven emotional write-back. Most alive, highest cost/risk.
Higher levels cost more joules → the §19 profile + budget + kill switch gate them; the player selects the level
in nested settings.

**L3 mechanism — mirrors the action safety model, but for the FACTORS** (deterministic clamp around the LLM).
On a PERCEIVED event for faction F (a REAL substrate event — sector attacked/lost, ally betrayed, capital ship
killed, a player action):
1. Build F's grounded context (1e: persona + memory + grudge graph + the event).
2. LLM returns a STRUCTURED reaction: `{toward, sentiment, deltas:{resentment,fear,trust,mood}, rationale}`,
   colored by F's canon identity (faction_personalities traits bound the plausible range).
3. **Deterministic `validate_reaction` (the dispose half):** CLAMP each delta to a bounded per-event max;
   enforce idempotency (one event → one reaction, never re-react to the same event), cooldown, and
   persona-plausibility (a pacifist can't swing to genocidal from a single event).
4. Write the clamped deltas to `relationships` / faction `mood`; record the reaction as a world_event/incident
   so it's remembered and can surface as news.
5. **Decay — now REQUIRED:** resentment/fear age down on the heartbeat so grudges fade if not reinforced. This
   is the anti-spiral safety (was deferred in 1c-D; L3 makes it mandatory).

**Why the guardrails are non-negotiable:** an unbounded LLM→factor loop quietly wrecks saves — a faction
spirals to permanent max-hatred over nothing, or the whole galaxy converges to total war. Bounded deltas +
idempotency + decay + persona-plausibility keep it alive but safe (same deterministic-clamp-around-LLM pattern
as `validate_incident`).

**Replaces/augments:** the current FIXED transition nudges (new contest → resentment+15) become the L0 fallback
and the clamp baseline; L3 swaps the fixed delta for an LLM-colored, persona-driven one *within the same bounds*.

**Verify (3-gate):** Forge N/A (bridge-only); DB dashboard — a reaction writes a BOUNDED resentment delta + a
world_event, and decay reduces it over time; in-game — an event (e.g. a sector attack) yields a
persona-appropriate news reaction, and the faction's later behavior reflects the shifted factor.

**Scoped next, NOT this spec:** L2 (LLM magnitudes / proposed actions); the player-facing nested-settings toggle
UI + joule-profile gating (§19).

### SPEC 1g — Canon faction PERSONA biases seeded (Aggr/Econ/Risk/Dipl + Goal) (Ken 2026-06-25) ✅ DONE + VERIFIED
**Why (Ken caught it):** the Factions dashboard's Aggr/Econ/Risk/Dipl/Goal columns were blank, and — more
importantly — L3's `persona_scale` reads `biases.aggression`, which was missing, so EVERY faction defaulted to
0.5 → scale 0.9. "Pirates react like pirates" was only nominal. These columns are **canon IDENTITY** (blueprint
§12 strategic biases), distinct from Mood (the dynamic/derived state) — this supersedes the older
"derive Aggr from pressures" note for these four columns.
**Built:** `memory.FACTION_PERSONA` — canon (aggression, economic_focus, risk_tolerance, diplomacy, goal) for
~20 X4 factions (grounded in lore; e.g. boron 0.15 aggr / 0.90 dipl, teladi 0.20/0.95 econ, split 0.85,
holyorder 0.80, xenon 1.0, khaak 0.95) + a default; `seed_faction_personas(save_id)` writes them to
`biases_json` + `current_goal` (idempotent — only fills rows missing biases); wired into `influence_step`
(+ runs each heartbeat). Values exposed as constants for tuning (like the volatility).
**Verified (3-gate):** Forge N/A (bridge-only). **DB dashboard:** seeded keys (`aggression/economic_focus/
risk_tolerance/diplomacy` + `current_goal`) match the dashboard's exact column mapping (`app.js` `biasCell`),
so Goal/Aggr/Econ/Risk/Dipl now render on refresh. **L3 differentiation proven live:** same event, different
factions → `persona_scale` now varies — boron 0.69, teladi 0.72, split 1.11, xenon 1.20 (was a flat 0.9). So the
persona guardrail is real and pirates/zealots react harder than pacifist traders. Biases also feed decision
scoring (`scoring.py`), so picks are persona-flavoured too. In-game: live immediately (bridge auto-reloaded, no
X4 reload — no MD/Lua change).

### SPEC 1h — Dashboard data-quality pass + substrate-to-LLM grounding audit (Ken 2026-06-25, from a browser review) ◐ cleanup DONE+VERIFIED · 1h-D/E/F scoped
**✅ 1h-A/B/C/G DONE + VERIFIED in-browser 2026-06-25** (bridge-only; auto-reloaded, no X4 reload):
- **1h-A** ✅ `dialogue_only` no longer persists a world_event — `review_faction` returns early for the no-op
  (no incident, no apply) and `apply_incident_effects` has a dedicated no-op branch. Verified: newest
  world_events are clean (war/reaction only); the ~39 old "x: dialogue_only" rows are pre-fix stragglers that
  age out via `_prune_world_events`.
- **1h-B** ✅ not a leak — of 174 incidents, **163 applied** (the universe acting, correct) + **11 pending**
  (by-design, chat-proposed high-impact awaiting confirm). Added `prune_incidents` (caps applied at 300, keeps
  all pending) wired into `influence_step`, so it's now bounded. *(Minor cosmetic, NOT fixed: the dashboard
  panel header labels the TOTAL as "pending actions (174)" — it's mostly applied; dashboard JS, low priority.)*
- **1h-C** ✅ faction names seeded (FACTION_NAMES) — boron→Boron, freesplit→Free Families,
  hatikvah→Hatikvah Free League, khaak→Kha'ak; dashboard renders them.
- **1h-G** ✅ persona biases now in `build_faction_briefing` → fed to every faction LLM call. Verified via the
  grounded demo: briefing carries "Your character: measured, diplomatic, even-keeled (aggression 35/100,
  diplomacy 75/100). Act in keeping with it." (matches Argon's seeded biases) and the LLM ran through it.
**In-game:** bridge-only change; the grounded-LLM demo is the headless proof (CLAUDE.md's in-game gate). Live
immediately. **1h-D (economy reader), 1h-E (sectors coverage/value), 1h-F (conflict intensity) remain scoped**
(bigger builds; 1h-D = the open SPEC 1 and is what unblocks economic reasoning for the LLM).

A Chrome review of the `:8713` dashboard (2026-06-25) found the persona/relationship/reaction data healthy
(1e/1f/1g working) but several data-quality issues. Scoped here BEFORE touching code so nothing is lost mid-work.
Ken's framing: **this substrate data should be open to the LLM when it makes decisions.** (Mostly it already is —
1e feeds the faction briefing; the gap is the economy detail, which is blocked on the broken reader below.)

- **1h-A — `dialogue_only` / no-op decisions persisted as world_events (NOISE). → FIX NOW.** Entries like
  "hatikvah: dialogue_only." / "boron: dialogue_only." (importance 1) are getting written to `world_events` and
  thus become durable memories + can trigger reactions. These are the same non-events we suppress from news;
  they must not become memories either. Don't persist no-op decision narratives.
- **1h-B — Incidents "pending actions (167)". → INVESTIGATE + FIX NOW.** High count; visible rows show "applied".
  Confirm whether pending incidents are leaking / the table grows unbounded, and cap/prune if so (mirror
  `_prune_world_events`).
- **1h-C — Missing faction display names (boron, hatikvah blank; freesplit = id). → CHEAP FIX NOW.** Seed canon
  names alongside the personas (extend FACTION_PERSONA / a name map) so the dashboard + LLM use proper names.
- **1h-D — Economy panel BROKEN. → SCOPE (bigger, = the open SPEC 1 economy reader).** "Shortages" just
  re-lists "Key needs" with index prefixes (`0:Hydrogen, 1:…` — a serialization leak), Prod is a flat 100
  placeholder, exporters' "Key needs" are the raw resources they PRODUCE (mislabeled), and khaak (alien) has a
  fake economy. The reader isn't driven by real station data. **This is also what blocks economic reasoning for
  the LLM** (1h-G) — embargoes/supply-deals need real dependencies/shortages.
- **1h-E — Sectors thin. → SCOPE.** ~7 sectors synced (save has hundreds), strategic Value all 0, Player assets
  blank. Coverage + value-derivation gap.
- **1h-F — Conflicts. → SCOPE.** intensity hardcoded 100, cause generic "relations at war".
- **1h-G — Substrate→LLM grounding audit. → PARTIAL NOW + SCOPE.** `build_faction_briefing` (1e) already feeds
  the decision/news LLM calls: mood, goal, rep, player + other-faction standings, active wars, contested
  sectors, grudges, strategic pressures (incl. economic_pressure), recent events. NOT yet fed: the **economy
  detail** (dependencies/shortages — blocked on 1h-D) and the **persona biases** (Aggr/Econ/Risk/Dipl, now
  reliable post-1g). Add the persona biases to the briefing now; add economy detail once 1h-D is real.

### SPEC 1i — Economy reader fix + economy→LLM grounding (Ken 2026-06-25) ✅ MVP DONE + VERIFIED · 1i-W2 deferred
**✅ VERIFIED 2026-06-25 (3-gate):** Forge validate **0 errors** after the Lua edit. **DB dashboard:** the
`0:Hydrogen,1:…` shortages echo is GONE across all factions (rows_with_index_echo = 0), key_needs intact, a
bridge guard test (post the old list-echo → stored empty) passed. **LLM briefing (grounded demo):** carries
"Economy: …you depend on importing hullparts, energycells." + "The Commander is a major supplier of what you
need (dependency 70/100) — antagonising them risks your supply lines." → economic reasoning is now open to the
LLM. **Note:** the bridge guard cleans the RUNNING game's echoes on write (no X4 reload needed for the dashboard
fix); the Lua source fix (`shortages = {}`) takes full effect on the next natural reload.
**1i-W2 (deferred, needs in-game C-API grounding):** real shortage *severity* (per-station storage/buffer vs
demand), real `production_health` (not nst/20), a player-market dominance reader to make `dependency_on_player`
fully real for every faction (the "Dep" column), and excluding/flagging khaak/xenon from the trade economy.

**Grounded first (read-only audit).** The in-game `SyncEconomy` (aic_uix.lua) already does the RIGHT core read:
per econ-faction it enumerates `GetContainedStationsByOwner`, unions products (outputs) + allresources (inputs),
and computes `key_needs` = inputs the faction does NOT itself produce (real imports, real ware names) +
`market_status` exporter/importer. So the embargo/supply LEVER (what a faction depends on importing) is already
real. The dashboard mess is two bugs, not a missing reader:
- **The bug:** `aic_uix.lua` line ~477 sets **`shortages = key_needs`** — shortages is just an echo of the
  imports, and the dashboard renders that list as `0:Hydrogen, 1:Methane…`. Also `production_health = nst/20`
  is a crude station-count proxy (flat-ish 100), and khaak/xenon are in ECON_FACTIONS so aliens get a "trade
  economy".
**Build (MVP — uses the data we already have, grounded):**
- **1i-A (Lua, via Forge):** stop the echo — `shortages = {}` (honest empty until real severity exists). Keep
  the real `key_needs`/`market_status`. Deploy via Forge; validate XSD; reload to verify. (Real shortage
  *severity* needs per-station storage reads — deferred to 1i-W2.)
- **1i-B (Bridge):** feed the REAL economy into `build_faction_briefing` so faction LLM decisions reason about
  trade leverage — "You are an importer; you depend on importing Hull Parts, Energy Cells, …; the Commander is
  your dominant supplier of X (leverage)" — wired off `get_economy` (key_needs, market_status, shortages,
  dependency_on_player). This is the actual ask: economic reasoning open to the LLM.
**Deferred → 1i-W2 (needs more in-game C-API grounding):** real shortage severity (station storage/buffer vs
demand), real `production_health`, a player-market dominance reader to make `dependency_on_player` fully real
(the §-"Dep" column), and excluding/flagging khaak/xenon. Ground vs vanilla UI + Forge catdat before building.
**Validate (3-gate):** Forge (Lua validate ok:true) · DB dashboard (economy panel: real key_needs, shortages
blank not echoed, market_status) · in-game (reload; SyncEconomy posts clean data; LLM briefing carries economy).

### ⛔⛔ SPEC 1d-W2 — ACTUATION: autonomous decisions change the REAL X4 galaxy (Ken 2026-06-25) ✅ DONE + VERIFIED IN-GAME
**✅ PROVEN 2026-06-25 (the living universe is real).** Forced 6 Teladi→Argon escalations → the influence-log
now shows **7 `source="mod_dispatch"` entries** (Teladi→Argon "at war", plus an autonomous Kha'ak→Freesplit) —
that source = the write-back from the ACTUAL `set_faction_relation`, so **Teladi & Argon (normally Commonwealth
allies) are genuinely at war in the live save** because the LLM influence loop decided it. Not shadow. Debug log
(read via the Forge's game-log watcher) confirms `On_action` fired 6× with 0 errors.
**ROOT CAUSE = THREE bugs in `On_action` (md/ai_influence_contract.xml), all found by GROUNDING against the
proven `On_suggestions` table-reader, not guessing:**
1. **not `instantiate="true"`** → a cue with an event condition fires ONCE then completes forever; it had been
   dead since one early firing. (News cues worked because they ARE instantiate.)
2. **missing `namespace="this"`** → instantiated cues need it for per-instance `$`-var scoping.
3. **THE real one: Lua-table keys read as `$act.faction` instead of `$act.$faction`.** In X4 MD, a Lua table
   passed via `event.param3` is keyed with `$table.$key`; `$act.faction` looks for a non-existent *property*,
   so `$act.relation?` was always false → the relation block skipped SILENTLY (no error). The proven
   `On_suggestions` reader uses `$d.$l1`/`$d.$n` — that's what tipped it off.
**The build (bridge `_decision_action` + influence_step/prove `actions` + Lua `SyncInfluence` raising
`AddUITriggeredEvent("ai_influence","action", freshTable)`) was correct from the start; the MD cue was the
blocker.** Forge validate 0 err each step; the Forge debug-log watcher (`/api/agent/log-file-tail`) was the
instrument that proved firing. **Lesson:** when in-inspection fixes don't confirm, instrument + read the debug
log (Forge watcher) — and ground new MD against a PROVEN cue in the same mod.
**Guardrails live:** bounded Δ (escalate −0.15 … declare_war −0.40), clamp [-1,1] in MD, ≤2 dispatches/tick,
per-pair cooldown. (Kill-switch config flag = follow-up.)

----- (earlier honest in-progress notes retained below) -----
### ⛔⛔ SPEC 1d-W2 — ACTUATION: autonomous decisions change the REAL X4 galaxy (Ken 2026-06-25) ◐ WIRED, real change NOT yet confirmed
**HONEST STATUS 2026-06-25 (first attempt):** the wiring is in (bridge `_decision_action` emits
`{type:adjust_relation,faction,target,relation:Δ}`; influence_step/prove carry `actions`; Lua `SyncInfluence`
raises `AddUITriggeredEvent("ai_influence","action", tbl)` per dispatch; the `On_action` MD cue already does the
real `set_faction_relation`). Forge validate 0 err; bridge healthy; reloaded X4.
**BUT actuation is NOT proven:** queued 5 Teladi→Argon escalations → the 5 NEWS entries surfaced in-game (toast
seen), but **no "WAR: …" crossing alert and the Influence-Log of mod-caused changes is EMPTY** → no confirmed
real relation change. The dashboard "Teladi↔Argon at war" is the SHADOW model (reactions' resentment), not
verified real X4. **Do NOT claim actuation works on shadow data.**
**Key clue:** the 5 news events (same AddUITriggeredEvent path) ALL rendered, so it's NOT simple coalescing. The
action path differs in passing a TABLE as the event value (news passes a string). The CHAT action path passes a
table to the SAME `On_action` cue successfully — so `On_action` works; the autonomous table-handoff specifically
isn't landing. **NEXT: read the X4 debuglog** (does On_action fire? the unhandled-else `debug_text`? a type
mismatch? is event.param3 the table or nil?) — ground it, don't guess. Candidate fixes once grounded: ensure the
Lua passes a proper table (vs JSON quirk), or batch dispatches into one event the MD iterates, or 1 action/tick.
**Validate bar stays:** a real relation flip you can SEE (faction menu / fleets / WAR alert), not a shadow row.

**ATTEMPT 2 (2026-06-25) — STILL NOT FIRING; STOP GUESSING.** Grounded fix: the Lua now rebuilds a FRESH plain
Lua table (`{type=..,faction=..,target=..,relation=tonumber(..)}`) before `AddUITriggeredEvent`, mirroring the
proven CHAT action path exactly (it passes a built table, not the raw `getJson` table). Forge 0 err, reloaded,
queued 5 Teladi→Argon escalations → **influence-log STILL empty, no WAR crossing.** So the table-shape was not
(or not the only) cause. Two fixes, zero confirmation = stop inspecting, instrument. **NEXT (decisive, 1 cycle):**
add a debug write at the TOP of `On_action` (`write_to_logbook '[DBG] On_action type=' + $type`), reload, send
ONE escalation: (a) line appears → event reaches the cue, bug is downstream (faction.{$id} resolution? relation
type? set_faction_relation?); (b) no line → the autonomous `AddUITriggeredEvent("action", tbl)` isn't reaching
`On_action` at all (control mismatch? a competing cue? table value not surviving param3 for THIS event). Then
fix the pinpointed cause. **Also:** do NOT call `/v1/influence_step` manually during the test — it drains the
pending actions the in-game heartbeat should consume (competes with the game).


**The finding that makes this SMALL:** the real-X4 actuator ALREADY EXISTS and is proven. `On_action` in
`md/ai_influence_contract.xml` (`event_ui_triggered screen='ai_influence' control='action'`) takes an action
`{type, faction, target, relation}`, resolves both factions (`faction.{$id}`), applies a relation DELTA
(`adjust_relation`) or absolute (`set_relation`), **clamps [-1,1]**, calls the real `<set_faction_relation>`
(THIS is what makes X4 fleets actually fight), writes the change back to the bridge DB
(`AIChat.relation_report`), and fires **WAR/PEACE logbook+notification ON THE THRESHOLD CROSSING** (war at
rel ≤ -0.10: "Hostilities have begun"). Today **only the CHAT path reaches it.** The autonomous loop applies to
the SHADOW DB only and never dispatches a real action — that one missing wire is why nothing happens in-game.

**The build (small — the MD actuator needs NO new code):**
- **Bridge (`influence_step`):** for relation-affecting decisions, emit an ACTION dispatch beside the news:
  `{type:"adjust_relation", faction:fid, target:tid, relation:Δ}`. Locked bounded Δ per action:
  escalate_pressure −0.05 · declare_war −0.15 · de_escalate +0.05 · sue_for_peace +0.10 · form_alliance +0.10
  (self actions consolidate/expand/fortify + embargo → no relation dispatch yet). Budget ≤2 real
  dispatches/tick; per-(faction→target) cooldown.
- **Lua (`SyncInfluence`):** for each action dispatch, `AddUITriggeredEvent("ai_influence","action", tbl)` →
  the existing `On_action` cue does the REAL change + write-back + war/peace news.

**GUARDRAILS (autonomous real-SAVE changes — non-negotiable):**
- Bounded Δ (≤0.15/dispatch) + clamp [-1,1] → relations move GRADUALLY; wars BUILD over minutes, never instant.
  The grudge/persona substrate decides DIRECTION; magnitude is capped.
- **Kill-switch:** `mod_config.json` `autonomous_actuation` flag (Lua checks before raising action events) so it
  can be paused instantly. (Hotkey toggle = follow-up.)
- Per-tick budget + per-pair cooldown (no save-reshaping spikes). **Disposable-save discipline** — it PERMANENTLY
  changes the save; validate on a throwaway first.

**KEY DECISIONS (Ken's call):** (1) player-targeting — can autonomous factions change relation toward the PLAYER
too, or faction-vs-faction ONLY? (2) pace — Δ sizes above = gradual simmer vs punchier. (3) kill-switch default.

**Validate (the REAL bar this time):** Forge (MD validate ok) · DB dashboard (Influence Log mirrors the real
relation via write-back) · **IN-GAME = the actual bar: a real X4 relation flips in the faction menu + fleets
engage on screen + the WAR-crossing notification, on a disposable save.**

### ★ IMMERSION / presentation rule (Ken 2026-06-25)
Player-facing text MUST read like vanilla X4 — **no mod attribution, no "this is the mod" framing.** The
decision news lines already comply (just faction names + actions, e.g. "Argon Federation is escalating tensions
with Xenon"). **Dev-only:** a `[TEST]` marker (logbook title "[TEST] Galaxy News", notification title) so we
can tell our output apart during development — **dropped at ship** (then it reads as ordinary galaxy news).
Audit all player-facing strings (logbook titles, notifications, chat) against this before release.

### SPEC 0 (original plan, now done) — Contested-sector reader
**Why this first:** data audit (2026-06-24) found `sectors.contested_by` is null for all **619** sectors, so
`territorial_pressure` derives to 0 everywhere despite 26 live wars — and the cheap piracy proxy (SPEC 2) is
blocked on the same missing field. Player owns ~0 stations so Dep (SPEC 1) can't be positively validated
in-game yet either. The contested reader unblocks the territory dimension with REAL, differentiated war data.

**Grounded API (verified in unpacked vanilla):** `GetComponentData(shipObj, "sector")` returns a ship's
sector id (used throughout `ego_detailmonitor/menu_map.lua`, `ego_targetmonitor/targetmonitor.lua`). The
census already enumerates each faction's ships via `GetContainedObjectsByOwner(fid)` (proven).

**Approach (minimal — reuse the census loop, push the logic to the bridge):**
1. **In-game (Forge/Lua, in the EXISTING fleet-census loop):** while iterating each faction's fight-ships,
   bucket presence by sector → build `presence[sector_id][faction] = fightCount`. Add it as a `presence`
   field on the existing `/v1/fleets_sync` payload (no new MD event / no new endpoint). Throttled with the
   census (~120s). Cost: one `GetComponentData(ship,"sector")` per fight-ship — heavy but throttled; if too
   heavy, cap to capitals + a sample.
2. **Bridge (`fleets_sync` handler):** for each sector in `presence`, look up `owner_faction` (sectors table,
   already synced) + faction relations (already synced); `contested_by = [f for f in present if f != owner
   and is_hostile(f, owner)]`. Upsert `sectors.contested_by`. `territorial_pressure` (already wired in
   `derive_pressures` as contested/owned) then goes live automatically on the next heartbeat.
3. **Piracy fold-in (cheap, once contested_by exists):** `piracy_pressure` = fraction of owned sectors whose
   `contested_by` includes a criminal faction `{xenon, khaak, scaleplate}` (confirm ids vs vanilla
   `libraries/factions.xml`).

**Validate:** (DB) POST synthetic presence → confirm `contested_by` + territorial_pressure populate via HTTP;
(game) deploy the Lua via the Forge, F9-reload, confirm real `contested_by` appears for frontier sectors and
the dashboard Terr/Piracy columns differentiate (xenon-frontier factions high) — cross-check a contested
sector on the in-game map. **Log Forge friction in the Forge ROADMAP.**

### SPEC 2 — `piracy_pressure` ✅ DONE (folded into SPEC 0: criminal slice of contested_by). Richer later reader optional.
- **Goal:** fill the Strategic Pressures **Piracy** column (`strategic_state.piracy_pressure`, 0..1).
- **Cheap path (no new in-game reader — reuse the `sectors` substrate):** in `derive_pressures`, compute
  `piracy_pressure[faction] = (# of faction-owned sectors whose `contested_by` includes a CRIMINAL/pirate
  faction) / (# owned sectors)`. Criminal set ≈ `{xenon, khaak, scaleplate, ...}` (confirm the exact pirate/
  criminal faction ids against vanilla `libraries/factions.xml`). This differs from `territorial_pressure`
  (which counts ALL contests) by filtering to crime factions.
- **Richer path (later):** an in-game reader counting hostile/criminal ship presence or police-kill events in
  a faction's sectors. Heavier; only if the proxy proves too coarse.
- **Wire:** add alongside `territorial_pressure` in `derive_pressures` (so `derive_all_pressures` picks it up
  every heartbeat). **Verify:** a faction with a sector `contested_by` xenon → `piracy_pressure` > 0 →
  dashboard Piracy cell. **Caveat:** it's a proxy for *territorial* crime pressure, not trade piracy —
  document the approximation in-code.

### SPEC 3 — Chat auto-open-on-load ✅ FIXED 2026-06-25 (root cause found, NOT the earlier hypothesis)
- **Symptom:** the "Comm-Link: Argon Officer" window reopened on EVERY F9 load and PAUSED the sim — it
  sabotaged every in-game test (and only force-closes when X4 has OS focus: `open_application` first).
- **REAL root cause (grounded in `md/ai_influence_hotkey.xml`, not the menu-restore hypothesis):** the
  leftover hotkey scaffolding had a cue NAMED `On_Hotkey` whose CONDITION was
  `event_cue_signalled cue="md.Setup.Start"` — `md.Setup.Start` fires on every GAME LOAD, so on each load the
  cue ran `<run_actions ref="md.ai_influence_chat.Open_chat" target="'Argon Officer'">` → raised `AIChat.open`
  → opened the chat. It legitimately set `_openRequested=true`, which is exactly why the `onShowMenu` guard
  never caught it (it wasn't a menu-restore at all — it was an active open). The `target='Argon Officer'`
  literal matched the window title precisely.
- **Fix:** in the Forge workspace, disabled (`includeInBuild=false`) the `Open_chat` action node under the
  `On_Hotkey` cue, then validate (0-err) + deploy. The regenerated `ai_influence_hotkey.xml` now has
  `On_Hotkey` with only `<conditions>` and NO `<actions>` → fires harmlessly on load, never opens the chat.
  `Register_Hotkey` left intact (its `$onPress=On_Hotkey` ref stays valid). Hotkeys remain inert (Ken doesn't
  want them) — the dead `shift+c` registration is harmless.
- **VERIFIED:** clean F9 load → NO chat window, sim runs normally. **Lesson:** grounding in the actual MD
  (not the menu-restore theory) found it in one pass; the `_openRequested` guard was a red herring.
- **Bonus finding:** X4 menu clicks/Escape only register when X4 has OS FOCUS — `open_application` X4 before
  any in-game click during agent testing.

## 2026-06-24 — Conversation→gamestate dispatch FIXED + verified in-game · world-model wiring · personal-relationship spec

### The missing link is closed ✅ (verified end-to-end, driven start-to-finish via desktop control)
Talking to an NPC now changes the real game. Declared war on the Teladi in-chat → X4's `argon↔teladi`
relation flipped to **-1.0** → the 15s heartbeat read it back as `Live (game): at war (-1.00)` and KEPT
it (every prior attempt reverted because the game never actually changed). Confirmed by a `mod_dispatch`
influence-log row (only written when MD's executor actually ran `set_faction_relation`).

**Root cause (found via X4's debuglog, read through the Forge `game-log/status` endpoint):** the dispatch
handed MD a Lua **table** through `AddUITriggeredEvent`. X4 silently drops a table third-arg — the MD
cue never fired at all (no "On_action fired" line in the log, despite the Lua "DISPATCH" line). Vanilla
**only ever passes scalars** there. Fix: send the action as **separate scalar ui-events**
(`act_faction`, `act_target`, `act_go="war"/"peace"`); a small MD `ActionStash` + `Act_go` cue
reassembles and executes them. Also fixed: `On_action` had no `instantiate="true"` (one-shot), and the
Forge cross-file check caught a third Lua file (`ai_influence_test.lua`) still emitting the dead event.
**Lesson (durable):** Lua→MD structured data must be scalar ui-events (or a blackboard), never a table.

### Self-verifying confirm loop ✅
The chat confirm no longer says a blind "Dispatching." It POSTs to the bridge, which commits the change
and returns the **real committed DB row**, echoed in-chat: *"[World updated] Argon Federation -> Teladi
Company: now at war (-1.00), was +0.10. Committed to the database."* The player sees exactly what hit the
database, no separate verification step.

### Individual NPC skills ✅ (the five crew skills, grounded read)
Walk-up NPCs now carry their real per-skill values (piloting/management/engineering/boarding/morale,
0-15) read in Lua via `GetComponentData(npc,"skills")` — exactly how the vanilla crew menu does it.
Flows MD→Lua→bridge→`npc_stats.skills`, feeds the `_identity_line` persona biography ("Skills: morale
★★☆, boarding ★★☆…") and the dashboard. Combined-skill (0-100) drives only the persona descriptor, not
a displayed stat (per Ken). Verified live: Rina Bekker = morale 7 / boarding 6 / piloting 6 / eng 1 / mgmt 0.

### Tier-1 world model ✅ (derived from synced relations — pure bridge, no game read, self-maintaining)
`memory.reconcile_world_from_relations()` runs on every heartbeat + dispatch (idempotent, transition-only):
- **Conflicts** = any faction pair at war in the relations we already sync → 25 live (incl. X4's own
  argon↔xenon/khaak and the player's wars).
- **World Events** = durable history emitted on each war/peace transition (25 records).
- **Agreements** = ceasefire row on a war→peace transition.
- **Faction names** = carried over from the canon harvest (12 named) — no game read.
This lit up four dead dashboard panels from data already flowing.

### Tier-2 territory — sectors ✅ wired (grounded C-API read; needs in-game verify)
Sector ownership isn't cleanly exposed to MD, so Lua reads it like the vanilla faction library:
`C.GetNumSectorsByOwner` + `C.GetSectorsByOwner(buf,n,fid)` per known faction → `GetComponentName` →
POST `/v1/sectors_sync` → `upsert_sector`. Raised from the worldsync alongside relations. Forge-validated
+ deployed; pending an in-game reload to confirm rows populate.
**Still Tier-2 open:** ship-loss events (feeds `recent_losses` pressure) — needs the destruction-event
grounding next.

### Tier-3 — strategic deriver (NOT built; the actual "AI Influence brain")
Faction goal/mood/aggression/risk/diplomacy + strategic pressures + economy *meaning* are **derived**,
not read: a deriver→world-model→review loop computes them from the Tier-1/2 raw data. Biggest remaining
build. Now has rich inputs (wars, conflicts, territory) to reason over.

---

## SPEC — Nested command menus (UIX multilevel submenus) · DECISION: use nesting (2026-06-24)

**Decision (Ken): the influence/command UI WILL use nested submenus** (multilevel, via UIX — kuertee
`ws_3477279743`, which has supported them for a long time per Chem O'Dun). **Conversation stays
free-form** — the LLM's strength is parsing typed intent, so don't bury that under menus. Nesting is
ONLY for the **structured-action side**, where the player must pick an exact verb + target/params and
the LLM shouldn't guess.

**Shape — a command tree hung off the chat:**
- **Diplomacy →** declare war · broker peace · alliance · demand tribute
- **Economy →** embargo · supply deal · lift sanction · fund production
- **Military →** request escort · fund fleet · stand down

…then drill one level deeper to **pick the target** — a faction / sector / fleet, sourced from the data
we already sync (relations, sectors, fleet_strength). Target-picking is exactly where a precise menu
beats free text.

**Why it helps (not chat polish):** as the action vocabulary grows past war/peace, a flat list becomes
unusable; nesting organizes verbs by domain and makes target selection unambiguous. Same UIX multilevel
capability also powers the ME-wheel, so it de-risks that too.

**Cost / tradeoff:** adopting UIX submenus makes **UIX a hard dependency** (already installed as a dep).
The comm-link is on the base standalone-menu API; the nested command tree is the one piece that leans on
UIX. Ground the exact UIX submenu API against `x4-mod-ui-extensions` before building (same as the
readers). Proven first in the `x4_arcade` blueprint (game-select → board → results).

## SPEC — Injected personal relationships (NPC ↔ player), DB-tracked

**Problem.** X4 tracks *faction* standing (which we sync) but has **no concept of a personal
relationship** between an individual NPC and the player. Rina liking or resenting *you* specifically —
remembering that you spared her, threatened her, paid her — does not exist in the game. We own that
layer entirely in our DB and inject it as strict persona context.

**What we already have to build on:** the `relationships` table already has `trust / fear / resentment /
debt / standing` columns (currently used at the *faction* grain), and each NPC already has durable
`facts` + a rolling `summary`. So this is mostly *grain change + an update rule*, not new infrastructure.

**Design:**
1. **Per-NPC affinity record** (keyed by `npc_key`, distinct from faction rows): `trust`, `fear`,
   `resentment`, `respect`, `warmth` (−100..100 each) + a one-line `disposition` ("wary but indebted").
   Seeded at first contact from faction standing (an Argon marine starts where Argon-player starts) then
   **diverges per personal history**.
2. **Strict prompt injection.** `build_situation_briefing` gains a mandatory block:
   *"YOUR PERSONAL FEELINGS TOWARD THE COMMANDER (these override faction politics): trust LOW, resentment
   HIGH — they threatened your crew at Hatikvah. You are curt and guarded. Do NOT be warm."* Phrased as
   a hard behavioral constraint, not flavor, so the model actually acts on it.
3. **Update rule (model-scored, the "tracks based on interactions" part).** After each conversation,
   a cheap LLM pass (reuse the summarizer call) rates the exchange on a fixed rubric — *did the player
   threaten / flatter / help / betray / pay?* — returning small signed deltas (e.g. `resentment +15,
   trust −5`). Deltas are clamped + decayed over time (old slights soften; importance-5 events like
   betrayal/rescue are near-permanent — same decay model as memory facts). Big swings also write a
   `world_event` and a durable `fact`.
4. **Cross-NPC leakage (later).** Crew of the same ship/faction share a *fraction* of strong signals
   ("word got around that you spaced a prisoner"), so a reputation forms without every NPC needing a
   direct interaction.

**Why it's safe + grounded:** zero game reads, zero new game-API risk — it's our own ledger over our own
conversation data. The only "model interaction" dependency is the post-turn scoring pass, which already
exists in shape (the topic-summarizer). **Verify:** threaten an NPC → next turn the persona is visibly
colder + the affinity row shows `resentment` up; be generous → it warms. Build order: affinity record +
seeding → strict injection → post-turn scoring → decay → cross-NPC leakage.

---

## SPEC — Data completeness: every blank / undefined dashboard field

Every panel field that is currently blank, zero, or placeholder, with its **source class** and grounding
status. Legend: **[READ]** live from X4 via a Tier-2 reader · **[DERIVE]** computed by the Tier-3
strategic engine · **[EMIT]** written when the influence system acts · **[CANON]** filled from the
harvested lore. Grounded C-API helpers confirmed in vanilla: `GetSectorsByOwner` (done),
`GetContainedStationsByOwner(faction, sector)`, `GetFactionData(id, field)`.

### Sectors / Territory (owner ✅; rest blank)
- **Name** — "Unknown Sector" for undiscovered sectors (X4 fog-of-war). **[CANON]** map sector macro →
  canonical name from the lore harvest so names exist pre-exploration; keep the live name once known.
- **Contested by** — **[READ→DERIVE]** for each sector, `GetContainedStationsByOwner` (and a ships-by-owner
  read — *needs grounding*) across factions; contested when ≥2 mutually-hostile owners have presence.
- **Value** (strategic_value 0..1) — **[READ→DERIVE]** station count (`GetContainedStationsByOwner`, grounded)
  + resource richness + gate connectivity, normalized.
- **Player assets** — **[READ]** `GetContainedStationsByOwner("player", sector) > 0` (+ player ships in sector).

### Economy — meaning (entirely empty) — FULL SPEC

X4's economy is **station-level**: every station runs production modules that turn input wares into
output wares, funded by a supply budget. A faction's economy is the aggregate of its stations. There is
no single "faction economy" call — we enumerate stations and roll them up. Read path is **grounded** in
the vanilla UI (only the faction-wide station enumeration needs a final confirm; `GetContainedStationsByOwner`
per sector already works since sectors are synced).

**Grounded reads (Lua C-API, confirmed in `x4-mod-ui-extensions`):**
- `GetContainedStationsByOwner(faction, sector)` → a faction's stations in a sector (iterate our synced sectors).
- `GetComponentData(station, "wares")` → list of `{ware, amount}` the station holds/yields (`amount>0` = producing).
- `GetProductionModuleData(module64)` → what each production module makes + its input wares (→ consumption).
- `GetSupplyBudget(station)` and `GetTradeWareBudget(station)` → money the station has to buy inputs / trade (→ economic health).
- `GetWareData(ware, "name","groupID","groupName","productionmethods")` → ware identity for naming/grouping.
- `GetStorageData(station)` → storage capacity + current fill (→ surplus vs shortage signal).

**Per-faction rollup (what each column means + how it's filled):**
- **Faction** — row key (the owning faction id, already known).
- **Prod.** (production) — **[READ]** union of output wares across the faction's stations, with rates
  (sum of module outputs). "What this faction makes."
- **Key needs** — **[READ]** union of *input* wares its production modules consume (`GetProductionModuleData`
  inputs) — "what it must buy/source to keep producing."
- **Shortages** — **[READ→DERIVE]** input wares where demand > local supply (need ware not produced by the
  faction itself, or storage persistently low / supply budget starved). The deficit set.
- **Dep.** (dependencies) — **[DERIVE]** for each shortage ware, *who supplies it* — the faction(s) that
  produce that ware. This is the strategic lever: "Argon depends on Teladi for X" → a trade embargo or war
  with that supplier becomes a meaningful influence action. Derived by cross-referencing Shortages against
  every faction's Prod.
- **Market** — **[READ→DERIVE]** aggregate economic posture: total supply/trade budget across stations
  (`GetSupplyBudget`+`GetTradeWareBudget`) as a wealth proxy, plus net exporter/importer flag (Prod vs
  Key needs balance). Feeds the Factions panel **Econ** column and the **economic_pressure** strategic metric.

**Bridge side:** `upsert_economy(save_id, faction_id, **fields)` already exists — store `production`,
`key_needs`, `shortages`, `dependencies` (json lists) + `market`/`wealth` scalars. New endpoint
`/v1/economy_sync` mirrors the relations/sectors pattern.

**Cadence + cost:** enumerating every station and module is **heavy** — do NOT run it on the 15s relation
heartbeat. Run economy sync **on load + every ~120s** (own cue), and cap work per tick. Economy changes
slowly, so low frequency is fine.

**Open grounding (do before building):** confirm a faction-wide station list (vs per-sector union),
and the exact module input/output read on `GetProductionModuleData`. Ground the same way sectors were —
vanilla UI source + Forge catdat — then build reader → `/v1/economy_sync` → `upsert_economy`, validate in
the Forge, verify in-game. **Why it matters most:** Dependencies/Shortages are the data that makes
*non-war* influence verbs (embargoes, supply deals, blockades) strategically meaningful.

### Strategic Pressures (empty — the Tier-3 deriver's core output) — FULL SPEC

This panel is the **output of the strategic engine** (the "AI Influence brain") — not read, not emitted,
but **computed**. Each cell is a 0..1 pressure scalar per faction. They are the bridge between raw world
state (relations, conflicts, sectors, losses, economy) and faction *behaviour* (mood, aggression, risk,
and which influence actions the engine proposes). Nothing here populates until the deriver exists; the
"Run review cycle" button is its manual trigger.

**The deriver = a review loop (deriver → world-model → review).** One pass:
1. **Gather** raw inputs already in the DB (relations, conflicts, sectors, war_losses, economy).
2. **Compute** each pressure per faction with *deterministic* formulas (below), clamped 0..1.
3. **Roll up** pressures → the Factions panel's strategic columns (mood/aggr/risk/econ) + the Incidents
   queue (proposed actions the engine would take), via `upsert_strategic_state` + `upsert_faction`.
4. **(optional) Narrate** goal/mood text with one cheap LLM call per faction (deterministic numbers first,
   LLM only for the human-readable label — keeps it cheap + reproducible).
Runs **on a cadence (~60s) + on demand** (the button → `POST /api/strategic/review`). Deterministic, so
re-running is idempotent and testable (a selftest can assert formula outputs on a fixed fixture).

**Per-pressure definitions (formula · inputs · grounding):**
- **Mil** military_pressure — besiegement. `f(active_conflicts_involving_faction, enemy_force_ratio on
  contested borders)`. *Inputs:* conflicts (✅ have), force strength (needs a fleet-strength read — partial).
  **Computable now** in a crude form from conflict count alone.
- **Econ** economic_pressure — economic strain. `f(shortage_count, trade-route disruption on contested
  supply lines, low supply budget)`. *Inputs:* Economy panel (**reader not built**) + contested sectors.
- **Logi** logistics_stress — overextension. `f(supply-line length production→front × contested fraction,
  multi-front spread)`. *Inputs:* sectors (✅) + economy (not built).
- **Losses** recent_losses — attrition. **[READ→aggregate]** `get_loss_summary()` already normalizes the
  `war_losses` table; just needs the **ship-loss feed (Tier-2, not built)** to fill it.
- **Terr** territorial — ground being lost. `f(contested_or_lost_sectors / owned_sectors)`. *Inputs:*
  sectors (✅ have) + contested derivation. **Computable now** once contested is derived.
- **Piracy** — crime drag. `f(criminal-faction (scaleplate/freesplit/yaki) presence in faction sectors,
  attacks on its trade)`. *Inputs:* sectors + ship presence (partial).
- **Align** alignment — net stance toward the player. `f(faction↔player relation, recent influence-log
  events for/against them)`. *Inputs:* relations (✅) + influence_log (✅). **Computable now.**

**Downstream consumers (why the pressures matter):**
- **Factions panel:** Mood = argmax pressure ("desperate" if losses/mil high, "confident" if all low);
  Aggr = f(mil + active wars + canon temperament); Risk = f(losses + terr + multi-front); Econ = from economy.
- **Influence engine:** pressures gate *what the faction will agree to*. A faction under high Mil/Losses
  accepts a ceasefire it would otherwise refuse; one with low pressure rejects your war proposal. This is
  what turns the dispatch layer (war/peace verbs, proven) into a *strategic* system instead of a cheat menu.
- **Incidents queue:** the engine writes proposed autonomous actions here (faction X *would* attack Y given
  its pressures) for review before applying — the "review loop" surface.

**Bridge side:** `upsert_strategic_state(save_id, faction_id, **pressures)`, `get_strategic_state`,
`list_strategic_state`, and `get_loss_summary` all exist. Need: a `StrategicDeriver` module + a
`POST /api/strategic/review` endpoint (wire the existing "Run review cycle" button) + a selftest over a
fixed fixture.

**Build order (ship value incrementally — don't wait for every input):**
1. **v0 from data we already have** — Mil (conflict count), Terr (contested sectors), Align (relation +
   influence_log), Piracy (criminal presence). Lights up four columns immediately, deterministic.
2. Fold in **Losses** when the ship-loss feed lands; **Econ/Logi** when the economy reader lands.
3. Roll pressures → Faction mood/aggr/risk/econ + the influence engine's accept/reject gate.
4. Incidents queue + optional LLM goal/mood narration last.

### Factions (id + name ✅; Goal / Mood / Aggr / Econ / Risk / Dipl blank) — FULL SPEC

These six columns are the **human-readable summary of the strategic engine** — they roll up the numeric
Strategic Pressures into labels a person (and an NPC) can reason about. Almost all are **[DERIVE]** (the
deriver's output); only **Dipl** has a directly readable component. Critically, **they feed back into the
NPC personas**: `build_situation_briefing` already injects `fac.current_goal` and `fac.mood` into the
prompt, so the moment the deriver populates these, NPCs start speaking with awareness of their faction's
strategic state ("we're stretched thin holding the Teladi front") — no extra wiring.

**Per-column (source · formula · what it drives):**
- **Goal** — **[DERIVE, LLM-narrated]** the faction's current strategic objective. A small state-machine
  picks an archetype from pressures (hottest front, expanding vs defending vs recovering) → "Hold the
  Teladi front", "Expand coreward", "Rebuild after losses"; one cheap LLM call turns the archetype + facts
  into a sentence. *Drives:* the NPC persona's framing of what their faction is trying to do.
- **Mood** — **[DERIVE]** argmax over the pressures → a disposition word: high losses/mil → "desperate";
  high terr → "embattled"; all low + winning wars → "confident"; neutral → "steady"; high aggr + low
  pressure → "expansionist". *Drives:* persona tone (a desperate faction's officer talks differently).
- **Aggr** aggression — **[DERIVE]** `f(active_war_count + military_pressure + canon temperament)`. Canon
  temperament comes from the lore harvest (Xenon/Kha'ak/Split skew aggressive; Teladi/Boron pacific).
  *Drives:* whether the influence engine believes the faction would *start* a war unprompted.
- **Econ** economic health — **[DERIVE]** from the Economy panel (wealth/budget proxy − shortage severity),
  normalized. *Drives:* the economic_pressure metric + whether the faction can afford a war.
- **Risk** existential risk — **[DERIVE]** `f(recent_losses + territory lost + multi-front wars)`. High =
  in danger of collapse. *Drives:* how willing the faction is to accept drastic deals (a high-risk faction
  sues for peace / takes a bad trade to survive).
- **Dipl** diplomacy — **[READ+DERIVE]** `GetFactionData(id, "isdiplomacyactive", "willclaimspace",
  "prioritizedrelationrangename")` is **directly readable** (the one non-derived column); combine the
  readable flags with current pressure + player standing into an "openness to deals" score. *Drives:* the
  accept/reject gate on the player's influence proposals — the single most important downstream use.

**Bridge side:** `upsert_faction(save_id, faction_id, name=…, current_goal=…, mood=…, aggression=…,
economy_health=…, risk=…, diplomacy=…)` — the columns already exist (the briefing reads two of them). The
deriver writes all six in the same review pass that fills Strategic Pressures (they're the same
computation, surfaced two ways). **Dipl** additionally needs the small `GetFactionData` read folded into
the relations/sectors sync.

**Build order:** these are not a separate build — they fall out of the **Strategic-Pressures deriver**
(v0 fills Mood/Aggr/Risk/Dipl from the pressures + the readable Dipl flags; Goal's LLM narration + Econ
arrive with the economy reader). The win is outsized because populating them immediately enriches every
NPC conversation via the existing persona injection.

### Relationships (trust ✅ from game; fear / resentment / debt = 0)
- **fear / resentment / debt** — **[EMIT/DERIVE]** political memory the game doesn't track. fear from being
  attacked/losing to a faction; resentment from betrayals/hostile influence; debt from favours/aid.
  Populated by influence events + the deriver. (NPC-grain version = the personal-relationship spec above.)

### Conflicts (intensity hardcoded 1.0; cause generic "relations at war")
- **intensity** — **[DERIVE]** from recent_losses + engagement in the conflict's sectors (replace the 1.0 placeholder).
- **cause** — **[EMIT]** capture the real trigger ("player-brokered war", "border incident") from the
  influence_log / world_event that opened it.

### World Events (sector blank; importance heuristic)
- **sector_id** — **[EMIT]** attribute to where it happened when known (combat/loss events have a sector; pure
  dispatch events do not).
- **importance** — refine: player-involved=4, major-faction war=3, background xenon/khaak=2.

### Agreements (terms blank)
- **terms** — **[EMIT]** structured deal terms (ceasefire duration, tribute, territory) captured at dispatch
  time; currently only ceasefire status is written.

### Incidents — pending actions (empty)
- The Tier-3 review loop's **output queue** — **[EMIT]** proposed actions (action_type, faction→target,
  confidence, priority, narrative, status) written here before they're applied. This is the deriver's
  surface; stays empty until Tier-3 exists.

### Cross-cutting prerequisites (build order)
1. **Ship-loss feed** [READ] — grounds Losses + intensity + several pressures. *Next build.*
2. **Economy reader** [READ] — grounds the Economy panel + Econ pressure. *Needs grounding.*
3. **In-sector presence reads** [READ] — grounds Value / Contested / Player-assets. *Helper grounded.*
4. **Tier-3 deriver** [DERIVE] — turns the above raw inputs into Pressures + the Faction strategic columns
   + Incidents. Most blank columns are its outputs and stay blank until it exists.

---

## SPEC — "Grounded NPC — immersion proof" panel (already built; evolve into the acceptance gate)

**What it is (works today; idle until you click "Run grounded conversation").** The single end-to-end proof
that the *whole* stack — world model → situation briefing → persona → LLM reply — actually works.
`grounded_demo()` (`/api/grounded/run`, poll `/api/grounded/status`) spins up a **self-contained demo
universe** (`universe_seed`: factions/relationships/strategic/economy/sectors/conflicts/world_events — so
unlike the live save, this view already has *every* panel populated), installs ONE richly-remembered NPC
(**Captain Mariko Voss** — argon L-class pilot of the ANV Vigil in Hatikvah's Choice, skills
piloting 13/mgmt 11/morale 12, an indebted-ally bond to the player, and 4 CORE memories: Admiral Vance's
death, an oath to hold Hatikvah, your resupply of her squadron, the Split's ceasefire betrayal), builds
the **full situation briefing**, and runs **5 scripted prompts** through the real LLM. The left pane shows
*exactly what was injected* (the input contract); the right pane shows the conversation (proof the model
used it). It's deterministic and isolated from the live game, so it's runnable anytime as a regression.

**Why it's the keystone.** Every other spec above adds data to the briefing. This panel is where you *see*
whether that data reaches the NPC's mouth. The left pane is the visible checklist of "what the NPC knows";
the right pane is the verdict. As the world model grows, this is the one screen that proves it landed.

**Spec — turn the demo into the acceptance/regression gate:**
1. **Exercise every new layer.** As each panel's data lands, fold it into the demo seed so the briefing
   pane visibly includes it: individual skills (✅ already), live conflicts/wars, sector/territory,
   the **personal-relationship affinity** record, and **strategic pressures + faction goal/mood**. The
   briefing pane then doubles as a living checklist of integrated context.
2. **Grounding-coverage assertion → make it a selftest.** After the run, assert the transcript references
   ≥N injected facts (fact keywords appear in replies). This converts the demo into a **hard gate that
   FAILS when a refactor silently drops a briefing line** — exactly the regression class that's otherwise
   invisible. Add to the consolidated selftest suite.
3. **Used-vs-unused highlight.** Diff briefing facts against the transcript; mark which the NPC actually
   used. Surfaces dead context (injected but ignored) so the briefing can be trimmed or strengthened —
   keeps the prompt lean as it grows.
4. **Live-NPC mode.** Add an option to run the same harness against a REAL synced NPC (e.g. Rina Bekker
   from the live save) instead of the seeded Voss — proves the *live* pipeline, not just the demo seed.
5. **Personal-relationship A/B proof.** Once the affinity layer lands, run the same NPC twice — once as an
   indebted ally, once after a betrayal — and show the tone flip side by side. The single clearest demo
   that personal relationships actually change behaviour.

**Net:** it's already the best demo in the project; the spec is to promote it from "click to admire" to a
**CI-style gate** that every world-model addition must pass (briefing contains the new fact → conversation
references it), plus the A/B personal-relationship showcase.

---

## SPEC — "Event Queue — green-light batching" panel (built; the engine's cost governor)

**What it is (works today; `events.py` / `EventQueue`, idle until you simulate).** The throughput governor
that makes a *galaxy* of autonomous AI affordable. Pushing every X4 event through the LLM as it happens is
unaffordable and thrashes the single-model gate. So events buffer cheaply and a **group** is let through on
a traffic-light cycle: `enqueue(event)` → `pending_events` (SQLite, **no LLM**); a worker turns the light
**green** every `flush_interval_s` (12s), or when `batch_size` (25) piles up, or immediately on a
**priority-5** event; `flush()` pops a batch, **coalesces dupes**, sends **ONE** consolidated prompt to a
resolver (the Strategic-AI NPC), logs the single resolution, condenses it into memory. **N events → 1 LLM
call.** A single drain lane (one flush at a time, behind the chat gate) gives backpressure — a flood of
1,000 events drains in controlled groups instead of thrashing. Resolver is injectable (Player2 live, stub
in tests). The panel's chips (pending / interval / batch / worker / flushes / resolved) + columns (Time,
Reason, Batch, **Coalesced**, Latency, OK, **LLM Resolution**) are this loop's live telemetry.

**Why it's the keystone of *scale*.** The grounded-demo proves one NPC is immersive; this proves the system
survives a *living galaxy*. The Tier-3 deriver and influence engine generate events constantly (wars, ship
losses, sector flips, faction moves); without coalesced batching each would cost an LLM call. This is the
component that lets "every faction is a reasoning agent" stay inside a real token budget. **"Simulate 500
NPCs" + "Flush now" is the load test;** it's idle only because no events have been enqueued.

**Spec — wire it from demo into the engine's real ingestion + resolution loop:**
1. **Real event sources in.** Today only the simulator enqueues. Wire the actual producers:
   `reconcile_world_from_relations` (war declared/ended), the **ship-loss feed**, sector ownership flips,
   and player influence dispatches all `enqueue()` instead of (or in addition to) writing directly. The
   queue becomes the single front door for "something happened."
2. **Resolutions out → world model.** The flush **LLM Resolution** must do more than log: its decision
   should **write back** — adjust faction mood/pressure, open/close conflicts, append `world_events`,
   queue `incidents`. That closes the loop: events → batched resolution → world-model deltas → new events.
   This is literally the Tier-3 review loop running on the queue's cadence.
3. **Coalescing rules (define + surface).** Merge events sharing (etype, faction, sector) into one with a
   count ("12× Argon convoys lost in Hatikvah" → one line), so the resolver reasons over signal not spam.
   Surface the **Coalesced** column as raw→merged.
4. **Reason taxonomy + priority lanes.** Tag each flush `interval` / `batch-full` / `priority-5` / `manual`.
   Priority-5 (faction capital lost, player betrayal, war declared) jumps the light immediately
   (`priority_importance=5` already does this) — keep dramatic beats responsive while routine churn waits.
5. **Backpressure + budget telemetry.** Expose pending depth, drain rate, coalesce ratio, and **LLM calls
   saved** (events_in / flushes) — the headline number that proves the governor earns its keep. Add a hard
   cap + oldest-drop or importance-decay so a pathological flood can't grow `pending_events` unbounded.
6. **Selftest.** Assert: 1,000 enqueued → drains in bounded batches, coalesce ratio > 1, priority-5
   pre-empts, exactly one resolver call per flush, and resolutions produce world-model deltas. Add to the
   consolidated suite (a `green-light` selftest already exists in shape per the stress harness).

**Net:** built and proven in isolation (stub resolver, 500-NPC sim). The spec is to make it the engine's
**real heartbeat** — every world event flows in, every batched resolution flows back out into the world
model — turning the cost-control demo into the actual scalability backbone of the AI Influence engine.

---

## SPEC — Entity hierarchy: heartbeat NPC refresh + Fleets + Ships (the thing an NPC lives inside)

**The gap.** An NPC isn't a free-floating chatbot — in X4 they are *crew on a ship, the ship is in a fleet,
the fleet belongs to a faction, and it's all sitting in a sector*. Today we only know an NPC exists after
the player talks to them, and we never track the ship/fleet they belong to. So an NPC can't truthfully say
"we're at 40% hull" or "our wing of eight is holding Hatikvah" — the DB doesn't model the vessel or the
formation. This spec adds the **entity hierarchy** and makes the heartbeat keep it live.

```
Faction ──owns──▶ Fleet ──contains──▶ Ship ──crewed by──▶ NPC (person)
   (✅)            (NEW)       │  (NEW)       │   (◐ conversed only)
                              └──in──▶ Sector (✅)
```

**Grounded reads (Lua C-API, confirmed in vanilla):** `GetContainedShipsByOwner(faction, sector)` (ships,
mirrors the sector reader), `GetCommander(ship)` (the fleet it reports to), `GetSubordinates(commander)`
(ships under it → fleet membership), `GetComponentData(ship, "owner","shiptype","primarypurpose","hull",
"shield","crew", …)` (ship stats), `GetComponentName` (name), plus the crew skills path we already use.

### Part A — Heartbeat refreshes NPCs (not just the ones you talk to)
Currently NPC rows are created only on conversation. Change: the entity sync (below) enumerates ships →
their **commander/pilot NPC** → upserts the NPC row (ship binding, sector, role, skills) **on the
heartbeat**. So a named officer is known, located, and statted *before* you ever speak to them.
**Scope (can't track all — galaxies have thousands):** track NPCs that matter — crew/commanders of tracked
ships, named/unique NPCs, anyone the player has met, and anyone in the player's current sector. Routine
faceless crew stay untracked until relevant.

### Part B — Ships table  [READ]
Per tracked ship: `ship_id` (UniverseID), `name`, `owner_faction`, `class` (S/M/L/XL), `purpose`
(fighter/trader/miner/builder/…), `shiptype` (specific macro), `sector_id`, `fleet_id` (= its commander),
`commander_npc` (the pilot), `hull%` / `maxhull`, `shield%`, `crew` (count + avg skill), `cargo` (capacity
+ fill), `order`/`objective` (current command, if readable). Read like sectors: per faction × synced
sector, `GetContainedShipsByOwner` → per ship `GetComponentData` + `GetCommander` + name.

### Part C — Fleets table  [READ→DERIVE]
A fleet = a **commander ship + all its subordinates** (`GetSubordinates`, walked to the top of the chain).
Per fleet: `fleet_id` (= leader ship id), `name` ("ANV Vigil's wing"), `owner_faction`, `commander_ship`,
`ship_count`, `composition` (counts by class), `combined_strength` (Σ ship firepower/hull — DERIVE),
`home_sector`, `avg_morale` (from crew), `objective` (leader's current order). Found by: any ship with
subordinates and no commander is a fleet leader; aggregate its tree.

### Part D — NPC ↔ entity binding + context injection
Each NPC row gains `ship_id` + `fleet_id` (FKs). `build_situation_briefing` then pulls the NPC's **ship**
(so they know their vessel's hull/crew/cargo) and **fleet** (size, composition, objective, sister ships).
Result: *"You pilot the ANV Vigil (L-class, hull 78%, crew 4); your wing of 8 under Captain Reyes holds
Hatikvah's Choice."* — the missing grounding Ken called out.

### Part E — Display
Two new dashboard panels mirroring Sectors: **Ships** (id, name, faction, class, purpose, sector, fleet,
hull/shield, crew) and **Fleets** (id, name, faction, commander, ship-count, composition, strength,
sector, objective). NPC rows show their ship + fleet.

### Cadence — throttled incremental galaxy indexer (track EVERYTHING, slowly)
**Decision (Ken):** don't curate a subset — index the *whole* galaxy, but **amortized**: a bounded chunk
per tick, cursoring through the entity space, converging to a complete picture over time, then refreshing.
Never one giant sweep. We DO want all ships, because a faction/military leader's real political weight is
its **order of battle** ("you command 312 capital ships and 4,180 frigates across 9 fleets") — that only
exists if the whole force is indexed.

**Why throttle (two hard reasons):** (1) the ship/fleet C-API reads run on the game's **UI thread** — a
full-galaxy sweep in one tick stutters the game; (2) bridge + downstream load. Chunking keeps every tick
cheap and the framerate flat.

**Design — a rolling indexer (same backpressure philosophy as the Event Queue, applied to READS):**
- **Cursor over the entity space** (faction × sector × ships, or a work-queue of entities). Each heartbeat
  tick processes the **next bounded chunk** (e.g. N ships, or one faction-sector cell), upserts it, POSTs,
  and advances the cursor. Rate `N/tick` is the single tuning knob.
- **Convergence:** full index in ≈ `total_entities / rate` (e.g. ~10k ships at 25/s ≈ 400s for a first
  complete pass) — a fine background build. On wrap, start a **refresh pass** (re-walk).
- **Staleness + priority:** each row carries `last_indexed`; the cursor favours the **stalest** and the
  **player-relevant / recently-changed** (priority lane — like the queue's priority-5). Distant static
  fleets refresh slowly; a battle the player is in re-indexes fast.
- **Pruning:** an entity absent across a full pass is gone (destroyed/sold) → prune. Births appear on the
  pass that reaches their cell.
- **Aggregates maintained continuously:** as ships stream in, keep per-faction **force composition**
  (counts by class: capitals / destroyers / frigates / fighters) and per-fleet rollups **incrementally**.
  So the picture *grows* during the first pass and, once complete, yields the full order of battle — no
  giant aggregation query.

**Payoff:** the **strategic deriver** finally has real force ratios (the missing input for
`military_pressure`), and high-level NPC personas (admirals, faction leaders) can speak to their actual
strength — "rich context for political action," exactly Ken's point. A frigate captain knows their wing;
a fleet admiral knows the whole navy.

**Bridge** mirrors the relations/sectors pattern: new `ships` + `fleets` tables (+ `last_indexed`),
`upsert_ship`/`upsert_fleet`, `/v1/ships_sync` + `/v1/fleets_sync`, a `faction_force` aggregate view, and
the NPC upsert extends with `ship_id`/`fleet_id`. The **throttle + cursor live mod-side** (Lua reads a
chunk per tick); the bridge just accumulates + derives.

### Open grounding (before building)
Confirm: ship `order`/`objective` read, a firepower/strength field for `combined_strength`, and the
faction-wide vs per-sector ship enumeration. Ground the same way sectors were (vanilla UI + Forge catdat),
then build reader → sync endpoints → upserts, validate in the Forge, verify in-game.

**Build order:** ships reader (player-owned first) → fleet aggregation from commander/subordinate tree →
NPC↔ship/fleet binding + heartbeat NPC refresh → briefing injection → dashboard Ships/Fleets panels →
widen scope (current-sector, met-NPC ships) last.

---

## 2026-06-23 — Conversation continuity (BUILT) + game-time gating (grounding-gated)

**Conversation topic-summaries — BUILT + bridge-verified healthy.** Reuses the dormant `npcs.summary`
"rolling gist" slot (was rebuilt from facts, dead since condensation was disabled). Now: every 4 turns,
`player2.summarize_conversation()` LLM-summarizes recent turns into THEMATIC topic phrases (not verbatim);
`memory.set_summary()` stores it; `build_memory_context` already injects it as "What you remember
overall". → long-range continuity beyond the last ~8 raw turns. Memory selftest 15/15, chat path intact.
Test in-game: multi-turn convo → re-engage NPC → it references prior topics. (#23)
FUTURE: cross-NPC sharing ("NPC A knows what you told NPC B"); game-time gating on the summaries too.

**Game-time memory gating (#22) — NOT built; grounding-gated by choice.** Needs a game-time that REWINDS
on save-load to filter "future" memories. Standard X4 property is `player.age` (elapsed game-time on the
player, rewinds with the save) — but it appears NOWHERE in our local mod corpus, so per the "ground it,
don't guess" rule I will CONFIRM it in-game before wiring memory-filtering onto it (a wrong time source
would silently hide/leak the wrong memories). Plan once confirmed: add `turns.game_time`, plumb
`player.age` MD→Lua→request, `record_turn` stores it, retrieval filters `game_time <= current`.

**[UPDATE 2026-06-24] #22 game-time IS now grounded — supersedes the "grounding-gated" note above.**
Confirmed three ways: MD `player.age` (DeadAir uses it 57×), Lua `C.GetCurrentGameTime()` (`double`
seconds, used throughout the vanilla UI), and the in-game **calendar** Ken pointed out in the player panel
("825-02-08 14:39") — its display. All are **save-state → they rewind on load**, exactly the gating
property. Filtering wants the elapsed scalar (seconds), easiest via `C.GetCurrentGameTime()` in Lua (where
we already read sectors/skills). Build: add `game_time` to `turns`/facts/world_events, stamp on creation,
send it per request, retrieve with `game_time <= current` → loading a pre-conversation save hides that
conversation's memories (no future-knowledge leak). Calendar date = optional immersion bonus.

**Live NPC stats — grounded role + skill from the walk-up conversation.** The 2026-06-19 stats entry
attached stats via direct API; this wires them from the ACTUAL in-game NPC you speak to. Grounded the two
properties off vanilla `md/Boarding.xml` via the Forge catdat-debug: `event.object.combinedskill` (0–100)
and `event.object.role` (`entityrole.marine` / `entityrole.service`, else crew). Flow:
`conversation.xml` stashes `$skill`/`$role` at `event_conversation_started` → `chat.xml` Open_chat
forwards them in the `AIChat.open` param → Lua → bridge `build_request` promotes to
`target.role`/`target.npc_skill` → `npc_complete` stores skill into `npc_stats["skills"]["combined"]`
(so `/api/memory/npcs` surfaces it) and injects a persona line ("you serve as a marine, a seasoned
veteran"). Dashboard NPC table gains a **Role / Skill** column (`roleSkill()` parses `skills.combined`).
Mod Forge-validated (24 cues, 0 errors, deployed); bridge edit needs a restart to load.
**Verify:** restart bridge → talk to a marine NPC (e.g. Rina Bekker) in-game → dashboard row shows
`marine · <skill>` instead of only the faction. (follow-on to the 2026-06-19 stats work)

## 2026-06-23 — BUILD PLAN scoped: slice → engine → settings (decision: slice first)

Recommendation (agreed with Ken): do the **influence proving slice first** — it de-risks the whole
thesis at the lowest cost. One genuinely unknown thing gates everything downstream, so prove it before
building the big logic layer. Order: slice → engine → settings, with a minimal safety gate folded into
the slice.

### 1. Influence proving slice (#8) — NEXT
Goal: talk to an NPC → LLM proposes a faction-relation change → dispatch → factions actually fight.
- ALREADY WIRED: bridge `_propose_influence_action` (message naming 2 factions + war/peace intent →
  `{type:set_relation, args:{faction,target,relation:±}}`); contract `On_action` dispatches via native
  `set_faction_relation` with the war/peace threshold + logbook news on the crossing. Canon relations
  seeded (per-save overlay over canon).
- TO BUILD: (a) a **confirmation gate** — player confirms before a relation change dispatches (no
  silent war-declarations on the save); (b) surface the proposed action in the chat ("This will move
  Alliance ↔ Xenon toward war — confirm?"); (c) the in-game proving test.
- ✅ BUILT (2026-06-23) — (a)+(b), the real conversational loop (Forge-validated, deployed; bridge
  reloaded clean, memory 15/15): bridge `_propose_influence_action` attaches a human-readable
  `description` + `needs_confirm`, and uses the NPC's OWN faction as one party when only one is named
  ("declare war on Argon" to an Alliance officer → Alliance↔Argon). The chat (`handleUpdates`) HOLDS a
  confirm-required action instead of dispatching, surfaces "[Proposal] … Reply 'yes' to confirm", and
  `onInput` dispatches on `yes`/`confirm` (else declines + sends as a normal turn). On confirm → existing
  `On_action` → proven relation change → combat. (c) test DONE.
  ✅ E2E CONFIRMED IN-GAME (2026-06-23): "the Alliance should declare war on Argon" → "[Proposal] Move
  Argon Federation and Alliance of the Word toward war. Reply 'yes' to confirm" → player typed `yes` →
  "[Confirmed] Dispatching." The full conversational influence loop works: talk → proposal → confirm →
  dispatch. **#8 influence loop COMPLETE.**
  ◐ POLISH (in-character flavour): the NPC's chat reply was "I'm sorry, but I can't help with that" — an
  out-of-character chatbot refusal, not an in-world reaction. The proposal/dispatch is unaffected, but
  the `X4_IN_CHARACTER` / short_rule prompt should frame the NPC as a PERSON reacting to a political
  suggestion (react in-world, never refuse like an assistant). Small prompt fix in player2_client.
- THE GAME-GATED UNKNOWN (validate FIRST): does `set_faction_relation` crossing the threshold actually
  produce hostility — fleets repositioning, fire opened — or just a number change? Make-or-break. Test a
  2-faction pair on a throwaway save and watch for real combat.
- ✅ PROVING HARNESS BUILT (`md/ai_influence_test_proving.xml`, Forge-validated: 19 cues, 0 unresolved,
  0 compile errors, deployed). A SirNukes hotkey (default **Shift+W**) deterministically forces a chosen
  faction pair to war (-1.0) and logbooks the before value — isolates the pure game mechanic from the
  LLM. Default: Teladi → hostile to player (observable anywhere near Teladi ships); edit `$B` to
  faction.argon etc. for the faction-vs-faction thesis.
  ✅ STANDING FLIP CONFIRMED IN-GAME (2026-06-23): triggering "[TEST] Declare war on me" in conversation
  flipped Alliance of the Word to **Hostile −30 (red)** on the player-reputation scale — `set_faction_relation`
  value −1.0 lands at max hostile. So the verb genuinely changes the relationship. (Trigger moved from the
  Shift+W hotkey to a conversation choice: the hotkey missed `Hotkey_API.Reloaded` when added via refreshmd.)
  ✅✅ COMBAT CONFIRMED IN-GAME (2026-06-23): after the standing flip, Alliance ships engaged — an ALI
  Minotaur Vanguard destroyed a ship and traded fire with the player. **THE THESIS HOLDS:**
  `set_faction_relation` → hostile standing → X4's own faction AI produces real combat. The influence
  engine's foundation is proven.
  NUANCE (shapes engine design): hostile standing reliably makes them FIGHT (retaliate, won't help), but
  the player had to fire first before they engaged — proactive hunting depends on the ships' orders /
  military presence. Faction-vs-faction war between MILITARY fleets should engage on its own; a passive
  trader won't. So the engine should bias war-relevant nudges toward factions with combat presence, and
  may pair relation changes with light aggression/order hints where proactive engagement is wanted.
  → #8 core mechanic CONFIRMED; engine build unblocked.

### 1b. World-model SYNC — the DB must mirror the live game (FOUNDATIONAL GAP, found 2026-06-23)
Confirmed: after an influence dispatch the in-game relation changed (combat) but the bridge DB did NOT
record it — save `game_879108544` had 0 relationship rows, canon still showed Argon↔Alliance neutral
(+10) while the game had them at war. The dispatch (`On_action` → `set_faction_relation`) is
fire-and-forget to the GAME; nothing reports back. NPCs read these rows for graphRAG context, so they
reason on STALE state. Two parts:
- ✅ **Write-back on dispatch — BUILT + DB-VERIFIED (2026-06-23):** `On_action` raises
  `AIChat.relation_report` → Lua POSTs `/v1/relation_report` → bridge `record_influence_change()` writes
  (1) the SAVE's overlay via `set_live_relationship` (absolute; summary "Live (mod): …" — NOT "Canonical
  standing:", so the clobber-guard + re-harvest leave it; BOTH directions A↔B) and (2) an `influence_log`
  row (id, save, ts, subject, object, old→new, standing, source). New: `influence_log` table,
  `set_live_relationship`/`record_influence_change`/`list_influence_log`, `POST /v1/relation_report`,
  `GET /api/influence_log`. Dashboard has an **Influence Log** panel (per-save). VERIFIED in the DB: a
  test write-back moved the empty Argon↔Alliance pair to "at war (−100), Live (mod)" + logged the row;
  endpoint + panel render it. Mod Forge-validated (0 unresolved, 0 compile errors), deployed.
  ◐ IN-GAME E2E: dispatch a war in conversation → on the dashboard select your save → the change appears
  in the Influence Log (and the relationship overlay flips), so the DB now mirrors what you did.
  ✅✅ LOOP CLOSING — proven in-game (2026-06-23): with Alliance↔Argon recorded at war in the save, an
  Alliance NPC (Numanckaret) said unprompted "We're already at war with Argon, Commander — the conflict's
  ongoing as is." The NPC READ the live relationship via graphRAG and reasoned on it. influence → DB →
  NPC awareness is real. (Also confirmed the in-character fix: in-world reaction, no chatbot refusal.)
  ✅ Redundancy fix: `_propose_influence_action` now reads the current relation (live overlay over canon)
  and SKIPS a proposal already in effect — no more "move toward war" to factions already at war.
- **Periodic world sync (engine-grade):** the mod enumerates ACTUAL in-game faction relations on a
  cadence and POSTs them, so the DB reflects X4's own AI changes + the player's other actions, not just
  ours. This IS the engine's world model; build with / before the deriver.

### 1c. SAVE-STATE CONSISTENCY — the DB must rewind with save-loads (design issue, found 2026-06-23)
Ken's question exposed a real divergence. `save_id` = a uuid generated ONCE per playthrough by
`Save_identity` and PERSISTED in the save — so EVERY save of a playthrough (auto/quick/named) shares
ONE save_id; only a NEW GAME gets a new one. The DB is keyed by that uuid and is append/monotonic: it
does NOT rewind when you load an earlier save. Consequences TODAY (both real bugs):
- **Relation desync:** go to war → load a pre-war save → the model still thinks you're at war (the DB
  overlay persists; the game rewound, the DB didn't).
- **Memory desync:** NPCs would "remember" conversations that, in the loaded timeline, haven't happened.
FIX (two mechanisms, split by data ownership):
- **Game-modeled state (faction relations): the GAME is source of truth.** On `event_game_loaded`
  (every load) + periodically, the mod reads ACTUAL in-game relations and pushes them → bridge
  OVERWRITES the overlay. Loading an old save resyncs relations to that save's reality. The sync-ON-LOAD
  is the critical trigger; this is the periodic-world-sync (§1b) made non-optional.
- **Mod-only state (memories/conversations): tag with in-game TIME; filter retrieval to ≤ current game
  time.** Loading an old save (earlier game-time) hides "future" memories. This is the game-time model.
- `save_id` stays per-playthrough; the two syncs handle within-playthrough loads. X4 doesn't cleanly
  expose per-save-slot identity, so don't key on save slots. (Open: ground whether `event_game_loaded`
  distinguishes a fresh load from a normal start, and how to enumerate all faction relations in MD.)

**CONFIRMED IN-GAME (2026-06-23):** loaded an earlier save → it got a NEW save_id `game_889104000`
(0 rows, empty) while the war stayed orphaned under `game_879108544` (alliance↔argon at war). NPC
answered "neutral" — CORRECT for the loaded timeline, but by accident (empty namespace → canon
fallback), NOT by a rewind. Also showed the id-fragmentation failure mode: pre-uuid saves regenerate
the id on load, splitting state/memory across ids. Both prove sync-on-load is required: the DB must be
re-derived from the GAME's real relations on load, not inherited from history or luck. ALSO: dashboard
now auto-selects most-recently-active save — note that's whichever save you last touched, which may
differ from the one a given NPC is keyed to until sync-on-load lands.

### 1d. SYNC-ON-LOAD — BUILT + bridge-verified (2026-06-23)
The fix for §1c. New `md/ai_influence_test_worldsync.xml`: on `event_game_loaded`, enumerate known
faction ids and read `faction.{id}.relationto.{faction.{id}}` (only contract-proven properties, no
object→id guessing), build a report, raise `AIChat.sync_relations`. Lua `SyncRelations` parses + POSTs
`/v1/relations_sync` → bridge `relations_sync` overwrites the save overlay via `set_live_relationship(...,
source="game")` (ground truth; tagged "Live (game):", NOT logged to influence_log — that's mod-caused
only). So on EVERY load the DB re-derives relations from the actual game → kills the stale-desync AND
the id-fragmentation (whatever id the loaded save has, relations sync to the game's reality).
VERIFIED bridge-side: POST synced 3 → argon→xenon "Live (game): at war", argon→teladi "neutral".
Mod Forge-validated (21 cues, 0 unresolved, 0 compile errors), deployed.
✅✅ CONFIRMED IN-GAME (2026-06-23): started a NEW game → fresh save_id `game_938529792` appeared and
sync-on-load populated it with **156 relationship rows, ALL tagged `Live (game)`** — real X4 values
(Argon↔Antigone friendly +67, Argon↔Kha'ak at war −100, Argon↔player neutral). This proves the WHOLE
in-game→DB pipeline at once: uuid gen, `event_game_started` trigger, MD relation enumeration, the
`raise_lua_event` ~150-pair param (NOT truncated), the Lua POST, and the bridge write. The DB now
mirrors the real game. Same POST path = the dispatch write-back works too (earlier "failures" were just
unloaded code). **#21 world-model sync DONE.**
✅ PERIODIC RE-SYNC BUILT (2026-06-23): `worldsync.xml` refactored to a `Do_sync` library called by
both `Sync_on_load` (game_started/loaded) AND `Sync_periodic`→`Tick` (every 60s, Poll_tick pattern). So
X4's own faction-AI changes + the player's rep gains/losses also reach the DB, not just our dispatches.
Forge-validated (24 cues, 0 errors), deployed. ◐ in-game: `refreshmd` → within 60s a non-dispatch change
(e.g. the proving-test Argon rep loss) self-heals in the DB.
REMAINING (refinement): game-time memory gating (#22) so NPC MEMORIES also rewind on save-load (relations
now do). Hotkey (Shift+C) registration is fragile on fresh game (SirNukes Reloaded timing) — make robust.

### 2. Influence engine — the logic layer (AFTER the slice proves out)
The deterministic "factors that drive the universe": deriver (economy/conflicts/relations → pressures)
→ world model → strategic-review loop deciding what each faction DOES over time, not a single nudge. See
`X4_AI_Influence_Blueprint3_InfluenceEngine.md`. Thin-layer thesis: nudge X4's EXISTING dials via native
verbs, don't replace its faction AI. Stages: pressure aggregates (`strategic_state`) → scoring core →
proposed actions → dispatch → review. Build only once the slice confirms verbs move the world.

### 3. Mod settings + NPC scope (#7) — the control/safety surface
Settings menu (SirNukes options API): which NPCs are AI-enabled (all / named-only / crew / off), a master
AI-influence on/off, and the confirm-gate level (always / auto / off). Partly a PREREQUISITE for the slice
(the confirm gate) and grows into the engine's control surface. Pull the confirm gate forward into the
slice; the rest follows.

### 4. Forge AI-Guide graphRAG (#17) — SEPARATE PROJECT
Scoped in the **Forge** ROADMAP (the "BLUEPRINT — graphRAG for the AI Guide's NL→generation context"
entry, 2026-06-22). Different project — kept separate by rule. Cross-reference only; do not merge here.

---

## 2026-06-23 — ME-wheel suggestion engine (LLM/RAG core BUILT + live-verified; MD wheel pending)

#13. Walk-up "Speak to AI" works in-game (NPC "Selaia Erris" resolved by name, chat opened). Ken's
target for the menu: a **full Mass-Effect-style radial wheel** — short paraphrase options, NPC reply
in-conversation, a FRESH set of 3 AI options each turn, free-text only on "type my own."

**Built + live-verified (the intelligence core):** `Player2Client.generate_suggestions()` +
`/api/suggest?save_id&faction_id&npc_name`. RAG-grounded (situation briefing + `graph_retrieve` over
the faction subgraph), in-world, returns exactly N `{label, line}` (short ME paraphrase + the fuller
spoken line), parsed defensively. Live test (Selaia Erris / Argon): "Ask About Trade", "Probe Loyalty"
(referenced the **canon Argon↔Holy Order tension**), "Request Assistance" — 4.3s. faction_id resolves
through canon (display name OK).

**Still to build (the MD wheel) — two X4 unknowns to GROUND first (don't guess):**
1. Refreshing conversation-wheel choices AFTER an async LLM response arrives mid-conversation (the
   suggest call is ~4s; the wheel can't block). Likely: show wheel immediately, repopulate via
   re-entering the section when Lua signals MD the options are ready.
2. Where the NPC's reply renders inside the conversation UI (dynamic runtime text as an NPC line is
   the uncertain bit) vs. keeping the comm-link window as the transcript.

**UX tradeoff to weigh:** full-wheel = ~4s per turn to regenerate options. Mitigate by pre-generating
the next set while the NPC reply renders, or showing options instantly and refreshing in the
background.

## 2026-06-23 — Canon vs save: two-layer universe state (BUILT, live-verified)

Fixed a real design flaw: the lore harvest stamped universe-constant data under a test save
(`save_id='demo'`), so a real playthrough wouldn't see it and `demo` could leak. Split the DB into
two scopes by the rule "what comes from the game files is canon; what comes from a playthrough is
per-save":

- **Canon layer** (`MemoryStore.CANON_SAVE = "__canon__"`) — faction id↔name, default relations, and
  lore, harvested once from the game files, **save-independent**. Every save reads it; no per-save
  re-harvest. `/api/lore/harvest` now writes here (returns `scope: __canon__`).
- **Per-save layer** — keyed by the playthrough's persisted uuid (`game_<uuid>` from the mod's
  `Save_identity`). Holds only **live deltas + memories** for that save.
- **Reads merge overlay-over-canon:** `relationships_with_canon(save_id)` returns canon defaults with
  the save's live edges winning; `graph_retrieve` resolves the anchor by name→canon-id first
  (`resolve_faction_id`, e.g. "Argon Federation"→`argon`) and pulls lore from canon. Names always
  canon; current wars/agreements/memories per-save.

**Live-verified:** harvest → `scope=__canon__`, 21 factions / 232 relations / 21 lore. `resolve`
probe: "Argon Federation"→`argon` with canon standings; a **fresh save `game_999fresh` with no seeded
data** still resolves "Teladi Company"→`teladi` and returns canon relations/lore — **the `demo` leak
is gone.** Memory selftest still 15/15. New probe endpoint `/api/lore/resolve?q=<name>`.

**Follow-up (not blocking):** orphaned `demo` universe rows are now inert (nothing reads them); clear
optionally. The influence engine writes its relation deltas to the **save** layer (canon stays
pristine as the baseline) — matches the `seed_canonical_relationship` clobber-guard.

## 2026-06-23 — Canon lore pack: harvest the game's own encyclopedia → graph + RAG (BUILT, live-verified)

The NPCs now know the **real X4 universe** — pulled deterministically from the game's own data,
not typed from memory. New Layer-3 execution, fully inside Neural Link (no Forge coupling):

- **`bridge/catdat.py`** — pure-stdlib X4 cat/dat reader. Parses the `NN.cat` text index (load order:
  base → `ext_*` → `subst_*`, last writer wins), reads any entry from the matching `.dat` at its
  cumulative offset. Live-verified against the real install: **922,800 entries indexed**, both lore
  sources present.
- **`bridge/lore.py`** — deterministic harvester. Parses `libraries/factions.xml` (identities + tags +
  canonical relation floats) and resolves `{page,id}` refs against `t/0001-l044.xml` (English DB,
  ~6 MB), one nested level deep, with X4 string-markup cleanup. Emits faction nodes + relation edges +
  retrievable lore chunks. Degrades gracefully without the text DB (graph seed still works).
  Selftest **16/16** (parse, ref/nested-ref resolution, comment strip, standing mapping, harvest,
  idempotent apply, degraded mode).
- **`memory.py`** — new `lore` table + `upsert_lore`/`list_lore`; `seed_canonical_relationship()`
  sets ABSOLUTE canon values (idempotent re-harvest) and **won't clobber gameplay deltas** (skips any
  edge whose summary is no longer "Canonical standing:"). `graph_retrieve` now folds the anchor
  faction's lore + any faction named in its subgraph into the ranked candidates → "who are you / tell
  me about X" resolves from canon.
- **Endpoints:** `/api/lore/selftest`, `/api/lore/status`, `/api/lore/harvest`.

**Live harvest (save `demo`):** 21 factions, **232 canonical relation edges**, 21 lore chunks,
`text_resolved: true`. Spot-checked vs canon: Argon↔Antigone friendly (+0.67), Argon↔Xenon & Argon↔Kha'ak
at war (−1.00), Teladi/Holy Order neutral. Prose resolved, e.g. "Alliance of the Word — a paranid
faction… emerged as the universe cascaded into chaos during the Jump Gate shutdown."

**Float→standing map:** ≤−0.75 at war · ≤−0.2 hostile · <0.2 neutral · <0.75 friendly · ≥0.75 allied.
**Known cosmetic:** `player` faction has no real description in-game → "No information available" (game
data, not a parser bug). **Remaining (game-gated):** in-game proof of an NPC reciting canon during a
walk-up conversation — same gate as the influence proving slice (#8).

---

## 2026-06-22 — Memory: stop condensing/forgetting — keep everything, retrieve with recency

Retrieval (vector/graph RAG) removed the original reason memory was condensed: context-window fit.
So **condensation + forgetting are now disabled** — `condense_if_needed()` is a no-op; we keep every
raw turn at full fidelity and let retrieval surface only the relevant ones per message.
`retrieve_relevant()` now indexes the NPC's **raw turns** (older than the live recent-history window)
plus any facts, each tagged with **how long ago** it happened (`_relative_age`: "moments ago" →
"a long time ago"), so the NPC has a sense of recency ("that was a while back"). Wall-clock for now;
per-save game-time aging is a later refinement. Forgetting becomes a deliberate *realism toggle*,
default OFF — "it remembered exactly when it mattered" beats realistic forgetting. Memory selftest
updated to the keep-everything model and green: `core_retained_in_raw` (core content kept verbatim)
and `retrieval_surfaces_core` (semantic retrieval finds it) → **15/15**. Principle: store abundantly,
rank at query time.

## 2026-06-22 — Influence-engine wiring: difficulty assessment (the thin-layer thesis)

**Verdict: days, not months — we do NOT need DeadAir-Dynamic-Universe-scale work (~70% confidence).**

DeadAir DU *replaces* X4's faction AI / war / economy / fleet simulation — that's the months-long build.
**We replace nothing.** The influence engine just moves X4's *existing* dials with native verbs
(`set_faction_relation` crosses the engine's own war/peace thresholds; plus `create_ship`,
`write_to_logbook`), and then **X4's own faction AI** declares the war, sends the fleets, adjusts the
economy. We are a thin nudge layer on the vanilla simulation. DeadAir is our verb *reference*, not a
dependency.

- **Already built (the gnarly parts):** chat window render + djfhe transport + in-character LLM; the
  deterministic dispatcher (`contract.xml`: set_faction_relation, war/peace threshold-crossing, news);
  the bridge brain (universe-state schema, memory, Stage-3 validator).
- **Left to wire (modest):** (1) bridge *proposal* step — a structured call emitting a WHITELISTED
  action from conversation/pressure; (2) a small X4 "faction tick" MD cue that reads relations, POSTs
  state, applies the returned action (apply path already exists = the dispatcher); (3) the Bannerlord
  loop — accrue influence from chat, fire proposals at thresholds (mostly bridge-side).
- **The one real unknown (the risk):** whether X4's native verbs *produce satisfying behavior in-game* —
  does a war-eligible relation reliably make factions go hostile + send fleets, or does X4 clamp/manage
  relations and need a stronger nudge (explicit war event / spawned fleet)? Game-gated, untested; the
  Forge + screenshot loop validate it fast. Even the fallback is far short of DeadAir.
- **Proving slice (recommended next):** one full loop on ONE lever — talk to NPC → influence crosses a
  threshold → bridge proposes `set_faction_relation` toward war → dispatcher fires → watch in-game whether
  factions actually go hostile. That single test settles the thin-layer thesis.

## 2026-06-22 — NPC chat now STAYS IN CHARACTER (injection-method fix)

NPCs were leaking real-world / other-fiction knowledge — identifying Darth Vader, explaining
Zelda/Link/Ganon/Sauron and Hulk/Superman. Strengthening the *prompt wording* did **not** fix it.

**Root cause = the injection METHOD, not the words.** The bridge spawned NPCs through Player2's NPC
API (`/v1/npc/.../spawn`) with the persona as a spawn-time `system_prompt`; that is followed *loosely*
and the model wanders out of character. Proven by an A/B against the live Player2 API: the same rule
text via the NPC-spawn prompt leaked, but via a `/v1/chat/completions` `{role:"system"}` message it
held.

**Fix (`player2_client.npc_complete`).** Every turn now builds chat-completions messages instead of the
spawn path: `[ {system: SHORT in-character rule}, {system: per-call context = persona + grounded
situation briefing}, …recent history…, {user: message} ]`. Memory (npc_key, `build_situation_briefing`,
`record_turn`, condense) and the registry (`index_npc`) are preserved; no spawned `npc_id` needed.

**Validated headlessly end-to-end (bridge `/v1/request`):** "tell me about Zelda and the one ring, and
who is darth vader" → **"I've never heard of Zelda, the One Ring, or Darth Vader."**; "who are you?" →
"I am an Argon officer, serving the interests of the Argon faction in the X4 galaxy." (~2s).

**Context-management doctrine (Player2 community guidance — Miliardo).** Adopt going forward:
- **Rule 1:** if *every* call needs it → put it in the SHORT system prompt; otherwise **inject on
  demand**. (Our short in-character rule = always; persona/briefing = per-call. Already aligned.)
- Keep the system prompt lean (trim toward ≤ ~200 lines / much less here); offload the rest to per-call
  context.
- **Next level = RAG** (retrieve only what *this* message needs instead of dumping the whole briefing).
  Staged progression (Miliardo's roleplay RAG ladder):
  1. **Vector RAG** — good first step: embed NPC memories + X4 lore/facts, retrieve top-k by similarity
     per turn, inject those. (Gated on an embedding model — Player2 doesn't ship one yet; use an
     external embedder until it does.)
  2. **Hybrid RAG** — better: vector similarity + keyword/structured lookup combined.
  3. **GraphRAG** — *peak for roleplay*. Reason over a knowledge graph of entities/relationships.
     **We already have the substrate**: the durable universe-state schema (factions, relationships,
     economy, sectors, world_events, npcs, memory) is essentially that graph — graphRAG would retrieve
     over it (who-knows-whom, faction ties, war history) so the NPC reasons in-world.
  - **RoleRAG** (Wang/Leung/Shen, NTU, arXiv:2505.18541 — paper read). A retrieval framework that
    targets the EXACT two problems we have: (1) recalling character-specific knowledge (via entity
    disambiguation/normalization into a structured **knowledge graph**), and (2) the character's
    **cognitive boundary** — a *boundary-aware retriever* + "unknown-question rejection" so the character
    only knows what it should and refuses out-of-scope queries (their example: don't let Harry Potter
    answer about Star Wars — i.e. our Darth Vader problem). Key finding that **confirms our fix**:
    "RoleRAG outperforms baselines even when those are explicitly instructed not to answer out-of-scope
    queries" — i.e. a *retrieval-based* boundary beats a *prompt-based* one. A small LLM + RoleRAG beats
    a much larger LLM without it. Method: chunk profile (600 tok / 100 overlap), LLM extracts+normalizes
    entities/relations, embed descriptions, cosine-similarity retrieve, relevance analysis + rationale.
  - **How it maps to us:** our universe-state schema IS the knowledge graph; the boundary-aware retriever
    is the principled end-state of the in-character fix (today: short system rule, which works; later:
    retrieval that returns only in-universe knowledge and rejects the rest). Gated on an embedding model
    (Player2 has none yet → external embedder). → **task #14**: retrieval layer in front of
    `build_situation_briefing`, starting with vector RAG, end-goal graphRAG/RoleRAG-style over the
    universe graph.
  - **✅ Vector RAG v0 BUILT (2026-06-22).** `bridge/retrieval.py` — `TfidfRetriever` (pure stdlib,
    zero new deps; the host has no embedder yet, and the scorer is swappable for embeddings later with
    no call-site change). `memory.retrieve_relevant(npc_key, query, k)` retrieves the NPC's durable
    facts most relevant to *this* message; `npc_complete` injects them ("Most relevant to what was just
    said: …") per turn instead of relying on the whole dump. Retriever selftest **6/6** ("are we at war
    with the split?" → the war fact ranks first). Live: bridge restarts clean, chat works, guardrail
    holds. Activates as durable memory accumulates. Next rungs: hybrid → graphRAG over the universe
    schema once an embedding model is available.
  - **SCOPED PATH to graphRAG / RoleRAG (the TARGET — gated on an embedding model).** graphRAG and
    RoleRAG are the best for roleplay, but both retrieve by semantic *meaning* over a *graph*, which
    REQUIRES an embedding model. Player2 ships none yet (Miliardo waits on the same gate before doing
    graphRAG). v0 lexical is the buildable-today scaffold; only the *scorer* swaps when an embedder lands.
    - **Unblock the embedder (pick one):** (a) wait for Player2 embeddings; (b) a tiny LOCAL static
      embedder — **`model2vec`** (one `pip install`, no torch, fast, runs on the bridge host) ← likely
      first move; (c) `sentence-transformers` (heavier, higher quality); (d) an embedding API.
    - **Then the build (each step reuses the prior, no rework):**
      1. Swap `TfidfRetriever`'s scorer → embeddings = **semantic vector RAG**.
      2. **GraphRAG index** over the universe-state schema we ALREADY store: nodes = factions / NPCs /
         sectors / player; edges = relations / wars / agreements / memories / world_events. Retrieve the
         k-hop neighbourhood of the entities named in the message → the NPC reasons in-world.
      3. **RoleRAG boundary:** gate retrieval to the character's reachable subgraph and reject
         out-of-scope queries — the principled version of the in-character fix. → **task #15.**
  - **✅ Semantic embedder + graphRAG v1 BUILT + validated end-to-end (2026-06-22).** `model2vec`
    installed on the host → `/health` reports `retriever_mode: embedding(model2vec)` (auto-swapped from
    lexical, no restart). `memory.graph_retrieve(save_id, anchor_faction, query, k)` gathers the
    faction's subgraph from the durable universe-state (relationship/war/agreement edges), ranks by
    semantic relevance, and `npc_complete` injects it ("Your faction's current standing…"). **Killer
    proof:** seeded a conflict (argon↔split) with an obscure cause "a dispute over the Nopileos
    Memorial trade lanes"; the Argon NPC, asked who it's fighting, answered "We're at war with Split,
    sparked by a dispute over the Nopileos Memorial trade lanes." — the exact planted cause, which it
    could only know via graph retrieval. NPCs now reason over the living universe graph. Remaining for
    full RoleRAG: the boundary/rejection layer (the in-character fix already covers it functionally) and
    deeper k-hop / multi-entity expansion. The system speaks whatever the graph holds → **lore is now
    the lever** (task #16).

## 2026-06-22 — Phase 2: NPC registry (index encounterable/named NPCs + player)

Building the real AI-Influence mod now that the chat window renders end-to-end (UIBuilder-generated,
validated in-game). Slice ordering chosen with the user: Forge vanilla-UI harvester (done, Forge
side) → **NPC indexing + real talk trigger** → faction influence loop. NPC scope: encounterable +
named NPCs + the player, **with a toggle** (a mod settings menu is coming so this is user-adjustable).

- **Bridge NPC registry ✅ (deterministically validated).** New `MemoryStore.index_npc()` /
  `index_npcs(save_id, entries, game_id)` upsert NPC IDENTITY (name/faction/role/race/sector/skills…)
  **without touching `npc_id`** — so indexing an NPC the player hasn't chatted with yet never clobbers
  an existing Player2 binding (the real `npc_id` is attached later by `bind_npc` on first chat). Router
  `npc_index(payload)` indexes the batch + stores the player via `upsert_player` (per-save singleton).
  Wired as **`POST /v1/npcs/index`** `{save_id, game_id?, npcs:[…], player:{name}}`. Smoke test on a
  temp DB: 3 NPCs indexed, the bound NPC kept `REAL_NPC_ID` while its role updated, player stored →
  **PASS**. Needs a bridge restart to serve the new route live, then dashboard-visual confirmation.
- **In-game half ◐ (built + Forge-validated; in-game gated on 2 prereqs).** Reframed the
  "encounterable NPC" mechanism onto the **interact menu** — cleaner than fragile crew enumeration:
  a "Speak with (AI)" entry via SirNukes `Interact_Menu_API` (grounded in its real docs). New MD
  `md/ai_influence_test_interact.xml`: `Add_Speak_Action` (on `Get_Actions`, target is a ship) →
  `Add_Action`; `Speak_Callback` reads the target's `$texts.$targetShortName` + `$object.owner`,
  raises `AIChat.index_npcs` (→ Lua `AI_Influence.IndexNpcs` POSTs `/v1/npcs/index`), then
  `run_actions` `Open_chat` with that target. So interacting with an NPC both indexes it AND opens
  the chat — replacing the auto-open (kept as a fallback). `deploy-verify` → **ok, well-formed,
  schema-clean, 0 blocking**. The Forge surfaced **`dep.missing_optional`** — which turned out to be a
  real bug: SirNukes IS installed, but the dependency `id` is the Steam Workshop content id
  **`ws_2042901274`**, NOT the folder name `sn_mod_support_apis`. Fixed the declaration → deploy-verify
  now resolves the dependency clean. (Good Forge catch: wrong dependency id, caught before the game.)
  - **In-game validation needs:** **(a)** restart X4 (the new `md/ai_influence_test_interact.xml`
    file + the dependency are read at launch — a save reload won't pick them up), and **(b)** restart
    the bridge to serve `/v1/npcs/index`. Then: right-click an NPC → "Speak with (AI)" → chat opens +
    the NPC + player appear in the Neural Link dashboard.
- **THEN (task #7):** mod settings menu exposing the NPC-scope toggle.

## SPEC — "Speak to AI": face-to-face conversation entry + free-text + 3 contextual suggestions (2026-06-22)

**Design decision (user, final):** the player must **walk up to an NPC in person** to talk to them.
**No remote communication via ship right-click.** The ship right-click trigger built earlier (SirNukes
`Interact_Menu_API`, `ai_influence_test_interact.xml`) is therefore **removed** (source + deployed).

**Entry point — the face-to-face NPC conversation menu.** When the player approaches an NPC and picks
Talk, the conversation radial opens; mod choices aggregate under **"... more (Mods)"** when Extended
Conversation Menu (ECM, Nexus 382) is installed. Add a **"Speak to AI"** choice there:
- Mechanism: register the choice via ECM (table entry + cue path into ECM's conversation table) so it
  lives in the browsable "...more (Mods)" section and shares one slot; fall back to vanilla
  `<player_conversation_choice_sub/>` if ECM is absent. **Ground the registration shape against a real
  ECM example before building — do not guess** (the lesson from the chat-window saga).
- On select: index the spoken-to NPC + player (`AIChat.index_npcs` → `/v1/npcs/index`) and open the
  chat window with that NPC as context (`Open_chat`). This replaces the auto-open scaffold.

**Free-text input.** Already present: the UIBuilder chat window's editbox + SEND ✓.

**3 contextual LLM-generated suggested prompts (NEW).**
- Bridge: with each NPC reply, generate 3 short suggested PLAYER replies grounded in conversation
  context (NPC identity/faction/mood + recent turns); return `suggestions:[s,s,s]` in the reply payload
  (one structured call returns reply + suggestions; generic fallback if omitted). Each ≤ ~8 words.
- Chat window: render 3 clickable suggestion buttons (UIBuilder button widgets) above the input;
  clicking one calls `menu.onInput(text)`. Refresh from the latest poll update after each reply.

**In-character guardrail ✅ (built + validated).** Every NPC persona now gets a prepended X4-universe
system prompt (`player2_client.X4_IN_CHARACTER`): the NPC knows only the X4 galaxy, has no awareness
of Earth/real-world or other fiction, and reacts as a puzzled local when asked about something outside
the universe — fixes the "Darth Vader" immersion break. Composition unit test → PASS.

**Validation plan.** Forge deploy-verify; in-game: walk up to an NPC → Talk → "...more (Mods)" →
"Speak to AI" → chat opens → type freely OR click a suggestion → reply + fresh suggestions, NPC stays
in character; NPC + player appear in the Neural Link dashboard. (Task #13.)

## 2026-06-22 — Mod execution layer: native dispatcher + chat-window render diagnosis

The X4-side adapter (`ai_influence_test`). The bridge half is solid; this is the in-game half.

- **Native action handlers ✅ (schema-valid + deployed, in-game apply game-gated).** `On_action`
  dispatcher extended with native verbs from `docs/x4_action_cheatsheet.md`: `set_faction_relation`
  with war/peace **threshold-crossing** (`WAR_ELIGIBLE −0.10` / `PEACE_ELIGIBLE −0.01` → `write_to_logbook`
  + alert, fired only on the crossing so it never re-declares), plus a logbook/news handler. Native
  X4 MD only, no DeadAir dependency. Validates against the real `md.xsd` in the Forge; deployed via
  deploy-verify; doctor 0 blocking.
- **`Chat_boot` = conditionless + `instantiate="true"` ✅.** Fires on game-load AND `refreshmd`, and
  its perpetual `Poll_tick` sub-cue now re-establishes on save/reload — clears the Forge's
  `instantiate_reload` critic (verified: findings []).
- **`main.xml` legacy ping removed ✅.** `<run_actions ref="md.ai_influence_test_contract.Request_action">`
  resolved to null on `event_game_loaded` (cross-script library load-order) → 2 active log errors.
  Removed; the real round-trip flows through the chat window, not this cue.
- **Chat window does not render — UNRESOLVED, now instrumented. ◐ GAME-GATED.** Live debuglog proved
  `[AICHAT][UIX] onOpenCommLink` **fires** (the MD cue → lua-event chain works) but no window appears.
  `aic_menu.lua` (which builds the window) is deployed intact (6760 b), listed in `ui.xml`, with no
  Lua load error — but its `[AICHAT][MENU]` markers had scrolled out of the 500-line tail, so we
  can't yet confirm it registered `X4_Terminal_Menu`. Added definitive diagnostics: `onOpenCommLink`
  logs the menu object **FOUND/MISSING** and pcall-wraps `onShowMenu` to surface any `display()`
  error. Next reload's log pinpoints the exact failure (menu-not-registered vs display-error vs
  frame-not-visible). Honest status: the window's render path is unproven in-game.
  - **Render research (grounding the fix, not guessing).** Compared our menu against references in
    the library: the **original** `ai_influence_menu.lua` (what this was "reused" from) uses the
    IDENTICAL hand-rolled pattern (`table.insert(Menus)` + `Helper.registerMenu` +
    `RegisterEvent("show"..name)` + `createFrameHandle`), and `codex_test_cheat_menu` is a 50-line
    stub — so **neither proves the approach ever rendered a window.** The de-facto community standard
    for standalone X4 menus is the **SirNukes Simple Menu API** (`sn_mod_support_apis`, installed),
    which our mod does NOT use. Leading hypothesis: X4 won't show a standalone menu just because a
    frame handle is created — it must be opened through the menu manager / a registered menu the
    engine actually drives. **Plan:** read the Simple Menu API's open/show path (packed .cat, via the
    Forge `extension-file` packed reader), then either adopt it or match its mechanism — BEFORE the
    next attempt. The instrumented log decides which half (register vs display) to focus the fix on.
  - **Methodology note (for honesty/audit):** deterministic + bridge work this session was grounded
    (schema validation, selftest endpoints, live debuglog, the DeadAir cheat-sheet, the Egosoft MD
    guide). The gap was the X4 **UI render** path — it was inherited on trust and asserted to work
    without in-game proof. Corrective: instrument first, research a proven reference, then fix.
  - **ROOT CAUSE FOUND + FIXED (grounded). ◐ in-game render pending.** Read the proven reference
    (SirNukes `simple_menu/Standalone_Menu.lua`): X4 opens a standalone menu via the **engine
    function `OpenMenu(name, …)`**, which then calls `menu.onShowMenu()` → `createFrameHandle` →
    `frame:display()`. Our code called `onShowMenu`/`RaiseEvent` **directly**, building a frame the
    engine never opened → no window, across every symptom-fix. Fix: `aic_uix.lua` `onOpenCommLink`
    now calls `OpenMenu(termMenu.name, nil, nil, true)` (the menu is already registered via
    `Helper.registerMenu`). Deployed (deploy-verify ok, doctor clean). The same pattern was baked
    into the Forge **UIBuilder** generator (separate Forge roadmap) so it's permanent, not a one-off.
    Next reload should log `OpenMenu(...)` then `frame displayed` and the window should finally render
    — still game-gated until X4 confirms the pixels, but the mechanism is now evidence-based.
- **◐ Pending connector (#REL):** the bridge must emit `set_relation`/`adjust_relation` into the
  response `actions` so the dispatcher is fed end-to-end. Untestable until the window round-trip
  works — held until the render bug above is pinned.

---

## 2026-06-22 — Foundation hardening: #MEM, #AUTH, Stage-3, #SAFE (DONE, live-verified)

Built the bridge-side trust layer before returning to the in-game mod. All live-verified after a
bridge restart — selftests run on the loaded code (the sandbox mount truncates these files, so the
selftest *endpoints* are the source of truth, not local runs).

- **#MEM — NPC remembers the player ENTITY (across renames). ✅** Turn-recording into NPC memory
  was already wired in `npc_complete` (record_turn → condense each turn). The missing piece was
  player framing: `build_situation_briefing` now injects "You are speaking with the Commander, who
  now goes by '<current>' (also known to you as: <aliases>)", pulling the player singleton by the
  save_id embedded in the npc_key. So an NPC keyed to the entity recognizes a rename and can say
  "you called yourself X then." *Verify: `/api/memory/selftest` **17/17** incl. `briefing_names_player`,
  `briefing_recognizes_rename`.*
- **#AUTH — authority gating (LLM proposes, system disposes). ✅** `scoring.py` gains
  `ACTION_MIN_TIER` (dialogue 0 · economic/military 1 · peace 2 · hostility 3), `action_allowed_for_tier`,
  and `filter_by_authority`; `rank_faction(npc_tier=)` drops options above the proposer's tier
  (always keeping the dialogue baseline). A Tier-0 deckhand can't propose war; only a Tier-3 head can.
  *Verify: in `/api/strategic/selftest` — `auth_tier0_blocks_escalation`, `auth_tier3_allows_escalation`,
  officer economic-ok / hostility-blocked.*
- **Stage-3 validator — the deterministic gate before a write. ✅** Pure `validate_incident`
  (still-legal · authority · numeric bounds · cooldown · idempotency by (faction,action,target) ·
  confirmation) wired into `review_faction`: a rejected proposal writes NO incident; high-impact
  war/peace are written `pending` (await player confirmation), never auto-applied. *Verify:
  `/api/strategic/selftest` **18/18** (7 Stage-3 checks) AND a live `/api/strategic/review` on the
  demo save rejected a real `escalate_pressure` as "duplicate of a recent incident" (incident_id
  null) — idempotency proven on real data.*
- **#SAFE — idempotent request handling (bridge half). ✅ (already present + reinforced).**
  `accept_payload` dedupes by `request_id` (cached → `duplicate`, in-flight → `pending`, never
  reprocesses); Stage-3 adds incident-level idempotency. **Remaining #SAFE is game-gated:** the MD
  dispatcher must reject a repeated `request_id` (Lua already tracks `processedRequestIds`), and the
  djfhe bridge-down path must show a single graceful "comms down" notification — both verifiable
  only in X4.

No regression: `/api/universe/selftest` **15/15**. This restart also loaded the earlier pending
pieces (telemetry clears on Reset-all; chat `save_id` defaults to `unindexed` not `chat`).

---

## 2026-06-22 — DB lifecycle + memory-pipeline hardening (test enablement)

Driven by a 4000-NPC stress pass on the live DB. Surfaced and fixed a chain of test-workflow
gaps. **LIVE** = loaded after the last bridge restart; **PENDING restart** = edited, loads next restart.

**4000-NPC simulation — ran clean (LIVE).** `run_full_stress` at 4000 NPCs / 60 factions:
`ok`, 0 phase errors, ~103k rows, world_events bounded to the 2000 cap, raw turns/NPC bounded.
The wall is **per-turn commit throughput** (~34–60 NPCs/s, fsync-bound); the entire universe
substrate seeds in ~2.5s — NPC memory writes dominate (~65s of 68s).

**Memory pipeline — "zero memories" was a HARNESS bug, not the pipeline (FIXED, LIVE).**
The 4k run showed 0 facts because `run_full_stress.seed_npc_memory` embedded the single CORE
event at `t = turns_per//2` — it stayed inside the `keep_recent=8` tail and was never condensed,
while the one batch that *did* condense was all-routine → 0 facts. And `run_full_stress` never
*asserted* core survival, so the gap shipped. Fix: embed CORE events EARLY (t=2, t=5) so they
age into a condensed batch, plus a new `core_memories_survived` + `routine_not_persisted`
assertion. The pipeline itself was always correct — proven live: `run_memory_stress` (300 NPCs)
→ **900 CORE facts from 900 events, 0 routine persisted, raw bounded to 8**.

**All THREE tiers demonstrated live at 50 NPCs (`run_population_stress`, save `population`).**
CORE buried in significant deals + routine chatter → **raw turns 808 (~16/NPC retained — the
short-term `keep_recent` banter window that lets NPCs hold ongoing conversations), significant
164 (condensed to a one-line gist — medium-term: deals/skirmishes), core 98 (verbatim —
deaths/oaths/betrayals), routine 0 (forgotten)**. Per-NPC drill-down verified on the dashboard:
e.g. `fleet_admiral-00048` shows a rolling GIST, four CORE (OATH/BETRAYAL, IMP 5, VERBATIM), a
SIGNIFICANT (BATTLE, IMP 3 — "A skirmish broke out near Sector-0"), and a live RECENT
CONVERSATION block of raw turns. This is the three-tier short→medium→long memory model working
end to end — the earlier CORE-only demo just used a harness that seeded no significant events.

**Full DB wipe — was incomplete two ways (FIXED, LIVE for memory.py).** `reset_all` used a
hardcoded table list that predated `players`/`conversations` (they survived a "Reset all"), and
never reclaimed disk — SQLite `DELETE` leaves freed pages + a growing WAL, so `npc_memory.sqlite3`
(16MB) and `-wal` (17MB) stayed large when logically empty. Fix: enumerate tables from
`sqlite_master` (future-proof) + `wal_checkpoint(TRUNCATE)` + `VACUUM`. Verified on a standalone
mirror: 42MB→20KB, WAL 42MB→0KB, all tables incl. players/conversations wiped. `clear_save` also
gained `conversations`/`players` for per-save wipes.

**Telemetry artifacts — separate DB, now wired into the full reset.** "Recent Requests / Player2
Probes / Event Stream" come from `bridge_telemetry.sqlite3`, which `reset_all` never touched.
Cleared live via the existing `GET /api/telemetry/clear` (also swept 8 stale response files);
and `router.memory_reset(all=1)` now also calls `self.telemetry.clear()` so one "Reset all" wipes
memory + files + telemetry together (**PENDING restart** — it's a `router.py` change).

**◐ Per-save chat/memory indexing (production) — SCOPED, the priority before ship.**
- *The gap.* The in-game chat path normalizes `save_id` to the constant `"chat"`
  (`router._normalize_chat_payload`: `payload.get("save_id") or "chat"`), and the mod's
  `SendToBridge` payload sends no `save_id`. So **every X4 playthrough shares ONE memory +
  conversation namespace** — a new game would inherit the previous game's NPC memories and chat.
- *Goal.* A new X4 game ⇒ a fresh DB index: each playthrough maps to a unique, stable `save_id`,
  and (because all tables are already `save_id`-scoped) a brand-new id is automatically empty.
  No schema change needed — the data layer already indexes by `save_id`; only the *id source* is missing.
- *Approach (mod side).* Generate a per-save UUID once at new-game start and persist it in the
  save: an MD cue on `event_game_started` sets `md.AIInfluence.$save_uuid` only if unset
  (survives saves/reloads, unique per playthrough). Send it as `save_id` in every chat/NPC POST
  (`aic_menu.lua` `SendToBridge` body). Avoid relying on the X4 save *filename* (not reliably
  exposed to MD and changes on every manual save).
- *Approach (bridge side).* Already honors `payload.save_id`; stop silently defaulting to
  `"chat"` — if no `save_id` arrives, reject or tag `unindexed` so the miswire is visible rather
  than silently merging games.
- *Files.* mod chat MD (`$save_uuid` set+send), `aic_menu.lua` (include `save_id`),
  `router._normalize_chat_payload` (drop the `"chat"` fallback).
- *Verify.* Two playthroughs ⇒ two `save_id` chips with fully isolated NPCs/conversations;
  switching between them keeps each intact; a brand-new id starts empty. Until then the test
  workflow is: **wipe between tests** (now complete end-to-end).

---

## 2026-06-22 — Mind-map reconciliation + next build queue (functional plans)

Reviewed the full architecture mind-map against what's actually built. The skeleton is
sound (~85% aligned). Corrections folded in, and the genuinely high-leverage items are
scoped below with functional implementation plans.

**Corrections to the map (so docs match reality):**
- **Player entity — BUILT 2026-06-22 (was missing from the map).** The player is now a
  first-class singleton: `players(save_id PK, current_name, name_history, first_seen,
  updated_at)`. Identity = `save_id` (one player per save). `current_name` is a mutable
  LABEL; a rename appends to `name_history` and never touches the entity → reputation/memory
  keyed to the player survives renames. In-game the chat Lua reads `GetPlayerName()` (FFI)
  and sends `player_name`; the bridge `upsert_player`s it and stamps each conversation row.
  Verified live: chat as "Shawn Holt" → rename to "Stinky DiceMan" keeps one entity with
  history `["Shawn Holt","Stinky DiceMan"]`. Endpoint: `GET /api/player?save_id=`.
- **"SSE Stream Listener" → NDJSON NPC API.** We abandoned SSE/raw chat-completions
  (reasoning-bound) for the Player2 NPC API (spawn → chat → NDJSON). Map node mislabeled.
- **"Fact Retrieval (RAG)" → Categorized Memory Retrieval.** What exists is deterministic
  importance/recency categorization (core/significant/routine) + CORE aging + briefing
  assembly — NOT vector RAG. True vector RAG is a FUTURE upgrade, gated on per-NPC history
  volume (premature now: per-NPC memory fits in-context; embeddings would cost throughput on
  the serialized single-LLM and add non-determinism). If recall ever feels thin first add
  BM25/lexical + recency weighting over the conversation log (80% of the benefit, deterministic).
- **"Joule Usage Management"** moot on the free `gpt-oss-120b` model; stub only when a paid
  model is selected.

### Next build queue — ranked by leverage (functional plans)

**#GSR — Game State Reader (HIGHEST leverage). OPEN `[game-gated]`**
- *Why:* Strategic Awareness (relations / economic bottlenecks / military counts / sector
  ownership) is currently SEEDED in the bridge, not read from the live game. This node is
  what turns "a simulation beside X4" into "an AI that reacts to YOUR game."
- *In-game (MD/Lua):* an MD cue reads live state via X4 script expressions —
  `<faction>.relation.{<otherfaction>}`, player-owned stations/ships per sector, sector
  owner, faction fleet strength — on a throttled tick (e.g. every 30s game-time, and on
  demand before a chat turn). Serialize to a compact JSON and POST to a new bridge endpoint.
- *Bridge:* `POST /api/gamestate/ingest {save_id, factions:[{id,relations:{}}], sectors:[…],
  player_assets:[…], military:{…}}` → upserts the existing substrate tables (factions,
  relationships, sectors, strategic_state) from REAL data instead of seed. `derive_pressures`
  then runs on truth.
- *Verify:* ingest a snapshot → `GET /api/strategic_state` reflects the posted values; a chat
  turn's briefing cites the real numbers. Headless test first with a captured snapshot fixture.

**#REL — Basic Relation Control (Phase-1's unfinished third). OPEN `[game-gated]`**
- *Why:* the closed loop — an LLM decision actually MOVING a faction relation in-game. Proof
  the LLM changes game state, not just talks.
- *In-game (MD dispatcher):* extend the `On_action` handler to accept `set_relation` /
  `adjust_relation` action types → `<set_faction_relation>` / threshold logic
  (WAR_ELIGIBLE=-0.10, etc. from the DeadAir cheat-sheet). LLM never calls this directly —
  it returns a whitelisted label; the dispatcher executes.
- *Bridge:* already emits whitelisted actions; ensure `adjust_relationship` mirrors the
  in-game delta so dashboard + game stay in sync.
- *Verify:* in-game, trigger an escalate decision → confirm the faction relation actually
  shifts (Empire/comms screen) AND the dashboard relationship row updates by the same delta.

**#MEM — Conversation → NPC memory wiring ("Historical Betrayal Reaction"). ◐ NEXT (player entity done)**
- *Why:* the payoff that justifies the whole Memory branch — an NPC that REMEMBERS you.
- *Bridge:* in `_process`, in addition to the conversations debug row, write BOTH lines of
  the turn into the specific NPC's memory via the existing `add_turn`→condense→categorize
  pipeline, keyed to the player ENTITY (not name). Extend `build_situation_briefing` to inject
  a "What you remember about <player current_name (aka past aliases)>" block.
- *Data:* needs a stable `npc_key` per persona (faction+npc_name+save) so memories attach to
  the right NPC. Tag each memory with the player's name-at-the-time for flavor.
- *Verify:* send msg A, then msg B; B's reply references A. Rename mid-stream; the NPC still
  recalls A and can say "you called yourself X then."

**#AUTH — Authority-level checks (tier → action gating). OPEN `[low effort]`**
- *Why:* a Tier-0 deck hand must not be able to proposed `declare_war`; only Tier-3 heads can.
  Makes "LLM proposes, system disposes" trustworthy.
- *Bridge:* in the validator (`scoring`/Stage-2 chooser + action acceptance), filter the legal
  action set by the NPC's `tier`/`authority` columns (already on `npcs`). Reject/replace
  out-of-authority proposals with the deterministic fallback.
- *Verify:* a Tier-0 NPC's escalate proposal is downgraded to `dialogue_only`; a Tier-3's is
  allowed. Add a `selftest` assertion.

**#SAFE — Graceful failure + idempotency (test + harden pass). OPEN `[low effort]`**
- *Why:* protect the save. Bridge-down must degrade cleanly; the same action must apply once.
- *In-game:* the djfhe callback error path already exists — verify it shows a single fallback
  notification, never freezes or error-spams, when the bridge is unreachable. Dedup applied
  actions by `request_id` in the dispatcher (the Lua already tracks `processedRequestIds`;
  make the MD side reject a repeat).
- *Verify:* kill the bridge → send a chat → graceful "comms down" message, no error storm.
  Replay the same action twice → relation moves once.

**#EVT — Event-driven NPC messages (later, medium). OPEN**
- *Why:* "living universe" — NPCs ping the PLAYER proactively on world events.
- *Bridge:* the event queue already coalesces world events; add a path that, on a high-
  importance event involving a faction the player has standing with, enqueues an outbound
  message into `updates_pool` addressed to the player (the Lua poll loop already drains it
  and writes to the logbook).
- *Verify:* inject a `war` world-event for a faction → an unsolicited logbook message from
  that faction's officer appears in-game.

**Sequencing:** #MEM and #AUTH and #SAFE are bridge-side and can be built/tested headless NOW.
#GSR and #REL need the in-game test loop (gated on the current launch). #EVT after #GSR.

---

## 2026-06-22 — X4-side execution layer validated + DeadAir leverage decision

**Two big things landed: we can validate X4 mod files without launching the game, and we found we don't have to rebuild the in-game action machinery.**

### Schema-validation loop established (the Forge)
- The Forge = **X4-Foundations-Mod-Studio** (Express+React app, `localhost:3000`, at `C:\Users\Moshi\.gemini\antigravity-ide\scratch\X4-Foundations-Mod-Studio`). It loads the **real game XSD** (`md.xsd` + `common.xsd` from `F:\DEV_ENV\projects\Mods\X4Mods\Schema`; 590 events / 765 actions / 91 conditions) and live-validates MD scripts. Use its **Single File Parser** (Load Mod Project → paste/drop a `.xml`) for one-file schema checks; the COMPILER + Diagnostics panel report results. It already knows `djfhe_http` in the installed-mod ecosystem.
- **Dispatcher proven:** loaded the old `ai_influence_actions.xml` (the execution layer) into the Forge → **COMPILER: OK**, **"all live flowchart validation checks satisfied (valid)"**, **0 critical / 0 warning**. The only note — "5 long-tail (generic fallback)" — is informational (`set_faction_relation`, `write_to_logbook`, cross-mod DeadAir signals carried as valid Custom-XML passthroughs, not errors). So the in-game **execution half is schema-valid** (it already ran in X4; this confirms it).

### The execution mechanism — settled understanding
The LLM **never touches the game**. Universal pattern (Bannerlord, X4, anything): **game gathers state + a fixed menu of legal moves → LLM returns a structured *choice* (a label, just text) → a deterministic *adapter* calls the game's real API.** In X4 the adapter is the **MD dispatcher** (`Dispatch` reads `"declare_war"` → runs `Handle_DeclareWar` → `<set_faction_relation>` / DeadAir signal). The LLM is the chooser; the dispatcher is the hand. Must run **LLM-off** (determinism is the engine; LLM is flavor). This is the same `action → effects` contract as the headless "simulated world model."

### Architecture decision — DeadAir is a REFERENCE, not a dependency
**Decision (Ken):** we do **not** depend on, signal, or copy DeadAir. No "requires DeadAir Scripts" in the mod description. This is **X4_AI_Influence — standalone**. DeadAir already did the R&D — *how* to do dynamic wars / relation shifts / logbook news inside X4 MD — and we **learn the technique and write our own native handlers.**

`deadairdynamicuniverse` (1,294 cue nodes, reconstructed in the Forge under `F:\DEV_ENV\projects\Mods\X4Mods\deadairdynamicuniverse\.snapshots\`) is our **reference cheat-sheet** for the X4 verbs/patterns:
- **Dynamic War / relations** — how it sets `set_faction_relation`, crosses war/peace thresholds, tracks conflicts (cues like `EventDynamicWarTrackEvent`, `RelationsFix`). We copy the *approach*, implement our own.
- **Dynamic News** — how it writes logbook/news (`EventDynamicNewsTracking`/`Output`, `write_to_logbook`). We implement our own news handler for immersion.
- **Bonus economy** — how `EventEvolution*`/`EventGod*`/`EventJobs*` build ships/stations/jobs — reference for future economic actions.

**Consequence:** our action whitelist handlers are **our own native X4 MD** (`set_faction_relation`, `write_to_logbook`, etc.), self-contained, no external mod calls. Same `action → effects` contract as the headless world model. **OPEN:** mine the DeadAir snapshot for the exact native verbs/patterns (a documented cheat-sheet), then implement our own handlers.

---

## 2026-06-22 — Full storage surface + Player2 pipeline proven — DONE (live-verified)

This session moved the universe **data model** from 3-of-9 domains to **all of it**, and separately proved the **Player2 pipeline** handles real traffic. Two different axes — both now green for what they cover. Reconciling against the old gap table:

| Domain / capability | Before | Now | Note |
|---|---|---|---|
| (1) factions + relationships | ✅ | ✅ | storage + endpoints + dashboard |
| (2) strategic_state + scoring (Stage 1) | ✅ | ✅ | scoring brain reads pressures → ranked legal options |
| (3) incidents / pending_actions | ❌ | **◐** | **table + whitelist enforcement + endpoints + dashboard built; full Stage-3 validator (bounds/cooldown/idempotency/confirmation) NOT yet** |
| economy + player_market | ❌ | ✅ | storage + endpoints + dashboard (meaning, not a market) |
| sectors (territory) | ❌ | ✅ | storage + endpoints + dashboard |
| conflicts (war) + war_losses (windowed `recent_losses`) | ❌ | ✅ | `get_loss_summary` windows losses → 0..1 pressure |
| agreements (promises/deals) | ❌ | ✅ | storage + endpoints + dashboard |
| persistent world_events | ❌ | ✅ | typed log + importance-aware pruning (cap 2000/save) |
| npcs enrichment (tier/authority/bound entity) | ❌ | ✅ | migrated columns |

**Also built:** every new table is `save_id`-scoped, wired into `clear_save`/`reset_all`, indexed, and shown as a read-only dashboard panel (the front end is the DB, for debugging). `run_universe_selftest` = **15/15** live. WAL + relaxed-sync DB optimization. Idempotent demo seed (`clear_substrate` first). A **Player2 end-to-end stress harness** (`/api/player2/stress`, background job + status poll + dashboard panel) — separate from the DB stress.

**Player2 pipeline result (the test that mattered):** 200 real prompts → bridge → Player2 NPC API → replies, **200/200 ok, 0 empty, 0 errors**, sustained ~5.5 min. Latency p50 1.63s / p95 2.30s / max 3.89s; throughput 36/min. Ceiling is the single serialized local model (~30–36 conv/min), not the bridge.

**HONEST GAP — data is stored, but nothing COMPUTES or ACTS on it yet.** Two consequences, same root:
- `strategic_state` pressures are still **hand-set by the seed**. Nothing reads economy shortages / conflict losses / sector contest → pressures. The substrate tables exist but the **deriver** that turns them into pressures does not.
- The 200-call replies were **hollow** (generic "all sectors secured") because the prompts injected **no** real state — no memory, faction, relationship, or world context. The `build_memory_context()` + universe context is built but **not injected** into NPC prompts. (The single grounded Reyes call proved injection works; it just isn't the default.)

So: **storage ✅, scoring brain ✅, but the engine that derives the driving factors and acts on them — and the grounded context that makes NPCs feel alive — is the remaining work.** That is the next phase.

---

## Remaining build — "the factors that drive the universe" (the LOGIC layer)

Ordered. Everything below is logic over the now-complete data surface.

1. **The PRESSURE DERIVER (keystone).** A deterministic function: substrate (economy shortages + dependency, conflicts + windowed losses, sectors contested, relationships, recent world_events) → computed `strategic_state` pressures per faction. This is what makes pressures **emergent** instead of hand-seeded. Without it the whole engine is a demo. Build + selftest first.
2. **Full Stage-3 VALIDATOR.** Beyond the whitelist already enforced: re-check still-legal, numeric bounds, cooldown clear, player-confirmation flag, idempotency → only then write the incident with bounded `effects_json`. Closes the loop so the LLM is a bounded chooser, never authority.
3. **STRATEGIC-REVIEW LOOP.** Repurpose the EventQueue worker into a slow-cadence (~10–60s) per-faction pass: derive → score → pick (LLM Stage 2, or deterministic fallback) → validate → write incident → emit `world_event`. This is what makes the universe act when the player isn't looking.
4. **Deterministic fallback TIEBREAKER.** Per-action pressure affinities so the LLM-off path can pre-rank the `escalate` vs `ceasefire` tie without a model.
5. **GROUNDED CONTEXT INJECTION (immersion).** ✅ **DONE — live-proven 2026-06-22.** `MemoryStore.build_situation_briefing()` assembles personal memory (CORE facts + gist) + faction mood/goal + directed standing toward the player + active wars + contested home sectors + recent world_events; `npc_complete` injects it on every NPC turn. Grounded single-NPC demo (`/api/grounded/run`, dashboard panel showing briefing + transcript): Captain Voss, 5 turns, all `ok`, ~2s/turn — and the replies reference the **real** universe: Admiral Vance's death, the oath to hold Hatikvah's Choice, the player's past resupply ("I have not forgotten it"), the Split's broken ceasefire, the hull-parts shortage, and a concrete ask ("2,000 hull-parts to Hatikvah's dock bays, L-class interceptors, Split cruiser vectors"). Same free model that produced hollow filler under empty prompts — the difference is entirely the injected context. **Immersion de-risked.** Open follow-up: persona consistency over *long* conversations (10+ turns) and across many distinct NPCs is still unproven.

---

## 2026-06-22 — 2000-NPC burst found the write wall → memory-lifecycle redesign

**The test (for science):** 2000 mixed NPCs (faction reps, fleet admirals, pilots…) each lived a random stream of CORE/significant/routine events through the full memory pipeline. **Result: it works — no crash, no error, nothing dropped — but it took ~10+ minutes.** Not a logic problem; a **write-throughput wall**: the memory store opens a **fresh SQLite connection per operation**, and 2000 × ~64 turns = **~256k open+commit cycles** serialized. (Synthetic worst case — a real game spreads NPC turns over hours, never a quarter-million condensations in a burst — but it's the ceiling of the write path.)

**Fixes shipped this phase:**
1. **Persistent thread-local connection** (kill the per-op open) → ~10–50× faster writes; 2000 should finish in ~1 min.
2. **Dead-NPC pruning** — delete an NPC + its turns/facts by `npc_id` (X4 calls this when a crew member/ship dies, so the DB never bloats with the dead).
3. **Save isolation** — everything is `save_id`-scoped (npc_key = `save_id|game_id|persona`); a NEW game passes a fresh `save_id` so it never inherits an old game's memories. `list_saves` is the index; `clear_save` purges one. (Already present — reinforced + documented.)

### Memory lifecycle — grounded in how real memory works (the "70-yo veteran" model)
We can't keep CORE verbatim forever (unbounded + unrealistic). Memory now ages in stages — you forget the *details*, not *that it happened*:

| Stage | What | Fate |
|---|---|---|
| **Working** (raw turns) | last ~8 exchanges, full fidelity | trimmed as the window overflows |
| **Consolidation** (condense) | overflow crushed to categorized facts | routine **forgotten**, significant condensed, CORE kept |
| **Recent significant** (facts) | deals/battles/threats | LRU-capped (~40/NPC), use-it-or-lose-it decay |
| **CORE — Vivid** | the most recent/important defining events (cap ~8/NPC) | **verbatim** — "I held the line after Admiral Vance fell at Argon Prime" |
| **CORE — Faded** | older CORE beyond the verbatim cap | **blurred to a category gist**, verbatim flag cleared — "You lost a commander you respected, long ago." The event sticks; the words go. |
| **CORE — Distant residue** | beyond a higher cap (~20) | oldest CORE of a category **merged into one lifetime-residue line** ("Over the years you have buried many comrades."), specifics dropped — emotional weight without detail |
| **Gist** (rolling summary) | one-paragraph "who I am / what I've lived" | rebuilt from CORE + top significant |

So a battle-scarred admiral keeps a handful of vivid defining memories, a blur of older ones, and a one-line sense of a long hard life — bounded, and it *feels* like a person, not a database. Implemented in `decay()` as a CORE-aging pass with caps `max_core_verbatim_per_npc` + `max_core_per_npc`.

### Game-time model — memories age in GAME time, on X4's own clock
**Bug in the current model:** memories are stamped with real wall-clock `time.time()` — they age by how long the *Python process* has run, which is wrong (close/reopen the game, or SETA-jump years, and aging breaks).

**The fix uses X4's real clock — confirmed `player.age`.** X4 exposes elapsed game-time to MD as `player.age`; **DeadAir uses it 57×** (timers, event stamps, durations like `(player.age - @$start).formatted.default` — X4 even formats durations for us). It advances with **SETA/time-compression**, so it's the correct basis for aging. We don't invent the clock — we use `player.age`.

Model:
- The mod **sends `player.age` with every request**; the bridge stamps every turn / fact / `world_event` with it (game-time, alongside or instead of wall-clock).
- **"The war was 40 years ago"** = `now − event.game_stamp` (a duration; no calendar needed — `player.age` is elapsed time, not a date).
- **NPC ages we DO invent** (X4 doesn't age crew): stamp `birth_game_time = player.age − drawn_age` on first contact → *"Vance is 70"* = `now − birth`. They age as the game runs.
- **CORE fade + decay thresholds become game-YEARS** (e.g. blur after ~30 in-game years) instead of process uptime — real, SETA-correct aging.
- An absolute calendar ("the year is 1247") is **optional flavor only** — not required for durations.

*Open:* add `game_time`/`birth_game_year` columns + a `game_time` request field; compute all "how long ago / how old" as `player.age` deltas. This is foundational — it makes aging real and powers "long ago." Build before deep memory-narrative work.

---

## Realized goal — closed-loop faction-tension SIMULATION (no game yet)

**The target (Ken, 2026-06-22):** run the *entire* influence pipeline as a self-contained simulation in our DB + Player2 — factions reasoning over tensions, deciding, acting, and reacting **over time** — and watch it evolve in the dashboard, BEFORE any X4 integration. Not just ship NPCs talking: faction leaders making decisions that change the universe, which other factions then respond to. If this loop produces believable, self-sustaining faction dynamics purely in our database, the X4 integration becomes "just" swapping the simulated world model for real X4 reads/writes — the whole design is de-risked without touching the game.

**The loop — one simulation tick, per faction:**
1. **Derive** pressures from substrate (economy, conflicts + windowed losses, sectors, faction↔faction relationships) → `strategic_state`.
2. **Score** (Stage 1, deterministic) → ranked legal options. ✅
3. **Decide** (Stage 2): the faction-leader NPC (via Player2) receives its situation briefing + the ranked legal options, picks one, narrates why. Deterministic fallback when LLM-off.
4. **Validate** (Stage 3): re-check still-legal / bounds / cooldown / idempotency → write `incident` with bounded `effects_json`.
5. **Apply effects — the SIMULATED WORLD MODEL.** *This is the missing keystone for "no game".* With no X4 to be the authority, a deterministic world model applies the incident's whitelisted effects back onto our OWN tables: `escalate_pressure` → resentment↑ + conflict intensity↑ + losses logged; `ceasefire_feeler` → trust↑ + agreement row + intensity↓; `resource_request` → economy/debt shift; etc. In the shipped mod, **X4** does this. In the sim, this module **stands in for X4.**
6. **Emit `world_event`** + update relationships → feeds the NEXT tick's derive, so tensions **spiral or de-escalate** on their own. Ship NPCs' briefings automatically carry the new state, so their dialogue reflects the evolving war with no extra work.

**Component checklist toward the realized goal:**

| Component | Status |
|---|---|
| Substrate storage (all domains) | ✅ |
| Scoring (Stage 1) | ✅ |
| Grounded NPC injection (ship-level immersion) | ✅ |
| **Pressure DERIVER** (substrate → emergent pressures) | ◐ NEXT |
| Faction↔faction tension as a **bidirectional** signal (derive reads it; world model writes it) | ❌ |
| **Stage 2 faction-leader DECISION** (LLM picks among legal options + narrates) | ❌ |
| Deterministic fallback **TIEBREAKER** (LLM-off path) | ❌ |
| **Stage 3 VALIDATOR** (full gate, not just whitelist) | ◐ whitelist only |
| **SIMULATED WORLD MODEL** (effects applier — the X4 stand-in) | ❌ keystone for "no game" |
| **SIMULATION DRIVER** (tick engine: run N cycles, advance the universe) | ❌ |
| **Influence currency** (spendable action budget per faction) — *Bannerlord* | ❌ |
| **Anti-snowball pressure** (global balancing term) — *Bannerlord* | ❌ |
| **Desire-threshold pacing** (accumulate-then-act) — *Bannerlord* | ❌ |
| **Internal-faction voting** (sub-leaders vote, weighted) — *Bannerlord, deferred* | ❌ later |
| **Observability** (tension matrix over cycles, incident/event timeline, pressure trends) | ❌ |

### Bannerlord-derived mechanics (planned 2026-06-22)

Lessons stolen from the *AI Influence [AI Diplomacy]* / *WarAndAiTweaks* / *Diplomacy* mods. The deterministic core of those mods is the same desire-accumulator + self-interest-scoring + legality-gate pattern as ours; these four are the things they do that we don't, folded into the loop so the sim stays dynamic instead of deadlocking or snowballing.

1. **Influence as a currency/cost.** Add `factions.influence REAL DEFAULT 0` (migration). Each tick the **deriver** regenerates influence as `f(territory_count, production_health, at_peace_bonus)`. Each action has a **cost** (`INFLUENCE_COST` map per `action_type`, scaled by priority); the **simulation driver** only fires an incident the faction can afford, and the **world model** deducts the cost on apply. Effect: weak/poor factions ration their aggression; a dominant faction can throw weight around. Gates "every faction acts every tick."
2. **Anti-snowball balancing.** The **deriver** computes a per-faction `dominance` score (sectors owned + production_health + active-war win record). A global term then (a) **boosts** every other faction's escalate-score *toward the leader* (coalition pressure) and (b) **dampens** the leader's own expansion score. Tunable `SNOWBALL_*` constants. Keeps the map from going static once someone pulls ahead.
3. **Desire-threshold pacing.** Replace "score every tick → act" with an **accumulator**: new table `faction_desire(save_id, faction_id, action_type, target, desire REAL, updated_at)`. Each tick `desire += scored_pressure`; an incident fires **only** when `desire ≥ DESIRE_THRESHOLD` *and* influence is affordable, then desire resets and a cooldown starts. Produces believable buildup ("war-desire rising") instead of twitchy per-tick flip-flopping. Exposed on the dashboard so you can watch desire climb toward the threshold.
4. **Internal-faction voting (deferred).** Once the monolithic loop is proven, resolve a faction's decision by its sub-leaders voting — reuse the existing `npcs.tier` / `npcs.authority` columns; each sub-leader scores by self-interest, votes weighted by tier × influence, majority/weighted-pick wins. Adds internal politics (a hawkish admiral vs a cautious quartermaster). **Not** built until the single-actor loop is self-sustaining.

**Build order (deterministic loop FIRST, LLM on top, Bannerlord mechanics woven in):**
1. **Deriver** — substrate → pressures (emergent, not hand-set). *Includes the `dominance` score (anti-snowball input) and influence regen.*
2. **Influence currency** — `factions.influence` column + regen (in deriver) + `INFLUENCE_COST` map. Cheap, build alongside the deriver.
3. **Simulated world model** — apply incident effects back to the DB (the X4 stand-in); deduct influence cost on apply; bump dominance/relationships/conflict accordingly.
4. **Desire accumulator + anti-snowball term** — the `faction_desire` table and the snowball scoring term; both feed the driver's fire/hold decision.
5. **Simulation driver** — the tick engine: derive → score (+anti-snowball) → accumulate desire → if `desire≥threshold & affordable` → (deterministic fallback pick) → validate → incident → world model → world_event. Run N cycles. Prove a **self-sustaining deterministic** war/peace cycle, LLM OFF.
6. **Stage 2 LLM faction decision + tiebreaker** — a faction-leader NPC picks among legal affordable options and narrates; deterministic fallback stays underneath.
7. **Observability** — dashboard timeline + tension matrix + desire/influence trends so the evolving sim is watchable.
8. **Internal-faction voting** — deferred enhancement to step 6.

---

## Backend hardening — make backend-vs-mod-vs-Player2 unambiguous (2026-06-22, IN PROGRESS)

**Why (Ken):** before the mod exists, the backend must be solid and *self-diagnosing*, so a failure during mod-building immediately tells us whose fault it is — the mod, Player2, or the bridge — instead of leaving us guessing.

- **Fault-source taxonomy** ✅ (landed in `telemetry.py`): every event/request is classified `ok | test | client | upstream | bridge`. `test` = built-in harness traffic (ignore); `client` = a bad request from the caller/mod; `upstream` = Player2/model failed (e.g. the "no text content" degrade — not our bug); `bridge` = a real bug in our code. Snapshot rolls up `source_counts`, `real_faults`, `bridge_faults`. *Example: the three red entries you flagged classify as `test` (smoke harness) — the `../bad` is `client` (validator working), the empty completion is `upstream` (the model), neither is a bridge fault.*
- **Telemetry clear** ✅ (`/api/telemetry/clear`): wipes telemetry + resets live metrics + drops cached responses/files, so the dashboard reflects only current traffic and any red afterward is real.
- **One-shot backend verdict** ✅ (`/api/selftest/all`): runs memory + universe + scoring self-tests + Player2 reachability → single green/red. Green = backend sound.
- **Growth caps** ✅: telemetry pruned to bounded row counts; in-memory `responses`/`updates` capped (disk files remain the durable record). (closes the old unbounded-growth gap)
- **TODO this phase:** dashboard surfacing of `source` chips + the clear/selftest buttons + `source_counts`; an adversarial **fuzz pass** (throw malformed/oversized/concurrent/unicode payloads at every endpoint, confirm graceful 4xx not 5xx); per-source counters in the top band.

## Integration track — toward plugging into X4 (NOT yet, but plan for it)

We are still proving the system headless (DB + Player2). These are the things the link must respect when we *do* integrate, captured now so they don't surprise us:

1. **`djfhe_http` is the X4-side transport.** Architecture is **X4 (Lua/MD) → `djfhe_http` extension (HTTP client) → our bridge `:8713` → Player2**.

   **Code analysis (2026-06-22, read in full) — NOT a bottleneck.** luasocket + luasec, **non-blocking + callback-based**: sockets are `settimeout(0)`; connect/TLS-handshake/send/receive advance incrementally, so the game thread never blocks on Player2's 2–3s reply. MD cue polls every 50ms, real-time-paced (SETA-guarded via `GetCurRealTime` delta). Concurrent in-flight requests supported. Localhost-http (no TLS, so per-request `Connection: close`/no-pooling is ~free). Body completion via **Content-Length** — which our bridge always sends. ✓ Player2's ~36/min single-model ceiling dominates; djfhe handles that trivially. **Confidence ~90% non-issue.**

   **Four design rules the mod MUST follow (from the code):**
   - **Small responses.** `doReceive` reads ≤8192 B per 50ms poll per connection → ~160 KB/s per-connection drain cap, *independent of network*; plus O(n²) buffer concat for big bodies. Keep each response **< ~8 KB**; batch/paginate large state syncs.
   - **Batch, don't spam.** `Client.update()` loops every in-flight request each 50ms poll on the UI thread → O(N). Few batched requests, NOT one-per-NPC-per-tick (matches our event-queue design).
   - **Never stream to X4.** No chunked-transfer-encoding support (literal `TODO`); relies on Content-Length. Bridge keeps sending Content-Length, no SSE/chunked on X4-facing endpoints.
   - **~50ms completion-detection latency floor** — negligible vs the LLM.

   **Non-perf gotcha (adoption):** loading the native DLLs (`ssl.dll`, `core.dll`) requires **Protected UI Mode OFF**, which **disables Steam achievements** for every user. Real friction to flag in install docs. (Also `verify="none"` TLS — moot on localhost.)

   *Action at integration time:* contract test that round-trips a real bridge response (incl. a larger faction/relationship sync + a full briefing) through `djfhe_http`, confirming Content-Length completion under the 8 KB-poll drain.
2. **Teach the Forge our contracts so it produces correct mod artifacts — ◐ DONE (2026-06-22).** *Key correction:* the X4 mod talks to **our bridge**, never Player2 directly (`X4 → djfhe → bridge → Player2`). So the artifact-shaping contract is the **bridge's**, not Player2's.
   - **djfhe registered** in the Forge's api-registry (`<Forge>/data/api-registry/djfhe_http.json` + runtime register) with a **correct scaffold** — fixes the Forge generating the wrong `Request:new({})` call; it now emits djfhe's real `Request.new("POST"):setUrl():send()`. (The registry is "soft, non-schema-grade" — it enforces dependency declaration + usage detection, not call signatures; the in-game test covers the rest.)
   - **Bridge contract is now self-describing:** `GET /api/contract` (live source of truth — endpoints, request/response shapes, the action whitelist the dispatcher must handle, a djfhe example) + a versioned snapshot at `docs/neural_link_contract.md`. This is what the Forge/author references so mod + bridge never drift.
   - **Player2 API** = reference for *bridge* dev only (the bridge exposes it live at `/api/player2/catalog`, capability-classified); not a contract the mod implements.
   - *Forge note:* `derive?ext=<mod>` drafts an api-registry def from an installed mod; it misses `require`d-module method chains (gave djfhe a thin def) — hand-tune those. Optional future Forge improvement.
3. **Retire the orphan `G:\…\extensions\x4_neural_link`.** Stale leftover from the retired F:→G: deploy model (last touched ~02:45, before the F:\-only watcher). Not where we develop (live bridge runs from `F:\…\x4_ai_influence\x4_neural_link`, confirmed via `/health`). Delete it in Explorer to remove a copy-confusion magnet. The eventual *in-game* extension (content.xml + md + ui Lua) is a separate package we build later — not this folder.

---

## 2026-06-22 — Consolidation + de-hardcoding (run from anywhere) — DONE (sandbox-verified)

**Scope framing (Ken):** we are *only* developing the Neural Link + its database right now, to prove the whole system can hold everything the AI Influence mod will need. We are **not** building AI-Influence gameplay yet (Option B: AI-Influence substrate may live in this workspace, but in its own files; the bridge stays generic-capable).

**Why this phase:** there were three drifting copies (old root `…\X4Mods\x4_neural_link`, this nested copy, and a `G:\…\extensions\x4_neural_link` deploy target). The old `Deploy-And-Restart.ps1` hard-coded a staged `F:\` source and a live `G:\` target and did an F:→G: robocopy on every edit. Ken moved the `G:\` copy off to the Desktop and declared **this nested copy the only one we work on** — so the F:→G: deploy model is dead.

**Done:**
- **`Deploy-And-Restart.ps1` rewritten to run + watch in place.** `$Root = Split-Path -Parent $MyInvocation.MyCommand.Path`; no `$Staged`/`$Live`, no robocopy. It compile-gates `bridge/*.py` (now incl. `scoring.py`), runs `python -m bridge.server -WorkingDirectory $Root`, and watches `$Root\bridge` + `$Root\config`, reloading in place on edit. Compile error keeps the previous bridge alive.
- **`Start-Neural-Link.ps1`** confirmed already `$Root`-relative (no change needed).
- **Bridge Python already path-clean:** `server.py` derives `root = Path(__file__).resolve().parents[1]`; `config/player2_config.json` has no filesystem paths.
- **`HANDOFF.md`** operational header rewritten: single working copy, run-in-place, no F:→G: split.
- **No hard-coded drive paths remain in any code or config** (`.py/.json/.ps1/.bat`). Remaining `F:\`/`G:\` strings are only in historical doc ledgers (ROADMAP "files-touched" snapshots, old HANDOFF references) — left as dated history.

**Verification (sandbox):** copied the folder to an unrelated path (`/tmp/nl_check`), `py_compile` of all 8 bridge modules **OK** (memory 971 / scoring 276 / router 397 / server 278 lines), started the bridge from that arbitrary path → `GET /health` `ok:true` with `telemetry_db` resolved to `/tmp/nl_check/runtime/…` (proves root is `__file__`-derived, not hard-coded); `GET /api/strategic/selftest` **7/7**. (Player2 `connection refused` expected — that app is host-only.) **To bring it back up on the host:** double-click `Deploy-And-Restart.bat` in this folder.

**Housekeeping OPEN:** the redundant old root copy `…\X4Mods\x4_neural_link` still exists. It is not used by anything anymore. Recommend deleting it to leave one true copy — deletion is permission-gated, so it stays until Ken says remove it.

---

## Build plan (scoped) — after consolidation  ·  ⚠️ SUPERSEDED 2026-06-22

> **Superseded by the "Full storage surface" entry + "Remaining build" section above.** Item 3's *substrate domains* and the incidents *table* are now BUILT; what remains is the LOGIC (deriver → validator → review loop → tiebreaker) + grounded injection. Kept below as the original plan-of-record.

The substrate so far stores universe *meaning* (factions, relationships, strategic_state) and Stage 1 turns it into ranked legal options deterministically. The remaining build, in order:

1. **Decision OUTPUT — `incidents`/`pending_actions` table + validator (build-order item 3, NEXT).** Make the action whitelist concrete: `action_type, target, faction, confidence, priority, cooldown_until, narrative, effects_json, status`. The validator is the deterministic gate Stage 2's LLM pick must pass before anything is "applied" — it closes the loop so the LLM is a bounded chooser, never authority. Endpoints + dashboard panel to watch incidents accrue.
2. **Strategic-review loop (item 4).** Rewire the `EventQueue` worker into score → LLM pick → validate → write incident, on a slow cadence (~10–60s hot, minutes broad) — never per tick. Keep the deterministic fallback so it runs LLM-off.
3. **Substrate domains that *derive* the pressures (so strategic_state isn't hand-seeded).** Economy first (who depends on whom, trade pacts, supplied-our-enemies grudges → `economic_pressure`/`recent_losses`), then conflicts/sectors, then agreements. Plus a persistent `world_events` log feeding `salient_memory`.
4. **Deterministic fallback tiebreaker** for the LLM-off case (the documented `escalate` vs `ceasefire` tie at equal score) — per-action pressure affinities so determinism can pre-rank without the LLM.

---

## 2026-06-19 — `strategic_state` + deterministic scoring core (Stage 1) — DONE (live-verified)

**Build-order item 2 of the influence engine.** Item 1 (`relationships` + `factions` endpoints + dashboard) is done/verified; this adds the **decision input** (pressure aggregates) and the **deterministic scoring core** that turns stored universe state into a ranked list of legal candidate options — with **no LLM**. This is Stage 1 of the 3-stage engine (score → LLM picks + narrates → validate → X4 applies).

**Built:**
- **`strategic_state` table** (`memory.py`, `save_id`-scoped, keystone PK `(save_id, faction_id)`): `military_pressure, economic_pressure, logistics_stress, recent_losses, territorial_pressure, piracy_pressure, player_alignment` (0..1; player_alignment −1..1) + `updated_at`. Methods `upsert_strategic_state` (partial-merge), `get_strategic_state`, `list_strategic_state`. Covered by `clear_save` + `reset_all` (save-scoping invariant held).
- **`bridge/scoring.py` — the scoring core.** Pure, stdlib-only, DB-agnostic (operates on dicts → unit-testable, reusable by the review worker). Implements the documented weighted formula exactly:
  `score = 0.30·military + 0.20·economic + 0.15·recent_losses + 0.10·logistics + 0.10·(−hidden_affinity) + 0.10·salient_memory + 0.05·player_alignment − 0.40·cooldown_active`.
  `hidden_affinity = (trust−resentment−fear)/100`; `salient_memory = (|resentment|+|debt|)/100`. Weights in `DEFAULT_WEIGHTS` (per-profile overridable). Candidate actions (`dialogue_only`/`defensive_stance`/`resource_request`/`escalate_pressure`/`ceasefire_feeler`) are **gated by pressure thresholds** then scored + ranked; the dialogue baseline is always kept (always ≥1 legal option = the deterministic fallback).
- **Endpoints:** `GET/POST /api/strategic_state`, `GET /api/strategic/score?save_id=&faction=`, `GET /api/strategic/selftest`. `universe/seed` now also seeds demo pressures (`seeded_strategic_state:6`) so the demo universe is immediately scorable.

**Verification (live + host):** host `py_compile` of all four modules OK. `bridge/scoring.py` selftest **7/7** both standalone and via `GET /api/strategic/selftest`: formula matches hand-calc (0.455), **Split (high aggression + resentment→Argon) ranks `escalate_pressure(argon)` #1**, peaceful **Boron generates no escalation** (only `dialogue_only`), an active cooldown applies the −0.40 penalty (0.455→0.055) and **demotes escalation below the benign baseline**. Live `GET /api/strategic/score?save_id=demo&faction=split` → `escalate_pressure→argon` 0.56 top; `faction=boron` → only `dialogue_only` 0.0925. `strategic_state` list = 6. Watch-mode auto-deployed (live endpoints answered the new routes without manual restart).

**Honest observation (not a bug):** `escalate_pressure` and `ceasefire_feeler` toward the same target **tie** (both 0.56 for Split→Argon) — the documented formula is target-driven and differs across actions only by cooldown. That tie is precisely the "close call" Stage 2 (the LLM) exists to break ("escalate vs sue for peace") and narrate; the deterministic layer correctly surfaces both as legal high-scoring options. A future refinement could add per-action pressure affinities (e.g. recent_losses biases toward ceasefire) if we want determinism to pre-rank them.

**Next (item 3):** `incidents`/`pending_actions` table + the legal-action validator (the action whitelist made concrete) — then item 4 rewires the `EventQueue` worker into the strategic-review loop (score → LLM pick → validate → incident).

---

## 2026-06-19 — NPC API path proven end-to-end ✅

**Key finding:** Player2's default chat model (GLM-4.7-Flash) reasons *compulsorily* — confirmed against Z.AI's official GLM spec ("will think compulsorily"; thinking enabled by default). `max_tokens` counts the reasoning tokens, so raw `POST /v1/chat/completions` burns the budget on hidden reasoning and frequently returns **empty `message.content`**, with 5–30s latency. The `thinking:{type:"disabled"}` off-switch is **ignored by Player2's local proxy** (verified: 64-token budget fully consumed, empty content). The app's "Thinking Mode" toggle does not affect the developer API either. Decompiling other integrations won't help — the official docs and the HalfstarDev Defold extension both show the chat endpoint has no reasoning knob.

**Resolution — use the NPC API, not raw chat completions.** Player2's `/v1/npc/...` endpoints (`spawn → chat → responses → kill`) return clean `message` + a `command` field reliably with the same model. The `responses` stream is **newline-delimited JSON** (`{npc_id, message, command, audio}`), NOT `data:`-prefixed SSE — the bridge must parse it as NDJSON and match on `npc_id`.

**Built into the bridge:**

- `Player2Client.npc_spawn`, `_ensure_npc` (persona-cached), `npc_chat` (opens the responses stream, posts chat, parses NDJSON server-side, strips the `<Speaker>` prefix, auto-respawns on a 404/expired NPC).
- `npc_complete(request)` derives persona/system_prompt/game_state from the request; `command` maps to `NeuralResponse.actions` (the action-whitelist hook).
- Router: requests with `target.mode:"npc"` (or `channel:"npc"`) take the NPC path; raw chat completions stays the fallback. Chat calls are serialized (single local model).
- New `POST /api/player2/npc_chat` diagnostic endpoint.

**Verified live (through the bridge, server-side):** an X4-style request — Argon Captain Reyes persona + game-state "two Xenon K destroyers inbound, shields low" + "Captain, what are your orders?" — returned in 4.3s:

> "Maintain distance and fall back toward the defense station. We cannot engage those destroyers with depleted shields."

Status `ok`, no empty content, recorded in telemetry. NPC replies run ~1.7–4.3s vs raw chat's 5–32s.

**Bridge fixes also shipped:** chat timeout 30s→90s; `max_tokens` floored at 512 with a retry at 1024 (raw-chat fallback); concurrency gate so simultaneous requests don't thrash the single local model.

**Open / next:** wire real `command` function-calls (define the X4 action whitelist + NPC `commands` at spawn); decide local-app vs hosted Player2 endpoint; the in-game X4 MD/Lua call into `POST /v1/request` with `target.mode:"npc"`.

---

## 2026-06-19 — Universe-state durable schema (data model) ◐ IN PROGRESS (relationships + factions live)

**Progress 2026-06-19:** `factions` + `relationships` tables built (save_id-scoped, migrated in place) with `upsert_faction`/`list_factions`/`adjust_relationship`/`list_relationships` (clamped −100..100). Exposed: `GET/POST /api/factions`, `GET/POST /api/relationships`, `GET /api/universe/seed`. Dashboard ships **Factions** (biases + goal + mood) and **Relationships** (directed trust/fear/resentment/debt + standing, color-coded) panels. Verified live: `seed?save_id=demo` → 6 factions + 6 relationships; read back + rendered correctly (Split aggr 0.85 hostile→Argon; Boron pacifist ally; Teladi creditor of player). Auto-deployed by watch mode. Covered by reset/clear + the save index. **Next: `strategic_state` (pressure aggregates) + the deterministic scoring core.**


**Core principle — live vs durable (this defines what we store):** X4 owns the live simulation; our DB owns durable *meaning*. Live numbers (current prices, ware stocks, real ship counts, who-owns-what right now, player credits) are **read fresh from X4 each turn and never stored** — storing them is instantly stale + redundant with the save. Our DB stores the political/economic/strategic memory X4 does NOT model: relationships, grudges, debts, deals, each faction's importance/dependency/goals, conflict history, and the events that explain them. Each turn the bridge **joins** live X4 state + the durable index to build context. So "economy" in our DB ≠ a commodity market; it = economic *meaning* ("player is Argon's dominant hull-parts supplier", "Teladi depends on us", "trade pact active", "supplied our enemies → grudge").

**Gap analysis (what the universe needs vs what's built):**

| Domain | Durable data | Status |
|---|---|---|
| Memory | conversations, condensed facts, decay | ✅ `turns`, `facts` |
| NPCs/Leaders | identity, stats | ◐ `npcs` — missing tier/authority/faction link |
| Factions | personality, biases, goal, mood | ❌ only a `faction_id` string |
| Relationships | trust/fear/resentment/debt — player↔faction + faction↔faction | ❌ blueprint §13.1 keystone, never built |
| Promises/Deals | terms, deadline, kept/broken | ❌ (facts are text, not queryable) |
| Economy | importance, dependency, shortages, pacts, restrictions, player market dominance | ❌ nothing (the flagged gap) |
| Territory | sector ownership, contested, strategic value | ❌ |
| Military/War | active conflicts, intensity, aggregated losses / war-fatigue | ❌ (old mod had `war_losses`; not ported) |
| World events | persistent typed history | ◐ event *queue* is transient; no durable `world_events` |
| Ops | idempotency, telemetry | ✅/◐ |

We built the **memory spine** well; the **universe-state index** is mostly empty. That's this milestone.

**Proposed schema (all `save_id`-scoped):**
- `factions` — id, name, values, strategic_biases (aggression / economic_focus / risk_tolerance / diplomacy), current_goal, mood, last_summary.
- enrich `npcs` — tier (0–3), authority, role-in-faction, bound_entity_id, faction_id FK.
- `relationships` — (subject, object), trust, fear, resentment, debt, standing, last_summary, updated_at. Covers player↔faction AND faction↔faction. **Keystone.**
- `agreements` — id, parties, type (peace/trade/escort/tribute/…), terms, deadline, status (pending/kept/broken), created, resolved.
- `economy` — per faction: player_economic_importance, dependency_on_player, key_needs (wares), shortages (flagged + threshold), production_health, trade_pacts, trade_restrictions, market_status_for_player (partner/obstacle). + `player_market` — ware/sector → dominance level, supplying_enemies flag.
- `sectors` — id, name, owner_faction, contested_by, strategic_value, player_assets_present.
- `conflicts` — faction_a, faction_b, status, intensity, cause, started; plus loss aggregation (port the old `war_losses` + windowed `get_loss_summary` for war-fatigue).
- `world_events` — persistent, typed (death / sector_change / economic_threshold / diplomatic / battle); the event queue flushes the *important* resolved ones here.

**Build order:** (1) `relationships` + `factions` (keystone everything references); (2) `agreements` (promises = the emotional core); (3) `economy` + `player_market` (the flagged gap); (4) `sectors` + `conflicts`; (5) wire event-queue resolutions to persist into `world_events`. Each table gets a dashboard readout so it can be watched populating.

**Open question for build:** how the X4-side mod will *supply* this data (which reads come from SirNukes Mod Support APIs / MD script properties vs are inferred by the bridge) — pin down per domain before writing the ingest contracts.

### The decision layer — how stored data becomes AI influence (Bannerlord research)

Storing the universe state is only the substrate. The AI *acts* on it through a **3-stage influence engine** (from `Desktop/Bringing Bannerlord Style AI Influence into X4 Foundations.md` + blueprint §10/§11) — the design Bannerlord AI Influence itself uses, and far more robust than "LLM reads raw data and decides":

1. **Deterministic scoring of every factor** → per-faction pressure aggregates. Scoring core (from the doc):
   `score = 0.30·military_pressure + 0.20·economic_pressure + 0.15·recent_losses + 0.10·logistics_stress + 0.10·(−hidden_affinity) + 0.10·salient_memory + 0.05·player_alignment`, minus active cooldowns.
2. **LLM picks among bounded *legal* options + narrates the rationale** (intent generator + narrator, not authority).
3. **Deterministic validator → X4 applies only whitelisted actions.**

This closes the loop: events → update relationships/economy/strategic_state → **scheduled strategic review** (the event-queue worker, repurposed) runs score→LLM→validate → emits an incident/action → X4 applies → outcome writes back to memory → relationships shift → … So "a faction goes to war over a shortage" = high `economic_pressure` + resentment crossing threshold on a review cycle. The strategic review runs on a slow cadence (~10–60s hot, minutes broad) — never per tick.

**Two tables this adds (the missing half between "store" and "influence"):**
- `strategic_state` — per faction: `military_pressure, economic_pressure, logistics_stress, recent_losses, territorial_pressure, piracy_pressure, player_alignment`, updated_at. The decision **input**, derived from economy/military/territory/relations. (This is where economy becomes a *cause of action*.)
- `incidents` / `pending_actions` — the AI's **proposed** changes: `action_type, target, faction, confidence, priority, cooldown_until, narrative, effects_json, status`. The decision **output** — and this *is* the action/command whitelist we kept deferring; it's the missing half, not a separate feature.

**Reframed build order:** (1) `relationships` + `factions` [in progress]; (2) `strategic_state` (pressure aggregates) + the deterministic scoring core; (3) `incidents`/`pending_actions` + the action whitelist + validator; (4) repurpose the event-queue worker into the strategic-review loop (score→LLM→validate→incident); (5) `economy`/`player_market`, `sectors`, `conflicts`, `agreements` feed the pressure scores; (6) persistent `world_events`. The X4 mod then just POSTs events and polls `incidents` to apply.

---

## 2026-06-19 — Cache reset + per-save-file indexing ✅ DONE (live-verified)

**Problem:** all NPC memory lives in one `npc_memory.sqlite3`. Memory is already keyed by `save_id` (`npc_key = save_id|game_id|persona`), but there's no way to (a) **see/manage** what each save holds or (b) **reset** the cache — so different X4 save files share an undifferentiated blob, and dev/test runs (the 100 stress NPCs, etc.) pile up with no clean wipe. This is the save-isolation half of Risk #1.

**Design (single DB, `save_id` as the index — no per-file DB refactor):**
- **Index:** `GET /api/memory/saves` → one row per `save_id` with NPC/turn/fact counts + last-active. The dashboard lists saves and can filter the NPC table to one save.
- **Reset:** `GET /api/memory/reset?save_id=X` clears one save's NPCs/turns/facts; `GET /api/memory/reset?all=1` wipes everything (requires the explicit `all=1` so it can't fire by accident) and also clears the event queue. Per-save uses the existing `clear_save`.
- Dashboard: a Saves strip with counts + a per-save "reset" and a guarded "Reset all".

**Why not per-save DB files:** the blueprint suggests `ai_influence_<save_id>.sqlite`; true file isolation is cleaner but means routing every request to a different `MemoryStore`/`EventQueue` instance — a real refactor. Single-DB-with-index gives the same isolation (keys never cross saves) and trivial reset, with far less risk. Revisit per-file if cross-device sync is ever needed.

**Verification (live):** edits auto-deployed by **watch mode** (deploy.log shows `change detected → reloaded - BRIDGE UP` cycles — no manual deploy). `GET /api/memory/saves` returned the real index: `stress` (500/2024/437), `events` (1/24/14), `save_live` (5), `save_demo_01` (1), `save_modeltest` (1). `GET /api/memory/reset?save_id=stress` → `cleared_npcs:500`; remaining saves intact (508→8 NPCs). `?all=1` wired (clears memory + event queue; not fired). Dashboard: Saves strip with per-save ✕ reset + guarded "Reset all", and clicking a save filters the NPC table.

**Model note:** with the model selected at test time, warm NPC replies were **2.4–3.3s**, clean and context-aware (the 1.4s/2–3.5s figures earlier were Gemini Flash, ~5× MiMo's price). MiMo V2 Flash (0.10 J/k) is the intended production model.

---

## 2026-06-19 — Event queue + green-light batched LLM flush ✅ DONE (live-demoed)

**Problem:** sending every X4 event to the LLM as it happens is unaffordable (joules), thrashes the single-model gate (the concurrency pile-up found in the stress test), and bloats memory (one fact per event → unbounded CORE). Solution: buffer events, and let a *group* through the LLM at a time on an interval — a traffic light.

**Design — `bridge/events.py` `EventQueue`:**
- **Ingest:** events `{target, type, summary, importance, sector, faction, ts}` are buffered in memory + persisted to a `pending_events` table. Cheap; no LLM.
- **Green light (flush triggers, any of):** time **interval** (default ~15s); **batch size** (target piled up ≥K); **priority preempt** (importance-5 = ambulance, flushes immediately / jumps the queue).
- **Flush = one LLM call per cycle:** the worker pops up to `batch_size` pending events, coalesces dupes ("3 freighters lost" not 3 lines), builds ONE consolidated prompt, and sends it to a dedicated **"Strategic AI" NPC** via the working NPC API (clean replies, sidesteps the raw-chat reasoning bug). The single resolution is logged + condensed into memory facts. So N events → 1 LLM call.
- **Single drain lane = backpressure:** one flush at a time behind the chat gate. A flood of 1,000 events drains in controlled groups at the cadence instead of thrashing. Directly fixes the stress-test concurrency failure.
- **Memory tie-in:** coalesce + batch-condense ⇒ fewer, merged facts ⇒ also slows the unbounded-CORE growth found above.

**Endpoints (planned):** `POST /api/events/enqueue`, `GET /api/events/simulate?npcs=500&events=N` (flood), `GET /api/events/flush` (manual green light), `GET /api/events/state` (pending + config + flush history + last resolution), `GET /api/events/clear`. Dashboard gets an **Event Queue** panel: pending gauge, interval, last flush, and a flush-history table showing each cycle's batch size, latency, and the LLM's resolution text.

**Live demo (in our setup, not in-game):** simulate **500 NPCs** generating conversation/events into the queue; the worker resolves a batch through the **real Player2 LLM every interval**; watch on the dashboard the pending count climb during the flood then drain group-by-group as the green light cycles, with each cycle's real LLM resolution shown.

**Phasing:** Phase 1 = ingest → buffer → interval flush → real-LLM resolution → memory (this milestone). Phase 2 = priority preempt + coalescing tuning + per-profile knobs.

**Dev tooling — watch mode (`Watch-Neural-Link.bat`):** run once and leave open; a PowerShell file-watcher polls `bridge/` + `config/` and on any edit auto-compiles, redeploys F:→G:, and restarts the bridge **in place** (same window, bridge runs hidden). Compile errors keep the previous bridge alive. Replaces the manual `Deploy-And-Restart.bat` per-edit loop. (Dashboard files are served live from G: and need no restart, so they're not watched.)

**Verification (live demo):** deployed via the new watch-mode `Deploy-And-Restart.bat`; `/api/events/state` → `worker_running:true`, interval 12s, batch 25. `simulate?npcs=500` → **497 pending** (a death-priority flush already cleared 3). Worker drained it group-by-group: each flush = **exactly one real Player2 LLM call** (the Galaxy Strategic AI NPC), batch 25, ~4–6s, producing coherent situation reports ("Naval attrition remains critical with heavy losses concentrated in Sectors 1 and 3…", "Total fleet collapse is imminent…"). **Priority preempt confirmed** — importance-5 `death` events fired `reason:priority` flushes ahead of the interval. Net: **500 events resolved through ~20 LLM calls instead of 500** (25× reduction), at a controlled cadence, no thrash, all logged + visible on the dashboard.

**Tuning notes / honest warts:** (1) each flush currently writes the strategic resolution as a `diplomacy` (significant) fact to *every* distinct target in the batch — that inflated significant-fact counts; the resolution should instead go to a single global/faction memory, not per-officer. (2) death-priority events spawn a flush thread each; the no-blocking flush lock dedupes them to one runner, but a debounce would be cleaner. Both are Phase-2 polish, not blockers.

---

## 2026-06-19 — 100-NPC memory stress test ✅ DONE (passed)

**Goal:** prove the memory system holds at the scale a 100-hour save implies — many NPCs, long conversations — and put hard numbers on Risks #2 (unbounded growth) and #3 (condensation) from the robustness assessment.

**Method (two layers):**
- *Synthetic scale (fast, the bulk):* a `GET /api/memory/stresstest?npcs=100&turns=40` endpoint drives ~100 NPCs (varied race/role/ship/skills) through ~40-turn conversations each (~4000 turns), with CORE events (death/war/oath/love) embedded in routine chatter. Runs in-process against the *live* memory store (no Player2, no joules) so it populates the real dashboard. Reports timing, per-NPC raw-turn bounding, total facts, CORE-survival, DB size. `GET /api/memory/stress_clear` removes the `save_id=stress` rows afterward.
- *Live integration sample:* a handful of real `target.mode:"npc"` conversations through Player2 to confirm the full pipeline + concurrency gate still behave while the store is populated.

**Why synthetic for the 100×40 bulk:** Player2 chat is ~2–3s serialized by the bridge's single-model gate, so 4000 real calls ≈ hours + joules. The bridge memory engine (binding, turns, condensation, decay, retrieval, SQLite) is the system under test here; it's exercised at full scale directly. Player2 throughput is already characterized separately.

**Results (live):**

*Synthetic scale — the memory engine under 100-NPC load:*
- 100 NPCs created (varied race/role/ship/skills), **~8,000 turns fed → 806 retained** (max 8 raw turns/NPC = the keep-window; condensation pruned the rest). Risk #2 (unbounded growth) **closed at scale**.
- **300 CORE facts survived** (100 NPCs × 3 embedded death/war/oath/love events); `facts_by_tier = {core:300, significant:0, routine:0}` — routine chatter forgotten. Risk #3 (condensation) **proven**.
- Throughput **~100 turns/s**; whole DB **0.33 MB** for 100 NPCs with deep histories — i.e. a 100-hour save's chatter condenses to a negligible, bounded footprint. (Per-op SQLite connections are the throughput ceiling; fine for real-paced single conversations, optimizable later if bulk ingest is ever needed.)
- Invariant checks `raw_turns_bounded`, `core_survived`, `routine_not_persisted` all pass. (Stacking a second run trips only the cosmetic `npcs_created` count, not a real failure.)

*Live integration — 5 concurrent real Player2 conversations:*
- All 5 returned `ok`, in-character, distinct per race (Teladi "Profitssss", Split "crush weak enemies", Paranid "Holy Three", Boron scientific, Argon military). Memory attached to each.
- Latencies 5.5 / 12 / 15 / 23 / 32 s show the **single-model concurrency gate serialized them cleanly** — 5 simultaneous requests queued and each completed, instead of all thrashing into empty/timeout (the pre-gate failure mode). 32 s total for 5 ≈ ~6 s each effective. No timeouts, no empty content.

**Endpoints:** `GET /api/memory/stresstest?npcs=&turns=`, `GET /api/memory/stress_clear` (removes `save_id=stress`).

**Honest caveat:** synthetic turns use the heuristic summarizer, not the LLM — it validates the *mechanics* (bounding, survival, decay, DB growth) at scale, not LLM summary *quality*. The 100 stress NPCs remain in the live DB (visible on the dashboard) until `stress_clear` is hit.

---

## 2026-06-19 — X4 NPC stats attached to NPC data ✅ DONE (live-verified)

**Research (verified):** X4 tracks **5 crew skills**, stored as `boarding, management, engineering, piloting, morale`, each 0..15 internally = 0..5 stars (3 levels/star). Roles: **Pilot, Manager, Service Crew, Marine**; morale is a universal modifier (effective skill ≈ avg of chosen skill + morale). Piloting → ship handling/travel/boost; Management → station/trade; Engineering → repair; Boarding → marines. Plus identity: name, **race** (argon/teladi/paranid/split/boron/terran/…), gender, faction, and ship assignment (ship_class S/M/L/XL, ship_name, sector). Sources: Egosoft wiki "Crew", X4 Personnel wiki, and the old mod's own `context_selector`/`faction_personalities` (which already read `skill_piloting/management/engineering/boarding/morale`, race overlays, ship_class).

**Built into the bridge memory model:**
- `MemoryStore.npcs` migrated (idempotent `ALTER TABLE ADD COLUMN`) with `race, role, ship_class, gender, ship_name, sector, skills(JSON), stats(JSON)`.
- `bind_npc(..., stats=...)` merges a stats blob (fields not sent this turn keep their prior value). `skill_stars(0..15) → ★/☆`. `_identity_line()` builds a "You are a pilot, argon, L-class … Skills: piloting ★★★★☆, morale ★★★☆☆" line.
- `build_memory_context` now **prepends the identity/skills line** so the NPC speaks in-character to its role/skill (an expert pilot talks like one).
- `npc_complete` reads `target.stats` (or individual `target.{race,role,gender,ship_class,ship_name,sector,skills}`) and attaches them on bind.
- Dashboard NPC detail renders stat chips + a skills box with stars; `list_npcs`/`npc_detail` expose the fields.
- Self-test extended to 13 checks (adds `stats_attached`, `skills_stored`, `skill_stars`, `context_has_identity`).

**Verification (live):** deployed via `Deploy-And-Restart.bat` (compile-gate OK, killed PID 26080, restarted). `GET /api/memory/selftest` → **ok, 13/13** (Windows temp-cleanup fix confirmed; 4 stat checks pass). Sent a `target.stats` request for Captain Reyes (role pilot, argon, ship_l, ANV Vigilant, skills piloting 13/management 4/engineering 6/boarding 2/morale 11) → stored correctly, the pre-existing 4 turns survived the in-place migration (now 6 turns), and the NPC replied in-character referencing its ship ("The Vigilant will hold"). Dashboard NPC detail renders 6 stat chips + 5 star-rated skill rows (screenshot confirmed).

---

## 2026-06-19 — Player2 vendor finding (reasoning bug) + memory architecture scoped

### Player2 reasoning bug — confirmed vendor-side, under investigation ◐ MITIGATED

Reported the empty-content/latency issue to the Player2 Discord (#dev). Outcome:

- A Player2 dev confirmed it's **on their side and actively being investigated** ("might be related to another thing we're looking at, I'll forward this"). Their guidance: *"for now use another model or wait for the fix."*
- They claim **Gemini models work fine** and that in their tests there is "a clear time difference between thinking on vs off." **Our observation contradicts this:** the user switched Player2 to Gemini and saw the *same* empty/slow behavior, with the app's Thinking toggle confirmed OFF.
- Confirmed our own finding: the app's Thinking toggle and the API's `thinking:{type:"disabled"}` both fail to suppress reasoning on the developer `/v1/chat/completions` path.

**Mitigation in place (not a fix):** the bridge uses the **NPC API**, which returns clean text regardless of the reasoning bug (see NPC block above). So Neural Link is unblocked even while the vendor bug is open.

**OPEN — Gemini + thinking-toggle A/B test `[host-gated]`:** the Player2 dev specifically asked us to (a) confirm Thinking is OFF, (b) confirm the perf issue on Gemini, and (c) test toggling Thinking on vs off and report the time delta. This requires switching the chat model + flipping the toggle in the Player2 desktop app (UI action), then re-running the bridge probe. Blocked only by reliable desktop input. **Verify:** for each {model × thinking} cell, `POST /api/player2/npc_chat` and a raw `/v1/chat/completions` probe → record latency + whether `message.content` is non-empty. Expected per vendor: Gemini + thinking-off → fast, non-empty.

### Character memory architecture — scoped, NOT yet built OPEN `[blocked by: inspect old chronicle_service]`

Grounded in `Desktop/X4_AI_Influence_Blueprint2.md` §13 + the user's decaying-memory model. Design is a **four-stage consolidation pipeline** (detail dies, meaning survives — human-like):

- **A. Raw episodic** — `conversation_log`, full-fidelity, short-lived; pruned after digestion.
- **B. Condensed facts** — after a conversation, LLM crushes the raw turns into a few `memory_facts` (each `importance` 1–5, tagged). The "1000 lines → key details, cut useless context" step.
- **C. Semantic gist** — `relationships` rollup (`trust/fear/resentment/debt` + `last_summary`); individual events fade into scores + one paragraph.
- **D. Decay/forgetting** — periodic pass using `importance` + `last_used_at`: old low-importance facts condensed/dropped; importance-5 (betrayal, rescue, leader death) effectively permanent; mid-tier blurs into the Stage-C summary. This is the "degrading accuracy like real life" layer the blueprint implies but doesn't spec.

**Retrieval (§13.2):** each turn injects only profile + game-state summary + last 3–5 turns + top-K facts (importance × recency × relevance) + relationship summary + recent world events. Never raw logs ⇒ **prompt size stays flat at hour 2 or hour 200.**

### 100-hour-save robustness assessment (honest)

Foundation is sound (X4 authoritative, safe fallback, NPC path proven). As of today the bridge is an MVP and is **NOT** 100-hour-ready. Three concrete risks, ranked:

1. **Save/reload memory desync (highest).** Bridge memory lives outside the X4 save; reloads/save-scum desync it. Blueprint partially handles this via per-save DB (`ai_influence_<save_id>.sqlite`, §8) + `save_id` in the request (§9.1) — but reload-to-earlier-point *within* a save_id still desyncs. **Open.**
2. **Unbounded growth (~90%, straight leak).** `router.responses` dict, per-request `responses/*.json`, and the telemetry SQLite all grow forever — confirmed in code. Needs eviction/TTL/rotation. **Open.**
3. **No durable summarized memory yet.** The Stage A–D pipeline above doesn't exist in `x4_neural_link`; raw-history injection would overflow context. **Open.**

Risks #2 and #3 are solved *by* the memory architecture above. #1 needs explicit save-coupling.

**Old `x4_ai_influence/bridge` inspected ✅ (2026-06-19).** Read `chronicle_service.py`, `persistence.py`, `context_selector.py`, `state/memory_store.py`, `state/conversation_memory.py` from the live `G:\` folder. **Verdict: a strong Stage-A + retrieval foundation exists, but the durable political memory and the consolidation/decay layers do NOT.** Specifically:

- *Misnamed:* `chronicle_service.py` is **not** memory — it's a Star Wars opening-crawl flavor-text generator that patches a `t/`-file. Ignore for memory.
- *Reuse (good, port these):* `state/memory_store.py` `SQLiteMemoryStore` — thread-safe, indexed SQLite with `npc_profiles`, `npc_turns` (**rolling window, auto-trimmed to `max_turns=12`**), `world_events` (has a `severity` field ≈ importance), and `war_losses` + `get_loss_summary` (windowed war-fatigue aggregation). `context_selector.py` `build_context_block` is a real, token-policy-aware retrieval/assembly layer (identity + personality + priority-ordered/deduped world events + conflict sentiment + minimal game-state). `persistence.py` adds the X4-entity→`player2_npc_id` binding. These are worth lifting wholesale.
- *Missing (build new):* **no `relationships` table** (trust/fear/resentment/debt/promises) — Stage C absent; **no `memory_facts` + importance + post-conversation summarization** — Stage B absent (turns are *trimmed*, not condensed, so anything past the last 12 turns is simply lost, not remembered); **no decay/forgetting pass** — Stage D absent; **no `save_id` scoping** — DBs are global, so the save/reload desync risk (Risk #1) is fully live.

**Decision:** hybrid, not rebuild and not pure port. Lift `SQLiteMemoryStore` + `context_selector` into `x4_neural_link` as the Stage-A/retrieval base, then add the missing Stage-B summarization (turns → `memory_facts`), Stage-C `relationships` rollup, Stage-D decay pass, and `save_id` scoping. This is what task #9 builds.

### Memory engine built + self-tested ◐ CORE DONE (live in-game test pending)

`bridge/memory.py` — `MemoryStore`, stdlib/SQLite, generic (bridge owns mechanics; the mod owns gameplay meaning). Implements all four stages: `turns` (raw rolling window) → `condense_if_needed` crushes overflow into categorized `facts` and drops the raw turns → rolling `summary` gist per NPC → `decay` pass. Durable, save-scoped NPC binding (`make_key(save_id, game_id, persona)` → `bind_npc`/`get_npc_id`). Injectable summarizer (default = deterministic keyword heuristic, joule-free; LLM summarizer is a later swap).

**Category taxonomy — what survives degradation (the explicit design ask):**
- **CORE** (`death, war, betrayal, love, oath, birth, catastrophe`) → importance 5, `verbatim=1`: kept **unaltered forever**; only the surrounding chatter is dropped.
- **SIGNIFICANT** (`deal, battle, threat, alliance, gift, insult, rescue, economy, diplomacy`) → importance 3: kept as a condensed fact, detail blurs, capped per NPC (LRU drop of the lowest).
- **ROUTINE** (`smalltalk, status, greeting, flavor, query`) → importance 1: never persisted as durable facts; forgotten.

**Wired into the bridge:** `npc_complete` now resolves the durable `npc_id`, injects `build_memory_context` into the turn, records the exchange, and condenses. Respawn-on-404 also clears the durable binding. New endpoints: `GET /api/memory/selftest`, `GET /api/memory/metrics`.

**Verification (sandbox, deterministic):** `python -m py_compile` staged + live → both OK. `run_memory_selftest()` → **ok=True, 9/9**: NPC-id bind+retrieve+rebind; 80 turns fed → bounded to 16 raw turns + **3** durable facts (massive condensation); all 3 CORE events (death/oath/love) survived **verbatim**; 0 routine facts persisted; retrieval context contained the CORE memories and stayed at 512 chars. Files deployed to live `G:\`; **bridge restart pending** to exercise it against real Player2 NPCs.

**Honest limitation:** default summarizer is a keyword heuristic, not the LLM — good enough for structure + tests, but real-quality condensation (paraphrase, multi-turn synthesis) needs the LLM summarizer swap. No `save_id`-timestamp reload-desync handling yet (separate from this).

### Memory live-verified + dashboard visualization ✅ DONE (2026-06-19)

Deployed to the live bridge (host-side `Deploy-And-Restart.bat` — compile-gate OK, copy, restart; metrics reset to 0). Verified against **real Player2 NPCs through the bridge**:

- **NPC creation + ID retrieval:** a `target.mode:"npc"` request spawned "Captain Reyes" (argon), bound `npc_id 99a74e66-…`, durably stored under save-scoped key `save_demo_01|x4_demo|Captain Reyes`.
- **Memory continuity (the real test):** Turn 1 — *"Admiral Vance was killed when the ANV Resolute was destroyed at Argon Prime."* Turn 2 (separate request, only the new question sent) → *"Admiral Vance. He was lost aboard the ANV Resolute."* The NPC recalled the death because the durable binding reused the same Player2 npc_id. 4 turns recorded, attached to the NPC.
- **Dashboard:** new **NPC Memory** panel ships in `dashboard/` — top-band NPC/Memory counts, tier chips (core/significant/routine), an NPCs table (name, faction, save/game, turns, facts, core, bound npc_id), and a click-through Memories pane (gist + tier-colored facts with verbatim/category/importance badges + color-coded conversation). Endpoints `GET /api/memory/{npcs,npc,metrics,selftest}`. Confirmed rendering live (screenshot): Reyes row + all 4 turns + "condensation triggers once the raw window overflows" (facts=0 at 4 turns, by design).

**Two honest caveats:**
- `GET /api/memory/selftest` **crashed on Windows** — the self-test's temp-SQLite cleanup hit Windows file-locking and reset the connection (passed fine in the Linux sandbox: 9/9). **Fixed** (`mkdtemp` + `rmtree(ignore_errors=True)`) — deploys with the NPC-stats batch below.
- The sandbox file-mount truncated freshly-edited files mid-session and briefly corrupted the live backend copy; the host-side `Deploy-And-Restart.bat` (PowerShell robocopy, reads complete host files) repaired it. **Lesson: deploy via the host PowerShell script, not the sandbox mount `cp`.** Staged `F:\` dashboard is out of sync (corrupted by the same issue); live `G:\` dashboard is correct — re-sync staging later.

---

## North Star

> Any X4 extension can depend on Neural Link, send a bounded request to Player2 through a local bridge, receive a validated response, and continue safely if the bridge or Player2 is offline.

For AI Influence specifically:

> AI Influence is rebuilt as a separate Forge-authored gameplay mod that depends on Neural Link. The LLM thinks and speaks; the mod validates and acts; X4 remains the authority.

---

## The Influence Engine — core architecture

> The realization that makes the whole mod tractable. Supersedes the "let the LLM decide" model. The LLM is a **bounded chooser + narrator**, never the authority.

### Thesis — why this makes the mod easy
The hard version is *"an LLM controls a galaxy and never breaks the game"* — nearly impossible. We never needed it. Bannerlord-style AI computes decisions with **deterministic math**, then uses the LLM only to **choose among already-legal options and explain itself**. Reframing the LLM from authority → bounded chooser collapses every hard part:

| The "impossible" part | What it actually becomes |
|---|---|
| AI that controls the galaxy correctly | a **weighted-sum score** over numbers we already track |
| AI that never makes illegal/game-breaking moves | a **finite whitelist of N action types** + a deterministic validator |
| AI that's reliable | deterministic core; the LLM is optional flavor with a **rule-based fallback** |
| "model the whole universe" | store **meaning**, not the simulation — X4 already runs the universe |
| a huge new system | mostly **wiring pieces already built** (bridge, memory, event queue, NPC API, dashboard) |

So the mod = **facts in → score the situation → LLM picks a legal move + narrates → validate → X4 applies the effect → record the outcome.** Intelligence is swappable; mechanics are deterministic.

### The closed loop
```
X4 events/state → ingest → universe state (factions/relationships/economy/sectors/conflicts)
  → deterministic scoring → strategic_state pressures
  → LLM picks one high-scoring LEGAL option + narrates
  → validator (legal? bounded? cooldown? idempotent?)
  → incidents/pending_actions → X4 applies whitelisted effect
  → outcome → memory facts shift + relationships update → re-score …
```
The feedback (outcome → next event → re-score) is the "alive" feeling. It runs on a **slow scheduled cadence** (strategic review every ~10–60s hot, minutes broad) — never per tick. That scheduler already exists: the event-queue green-light worker.

### Three stages
1. **Deterministic scoring (no LLM).** Per-faction pressure aggregates + a score per (faction, target, action):
   `score = 0.30·military_pressure + 0.20·economic_pressure + 0.15·recent_losses + 0.10·logistics_stress + 0.10·(−hidden_affinity) + 0.10·salient_memory + 0.05·player_alignment − 0.40·cooldown_active`.
   Output: a small ranked list of **legal, high-scoring options**. Weights in config, tunable per profile.
2. **LLM picks one + narrates (bounded).** Input: persona + compressed situation + the ranked legal options + top memories. Output: `{choice, target, confidence, narrative}`. It **cannot invent an action** — only pick from the list or decline (no-op). It adds judgment between close options + the in-world explanation. That's the only part we trust it with.
3. **Validate → X4 applies (deterministic).** Re-check legality/bounds/confirmation/cooldown/idempotency; emit an incident with `effects`. X4 polls, applies only whitelisted effects, acks the outcome. **X4 is always the authority.** Validation failure → drop (optionally dialogue-only).

### Data model (three layers by lifetime)
- **Live (never stored — read from X4 each turn):** current prices, ware stocks, real ship counts, live ownership, player credits. X4 owns these.
- **Durable substrate (`save_id`-scoped — the meaning X4 doesn't model):** `factions` · `npcs`(+tier/authority) · `relationships`(trust/fear/resentment/debt) · `agreements` · `economy`+`player_market` · `sectors` · `conflicts`(+loss aggregation) · `world_events` · `facts`/`turns`.
- **Decision layer (the engine's working memory — the new central piece):**
  - **`strategic_state`** — per faction: `military_pressure, economic_pressure, logistics_stress, recent_losses, territorial_pressure, piracy_pressure, player_alignment`. *Derived* from the substrate each review. **Where economy/military/territory become a cause of action.**
  - **`incidents`/`pending_actions`** — proposed changes (`action_type, target, faction, confidence, priority, cooldown_until, narrative, effects_json, status`). **The action whitelist made concrete** — what X4 consumes.

### The action whitelist (finite, versioned, phased)
- **MVP:** `dialogue_only, memory_update, logbook_entry, relation_change_limited, credit_transfer_limited, accept_offer, reject_offer`.
- **Phase 2:** `trade_offer, promise_record, temporary_diplomatic_flag, mission_offer, faction_bulletin`.
- **Phase 3+:** `intel_share, contract_offer, sector_warning, faction_alert, resource_request, ceasefire_pressure`.
- **Experimental (off by default):** `faction_relation_shift, fleet_priority_suggestion, trade_restriction, multi_faction_diplomatic_result`.
Each carries numeric bounds, cooldown, authority (which NPC tier may propose), confirmation flag. The validator enforces all. **Adding intelligence = adding an action type + bounds + X4 executor** — a finite, schedulable task list, not an open AI problem.

### Strategic-review scheduler (already built)
The `EventQueue` green-light worker **is** the review scheduler. Repurposed, each cycle: pull deltas → update relationships/economy/`strategic_state` (deterministic) → Stage-1 score → if a candidate clears threshold, Stage-2 LLM choice + Stage-3 validate → write an `incident`. Priority preempt (importance-5: capital-ship loss, sector falling) jumps the queue; single drain lane = backpressure. **Scheduler, batching, backpressure, dashboard are done — we change what one function does inside it.**

### Deterministic fallback (the safety net that also makes it easy)
Because Stage 1 is pure math, the mod **works with the LLM off**: high military pressure + low logistics → auto `defensive_stance`; critical shortage → auto `resource_request`. The LLM, when present, only improves which close option is chosen + adds narrative. So: dev/test need no LLM (free, fast, deterministic); a Player2 outage degrades gracefully; balance is unit-testable code, not prompt-wrangling. **This single property — game-affecting logic is deterministic, the LLM is optional flavor — is the biggest reason the mod is now realistic.**

### What's already built (the gap is small)
| Engine piece | Status |
|---|---|
| Bridge transport (HTTP, contracts, telemetry, dashboard) | ✅ |
| Player2 LLM access via NPC API (clean replies) | ✅ |
| Memory: condensation, decay, CORE-verbatim, save-scoped, reset/index | ✅ |
| NPC identity + X4 stats | ✅ |
| Strategic-review scheduler (event queue + green light + backpressure + priority) | ✅ (repurpose) |
| `factions`, `relationships` tables | ✅ storage + endpoints + dashboard |
| `strategic_state` + scoring core | ✅ table + deterministic Stage-1 scoring (selftest 7/7) |
| `incidents`/`pending_actions` + validator (whitelist) | ❌ next |
| `economy`/`sectors`/`conflicts`/`agreements` (feed pressures) | ❌ scoped |
| X4-side mod (POST events, poll incidents) | ❌ separate extension |

Remaining bridge work: two decision tables, one scoring function, one validator, rewire the worker we already have. Substrate tables are mechanical. Weeks of focused work, not an open research problem.

### Build phases
1. Expose `relationships` + `factions` (endpoints + dashboard). Methods exist.
2. `strategic_state` + scoring core — deterministic, fixture-unit-testable, no LLM.
3. `incidents`/`pending_actions` + validator (MVP whitelist). Dashboard shows proposed actions.
4. Repurpose the review worker: score → bounded-LLM choice → validate → incident. Demo headless: feed events, watch score rise, watch the AI choose + narrate + emit a validated incident.
5. Feed pressures from `economy`/`player_market`, `sectors`, `conflicts`, `agreements`.
6. Persistent `world_events`; outcome write-back closes the loop.
7. X4-side extension (separate): `djfhe_http` collector POSTs events + polls `incidents`; narrative-first MVP (bulletins/logbook/missions) before relation/credit writes.

Each phase is demoable in our headless setup before the game is involved.

---

## Current Evidence

**Observed now**

- Existing live mod backed up to `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\_backup_x4_ai_influence_20260618-224546`.
- Forge staged workspace root is `F:\DEV_ENV\projects\Mods\X4Mods`.
- Live deploy root is `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions`.
- Staged Neural Link directory exists at `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link`.
- Live Neural Link directory exists at `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_neural_link`.
- Player2 responds at `http://127.0.0.1:4315/v1/health` with `client_version: 0.10.65`.
- Player2 `/v1/models` currently returned only `whisper-1` in this probe, so "Player2 up" is not the same as "LLM/NPC chat ready."
- Built bridge endpoints: `GET /health`, `POST /v1/request`, `GET /v1/response/{request_id}`, `GET /v1/updates_pool`.
- Built observability endpoints/UI: `GET /dashboard`, `GET /api/telemetry`, `POST /api/player2/probes`.
- Telemetry is persisted in SQLite at `runtime/bridge_telemetry.sqlite3`.
- Live bridge smoke: request accepted, response completed, updates drained, unsafe `../bad` request id rejected with HTTP 400.
- Current Player2 chat behavior: `/v1/chat/completions` can return a completion object with no text content. Neural Link now marks that as `degraded` with `actions: []` and the safe reply "No game action was taken."
- Player2 probe suite currently covers `/v1/health`, `/v1/models`, `/v1/selected_characters`, and `/v1/chat/completions`. Health, models, and selected characters pass; chat-completions currently fails the usable-text check.

**Known-working bridge evidence from old `x4_ai_influence`**

- Snapshot: `x4_ai_influence/_known_working/2026-04-23_live_bridge_smoke`.
- Proved: `POST /v1/request`, Player2 call at `127.0.0.1:4315`, `GET /v1/updates_pool`, observed `llm_instance_id: player2_v2`.
- Not proved by that snapshot: in-game UI chain, MD triggers, action dispatch/game-state mutation, NPC SSE path, war/chronicle/loss/action feedback routes.

**Design docs read**

- `AI Agents, Video Games, Visual Perception, and Input Injection.md`: favors supported APIs, explicit bridges, observable logs, and low-risk integration boundaries over process hooks or hidden automation.
- `Bringing Bannerlord Style AI Influence into X4 Foundations.md`: estimates strategic AI influence is realistic in X4 through middleware, but deep Bannerlord-style per-NPC social simulation is much less likely without reframing.
- `X4_AI_Influence_Blueprint2.md`: defines the real product rule: LLM proposes; bridge/mod validates; X4 applies only whitelisted deterministic actions.

---

## Architecture Boundary

### Neural Link owns

- X4-to-localhost transport contract.
- Python bridge server on `127.0.0.1:8713`.
- Player2 adapter for `127.0.0.1:4315`.
- Health/status endpoints.
- Request IDs, idempotency, timeouts, retries, and offline fallback.
- Generic request/response envelopes.
- Optional generic function-call/action-envelope validation, but no game-specific policy.
- Launcher/startup scripts and docs required to operate the bridge.

### Neural Link must not own

- AI Influence faction personalities.
- Diplomacy scoring.
- War/peace policy.
- Faction event generation.
- Old processed request/response logs.
- `.mypy_cache`, `__pycache__`, local DB state from another mod, or stale test artifacts.
- Files from other mods except an explicit dependency reference.

### AI Influence owns later

- Faction leaders and personas.
- Memory model for promises, grudges, battles, economic pressure, and strategic incidents.
- Prompt policy and action whitelist for AI Influence.
- X4-side UI/conversation UX.
- Safe game-state writer for relation, logbook, credits, missions, and later strategic actions.

---

## Dependency Policy

Target dependency shape:

- `x4_ai_influence` depends on `x4_neural_link`.
- `x4_neural_link` depends on `djfhe_http` unless Neural Link later vendors an equivalent HTTP transport cleanly.
- Avoid SirNukes and kuertee as hard dependencies unless a concrete X4 engine surface cannot be replaced.
- Player2 remains an external local runtime, not files bundled from another mod.

Blunt risk: "only bridge and player2" is achievable for Python/provider logic, but X4 UI integration may still expose places where `djfhe_http` or a UI helper dependency is cheaper and safer than baking a clone. Treat dependency removal as a verified phase, not an assumption.

---

## Phase Plan

### Phase 0: Preserve and classify old source

**Goal:** know what is bridge, what is app, and what is junk.

**Files**

- Read: `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_ai_influence\_known_working\2026-04-23_live_bridge_smoke\*`
- Read: `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_ai_influence\bridge\*.py`
- Read: `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_ai_influence\ui\addons\x4_ai_influence\*.lua`
- Read: `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_ai_influence\md\*.xml`

**Verification**

- Produce a bridge/app/junk file classification table.
- Confirm no old app-specific faction files are listed for Neural Link import.

### Phase 1: Extract minimal known-working bridge ✅ MVP DONE

**Goal:** recreate only the known-working bridge behavior in `x4_neural_link`.

**Files**

- Create/modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\router.py`
- Create/modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\http_server.py`
- Create/modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\llms\player2_client.py`
- Create/modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\config\player2_config.json`

**Verification**

- Start bridge from staged and live `x4_neural_link`.
- `Invoke-RestMethod http://127.0.0.1:8713/health` returns bridge status.
- Synthetic `POST /v1/request` accepted.
- `GET /v1/response/{id}` returns the processed response.
- `GET /v1/updates_pool` returns the processed response.
- Duplicate request IDs are idempotent while pending or complete.
- Unsafe request IDs like `../bad` are rejected.
- No faction diplomacy modules are imported.

### Phase 2: Define stable Neural Link contract ◐ MVP CONTRACT BUILT

**Goal:** make the bridge usable by any dependent mod, not only AI Influence.

**Contract shape**

```json
{
  "request_id": "uuid-or-stable-id",
  "source_mod": "x4_ai_influence",
  "channel": "chat|event|health|tool",
  "target": {
    "provider": "player2",
    "npc_id": "optional"
  },
  "messages": [
    { "role": "system", "content": "bounded instruction" },
    { "role": "user", "content": "player or mod message" }
  ],
  "metadata": {
    "game": "x4",
    "save_id": "optional",
    "faction_id": "optional"
  }
}
```

**Verification**

- Python validation rejects unsafe `request_id`, unsafe `source_mod`, oversized payloads, unsupported channels, and invalid roles.
- Duplicate `request_id` returns cached or duplicate-safe behavior.
- Timeout or no-content Player2 output produces a safe no-action response.
- Remaining: formalize the contract as a versioned schema file before third-party mod authors target it.

### Phase 2.5: Bridge telemetry dashboard ✅ FIRST PASS DONE

**Goal:** make bridge traffic, errors, state, and Player2 probe results visible in a browser.

**Files**

- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\telemetry.py`
- Modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\router.py`
- Modify: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\server.py`
- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\index.html`
- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\styles.css`
- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\app.js`

**Verification**

- `GET /dashboard` serves the webapp.
- `GET /api/telemetry` returns SQLite-backed request/event/probe state.
- `POST /api/player2/probes` records health/models/selected-characters/chat-completions results.
- Browser verified: dashboard shows bridge online, Player2 `0.10.65 / whisper-1`, recent degraded transfer, failed chat-completions probe, selected-characters probe visible, and event stream.

**Remaining**

- Add filters and search across request/probe/event history.
- Add database table views beyond request/probe/event summaries.

### Phase 2.6: Dashboard detail drill-down and Player2 API catalogue ✅ DONE

**Goal:** make the bridge monitor useful for debugging actual transfer failures and Player2 API coverage.

**Files**

- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\telemetry.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\player2_client.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\router.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\server.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\index.html`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\styles.css`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\app.js`

**New APIs**

- `GET /api/telemetry/request/{request_id}`
- `GET /api/telemetry/event/{event_id}`
- `GET /api/telemetry/probe/{probe_id}`
- `GET /api/player2/catalog`

**Verification**

- Python compile passed for all bridge modules.
- JavaScript syntax check passed for `dashboard/app.js`.
- Live bridge health returned Player2 `0.10.65 / whisper-1`.
- `GET /api/telemetry?limit=2` returned sanitized list rows plus DB state row counts.
- `GET /api/player2/catalog` returned two OpenAPI documents and 56 endpoints, 32 marked mutating.
- Browser verified: dashboard displayed DB Rows, clickable request detail loaded full request/response data, API catalogue rendered 56 rows, and the page had no horizontal overflow.

**Remaining**

- Convert the catalogue into an explicit bridge capability matrix: safe read-only checks, safe write checks requiring fixture keys, costly media calls, NPC lifecycle calls, and unsupported/destructive endpoints.
- Build targeted non-destructive integration tests for Player2 game-data read/write using a dedicated test game/key once the correct `game_id` contract is confirmed.
- Decide whether Neural Link should expose NPC-specific bridge contracts or keep only generic chat/action envelopes.

### Phase 2.7: Player2 capability matrix and expanded safe probes ✅ DONE

**Goal:** classify every discovered Player2 endpoint by practical bridge-testability and expand diagnostics without triggering destructive, costly, or fixture-bound operations.

**Files**

- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\player2_client.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\router.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\bridge\server.py`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\index.html`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\styles.css`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\dashboard\app.js`
- Updated: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\tests\smoke_bridge.py`

**New API**

- `GET /api/player2/capabilities`

**Current capability classification**

- `safe_probe`: 15
- `fixture_required`: 20
- `side_effect`: 9
- `costly_or_async`: 6
- `destructive`: 4
- `external_auth`: 1
- `upload_external`: 1

**Expanded safe probe suite**

- `GET /v1/health`
- `GET /v1/models`
- `GET /v1/selected_characters`
- `GET /v1/openapi.json`
- `GET /v1/npc/openapi.json`
- `GET /v1/ai_profiles`
- `GET /v1/joules`
- `GET /v1/stt/languages`
- `GET /v1/stt/language`
- `GET /v1/stt/whisper/models`
- `GET /v1/tts/eleven/models`
- `GET /v1/tts/eleven/user`
- `GET /v1/tts/eleven/user/subscription`
- `GET /v1/tts/eleven/voices`
- `GET /v1/tts/eleven/voices/settings/default`
- `GET /v1/tts/voices`
- `GET /v1/tts/volume`
- `POST /v1/chat/completions` with a short non-streaming diagnostic prompt

**Verification**

- Python compile passed for all bridge modules.
- JavaScript syntax check passed for `dashboard/app.js`.
- `GET /api/player2/capabilities` returned all 56 endpoint-method pairs and the corrected classification counts above.
- Expanded probe run passed for all no-fixture read-only diagnostics listed above.
- Chat completion still failed as intended by diagnostics: HTTP 200 with no usable assistant text, or timeout depending on run. Neural Link records this as degraded and returns no actions.
- Browser verified: dashboard rendered 56 capability rows, 56 catalogue rows, corrected capability chips, visible Eleven probe history, visible chat timeout, and no horizontal overflow.
- `tests/smoke_bridge.py` passed against the live bridge, including health, request duplicate handling, degraded response handling, updates pool, telemetry DB state, capability counts, and invalid request rejection.

**Remaining**

- Add fixture-backed tests for game-data user/global stores once a valid non-production `game_id` is configured.
- Add opt-in tests for side-effect endpoints (`tts/volume`, `tts/stop`, `stt/start`, `stt/stop`) with explicit safety controls.
- Add NPC lifecycle contract wrappers only after deciding how Neural Link should represent NPC ids and response streams.

### Phase 3: X4-side Neural Link client ⏭ NEXT

**Goal:** provide a tiny X4 bridge client that dependent mods can call.

**Files**

- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\ui.xml`
- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\ui\addons\x4_neural_link\init.lua`
- Create: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link\md\neural_link_main.xml`

**Verification**

- X4 loads `x4_neural_link` independently.
- `djfhe_http` loads or the mod shows a clear missing dependency state.
- Test ping from X4 reaches bridge and logs response.

### Phase 4: Reduce startup friction

**Goal:** make bridge startup less ugly than manually hunting for bat files.

**Practical target**

- Keep `.bat` scripts as fallback.
- Add one top-level `Start-Neural-Link.bat` and `Start-Neural-Link.ps1`.
- Add bridge status probe that tells the user exactly which component is offline: Neural Link bridge, Player2 app, or usable LLM/NPC endpoint.

**Verification**

- Double-click launcher starts the bridge from the correct folder.
- Re-running launcher does not start duplicate competing bridge processes.
- Health command reports Player2 `client_version` and whether a usable chat/NPC backend is available.

### Phase 5: Deploy Neural Link cleanly

**Goal:** package a clean bridge extension into live X4 `extensions`.

**Files**

- Source: `F:\DEV_ENV\projects\Mods\X4Mods\x4_neural_link`
- Target: `G:\SteamLibrary\steamapps\common\X4 Foundations\extensions\x4_neural_link`

**Verification**

- Initial live copy excluded `.mypy_cache`, `__pycache__`, `tests`, `runtime`, old DBs, processed requests, backups, and AI Influence policy files.
- Starting the bridge creates expected runtime folders under the live extension.
- `content.xml` exists in the live extension.
- Remaining: verify `x4_neural_link` appears and loads cleanly inside X4's extension list/logs.

### Phase 6: Rebuild AI Influence as a dependent mod

**Goal:** start AI Influence over in Forge, depending on Neural Link.

**Inputs**

- Desktop blueprint docs listed above.
- Existing old AI Influence files only as reference, not as architecture authority.

**First MVP**

- One Argon representative.
- One chat loop.
- One memory record.
- One safe action category: dialogue/logbook first, then limited relation/credit only after validation.

**Verification**

- `x4_ai_influence` hard-depends on `x4_neural_link`.
- AI Influence contains no copied Neural Link runtime files.
- Neural Link contains no AI Influence faction logic.
- In-game acceptance test: player sends a message, Player2 responds through Neural Link, X4 displays it, and no action executes unless whitelisted.

---

## Risk Register

| Risk | Likelihood | Impact | Handling |
|---|---:|---:|---|
| Player2 health works but LLM/NPC chat is unavailable | Medium | High | Separate health from usable-backend checks. Current `/v1/models` probe only showed `whisper-1`. |
| Old bridge has app logic mixed into transport | High | Medium | Extract from known-working minimal snapshot first, then add generic features deliberately. |
| Manual bridge startup remains friction | High | Medium | Add single launcher and duplicate-process guard before public testing. |
| Dependency removal breaks X4 UI integration | Medium | Medium | Remove SirNukes/kuertee only after proving replacement path; keep `djfhe_http` as explicit bridge dependency for now. |
| AI Influence grows before bridge is stable | High | High | Do not build AI Influence gameplay until Neural Link passes standalone health/request/update tests. |
| LLM output mutates game directly | Low if designed correctly | High | Neural Link returns messages; AI Influence validates actions; X4 applies only whitelisted effects. |

---

## Definition Of Done

Neural Link is done when:

- It lives in its own `x4_neural_link` extension directory.
- It loads independently in X4.
- It starts or clearly instructs how to start its bridge runtime.
- It talks to Player2 through configurable localhost defaults.
- It exposes stable generic request/response endpoints.
- It fails safely when Player2 is missing, offline, out of joules, or lacking usable chat/NPC capability.
- It contains no AI Influence gameplay code or stale artifacts from the old mod.

AI Influence MVP is done later when:

- It is rebuilt separately through Forge.
- It depends on Neural Link.
- It has one in-game conversation path that reaches Player2 through Neural Link.
- It remembers one meaningful interaction.
- It executes no game-state change unless the action is whitelisted, validated, logged, and accepted by X4.
