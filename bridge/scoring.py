"""Neural Link bridge — deterministic influence scoring (Stage 1 of the engine).

The influence engine never lets the LLM decide from raw data. Stage 1 is pure
deterministic math over the stored universe state: given a faction's
`strategic_state` (pressure aggregates) and its `relationships`, produce a ranked
list of *legal, high-scoring* candidate options. Stage 2 (the LLM) only PICKS
among these and narrates; Stage 3 (the validator) re-checks legality before X4
applies anything. Because Stage 1 is deterministic, the mod works with the LLM
off (rule-based fallback = take the top-scoring option) and is fully unit-testable.

Scoring core (Bannerlord-style influence design):

    score(faction, target, action) =
        0.30 * military_pressure
      + 0.20 * economic_pressure
      + 0.15 * recent_losses
      + 0.10 * logistics_stress
      + 0.10 * (-hidden_affinity(faction, target))
      + 0.10 * salient_memory_weight(faction, target)
      + 0.05 * player_alignment(faction)
      - 0.40 * cooldown_active(faction, target, action_class)

Pressures are stored 0..1 (player_alignment -1..1). Weights live in
DEFAULT_WEIGHTS and are overridable per performance profile. This module is
stdlib-only and DB-agnostic — it operates on plain dicts so it can be tested
with fixtures and reused by the strategic-review worker later.
"""

from __future__ import annotations

import time
from typing import Any, Optional

try:  # #65: war/peace eligibility gate (pure module, keeps scoring stdlib-only + DB-agnostic)
    from . import diplomacy as _diplomacy
except ImportError:  # pragma: no cover - non-package execution
    import diplomacy as _diplomacy  # type: ignore

# --- Weights (tunable per performance profile) -------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "military_pressure": 0.30,
    "economic_pressure": 0.20,
    "recent_losses": 0.15,
    "logistics_stress": 0.10,
    "hidden_affinity": 0.10,   # applied to (-hidden_affinity): hostility raises the score
    "salient_memory": 0.10,
    "player_alignment": 0.05,
    "cooldown": 0.40,          # subtracted when a cooldown is active for the action class
}

# --- Provisional candidate action set ----------------------------------------
# These map onto the action whitelist that the NEXT phase formalizes + validates.
# Stage 1 only proposes them; the validator (Stage 3) is the authority on legality.
DIALOGUE = "dialogue_only"        # always-legal benign baseline / no-op
DEFENSIVE = "defensive_stance"    # pull back, fortify
RESOURCE_REQUEST = "resource_request"
ESCALATE = "escalate_pressure"    # hostile pressure toward a resented faction
CEASEFIRE = "ceasefire_feeler"    # sue for peace when bleeding

# G3 (Gameplay Changes doc): Kha'ak/Xenon don't do diplomacy — they have distinct OPERATIONAL aggression
# families. Classed "military" (real orders, like mobilize/patrol), NOT "hostility" — so they execute as orders
# and never hit the #65 war-eligibility gate (which excludes khaak/xenon from RELATION moves).
KHAAK_RAID = "khaak_raid"            # hive pressure / swarm / survival raiding
XENON_INCURSION = "xenon_incursion"  # machine expansion / sector incursion / infrastructure threat

ACTION_CLASS: dict[str, str] = {
    DIALOGUE: "dialogue",
    DEFENSIVE: "military",
    RESOURCE_REQUEST: "economic",
    ESCALATE: "hostility",
    CEASEFIRE: "peace",
    KHAAK_RAID: "military",
    XENON_INCURSION: "military",
}

# G3: which behavior family a faction draws from. khaak->hive, xenon->machine, everyone else->normal diplomacy.
def behavior_kind(faction_id: str) -> str:
    f = str(faction_id or "").strip().lower()
    if f == "khaak":
        return "hive"
    if f == "xenon":
        return "machine"
    return "normal"

# --- #AUTH: authority gating — who is ALLOWED to propose what -----------------
# Minimum NPC authority tier required for each action class. "LLM proposes, system
# disposes": a Tier-0 deck hand can chatter, but only a Tier-3 faction head can move a
# faction toward war or peace. Enforced deterministically; out-of-authority proposals
# are downgraded to the dialogue baseline, never accepted.
#   tier 0 = crew/deckhand · 1 = officer · 2 = commander · 3 = faction head
ACTION_MIN_TIER: dict[str, int] = {
    "dialogue": 0,
    "economic": 1,
    "military": 1,
    "peace": 2,
    "hostility": 3,
}


def action_allowed_for_tier(action: str, npc_tier: Any) -> bool:
    """True if an NPC of `npc_tier` may PROPOSE `action`. Unknown actions need the top tier."""
    try:
        tier = int(npc_tier or 0)
    except (TypeError, ValueError):
        tier = 0
    cls = ACTION_CLASS.get(action, "hostility")
    return tier >= ACTION_MIN_TIER.get(cls, 3)


def filter_by_authority(options: list[dict], npc_tier: Any) -> list[dict]:
    """Drop options whose action class exceeds the NPC's authority tier. Always retains
    the dialogue baseline so there is always at least one legal move (the safe no-op)."""
    allowed = [o for o in options if action_allowed_for_tier(o.get("action", ""), npc_tier)]
    if not any(o.get("action") == DIALOGUE for o in allowed):
        base = next((o for o in options if o.get("action") == DIALOGUE), None)
        if base:
            allowed.append(base)
    return allowed

# --- Candidate-generation gates (when an action becomes *relevant*) -----------
DEFAULT_THRESHOLDS: dict[str, float] = {
    "military": 0.50,        # military_pressure to consider defensive_stance
    "economic": 0.50,        # economic_pressure to consider resource_request
    "logistics": 0.50,       # logistics_stress (alt trigger for defensive / supply)
    "losses": 0.45,          # recent_losses (war-fatigue → ceasefire / defensive)
    "resentment": 25.0,      # relationship resentment to consider escalation
    "affinity_hostile": 0.30,  # |negative affinity| to consider escalation
    "min_score": 0.0,        # options below this are dropped (dialogue always kept)
}

PRESSURE_FIELDS = (
    "military_pressure", "economic_pressure", "logistics_stress", "recent_losses",
    "territorial_pressure", "piracy_pressure", "player_alignment",
)


def _clampf(v: Any, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def hidden_affinity(rel: Optional[dict]) -> float:
    """Friendly→positive, hostile→negative, in [-1, 1]. Derived from the directed
    relationship (trust minus resentment minus fear)."""
    if not rel:
        return 0.0
    raw = (float(rel.get("trust", 0)) - float(rel.get("resentment", 0)) - float(rel.get("fear", 0))) / 100.0
    return _clampf(raw, -1.0, 1.0)


def salient_memory_weight(rel: Optional[dict]) -> float:
    """How much this relationship dominates memory: a strong grudge or large debt
    is salient. In [0, 1]."""
    if not rel:
        return 0.0
    raw = (abs(float(rel.get("resentment", 0))) + abs(float(rel.get("debt", 0)))) / 100.0
    return _clampf(raw, 0.0, 1.0)


def score_option(
    state: dict,
    rel: Optional[dict],
    action: str,
    weights: dict[str, float],
    cooldown_active: bool,
) -> tuple[float, dict[str, float]]:
    """Score one (faction, target, action) candidate. Returns (score, breakdown)."""
    w = weights
    aff = hidden_affinity(rel)
    sal = salient_memory_weight(rel)
    pa = _clampf(state.get("player_alignment", 0.0), -1.0, 1.0)
    terms = {
        "military_pressure": w["military_pressure"] * _clampf(state.get("military_pressure", 0.0), 0.0, 1.0),
        "economic_pressure": w["economic_pressure"] * _clampf(state.get("economic_pressure", 0.0), 0.0, 1.0),
        "recent_losses": w["recent_losses"] * _clampf(state.get("recent_losses", 0.0), 0.0, 1.0),
        "logistics_stress": w["logistics_stress"] * _clampf(state.get("logistics_stress", 0.0), 0.0, 1.0),
        "neg_affinity": w["hidden_affinity"] * (-aff),
        "salient_memory": w["salient_memory"] * sal,
        "player_alignment": w["player_alignment"] * pa,
        "cooldown": -w["cooldown"] * (1.0 if cooldown_active else 0.0),
    }
    return sum(terms.values()), terms


def generate_candidates(
    faction_id: str,
    state: dict,
    relationships: list[dict],
    thresholds: dict[str, float],
) -> list[tuple[str, str]]:
    """Produce the *relevant, legal* (action, target) candidates for a faction,
    gated by its pressures and relationships. The benign baseline is always present."""
    th = thresholds
    cands: list[tuple[str, str]] = [(DIALOGUE, "self")]

    # G3: Kha'ak/Xenon draw from an OPERATIONAL aggression family, NOT diplomacy. They never sue for peace,
    # request supplies, or do relation escalation — they raid/incurse on existing presence. (Targets are other
    # factions/the player; the scorer + cooldowns still rank/throttle them.)
    kind = behavior_kind(faction_id)
    if kind in ("hive", "machine"):
        op = KHAAK_RAID if kind == "hive" else XENON_INCURSION
        for rel in relationships:
            obj = str(rel.get("object") or "")
            if obj and obj != faction_id:
                cands.append((op, obj))
        return cands

    mil = _clampf(state.get("military_pressure", 0.0), 0.0, 1.0)
    eco = _clampf(state.get("economic_pressure", 0.0), 0.0, 1.0)
    log = _clampf(state.get("logistics_stress", 0.0), 0.0, 1.0)
    loss = _clampf(state.get("recent_losses", 0.0), 0.0, 1.0)

    if mil >= th["military"] or loss >= th["losses"] or log >= th["logistics"]:
        cands.append((DEFENSIVE, "self"))
    if eco >= th["economic"] or log >= th["logistics"]:
        cands.append((RESOURCE_REQUEST, "player"))

    for rel in relationships:
        obj = str(rel.get("object") or "")
        if not obj or obj == faction_id:
            continue
        aff = hidden_affinity(rel)
        resent = float(rel.get("resentment", 0))
        if resent >= th["resentment"] or aff <= -th["affinity_hostile"]:
            cands.append((ESCALATE, obj))
        if loss >= th["losses"] and str(rel.get("standing") or "") in ("hostile", "wary"):
            cands.append((CEASEFIRE, obj))
    return cands


# --- Stage 3: deterministic validator (the system "disposes") ----------------
# High-impact action classes never auto-apply — they require explicit player
# confirmation, so the LLM (or the deterministic picker) can PROPOSE war/peace but
# only the player commits it. Everything else applies once validated.
INCIDENT_CONFIRM_CLASSES = {"hostility", "peace"}


def validate_incident(
    action: str,
    faction_id: str,
    target: str,
    *,
    legal_actions: list[str],
    confidence: Any,
    cooldowns: Optional[dict[tuple, float]] = None,
    recent: Optional[list[dict]] = None,
    npc_tier: Optional[int] = None,
    now: Optional[float] = None,
) -> dict:
    """Stage-3 gate run BEFORE an incident is written/applied. Pure + deterministic.

    Returns {ok, reason, status, requires_confirmation}:
      - still-legal: the action must be in the current ranked option set
      - authority:   if npc_tier given, the action must be within the proposer's tier
      - bounds:      confidence must be a number in [0,1]
      - cooldown:    (faction,target,class) must not be on cooldown
      - idempotency: a same (faction,action,target) in `recent` is a duplicate (apply once)
      - confirmation: hostility/peace are written 'pending' (await player), not 'applied'
    """
    now = now if now is not None else time.time()
    cls = ACTION_CLASS.get(action, "hostility")

    def fail(reason: str, status: str = "rejected") -> dict:
        return {"ok": False, "reason": reason, "status": status, "requires_confirmation": False}

    if action not in set(legal_actions or []):
        return fail("action is not in the current legal option set")
    if npc_tier is not None and not action_allowed_for_tier(action, npc_tier):
        return fail(f"action '{action}' exceeds the proposer's authority tier {npc_tier}")
    # #65 anti-cheat: a war/peace move must be between WAR-ELIGIBLE factions — NEVER the engine-permanent
    # hostiles (khaak/xenon), the player, or non-combatant background factions (civilian/criminal/smuggler/
    # visitor). Pure EXCLUDED check (no DB) ported from DeadAir DynamicWars; see bridge/diplomacy.py.
    if cls in ("hostility", "peace") and target:
        _elig = _diplomacy.war_eligibility(faction_id, target)
        if not _elig.get("eligible"):
            return fail(f"war/peace move is ineligible — {_elig.get('reason')}", status="ineligible")
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return fail(f"confidence {confidence!r} is not numeric")
    if not (0.0 <= conf <= 1.0):
        return fail(f"confidence {conf} out of bounds [0,1]")
    if now < float((cooldowns or {}).get((faction_id, target, cls), 0.0)):
        return fail("action class is on cooldown", status="cooldown")
    for r in (recent or []):
        if (r.get("faction_id") == faction_id
                and r.get("action_type") == action
                and (r.get("target") or "") == (target or "")):
            return fail("duplicate of a recent incident", status="duplicate")

    requires = cls in INCIDENT_CONFIRM_CLASSES
    return {"ok": True, "reason": "validated", "requires_confirmation": requires,
            "status": "pending" if requires else "applied"}


def rank_faction(
    faction_id: str,
    state: dict,
    relationships: list[dict],
    cooldowns: Optional[dict[tuple, float]] = None,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
    top_n: int = 4,
    now: Optional[float] = None,
    npc_tier: Optional[int] = None,
) -> list[dict]:
    """Rank a faction's candidate options by the deterministic score.

    `cooldowns` maps (faction_id, target, action_class) -> expiry epoch seconds;
    an option whose class is on cooldown takes the -0.40 penalty. Returns a list
    of dicts (highest score first), always including the dialogue baseline.
    """
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    cooldowns = cooldowns or {}
    now = now if now is not None else time.time()

    # Index relationships by object for quick per-target lookup.
    rel_by_obj = {str(r.get("object") or ""): r for r in relationships}

    options: list[dict] = []
    for action, target in generate_candidates(faction_id, state, relationships, thresholds):
        rel = rel_by_obj.get(target)
        action_class = ACTION_CLASS.get(action, "dialogue")
        cd_expiry = cooldowns.get((faction_id, target, action_class), 0.0)
        cd_active = now < float(cd_expiry)
        score, breakdown = score_option(state, rel, action, weights, cd_active)
        options.append({
            "action": action,
            "action_class": action_class,
            "target": target,
            "score": round(score, 6),
            "cooldown_active": cd_active,
            "breakdown": {k: round(v, 6) for k, v in breakdown.items()},
        })

    options.sort(key=lambda o: o["score"], reverse=True)
    # Keep options above the floor, but always retain the dialogue baseline as a
    # safe no-op so there's always at least one legal choice.
    kept = [o for o in options if o["score"] >= thresholds["min_score"] or o["action"] == DIALOGUE]
    # #AUTH: if a proposing NPC's tier is known, drop options above its authority.
    if npc_tier is not None:
        kept = filter_by_authority(kept, npc_tier)
    return kept[:top_n] if top_n else kept


# --- Self-test (fixtures, no DB, no LLM) -------------------------------------

def run_scoring_selftest() -> dict:
    """Deterministic oracle for the scoring core. Returns {ok, checks:[...]}."""
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    # Fixture 1 — Split: high aggression + heavy losses, resentment toward Argon.
    split_state = {
        "military_pressure": 0.6, "economic_pressure": 0.3, "recent_losses": 0.5,
        "logistics_stress": 0.4, "player_alignment": -0.2,
    }
    split_rels = [{"object": "argon", "trust": -20, "fear": 10, "resentment": 40, "debt": 0, "standing": "hostile"}]

    # Hand-computed escalate(argon) score (see module docstring weights):
    #  mil .30*.6=.18  eco .20*.3=.06  loss .15*.5=.075  log .10*.4=.04
    #  aff = (-20-40-10)/100 = -0.7 -> -aff term .10*0.7 = .07
    #  sal = (40+0)/100 = .4 -> .10*.4 = .04   pa .05*-0.2 = -.01   cd 0
    #  total = .455
    sc, _ = score_option(split_state, split_rels[0], ESCALATE, DEFAULT_WEIGHTS, False)
    check("formula_matches_hand_calc", abs(sc - 0.455) < 1e-6, f"got {sc}")

    ranked = rank_faction("split", split_state, split_rels)
    top = ranked[0] if ranked else {}
    check("split_escalates_argon",
          top.get("action") == ESCALATE and top.get("target") == "argon",
          f"top={top.get('action')}->{top.get('target')} ({top.get('score')})")

    # Fixture 2 — Boron: peaceful, ally of Argon, low pressures. No escalation.
    boron_state = {
        "military_pressure": 0.1, "economic_pressure": 0.15, "recent_losses": 0.05,
        "logistics_stress": 0.1, "player_alignment": 0.3,
    }
    boron_rels = [{"object": "argon", "trust": 40, "fear": 0, "resentment": 0, "debt": 0, "standing": "ally"}]
    branked = rank_faction("boron", boron_state, boron_rels)
    actions = {o["action"] for o in branked}
    check("boron_no_escalation", ESCALATE not in actions, f"actions={actions}")
    check("boron_has_dialogue_baseline", DIALOGUE in actions, f"actions={actions}")

    # Fixture 3 — cooldown drops the escalation below the benign baseline.
    now = time.time()
    cds = {("split", "argon", "hostility"): now + 600.0}
    cranked = rank_faction("split", split_state, split_rels, cooldowns=cds)
    esc = next((o for o in cranked if o["action"] == ESCALATE), None)
    base = next((o for o in cranked if o["action"] in (DEFENSIVE, DIALOGUE)), None)
    check("cooldown_applies_penalty",
          esc is not None and esc["cooldown_active"] and abs((0.455 - 0.40) - esc["score"]) < 1e-6,
          f"escalate score on cooldown={esc.get('score') if esc else None}")
    check("cooldown_demotes_escalation",
          esc is not None and base is not None and base["score"] > esc["score"],
          f"base={base.get('score') if base else None} vs esc={esc.get('score') if esc else None}")

    # Fixture 4 — every option carries a class and a score breakdown.
    check("options_well_formed",
          all({"action", "action_class", "target", "score", "breakdown"} <= set(o) for o in ranked),
          "")

    # Fixture 5 — #AUTH: a Tier-0 deckhand on the Split can NOT propose escalation (war);
    # only dialogue survives. A Tier-3 head keeps the escalation.
    t0 = rank_faction("split", split_state, split_rels, npc_tier=0)
    t0_actions = {o["action"] for o in t0}
    check("auth_tier0_blocks_escalation", ESCALATE not in t0_actions and DIALOGUE in t0_actions, f"tier0={t0_actions}")
    t3 = rank_faction("split", split_state, split_rels, npc_tier=3)
    check("auth_tier3_allows_escalation", ESCALATE in {o["action"] for o in t3}, f"tier3={[o['action'] for o in t3]}")
    # Direct helper: a Tier-1 officer may request resources (economic) but not declare hostility.
    check("auth_officer_economic_ok", action_allowed_for_tier(RESOURCE_REQUEST, 1) is True, "")
    check("auth_officer_hostility_blocked", action_allowed_for_tier(ESCALATE, 1) is False, "")

    # Fixture 6 — Stage-3 validator: gates before an incident is written.
    legal = [DIALOGUE, DEFENSIVE, ESCALATE]
    # benign defensive validates and applies immediately
    vd = validate_incident(DEFENSIVE, "split", "", legal_actions=legal, confidence=0.4)
    check("stage3_defensive_applies", vd["ok"] and vd["status"] == "applied" and not vd["requires_confirmation"], str(vd))
    # hostility validates but needs player confirmation → 'pending', not applied
    vh = validate_incident(ESCALATE, "split", "argon", legal_actions=legal, confidence=0.5)
    check("stage3_hostility_needs_confirm", vh["ok"] and vh["requires_confirmation"] and vh["status"] == "pending", str(vh))
    # not in the legal option set → rejected
    vi = validate_incident("declare_war", "split", "argon", legal_actions=legal, confidence=0.5)
    check("stage3_illegal_rejected", (not vi["ok"]) and vi["status"] == "rejected", str(vi))
    # confidence out of bounds → rejected
    vb = validate_incident(DEFENSIVE, "split", "", legal_actions=legal, confidence=1.7)
    check("stage3_bounds_rejected", not vb["ok"], str(vb))
    # idempotency: a matching recent incident → duplicate
    recent = [{"faction_id": "split", "action_type": ESCALATE, "target": "argon"}]
    vdup = validate_incident(ESCALATE, "split", "argon", legal_actions=legal, confidence=0.5, recent=recent)
    check("stage3_idempotent_duplicate", (not vdup["ok"]) and vdup["status"] == "duplicate", str(vdup))
    # cooldown active → rejected
    vc = validate_incident(ESCALATE, "split", "argon", legal_actions=legal, confidence=0.5,
                           cooldowns={("split", "argon", "hostility"): time.time() + 600})
    check("stage3_cooldown_rejected", (not vc["ok"]) and vc["status"] == "cooldown", str(vc))
    # authority: tier-0 proposing hostility → rejected
    va = validate_incident(ESCALATE, "split", "argon", legal_actions=legal, confidence=0.5, npc_tier=0)
    check("stage3_authority_rejected", not va["ok"], str(va))

    # #65 anti-cheat: war/peace move toward an EXCLUDED faction is refused (status 'ineligible').
    legal_h = [DIALOGUE, DEFENSIVE, ESCALATE]
    ve_khaak = validate_incident(ESCALATE, "split", "khaak", legal_actions=legal_h, confidence=0.5)
    check("stage3_ineligible_khaak_rejected", (not ve_khaak["ok"]) and ve_khaak["status"] == "ineligible", str(ve_khaak))
    ve_player = validate_incident(ESCALATE, "split", "player", legal_actions=legal_h, confidence=0.5)
    check("stage3_ineligible_player_rejected", (not ve_player["ok"]) and ve_player["status"] == "ineligible", str(ve_player))
    ve_peace = validate_incident(CEASEFIRE, "split", "xenon", legal_actions=[DIALOGUE, CEASEFIRE], confidence=0.5)
    check("stage3_ineligible_peace_with_xenon_rejected", (not ve_peace["ok"]) and ve_peace["status"] == "ineligible", str(ve_peace))
    ve_ok = validate_incident(ESCALATE, "split", "argon", legal_actions=legal_h, confidence=0.5)
    check("stage3_eligible_pair_passes", ve_ok["ok"], str(ve_ok))

    # G3: Kha'ak/Xenon behavior families — distinct operational aggression, no diplomacy; normal factions untouched.
    check("behavior_kind_khaak_hive", behavior_kind("khaak") == "hive")
    check("behavior_kind_xenon_machine", behavior_kind("xenon") == "machine")
    check("behavior_kind_argon_normal", behavior_kind("argon") == "normal")
    _grels = [{"object": "argon", "resentment": 50, "standing": "hostile"}]
    _kh = {a for a, _ in generate_candidates("khaak", {"military_pressure": 0.9}, _grels, DEFAULT_THRESHOLDS)}
    check("khaak_raids_not_diplomacy", KHAAK_RAID in _kh and CEASEFIRE not in _kh and RESOURCE_REQUEST not in _kh, str(_kh))
    check("khaak_raid_is_military_class", ACTION_CLASS.get(KHAAK_RAID) == "military")
    _xe = {a for a, _ in generate_candidates("xenon", {"military_pressure": 0.9}, _grels, DEFAULT_THRESHOLDS)}
    check("xenon_incursion_not_ceasefire", XENON_INCURSION in _xe and CEASEFIRE not in _xe, str(_xe))
    _norm = {a for a, _ in generate_candidates("argon", {"economic_pressure": 0.9}, _grels, DEFAULT_THRESHOLDS)}
    check("normal_faction_unchanged", KHAAK_RAID not in _norm and (RESOURCE_REQUEST in _norm or ESCALATE in _norm), str(_norm))

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}


if __name__ == "__main__":
    import json
    print(json.dumps(run_scoring_selftest(), indent=2))
