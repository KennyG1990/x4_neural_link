"""#58 — faction war/peace eligibility validators. Pure + deterministic, ported from DeadAir DynamicWars
(`dynamicwar.xml` ExcludedFactions + isactive; see the x4-reference-mods skill / StarForge canon).

This is the gate #65 needs: chat->relation and ForceWar mutations must pass `war_eligibility()` or be REFUSED,
so the influence layer can never declare war on engine-permanent hostiles (khaak/xenon), drag the player into
auto-war, or mint a war between non-combatant background factions (civilian/criminal/smuggler/visitor).
"""
from __future__ import annotations

from typing import Iterable, Optional

# Ported verbatim from DeadAir dynamicwar.xml:273 / :989 — NEVER subject to dynamic war/peace.
# khaak/xenon = engine-permanent hostiles (not negotiable); civilian/criminal/smuggler/visitor = non-combatant
# background/economic factions; player = excluded from auto-war.
EXCLUDED_FROM_WAR = frozenset({"civilian", "criminal", "khaak", "player", "smuggler", "visitor", "xenon"})


def _norm(x) -> str:
    return str(x or "").strip().lower()


def war_eligibility(a, b, known_factions: Optional[Iterable] = None) -> dict:
    """Can the influence layer put factions a and b INTO (or OUT of) a war?
    Eligible iff: distinct, NEITHER in EXCLUDED_FROM_WAR, and (if a known-faction set is supplied) both are
    active/known to this save."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return {"eligible": False, "reason": "missing faction id"}
    if a == b:
        return {"eligible": False, "reason": "a faction cannot go to war with itself"}
    for f in (a, b):
        if f in EXCLUDED_FROM_WAR:
            return {"eligible": False,
                    "reason": f"'{f}' is excluded from dynamic war (engine-permanent hostile or non-combatant)"}
    if known_factions is not None:
        known = {_norm(k) for k in known_factions}
        for f in (a, b):
            if f not in known:
                return {"eligible": False, "reason": f"'{f}' is not an active faction in this game"}
    return {"eligible": True, "reason": "both factions active and combat-eligible"}


def relation_move_ok(current, delta, lo: float = -1.0, hi: float = 1.0) -> dict:
    """Engine relation scale is [-1, +1] (DeadAir works in +/-25 uivalue; same idea). Clamp the resulting value
    and report whether the requested move stayed in-bounds."""
    try:
        raw = float(current) + float(delta)
    except (TypeError, ValueError):
        return {"ok": False, "clamped": current, "reason": "non-numeric relation"}
    clamped = max(lo, min(hi, raw))
    in_bounds = abs(raw - clamped) < 1e-9
    return {"ok": in_bounds, "clamped": round(clamped, 4),
            "reason": "in bounds" if in_bounds else f"clamped to [{lo}, {hi}]"}


def run_selftest() -> dict:
    checks: list[dict] = []

    def ok(n, p, d=None):
        checks.append({"name": n, "pass": bool(p), "detail": d})

    known = {"argon", "split", "teladi", "paranid", "antigone", "holyorder", "xenon", "khaak"}
    ok("eligible_pair_true", war_eligibility("argon", "split", known)["eligible"] is True)
    ok("khaak_excluded", war_eligibility("argon", "khaak", known)["eligible"] is False)
    ok("xenon_excluded", war_eligibility("paranid", "xenon", known)["eligible"] is False)
    ok("player_excluded", war_eligibility("argon", "player", known)["eligible"] is False)
    ok("civilian_excluded", war_eligibility("civilian", "argon", known)["eligible"] is False)
    ok("self_war_rejected", war_eligibility("argon", "argon", known)["eligible"] is False)
    ok("unknown_faction_rejected", war_eligibility("argon", "narnia", known)["eligible"] is False)
    ok("excludes_even_without_known_set", war_eligibility("argon", "khaak")["eligible"] is False)
    ok("case_insensitive", war_eligibility("Argon", "SPLIT", known)["eligible"] is True)
    r1 = relation_move_ok(0.1, -0.2)
    ok("rel_in_bounds", r1["ok"] is True and r1["clamped"] == -0.1)
    r2 = relation_move_ok(-0.9, -0.5)
    ok("rel_clamped_low", r2["clamped"] == -1.0 and r2["ok"] is False)
    ok("rel_clamped_high", relation_move_ok(0.9, 0.5)["clamped"] == 1.0)
    passed = sum(1 for c in checks if c["pass"])
    return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}
