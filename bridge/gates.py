"""SPEC 3 — Event priority hierarchy: the validator/executor boundary in SCHEDULER form.

Codex/Ken: 15 seconds is a HEARTBEAT, not a content-generation interval. Today the loop refreshes flat every
tick and turns every faction twitch into news/narration — a polling demo. This layer makes each candidate event
pass through TIERS + GATES before it fires, and then ROUTES it (actuate / news / narrate / comms / store-
silently / suppress). This is what stops a well-built narrator from narrating spam.

TIERS (high → low):
  critical  — war declared/peace, sector ownership change, station/fleet destroyed, major relation threshold.
  strategic — sustained pressure crossings, war-phase moves (mobilise/raid/ceasefire feeler), buildup.
  policy    — escalate / de-escalate / sanction / embargo / patrol / blockade / bounty / convoy.
  local     — crew rumour, officer reactions → update MEMORY, not the logbook.
  ambient   — gossip, morale → cheap, sparse, mostly stored SILENTLY.

GATES (a candidate fires only if ALL pass): importance ≥ tier floor · cooldown expired · state ACTUALLY changed
(the saturation guard, generalised) · faction is AUTHORISED to do it now · not a semantic duplicate of recent
output. Player-facing comms additionally require player_relevant.
"""
from __future__ import annotations

import time
from typing import Any

TIERS = ("critical", "strategic", "policy", "local", "ambient")

# action / event_type → tier
ACTION_TIER: dict[str, str] = {
    # critical — the galaxy genuinely shifted
    "declare_war": "critical", "sue_for_peace": "critical", "form_alliance": "critical",
    "sector_change": "critical", "sector_lost": "critical", "station_destroyed": "critical",
    "fleet_wiped": "critical", "war_threshold": "critical", "peace": "critical",
    "operation_completed": "critical", "objective_secured": "critical",
    # OPORD operation milestones (military command layer) — strategic/policy so they make news without spamming
    "opord_issued": "strategic", "operation_started": "strategic", "operation_failed": "strategic",
    "major_contact": "strategic", "after_action_report": "policy", "frago_issued": "policy",
    "warning_order_created": "local",
    # strategic — war-phase moves + sustained pressure
    "mobilize_fleet": "strategic", "raid_supply_line": "strategic", "fortify_sector": "strategic",
    "request_supplies": "strategic", "demand_reparations": "strategic", "war_exhaustion_warning": "strategic",
    "seek_ceasefire": "strategic", "offer_privateer_contract": "strategic", "buildup": "strategic",
    "shortage_threshold": "strategic",
    # policy — bounded faction decisions
    "escalate_pressure": "policy", "escalate": "policy", "de_escalate": "policy", "deescalate": "policy",
    "sanction": "policy", "impose_embargo": "policy", "patrol": "policy", "blockade": "policy",
    "bounty": "policy", "convoy": "policy", "consolidate": "policy", "expand_economy": "policy", "fortify": "policy",
    # local / ambient — memory-only by default
    "rumor": "local", "officer_reaction": "local", "market_note": "local",
    "morale": "ambient", "gossip": "ambient", "dialogue_only": "ambient",
}

# per-tier policy: importance floor, cooldown (s) per (faction,target,action), and routing flags.
TIER_POLICY: dict[str, dict[str, Any]] = {
    "critical":  {"min_importance": 4, "cooldown_s": 0.0,   "actuate": True,  "news": True,  "narrate": True,  "comms": True,  "store": True},
    "strategic": {"min_importance": 3, "cooldown_s": 150.0, "actuate": True,  "news": True,  "narrate": True,  "comms": False, "store": True},
    "policy":    {"min_importance": 3, "cooldown_s": 420.0, "actuate": True,  "news": True,  "narrate": False, "comms": False, "store": True},
    "local":     {"min_importance": 2, "cooldown_s": 600.0, "actuate": False, "news": False, "narrate": False, "comms": False, "store": True},
    "ambient":   {"min_importance": 1, "cooldown_s": 900.0, "actuate": False, "news": False, "narrate": False, "comms": False, "store": True},
}


def tier_of(action: str) -> str:
    return ACTION_TIER.get(str(action or "").lower(), "policy")


class EventGate:
    """Stateful gate over the heartbeat. `evaluate(save_id, candidate)` → {tier, fire, routes[], reason}.
    Cooldown + semantic-dedup state is in-memory (cheap; the heartbeat is frequent). Routes tell the caller
    what the event is allowed to DO this tick — the hierarchy in one place instead of scattered cooldowns."""

    def __init__(self) -> None:
        self._last: dict[tuple, float] = {}     # (save, faction, target, action) -> last-fire ts (cooldown)
        self._recent: dict[str, list] = {}      # save -> recent semantic signatures (dedup)

    def evaluate(self, save_id: str, candidate: dict) -> dict:
        """candidate keys (all optional except action):
            action/event_type, faction, target, importance(int), state_changed(bool|None),
            authorized(bool|None), player_relevant(bool)."""
        action = str(candidate.get("action") or candidate.get("event_type") or "").lower()
        tier = tier_of(action)
        pol = TIER_POLICY[tier]
        importance = int(candidate.get("importance") or 0)

        def block(reason: str, allow_store: bool = True) -> dict:
            return {"tier": tier, "fire": False, "routes": (["store"] if (allow_store and pol["store"]) else []), "reason": reason}

        # GATE 1 — importance floor for the tier
        if importance and importance < pol["min_importance"]:
            return block("below tier importance floor")
        # GATE 2 — state actually changed (the saturation guard, generalised). False = a no-op (e.g. escalate at -1.0).
        if candidate.get("state_changed") is False:
            return block("no state change (no-op)")
        # GATE 3 — authority: is the faction allowed to do THIS, in its CURRENT state?
        if candidate.get("authorized") is False:
            return block("faction not authorised for this action now", allow_store=False)
        # GATE 4 — cooldown per (faction,target,action)
        now = time.time()
        sig = (save_id, candidate.get("faction"), candidate.get("target"), action)
        if pol["cooldown_s"] and now - self._last.get(sig, 0.0) < pol["cooldown_s"]:
            return block("cooldown")
        # GATE 5 — semantic duplicate of recent output (faction|action|target)
        ssig = f"{candidate.get('faction')}|{action}|{candidate.get('target')}"
        recent = self._recent.setdefault(save_id, [])
        if ssig in recent[-40:]:
            return block("semantic duplicate of recent output")

        # FIRE — route by tier policy (+ player relevance for comms)
        routes = []
        if pol["actuate"]:
            routes.append("actuate")
        if pol["news"]:
            routes.append("news")
        if pol["narrate"]:
            routes.append("narrate")
        if pol["comms"] and candidate.get("player_relevant"):
            routes.append("comms")
        if pol["store"]:
            routes.append("store")
        self._last[sig] = now
        recent.append(ssig)
        if len(recent) > 200:
            del recent[:100]
        return {"tier": tier, "fire": True, "routes": routes, "reason": "passed gates"}


def run_gates_selftest() -> dict:
    checks: list[dict] = []
    ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
    g = EventGate()

    ok("tier_war_critical", tier_of("declare_war") == "critical")
    ok("tier_escalate_policy", tier_of("escalate_pressure") == "policy")
    ok("tier_morale_ambient", tier_of("morale") == "ambient")

    # critical fires with all routes incl narrate; player-relevant adds comms
    r = g.evaluate("s", {"action": "declare_war", "faction": "argon", "target": "khaak", "importance": 4,
                         "state_changed": True, "authorized": True, "player_relevant": True})
    ok("critical_fires", r["fire"] and r["tier"] == "critical", r)
    ok("critical_routes_full", set(["actuate", "news", "narrate", "comms", "store"]).issubset(set(r["routes"])), r["routes"])

    # no-op escalate (state didn't change) -> suppressed, store only (the -1.0 spam)
    r2 = g.evaluate("s", {"action": "escalate_pressure", "faction": "split", "target": "khaak", "importance": 4,
                          "state_changed": False, "authorized": True})
    ok("noop_suppressed", not r2["fire"] and r2["routes"] == ["store"], r2)

    # unauthorised action -> hard suppress (no store)
    r3 = g.evaluate("s", {"action": "declare_war", "faction": "teladi", "target": "argon", "importance": 4,
                          "state_changed": True, "authorized": False})
    ok("unauthorized_blocked", not r3["fire"] and r3["routes"] == [], r3)

    # policy fires once, then cooldown blocks the immediate repeat
    a = g.evaluate("s", {"action": "sanction", "faction": "boron", "target": "split", "importance": 3, "state_changed": True, "authorized": True})
    b = g.evaluate("s", {"action": "sanction", "faction": "boron", "target": "split", "importance": 3, "state_changed": True, "authorized": True})
    ok("policy_cooldown", a["fire"] and not b["fire"] and b["reason"] == "cooldown", (a["fire"], b["reason"]))

    # ambient never narrates/actuates, just stores silently
    r4 = g.evaluate("s", {"action": "morale", "faction": "argon", "importance": 1, "state_changed": True, "authorized": True})
    ok("ambient_store_only", r4["fire"] and r4["routes"] == ["store"], r4)

    passed = sum(1 for c in checks if c["pass"])
    return {"allPassed": passed == len(checks), "pass": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    import json
    print(json.dumps(run_gates_selftest(), indent=1))
