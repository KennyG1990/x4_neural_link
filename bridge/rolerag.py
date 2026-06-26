"""RoleRAG boundary-aware retrieval — SPEC 1k.

Faithful adaptation of RoleRAG (Wang, Leung & Shen, 2025, arXiv:2505.18541) §3.4 "Retrieval Module for
Role-playing", for the X4 Neural Link bridge.

The paper has two modules:
  1. **Entity normalization** — semantic dedup of ambiguous entity names from messy corpora (e.g.
     "Anakin" = "Vader"). NOT needed here: X4 entities arrive from the game's OWN data with canonical ids
     (factions.xml, sectors), so we are canonical-by-construction. We build the entity index directly.
  2. **Boundary-aware retrieval** (this module): given the player's message,
       (a) infer a brief hypothetical context (HyDE) + extract every referenced entity, tagging each
           {name, type, in_scope, specificity, rationale};
       (b) route by tag through the paper's THREE retrieval strategies —
             SPECIFIC in-scope -> that entity's own subgraph (faction relations/wars/agreements/lore;
                                  sector ownership);
             GENERAL           -> the NPC faction's 1-hop neighborhood;
             OUT-OF-SCOPE      -> EXPLICIT rejection: tell the NPC it has no knowledge of the entity
                                  (+rationale) so it refuses instead of hallucinating.
     The out-of-scope rejection is the paper's key anti-hallucination mechanism, and the hardening over a
     blanket "you only know X4" system prompt.

Deterministic-first: known X4 entities are matched without any LLM call (high precision, free). One cheap
LLM classification call adds out-of-scope detection + specific/general for anything the deterministic pass
missed; if the LLM is unavailable the module degrades to deterministic in-scope matching (it never throws,
and it never rejects without evidence).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

try:
    from .retrieval import make_retriever
except ImportError:  # allow direct (non-package) import in tests
    from retrieval import make_retriever

# Entity types that are FACTION-LIKE — a closed, fully-enumerable set in X4. Any such entity that does not
# resolve to our complete faction roster is provably fabricated (the airtight out-of-scope backstop).
_FACTION_LIKE_TYPES = {"faction", "organization", "organisation", "power", "government", "military",
                       "guild", "syndicate", "house", "clan", "empire", "collective", "alliance", "group"}

# Ware-like entity types — a closed set ONCE the ware catalog is harvested (libraries/wares.xml).
_WARE_LIKE_TYPES = {"ware", "commodity", "resource", "good", "goods", "material", "ore", "mineral", "cargo"}

_WORD = re.compile(r"[a-z0-9']+")
_STOP = {"the", "of", "and", "for", "you", "your", "with", "what", "who", "how", "why",
         "are", "does", "did", "this", "that", "them", "they", "from", "into", "about"}

# Canon race/faction ids X4 uses — seeded so recognition works even before the save has populated factions.
_CANON_FACTION_IDS = [
    "argon", "teladi", "paranid", "split", "boron", "ministry", "antigone", "alliance", "holyorder",
    "hatikvah", "freesplit", "scaleplate", "xenon", "khaak", "terran", "pioneers", "yaki", "vigor",
    "riptide", "zyarth", "segaris", "godrealm", "trinity", "fallensplit",
]

_CLASSIFY_SYS = (
    "You are an entity analyzer for a role-playing NPC in the universe of X4: Foundations. The NPC's "
    "cognitive boundary is the X4 universe ONLY: it knows X4 factions, sectors, wares, ships, races, and "
    "galaxy events. It does NOT know Earth, real-world people/places/brands/history, or any other fiction.\n"
    "Given the player's message, FIRST silently infer what the NPC would need to know to answer. THEN list "
    "EVERY named entity referenced in the message or that inferred context. For each entity output: "
    "name; type (faction|sector|ware|ship|race|person|place|concept|other); in_scope (true if it exists in "
    "the X4 universe, false otherwise); specificity (specific=a concrete named thing, general=a broad "
    "concept like trade/war/honour); rationale (one short clause). "
    'Respond with ONLY a compact JSON array, no prose, e.g. '
    '[{"name":"Teladi","type":"faction","in_scope":true,"specificity":"specific","rationale":"an X4 faction"}]. '
    "If there are no entities, respond with []."
)


def _parse_json_array(raw: Any) -> list[dict]:
    """Best-effort extract a JSON array of objects from a model reply (handles code fences / surrounding prose)."""
    s = str(raw or "").strip()
    if not s:
        return []
    if "```" in s:
        s = re.sub(r"```(?:json)?", "", s).strip()
    a, b = s.find("["), s.rfind("]")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    try:
        data = json.loads(s)
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception:
        return []


class EntityIndex:
    """Canonical X4 entity set (factions, sectors, races) + a vector index for fuzzy match. Built straight
    from the game's own data via the memory store — names are already canonical, so no normalization pass."""

    def __init__(self, memory: Any, save_id: str):
        self.memory = memory
        self.save_id = save_id
        self.entities: list[dict] = []        # {key, name, type, aliases:set[str], text}
        self._alias: dict[str, dict] = {}     # alias_lower -> entity
        self._keys: set[str] = set()
        self._build()

    def _add(self, key: str, name: str, etype: str, aliases: set, text: str = "") -> None:
        if key in self._keys:
            return
        ent = {"key": key, "name": name, "type": etype,
               "aliases": {a.lower() for a in aliases if a}, "text": text or name}
        self.entities.append(ent)
        self._keys.add(key)
        for a in ent["aliases"]:
            self._alias.setdefault(a, ent)

    def _build(self) -> None:
        m = self.memory
        lore: dict[str, str] = {}
        try:
            for lo in m.list_lore(m.CANON_SAVE, "faction"):
                lore[str(lo.get("key") or "").lower()] = str(lo.get("text") or "")
        except Exception:
            pass

        def add_fac(fid: Any, name: Any) -> None:
            fid = str(fid or "").strip().lower()
            if not fid or fid == "player" or ("faction:" + fid) in self._keys:
                return
            nm = str(name or "") or (getattr(m, "FACTION_NAMES", {}) or {}).get(fid) or fid.replace("_", " ").title()
            aliases = {fid, nm.lower()}
            for w in _WORD.findall(nm.lower()):
                if len(w) >= 4 and w not in _STOP:
                    aliases.add(w)
            self._add("faction:" + fid, nm, "faction", aliases, lore.get(fid) or nm)

        try:
            for f in m.list_factions(self.save_id):
                add_fac(f.get("faction_id"), f.get("name"))
        except Exception:
            pass
        for fid, nm in (getattr(m, "FACTION_NAMES", {}) or {}).items():
            add_fac(fid, nm)
        for fid in _CANON_FACTION_IDS:
            add_fac(fid, None)
        # Wares — the game's COMPLETE commodity catalog (harvested from libraries/wares.xml into canon lore
        # kind='ware'). A closed set, so an unresolved ware-typed entity is provably off-universe.
        self.has_wares = False
        try:
            for lo in m.list_lore(m.CANON_SAVE, "ware"):
                nm = str(lo.get("title") or "").strip()
                key = str(lo.get("key") or "").strip().lower()
                if nm and key:
                    aliases = {nm.lower()}
                    for w in _WORD.findall(nm.lower()):
                        if len(w) >= 4 and w not in _STOP:
                            aliases.add(w)
                    self._add("ware:" + key, nm, "ware", aliases, str(lo.get("text") or nm))
                    self.has_wares = True
        except Exception:
            pass
        # Sectors — places the NPC knows by name.
        try:
            for s in m.list_sectors(self.save_id):
                nm = str(s.get("name") or "").strip()
                if nm and nm.lower() != "unknown sector":
                    self._add("sector:" + nm.lower(), nm, "sector", {nm.lower()}, nm)
        except Exception:
            pass
        # Vector index over name+description for fuzzy/semantic resolution of an LLM-named entity.
        try:
            r = make_retriever()
            r.index([{"id": e["key"], "text": e["name"] + ". " + e["text"], "key": e["key"]} for e in self.entities])
            self._retriever = r
        except Exception:
            self._retriever = None

    def match_deterministic(self, message: str) -> list[dict]:
        """Word-bounded alias matches of known entities in the message (high precision, no LLM)."""
        msg = (message or "").lower()
        hits: dict[str, dict] = {}
        for alias, ent in self._alias.items():
            if len(alias) < 3:
                continue
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", msg):
                hits[ent["key"]] = ent
        return list(hits.values())

    def unknown_proper_nouns(self, message: str) -> list[str]:
        """Capitalized tokens in the message that are NOT a known X4 entity alias — the cheap gate for
        whether the LLM scope-classifier is worth calling (only an unknown proper noun can be out-of-scope)."""
        out: list[str] = []
        for w in re.findall(r"\b[A-Z][A-Za-z'\-]{2,}\b", str(message or "")):
            wl = w.lower()
            if wl not in self._alias and wl not in _STOP:
                out.append(w)
        return out

    def resolve(self, name: str) -> Optional[dict]:
        """Resolve an LLM-named entity to a canonical entity: exact alias, faction-id resolution, then vector."""
        n = (name or "").strip().lower()
        if not n:
            return None
        if n in self._alias:
            return self._alias[n]
        try:
            fid = self.memory.resolve_faction_id(name)
            if fid and ("faction:" + str(fid).lower()) in self._keys:
                return self._alias.get(str(fid).lower()) or self._alias.get(("faction:" + str(fid).lower()))
        except Exception:
            pass
        if self._retriever is not None:
            try:
                top = self._retriever.retrieve(name, k=1, min_score=0.35)
                if top:
                    key = top[0].get("key")
                    for e in self.entities:
                        if e["key"] == key:
                            return e
            except Exception:
                pass
        return None


def analyze_query(message: str, npc_faction: str, index: EntityIndex,
                  classify_llm: Optional[Callable[[list], str]] = None,
                  local_facts: Optional[list] = None) -> dict:
    """Boundary-aware query analysis (paper §3.4 step 1). Deterministic in-scope matches first, then one
    optional LLM call for HyDE + out-of-scope + specific/general. Returns {specific, general, out_of_scope}.

    FACT HIERARCHY (Codex 2026-06-26): `local_facts` are the NPC's OWN assignment facts — its ship, sector,
    posting — passed in by the caller from the persona/stats. These OUTRANK the refusal guard: the NPC knows
    its own ship even if that ship name is absent from the global X4 lore corpus. We match them first, mark them
    in-scope+local, tell the classifier they are known, and forbid the out-of-scope backstop from rejecting
    them. This fixes "tell me about the Vigilant" → "never heard of it" when the Vigilant IS the NPC's ship."""
    out: dict[str, list] = {"specific": [], "general": [], "out_of_scope": []}
    seen: set[str] = set()        # entity NAMEs already emitted (lowercased)
    seen_keys: set[str] = set()   # canonical entity KEYs already emitted (dedup across det + LLM aliases)

    # LOCAL ASSIGNMENT FACTS — highest authority. Word-bounded, case-insensitive match against the message.
    local_facts = local_facts or []
    local_names: set[str] = set()
    msg_l = (message or "").lower()
    for lf in local_facts:
        nm = str((lf or {}).get("name") or "").strip()
        if not nm or nm.lower() in seen:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(nm.lower()) + r"(?![a-z0-9])", msg_l):
            out["specific"].append({"name": nm, "type": str(lf.get("kind") or "local"),
                                    "key": "local:" + nm.lower(), "specificity": "specific",
                                    "in_scope": True, "local": True, "note": str(lf.get("note") or "")})
            seen.add(nm.lower())
            local_names.add(nm.lower())

    for e in index.match_deterministic(message):
        if e["name"].lower() in seen:
            continue
        spec = "specific" if e["type"] in ("faction", "sector") else "general"
        item = {"name": e["name"], "type": e["type"], "key": e["key"], "specificity": spec, "in_scope": True}
        out["specific" if spec == "specific" else "general"].append(item)
        seen.add(e["name"].lower())
        seen_keys.add(e["key"])

    if classify_llm:
        try:
            # GROUND TRUTH, not model memory: factions are a CLOSED, fully-enumerable set, so hand the model
            # the authoritative roster to check against. A small local model can't recall the X4 faction list
            # reliably and will wave through a galaxy-plausible fake ("the Vortyx Collective") — giving it the
            # list turns "do you remember this faction?" into "is this name in this list?".
            roster = sorted({e["name"] for e in index.entities if e["type"] == "faction"})
            cls_msgs = [
                {"role": "system", "content": _CLASSIFY_SYS},
                {"role": "system", "content": "The NPC represents the X4 faction: " + str(npc_faction or "unknown")},
                {"role": "system", "content": (
                    "AUTHORITATIVE — the ONLY factions / major powers / organisations that exist in this X4 "
                    "universe are: " + "; ".join(roster) + ". Any faction-like, organisation, power, "
                    "government, or military name NOT in this list does NOT exist — mark it in_scope=false.")},
            ]
            if local_facts:
                known = "; ".join(f"{lf.get('name')} ({lf.get('kind') or 'local'})" for lf in local_facts if lf.get("name"))
                cls_msgs.append({"role": "system", "content": (
                    "LOCAL FACTS — the NPC's OWN posting, which it knows personally even if absent from the "
                    "galaxy lore above: " + known + ". These are IN-SCOPE. NEVER mark them in_scope=false; the "
                    "NPC's own ship/sector/role outrank the lore corpus.")})
            cls_msgs.append({"role": "user", "content": "Player message: " + str(message or "")[:600]})
            raw = classify_llm(cls_msgs)
            for it in _parse_json_array(raw):
                nm = str(it.get("name") or "").strip()
                if not nm or nm.lower() in seen:
                    continue
                seen.add(nm.lower())
                etype = str(it.get("type") or "other").lower()
                rationale = str(it.get("rationale") or "").strip()
                if not bool(it.get("in_scope", True)):
                    out["out_of_scope"].append({"name": nm, "type": etype, "rationale": rationale})
                    continue
                ent = index.resolve(nm)
                # AIRTIGHT closed-set backstop: factions are fully enumerable, so a faction-like entity that
                # resolves to NOTHING in our complete roster is fabricated — override the model and reject it,
                # regardless of what it claimed. (Wares/ships are NOT yet enumerated -> best-effort, see roadmap.)
                if ent is None and etype in _FACTION_LIKE_TYPES:
                    out["out_of_scope"].append({"name": nm, "type": etype,
                                                "rationale": rationale or "no such faction or power exists in the X4 universe"})
                    continue
                # Same closed-set backstop for WARES — but only once the ware catalog has been harvested
                # (else we'd reject real wares we simply haven't loaded). Closes the off-universe-ore leak.
                if ent is None and etype in _WARE_LIKE_TYPES and getattr(index, "has_wares", False):
                    out["out_of_scope"].append({"name": nm, "type": etype,
                                                "rationale": rationale or "no such commodity exists in the X4 economy"})
                    continue
                if ent is not None and ent["key"] in seen_keys:
                    continue  # an alias of an entity the deterministic pass already emitted
                if ent is not None:
                    seen_keys.add(ent["key"])
                    seen.add(ent["name"].lower())
                spec = str(it.get("specificity") or "general").lower()
                is_specific = spec == "specific" and ent is not None
                out["specific" if is_specific else "general"].append({
                    "name": ent["name"] if ent else nm, "type": etype,
                    "key": ent["key"] if ent else None,
                    "specificity": "specific" if is_specific else "general", "in_scope": True})
        except Exception:
            pass
    return out


def retrieve(memory: Any, save_id: str, npc_faction: str, analysis: dict, message: str, k: int = 5) -> dict:
    """Three-route retrieval (paper §3.4 step 2). Returns {context:[lines], boundary:[rejection lines]}."""
    context: list[str] = []
    anchored: set[str] = set()

    # SPECIFIC in-scope -> the entity's own subgraph.
    for it in analysis.get("specific", []):
        key = str(it.get("key") or "")
        if key.startswith("local:") or it.get("local"):
            # LOCAL ASSIGNMENT FACT — the NPC's own ship/sector/posting. Surface it as POSITIVE first-person
            # knowledge so the model answers from it instead of refusing. The note carries the grounded gist.
            nm = it.get("name")
            note = str(it.get("note") or "").strip()
            context.append(
                (f"{nm} is part of your own posting — you know it personally. {note}".strip())
                + " You know it at your own level (your duties, your deck, your squad), not the officer-level"
                  " operational picture; say so plainly rather than claiming you've never heard of it.")
            continue
        if key.startswith("faction:"):
            fid = key.split(":", 1)[1]
            if fid in anchored:
                continue
            anchored.add(fid)
            try:
                for d in memory.graph_retrieve(save_id, fid, message, k=4):
                    context.append(str(d.get("text") or ""))
            except Exception:
                pass
        elif key.startswith("sector:"):
            try:
                nm = it["name"]
                for s in memory.list_sectors(save_id):
                    if str(s.get("name") or "").lower() == nm.lower():
                        owner = s.get("owner_faction") or "an unknown power"
                        cont = ", ".join(s.get("contested_by") or [])
                        context.append(f"{nm} is held by {owner}." + (f" It is contested by {cont}." if cont else ""))
                        break
            except Exception:
                pass

    # GENERAL -> the NPC's own faction 1-hop (existing behavior), unless already anchored.
    if npc_faction and str(npc_faction).strip().lower() not in anchored:
        try:
            for d in memory.graph_retrieve(save_id, npc_faction, message, k=3):
                context.append(str(d.get("text") or ""))
        except Exception:
            pass

    # OUT-OF-SCOPE -> explicit cognitive-boundary rejection (the paper's anti-hallucination mechanism).
    boundary: list[str] = []
    for it in analysis.get("out_of_scope", []):
        nm = it.get("name")
        why = it.get("rationale") or "it does not exist in the X4 universe"
        boundary.append(
            f'You have NO knowledge of "{nm}" — {why}. It does not exist in your world; do not pretend to '
            f"know it or invent details. If pressed, say plainly that it means nothing to you.")

    seen: set[str] = set()
    ctx: list[str] = []
    for c in context:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            ctx.append(c)
    return {"context": ctx[: k + 3], "boundary": boundary}


class RoleRAG:
    """Caches one EntityIndex per save and exposes the combined analyze+retrieve. `classify_llm(messages)->str`
    is injected by the caller (player2_client) so this module stays decoupled from the Player2 transport."""

    def __init__(self, memory: Any):
        self.memory = memory
        self._index: dict[str, EntityIndex] = {}

    def index_for(self, save_id: str) -> EntityIndex:
        idx = self._index.get(save_id)
        if idx is None:
            idx = EntityIndex(self.memory, save_id)
            self._index[save_id] = idx
        return idx

    def invalidate(self, save_id: Optional[str] = None) -> None:
        if save_id is None:
            self._index.clear()
        else:
            self._index.pop(save_id, None)

    def analyze_and_retrieve(self, save_id: str, npc_faction: str, message: str,
                             classify_llm: Optional[Callable[[list], str]] = None,
                             local_facts: Optional[list] = None) -> dict:
        idx = self.index_for(save_id)
        analysis = analyze_query(message, npc_faction, idx, classify_llm=classify_llm, local_facts=local_facts)
        result = retrieve(self.memory, save_id, npc_faction, analysis, message)
        result["analysis"] = analysis
        return result


# --- Self-test (deterministic; no LLM, no network) ------------------------------------------------
class _FakeMemory:
    """Minimal memory shim for the selftest — just the accessors EntityIndex/retrieve touch."""
    CANON_SAVE = "__canon__"
    FACTION_NAMES = {"teladi": "Teladi Company", "argon": "Argon Federation", "split": "Zyarth Patriarchy"}

    def list_lore(self, save_id, kind):
        return [{"key": "teladi", "text": "A profit-driven reptilian trading faction."}]

    def list_factions(self, save_id):
        return [{"faction_id": "teladi", "name": "Teladi Company"},
                {"faction_id": "argon", "name": "Argon Federation"}]

    def list_sectors(self, save_id):
        return [{"name": "Grand Exchange", "owner_faction": "teladi", "contested_by": ["xenon"]}]

    def resolve_faction_id(self, name):
        n = str(name or "").lower()
        for fid in ("teladi", "argon", "split"):
            if fid in n:
                return fid
        return None

    def graph_retrieve(self, save_id, anchor, query, k=6):
        return [{"text": f"{anchor} regards argon as hostile."}, {"text": f"{anchor} is a major trading power."}]


def run_rolerag_selftest() -> dict:
    checks: list[dict] = []
    ok = lambda name, passed, detail=None: checks.append({"name": name, "pass": bool(passed), "detail": detail})

    idx = EntityIndex(_FakeMemory(), "save1")
    ok("index_has_factions", any(e["key"] == "faction:teladi" for e in idx.entities), len(idx.entities))
    ok("index_has_sector", any(e["key"].startswith("sector:") for e in idx.entities))

    det = idx.match_deterministic("are we at war with the teladi over Grand Exchange?")
    keys = {e["key"] for e in det}
    ok("matches_faction", "faction:teladi" in keys, sorted(keys))
    ok("matches_sector", "sector:grand exchange" in keys, sorted(keys))

    # No-LLM analysis: deterministic in-scope only, no false out-of-scope.
    a = analyze_query("tell me about the teladi", "argon", idx, classify_llm=None)
    ok("specific_has_teladi", any(i["name"] == "Teladi Company" for i in a["specific"]), a["specific"])
    ok("no_false_rejection", a["out_of_scope"] == [], a["out_of_scope"])

    # Stub LLM returns one in-scope + one out-of-scope entity → routing + rejection.
    def stub_llm(messages):
        return ('[{"name":"Argon Federation","type":"faction","in_scope":true,"specificity":"specific","rationale":"X4 faction"},'
                '{"name":"United Nations","type":"person","in_scope":false,"specificity":"specific","rationale":"real-world, not in X4"}]')

    a2 = analyze_query("what would the Argon and the United Nations think?", "teladi", idx, classify_llm=stub_llm)
    ok("llm_specific_resolved", any(i["name"] == "Argon Federation" for i in a2["specific"]), a2["specific"])
    ok("llm_out_of_scope_caught", any(i["name"] == "United Nations" for i in a2["out_of_scope"]), a2["out_of_scope"])

    res = retrieve(_FakeMemory(), "save1", "teladi", a2, "what would the Argon and the United Nations think?")
    ok("retrieve_has_context", len(res["context"]) > 0, res["context"][:2])
    ok("retrieve_has_boundary", any("United Nations" in b for b in res["boundary"]), res["boundary"])
    ok("boundary_instructs_refusal", any("do not pretend" in b for b in res["boundary"]))

    passed = sum(1 for c in checks if c["pass"])
    total = len(checks)
    return {"allPassed": passed == total, "pass": passed == total, "passed": passed, "total": total, "checks": checks}


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(run_rolerag_selftest(), indent=1))
