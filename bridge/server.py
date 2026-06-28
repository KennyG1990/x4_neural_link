from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .contracts import ContractError
from .router import NeuralRouter, load_config


class NeuralLinkHandler(BaseHTTPRequestHandler):
    router: NeuralRouter

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/" or parsed.path == "/dashboard":
            self._send_file(self.router.root / "dashboard" / "index.html")
            return
        if parsed.path.startswith("/dashboard/"):
            rel = parsed.path.removeprefix("/dashboard/")
            if "/" in rel:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            self._send_file(self.router.root / "dashboard" / rel)
            return
        if parsed.path == "/health":
            self._send_json(200, self.router.health())
            return
        if parsed.path == "/api/contract":
            self._send_json(200, self.router.contract())
            return
        if parsed.path == "/api/telemetry":
            limit = self._limit(query.get("limit", ["100"])[0])
            self._send_json(200, self.router.telemetry_snapshot(limit=limit))
            return
        if parsed.path.startswith("/api/telemetry/request/"):
            request_id = unquote(parsed.path.rsplit("/", 1)[-1])
            detail = self.router.request_detail(request_id)
            if detail is None:
                self._send_json(404, {"ok": False, "error": "request not found", "request_id": request_id})
                return
            self._send_json(200, {"ok": True, "request": detail})
            return
        if parsed.path.startswith("/api/telemetry/event/"):
            event_id = self._int_id(parsed.path.rsplit("/", 1)[-1])
            if event_id is None:
                self._send_json(400, {"ok": False, "error": "invalid event id"})
                return
            detail = self.router.event_detail(event_id)
            if detail is None:
                self._send_json(404, {"ok": False, "error": "event not found", "id": event_id})
                return
            self._send_json(200, {"ok": True, "event": detail})
            return
        if parsed.path.startswith("/api/telemetry/probe/"):
            probe_id = self._int_id(parsed.path.rsplit("/", 1)[-1])
            if probe_id is None:
                self._send_json(400, {"ok": False, "error": "invalid probe id"})
                return
            detail = self.router.probe_detail(probe_id)
            if detail is None:
                self._send_json(404, {"ok": False, "error": "probe not found", "id": probe_id})
                return
            self._send_json(200, {"ok": True, "probe": detail})
            return
        if parsed.path == "/api/memory/selftest":
            self._send_json(200, self.router.memory_selftest())
            return
        if parsed.path == "/api/memory/role_selftest":
            self._send_json(200, self.router.role_inference_selftest())
            return
        if parsed.path == "/api/memory/reinfer_roles":
            self._send_json(200, self.router.reinfer_roles())
            return
        if parsed.path == "/api/comms/sender_selftest":
            self._send_json(200, self.router.comms_sender_selftest())
            return
        if parsed.path == "/api/identity/selftest":
            self._send_json(200, self.router.npc_identity_selftest())
            return
        if parsed.path == "/api/identity/rebind_selftest":
            self._send_json(200, self.router.npc_rebind_selftest())
            return
        if parsed.path == "/api/identity/promotion_selftest":
            self._send_json(200, self.router.npc_promotion_selftest())
            return
        if parsed.path == "/api/identity/recall_selftest":
            self._send_json(200, self.router.npc_recall_gate_selftest())
            return
        if parsed.path == "/api/identity/soft_confirm_selftest":
            self._send_json(200, self.router.npc_soft_confirm_selftest())
            return
        if parsed.path == "/api/identity/backfill":
            self._send_json(200, self.router.identity_backfill())
            return
        if parsed.path == "/api/identity/reset":
            self._send_json(200, self.router.identity_reset())
            return
        if parsed.path == "/api/identities":
            self._send_json(200, self.router.identities_list())
            return
        if parsed.path == "/api/identity":
            self._send_json(200, self.router.identity_detail(query.get("persistent_npc_key", [""])[0]))
            return
        if parsed.path == "/api/lore/selftest":
            self._send_json(200, self.router.lore_selftest())
            return
        if parsed.path == "/api/lore/status":
            self._send_json(200, self.router.lore_status(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/lore/resolve":
            self._send_json(200, self.router.lore_resolve(
                query.get("q", [""])[0], query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/influence_log":
            self._send_json(200, self.router.influence_log(
                query.get("save_id", [""])[0], int((query.get("limit", ["50"])[0]) or 50)))
            return
        if parsed.path == "/api/suggest":
            # game_id MUST match the chat NPC key namespace ('chat') or generate_suggestions finds no turns
            # and falls back to generic openers instead of conversation-aware follow-ups.
            self._send_json(200, self.router.suggest(
                query.get("save_id", [""])[0], query.get("faction_id", ["argon"])[0],
                query.get("npc_name", ["Officer"])[0],
                query.get("game_id", ["chat"])[0]))
            return
        if parsed.path == "/api/lore/harvest":
            self._send_json(200, self.router.lore_harvest(
                query.get("save_id", ["demo"])[0],
                (query.get("game_path", [""])[0] or None)))
            return
        if parsed.path == "/api/memory/metrics":
            npc_key = query.get("npc_key", [None])[0]
            self._send_json(200, self.router.memory_metrics(npc_key))
            return
        if parsed.path == "/api/memory/stresstest":
            npcs = query.get("npcs", ["100"])[0]
            turns = query.get("turns", ["40"])[0]
            try:
                self._send_json(200, self.router.memory_stress(npcs, turns))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/memory/stress_clear":
            self._send_json(200, self.router.memory_stress_clear())
            return
        if parsed.path == "/api/events/state":
            self._send_json(200, self.router.events_state())
            return
        if parsed.path == "/api/events/simulate":
            npcs = query.get("npcs", ["500"])[0]
            events = query.get("events", ["1"])[0]
            self._send_json(200, self.router.events_simulate(npcs, events))
            return
        if parsed.path == "/api/events/flush":
            self._send_json(200, self.router.events_flush())
            return
        if parsed.path == "/api/events/clear":
            self._send_json(200, self.router.events_clear())
            return
        if parsed.path == "/api/memory/saves":
            self._send_json(200, self.router.memory_saves())
            return
        if parsed.path == "/api/memory/npc/delete":
            self._send_json(200, self.router.npc_delete(
                query.get("save_id", [""])[0], query.get("npc_id", [None])[0],
                query.get("npc_key", [None])[0]))
            return
        if parsed.path == "/api/factions":
            self._send_json(200, self.router.factions_list(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/relationships":
            self._send_json(200, self.router.relationships_list(
                query.get("save_id", [""])[0], query.get("subject", [None])[0]))
            return
        if parsed.path == "/api/universe/seed":
            self._send_json(200, self.router.universe_seed(query.get("save_id", ["demo"])[0]))
            return
        if parsed.path == "/api/strategic_state":
            self._send_json(200, self.router.strategic_state_list(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/strategic/score":
            self._send_json(200, self.router.strategic_score(
                query.get("save_id", [""])[0], query.get("faction", [""])[0]))
            return
        if parsed.path == "/api/strategic/selftest":
            self._send_json(200, self.router.strategic_selftest())
            return
        if parsed.path == "/api/strategic/review":
            self._send_json(200, self.router.review_faction(
                query.get("save_id", [""])[0], query.get("faction", [""])[0]))
            return
        if parsed.path == "/api/strategic/review_all":
            use_llm = query.get("llm", ["0"])[0] in ("1", "true", "yes")
            self._send_json(200, self.router.review_all(query.get("save_id", ["demo"])[0], use_llm))
            return
        if parsed.path == "/api/influence/stress":
            try:
                self._send_json(200, self.router.influence_stress(query.get("factions", ["50"])[0]))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/influence/stress_status":
            self._send_json(200, self.router.influence_stress_status())
            return
        if parsed.path == "/api/incidents":
            self._send_json(200, self.router.incidents_list(
                query.get("save_id", [""])[0], query.get("status", [None])[0]))
            return
        if parsed.path == "/api/agreements":
            self._send_json(200, self.router.agreements_list(
                query.get("save_id", [""])[0], query.get("status", [None])[0]))
            return
        if parsed.path == "/api/economy":
            self._send_json(200, self.router.economy_list(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/sectors":
            self._send_json(200, self.router.sectors_list(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/fleets":
            self._send_json(200, self.router.fleets_list(query.get("save_id", [""])[0]))
            return
        if parsed.path == "/api/conflicts":
            self._send_json(200, self.router.conflicts_list(
                query.get("save_id", [""])[0], query.get("status", [None])[0]))
            return
        if parsed.path == "/api/losses":
            self._send_json(200, self.router.losses_summary(
                query.get("save_id", [""])[0], query.get("faction", [None])[0]))
            return
        if parsed.path == "/api/world_events":
            self._send_json(200, self.router.world_events_list(
                query.get("save_id", [""])[0],
                self._limit(query.get("limit", ["200"])[0]),
                self._int_id(query.get("min_importance", ["1"])[0]) or 1))
            return
        if parsed.path == "/api/universe/selftest":
            self._send_json(200, self.router.universe_selftest())
            return
        if parsed.path == "/api/universe/stress":
            npcs = query.get("npcs", ["2000"])[0]
            factions = query.get("factions", ["60"])[0]
            turns = query.get("turns", ["12"])[0]
            try:
                self._send_json(200, self.router.universe_stress(npcs, factions, turns))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/universe/stress_status":
            self._send_json(200, self.router.universe_stress_status())
            return
        if parsed.path == "/api/population/stress":
            try:
                self._send_json(200, self.router.population_stress(query.get("npcs", ["2000"])[0]))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/population/stress_clear":
            self._send_json(200, self.router.population_stress_clear())
            return
        if parsed.path == "/api/universe/stress_clear":
            self._send_json(200, self.router.universe_stress_clear())
            return
        if parsed.path == "/api/player2/stress":
            calls = query.get("calls", ["10"])[0]
            threads = query.get("threads", ["1"])[0]
            try:
                self._send_json(200, self.router.player2_stress(calls, threads))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/player2/stress_status":
            self._send_json(200, self.router.player2_stress_status())
            return
        if parsed.path == "/api/player2/stress_clear":
            self._send_json(200, self.router.player2_stress_clear())
            return
        if parsed.path == "/api/grounded/run":
            try:
                self._send_json(200, self.router.grounded_demo())
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/grounded/status":
            self._send_json(200, self.router.grounded_status())
            return
        if parsed.path == "/api/telemetry/clear":
            self._send_json(200, self.router.telemetry_clear())
            return
        if parsed.path == "/api/selftest/all":
            self._send_json(200, self.router.selftest_all())
            return
        if parsed.path == "/api/test/roundtrip":
            bloat = self._int_id(query.get("bloat", ["0"])[0]) or 0
            self._send_json(200, self.router.test_roundtrip({}, bloat))
            return
        if parsed.path == "/api/test/llm_action":
            self._send_json(200, self.router.test_llm_action(query.get("faction", ["argon"])[0]))
            return
        if parsed.path == "/api/memory/reset":
            save_id = query.get("save_id", [None])[0]
            all_flag = query.get("all", ["0"])[0] in ("1", "true", "yes")
            self._send_json(200, self.router.memory_reset(save_id, all_flag))
            return
        if parsed.path == "/api/memory/npcs":
            self._send_json(200, self.router.memory_npcs())
            return
        if parsed.path == "/api/memory/npc":
            npc_key = query.get("npc_key", [""])[0]
            detail = self.router.memory_npc_detail(npc_key)
            if detail is None:
                self._send_json(404, {"ok": False, "error": "npc not found", "npc_key": npc_key})
                return
            self._send_json(200, detail)
            return
        if parsed.path == "/api/player2/catalog":
            self._send_json(200, self.router.player2_catalog())
            return
        if parsed.path == "/api/player2/capabilities":
            self._send_json(200, self.router.player2_capabilities())
            return
        if parsed.path == "/api/conversations":
            self._send_json(200, self.router.conversations_list(
                query.get("save_id", [None])[0], self._limit(query.get("limit", ["100"])[0])))
            return
        if parsed.path == "/api/player":
            self._send_json(200, self.router.player_get(query.get("save_id", ["chat"])[0]))
            return
        if parsed.path == "/v1/updates_pool":
            self._send_json(200, {"ok": True, "updates": self.router.drain_updates()})
            return
        # SPEC 1j: drain prominent faction->player communiqués (the mod heartbeat consumes these and surfaces
        # each as an incoming transmission + logbook entry in-game).
        if parsed.path == "/v1/player_comms":
            self._send_json(200, {"ok": True, "comms": self.router.drain_player_comms()})
            return
        # KEYSTONE: fast, LLM-free drain of the influence loop (news/actions/articles/phase_effects). The mod's
        # heartbeat calls THIS instead of the slow influence_step POST; generation happens in a background daemon.
        if parsed.path == "/v1/influence_drain":
            qs = parse_qs(parsed.query or "")
            save_id = (qs.get("save_id") or [""])[0]
            self._send_json(200, self.router.influence_drain({"save_id": save_id}))
            return
        # SPEC 1k: RoleRAG boundary-aware retrieval self-test (deterministic, no LLM/network).
        if parsed.path == "/v1/rolerag/selftest":
            self._send_json(200, self.router.rolerag_selftest())
            return
        # SPEC 2a: PersonaCard authority-model self-test (deterministic).
        if parsed.path == "/v1/persona/selftest":
            self._send_json(200, self.router.persona_selftest())
            return
        # SPEC 2b: Narrator self-test (deterministic).
        if parsed.path == "/v1/narrator/selftest":
            self._send_json(200, self.router.narrator_selftest())
            return
        # SPEC 3: event-priority-hierarchy self-test (deterministic).
        if parsed.path == "/v1/gates/selftest":
            self._send_json(200, self.router.gates_selftest())
            return
        if parsed.path.startswith("/v1/response/"):
            request_id = parsed.path.rsplit("/", 1)[-1]
            response = self.router.get_response(request_id)
            if response is None:
                self._send_json(404, {"ok": False, "error": "response not ready", "request_id": request_id})
                return
            self._send_json(200, {"ok": True, "response": response})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    @staticmethod
    def _limit(value: str) -> int:
        try:
            return max(1, min(500, int(value)))
        except Exception:
            return 100

    @staticmethod
    def _int_id(value: str) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except Exception:
            return None

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/player2/probes":
            self._send_json(200, self.router.run_player2_probes())
            return
        if parsed.path in ("/api/factions", "/api/relationships", "/api/strategic_state"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                if parsed.path == "/api/factions":
                    self._send_json(200, self.router.faction_upsert(payload))
                elif parsed.path == "/api/strategic_state":
                    self._send_json(200, self.router.strategic_state_upsert(payload))
                else:
                    self._send_json(200, self.router.relationship_adjust(payload))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/events/enqueue":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                self._send_json(200, self.router.events_enqueue(payload))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        substrate_post = {
            "/api/incidents": self.router.incident_add,
            "/api/incident_status": self.router.incident_status,
            "/api/agreements": self.router.agreement_add,
            "/api/economy": self.router.economy_upsert,
            "/api/sectors": self.router.sector_upsert,
            "/api/conflicts": self.router.conflict_add,
            "/api/losses": self.router.loss_record,
            "/api/world_events": self.router.world_event_add,
            "/v1/npcs/index": self.router.npc_index,
            "/v1/relation_report": self.router.relation_report,
            "/v1/relations_sync": self.router.relations_sync,
            "/v1/sectors_sync": self.router.sectors_sync,
            "/v1/fleets_sync": self.router.fleets_sync,
            "/v1/logbook_sync": self.router.logbook_sync,
            "/v1/factions_sync": self.router.factions_sync,
            "/v1/influence_step": self.router.influence_step,
            "/v1/influence_prove": self.router.influence_prove,
            "/v1/react_prove": self.router.react_prove,
            "/v1/player_comms/prove": self.router.player_comms_prove,
            "/v1/rolerag/analyze": self.router.rolerag_analyze,
            "/v1/persona/card": self.router.persona_card,
            "/v1/narrator/prove": self.router.narrator_prove,
            "/v1/warphase/test": self.router.warphase_test,
            "/v1/warphase/actuate_selftest": self.router.warphase_actuate_selftest,
            "/v1/warphase/prove": self.router.warphase_prove,
            "/v1/order/prove": self.router.order_prove,
            "/v1/economy/stations": self.router.economy_stations_ingest,
            "/v1/economy/rollup_selftest": self.router.economy_rollup_selftest,
            "/v1/hostile_events": self.router.hostile_events_ingest,
            "/v1/hostile_ledger_selftest": self.router.hostile_ledger_selftest,
            "/v1/diplomacy/eligibility": self.router.diplomacy_eligibility,
            "/v1/diplomacy/eligibility_selftest": self.router.diplomacy_eligibility_selftest,
            "/v1/offers/list": self.router.offers_list,
            "/v1/offers/render": self.router.offers_render,
            "/v1/offers/selftest": self.router.offers_selftest,
            "/v1/offers/supply": self.router.economy_supply_offer,
            "/v1/offers/supply_selftest": self.router.economy_supply_offer_selftest,
            "/v1/offers/patrol": self.router.sector_patrol_offer,
            "/v1/offers/patrol_selftest": self.router.sector_patrol_offer_selftest,
            "/v1/economy/budget_status": self.router.budget_status,
            "/v1/economy/budget_list": self.router.budget_list,  # A1b: faction budgets panel (earned economy)
            "/v1/economy/earned_validate": self.router.earned_validate,
            "/v1/economy/earned_validate_selftest": self.router.earned_validate_selftest,
            "/v1/player/role": self.router.player_role,
            "/v1/player/role_selftest": self.router.player_role_selftest,
            "/v1/memory/audit": self.router.memory_audit,
            "/v1/memory/audit_selftest": self.router.memory_audit_selftest,
            "/v1/memory/promote_facts": self.router.memory_promote_facts,
            "/v1/memory/promote_selftest": self.router.memory_promote_selftest,
            "/v1/memory/reap_selftests": self.router.memory_reap_selftests,
            "/v1/memory/promote_cadence_selftest": self.router.record_turn_promote_selftest,
            "/v1/llm/budget_status": self.router.llm_budget_status,
            "/v1/llm/budget_set": self.router.llm_budget_set,
            "/v1/llm/budget_selftest": self.router.llm_budget_selftest,
            "/v1/agreements/generate": self.router.agreements_generate,
            "/v1/agreements/generate_selftest": self.router.agreements_generate_selftest,
            "/v1/gameplay/tick": self.router.gameplay_tick,
            "/v1/gameplay/tick_selftest": self.router.gameplay_tick_selftest,
            "/v1/social/list": self.router.social_list,
            "/v1/social/briefing_selftest": self.router.social_briefing_selftest,
            "/v1/rumor/propagate": self.router.rumor_propagate,
            "/v1/rumor/list": self.router.rumor_list,
            "/v1/rumor/selftest": self.router.rumor_selftest,
            "/v1/social/event": self.router.social_event,
            "/v1/social/edge_brief": self.router.social_edge_brief,
            "/v1/social/selftest": self.router.social_selftest,
            "/v1/wares_harvest": self.router.wares_harvest,
            "/v1/ensure_canon": self.router.ensure_canon,
            # EPIC I: identity rebind + promotion (payload-bearing POSTs)
            "/v1/identity/rebind": self.router.identity_rebind,
            "/api/identity/rebind": self.router.identity_rebind,
            "/v1/identity/promote": self.router.identity_promote,
            "/api/identity/promote": self.router.identity_promote,
            "/v1/identity/soft_confirm": self.router.identity_soft_confirm,
            "/api/identity/soft_confirm": self.router.identity_soft_confirm,
        }
        if parsed.path in substrate_post:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                self._send_json(200, substrate_post[parsed.path](payload))
                # A2 (IG-3): a selftest leaves deterministic '__*selftest__*' rows. Reap them AFTER the
                # response is sent (so a reap error can never affect the result), so they never
                # accumulate on the live dashboard. One hook covers every selftest, now and future.
                if "selftest" in parsed.path and parsed.path != "/v1/memory/reap_selftests":
                    try:
                        self.router.memory.reap_selftest_saves()
                    except Exception:
                        pass
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/test/roundtrip":
            bloat = self._int_id(parse_qs(parsed.query).get("bloat", ["0"])[0]) or 0
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                self._send_json(200, self.router.test_roundtrip(payload, bloat))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/player2/npc_chat":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
                self._send_json(200, self.router.run_npc_chat(payload))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
            except ContractError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path != "/v1/request":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256_000:
                self._send_json(413, {"ok": False, "error": "invalid request size"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            accepted = self.router.accept_payload(payload)
            self._send_json(202, accepted)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
        except ContractError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    router = NeuralRouter(root=root, config=config)
    NeuralLinkHandler.router = router

    host = str(config.get("bridge_host", "127.0.0.1"))
    port = int(config.get("bridge_port", 8713))
    server = ThreadingHTTPServer((host, port), NeuralLinkHandler)
    print(f"[Neural Link] listening on http://{host}:{port}")
    print(f"[Neural Link] Player2 base URL: {config.get('player2_base_url')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[Neural Link] shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
