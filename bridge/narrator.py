"""SPEC 2b — Narrator layer: the WORLD's voice.

Three voices, kept separate (Codex): NPCs create OPINIONS · Factions create DECISIONS · **Narrator creates
HISTORY**. The Narrator converts real simulation deltas (recorded `world_events`) into legible political
history — a titled article with participants, body, an optional consequence and quote. It does NOT roleplay as
an NPC and does NOT make decisions; it interprets + dramatizes what ALREADY happened.

HARD RULE: **no real cause in the DB → no article.** Every article is grounded in actual `world_events`
(relation shift / battle loss / sector contested / shortage / agreement made-or-broken / faction action / war-
peace threshold). Related events are CLUSTERED into one article. LLM-authored with a deterministic fallback;
reuses the SPEC 1l name-hygiene discipline (no raw ids in prose).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

# Event types worth narrating as HISTORY (a thing that happened in the galaxy). Internal mood "reaction" rows
# and trivial no-ops are NOT history.
_WORTHY_TYPES = {"battle", "war", "conflict", "diplomatic", "sector_change", "economic_threshold",
                 "agreement", "ceasefire", "loss", "relation_shift", "betrayal", "death"}
_SKIP_SOURCES = {"reaction"}          # the L3 emotional-reaction rows are opinions, not history
_MIN_IMPORTANCE = 3                   # only meaningful events become articles
# Thematic topic (Codex output has category:"Political" etc.) — distinct from the 'news' logbook tab.
_TOPIC_MAP = {"battle": "Military", "war": "Military", "conflict": "Military", "loss": "Military",
              "diplomatic": "Political", "agreement": "Political", "ceasefire": "Political",
              "relation_shift": "Political", "betrayal": "Political", "death": "Political",
              "economic_threshold": "Economic", "sector_change": "Territorial"}


def _parse_json_obj(raw: Any) -> dict:
    s = str(raw or "").strip()
    if "```" in s:
        s = re.sub(r"```(?:json)?", "", s).strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class Narrator:
    """Turns recent world_events into grounded history articles. Stateful only in a per-save 'last narrated'
    cursor (in-memory) so the same event isn't narrated twice."""

    def __init__(self, memory: Any):
        self.memory = memory
        self._cursor: dict[str, int] = {}      # save_id -> max world_event id already narrated
        self._recent_titles: dict[str, list] = {}  # save_id -> recent titles (dedup)

    # -- helpers --------------------------------------------------------------
    def _fac_name(self, save_id: str, fid: str) -> str:
        if not fid:
            return ""
        try:
            for f in self.memory.list_factions(save_id):
                if f.get("faction_id") == fid and f.get("name") and f.get("name") != fid:
                    return f["name"]
        except Exception:
            pass
        return (getattr(self.memory, "FACTION_NAMES", {}) or {}).get(fid, fid.replace("_", " ").title())

    def _normalize(self, save_id: str, text: str) -> str:
        """Replace raw faction ids embedded in a summary with display names (hygiene)."""
        if not text:
            return text
        try:
            ids = set(getattr(self.memory, "FACTION_NAMES", {}).keys())
            for f in self.memory.list_factions(save_id):
                if f.get("faction_id"):
                    ids.add(f["faction_id"])
        except Exception:
            ids = set(getattr(self.memory, "FACTION_NAMES", {}).keys())
        # CASE-SENSITIVE on the lowercase id only: raw event summaries use lowercase ids ("argon"), while
        # resolved display names are Title-Case ("Argon Federation") — so this is idempotent (won't re-expand
        # the "Argon" already inside "Argon Federation"), unlike a case-insensitive pass.
        for fid in sorted((i for i in ids if i and len(i) >= 3 and i.islower()), key=len, reverse=True):
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(fid) + r"(?![A-Za-z0-9])",
                          lambda m, fid=fid: self._fac_name(save_id, fid), text)
        return text

    def _sector_name(self, save_id: str, sector_id: str) -> str:
        if not sector_id:
            return ""
        try:
            for s in self.memory.list_sectors(save_id):
                if str(s.get("sector_id") or "") == str(sector_id) or str(s.get("name") or "") == str(sector_id):
                    return str(s.get("name") or sector_id)
        except Exception:
            pass
        return str(sector_id)

    def _evidence(self, save_id: str, primary: str, secondary: str, sector: str) -> list[str]:
        """Concrete EVIDENCE from the substrate (Codex 2nd-review): the real numbers/causes behind the story —
        relation standing+value, the conflict's cause + intensity, recent fleet losses, the contested location.
        This is what turns 'X pressures Y' into 'X stands at war with Y (-1.00) after heavy losses near Z'."""
        ev: list[str] = []
        A = self._fac_name(save_id, primary)
        B = self._fac_name(save_id, secondary)
        if primary and secondary:
            try:
                rel = self.memory.get_relationship(save_id, primary, secondary)
                if rel:
                    standing = str(rel.get("standing") or "").strip()
                    tr = rel.get("trust")
                    val = f" ({float(tr) / 100:+.2f})" if isinstance(tr, (int, float)) else ""
                    if standing:
                        ev.append(f"{A} now stands {standing} toward {B}{val}")
            except Exception:
                pass
        try:
            for c in self.memory.list_conflicts(save_id, status="active"):
                pair = {str(c.get("faction_a")), str(c.get("faction_b"))}
                if primary in pair and secondary in pair:
                    cause = str(c.get("cause") or "").strip()
                    inten = c.get("intensity")
                    if cause:
                        ev.append(f"the conflict began over {self._normalize(save_id, cause)}")
                    if isinstance(inten, (int, float)) and inten > 0:
                        ev.append(f"the war runs at {round(float(inten) * 100)}% intensity")
                    break
        except Exception:
            pass
        try:
            pr = self.memory.derive_pressures(save_id, primary) or {}
            rl = float(pr.get("recent_losses", 0) or 0)
            if rl >= 0.5:
                ev.append(f"{A} has taken heavy fleet losses lately")
            elif rl >= 0.3:
                ev.append(f"{A} has taken notable losses lately")
        except Exception:
            pass
        if sector:
            ev.append(f"the fighting centres on {sector}")
        return ev

    # -- clustering -----------------------------------------------------------
    @staticmethod
    def _pair_key(e: dict) -> tuple:
        a = str(e.get("primary_faction") or "")
        b = str(e.get("secondary_faction") or "")
        return tuple(sorted([a, b]))

    def _cluster(self, events: list[dict]) -> list[dict]:
        """Group worthy events by faction-pair into clusters, ranked by summed importance (most significant
        story first). Each cluster carries its member events + the dominant participants/sector."""
        groups: dict[tuple, dict] = {}
        for e in events:
            if str(e.get("source") or "") in _SKIP_SOURCES:
                continue
            if str(e.get("event_type") or "") not in _WORTHY_TYPES:
                continue
            if int(e.get("importance") or 0) < _MIN_IMPORTANCE:
                continue
            if not e.get("primary_faction"):
                continue
            k = self._pair_key(e)
            g = groups.setdefault(k, {"events": [], "weight": 0, "primary": e.get("primary_faction"),
                                      "secondary": e.get("secondary_faction"), "sector_id": e.get("sector_id"),
                                      "max_id": 0})
            g["events"].append(e)
            g["weight"] += int(e.get("importance") or 1)
            g["max_id"] = max(g["max_id"], int(e.get("id") or 0))
            if e.get("sector_id") and not g["sector_id"]:
                g["sector_id"] = e.get("sector_id")
        clusters = list(groups.values())
        clusters.sort(key=lambda g: g["weight"], reverse=True)
        return clusters

    # -- narrate one cluster --------------------------------------------------
    def narrate(self, save_id: str, cluster: dict, chat_fn: Optional[Callable[[list], str]] = None) -> Optional[dict]:
        """Build ONE history article from a cluster. CAUSE-GATED: returns None if the cluster has no real,
        participant-bearing events. LLM-authored when chat_fn given, else a deterministic fallback."""
        events = cluster.get("events") or []
        if not events:
            return None
        a = self._fac_name(save_id, cluster.get("primary") or "")
        b = self._fac_name(save_id, cluster.get("secondary") or "")
        sector = self._sector_name(save_id, cluster.get("sector_id") or "")
        # EVIDENCE (the concrete cause from the substrate) leads, then the event summaries — so the article is
        # SPECIFIC, not generic ("X pressures Y" -> "X stands at war with Y (-1.00) after heavy losses near Z").
        evidence = self._evidence(save_id, cluster.get("primary") or "", cluster.get("secondary") or "", sector)
        facts = list(evidence)
        for e in sorted(events, key=lambda x: int(x.get("importance") or 0), reverse=True)[:4]:
            s = self._normalize(save_id, str(e.get("summary") or "")).strip()
            if s and s not in facts:
                facts.append(s)
        if not facts and not (a or b):
            return None  # nothing real to report
        participants = [p for p in (a, b) if p]
        category = "news"
        topic = "Galactic"
        for e in sorted(events, key=lambda x: int(x.get("importance") or 0), reverse=True):
            _t = _TOPIC_MAP.get(str(e.get("event_type") or ""))
            if _t:
                topic = _t
                break

        # LLM article — cite the evidence, end with a quote.
        article = {}
        if chat_fn:
            try:
                fp = "\n".join("- " + f for f in facts) or "- (a development between these powers)"
                sys = ("You are the galaxy news desk / historian of the X4: Foundations universe. From the FACTS "
                       "below — the ONLY real causes, drawn from the live simulation — write a short, neutral "
                       "HISTORY bulletin about what HAPPENED (not an opinion, not a character speaking). CITE the "
                       "concrete evidence (standings, losses, the contested location). Do NOT invent ship counts, "
                       "casualty numbers, names, dates, or anything not in the facts. End with ONE short quote "
                       "attributed to an unnamed faction official that fits the facts. Respond with ONLY a compact "
                       'JSON object: {"title":"<=8 word headline","body":"1-2 sentences citing the evidence",'
                       '"consequence":"one clause on what it means","quote":"one short attributed line"}.')
                usr = (f"PARTICIPANTS: {', '.join(participants) or 'a faction'}\n"
                       f"LOCATION: {sector or 'the wider galaxy'}\nFACTS:\n{fp}")
                article = _parse_json_obj(chat_fn([{"role": "system", "content": sys},
                                                   {"role": "user", "content": usr}]))
            except Exception:
                article = {}

        title = str(article.get("title") or "").strip()
        body = str(article.get("body") or "").strip()
        if not title or not body:
            # deterministic fallback — grounded, evidence-led, neutral.
            who = a or (participants[0] if participants else "A faction")
            where = f" in {sector}" if sector and sector not in (evidence[0] if evidence else "") else ""
            lead = (evidence[0] if evidence else (facts[0] if facts else f"tensions shifted around {who}"))
            lead = lead[:1].upper() + lead[1:] if lead else lead
            extra = ""
            if len(facts) > 1:
                ex = facts[1].strip()
                extra = " " + ex[:1].upper() + ex[1:] + ("." if not ex.endswith(".") else "")
            title = title or (f"{who} and {b}: A Reckoning" if b else f"{who}: A Reckoning")
            body = body or (f"{lead.rstrip('.')}{where}.{extra}")
        consequence = str(article.get("consequence") or "").strip()
        quote = str(article.get("quote") or "").strip()
        if not quote:  # fallback quote, attributed + seeded for variety (Codex output always carries one)
            who = a or (participants[0] if participants else "a faction")
            art = "An" if who[:1].lower() in "aeiou" else "A"   # a/an grammar (Codex flagged "A Argon…")
            qs = [f'{art} {who} official called it "a matter Command can no longer ignore."',
                  (f'"{b} will answer for this," {art.lower()} {who} spokesperson said.' if b
                   else f'"We hold the line," {art.lower()} {who} spokesperson said.'),
                  f'{art} {who} envoy warned of "consequences across the frontier."']
            quote = qs[int(cluster.get("max_id") or 0) % len(qs)]
        body = re.sub(r"\s+", " ", self._normalize(save_id, body)).strip()
        title = re.sub(r"\s+", " ", self._normalize(save_id, title)).strip().strip('"')
        quote = re.sub(r"\s+", " ", self._normalize(save_id, quote)).strip()
        return {"title": title, "category": category, "topic": topic, "participants": participants,
                "body": body, "consequence": consequence, "quote": quote, "weight": cluster.get("weight", 0)}

    # -- one pass over the heartbeat -----------------------------------------
    def run_pass(self, save_id: str, chat_fn: Optional[Callable[[list], str]] = None, budget: int = 1) -> list[dict]:
        """Narrate up to `budget` NEW high-importance event clusters since the last pass. Advances the per-save
        cursor so events aren't re-narrated. Returns article dicts ({title, body, category, ...})."""
        try:
            events = self.memory.list_world_events(save_id, limit=120, min_importance=_MIN_IMPORTANCE)
        except Exception:
            return []
        # DURABLE cursor (Codex 2b review): load the persisted last-narrated id so a bridge RESTART does not
        # re-narrate old events. In-memory cache mirrors it.
        since = self._cursor.get(save_id)
        if since is None:
            since = self._load_cursor(save_id)
            self._cursor[save_id] = since
        fresh = [e for e in events if int(e.get("id") or 0) > since]
        if not fresh:
            return []
        clusters = self._cluster(fresh)
        out: list[dict] = []
        recent = self._recent_titles.setdefault(save_id, [])
        max_id = since
        for c in clusters:
            max_id = max(max_id, int(c.get("max_id") or 0))
            if len(out) >= max(1, budget):
                continue
            art = self.narrate(save_id, c, chat_fn=chat_fn)
            if art and art["title"].lower() not in [t.lower() for t in recent[-20:]]:
                out.append(art)
                recent.append(art["title"])
        self._cursor[save_id] = max(max_id, since)
        self._save_cursor(save_id, self._cursor[save_id])
        return out

    def _load_cursor(self, save_id: str) -> int:
        try:
            for m in self.memory.list_lore(save_id, "_meta"):
                if str(m.get("key")) == "narrator_cursor":
                    return int(float(m.get("text") or 0))
        except Exception:
            pass
        return 0

    def _save_cursor(self, save_id: str, val: int) -> None:
        try:
            self.memory.upsert_lore(save_id, "_meta", "narrator_cursor", "narrator_cursor", str(int(val)))
        except Exception:
            pass


# --- Self-test (deterministic; no network) --------------------------------------------------------
class _FakeMem:
    FACTION_NAMES = {"argon": "Argon Federation", "khaak": "Kha'ak"}

    def list_factions(self, s):
        return [{"faction_id": "argon", "name": "Argon Federation"}]

    def list_sectors(self, s):
        return [{"sector_id": "hat1", "name": "Hatikvah's Choice"}]

    def get_relationship(self, s, a, b):
        return {"standing": "at war", "trust": -100} if {a, b} == {"argon", "khaak"} else None

    def list_conflicts(self, s, status=None):
        return [{"faction_a": "argon", "faction_b": "khaak", "status": "active", "cause": "khaak raids", "intensity": 0.7}]

    def derive_pressures(self, s, f):
        return {"recent_losses": 0.6} if f == "argon" else {}

    def __init__(self):
        self._ev = [
            {"id": 10, "event_type": "battle", "summary": "argon lost patrols to khaak near the border",
             "primary_faction": "argon", "secondary_faction": "khaak", "sector_id": "hat1", "importance": 4, "source": "engine"},
            {"id": 11, "event_type": "war", "summary": "argon escalates against khaak",
             "primary_faction": "argon", "secondary_faction": "khaak", "sector_id": "hat1", "importance": 4, "source": "engine"},
            {"id": 12, "event_type": "reaction", "summary": "argon feels resentment", "primary_faction": "argon",
             "secondary_faction": "khaak", "sector_id": "", "importance": 3, "source": "reaction"},  # must be SKIPPED
            {"id": 13, "event_type": "diplomatic", "summary": "boron observes quietly", "primary_faction": "boron",
             "secondary_faction": "", "sector_id": "", "importance": 1, "source": "engine"},  # below min importance
        ]

    def list_world_events(self, s, limit=120, min_importance=1):
        return [e for e in self._ev if e["importance"] >= min_importance]


def run_narrator_selftest() -> dict:
    checks: list[dict] = []
    ok = lambda n, p, d=None: checks.append({"name": n, "pass": bool(p), "detail": d})
    n = Narrator(_FakeMem())

    arts = n.run_pass("s", chat_fn=None, budget=3)
    ok("produces_article", len(arts) >= 1, [a["title"] for a in arts])
    if arts:
        a = arts[0]
        ok("article_has_title_body", bool(a["title"]) and bool(a["body"]), a)
        ok("name_hygiene", "khaak" not in (a["title"] + a["body"]).lower() and "Kha'ak" in (a["title"] + a["body"]), a["body"])
        ok("participants_resolved", "Argon Federation" in a["participants"], a["participants"])
        # 2nd-pass: concrete EVIDENCE cited (standing value / losses), a QUOTE, and a thematic TOPIC.
        ok("body_cites_evidence", ("-1.00" in a["body"] or "at war" in a["body"] or "losses" in a["body"]), a["body"])
        ok("has_quote", bool(a.get("quote")), a.get("quote"))
        ok("has_topic", a.get("topic") in ("Military", "Political", "Economic", "Territorial"), a.get("topic"))
    # cause-gating: the reaction row + the low-importance row must NOT spawn their own article
    titles = " ".join(x["title"] + x["body"] for x in arts).lower()
    ok("skips_reaction_and_trivial", "boron" not in titles and "resentment" not in titles, titles[:120])
    # cursor: a second pass with no new events yields nothing
    ok("cursor_dedup", n.run_pass("s", chat_fn=None) == [], None)
    # no events -> no article (cause-gated)
    empty = Narrator(type("M", (), {"list_world_events": lambda self, s, **k: [], "list_factions": lambda self, s: [],
                                     "list_sectors": lambda self, s: [], "FACTION_NAMES": {}})())
    ok("no_cause_no_article", empty.run_pass("s") == [], None)

    passed = sum(1 for c in checks if c["pass"])
    return {"allPassed": passed == len(checks), "pass": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(run_narrator_selftest(), indent=1))
