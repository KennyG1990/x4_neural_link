"""#59 — X4-native mission/offer TEMPLATE catalog. Pure + deterministic, stdlib-only, DB-agnostic.

NPCs offer the player missions grounded in REAL world state (a live shortage, an active conflict, a contested
sector). This module defines the SHAPES (templates) and renders a template + params into a concrete offer dict.

It does NOT grant rewards or mutate the world — an offer is a PROPOSAL; accepting / fulfilling + any reward is a
SEPARATE gated flow (the reward must be EARNED, #63), explicitly out of scope here. Template `kind`s mirror X4's
native mission types so offers read as in-universe. #60 instantiates `supply_delivery` against a real shortage.
"""
from __future__ import annotations

from typing import Optional

# Each template: `kind` mirrors an X4-native mission type; `grounding` names the world-data source #60+ fills.
TEMPLATES: dict[str, dict] = {
    "supply_delivery": {
        "kind": "Deliver Wares",
        "title": "Supply run: {ware}",
        "summary": "{faction} needs {amount} {ware} delivered to {where}. {reason}",
        "required_params": ["faction", "ware", "amount", "where"],
        "grounding": "economy.shortages — a real per-faction shortage (#54)",
        "reward_kind": "credits",
    },
    "bounty": {
        "kind": "Destroy Target",
        "title": "Bounty: {target}",
        "summary": "{faction} wants {target} hit in {where}. {reason}",
        "required_params": ["faction", "target", "where"],
        "grounding": "conflicts — an active located conflict (#62/#66)",
        "reward_kind": "credits",
    },
    "patrol": {
        "kind": "Patrol",
        "title": "Patrol: {where}",
        "summary": "{faction} asks you to patrol {where}, contested by {threat}.",
        "required_params": ["faction", "where", "threat"],
        "grounding": "sectors.contested_by — a real contested home sector",
        "reward_kind": "credits",
    },
    "trade_buy": {
        "kind": "Trade",
        "title": "Buy offer: {ware}",
        "summary": "{faction} will sell you {amount} {ware} at {where}.",
        "required_params": ["faction", "ware", "amount", "where"],
        "grounding": "economy.products — a real surplus the faction exports",
        "reward_kind": "ware",
    },
    "trade_sell": {
        "kind": "Trade",
        "title": "Sell offer: {ware}",
        "summary": "{faction} will buy {amount} {ware} from you at {where}.",
        "required_params": ["faction", "ware", "amount", "where"],
        "grounding": "economy.shortages — a real need the faction imports",
        "reward_kind": "credits",
    },
}

# Optional params that get a benign default so a partly-specified offer still renders cleanly.
DEFAULTS = {"reason": "", "where": "their territory", "threat": "hostiles", "target": "the target"}


def list_templates() -> list[dict]:
    """The catalog (without the raw summary format string)."""
    return [{"id": tid, **{k: v for k, v in t.items() if k != "summary"}} for tid, t in TEMPLATES.items()]


def render_offer(template_id: str, params: Optional[dict] = None) -> dict:
    """Fill a template into a concrete offer. Returns {ok, offer} or {ok:False, reason}.
    Missing REQUIRED params fail loudly — an offer must be fully grounded (no placeholder offers leak to the
    player). Trailing whitespace from an empty {reason} is collapsed."""
    t = TEMPLATES.get(str(template_id or ""))
    if not t:
        return {"ok": False, "reason": f"unknown template '{template_id}'"}
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    missing = [k for k in t["required_params"] if not p.get(k)]
    if missing:
        return {"ok": False, "reason": f"missing required params: {', '.join(missing)}"}
    try:
        title = " ".join(t["title"].format(**p).split())
        summary = " ".join(t["summary"].format(**p).split())
    except (KeyError, IndexError, ValueError) as exc:
        return {"ok": False, "reason": f"template fill error: {exc}"}
    return {"ok": True, "offer": {
        "template_id": template_id, "kind": t["kind"], "title": title, "summary": summary,
        "reward_kind": t["reward_kind"], "grounding": t["grounding"],
        "params": {k: p[k] for k in t["required_params"]},
    }}


def run_selftest() -> dict:
    checks: list[dict] = []

    def ok(n, p, d=None):
        checks.append({"name": n, "pass": bool(p), "detail": d})

    kinds = {t["kind"] for t in TEMPLATES.values()}
    ok("catalog_has_xnative_kinds", kinds >= {"Deliver Wares", "Destroy Target", "Patrol", "Trade"}, sorted(kinds))
    r = render_offer("supply_delivery", {"faction": "Argon Federation", "ware": "Energy Cells",
                                         "amount": "5,000", "where": "Argon Prime",
                                         "reason": "Their stations are critically short."})
    ok("supply_renders", r["ok"] and "Energy Cells" in r["offer"]["summary"] and r["offer"]["kind"] == "Deliver Wares", r)
    ok("supply_no_unfilled_braces", r["ok"] and "{" not in r["offer"]["summary"] and "}" not in r["offer"]["summary"], r)
    miss = render_offer("supply_delivery", {"faction": "Argon Federation", "ware": "Energy Cells"})
    ok("missing_params_rejected", (not miss["ok"]) and "missing" in miss["reason"], miss)
    unk = render_offer("teleport_request", {})
    ok("unknown_template_rejected", not unk["ok"], unk)
    b = render_offer("bounty", {"faction": "Split", "target": "an Argon convoy", "where": "Hatikvah", "reason": "Old grudge."})
    ok("bounty_renders", b["ok"] and b["offer"]["kind"] == "Destroy Target", b)
    empty_reason = render_offer("patrol", {"faction": "Teladi", "where": "Grand Exchange", "threat": "Xenon"})
    ok("optional_default_clean", empty_reason["ok"] and "  " not in empty_reason["offer"]["summary"], empty_reason)
    ok("list_templates_hides_format", all(("id" in t and "kind" in t and "summary" not in t) for t in list_templates()))
    passed = sum(1 for c in checks if c["pass"])
    return {"allPassed": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}
