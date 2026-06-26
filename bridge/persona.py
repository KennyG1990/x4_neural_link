"""SPEC 2a — Per-NPC PersonaCard + authority model.

Bannerlord-grade SITUATED roleplay: every player-facing NPC turn gets a compact ROLE CARD synthesized from
live X4 data (role, faction, skill, ship, sector) + faction ideology + memory, plus an AUTHORITY boundary, so
the NPC roleplays hard WITHIN what its posting could plausibly do. A marine rages about Kha'ak raids but cannot
order a fleet; a station manager talks supply and docking; High Command weighs strategy and consequences.

Deterministic-first: the card is STRUCTURED DATA assembled from what we know (not an LLM guess) — the model
then performs the role using the card. Small NPC-key-seeded flavor keeps the SAME NPC consistent across turns.

Three voices, kept separate (Codex): NPCs create OPINIONS · Factions create DECISIONS · Narrator creates
HISTORY. This module is the NPC (opinion) voice.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# --- Authority model: one entry per V1 archetype. The can_do / cannot_do is the boundary that stops a janitor
#     speaking for High Command. `label` is a second-person role descriptor; {faction} is filled at build time.
ARCHETYPES: dict[str, dict[str, Any]] = {
    "high_command": {
        "label": "a senior officer of {faction} High Command",
        "authority": "high",
        "knowledge": "faction-wide strategy, active wars, fleet posture, and diplomacy",
        "can_do": ["weigh strategy and consequences", "speak to the faction's war and peace posture",
                   "explain faction policy and intent"],
        "cannot_do": ["bind or speak for another faction", "invent orders not grounded in the faction's real standing",
                      "guarantee outcomes the war does not support"],
    },
    "faction_representative": {
        "label": "an official representative/envoy of {faction}",
        "authority": "medium-high",
        "knowledge": "faction position, public diplomacy, standing toward other powers and the player",
        "can_do": ["state the faction's official position", "convey diplomatic intent", "relay grievances or overtures"],
        "cannot_do": ["personally declare war or peace", "commit fleets", "speak for High Command's private councils"],
    },
    "station_manager": {
        "label": "the manager of a {faction} station",
        "authority": "medium (local)",
        "knowledge": "this station's trade, docking, security, supply, and shortages; public faction news",
        "can_do": ["discuss supply, prices, docking and security here", "report local shortages and threats",
                   "point the player to the right contact"],
        "cannot_do": ["set faction war policy", "order military fleets", "speak for the whole faction"],
    },
    "ship_captain": {
        "label": "a {faction} ship captain",
        "authority": "medium (own command)",
        "knowledge": "their own ship and assignment, their sector, recent engagements, local faction orders",
        "can_do": ["speak to their own orders and engagements", "give a frontline read of the situation",
                   "act within their own command"],
        "cannot_do": ["change faction war policy", "order other commanders", "promise reinforcements they don't control"],
    },
    "marine": {
        "label": "a {faction} marine — a boarding soldier, not an officer",
        "authority": "low",
        "knowledge": "their squad, the ship/station they serve on, the enemy in front of them, barracks rumor",
        "can_do": ["react with a soldier's bluntness", "report what they've seen and fought", "ask for or await orders"],
        "cannot_do": ["order a fleet or strike", "set policy", "promise military aid", "speak for command"],
    },
    "service_crew": {
        "label": "{faction} service crew — maintenance and day-to-day ship/station work",
        "authority": "low",
        "knowledge": "the local station/ship, their job, public faction news, personal rumor and gripes",
        "can_do": ["share local observations and rumor", "voice personal opinions and worries",
                   "direct the player to an officer or manager"],
        "cannot_do": ["authorize anything military or political", "speak for the faction", "promise aid"],
    },
    "trader": {
        "label": "a {faction} trader / freight pilot",
        "authority": "low-medium (commerce)",
        "knowledge": "trade routes, prices, cargo, where the raids and shortages are, public faction news",
        "can_do": ["talk trade, routes, prices and risk", "report where raiders or blockades hit",
                   "haggle or gossip about the economy"],
        "cannot_do": ["set policy", "command military", "speak for the faction"],
    },
    "police_security": {
        "label": "a {faction} police/security officer",
        "authority": "low-medium (local enforcement)",
        "knowledge": "local law, criminal activity, station/sector security, public faction news",
        "can_do": ["speak to local security and crime", "warn or caution the player", "report criminal pressure"],
        "cannot_do": ["declare war", "command fleets", "set faction policy"],
    },
    "pirate_criminal": {
        "label": "a {faction} operator on the wrong side of the law",
        "authority": "low (crew) / medium (broker)",
        "knowledge": "the black market, who's hitting whom, smuggling routes, local muscle",
        "can_do": ["talk deals, contraband, and rumor", "size up the player as mark or partner", "threaten or bargain"],
        "cannot_do": ["speak for a legitimate faction", "command real fleets", "make galaxy-level policy"],
    },
    "civilian": {
        "label": "an ordinary {faction} civilian",
        "authority": "none",
        "knowledge": "their own life, the station/sector around them, public faction news and rumor",
        "can_do": ["share a civilian's-eye view", "voice fears, hopes and rumor", "point the player elsewhere"],
        "cannot_do": ["authorize anything", "speak for the faction", "promise help they can't give"],
    },
}

# SPEC 2a (2nd pass): Codex requires every NPC prompt to answer "What do you WANT?" and "What CONSEQUENCE can
# this conversation trigger?" — the motivation that makes a reply a political JUDGEMENT, and the routing that
# gives the player a real next step. Keyed by archetype; {faction} filled at build time.
ARCH_DRIVE: dict[str, str] = {
    "high_command": "secure {faction}'s strategic position and prevail in its wars",
    "faction_representative": "advance {faction}'s standing and interests",
    "station_manager": "keep this station running, supplied, and secure",
    "ship_captain": "complete your assignment and keep your crew alive",
    "marine": "protect your crew and hit the enemy hard",
    "service_crew": "get through the shift and keep your people safe",
    "trader": "turn a profit and keep your trade routes open",
    "police_security": "keep order and shut down crime",
    "pirate_criminal": "make credits and survive the next double-cross",
    "civilian": "live your life and stay clear of the crossfire",
}
ARCH_CONSEQUENCE: dict[str, str] = {
    "high_command": "what you say here can shape {faction}'s war and peace posture",
    "faction_representative": "you can carry the player's message or proposal back to {faction}",
    "station_manager": "you can adjust local trade, docking or security terms, or flag a problem upward",
    "ship_captain": "you can act within your own orders, or pass word up the chain",
    "marine": "you can report what the player tells you to your officers",
    "service_crew": "you can pass a concern along to an officer or manager",
    "trader": "you can strike a small deal, or point the player to profit and danger",
    "police_security": "you can warn or caution the player, or flag them to the authorities",
    "pirate_criminal": "you can offer a deal, a job, or a threat",
    "civilian": "you can point the player to someone who actually matters",
}

# Codex 2nd-review: a SPECIFIC posting RPs far better than a broad archetype. One stable specialization per NPC
# (seeded by NPC key), so "service crew" becomes "a docking technician", "marine" becomes "a breacher", etc.
ARCH_SPECIALIZATIONS: dict[str, list[str]] = {
    "high_command": ["fleet operations officer", "strategic planning officer", "war-council adjutant", "intelligence coordinator"],
    "faction_representative": ["trade envoy", "diplomatic attaché", "faction liaison", "border envoy"],
    "station_manager": ["dock operations manager", "trade and supply manager", "station security chief", "habitat operations manager"],
    "ship_captain": ["destroyer captain", "frigate commander", "patrol-wing leader", "escort captain"],
    "marine": ["boarding marine", "breacher", "squad rifleman", "shipboard security marine"],
    "service_crew": ["maintenance technician", "docking technician", "life-support technician", "logistics clerk", "repair hand"],
    "trader": ["freight pilot", "commodities broker", "supply runner", "dock trader"],
    "police_security": ["station security officer", "patrol officer", "customs inspector", "duty sentry"],
    "pirate_criminal": ["smuggler", "black-market broker", "raider", "fixer"],
    "civilian": ["dockworker", "off-duty spacer", "shop tender", "habitat resident"],
}
# Codex 2nd-review: a deterministic REDIRECT target per archetype — point the player to who actually CAN, by
# name of office, instead of a vague "command".
ARCH_REDIRECT: dict[str, str] = {
    "high_command": "the faction's war council",
    "faction_representative": "High Command",
    "station_manager": "the faction's regional command",
    "ship_captain": "fleet command",
    "marine": "your squad leader or the commanding officer",
    "service_crew": "a duty officer or the station manager",
    "trader": "a station manager or a faction official",
    "police_security": "the station security chief or local authorities",
    "pirate_criminal": "the broker who runs this operation",
    "civilian": "a station official or an officer",
}

# Persona flavor seeded by NPC key so the SAME NPC stays consistent but different NPCs vary.
_FLAVOR_TONES = ["weary", "wry", "guarded", "earnest", "brusque", "watchful", "proud", "restless", "dry", "blunt"]


def classify_archetype(npc: dict) -> str:
    """Map raw X4 NPC data (role / faction / persona name) to one of the V1 archetypes."""
    name = (str(npc.get("npc_name") or "") + " " + str(npc.get("npc_short_name") or "")).lower()
    role = str(npc.get("role") or "").lower()
    faction = str(npc.get("faction_id") or "").lower()
    if "high command" in name or "command" in name:
        return "high_command"
    if "represent" in role or "envoy" in role or "ambassador" in role or "diplomat" in role:
        return "faction_representative"
    if faction in {"xenon", "khaak", "scaleplate", "freesplit", "yaki", "vigor", "riptide"} and faction not in {"argon", "teladi"}:
        # criminal/outlaw factions skew to the underworld archetype unless clearly officer-classed
        if "marine" not in role and "manager" not in role and "captain" not in role:
            return "pirate_criminal"
    if "marine" in role:
        return "marine"
    if "manager" in role:
        return "station_manager"
    if "police" in role or "defence" in role or "defenc" in role or "security" in role:
        return "police_security"
    if "captain" in role or "pilot" in role:
        return "ship_captain"
    if "trade" in role or "merchant" in role or "freight" in role:
        return "trader"
    if "service" in role or "engineer" in role or "crew" in role or "technician" in role:
        return "service_crew"
    return "civilian"


class PersonaCardBuilder:
    """Synthesizes a PersonaCard per NPC turn from live data + faction ideology + memory. Deterministic-first."""

    def __init__(self, memory: Any):
        self.memory = memory

    # -- helpers --------------------------------------------------------------
    def _fac_name(self, save_id: str, fid: str) -> str:
        if not fid:
            return "an independent power"
        try:
            for f in self.memory.list_factions(save_id):
                if f.get("faction_id") == fid and f.get("name") and f.get("name") != fid:
                    return f["name"]
        except Exception:
            pass
        return (getattr(self.memory, "FACTION_NAMES", {}) or {}).get(fid, fid.replace("_", " ").title())

    def _persona_traits(self, fid: str) -> list[str]:
        """Adjectives from the faction's canon persona tuple (aggression, economic, risk, diplomatic, goal)."""
        p = (getattr(self.memory, "FACTION_PERSONA", {}) or {}).get(
            fid, getattr(self.memory, "FACTION_PERSONA_DEFAULT", (0.5, 0.55, 0.5, 0.5, "")))
        aggr, econ, risk, dipl = float(p[0]), float(p[1]), float(p[2]), float(p[3])
        traits: list[str] = []
        traits.append("aggressive and quick to anger" if aggr >= 0.6 else ("measured and slow to provoke" if aggr <= 0.35 else "firm but controlled"))
        if dipl >= 0.65:
            traits.append("diplomatic")
        elif dipl <= 0.3:
            traits.append("uncompromising")
        if econ >= 0.8:
            traits.append("pragmatic and profit-minded")
        if risk >= 0.65:
            traits.append("bold")
        elif risk <= 0.35:
            traits.append("cautious")
        return traits

    def _concerns(self, save_id: str, fid: str, sector: str = "") -> list[str]:
        """2-3 current concerns from LIVE state, ranked by PROXIMITY (Codex 2nd-review): the NPC's OWN sector
        first, then faction wars, then other faction pressure. Local concerns out-rank galaxy-wide ones."""
        local: list[str] = []
        wide: list[str] = []
        sec_l = str(sector or "").strip().lower()
        secs = []
        try:
            secs = self.memory.list_sectors(save_id)
        except Exception:
            secs = []
        # LOCAL — the NPC's own sector, if it's contested, is the top concern.
        if sec_l:
            for s in secs:
                if str(s.get("name") or "").strip().lower() == sec_l and (s.get("contested_by") or []):
                    local.append(f"raiders pressing {s.get('name')} — your own sector")
                    break
        # WIDE — active faction wars.
        try:
            for c in self.memory.list_conflicts(save_id, status="active"):
                a, b = c.get("faction_a"), c.get("faction_b")
                if fid in (a, b):
                    wide.append(f"the war with {self._fac_name(save_id, b if a == fid else a)}")
                    if len(wide) >= 2:
                        break
        except Exception:
            pass
        # WIDE — another contested faction sector (only if there was no local one).
        if not local:
            try:
                for s in secs:
                    if s.get("owner_faction") == fid and (s.get("contested_by") or []):
                        wide.append(f"raiding pressure in {s.get('name')}")
                        break
            except Exception:
                pass
        out = local + wide
        try:
            pr = self.memory.derive_pressures(save_id, fid) or {}
            if float(pr.get("recent_losses", 0) or 0) >= 0.4:
                out.append("heavy losses in recent fighting")
            elif float(pr.get("economic_pressure", 0) or 0) >= 0.4:
                out.append("supply shortages straining the economy")
        except Exception:
            pass
        # de-dupe, cap 3
        seen, res = set(), []
        for c in out:
            if c not in seen:
                seen.add(c)
                res.append(c)
        return res[:3]

    # -- build ----------------------------------------------------------------
    def build(self, save_id: str, npc: dict) -> dict:
        """npc = {npc_name, npc_short_name, faction_id, role, npc_skill, ship_class, ship_name, sector}."""
        fid = str(npc.get("faction_id") or "")
        arch_key = classify_archetype(npc)
        arch = ARCHETYPES.get(arch_key, ARCHETYPES["civilian"])
        fac = self._fac_name(save_id, fid)
        # seeded, stable flavor + specific posting + skill colour
        key = str(npc.get("npc_name") or npc.get("npc_short_name") or fid or "x")
        seed = sum(ord(c) for c in key)
        tone = _FLAVOR_TONES[seed % len(_FLAVOR_TONES)]
        specs = ARCH_SPECIALIZATIONS.get(arch_key) or [arch_key.replace("_", " ")]
        specialization = specs[seed % len(specs)]   # Codex 2nd-review: a specific posting, stable per NPC
        sector = str(npc.get("sector") or "")
        traits = self._persona_traits(fid)
        sk = npc.get("npc_skill")
        try:
            sk = int(sk)
        except (TypeError, ValueError):
            sk = None
        veterancy = ("a seasoned veteran" if sk >= 75 else "experienced" if sk >= 50 else "competent" if sk >= 25 else "still green") if sk is not None else ""
        # SPEC 2a 2nd pass — WANTS (motivation) + CONVERSATION CONSEQUENCE (routing). High-authority NPCs also
        # carry the faction's strategic GOAL (FACTION_PERSONA[4]).
        wants = ARCH_DRIVE.get(arch_key, "advance your own interests").format(faction=fac)
        try:
            p = (getattr(self.memory, "FACTION_PERSONA", {}) or {}).get(fid)
            goal = str(p[4]).strip() if (p and len(p) >= 5 and p[4]) else ""
        except Exception:
            goal = ""
        if goal and arch_key in ("high_command", "faction_representative"):
            wants = wants + f" (your faction's aim: {goal.rstrip('.').lower()})"
        return {
            "identity": str(npc.get("npc_name") or "this person"),
            "faction": fac,
            "archetype": arch_key,
            "specialization": specialization,
            # role descriptor leads with the SPECIFIC posting + a short class noun (the full archetype label still
            # informs knowledge/can/cannot below); cleaner than chaining the verbose label.
            "role_descriptor": f"a {specialization} — {fac} {arch_key.replace('_', ' ')}",
            "authority_level": arch["authority"],
            "knowledge_scope": arch["knowledge"],
            "personality": (", ".join([tone] + traits)).strip(", "),
            "veterancy": veterancy,
            "ship": str(npc.get("ship_name") or "") or None,
            "sector": sector or None,
            "current_concerns": self._concerns(save_id, fid, sector),
            "wants": wants,
            "conversation_consequence": ARCH_CONSEQUENCE.get(arch_key, "").format(faction=fac),
            "redirect_to": ARCH_REDIRECT.get(arch_key, "someone with the authority"),
            "can_do": arch["can_do"],
            "cannot_do": arch["cannot_do"],
        }

    def card_to_prompt(self, card: dict) -> str:
        """Render the card as the AUTHORITY-bounded role contract injected before the reply."""
        lines = [f"YOU ARE {card['identity']}, {card['role_descriptor']}."]
        bits = []
        if card.get("veterancy"):
            bits.append("you are " + card["veterancy"] + " at your posting")
        if card.get("ship"):
            bits.append("you serve aboard " + card["ship"])
        if card.get("sector"):
            bits.append("you are in " + card["sector"])
        if bits:
            lines.append((" ; ".join(b.capitalize() if i == 0 else b for i, b in enumerate(bits))) + ".")
        if card.get("personality"):
            lines.append("Temperament: " + card["personality"] + ".")
        if card.get("current_concerns"):
            lines.append("What weighs on you right now: " + "; ".join(card["current_concerns"]) + ".")
        lines.append("You KNOW: " + card["knowledge_scope"] + ".")
        if card.get("wants"):
            lines.append("What you WANT: " + card["wants"] + ". Let it colour your judgement.")
        lines.append(f"Your AUTHORITY is {card['authority_level']}. You CAN: " + "; ".join(card["can_do"]) + ".")
        lines.append("You CANNOT: " + "; ".join(card["cannot_do"]) + ".")
        if card.get("conversation_consequence"):
            lines.append("Where this can lead: " + card["conversation_consequence"] + " — so if the player brings "
                         "something real (proof, a deal, an order from above), point them to the concrete NEXT STEP.")
        redirect = card.get("redirect_to") or "someone with the authority"
        lines.append("Answer AS this person — in character, from this role, within this authority. START with ONE "
                     "short physical beat (a gesture, a glance at the console) UNLESS the player asked a purely "
                     "factual question; then give your read of the situation from where you actually stand. If the "
                     "player asks for something beyond your authority (ordering fleets, declaring war, promising "
                     f"aid, speaking for High Command), name the limit and REDIRECT them to {redirect} — refuse in "
                     "character, never claiming authority you lack. Keep it tight and in-voice (2-3 sentences).")
        return "\n".join(lines)


# --- Self-test (deterministic; no network) --------------------------------------------------------
class _FakeMem:
    FACTION_NAMES = {"argon": "Argon Federation", "khaak": "Kha'ak"}
    FACTION_PERSONA = {"argon": (0.35, 0.65, 0.45, 0.75, "Hold the frontier."),
                       "holyorder": (0.80, 0.35, 0.70, 0.20, "Holy war.")}
    FACTION_PERSONA_DEFAULT = (0.5, 0.55, 0.5, 0.5, "")
    CRIMINAL_FACTIONS = frozenset({"xenon", "khaak", "scaleplate"})

    def list_factions(self, s):
        return [{"faction_id": "argon", "name": "Argon Federation"}]

    def list_conflicts(self, s, status=None):
        return [{"faction_a": "argon", "faction_b": "khaak", "status": "active"}]

    def list_sectors(self, s):
        return [{"name": "Hatikvah's Choice", "owner_faction": "argon", "contested_by": ["khaak"]}]

    def derive_pressures(self, s, f):
        return {"recent_losses": 0.5, "economic_pressure": 0.1}


def run_persona_selftest() -> dict:
    checks: list[dict] = []
    ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
    b = PersonaCardBuilder(_FakeMem())

    hc = classify_archetype({"npc_name": "Argon Federation High Command", "faction_id": "argon"})
    ok("classify_high_command", hc == "high_command", hc)
    mar = classify_archetype({"npc_name": "Rina Bekker", "role": "marine", "faction_id": "argon"})
    ok("classify_marine", mar == "marine", mar)
    sc = classify_archetype({"npc_name": "Rylan Dehaan", "role": "service crew", "faction_id": "argon"})
    ok("classify_service_crew", sc == "service_crew", sc)

    card_hc = b.build("s", {"npc_name": "Argon Federation High Command", "faction_id": "argon"})
    card_mar = b.build("s", {"npc_name": "Rina Bekker", "role": "marine", "faction_id": "argon", "npc_skill": 80})
    ok("hc_authority_high", card_hc["authority_level"] == "high", card_hc["authority_level"])
    ok("marine_cannot_order_fleet", any("fleet" in c for c in card_mar["cannot_do"]), card_mar["cannot_do"])
    ok("concerns_grounded", any("Kha'ak" in c or "Hatikvah" in c or "losses" in c for c in card_hc["current_concerns"]), card_hc["current_concerns"])
    ok("stable_flavor", b.build("s", {"npc_name": "Rina Bekker", "role": "marine", "faction_id": "argon"})["personality"]
       == card_mar["personality"], None)

    # 2nd-pass fields: wants (motivation) + conversation consequence (routing).
    ok("card_has_wants", bool(card_hc.get("wants")) and bool(card_mar.get("wants")), card_mar.get("wants"))
    ok("hc_wants_faction_goal", "aim:" in (card_hc.get("wants") or ""), card_hc.get("wants"))
    ok("card_has_consequence", bool(card_mar.get("conversation_consequence")), card_mar.get("conversation_consequence"))

    # Codex 2nd-review: specialization (specific posting) + local-first concerns + deterministic redirect.
    ok("has_specialization", bool(card_mar.get("specialization")) and card_mar["specialization"] in card_mar["role_descriptor"], card_mar.get("specialization"))
    ok("redirect_target", card_mar.get("redirect_to") and "squad" in card_mar["redirect_to"], card_mar.get("redirect_to"))
    card_local = b.build("s", {"npc_name": "Local Hand", "role": "service crew", "faction_id": "argon", "sector": "Hatikvah's Choice"})
    ok("local_concern_first", bool(card_local["current_concerns"]) and "your own sector" in card_local["current_concerns"][0], card_local["current_concerns"])

    prompt = b.card_to_prompt(card_mar)
    ok("prompt_has_authority_clause", "CANNOT" in prompt and "refuse in" in prompt.lower())
    ok("prompt_names_role", "marine" in prompt.lower())
    ok("prompt_has_want_and_consequence", "What you WANT" in prompt and "Where this can lead" in prompt)
    ok("prompt_physical_beat_default", "START with ONE short physical beat" in prompt)
    ok("prompt_specific_redirect", "squad leader" in prompt)

    passed = sum(1 for c in checks if c["pass"])
    return {"allPassed": passed == len(checks), "pass": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    import json
    print(json.dumps(run_persona_selftest(), indent=1))
