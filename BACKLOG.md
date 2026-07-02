# BACKLOG — X4 Neural Link + AI Influence (OPEN work only)

> Workflow v2: sessions START here. States: `spec'd` · `in-progress` · `blocked(<on>)` · `watch`.
> Closing an item = delete it here + write the dated, validation-cited entry in ROADMAP.md (Ken commits via
> Antigravity — agents never run git).
> History NEVER lives in this file. Verified history: ROADMAP.md. Decisions: F:\StarForge\wiki\x4-neural-link\decisions.md.
> ⚠ AGENTS: never read-modify-write this (or any) file through the SANDBOX MOUNT — stale reads truncate content
> (bit us twice 2026-07-01). Use the host file tools (Read/Edit/Write).

## ⚠ READ FIRST — quickload does NOT reload ui/*.lua. THE FAST PATH (Ken): in-game chat commands
## **/reloadui** (reloads Lua) and **/refreshmd** (reloads MD) — no restart, no F5/F9 needed. Confirm the
## resident Lua via the "LUAV=3" marker in the poll log before trusting offer/accept behavior.

✅ BRIDGE RESTORED + A4 slice-2 VERIFIED 2026-07-02 14:21 → ROADMAP #123 (CI GATE PASS ×2; route 7/7 incl.
player2_verb_choice_rides_job; full regression sweep green). Watcher tool-improvement (RED line should carry
the failing check name / first trace line, not just "unreachable") logged below under Tooling.

## NEXT SESSION — first 20 minutes (UPDATED 4th: post-#94, 2026-07-02 marathon end)
-1. READ ROADMAP #84–#94 (the whole contract lifecycle shipped + in-game-proven this run: accept/claim,
    abort+rep, FRAGO push, patrol RML, escort binding, guidance fix, urgency window). Debuglog is directly
    grepable via the connected save folder. Direct evidence beats the log-tail window.
0a. IN-GAME GATES with Ken (= G6 core): patrol contract flown to COMPLETION → yellow RML objectives →
    reward_player credits + rep + "Contract fulfilled" + bridge /v1/job/complete (budget_spent). Escort
    contract: guidance to the REAL freighter → 15km proximity → convoy runs to AO → paid (or ship dies →
    hostile_event on dashboard). FRAGO on a claimed contract: description update + notification.
0b. Then implement the three remaining RML handoffs — params ALREADY GROUNDED (see G4 item below): supply →
    DeliverWares (research gm_supplyfactory Offers construction FIRST), bounty → Destroy_Entities (real
    hostiles group), recon → Scan (TargetStation mode).
0b. ~~ACCEPT LISTENER~~ ✅ 2026-07-01 → ROADMAP #84 (kuertee actor-signal shape; ACCEPTED line + mission in
    manager + bridge claimed — G3 CLOSED after 6 attempts).
0c. ~~ABORT slice~~ ✅ IN-GAME 2026-07-02 → ROADMAP #88 addendum (Ken's live abort: ABORTED line + mission
    cleared + trust −2 / rep −0.02 both layers + job re-listed).
0f. `in-progress` G4 IN-GAME GATE, live with Ken: accept/activate/abort/penalty/escalation-raise ALL PROVEN
    (#88 addendum). OPEN: no yellow objective line on our mission entry — awaiting Ken's mission-popup
    screenshot to discriminate empty-objectives vs undock-first (rml_patrol.xml:301, he was docked) vs working.
    Then: fly the patrol → complete → PAID = G6 core.
0d. ~~STRIP [AI TEST] force-war slice~~ ✅ 2026-07-02 → ROADMAP #85 (also found: it re-forced war on EVERY load
    via md.Setup.Start). OPEN residue: live save still carries the forced -1.0 alliance→player relation — ask Ken
    whether to restore to 0.
0e. ~~ABORT costs reputation~~ ◐ 2026-07-02 → ROADMAP #85 (MD -0.02 + logbook; bridge trust -2 player-only,
    unit-tested). In-game verify rides the 0c abort pass (/refreshmd + /reloadui → accept → abort).

## PREVIOUS (superseded) first-20 list
0. G3 accept fix attempt 3: TOP-LEVEL bare `<event_offer_accepted />` + `event.cue.$job` matching (see ROADMAP
   #75-G3 addendum 2 — child-of-instance listeners don't receive the event, two shapes proven dead). Also one
   debug_text on $d.$task? to pin the Objectives-dup (bridge side ruled out). Then accept → claimed proof.

## PREVIOUS first-20 list
1. F5/F9 reload → fresh offers carry the doctrinal SMESC briefing + element-task objectives + correct 70k rewards
   (ROADMAP #77/#78 ◐) — screenshot a briefing, flip #77/#78 ✅.
2. G3 accept→claim fix (ShowOffer listener pattern, ROADMAP #75-G3) — then accept a contract and verify
   /api/jobs status=claimed.
3. G5 load-time cleanup: cancel stale savegame offer instances (700 Cr / 7M Cr rows).

## Keystone (player-facing)
- **R — MISSION CAPABILITY REQUIREMENTS (THE SUBSTANTIATION SET — Ken 2026-07-02): wiki
  [[opord-mission-requirements]] is the authoritative scoreboard.** R1-R11 Tier-1 missions must each be flyable
  AND payable in-game (the #97 standard) for the OPORD gameplay to count as delivered. R1 escort ✅ paid ·
  R3 patrol ◐ · R2/R4-R11 spec'd. A4 (verb engine) is the shared substrate; each R-row is then a thin slice.
  Flip rows only with cited in-game evidence.
- **A — ASSESSMENT LINKAGE (parent; Ken doctrine 2026-07-02: "these things need to be linked to the cause")**
  Every mission must trace to the ASSESSED event that spooled it; the NATO sequence's assessment phase is the
  source of record, not accept-time improvisation.
  - ~~A1~~ ✅ 2026-07-02 → ROADMAP #99 (floor OP_MIN_EVENTS=2/OP_MIN_MAGNITUDE=6 + assessment record with
    threatened_assets in op evidence; recognize_selftest 12/12 live). A2 now has its binding pool.
  - A2 `◐ GENERALIZED 2026-07-02` → ROADMAP #101 + #113 (verb-aware bind: escort→surviving ship,
    defend→damaged station; every gate reads the assessment first; destroy sector-scoped = honest limit).
    IN-GAME proof pending: "CAUSE-LINKED bind" debuglog lines on the next combat-op escort/defend contracts. (Ken doctrine 2026-07-02: "the escort target needs a
    goal... 'you will escort your target to safety', not 'in circles for 20 minutes'"): at op formation record
    "assets under threat" (victim ships/routes from the triggering hostile events) into op evidence; accept-time
    binding draws from that list (alive → bind), fallback = victim-faction freighter transiting the AO/route;
    NO cause-linked candidate → do NOT post an escort (rule: bindable CAUSE-LINKED object). THE GOAL IS PART OF
    THE ORDER: destination derives from the op (the threatened route's endpoint), and it is STATED consistently
    in the SMESC mission statement, the briefing, the objective text ("Escort <ship> to <station>"), and enacted
    by the ship's behavior (#96's dock-destination is the mechanical half). Also: commandeer on a squadron
    leader moves ALL 4 ships — decide whether convoy=squadron is a feature (bigger convoys) or bind loose hulls
    only. Replaces #93's nearest-hull-anywhere.
  - A3 `◐ core live 2026-07-02` → ROADMAP #102 (probation tier: trust ≤ -10 hides >100k contracts; selftest
    10/10 incl. recovery). REMAINDER: in-game rep weighting via relations_sync · preferred-tier perks
    (advances/exclusives) · deposit mechanics.
  - `spec'd→◐` PATROL WINDOW FROM ASSESSMENT → ROADMAP #102 (mintime 3min×urgency; rides next /refreshmd).
  - A6 `◐ CORE LIVE 2026-07-02` → ROADMAP #104 (pricing ceiling) + **#108 (decision half: costed options in the
    routing brief, accept_risk route, economy convoys through the chooser — route_decision_selftest 6/6)**.
    ~~risk consequence watch~~ ✅ #109 (sweep_risk_watches 4/4 — realized gambles attributed to the op ledger).
    ~~seek_ceasefire~~ ✅ 2026-07-02 → ROADMAP #124 (broke+war-eligible → political option; route_decision
    12/12; in-game observation of a live broke faction choosing politics = ◐ rides the decision cadence).
    REMAINDER: threat-scaled treasury fraction · in-game observation of a real accept_risk convoy loss feeding
    back. ORIGINAL SPEC — FORCE-ECONOMICS GATE — make-vs-buy-vs-TALK (Ken doctrine 2026-07-02: "they should be using
    politics to avoid war while they build their economy, not giving all their money to contractors... they
    literally have hundreds of ships"): BEFORE commissioning a contract, Player2's decision must weigh the
    LEGAL OPTIONS with real costs attached: (a) TASK OWN FLEET — fleet_strength shows availability (antigone:
    283 fight ships); cost = opportunity/attrition risk, not treasury; (b) HIRE CONTRACTOR — treasury cost,
    capped as % of available (a 2.1M faction posting 232k patrols = 11% of liquidity on ONE contract is
    irrational); (c) DIPLOMACY — the negotiation system already exists (allied_support, agreements): broke or
    losing factions should sue for de-escalation/support, not outspend; (d) ACCEPT RISK (do nothing, log the
    assessment). Contracts become what they are in reality: the option for surge capacity and jobs OWN forces
    can't cover — not the reflex. Engine derives the option set + costs deterministically; Player2 chooses
    (ADR-001). Also: contract pricing ceiling as fraction of available treasury, scaled by fleet-coverage gap.
  - `spec'd` PATROL WINDOW FROM ASSESSMENT (with A6): mintime/maxtime in the patrol Destinations entry derive
    from op urgency/magnitude (10-30min, not the hardcoded 1min/10min that paid 232k for 60 quiet seconds);
    quiet-AO completion pays partial (presence) vs full (contact handled) — completion QUALITY in evidence.
  - ~~A5~~ 🏁 COMPLETE 5/5 2026-07-02 → #106 (d) · #110 (a)(c) · #120 (e) · #121 (b: engagement_id +
    co_victims in assessments; recognize 17/17). Follow-ons banked: per-engagement contract caps ·
    follow_support toward co-victims · galaxy topology sync (nearest-safe, route-aware interdiction — W-adjacent).
  - A4 `◐ slice 1 live 2026-07-02` → ROADMAP #105 (TASK_VERBS table + derive_legal_verbs from assessment +
    task_verb/legal_verbs on every op-minted job; verb_engine_selftest 7/7). SLICE 2: Player2 in-set choice at
    routing · verb-conjugated SMESC templates · MD gates by task_verb · economy types through the EXISTING
    route_task chooser (reconcile: make-vs-buy-vs-talk already exists for combat — extend, don't rebuild).
    ORIGINAL SPEC — MISSION TASK VERBS = the contract type system (Ken doc 2026-07-02; wiki [[mission-task-verbs]],
    raw source in StarForge raw/): verb DERIVED from assessed cause + target ACTIVITY; each verb has binding
    PRECONDITIONS (escort requires an entity ON THE MOVE with a destination — #97's patrol squadron made escort
    ILLEGAL; correct verb was Follow and Support), an RML mapping, and doctrinal SUCCESS criteria; Player2 picks
    from the LEGAL verb set (ADR-001). Mission statement, objective, binding, gameplay, and completion evidence
    must all conjugate the SAME verb. Implement one verb slice at a time — Follow and Support first (#97 proved
    the demand); verb table lives as bridge data.
- **W — WAR INDUSTRY pipeline** (parent; Ken directive 2026-07-01; spec: wiki [[war-industry-pipeline-spec]]) —
  losses → Player2 build decision → build_orders at REAL shipyards → ware bills → market supply jobs → observed
  deliveries → real hulls → fleet_strength → OPORD force. Order: W2 RESEARCH (build-placement recipe, BLOCKS all)
  → W1 ledger → W3 market wiring → W4 completion → W5 dashboard panel.
- ~~G4a escort binding~~ ◐ SHIPPED 2026-07-02 → ROADMAP #93 (real freighter + objective.escort guidance +
  proximity-gated RML_Escort to the AO + loss→hostile_event; objective.custom null-string killed). In-game
  verify: accept an escort contract post-reload.
- **FRAGO push to active player contracts** `spec'd` blocked(G3,G4) — operation frago_issued events for ops with
  player-claimed jobs → drain `contract_frago` → Lua ui event → MD updates the accepted mission cue
  (set_objective/update_mission: new objective line + reward bump) + comm-link ping "FRAGO from <faction> High
  Command" + report row. The "element under command" moment — situation changes reach the player MID-MISSION.
- **#75 G — mission offers over market_jobs** (parent; Ken decision 2026-07-01, spec in ROADMAP #75)
  - ~~G1~~ ✅ 2026-07-01 → ROADMAP #75-G1 (offers/claim routes live, selftest 4/4→7/7)
  - ~~G2~~ ✅ 2026-07-01 → ROADMAP #75-G2 + wiki [[mission-offer-recipe]] (custom create_offer path; the
    cross-script cue-ref idea was falsified)
  - G3 `in-progress ◐` (ROADMAP #75-G3) — offers LIVE ON SCREEN (correct rewards, briefing, Accept renders).
    ONE open link: event_offer_accepted child-cue never fires → refactor to vanilla's ShowOffer shape (top-level
    listener on stored $OfferCue via Registry). Prove: accept → ACCEPTED debug line → /api/jobs status=claimed.
    START HERE.
  - ~~G4 / R-row builds~~ 🏁 BUILD COMPLETE 2026-07-02 → ROADMAP #88-#117: ALL 11 Tier-1 mission types exist
    (2 PAID, 9 wired — scoreboard: wiki [[opord-mission-requirements]]; per-verb runbook:
    [[adding-a-mission-verb]]). REMAINING under this banner: in-game flights per row (#97 standard) ·
    FactionRelations_Changed guard (gm_escort:934 shape) · FRAGO structured amendments (objective/reward
    delta) · task_verb + deliver-amount as first-class job columns (one schema touch, with the A4 slice-2
    tail: Player2 picks the verb from legal_verbs at routing).
  - G5 `◐` — DONE 2026-07-02: escalation repricing (ROADMAP #90, withdraw+re-offer, unit-proven) · NPC-claim
    withdrawal (covered by the gone→withdraw path, #90) · abort/release lifecycle (#84/#85/#89). REMAINING:
    Cleanup_on_load reconcile-not-cancel (board churn per reload — #84 AAR pick), expiry-vs-job-row policy,
    desc money formatting (cosmetic), residue purge (LGV-705 orphan lease · stance_probe_* saves ·
    freq_b8eee7a420 — needs a safe admin route; never raw-write the live DB)
  - G6 `spec'd` blocked(G4,G5) — E2E in-game gate: see offer → accept → complete → PAID (screenshots; player
    credits up + faction budget_spent up + ledger row) → #75 ✅

## Open (bridge/dashboard)
- ~~G3c~~ ✅ 2026-07-01 → ROADMAP #78 (doctrinal SMESC subparagraphs — Enemy/Friendly/Constraints, doubled
  mission, concept of ops from the real #65 opord_json, repair/salvage, Command a./Signal b. — all from live
  data; in-game render rides next reload). Deferred nicety: warning-order → offer teaser (WNGO already exists).
- E `spec'd` — Economy Truth freshness panel (dashboard): last sync ts, station/offer/module counts, staleness
  warnings (Economy Update spec §7)
- ~~D~~ ✅ 2026-07-02 → ROADMAP #103 (all temp diags + navtest + test_frago stripped; lifecycle evidence
  lines + LUAV marker deliberately kept)
- CI-gate hardening `spec'd` — consider full-suite nightly vs fast-subset per reload (watch reload latency);
  gate activates when Ken restarts the watcher window

## Small / cleanup
- `spec'd` (AAR #125): /api routes requiring save_id should 400 on empty instead of silently matching nothing
  (WHERE save_id='' returned [] — looked like "no jobs" during live-defect triage), or default to the
  most-recently-active save from /api/memory/saves.
- `spec'd` TOOLING (AAR #123): watcher CI-gate catch block — on selftest failure, read the 500 response body
  (Invoke-RestMethod throws on 500; catch has the response) and log the failing check names + first trace line
  to ci_gate.log instead of bare "(unreachable)". A RED line should be actionable without a browser fetch.
- Verify complete_job's trust +3 fired for job_8dcf98ca2f (antigone reads -2 post-completion where +1 expected;
  check the claimant plumb through Lua ContractCompleted → /v1/job/complete → complete_job)
- Confirm antigone budget_spent bumped on the dashboard for the #97 payout (the /api/factions probe returned {})
- Verify stance pass-through in-game after next reload (one aggressive-type task) — closes ROADMAP #72 ◐
- Purge residue: orphan lease LGV-705 (pre-fix, status 'issued'), `stance_probe_*` save rows, empty force_request
  `freq_b8eee7a420`, forced test op op_argon_6d827f1a1d/task_6fb9d6cbfb (now a real running order — keep/kill?)
- Consider a proper stance column on operation_tasks when a COA type needs a non-derivable posture (#72 tail)

## Watch
- #70b unexplained window: one reload where opord_assign UI events didn't reach MD while other UI events worked —
  if it recurs, add a Lua-side log around AddUITriggeredEvent and compare first-poll-after-load vs later polls
