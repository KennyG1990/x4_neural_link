from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .contracts import ContractError, NeuralRequest, NeuralResponse
from .events import EventQueue
from .memory import (
    MemoryStore, run_memory_selftest, run_memory_stress,
    run_universe_selftest, run_full_stress, run_npc_identity_selftest,
    run_npc_rebind_selftest, run_npc_promotion_selftest,
)
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
        """Give every existing npcs row a persistent identity (idempotent + reversible)."""
        return self.memory.backfill_identities()

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
                game_id: str = "x4_neural_link", count: int = 3) -> dict[str, Any]:
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

        llm_meta = None
        if use_llm:
            fac = self.memory.get_faction(save_id, faction_id) or {}
            llm_meta = self._llm_decide(save_id, faction_id, str(fac.get("name") or faction_id), state, options)
            top = options[llm_meta["index"]]
        else:
            top = options[0]

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
            effects={"source": "llm" if use_llm else "deterministic", "score": top.get("score")})
        if v["requires_confirmation"] and not autonomous:
            # Player-facing path only (e.g. a chat-proposed action): high-impact awaits confirm.
            self.memory.set_incident_status(save_id, inc["id"], "pending")
            applied = None
        else:
            # LIVING UNIVERSE (Ken 2026-06-25): autonomous faction decisions apply WITHOUT player approval —
            # no confirmation friction, no immersion break. The universe acts on its own; the player reacts.
            applied = self.memory.apply_incident_effects(save_id, action, faction_id, eff_target)
            self.memory.set_incident_status(save_id, inc["id"], "applied")
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
            try:
                res = self.influence_step({"save_id": save, "budget": 2})
            except Exception:
                continue
            try:
                self._enqueue_drain(save, res)
            except Exception:
                pass

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
            ag = self.memory.generate_agreements(save_id, max_new=2)
            ags = ag.get("agreements") or []
            out["agreements"] = len(ags)
            if not dry_run:
                for a in ags:
                    fa = self._fac_name(save_id, a.get("party_a"))
                    fb = self._fac_name(save_id, a.get("party_b"))
                    verb = {"ceasefire": "has put out a ceasefire feeler to",
                            "trade": "is proposing a trade pact with"}.get(a.get("type"), "is opening talks with")
                    self.player_comms.append({"title": f"{fa.upper()} DIPLOMATIC OVERTURE", "body": f"{fa} {verb} {fb}.",
                                              "faction": a.get("party_a"), "faction_name": fa, "category": "diplomacy",
                                              "kind": "agreement", "save_id": save_id, "ts": now})
        except Exception:
            pass
        if not dry_run:
            try:
                alt = int(now) % 2 == 0
                r = (self.sector_patrol_offer if alt else self.economy_supply_offer)({"save_id": save_id})
                out["offer"] = ("patrol" if alt else "supply") if r.get("ok") else None
            except Exception:
                pass
            while len(self.player_comms) > 200:
                self.player_comms.popleft()
        return out

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
        return {"title": f"{fname.upper()} {title_word}", "body": body, "faction": fid,
                "faction_name": fname, "category": category, "kind": kind, "save_id": save_id, "ts": time.time()}

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

    def drain_player_comms(self) -> list[dict[str, Any]]:
        """Drain all queued player communiqués since the last call (the mod heartbeat consumes these)."""
        with self.lock:
            items = list(self.player_comms)
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
            rows.append({"faction_id": fid, "capacity": cap, "spent": round(spent, 2),
                         "remaining": round(cap - spent, 2)})
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

    def social_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """#39: apply a social EVENT (saved_life, betrayal, served_together, …) to an NPC↔NPC edge — the only
        sanctioned way relationships change. Mutates scores + evidence + re-derives narrative status."""
        save_id = str(payload.get("save_id") or "unindexed")
        return self.memory.apply_social_event(save_id, str(payload.get("subject_npc") or ""),
                                              str(payload.get("object_npc") or ""),
                                              str(payload.get("event_type") or ""), str(payload.get("note") or ""))

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
