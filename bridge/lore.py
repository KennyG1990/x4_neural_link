"""X4 canon lore harvester (Neural Link, Layer-3 execution).

Turns the GAME'S OWN encyclopedia into structured, retrievable knowledge:

  inputs  (extracted by catdat.py, or passed directly as XML strings)
    - libraries/factions.xml   faction identities + canonical relations
    - t/0001-l044.xml          English text DB ({page,id} -> string)

  outputs
    1. faction nodes      id, name, race, tags, description    -> universe graph
    2. relation edges     canonical faction<->faction standing -> universe graph
    3. lore chunks        retrievable identity/description text -> RAG corpus

Deterministic, idempotent, no network, no LLM. The text DB references resolve
`{page,id}` tokens; descriptions may themselves embed `{page,id}` refs, which we
resolve one level deep. X4 string markup (leading translator comments in
parentheses, `\\(` escapes, `\\n`) is lightly cleaned for readability.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from typing import Optional

_REF = re.compile(r"\{(\d+),(\d+)\}")
_LEADING_COMMENT = re.compile(r"^\([^)]*\)")


# ---------------------------------------------------------------------------
# Text DB
# ---------------------------------------------------------------------------

def parse_text_db(xml_text: str) -> dict[tuple[int, int], str]:
    """{ (page_id, t_id): raw_text } from a language file. iterparse keeps memory
    bounded on the ~6 MB vanilla file."""
    out: dict[tuple[int, int], str] = {}
    page_id: Optional[int] = None
    for event, el in ET.iterparse(io.StringIO(xml_text), events=("start", "end")):
        if event == "start" and el.tag == "page":
            try:
                page_id = int(el.get("id"))
            except (TypeError, ValueError):
                page_id = None
        elif event == "end" and el.tag == "t" and page_id is not None:
            try:
                t_id = int(el.get("id"))
            except (TypeError, ValueError):
                t_id = None
            if t_id is not None:
                out[(page_id, t_id)] = el.text or ""
            el.clear()
        elif event == "end" and el.tag == "page":
            el.clear()
            page_id = None
    return out


def _clean(text: str) -> str:
    """Strip X4 string markup so the prose reads naturally."""
    if not text:
        return ""
    text = _LEADING_COMMENT.sub("", text)      # leading (translator comment)
    text = text.replace("\\(", "(").replace("\\)", ")")
    text = text.replace("\\n", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_ref(value: Optional[str], db: dict[tuple[int, int], str], _depth: int = 0) -> str:
    """Resolve a `{page,id}` token (or text containing one) against the DB,
    one level of nested refs deep. Returns cleaned prose."""
    if not value:
        return ""

    def _sub(m: re.Match) -> str:
        key = (int(m.group(1)), int(m.group(2)))
        raw = db.get(key, "")
        if raw and _depth < 1 and _REF.search(raw):
            return resolve_ref(raw, db, _depth + 1)
        return raw

    return _clean(_REF.sub(_sub, value))


# ---------------------------------------------------------------------------
# Factions
# ---------------------------------------------------------------------------

def _standing_label(relation: float) -> str:
    """Map an X4 relation float [-1,1] to a human standing label."""
    if relation <= -0.75:
        return "at war"
    if relation <= -0.2:
        return "hostile"
    if relation < 0.2:
        return "neutral"
    if relation < 0.75:
        return "friendly"
    return "allied"


def parse_factions(xml_text: str) -> list[dict]:
    """List of faction dicts with raw refs + parsed relations."""
    root = ET.fromstring(xml_text)
    factions: list[dict] = []
    for fac in root.findall(".//faction"):
        rels = []
        rel_parent = fac.find("relations")
        if rel_parent is not None:
            for r in rel_parent.findall("relation"):
                target = r.get("faction")
                try:
                    val = float(r.get("relation"))
                except (TypeError, ValueError):
                    continue
                if target:
                    rels.append({"faction": target, "relation": val})
        factions.append({
            "id": fac.get("id"),
            "name_ref": fac.get("name"),
            "description_ref": fac.get("description"),
            "shortname_ref": fac.get("shortname"),
            "primaryrace": fac.get("primaryrace") or "",
            "tags": (fac.get("tags") or "").split(),
            "relations": rels,
        })
    return factions


def harvest(factions_xml: str, text_xml: Optional[str] = None) -> dict:
    """Parse both sources into faction nodes, relation edges, and lore chunks.

    `text_xml` is optional: without it, names/descriptions stay as raw refs and
    lore chunks fall back to id + race + tags (the graph seed still works)."""
    db = parse_text_db(text_xml) if text_xml else {}
    factions = parse_factions(factions_xml)

    nodes: list[dict] = []
    edges: list[dict] = []
    chunks: list[dict] = []

    for f in factions:
        fid = f["id"]
        if not fid:
            continue
        name = resolve_ref(f["name_ref"], db) or fid
        description = resolve_ref(f["description_ref"], db)
        race = f["primaryrace"]
        tags = f["tags"]

        nodes.append({"id": fid, "name": name, "race": race,
                      "tags": tags, "description": description})

        # Retrievable lore chunk: identity + prose, always something useful.
        body = name
        if race:
            body += f" — a {race} faction"
        if tags:
            body += f" ({', '.join(tags)})"
        if description:
            body += f". {description}"
        chunks.append({"kind": "faction", "key": fid, "title": name, "text": body})

        for r in f["relations"]:
            edges.append({
                "subject": fid,
                "object": r["faction"],
                "relation": r["relation"],
                "standing": _standing_label(r["relation"]),
            })

    return {"factions": nodes, "relations": edges, "lore_chunks": chunks,
            "text_resolved": bool(db)}


# ---------------------------------------------------------------------------
# Apply to the universe graph + RAG corpus
# ---------------------------------------------------------------------------

def parse_wares(wares_xml: str, text_xml: Optional[str] = None) -> dict:
    """Parse libraries/wares.xml — the game's COMPLETE ware catalog (the closed set the in-game encyclopedia
    is built from) — into canonical ware lore chunks (kind='ware'). Lets RoleRAG treat wares as a closed set
    and reject off-universe commodities. Returns the lore.apply() shape (empty factions/relations)."""
    db = parse_text_db(text_xml) if text_xml else {}
    chunks: list[dict] = []
    seen: set = set()
    try:
        root = ET.fromstring(wares_xml)
    except Exception:
        return {"factions": [], "relations": [], "lore_chunks": [], "text_resolved": bool(db)}
    for w in root.findall(".//ware"):
        wid = w.get("id")
        if not wid or wid in seen:
            continue
        name = resolve_ref(w.get("name"), db) if w.get("name") else ""
        if not name:
            continue  # unnamed/internal ware — not something a character would ever name
        seen.add(wid)
        group = (w.get("group") or "").strip()
        body = name + (f" — a {group} ware in the X4 economy" if group else " — a tradeable commodity in the X4 economy")
        chunks.append({"kind": "ware", "key": wid, "title": name, "text": body})
    return {"factions": [], "relations": [], "lore_chunks": chunks, "text_resolved": bool(db)}


def apply(store, save_id: str, result: dict) -> dict:
    """Seed faction nodes, canonical relation edges, and lore chunks into the
    universe-state store. Idempotent: relation seeds set absolute canonical
    values rather than incrementing."""
    nfac = nrel = nlore = 0
    for node in result["factions"]:
        summary = node["description"] or node["name"]
        store.upsert_faction(save_id, node["id"], name=node["name"], summary=summary[:400])
        nfac += 1
    for e in result["relations"]:
        store.seed_canonical_relationship(
            save_id, e["subject"], e["object"], e["relation"], e["standing"])
        nrel += 1
    for c in result["lore_chunks"]:
        store.upsert_lore(save_id, c["kind"], c["key"], c["title"], c["text"])
        nlore += 1
    return {"factions": nfac, "relations": nrel, "lore_chunks": nlore}


# ---------------------------------------------------------------------------
# Selftest — deterministic, self-contained
# ---------------------------------------------------------------------------

_FIXTURE_FACTIONS = """<?xml version="1.0" encoding="utf-8"?>
<factions>
  <faction id="argon" name="{20203,101}" description="{20203,102}" shortname="{20203,103}" primaryrace="argon" tags="economic police standard">
    <relations>
      <relation faction="antigone" relation="0.67" />
      <relation faction="xenon" relation="-1" />
      <relation faction="teladi" relation="0.1" />
    </relations>
  </faction>
  <faction id="xenon" name="{20203,201}" description="{20203,202}" primaryrace="xenon" tags="aggressive">
    <relations>
      <relation faction="argon" relation="-1" />
    </relations>
  </faction>
</factions>"""

_FIXTURE_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<language id="44">
  <page id="20203" title="Factions">
    <t id="101">Argon Federation</t>
    <t id="102">(desc)A democratic union of {20203,103} descended from Terran colonists.</t>
    <t id="103">Argon</t>
    <t id="201">Xenon</t>
    <t id="202">Hostile self-replicating machine intelligences.</t>
  </page>
</language>"""


class _MemStub:
    """Minimal in-memory stand-in so the selftest needs no DB/host state."""
    def __init__(self):
        self.factions = {}
        self.relations = {}
        self.lore = {}

    def upsert_faction(self, save_id, faction_id, name=None, summary=None, **_):
        self.factions[faction_id] = {"name": name, "summary": summary}

    def seed_canonical_relationship(self, save_id, subject, obj, relation, standing):
        self.relations[(subject, obj)] = {"relation": relation, "standing": standing}

    def upsert_lore(self, save_id, kind, key, title, text):
        self.lore[(kind, key)] = {"title": title, "text": text}


def run_lore_selftest() -> dict:
    checks: list[dict] = []

    def check(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond), "detail": str(detail)})

    db = parse_text_db(_FIXTURE_TEXT)
    check("textdb_parsed", db.get((20203, 101)) == "Argon Federation", db.get((20203, 101)))
    check("ref_resolves", resolve_ref("{20203,101}", db) == "Argon Federation")
    check("nested_ref_resolves",
          "Argon" in resolve_ref("{20203,102}", db) and "{" not in resolve_ref("{20203,102}", db),
          resolve_ref("{20203,102}", db))
    check("comment_stripped", not resolve_ref("{20203,102}", db).startswith("("))

    facs = parse_factions(_FIXTURE_FACTIONS)
    check("factions_parsed", len(facs) == 2, len(facs))
    argon = next((f for f in facs if f["id"] == "argon"), None)
    check("relations_parsed", argon and len(argon["relations"]) == 3,
          argon and len(argon["relations"]))

    check("standing_war", _standing_label(-1.0) == "at war")
    check("standing_friendly", _standing_label(0.67) == "friendly")
    check("standing_neutral", _standing_label(0.1) == "neutral")

    res = harvest(_FIXTURE_FACTIONS, _FIXTURE_TEXT)
    check("text_resolved_flag", res["text_resolved"] is True)
    check("node_named", any(n["name"] == "Argon Federation" for n in res["factions"]))
    check("edge_count", len(res["relations"]) == 4, len(res["relations"]))
    chunk = next((c for c in res["lore_chunks"] if c["key"] == "argon"), None)
    check("chunk_has_prose", chunk and "democratic union" in chunk["text"], chunk and chunk["text"])

    # Degraded mode: no text DB -> graph seed still works, chunks fall back.
    res_nodb = harvest(_FIXTURE_FACTIONS, None)
    check("degrades_without_textdb",
          res_nodb["text_resolved"] is False and len(res_nodb["relations"]) == 4)

    # apply() against the stub: idempotent seed.
    stub = _MemStub()
    a1 = apply(stub, "save1", res)
    a2 = apply(stub, "save1", res)  # re-run must not duplicate or drift
    check("apply_seeds_relations", a1["relations"] == 4)
    check("apply_idempotent",
          stub.relations[("argon", "xenon")]["standing"] == "at war" and a2["relations"] == 4)

    passed = sum(1 for c in checks if c["pass"])
    return {"suite": "lore", "passed": passed, "total": len(checks),
            "allPassed": passed == len(checks), "checks": checks}


if __name__ == "__main__":
    import json
    print(json.dumps(run_lore_selftest(), indent=2))
