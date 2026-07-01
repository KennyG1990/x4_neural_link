# HANDOFF — x4_neural_link (Python bridge)

> Updated 2026-07-01. Supersedes the 2026-06-30 version (which predated the finished mod and the Player2 decision
> layer — both now exist and are live). `ROADMAP.md` (this repo) is the source of truth (✅/◐ + dated verification
> snapshots). The full project handoff is `handoff-fable-2026-07-01.md` — architecture, the 8-step workflow, and
> banked gotchas. Read ROADMAP + that first.

## State (2026-07-01)

All backlog tasks #1–#67 (+#48/#49) closed; **22/22 selftest suites green**. The engine is handed to Player2:
- Every "thinking" decision routes through `router.decide(...)` (bounded menu → Player2 picks in character →
  audited `decision_records` row) or `router.decide_actions(...)` (proposal mode `{response, actions[]}` →
  whitelist → audit). On any Player2 failure the decision DEFERS — no math fallback, ever (online-only by design).
- Live loop: `_influence_daemon` (~22s) drives `decision_tick(save)` — self-gated tiers (operational ~300s:
  COA/route/assess; strategic ~900s: offers/propose/faction review/NPC scene) — and `_drain_from_tick` pushes
  verdicts, COA commitments, "Overheard —" scene lines, and validated relation actions into `/v1/influence_drain`
  for the game's Lua heartbeat.
- Transport (`player2_client.py`): JSON-contract calls → `complete()` (stateless `/v1/chat/completions`);
  free-form chat / numbered `decide()` → `npc_complete()` (NPC API). Do not cross them (prose kills the JSON parse).
- Action gate (`actions.py`): normalize (object or terse `"relation:argon,change:negative"`) → classify
  allowed/gated/unknown (unknown = DENY) → only validated subset executes. `prompt_action_spec()` advertises ONLY
  enabled verbs with grammar. Whitelist: `x4_ai_influence/config/action_whitelist.json` (+ embedded DEFAULT here —
  keep in sync; when a verb changes tier, grep selftests that hardcode its classification).
- Relation actions are live end-to-end: `validate_relation_move` (DeadAir-grounded eligibility, ±5 step, ±25 band)
  → drain `actions[]` → existing Lua `On_action` → MD `set_faction_relation`.

## Runtime

- Run+watch: `Deploy-And-Restart.bat` — compile-gates and auto-reloads `bridge/` + `config/` in place (~6s after
  save). Bridge: `http://127.0.0.1:8713` (dashboard `/dashboard`). Player2 app: `http://127.0.0.1:4315`.
- Selftests/drivers: `GET /api/ops/<name>`. Live demos: `opord_player2_demo`, `scheduled_scene`,
  `offers_resolve_llm`, `decision_tick`. Proof pattern for behaviour: BEFORE/AFTER counts on the live save
  (`/api/agreements` by status, `/api/ops/decisions` by type).

## Hard rules

- **NEVER hardcode or assume a save_id — anywhere.** Ken starts fresh save files at will to prove new logic against
  clean data, and reloads change the uuid (`game_<save_uuid>`). Any save id written in ROADMAP/docs/tests is a
  HISTORICAL record only. ALWAYS resolve the active save at time-of-use: `GET /api/memory/saves` → most-recent
  `last_active_ms`. Old saves in the DB are expected residue — never treat their contents as current state.
- Never validate bridge Python via the bash mount (it truncates large files — `memory.py` is 548KB, `router.py`
  321KB). Use host Read/Edit/Grep; prove changes via the live bridge after the watcher reloads.
- Honesty gate: ✅ only when every applicable check passed and is cited by name; player-facing = ✅ only after
  in-game visual proof (else ◐). Update ROADMAP.md at the close of every unit of work.

## Frontier

In-game visual verification (the one recurring ◐ across #62/#63/#64/#67): foreground X4 on the CURRENT active save
(resolve it live), let the strategic tick fire, and SEE the logbook lines + relation shift on screen.
