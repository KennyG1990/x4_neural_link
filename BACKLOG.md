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

## NEXT SESSION — first 20 minutes (UPDATED 3rd: post-attempt-3 state)
-1. /reloadui + /refreshmd in-game → confirm "LUAV=3" in debuglog → offers list WITH element-task objectives
    and sentence-case titles → then run 0a/0b below.
0a. DIAGNOSE: offers CREATED but NOT LISTED post-reload (debuglog 10106: "AIC contract offered job=…" for
    ministry/antigone, yet Mission Offers board shows "No missions"). Prior reloads listed them fine. Suspects:
    the persistent `'null' is not a string` attr in create_offer (find it — add per-attr debug_texts before
    create_offer: $title/$desc/$task/$mtype/$otype/$Client), or offer visibility/space grouping. NOTE: the
    debug reward print "7000000" is CENTS (display was 70,000 Cr — non-issue).
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
- **W — WAR INDUSTRY pipeline** (parent; Ken directive 2026-07-01; spec: wiki [[war-industry-pipeline-spec]]) —
  losses → Player2 build decision → build_orders at REAL shipyards → ware bills → market supply jobs → observed
  deliveries → real hulls → fleet_strength → OPORD force. Order: W2 RESEARCH (build-placement recipe, BLOCKS all)
  → W1 ledger → W3 market wiring → W4 completion → W5 dashboard panel.
- **G4a — escort binding** `spec'd` (in G4): bind a REAL freighter at accept (find_ship pattern), guidance-to-ship
  objective, survival = completion evidence, death = fail + hostile event + FRAGO; no bindable freighter → post
  as patrol. Rule banked: **no contract without a bindable real object.**
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
  - G4 `◐ patrol slice SHIPPED 2026-07-02` → ROADMAP #88 (RML_Patrol handoff + endtime/timeout + MissionEnded
    payout: reward_player + rep + /v1/job/complete; Forge-clean, Lua registers 0 missing). REMAINING: (a)
    IN-GAME GATE — accept a patrol contract, see RML objectives, complete, get PAID (this also = G6); (b)
    template RML handoffs for escort (G4a TargetShip)/supply/bounty/recon after patrol proves; (c)
    FactionRelations_Changed guard (ground the event shape in gm_escort:934 first); (d) mission duration from
    job urgency instead of fixed 4h.
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
- D `spec'd` — strip #70 TEMP diagnostics after one more stable session: bridge poll-logger
  (router.opord_orders_pending), Lua "POST sent"/"pending=N" info logs (KEEP the error logs), MD ENTER debug_text
- CI-gate hardening `spec'd` — consider full-suite nightly vs fast-subset per reload (watch reload latency);
  gate activates when Ken restarts the watcher window

## Small / cleanup
- Verify stance pass-through in-game after next reload (one aggressive-type task) — closes ROADMAP #72 ◐
- Purge residue: orphan lease LGV-705 (pre-fix, status 'issued'), `stance_probe_*` save rows, empty force_request
  `freq_b8eee7a420`, forced test op op_argon_6d827f1a1d/task_6fb9d6cbfb (now a real running order — keep/kill?)
- Consider a proper stance column on operation_tasks when a COA type needs a non-derivable posture (#72 tail)

## Watch
- #70b unexplained window: one reload where opord_assign UI events didn't reach MD while other UI events worked —
  if it recurs, add a Lua-side log around AddUITriggeredEvent and compare first-poll-after-load vs later polls
