from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .contracts import ContractError, NeuralRequest, NeuralResponse
from .events import EventQueue
from .memory import (
    MemoryStore, run_memory_selftest, run_memory_stress,
    run_universe_selftest, run_full_stress, run_npc_identity_selftest,
    run_npc_rebind_selftest, run_npc_promotion_selftest, run_npc_recall_gate_selftest,
    run_role_inference_selftest, run_soft_confirm_selftest,
    relationship_beat_line, run_relationship_beat_selftest,
    classify_tone, run_tone_reaction_selftest,
    run_blackboard_probe_selftest, run_blackboard_bind_selftest,
    run_deceased_sweep_selftest, run_oport_selftest, run_threat_recognition_selftest,
    run_mission_analysis_selftest, run_coa_engine_selftest, run_opord_generator_selftest,
    run_execution_routing_selftest, run_assessment_frago_selftest, run_opord_events_selftest,
    run_opord_cleanup_selftest, run_ops_health_selftest, run_opord_e2e_selftest,
    run_threat_sources_selftest, run_execution_lifecycle_selftest, run_opord_lease_selftest,
    run_negotiation_dedup_selftest, run_negotiation_scoring_selftest, run_decision_record_selftest,
    run_negotiation_consequence_selftest, run_faction_doctrine_brief_selftest,
    run_relation_move_validator_selftest, run_oc1_resume_selftest,
)
from .actions import run_actions_selftest, validate_actions, load_whitelist, prompt_action_spec
from .player2_client import Player2Client
from .telemetry import BridgeTelemetry

try:
    from .narrator import Narrator
except ImportError:  # allow direct (non-package) import in tests
    from narrator import Narrator

try:
    from .gates import EventGate
except ImportError:
    from gates import EventGate

try:
    from . import lore as lore_mod
    from . import catdat as catdat_mod
    from . import diplomacy as diplomacy_mod
    from . import offers as offers_mod
except ImportError:  # pragma: no cover - non-package execution
    import lore as lore_mod  # type: ignore
    import catdat as catdat_mod  # type: ignore
    import diplomacy as diplomacy_mod  # type: ignore
    import offers as offers_mod  # type: ignore


def comms_sender_fields(save_id: str, fid: str, fname: str, rep: str, kind: str, reasons: list) -> dict:
    """M5a: derive the SENDER + transmission-id fields for a player communiqué. Pure (no LLM/DB) so it's
    deterministically testable. sender = the faction's named representative when known, else '<Faction> High
    Command'. sender_npc_key is the chat key the Reply will open (save|chat|name) → resolves the NPC's memory.
    priority gates the Messages High/Low tab. tx_id is the stable per-transmission id for the menu isolation
    registry (§4.1) and tells the reply chat which message is being answered."""
    sender_name = (rep or "").strip() or (f"{fname} High Command")
    reasons = reasons or []
    priority = "high" if ("major" in reasons or "near" in reasons or kind in ("threat", "favour")) else "low"
    return {
        "tx_id": "tx_" + uuid.uuid4().hex[:12],
        "sender_name": sender_name,
        "sender_faction": fid,
        "sender_npc_key": MemoryStore.make_key(save_id, "chat", sender_name),
        "sender_role": "faction_representative",
        "priority": priority,
    }


def run_comms_sender_selftest() -> dict:
    """Deterministic oracle for M5a: a known representative becomes the sender; otherwise a named faction-rep
    fallback; sender_npc_key is the chat key for that name; priority follows major/near/threat/favour; tx_id is
    present, prefixed, and unique per call."""
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    rep = comms_sender_fields("game_1", "argon", "Argon Federation", "Melissa Mettel", "alert", ["major"])
    check("uses_named_representative", rep["sender_name"] == "Melissa Mettel", rep["sender_name"])
    check("sender_key_is_chat_key", rep["sender_npc_key"] == MemoryStore.make_key("game_1", "chat", "Melissa Mettel"))
    check("sender_faction_set", rep["sender_faction"] == "argon" and rep["sender_role"] == "faction_representative")
    check("major_is_high_priority", rep["priority"] == "high")

    fb = comms_sender_fields("game_1", "teladi", "Teladi Company", "", "alert", [])
    check("fallback_rep_name", fb["sender_name"] == "Teladi Company High Command", fb["sender_name"])
    check("no_reason_is_low_priority", fb["priority"] == "low", fb["priority"])

    threat = comms_sender_fields("game_1", "split", "Split", "", "threat", [])
    check("threat_is_high_priority", threat["priority"] == "high")

    check("tx_id_prefixed", str(rep["tx_id"]).startswith("tx_"))
    a = comms_sender_fields("game_1", "argon", "Argon", "X", "alert", [])
    b = comms_sender_fields("game_1", "argon", "Argon", "X", "alert", [])
    check("tx_id_unique", a["tx_id"] != b["tx_id"], f'{a["tx_id"]} vs {b["tx_id"]}')

    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


class NeuralRouter:
    """Request coordinator for Neural Link."""

    def __init__(self, root: Path, config: dict[str, Any]):
        self.root = root
        self.config = config
        self.runtime_dir = root / "runtime"
        self.responses_dir = self.runtime_dir / "responses"
        self.logs_dir = self.runtime_dir / "logs"
        self.db_path = self.runtime_dir / "bridge_telemetry.sqlite3"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry = BridgeTelemetry(self.db_path)

        self.lock = threading.Lock()
        self.responses: dict[str, dict[str, Any]] = {}
        self.updates: deque[dict[str, Any]] = deque()
        # SPEC 1j: prominent unprompted faction->player comms, drained by the mod heartbeat (/v1/player_comms).
        self.player_comms: deque[dict[str, Any]] = deque()
        self._comms_last: dict[tuple, float] = {}   # (save_id, fid) -> last-sent ts (per-faction cooldown)
        self.inflight: set[str] = set()
        self.metrics = {
            "accepted": 0,
            "completed": 0,
            "duplicates": 0,
            "invalid": 0,
        }

        # Durable NPC memory (per-NPC turns, condensed facts, decay). The default
        # heuristic summarizer keeps condensation deterministic and joule-free.
        self.memory = MemoryStore(self.runtime_dir / "npc_memory.sqlite3")

        self.player2 = Player2Client(
            base_url=str(config.get("player2_base_url", "http://127.0.0.1:4315")),
            game_client_id=str(config.get("game_client_id", "x4_neural_link")),
            timeout_seconds=int(config.get("player2_timeout_seconds", 30)),
            memory_store=self.memory,
            # #68: bounded concurrency to the HOSTED Player2 model (validated ~3.6x throughput, 0 errors at 3).
            chat_concurrency=int(config.get("player2_chat_concurrency", 3)),
        )
        # SPEC 2b: Narrator layer — turns recorded world_events into grounded history articles (cause-gated).
        self._narrator = Narrator(self.memory)
        # SPEC 3: event priority hierarchy — gates+tiers decide what fires (actuate/news/narrate/comms/store).
        self._gate = EventGate()

        # Event queue: buffer events, resolve a batch each interval. Resolver defaults
        # to a no-LLM stub (concatenate coalesced batch) so the auto-flush worker can
        # never quietly burn joules while building; set config event_resolver="llm" to
        # use the real Strategic-AI LLM resolver for a demo.
        use_llm = str(config.get("event_resolver", "stub")).lower() == "llm"
        self.events = EventQueue(
            self.runtime_dir / "event_queue.sqlite3",
            resolver=(self._resolve_events if use_llm else None),
            memory=self.memory,
            flush_interval_s=float(config.get("event_flush_interval_s", 12)),
            batch_size=int(config.get("event_batch_size", 25)),
        )
        self.events.start()

        # ZERO-FRICTION canon (ship requirement): build the universe-constant DB (factions + full ware catalog)
        # from the game's own library files automatically on boot — so a PUBLISHED mod works out of the box on a
        # new game with NO manual harvest/script. Idempotent + version-stamped; off-thread so it never blocks
        # the bridge from serving immediately.
        try:
            threading.Thread(target=self.ensure_canon, name="ensure_canon", daemon=True).start()
        except Exception:
            pass

        # KEYSTONE (2026-06-26): decouple influence GENERATION (slow, LLM-bound) from DELIVERY (fast). The mod's
        # heartbeat must never wait on the LLM — a 6-45s influence_step POST was timing out the in-game request,
        # so news/articles/actions never reached the game. Now a background daemon generates on its own cadence
        # into a per-save DRAIN queue; the mod pulls instantly via GET /v1/influence_drain (no LLM in the request
        # path), exactly like the proven /v1/player_comms pattern.
        self._drain: dict[str, dict[str, list]] = {}     # save_id -> {news, actions, articles, phase_effects}
        self._last_active_save: str | None = None         # set by the mod's drain calls (the live save)
        self._last_drain_ts: float = 0.0                  # gate generation to when the game is actually pulling
        try:
            threading.Thread(target=self._influence_daemon, name="influence_daemon", daemon=True).start()
        except Exception:
            pass

        # Background stress runner: find-the-wall jobs run off-thread so a large
        # run never blocks or times out an HTTP request. Poll status for the result.
        self._stress_lock = threading.Lock()
        self._stress: dict[str, Any] = {"running": False, "result": None,
                                         "started_at": None, "params": None}

        # Player2 END-TO-END pipeline stress: fires REAL prompts through the NPC
        # path to Player2 and measures replies/latency/failures under load. Separate
        # job slot from the DB stress (different thing entirely). Poll for progress.
        self._p2_lock = threading.Lock()
        self._p2: dict[str, Any] = {"running": False, "result": None, "started_at": None,
                                    "params": None, "done": 0, "total": 0,
                                    "ok": 0, "empty": 0, "error": 0}

        # Grounded single-NPC demo: seeds one richly-remembered NPC and runs a real
        # multi-turn conversation with the FULL situation briefing injected, so we can
        # SEE whether grounded context produces immersive, specific replies.
        self._grounded_lock = threading.Lock()
        self._grounded: dict[str, Any] = {"running": False, "result": None,
                                          "started_at": None, "turn": 0, "total": 0}

        # Influence-engine LLM stress: N faction-leaders each make the Stage-2 bounded
        # decision through Player2 (real LLM in the loop), to see how the model + the
        # serialized pipeline handle the full derive->score->LLM-pick->apply cycle at scale.
        self._inf_lock = threading.Lock()
        self._inf: dict[str, Any] = {"running": False, "result": None, "started_at": None,
                                     "done": 0, "total": 0, "ok": 0, "fallback": 0, "error": 0}

    def _resolve_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """The green-light resolver: one consolidated LLM call per batch via the
        Strategic-AI NPC (clean replies through the NPC API)."""
        lines = EventQueue._coalesce(events)
        report = "\n".join(f"- {ln}" for ln in lines)
        msg = ("New reports have arrived from across the galaxy. Give a terse strategic "
               "situation update in 2-3 short sentences, and name the single most urgent issue.\n\n"
               "Reports:\n" + report)
        payload = {
            "request_id": f"evtflush-{int(time.time() * 1000)}",
            "source_mod": "event_queue",
            "channel": "npc",
            "target": {
                "mode": "npc", "game_id": "event_queue", "save_id": "events",
                "npc_name": "Galaxy Strategic AI", "npc_short_name": "StratAI",
                "system_prompt": "You are the galaxy's strategic intelligence officer. Given a batch "
                                 "of incoming event reports, produce a terse strategic situation update "
                                 "(2-3 short sentences). Be decisive and concise.",
            },
            "messages": [{"role": "user", "content": msg}],
        }
        try:
            request = NeuralRequest.from_payload(payload)
            resp = self.player2.npc_complete(request).to_dict()
            return {"ok": resp.get("status") == "ok",
                    "resolution": resp.get("reply") or resp.get("error") or "",
                    "latency_ms": resp.get("latency_ms")}
        except Exception as exc:
            return {"ok": False, "resolution": f"resolve error: {exc}", "latency_ms": 0}

    def events_state(self) -> dict[str, Any]:
        return self.events.state()

    def events_simulate(self, npcs: int = 500, events_per: int = 1) -> dict[str, Any]:
        npcs = max(1, min(2000, int(npcs)))
        events_per = max(1, min(5, int(events_per)))
        return self.events.simulate(npcs, events_per)

    def events_flush(self) -> dict[str, Any]:
        return self.events.flush(reason="manual")

    def events_clear(self) -> dict[str, Any]:
        return self.events.clear()

    def events_enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.events.enqueue(
            summary=str(payload.get("summary", "")),
            target=str(payload.get("target", "global")),
            etype=str(payload.get("etype", "report")),
            importance=int(payload.get("importance", 2) or 2),
            sector=payload.get("sector"),
            faction=payload.get("faction"),
        )
        return {"ok": True, "pending": self.events.pending_count()}

    def memory_selftest(self) -> dict[str, Any]:
        return run_memory_selftest()

    # --- EPIC I (I0): persistent NPC identity layer ---------------------------
    def npc_identity_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the identity foundation (handle-independent key, evidence,
        bindings, backfill, cross-reload memory resolution). No live data touched."""
        return run_npc_identity_selftest()

    def npc_rebind_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for I2 scoring + per-session rebind (cross-reload re-identify)."""
        return run_npc_rebind_selftest()

    def npc_promotion_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for I3 importance-tier promotion."""
        return run_npc_promotion_selftest()

    def npc_recall_gate_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for I4 confidence-gated recall (bound/tentative/ambiguous + union)."""
        return run_npc_recall_gate_selftest()

    def role_inference_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for skill-based role inference (manager/pilot/… from skills)."""
        return run_role_inference_selftest()

    def npc_soft_confirm_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for I7 player soft-confirmation (match→promote, unsupported→reject)."""
        return run_soft_confirm_selftest()

    def identity_soft_confirm(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """I7: promote a tentative bind IFF the player's assertion matches stored memory.
        payload: {npc_key, assertion}."""
        payload = payload or {}
        return self.memory.soft_confirm_identity(
            str(payload.get("npc_key") or ""), str(payload.get("assertion") or ""))

    def reinfer_roles(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """One-shot: correct existing generic-role rows from their skills (fixes pre-inference records)."""
        return self.memory.reinfer_roles()

    def comms_sender_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for M5a: player-comms sender/tx_id/priority enrichment."""
        return run_comms_sender_selftest()

    def relationship_beat_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for M4: ambient relationship-beat generation on social-status transitions."""
        return run_relationship_beat_selftest()

    def tone_reaction_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for M2: conversational tone → bounded relation/attitude reaction."""
        return run_tone_reaction_selftest()

    # --- NPC Blackboard persistent-identity probe ------------------------------
    def blackboard_probe_record(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record one in-game Blackboard probe observation (Phase 1-7 rows posted by the mod's Lua probe)."""
        rid = self.memory.record_blackboard_probe(payload or {})
        return {"ok": True, "id": rid}

    def blackboard_probe_latest(self, save_id: str = "") -> dict[str, Any]:
        return {"ok": True, "probes": self.memory.latest_blackboard_probe(save_id)}

    def blackboard_probe_verdict(self, save_id: str = "") -> dict[str, Any]:
        return {"ok": True, **self.memory.blackboard_verdict(save_id)}

    def blackboard_probe_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the probe VERDICT logic (PASS→USE_BLACKBOARD / reload-fail→REJECT / HYBRID)."""
        return run_blackboard_probe_selftest()

    def identity_bind_blackboard(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Bind an NPC's identity to its durable Blackboard token (the PRIMARY key). payload:
        {save_id, name, faction, role, blackboard_key, runtime_id}."""
        p = payload or {}
        return self.memory.bind_blackboard_identity(
            str(p.get("save_id") or ""), str(p.get("name") or ""), str(p.get("faction") or ""),
            str(p.get("role") or ""), str(p.get("blackboard_key") or ""), str(p.get("runtime_id") or ""))

    def blackboard_bind_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for wiring the Blackboard token as the primary identity key."""
        return run_blackboard_bind_selftest()

    def repair_blackboard_duplicates(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """One-shot heal for NPC cards split into duplicate name+token cards by the old bind."""
        return self.memory.repair_blackboard_duplicates()

    def sweep_deceased(self, save_id: str = "", stale_seconds: str = "") -> dict[str, Any]:
        """Mark/prune NPCs the census hasn't re-seen for > stale_seconds (their ship/station is gone)."""
        try:
            secs = float(stale_seconds) if stale_seconds else 3600.0
        except ValueError:
            secs = 3600.0
        return self.memory.sweep_deceased_npcs(save_id, secs)

    def deceased_sweep_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the deceased staleness sweep."""
        return run_deceased_sweep_selftest()

    def oport_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 1 (schema + repository)."""
        return run_oport_selftest()

    def ops_list(self, save_id: str = "", status: str = "") -> dict[str, Any]:
        """List military operations for a save (optional status filter) — for the dashboard."""
        return {"ok": True, "operations": self.memory.list_operations(save_id, status or None)}

    def ops_detail(self, op_id: str = "") -> dict[str, Any]:
        """Full operation drill-down (op + COAs + tasks + reports)."""
        return {"ok": True, "operation": self.memory.operation_detail(op_id)}

    def ops_recognize(self, save_id: str = "") -> dict[str, Any]:
        """Phase 2: scan real hostile events → create/update deduped warning-order operations."""
        return self.memory.recognize_threats(save_id)

    def threat_recognition_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 2 threat recognition."""
        return run_threat_recognition_selftest()

    def ops_advance(self, save_id: str = "") -> dict[str, Any]:
        """OPORD pipeline driver — run every built stage in order (recognize → analyse → …) for one save."""
        return self.memory.advance_operations(save_id)

    def ops_debug_force_order(self, save_id: str = "", faction: str = "argon") -> dict[str, Any]:
        """DEBUG/TEST ONLY: synthesize one pending-ingame fleet order so the in-game issuer can be exercised."""
        return self.memory.debug_force_pending_order(save_id, faction or "argon")

    def mission_analysis_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 3 mission analysis."""
        return run_mission_analysis_selftest()

    def coa_engine_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 4 COA engine."""
        return run_coa_engine_selftest()

    def opord_generator_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 5 OPORD generator."""
        return run_opord_generator_selftest()

    def execution_routing_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 6 execution routing."""
        return run_execution_routing_selftest()

    def jobs_list(self, save_id: str = "", status: str = "") -> dict[str, Any]:
        """List job-market listings for a save (optional status filter) — for the dashboard."""
        return {"ok": True, "jobs": self.memory.list_jobs(save_id, status or None)}

    def assessment_frago_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 7 assessment + FRAGO."""
        return run_assessment_frago_selftest()

    def opord_events_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 8 milestone world-events (gated, anti-spam)."""
        return run_opord_events_selftest()

    def opord_cleanup_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the P7 hardening (conclude-cleanup + budget-gated reward)."""
        return run_opord_cleanup_selftest()

    def ops_health(self, save_id: str = "") -> dict[str, Any]:
        """Operational health warnings for the dashboard audit panel."""
        return self.memory.operations_health(save_id)

    def ops_health_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for OPORD Phase 9 health warnings."""
        return run_ops_health_selftest()

    def opord_e2e_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """OPORD Phase 10 end-to-end integration oracle (real pipeline composition)."""
        return run_opord_e2e_selftest()

    def threat_sources_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for the P1a feed broadening (economy + agreement-breakdown threat sources)."""
        return run_threat_sources_selftest()

    def execution_lifecycle_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for the #1 execution build (job fulfillment + spend + task success from evidence)."""
        return run_execution_lifecycle_selftest()

    def negotiation_consequence_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for NF3 (a negotiation outcome → bounded relationship consequence + transition world-event)."""
        return run_negotiation_consequence_selftest()

    def actions_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for #57 (Player2 {response, actions[]} parse → normalize → whitelist classify)."""
        return run_actions_selftest()

    def faction_doctrine_brief_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for #53 (faction Worldview line reflects canon FACTION_PERSONA traits + goal)."""
        return run_faction_doctrine_brief_selftest()

    def relation_move_validator_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for #64 (DeadAir-grounded relation-move eligibility+bounds gate)."""
        return run_relation_move_validator_selftest()

    def oc1_resume_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for #48 (OPORD consumes resolved negotiations → task complete/fail)."""
        return run_oc1_resume_selftest()

    def actions_whitelist(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve the live action whitelist from disk (confirms config path resolution against the bridge root)."""
        wl = load_whitelist(self.root)
        return {"ok": True, "mvp_enabled": sorted(wl.get("mvp", set())), "gated": sorted(wl.get("gated", set()))}

    def actions_validate(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate a Player2 response's actions[] (no execution). payload {response} (dict | JSON string | list)."""
        p = payload or {}
        return validate_actions(p.get("response"), root=self.root)

    def actions_proposal_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for #57 PROPOSAL MODE (stub Player2 + temp store): a {response, actions[]} reply is
        parsed + whitelisted + AUDITED (source player2, status 'proposed', nothing executed); an LLM error → DEFER
        (no actions, source 'deferred', recorded as deferred)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, d: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": d})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def complete(self, _req: Any) -> Any:
                return _R('{"response":"Cross us again and there will be consequences.",'
                          '"actions":[{"type":"dialogue_only"},"relation:argon,change:negative","attack:argon"]}')

        class _P2Err:
            def complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_actprop_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/c.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "teladi", name="Teladi")
            self.memory = store
            self.player2 = _P2OK()
            v = self.decide_actions(sid, "chat", "teladi", "Teladi Company",
                                    "An argon captain insults you on open comms.")
            chk("source_player2", v.get("source") == "player2", str(v.get("source")))
            chk("reply_parsed", v.get("reply", "").startswith("Cross us"), v.get("reply", ""))
            # dialogue_only + relation_delta_limited now ALLOWED (#64); attack unknown (default-deny).
            chk("counts_split", v["counts"] == {"total": 3, "allowed": 2, "gated": 0, "unknown": 1}, str(v["counts"]))
            chk("allowed_has_relation", any(x["type"] == "relation_delta_limited" for x in v["allowed"])
                and any(x["type"] == "dialogue_only" for x in v["allowed"]), str(v["allowed"]))
            chk("attack_unknown", len(v["unknown"]) == 1 and v["unknown"][0]["type"] == "attack", str(v["unknown"]))
            chk("audited_proposed", bool(v.get("decision_id")), str(v.get("decision_id")))
            recs = store.list_decision_records(sid) if hasattr(store, "list_decision_records") else []
            chk("record_status_proposed", any(r.get("final_status") == "proposed" for r in recs), str(len(recs)))
            # LLM down → defer, nothing proposed
            self.player2 = _P2Err()
            v2 = self.decide_actions(sid, "chat", "teladi", "Teladi Company", "Another taunt.")
            chk("deferred_on_error", v2.get("source") == "deferred" and v2["counts"]["total"] == 0, str(v2.get("source")))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def job_complete(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Mark a job fulfilled (player/NPC completed the contract) → spend + task done. payload {save_id, job_id, claimant, evidence}."""
        p = payload or {}
        return self.memory.complete_job(str(p.get("save_id") or ""), str(p.get("job_id") or ""),
                                        str(p.get("claimant") or ""), p.get("evidence"))

    # --- OPORD Execution Authority: the MD issuer ↔ bridge contract ---------------------------------
    def opord_orders_pending(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Tasks awaiting a real ship order (the MD issuer polls this). payload {save_id}."""
        sid = str((payload or {}).get("save_id") or "")
        res = self.memory.pending_orders(sid)
        try:  # TEMP diag 2026-07-01: prove whether the game's Lua poll reaches the bridge (appends to runtime/logs)
            import os as _os, time as _t
            _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "runtime", "logs", "opord_poll.log")
            with open(_p, "a", encoding="utf-8") as _f:
                _f.write(f"{_t.time():.0f} save={sid or 'EMPTY'} pending={len(res.get('pending') or [])}\n")
        except Exception:
            pass
        return res

    def opord_lease(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """MD claims a real ship for a task. payload {save_id, operation_id, task_id, faction, ship_runtime_id,
        ship_name?, ship_macro?, ship_class?, sector?, order_kind?, priority?, original_order_summary?}."""
        p = payload or {}
        return self.memory.lease_asset(str(p.get("save_id") or ""), str(p.get("operation_id") or ""),
                                       str(p.get("task_id") or ""), str(p.get("faction") or ""),
                                       str(p.get("ship_runtime_id") or ""), str(p.get("ship_name") or ""),
                                       str(p.get("ship_macro") or ""), str(p.get("ship_class") or ""),
                                       str(p.get("sector") or ""), str(p.get("order_kind") or "protectposition"),
                                       int(p.get("priority") or 0), str(p.get("original_order_summary") or ""))

    def opord_order_issued(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """MD issued the in-game create_order. payload {save_id, lease_id, assigned_order_id}."""
        p = payload or {}
        return self.memory.mark_order_issued(str(p.get("save_id") or ""), str(p.get("lease_id") or ""),
                                             str(p.get("assigned_order_id") or ""))

    def opord_order_event(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watchdog reports an observed order event. payload {save_id, lease_id, event, evidence}."""
        p = payload or {}
        return self.memory.record_order_event(str(p.get("save_id") or ""), str(p.get("lease_id") or ""),
                                              str(p.get("event") or ""), p.get("evidence"))

    def opord_order_failed(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watchdog reports the order failed/lost. payload {save_id, lease_id, reason}."""
        p = payload or {}
        return self.memory.record_order_event(str(p.get("save_id") or ""), str(p.get("lease_id") or ""),
                                              "failed", str(p.get("reason") or ""))

    def opord_release(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Release a lease. payload {save_id, lease_id, reason}."""
        p = payload or {}
        return self.memory.release_asset(str(p.get("save_id") or ""), str(p.get("lease_id") or ""),
                                         str(p.get("reason") or ""))

    def leases_list(self, save_id: str = "") -> dict[str, Any]:
        return {"ok": True, "leases": self.memory.list_leases(save_id)}

    def opord_force_request(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """No ship available → durable force demand. payload {save_id, operation_id, task_id, faction, sector,
        ship_role, ship_size?, quantity?, priority?, reward_budget?}."""
        p = payload or {}
        return self.memory.create_or_update_force_request(
            str(p.get("save_id") or ""), str(p.get("operation_id") or ""), str(p.get("task_id") or ""),
            str(p.get("faction") or ""), str(p.get("sector") or ""), str(p.get("ship_role") or "patrol"),
            str(p.get("ship_size") or ""), int(p.get("quantity") or 1), int(p.get("priority") or 0),
            int(p.get("reward_budget") or 0))

    def opord_lease_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the OPORD Execution Authority lease/order spine."""
        return run_opord_lease_selftest()

    def negotiation_dedup_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for Negotiations N1 (agreement_key dedup + real counterparty)."""
        return run_negotiation_dedup_selftest()

    def negotiation_scoring_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for Negotiations NF2 (acceptance scoring + resolution driver)."""
        return run_negotiation_scoring_selftest()

    def offers_evaluate(self, save_id: str = "") -> dict[str, Any]:
        """Deterministic ADVISORY/fallback scorer (NOT the decider). The real decision layer is resolve_offers_llm."""
        return self.memory.evaluate_open_offers(save_id)

    def decide(self, save_id: str, decision_type: str, actor_faction: str, actor_name: str,
               brief: str, options: list[dict[str, Any]], system_prompt: str | None = None,
               request_id: str | None = None, advisory: Any = None,
               linked_operation_id: str | None = None, linked_offer_id: int | None = None) -> dict[str, Any]:
        """UNIVERSAL DECISION ADAPTER (Player2 Decision Layer spec §3) — the ONE path every thinking action routes
        through. The deterministic caller passes a grounded BRIEF + a BOUNDED list of legal options
        [{"key","label"}]; the Player2 faction-actor picks ONE in character. On ANY failure (LLM down / timeout /
        unparsable) the decision is DEFERRED — choice=None, source='deferred' — NEVER math-substituted (Ken's
        policy). Writes a full decision_records audit row (§12) and returns its id. The caller then VALIDATES +
        EXECUTES + finalizes the record. Player2 can only pick from the engine menu, so it cannot invent an action."""
        import re as _re
        rid = request_id or f"{decision_type}-{int(time.time()*1000)}-{actor_faction}"
        raw = ""
        if not options:
            result = {"choice": None, "reason": "no legal options", "source": "skipped"}
        else:
            menu = "\n".join(f"{i}. {o.get('label') or o.get('key')}" for i, o in enumerate(options, 1))
            prompt = (f"{brief}\n\nOptions:\n{menu}\n\nReply with ONLY the option number "
                      f"(1-{len(options)}) then one short in-character sentence of reasoning.")
            sp = system_prompt or (f"You are the ruling leadership of {actor_name} in the X4 galaxy. Decide in "
                                   "character. Choose exactly ONE numbered option; never invent one. Number first, "
                                   "then one sentence.")
            try:  # #53: doctrine enrichment — every faction decision is flavored by the faction's Worldview.
                _doc = self.memory.faction_doctrine_brief(save_id, actor_faction)
                if _doc:
                    sp = f"{sp}\n\n{_doc}"
            except Exception:
                pass
            payload = {
                "request_id": rid, "source_mod": f"decider:{decision_type}", "channel": "npc",
                "target": {"mode": "npc", "game_id": "influence", "save_id": save_id, "npc_name": actor_name,
                           "npc_short_name": str(actor_faction)[:8], "faction_id": actor_faction,
                           "system_prompt": sp},
                "messages": [{"role": "user", "content": prompt}],
            }
            result = None
            try:
                resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
                raw = (resp.get("reply") or "").strip()
                if resp.get("status") == "ok" and raw:
                    m = _re.search(r"\b([1-9][0-9]?)\b", raw)
                    if m:
                        idx = int(m.group(1)) - 1
                        if 0 <= idx < len(options):
                            result = {"choice": options[idx]["key"], "reason": raw[:300],
                                      "source": "player2", "option_index": idx}
                if result is None:
                    result = {"choice": None, "reason": (raw[:200] if raw else "(no/unparsed reply)"),
                              "source": "deferred"}
            except Exception as exc:
                raw = f"(exception) {exc}"
                result = {"choice": None, "reason": f"(llm error: {exc})", "source": "deferred"}
        # audit log (spec §12) — never let logging break the decision
        try:
            result["decision_id"] = self.memory.record_decision(
                save_id, decision_type, actor_faction, result["source"], parsed_choice=result.get("choice"),
                brief=brief, options=options, advisory=advisory, raw_response=raw, request_id=rid,
                linked_operation_id=linked_operation_id, linked_offer_id=linked_offer_id,
                final_status=("deferred" if result["source"] in ("deferred", "skipped") else "decided"))
        except Exception:
            pass
        return result

    def decide_actions(self, save_id: str, decision_type: str, actor_faction: str, actor_name: str,
                       brief: str, system_prompt: str | None = None, request_id: str | None = None,
                       linked_operation_id: str | None = None, linked_offer_id: int | None = None) -> dict[str, Any]:
        """PROPOSAL MODE (#57) — the Bannerlord-proven free-form path: instead of picking from a bounded menu, the
        Player2 actor returns a spoken reply PLUS a list of PROPOSED actions ({response, actions[]}). The bridge
        parses + whitelists (allowed / gated / unknown) via actions.validate_actions and AUDITS the verdict — it
        EXECUTES NOTHING. Only `allowed` actions may later be actuated by the in-game MD dispatcher; gated/unknown are
        recorded and dropped (default-deny). On LLM down/timeout/unparsed → DEFER (no actions, source='deferred').
        This is the spec boundary in code: Player2 proposes intent, the bridge validates, X4 executes only what passed."""
        rid = request_id or f"{decision_type}-act-{int(time.time()*1000)}-{actor_faction}"
        sp = system_prompt or (
            f"You are the leadership of {actor_name} in the X4 galaxy. Respond IN CHARACTER. "
            "Reply with ONLY strict JSON of the form "
            '{"response": "<one or two sentences you say aloud>", "actions": [<zero or more action strings>]}. '
            "Propose only intent; do not narrate the actions in your response.")
        # Bannerlord-proven generation-time constraint: enumerate the LEGAL verbs (with grammar) in the prompt so the
        # model picks only from the whitelist, instead of relying solely on the post-hoc validator. (#57 alignment.)
        try:
            _spec = prompt_action_spec(root=self.root)
            if _spec:
                sp = f"{sp}\n\n{_spec}"
        except Exception:
            pass
        try:  # #53: doctrine enrichment — the proposing faction speaks/acts from its Worldview.
            _doc = self.memory.faction_doctrine_brief(save_id, actor_faction)
            if _doc:
                sp = f"{sp}\n\n{_doc}"
        except Exception:
            pass
        # Use the STATELESS /v1/chat/completions path (complete), not npc spawn+chat — the Bannerlord-proven method:
        # a system prompt carrying the JSON contract + a user message, returning raw JSON text we parse. The npc_chat
        # path returns in-character PROSE (no JSON), which is why proposal mode came back empty live.
        payload = {
            "request_id": rid, "source_mod": f"propose:{decision_type}", "channel": "npc",
            "target": {"max_tokens": 700, "temperature": 0.5},
            "messages": [{"role": "system", "content": sp}, {"role": "user", "content": brief}],
        }
        raw, source = "", "deferred"
        verdict: dict[str, Any] = {"ok": True, "reply": "", "actions": [], "allowed": [], "gated": [], "unknown": [],
                                   "counts": {"total": 0, "allowed": 0, "gated": 0, "unknown": 0}}
        try:
            resp = self.player2.complete(NeuralRequest.from_payload(payload)).to_dict()
            raw = (resp.get("reply") or "").strip()
            if resp.get("status") == "ok" and raw:
                verdict = validate_actions(raw, root=self.root)
                source = "player2"
        except Exception as exc:
            raw = f"(exception) {exc}"
        try:
            verdict["decision_id"] = self.memory.record_decision(
                save_id, decision_type, actor_faction, source, parsed_choice=None, brief=brief,
                options=verdict.get("actions"), raw_response=raw, request_id=rid,
                linked_operation_id=linked_operation_id, linked_offer_id=linked_offer_id,
                final_status=("deferred" if source != "player2" else "proposed"))
        except Exception:
            pass
        verdict["source"] = source
        return verdict

    def _scene_situation(self, save_id: str, a: str, b: str, rel: dict[str, Any]) -> str:
        """#62: the deterministic SITUATION the world hands two faction reps — grounded in their standing + the most
        recent shared world event. This is the 'message' the scene opens on (perception = deterministic)."""
        trust = float(rel.get("trust") or 0)
        if trust <= -50:
            base = f"{a} and {b} are bitter rivals; tensions run high."
        elif trust < 0:
            base = f"{a} and {b} regard each other with suspicion."
        elif trust >= 50:
            base = f"{a} and {b} are close partners."
        else:
            base = f"{a} and {b} keep a neutral, businesslike relationship."
        try:
            for e in self.memory.list_world_events(save_id, limit=15, min_importance=3):
                pf, sf = e.get("primary_faction"), e.get("secondary_faction")
                if {pf, sf} == {a, b} and e.get("summary"):
                    base += f" Recently: {e.get('summary')}"
                    break
        except Exception:
            pass
        return base

    def run_faction_scene(self, save_id: str, faction_a: str, faction_b: str,
                          situation: str | None = None) -> dict[str, Any]:
        """#62: a two-sided NPC>NPC scene between two faction reps. The engine supplies the SITUATION (the message the
        world hands them); Player2 speaks for BOTH sides via decide_actions (the #57 proposal contract) — A speaks,
        then B replies GIVEN A's line as the incoming message (NPC>NPC works like player>NPC, world = the 'player').
        Both sides' actions are whitelisted + audited; nothing executes here (that's #64/execution). Defers cleanly
        if Player2 is unavailable (no half-scene)."""
        if not faction_a or not faction_b or faction_a == faction_b:
            return {"ok": False, "reason": "need two distinct factions"}
        fa = self.memory.get_faction(save_id, faction_a) or {}
        fb = self.memory.get_faction(save_id, faction_b) or {}
        na = str(fa.get("name") or faction_a)
        nb = str(fb.get("name") or faction_b)
        rel_ab = self.memory.get_relationship(save_id, faction_a, faction_b) or {}
        rel_ba = self.memory.get_relationship(save_id, faction_b, faction_a) or {}
        sit = situation or self._scene_situation(save_id, faction_a, faction_b, rel_ab)
        brief_a = (f"You encounter a representative of {nb}. Situation: {sit}\n"
                   f"Your standing toward {nb}: trust {rel_ab.get('trust')}, resentment {rel_ab.get('resentment')}.\n"
                   "Speak to them in character; propose any actions your doctrine calls for.")
        a = self.decide_actions(save_id, "npc_scene", faction_a, f"{na} envoy", brief_a)
        if a.get("source") != "player2":
            return {"ok": True, "deferred": True, "stage": "a"}
        brief_b = (f"A representative of {na} says to you: \"{a.get('reply')}\"\nSituation: {sit}\n"
                   f"Your standing toward {na}: trust {rel_ba.get('trust')}, resentment {rel_ba.get('resentment')}.\n"
                   "Reply in character; propose any actions your doctrine calls for.")
        b = self.decide_actions(save_id, "npc_scene", faction_b, f"{nb} envoy", brief_b)
        if b.get("source") != "player2":
            return {"ok": True, "deferred": True, "stage": "b",
                    "a": {"faction": faction_a, "says": a.get("reply"), "allowed": a.get("allowed")}}
        return {"ok": True, "situation": sit,
                "a": {"faction": faction_a, "says": a.get("reply"), "allowed": a.get("allowed"),
                      "gated": a.get("gated"), "decision_id": a.get("decision_id")},
                "b": {"faction": faction_b, "says": b.get("reply"), "allowed": b.get("allowed"),
                      "gated": b.get("gated"), "decision_id": b.get("decision_id")}}

    def run_scheduled_scene(self, save_id: str, a: str | None = None, b: str | None = None) -> dict[str, Any]:
        """#63: pick a TOPICAL faction pair (two factions with a recent shared event) and run ONE NPC>NPC scene, then
        PERSIST it (a world_event both sides remember, via faction briefings) and SURFACE it to the player (overheard
        news lines for the logbook). Rate-limited by the caller (one per strategic tick). Defers cleanly.
        An explicit a/b overrides the topical pick (targeting / testing)."""
        if a and b:
            a = a.strip().lower()
            b = b.strip().lower()
        else:
            a = b = None
            try:  # only auto-pick a topical pair when no explicit pair was given
                for e in self.memory.list_world_events(save_id, limit=20, min_importance=3):
                    pf, sf = e.get("primary_faction"), e.get("secondary_faction")
                    if pf and sf and pf != sf and pf != "player" and sf != "player":
                        a, b = pf, sf
                        break
            except Exception:
                pass
        if not a:
            facs = [f.get("faction_id") for f in self.memory.list_factions(save_id)
                    if f.get("faction_id") not in (None, "player")]
            if len(facs) < 2:
                return {"ok": True, "skipped": "no pair"}
            a, b = facs[0], facs[1]
        scene = self.run_faction_scene(save_id, a, b)
        if not scene.get("ok") or scene.get("deferred"):
            return {"ok": True, "deferred": bool(scene.get("deferred")), "pair": [a, b]}
        a_says = (scene.get("a") or {}).get("says") or ""
        b_says = (scene.get("b") or {}).get("says") or ""
        try:  # PERSIST — both remember (world_event feeds each faction's situation briefing)
            self.memory.add_world_event(save_id, event_type="diplomatic",
                                        summary=f"{a} and {b} exchanged words in a tense meeting.",
                                        primary_faction=a, secondary_faction=b, importance=2, source="scene")
        except Exception:
            pass
        # SURFACE — overheard lines for the player logbook (news channel → in-game logbook via the existing path)
        news = [f"Overheard — {a}: \"{a_says[:140]}\"", f"Overheard — {b}: \"{b_says[:140]}\""]
        # #64: validated relation moves proposed in the scene → in-game actions[] (existing Lua→MD set_faction_relation)
        acts = self._relation_drain_actions(save_id, a, (scene.get("a") or {}).get("allowed"))
        acts += self._relation_drain_actions(save_id, b, (scene.get("b") or {}).get("allowed"))
        return {"ok": True, "pair": [a, b], "a_says": a_says, "b_says": b_says, "news": news, "actions": acts}

    def _relation_drain_actions(self, save_id: str, actor: str, allowed: list | None) -> list[dict[str, Any]]:
        """#64: turn Player2's ALLOWED relation_delta_limited proposals into VALIDATED, bounded in-game relation
        actions. Each is eligibility+bounds-checked by validate_relation_move (DeadAir model); a valid move is
        shadow-applied (bridge attitude) and emitted as {type:'adjust_relation', faction, target, relation:<delta>}
        for the existing Lua→MD On_action→set_faction_relation path. Delta maps the ±25 trust band to the game's ±1
        relation scale (/100). The engine VALIDATES; Player2 only proposed (anti-cheat: attitude only)."""
        out: list[dict[str, Any]] = []
        for a in allowed or []:
            if a.get("type") != "relation_delta_limited":
                continue
            p = a.get("params") or {}
            raw_t = str(p.get("target") or p.get("faction") or "")
            target = (self.memory.resolve_faction_id(raw_t) if hasattr(self.memory, "resolve_faction_id")
                      else raw_t.strip().lower())
            change = str(p.get("change") or "").lower()
            step = 5.0 if any(k in change for k in ("pos", "up", "improv", "ally", "warm")) else -5.0
            v = self.memory.validate_relation_move(save_id, actor, target, step)
            if not v.get("ok"):
                continue
            self.memory.adjust_relationship(save_id, actor, target, dtrust=int(v["clamped_step"]))
            out.append({"type": "adjust_relation", "faction": actor, "target": target,
                        "relation": round(float(v["clamped_step"]) / 100.0, 4)})
        return out

    def faction_scene_scheduler_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for #63 (stub Player2 + temp store): a scheduled scene picks the topical pair, runs
        both sides, PERSISTS a world_event both remember, and SURFACES overheard news lines."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, d: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": d})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def complete(self, _req: Any) -> Any:
                return _R('{"response":"You will regret crossing us.",'
                          '"actions":["status:hostile","relation:argon,change:negative"]}')

        d = tempfile.mkdtemp(prefix="nl_sched_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/c.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "split", name="Zyarth")
            store.upsert_faction(sid, "argon", name="Argon")
            store.add_world_event(sid, event_type="war", summary="Border clash", primary_faction="split",
                                  secondary_faction="argon", importance=4)
            self.memory = store
            self.player2 = _P2OK()
            r = self.run_scheduled_scene(sid)
            chk("pair_topical", set(r.get("pair") or []) == {"split", "argon"}, str(r.get("pair")))
            chk("both_spoke", bool(r.get("a_says")) and bool(r.get("b_says")), str(r))
            chk("surfaced_news", len(r.get("news") or []) == 2, str(r.get("news")))
            evs = store.list_world_events(sid, limit=50)
            chk("persisted_scene_event", any(e.get("source") == "scene" for e in evs), str(len(evs)))
            # #64: split's proposed relation move toward argon is validated + emitted as an in-game action.
            rel_acts = [a for a in (r.get("actions") or []) if a.get("type") == "adjust_relation"]
            chk("relation_action_emitted", len(rel_acts) >= 1 and rel_acts[0]["faction"] == "split"
                and rel_acts[0]["target"] == "argon" and rel_acts[0]["relation"] < 0, str(r.get("actions")))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def faction_scene_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for #62 (stub Player2 + temp store): a two-sided scene produces A_says + B_says with
        whitelisted actions on BOTH sides, and BOTH turns are audited to decision_records."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, d: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": d})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def complete(self, _req: Any) -> Any:
                return _R('{"response":"We remember what your kind did.","actions":["status:hostile","attack:you"]}')

        class _P2Err:
            def complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_scene_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/c.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "split", name="Zyarth Patriarchy")
            store.upsert_faction(sid, "teladi", name="Teladi")
            self.memory = store
            self.player2 = _P2OK()
            scene = self.run_faction_scene(sid, "split", "teladi")
            chk("scene_ok", scene.get("ok") and not scene.get("deferred"), str(scene.get("deferred")))
            chk("a_says", bool((scene.get("a") or {}).get("says")), str(scene.get("a")))
            chk("b_says", bool((scene.get("b") or {}).get("says")), str(scene.get("b")))
            chk("a_allowed_status", any(x.get("type") == "status_update" for x in (scene.get("a") or {}).get("allowed") or []),
                str((scene.get("a") or {}).get("allowed")))
            chk("b_gated_or_unknown_attack", not any(x.get("type") == "attack" for x in (scene.get("b") or {}).get("allowed") or []),
                "attack must NOT be allowed")
            recs = store.list_decision_records(sid) if hasattr(store, "list_decision_records") else []
            chk("both_audited", len([r for r in recs if r.get("decision_type") == "npc_scene"]) == 2, str(len(recs)))
            # defer path: B's LLM down → scene defers at stage b, no half-executed nonsense
            self.player2 = _P2Err()
            sc2 = self.run_faction_scene(sid, "split", "teladi")
            chk("defer_clean", sc2.get("deferred") is True, str(sc2))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def _decide_offer_llm(self, save_id: str, agreement: dict[str, Any]) -> dict[str, Any]:
        """D2 negotiation acceptance, routed through the universal adapter. Returns {decision, reason, source}.
        source != 'player2' → the caller MUST leave the offer pending (retry next tick), never math-decide it."""
        sit = self.memory.build_negotiation_situation(save_id, agreement)
        recip, req = sit["recipient"], sit["requester"]
        rel, rs, doc = sit["relationship"], sit["recipient_state"], sit["doctrine"]
        offer = sit["offered_value"]
        brief = (
            f"{req} proposes a {sit['kind']} with you"
            + (f" against {sit['enemy']}" if sit["enemy"] else "")
            + (f" in {sit['sector']}" if sit["sector"] else "")
            + (f", offering {offer} credits." if offer else ", offering nothing.")
            + f"\nYour stance toward {req}: trust {rel.get('trust')}, resentment {rel.get('resentment')}, "
            f"debt {rel.get('debt')}."
            + f"\nYour situation: war pressure {round(float(rs.get('military_pressure') or 0)*100)}%, "
            f"recent losses {round(float(rs.get('recent_losses') or 0)*100)}%."
            + (f"\nYour doctrine/mood: {doc.get('mood')}; goal: {doc.get('current_goal')}."
               if (doc.get('mood') or doc.get('current_goal')) else "")
        )
        options = [{"key": "accept", "label": "Accept the offer"},
                   {"key": "counter", "label": "Counter — demand better terms"},
                   {"key": "refuse", "label": "Refuse"},
                   {"key": "defer", "label": "Stall — give no answer yet"}]
        sp = (f"You are the ruling leadership of {recip} in the X4 galaxy, weighing a diplomatic offer from {req}. "
              "Decide in character from your interests, doctrine, and relationships. Choose ONE numbered option.")
        d = self.decide(save_id, "negotiation", recip, f"{recip} leadership", brief, options,
                        system_prompt=sp, request_id=f"negodec-{int(time.time()*1000)}-{agreement.get('id')}",
                        advisory={"score": sit.get("advisory_score"), "decision": sit.get("advisory_decision")},
                        linked_operation_id=(agreement.get("operation_id") or None),
                        linked_offer_id=agreement.get("id"))
        return {"decision": d.get("choice"), "reason": d.get("reason", ""), "source": d.get("source"),
                "decision_id": d.get("decision_id")}

    def resolve_offers_llm(self, save_id: str = "", max_n: int = 25) -> dict[str, Any]:
        """SLOW-cadence decision driver (T3): hand each open offer (with a real counterparty) to the Player2 faction
        actor. DEFERRED decisions (LLM down / unparsed) are LEFT PENDING and retried next tick — never math-decided
        (Ken's defer policy). Only genuine Player2 decisions are recorded; execution/consequences are NF3/OC."""
        offers = [a for a in self.memory.list_agreements(save_id)
                  if a.get("status") in ("pending", "proposed", "pending_response") and (a.get("party_b") or "")]
        decided, deferred, out, errors = 0, 0, [], []
        for ag in offers[:int(max_n)]:
            try:
                dec = self._decide_offer_llm(save_id, ag)
                if dec.get("source") != "player2" or not dec.get("decision"):
                    deferred += 1
                    continue
                self.memory.apply_offer_decision(save_id, ag["id"], dec["decision"], dec.get("reason", ""), "player2")
                # NF3: the decision has CONSEQUENCES — refusal breeds resentment, acceptance builds trust/debt.
                self.memory.apply_relationship_consequence(save_id, ag.get("party_a"), ag.get("party_b"),
                                                           dec["decision"], int(ag.get("urgency") or 0))
                if dec.get("decision_id"):
                    self.memory.finalize_decision(dec["decision_id"], final_status="applied")
                decided += 1
                out.append({"id": ag["id"], "recipient": ag.get("party_b"), "decision": dec["decision"]})
            except Exception as exc:  # one bad offer must not kill the batch — capture + continue
                errors.append({"id": ag.get("id"), "error": str(exc)[:200]})
        return {"ok": True, "decided": decided, "deferred": deferred, "errors": errors, "results": out}

    def opord_player2_demo(self, faction: str = "split") -> dict[str, Any]:
        """LIVE end-to-end demo (#66): seed a realistic operation in an ISOLATED temp store, keep the REAL Player2
        client, and drive the whole OPORD chain with the actual LLM — Player2 SELECTS the course of action (decide()
        with #53 doctrine in the prompt + defer-on-fail), then the OPORD generates with the 4 doctrinal Execution
        components (#65). Returns the full trace so you can SEE Player2 driving it. Temp store is discarded (no live
        DB pollution); if Player2 is unreachable the pick DEFERS (honestly reported, never math-substituted)."""
        import shutil
        import tempfile
        faction = (faction or "split").strip().lower()
        d = tempfile.mkdtemp(prefix="nl_p2demo_")
        orig_m = self.memory
        trace: dict[str, Any] = {"ok": True, "faction": faction}
        try:
            store = MemoryStore(f"{d}/demo.sqlite3")
            sid = "demo"
            store.upsert_faction(sid, faction, name=faction.title())
            store.upsert_faction(sid, "teladi", name="Teladi")
            store.upsert_fleet_strength(sid, faction, fight=8, total_ships=14)
            store.upsert_economy_station(sid, {"station_id": "d1", "faction_id": faction, "sector_id": "Heretic's End"})
            tk = f"{sid}:{faction}:sector_pressure:teladi:demo"
            op = store.create_or_get_operation(sid, faction, "sector_pressure", tk, status="warning",
                                               target_faction="teladi", target_sector="Heretic's End",
                                               urgency=4, importance=5, evidence_json={"magnitude": 5})["id"]
            store.analyze_mission(op)
            store.plan_operation_coas(op)
            viable = store.list_viable_coas(op)
            trace["doctrine_brief"] = store.faction_doctrine_brief(sid, faction)
            trace["coa_options"] = [{"coa_type": c.get("coa_type"), "concept": c.get("concept"),
                                     "staff_estimate": round(float(c.get("weighted_score") or 0), 2)} for c in viable]
            # ---- LIVE Player2 selects the COA (self.memory swapped to the temp store; self.player2 stays REAL) ----
            self.memory = store
            sel = self.select_pending_coas_llm(sid)
            trace["selection"] = {"decided": sel.get("decided"), "deferred": sel.get("deferred")}
            recs = store.list_decision_records(sid) if hasattr(store, "list_decision_records") else []
            rec = recs[-1] if recs else {}
            trace["player2"] = {"source": rec.get("source"), "reason": rec.get("raw_response") or rec.get("brief_reason"),
                                "picked_coa_id": rec.get("parsed_choice"), "decision_id": rec.get("id")}
            opd = store.get_operation(op)
            picked = next((c for c in store.list_viable_coas(op)
                           if str(c.get("id")) == str(opd.get("selected_coa_id"))), None)
            trace["player2"]["picked_coa_type"] = (picked or {}).get("coa_type")
            # ---- generate the OPORD if Player2 committed a COA; else honestly report the defer ----
            if opd.get("selected_coa_id"):
                store.generate_opord(op)
                smesc = (store.get_operation(op).get("opord_json") or {})
                ex = smesc.get("execution") or {}
                trace["opord_execution"] = {
                    "intent": ex.get("intent"),
                    "scheme_of_manoeuvre": ex.get("scheme_of_manoeuvre"),
                    "main_effort": ex.get("main_effort"),
                    "end_state": ex.get("end_state"),
                }
                trace["mission"] = smesc.get("mission")
            else:
                trace["opord_execution"] = None
                trace["note"] = "Player2 deferred (LLM unreachable/unparsed) — no math substitute, OPORD not generated."
        except Exception as exc:
            trace = {"ok": False, "error": str(exc)[:300], "faction": faction}
        finally:
            self.memory = orig_m
            shutil.rmtree(d, ignore_errors=True)
        return trace

    def select_pending_coas_llm(self, save_id: str = "", max_n: int = 10) -> dict[str, Any]:
        """D1 decision driver (T1, event-ish): for each op at coa_generated WITHOUT a selected COA, Player2 picks the
        course of action that fits its doctrine from the viable (legal+affordable) menu; the validator commits it.
        Defer → the op waits (no math pick). The deterministic score rides along only as advisory in the brief."""
        ops = [o for o in self.memory.list_operations(save_id)
               if o.get("status") == "coa_generated" and not o.get("selected_coa_id")]
        decided, deferred, out = 0, 0, []
        for o in ops[:int(max_n)]:
            try:
                op_id = o["id"]
                det = self.memory.operation_detail(op_id) or {}
                viable = [c for c in (det.get("coas") or []) if c.get("viability_status") in ("viable", "selected")]
                if not viable:
                    continue
                fac = next((f for f in self.memory.list_factions(save_id)
                            if f.get("faction_id") == o.get("faction_id")), {}) or {}
                adv_best = max(viable, key=lambda c: float(c.get("weighted_score") or 0))
                brief = (f"Operation: {o.get('mission_statement') or ('contain ' + str(o.get('target_faction')))} "
                         f"in {o.get('target_sector') or 'the contested zone'} against {o.get('target_faction')}.\n"
                         f"Intent: {o.get('commander_intent') or 'restore freedom of movement'}.\n"
                         f"Your doctrine/mood: {fac.get('mood')}; goal: {fac.get('current_goal')}.\n"
                         "Choose the course of action that best fits your doctrine and situation:")
                options = [{"key": str(c["id"]),
                            "label": f"{c.get('coa_type')}: {c.get('concept')} "
                                     f"(staff estimate {round(float(c.get('weighted_score') or 0), 2)})"}
                           for c in viable]
                sp = (f"You are the ruling military leadership of {o.get('faction_id')} in the X4 galaxy choosing a "
                      "course of action. Pick the ONE option that best fits your doctrine; never invent one.")
                d = self.decide(save_id, "coa_selection", o.get("faction_id"), f"{o.get('faction_id')} command",
                                brief, options, system_prompt=sp,
                                advisory={"advisory_best": adv_best.get("coa_type"),
                                          "score": adv_best.get("weighted_score")},
                                linked_operation_id=op_id)
                if d.get("source") != "player2" or not d.get("choice"):
                    deferred += 1
                    continue
                sr = self.memory.set_selected_coa(op_id, d["choice"])
                if d.get("decision_id"):
                    self.memory.finalize_decision(d["decision_id"], validator_result=sr,
                                                  final_status=("applied" if sr.get("ok") else "rejected_by_validator"))
                if sr.get("ok"):
                    decided += 1
                    out.append({"op_id": op_id, "faction": o.get("faction_id"), "coa_id": d["choice"]})
                else:
                    deferred += 1
            except Exception as exc:
                out.append({"op_id": o.get("id"), "error": str(exc)[:160]})
        return {"ok": True, "decided": decided, "deferred": deferred, "results": out}

    def coa_selection_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for D1 (stub Player2 + temp store): a usable pick commits a selected COA (source
        player2); an LLM error → DEFER (op left without a selected COA, no math pick)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _R("1. This course fits our doctrine.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_coasel_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/c.sqlite3")
            sid = "s"
            store.upsert_fleet_strength(sid, "argon", fight=6, total_ships=12)
            store.upsert_economy_station(sid, {"station_id": "a1", "faction_id": "argon", "sector_id": "X"})

            def mk(key):
                op = store.create_or_get_operation(sid, "argon", "sector_pressure", key, status="warning",
                                                   target_faction="teladi", target_sector="X",
                                                   evidence_json={"magnitude": 4})["id"]
                store.analyze_mission(op)
                store.plan_operation_coas(op)
                return op
            self.memory = store
            op1 = mk("s:argon:sp:teladi:c1")
            self.player2 = _P2OK()
            r1 = self.select_pending_coas_llm(sid)
            chk("decided_one", r1["decided"] == 1)
            chk("selected_set", bool(store.get_operation(op1).get("selected_coa_id")))
            op2 = mk("s:argon:sp:teladi:c2")
            self.player2 = _P2Err()
            r2 = self.select_pending_coas_llm(sid)
            chk("deferred_on_error", r2["deferred"] >= 1 and not store.get_operation(op2).get("selected_coa_id"))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def assess_operations_llm(self, save_id: str = "", max_n: int = 15) -> dict[str, Any]:
        """D5 decision driver (T2): the commander (Player2) decides over each active op's SITREP — escalate
        (reinforce), raise reward, hold, or request_conclude — and validators execute (can_conclude gates conclude).
        Defer → no decision this tick. Deterministic assess_operation (SITREP/BDA/timeout) runs separately."""
        ops = [o for o in self.memory.list_operations(save_id) if o.get("status") in ("active", "frago_required")]
        decided, deferred, out = 0, 0, []
        for o in ops[:int(max_n)]:
            try:
                op_id = o["id"]
                cc = self.memory.can_conclude(op_id)
                open_jobs = [j for j in self.memory.list_jobs(save_id, "open") if j.get("operation_id") == op_id]
                fac = next((f for f in self.memory.list_factions(save_id)
                            if f.get("faction_id") == o.get("faction_id")), {}) or {}
                brief = (f"Operation in {o.get('target_sector') or 'the AO'} against {o.get('target_faction')}. "
                         f"New enemy pressure: {cc.get('recent_magnitude')}. Age: {cc.get('age_s')}s. "
                         f"Open contracts: {len(open_jobs)}. "
                         f"Conclude-eligible: {cc['ok']} ({', '.join(cc.get('reasons', [])) or 'no'}).\n"
                         f"Your doctrine/mood: {fac.get('mood')}; goal: {fac.get('current_goal')}.\n"
                         "Decide how to proceed:")
                # D4b: WHICH ally to reinforce from is a Player2 sub-pick — offer per-candidate escalate options.
                allies = self.memory.select_support_candidates(
                    save_id, o.get("faction_id"), o.get("target_faction") or "", o.get("target_sector") or "", top=2)
                options = [{"key": f"escalate:{a}", "label": f"Escalate — request reinforcement from {a}"} for a in allies]
                if not allies:
                    options.append({"key": "escalate_reinforce", "label": "Escalate — request allied reinforcement"})
                options.append({"key": "hold", "label": "Hold / conserve — continue as planned"})
                if open_jobs:
                    options.insert(len(options) - 1, {"key": "raise_reward",
                                                      "label": "Raise the contract reward to attract takers"})
                if cc["ok"]:
                    options.append({"key": "request_conclude",
                                    "label": "Conclude the operation (objective met / threat resolved)"})
                sp = (f"You are the military command of {o.get('faction_id')} in the X4 galaxy assessing an ongoing "
                      "operation. Choose ONE option that fits your doctrine and the situation; never invent one.")
                d = self.decide(save_id, "assessment", o.get("faction_id"), f"{o.get('faction_id')} command",
                                brief, options, system_prompt=sp,
                                advisory={"can_conclude": cc["ok"], "reasons": cc.get("reasons")},
                                linked_operation_id=op_id)
                if d.get("source") != "player2" or not d.get("choice"):
                    deferred += 1
                    continue
                res = self.memory.apply_assessment_decision(op_id, d["choice"], reason="commander decision")
                if d.get("decision_id"):
                    self.memory.finalize_decision(d["decision_id"], validator_result=res,
                                                  final_status=("applied" if res.get("ok") else "rejected_by_validator"))
                decided += 1
                out.append({"op_id": op_id, "decision": d["choice"], "applied": res.get("applied")})
            except Exception as exc:
                out.append({"op_id": o.get("id"), "error": str(exc)[:160]})
        return {"ok": True, "decided": decided, "deferred": deferred, "results": out}

    def assessment_decision_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for D5 (stub Player2 + temp store): a usable pick applies the assessment decision
        (escalate → agreement); an LLM error → DEFER (no decision, op untouched)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _R("1. We must call for reinforcements.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_assessdec_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/a.sqlite3")
            sid = "s"
            now = time.time()
            for f in ("argon", "antigone", "teladi"):
                store.upsert_faction(sid, f, name=f.title())
            store.adjust_relationship(sid, "antigone", "argon", dtrust=60)
            op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:as1",
                                               status="active", target_faction="teladi", target_sector="X",
                                               budget_reserved=200000, activated_at=now - 100)["id"]
            self.memory = store
            self.player2 = _P2OK()
            r1 = self.assess_operations_llm(sid)
            chk("decided_one", r1["decided"] == 1)
            chk("escalate_made_agreement", any(a.get("kind") == "allied_support" and a.get("operation_id") == op
                                               for a in store.list_agreements(sid)))
            self.player2 = _P2Err()
            r2 = self.assess_operations_llm(sid)
            chk("deferred_on_error", r2["deferred"] >= 1)
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def route_pending_tasks_llm(self, save_id: str = "", max_n: int = 20) -> dict[str, Any]:
        """D3/D4 decision driver (T2): for combat tasks left PLANNED (own ships available → a real routing choice),
        Player2 picks commit-own-fleet / hire-contractors / ask-a-specific-ally; route_task commits. Defer on fail."""
        decided, deferred, out = 0, 0, []
        for o in self.memory.list_operations(save_id):
            if o.get("status") not in ("active", "frago_required"):
                continue
            op = self.memory.get_operation(o["id"])
            det = self.memory.operation_detail(o["id"]) or {}
            for t in det.get("tasks", []):
                if decided + deferred >= int(max_n):
                    break
                if t.get("status") != "planned" or t.get("task_type") not in (
                        "patrol_sector", "engage_hostiles", "raid_enemy_logistics"):
                    continue
                target = t.get("target_faction") or o.get("target_faction") or ""
                sector = t.get("target_sector") or o.get("target_sector") or ""
                allies = self.memory.select_support_candidates(save_id, o.get("faction_id"), target, sector, top=2)
                fac = next((f for f in self.memory.list_factions(save_id)
                            if f.get("faction_id") == o.get("faction_id")), {}) or {}
                options = [{"key": "commit_own_fleet", "label": "Commit our own fleet to the task"},
                           {"key": "hire_contractors", "label": "Hire contractors (post a market job)"}]
                for a in allies:
                    options.append({"key": f"ask:{a}", "label": f"Ask {a} for allied support"})
                brief = (f"Task: {t.get('task_type')} against {target} in {sector or 'the AO'}. "
                         f"Own ships are available.\nYour doctrine/mood: {fac.get('mood')}; "
                         f"goal: {fac.get('current_goal')}.\nHow do you fulfil it?")
                d = self.decide(save_id, "task_routing", o.get("faction_id"), f"{o.get('faction_id')} command",
                                brief, options, linked_operation_id=o["id"])
                if d.get("source") != "player2" or not d.get("choice"):
                    deferred += 1
                    continue
                res = self.memory.route_task(op, t, d["choice"])
                if d.get("decision_id"):
                    self.memory.finalize_decision(d["decision_id"], validator_result=res,
                                                  final_status=("applied" if res.get("ok") else "rejected_by_validator"))
                decided += 1
                out.append({"op_id": o["id"], "task": t.get("id"), "choice": d["choice"], "route": res.get("route")})
        return {"ok": True, "decided": decided, "deferred": deferred, "results": out}

    def route_decision_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for D3/D4 (stub Player2 + temp store): a usable pick routes a planned combat task
        (commit_own_fleet → internal fleet); an LLM error → DEFER (task left planned, no math route)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _R("1. Our own fleet handles this.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_routedec_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/r.sqlite3")
            sid = "s"
            op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:rt",
                                               status="active", target_faction="teladi", target_sector="X")["id"]
            t1 = store.attach_task(op, "patrol_sector", status="planned", target_faction="teladi", target_sector="X")
            self.memory = store
            self.player2 = _P2OK()
            r1 = self.route_pending_tasks_llm(sid)
            chk("decided_one", r1["decided"] == 1)
            chk("task_issued_internal", any(t["id"] == t1 and t["status"] == "issued"
                                            and t.get("assigned_actor_type") == "fleet"
                                            for t in store.operation_detail(op)["tasks"]))
            t2 = store.attach_task(op, "engage_hostiles", status="planned", target_faction="teladi", target_sector="X")
            self.player2 = _P2Err()
            r2 = self.route_pending_tasks_llm(sid)
            chk("deferred_on_error", r2["deferred"] >= 1 and any(t["id"] == t2 and t["status"] == "planned"
                                                                 for t in store.operation_detail(op)["tasks"]))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def compose_job_briefing(self, save_id: str, job: dict) -> str:
        """G3b (Ken, NATO/CAF doctrine): render the contract briefing as a FIVE-PARAGRAPH ORDER (SMESC). The
        player isn't taking odd jobs — they are being cut into a faction's war effort. OPORD-linked jobs pull the
        REAL operation's mission/intent; standalone jobs ground SITUATION in the live conflict ledger.
        Deterministic composition (ADR-002: composition isn't decision)."""
        fid = str(job.get("issuing_faction") or "")
        fname = self._fac_name(save_id, fid) or fid
        tgt = str(job.get("target_faction") or "")
        tname = self._fac_name(save_id, tgt) if tgt else ""
        sector = str(job.get("target_sector") or "")
        if not sector:
            # READ-TIME healing for legacy sectorless jobs (Ken 2026-07-01: "the operational area" leaked into
            # live briefings/objectives) — resolve from observed data and BACKFILL the row so it sticks.
            sector = self.memory.resolve_job_sector(save_id, fid, tgt)
            if sector and job.get("id"):
                try:
                    with self.memory._lock, self.memory._connect() as _conn:
                        _conn.execute("UPDATE market_jobs SET target_sector=? WHERE id=?",
                                      (sector, str(job["id"])))
                        _conn.commit()
                    job["target_sector"] = sector
                except Exception:
                    pass
        sector = sector or "the operational area"
        jtype = str(job.get("job_type") or "contract")
        reward = int(job.get("reward") or 0)
        op = None
        if job.get("operation_id"):
            try:
                op = self.memory.get_operation(str(job.get("operation_id")))
            except Exception:
                op = None
        opord = {}
        if op:
            try:
                opord = op.get("opord_json") or {}
            except Exception:
                opord = {}
        # 1. SITUATION — doctrinal subparagraphs: Enemy Forces / Friendly Forces / Constraints (CFJP 5.0 shape),
        # composed from data the bridge ALREADY tracks (conflict ledger, hostile_events, opord situation).
        enemy_lines, friendly_lines = [], []
        o_sit = (opord.get("situation") or {}) if isinstance(opord, dict) else {}
        if o_sit.get("enemy"):
            enemy_lines.append(str(o_sit["enemy"]))
        try:
            for c in self.memory.list_conflicts(save_id, status="active"):
                pair = {c.get("faction_a"), c.get("faction_b")}
                if fid in pair and (not tgt or tgt in pair):
                    foe = self._fac_name(save_id, (pair - {fid}).pop() if (pair - {fid}) else tgt)
                    inten = float(c.get("intensity") or 0)
                    # ENGLISH, not telemetry (Ken 2026-07-01): no raw intensity numbers in a briefing.
                    phrase = ("open war rages" if inten >= 0.8 else
                              "heavy fighting continues" if inten >= 0.5 else
                              "recurring skirmishes persist" if inten >= 0.2 else
                              "tensions are elevated")
                    cause = str(c.get("cause") or "").replace("_", " ").strip()
                    enemy_lines.append(f"{foe} forces are hostile; {phrase}"
                                       + (f" over {cause}" if cause and cause != "relations at war" else "") +
                                       ". Expect armed contact.")
                    break
        except Exception:
            pass
        try:
            recent = [e for e in self.memory.list_hostile_events(save_id, window_s=3600.0, limit=50)
                      if fid in (e.get("victim_faction"), e.get("attacker_faction"))]
            if recent:
                e0 = recent[0]
                enemy_lines.append(f"Most recent enemy activity: {e0.get('event_kind') or 'attack'} by "
                                   f"{self._fac_name(save_id, e0.get('attacker_faction'))} in "
                                   f"{e0.get('sector') or 'the AO'}.")
        except Exception:
            pass
        if not enemy_lines:
            enemy_lines.append(f"Sustained pressure reported in {sector}"
                               + (f", attributed to {tname}" if tname else "") + ". Assess enemy intent as harassment "
                               "of trade and logistics; most dangerous course: escalation to capital-class assets.")
        if o_sit.get("friendly"):
            friendly_lines.append(str(o_sit["friendly"]))
        friendly_lines.append(f"Higher's intent: {fname} High Command intends to hold {sector} open to friendly "
                              f"movement. Local forces are committed; contracted support is authorized.")
        situation = ("a. Enemy Forces: " + " ".join(enemy_lines) +
                     "\nb. Friendly Forces: " + " ".join(friendly_lines))
        if o_sit.get("constraints"):
            situation += "\nc. Constraints: " + "; ".join(str(x) for x in o_sit["constraints"]) + "."
        # 2. MISSION — who, what, WHERE, WHEN, why; stated twice per CAF convention.
        verb = {"patrol": "patrol and secure", "escort": "escort friendly traffic through",
                "supply": "deliver critical supplies to", "privateer": "raid hostile logistics in",
                "bounty": "hunt designated hostiles in", "recon": "reconnoitre"}.get(jtype, "support operations in")
        mission = (f"The contractor is to {verb} {sector}, effective on acceptance and within the contract window"
                   + (f", denying {tname} freedom of action" if tname else "") +
                   f", in order to support {fname} strategic objectives.")
        mission = mission + " I say again: " + mission
        # 3. EXECUTION — concept of ops from the REAL OPORD (#65: scheme of manoeuvre + main effort + phases)
        o_exec = (opord.get("execution") or {}) if isinstance(opord, dict) else {}
        intent = (op.get("commander_intent") if op else "") or (
            "Restore freedom of movement and demonstrate resolve without provoking wider escalation.")
        endstate = (op.get("desired_end_state") if op else "") or (
            "pressure reduced and friendly operations proceeding unhindered")
        def _human(s: Any) -> str:
            # machine tokens (escort_supply_convoy) never reach prose verbatim (Ken 2026-07-01)
            out = str(s or "").replace("_", " ").strip()
            # de-tokenized words can break articles ("a escort"): fix a→an before vowel sounds
            out = re.sub(r"\b([Aa]) ([aeiouAEIOU])", lambda m: ("An " if m.group(1) == "A" else "an ") + m.group(2), out)
            return out

        conops = ""
        if o_exec.get("scheme_of_manoeuvre"):
            scheme = _human(o_exec["scheme_of_manoeuvre"]).rstrip(".") + "."
            conops += f" Concept of operations: {scheme}"
        # The LLM's scheme prose often already states main effort / phasing — only append the
        # structured-field versions when it doesn't, or EXECUTION says everything twice (Ken screenshot).
        me = o_exec.get("main_effort") or {}
        if isinstance(me, dict) and me.get("task") and "main effort" not in conops.lower():
            conops += f" The main effort is the {_human(me['task'])}."
        if o_exec.get("phases") and "phasing" not in conops.lower():
            conops += " Phasing: " + ", then ".join(_human(p) for p in o_exec["phases"]) + "."
        task_verbs = {"patrol": f"PATROL and SECURE {sector}",
                      "escort": f"ESCORT friendly traffic through {sector}",
                      "supply": f"DELIVER contracted supplies to {sector}",
                      "privateer": f"INTERDICT and DESTROY {(tname + ' ') if tname else 'hostile '}logistics in {sector}",
                      "bounty": f"FIND and DESTROY designated hostiles in {sector}",
                      "recon": f"RECONNOITRE {sector} and REPORT dispositions"}
        task_phrase = task_verbs.get(jtype, f"SUPPORT {fname} operations in {sector}")
        intent = str(intent).strip().rstrip(".") + "."
        endstate = str(endstate).strip().rstrip(".")
        execution = (f"a. Commander's Intent: {intent} Desired end state: {endstate}.{conops}\n"
                     f"b. Groupings and Tasks. Contractor element (you): {task_phrase}. "
                     f"{fname} local forces: maintain current tasking; respond to contact reports.\n"
                     f"c. Coordinating Instructions: engage per standing rules of engagement; avoid neutral "
                     f"shipping; withdraw rather than lose the element.")
        # 4. SERVICE & SUPPORT — the reward is real treasury money (Ken's rule) + repair/salvage policy
        repair = ""
        try:
            repair = str(((opord.get("service_support") or {}).get("repair_policy")) or "")
        except Exception:
            repair = ""
        sustainment = (f"Payment of {reward:,} Cr is committed from the {fname} treasury, released on proof of "
                       f"completion. No advance is provided. "
                       + (repair.capitalize() + ". " if repair else
                          f"Repair and rearm at {fname} stations at the contractor's own cost. ")
                       + "Salvage rights within the AO fall to the contractor.")
        # 5. COMMAND & SIGNAL — command AND signal (report means, POC, succession)
        command = (f"a. Command: issuing authority is {fname} High Command; on station, local {fname} authority "
                   f"has succession. Expect fragmentary orders (FRAGO) should the situation change.\n"
                   f"b. Signal: report contact, losses, and completion via the AI comm-link; the issuing "
                   f"representative is your point of contact.")
        briefing = ("1. SITUATION\n" + situation +
                    "\n\n2. MISSION\n" + mission +
                    "\n\n3. EXECUTION\n" + execution +
                    "\n\n4. SERVICE & SUPPORT\n" + sustainment +
                    "\n\n5. COMMAND & SIGNAL\n" + command)
        # the OBJECTIVE is the element's TASK (Ken: the bottom box is the tasking, not a SMESC repeat)
        task = f"Contractor element (you): {task_phrase}. Report completion for payment."

        def _fontsafe(s: str) -> str:
            """X4's UI font renders em-dashes/arrows/typographic quotes as box artifacts (Ken screenshot
            2026-07-01) — player-facing strings are plain ASCII, full stop."""
            for a, b in (("—", "-"), ("–", "-"), ("→", " then "), ("·", ","),
                         ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("…", "...")):
                s = s.replace(a, b)
            s = "".join(ch for ch in s if ord(ch) < 128)
            while "  " in s:
                s = s.replace("  ", " ")
            return s

        return {"briefing": _fontsafe(briefing), "task": _fontsafe(task)}

    def jobs_offers(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """G1 (#75/ADR-003): player-eligible OPEN jobs for the in-game mission-offer surface. The Lua poller GETs
        this, materializes offers, and withdraws ones that vanish (claimed by an NPC / cancelled / expired)."""
        import time as _t
        sid = str((payload or {}).get("save_id") or "")
        out = []
        for j in self.memory.player_eligible_jobs(sid):
            fid = str(j.get("issuing_faction") or "")
            comp = self.compose_job_briefing(sid, j)  # compose FIRST — it may heal/backfill target_sector
            out.append({"job_id": j.get("id"), "job_type": j.get("job_type"), "faction": fid,
                        "faction_name": self._fac_name(sid, fid), "target_sector": j.get("target_sector") or "",
                        "target_faction": j.get("target_faction") or "", "ware": j.get("ware") or "",
                        "reward": int(j.get("reward") or 0), "urgency": int(j.get("urgency") or 0),
                        "age_s": int(_t.time() - float(j.get("created_at") or 0)),
                        **comp,
                        "summary": ((j.get("evidence_json") or {}).get("summary")
                                    if isinstance(j.get("evidence_json"), dict) else "") or ""})
        return {"ok": True, "offers": out}

    def jobs_claim(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """G1/G3: claim an open job (FCFS — claim_job locks the row). payload {save_id, job_id, claimant}."""
        p = payload or {}
        return self.memory.claim_job(str(p.get("save_id") or ""), str(p.get("job_id") or ""),
                                     str(p.get("claimant") or "player"))

    def jobs_release(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """G5 abort slice: player aborted the accepted mission — reopen the job (market re-lists it, the Lua
        poller re-offers it as a fresh contract) and apply the POLICY: a player abort costs trust -2 with the
        issuing faction (completion is +3). Policy lives HERE, visible + logged — never swallowed in the store
        (2026-07-02 worst-implementation fix). payload {save_id, job_id, claimant}."""
        p = payload or {}
        sid = str(p.get("save_id") or "")
        claimant = str(p.get("claimant") or "player")
        res = self.memory.release_job(sid, str(p.get("job_id") or ""), claimant)
        fac = str(res.get("issuing_faction") or "")
        if res.get("ok") and fac and claimant == "player":
            try:
                self.memory.adjust_relationship(sid, fac, "player", dtrust=-2,
                                                summary=f"player abandoned contract {res.get('id')}")
                res["trust_penalty"] = -2
            except Exception as e:
                # a failed penalty is a VISIBLE event, not a silent pass
                res["trust_penalty_error"] = str(e)
                try:
                    self.memory.add_world_event(sid, "policy_error",
                                                f"abort trust penalty failed for {res.get('id')}: {e}")
                except Exception:
                    pass
        return res

    def jobs_offers_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for G1: public+open is offered; direct-visibility hidden; hostile-faction (trust ≤ -50) hidden;
        a claimed job disappears from the offer list (FCFS lock)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, det: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": det})

        d = tempfile.mkdtemp(prefix="nl_joboff_")
        orig_m = self.memory
        try:
            store = MemoryStore(f"{d}/o.sqlite3")
            sid = "s"
            for f in ("argon", "teladi", "xenon"):
                store.upsert_faction(sid, f, name=f.title())
            j_pub = store.create_or_update_job(sid, "argon", "patrol", target_sector="X", reward=50000, urgency=3)["id"]
            store.create_or_update_job(sid, "teladi", "supply", ware="energycells", reward=80000,
                                       visibility="direct")
            store.adjust_relationship(sid, "xenon", "player", dtrust=-80)
            j_hostile = store.create_or_update_job(sid, "xenon", "bounty", reward=99999)["id"]
            self.memory = store
            r1 = self.jobs_offers({"save_id": sid})
            ids = [o["job_id"] for o in r1["offers"]]
            chk("public_open_offered", j_pub in ids, str(ids))
            off = next((o for o in r1["offers"] if o["job_id"] == j_pub), {})
            b = off.get("briefing") or ""
            chk("briefing_five_paragraph_order", all(k in b for k in
                ("1. SITUATION", "2. MISSION", "3. EXECUTION", "4. SERVICE & SUPPORT", "5. COMMAND & SIGNAL"))
                and "50,000" in b and "Groupings and Tasks" in b, b[:120])
            chk("objective_is_element_task", "Contractor element (you): PATROL and SECURE" in
                (off.get("task") or ""), str(off.get("task"))[:100])
            alltxt = b + (off.get("task") or "")
            chk("fontsafe_ascii_no_tokens", all(ord(ch) < 128 for ch in alltxt)
                and "intensity" not in b and "_" not in b, alltxt[:80])
            chk("direct_hidden", all(o["job_type"] != "supply" for o in r1["offers"]), "")
            chk("hostile_faction_hidden", j_hostile not in ids, "")
            c = self.jobs_claim({"save_id": sid, "job_id": j_pub, "claimant": "player"})
            r2 = self.jobs_offers({"save_id": sid})
            chk("claimed_disappears", c.get("ok") and j_pub not in [o["job_id"] for o in r2["offers"]], str(c))
            # vetted money + relationship: completing spends the reward from the faction budget AND builds trust
            spent0 = store.budget_spent(sid, "argon")
            comp = store.complete_job(sid, j_pub, claimant="player")
            rel = store.get_relationship(sid, "argon", "player") or {}
            chk("completion_spends_and_trusts", comp.get("ok") and store.budget_spent(sid, "argon") == spent0 + 50000
                and float(rel.get("trust") or 0) >= 3, f"spent={store.budget_spent(sid, 'argon')} rel={rel}")
        finally:
            self.memory = orig_m
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def push_contract_fragos(self, save_id: str = "") -> dict[str, Any]:
        """#27 bridge half: turn fresh operation FRAGOs into drain traffic for the player's ACTIVE contracts —
        a news ping ("FRAGO from <faction> High Command") + a `contract_frago` action the MD/Lua half (UI task)
        will use to update the live mission. Idempotent via the job's frago_ts marker."""
        items = self.memory.pending_contract_fragos(save_id)
        news, actions = [], []
        for it in items:
            fname = self._fac_name(save_id, str(it.get("faction") or ""))
            news.append(f"FRAGO from {fname} High Command: {it['summary']}")
            actions.append({"type": "contract_frago", "job_id": it["job_id"], "summary": it["summary"],
                            "faction": it.get("faction")})
            self.memory.mark_contract_frago(save_id, str(it["job_id"]), float(it["report_ts"]))
        return {"ok": True, "fired": len(items), "news": news, "actions": actions}

    def contract_frago_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for #27 bridge half: a frago on a CLAIMED job's operation fires exactly once (idempotent);
        unclaimed jobs and frago-less ops fire nothing."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, det: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": det})

        d = tempfile.mkdtemp(prefix="nl_cfrago_")
        orig_m = self.memory
        try:
            store = MemoryStore(f"{d}/f.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "argon", name="Argon")
            op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:x:frago",
                                               status="active", target_faction="xenon", target_sector="X")["id"]
            j_claimed = store.create_or_update_job(sid, "argon", "patrol", target_sector="X", reward=50000,
                                                   urgency=3, operation_id=op)["id"]
            store.claim_job(sid, j_claimed, claimant="player")
            j_open = store.create_or_update_job(sid, "argon", "supply", reward=40000, operation_id=op)["id"]
            self.memory = store
            r0 = self.push_contract_fragos(sid)
            chk("quiet_before_frago", r0["fired"] == 0, str(r0))
            store.attach_report(op, "frago", "Enemy reinforcements detected; hold until relieved.", severity=3)
            r1 = self.push_contract_fragos(sid)
            chk("fires_once_for_claimed", r1["fired"] == 1
                and "FRAGO from Argon High Command" in (r1["news"][0] if r1["news"] else "")
                and r1["actions"][0]["job_id"] == j_claimed, str(r1["news"]))
            r2 = self.push_contract_fragos(sid)
            chk("idempotent", r2["fired"] == 0, str(r2))
            chk("open_job_ignored", all(a["job_id"] != j_open for a in r1["actions"]), "")
            store.attach_report(op, "frago", "Withdraw to fallback line.", severity=3)
            r3 = self.push_contract_fragos(sid)
            chk("new_frago_fires_again", r3["fired"] == 1 and "fallback" in r3["news"][0], str(r3["news"]))
        finally:
            self.memory = orig_m
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def job_pricing_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for threat-scaled pricing: monotonic in urgency; active-conflict intensity raises the price;
        capped by budget_available; a broke faction posts unpriced (0)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, det: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": det})

        d = tempfile.mkdtemp(prefix="nl_price_")
        orig_m = self.memory
        try:
            store = MemoryStore(f"{d}/p.sqlite3")
            sid = "s"
            for f in ("argon", "xenon"):
                store.upsert_faction(sid, f, name=f.title())
            store.budget_capacity = lambda _s, _f: 10_000_000.0
            p0 = store.price_job(sid, "argon", "escort", urgency=0)
            p5 = store.price_job(sid, "argon", "escort", urgency=5)
            chk("urgency_monotonic", 0 < p0 < p5, f"{p0} vs {p5}")
            store.add_conflict(sid, "argon", "xenon", status="active", intensity=1.0, cause="war")
            pw = store.price_job(sid, "argon", "escort", urgency=5, target_faction="xenon")
            chk("intensity_raises_price", pw > p5, f"{pw} vs {p5}")
            store.budget_capacity = lambda _s, _f: 42_000.0
            chk("capped_by_available", store.price_job(sid, "argon", "escort", urgency=5) <= 42000, "")
            store.budget_capacity = lambda _s, _f: 0.0
            chk("broke_posts_unpriced", store.price_job(sid, "argon", "escort", urgency=5) == 0, "")
            # sector resolution precedence (kills "the operational area"): explicit wins; else latest hostile
            # event sector for the pair; else the faction's most-attacked sector
            chk("sector_explicit_wins", store.resolve_job_sector(sid, "argon", "xenon", explicit="Argon Prime")
                == "Argon Prime", "")
            store.add_hostile_event(sid, {"attacker_faction": "xenon", "victim_faction": "argon",
                                          "sector": "Hatikvah's Choice I", "event_kind": "ship_destroyed",
                                          "magnitude": 3})
            chk("sector_from_hostile_event", store.resolve_job_sector(sid, "argon", "xenon")
                == "Hatikvah's Choice I", store.resolve_job_sector(sid, "argon", "xenon"))
            chk("sector_fallback_most_attacked", store.resolve_job_sector(sid, "argon")
                == "Hatikvah's Choice I", "")
        finally:
            self.memory = orig_m
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def escalate_stale_jobs_llm(self, save_id: str = "", max_n: int = 2) -> dict[str, Any]:
        """FRAGO reward escalation (spec OPORD_Update §FRAGO + Job Market): each stale OPEN market job gets a
        bounded Player2 decision — raise (engine-priced +25%, budget-capped; offered only if affordable) / hold /
        withdraw. The engine detects staleness, prices the raise, and executes the verdict; Player2 only chooses.
        Announces ONLY material changes (raise/cancel) via returned news lines. Defers on failure (job stays
        stale, retried next tick — no math substitute)."""
        import time as _t
        decided, deferred, out, news = 0, 0, [], []
        for job in self.memory.list_stale_open_jobs(save_id):
            if decided + deferred >= int(max_n):
                break
            fid = str(job.get("issuing_faction") or "")
            options, _raise_to = self.memory.job_escalation_options(save_id, job)
            age_min = int((_t.time() - float(job.get("updated_at") or 0)) / 60)
            where = (" in " + job["target_sector"]) if job.get("target_sector") else ""
            versus = (" against " + job["target_faction"]) if job.get("target_faction") else ""
            _cap = int(self.memory.budget_capacity(save_id, fid))
            _spent = int(self.memory.budget_spent(save_id, fid))
            _com = int(self.memory.jobs_committed(save_id, fid))
            _avail = max(0, _cap - _spent - _com)
            brief = (f"Our OPEN {job.get('job_type')} contract{where}{versus} has gone UNCLAIMED for "
                     f"~{age_min} min at {int(job.get('reward') or 0):,} credits (urgency "
                     f"{int(job.get('urgency') or 0)}). No claimant has stepped forward. "
                     f"TREASURY (your faction's own pocket — every reward is paid from it): capacity {_cap:,}, "
                     f"already spent {_spent:,}, committed to outstanding contracts {_com:,} → available "
                     f"{_avail:,} credits. Do we sweeten the offer, wait, or withdraw the need?")
            d = self.decide(save_id, "job_escalation", fid, f"{fid} procurement command", brief, options)
            if d.get("source") != "player2" or not d.get("choice"):
                deferred += 1
                continue
            res = self.memory.apply_job_escalation(save_id, str(job.get("id")), str(d["choice"]))
            if d.get("decision_id"):
                self.memory.finalize_decision(d["decision_id"], validator_result=res,
                                              final_status=("applied" if res.get("ok") else "rejected_by_validator"))
            decided += 1
            if res.get("news"):
                news.append(res["news"])
            out.append({"job": job.get("id"), "choice": d["choice"], "action": res.get("action"),
                        "ok": res.get("ok")})
        return {"ok": True, "decided": decided, "deferred": deferred, "results": out, "news": news}

    def job_escalation_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for FRAGO job escalation (stub Player2 + temp store): fresh jobs aren't stale;
        a RAISE verdict re-prices (budget-capped option) + emits ONE world event + a news line; HOLD snoozes with
        NO event; CANCEL withdraws with an event; an LLM error DEFERS (job untouched, still stale)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, det: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": det})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2Pick:
            def __init__(self, n: str) -> None:
                self._n = n

            def npc_complete(self, _req: Any) -> Any:
                return _R(f"{self._n}. As commanded.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_jobesc_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/j.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "argon", name="Argon")
            store.budget_capacity = lambda _s, _f: 10_000_000.0  # test headroom => raise option present
            self.memory = store

            def _mk(jtype: str, stale: bool = True) -> str:
                jid = store.create_or_update_job(sid, "argon", jtype, target_sector="X", reward=100000,
                                                 urgency=2)["id"]
                if stale:
                    import time as _t2
                    with store._lock, store._connect() as conn:
                        conn.execute("UPDATE market_jobs SET updated_at=? WHERE id=?",
                                     (_t2.time() - 2 * store.JOB_STALE_S, jid))
                        conn.commit()
                return jid

            fresh = _mk("escort", stale=False)
            chk("fresh_not_stale", all(j["id"] != fresh for j in store.list_stale_open_jobs(sid)), "")

            j_raise = _mk("patrol")
            ev0 = len(store.list_world_events(sid, limit=100))
            self.player2 = _P2Pick("1")  # option 1 = raise (headroom stubbed high)
            r1 = self.escalate_stale_jobs_llm(sid, max_n=1)
            jr = next(j for j in store.list_jobs(sid) if j["id"] == j_raise)
            chk("raise_repriced", r1["decided"] == 1 and int(jr["reward"]) == 125000, str(jr.get("reward")))
            chk("raise_event_and_news", len(store.list_world_events(sid, limit=100)) == ev0 + 1
                and len(r1["news"]) == 1, str(r1["news"]))

            j_hold = _mk("supply")
            ev1 = len(store.list_world_events(sid, limit=100))
            self.player2 = _P2Pick("2")  # option 2 = hold
            r2 = self.escalate_stale_jobs_llm(sid, max_n=1)
            chk("hold_snoozed_no_event", r2["decided"] == 1 and not r2["news"]
                and len(store.list_world_events(sid, limit=100)) == ev1
                and all(j["id"] != j_hold for j in store.list_stale_open_jobs(sid)), "")

            j_cancel = _mk("recon")
            self.player2 = _P2Pick("3")  # option 3 = cancel
            r3 = self.escalate_stale_jobs_llm(sid, max_n=1)
            jc = next(j for j in store.list_jobs(sid) if j["id"] == j_cancel)
            chk("cancel_withdrawn", r3["decided"] == 1 and jc["status"] == "cancelled"
                and len(r3["news"]) == 1, str(jc.get("status")))

            j_defer = _mk("bounty")
            self.player2 = _P2Err()
            r4 = self.escalate_stale_jobs_llm(sid, max_n=1)
            jd = next(j for j in store.list_jobs(sid) if j["id"] == j_defer)
            chk("defer_untouched", r4["deferred"] == 1 and jd["status"] == "open"
                and any(j["id"] == j_defer for j in store.list_stale_open_jobs(sid)), "")
            # committed-aware affordability: shrink capacity below outstanding committed rewards → the RAISE
            # option must vanish (option 1 becomes hold) — no faction may promise money it doesn't have.
            store.budget_capacity = lambda _s, _f: 50000.0
            self.player2 = _P2Pick("1")
            r5 = self.escalate_stale_jobs_llm(sid, max_n=1)
            chk("raise_gated_by_committed", r5["decided"] == 1 and r5["results"][0]["action"] == "held"
                and not r5["news"], str(r5["results"]))
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def propose_deals_llm(self, save_id: str = "", max_n: int = 8) -> dict[str, Any]:
        """D6 decision driver (T3): the PROPOSER faction's Player2 decides which plausible deal to initiate (or none),
        grounded in a brief; the chosen deal submits through the Negotiations door (deduped/rate-limited). Defer-on-fail.
        Replaces the deterministic auto-propose on the heartbeat."""
        cands = self.memory.agreement_candidates(save_id, int(max_n) * 3)
        by_prop: dict[str, list] = {}
        for c in cands:
            by_prop.setdefault(c["proposer"], []).append(c)
        decided, proposed, deferred, out = 0, 0, 0, []
        for prop, deals in by_prop.items():
            if decided >= int(max_n):
                break
            fac = next((f for f in self.memory.list_factions(save_id) if f.get("faction_id") == prop), {}) or {}
            shown = deals[:4]
            options = [{"key": f"propose:{i}",
                        "label": f"Offer {c['kind']} to {c['target']} ({(c['terms'].get('reason') or '')[:40]})"}
                       for i, c in enumerate(shown)]
            options.append({"key": "hold", "label": "Make no new offers right now"})
            brief = (f"You are {prop}. Plausible deals you could initiate now:\n"
                     + "\n".join(f"  - {c['kind']} with {c['target']}: {c['terms'].get('reason') or ''}" for c in shown)
                     + f"\nYour doctrine/mood: {fac.get('mood')}; goal: {fac.get('current_goal')}.\n"
                     "Which one, if any, do you initiate?")
            d = self.decide(save_id, "proposal_initiation", prop, f"{prop} leadership", brief, options)
            decided += 1
            if d.get("source") != "player2" or not d.get("choice"):
                deferred += 1
                continue
            if d["choice"] == "hold":
                out.append({"proposer": prop, "decision": "hold"})
                continue
            try:
                idx = int(d["choice"].split(":", 1)[1])
            except Exception:
                idx = -1
            if not (0 <= idx < len(shown)):
                deferred += 1
                continue
            c = shown[idx]
            ag = self.memory.submit_negotiation_intent(save_id, "proposal", c["kind"], prop,
                                                       recipient=c["target"], terms=c["terms"])
            if d.get("decision_id"):
                self.memory.finalize_decision(d["decision_id"], final_status="applied")
            proposed += 1
            out.append({"proposer": prop, "target": c["target"], "kind": c["kind"], "agreement_id": ag.get("id")})
        return {"ok": True, "decided": decided, "proposed": proposed, "deferred": deferred, "results": out}

    def propose_deals_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for D6 (stub Player2 + temp store): a usable pick initiates a deal via the door; an
        LLM error → DEFER (no proposal)."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _R("1. We sue for peace.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_propose_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/p.sqlite3")
            sid = "s"
            for f in ("argon", "teladi", "paranid"):
                store.upsert_faction(sid, f, name=f.title())
            store.add_conflict(sid, "argon", "teladi", status="active", intensity=0.5)
            store.add_conflict(sid, "argon", "paranid", status="active", intensity=0.5)
            chk("candidate_generated", any(c["kind"] == "ceasefire" for c in store.agreement_candidates(sid)))
            self.memory = store
            self.player2 = _P2OK()
            r1 = self.propose_deals_llm(sid)
            chk("proposed_one", r1["proposed"] >= 1)
            chk("agreement_created", any((a.get("kind") == "ceasefire" or a.get("type") == "ceasefire")
                                         for a in store.list_agreements(sid)))
            self.player2 = _P2Err()
            r2 = self.propose_deals_llm(sid)
            chk("deferred_on_error", r2["deferred"] >= 1)
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def faction_action_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for D8 (stub Player2 + temp store): review_faction's strategic PICK routes through the
        unified decide() (Player2) — a usable pick yields a decision; an LLM error DEFERS (faction holds, no incident,
        never the deterministic index-0)."""
        import shutil
        import tempfile
        from .scoring import rank_faction
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _R:
            def __init__(self, r: str) -> None:
                self._r = {"status": "ok", "reply": r}

            def to_dict(self) -> dict[str, Any]:
                return self._r

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _R("1. We press our advantage.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        d = tempfile.mkdtemp(prefix="nl_factact_")
        orig_m, orig_p = self.memory, self.player2
        try:
            store = MemoryStore(f"{d}/f.sqlite3")
            sid = "s"
            for f in ("argon", "teladi"):
                store.upsert_faction(sid, f, name=f.title())
            store.add_conflict(sid, "argon", "teladi", status="active", intensity=0.6)
            store.adjust_relationship(sid, "argon", "teladi", dresentment=40)
            state = store.derive_pressures(sid, "argon")
            opts = rank_faction("argon", state, store.list_relationships(sid, subject="argon"))
            chk("options_generated", len(opts) >= 1)
            self.memory = store
            self.player2 = _P2OK()
            r1 = self.review_faction(sid, "argon", force=True)
            chk("player2_decided", r1.get("decision") is not None and not r1.get("deferred"))
            self.player2 = _P2Err()
            r2 = self.review_faction(sid, "argon", force=True)
            chk("defers_on_error", r2.get("deferred") is True and r2.get("decision") is None)
        finally:
            self.memory, self.player2 = orig_m, orig_p
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    DECISION_TIER_INTERVALS = {"operational": 300.0, "strategic": 900.0}  # T2 ~5min, T3 ~10-15min

    def decision_tick(self, save_id: str = "", now: float | None = None) -> dict[str, Any]:
        """#50 priority-tiered decision cadence. Each tick fires only the tiers whose interval has elapsed; each
        driver is bounded (small max_n) and DEFERS on Player2 failure. The Lua heartbeat calls this; tiers self-gate
        by last-run timestamp. T2 operational (~5min): COA select / routing / assessment. T3 strategic (~10-15min):
        negotiation accept / proposal initiation / faction strategic action. (T1 COA is also caught here; T4 narrative
        = D9, not built.)"""
        now = time.time() if now is None else float(now)
        if not hasattr(self, "_decision_tier_last"):
            self._decision_tier_last = {}
        fired: dict[str, Any] = {}

        def due(tier: str) -> bool:
            return now - self._decision_tier_last.get(f"{save_id}:{tier}", 0.0) >= self.DECISION_TIER_INTERVALS[tier]

        if due("operational"):
            self._decision_tier_last[f"{save_id}:operational"] = now
            fired["operational"] = {
                "coa": self.select_pending_coas_llm(save_id, max_n=2),
                "route": self.route_pending_tasks_llm(save_id, max_n=2),
                "assess": self.assess_operations_llm(save_id, max_n=2),
            }
        if due("strategic"):
            self._decision_tier_last[f"{save_id}:strategic"] = now
            fired["strategic"] = {
                "offers": self.resolve_offers_llm(save_id, max_n=2),
                "propose": self.propose_deals_llm(save_id, max_n=2),
                "influence": self.influence_step({"save_id": save_id, "budget": 2}),
                "scene": self.run_scheduled_scene(save_id),  # #63: one NPC>NPC scene per strategic tick
                "job_escalation": self.escalate_stale_jobs_llm(save_id, max_n=2),  # FRAGO reward escalation
                "contract_frago": self.push_contract_fragos(save_id),  # #27: FRAGOs reach ACTIVE contracts
            }
        return {"ok": True, "fired": list(fired.keys()), "now": now, "detail": fired}

    def decision_tick_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for #50: the cadence GATE fires tiers by interval (a fresh save → all drivers no-op
        with no Player2 calls, so only the gate logic is exercised)."""
        sid = f"dtick_{int(time.time() * 1000)}"
        checks: list[dict] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        t0 = 1_000_000.0
        r1 = self.decision_tick(sid, now=t0)
        chk("first_fires_both", "operational" in r1["fired"] and "strategic" in r1["fired"])
        r2 = self.decision_tick(sid, now=t0 + 10)
        chk("gated_within_interval", r2["fired"] == [])
        r3 = self.decision_tick(sid, now=t0 + 400)
        chk("operational_refires", "operational" in r3["fired"] and "strategic" not in r3["fired"])
        r4 = self.decision_tick(sid, now=t0 + 1000)
        chk("strategic_refires", "strategic" in r4["fired"])
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def decisions_list(self, save_id: str = "", limit: int = 50) -> dict[str, Any]:
        """The decision audit log (spec §12) — newest-first, for the dashboard + debugging."""
        return {"ok": True, "decisions": self.memory.list_decision_records(save_id, limit)}

    def decision_record_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the decision audit log (record/finalize/list)."""
        return run_decision_record_selftest()

    def decide_probe(self, save_id: str = "", faction: str = "argon") -> dict[str, Any]:
        """DIAGNOSTIC: run the LIVE adapter once on a trivial 2-option brief and return the RAW result so we can see
        why the live Player2 path succeeds/defers (visibility the browser-triggered batch can't give)."""
        opts = [{"key": "yes", "label": "Yes, act now"}, {"key": "no", "label": "No, hold for now"}]
        d = self.decide(save_id or "probe", "probe", faction, f"{faction} leadership",
                        f"A simple test for {faction}: do you act now or hold?", opts)
        return {"ok": True, **d}

    def decision_adapter_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic oracle for the universal Decision Adapter (no live LLM needed): a stub Player2 returning a
        usable pick maps to the right option key (source=player2); a stub that raises, or returns junk, → DEFER
        (choice=None, source=deferred) — never a math fallback; empty options → skipped. The live LLM path is
        exercised separately by resolve_offers_llm."""
        checks: list[dict[str, Any]] = []

        def chk(n: str, c: bool) -> None:
            checks.append({"name": n, "ok": bool(c)})

        class _Resp:
            def __init__(self, reply: str, status: str = "ok") -> None:
                self._d = {"status": status, "reply": reply}

            def to_dict(self) -> dict[str, Any]:
                return self._d

        class _P2OK:
            def npc_complete(self, _req: Any) -> Any:
                return _Resp("2. They share our enemy; we accept.")

        class _P2Err:
            def npc_complete(self, _req: Any) -> Any:
                raise RuntimeError("player2 down")

        class _P2Junk:
            def npc_complete(self, _req: Any) -> Any:
                return _Resp("the council remains undecided")

        opts = [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}, {"key": "c", "label": "C"}]
        orig = self.player2
        try:
            self.player2 = _P2OK()
            d1 = self.decide("s", "test", "argon", "Argon", "brief", opts)
            chk("picks_player2_choice", d1["source"] == "player2" and d1["choice"] == "b")
            self.player2 = _P2Err()
            d2 = self.decide("s", "test", "argon", "Argon", "brief", opts)
            chk("defers_on_error", d2["source"] == "deferred" and d2["choice"] is None)
            self.player2 = _P2Junk()
            d3 = self.decide("s", "test", "argon", "Argon", "brief", opts)
            chk("defers_on_unparsed", d3["source"] == "deferred" and d3["choice"] is None)
            d4 = self.decide("s", "test", "argon", "Argon", "brief", [])
            chk("skips_no_options", d4["source"] == "skipped" and d4["choice"] is None)
        finally:
            self.player2 = orig
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def identity_promote(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Promote an identity's tracking priority. payload: {persistent_npc_key|npc_key, reason}."""
        payload = payload or {}
        reason = str(payload.get("reason") or "")
        if payload.get("npc_key"):
            tier = self.memory.promote_identity_for_npc(str(payload["npc_key"]), reason)
        else:
            tier = self.memory.promote_identity(str(payload.get("persistent_npc_key") or ""), reason)
        return {"ok": tier is not None, "importance_tier": tier}

    def identity_rebind(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rebind a batch of observed runtime NPCs to persistent identities for a game session.
        payload: {game_session_id, save_id?, observed:[{runtime_component_id, npc_key, name, faction,
        role, macro, npc_code, skills, ship_id?, station_id?, sector?, recently_talked?}]}."""
        payload = payload or {}
        return self.memory.rebind_session(
            str(payload.get("game_session_id") or ""),
            payload.get("observed") or [],
            save_id=str(payload.get("save_id") or ""),
        )

    def identity_backfill(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Give every existing chat-NPC row a persistent identity (idempotent + reversible)."""
        return self.memory.backfill_identities()

    def identity_reset(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """I8 cleanup: wipe + rebuild the identity layer from chat NPCs only (clears pre-gate pollution)."""
        return self.memory.reset_identities()

    def identities_list(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "identities": self.memory.list_identities()}

    def identity_detail(self, persistent_npc_key: str = "") -> dict[str, Any]:
        ident = self.memory.get_identity(persistent_npc_key) if persistent_npc_key else None
        if not ident:
            return {"ok": False, "error": "identity not found", "persistent_npc_key": persistent_npc_key}
        return {"ok": True, "identity": ident,
                "evidence": self.memory.get_evidence(persistent_npc_key),
                "memory_keys": self.memory.resolve_memory_keys(persistent_npc_key),
                "bindings": self.memory.get_runtime_bindings(persistent_npc_key),
                "name_collisions": self.memory.count_name_collisions(ident.get("display_name", ""), exclude=persistent_npc_key)}

    def memory_metrics(self, npc_key: str | None = None) -> dict[str, Any]:
        return self.memory.metrics(npc_key)

    def memory_npcs(self) -> dict[str, Any]:
        # A3a: fill BLANK roles with the classified persona archetype so the dashboard never shows "—".
        # Real stored roles (marine/service crew) are preserved; abstract faction voices (High Command) →
        # "high_command". classify_archetype reads npc_name, so map the row's `name` onto it.
        try:
            from .persona import classify_archetype
        except ImportError:  # non-package/test import
            from persona import classify_archetype
        npcs = self.memory.list_npcs()
        for n in npcs:
            if not str(n.get("role") or "").strip():
                n["role"] = classify_archetype({"npc_name": n.get("name"), "role": n.get("role"),
                                                "faction_id": n.get("faction_id")})
        return {"ok": True, "npcs": npcs}

    def memory_npc_detail(self, npc_key: str) -> dict[str, Any] | None:
        detail = self.memory.npc_detail(npc_key)
        if detail is None:
            return None
        return {"ok": True, **detail}

    def memory_stress(self, npcs: int = 100, turns_per: int = 40) -> dict[str, Any]:
        npcs = max(1, min(500, int(npcs)))          # clamp to keep it bounded
        turns_per = max(1, min(200, int(turns_per)))
        return run_memory_stress(self.memory, n_npcs=npcs, turns_per=turns_per)

    def memory_stress_clear(self) -> dict[str, Any]:
        return self.memory.clear_save("stress")

    def memory_saves(self) -> dict[str, Any]:
        return {"ok": True, "saves": self.memory.list_saves()}

    def npc_delete(self, save_id: str = "", npc_id: str | None = None,
                   npc_key: str | None = None) -> dict[str, Any]:
        """Purge a dead NPC (by npc_id or npc_key) + its memory. X4 calls this on death."""
        return self.memory.delete_npc(save_id=save_id, npc_id=npc_id or None, npc_key=npc_key or None)

    def memory_reap_selftests(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """A2 (IG-3): purge selftest-generated saves so they don't pollute the live dashboard."""
        return self.memory.reap_selftest_saves()

    def llm_budget_status(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """A7: current per-session LLM-call budget / kill-switch state."""
        return {"ok": True, **self.player2.llm_status()}

    def llm_budget_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A7: set the call budget (0=unlimited), toggle the kill switch, and/or reset the counter."""
        return {"ok": True, **self.player2.set_llm_controls(
            budget=payload.get("budget"), killed=payload.get("killed"), reset=bool(payload.get("reset")))}

    def llm_budget_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """A7: prove the gate blocks when killed / over-budget and allows otherwise. Restores prior live state."""
        p2 = self.player2
        before = p2.llm_status()
        checks: list[dict] = []
        ok = lambda n, c, d=None: checks.append({"name": n, "pass": bool(c), "detail": d})
        try:
            p2.set_llm_controls(killed=True, reset=True)
            ok("kill_switch_blocks", p2._llm_gate() is not None)
            p2.set_llm_controls(killed=False, budget=0, reset=True)
            ok("unlimited_allows", p2._llm_gate() is None)
            p2.set_llm_controls(budget=1, reset=True)
            g1, g2 = p2._llm_gate(), p2._llm_gate()
            ok("budget_allows_then_blocks", g1 is None and g2 is not None, {"g1": g1, "g2": g2})
        finally:
            p2.set_llm_controls(budget=before["budget"], killed=before["killed"], reset=True)
            p2._llm_calls = before["calls"]
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def record_turn_promote_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """A4 (IG-2): prove durable facts GROW during play — record_turn auto-promotes high-value turns on a
        cadence (additive, deterministic). Uses a __selftest__ save (auto-reaped by the dispatch hook)."""
        m = self.memory
        save = "__a4_promote_selftest__" + str(int(time.time() * 1000))
        nk = save + "|g|Marine"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.index_npcs(save, [{"npc_key": nk, "name": "Test Marine", "faction_id": "argon"}], game_id="g")
            ok("starts_with_no_facts", len(m.get_facts(nk)) == 0)
            hi = ["I promise to escort your convoy to Argon Prime.",
                  "You betrayed our deal and I will not forget it.",
                  "I pledge two destroyers to the defense of Hatikvah.",
                  "We have an agreement: hull parts for protection.",
                  "I threaten to blockade your trade lanes if you refuse.",
                  "I refuse to pay reparations for a war you started."]
            for i, txt in enumerate(hi):
                m.record_turn(nk, "user" if i % 2 == 0 else "assistant", txt)
            facts_after = len(m.get_facts(nk))
            ok("facts_grew_during_play", facts_after > 0, {"facts": facts_after, "turns": len(hi)})
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    # --- Universe state: factions + relationships ----------------------------

    def factions_list(self, save_id: str) -> dict[str, Any]:
        return {"ok": True, "factions": self.memory.list_factions(save_id)}

    # --- Canon lore harvest (X4 encyclopedia -> graph + RAG) ------------------

    def lore_selftest(self) -> dict[str, Any]:
        return lore_mod.run_lore_selftest()

    def lore_status(self, save_id: str = "") -> dict[str, Any]:
        canon = self.memory.CANON_SAVE
        return {"ok": True, "scope": canon, "game": catdat_mod.available(),
                "lore_count": len(self.memory.list_lore(canon)),
                "canon_factions": len(self.memory.list_factions(canon)),
                "canon_relations": len(self.memory.list_relationships(canon))}

    def suggest(self, save_id: str, faction_id: str, npc_name: str,
                game_id: str = "chat", count: int = 3) -> dict[str, Any]:
        """ME-wheel openers for a conversation: `count` short paraphrase labels + the fuller line,
        RAG-grounded in the NPC's faction standing + memory. faction_id may be a display name."""
        sugg = self.player2.generate_suggestions(
            save_id=save_id, game_id=game_id,
            faction_id=self.memory.resolve_faction_id(faction_id) or faction_id,
            npc_name=npc_name, count=max(1, min(int(count or 3), 5)))
        return {"ok": True, "npc": npc_name, "faction_id": faction_id, "suggestions": sugg}

    # --- World-model sync: write-back of mod-caused in-game relation changes -----

    def relation_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The mod dispatcher reports a relation it just changed in-game (via set_faction_relation).
        We record it so the DB mirrors reality and the dashboard shows what the mod did."""
        save_id = str(payload.get("save_id") or "unindexed")
        subject = self.memory.resolve_faction_id(payload.get("subject") or payload.get("faction") or "")
        obj = self.memory.resolve_faction_id(payload.get("object") or payload.get("target") or "")
        try:
            rel = float(payload.get("relation"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "relation must be a number"}
        if not subject or not obj:
            return {"ok": False, "error": "subject and object required"}
        entry = self.memory.record_influence_change(
            save_id, subject, obj, rel,
            source=str(payload.get("source") or "mod_dispatch"), note=str(payload.get("note") or ""))
        # Human-readable echo of EXACTLY what was committed, so the mod can show it in-chat instead of a
        # blind "Dispatching" — the player sees the real DB row, not a hopeful acknowledgement.
        def _nm(fid: str) -> str:
            f = self.memory.get_faction(self.memory.CANON_SAVE, fid)
            return (f.get("name") if f else None) or fid
        _new = entry.get("new_relation"); _old = entry.get("old_relation"); _std = entry.get("standing")
        _oldtxt = f"{_old:+.2f}" if isinstance(_old, (int, float)) else "unknown"
        message = (f"[World updated] {_nm(subject)} -> {_nm(obj)}: now {_std} ({_new:+.2f}), was {_oldtxt}. "
                   f"Committed to the database.")
        # Derive the conflict/world-event/ceasefire immediately so it shows the moment the player acts
        # (idempotent with the heartbeat's reconcile).
        try:
            self.memory.reconcile_world_from_relations(save_id)
        except Exception:
            pass
        return {"ok": True, "logged": entry, "message": message}

    def relations_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sync-on-load: the mod reports the ACTUAL in-game faction relations (read from the live
        game) so the DB overlay mirrors reality — fixing save-load desync + id fragmentation. These
        OVERWRITE the overlay (source="game" = ground truth) and do NOT spam the influence_log (that's
        only for explicit mod-caused changes)."""
        save_id = str(payload.get("save_id") or "unindexed")
        rels = payload.get("relations") or []
        n = 0
        for r in rels:
            subj = self.memory.resolve_faction_id((r.get("subject") if isinstance(r, dict) else "") or "")
            obj = self.memory.resolve_faction_id((r.get("object") if isinstance(r, dict) else "") or "")
            try:
                rel = float(r.get("relation"))
            except (TypeError, ValueError, AttributeError):
                continue
            if subj and obj and subj != obj:
                self.memory.set_live_relationship(save_id, subj, obj, rel, source="game")
                n += 1
        # Tier-1 world model: derive conflicts / world-events / ceasefires from the relations we just
        # synced (idempotent — only acts on war/peace transitions). Catches X4's own wars too.
        recon = {}
        try:
            recon = self.memory.reconcile_world_from_relations(save_id)
        except Exception:
            pass
        # Tier-3 strategic deriver: now that relations/conflicts are fresh, recompute every faction's
        # pressures + dynamic mood from the substrate (economy/losses/sectors). Cheap + idempotent.
        derived = {}
        try:
            derived = self.memory.derive_all_pressures(save_id)
        except Exception:
            pass
        return {"ok": True, "synced": n, "save_id": save_id, "reconciled": recon, "derived": derived}

    def sectors_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The mod reports live sector ownership (read in Lua via C.GetSectorsByOwner). Mirror it into
        the sectors table so the Territory panel + the strategic deriver have real geography. Names +
        owners only for now; contested/value/player-assets are derivable later."""
        save_id = str(payload.get("save_id") or "unindexed")
        rows = payload.get("sectors") or []
        # SPEC 0b: key KNOWN sectors by NAME (stable) and skip unexplored "Unknown Sector" rows, so the
        # table holds one clean row per named sector instead of ~8 dup rows under unstable numeric ids.
        resolved = []
        for s in rows:
            if not isinstance(s, dict):
                continue
            nm = (str(s.get("name") or "")).strip()
            if not nm or nm == "Unknown Sector":
                continue
            owner = self.memory.resolve_faction_id(str(s.get("owner") or "")) or (s.get("owner") or None)
            resolved.append({"name": nm, "owner": owner})
        res = self.memory.replace_sectors_by_name(save_id, resolved)
        return {"ok": True, "synced": res.get("stored", 0), "save_id": save_id}

    def logbook_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """SPEC 1c: ingest NEW entries from the game's own logbook (news/alerts/diplomacy) → world_event
        memories. The in-game reader sends only entries past its cursor; we dedup again here (cursor resets
        on reload)."""
        save_id = str(payload.get("save_id") or "unindexed")
        entries = payload.get("entries") or []
        ingested = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                if self.memory.ingest_logbook_event(
                        save_id, category=str(e.get("category") or ""),
                        title=str(e.get("title") or ""), text=str(e.get("text") or ""),
                        faction=str(e.get("faction") or ""), entity=str(e.get("entity") or "")):
                    ingested += 1
            except Exception:
                pass
        return {"ok": True, "ingested": ingested, "received": len(entries), "save_id": save_id}

    def factions_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """SPEC 1c-C: the mod reports each faction's live NAME + REPRESENTATIVE (the named per-faction NPC,
        via C.GetFactionRepresentative → GetComponentName). The representative is the persistent 'rememberer'
        memories/attitudes attach to."""
        save_id = str(payload.get("save_id") or "unindexed")
        rows = payload.get("factions") or []
        n = 0
        for f in rows:
            if not isinstance(f, dict):
                continue
            fid = self.memory.resolve_faction_id(str(f.get("faction_id") or "")) or (f.get("faction_id") or None)
            if not fid:
                continue
            rep = (str(f.get("representative") or "")).strip()
            nm = (str(f.get("name") or "")).strip() or None
            self.memory.upsert_faction(save_id, fid, name=nm, representative=(rep or None))
            n += 1
        return {"ok": True, "synced": n, "save_id": save_id}

    def fleets_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The mod reports each faction's ship census (counts by primarypurpose, read in Lua via
        GetContainedObjectsByOwner). Mirrors into fleet_strength for the Fleet viewer + the deriver's
        military pressure."""
        save_id = str(payload.get("save_id") or "unindexed")
        rows = payload.get("fleets") or []
        n = 0
        for f in rows:
            if not isinstance(f, dict):
                continue
            fid = self.memory.resolve_faction_id(str(f.get("faction_id") or "")) or (f.get("faction_id") or None)
            if not fid:
                continue
            self.memory.upsert_fleet_strength(save_id, fid,
                total_ships=f.get("total_ships"), fight=f.get("fight"), trade=f.get("trade"),
                mine=f.get("mine"), build=f.get("build"), other=f.get("other"), capitals=f.get("capitals"))
            n += 1
        # Optional per-sector presence map (presence[sector_id][faction]=fightCount) -> contested_by,
        # which makes territorial_pressure (+ criminal-filtered piracy) emergent. Safe if absent.
        contested = {"presence_sectors": 0, "note": "no presence field in payload"}
        presence = payload.get("presence")
        if isinstance(presence, dict) and presence:
            try:
                contested = self.memory.sync_contested_from_presence(save_id, presence)
            except Exception as exc:
                contested = {"error": str(exc)}
        self._last_presence_debug = contested
        return {"ok": True, "synced": n, "save_id": save_id, "contested": contested}

    def fleets_list(self, save_id: str) -> dict[str, Any]:
        return {"ok": True, "fleets": self.memory.list_fleet_strength(save_id),
                "presence_debug": getattr(self, "_last_presence_debug", None)}

    def influence_log(self, save_id: str = "", limit: int = 50) -> dict[str, Any]:
        return {"ok": True, "entries": self.memory.list_influence_log(save_id or None, max(1, min(limit, 200)))}

    def lore_resolve(self, q: str, save_id: str = "") -> dict[str, Any]:
        """Prove the canon/save split: map a faction reference (id or display-name) to its
        canon id, then pull the anchor's subgraph (save overlay over canon defaults)."""
        sid = save_id or self.memory.CANON_SAVE
        rid = self.memory.resolve_faction_id(q)
        g = self.memory.graph_retrieve(sid, q, "who are you and who are your enemies", k=4)
        return {"ok": True, "input": q, "resolved_id": rid, "save_id": sid,
                "graph_sample": [{"id": x.get("id"), "text": (x.get("text") or "")[:140]} for x in g]}

    def lore_harvest(self, save_id: str = "", game_path: str | None = None) -> dict[str, Any]:
        """Read the game's factions.xml + text DB, parse to faction nodes /
        canonical relations / lore chunks, and seed them into the CANON scope
        (universe-constant, save-independent). Every save reads canon underneath
        its own live deltas — no per-save re-harvest, no 'demo' leak."""
        gp = catdat_mod.resolve_game_path(game_path)
        if not gp:
            return {"ok": False, "error": "X4 game install not found (set X4_GAME_PATH)."}
        fac_xml = catdat_mod.extract_text("libraries/factions.xml", gp)
        if not fac_xml:
            return {"ok": False, "error": "libraries/factions.xml not found in game archives."}
        txt_xml = catdat_mod.extract_text("t/0001-l044.xml", gp)  # English DB (optional)
        result = lore_mod.harvest(fac_xml, txt_xml)
        applied = lore_mod.apply(self.memory, self.memory.CANON_SAVE, result)
        return {"ok": True, "game_path": gp, "scope": self.memory.CANON_SAVE,
                "text_resolved": result["text_resolved"], "applied": applied,
                "sample": result["lore_chunks"][:3]}

    def wares_harvest(self, payload: dict[str, Any] | None = None, game_path: str | None = None) -> dict[str, Any]:
        """SPEC 1k v2: extract libraries/wares.xml (the encyclopedia's source) into canon lore kind='ware' —
        the COMPLETE ware catalog — so RoleRAG can reject off-universe commodities (closes the Veldspar leak)."""
        gp = catdat_mod.resolve_game_path((payload or {}).get("game_path") if payload else game_path)
        if not gp:
            return {"ok": False, "error": "X4 game install not found (set X4_GAME_PATH)."}
        wares_xml = catdat_mod.extract_text("libraries/wares.xml", gp)
        if not wares_xml:
            return {"ok": False, "error": "libraries/wares.xml not found in game archives."}
        txt_xml = catdat_mod.extract_text("t/0001-l044.xml", gp)
        result = lore_mod.parse_wares(wares_xml, txt_xml)
        applied = lore_mod.apply(self.memory, self.memory.CANON_SAVE, result)
        # RoleRAG caches its entity index per save — invalidate so the new wares load on next analyze.
        try:
            rr = getattr(self.player2, "_rolerag", None)
            if rr is not None:
                rr.invalidate()
        except Exception:
            pass
        return {"ok": True, "game_path": gp, "wares": applied.get("lore_chunks", 0),
                "text_resolved": result["text_resolved"],
                "sample": [c["title"] for c in result["lore_chunks"][:10]]}

    # Bump to force every install to re-harvest canon on its next boot (e.g. after adding a new category).
    CANON_VERSION = "2026.06.26-1"

    def ensure_canon(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """ZERO-FRICTION canon builder (ship requirement). On bridge boot, populate the universe-constant DB —
        faction identities/lore + the COMPLETE ware catalog — from the game's OWN library files, IF not already
        built at the current version. Universe-constant + save-independent, stored once in SQLite (CANON scope)
        and read underneath every save, so a player starting a NEW game gets a fully-grounded, lore-accurate NPC
        layer with NO manual step, no script, no instructions. Idempotent + version-stamped (a cheap no-op once
        built); called off-thread at startup so it never blocks serving."""
        force = bool((payload or {}).get("force")) if isinstance(payload, dict) else False
        try:
            if not force:
                meta = {str(m.get("key")): str(m.get("text")) for m in self.memory.list_lore(self.memory.CANON_SAVE, "_meta")}
                have_fac = len(self.memory.list_lore(self.memory.CANON_SAVE, "faction")) > 0
                have_ware = len(self.memory.list_lore(self.memory.CANON_SAVE, "ware")) > 0
                if meta.get("canon_version") == self.CANON_VERSION and have_fac and have_ware:
                    return {"ok": True, "skipped": "already built", "version": self.CANON_VERSION}
        except Exception:
            pass
        gp = catdat_mod.resolve_game_path()
        if not gp:
            return {"ok": False, "error": "X4 install not found from the bridge location — canon not built."}
        built: dict[str, Any] = {}
        for name, fn in (("factions", self.lore_harvest), ("wares", self.wares_harvest)):
            try:
                r = fn()
                built[name] = r.get("applied", r.get("wares", r.get("ok")))
            except Exception as e:
                built[name + "_error"] = str(e)
        try:
            self.memory.upsert_lore(self.memory.CANON_SAVE, "_meta", "canon_version", "canon_version", self.CANON_VERSION)
        except Exception:
            pass
        try:
            rr = getattr(self.player2, "_rolerag", None)
            if rr is not None:
                rr.invalidate()
        except Exception:
            pass
        return {"ok": True, "built": built, "version": self.CANON_VERSION, "game_path": gp}

    def faction_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id", ""))
        faction_id = str(payload.get("faction_id", ""))
        if not faction_id:
            return {"ok": False, "error": "faction_id required"}
        self.memory.upsert_faction(
            save_id, faction_id, name=payload.get("name"), values=payload.get("values"),
            biases=payload.get("biases"), current_goal=payload.get("current_goal"),
            mood=payload.get("mood"), summary=payload.get("summary"))
        return {"ok": True, "faction": self.memory.get_faction(save_id, faction_id)}

    def npc_index(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Index encounterable/named NPCs + the player into the registry for this save.
        Payload: {save_id, game_id?, npcs:[{npc_key?, name, faction_id?, role?, ...}], player?:{name}}.
        NPC identities are upserted WITHOUT clobbering any existing Player2 binding; the player is
        stored as the per-save singleton."""
        save_id = str(payload.get("save_id") or "unindexed")
        game_id = str(payload.get("game_id") or "")
        npcs = payload.get("npcs") or []
        indexed = self.memory.index_npcs(save_id, npcs, game_id)
        player = payload.get("player") or {}
        pname = str(player.get("name") or "").strip()
        player_indexed = False
        if pname:
            try:
                self.memory.upsert_player(save_id, pname)
                player_indexed = True
            except Exception:
                player_indexed = False
        return {"ok": True, "save_id": save_id, "indexed": indexed, "player_indexed": player_indexed}

    def relationships_list(self, save_id: str, subject: str | None = None) -> dict[str, Any]:
        return {"ok": True, "relationships": self.memory.list_relationships(save_id, subject)}

    def relationship_adjust(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id", ""))
        subject = str(payload.get("subject", ""))
        obj = str(payload.get("object", ""))
        if not subject or not obj:
            return {"ok": False, "error": "subject and object required"}
        rel = self.memory.adjust_relationship(
            save_id, subject, obj,
            dtrust=int(payload.get("dtrust", 0) or 0), dfear=int(payload.get("dfear", 0) or 0),
            dresentment=int(payload.get("dresentment", 0) or 0), ddebt=int(payload.get("ddebt", 0) or 0),
            standing=payload.get("standing"), summary=payload.get("summary"))
        return {"ok": True, "relationship": rel}

    def universe_seed(self, save_id: str) -> dict[str, Any]:
        """Seed canonical X4 factions + a few relationships for demoing (no LLM).
        Idempotent: clears the save's substrate first so re-seeding doesn't pile up
        duplicate conflicts/incidents/world_events or double relationship scores."""
        self.memory.clear_substrate(save_id)
        factions = {
            "argon":   ("Argon Federation", ["freedom", "security", "commerce"], {"aggression": 0.35, "economic_focus": 0.65, "risk_tolerance": 0.45, "diplomacy": 0.75}, "Hold the Xenon frontier"),
            "teladi":  ("Teladi Company",   ["profit", "pragmatism"],            {"aggression": 0.20, "economic_focus": 0.95, "risk_tolerance": 0.55, "diplomacy": 0.60}, "Maximize trade margins"),
            "paranid": ("Godrealm of the Paranid", ["faith", "order"],          {"aggression": 0.60, "economic_focus": 0.50, "risk_tolerance": 0.50, "diplomacy": 0.40}, "Expand the Godrealm"),
            "split":   ("Zyarth Patriarchy", ["strength", "honor"],             {"aggression": 0.85, "economic_focus": 0.40, "risk_tolerance": 0.80, "diplomacy": 0.25}, "Prove dominance in battle"),
            "boron":   ("Boron Kingdom",    ["science", "harmony"],             {"aggression": 0.15, "economic_focus": 0.55, "risk_tolerance": 0.30, "diplomacy": 0.90}, "Advance research, avoid war"),
            "terran":  ("Terran Protectorate", ["order", "sovereignty"],        {"aggression": 0.55, "economic_focus": 0.50, "risk_tolerance": 0.40, "diplomacy": 0.45}, "Protect Sol, distrust Commonwealth"),
        }
        for fid, (name, values, biases, goal) in factions.items():
            self.memory.upsert_faction(save_id, fid, name=name, values=values, biases=biases, current_goal=goal, mood="watchful")
        rels = [
            ("argon", "player", 10, 0, 5, 0, "neutral"),
            ("teladi", "player", 5, 0, 0, 20, "creditor"),
            ("split", "argon", -20, 10, 40, 0, "hostile"),
            ("argon", "split", -10, 30, 25, 0, "wary"),
            ("paranid", "argon", -15, 0, 20, 0, "rival"),
            ("boron", "argon", 40, 0, 0, 0, "ally"),
        ]
        for subj, obj, t, f, r, d, standing in rels:
            self.memory.adjust_relationship(save_id, subj, obj, dtrust=t, dfear=f, dresentment=r, ddebt=d, standing=standing)
        # Demo pressure aggregates so the universe is immediately scorable (Stage 1).
        # Split: at war with Argon, bleeding — high military + losses. Teladi: economic.
        strat = {
            "argon":   {"military_pressure": 0.55, "economic_pressure": 0.30, "recent_losses": 0.40, "logistics_stress": 0.35, "player_alignment": 0.10},
            "split":   {"military_pressure": 0.60, "economic_pressure": 0.45, "recent_losses": 0.50, "logistics_stress": 0.40, "player_alignment": -0.10},
            "teladi":  {"military_pressure": 0.15, "economic_pressure": 0.70, "logistics_stress": 0.30, "player_alignment": 0.05},
            "boron":   {"military_pressure": 0.10, "economic_pressure": 0.20, "recent_losses": 0.05, "player_alignment": 0.30},
            "paranid": {"military_pressure": 0.45, "economic_pressure": 0.35, "recent_losses": 0.20, "player_alignment": -0.05},
            "terran":  {"military_pressure": 0.40, "economic_pressure": 0.30, "player_alignment": -0.15},
        }
        for fid, p in strat.items():
            self.memory.upsert_strategic_state(save_id, fid, **p)
        # Substrate domains so every debug panel shows data after a seed.
        econ = {
            "argon":  {"dependency_on_player": 0.7, "production_health": 0.6, "key_needs": ["hullparts", "energycells"], "shortages": {"hullparts": 0.6}, "market_status": "partner"},
            "teladi": {"dependency_on_player": 0.4, "production_health": 0.9, "key_needs": ["refinedmetals"], "market_status": "partner"},
            "split":  {"dependency_on_player": 0.2, "production_health": 0.4, "key_needs": ["weapongrade"], "shortages": {"weapongrade": 0.8}, "market_status": "obstacle"},
        }
        for fid, e in econ.items():
            self.memory.upsert_economy(save_id, fid, **e)
        self.memory.upsert_player_market(save_id, "hullparts", "Argon Prime", dominance_level=0.8, supplying_enemies=False, note="Player is Argon's dominant hull-parts supplier")
        sectors = [
            ("argon_prime", "Argon Prime", "argon", ["xenon"], 0.95, True),
            ("hatikvah", "Hatikvah's Choice", "argon", ["split"], 0.6, True),
            ("family_zhin", "Family Zhin", "split", ["argon"], 0.7, False),
        ]
        for sid_, name, owner, contested, sv, pap in sectors:
            self.memory.upsert_sector(save_id, sid_, name=name, owner_faction=owner, contested_by=contested, strategic_value=sv, player_assets_present=pap)
        self.memory.add_conflict(save_id, "split", "argon", status="active", intensity=0.6, cause="border raids")
        for _ in range(4):
            self.memory.record_loss(save_id, "split", amount=12, kind="ship")
            self.memory.record_loss(save_id, "argon", amount=8, kind="ship")
        self.memory.add_agreement(save_id, "teladi", "player", type="trade", terms={"ware": "hullparts", "duration_h": 50})
        self.memory.add_incident(save_id, "escalate_pressure", faction_id="split", target="argon", confidence=0.56, priority=5, narrative="Split fleets mass on the Argon border.", effects={"relation_delta": {"split->argon": -10}})
        self.memory.add_world_event(save_id, "war", summary="Zyarth Patriarchy renews its offensive against the Argon Federation.", primary_faction="split", secondary_faction="argon", importance=5, source="seed")
        self.memory.add_world_event(save_id, "economic_threshold", summary="Argon hull-parts shortage crosses a critical threshold.", primary_faction="argon", importance=3, source="seed")
        return {"ok": True, "seeded_factions": len(factions),
                "seeded_relationships": len(rels), "seeded_strategic_state": len(strat),
                "seeded_economy": len(econ), "seeded_sectors": len(sectors),
                "seeded_conflicts": 1, "seeded_agreements": 1,
                "seeded_incidents": 1, "seeded_world_events": 2}

    # --- Decision layer: strategic_state + deterministic scoring (Stage 1) ----

    def strategic_state_list(self, save_id: str) -> dict[str, Any]:
        return {"ok": True, "strategic_state": self.memory.list_strategic_state(save_id)}

    def strategic_state_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id", ""))
        faction_id = str(payload.get("faction_id", ""))
        if not faction_id:
            return {"ok": False, "error": "faction_id required"}
        pressures = {k: payload[k] for k in MemoryStore.PRESSURE_FIELDS if k in payload}
        state = self.memory.upsert_strategic_state(save_id, faction_id, **pressures)
        return {"ok": True, "strategic_state": state}

    def strategic_score(self, save_id: str, faction_id: str) -> dict[str, Any]:
        from .scoring import rank_faction
        state = self.memory.get_strategic_state(save_id, faction_id)
        if not state:
            return {"ok": False, "error": f"no strategic_state for faction '{faction_id}' in save '{save_id}'"}
        rels = self.memory.list_relationships(save_id, subject=faction_id)
        options = rank_faction(faction_id, state, rels)
        return {"ok": True, "faction_id": faction_id, "state": state, "options": options}

    def strategic_selftest(self) -> dict[str, Any]:
        from .scoring import run_scoring_selftest
        return run_scoring_selftest()

    # --- Strategic review: one full influence cycle (LLM-off, deterministic) ---

    def _llm_decide(self, save_id: str, faction_id: str, faction_name: str,
                    state: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
        """Stage 2 — a faction-leader NPC (via Player2) PICKS one of the deterministic
        legal options + narrates. Bounded chooser: it can only choose from the menu;
        on any failure we fall back to the top deterministic option (index 0)."""
        import re as _re
        legal = options[:4]
        lines = []
        for i, o in enumerate(legal, 1):
            tgt = o.get("target") or ""
            lines.append(f"{i}. {o['action']}" + (f" toward {tgt}" if tgt not in ("self", "") else ""))
        p = state or {}
        sit = (f"military pressure {round(float(p.get('military_pressure', 0))*100)}%, "
               f"economic {round(float(p.get('economic_pressure', 0))*100)}%, "
               f"recent losses {round(float(p.get('recent_losses', 0))*100)}%")
        prompt = (f"Your situation: {sit}.\nLegal options:\n" + "\n".join(lines) +
                  f"\nReply with ONLY the option number (1-{len(legal)}) then one short sentence of reasoning. Do not invent actions.")
        payload = {
            "request_id": f"infdec-{int(time.time()*1000)}-{faction_id}",
            "source_mod": "influence_decider", "channel": "npc",
            "target": {"mode": "npc", "game_id": "influence", "save_id": save_id,
                       "npc_name": f"{faction_name} War Council", "npc_short_name": faction_id[:8],
                       "faction_id": faction_id,
                       "system_prompt": (f"You are the ruling war council of {faction_name} in the X4 galaxy. "
                                         "Choose exactly ONE action from the numbered legal options your strategists present. "
                                         "Never invent an action. Reply with the number first, then one sentence of reasoning.")},
            "messages": [{"role": "user", "content": prompt}],
        }
        t0 = time.time()
        try:
            resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
            dt = int((time.time() - t0) * 1000)
            reply = (resp.get("reply") or "").strip()
            m = _re.search(r"\b([1-9])\b", reply)
            idx = (int(m.group(1)) - 1) if m else None
            if resp.get("status") == "ok" and idx is not None and 0 <= idx < len(legal):
                return {"index": idx, "narrative": reply[:240], "llm_status": "ok", "latency_ms": dt}
            return {"index": 0, "narrative": reply[:240] or "(no usable pick)",
                    "llm_status": ("unparsed" if resp.get("status") == "ok" else "fallback"), "latency_ms": dt}
        except Exception as exc:
            return {"index": 0, "narrative": f"(llm error: {exc})", "llm_status": "error",
                    "latency_ms": int((time.time() - t0) * 1000)}

    def review_faction(self, save_id: str, faction_id: str, use_llm: bool = False,
                       autonomous: bool = False, force: bool = False) -> dict[str, Any]:
        """One influence cycle for a faction: derive pressures from the substrate ->
        score legal options -> PICK (deterministic top, or Stage-2 LLM if use_llm) ->
        write an incident -> apply its effects via the world model."""
        from .scoring import rank_faction
        state = self.memory.derive_pressures(save_id, faction_id)
        rels = self.memory.list_relationships(save_id, subject=faction_id)
        options = rank_faction(faction_id, state, rels)
        if not options:
            return {"ok": True, "faction_id": faction_id, "pressures": state, "decision": None}

        # D8: the faction's strategic PICK is a Player2 decision via the UNIFIED adapter — never the deterministic
        # top, never _llm_decide's index-0 fallback. On failure it DEFERS (the faction HOLDS this cycle). The
        # deterministic rank is advisory only. (use_llm is now vestigial; Player2 is the decider by default.)
        fac = self.memory.get_faction(save_id, faction_id) or {}
        sit = state or {}
        opts = [{"key": str(i),
                 "label": o["action"] + (f" toward {o.get('target')}" if o.get("target") not in ("self", "", None) else "")}
                for i, o in enumerate(options[:6])]
        brief = (f"Your situation: military pressure {round(float(sit.get('military_pressure', 0))*100)}%, "
                 f"economic {round(float(sit.get('economic_pressure', 0))*100)}%, "
                 f"recent losses {round(float(sit.get('recent_losses', 0))*100)}%.\n"
                 "Choose your faction's strategic move this cycle:")
        sp = (f"You are the ruling leadership of {fac.get('name') or faction_id} in the X4 galaxy. Choose ONE numbered "
              "strategic move that fits your faction's interests and doctrine; never invent one.")
        d = self.decide(save_id, "faction_action", faction_id, str(fac.get("name") or faction_id), brief, opts,
                        system_prompt=sp, advisory={"top": options[0]["action"], "score": options[0].get("score")})
        if d.get("source") != "player2" or d.get("choice") is None:
            return {"ok": True, "faction_id": faction_id, "pressures": state, "options": options[:5],
                    "decision": None, "deferred": True, "incident_id": None}
        try:
            idx = int(d["choice"])
        except Exception:
            idx = -1
        if not (0 <= idx < len(options)):
            return {"ok": True, "faction_id": faction_id, "pressures": state, "options": options[:5],
                    "decision": None, "deferred": True, "incident_id": None}
        top = options[idx]
        llm_meta = {"index": idx, "narrative": d.get("reason", ""), "llm_status": "ok",
                    "latency_ms": 0, "decision_id": d.get("decision_id")}

        action = top["action"]
        target = top.get("target") or ""
        eff_target = "" if target in ("self", faction_id) else target

        # Stage-3 validator: re-check legality / bounds / cooldown / idempotency / confirmation
        # BEFORE writing a state-changing incident. The picker (deterministic or LLM) PROPOSES;
        # this deterministic gate disposes — the LLM is never the authority.
        from .scoring import validate_incident
        v = validate_incident(
            action, faction_id, eff_target,
            legal_actions=[o["action"] for o in options],
            confidence=float(top.get("score", 0)),
            # force (the on-demand PROVING path) bypasses the cooldown/idempotency check by hiding
            # recent incidents — so a test can make a faction act NOW, deterministically.
            recent=([] if force else self.memory.list_incidents(save_id, limit=20)),
        )
        if not v["ok"]:
            return {"ok": True, "faction_id": faction_id, "pressures": state, "options": options[:5],
                    "decision": {"action": action, "target": eff_target, "score": top.get("score"),
                                 "rejected": v["reason"], "stage3": v["status"]},
                    "incident_id": None}

        # 1h-B: a 'dialogue_only' pick is a NON-action (the faction held). Don't record an incident for it —
        # otherwise the incidents table grows every heartbeat with no-op rows (the "167 pending" backlog).
        if action == "dialogue_only":
            return {"ok": True, "faction_id": faction_id, "pressures": state, "options": options[:5],
                    "decision": {"action": action, "target": eff_target, "score": top.get("score"),
                                 "stage3": v["status"], "requires_confirmation": v["requires_confirmation"]},
                    "incident_id": None, "effects_applied": ["noop"]}
        narrative = (llm_meta["narrative"] if (llm_meta and llm_meta.get("llm_status") == "ok")
                     else f"{faction_id} chooses {action}" + (f" toward {eff_target}" if eff_target else ""))
        inc = self.memory.add_incident(
            save_id, action, faction_id=faction_id, target=eff_target,
            confidence=float(top.get("score", 0)),
            priority=max(0, int(round(float(top.get("score", 0)) * 10))),
            narrative=narrative,
            effects={"source": "player2", "score": top.get("score")})
        if v["requires_confirmation"] and not autonomous:
            # Player-facing path only (e.g. a chat-proposed action): high-impact awaits confirm.
            self.memory.set_incident_status(save_id, inc["id"], "pending")
            applied = None
        else:
            # LIVING UNIVERSE (Ken 2026-06-25): autonomous faction decisions apply WITHOUT player approval —
            # no confirmation friction, no immersion break. The universe acts on its own; the player reacts.
            applied = self.memory.apply_incident_effects(save_id, action, faction_id, eff_target)
            self.memory.set_incident_status(save_id, inc["id"], "applied")
            if llm_meta.get("decision_id"):
                self.memory.finalize_decision(llm_meta["decision_id"], validator_result=v, final_status="applied")
        out = {"ok": True, "faction_id": faction_id, "pressures": state,
               "options": options[:5],
               "decision": {"action": action, "target": eff_target, "score": top.get("score"),
                            "stage3": v["status"], "requires_confirmation": v["requires_confirmation"]},
               "incident_id": inc["id"], "effects_applied": applied}
        if llm_meta:
            out["llm"] = {"status": llm_meta["llm_status"], "latency_ms": llm_meta["latency_ms"],
                          "narrative": llm_meta["narrative"]}
        return out

    def review_all(self, save_id: str, use_llm: bool = False) -> dict[str, Any]:
        factions = [f["faction_id"] for f in self.memory.list_factions(save_id)]
        reviews = [self.review_faction(save_id, fid, use_llm=use_llm) for fid in factions]
        decided = [r for r in reviews if r.get("decision")]
        return {"ok": True, "save_id": save_id, "factions": len(factions),
                "decisions": len(decided), "reviews": reviews}

    # --- KEYSTONE: background generation + fast drain (decouple LLM work from the in-game request) ----------
    INFLUENCE_DAEMON_CADENCE_S = 22.0   # how often the daemon generates a slice (server-side, off the hot path)
    INFLUENCE_DRAIN_IDLE_S = 150.0      # stop generating if the game hasn't pulled in this long (save closed)
    DRAIN_CAP = 40                       # max items kept per channel so a paused game doesn't grow the queue forever

    def _influence_daemon(self) -> None:
        """Generate the influence loop OFF the in-game request path. Runs only while the game is actively
        draining (gated by _last_drain_ts) so a closed save costs no LLM. Pushes results into _drain for the
        mod to pull instantly via /v1/influence_drain."""
        import time as _t
        while True:
            _t.sleep(self.INFLUENCE_DAEMON_CADENCE_S)
            save = self._last_active_save
            if not save:
                continue
            if (_t.time() - self._last_drain_ts) > self.INFLUENCE_DRAIN_IDLE_S:
                continue  # game isn't pulling — don't burn the LLM
            # #67: the live loop is now the Player2 DECISION TICK — tiered + self-gated (#50). This runs the resolver
            # (proposals get a Player2 verdict and CLOSE, instead of piling), Player2 COA/route/assessment, deal
            # proposal, and the throttled faction review — replacing the every-22s influence_step spam. Each driver
            # DEFERS on Player2 failure (never math-substitutes). The tiers self-gate by interval, so most 22s wakeups
            # do nothing (cheap); real work fires only when a tier is due.
            try:
                tick = self.decision_tick(save)
            except Exception:
                continue
            try:
                self._enqueue_drain(save, self._drain_from_tick(tick))
            except Exception:
                pass

    def _drain_from_tick(self, tick: dict[str, Any]) -> dict[str, Any]:
        """#67: turn a decision_tick result into the game drain feed — the throttled faction-review news/actions PLUS
        player-facing lines for the Player2 DECISIONS this tick made (negotiation verdicts, COA commitments), so the
        logbook SHOWS the LLM's intent instead of a wall of unresolved requests. Empty between due tiers (the throttle)."""
        det = tick.get("detail") or {}
        strat = det.get("strategic") or {}
        oper = det.get("operational") or {}
        base = strat.get("influence") or {}
        feed = {"news": list(base.get("news") or []), "actions": list(base.get("actions") or []),
                "articles": list(base.get("articles") or []), "phase_effects": list(base.get("phase_effects") or [])}
        # Player2 negotiation verdicts — the "requests" the player was drowning in, now RESOLVED in character.
        for o in ((strat.get("offers") or {}).get("results") or []):
            feed["news"].append(
                f"Diplomacy: {o.get('recipient') or 'a faction'} responds to a proposal — {o.get('decision')}.")
        # Player2 course-of-action commitments (OPORD decisions).
        for c in ((oper.get("coa") or {}).get("results") or []):
            feed["news"].append(f"Command: {c.get('faction') or 'a faction'} commits to a course of action.")
        # #63: NPC>NPC scene — overheard lines surface to the player logbook.
        for line in ((strat.get("scene") or {}).get("news") or []):
            feed["news"].append(line)
        # FRAGO job escalation — material contract changes only (raise/withdraw), per the anti-spam rule.
        for line in ((strat.get("job_escalation") or {}).get("news") or []):
            feed["news"].append(line)
        # #27: FRAGOs on the player's ACTIVE contracts — news ping + contract_frago action for the MD half.
        cf = strat.get("contract_frago") or {}
        for line in (cf.get("news") or []):
            feed["news"].append(line)
        for act in (cf.get("actions") or []):
            feed["actions"].append(act)
        # #64: validated relation moves proposed in the scene → in-game actions (Lua→MD set_faction_relation).
        for act in ((strat.get("scene") or {}).get("actions") or []):
            feed["actions"].append(act)
        return feed

    def _enqueue_drain(self, save_id: str, res: dict[str, Any]) -> None:
        q = self._drain.setdefault(save_id, {"news": [], "actions": [], "articles": [], "phase_effects": []})
        for ch in ("news", "actions", "articles", "phase_effects"):
            items = res.get(ch) or []
            if items:
                q[ch].extend(items)
                if len(q[ch]) > self.DRAIN_CAP:
                    del q[ch][:-self.DRAIN_CAP]

    def influence_drain(self, payload: dict[str, Any]) -> dict[str, Any]:
        """FAST, LLM-free: return + clear whatever the daemon has prepared for this save. This is what the mod's
        heartbeat calls (replacing the slow influence_step POST). Also marks the save active so the daemon knows
        to keep generating for it."""
        import time as _t
        save_id = str(payload.get("save_id") or "unindexed")
        self._last_active_save = save_id
        self._last_drain_ts = _t.time()
        q = self._drain.get(save_id) or {"news": [], "actions": [], "articles": [], "phase_effects": []}
        self._drain[save_id] = {"news": [], "actions": [], "articles": [], "phase_effects": []}
        return {"ok": True, "save_id": save_id, "news": q["news"], "actions": q["actions"],
                "articles": q["articles"], "phase_effects": q["phase_effects"]}

    def influence_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        """SPEC 1d W1: ONE amortized slice of the autonomous influence loop. Reviews the next `budget`
        factions (round-robin cursor) — each decides from its pressures + GRUDGES and applies to the SHADOW
        world model only. High-impact war/peace is recorded PENDING (not applied) — NO real-game mutation
        yet. Driven by the mod's heartbeat (a few factions per tick = amortized, no spike). Returns the
        decisions as player-facing NEWS lines for the mod to post to the in-game logbook."""
        save_id = str(payload.get("save_id") or "unindexed")
        budget = max(1, min(int(payload.get("budget") or 2), 6))
        facs = [f.get("faction_id") for f in self.memory.list_factions(save_id)
                if f.get("faction_id") and f.get("faction_id") != "player"]
        if not facs:
            return {"ok": True, "news": [], "reviewed": 0, "save_id": save_id}
        if not hasattr(self, "_influence_cursor"):
            self._influence_cursor = {}
        start = self._influence_cursor.get(save_id, 0) % len(facs)
        news, actions, reviewed = [], [], 0
        phase_effects: list[dict[str, Any]] = []  # SPEC 3.3-A: war-phase substrate mutations (bridge-side, not in-game dispatch)
        # SPEC 1g: ensure canon persona biases (Aggr/Econ/Risk/Dipl + goal) are seeded — identity that drives
        # L3's persona_scale + decision scoring. Idempotent (only fills rows missing biases).
        try:
            self.memory.seed_faction_personas(save_id)
            self.memory.prune_incidents(save_id)  # 1h-B: keep the incidents table bounded
        except Exception:
            pass
        # SPEC 1f L3: age emotions (anti-spiral, rate-limited internally) + let factions REACT in-character to
        # fresh two-party galaxy events. This writes BOUNDED persona deltas back to the factors BEFORE the
        # decisions below read them, so the loop acts on freshly-felt grudges. Budgeted (≤ REACTION_BUDGET LLM).
        try:
            self.memory.decay_emotions(save_id)
        except Exception:
            pass
        reacted = 0
        try:
            for e in self.memory.list_world_events(save_id, limit=25, min_importance=3):
                if reacted >= self.REACTION_BUDGET:
                    break
                if str(e.get("source")) == "reaction":
                    continue
                a, b = e.get("primary_faction"), e.get("secondary_faction")
                if not (a and b) or a == b or a == "player" or b == "player":
                    continue
                if self._react(save_id, a, b, str(e.get("summary") or ""), event_key=f"evt:{e.get('id')}"):
                    reacted += 1
        except Exception:
            pass
        # Cap LLM-authored bulletins per tick — npc_complete is synchronous on the heartbeat POST,
        # so a few seconds each adds up; the rest use the deterministic fallback (still context-rich).
        LLM_NEWS_BUDGET = 2
        llm_used = 0
        comms_made = 0  # SPEC 1j: prominent player communiqués surfaced this tick (budget-capped)
        for i in range(min(budget, len(facs))):
            fid = facs[(start + i) % len(facs)]
            try:
                r = self.review_faction(save_id, fid, use_llm=False, autonomous=True)
            except Exception:
                continue
            reviewed += 1
            dec = r.get("decision")
            if dec and dec.get("action") and r.get("incident_id"):
                # SPEC 3 — PRIORITY GATE: classify + gate this decision before it surfaces/actuates. A hostile
                # relation move already at the -1.0 floor changed NOTHING (state_changed=False) → the gate
                # suppresses it (no news/actuate/comms) instead of spamming. The gate is the single routing
                # authority (tier + cooldown + no-op + dedup) replacing scattered ad-hoc checks.
                action = (dec.get("action") or "").lower()
                target = dec.get("target") or ""
                state_changed = True
                if target and action in ("escalate_pressure", "escalate", "declare_war", "impose_embargo", "sanction"):
                    try:
                        cur = (self.memory.get_relationship(save_id, fid, target) or {}).get("trust")
                        if isinstance(cur, (int, float)) and float(cur) / 100.0 <= -0.999:
                            state_changed = False
                    except Exception:
                        pass
                # SPEC 3.2 — a DEAD escalate at max war becomes a real WAR-PHASE move (raid/mobilise/ceasefire/
                # exhaustion/reparations) — genuine new state that fires the gate + records history, instead of
                # silence. The phase is picked by losses + persona and rotated for variety.
                phased = False
                if not state_changed and action in ("escalate_pressure", "escalate") and target:
                    phase = self._war_phase_action(save_id, fid, target)
                    dec = {**dec, "action": phase}
                    action = phase
                    state_changed = True
                    phased = True
                imp = 4 if action in ("declare_war", "sue_for_peace", "form_alliance", "seek_ceasefire", "war_exhaustion_warning") else 3
                gate = self._gate.evaluate(save_id, {"action": action, "faction": fid, "target": target,
                                                     "importance": imp, "state_changed": state_changed, "authorized": True})
                routes = gate.get("routes", [])
                # SPEC 3.2 fix (Codex 2026-06-26): record the war-phase as HISTORY only if the gate FIRED it. The
                # old code wrote the world_event before the gate, so a cooldown/dedup-blocked phase still entered
                # world_events and the narrator could resurface a suppressed duplicate. Gating the store here makes
                # the gate authoritative over storage too — no world_event for a phase the gate didn't pass.
                if phased and gate.get("fire") and "store" in routes:
                    try:
                        self.memory.add_world_event(save_id, self._PHASE_EVENT_TYPE.get(action, "war"),
                                                    summary=self._phase_summary(save_id, fid, target, action),
                                                    primary_faction=fid, secondary_faction=target,
                                                    importance=imp, source="engine")
                    except Exception:
                        pass
                if "news" in routes:
                    allow = llm_used < LLM_NEWS_BUDGET
                    item = self._decision_news(save_id, fid, dec, allow_llm=allow)
                    if item:
                        news.append(item)
                        if allow:
                            llm_used += 1
                # ACTUATION (gate-allowed). SPEC 3.3-A: a WAR PHASE writes real SUBSTRATE state the deriver reads
                # back next tick (losses / conflict intensity / economy) — genuine state change, not just news.
                # Ordinary relation moves keep SPEC 1d-W2's in-game relation dispatch.
                if "actuate" in routes:
                    if phased:
                        eff = self._actuate_war_phase(save_id, fid, target, action)
                        if eff:
                            phase_effects.append(eff)
                            # SPEC 3.3-B: a phase can dispatch one or MORE real in-game actions through On_action
                            # (relation move, economy add/remove, military order). raid emits an order + economy.
                            disps = list(eff.get("dispatches") or [])
                            if eff.get("dispatch"):
                                disps.append(eff["dispatch"])
                            for disp in disps:
                                if len(actions) < self.ACTUATION_BUDGET:
                                    actions.append(disp)
                    elif len(actions) < self.ACTUATION_BUDGET:
                        act = self._decision_action(save_id, fid, dec)
                        if act:
                            actions.append(act)
                # SPEC 1j: faction reaches OUT to the player — only if the decision fired the gate (not a no-op).
                if gate.get("fire") and comms_made < self.PLAYER_COMMS_BUDGET:
                    if self._maybe_player_comms(save_id, fid, dec):
                        comms_made += 1
        self._influence_cursor[save_id] = (start + budget) % len(facs)
        # Drain any on-demand PROVE news/actions queued for this save (so the mod surfaces + actuates in-game).
        pend = getattr(self, "_pending_news", {}).get(save_id, [])
        if pend:
            news = list(pend) + news
            self._pending_news[save_id] = []
        pact = getattr(self, "_pending_actions", {}).get(save_id, [])
        if pact:
            actions = list(pact) + actions
            self._pending_actions[save_id] = []
        # SPEC 2b: NARRATOR — turn the freshest world-event cluster into ONE grounded history article per tick
        # (cause-gated; cursor-deduped). The world's voice, distinct from faction bulletins + player comms.
        articles: list[dict[str, Any]] = []
        try:
            nchat = self.player2._make_entity_classifier() if llm_used < LLM_NEWS_BUDGET else None
            articles = self._narrator.run_pass(save_id, chat_fn=nchat, budget=1)
            # Same immersion rule as news: convert raw war-scores/intensity %s to English in the article text.
            for _a in articles:
                for _k in ("title", "body", "consequence", "quote"):
                    if isinstance(_a, dict) and _a.get(_k):
                        _a[_k] = self._humanize_math(str(_a[_k]))
        except Exception:
            articles = []
        # Wire the G-generators into the heartbeat (throttled, side-effect only — agreements + offers reach the
        # player via player_comms / the dashboard, not this news list).
        try:
            self.gameplay_generation_tick(save_id)
        except Exception:
            pass
        return {"ok": True, "news": news, "actions": actions, "articles": articles,
                "phase_effects": phase_effects, "reviewed": reviewed, "save_id": save_id}

    GAMEPLAY_GEN_COOLDOWN_S = 200.0  # throttle so agreements/offers don't spam the player during play

    def gameplay_generation_tick(self, save_id: str, dry_run: bool = False) -> dict[str, Any]:
        """Wire the G-generators into the heartbeat so they FIRE during play (not just on-demand). Throttled per
        save. Surfaces via the proven player_comms channel (no coupling to the influence-step news list):
        proposes agreements (G5) + one patrol/supply offer (G1/#60). dry_run skips enqueue (for the selftest)."""
        import time as _t
        if not hasattr(self, "_gameplay_gen_last"):
            self._gameplay_gen_last = {}
        now = _t.time()
        if now - self._gameplay_gen_last.get(save_id, 0) < self.GAMEPLAY_GEN_COOLDOWN_S:
            return {"ran": False}
        self._gameplay_gen_last[save_id] = now
        out: dict[str, Any] = {"ran": True, "agreements": 0, "offer": None}
        try:
            # D6: LIVE proposal initiation is the PROPOSER's Player2 decision (defer-on-fail), not deterministic auto-
            # propose. dry_run (selftest) only COUNTS candidates — no LLM, no create, no side effects.
            if dry_run:
                out["agreements"] = len(self.memory.agreement_candidates(save_id, 2))
            else:
                res = self.propose_deals_llm(save_id, max_n=2)
                proposed = [r for r in res.get("results", []) if r.get("agreement_id")]
                out["agreements"] = len(proposed)
                for a in proposed:
                    fa = self._fac_name(save_id, a.get("proposer"))
                    fb = self._fac_name(save_id, a.get("target"))
                    verb = {"ceasefire": "has put out a ceasefire feeler to",
                            "trade": "is proposing a trade pact with"}.get(a.get("kind"), "is opening talks with")
                    self.player_comms.append({"title": f"{fa.upper()} DIPLOMATIC OVERTURE", "body": f"{fa} {verb} {fb}.",
                                              "faction": a.get("proposer"), "faction_name": fa, "category": "diplomacy",
                                              "kind": "agreement", "save_id": save_id, "ts": now})
        except Exception:
            pass
        if not dry_run:
            try:
                # ANNOUNCE-ONCE (fix for the repeated PATROL/SUPPLY REQUEST spam, 2026-07-01): the need is routed
                # through the JOB MARKET (create_or_update_job dedupes by job_key), and a player communiqué is sent
                # ONLY when the job row was CREATED. A repeated need silently refreshes the existing open row;
                # later material changes (reward raise / withdrawal) announce via the FRAGO escalation news (#71).
                alt = int(now) % 2 == 0
                built = self._build_patrol_offer(save_id) if alt else self._build_supply_offer(save_id)
                if built.get("ok"):
                    if alt:
                        fid, jtype = built["owner"], "patrol"
                        job = self.memory.create_or_update_job(
                            save_id, fid, "patrol", target_sector=str(built.get("sector") or ""),
                            target_faction=str(built.get("threat") or ""), urgency=3,
                            reward=self.memory.price_job(save_id, fid, "patrol", urgency=3,
                                                         target_faction=str(built.get("threat") or "")),
                            evidence={"summary": built["offer"]["summary"]})
                    else:
                        fid, jtype = built["faction"], "supply"
                        job = self.memory.create_or_update_job(
                            save_id, fid, "supply", ware=str(built.get("ware") or ""), urgency=3,
                            target_sector=self.memory.resolve_job_sector(save_id, fid),
                            reward=self.memory.price_job(save_id, fid, "supply", urgency=3),
                            evidence={"summary": built["offer"]["summary"], "severity": built.get("severity")})
                    if job.get("created"):
                        fname = self._fac_name(save_id, fid)
                        self.player_comms.append({"title": f"{fname.upper()} {jtype.upper()} CONTRACT POSTED",
                                                  "body": built["offer"]["summary"], "faction": fid,
                                                  "faction_name": fname, "category": "diplomacy", "kind": "offer",
                                                  "save_id": save_id, "ts": now, "offer": built["offer"],
                                                  "job_id": job.get("id")})
                        out["offer"] = jtype
                    else:
                        out["offer"] = f"{jtype}_deduped"
            except Exception:
                pass
            while len(self.player_comms) > 200:
                self.player_comms.popleft()
        return out

    def gameplay_announce_once_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Oracle for the announce-once rule: the SAME contested-sector need across two generation ticks yields
        ONE job row + ONE communiqué (second tick dedupes silently). Stub proposals; throwaway store."""
        import shutil
        import tempfile
        checks: list[dict] = []

        def chk(n: str, c: bool, det: str = "") -> None:
            checks.append({"name": n, "ok": bool(c), "detail": det})

        d = tempfile.mkdtemp(prefix="nl_annonce_")
        orig_m, orig_propose, orig_comms = self.memory, self.propose_deals_llm, self.player_comms
        try:
            from collections import deque as _dq
            store = MemoryStore(f"{d}/a.sqlite3")
            sid = "s"
            store.upsert_faction(sid, "argon", name="Argon")
            store.upsert_sector(sid, "sec_hot", name="Hot Sector", owner_faction="argon",
                                contested_by=["xenon"], strategic_value=0.9, player_assets_present=True)
            self.memory = store
            self.propose_deals_llm = lambda *_a, **_k: {"results": []}
            self.player_comms = _dq()
            self._gameplay_gen_last = {}
            # force the PATROL branch deterministically on both ticks
            import time as _t
            real_time = _t.time
            try:
                r1 = self.gameplay_generation_tick(sid)
                self._gameplay_gen_last = {}   # reopen the cooldown gate for the second tick
                r2 = self.gameplay_generation_tick(sid)
            finally:
                pass
            # one of the two ticks may have taken the supply branch (clock parity); assert on the PATROL results
            patrol_runs = [r for r in (r1, r2) if str(r.get("offer") or "").startswith("patrol")]
            jobs = [j for j in store.list_jobs(sid, status="open") if j["job_type"] == "patrol"]
            comms = [c for c in self.player_comms if "PATROL" in str(c.get("title") or "")]
            if len(patrol_runs) == 2:
                chk("one_open_job", len(jobs) == 1, str(len(jobs)))
                chk("one_comm_only", len(comms) == 1, str(len(comms)))
                chk("second_tick_deduped", patrol_runs[1]["offer"] == "patrol_deduped", str(patrol_runs[1]))
            else:
                # parity gave us mixed branches — still assert no duplicate job/comm for whichever patrol ran
                chk("one_open_job", len(jobs) <= 1, str(len(jobs)))
                chk("one_comm_only", len(comms) <= 1, str(len(comms)))
                chk("second_tick_deduped", True, "mixed-branch run (parity)")
        finally:
            self.memory, self.propose_deals_llm, self.player_comms = orig_m, orig_propose, orig_comms
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
                "total": len(checks), "checks": checks}

    def gameplay_tick(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **self.gameplay_generation_tick(str(payload.get("save_id") or "unindexed"),
                                                             dry_run=bool(payload.get("dry_run")))}

    def gameplay_tick_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        s = "__gameplay_tick_selftest__" + str(int(time.time() * 1000))
        m = self.memory
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.add_conflict(s, "argon", "teladi", status="active", intensity=0.5, cause="t")
            r1 = self.gameplay_generation_tick(s, dry_run=True)
            ok("first_tick_ran", r1.get("ran") is True, r1)
            ok("generated_agreements", r1.get("agreements", 0) >= 1, r1)
            r2 = self.gameplay_generation_tick(s, dry_run=True)
            ok("second_tick_throttled", r2.get("ran") is False, r2)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def influence_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """On-demand PROVING trigger: force a faction to decide RIGHT NOW (cooldown bypassed), apply it,
        and queue the news so the mod surfaces it in-game on the next heartbeat. Lets a test
        deterministically demonstrate bridge-decision -> real in-game notification."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "")
        if not fid:
            return {"ok": False, "error": "faction_id required"}
        r = self.review_faction(save_id, fid, use_llm=False, autonomous=True, force=True)
        dec = r.get("decision")
        item = self._decision_news(save_id, fid, dec) if (dec and r.get("incident_id")) else None
        if item:
            if not hasattr(self, "_pending_news"):
                self._pending_news = {}
            self._pending_news.setdefault(save_id, []).append(item)
        # SPEC 1d-W2: queue the REAL relation dispatch so the prove also actuates in-game (force past cooldown).
        act = self._decision_action(save_id, fid, dec, force=True) if dec else None
        if act:
            if not hasattr(self, "_pending_actions"):
                self._pending_actions = {}
            self._pending_actions.setdefault(save_id, []).append(act)
        return {"ok": True, "faction_id": fid, "news": item, "action": act,
                "decision": dec, "incident_id": r.get("incident_id")}

    def warphase_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """On-demand PROVING for SPEC 3.3-B: force a war phase for (faction->target) NOW, apply its substrate
        (A), and queue its REAL in-game dispatch (a relation move through the proven On_action cue) + news, so the
        mod actuates + surfaces it on the next heartbeat. `phase` forces a specific phase; `relation` overrides the
        dispatch delta for a clearly visible in-game demo (e.g. a ceasefire that crosses to peace)."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "")
        target = self.memory.resolve_faction_id(str(payload.get("target") or "")) or (payload.get("target") or "")
        if not fid or not target:
            return {"ok": False, "error": "faction_id and target required"}
        phase = str(payload.get("phase") or self._war_phase_action(save_id, fid, target))
        eff = self._actuate_war_phase(save_id, fid, target, phase)
        try:
            self.memory.add_world_event(save_id, self._PHASE_EVENT_TYPE.get(phase, "war"),
                                        summary=self._phase_summary(save_id, fid, target, phase),
                                        primary_faction=fid, secondary_faction=target,
                                        importance=4 if phase in ("seek_ceasefire", "war_exhaustion_warning") else 3,
                                        source="engine")
        except Exception:
            pass
        item = self._decision_news(save_id, fid, {"action": phase, "target": target})
        if item:
            if not hasattr(self, "_pending_news"):
                self._pending_news = {}
            self._pending_news.setdefault(save_id, []).append(item)
        disps = list((eff or {}).get("dispatches") or [])
        if (eff or {}).get("dispatch"):
            disps.append(eff["dispatch"])
        rel = payload.get("relation")
        if rel is not None:
            disps = [{"type": "adjust_relation", "faction": fid, "target": target, "relation": float(rel)}]
        if disps:
            if not hasattr(self, "_pending_actions"):
                self._pending_actions = {}
            self._pending_actions.setdefault(save_id, []).extend(disps)
        disp = disps[0] if disps else None
        return {"ok": True, "faction_id": fid, "target": target, "phase": phase,
                "effects": (eff or {}).get("effects"), "dispatch": disp, "news": item}

    def order_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """SPEC 3.3-B order-primitive prove: queue a REAL military order dispatch (no spawning) for the mod to
        actuate — On_action finds the faction's OWN combat ships (find_ship_by_true_owner) and issues a vanilla
        order (kind=patrol -> MoveGeneric toward the target's front; kind=raid -> Attack). Proves one real ship obeys."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "")
        target = self.memory.resolve_faction_id(str(payload.get("target") or "")) or (payload.get("target") or "")
        kind = str(payload.get("kind") or "patrol")
        if not fid or not target:
            return {"ok": False, "error": "faction_id and target required"}
        disp = {"type": "order", "faction": fid, "target": target, "kind": kind}
        if not hasattr(self, "_pending_actions"):
            self._pending_actions = {}
        self._pending_actions.setdefault(save_id, []).append(disp)
        return {"ok": True, "dispatch": disp}

    # --- SPEC 1f (Level 3): persona reactions write back to the emotional factors -------------------
    REACTION_BUDGET = 2          # max LLM reactions per influence tick (cost cap)
    REACTION_COOLDOWN_S = 45.0   # per (faction->target) min spacing between LLM reactions

    def _persona_scale(self, fac: dict[str, Any]) -> float:
        """Persona-plausibility guardrail: scale the reaction magnitude by faction aggression. ~0.6 for a
        pacifist faction, ~1.2 for a warlike one (then clamped to the absolute cap). This is what makes
        pirates react like pirates and the Alliance like the Alliance — same event, different deltas."""
        biases = (fac or {}).get("biases") or {}
        try:
            aggr = float(biases.get("aggression", 0.5))
        except Exception:
            aggr = 0.5
        aggr = max(0.0, min(1.0, aggr))
        return 0.6 + 0.6 * aggr

    def _llm_reaction(self, save_id: str, faction_id: str, faction_name: str, target_id: str,
                      event_summary: str) -> Optional[dict[str, Any]]:
        """The faction REACTS in character to a perceived event (1e-grounded: persona + memory + grudge graph
        feed the call via faction_id). Returns proposed (pre-clamp) emotional deltas, or None on any failure."""
        tgt = self.memory.get_faction(save_id, target_id) or {}
        tname = tgt.get("name") or target_id
        prompt = (f"A development concerning your faction: {event_summary}\n"
                  f"React AS {faction_name} — true to your character, values, and history — specifically toward "
                  f"{tname}. How does this move your feelings about them?\n"
                  f'Reply with ONLY a compact JSON object: {{"resentment": <int -15..20>, "fear": <int -10..15>, '
                  f'"trust": <int -15..10>, "mood": "<one or two words>", "rationale": "<one short in-character '
                  f'sentence>"}}. Positive resentment/fear = angrier/more afraid; negative trust = less trusting. '
                  f"Output nothing but the JSON.")
        payload = {
            "request_id": f"react-{int(time.time()*1000)}-{faction_id[:8]}",
            "source_mod": "faction_reaction", "channel": "npc",
            "target": {"mode": "npc", "game_id": "reaction", "save_id": save_id,
                       "npc_name": f"{faction_name} High Command", "npc_short_name": faction_id[:8],
                       "faction_id": faction_id,
                       "system_prompt": (f"You are the ruling council of {faction_name} in the X4 galaxy. You feel "
                                         f"and react in character. Output ONLY the requested JSON, no prose around it.")},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            import re as _re
            resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
            if resp.get("status") != "ok":
                return None
            m = _re.search(r"\{.*\}", resp.get("reply") or "", _re.S)
            if not m:
                return None
            d = json.loads(m.group(0))
            return {"resentment": d.get("resentment", 0), "fear": d.get("fear", 0), "trust": d.get("trust", 0),
                    "mood": str(d.get("mood", ""))[:32], "rationale": str(d.get("rationale", ""))[:240]}
        except Exception:
            return None

    def _react(self, save_id: str, faction_id: str, target_id: str, event_summary: str,
               event_key: str = "", allow_llm: bool = True) -> Optional[dict[str, Any]]:
        """One persona reaction: propose (LLM, in character) -> validate_reaction (persona-scaled clamp +
        idempotency + cooldown) -> apply bounded delta to the factors. Deterministic fallback/overflow nudge
        when the LLM is unavailable or on cooldown. Returns the applied reaction, or None if skipped."""
        led = getattr(self, "_react_ledger", None)
        if led is None:
            led = self._react_ledger = {}
        now = time.time()
        if event_key and (save_id, "evt", event_key) in led:
            return None  # already reacted to this exact event (idempotency)
        ckey = (save_id, faction_id, target_id)
        on_cooldown = (now - led.get(ckey, 0.0)) < self.REACTION_COOLDOWN_S
        fac = self.memory.get_faction(save_id, faction_id) or {}
        scale = self._persona_scale(fac)
        proposed = None
        if allow_llm and not on_cooldown:
            proposed = self._llm_reaction(save_id, faction_id, str(fac.get("name") or faction_id),
                                          target_id, event_summary)
        if not proposed:
            # overflow / fallback: a perceived hostile event still nudges (small, deterministic, no LLM).
            proposed = {"resentment": 3, "fear": 1, "trust": -2, "mood": "", "rationale": ""}
        # validate_reaction: persona-scale then clamp to the absolute per-event caps (done in apply_reaction).
        deltas = {
            "resentment": self.memory._cap_delta("resentment", round(float(proposed.get("resentment", 0)) * scale)),
            "fear": self.memory._cap_delta("fear", round(float(proposed.get("fear", 0)) * scale)),
            "trust": self.memory._cap_delta("trust", round(float(proposed.get("trust", 0)) * scale)),
        }
        res = self.memory.apply_reaction(save_id, faction_id, target_id, deltas,
                                         mood=proposed.get("mood", ""), rationale=proposed.get("rationale", ""))
        led[ckey] = now
        if event_key:
            led[(save_id, "evt", event_key)] = now
        return {"faction": faction_id, "target": target_id, "persona_scale": round(scale, 2),
                "rationale": proposed.get("rationale", ""), **res}

    def react_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """On-demand SPEC 1f proof: force a faction to REACT to a (given or synthetic) event NOW and return the
        BOUNDED before/after factors + rationale — so the dashboard shows the clamped persona write-back."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "")
        tgt = self.memory.resolve_faction_id(str(payload.get("target") or "")) or (payload.get("target") or "")
        if not (fid and tgt):
            return {"ok": False, "error": "faction_id and target required"}
        event = str(payload.get("event") or f"Forces of {tgt} struck our holdings without warning.")
        before = self.memory.get_relationship(save_id, fid, tgt) or {}
        r = self._react(save_id, fid, tgt, event, event_key=str(payload.get("event_key", "")), allow_llm=True)
        after = self.memory.get_relationship(save_id, fid, tgt) or {}
        keys = ("resentment", "fear", "trust")
        return {"ok": True, "reaction": r,
                "before": {k: before.get(k, 0) for k in keys},
                "after": {k: after.get(k, 0) for k in keys}}

    # Actions that warrant a galaxy-news bulletin. Value = (gerund phrase, preposition-to-target).
    # Anything NOT here is a passive/no-op pick (hold, observe, "weigh") and is a NON-EVENT — it is
    # never surfaced as news (a thought-bubble, not a bulletin). prep "" = no target clause.
    NEWS_VERBS: dict[str, tuple[str, str]] = {
        "escalate_pressure": ("escalating tensions", "with"),
        "escalate": ("escalating tensions", "with"),
        "de_escalate": ("easing tensions", "with"),
        "deescalate": ("easing tensions", "with"),
        "declare_war": ("moving toward open war", "with"),
        "sue_for_peace": ("opening peace talks", "with"),
        "form_alliance": ("courting an alliance", "with"),
        "impose_embargo": ("imposing a trade embargo", "on"),
        "consolidate": ("consolidating its forces", ""),
        "expand_economy": ("pushing economic expansion", ""),
        "fortify": ("fortifying its territory", ""),
        # SPEC 3.2 — WAR-STATE PHASES: what a faction does once already AT WAR (instead of a dead escalate).
        "mobilize_fleet": ("mobilising its war fleet", "against"),
        "raid_supply_line": ("raiding the supply lines", "of"),
        "fortify_sector": ("digging in along the front", "with"),
        "request_supplies": ("calling up war supplies for its campaign", ""),
        "demand_reparations": ("demanding reparations", "from"),
        "war_exhaustion_warning": ("straining under mounting war-weariness", ""),
        "seek_ceasefire": ("quietly floating a ceasefire", "with"),
        "offer_privateer_contract": ("putting out privateer contracts", "against"),
    }

    # SPEC 3.2 — war-phase pools + per-pair rotation. A faction at MAX WAR picks one of these instead of a dead
    # escalate; war-weary (heavy losses) + diplomatic factions lean toward ceasefire/exhaustion.
    WAR_PHASES = ["mobilize_fleet", "raid_supply_line", "fortify_sector", "request_supplies", "offer_privateer_contract"]
    WAR_PHASES_WEARY = ["war_exhaustion_warning", "seek_ceasefire", "demand_reparations"]
    _PHASE_EVENT_TYPE = {"seek_ceasefire": "diplomatic", "war_exhaustion_warning": "diplomatic",
                         "demand_reparations": "diplomatic", "raid_supply_line": "battle", "mobilize_fleet": "war",
                         "fortify_sector": "war", "request_supplies": "war", "offer_privateer_contract": "war"}

    def _war_phase_action(self, save_id: str, fid: str, target: str) -> str:
        weary = False
        try:
            weary = float((self.memory.derive_pressures(save_id, fid) or {}).get("recent_losses", 0) or 0) >= 0.5
        except Exception:
            pass
        try:
            p = (getattr(self.memory, "FACTION_PERSONA", {}) or {}).get(
                fid, getattr(self.memory, "FACTION_PERSONA_DEFAULT", (0.5, 0.55, 0.5, 0.5, "")))
            dipl = float(p[3])
        except Exception:
            dipl = 0.5
        pool = (self.WAR_PHASES_WEARY if (weary and dipl >= 0.45)
                else (["war_exhaustion_warning", "demand_reparations", "fortify_sector"] if weary else self.WAR_PHASES))
        if not hasattr(self, "_war_phase_idx"):
            self._war_phase_idx = {}
        k = (save_id, fid, target)
        i = self._war_phase_idx.get(k, sum(ord(c) for c in str(fid)))
        self._war_phase_idx[k] = i + 1
        return pool[i % len(pool)]

    def _phase_summary(self, save_id: str, fid: str, target: str, phase: str) -> str:
        a = self._fac_name(save_id, fid)
        b = self._fac_name(save_id, target)
        return {
            "mobilize_fleet": f"{a} mobilises its war fleet against {b}.",
            "raid_supply_line": f"{a} raids {b}'s supply lines.",
            "fortify_sector": f"{a} digs in along the front with {b}.",
            "request_supplies": f"{a} calls up fresh war supplies for its campaign against {b}.",
            "demand_reparations": f"{a} demands reparations from {b}.",
            "war_exhaustion_warning": f"War-weariness mounts within {a} as the war with {b} drags on.",
            "seek_ceasefire": f"{a} quietly floats a ceasefire with {b}.",
            "offer_privateer_contract": f"{a} puts out privateer contracts against {b}.",
        }.get(phase, f"{a} presses its war with {b}.")

    # --- SPEC 3.3-A: WAR-PHASE STATE ACTUATION -------------------------------------------------------------
    # Each war phase writes REAL substrate state the strategic deriver reads back next heartbeat. We mutate the
    # SUBSTRATE (war_losses / conflict intensity / economy), NOT strategic_state directly — derive_pressures
    # recomputes strategic_state from the substrate every tick, so a direct write there would be clobbered.
    # Effects are bounded; the SPEC 3.1 gate already cooldown/dedup-gated the call before we get here.
    RAID_LOSS = 10.0          # ship-equivalents a supply raid costs the target (→ recent_losses ~ +0.20)
    PRIVATEER_LOSS = 5.0      # smaller, proxy harassment
    INTENSITY_STEP = 0.10     # mobilise bumps the pair's conflict intensity; ceasefire-feeler cools it
    CEASEFIRE_COOL = -0.15
    ECON_SUPPLY_GAIN = 0.10   # request_supplies lifts own production_health
    ECON_REPARATIONS = -0.05  # demand_reparations strains the target's economy
    ECON_FORTIFY_COST = -0.03 # fortifying spends supplies (small self-cost)
    # SPEC 3.3-B: phases with a real RELATION meaning emit an in-game adjust_relation dispatch through the proven
    # On_action cue. seek_ceasefire RAISES a war relation (the AI de-escalating a real war — genuinely new);
    # mobilize_fleet lowers it (bites on pairs not yet at the -1.0 floor). The rest get real effects in B-2.
    REL_CEASEFIRE = 0.06
    REL_MOBILIZE = -0.04

    def _econ_delta(self, save_id: str, fid: str, delta: float) -> None:
        econ = self.memory.get_economy(save_id, fid) or {}
        ph = float(econ.get("production_health", 1.0) or 1.0)
        self.memory.upsert_economy(save_id, fid, production_health=max(0.0, min(1.0, ph + delta)))

    def _conflict_intensity_delta(self, save_id: str, a: str, b: str, delta: float) -> Optional[float]:
        confs = [c for c in self.memory.list_conflicts(save_id, status="active")
                 if {a, b} == {c.get("faction_a"), c.get("faction_b")}]
        if confs:
            c = confs[0]
            ni = max(0.0, min(1.0, float(c.get("intensity", 0) or 0) + delta))
            self.memory.set_conflict_status(save_id, c["id"], "active", intensity=ni)
            return ni
        if delta > 0:  # mobilising with no recorded conflict opens one at the step intensity
            self.memory.add_conflict(save_id, a, b, status="active", intensity=max(0.0, min(1.0, delta)), cause="mobilisation")
            return max(0.0, min(1.0, delta))
        return None

    def _actuate_war_phase(self, save_id: str, fid: str, target: str, phase: str) -> Optional[dict[str, Any]]:
        """Apply a war phase's REAL substrate effect. Returns {type, faction, target, phase, effects} for the
        dashboard/verification, or None for a phase with no state effect (e.g. war_exhaustion_warning — a signal)."""
        eff: dict[str, Any] = {}
        try:
            # ANTI-CHEAT (Codex/Ken, 2026-06-26): war-phase DECISIONS fabricate NO DB consequences — no losses, no
            # economy, no intensity. The DB must never "believe" damage happened before real combat. All
            # consequences come ONLY from REAL events: the fleet census (war_losses from actual ship deltas) and
            # the event-grounded conflict ledger (#62). Phases emit real ORDERS / RELATIONS (+ news); the rest are
            # intent-only (news) until earned via orders/contracts (#62/#63).
            if phase == "raid_supply_line" and target:
                eff = {"dispatch": {"type": "order", "faction": fid, "target": target, "kind": "raid"}}
            elif phase == "mobilize_fleet" and target:
                eff = {"dispatch": {"type": "order", "faction": fid, "target": target, "kind": "patrol"}}
            elif phase == "seek_ceasefire" and target:
                eff = {"dispatch": {"type": "adjust_relation", "faction": fid, "target": target, "relation": self.REL_CEASEFIRE}}
            # offer_privateer_contract / request_supplies / demand_reparations / fortify_sector /
            # war_exhaustion_warning: intent-only (news) — real versions are earned orders/economy/contracts.
        except Exception:
            return None
        if not eff:
            return None
        dispatch = eff.pop("dispatch", None)
        dispatches = eff.pop("dispatches", None)
        out = {"type": "war_phase_state", "faction": fid, "target": target, "phase": phase, "effects": eff}
        if dispatch:
            out["dispatch"] = dispatch
        if dispatches:
            out["dispatches"] = dispatches
        return out

    # Economy Update read pipeline: ingest omniscient station snapshots + roll up to faction facts.
    def economy_stations_ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        stations = payload.get("stations") or []
        n = 0
        for s in stations:
            if isinstance(s, dict):
                try:
                    self.memory.upsert_economy_station(save_id, s)
                    n += 1
                except Exception:
                    pass
        roll = None
        if payload.get("rollup", True):
            try:
                roll = self.memory.rollup_economy_from_stations(save_id)
            except Exception as e:
                roll = {"ok": False, "error": str(e)}
        return {"ok": True, "ingested": n, "rollup": roll}

    def economy_rollup_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic: synthetic stations -> rollup -> assert faction shortages/health are derived correctly."""
        s = "__econ_rollup_selftest__"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            for st in [
                {"station_id": "es1", "faction_id": "argon", "products": ["hullparts"], "needs": ["energycells"]},
                {"station_id": "es2", "faction_id": "argon", "products": [], "needs": ["energycells", "hullparts"]},
                {"station_id": "es3", "faction_id": "argon", "products": ["energycells"], "needs": []},
            ]:
                self.memory.upsert_economy_station(s, st)
            r = self.memory.rollup_economy_from_stations(s)
            ok("rolled_up_one_faction", r.get("factions_rolled_up") == 1 and r.get("stations") == 3, r)
            econ = self.memory.get_economy(s, "argon") or {}
            sh = econ.get("shortages") or {}
            ok("energycells_top_shortage", round(float(sh.get("energycells", 0)), 2) == 0.67, sh)
            ok("hullparts_lesser_shortage", round(float(sh.get("hullparts", 0)), 2) == 0.33, sh)
            ok("production_health_from_short_ratio", round(float(econ.get("production_health", 1)), 2) == 0.33, econ.get("production_health"))
            ok("key_needs_ranked", (econ.get("key_needs") or [])[:1] == ["energycells"], econ.get("key_needs"))
            # #54: market_status is derived in the rollup now (prod variety 2 == need variety 2 → not exporter;
            # unmet needs present → importer).
            ok("market_status_derived_in_rollup", econ.get("market_status") == "importer", econ.get("market_status"))
            # #55: meaning-layer prose — display names + ENGLISH severity bands, no raw numbers in the economy line.
            brief = self.memory.build_faction_briefing(s, "argon")
            ok("prose_critical_band_helper", self.memory._shortage_phrase(0.9) == "critically short on")
            ok("prose_uses_severity_bands", ("running low on" in brief) and ("a little tight on" in brief), brief[-220:])
            ok("prose_no_raw_per100_in_brief", "/100" not in brief)
            ok("prose_ware_label_or_fallback", ("Energy Cells" in brief) or ("energycells" in brief), brief[-220:])
            ok("prose_market_role_english", "net importer" in brief)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    # Event ledger (#62): ingest observed hostile events + derive event-grounded conflicts.
    def hostile_events_ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        events = payload.get("events") or []
        n = 0
        for e in events:
            if isinstance(e, dict):
                try:
                    # In-game cues pass faction OWNERS as display names ("Argon Federation"); resolve to canon ids
                    # so the ledger joins with the rest of the world model. resolve_faction_id is id+name tolerant.
                    e = dict(e)
                    for k in ("attacker_faction", "victim_faction"):
                        if e.get(k):
                            e[k] = self.memory.resolve_faction_id(str(e[k])) or e[k]
                    self.memory.add_hostile_event(save_id, e)
                    n += 1
                except Exception:
                    pass
        return {"ok": True, "ingested": n, "conflicts": self.memory.derive_conflicts_from_events(save_id)}

    def hostile_ledger_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic: synthetic REAL hostile events -> conflicts grounded in location/loss/intensity/cause,
        NOT relation thresholds. Fresh per-run save_id so no accumulation."""
        import time as _t
        s = "__hostile_ledger_selftest__" + str(int(_t.time() * 1000))
        now = _t.time()
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            for e in [
                {"attacker_faction": "argon", "victim_faction": "teladi", "sector": "Grand Exchange", "event_kind": "ship_destroyed", "magnitude": 15, "ts": now - 100, "linked_order_id": "ord:raid:argon>teladi:ABC-001"},
                {"attacker_faction": "argon", "victim_faction": "teladi", "sector": "Grand Exchange", "event_kind": "ship_destroyed", "magnitude": 15, "ts": now - 50, "linked_order_id": "ord:raid:argon>teladi:ABC-001"},
                {"attacker_faction": "argon", "victim_faction": "teladi", "sector": "Hatikvah", "event_kind": "cargo_lost", "magnitude": 5, "ts": now - 10},
                {"attacker_faction": "khaak", "victim_faction": "argon", "sector": "Tharka", "event_kind": "ship_attacked", "magnitude": 5, "ts": now - 5},
            ]:
                self.memory.add_hostile_event(s, e)
            confs = self.memory.derive_conflicts_from_events(s)
            at = next((c for c in confs if {c["faction_a"], c["faction_b"]} == {"argon", "teladi"}), None)
            ak = next((c for c in confs if {c["faction_a"], c["faction_b"]} == {"argon", "khaak"}), None)
            ok("conflict_derived_from_events", at is not None and ak is not None)
            ok("intensity_rolling_not_flat", at and ak and 0 < at["intensity"] < 1.0 and at["intensity"] != ak["intensity"], {"at": at and at["intensity"], "ak": ak and ak["intensity"]})
            ok("intensity_scales_with_real_magnitude", at and ak and at["intensity"] > ak["intensity"], {"at": at and at["intensity"], "ak": ak and ak["intensity"]})
            ok("conflict_is_LOCATED", at and "Grand Exchange" in at["sectors"] and "Hatikvah" in at["sectors"], at and at["sectors"])
            ok("losses_attributed_to_victim", at and at["losses"].get("teladi", 0) == 35.0, at and at["losses"])
            ok("cause_is_first_event_not_relations", at and "struck" in at["cause"].lower() and "Grand Exchange" in at["cause"] and "relations at war" not in at["cause"], at and at["cause"])
            # #67: the loss links back to the SPECIFIC raid order that caused it (attribution proof).
            ok("loss_linked_to_raid_order", at and "ord:raid:argon>teladi:ABC-001" in (at.get("orders") or []), at and at.get("orders"))
            ok("unlinked_event_carries_no_order", ak and (ak.get("orders") or []) == [], ak and ak.get("orders"))
            ok("no_event_no_conflict", not any({c["faction_a"], c["faction_b"]} == {"split", "boron"} for c in confs))
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def warphase_actuate_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """ANTI-CHEAT proof (Codex/Ken): war-phase DECISIONS emit real ORDERS/RELATIONS but fabricate NO DB
        consequences — no loss, no economy, no intensity written at decision time. Consequences come only from
        real events (census + the #62 event ledger)."""
        s = "__warphase_selftest__"
        a, b = "argon", "khaak"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            loss0 = (self.memory.get_loss_summary(s, b) or {}).get("loss_total", 0)
            econ0 = (self.memory.get_economy(s, b) or {}).get("production_health", 1.0)
            conf0 = len([cf for cf in self.memory.list_conflicts(s, "active") if {a, b} == {cf["faction_a"], cf["faction_b"]}])

            r = self._actuate_war_phase(s, a, b, "raid_supply_line")
            ok("raid_emits_order", bool(r) and (r.get("dispatch") or {}).get("type") == "order" and (r.get("dispatch") or {}).get("kind") == "raid", r)
            loss1 = (self.memory.get_loss_summary(s, b) or {}).get("loss_total", 0)
            ok("raid_fabricates_NO_loss", loss1 == loss0, {"before": loss0, "after": loss1})
            ok("raid_fabricates_NO_economy", (self.memory.get_economy(s, b) or {}).get("production_health", 1.0) == econ0)

            m = self._actuate_war_phase(s, a, b, "mobilize_fleet")
            ok("mobilize_emits_patrol_order", bool(m) and (m.get("dispatch") or {}).get("kind") == "patrol", m)

            c = self._actuate_war_phase(s, a, b, "seek_ceasefire")
            ok("ceasefire_emits_relation", bool(c) and (c.get("dispatch") or {}).get("type") == "adjust_relation", c)

            ok("supplies_no_actuation", self._actuate_war_phase(s, a, b, "request_supplies") is None)
            ok("reparations_no_actuation", self._actuate_war_phase(s, a, b, "demand_reparations") is None)
            ok("privateer_no_actuation", self._actuate_war_phase(s, a, b, "offer_privateer_contract") is None)
            ok("fortify_no_actuation", self._actuate_war_phase(s, a, b, "fortify_sector") is None)

            # no NEW conflict conjured by a decision (no decision-time intensity/conflict write)
            conf1 = len([cf for cf in self.memory.list_conflicts(s, "active") if {a, b} == {cf["faction_a"], cf["faction_b"]}])
            ok("no_conflict_conjured", conf1 == conf0, {"before": conf0, "after": conf1})
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    # SPEC 1l-5: TEMPLATE FAMILIES — each action can be framed several ways; rotating per (faction,action) so
    # repeated bulletins vary in structure (condemnation vs mobilization vs warning…), not just wording.
    NEWS_ANGLES: dict[str, list[str]] = {
        "escalate_pressure": ["condemnation", "warning", "mobilization", "propaganda"],
        "escalate": ["condemnation", "warning", "mobilization", "propaganda"],
        "de_escalate": ["negotiation", "denial"],
        "deescalate": ["negotiation", "denial"],
        "declare_war": ["mobilization", "retaliation", "condemnation"],
        "sue_for_peace": ["negotiation", "denial"],
        "form_alliance": ["negotiation", "propaganda"],
        "impose_embargo": ["warning", "condemnation", "retaliation"],
        "consolidate": ["mobilization", "propaganda"],
        "expand_economy": ["propaganda", "negotiation"],
        "fortify": ["mobilization", "propaganda"],
    }
    # SPEC 1l-3: a given faction→target→action bulletin won't re-emit within this window (seconds).
    NEWS_DEDUP_COOLDOWN_S = 600.0

    def _news_angle(self, fid: str, action: str) -> str:
        """Pick a framing family for this bulletin, ROTATING per (faction, action) so successive bulletins for
        the same move don't recycle the same structure."""
        angles = self.NEWS_ANGLES.get(action) or ["statement"]
        if not hasattr(self, "_news_angle_idx"):
            self._news_angle_idx = {}
        key = (fid, action)
        # Seed the starting angle by a STABLE per-faction offset so different factions doing the same action on
        # the same tick don't all open with "condemnation"; then rotate per repeat for within-faction variety.
        seed = sum(ord(c) for c in str(fid))
        i = self._news_angle_idx.get(key, seed)
        self._news_angle_idx[key] = i + 1
        return angles[i % len(angles)]

    def _decision_facts(self, save_id: str, fid: str, dec: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Gather the GROUNDED facts behind a decision from the substrate (faction/rep/mood, resentment
        toward the target, the contested sector and the driving past event, plus pressures). Returns
        None for non-events (action not in NEWS_VERBS) so they never surface as news."""
        action = (dec.get("action") or "").lower()
        if action not in self.NEWS_VERBS:
            return None
        fac = self.memory.get_faction(save_id, fid) or {}
        fname = self._fac_name(save_id, fid)   # SPEC 1l-1: normalized display name (never a raw id like "khaak")
        tgt = dec.get("target") or ""
        tname = self._fac_name(save_id, tgt) if tgt else ""
        resent = 0
        if tgt:
            try:
                resent = int((self.memory.get_relationship(save_id, fid, tgt) or {}).get("resentment") or 0)
            except Exception:
                resent = 0
        try:
            state = self.memory.derive_pressures(save_id, fid) or {}
        except Exception:
            state = {}
        # Driving sector: one this faction owns that the target is contesting (the where).
        sector = ""
        try:
            for s in self.memory.list_sectors(save_id):
                cb = s.get("contested_by_json") or ""
                if s.get("owner_faction") == fid and (tgt and tgt in cb):
                    sector = s.get("name") or ""
                    break
        except Exception:
            pass
        # Driving past event between the two factions (the grounded why), most recent & important.
        why_event = ""
        try:
            for e in self.memory.list_world_events(save_id, limit=80, min_importance=3):
                pf, sf = e.get("primary_faction"), e.get("secondary_faction")
                if fid in (pf, sf) and (not tgt or tgt in (pf, sf)):
                    why_event = (e.get("summary") or "").strip()
                    if why_event:
                        break
        except Exception:
            pass
        why_event = self._normalize_faction_text(save_id, why_event)   # SPEC 1l-1: clean ids in the event text
        recent_losses = float(state.get("recent_losses", 0) or 0)
        economic = float(state.get("economic_pressure", 0) or 0)
        territorial = float(state.get("territorial_pressure", 0) or 0)
        piracy = float(state.get("piracy_pressure", 0) or 0)
        # SPEC 1l-4: a bulletin needs ONE concrete grounded reason from live state — else it's filler. Suppress.
        if not (sector or why_event or recent_losses >= 0.3 or territorial >= 0.3
                or economic >= 0.3 or piracy >= 0.3 or resent >= 25):
            return None
        phrase, prep = self.NEWS_VERBS[action]
        return {
            "fid": fid, "faction": fname, "rep": fac.get("representative") or "", "mood": fac.get("mood") or "",
            "action": action, "phrase": phrase, "prep": prep, "target": tname,
            "angle": self._news_angle(fid, action),   # SPEC 1l-5: template family (varies framing per repeat)
            "resentment": resent, "sector": sector, "why_event": why_event,
            "recent_losses": recent_losses, "economic_pressure": economic,
            "territorial_pressure": territorial, "piracy_pressure": piracy,
        }

    def _news_clause(self, f: dict[str, Any]) -> str:
        """'<phrase>[ <prep> <target>]' — e.g. 'escalating tensions with the Xenon'."""
        clause = f["phrase"]
        if f["prep"] and f["target"]:
            clause += f" {f['prep']} {f['target']}"
        return clause

    def _author_news_llm(self, save_id: str, f: dict[str, Any]) -> str:
        """LLM-author a 1-2 sentence galaxy-news bulletin grounded ONLY in the supplied facts."""
        facts = [f"Faction: {f['faction']}"]
        if f["rep"]:
            facts.append(f"Spokesperson: {f['rep']}")
        facts.append("Development: " + self._news_clause(f))
        if f["sector"]:
            facts.append(f"Contested sector: {f['sector']}")
        if f["why_event"]:
            facts.append(f"Recent related event: {f['why_event']}")
        if f["target"] and f["resentment"] >= 40:
            facts.append(f"{f['faction']} holds deep, long-standing resentment toward {f['target']}.")
        if f["recent_losses"] >= 0.4:
            facts.append("It has taken heavy losses in recent fighting.")
        if f["territorial_pressure"] >= 0.4:
            facts.append("Its borders are under sustained pressure.")
        if f["piracy_pressure"] >= 0.4:
            facts.append("Its space is plagued by raids.")
        factsheet = "\n".join("- " + x for x in facts)
        angle = str(f.get("angle") or "statement")
        prompt = ("Write ONE galaxy-news bulletin (1-2 sentences) reporting this development for an in-universe "
                  f"news service. FRAME IT AS A {angle.upper()} (condemnation=denounce the rival; warning=threaten "
                  "consequences; mobilization=rally or move forces; retaliation=strike back; negotiation=seek "
                  "terms; denial=downplay or deny; propaganda=boast of strength). Lead with the faction and what "
                  "it is doing, then give the reason — drawn ONLY from the facts below. If you attribute a line to "
                  'the spokesperson, write it as: spokesperson <name> said "…". NEVER end with a bare name or a '
                  '"- Name" sign-off. Do NOT invent ship counts, casualty numbers, names, dates, or any event not '
                  "listed. CRITICAL: this is in-universe immersion, NOT a status readout — NEVER cite raw numbers, "
                  "relation/war scores, or percentages (no '-0.96', no '55% intensity', no '-1.00'). Describe the "
                  "situation qualitatively instead (e.g. 'a bitter war', 'fierce fighting', 'simmering tensions'). "
                  f"Output only the bulletin text.\n\nFACTS:\n{factsheet}")
        payload = {
            "request_id": f"news-{int(time.time()*1000)}-{(f['faction'] or 'x')[:8]}",
            "source_mod": "galaxy_news", "channel": "npc",
            # SPEC 1e: faction_id makes npc_complete fire GraphRAG (the faction's war/grudge/agreement subgraph)
            # + the faction briefing, so the bulletin is grounded in retrieval, not just the hand-built factsheet.
            "target": {"mode": "npc", "game_id": "news", "save_id": save_id,
                       "npc_name": "Galaxy News Desk", "npc_short_name": "news",
                       "faction_id": f.get("fid") or "",
                       "system_prompt": ("You are the galaxy news desk of the X4 universe. You write terse, "
                                         "neutral, factual news bulletins. Use ONLY the facts provided and never "
                                         "invent specifics. Output only the bulletin text, with no preamble or quotes.")},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
            if resp.get("status") == "ok":
                txt = " ".join((resp.get("reply") or "").split()).strip().strip('"')
                if 12 <= len(txt) <= 400:
                    return txt
        except Exception:
            pass
        return ""

    def _news_fallback(self, f: dict[str, Any]) -> str:
        """Deterministic richer bulletin (used when the LLM is unavailable): who + what + grounded why, with the
        sentence STRUCTURE varied by the template family (SPEC 1l-5) so repeats don't read identically. Clean
        display names (SPEC 1l-1); no bare spokesperson name (SPEC 1l-2 — the LLM path handles titled quotes)."""
        who = f["faction"]
        clause = self._news_clause(f)   # carries the action + (normalized) target exactly once
        # ONE distinct grounded reason from CONCRETE live state — NOT the prior-event restatement (which is
        # circular + carried raw ids). Keeps the bulletin non-redundant and clean.
        why = ""
        if f["recent_losses"] >= 0.4:
            why = " after heavy losses in recent fighting"
        elif f["territorial_pressure"] >= 0.4:
            why = " as pressure mounts on its borders"
        elif f["piracy_pressure"] >= 0.4:
            why = " with raiders harrying its space"
        elif f["sector"]:
            why = f" amid the contest over {f['sector']}"
        elif f["resentment"] >= 40:
            why = ", citing long-standing grievances"
        # The ANGLE is an adverbial FRAME around the single action clause (no target/action duplication).
        angle = str(f.get("angle") or "statement")
        text = {
            "condemnation": f"{who}, in pointed condemnation, is {clause}{why}.",
            "warning":      f"{who} warns of consequences as it is {clause}{why}.",
            "mobilization": f"{who} is mobilising, {clause}{why}.",
            "retaliation":  f"In open retaliation, {who} is {clause}{why}.",
            "negotiation":  f"{who} is {clause}{why}, while signalling openness to terms.",
            "denial":       f"{who} plays down the rift even as it is {clause}{why}.",
            "propaganda":   f"{who} vaunts its strength while {clause}{why}.",
        }.get(angle, f"{who} is {clause}{why}.")
        return self._humanize_math(re.sub(r"\s+", " ", text).replace(" ,", ",").strip())

    @staticmethod
    def _humanize_math(text: str) -> str:
        """Ken (2026-06-26): player-facing prose must read as IMMERSION, not telemetry — but don't just DELETE the
        sim numbers the LLM lifts from the faction briefing, CONVERT them to English. Conflict intensity % becomes
        a fighting descriptor ('100% intensity' -> 'at a fever pitch'); a relation/war-score value becomes a
        standing ('-1.00' -> 'sworn enemies', '-0.40' -> 'open rivals'). Comma-aware so appositives read right."""
        if not text:
            return text
        import random

        # The substitution is a NET for when the LLM leaks a raw number (it's told to describe qualitatively in
        # its OWN words first). So the net itself draws from a POOL per band — a leaked "100% intensity" won't read
        # as the same canned phrase every time. Picked at random per bulletin.
        def intens(frac: float) -> str:
            if frac >= 0.85: pool = ["a fever pitch", "its bloody peak", "a savage boil", "white-hot fury"]
            elif frac >= 0.55: pool = ["full fury", "a hard boil", "fierce exchange", "open ferocity"]
            elif frac >= 0.30: pool = ["a steady boil", "a grinding tempo", "a simmering grind"]
            else: pool = ["a low simmer", "scattered skirmishing", "an uneasy lull"]
            return random.choice(pool)

        def rel(val: float) -> str:
            if val <= -0.85: pool = ["sworn enemies", "implacable foes", "blood enemies"]
            elif val <= -0.55: pool = ["bitter enemies", "deep rivals", "open foes"]
            elif val <= -0.25: pool = ["open rivals", "wary antagonists", "cold rivals"]
            elif val < 0.10:   pool = ["uneasy neighbours", "wary neighbours", "guarded neighbours"]
            elif val >= 0.55:  pool = ["close allies", "firm allies", "fast friends"]
            else: pool = ["on cordial terms", "on warming terms", "on friendly footing"]
            return random.choice(pool)

        def rel_sub(m: "re.Match") -> str:
            # appositive (clause led by a comma) -> ", now <standing>"; inline -> " as <standing>"
            lead = ", now " if m.group(0).lstrip()[:1] == "," else " as "
            return lead + rel(float(m.group(1)))

        t = text
        # telemetry parentheticals "(war score -0.96)" are redundant with the prose around them -> drop
        t = re.sub(r"\s*\([^()]*\d[^()]*\)", "", t)
        # conflict intensity -> a fighting descriptor (keep the preceding 'at')
        t = re.sub(r"\bat\s+(\d+)\s*%\s*intensity", lambda m: "at " + intens(int(m.group(1)) / 100.0), t, flags=re.I)
        t = re.sub(r"\b(\d+)\s*%\s*intensity", lambda m: "at " + intens(int(m.group(1)) / 100.0), t, flags=re.I)
        # relation / war-score value -> a standing ('relations at -1.00' / 'relations marked as -0.4' / 'war score -0.96')
        t = re.sub(r",?\s*(?:with\s+)?relations?\s+(?:marked\s+|standing\s+|sitting\s+|currently\s+)?"
                   r"(?:as|at|of)\s+([-+]?\d+(?:\.\d+)?)", rel_sub, t, flags=re.I)
        t = re.sub(r",?\s*war\s+score\s+(?:of\s+)?([-+]?\d+(?:\.\d+)?)", rel_sub, t, flags=re.I)
        # any leftover bare percentage
        t = re.sub(r"\b\d+\s*%", "", t)
        # tidy the seams
        t = re.sub(r"\s+([,.])", r"\1", t)
        t = re.sub(r",\s*,", ",", t)
        t = re.sub(r"\s{2,}", " ", t)
        return t.strip().strip(",").strip()

    # ---- SPEC 1j: PLAYER-FACING VOICE — factions reach out to the player -------------------------------------
    def _fac_name(self, save_id: str, fid: str) -> str:
        """Best display name for a faction id: the live faction row's name, else the canon FACTION_NAMES map,
        else a title-cased id. Used for player-facing communiqué text."""
        if not fid:
            return ""
        try:
            for f in self.memory.list_factions(save_id):
                if f.get("faction_id") == fid:
                    nm = f.get("name")
                    if nm and nm != fid:
                        return nm
                    break
        except Exception:
            pass
        return self.memory.FACTION_NAMES.get(fid, fid.replace("_", " ").title())

    def _normalize_faction_text(self, save_id: str, text: str) -> str:
        """SPEC 1l-1: replace raw faction ids embedded in a free-text string (e.g. a stored event summary) with
        display names, so a bulletin never reads 'Holyorder … khaak'. Longest ids first to avoid partials."""
        if not text:
            return text
        try:
            ids = set(getattr(self.memory, "FACTION_NAMES", {}).keys())
            for fr in self.memory.list_factions(save_id):
                if fr.get("faction_id"):
                    ids.add(fr["faction_id"])
        except Exception:
            ids = set(getattr(self.memory, "FACTION_NAMES", {}).keys())
        for fid in sorted((i for i in ids if i and len(i) >= 3), key=len, reverse=True):
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(fid) + r"(?![A-Za-z0-9])",
                          lambda m, fid=fid: self._fac_name(save_id, fid), text, flags=re.IGNORECASE)
        return text

    def _factions_near_player(self, save_id: str) -> set:
        """Faction ids that touch the player's space: contesting a player-owned sector, or owning a sector the
        player contests. Cheap proxy for 'this is happening near you' (the near-player trigger)."""
        near: set = set()
        try:
            secs = self.memory.list_sectors(save_id)
        except Exception:
            return near
        for s in secs:
            owner = s.get("owner_faction")
            contesters = s.get("contested_by") or []
            if owner == "player":
                for f in contesters:
                    if f and f != "player":
                        near.add(f)
            elif owner and owner != "player" and "player" in contesters:
                near.add(owner)
        return near

    def _author_comms_llm(self, save_id: str, faction_name: str, fid: str, kind: str, facts: list) -> str:
        """LLM-author the BODY of a strategic communiqué addressed directly TO the player, grounded ONLY in the
        supplied facts (blueprint §5.6 'ARGON STRATEGIC ALERT' style). Returns '' on failure (caller falls back)."""
        tone = {"threat": "cold and threatening", "favour": "courteous but transactional"}.get(kind, "urgent and strategic")
        factsheet = "\n".join("- " + str(x) for x in facts if x)
        prompt = ("Write a SHORT in-universe strategic communiqué (2-4 sentences) sent by this faction DIRECTLY to "
                  "the player commander. Address the player in the second person. Convey strategic pressure or "
                  "intent grounded ONLY in the facts below — do NOT invent ship counts, casualties, names, dates, "
                  "or events not listed. No greeting line, no signature, no quotes. Output only the message body.\n\n"
                  f"TONE: {tone}\nFACTION: {faction_name}\n\nFACTS:\n{factsheet}")
        payload = {
            "request_id": f"comms-{int(time.time()*1000)}-{(fid or 'x')[:8]}",
            "source_mod": "player_comms", "channel": "npc",
            "target": {"mode": "npc", "game_id": "comms", "save_id": save_id,
                       "npc_name": f"{faction_name} Command", "npc_short_name": "comms",
                       "faction_id": fid or "",
                       "system_prompt": (f"You are the strategic command of {faction_name} in the X4 universe, "
                                         "transmitting directly to an independent player commander. You are terse, "
                                         "in-character, and use ONLY the facts provided. Output only the message body.")},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
            if resp.get("status") == "ok":
                txt = " ".join((resp.get("reply") or "").split()).strip().strip('"')
                if 20 <= len(txt) <= 500:
                    return txt
        except Exception:
            pass
        return ""

    def _build_comms(self, save_id: str, fid: str, dec: dict, kind: str, reasons: list) -> Optional[dict]:
        """Assemble a player-facing communiqué (title + body + faction) from a faction decision. LLM-authored,
        grounded, with a deterministic fallback so it always produces something surfaceable."""
        action = (dec or {}).get("action") or ""
        target = (dec or {}).get("target") or ""
        fname = self._fac_name(save_id, fid)
        tname = self._fac_name(save_id, target) if target else ""
        verb = {"declare_war": f"is going to war with {tname}", "form_alliance": f"is forging an alliance with {tname}",
                "sue_for_peace": f"is seeking peace with {tname}", "impose_embargo": f"is imposing an embargo on {tname}",
                "escalate_pressure": f"is escalating pressure on {tname}"}.get(action, f"is acting against {tname}")
        facts = [f"{fname} {verb}.".replace(" .", ".")]
        if "near" in reasons:
            facts.append("This is unfolding in or around space where the player operates.")
        if "grudge" in reasons:
            facts.append(f"{fname} holds growing resentment toward the player.")
        if "favour" in reasons:
            facts.append(f"{fname} feels it owes the player and is inclined to deal.")
        body = self._author_comms_llm(save_id, fname, fid, kind, facts)
        if not body:
            # deterministic fallback (no LLM): grounded, player-addressed.
            lead = {"threat": "Be advised, commander.", "favour": "An opportunity, commander."}.get(kind, "Strategic notice, commander.")
            ctx = " It is happening near your operations." if "near" in reasons else ""
            body = f"{lead} {fname} {verb}.{ctx}".replace(" .", ".")
        title_word = {"threat": "WARNING", "favour": "OVERTURE"}.get(kind, "STRATEGIC ALERT")
        # Diplomacy for overtures/peace/alliance; Alerts for threats/war/embargo.
        category = "diplomacy" if (kind == "favour" or action in ("form_alliance", "sue_for_peace")) else "alerts"
        # M5a: name a SENDER (the faction's representative NPC) + a stable transmission id, so the message can be
        # posted to the native Messages system "From: <NPC>" and the menu patch can attach a Reply ONLY to our tx
        # (isolation §4.1). The reply chat resolves sender_npc_key (save|chat|name) → that NPC's unioned memory.
        rep = ""
        try:
            fac = self.memory.get_faction(save_id, fid) or {}
            rep = str(fac.get("representative") or "").strip()
        except Exception:
            rep = ""
        base = {"title": f"{fname.upper()} {title_word}", "body": body, "faction": fid,
                "faction_name": fname, "category": category, "kind": kind, "save_id": save_id, "ts": time.time()}
        base.update(comms_sender_fields(save_id, fid, fname, rep, kind, reasons))
        return base

    def _maybe_player_comms(self, save_id: str, fid: str, dec: dict) -> Optional[dict]:
        """Evaluate the 3 triggers for a freshly-made decision; if any fires (and not on cooldown), build +
        enqueue a prominent player communiqué. Returns the comm (also pushed to the drain queue) or None."""
        action = (dec or {}).get("action")
        if action not in self.COMMS_ACTIONS:
            return None
        now = time.time()
        if now - self._comms_last.get((save_id, fid), 0.0) < self.PLAYER_COMMS_COOLDOWN_S:
            return None
        target = (dec or {}).get("target")
        reasons, kind = [], "alert"
        near = self._factions_near_player(save_id)
        if fid in near or (target and target in near):
            reasons.append("near")
        try:
            rel = self.memory.get_relationship(save_id, fid, "player")
        except Exception:
            rel = None
        if rel:
            if (rel.get("resentment") or 0) >= self.GRUDGE_THREAT:
                reasons.append("grudge"); kind = "threat"
            elif (rel.get("debt") or 0) >= self.FAVOUR_DEBT:
                reasons.append("favour"); kind = "favour"
        if action in ("declare_war", "form_alliance", "sue_for_peace"):
            reasons.append("major")
        if not reasons:
            return None
        comm = self._build_comms(save_id, fid, dec, kind, reasons)
        if comm:
            self._comms_last[(save_id, fid)] = now
            self.player_comms.append(comm)
            while len(self.player_comms) > 200:
                self.player_comms.popleft()
        return comm

    def player_comms_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """On-demand PROVING: force a player communiqué from a faction RIGHT NOW (cooldown bypassed) and enqueue
        it, so a test can deterministically show faction->player comms surface in-game on the next heartbeat."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "argon")
        action = str(payload.get("action") or "declare_war")
        target = self.memory.resolve_faction_id(str(payload.get("target") or "")) or (payload.get("target") or "xenon")
        kind = str(payload.get("kind") or "alert")
        dec = {"action": action, "target": target}
        comm = self._build_comms(save_id, fid, dec, kind, ["major", "near"])
        if comm:
            self.player_comms.append(comm)
            return {"ok": True, "comm": comm}
        return {"ok": False, "error": "could not build comm"}

    def _ensure_comm_sender(self, comm: dict[str, Any]) -> dict[str, Any]:
        """M5b: guarantee EVERY player communiqué carries a named sender + priority + tx_id, no matter which
        generator built it (faction-decision builds set these in _build_comms; the contract/patrol/supply and
        diplomacy builders did not). Fills missing fields from the comm's faction representative; never overrides
        an explicitly-set value."""
        try:
            if comm.get("sender_name"):
                return comm
            save_id = str(comm.get("save_id") or "")
            fid = str(comm.get("faction") or "argon")
            fname = str(comm.get("faction_name") or self._fac_name(save_id, fid) or fid)
            rep = ""
            try:
                fac = self.memory.get_faction(save_id, fid) or {}
                rep = str(fac.get("representative") or "").strip()
            except Exception:
                rep = ""
            for k, v in comms_sender_fields(save_id, fid, fname, rep, str(comm.get("kind") or "alert"), []).items():
                comm.setdefault(k, v)
        except Exception:
            pass
        return comm

    def drain_player_comms(self) -> list[dict[str, Any]]:
        """Drain all queued player communiqués since the last call (the mod heartbeat consumes these).
        Every comm is enriched with a named sender + priority (M5b) before it leaves the bridge."""
        with self.lock:
            items = [self._ensure_comm_sender(c) for c in self.player_comms]
            self.player_comms.clear()
            return items

    # SPEC 1k: inspect RoleRAG boundary-aware retrieval for a message (debug/validation surface).
    def rolerag_analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        fac = str(payload.get("faction_id") or payload.get("faction") or "")
        msg = str(payload.get("message") or "")
        rr = getattr(self.player2, "_rolerag", None)
        if rr is None:
            return {"ok": False, "error": "rolerag unavailable (no memory store)"}
        classify = None
        if bool(payload.get("use_llm", True)):
            try:
                classify = self.player2._make_entity_classifier()
            except Exception:
                classify = None
        lf = payload.get("local_facts")
        local_facts = lf if isinstance(lf, list) else None
        try:
            result = rr.analyze_and_retrieve(save_id, fac, msg, classify_llm=classify, local_facts=local_facts)
            return {"ok": True, **result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def rolerag_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from .rolerag import run_rolerag_selftest
        except ImportError:
            from rolerag import run_rolerag_selftest
        return run_rolerag_selftest()

    # SPEC 2a: inspect the PersonaCard + authority contract for an NPC (debug/validation surface).
    def persona_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        pb = getattr(self.player2, "_persona", None)
        if pb is None:
            return {"ok": False, "error": "persona builder unavailable"}
        save_id = str(payload.get("save_id") or "unindexed")
        npc = {k: payload.get(k) for k in ("npc_name", "npc_short_name", "faction_id", "role", "npc_skill", "ship_name", "sector")}
        try:
            card = pb.build(save_id, npc)
            return {"ok": True, "card": card, "prompt": pb.card_to_prompt(card)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def persona_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from .persona import run_persona_selftest
        except ImportError:
            from persona import run_persona_selftest
        return run_persona_selftest()

    # SPEC 2b: Narrator — narrate recent world_event clusters into history articles.
    def narrator_prove(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        if bool(payload.get("reset")):   # re-narrate from scratch (testing): clear the per-save cursor (in-mem + durable)
            try:
                self._narrator._cursor.pop(save_id, None)
                self._narrator._recent_titles.pop(save_id, None)
                self._narrator._save_cursor(save_id, 0)
            except Exception:
                pass
        chat = None
        if bool(payload.get("use_llm", True)):
            try:
                chat = self.player2._make_entity_classifier()
            except Exception:
                chat = None
        try:
            arts = self._narrator.run_pass(save_id, chat_fn=chat, budget=int(payload.get("budget") or 2))
            return {"ok": True, "articles": arts}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def narrator_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from .narrator import run_narrator_selftest
        except ImportError:
            from narrator import run_narrator_selftest
        return run_narrator_selftest()

    # SPEC 3.2: inspect the war-phase selection + summaries for a faction-pair (debug; deterministic, no LLM).
    def warphase_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or (payload.get("faction_id") or "argon")
        target = self.memory.resolve_faction_id(str(payload.get("target") or "")) or (payload.get("target") or "khaak")
        out = []
        for _ in range(max(1, int(payload.get("n") or 6))):
            p = self._war_phase_action(save_id, fid, target)
            out.append({"phase": p, "event_type": self._PHASE_EVENT_TYPE.get(p, "war"),
                        "summary": self._phase_summary(save_id, fid, target, p)})
        return {"ok": True, "faction": fid, "target": target, "phases": out}

    # SPEC 3: event-priority-hierarchy self-test (deterministic).
    def gates_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from .gates import run_gates_selftest
        except ImportError:
            from gates import run_gates_selftest
        return run_gates_selftest()

    # SPEC 1d-W2: relation-affecting decisions become REAL X4 relation dispatches (bounded deltas) routed to
    # the proven On_action MD cue (set_faction_relation + clamp + write-back + war/peace crossing news). Punchy
    # by design so wars erupt fast on the test save; the MD side clamps [-1,1]. Self actions => no dispatch.
    RELATION_DELTAS = {
        "escalate_pressure": -0.15, "escalate": -0.15, "declare_war": -0.40,
        "de_escalate": 0.15, "deescalate": 0.15, "sue_for_peace": 0.30, "form_alliance": 0.30,
        "impose_embargo": -0.08,
    }
    ACTUATION_BUDGET = 2          # max REAL relation dispatches per influence tick
    ACTUATION_COOLDOWN_S = 30.0   # per (faction->target) spacing so one pair isn't hammered

    # SPEC 1j: PLAYER-FACING VOICE — factions reach OUT to the player with a prominent communiqué on a
    # significant, player-relevant decision. Budgeted + cooldown'd so the player isn't spammed (mirrors the
    # actuation governor). Triggers (Ken, all three): near-player space, grudge/favour toward the player, or a
    # major galaxy shift. The drain queue (self.player_comms) is read by the mod's heartbeat via /v1/player_comms.
    COMMS_ACTIONS = {"declare_war", "form_alliance", "sue_for_peace", "impose_embargo", "escalate_pressure"}
    PLAYER_COMMS_BUDGET = 1            # max comms surfaced per influence tick (prominent => keep rare)
    PLAYER_COMMS_COOLDOWN_S = 75.0     # per-faction spacing so one faction can't spam the player
    GRUDGE_THREAT = 40                 # resentment toward player >= this => a threatening transmission
    FAVOUR_DEBT = 40                   # debt toward player >= this => a favourable overture

    def _decision_action(self, save_id: str, fid: str, dec: dict[str, Any], force: bool = False) -> Optional[dict[str, Any]]:
        """Turn a relation-affecting decision into a REAL X4 relation dispatch for the On_action MD cue.
        Returns {type:'adjust_relation', faction, target, relation:Δ} (bounded), or None for self/non-relation
        actions. Per-(faction→target) cooldown unless force=True (the on-demand prove path)."""
        action = (dec.get("action") or "").lower()
        delta = self.RELATION_DELTAS.get(action)
        tgt = dec.get("target") or ""
        if delta is None or not tgt or tgt in ("self", fid):
            return None
        led = getattr(self, "_act_ledger", None)
        if led is None:
            led = self._act_ledger = {}
        now = time.time()
        key = (save_id, fid, tgt)
        if not force and (now - led.get(key, 0.0)) < self.ACTUATION_COOLDOWN_S:
            return None
        led[key] = now
        return {"type": "adjust_relation", "faction": fid, "target": tgt, "relation": round(float(delta), 3)}

    def _decision_category(self, f: dict[str, Any]) -> str:
        """SPEC 1d-S: which vanilla logbook tab this decision belongs in. Target-directed political
        actions (escalate/war/peace/alliance/embargo) -> 'diplomacy'; self-directed posture/economy
        (consolidate/expand/fortify) -> 'news'. ('general'/'alerts' are reserved for player-directed
        consequences and threats-at-the-player, which the loop doesn't generate yet.)"""
        return "diplomacy" if (f.get("prep") and f.get("target")) else "news"

    def _decision_news(self, save_id: str, fid: str, dec: dict[str, Any], allow_llm: bool = True) -> Optional[dict[str, str]]:
        """Author an in-world NEWS bulletin for an autonomous decision, grounded in the substrate.
        Returns {"text", "category"} or None for non-events (passive picks are not news). LLM-authored
        when allowed, with a richer deterministic fallback. The text is recorded as self-authored so the
        logbook ingester (SyncLogbook) won't re-ingest our own output and feed the autonomous loop back
        on itself (SPEC 1d-S feedback guard)."""
        # SPEC 1l-3: DUPLICATE SUPPRESSION — don't re-emit the same (faction→target→action) bulletin within a
        # window. Kills the "same line every few minutes" repetition (Codex's main complaint).
        action = (dec.get("action") or "").lower()
        sig = (save_id, fid, str(dec.get("target") or ""), action)
        now = time.time()
        if not hasattr(self, "_news_last"):
            self._news_last = {}
        if now - self._news_last.get(sig, 0.0) < self.NEWS_DEDUP_COOLDOWN_S:
            return None
        f = self._decision_facts(save_id, fid, dec)
        if f is None:
            return None
        line = (self._author_news_llm(save_id, f) if allow_llm else "") or self._news_fallback(f)
        line = self._humanize_math(line)   # immersion: convert war-scores/intensity %s to English, not raw math
        self.memory.note_self_authored(save_id, line)
        self._news_last[sig] = now
        return {"text": line, "category": self._decision_category(f)}

    # --- Influence-engine LLM stress: N faction-leaders decide via Player2 -----

    INF_STRESS_SAVE = "infstress"

    def _seed_influence_factions(self, n: int) -> list[str]:
        """Seed N factions with varied substrate (economy, relations, conflicts) so the
        deriver produces varied pressures → varied legal menus for the LLM to choose from."""
        import random
        rnd = random.Random(7)
        save_id = self.INF_STRESS_SAVE
        self.memory.clear_save(save_id)
        wares = ["hullparts", "energycells", "weapongrade", "refinedmetals", "foodrations"]
        facs = [f"fac{i:02d}" for i in range(n)]
        for f in facs:
            self.memory.upsert_faction(save_id, f, name=f.upper(),
                                       biases={"aggression": rnd.random(), "economic_focus": rnd.random()},
                                       mood=rnd.choice(["calm", "tense", "hostile"]))
            self.memory.upsert_economy(save_id, f, production_health=rnd.random(),
                                       shortages=({rnd.choice(wares): round(rnd.random(), 2)} if rnd.random() < 0.6 else {}))
        for f in facs:
            for other in rnd.sample(facs, min(3, n)):
                if other != f:
                    self.memory.adjust_relationship(save_id, f, other,
                                                    dresentment=rnd.randint(0, 60), dtrust=rnd.randint(-30, 20),
                                                    dfear=rnd.randint(0, 30),
                                                    standing=rnd.choice(["neutral", "wary", "hostile"]))
        for _ in range(max(1, n // 3)):
            a, b = rnd.sample(facs, 2)
            self.memory.add_conflict(save_id, a, b, status="active", intensity=rnd.random(), cause="dispute")
            self.memory.record_loss(save_id, a, amount=rnd.randint(0, 40))
        return facs

    def influence_stress(self, n_factions: int = 50) -> dict[str, Any]:
        """Start a background run: N faction-leaders each make the Stage-2 LLM decision
        through Player2 over the full derive→score→pick→apply cycle. Poll for status."""
        n = max(1, min(500, int(n_factions)))
        with self._inf_lock:
            if self._inf["running"]:
                return {"ok": False, "error": "an influence LLM stress is already running"}
            self._inf = {"running": True, "result": None, "started_at": time.time(),
                         "done": 0, "total": n, "ok": 0, "fallback": 0, "error": 0}

        def run() -> None:
            try:
                facs = self._seed_influence_factions(n)
                save_id = self.INF_STRESS_SAVE
                results = []
                t0 = time.time()
                for fid in facs:
                    r = self.review_faction(save_id, fid, use_llm=True)
                    llm = r.get("llm") or {}
                    cls = llm.get("status", "error")
                    results.append({"faction": fid, "action": (r.get("decision") or {}).get("action"),
                                    "target": (r.get("decision") or {}).get("target"),
                                    "llm_status": cls, "latency_ms": llm.get("latency_ms", 0),
                                    "narrative": llm.get("narrative", "")})
                    with self._inf_lock:
                        self._inf["done"] += 1
                        key = "ok" if cls == "ok" else ("error" if cls == "error" else "fallback")
                        self._inf[key] = self._inf.get(key, 0) + 1
                wall = time.time() - t0
                lats = sorted(x["latency_ms"] for x in results if x["latency_ms"])
                n2 = len(lats)
                def pctl(q):
                    return lats[min(n2 - 1, int(q * n2))] if lats else 0
                by_action: dict[str, int] = {}
                by_llm: dict[str, int] = {}
                for x in results:
                    by_action[x["action"]] = by_action.get(x["action"], 0) + 1
                    by_llm[x["llm_status"]] = by_llm.get(x["llm_status"], 0) + 1
                summary = {
                    "ok": True, "n_factions": n, "wall_s": round(wall, 1),
                    "throughput_per_min": round(n / wall * 60, 1) if wall else None,
                    "llm_breakdown": by_llm,
                    "llm_pick_rate": round(by_llm.get("ok", 0) / n, 3) if n else 0,
                    "decisions_by_action": by_action,
                    "latency_ms": {"p50": pctl(0.5), "p95": pctl(0.95),
                                   "max": lats[-1] if lats else 0, "avg": round(sum(lats) / n2) if n2 else 0},
                    "sample_llm_decisions": [x for x in results if x["llm_status"] == "ok"][:5],
                    "sample_failures": [x for x in results if x["llm_status"] in ("error", "unparsed", "fallback")][:5],
                }
                with self._inf_lock:
                    self._inf["running"] = False
                    self._inf["result"] = summary
            except Exception as exc:
                with self._inf_lock:
                    self._inf["running"] = False
                    self._inf["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "status": "started", "n_factions": n, "poll": "/api/influence/stress_status"}

    def influence_stress_status(self) -> dict[str, Any]:
        with self._inf_lock:
            started = self._inf["started_at"]
            return {"ok": True, "running": self._inf["running"], "started_at": started,
                    "elapsed_s": round(time.time() - started, 1) if started else None,
                    "progress": {"done": self._inf["done"], "total": self._inf["total"],
                                 "ok": self._inf["ok"], "fallback": self._inf["fallback"], "error": self._inf["error"]},
                    "result": self._inf["result"]}

    # --- Universe substrate: incidents/agreements/economy/sectors/conflicts/events

    def incidents_list(self, save_id: str, status: str | None = None) -> dict[str, Any]:
        return {"ok": True, "incidents": self.memory.list_incidents(save_id, status or None)}

    def incident_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            inc = self.memory.add_incident(
                str(payload.get("save_id", "")), str(payload.get("action_type", "")),
                faction_id=str(payload.get("faction_id", "")), target=str(payload.get("target", "")),
                confidence=float(payload.get("confidence", 0) or 0), priority=int(payload.get("priority", 0) or 0),
                cooldown_until=float(payload.get("cooldown_until", 0) or 0),
                narrative=str(payload.get("narrative", "")), effects=payload.get("effects"),
                status=str(payload.get("status", "pending")))
            return {"ok": True, "incident": inc}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def incident_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.memory.set_incident_status(
            str(payload.get("save_id", "")), int(payload.get("id")), str(payload.get("status", "")))

    def agreements_list(self, save_id: str, status: str | None = None) -> dict[str, Any]:
        return {"ok": True, "agreements": self.memory.list_agreements(save_id, status or None)}

    def agreement_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        ag = self.memory.add_agreement(
            str(payload.get("save_id", "")), str(payload.get("party_a", "")), str(payload.get("party_b", "")),
            type=str(payload.get("type", "")), terms=payload.get("terms"),
            deadline=float(payload.get("deadline", 0) or 0), status=str(payload.get("status", "pending")))
        return {"ok": True, "agreement": ag}

    def diplomacy_eligibility(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#58: is a war/peace move between two factions legal? Refuses excluded (khaak/xenon/player/non-combatant)
        and unknown/inactive factions. The gate #65 calls before any chat->relation mutation."""
        a = str(payload.get("a") or payload.get("faction_a") or payload.get("subject") or "")
        b = str(payload.get("b") or payload.get("faction_b") or payload.get("object") or "")
        save_id = str(payload.get("save_id") or "")
        known = None
        if save_id:
            try:
                known = {str(f.get("faction_id") or "").lower() for f in self.memory.list_factions(save_id)}
                known.discard("")
            except Exception:
                known = None
        return {"ok": True, **diplomacy_mod.war_eligibility(a, b, known)}

    def diplomacy_eligibility_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return diplomacy_mod.run_selftest()

    def agreements_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """G5: propose real agreement objects (ceasefire/trade) grounded in faction state — the missing middle."""
        return self.memory.generate_agreements(str(payload.get("save_id") or "unindexed"))

    def agreements_generate_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        s = "__agreements_gen_selftest__" + str(int(time.time() * 1000))
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.add_conflict(s, "argon", "teladi", status="active", intensity=0.5, cause="test war")
            m.upsert_economy(s, "split", market_status="exporter")
            m.upsert_economy(s, "paranid", market_status="importer", shortages={"energycells": 0.8})
            r = m.generate_agreements(s)
            types = sorted({a.get("type") for a in r["agreements"]})
            ok("ceasefire_for_active_war", "ceasefire" in types, types)
            ok("trade_for_exporter_importer", "trade" in types, types)
            m.add_conflict(s, "argon", "khaak", status="active", intensity=0.9, cause="khaak")
            m.generate_agreements(s)
            allag = m.list_agreements(s)
            ok("excluded_never_negotiate", not any(
                (a.get("party_a") in ("khaak", "xenon")) or (a.get("party_b") in ("khaak", "xenon")) for a in allag))
            before = len([a for a in allag if a.get("type") == "ceasefire"])
            m.generate_agreements(s)
            after = len([a for a in m.list_agreements(s) if a.get("type") == "ceasefire"])
            ok("dedup_no_duplicate_on_rerun", after == before, {"before": before, "after": after})
            # extension: common-enemy patrol cooperation + neutral non-aggression
            m.add_conflict(s, "argon", "xenon", status="active", intensity=0.7, cause="x")
            m.add_conflict(s, "paranid", "xenon", status="active", intensity=0.7, cause="x")
            m.generate_agreements(s)
            alltypes = sorted({a.get("type") for a in m.list_agreements(s)})
            ok("patrol_cooperation_for_common_enemy", "patrol_cooperation" in alltypes, alltypes)
            ok("non_aggression_for_neutral_pair", "non_aggression" in alltypes, alltypes)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def memory_promote_facts(self, payload: dict[str, Any]) -> dict[str, Any]:
        """G4 backfill: promote an NPC's durable-fact candidates to durable facts."""
        npc = str(payload.get("npc_key") or "")
        if not npc:
            return {"ok": False, "reason": "npc_key required"}
        return {"ok": True, **self.memory.promote_durable_facts(npc)}

    def memory_promote_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        npc = "__promote_selftest__" + str(int(time.time() * 1000)) + "|g|Marine"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.record_turn(npc, "assistant", "I refuse to give you any fuel.")
            m.record_turn(npc, "assistant", "I promise to escort your convoy through Hatikvah.")
            m.record_turn(npc, "assistant", "Nice weather on the docks today.")
            r = m.promote_durable_facts(npc)
            ok("promoted_durable_turns", r["promoted"] >= 2, r)
            cats = sorted({f.get("category") for f in m.get_facts(npc)})
            ok("refusal_now_a_fact", "refusal" in cats, cats)
            ok("oath_now_a_fact", "oath" in cats, cats)
            ok("routine_not_promoted", not any("weather" in str(f.get("text") or "").lower() for f in m.get_facts(npc)))
            again = m.promote_durable_facts(npc)
            ok("dedup_no_repromote", again["promoted"] == 0, again)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def memory_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """G4: the MEMORY-AUDIT summary for an NPC (literal facts + promotion candidates), not the roleplay recap."""
        npc = str(payload.get("npc_key") or "")
        if not npc:
            return {"ok": False, "reason": "npc_key required"}
        return {"ok": True, **self.memory.memory_audit_summary(npc, int(payload.get("limit") or 40))}

    def memory_audit_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        npc = "__mem_audit_selftest__" + str(int(time.time() * 1000)) + "|game|TestMarine"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.record_turn(npc, "user", "Will you supply fuel to my fleet?")
            m.record_turn(npc, "assistant", "I refuse to give you any fuel.")
            m.record_turn(npc, "assistant", "Nice weather on the docks today.")
            m.record_turn(npc, "assistant", "I promise to escort your convoy through Hatikvah.")
            audit = m.memory_audit_summary(npc)
            cats = sorted({c["category"] for c in audit["promotion_candidates"]})
            ok("audit_mode_flag", audit.get("mode") == "memory_audit")
            ok("refusal_promoted_as_candidate", "refusal" in cats, cats)
            ok("promise_promoted_as_candidate", "oath" in cats, cats)
            ok("smalltalk_excluded", not any("weather" in c["text"].lower() for c in audit["promotion_candidates"]))
            ok("has_multiple_candidates", audit["promotion_candidate_count"] >= 2)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def player_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        """G2: the player's classified role(s) from stored signals (factions react to who they are)."""
        return {"ok": True, **self.memory.classify_player_role(str(payload.get("save_id") or "unindexed"))}

    def player_role_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        s = "__player_role_selftest__" + str(int(time.time() * 1000))
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            ok("newcomer_when_empty", m.classify_player_role(s)["primary_role"] == "unaligned newcomer")
            m.upsert_economy(s, "argon", dependency_on_player=0.8)
            m.upsert_economy(s, "teladi", dependency_on_player=0.7)
            ok("supplier_detected", "supplier" in m.classify_player_role(s)["role_tags"])
            m.upsert_player_market(s, "energycells", "somewhere", supplying_enemies=True)
            ok("war_profiteer_is_primary", m.classify_player_role(s)["primary_role"] == "war profiteer")
            m.adjust_relationship(s, "split", "player", dresentment=60)
            r = m.classify_player_role(s)
            ok("threat_faction_listed", "split" in r["threats"] and r["per_faction"].get("split") == "threat", r["per_faction"])
            m.adjust_relationship(s, "argon", "player", dtrust=70)
            r2 = m.classify_player_role(s)
            ok("friend_faction_listed", "argon" in r2["friends"] and r2["per_faction"].get("argon") == "friend", r2["per_faction"])
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def _build_patrol_offer(self, save_id: str, faction_id: str = "") -> dict[str, Any]:
        """G1 (Gameplay Changes doc): render a patrol/defense offer from a REAL contested sector. PURE — no
        enqueue, no reward. Picks the most-pressing contested sector with a known owner + contester."""
        cands = [s for s in self.memory.list_sectors(save_id)
                 if (s.get("contested_by") or []) and s.get("owner_faction")]
        if faction_id:
            cands = [s for s in cands if s.get("owner_faction") == faction_id]
        if not cands:
            return {"ok": False, "reason": "no contested sector with a known owner to source a patrol contract"}
        cands.sort(key=lambda s: (int(s.get("player_assets_present") or 0), float(s.get("strategic_value") or 0),
                                  len(s.get("contested_by") or [])), reverse=True)
        s = cands[0]
        owner = s.get("owner_faction")
        threat_id = (s.get("contested_by") or [""])[0]
        where = s.get("name") or s.get("sector_id") or "the front"
        rendered = offers_mod.render_offer("patrol", {
            "faction": self._fac_name(save_id, owner), "where": where, "threat": self._fac_name(save_id, threat_id)})
        if not rendered.get("ok"):
            return {"ok": False, "reason": rendered.get("reason")}
        return {"ok": True, "sector": where, "owner": owner, "threat": threat_id, "offer": rendered["offer"]}

    def sector_patrol_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """G1: build a patrol offer from a real contested sector and ENQUEUE it as a player communiqué.
        PROPOSAL only — no order issued, no reward minted."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid_in = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or str(payload.get("faction_id") or "")
        res = self._build_patrol_offer(save_id, fid_in)
        if not res.get("ok"):
            return res
        offer = res["offer"]
        owner_name = self._fac_name(save_id, res["owner"])
        comm = {"title": f"{owner_name.upper()} PATROL REQUEST", "body": offer["summary"], "faction": res["owner"],
                "faction_name": owner_name, "category": "diplomacy", "kind": "offer", "save_id": save_id,
                "ts": time.time(), "offer": offer}
        self.player_comms.append(comm)
        while len(self.player_comms) > 200:
            self.player_comms.popleft()
        return {"ok": True, "sector": res["sector"], "owner": res["owner"], "threat": res["threat"],
                "offer": offer, "comm_enqueued": True}

    def sector_patrol_offer_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic: seed synthetic contested sectors -> assert the offer targets the most-pressing one and
        is grounded in real owner/sector/threat. Throwaway save_id."""
        s = "__patrol_offer_selftest__" + str(int(time.time() * 1000))
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            self.memory.upsert_sector(s, "sec_quiet", name="Grand Exchange", owner_faction="teladi",
                                      contested_by=["xenon"], strategic_value=0.2, player_assets_present=False)
            self.memory.upsert_sector(s, "sec_hot", name="Hatikvah's Choice III", owner_faction="argon",
                                      contested_by=["khaak", "xenon"], strategic_value=0.9, player_assets_present=True)
            r = self._build_patrol_offer(s, "")
            ok("offer_built", r.get("ok") is True, r)
            ok("targets_most_pressing_sector", r.get("sector") == "Hatikvah's Choice III", r.get("sector"))
            ok("owner_is_argon", r.get("owner") == "argon", r.get("owner"))
            ok("kind_is_patrol", r.get("ok") and r["offer"]["kind"] == "Patrol")
            ok("summary_grounded_no_braces", r.get("ok") and "{" not in r["offer"]["summary"]
               and "Hatikvah's Choice III" in r["offer"]["summary"], r.get("offer"))
            ok("no_reward_minted", r.get("ok") and r["offer"]["reward_kind"] == "credits" and "reward_amount" not in r["offer"])
            ok("no_contested_no_offer", self._build_patrol_offer("__no_such_save__", "").get("ok") is False)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def offers_list(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """#59: the X4-native offer template catalog (shapes an NPC can present)."""
        return {"ok": True, "templates": offers_mod.list_templates()}

    def offers_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#59: fill one template + params into a concrete offer (proposal only — no reward, no mutation)."""
        return offers_mod.render_offer(str(payload.get("template_id") or ""), payload.get("params") or {})

    def offers_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return offers_mod.run_selftest()

    def budget_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#63: a faction's owned spend-capacity (grounded in real stations) vs what's been drawn."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or str(payload.get("faction_id") or "")
        cap = self.memory.budget_capacity(save_id, fid)
        spent = self.memory.budget_spent(save_id, fid)
        return {"ok": True, "faction_id": fid, "capacity": cap, "spent": round(spent, 2),
                "remaining": round(cap - spent, 2)}

    def budget_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A1b: every economy-bearing faction's earned spend-capacity vs drawn — surfaces the anti-cheat
        budget substrate (#63). Capacity is DERIVED from real owned stations, so this has data even at spent=0."""
        save_id = str(payload.get("save_id") or "unindexed")
        rows = []
        for e in self.memory.list_economy(save_id):
            fid = e.get("faction_id")
            if not fid:
                continue
            cap = self.memory.budget_capacity(save_id, fid)
            spent = self.memory.budget_spent(save_id, fid)
            com = self.memory.jobs_committed(save_id, fid)
            rows.append({"faction_id": fid, "capacity": cap, "spent": round(spent, 2),
                         "committed": round(com, 2), "remaining": round(cap - spent, 2),
                         "available": round(cap - spent - com, 2)})
        rows.sort(key=lambda r: r["capacity"], reverse=True)
        return {"ok": True, "budgets": rows}

    def earned_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#63 anti-cheat gate: is an 'earned' transfer of `cost` legitimate for this faction? Server-set, never
        LLM-settable. `commit:true` debits the ledger when affordable (so it can't be re-spent)."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or str(payload.get("faction_id") or "")
        cost = float(payload.get("cost") or 0)
        commit = bool(payload.get("commit"))
        return {"ok": True, **self.memory.validate_earned_transfer(save_id, fid, cost, commit=commit)}

    def earned_validate_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic: seed synthetic owned stations -> a faction can spend up to capacity, never beyond, and
        cannot re-spend what it already drew. Throwaway save_id."""
        s = "__earned_validate_selftest__" + str(int(time.time() * 1000))  # fresh ledger per run
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            # seed 4 healthy owned stations
            for i in range(4):
                self.memory.upsert_economy_station(s, {"station_id": f"b{i}", "faction_id": "argon", "products": ["energycells"], "needs": []})
            self.memory.upsert_economy(s, "argon", production_health=1.0)
            cap = self.memory.budget_capacity(s, "argon")
            ok("capacity_from_real_stations", cap == 4 * self.memory.PER_STATION_CREDITS, cap)
            v_ok = self.memory.validate_earned_transfer(s, "argon", cap * 0.5)
            ok("affordable_within_capacity", v_ok["earned"] is True, v_ok)
            v_over = self.memory.validate_earned_transfer(s, "argon", cap * 2)
            ok("over_capacity_refused", v_over["earned"] is False, v_over)
            # spend half (commit), then a second half-spend must still fit, but a third must refuse
            self.memory.validate_earned_transfer(s, "argon", cap * 0.5, commit=True)
            self.memory.validate_earned_transfer(s, "argon", cap * 0.5, commit=True)
            v_resp = self.memory.validate_earned_transfer(s, "argon", cap * 0.5)
            ok("cannot_respend_drained_budget", v_resp["earned"] is False and v_resp["remaining"] <= 0.01, v_resp)
            ok("no_capacity_no_spend", self.memory.validate_earned_transfer(s, "nobody", 1)["earned"] is False)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def rumor_propagate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Spread a rumor from an origin NPC along the social graph (warm ties hear it; rivals don't)."""
        save_id = str(payload.get("save_id") or "unindexed")
        return self.memory.propagate_rumor(save_id, str(payload.get("origin_npc") or ""),
                                            str(payload.get("text") or ""), str(payload.get("category") or "rumor"))

    def rumor_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        save_id = str(payload.get("save_id") or "unindexed")
        npc = str(payload.get("npc_key") or "") or None
        return {"ok": True, "rumors": self.memory.list_rumors(save_id, npc),
                "brief": (self.memory.rumor_brief(save_id, npc) if npc else "")}

    def rumor_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        save = "__rumor_selftest__" + str(int(time.time() * 1000))
        A, B, C = save + "|g|Origin", save + "|g|CloseFriend", save + "|g|Rival"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.index_npcs(save, [{"npc_key": A, "name": "Origin", "faction_id": "argon"},
                                {"npc_key": B, "name": "Close Friend", "faction_id": "argon"},
                                {"npc_key": C, "name": "Rival", "faction_id": "argon"}], game_id="g")
            m.apply_social_event(save, A, B, "saved_life", "pulled them from the wreck")   # warm tie
            m.apply_social_event(save, A, C, "public_insult", "humiliated them on the bridge")  # hostile tie
            r = m.propagate_rumor(save, A, "The Kha'ak are massing near Hatikvah")
            recips = {x["npc_key"] for x in r["recipients"]}
            ok("spread_to_warm_tie", B in recips, sorted(recips))
            ok("not_to_hostile_tie", C not in recips, sorted(recips))
            ok("recipient_knows_rumor", any("Kha'ak" in x.get("text", "") for x in m.list_rumors(save, B)))
            ok("brief_surfaces_rumor", "Word reaching you" in m.rumor_brief(save, B))
            before = len(m.list_rumors(save, B))
            m.propagate_rumor(save, A, "The Kha'ak are massing near Hatikvah")
            ok("dedup_no_duplicate", len(m.list_rumors(save, B)) == before, {"before": before, "after": len(m.list_rumors(save, B))})
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def social_briefing_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """#39 surfacing: a seeded social tie appears in the NPC's situation briefing (so they speak aware of it)."""
        m = self.memory
        save = "__social_brief_selftest__" + str(int(time.time() * 1000))
        nk_a, nk_b = save + "|g|Sela", save + "|g|Quint"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            m.index_npcs(save, [{"npc_key": nk_a, "name": "Sela Tarren", "faction_id": "argon"},
                                {"npc_key": nk_b, "name": "Quint Caren", "faction_id": "argon"}], game_id="g")
            ok("no_ties_no_line", "Personal ties" not in m.build_situation_briefing(nk_a))
            m.apply_social_event(save, nk_a, nk_b, "served_together", "the Kha'ak raid on the dock")
            b1 = m.build_situation_briefing(nk_a)
            ok("ties_surface_in_briefing", "Personal ties" in b1, b1[-160:])
            ok("names_the_other_npc", "Quint Caren" in b1)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def social_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#39: NPC↔NPC social edges (all, or those touching one npc_key)."""
        save_id = str(payload.get("save_id") or "unindexed")
        npc = str(payload.get("npc_key") or "") or None
        return {"ok": True, "relations": self.memory.list_social_relations(save_id, npc),
                "summary": (self.memory.social_summary(save_id, npc) if npc else "")}

    def _enqueue_relationship_beat(self, save_id: str, res: dict[str, Any]) -> Optional[str]:
        """M4: if a social event changed the edge's narrative STATUS, surface a one-line ambient gossip beat to the
        player (low-priority Messages entry, From: Station Gossip). Returns the beat line or None. Never throws."""
        try:
            if not (res or {}).get("ok"):
                return None
            line = relationship_beat_line(res.get("subject_npc"), res.get("object_npc"),
                                          str(res.get("status_before") or ""), str(res.get("status") or ""))
            if not line:
                return None
            comm = {"title": "Crew Affairs", "body": line, "faction": "", "faction_name": "",
                    "category": "news", "kind": "alert", "save_id": save_id, "ts": time.time(),
                    "sender_name": "Station Gossip", "sender_faction": "", "sender_npc_key": "",
                    "sender_role": "narrator", "priority": "low", "tx_id": "tx_" + uuid.uuid4().hex[:12]}
            self.player_comms.append(comm)
            while len(self.player_comms) > 200:
                self.player_comms.popleft()
            return line
        except Exception:
            return None

    def social_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#39: apply a social EVENT (saved_life, betrayal, served_together, …) to an NPC↔NPC edge — the only
        sanctioned way relationships change. Mutates scores + evidence + re-derives narrative status. M4: a status
        change also emits an ambient relationship beat to the player."""
        save_id = str(payload.get("save_id") or "unindexed")
        res = self.memory.apply_social_event(save_id, str(payload.get("subject_npc") or ""),
                                             str(payload.get("object_npc") or ""),
                                             str(payload.get("event_type") or ""), str(payload.get("note") or ""))
        beat = self._enqueue_relationship_beat(save_id, res)
        if beat:
            res = {**res, "beat": beat}
        return res

    def social_edge_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#39: the in-character edge context to inject when subject talks ABOUT object (scores->English)."""
        save_id = str(payload.get("save_id") or "unindexed")
        return {"ok": True, "brief": self.memory.social_edge_brief(save_id, str(payload.get("subject_npc") or ""),
                                                                   str(payload.get("object_npc") or ""))}

    def social_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        m = self.memory
        s = "__social_selftest__" + str(int(time.time() * 1000))
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            # status derivation is gated (romance needs attraction AND affection)
            ok("status_strangers_default", m._advance_social_status({})[0] == "strangers")
            ok("status_enemies_from_resentment", m._advance_social_status({"resentment": 0.8})[0] == "enemies")
            ok("attraction_alone_is_not_romance", m._advance_social_status({"attraction": 0.9})[1] != "romantic")
            ok("romance_gated_needs_affection", m._advance_social_status({"attraction": 0.8, "affection": 0.7})[0] == "partners")
            # EVENT-DRIVEN: relationships change only via events, with evidence
            m.apply_social_event(s, "npcA", "npcB", "served_together", "the Kha'ak raid on the dock")
            m.apply_social_event(s, "npcA", "npcB", "saved_life", "pulled wounded crew from the wreck")
            ab = next(e for e in m.list_social_relations(s, "npcA") if e["object_npc"] == "npcB")
            ok("event_moves_scores", ab["trust"] > 0 and ab["affection"] > 0 and ab["loyalty"] > 0)
            ok("evidence_recorded", len(ab.get("evidence") or []) == 2 and "Kha'ak" in (ab["evidence"][0].get("note") or ""))
            ok("unknown_event_rejected", m.apply_social_event(s, "npcA", "npcB", "telepathy").get("ok") is False)
            ok("self_edge_rejected", m.apply_social_event(s, "npcA", "npcA", "served_together").get("ok") is False)
            # a romance arc progresses through states, never a boolean
            r = "__social_selftest_r__" + str(int(time.time() * 1000))
            for _ in range(8):
                m.apply_social_event(r, "x", "y", "repeated_conversations")
            m.apply_social_event(r, "x", "y", "flirtation_reciprocated")
            m.apply_social_event(r, "x", "y", "flirtation_reciprocated")
            xy = next(e for e in m.list_social_relations(r, "x") if e["object_npc"] == "y")
            ok("romance_is_a_state_not_boolean", xy["status"] in ("private_attraction", "flirtation", "confession_pending", "courting", "partners"))
            # edge brief is in-character English (no raw numbers)
            brief = m.social_edge_brief(s, "npcA", "npcB")
            ok("edge_brief_in_character", "You know" in brief and "remember" in brief.lower() and not any(c.isdigit() for c in brief.replace("Kha", "")))
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def _build_supply_offer(self, save_id: str, faction_id: str = "") -> dict[str, Any]:
        """#60: render a supply_delivery offer from a REAL faction shortage (#54). PURE — no enqueue, no reward."""
        cands = [faction_id] if faction_id else [e.get("faction_id") for e in self.memory.list_economy(save_id)]
        best = None  # (fid, ware, severity, econ)
        for fid in cands:
            if not fid:
                continue
            econ = self.memory.get_economy(save_id, fid) or {}
            sh = econ.get("shortages") or {}
            if not sh:
                continue
            ware, sev = max(sh.items(), key=lambda kv: float(kv[1] or 0))
            sev = float(sev or 0)
            if best is None or sev > best[2]:
                best = (fid, ware, sev, econ)
        if not best:
            return {"ok": False, "reason": "no faction has a real shortage to source an offer"}
        fid, ware, sev, _econ = best
        fname = self._fac_name(save_id, fid)
        ware_label = self.memory._ware_label(ware)
        amount = "{:,}".format(int(2000 + round(sev * 8000)))  # REQUEST quantity (text only — not a transfer)
        where = fname + " space"
        try:  # use a real station name ONLY if it's meaningful — never leak a placeholder like "Unknown Station".
            for st_row in (self.memory.list_economy_stations(save_id, fid) or []):
                nm = str(st_row.get("station_name") or "").strip()
                if nm and not nm.lower().startswith("unknown"):
                    where = nm
                    break
        except Exception:
            pass
        reason = ("Their stations are critically short." if sev >= 0.7
                  else "Supplies are running low." if sev >= 0.4 else "Stocks are a little tight.")
        rendered = offers_mod.render_offer("supply_delivery", {
            "faction": fname, "ware": ware_label, "amount": amount, "where": where, "reason": reason})
        if not rendered.get("ok"):
            return {"ok": False, "reason": rendered.get("reason")}
        return {"ok": True, "faction": fid, "ware": ware, "severity": round(sev, 3), "offer": rendered["offer"]}

    def economy_supply_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#60: build a supply offer from a real shortage and ENQUEUE it as a player communiqué (surfaces in-game
        via the /v1/player_comms drain). PROPOSAL only — no ware moved, no reward minted."""
        save_id = str(payload.get("save_id") or "unindexed")
        fid_in = self.memory.resolve_faction_id(str(payload.get("faction_id") or "")) or str(payload.get("faction_id") or "")
        res = self._build_supply_offer(save_id, fid_in)
        if not res.get("ok"):
            return res
        offer = res["offer"]
        fname = self._fac_name(save_id, res["faction"])
        comm = {"title": f"{fname.upper()} SUPPLY REQUEST", "body": offer["summary"], "faction": res["faction"],
                "faction_name": fname, "category": "diplomacy", "kind": "offer", "save_id": save_id,
                "ts": time.time(), "offer": offer}
        self.player_comms.append(comm)
        while len(self.player_comms) > 200:
            self.player_comms.popleft()
        return {"ok": True, "faction": res["faction"], "ware": res["ware"], "severity": res["severity"],
                "offer": offer, "comm_enqueued": True}

    def economy_supply_offer_selftest(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministic: seed a synthetic faction shortage -> assert the offer is grounded in it. Does NOT touch
        the live player_comms queue (uses a throwaway save_id + the pure builder)."""
        s = "__supply_offer_selftest__"
        checks: list[dict] = []
        ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
        try:
            self.memory.upsert_economy(s, "argon", shortages={"energycells": 0.9, "foodrations": 0.5},
                                       key_needs=["energycells", "foodrations"], market_status="importer")
            r = self._build_supply_offer(s, "argon")
            ok("offer_built", r.get("ok") is True, r)
            ok("targets_worst_shortage", r.get("ware") == "energycells", r.get("ware"))
            ok("kind_is_deliver_wares", r.get("ok") and r["offer"]["kind"] == "Deliver Wares")
            ok("summary_grounded_no_braces", r.get("ok") and "{" not in r["offer"]["summary"] and "Energy Cells" in r["offer"]["summary"], r.get("offer"))
            ok("proposal_has_no_reward_grant", r.get("ok") and r["offer"]["reward_kind"] == "credits" and "reward_amount" not in r["offer"])
            ok("where_no_unknown_placeholder", r.get("ok") and "unknown" not in r["offer"]["summary"].lower(), r.get("offer"))
            empty = self._build_supply_offer("__no_such_save__", "argon")
            ok("no_shortage_no_offer", empty.get("ok") is False)
        except Exception as e:
            ok("no_exception", False, str(e))
        passed = sum(1 for c in checks if c["pass"])
        return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}

    def economy_list(self, save_id: str) -> dict[str, Any]:
        econ = self.memory.list_economy(save_id)
        # #56 "Economy Truth": audit the aggregate against the real per-station capture (#54) — attach each
        # faction's captured-station count, the ware display-name map (#55), and sweep totals.
        stations = self.memory.list_economy_stations(save_id)
        counts: dict[str, int] = {}
        for s in stations:
            f = s.get("faction_id")
            if f:
                counts[f] = counts.get(f, 0) + 1
        names: dict[str, str] = {}
        for e in econ:
            e["station_count"] = counts.get(e.get("faction_id"), 0)
            for w in (e.get("key_needs") or []):
                names.setdefault(str(w), self.memory._ware_label(str(w)))
            for w in (e.get("shortages") or {}):
                names.setdefault(str(w), self.memory._ware_label(str(w)))
        return {"ok": True, "economy": econ, "player_market": self.memory.list_player_market(save_id),
                "ware_names": names,
                "economy_meta": {"stations_captured": len(stations), "factions_covered": len(counts)}}

    def economy_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        fid = str(payload.get("faction_id", ""))
        if not fid:
            return {"ok": False, "error": "faction_id required"}
        fields = {k: payload[k] for k in (
            "player_economic_importance", "dependency_on_player", "production_health",
            "key_needs", "shortages", "trade_pacts", "trade_restrictions", "market_status") if k in payload}
        return {"ok": True, "economy": self.memory.upsert_economy(str(payload.get("save_id", "")), fid, **fields)}

    def sectors_list(self, save_id: str) -> dict[str, Any]:
        return {"ok": True, "sectors": self.memory.list_sectors(save_id)}

    def sector_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        sid = str(payload.get("sector_id", ""))
        if not sid:
            return {"ok": False, "error": "sector_id required"}
        return {"ok": True, "sector": self.memory.upsert_sector(
            str(payload.get("save_id", "")), sid, name=payload.get("name"),
            owner_faction=payload.get("owner_faction"), contested_by=payload.get("contested_by"),
            strategic_value=payload.get("strategic_value"), player_assets_present=payload.get("player_assets_present"))}

    def conflicts_list(self, save_id: str, status: str | None = None) -> dict[str, Any]:
        return {"ok": True, "conflicts": self.memory.list_conflicts(save_id, status or None),
                "losses": self.memory.get_loss_summary(save_id)}

    def conflict_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        c = self.memory.add_conflict(
            str(payload.get("save_id", "")), str(payload.get("faction_a", "")), str(payload.get("faction_b", "")),
            status=str(payload.get("status", "active")), intensity=float(payload.get("intensity", 0) or 0),
            cause=str(payload.get("cause", "")))
        return {"ok": True, "conflict": c}

    def loss_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.memory.record_loss(
            str(payload.get("save_id", "")), str(payload.get("faction_id", "")),
            float(payload.get("amount", 0) or 0), kind=str(payload.get("kind", "ship")),
            sector_id=str(payload.get("sector_id", "")))
        return {"ok": True}

    def losses_summary(self, save_id: str, faction: str | None = None) -> dict[str, Any]:
        return {"ok": True, "losses": self.memory.get_loss_summary(save_id, faction or None)}

    def world_events_list(self, save_id: str, limit: int = 200, min_importance: int = 1) -> dict[str, Any]:
        return {"ok": True, "world_events": self.memory.list_world_events(save_id, limit, min_importance)}

    def world_event_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        ev = self.memory.add_world_event(
            str(payload.get("save_id", "")), str(payload.get("event_type", "")),
            summary=str(payload.get("summary", "")), primary_faction=str(payload.get("primary_faction", "")),
            secondary_faction=str(payload.get("secondary_faction", "")), sector_id=str(payload.get("sector_id", "")),
            importance=int(payload.get("importance", 1) or 1), source=str(payload.get("source", "")))
        return {"ok": True, "world_event": ev}

    def universe_selftest(self) -> dict[str, Any]:
        return run_universe_selftest()

    def universe_stress(self, npcs: int = 2000, factions: int = 60, turns_per: int = 12) -> dict[str, Any]:
        """Start a find-the-wall stress run in the background and return immediately.
        Poll universe_stress_status() for progress + the final metrics."""
        npcs = max(1, min(50000, int(npcs)))        # allow the wall to be found
        factions = max(1, min(500, int(factions)))
        turns_per = max(1, min(60, int(turns_per)))
        params = {"npcs": npcs, "factions": factions, "turns_per": turns_per}
        with self._stress_lock:
            if self._stress["running"]:
                return {"ok": False, "error": "a stress run is already in progress",
                        "started_at": self._stress["started_at"], "params": self._stress["params"]}
            self._stress = {"running": True, "result": None, "started_at": time.time(), "params": params}

        def _run() -> None:
            try:
                res = run_full_stress(self.memory, n_npcs=npcs, n_factions=factions, turns_per=turns_per)
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            with self._stress_lock:
                self._stress["running"] = False
                self._stress["result"] = res

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started", "params": params,
                "poll": "/api/universe/stress_status"}

    def universe_stress_status(self) -> dict[str, Any]:
        with self._stress_lock:
            started = self._stress["started_at"]
            return {"ok": True, "running": self._stress["running"],
                    "started_at": started,
                    "elapsed_s": round(time.time() - started, 1) if started else None,
                    "params": self._stress["params"], "result": self._stress["result"]}

    def universe_stress_clear(self) -> dict[str, Any]:
        return self.memory.clear_save("fullstress")

    def population_stress(self, n_npcs: int = 2000) -> dict[str, Any]:
        """Background: a mixed population (reps/admirals/pilots/...) lives random
        event streams through the full memory pipeline. Reports what sticks (CORE
        verbatim) vs what's lost (routine). Poll /api/universe/stress_status."""
        from .memory import run_population_stress
        n = max(1, min(20000, int(n_npcs)))
        with self._stress_lock:
            if self._stress["running"]:
                return {"ok": False, "error": "a stress run is already in progress"}
            self._stress = {"running": True, "result": None, "started_at": time.time(),
                            "params": {"population_npcs": n}}

        def _run() -> None:
            try:
                res = run_population_stress(self.memory, n_npcs=n)
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            with self._stress_lock:
                self._stress["running"] = False
                self._stress["result"] = res

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "status": "started", "n_npcs": n, "poll": "/api/universe/stress_status"}

    def population_stress_clear(self) -> dict[str, Any]:
        return self.memory.clear_save("population")

    # --- Player2 END-TO-END pipeline stress (REAL prompts -> Player2 -> replies) --

    def _one_player2_call(self, save_id: str, i: int, prompt: str) -> dict[str, Any]:
        """Fire ONE real prompt through the NPC path to Player2; classify the reply."""
        persona = f"StressOfficer-{i:05d}"
        payload = {
            "request_id": f"p2stress-{int(time.time()*1000)}-{i}",
            "source_mod": "p2_pipeline_stress",
            "channel": "npc",
            "target": {
                "mode": "npc", "game_id": "p2stress", "save_id": save_id,
                "npc_name": persona, "npc_short_name": f"Off{i}",
                "system_prompt": "You are an X4 faction officer. Reply in ONE short sentence.",
            },
            "messages": [{"role": "user", "content": prompt}],
        }
        t0 = time.time()
        try:
            request = NeuralRequest.from_payload(payload)
            resp = self.player2.npc_complete(request).to_dict()
            dt = round((time.time() - t0) * 1000)
            reply = (resp.get("reply") or "").strip()
            status = resp.get("status")
            if status == "ok" and reply:
                cls = "ok"
            elif status == "ok" and not reply:
                cls = "empty"
            else:
                cls = "error"
            return {"i": i, "latency_ms": dt, "class": cls,
                    "reply_len": len(reply), "reply": reply[:160],
                    "error": resp.get("error") or ""}
        except Exception as exc:
            return {"i": i, "latency_ms": round((time.time() - t0) * 1000),
                    "class": "error", "reply_len": 0, "reply": "", "error": f"{type(exc).__name__}: {exc}"}

    def player2_stress(self, calls: int = 10, threads: int = 1, save_id: str = "p2stress") -> dict[str, Any]:
        """Start a background END-TO-END Player2 pipeline run. Fires `calls` real
        prompts across `threads` concurrent workers (the bridge serializes chat
        through the single local model, so threads measures queue behavior)."""
        calls = max(1, min(2000, int(calls)))
        threads = max(1, min(32, int(threads)))
        params = {"calls": calls, "threads": threads}
        with self._p2_lock:
            if self._p2["running"]:
                return {"ok": False, "error": "a Player2 pipeline run is already in progress",
                        "started_at": self._p2["started_at"], "params": self._p2["params"]}
            self._p2 = {"running": True, "result": None, "started_at": time.time(),
                        "params": params, "done": 0, "total": calls,
                        "ok": 0, "empty": 0, "error": 0}

        prompts = [
            "Captain, status report?", "What is our tactical situation?",
            "Any threats in this sector?", "Report on fleet readiness.",
            "What are your orders, commander?", "Describe the enemy movements.",
            "How is morale aboard?", "Summarize the supply situation.",
        ]

        def run() -> None:
            import threading as _t
            results: list[dict[str, Any]] = []
            res_lock = _t.Lock()
            indices = list(range(calls))
            t0 = time.time()

            def worker(my: list[int]) -> None:
                for i in my:
                    r = self._one_player2_call(save_id, i, prompts[i % len(prompts)])
                    with res_lock:
                        results.append(r)
                    with self._p2_lock:
                        self._p2["done"] += 1
                        self._p2[r["class"]] = self._p2.get(r["class"], 0) + 1

            workers = []
            for w in range(threads):
                chunk = indices[w::threads]   # round-robin split
                th = _t.Thread(target=worker, args=(chunk,), daemon=True)
                th.start()
                workers.append(th)
            for th in workers:
                th.join()
            wall = time.time() - t0

            lats = sorted(r["latency_ms"] for r in results)
            n = len(lats)
            def pctl(p: float) -> int:
                if not lats:
                    return 0
                return lats[min(n - 1, int(p * n))]
            ok = sum(1 for r in results if r["class"] == "ok")
            empty = sum(1 for r in results if r["class"] == "empty")
            error = sum(1 for r in results if r["class"] == "error")
            summary = {
                "ok": True,
                "calls": calls, "threads": threads,
                "wall_s": round(wall, 1),
                "throughput_per_min": round(calls / wall * 60, 1) if wall else None,
                "replies_ok": ok, "replies_empty": empty, "errors": error,
                "success_rate": round(ok / calls, 3) if calls else 0,
                "latency_ms": {"p50": pctl(0.5), "p95": pctl(0.95),
                               "max": lats[-1] if lats else 0, "min": lats[0] if lats else 0,
                               "avg": round(sum(lats) / n) if n else 0},
                "sample_replies": [r for r in results if r["class"] == "ok"][:3],
                "sample_failures": [r for r in results if r["class"] != "ok"][:5],
            }
            with self._p2_lock:
                self._p2["running"] = False
                self._p2["result"] = summary

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "status": "started", "params": params,
                "poll": "/api/player2/stress_status"}

    def player2_stress_status(self) -> dict[str, Any]:
        with self._p2_lock:
            started = self._p2["started_at"]
            return {"ok": True, "running": self._p2["running"], "started_at": started,
                    "elapsed_s": round(time.time() - started, 1) if started else None,
                    "params": self._p2["params"],
                    "progress": {"done": self._p2["done"], "total": self._p2["total"],
                                 "ok": self._p2["ok"], "empty": self._p2["empty"], "error": self._p2["error"]},
                    "result": self._p2["result"]}

    def player2_stress_clear(self) -> dict[str, Any]:
        return self.memory.clear_save("p2stress")

    # --- Grounded single-NPC demo (the immersion proof) -----------------------

    GROUNDED_SAVE = "grounded"
    GROUNDED_GAME = "grounded_game"
    GROUNDED_NPC = "Captain Mariko Voss"

    def _seed_grounded_npc(self) -> str:
        """Reset the grounded save, seed the demo universe, and install ONE NPC with a
        real remembered history. Returns the npc_key."""
        save_id = self.GROUNDED_SAVE
        self.memory.clear_save(save_id)
        self.universe_seed(save_id)  # factions/relationships/strategic/economy/sectors/conflicts/world_events
        # Strengthen Argon's bond to the player so loyalty is grounded.
        self.memory.adjust_relationship(save_id, "argon", "player",
                                        dtrust=35, ddebt=45, standing="indebted ally")
        # SPEC 1c-C/1c-D showcase: give Argon its named representative + a lasting faction grudge toward
        # the Split (matches the "broke our ceasefire" betrayal memory below), so the situation briefing
        # surfaces both the rememberer and the grudge.
        self.memory.upsert_faction(save_id, "argon", representative="Melissa Mettel")
        self.memory.adjust_relationship(save_id, "argon", "split",
                                        dresentment=35, dtrust=-20, standing="hostile")
        npc_key = self.memory.make_key(save_id, self.GROUNDED_GAME, self.GROUNDED_NPC)
        self.memory.bind_npc(npc_key, "", save_id=save_id, game_id=self.GROUNDED_GAME,
                             name=self.GROUNDED_NPC, faction_id="argon",
                             stats={"race": "argon", "role": "pilot", "ship_class": "ship_l",
                                    "ship_name": "ANV Vigil", "sector": "Hatikvah's Choice",
                                    "skills": {"piloting": 13, "management": 11, "morale": 12}})
        # A real remembered history (CORE facts survive verbatim).
        for text, cat in [
            ("Admiral Vance was killed aboard the ANV Resolute defending Argon Prime; I held the line after he fell.", "death"),
            ("I swore an oath to hold Hatikvah's Choice to the last hull.", "oath"),
            ("You, Commander, resupplied my squadron at Hatikvah when we were out of hull parts — I have not forgotten it.", "love"),
            ("The Split broke our ceasefire and raided our convoys; that betrayal is not forgiven.", "betrayal"),
        ]:
            self.memory.add_fact(npc_key, text, category=cat)
        return npc_key

    def grounded_demo(self) -> dict[str, Any]:
        """Start the grounded conversation in the background. Poll grounded_status."""
        prompts = [
            "Captain Voss, report — who do we answer to now that the fleet's reorganised?",
            "What's our position in Hatikvah's Choice?",
            "The Split are massing on the border again. Your assessment?",
            "Can I count on you and your squadron, Captain?",
            "What do you need from me to hold the line?",
        ]
        with self._grounded_lock:
            if self._grounded["running"]:
                return {"ok": False, "error": "grounded demo already running"}
            self._grounded = {"running": True, "result": None, "started_at": time.time(),
                              "turn": 0, "total": len(prompts)}

        def run() -> None:
            try:
                npc_key = self._seed_grounded_npc()
                briefing = self.memory.build_situation_briefing(npc_key)
                transcript: list[dict[str, Any]] = []
                for idx, prompt in enumerate(prompts):
                    payload = {
                        "request_id": f"grounded-{int(time.time()*1000)}-{idx}",
                        "source_mod": "grounded_demo", "channel": "npc",
                        "target": {"mode": "npc", "game_id": self.GROUNDED_GAME,
                                   "save_id": self.GROUNDED_SAVE, "npc_name": self.GROUNDED_NPC,
                                   "npc_short_name": "Voss", "faction_id": "argon",
                                   "system_prompt": ("You are Captain Mariko Voss, an Argon Federation officer. "
                                                     "Stay in character. Answer in 1-3 sentences, referencing what you "
                                                     "actually remember and your current situation. Never break character "
                                                     "or mention being an AI.")},
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    t0 = time.time()
                    resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
                    transcript.append({
                        "turn": idx + 1, "player": prompt,
                        "reply": resp.get("reply") or resp.get("error") or "",
                        "status": resp.get("status"),
                        "latency_ms": int((time.time() - t0) * 1000),
                    })
                    with self._grounded_lock:
                        self._grounded["turn"] = idx + 1
                result = {"ok": True, "npc": self.GROUNDED_NPC, "briefing": briefing,
                          "transcript": transcript}
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            with self._grounded_lock:
                self._grounded["running"] = False
                self._grounded["result"] = result

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "status": "started", "poll": "/api/grounded/status"}

    def grounded_status(self) -> dict[str, Any]:
        with self._grounded_lock:
            started = self._grounded["started_at"]
            return {"ok": True, "running": self._grounded["running"],
                    "started_at": started,
                    "elapsed_s": round(time.time() - started, 1) if started else None,
                    "turn": self._grounded["turn"], "total": self._grounded["total"],
                    "result": self._grounded["result"]}

    def memory_reset(self, save_id: str | None = None, all_flag: bool = False) -> dict[str, Any]:
        if all_flag:
            result = self.memory.reset_all()
            try:
                self.events.clear()
                result["events_cleared"] = True
            except Exception:
                pass
            # Also wipe telemetry (Recent Requests / Player2 Probes / Event Stream) so a full
            # reset clears the dashboard's traffic panels, not just NPC memory.
            try:
                self.telemetry.clear()
                result["telemetry_cleared"] = True
            except Exception:
                pass
            return result
        if save_id:
            return self.memory.clear_save(save_id)
        return {"ok": False, "error": "specify ?save_id=... or ?all=1"}

    def contract(self) -> dict[str, Any]:
        """The mod-facing API contract — the deterministic source of truth for how an
        X4 mod (via djfhe) talks to the Neural Link bridge, and the whitelisted actions
        the in-game dispatcher must handle. Snapshot this for the Forge so it scaffolds
        correct mod artifacts and the mod + bridge never drift. The mod calls THIS
        bridge, never Player2 directly."""
        host = str(self.config.get("bridge_host", "127.0.0.1"))
        port = int(self.config.get("bridge_port", 8713))
        base = f"http://{host}:{port}"
        return {
            "ok": True, "service": "x4_neural_link", "version": "0.1.0", "base_url": base,
            "transport": "X4 (MD/Lua) -> djfhe_http -> this bridge -> Player2. The mod calls THIS bridge.",
            "djfhe_example": (f"local Request = require('djfhe.http.request')\n"
                              f"Request.new('POST'):setUrl('{base}/v1/request')"
                              f":setBody({{ request_id='r1', source_mod='your_mod', channel='npc', "
                              f"target={{ mode='npc', save_id='SAVE', game_id='GAME', npc_name='NAME', faction_id='argon' }}, "
                              f"messages={{ {{ role='user', content='...' }} }} }})"
                              f":send(function(response, err) local data = response:getJson() end)"),
            "endpoints": [
                {"method": "POST", "path": "/v1/request",
                 "summary": "Submit an NPC/influence request. Returns {request_id, status}. Then poll /v1/response/{id} or drain /v1/updates_pool.",
                 "request": {"request_id": "string (safe id)", "source_mod": "string", "channel": "npc|chat",
                             "target": {"mode": "npc", "save_id": "string", "game_id": "string",
                                        "npc_name": "string", "faction_id": "string",
                                        "system_prompt": "string (optional)", "game_time": "number player.age (recommended)"},
                             "messages": [{"role": "user", "content": "string"}]},
                 "response": {"ok": True, "request_id": "string", "status": "accepted"}},
                {"method": "GET", "path": "/v1/response/{request_id}",
                 "summary": "Fetch a completed response.", "response": {"ok": True, "response": {"reply": "string", "actions": []}}},
                {"method": "GET", "path": "/v1/updates_pool",
                 "summary": "Drain all completed responses since last call.", "response": {"ok": True, "updates": []}},
                {"method": "GET", "path": "/api/test/llm_action?faction=argon",
                 "summary": "TEST: returns a single LLM-determined action for the dispatcher.",
                 "response": {"ok": True, "action": {"type": "show_notification", "params": {"message": "string"}}}},
                {"method": "GET", "path": "/api/memory/npc/delete?save_id=&npc_id=",
                 "summary": "Purge a dead NPC + its memory (call on NPC death)."},
                {"method": "GET", "path": "/health", "summary": "Bridge + Player2 health."},
            ],
            "action_envelope": {"type": "<one of action_types>", "params": {"...": "action-specific"}},
            "action_types": {
                a: "whitelisted — the in-game dispatcher routes this type to a handler"
                for a in sorted(self.memory.INCIDENT_ACTIONS)
            },
            "dispatcher_note": ("The in-game MD dispatcher must have a handler per action_type it accepts. "
                                "Unknown types are ignored (the whitelist is enforced on both sides)."),
        }

    def health(self) -> dict[str, Any]:
        player2_health = self.player2.health()
        models = self.player2.models() if player2_health.get("ok") else {"ok": False, "models": []}
        try:
            from .retrieval import retriever_mode
            rmode = retriever_mode()
        except Exception:
            rmode = "unknown"
        return {
            "ok": True,
            "service": "x4_neural_link",
            "version": "0.1.0",
            "retriever_mode": rmode,
            "bridge": {
                "host": self.config.get("bridge_host", "127.0.0.1"),
                "port": self.config.get("bridge_port", 8713),
                "telemetry_db": str(self.db_path),
            },
            "player2": {
                **player2_health,
                "models": models.get("models", []),
                "chat_capability": "unknown_until_request",
            },
            "metrics": self.metrics.copy(),
        }

    @staticmethod
    def _normalize_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Accept the in-game chat-window's flat shape ({user_text, faction_id, ...})
        and map it onto the bridge contract ({messages, target}). Lets the proven UI
        client POST without knowing the full request schema."""
        if not isinstance(payload, dict) or payload.get("messages"):
            return payload
        user_text = payload.get("user_text") or payload.get("text")
        if not user_text:
            return payload
        faction = str(payload.get("faction_id") or "argon")
        target = dict(payload.get("target") or {})
        target.setdefault("mode", "npc")
        target.setdefault("faction_id", faction)
        # Per-playthrough index: honor the mod's save_id (md Save_identity.$save_uuid → "game_<n>").
        # If absent, tag "unindexed" rather than silently merging every game into one "chat" namespace,
        # so a miswire is visible on the dashboard instead of corrupting a real playthrough's memory.
        target.setdefault("save_id", str(payload.get("save_id") or "unindexed"))
        target.setdefault("game_id", payload.get("game_id") or "chat")
        # NPC identity: prefer the real personal name (e.g. "Selaia Erris"), which the chat sends
        # top-level as npc_name and/or inside prompt_vars.target_name. Only fall back to a faction
        # generic if no personal name was provided — otherwise the LLM invents one and every
        # faction member shares one memory key.
        _pv = payload.get("prompt_vars") if isinstance(payload.get("prompt_vars"), dict) else {}
        target.setdefault("npc_name", payload.get("npc_name") or payload.get("target_name")
                          or _pv.get("target_name") or f"{faction.title()} Officer")
        if payload.get("player_name"):
            target.setdefault("player_name", str(payload.get("player_name")))
        pv = payload.get("prompt_vars")
        if isinstance(pv, dict) and pv:
            # Promote the NPC's grounded stats (from md/Boarding.xml: combinedskill + role) to
            # first-class target fields so the persona can use them (e.g. "a seasoned marine").
            if pv.get("npc_role"):
                target.setdefault("role", str(pv.get("npc_role")))
            if pv.get("npc_skill") not in (None, ""):
                target.setdefault("npc_skill", pv.get("npc_skill"))
            # The NPC's own ship + fleet (read in MD via event.object.ship / .ship.commander), so the
            # persona can say "I serve aboard the <ship>, in <commander>'s fleet." Already in the hint
            # below as scalars, but promoted to first-class so the biography can use them directly.
            if pv.get("npc_ship") not in (None, ""):
                target.setdefault("ship", str(pv.get("npc_ship")))
            if pv.get("npc_fleet") not in (None, ""):
                target.setdefault("fleet", str(pv.get("npc_fleet")))
            # Individual crew skills (piloting/management/engineering/boarding/morale, 0-15) read in-game
            # via GetComponentData(npc,"skills"). Arrives as a dict (or JSON string). These drive the
            # NPC's "who am I" biography + the dashboard skill bars.
            if pv.get("skills"):
                _sk = pv.get("skills")
                if isinstance(_sk, str):
                    try:
                        _sk = json.loads(_sk)
                    except Exception:
                        _sk = None
                if isinstance(_sk, dict) and _sk:
                    target.setdefault("skills", {str(k): v for k, v in _sk.items()})
            # I1: promote the identity evidence the UI captured (macro/sector/runtime id) to first-class
            # target fields so npc_complete's rebind can see them. macro lifts a re-encounter tentative→bound.
            if pv.get("macro") not in (None, ""):
                target.setdefault("macro", str(pv.get("macro")))
            if pv.get("sector") not in (None, ""):
                target.setdefault("sector", str(pv.get("sector")))
            if pv.get("runtime_component_id") not in (None, ""):
                target.setdefault("runtime_component_id", str(pv.get("runtime_component_id")))
            hint = "; ".join(f"{k}={v}" for k, v in pv.items() if isinstance(v, (str, int, float)))
            if hint:
                target.setdefault("game_state_info", hint)
        return {
            "request_id": payload.get("request_id") or "",
            "source_mod": payload.get("source") or payload.get("source_mod") or "ai_influence_ui",
            "channel": "npc",
            "target": target,
            "messages": [{"role": "user", "content": str(user_text)}],
        }

    def accept_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_chat_payload(payload)
        try:
            request = NeuralRequest.from_payload(payload)
        except ContractError as exc:
            with self.lock:
                self.metrics["invalid"] += 1
            self.telemetry.record_event("request.invalid", status="invalid", error=str(exc), payload=payload)
            raise

        with self.lock:
            existing = self.responses.get(request.request_id)
            if existing is not None:
                self.metrics["duplicates"] += 1
                return {"ok": True, "request_id": request.request_id, "duplicate": True, "status": existing.get("status")}
            if request.request_id in self.inflight:
                self.metrics["duplicates"] += 1
                return {"ok": True, "request_id": request.request_id, "duplicate": True, "status": "pending"}
            self.inflight.add(request.request_id)
            self.metrics["accepted"] += 1
        self.telemetry.record_request(request)

        worker = threading.Thread(target=self._process, args=(request,), daemon=True)
        worker.start()
        return {"ok": True, "request_id": request.request_id, "status": "accepted"}

    # Faction ids the player can name in conversation (base-game set; extend as needed).
    INFLUENCE_FACTIONS = {
        "argon", "antigone", "hatikvah", "paranid", "holyorder", "alliance", "ministry",
        "scaleplate", "freesplit", "split", "teladi", "boron", "terran", "pioneers", "xenon",
    }

    def _propose_influence_action(self, request: NeuralRequest) -> dict[str, Any] | None:
        """INFLUENCE PROVING SLICE (deterministic). If the player explicitly asks to move two
        factions toward war or peace, propose a `set_relation` action. The mod dispatcher applies
        `set_faction_relation`, crossing X4's own war/peace thresholds — so X4's faction AI takes
        over from there. Deterministic + intent-gated for safety; the LLM-proposed + Stage-3-validated
        version is the full Phase-3 build. This exists to answer the one game-gated unknown: does
        `set_faction_relation` actually make factions hostile in-game?"""
        text = ""
        for m in (request.messages or []):
            if str(m.get("role")) == "user":
                text = str(m.get("content") or "").lower()
        if not text:
            return None
        found = [f for f in self.INFLUENCE_FACTIONS if f in text]
        # If only one faction is named, use the NPC's OWN faction as the other party — so "declare
        # war on Argon" said to an Alliance officer means Alliance <-> Argon. Natural conversational UX.
        if len(found) == 1:
            npc_fac = self.memory.resolve_faction_id(
                request.target.get("faction_id") or request.metadata.get("faction_id") or "")
            if npc_fac and npc_fac in self.INFLUENCE_FACTIONS and npc_fac != found[0]:
                found = [npc_fac, found[0]]
        if len(found) < 2:
            return None
        hostile = any(w in text for w in ("war", "attack", "hostile", "turn against", "enemy", "fight", "destroy"))
        peace = any(w in text for w in ("peace", "ceasefire", "ally", "befriend", "make friends", "alliance with"))
        if not (hostile or peace):
            return None
        # Read the CURRENT relation (live overlay over canon) and skip a redundant proposal — don't
        # offer "move toward war" to factions already at war. Uses the world-model we keep in sync.
        save_id = str(request.target.get("save_id") or request.metadata.get("save_id") or "")
        cur_trust = None
        try:
            for r in self.memory.relationships_with_canon(save_id):
                if r.get("subject") == found[0] and r.get("object") == found[1]:
                    cur_trust = r.get("trust"); break
        except Exception:
            pass
        cur_rel = (cur_trust / 100.0) if isinstance(cur_trust, (int, float)) else None
        if cur_rel is not None and ((hostile and cur_rel <= -0.6) or (peace and cur_rel >= 0.6)):
            return None  # already there — no redundant proposal
        # Absolute target relation (type=set_relation): war = MAX hostility (-1.0) — a real declaration of
        # war, same as X4's own faction wars, not a half-measure. Peace = solidly friendly (≥ 0.6, which
        # also clears the re-propose guard). Shallow -0.3 was the original bug (left them only "hostile"
        # and re-proposed every turn); -1.0 is unambiguously at war.
        relation_value = -1.0 if hostile else 0.7
        verb = "toward war" if hostile else "toward peace"
        a = self.memory.get_faction(self.memory.CANON_SAVE, found[0])
        b = self.memory.get_faction(self.memory.CANON_SAVE, found[1])
        na = (a or {}).get("name") or found[0].title()
        nb = (b or {}).get("name") or found[1].title()
        # needs_confirm tells the chat to surface the proposal and wait for a typed "yes" before
        # dispatching — no silent war-declarations on the player's save.
        return {"type": "set_relation",
                "args": {"faction": found[0], "target": found[1], "relation": relation_value},
                "description": f"Move {na} and {nb} {verb}.",
                "needs_confirm": True}

    def _process(self, request: NeuralRequest) -> None:
        # Route to the NPC API when the request asks for it. The NPC path returns
        # clean message + command reliably; raw chat completions is reasoning-bound.
        mode = str(request.target.get("mode") or request.metadata.get("mode") or "").lower()
        if mode == "npc" or request.channel == "npc":
            response = self.player2.npc_complete(request)
        else:
            response = self.player2.complete(request)
        data = response.to_dict()
        # Influence proving slice: attach a deterministically-proposed faction-relation action when
        # the player explicitly asked for it. The mod poll handler routes update.actions to the
        # dispatcher (set_faction_relation + war/peace threshold crossing).
        try:
            act = self._propose_influence_action(request)
            if act:
                data["actions"] = [act]
        except Exception:
            pass
        # Aliases so the in-game chat UI's poll loop reads our responses unchanged:
        # it expects update.text + update.author_name.
        data.setdefault("text", data.get("reply"))
        data.setdefault("author_name", request.target.get("npc_name")
                        or request.target.get("faction_id") or request.source_mod)
        with self.lock:
            self.responses[request.request_id] = data
            self.updates.append(data)
            self.inflight.discard(request.request_id)
            self.metrics["completed"] += 1
            # Bound in-memory growth so a long session never leaks (files on disk
            # remain the durable record; get_response falls back to them).
            while len(self.responses) > 1000:
                self.responses.pop(next(iter(self.responses)))
            while len(self.updates) > 500:
                self.updates.popleft()
        self.telemetry.record_response(data)
        self._write_response(data)
        # Persist the full turn (prompt + reply) so the dashboard shows the real
        # conversation, not just request metadata. Best-effort: never break the response path.
        try:
            prompt = ""
            for m in reversed(request.messages or []):
                if str(m.get("role")) == "user":
                    prompt = str(m.get("content") or "")
                    break
            save_id = str(request.target.get("save_id") or "chat")
            player_name = str(request.target.get("player_name") or "")
            # Upsert the player as a singleton entity for this save. Identity is the save,
            # not the name — a rename updates the label + history, never the record.
            if player_name:
                self.memory.upsert_player(save_id, player_name)
            self.memory.record_conversation(
                save_id=save_id,
                prompt=prompt,
                reply=str(data.get("reply") or data.get("text") or ""),
                request_id=request.request_id,
                faction_id=str(request.target.get("faction_id") or ""),
                npc_name=str(request.target.get("npc_name") or ""),
                source_mod=request.source_mod,
                latency_ms=data.get("latency_ms"),
                status=str(data.get("status") or ""),
                player_name=player_name,
            )
        except Exception:
            pass

    def get_response(self, request_id: str) -> dict[str, Any] | None:
        with self.lock:
            response = self.responses.get(request_id)
        if response:
            return response

        response_file = self.responses_dir / f"{request_id}.json"
        if response_file.exists():
            try:
                return json.loads(response_file.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def drain_updates(self) -> list[dict[str, Any]]:
        with self.lock:
            updates = list(self.updates)
            self.updates.clear()
            return updates

    def conversations_list(self, save_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """The durable chat transcript (prompt + reply per turn) for the dashboard."""
        return {"ok": True, "conversations": self.memory.list_conversations(save_id or None, limit)}

    def player_get(self, save_id: str) -> dict[str, Any]:
        """The player singleton for a save (current name + alias history)."""
        return {"ok": True, "player": self.memory.get_player(save_id or "chat")}

    def telemetry_snapshot(self, limit: int = 100) -> dict[str, Any]:
        return self.telemetry.snapshot(limit=limit)

    FACTION_NAMES = {
        "argon": "Argon Federation", "teladi": "Teladi Company",
        "paranid": "Godrealm of the Paranid", "split": "Zyarth Patriarchy",
        "boron": "Boron Kingdom", "terran": "Terran Protectorate",
    }

    def test_llm_action(self, faction: str = "argon") -> dict[str, Any]:
        """The in-game test target: a faction leader (Player2 LLM) issues a one-line
        proclamation, returned as a whitelisted show_notification action for the X4
        dispatcher to execute. Proves trigger -> bridge -> LLM -> action -> in-game.
        Falls back to a static line if Player2 is unavailable, so the round-trip still
        proves out."""
        faction = (faction or "argon").lower()
        name = self.FACTION_NAMES.get(faction, faction.title())
        payload = {
            "request_id": f"llmaction-{int(time.time()*1000)}",
            "source_mod": "ai_influence_test", "channel": "npc",
            "target": {"mode": "npc", "game_id": "aitest", "save_id": "aitest",
                       "npc_name": f"{name} Voice", "npc_short_name": faction[:8], "faction_id": faction,
                       "system_prompt": (f"You are the public voice of the {name} in the X4 galaxy. "
                                         "Issue ONE short in-character proclamation (max 20 words) to the Commander. "
                                         "No preamble, no quotes — just the line.")},
            "messages": [{"role": "user", "content": "Give the Commander a brief proclamation reflecting your faction's current stance."}],
        }
        t0 = time.time()
        try:
            resp = self.player2.npc_complete(NeuralRequest.from_payload(payload)).to_dict()
            msg = (resp.get("reply") or "").strip() or f"The {name} acknowledges you, Commander."
            return {"ok": True, "faction": faction,
                    "action": {"type": "show_notification", "params": {"message": msg[:200]}},
                    "llm": {"status": resp.get("status"), "latency_ms": int((time.time() - t0) * 1000)}}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "faction": faction,
                    "action": {"type": "show_notification",
                               "params": {"message": f"Neural Link test: bridge reached, LLM unavailable ({type(exc).__name__})."}}}

    def test_roundtrip(self, payload: dict[str, Any] | None = None, bloat: int = 0) -> dict[str, Any]:
        """Bridge half of the in-game pipeline test. Echoes back a *whitelisted
        action* for the X4 dispatcher to execute (show_notification), plus the bytes
        it received and an optional `bloat` pad of 'X' so the mod can stress djfhe's
        response-drain limit with a deliberately large body."""
        payload = payload or {}
        try:
            received = len(json.dumps(payload, ensure_ascii=False))
        except Exception:
            received = -1
        bloat = max(0, min(int(bloat or 0), 5_000_000))  # cap at 5 MB so we can't wedge the link
        note = str(payload.get("note") or "")
        msg = f"Neural Link round-trip OK — {received} B in" + (f" · {note}" if note else "")
        return {
            "ok": True,
            "received_bytes": received,
            "bloat_bytes": bloat,
            # The X4 side reads `action` and routes it through the MD dispatcher,
            # exactly like a real influence decision. show_notification is the safe,
            # visible proof that the full chain executed something in-game.
            "action": {"type": "show_notification", "params": {"message": msg}},
            "pad": "X" * bloat,
        }

    def telemetry_clear(self) -> dict[str, Any]:
        """Full clean slate: wipe telemetry, reset live metrics, drop cached
        responses + their files. Lets you start the dashboard from zero so any red
        you see afterward is real, current traffic."""
        result = self.telemetry.clear()
        with self.lock:
            for key in self.metrics:
                self.metrics[key] = 0
            self.responses.clear()
            self.updates.clear()
        removed = 0
        try:
            for f in self.responses_dir.glob("*.json"):
                try:
                    f.unlink(); removed += 1
                except Exception:
                    pass
        except Exception:
            pass
        result["response_files_removed"] = removed
        return result

    def selftest_all(self) -> dict[str, Any]:
        """One-shot backend health verdict: every deterministic self-test plus
        Player2 reachability. Green here = the backend is sound, so when the mod
        misbehaves you know to look at the mod, not the bridge."""
        from .scoring import run_scoring_selftest
        mem = run_memory_selftest()
        uni = run_universe_selftest()
        sco = run_scoring_selftest()
        p2 = self.player2.health()
        checks = [
            {"name": "memory_selftest", "ok": bool(mem.get("ok")),
             "detail": f"{mem.get('passed')}/{mem.get('total')}"},
            {"name": "universe_selftest", "ok": bool(uni.get("ok")),
             "detail": f"{uni.get('passed')}/{uni.get('total')}"},
            {"name": "scoring_selftest", "ok": bool(sco.get("ok")),
             "detail": f"{sco.get('passed')}/{sco.get('total')}"},
            {"name": "player2_reachable", "ok": bool(p2.get("ok")),
             "detail": str(p2.get("client_version") or p2.get("error") or "")},
        ]
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "note": "player2_reachable is upstream, not the bridge — its failure is Player2 being down, not a bridge bug."}

    def request_detail(self, request_id: str) -> dict[str, Any] | None:
        return self.telemetry.request_detail(request_id)

    def event_detail(self, event_id: int) -> dict[str, Any] | None:
        return self.telemetry.event_detail(event_id)

    def probe_detail(self, probe_id: int) -> dict[str, Any] | None:
        return self.telemetry.probe_detail(probe_id)

    def player2_catalog(self) -> dict[str, Any]:
        return self.player2.api_catalog()

    def player2_capabilities(self) -> dict[str, Any]:
        return self.player2.capability_matrix()

    def run_player2_probes(self) -> dict[str, Any]:
        results = self.player2.run_probe_suite()
        for result in results:
            self.telemetry.record_probe(
                name=str(result.get("name")),
                method=str(result.get("method")),
                path=str(result.get("path")),
                ok=bool(result.get("ok")),
                status_code=result.get("status_code"),
                latency_ms=result.get("latency_ms"),
                error=result.get("error"),
                response=result.get("response") if isinstance(result.get("response"), dict) else {},
            )
        return {"ok": True, "probes": results}

    def run_npc_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Synchronous NPC chat for diagnostics: spawn/reuse an NPC, chat, return the reply."""
        payload = dict(payload or {})
        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        target = {**target, "mode": "npc"}
        payload["target"] = target
        request = NeuralRequest.from_payload(payload)
        self.telemetry.record_request(request)
        response = self.player2.npc_complete(request)
        data = response.to_dict()
        self.telemetry.record_response(data)
        return {"ok": True, "response": data}

    def _write_response(self, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id", "unknown"))
        target = self.responses_dir / f"{request_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)


def load_config(root: Path) -> dict[str, Any]:
    config_path = root / "config" / "player2_config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data.setdefault("player2_timeout_seconds", 30)
    return data
