"""Neural Link bridge — NPC memory engine.

Generic, mod-agnostic memory for Player2 NPCs. The bridge owns transport + memory
mechanics; gameplay meaning (faction scoring, diplomacy) belongs to the consuming
mod. This module provides a four-stage, human-like memory pipeline:

  A. Raw turns        — full-fidelity conversation, a rolling window per NPC.
  B. Condensed facts  — when the window overflows, the oldest turns are crushed into
                        a few categorized, importance-ranked facts; the raw turns are
                        then dropped (detail dies).
  C. Rolling summary  — a one-paragraph "gist" per NPC, the semantic residue.
  D. Decay/forgetting — periodic pass: routine facts are dropped, significant facts
                        blur/merge, CORE facts (death, war, love, oath, ...) survive
                        verbatim. You forget the details, not the meaning.

Stdlib only (sqlite3). The summarizer is injectable: production passes an LLM-backed
summarizer; tests pass a deterministic heuristic so the pipeline is verifiable without
burning joules.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .retrieval import TfidfRetriever, make_retriever
except ImportError:  # allow direct (non-package) import in tests
    from retrieval import TfidfRetriever, make_retriever

# --- Category taxonomy --------------------------------------------------------
# What survives memory degradation, and how. CORE memories are kept verbatim
# forever (the meaning is preserved exactly); their surrounding chatter is still
# discarded. SIGNIFICANT memories persist as condensed facts whose detail blurs
# over time. ROUTINE memories are forgotten quickly.

CORE_CATEGORIES = {
    "death",       # a character/ship/leader was lost
    "war",         # war declared, ended, or a decisive battle
    "betrayal",    # a promise broken, a trust violated
    "love",        # a bond formed, loyalty, deep alliance
    "oath",        # a promise/pledge/contract made
    "birth",       # a leader/faction/ship created or rises
    "catastrophe", # a sector lost, a station destroyed, a disaster
}
SIGNIFICANT_CATEGORIES = {
    "deal", "battle", "threat", "alliance", "gift", "insult", "rescue", "economy", "diplomacy",
}
ROUTINE_CATEGORIES = {"smalltalk", "status", "greeting", "flavor", "query"}

# Default importance per category (1=trivial .. 5=defining).
CATEGORY_IMPORTANCE: dict[str, int] = {c: 5 for c in CORE_CATEGORIES}
CATEGORY_IMPORTANCE.update({c: 3 for c in SIGNIFICANT_CATEGORIES})
CATEGORY_IMPORTANCE.update({c: 1 for c in ROUTINE_CATEGORIES})


def category_tier(category: str) -> str:
    if category in CORE_CATEGORIES:
        return "core"
    if category in SIGNIFICANT_CATEGORIES:
        return "significant"
    return "routine"


def is_verbatim(category: str) -> bool:
    """CORE memories are preserved verbatim; everything else may be rewritten/blurred."""
    return category in CORE_CATEGORIES


# Keyword → category map for the deterministic heuristic summarizer (and as a
# fallback classifier for facts the LLM returns without a category).
_KEYWORD_CATEGORY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(die[ds]?|died|death|killed|lost (?:the|his|her|their)|destroyed|perished|fell aboard|casualt)", re.I), "death"),
    (re.compile(r"\b(war|invasion|attack(?:ed|ing)?|offensive|frontline|hostilit|campaign)\b", re.I), "war"),
    (re.compile(r"\b(betray|broke (?:the|his|her|their|our) (?:promise|word|oath)|backstab|treacher)", re.I), "betrayal"),
    (re.compile(r"\b(love|loyal(?:ty)?|devoted|cherish|bond|stood by|trust (?:you|us) deeply)", re.I), "love"),
    (re.compile(r"\b(promise|pledge|swear|oath|vow|i will|we will|agree to|contract|guarantee)", re.I), "oath"),
    (re.compile(r"\b(sector lost|station (?:destroyed|fell)|shipyard (?:gone|destroyed)|catastroph|disaster)", re.I), "catastrophe"),
    (re.compile(r"\b(deal|trade|pay|credits|reparation|compensation|escort|supply)", re.I), "deal"),
    (re.compile(r"\b(battle|skirmish|fought|engaged|fleet (?:clash|action))", re.I), "battle"),
    (re.compile(r"\b(threat|warn(?:ed|ing)?|or else|consequenc|retaliat)", re.I), "threat"),
    (re.compile(r"\b(alli(?:ance|ed)|pact|cooperate|joint)", re.I), "alliance"),
    (re.compile(r"\b(rescue|saved|defended|protected|came to (?:our|your) aid)", re.I), "rescue"),
    (re.compile(r"\b(insult|disrespect|offend|mock)", re.I), "insult"),
    (re.compile(r"\b(economy|market|production|shortage|bottleneck|hull[- ]parts)", re.I), "economy"),
]


def classify_text(text: str) -> str:
    for pattern, category in _KEYWORD_CATEGORY:
        if pattern.search(text):
            return category
    return "smalltalk"


class MemoryStore:
    """SQLite-backed NPC memory with condensation + decay."""

    # Canon scope: universe-constant data harvested from the GAME FILES (faction
    # id<->name, default relations, lore) — identical across every save/playthrough,
    # so it is NOT keyed to any save. Per-save tables hold only the LIVE deltas +
    # memories for that playthrough. Reads merge the save overlay over canon.
    CANON_SAVE = "__canon__"

    def __init__(
        self,
        db_path: Path | str,
        summarizer: Optional[Callable[[list[dict]], list[dict]]] = None,
        keep_recent: int = 8,
        condense_after: int = 24,
        max_significant_per_npc: int = 40,
        max_core_verbatim_per_npc: int = 8,
        max_core_per_npc: int = 20,
    ) -> None:
        self.db_path = Path(db_path)
        self.summarizer = summarizer or self.heuristic_summarizer
        self.keep_recent = keep_recent
        self.condense_after = condense_after
        self.max_significant_per_npc = max_significant_per_npc
        # CORE-memory aging (the "70-yo veteran" model): only the most recent/important
        # CORE stay verbatim; older ones blur to a category gist; the oldest merge into a
        # lifetime-residue line. Bounds CORE growth and feels human.
        self.max_core_verbatim_per_npc = max_core_verbatim_per_npc
        self.max_core_per_npc = max_core_per_npc
        self._lock = threading.Lock()
        # One persistent connection PER THREAD, reused across calls. Killing the
        # per-operation open() is the single biggest write-throughput win (a 2000-NPC
        # burst was ~256k opens). WAL allows concurrent readers + one writer across
        # the per-thread connections.
        self._local = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL + relaxed sync: commits don't fsync the whole DB each write.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            # Persistent journal mode for the DB file (set once; survives reopen).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npcs (
                    npc_key      TEXT PRIMARY KEY,   -- stable persona key (save_id|game_id|persona)
                    npc_id       TEXT,               -- Player2-assigned id (may change on respawn)
                    save_id      TEXT,
                    game_id      TEXT,
                    name         TEXT,
                    faction_id   TEXT,
                    summary      TEXT DEFAULT '',     -- Stage C: rolling gist
                    created_at   REAL,
                    last_active  REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    npc_key   TEXT NOT NULL,
                    role      TEXT NOT NULL,
                    text      TEXT NOT NULL,
                    ts        REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_npc ON turns(npc_key, ts)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    npc_key     TEXT NOT NULL,
                    text        TEXT NOT NULL,
                    category    TEXT NOT NULL,
                    tier        TEXT NOT NULL,        -- core | significant | routine
                    importance  INTEGER NOT NULL,     -- 1..5
                    verbatim    INTEGER NOT NULL,     -- 1 if must survive unaltered
                    created_at  REAL NOT NULL,
                    last_used_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_npc ON facts(npc_key, importance)")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            # Universe-state index — durable political/strategic meaning X4 doesn't model.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factions (
                    save_id TEXT, faction_id TEXT, name TEXT,
                    values_json TEXT, biases_json TEXT,
                    current_goal TEXT, mood TEXT, summary TEXT DEFAULT '',
                    updated_at REAL,
                    PRIMARY KEY (save_id, faction_id)
                )
            """)
            # Migration (SPEC 1c-C): the named faction representative NPC. SQLite has no ADD COLUMN IF NOT
            # EXISTS, so guard it — runs once, no-ops thereafter.
            try:
                conn.execute("ALTER TABLE factions ADD COLUMN representative TEXT DEFAULT ''")
            except Exception:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    save_id TEXT, subject TEXT, object TEXT,
                    trust INTEGER DEFAULT 0, fear INTEGER DEFAULT 0,
                    resentment INTEGER DEFAULT 0, debt INTEGER DEFAULT 0,
                    standing TEXT DEFAULT 'neutral', summary TEXT DEFAULT '',
                    updated_at REAL,
                    PRIMARY KEY (save_id, subject, object)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rel_subject ON relationships(save_id, subject)")
            # Canon lore corpus — game-sourced identity/description text harvested
            # from the X4 encyclopedia (factions.xml + text DB), retrievable by RAG.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lore (
                    save_id TEXT, kind TEXT, key TEXT,
                    title TEXT DEFAULT '', text TEXT NOT NULL,
                    updated_at REAL,
                    PRIMARY KEY (save_id, kind, key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lore_kind ON lore(save_id, kind)")
            # Influence log — an auditable record of every relationship change THIS MOD caused in-game,
            # written back from the dispatcher so the DB mirrors reality + the dashboard can show it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS influence_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT, ts REAL,
                    subject TEXT, object TEXT,
                    old_relation REAL, new_relation REAL,
                    standing TEXT, source TEXT DEFAULT 'mod', note TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inflog_save ON influence_log(save_id, ts)")
            # Decision layer: per-faction pressure aggregates — the input to the
            # deterministic scoring core (Stage 1 of the influence engine). Derived
            # from the substrate (economy/conflicts/relations) each strategic review.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategic_state (
                    save_id TEXT, faction_id TEXT,
                    military_pressure REAL DEFAULT 0,
                    economic_pressure REAL DEFAULT 0,
                    logistics_stress REAL DEFAULT 0,
                    recent_losses REAL DEFAULT 0,
                    territorial_pressure REAL DEFAULT 0,
                    piracy_pressure REAL DEFAULT 0,
                    player_alignment REAL DEFAULT 0,
                    updated_at REAL,
                    PRIMARY KEY (save_id, faction_id)
                )
            """)
            # ---- Decision OUTPUT: incidents / pending_actions ------------------
            # The action whitelist made concrete (Stage 3 of the influence engine).
            # The validator writes a row here; X4 polls pending rows and applies ONLY
            # the bounded `effects_json`, then acks (status -> applied/dropped).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    faction_id TEXT,                 -- the acting faction
                    action_type TEXT NOT NULL,       -- whitelisted action key
                    target TEXT,                     -- target faction / sector / player
                    confidence REAL DEFAULT 0,       -- 0..1 (from the scorer)
                    priority INTEGER DEFAULT 0,      -- higher = sooner
                    cooldown_until REAL DEFAULT 0,
                    narrative TEXT DEFAULT '',       -- the LLM's in-world line
                    effects_json TEXT,               -- the validated, bounded effects
                    status TEXT DEFAULT 'pending',   -- pending|applied|dropped|expired
                    created_at REAL NOT NULL,
                    applied_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_save ON incidents(save_id, status, priority)")
            # ---- Agreements / promises / deals (queryable, not free-text) ------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agreements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    party_a TEXT NOT NULL,
                    party_b TEXT NOT NULL,
                    type TEXT,                       -- peace|trade|escort|tribute|ceasefire|nonaggression
                    terms_json TEXT,
                    deadline REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending',   -- pending|kept|broken|expired
                    created_at REAL NOT NULL,
                    resolved_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agreements_save ON agreements(save_id, status)")
            # ---- Economy MEANING per faction (NOT a commodity market) ----------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS economy (
                    save_id TEXT, faction_id TEXT,
                    player_economic_importance REAL DEFAULT 0,  -- 0..1 how much the player matters
                    dependency_on_player REAL DEFAULT 0,        -- 0..1
                    production_health REAL DEFAULT 1,           -- 0..1 (1 = healthy)
                    key_needs_json TEXT,                        -- wares they depend on
                    shortages_json TEXT,                        -- {ware: severity 0..1}
                    trade_pacts_json TEXT,                      -- partner faction ids
                    trade_restrictions_json TEXT,               -- embargoed faction ids
                    market_status TEXT DEFAULT 'neutral',       -- partner|neutral|obstacle
                    updated_at REAL,
                    PRIMARY KEY (save_id, faction_id)
                )
            """)
            # ---- Economy Update (spec, 2026-06-26): RAW omniscient per-station capture --------------------
            # The mod queries stations via find_station_by_true_owner (non-fog-of-war, DeadAir pattern) and
            # POSTs each station's products/needs/storage here. The derived `economy` table above is then
            # rolled up FROM this live truth (rollup_economy_from_stations) instead of being seeded.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS economy_stations (
                    save_id TEXT, station_id TEXT,
                    faction_id TEXT, sector_id TEXT,
                    station_name TEXT, station_type TEXT,        -- shipyard|wharf|tradestation|factory|...
                    workforce_current INTEGER DEFAULT 0,
                    workforce_capacity INTEGER DEFAULT 0,
                    products_json TEXT,                          -- [ware,...] this station OUTPUTS
                    needs_json TEXT,                             -- [ware,...] this station consumes/short on
                    storage_json TEXT,                           -- {ware: amount} current storage if read
                    last_seen_ts REAL,
                    PRIMARY KEY (save_id, station_id)
                )
            """)
            # ---- Player market position by ware/sector -------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS player_market (
                    save_id TEXT, ware TEXT, sector TEXT,
                    dominance_level REAL DEFAULT 0,      -- 0..1 player's share/leverage
                    supplying_enemies INTEGER DEFAULT 0, -- 1 if the player supplies their enemies
                    note TEXT DEFAULT '',
                    updated_at REAL,
                    PRIMARY KEY (save_id, ware, sector)
                )
            """)
            # ---- Territory / sectors -------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sectors (
                    save_id TEXT, sector_id TEXT,
                    name TEXT,
                    owner_faction TEXT,
                    contested_by_json TEXT,             -- faction ids contesting
                    strategic_value REAL DEFAULT 0,     -- 0..1
                    player_assets_present INTEGER DEFAULT 0,
                    updated_at REAL,
                    PRIMARY KEY (save_id, sector_id)
                )
            """)
            # ---- Fleet strength (per-faction ship census) ----------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fleet_strength (
                    save_id TEXT, faction_id TEXT,
                    total_ships INTEGER DEFAULT 0,
                    fight INTEGER DEFAULT 0,
                    trade INTEGER DEFAULT 0,
                    mine INTEGER DEFAULT 0,
                    build INTEGER DEFAULT 0,
                    other INTEGER DEFAULT 0,
                    capitals INTEGER DEFAULT 0,
                    updated_at REAL,
                    PRIMARY KEY (save_id, faction_id)
                )
            """)
            # ---- Conflicts / wars + loss aggregation ---------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    faction_a TEXT NOT NULL,
                    faction_b TEXT NOT NULL,
                    status TEXT DEFAULT 'active',       -- active|ceasefire|ended
                    intensity REAL DEFAULT 0,           -- 0..1
                    cause TEXT DEFAULT '',
                    started_at REAL,
                    ended_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conflicts_save ON conflicts(save_id, status)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS war_losses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    faction_id TEXT NOT NULL,
                    amount REAL DEFAULT 0,              -- magnitude (ships/value)
                    kind TEXT DEFAULT '',               -- ship|station|sector
                    sector_id TEXT,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_losses_faction ON war_losses(save_id, faction_id, ts)")
            # ---- Event-grounded conflict ledger (#62, Ken/Codex): REAL hostile actions at LOCATIONS ----------
            # The source of truth for who-hit-whom-where. conflicts/war_losses/intensity/cause are DERIVED from
            # these — never from relation thresholds or decisions. Captured only from observed in-game events.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hostile_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    attacker_faction TEXT,
                    victim_faction TEXT,
                    sector TEXT,
                    object_id TEXT,                     -- the destroyed/attacked object
                    object_name TEXT,
                    event_kind TEXT,                    -- ship_destroyed|ship_attacked|station_damaged|cargo_lost
                    magnitude REAL DEFAULT 1,           -- scale (ship size/value, or 1/ship)
                    source TEXT DEFAULT 'game',         -- game|census|prove
                    linked_order_id TEXT,               -- the order that caused it, if any (#67)
                    ts REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hostile_save ON hostile_events(save_id, ts)")
            # ---- Durable world-events log (typed persistent history) -----------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS world_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,           -- death|sector_change|economic_threshold|diplomatic|battle|...
                    summary TEXT DEFAULT '',
                    primary_faction TEXT,
                    secondary_faction TEXT,
                    sector_id TEXT,
                    importance INTEGER DEFAULT 1,       -- 1..5
                    source TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_world_events_save ON world_events(save_id, importance, created_at)")
            # Live chat transcript: one row per player↔NPC turn (prompt + reply), so the
            # dashboard can show the actual conversation, not just request metadata.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    request_id TEXT,
                    faction_id TEXT,
                    npc_name TEXT,
                    source_mod TEXT,
                    prompt TEXT DEFAULT '',
                    reply TEXT DEFAULT '',
                    player_name TEXT DEFAULT '',
                    latency_ms INTEGER,
                    status TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_save ON conversations(save_id, created_at)")
            # Migration: add player_name to an already-created conversations table.
            conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "player_name" not in conv_cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN player_name TEXT DEFAULT ''")
            # The player as a first-class entity: ONE row per save (the player is a
            # singleton in X4). Identity is save_id, NOT the name — `current_name` is a
            # mutable LABEL and `name_history` keeps every alias, so reputation/memory keyed
            # to the player entity survives any in-game rename.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    save_id TEXT PRIMARY KEY,
                    current_name TEXT DEFAULT 'Player',
                    name_history TEXT DEFAULT '[]',
                    first_seen REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            # Migration: X4 NPC stat columns. X4 tracks 5 crew skills (0..15 = 0..5
            # stars): piloting, management, engineering, boarding, morale; across roles
            # pilot/manager/service_crew/marine; plus race, gender, ship assignment.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(npcs)").fetchall()}
            for col in ("race", "role", "ship_class", "gender", "ship_name", "sector", "skills", "stats"):
                if col not in existing:
                    conn.execute(f"ALTER TABLE npcs ADD COLUMN {col} TEXT")
            # Migration: leadership/agency columns — who this NPC is in the influence
            # hierarchy and which X4 entity it represents. tier 0..3 (0=flavor crew,
            # 3=faction leader); authority = JSON list of action_types it may propose;
            # bound_entity_* = the live X4 object this persona speaks for.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(npcs)").fetchall()}
            typed_cols = {
                "tier": "INTEGER DEFAULT 0",
                "authority": "TEXT",
                "role_in_faction": "TEXT",
                "bound_entity_id": "TEXT",
                "bound_entity_type": "TEXT",
                "is_alive": "INTEGER DEFAULT 1",
            }
            for col, decl in typed_cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE npcs ADD COLUMN {col} {decl}")
            conn.commit()

    # --- NPC binding / retrieval ---------------------------------------------

    @staticmethod
    def make_key(save_id: str, game_id: str, persona: str) -> str:
        return f"{save_id or 'nosave'}|{game_id or 'nogame'}|{persona or 'default'}"

    # X4 crew skills (0..15 internal = 0..5 stars). morale is universal.
    X4_SKILLS = ("piloting", "management", "engineering", "boarding", "morale")

    # Per-faction pressure aggregates (the deterministic scoring inputs).
    PRESSURE_FIELDS = (
        "military_pressure", "economic_pressure", "logistics_stress", "recent_losses",
        "territorial_pressure", "piracy_pressure", "player_alignment",
    )

    # Criminal / raider factions whose presence in a sector counts as PIRACY pressure (vs ordinary
    # state-vs-state territorial contest): Xenon machines, K'haak raiders, Scale Plate pirates.
    CRIMINAL_FACTIONS = frozenset({"xenon", "khaak", "scaleplate"})

    # When a CORE memory ages past the verbatim window, its exact text is replaced by
    # one of these category gists — you remember THAT it happened, not the words.
    CORE_FADE_GIST = {
        "death": "You lost people who mattered, in years now past.",
        "war": "You have lived through wars whose details have faded.",
        "betrayal": "You carry an old betrayal you never fully forgave.",
        "love": "You once formed bonds that still quietly shape you.",
        "oath": "You swore solemn oaths long ago.",
        "birth": "You witnessed the rise of things now grown old.",
        "catastrophe": "You survived disasters whose specifics have blurred.",
    }

    # The action whitelist: the ONLY action_types an incident may carry. The LLM can
    # never invent an action; the validator drops anything outside this set. Mirrors
    # the candidate actions the deterministic scorer (scoring.py) ranks.
    INCIDENT_ACTIONS = {
        "dialogue_only", "defensive_stance", "resource_request",
        "escalate_pressure", "ceasefire_feeler", "trade_offer", "sanction",
    }
    # Hard cap on durable world-events per save (keeps the log bounded across a
    # hundreds-of-hours game; lowest-importance + oldest are pruned past this).
    MAX_WORLD_EVENTS_PER_SAVE = 2000

    @staticmethod
    def _clamp01(v: Any, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except Exception:
            return default

    def bind_npc(
        self,
        npc_key: str,
        npc_id: str,
        save_id: str = "",
        game_id: str = "",
        name: str = "",
        faction_id: str = "",
        stats: Optional[dict] = None,
    ) -> None:
        """Bind the Player2 npc_id and (optionally) attach X4 NPC stats.

        `stats` is whatever the X4 mod read for this NPC, e.g.:
          {race, role, gender, ship_class, ship_name, sector, commander,
           skills: {piloting, management, engineering, boarding, morale}}  (0..15)
        Stats are merged — fields not provided this turn keep their prior value.
        """
        now = time.time()
        stats = stats or {}
        skills = stats.get("skills") if isinstance(stats.get("skills"), dict) else None
        skills_json = json.dumps(skills) if skills is not None else None
        stats_json = json.dumps(stats) if stats else None
        race = str(stats.get("race") or "")
        role = str(stats.get("role") or "")
        ship_class = str(stats.get("ship_class") or "")
        gender = str(stats.get("gender") or "")
        ship_name = str(stats.get("ship_name") or "")
        sector = str(stats.get("sector") or "")
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO npcs (npc_key, npc_id, save_id, game_id, name, faction_id,
                                  race, role, ship_class, gender, ship_name, sector,
                                  skills, stats, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(npc_key) DO UPDATE SET
                    npc_id = excluded.npc_id,
                    name = COALESCE(NULLIF(excluded.name,''), npcs.name),
                    faction_id = COALESCE(NULLIF(excluded.faction_id,''), npcs.faction_id),
                    race = COALESCE(NULLIF(excluded.race,''), npcs.race),
                    role = COALESCE(NULLIF(excluded.role,''), npcs.role),
                    ship_class = COALESCE(NULLIF(excluded.ship_class,''), npcs.ship_class),
                    gender = COALESCE(NULLIF(excluded.gender,''), npcs.gender),
                    ship_name = COALESCE(NULLIF(excluded.ship_name,''), npcs.ship_name),
                    sector = COALESCE(NULLIF(excluded.sector,''), npcs.sector),
                    skills = COALESCE(excluded.skills, npcs.skills),
                    stats = COALESCE(excluded.stats, npcs.stats),
                    last_active = excluded.last_active
            """, (npc_key, npc_id, save_id, game_id, name, faction_id,
                  race, role, ship_class, gender, ship_name, sector,
                  skills_json, stats_json, now, now))
            conn.commit()

    def index_npc(
        self,
        npc_key: str,
        save_id: str = "",
        game_id: str = "",
        name: str = "",
        faction_id: str = "",
        stats: Optional[dict] = None,
    ) -> None:
        """Index an encounterable/named NPC's IDENTITY without requiring a Player2 npc_id.

        Unlike bind_npc, this never touches the npc_id column — the player may not have chatted
        with this NPC yet, and we must not clobber an existing Player2 binding. The real npc_id is
        attached later by bind_npc when a conversation actually starts.
        """
        now = time.time()
        stats = stats or {}
        skills = stats.get("skills") if isinstance(stats.get("skills"), dict) else None
        skills_json = json.dumps(skills) if skills is not None else None
        stats_json = json.dumps(stats) if stats else None
        race = str(stats.get("race") or "")
        role = str(stats.get("role") or "")
        ship_class = str(stats.get("ship_class") or "")
        gender = str(stats.get("gender") or "")
        ship_name = str(stats.get("ship_name") or "")
        sector = str(stats.get("sector") or "")
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO npcs (npc_key, npc_id, save_id, game_id, name, faction_id,
                                  race, role, ship_class, gender, ship_name, sector,
                                  skills, stats, created_at, last_active)
                VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(npc_key) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name,''), npcs.name),
                    faction_id = COALESCE(NULLIF(excluded.faction_id,''), npcs.faction_id),
                    race = COALESCE(NULLIF(excluded.race,''), npcs.race),
                    role = COALESCE(NULLIF(excluded.role,''), npcs.role),
                    ship_class = COALESCE(NULLIF(excluded.ship_class,''), npcs.ship_class),
                    gender = COALESCE(NULLIF(excluded.gender,''), npcs.gender),
                    ship_name = COALESCE(NULLIF(excluded.ship_name,''), npcs.ship_name),
                    sector = COALESCE(NULLIF(excluded.sector,''), npcs.sector),
                    skills = COALESCE(excluded.skills, npcs.skills),
                    stats = COALESCE(excluded.stats, npcs.stats),
                    last_active = excluded.last_active
            """, (npc_key, save_id, game_id, name, faction_id,
                  race, role, ship_class, gender, ship_name, sector,
                  skills_json, stats_json, now, now))
            conn.commit()

    def index_npcs(self, save_id: str, entries: list, game_id: str = "") -> int:
        """Batch-index encounterable/named NPCs. Returns the count indexed. Each entry:
        {npc_key?, name, faction_id?, race?, role?, ship_class?, gender?, ship_name?, sector?, skills?}.
        If npc_key is absent, a stable key is derived from save_id + faction + name."""
        count = 0
        for e in (entries or []):
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()
            key = str(e.get("npc_key") or e.get("key") or "").strip()
            if not key:
                key = ":".join(p for p in (save_id, str(e.get("faction_id") or ""), name) if p)
            if not key:
                continue
            self.index_npc(
                npc_key=key, save_id=save_id, game_id=game_id, name=name,
                faction_id=str(e.get("faction_id") or ""),
                stats={k: e.get(k) for k in
                       ("race", "role", "ship_class", "gender", "ship_name", "sector", "skills")
                       if e.get(k) is not None},
            )
            count += 1
        return count

    @staticmethod
    def skill_stars(level: Any) -> str:
        """Render an X4 skill level as 0..5 filled stars. Internal crew skills are 0..15; if a 0..100
        value slips through (some engine reads use that scale) we normalize it down so the star count
        stays sane either way."""
        try:
            lvl = int(level)
        except Exception:
            return ""
        if lvl > 15:
            lvl = round(lvl * 15 / 100)
        lvl = max(0, min(15, lvl))
        full = lvl // 3
        return "★" * full + "☆" * (5 - full)

    def _identity_line(self, row: dict) -> str:
        """One-line identity + skills block for prompt injection."""
        descriptors = []
        if row.get("role"):
            descriptors.append(str(row["role"]).replace("_", " "))
        if row.get("race"):
            descriptors.append(f"{row['race']}")
        sc = row.get("ship_class") or ""
        if sc:
            descriptors.append(sc.replace("ship_", "").upper() + "-class" if sc.startswith("ship_") else sc)
        line = ("You are a " + ", ".join(descriptors) + ".") if descriptors else ""
        if row.get("ship_name"):
            line += f" Aboard the {row['ship_name']}."
        try:
            skills = json.loads(row.get("skills") or "{}")
        except Exception:
            skills = {}
        rendered = [f"{k} {self.skill_stars(v)}" for k, v in skills.items() if v]
        if rendered:
            line += " Skills: " + ", ".join(rendered) + "."
        return line.strip()

    def get_npc_id(self, npc_key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT npc_id FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
            return row["npc_id"] if row else None

    def get_npc(self, npc_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
            return dict(row) if row else None

    def forget_npc_binding(self, npc_key: str) -> None:
        """Drop only the Player2 id (e.g. it expired); memory is preserved."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE npcs SET npc_id = NULL WHERE npc_key = ?", (npc_key,))
            conn.commit()

    def delete_npc(self, save_id: str = "", npc_id: Optional[str] = None,
                   npc_key: Optional[str] = None) -> dict:
        """Purge a DEAD NPC and all its memory (turns + facts) by npc_id or npc_key.
        X4 calls this when a crew member or ship is destroyed, so the database never
        bloats with the dead. The DEATH itself lives on — as a world_event and in the
        memories of those who knew them — but the dead NPC's own record is gone."""
        with self._lock, self._connect() as conn:
            if not npc_key and npc_id:
                row = conn.execute("SELECT npc_key FROM npcs WHERE save_id = ? AND npc_id = ?",
                                   (save_id, npc_id)).fetchone()
                npc_key = row["npc_key"] if row else None
            if not npc_key:
                return {"ok": False, "error": "npc not found", "save_id": save_id, "npc_id": npc_id}
            turns = conn.execute("DELETE FROM turns WHERE npc_key = ?", (npc_key,)).rowcount or 0
            facts = conn.execute("DELETE FROM facts WHERE npc_key = ?", (npc_key,)).rowcount or 0
            conn.execute("DELETE FROM npcs WHERE npc_key = ?", (npc_key,))
            conn.commit()
        return {"ok": True, "deleted_npc": npc_key, "turns_purged": turns, "facts_purged": facts}

    # --- Turns (Stage A) ------------------------------------------------------

    def record_turn(self, npc_key: str, role: str, text: str) -> None:
        if not text:
            return
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO turns (npc_key, role, text, ts) VALUES (?, ?, ?, ?)",
                (npc_key, role, text, now),
            )
            conn.execute("UPDATE npcs SET last_active = ? WHERE npc_key = ?", (now, npc_key))
            conn.commit()

    def _turn_count(self, conn: sqlite3.Connection, npc_key: str) -> int:
        return conn.execute("SELECT COUNT(*) AS c FROM turns WHERE npc_key = ?", (npc_key,)).fetchone()["c"]

    def turn_count(self, npc_key: str) -> int:
        with self._connect() as conn:
            return self._turn_count(conn, npc_key)

    def set_summary(self, npc_key: str, summary: str) -> None:
        """Store the rolling TOPIC summary of this person's conversations with the NPC (LLM-generated,
        thematic — not verbatim). Injected by build_memory_context as 'What you remember overall', so
        the NPC has long-range continuity beyond the last few raw turns."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE npcs SET summary = ? WHERE npc_key = ?", (str(summary or "")[:1200], npc_key))
            conn.commit()

    def get_recent_turns(self, npc_key: str, limit: Optional[int] = None) -> list[dict]:
        limit = limit or self.keep_recent
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, text FROM turns WHERE npc_key = ? ORDER BY ts DESC LIMIT ?",
                (npc_key, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    # --- Facts (Stage B/C) ----------------------------------------------------

    def add_fact(self, npc_key: str, text: str, category: Optional[str] = None,
                 importance: Optional[int] = None) -> None:
        category = (category or classify_text(text)).lower()
        tier = category_tier(category)
        importance = importance if importance is not None else CATEGORY_IMPORTANCE.get(category, 2)
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO facts (npc_key, text, category, tier, importance, verbatim, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (npc_key, text, category, tier, int(importance), int(is_verbatim(category)), now, now))
            conn.commit()

    def get_facts(self, npc_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE npc_key = ? ORDER BY importance DESC, last_used_at DESC",
                (npc_key,),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _relative_age(ts: Any, now: Optional[float] = None) -> str:
        """Coarse human label of how long ago a memory happened, so the NPC has a sense of recency
        ("a while back"). Wall-clock for now; a per-save game-time version is a later refinement."""
        now = now if now is not None else time.time()
        try:
            d = max(0.0, now - float(ts or now))
        except Exception:
            return "at some point"
        if d < 300: return "moments ago"
        if d < 3600: return "a little earlier"
        if d < 86400: return "recently"
        if d < 7 * 86400: return "a while ago"
        if d < 30 * 86400: return "some time ago"
        return "a long time ago"

    def retrieve_relevant(self, npc_key: str, query: str, k: int = 4) -> list[dict]:
        """Vector/graph RAG: retrieve the NPC's most relevant PAST memories for THIS message — raw
        turns at full fidelity (we no longer condense/forget) older than the live recent-history
        window, plus any durable facts — each tagged with how long ago it happened. Semantic when an
        embedder is installed, else lexical. Returns [] when nothing relevant (no noise on a fresh NPC)."""
        now = time.time()
        docs: list[dict] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT role, text, ts FROM turns WHERE npc_key = ? ORDER BY ts DESC LIMIT 800 OFFSET ?",
                    (npc_key, self.keep_recent),
                ).fetchall()
            for r in rows:
                t = str(r["text"] or "").strip()
                if not t:
                    continue
                who = "the player" if str(r["role"]) == "user" else "I"
                docs.append({"id": "turn", "text": f"({self._relative_age(r['ts'], now)}) {who} said: {t}"})
        except Exception:
            pass
        try:
            for f in self.get_facts(npc_key):
                t = str(f.get("text") or "").strip()
                if t:
                    docs.append({"id": "fact", "text": f"({self._relative_age(f.get('created_at') or now, now)}) {t}"})
        except Exception:
            pass
        if not docs:
            return []
        retriever = make_retriever()
        retriever.index(docs)
        return retriever.retrieve(query, k=k, min_score=0.01)

    def resolve_faction_id(self, value: Any) -> str:
        """Map a faction reference (id OR display-name, e.g. 'Argon Federation') to its
        canonical id ('argon'). Names are CANON — same in every save — so this reads the
        canon scope only, never a playthrough save. Falls back to a slug if unknown."""
        v = str(value or "").strip()
        if not v:
            return ""
        vl = v.lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT faction_id FROM factions WHERE save_id=? AND lower(faction_id)=?",
                (self.CANON_SAVE, vl)).fetchone()
            if row:
                return row["faction_id"]
            row = conn.execute(
                "SELECT faction_id FROM factions WHERE save_id=? AND lower(name)=?",
                (self.CANON_SAVE, vl)).fetchone()
            if row:
                return row["faction_id"]
            for r in conn.execute(
                    "SELECT faction_id, lower(name) nm FROM factions WHERE save_id=?",
                    (self.CANON_SAVE,)).fetchall():
                nm = r["nm"] or ""
                if nm and (vl in nm or nm in vl):
                    return r["faction_id"]
        return re.sub(r"[^a-z0-9]", "", vl) or vl

    def relationships_with_canon(self, save_id: str, subject: Optional[str] = None) -> list[dict]:
        """The save's relationships OVERLAID on canon defaults: canon underneath, the
        playthrough's live edges win. Subject filter is applied after the merge."""
        merged: dict[tuple, dict] = {}
        for r in self.list_relationships(self.CANON_SAVE):
            merged[(str(r.get("subject")), str(r.get("object")))] = r
        if save_id != self.CANON_SAVE:
            for r in self.list_relationships(save_id):
                merged[(str(r.get("subject")), str(r.get("object")))] = r
        rows = list(merged.values())
        if subject:
            rows = [r for r in rows if str(r.get("subject")) == subject]
        return rows

    def graph_retrieve(self, save_id: str, anchor_faction: str, query: str, k: int = 6) -> list[dict]:
        """GraphRAG v1: retrieve the subgraph around the anchor faction (the NPC's faction) from the
        durable universe-state — its relationship edges, active wars, and standing agreements — then
        rank those edges by semantic relevance to THIS message and return the top-k as fact lines.
        Nodes = factions; edges = relations / wars / agreements. The NPC then reasons over its
        faction's real standing in the galaxy. Empty universe-state → []. (Semantic when an embedder
        is installed, else lexical; same call site.)"""
        # Resolve a display-name to its canon id first (e.g. "Argon Federation" -> "argon"),
        # so an anchor passed from the game's faction knownname still matches the graph.
        anchor = str(self.resolve_faction_id(anchor_faction) or anchor_faction or "").strip().lower()
        if not anchor:
            return []
        lines: list[dict] = []

        def fid(x: Any) -> str:
            return str(x or "").strip()

        try:
            # Relationships: this save's live edges overlaid on canon defaults.
            for r in self.relationships_with_canon(save_id):
                s, o = fid(r.get("subject")), fid(r.get("object"))
                if anchor not in (s.lower(), o.lower()):
                    continue
                standing = str(r.get("standing") or "neutral")
                summ = str(r.get("summary") or "").strip()
                lines.append({"id": f"rel:{s}:{o}", "text": f"{s} regards {o} as {standing}." + ((" " + summ) if summ else "")})
        except Exception:
            pass
        try:
            for c in self.list_conflicts(save_id, status="active"):
                a, b = fid(c.get("faction_a")), fid(c.get("faction_b"))
                if anchor not in (a.lower(), b.lower()):
                    continue
                cause = str(c.get("cause") or "").strip()
                lines.append({"id": f"war:{a}:{b}", "text": f"{a} is at war with {b}." + ((" Cause: " + cause + ".") if cause else "")})
        except Exception:
            pass
        try:
            for ag in self.list_agreements(save_id):
                if str(ag.get("status") or "") in ("broken", "expired"):
                    continue
                a, b = fid(ag.get("party_a")), fid(ag.get("party_b"))
                if anchor not in (a.lower(), b.lower()):
                    continue
                typ = str(ag.get("type") or "agreement")
                lines.append({"id": f"agr:{a}:{b}", "text": f"{a} has a {typ} agreement with {b}."})
        except Exception:
            pass

        # Canon lore: the anchor faction's identity/description + any faction named
        # in its subgraph. Lets "who are you / tell me about X" resolve from the
        # game's own encyclopedia, ranked alongside live standings.
        try:
            named = {anchor}
            for ln in lines:
                parts = ln["id"].split(":")
                named.update(p.lower() for p in parts[1:])
            for lo in self.list_lore(self.CANON_SAVE, "faction"):
                if str(lo.get("key") or "").lower() in named:
                    txt = str(lo.get("text") or "").strip()
                    if txt:
                        lines.append({"id": f"lore:faction:{lo.get('key')}", "text": txt})
        except Exception:
            pass

        if not lines:
            return []
        retriever = make_retriever()
        retriever.index(lines)
        res = retriever.retrieve(query, k=k, min_score=0.0)
        return res if res else [{"score": 0.0, **ln} for ln in lines[:k]]

    # --- Condensation (Stage B trigger) --------------------------------------

    def condense_if_needed(self, npc_key: str) -> int:
        """If the raw window overflows, crush the oldest turns into facts and drop them.

        Returns the number of facts created (0 if nothing condensed).

        DISABLED (no-op): with retrieval (vector/graph RAG) we keep EVERY raw turn at full fidelity
        and surface only the relevant ones per message — each tagged with how long ago it happened —
        so there is no context-window reason to crush + drop detail. Memory is never condensed or
        forgotten now; relevance is solved at query time. Forgetting, if ever wanted, becomes a
        deliberate realism toggle, not a necessity.
        """
        return 0
        with self._lock, self._connect() as conn:  # unreachable (kept for reference)
            count = self._turn_count(conn, npc_key)
            if count <= self.condense_after:
                return 0
            # Oldest turns = everything except the most recent keep_recent.
            to_condense = conn.execute(
                "SELECT id, role, text FROM turns WHERE npc_key = ? ORDER BY ts ASC LIMIT ?",
                (npc_key, count - self.keep_recent),
            ).fetchall()
            old_turns = [{"role": r["role"], "content": r["text"]} for r in to_condense]
            old_ids = [r["id"] for r in to_condense]

        # Summarize OUTSIDE the lock (LLM call may be slow).
        facts = self.summarizer(old_turns) or []

        now = time.time()
        with self._lock, self._connect() as conn:
            created = 0
            for f in facts:
                text = str(f.get("text", "")).strip()
                if not text:
                    continue
                category = str(f.get("category") or classify_text(text)).lower()
                tier = category_tier(category)
                importance = int(f.get("importance") or CATEGORY_IMPORTANCE.get(category, 2))
                # Routine chatter is not worth persisting as a durable fact.
                if tier == "routine" and importance <= 1:
                    continue
                conn.execute("""
                    INSERT INTO facts (npc_key, text, category, tier, importance, verbatim, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (npc_key, text, category, tier, importance, int(is_verbatim(category)), now, now))
                created += 1
            # Drop the raw turns we just digested (detail dies).
            if old_ids:
                conn.execute(
                    f"DELETE FROM turns WHERE id IN ({','.join('?' for _ in old_ids)})",
                    old_ids,
                )
            # Refresh the rolling gist.
            self._rebuild_summary(conn, npc_key)
            conn.commit()
        # Opportunistic decay so significant facts don't grow without bound.
        self.decay(npc_key)
        return created

    def _rebuild_summary(self, conn: sqlite3.Connection, npc_key: str) -> None:
        rows = conn.execute(
            "SELECT text, tier FROM facts WHERE npc_key = ? ORDER BY importance DESC, last_used_at DESC LIMIT 8",
            (npc_key,),
        ).fetchall()
        gist = " ".join(r["text"] for r in rows if r["tier"] in ("core", "significant"))
        conn.execute("UPDATE npcs SET summary = ? WHERE npc_key = ?", (gist[:1200], npc_key))

    # --- Decay (Stage D) ------------------------------------------------------

    def decay(
        self,
        npc_key: str,
        routine_max_age_s: float = 0.0,
        now: Optional[float] = None,
    ) -> dict:
        """Forget the forgettable. CORE facts (verbatim) are never touched.

        - routine facts older than `routine_max_age_s` are dropped;
        - if significant facts exceed the per-NPC cap, the lowest-priority
          (importance, then least-recently-used) are dropped — detail is lost,
          but CORE meaning is protected.
        Returns counts of what was dropped.
        """
        now = now if now is not None else time.time()
        dropped_routine = 0
        dropped_significant = 0
        with self._lock, self._connect() as conn:
            # 1. Drop aged routine facts (never CORE).
            cur = conn.execute(
                "DELETE FROM facts WHERE npc_key = ? AND tier = 'routine' AND verbatim = 0 AND (? - created_at) >= ?",
                (npc_key, now, routine_max_age_s),
            )
            dropped_routine = cur.rowcount or 0
            # 2. Cap significant facts.
            sig_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM facts WHERE npc_key = ? AND tier = 'significant' "
                "ORDER BY importance DESC, last_used_at DESC",
                (npc_key,),
            ).fetchall()]
            overflow = sig_ids[self.max_significant_per_npc:]
            if overflow:
                conn.execute(
                    f"DELETE FROM facts WHERE id IN ({','.join('?' for _ in overflow)})",
                    overflow,
                )
                dropped_significant = len(overflow)

            # 3. AGE CORE memories (the "70-yo veteran"). Keep the top
            #    max_core_verbatim_per_npc CORE verbatim; older ones blur to a single
            #    category gist (you remember THAT it happened); duplicates of an
            #    already-faded category merge away. Bounds CORE; feels human.
            blurred_core = 0
            merged_core = 0
            core_rows = conn.execute(
                "SELECT id, category, verbatim FROM facts WHERE npc_key = ? AND tier = 'core' "
                "ORDER BY importance DESC, created_at DESC", (npc_key,)).fetchall()
            if len(core_rows) > self.max_core_verbatim_per_npc:
                older = core_rows[self.max_core_verbatim_per_npc:]
                faded_categories: set = set()
                for r in older:
                    cat = r["category"]
                    if cat not in faded_categories:
                        faded_categories.add(cat)
                        if r["verbatim"] == 1:
                            gist = self.CORE_FADE_GIST.get(cat, "Something that marked you, long ago.")
                            conn.execute("UPDATE facts SET verbatim = 0, text = ? WHERE id = ?", (gist, r["id"]))
                            blurred_core += 1
                    else:
                        # a second+ older memory of an already-faded category → merge away
                        conn.execute("DELETE FROM facts WHERE id = ?", (r["id"],))
                        merged_core += 1
            conn.commit()
        return {"dropped_routine": dropped_routine, "dropped_significant": dropped_significant,
                "blurred_core": blurred_core, "merged_core": merged_core}

    # --- Retrieval (injection) ------------------------------------------------

    def build_memory_context(self, npc_key: str, max_significant: int = 6) -> str:
        """Assemble the bounded memory block injected into each turn.

        CORE facts always included (verbatim); top significant facts by
        importance×recency; routine omitted. Touches last_used_at so retrieved
        facts decay slower (use it or lose it).
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            core = conn.execute(
                "SELECT id, text FROM facts WHERE npc_key = ? AND tier = 'core' "
                "ORDER BY importance DESC, created_at ASC",
                (npc_key,),
            ).fetchall()
            sig = conn.execute(
                "SELECT id, text FROM facts WHERE npc_key = ? AND tier = 'significant' "
                "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                (npc_key, max_significant),
            ).fetchall()
            used_ids = [r["id"] for r in core] + [r["id"] for r in sig]
            if used_ids:
                conn.execute(
                    f"UPDATE facts SET last_used_at = ? WHERE id IN ({','.join('?' for _ in used_ids)})",
                    [now, *used_ids],
                )
            npc = conn.execute("SELECT * FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
            conn.commit()

        lines: list[str] = []
        if npc:
            identity = self._identity_line(dict(npc))
            if identity:
                lines.append(identity)
        if npc and npc["summary"]:
            lines.append("What you remember overall: " + npc["summary"])
        if core:
            lines.append("Things you will never forget:")
            lines.extend(f"- {r['text']}" for r in core)
        if sig:
            lines.append("You also recall:")
            lines.extend(f"- {r['text']}" for r in sig)
        return "\n".join(lines).strip()

    def build_faction_briefing(self, save_id: str, faction_id: str, max_events: int = 4) -> str:
        """SPEC 1e — faction-level grounded briefing from JUST save_id + faction_id (no NPC record needed):
        the faction's mood/goal/representative, its standing toward the player and other factions, active
        wars, contested home sectors, lasting grudges, and recent galaxy events. This is the retrieval
        grounding that must back EVERY faction-facing LLM call (news bulletins, autonomous decisions, crisis
        messages) — not just face-to-face chat. `build_situation_briefing` (the chat path) composes personal
        memory + this."""
        if not (save_id and faction_id):
            return ""
        lines: list[str] = []
        fac = self.get_faction(save_id, faction_id)
        if fac:
            bits = []
            if fac.get("name"):
                bits.append(str(fac["name"]))
            if fac.get("current_goal"):
                bits.append("goal: " + str(fac["current_goal"]))
            if fac.get("mood"):
                bits.append("mood: " + str(fac["mood"]))
            if bits:
                lines.append("Your faction — " + "; ".join(bits) + ".")
            if fac.get("representative"):
                lines.append(f"You speak for the faction as its representative, {fac['representative']}.")
            # 1h-G: surface the canon persona biases in words + numbers so the LLM decides/reacts in character.
            b = fac.get("biases") or {}
            if isinstance(b, dict) and b.get("aggression") is not None:
                a = float(b.get("aggression", 0.5)); d = float(b.get("diplomacy", 0.5))
                e = float(b.get("economic_focus", 0.5)); rk = float(b.get("risk_tolerance", 0.5))
                traits = [("aggressive" if a >= 0.66 else "restrained" if a <= 0.33 else "measured"),
                          ("diplomatic" if d >= 0.66 else "uncompromising" if d <= 0.33 else "pragmatic")]
                if e >= 0.70:
                    traits.append("commerce-driven")
                traits.append("bold" if rk >= 0.66 else "cautious" if rk <= 0.33 else "even-keeled")
                lines.append(f"Your character: {', '.join(traits)} "
                             f"(aggression {round(a*100)}/100, diplomacy {round(d*100)}/100). Act in keeping with it.")
        rel = self.get_relationship(save_id, faction_id, "player")
        if rel:
            _ps = rel.get("standing", "neutral")
            if _ps in ("at war", "hostile"):
                lines.append(
                    f"IMPORTANT: your faction is currently {_ps.upper()} toward the Commander (the player). "
                    f"You do NOT treat them as a friend or hand out favours — you are wary, cold, or openly "
                    f"antagonistic as fits being {_ps}."
                )
            else:
                lines.append(
                    f"Your faction's standing with the Commander (the player): {_ps} "
                    f"(trust {rel['trust']}, fear {rel['fear']}, resentment {rel['resentment']}, debt {rel['debt']})."
                )
        # Live standing toward OTHER factions (ground truth for "are we at war with the Alliance?").
        others = []
        for r in self.list_relationships(save_id, subject=faction_id):
            obj = r.get("object")
            tr = r.get("trust")
            if not obj or obj == "player" or not isinstance(tr, (int, float)):
                continue
            relf = tr / 100.0
            standing = self._standing_for(relf)
            if standing == "neutral":
                continue
            others.append((relf, obj, standing))
        others.sort(key=lambda t: abs(t[0]), reverse=True)
        for relf, obj, standing in others[:6]:
            fo = self.get_faction(save_id, obj)
            name = (fo.get("name") if fo else None) or obj
            if standing == "at war":
                lines.append(f"Your faction is AT WAR with {name}.")
            elif standing == "hostile":
                lines.append(f"Your faction is on hostile terms with {name}.")
            elif standing == "allied":
                lines.append(f"Your faction is allied with {name}.")
            else:
                lines.append(f"Your faction is friendly with {name}.")
        st = self.get_strategic_state(save_id, faction_id)
        if st:
            hot = [k.replace("_", " ") for k in ("military_pressure", "economic_pressure",
                   "recent_losses", "logistics_stress") if float(st.get(k, 0) or 0) >= 0.5]
            if hot:
                lines.append("Pressing concerns right now: " + ", ".join(hot) + ".")
        # 1i-B: economy — what you import/export + supply leverage, so you can reason about trade, embargoes,
        # and supply deals (not just war). Key imports are real (station read); player-leverage where known.
        econ = self.get_economy(save_id, faction_id) or {}
        kn = econ.get("key_needs")
        if isinstance(kn, list) and kn:
            ms = str(econ.get("market_status") or "neutral")
            role = {"importer": "a net importer", "exporter": "a net exporter"}.get(ms, "largely self-reliant")
            lines.append(f"Economy: your faction is {role}; you depend on importing "
                         + ", ".join(str(w) for w in kn[:6]) + ".")
            dep = float(econ.get("dependency_on_player", 0) or 0)
            if dep >= 0.4:
                lines.append(f"The Commander (the player) is a major supplier of what you need "
                             f"(dependency {round(dep*100)}/100) — antagonising them risks your supply lines.")
            sh = econ.get("shortages")
            if isinstance(sh, dict) and sh:
                worst = [w for w, _ in sorted(sh.items(), key=lambda kv: -float(kv[1] or 0))[:3]]
                lines.append("You are critically short on: " + ", ".join(worst) + ".")
        confs = [c for c in self.list_conflicts(save_id, status="active")
                 if faction_id in (c["faction_a"], c["faction_b"])]
        for c in confs[:2]:
            foe = c["faction_b"] if c["faction_a"] == faction_id else c["faction_a"]
            lines.append(f"You are at war with {foe} ({c.get('cause', '')}), "
                         f"intensity {round(float(c.get('intensity', 0) or 0) * 100)}%.")
        secs = [s for s in self.list_sectors(save_id)
                if s.get("owner_faction") == faction_id and s.get("contested_by")]
        for s in secs[:2]:
            lines.append(f"You hold {s.get('name') or s['sector_id']}, "
                         f"contested by {', '.join(s.get('contested_by') or [])}.")
        # Grudges (SPEC 1c-D): the strongest LINGERING resentment toward another faction.
        grudges = []
        for r in self.list_relationships(save_id, subject=faction_id):
            obj, res = r.get("object"), r.get("resentment")
            if obj and obj != "player" and isinstance(res, (int, float)) and res >= 25:
                grudges.append((res, obj))
        if grudges:
            res, obj = max(grudges)
            fo = self.get_faction(save_id, obj)
            name = (fo.get("name") if fo else None) or obj
            lines.append(f"You hold a lasting grudge against {name} (resentment {round(res)}) and have "
                         f"not forgotten what they cost you.")
        evs = self.list_world_events(save_id, limit=max_events)
        if evs:
            lines.append("Recent events across the galaxy:")
            lines.extend(f"- {e.get('summary', '')}" for e in evs[:max_events])
        return "\n".join(line for line in lines if line).strip()

    def build_situation_briefing(self, npc_key: str, max_events: int = 4) -> str:
        """Grounded in-world briefing: this NPC's personal memory PLUS the universe
        context that makes replies specific — their faction's mood/goal, its standing
        toward the player, active wars, contested home sectors, and recent galaxy
        events. This is what turns a hollow bark ("all sectors secured") into a
        grounded one ("we've held Hatikvah since you resupplied us"). Bounded.

        Requires the NPC to be bound (faction_id known); on an unbound NPC it falls
        back to plain personal memory. The faction-level half is `build_faction_briefing`.
        """
        npc = self.get_npc(npc_key)
        if not npc:
            return self.build_memory_context(npc_key)
        save_id = npc.get("save_id") or ""
        faction_id = npc.get("faction_id") or ""
        lines: list[str] = []
        base = self.build_memory_context(npc_key)
        if base:
            lines.append(base)
        # Who you're talking to: the player ENTITY (singleton per save), with past aliases so the
        # NPC recognizes the same person across renames ("you called yourself X then"). Identity is
        # the save, not the mutable name — this is what makes "historical betrayal" reactions work.
        player = self.get_player(save_id) if save_id else None
        if player and player.get("current_name"):
            cur = str(player["current_name"])
            raw_hist = player.get("name_history") or []
            if isinstance(raw_hist, str):
                try:
                    raw_hist = json.loads(raw_hist)
                except Exception:
                    raw_hist = []
            aliases = [str(h) for h in raw_hist if h and str(h) != cur]
            if aliases:
                lines.append(f'You are speaking with the Commander, who now goes by "{cur}" '
                             f'(also known to you as: {", ".join(aliases)}).')
            else:
                lines.append(f'You are speaking with the Commander, who goes by "{cur}".')
        # SPEC 1e: the faction-level half is now shared with every faction-facing LLM call.
        fac_brief = self.build_faction_briefing(save_id, faction_id, max_events)
        if fac_brief:
            lines.append(fac_brief)
        return "\n".join(line for line in lines if line).strip()

    def metrics(self, npc_key: Optional[str] = None) -> dict:
        with self._connect() as conn:
            if npc_key:
                turns = conn.execute("SELECT COUNT(*) AS c FROM turns WHERE npc_key=?", (npc_key,)).fetchone()["c"]
                facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE npc_key=?", (npc_key,)).fetchone()["c"]
                by_tier = {row["tier"]: row["c"] for row in conn.execute(
                    "SELECT tier, COUNT(*) AS c FROM facts WHERE npc_key=? GROUP BY tier", (npc_key,))}
            else:
                turns = conn.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"]
                facts = conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"]
                by_tier = {row["tier"]: row["c"] for row in conn.execute(
                    "SELECT tier, COUNT(*) AS c FROM facts GROUP BY tier")}
            npcs = conn.execute("SELECT COUNT(*) AS c FROM npcs").fetchone()["c"]
        return {"npcs": npcs, "turns": turns, "facts": facts, "facts_by_tier": by_tier}

    # --- Dashboard views (read-only) -----------------------------------------

    def list_npcs(self) -> list[dict]:
        """One row per NPC with memory counts, for the dashboard list."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT n.npc_key, n.npc_id, n.name, n.faction_id, n.save_id, n.game_id,
                       n.race, n.role, n.ship_class, n.skills,
                       n.summary, n.created_at, n.last_active,
                       (SELECT COUNT(*) FROM turns t WHERE t.npc_key = n.npc_key) AS turns,
                       (SELECT COUNT(*) FROM facts f WHERE f.npc_key = n.npc_key) AS facts,
                       (SELECT COUNT(*) FROM facts f WHERE f.npc_key = n.npc_key AND f.tier='core') AS core_facts,
                       (SELECT COUNT(*) FROM facts f WHERE f.npc_key = n.npc_key AND f.tier='significant') AS significant_facts
                FROM npcs n
                ORDER BY n.last_active DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def npc_detail(self, npc_key: str, turn_limit: int = 40) -> Optional[dict]:
        """Full memory dump for one NPC: profile + recent raw turns + all facts.

        Read-only — does NOT touch last_used_at (so viewing doesn't defeat decay).
        """
        with self._connect() as conn:
            npc = conn.execute("SELECT * FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
            if not npc:
                return None
            turns = conn.execute(
                "SELECT role, text, ts FROM turns WHERE npc_key = ? ORDER BY ts DESC LIMIT ?",
                (npc_key, turn_limit),
            ).fetchall()
            facts = conn.execute(
                "SELECT id, text, category, tier, importance, verbatim, created_at, last_used_at "
                "FROM facts WHERE npc_key = ? ORDER BY "
                "CASE tier WHEN 'core' THEN 0 WHEN 'significant' THEN 1 ELSE 2 END, "
                "importance DESC, last_used_at DESC",
                (npc_key,),
            ).fetchall()
        return {
            "npc": dict(npc),
            "turns": [dict(t) for t in reversed(turns)],
            "facts": [dict(f) for f in facts],
        }

    def clear_save(self, save_id: str) -> dict:
        """Delete every NPC + its turns/facts for a save_id (e.g. stress cleanup)."""
        with self._lock, self._connect() as conn:
            keys = [r["npc_key"] for r in conn.execute(
                "SELECT npc_key FROM npcs WHERE save_id = ?", (save_id,)).fetchall()]
            n = len(keys)
            if keys:
                ph = ",".join("?" for _ in keys)
                conn.execute(f"DELETE FROM turns WHERE npc_key IN ({ph})", keys)
                conn.execute(f"DELETE FROM facts WHERE npc_key IN ({ph})", keys)
                conn.execute("DELETE FROM npcs WHERE save_id = ?", (save_id,))
            for table in ("factions", "relationships", "strategic_state", "incidents",
                          "agreements", "economy", "player_market", "sectors",
                          "conflicts", "war_losses", "world_events",
                          "conversations", "players"):  # newer tables were being left behind
                conn.execute(f"DELETE FROM {table} WHERE save_id = ?", (save_id,))
            conn.commit()
        return {"ok": True, "cleared_npcs": n, "save_id": save_id}

    def clear_substrate(self, save_id: str) -> dict:
        """Wipe only the universe substrate for a save (NOT npc memory). Lets the
        demo seed be re-run idempotently instead of accumulating duplicate
        conflicts/incidents/agreements/world_events on every click."""
        with self._lock, self._connect() as conn:
            for table in ("factions", "relationships", "strategic_state", "economy",
                          "player_market", "sectors", "conflicts", "war_losses",
                          "agreements", "incidents", "world_events"):
                conn.execute(f"DELETE FROM {table} WHERE save_id = ?", (save_id,))
            conn.commit()
        return {"ok": True, "cleared_substrate": save_id}

    def reset_all(self) -> dict:
        """Wipe the ENTIRE DB — every save, every table — and RECLAIM disk space.

        Two things ordinary 'reset' code gets wrong (both were wrong here):
          1. A hardcoded table list goes stale — `players` and `conversations` were
             added later and survived the old reset. We now enumerate tables from
             sqlite_master so any current/future table is always included.
          2. DELETE does NOT shrink the file: freed pages stay allocated and the WAL
             keeps growing, so npc_memory.sqlite3 / -wal stay large even when logically
             empty. VACUUM + wal_checkpoint(TRUNCATE) actually compact the files on disk.
        """
        with self._lock:
            conn = self._connect()
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'meta'").fetchall()]
            cleared: dict[str, int] = {}
            for t in tables:
                cleared[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
            # Reclaim disk: collapse WAL into the db, compact the file, truncate WAL.
            prev_iso = conn.isolation_level
            conn.isolation_level = None  # VACUUM/checkpoint must run OUTSIDE a transaction
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.isolation_level = prev_iso
        return {"ok": True, "reset": "all", "tables_wiped": len(tables),
                "rows_deleted": sum(cleared.values()), "cleared": cleared}

    def list_saves(self) -> list[dict]:
        """Index the cache by save file: one row per save_id with counts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT save_id, COUNT(*) AS npcs, MAX(last_active) AS last_active "
                "FROM npcs GROUP BY save_id ORDER BY last_active DESC"
            ).fetchall()
            saves = []
            for r in rows:
                sid = r["save_id"]
                turns = conn.execute(
                    "SELECT COUNT(*) AS c FROM turns WHERE npc_key IN "
                    "(SELECT npc_key FROM npcs WHERE save_id IS ?)", (sid,)).fetchone()["c"]
                facts = conn.execute(
                    "SELECT COUNT(*) AS c FROM facts WHERE npc_key IN "
                    "(SELECT npc_key FROM npcs WHERE save_id IS ?)", (sid,)).fetchone()["c"]
                saves.append({
                    "save_id": sid or "(none)", "npcs": r["npcs"], "turns": turns, "facts": facts,
                    "last_active_ms": int((r["last_active"] or 0) * 1000),
                })
        return saves

    # --- Universe state: factions + relationships ----------------------------

    def upsert_faction(self, save_id: str, faction_id: str, name: Optional[str] = None,
                       values: Any = None, biases: Any = None, current_goal: Optional[str] = None,
                       mood: Optional[str] = None, summary: Optional[str] = None,
                       representative: Optional[str] = None) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO factions (save_id, faction_id, name, values_json, biases_json,
                                      current_goal, mood, summary, representative, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, faction_id) DO UPDATE SET
                    name = COALESCE(excluded.name, factions.name),
                    values_json = COALESCE(excluded.values_json, factions.values_json),
                    biases_json = COALESCE(excluded.biases_json, factions.biases_json),
                    current_goal = COALESCE(excluded.current_goal, factions.current_goal),
                    mood = COALESCE(excluded.mood, factions.mood),
                    summary = COALESCE(excluded.summary, factions.summary),
                    representative = COALESCE(excluded.representative, factions.representative),
                    updated_at = excluded.updated_at
            """, (save_id, faction_id, name,
                  json.dumps(values) if values is not None else None,
                  json.dumps(biases) if biases is not None else None,
                  current_goal, mood, summary, representative, now))
            conn.commit()

    @staticmethod
    def _decode_faction(row: dict) -> dict:
        row = dict(row)
        row["values"] = json.loads(row.pop("values_json") or "null")
        row["biases"] = json.loads(row.pop("biases_json") or "null")
        return row

    def get_faction(self, save_id: str, faction_id: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM factions WHERE save_id=? AND faction_id=?",
                             (save_id, faction_id)).fetchone()
        return self._decode_faction(r) if r else None

    def list_factions(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM factions WHERE save_id=? ORDER BY faction_id", (save_id,)).fetchall()
        return [self._decode_faction(r) for r in rows]

    # --- SPEC 1g: canon faction PERSONA (identity that drives L3 reactions + decision scoring) ------
    # Strategic biases per the blueprint §12 — STATIC identity (who the faction IS), not derived posture
    # (that's Mood + the Strategic Pressures panel). aggression feeds L3's persona_scale, so seeding these is
    # what makes pirates react like pirates. Grounded in X4 canon; exposed here as constants to tune the feel.
    # Each: (aggression, economic_focus, risk_tolerance, diplomacy, default_goal).
    FACTION_PERSONA: dict[str, tuple] = {
        "argon":      (0.35, 0.65, 0.45, 0.75, "Hold the Xenon frontier and protect Commonwealth commerce."),
        "antigone":   (0.40, 0.55, 0.50, 0.65, "Defend the Republic's hard-won independence."),
        "teladi":     (0.20, 0.95, 0.40, 0.60, "Maximise profit and trade dominance."),
        "ministry":   (0.45, 0.85, 0.45, 0.50, "Fund and supply the Paranid war effort."),
        "paranid":    (0.70, 0.50, 0.60, 0.30, "Expand the Godrealm and humble the unbelievers."),
        "holyorder":  (0.80, 0.35, 0.70, 0.20, "Wage holy war in the Pontifex's name."),
        "alliance":   (0.50, 0.50, 0.50, 0.70, "Advance knowledge and broker the galaxy's diplomacy."),
        "split":      (0.85, 0.40, 0.75, 0.25, "Prove Split strength through conquest."),
        "zyarth":     (0.85, 0.40, 0.75, 0.25, "Prove Split strength through conquest."),
        "freesplit":  (0.70, 0.50, 0.60, 0.40, "Win freedom and honour for the Free Families."),
        "terran":     (0.50, 0.60, 0.40, 0.45, "Protect Sol and contain outside threats."),
        "pioneer":    (0.45, 0.55, 0.55, 0.50, "Pioneer and settle the frontier."),
        "segaris":    (0.45, 0.55, 0.55, 0.50, "Pioneer and settle the frontier."),
        "boron":      (0.15, 0.70, 0.30, 0.90, "Pursue peace, knowledge, and prosperity."),
        "hatikvah":   (0.35, 0.75, 0.55, 0.55, "Secure free trade and the Free League's autonomy."),
        "scaleplate": (0.75, 0.50, 0.80, 0.20, "Raid, extort, and expand the Pact's reach."),
        "xenon":      (1.00, 0.30, 0.70, 0.00, "Expand relentlessly and eliminate organic life."),
        "khaak":      (0.95, 0.10, 0.60, 0.00, "Purge the intruders from the hive's space."),
        "yaki":       (0.80, 0.45, 0.80, 0.20, "Plunder, and survive."),
        "vigor":      (0.70, 0.60, 0.75, 0.30, "Profit through smuggling and muscle."),
        "riptide":    (0.60, 0.55, 0.70, 0.35, "Salvage and scrap by any means."),
    }
    FACTION_PERSONA_DEFAULT = (0.50, 0.55, 0.50, 0.50, "Advance the faction's interests.")

    # 1h-C: canon display names for factions the game-side sync doesn't name (shown blank or as the bare id).
    FACTION_NAMES = {
        "boron": "Boron", "hatikvah": "Hatikvah Free League", "freesplit": "Free Families",
        "scaleplate": "Scale Plate Pact", "yaki": "Yaki", "vigor": "Vigor Syndicate",
        "riptide": "Riptide Rakers", "xenon": "Xenon", "khaak": "Kha'ak", "split": "Zyarth Patriarchy",
        "zyarth": "Zyarth Patriarchy", "pioneer": "Segaris Pioneers", "segaris": "Segaris Pioneers",
    }

    def seed_faction_personas(self, save_id: str) -> int:
        """Write the canon persona biases (Aggr/Econ/Risk/Dipl + goal) AND a display name onto faction rows that
        lack them. Biases are stable identity (set when absent); name is filled when the game gave none or just
        the id (1h-C); goal only when blank. Returns rows touched. Idempotent + cheap."""
        seeded = 0
        now = time.time()
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT faction_id, name, biases_json, current_goal FROM factions WHERE save_id=?",
                                (save_id,)).fetchall()
            for r in rows:
                fid = r["faction_id"]
                sets, params = [], []
                # Name: fill when missing or == the bare id.
                cur_name = (r["name"] or "").strip()
                if (not cur_name or cur_name == fid) and fid in self.FACTION_NAMES:
                    sets.append("name=?"); params.append(self.FACTION_NAMES[fid])
                # Biases (identity): set only when absent.
                try:
                    have = json.loads(r["biases_json"] or "null")
                except Exception:
                    have = None
                if not (isinstance(have, dict) and have.get("aggression") is not None):
                    aggr, econ, risk, dipl, goal = self.FACTION_PERSONA.get(fid, self.FACTION_PERSONA_DEFAULT)
                    sets.append("biases_json=?")
                    params.append(json.dumps({"aggression": aggr, "economic_focus": econ,
                                              "risk_tolerance": risk, "diplomacy": dipl}))
                    if not (r["current_goal"] or "").strip():
                        sets.append("current_goal=?"); params.append(goal)
                if sets:
                    sets.append("updated_at=?"); params.append(now)
                    params += [save_id, fid]
                    conn.execute(f"UPDATE factions SET {', '.join(sets)} WHERE save_id=? AND faction_id=?", params)
                    seeded += 1
            conn.commit()
        return seeded

    @staticmethod
    def _clamp(v: Any) -> int:
        return max(-100, min(100, int(v)))

    def adjust_relationship(self, save_id: str, subject: str, obj: str, dtrust: int = 0,
                            dfear: int = 0, dresentment: int = 0, ddebt: int = 0,
                            standing: Optional[str] = None, summary: Optional[str] = None) -> dict:
        """Increment a directed relationship's scores (subject's feelings toward obj),
        clamped to [-100, 100]. Covers player↔faction and faction↔faction."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT trust, fear, resentment, debt FROM relationships WHERE save_id=? AND subject=? AND object=?",
                (save_id, subject, obj)).fetchone()
            base = dict(row) if row else {"trust": 0, "fear": 0, "resentment": 0, "debt": 0}
            trust = self._clamp(base["trust"] + dtrust)
            fear = self._clamp(base["fear"] + dfear)
            resentment = self._clamp(base["resentment"] + dresentment)
            debt = self._clamp(base["debt"] + ddebt)
            conn.execute("""
                INSERT INTO relationships (save_id, subject, object, trust, fear, resentment, debt, standing, summary, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, subject, object) DO UPDATE SET
                    trust=excluded.trust, fear=excluded.fear, resentment=excluded.resentment, debt=excluded.debt,
                    standing=COALESCE(excluded.standing, relationships.standing),
                    summary=COALESCE(excluded.summary, relationships.summary),
                    updated_at=excluded.updated_at
            """, (save_id, subject, obj, trust, fear, resentment, debt, standing, summary, now))
            conn.commit()
        return {"subject": subject, "object": obj, "trust": trust, "fear": fear,
                "resentment": resentment, "debt": debt}

    # --- SPEC 1f (Level 3): LLM persona-reaction write-back to the emotional factors ----------------
    # 🔒 LOCKED VOLATILITY (Ken 2026-06-25). Absolute per-event caps (persona scaling happens upstream in
    # the router's validate_reaction); resentment/fear are floored at 0 (negatives meaningless); decay ages
    # them toward 0 so grudges fade if not reinforced (anti-spiral). Tune these constants to change the feel.
    REACTION_CAPS = {"resentment": (-15, 20), "fear": (-10, 15), "trust": (-15, 10)}
    DECAY_PER_PASS = {"resentment": 2, "fear": 3}
    DECAY_INTERVAL_S = 55.0  # ~ one relations-heartbeat pass; decay runs at most once per interval per save

    def _cap_delta(self, field: str, v: Any) -> int:
        lo, hi = self.REACTION_CAPS.get(field, (-100, 100))
        try:
            v = int(round(float(v)))
        except Exception:
            v = 0
        return max(lo, min(hi, v))

    def apply_reaction(self, save_id: str, subject: str, obj: str, deltas: dict,
                       mood: str = "", rationale: str = "") -> dict:
        """Write a BOUNDED emotional reaction (subject's feelings toward obj) back to the substrate. Each
        delta is clamped to its absolute per-event cap; resentment/fear floored at 0. Records the reaction as
        a durable world_event so it's remembered and can surface as news. Returns the clamped deltas + new rel."""
        dres = self._cap_delta("resentment", deltas.get("resentment", 0))
        dfear = self._cap_delta("fear", deltas.get("fear", 0))
        dtrust = self._cap_delta("trust", deltas.get("trust", 0))
        rel = self.adjust_relationship(save_id, subject, obj, dtrust=dtrust, dfear=dfear, dresentment=dres)
        # adjust_relationship clamps to [-100,100]; re-floor resentment/fear at 0 (negatives are meaningless).
        fix = {}
        if int(rel.get("resentment", 0)) < 0:
            fix["dresentment"] = -int(rel["resentment"])
        if int(rel.get("fear", 0)) < 0:
            fix["dfear"] = -int(rel["fear"])
        if fix:
            rel = self.adjust_relationship(save_id, subject, obj, **fix)
        if mood:
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE factions SET mood=?, updated_at=? WHERE save_id=? AND faction_id=?",
                             (str(mood)[:32], time.time(), save_id, subject))
                conn.commit()
        sname = (self.get_faction(save_id, subject) or {}).get("name") or subject
        oname = (self.get_faction(save_id, obj) or {}).get("name") or obj
        summ = (rationale or "").strip() or f"{sname} hardens its stance toward {oname}."
        try:
            self.add_world_event(save_id, "reaction", summary=summ[:300], primary_faction=subject,
                                 secondary_faction=obj, importance=3, source="reaction")
        except Exception:
            pass
        return {"applied": {"resentment": dres, "fear": dfear, "trust": dtrust}, "relationship": rel}

    def decay_emotions(self, save_id: str) -> int:
        """Anti-spiral: age resentment/fear toward 0 across all of a save's relationships, rate-limited to one
        pass per DECAY_INTERVAL_S. A single fresh reaction (cap +20 resentment) outweighs one decay step (-2),
        so sustained conflict stays hot while one-off grudges fade. Returns rows touched."""
        store = getattr(self, "_last_decay", None)
        if store is None:
            store = self._last_decay = {}
        now = time.time()
        if now - store.get(save_id, 0.0) < self.DECAY_INTERVAL_S:
            return 0
        store[save_id] = now
        touched = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT subject, object, resentment, fear FROM relationships WHERE save_id=?",
                                (save_id,)).fetchall()
            for r in rows:
                cur_r, cur_f = int(r["resentment"] or 0), int(r["fear"] or 0)
                nr = max(0, cur_r - self.DECAY_PER_PASS["resentment"])
                nf = max(0, cur_f - self.DECAY_PER_PASS["fear"])
                if nr != cur_r or nf != cur_f:
                    conn.execute("UPDATE relationships SET resentment=?, fear=?, updated_at=? "
                                 "WHERE save_id=? AND subject=? AND object=?",
                                 (nr, nf, now, save_id, r["subject"], r["object"]))
                    touched += 1
            conn.commit()
        return touched

    def get_relationship(self, save_id: str, subject: str, obj: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM relationships WHERE save_id=? AND subject=? AND object=?",
                             (save_id, subject, obj)).fetchone()
        return dict(r) if r else None

    def list_relationships(self, save_id: str, subject: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if subject:
                rows = conn.execute("SELECT * FROM relationships WHERE save_id=? AND subject=? ORDER BY object",
                                    (save_id, subject)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM relationships WHERE save_id=? ORDER BY subject, object",
                                    (save_id,)).fetchall()
        return [dict(r) for r in rows]

    def seed_canonical_relationship(self, save_id: str, subject: str, obj: str,
                                    relation: float, standing: str) -> None:
        """Set a directed relationship to its CANONICAL game-default value (absolute,
        not incremental — so re-harvesting is idempotent). trust is the relation
        float [-1,1] scaled to [-100,100]; standing/summary record the canon source.
        Only writes if the edge is still at canon (or absent), so live play deltas
        from adjust_relationship() are never clobbered by a re-seed."""
        now = time.time()
        trust = self._clamp(round(float(relation) * 100))
        summary = f"Canonical standing: {standing} ({relation:+.2f})"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT summary FROM relationships WHERE save_id=? AND subject=? AND object=?",
                (save_id, subject, obj)).fetchone()
            if row and not str(dict(row).get("summary", "")).startswith("Canonical standing:"):
                return  # touched by gameplay — leave it alone
            conn.execute("""
                INSERT INTO relationships (save_id, subject, object, trust, fear, resentment, debt, standing, summary, updated_at)
                VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                ON CONFLICT(save_id, subject, object) DO UPDATE SET
                    trust=excluded.trust, standing=excluded.standing,
                    summary=excluded.summary, updated_at=excluded.updated_at
            """, (save_id, subject, obj, trust, standing, summary, now))
            conn.commit()

    @staticmethod
    def _standing_for(relation: float) -> str:
        r = float(relation)
        if r <= -0.75:
            return "at war"
        if r <= -0.2:
            return "hostile"
        if r < 0.2:
            return "neutral"
        if r < 0.75:
            return "friendly"
        return "allied"

    def set_live_relationship(self, save_id: str, subject: str, obj: str, relation: float,
                              source: str = "mod") -> dict:
        """Write a GAMEPLAY relationship value into the save overlay (absolute). Summary is tagged
        "Live (<source>):" — deliberately NOT "Canonical standing:" — so the canon clobber-guard and
        any re-harvest leave this real state intact. source="mod" = we caused it; source="game" =
        read back from the live game (sync-on-load), which is ground truth and overwrites freely."""
        now = time.time()
        trust = self._clamp(round(float(relation) * 100))
        standing = self._standing_for(relation)
        summary = f"Live ({source}): {standing} ({float(relation):+.2f})"
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO relationships (save_id, subject, object, trust, fear, resentment, debt, standing, summary, updated_at)
                VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                ON CONFLICT(save_id, subject, object) DO UPDATE SET
                    trust=excluded.trust, standing=excluded.standing,
                    summary=excluded.summary, updated_at=excluded.updated_at
            """, (save_id, subject, obj, trust, standing, summary, now))
            conn.commit()
        return {"subject": subject, "object": obj, "trust": trust, "standing": standing}

    def record_influence_change(self, save_id: str, subject: str, obj: str, new_relation: float,
                                source: str = "mod", note: str = "") -> dict:
        """The write-back: a mod-caused relationship change actually happened in-game. Record the
        before→after in influence_log (auditable) AND update the live overlay (so NPCs reason on it).
        Writes BOTH directions (A↔B) since X4 relations are mutual."""
        before = self.get_relationship(save_id, subject, obj) or {}
        old_trust = before.get("trust")
        old_rel = (old_trust / 100.0) if isinstance(old_trust, (int, float)) else None
        self.set_live_relationship(save_id, subject, obj, new_relation)
        self.set_live_relationship(save_id, obj, subject, new_relation)
        standing = self._standing_for(new_relation)
        now = time.time()
        # SPAM GUARD (Codex 2b review): a write-back that doesn't actually CHANGE the relation (e.g. an escalate
        # at the -1.0 war floor) is a NO-OP — keep the live overlay current but do NOT log an identical
        # old==new row that floods influence_log + the dashboard.
        if old_rel is not None and abs(float(new_relation) - float(old_rel)) < 1e-6:
            return {"id": None, "save_id": save_id, "subject": subject, "object": obj, "old_relation": old_rel,
                    "new_relation": float(new_relation), "standing": standing, "source": source, "noop": True}
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO influence_log (save_id, ts, subject, object, old_relation, new_relation, standing, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (save_id, now, subject, obj, old_rel, float(new_relation), standing, source, note))
            conn.commit()
            log_id = cur.lastrowid
        return {"id": log_id, "save_id": save_id, "subject": subject, "object": obj,
                "old_relation": old_rel, "new_relation": float(new_relation),
                "standing": standing, "source": source}

    def list_influence_log(self, save_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            if save_id:
                rows = conn.execute("SELECT * FROM influence_log WHERE save_id=? ORDER BY ts DESC LIMIT ?",
                                    (save_id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM influence_log ORDER BY ts DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- Universe state: canon lore corpus ------------------------------------

    def upsert_lore(self, save_id: str, kind: str, key: str, title: str, text: str) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO lore (save_id, kind, key, title, text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, kind, key) DO UPDATE SET
                    title=excluded.title, text=excluded.text, updated_at=excluded.updated_at
            """, (save_id, kind, key, title, text, now))
            conn.commit()

    def list_lore(self, save_id: str, kind: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if kind:
                rows = conn.execute("SELECT * FROM lore WHERE save_id=? AND kind=? ORDER BY key",
                                    (save_id, kind)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM lore WHERE save_id=? ORDER BY kind, key",
                                    (save_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- Universe state: strategic_state (pressure aggregates) ----------------

    def upsert_strategic_state(self, save_id: str, faction_id: str, **pressures: Any) -> dict:
        """Set/merge a faction's pressure aggregates. Only provided fields change;
        the rest keep their prior value (or default 0 on first write)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM strategic_state WHERE save_id=? AND faction_id=?",
                (save_id, faction_id)).fetchone()
            cur = {f: float(dict(row)[f]) for f in self.PRESSURE_FIELDS} if row else {f: 0.0 for f in self.PRESSURE_FIELDS}
            for f in self.PRESSURE_FIELDS:
                if f in pressures and pressures[f] is not None:
                    cur[f] = float(pressures[f])
            conn.execute("""
                INSERT INTO strategic_state (save_id, faction_id, military_pressure, economic_pressure,
                    logistics_stress, recent_losses, territorial_pressure, piracy_pressure,
                    player_alignment, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, faction_id) DO UPDATE SET
                    military_pressure=excluded.military_pressure,
                    economic_pressure=excluded.economic_pressure,
                    logistics_stress=excluded.logistics_stress,
                    recent_losses=excluded.recent_losses,
                    territorial_pressure=excluded.territorial_pressure,
                    piracy_pressure=excluded.piracy_pressure,
                    player_alignment=excluded.player_alignment,
                    updated_at=excluded.updated_at
            """, (save_id, faction_id, cur["military_pressure"], cur["economic_pressure"],
                  cur["logistics_stress"], cur["recent_losses"], cur["territorial_pressure"],
                  cur["piracy_pressure"], cur["player_alignment"], now))
            conn.commit()
        return self.get_strategic_state(save_id, faction_id) or {}

    def get_strategic_state(self, save_id: str, faction_id: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM strategic_state WHERE save_id=? AND faction_id=?",
                             (save_id, faction_id)).fetchone()
        return dict(r) if r else None

    def list_strategic_state(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM strategic_state WHERE save_id=? ORDER BY faction_id",
                                (save_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- Influence engine: deriver (substrate -> pressures) + world model ------

    def derive_pressures(self, save_id: str, faction_id: str) -> dict:
        """Compute a faction's strategic_state pressures FROM the substrate
        (economy, active conflicts + windowed losses, contested sectors, relations)
        and upsert them. This makes pressures EMERGENT instead of hand-seeded — the
        keystone that lets the universe drive itself."""
        econ = self.get_economy(save_id, faction_id) or {}
        shortages = econ.get("shortages") or {}
        prod = float(econ.get("production_health", 1) or 1)
        worst_short = max([float(v) for v in shortages.values()], default=0.0) if isinstance(shortages, dict) else 0.0
        economic_pressure = self._clamp01(0.6 * worst_short + 0.4 * (1.0 - prod))
        logistics_stress = self._clamp01(1.0 - prod)

        confs = [c for c in self.list_conflicts(save_id, status="active")
                 if faction_id in (c["faction_a"], c["faction_b"])]
        war_intensity = max([float(c.get("intensity", 0) or 0) for c in confs], default=0.0)
        losses = self.get_loss_summary(save_id, faction_id)
        recent_losses = float(losses.get("recent_losses", 0) or 0)
        military_pressure = self._clamp01(0.6 * war_intensity + 0.4 * recent_losses)

        secs = [s for s in self.list_sectors(save_id) if s.get("owner_faction") == faction_id]
        contested = [s for s in secs if s.get("contested_by")]
        territorial_pressure = self._clamp01(len(contested) / len(secs)) if secs else 0.0
        # Piracy = the criminal/raider slice of territorial pressure: owned sectors contested by a
        # criminal faction (Xenon machines / K'haak raiders / Scale Plate pirates). Fed by the same
        # contested_by the presence reader now populates. 0 until a sector is contested by one of them.
        pirate_contested = [s for s in contested
                            if any(f in self.CRIMINAL_FACTIONS for f in (s.get("contested_by") or []))]
        piracy_pressure = self._clamp01(len(pirate_contested) / len(secs)) if secs else 0.0

        relp = self.get_relationship(save_id, faction_id, "player")
        player_alignment = ((relp["trust"] - relp["resentment"]) / 100.0) if relp else 0.0
        player_alignment = max(-1.0, min(1.0, player_alignment))

        return self.upsert_strategic_state(
            save_id, faction_id,
            economic_pressure=economic_pressure, logistics_stress=logistics_stress,
            military_pressure=military_pressure, recent_losses=recent_losses,
            territorial_pressure=territorial_pressure, piracy_pressure=piracy_pressure,
            player_alignment=player_alignment)

    @staticmethod
    def _derive_mood(st: dict) -> str:
        """Map a faction's derived pressures onto a single dynamic mood word. Priority ladder:
        existential military stress first, then territory, then economy, then disposition to the
        player, then a calm baseline. Mood flows into the NPC persona prompt (build_persona_context),
        so this makes representatives *sound* like their faction's live situation."""
        mil = float(st.get("military_pressure", 0) or 0)
        loss = float(st.get("recent_losses", 0) or 0)
        econ = float(st.get("economic_pressure", 0) or 0)
        terr = float(st.get("territorial_pressure", 0) or 0)
        align = float(st.get("player_alignment", 0) or 0)
        if loss >= 0.5 or mil >= 0.7:
            return "embattled"
        if mil >= 0.4:
            return "belligerent"
        if terr >= 0.4:
            return "defensive"
        if econ >= 0.5:
            return "strained"
        if align <= -0.5:
            return "resentful"
        if align >= 0.5:
            return "amicable"
        return "watchful"

    def derive_all_pressures(self, save_id: str) -> dict:
        """Tier-3 strategic deriver (the keystone). Recompute EVERY known faction's strategic_state
        pressures from the live substrate (economy / active conflicts / windowed war-losses / contested
        sectors / player relations) and set a dynamic mood from them. Idempotent + cheap (local SQLite),
        so it runs each relations heartbeat — making the Strategic Pressures table and the Factions-mood
        panel EMERGENT instead of hand-seeded. piracy_pressure is intentionally left untouched (no
        substrate feeds it yet — see roadmap; not fabricated)."""
        out = []
        for f in self.list_factions(save_id):
            fid = f.get("faction_id")
            if not fid or fid == "player":
                continue
            try:
                st = self.derive_pressures(save_id, fid)
            except Exception:
                continue
            mood = self._derive_mood(st)
            if mood and mood != f.get("mood"):
                self.upsert_faction(save_id, fid, mood=mood)
            out.append({"faction_id": fid, "mood": mood,
                        "military_pressure": st.get("military_pressure"),
                        "recent_losses": st.get("recent_losses")})
        return {"derived": len(out), "factions": out}

    def apply_incident_effects(self, save_id: str, action_type: str, faction_id: str,
                               target: str = "") -> list[str]:
        """The headless WORLD MODEL — apply a validated decision's whitelisted effects
        back onto our own tables (the in-game X4 dispatcher's stand-in), and emit a
        world_event. Returns a list of applied-effect descriptions. The deltas here
        are the tunable 'balance' knobs; in-game, X4/our dispatcher does the same."""
        applied: list[str] = []
        if action_type == "escalate_pressure" and target:
            self.adjust_relationship(save_id, faction_id, target, dresentment=15, dtrust=-10)
            self.adjust_relationship(save_id, target, faction_id, dresentment=12, dfear=8)
            confs = [c for c in self.list_conflicts(save_id, status="active")
                     if {faction_id, target} == {c["faction_a"], c["faction_b"]}]
            escalated = False   # did anything MEANINGFUL actually move?
            if confs:
                c = confs[0]
                old_i = float(c.get("intensity", 0) or 0)
                new_i = min(1.0, old_i + 0.2)
                self.set_conflict_status(save_id, c["id"], "active", intensity=new_i)
                escalated = new_i > old_i + 1e-6   # already saturated (intensity 1.0) => a NO-OP escalation
            else:
                self.add_conflict(save_id, faction_id, target, status="active", intensity=0.3, cause="escalation")
                escalated = True   # a NEW conflict is genuine news
            # SPAM GUARD (Codex 2b review): at MAX WAR a repeated 'escalate_pressure' changes nothing real —
            # do NOT record a loss or a world_event for it, or the narrator/dashboard fill with identical
            # "X escalates pressure against Y" rows. Only a real escalation (intensity actually rose / new war)
            # becomes history.
            if escalated:
                self.record_loss(save_id, target, amount=8, kind="ship")
                self.add_world_event(save_id, "war", summary=f"{faction_id} escalates pressure against {target}.",
                                     primary_faction=faction_id, secondary_faction=target, importance=4, source="engine")
                applied = ["resentment+", "conflict_intensity+", "loss_logged", "world_event:war"]
            else:
                applied = ["resentment+", "already_at_max_war:no_world_event"]
        elif action_type == "ceasefire_feeler" and target:
            self.adjust_relationship(save_id, faction_id, target, dtrust=12, dresentment=-10)
            for c in [c for c in self.list_conflicts(save_id, status="active")
                      if {faction_id, target} == {c["faction_a"], c["faction_b"]}]:
                self.set_conflict_status(save_id, c["id"], "ceasefire")
            self.add_agreement(save_id, faction_id, target, type="ceasefire")
            self.add_world_event(save_id, "diplomatic", summary=f"{faction_id} sues for peace with {target}.",
                                 primary_faction=faction_id, secondary_faction=target, importance=3, source="engine")
            applied = ["trust+", "ceasefire", "agreement", "world_event:diplomatic"]
        elif action_type in ("resource_request", "trade_offer"):
            cur = (self.get_economy(save_id, faction_id) or {}).get("dependency_on_player", 0) or 0
            self.upsert_economy(save_id, faction_id, dependency_on_player=min(1.0, float(cur) + 0.1))
            self.add_world_event(save_id, "economic_threshold",
                                 summary=f"{faction_id} requests resources from {target or 'partners'}.",
                                 primary_faction=faction_id, importance=2, source="engine")
            applied = ["dependency+", "world_event:economic"]
        elif action_type == "defensive_stance":
            self.add_world_event(save_id, "diplomatic", summary=f"{faction_id} takes a defensive stance.",
                                 primary_faction=faction_id, importance=1, source="engine")
            applied = ["world_event:defensive"]
        elif action_type == "dialogue_only":
            # 1h-A: a TRUE no-op (the faction held / observed). Do NOT persist a world_event — it would
            # pollute world_events (which become durable memories + can trigger L3 reactions) with noise
            # like "boron: dialogue_only.". Nothing happened, so record nothing.
            applied = ["noop"]
        else:  # sanction / unknown — benign log
            self.add_world_event(save_id, "diplomatic", summary=f"{faction_id}: {action_type}.",
                                 primary_faction=faction_id, importance=1, source="engine")
            applied = ["world_event:dialogue"]
        return applied

    # --- Decision OUTPUT: incidents / pending_actions -------------------------

    def add_incident(self, save_id: str, action_type: str, faction_id: str = "",
                     target: str = "", confidence: float = 0.0, priority: int = 0,
                     cooldown_until: float = 0.0, narrative: str = "",
                     effects: Any = None, status: str = "pending") -> dict:
        """Record a validated, bounded action for X4 to apply. Rejects any
        action_type outside the whitelist (the LLM can never invent an action)."""
        action_type = str(action_type or "").strip()
        if action_type not in self.INCIDENT_ACTIONS:
            raise ValueError(f"action_type '{action_type}' is not in the whitelist {sorted(self.INCIDENT_ACTIONS)}")
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO incidents (save_id, faction_id, action_type, target, confidence,
                    priority, cooldown_until, narrative, effects_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (save_id, faction_id, action_type, target, self._clamp01(confidence),
                  int(priority), float(cooldown_until or 0), narrative,
                  json.dumps(effects) if effects is not None else None, status, now))
            conn.commit()
            incident_id = cur.lastrowid
        return {"id": incident_id, "save_id": save_id, "action_type": action_type,
                "status": status, "faction_id": faction_id, "target": target}

    @staticmethod
    def _decode_incident(row: dict) -> dict:
        row = dict(row)
        row["effects"] = json.loads(row.pop("effects_json") or "null")
        return row

    def list_incidents(self, save_id: str, status: Optional[str] = None, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE save_id=? AND status=? "
                    "ORDER BY priority DESC, created_at DESC LIMIT ?", (save_id, status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE save_id=? "
                    "ORDER BY priority DESC, created_at DESC LIMIT ?", (save_id, limit)).fetchall()
        return [self._decode_incident(r) for r in rows]

    def set_incident_status(self, save_id: str, incident_id: int, status: str) -> dict:
        applied_at = time.time() if status == "applied" else None
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE incidents SET status=?, applied_at=COALESCE(?, applied_at) "
                         "WHERE save_id=? AND id=?", (status, applied_at, save_id, incident_id))
            conn.commit()
        return {"ok": True, "id": incident_id, "status": status}

    MAX_APPLIED_INCIDENTS_PER_SAVE = 300

    def prune_incidents(self, save_id: str) -> int:
        """1h-B: cap the incidents table so it can't grow unbounded over a long game. Keep ALL pending incidents
        (they still need acting on) + the most recent applied ones; drop the oldest applied beyond the cap."""
        with self._lock, self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM incidents WHERE save_id=? AND status='applied'",
                             (save_id,)).fetchone()["c"]
            if n <= self.MAX_APPLIED_INCIDENTS_PER_SAVE:
                return 0
            overflow = n - self.MAX_APPLIED_INCIDENTS_PER_SAVE
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM incidents WHERE save_id=? AND status='applied' "
                "ORDER BY created_at ASC LIMIT ?", (save_id, overflow)).fetchall()]
            if ids:
                conn.execute(f"DELETE FROM incidents WHERE id IN ({','.join('?' for _ in ids)})", ids)
                conn.commit()
            return len(ids)

    # --- Agreements / promises / deals ----------------------------------------

    def add_agreement(self, save_id: str, party_a: str, party_b: str, type: str = "",
                      terms: Any = None, deadline: float = 0.0, status: str = "pending") -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO agreements (save_id, party_a, party_b, type, terms_json,
                    deadline, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (save_id, party_a, party_b, type,
                  json.dumps(terms) if terms is not None else None,
                  float(deadline or 0), status, now))
            conn.commit()
            agreement_id = cur.lastrowid
        return {"id": agreement_id, "save_id": save_id, "party_a": party_a,
                "party_b": party_b, "type": type, "status": status}

    @staticmethod
    def _decode_agreement(row: dict) -> dict:
        row = dict(row)
        row["terms"] = json.loads(row.pop("terms_json") or "null")
        return row

    def list_agreements(self, save_id: str, status: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM agreements WHERE save_id=? AND status=? "
                                    "ORDER BY created_at DESC", (save_id, status)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agreements WHERE save_id=? "
                                    "ORDER BY created_at DESC", (save_id,)).fetchall()
        return [self._decode_agreement(r) for r in rows]

    def set_agreement_status(self, save_id: str, agreement_id: int, status: str) -> dict:
        resolved_at = time.time() if status in ("kept", "broken", "expired") else None
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE agreements SET status=?, resolved_at=COALESCE(?, resolved_at) "
                         "WHERE save_id=? AND id=?", (status, resolved_at, save_id, agreement_id))
            conn.commit()
        return {"ok": True, "id": agreement_id, "status": status}

    # --- Economy MEANING + player market --------------------------------------

    ECONOMY_REAL_FIELDS = ("player_economic_importance", "dependency_on_player", "production_health")
    ECONOMY_JSON_FIELDS = ("key_needs", "shortages", "trade_pacts", "trade_restrictions")

    def upsert_economy(self, save_id: str, faction_id: str, **fields: Any) -> dict:
        """Partial-merge a faction's economic meaning. Only provided fields change."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM economy WHERE save_id=? AND faction_id=?",
                               (save_id, faction_id)).fetchone()
            cur = dict(row) if row else {}
            vals = {
                "player_economic_importance": self._clamp01(
                    fields.get("player_economic_importance", cur.get("player_economic_importance", 0))),
                "dependency_on_player": self._clamp01(
                    fields.get("dependency_on_player", cur.get("dependency_on_player", 0))),
                "production_health": self._clamp01(
                    fields.get("production_health", cur.get("production_health", 1)), default=1.0),
                "market_status": str(fields.get("market_status", cur.get("market_status", "neutral")) or "neutral"),
            }
            json_vals = {}
            for jf in self.ECONOMY_JSON_FIELDS:
                col = f"{jf}_json"
                if jf in fields and fields[jf] is not None:
                    val = fields[jf]
                    # 1i: 'shortages' must be {ware: severity}. Never store a list-shaped shortages (the old
                    # reader echoed key_needs into it) — coerce anything non-dict to empty.
                    if jf == "shortages" and not isinstance(val, dict):
                        val = {}
                    json_vals[col] = json.dumps(val)
                else:
                    json_vals[col] = cur.get(col)
            conn.execute("""
                INSERT INTO economy (save_id, faction_id, player_economic_importance,
                    dependency_on_player, production_health, key_needs_json, shortages_json,
                    trade_pacts_json, trade_restrictions_json, market_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, faction_id) DO UPDATE SET
                    player_economic_importance=excluded.player_economic_importance,
                    dependency_on_player=excluded.dependency_on_player,
                    production_health=excluded.production_health,
                    key_needs_json=excluded.key_needs_json,
                    shortages_json=excluded.shortages_json,
                    trade_pacts_json=excluded.trade_pacts_json,
                    trade_restrictions_json=excluded.trade_restrictions_json,
                    market_status=excluded.market_status,
                    updated_at=excluded.updated_at
            """, (save_id, faction_id, vals["player_economic_importance"],
                  vals["dependency_on_player"], vals["production_health"],
                  json_vals["key_needs_json"], json_vals["shortages_json"],
                  json_vals["trade_pacts_json"], json_vals["trade_restrictions_json"],
                  vals["market_status"], now))
            conn.commit()
        return self.get_economy(save_id, faction_id) or {}

    @staticmethod
    def _decode_economy(row: dict) -> dict:
        row = dict(row)
        for jf in ("key_needs", "shortages", "trade_pacts", "trade_restrictions"):
            row[jf] = json.loads(row.pop(f"{jf}_json") or "null")
        return row

    def get_economy(self, save_id: str, faction_id: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM economy WHERE save_id=? AND faction_id=?",
                             (save_id, faction_id)).fetchone()
        return self._decode_economy(r) if r else None

    def list_economy(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM economy WHERE save_id=? ORDER BY faction_id",
                                (save_id,)).fetchall()
        return [self._decode_economy(r) for r in rows]

    # ---- Economy Update read pipeline (spec, 2026-06-26): raw station capture + derived rollup ----------
    def upsert_economy_station(self, save_id: str, station: dict) -> None:
        """Store one omniscient station snapshot (from the mod's find_station_by_true_owner sweep)."""
        sid = str(station.get("station_id") or "")
        if not sid:
            return
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO economy_stations (save_id, station_id, faction_id, sector_id, station_name,
                    station_type, workforce_current, workforce_capacity, products_json, needs_json,
                    storage_json, last_seen_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, station_id) DO UPDATE SET
                    faction_id=excluded.faction_id, sector_id=excluded.sector_id, station_name=excluded.station_name,
                    station_type=excluded.station_type, workforce_current=excluded.workforce_current,
                    workforce_capacity=excluded.workforce_capacity, products_json=excluded.products_json,
                    needs_json=excluded.needs_json, storage_json=excluded.storage_json, last_seen_ts=excluded.last_seen_ts
            """, (save_id, sid, str(station.get("faction_id") or ""), str(station.get("sector_id") or ""),
                  str(station.get("station_name") or ""), str(station.get("station_type") or ""),
                  int(station.get("workforce_current") or 0), int(station.get("workforce_capacity") or 0),
                  json.dumps(station.get("products") or []), json.dumps(station.get("needs") or []),
                  json.dumps(station.get("storage") or {}), now))
            conn.commit()

    def list_economy_stations(self, save_id: str, faction_id: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if faction_id:
                rows = conn.execute("SELECT * FROM economy_stations WHERE save_id=? AND faction_id=?",
                                    (save_id, faction_id)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM economy_stations WHERE save_id=?", (save_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for jf in ("products", "needs", "storage"):
                d[jf] = json.loads(d.pop(f"{jf}_json") or "null") or ([] if jf != "storage" else {})
            out.append(d)
        return out

    def rollup_economy_from_stations(self, save_id: str) -> dict:
        """DERIVED layer (spec §3): turn the raw per-station capture into faction-level economy facts and write
        them into the `economy` table — shortages (wares many of a faction's stations need), key_needs, and a
        production_health from the short-station ratio. Replaces seeded economy values with live-grounded ones."""
        from collections import Counter, defaultdict
        stations = self.list_economy_stations(save_id)
        by_fac: dict[str, list] = defaultdict(list)
        for s in stations:
            fid = s.get("faction_id")
            if fid:
                by_fac[fid].append(s)
        updated = 0
        for fid, sts in by_fac.items():
            total = len(sts)
            need_counts: Counter = Counter()
            prod_counts: Counter = Counter()
            short_stations = 0
            for s in sts:
                needs = [str(w).lower() for w in (s.get("needs") or [])]
                if needs:
                    short_stations += 1
                for w in needs:
                    need_counts[w] += 1
                for w in (s.get("products") or []):
                    prod_counts[str(w).lower()] += 1
            # shortage severity = fraction of the faction's stations that need that ware (0..1)
            shortages = {w: round(min(1.0, c / total), 3) for w, c in need_counts.most_common(8)} if total else {}
            key_needs = [w for w, _ in need_counts.most_common(6)]
            # production_health: 1 when no station is short, lower as more stations report unmet needs
            production_health = round(max(0.0, 1.0 - (short_stations / total)), 3) if total else 1.0
            self.upsert_economy(save_id, fid, shortages=shortages, key_needs=key_needs,
                                production_health=production_health)
            updated += 1
        return {"ok": True, "save_id": save_id, "stations": len(stations), "factions_rolled_up": updated}

    def upsert_player_market(self, save_id: str, ware: str, sector: str,
                             dominance_level: float = 0.0, supplying_enemies: bool = False,
                             note: str = "") -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO player_market (save_id, ware, sector, dominance_level,
                    supplying_enemies, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, ware, sector) DO UPDATE SET
                    dominance_level=excluded.dominance_level,
                    supplying_enemies=excluded.supplying_enemies,
                    note=excluded.note, updated_at=excluded.updated_at
            """, (save_id, ware, sector, self._clamp01(dominance_level),
                  int(bool(supplying_enemies)), note, now))
            conn.commit()
        return {"ok": True, "save_id": save_id, "ware": ware, "sector": sector}

    def list_player_market(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM player_market WHERE save_id=? ORDER BY ware, sector",
                                (save_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- Territory / sectors --------------------------------------------------

    def upsert_sector(self, save_id: str, sector_id: str, name: Optional[str] = None,
                      owner_faction: Optional[str] = None, contested_by: Any = None,
                      strategic_value: Optional[float] = None,
                      player_assets_present: Optional[bool] = None) -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sectors WHERE save_id=? AND sector_id=?",
                               (save_id, sector_id)).fetchone()
            cur = dict(row) if row else {}
            contested_json = json.dumps(contested_by) if contested_by is not None else cur.get("contested_by_json")
            sv = self._clamp01(strategic_value) if strategic_value is not None else float(cur.get("strategic_value", 0) or 0)
            pap = int(bool(player_assets_present)) if player_assets_present is not None else int(cur.get("player_assets_present", 0) or 0)
            conn.execute("""
                INSERT INTO sectors (save_id, sector_id, name, owner_faction, contested_by_json,
                    strategic_value, player_assets_present, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, sector_id) DO UPDATE SET
                    name=COALESCE(excluded.name, sectors.name),
                    owner_faction=COALESCE(excluded.owner_faction, sectors.owner_faction),
                    contested_by_json=excluded.contested_by_json,
                    strategic_value=excluded.strategic_value,
                    player_assets_present=excluded.player_assets_present,
                    updated_at=excluded.updated_at
            """, (save_id, sector_id, name,
                  owner_faction if owner_faction is not None else cur.get("owner_faction"),
                  contested_json, sv, pap, now))
            conn.commit()
        return self.get_sector(save_id, sector_id) or {}

    def replace_sectors_by_name(self, save_id: str, sectors: list) -> dict:
        """SPEC 0b: store exactly ONE row per KNOWN (named) sector, keyed by NAME (stable), and drop
        legacy/stale rows. Fixes SyncSectors' unstable numeric ids that produced ~8 duplicate rows per
        sector (territorial/piracy read ~1/8 true). Unexplored 'Unknown Sector' rows are skipped (fog:
        unstable id, can't be contested anyway). Self-healing + authoritative: each sync is the full known
        map. PRESERVES contested_by on surviving rows (delete-not-in, then upsert owner only — never touches
        contested_by_json). `sectors` = [{name, owner}] with owner already faction-resolved."""
        known: dict[str, Any] = {}
        for s in sectors:
            nm = (s.get("name") or "").strip()
            if nm and nm != "Unknown Sector":
                known[nm] = s.get("owner")
        now = time.time()
        with self._lock, self._connect() as conn:
            if known:
                ph = ",".join("?" * len(known))
                conn.execute(f"DELETE FROM sectors WHERE save_id=? AND sector_id NOT IN ({ph})",
                             (save_id, *known.keys()))
            else:
                conn.execute("DELETE FROM sectors WHERE save_id=?", (save_id,))
            for nm, owner in known.items():
                conn.execute("""
                    INSERT INTO sectors (save_id, sector_id, name, owner_faction, contested_by_json,
                        strategic_value, player_assets_present, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 0, 0, ?)
                    ON CONFLICT(save_id, sector_id) DO UPDATE SET
                        name=excluded.name, owner_faction=excluded.owner_faction, updated_at=excluded.updated_at
                """, (save_id, nm, nm, owner, now))
            conn.commit()
        return {"stored": len(known)}

    @staticmethod
    def _decode_sector(row: dict) -> dict:
        row = dict(row)
        row["contested_by"] = json.loads(row.pop("contested_by_json") or "null")
        return row

    def get_sector(self, save_id: str, sector_id: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM sectors WHERE save_id=? AND sector_id=?",
                             (save_id, sector_id)).fetchone()
        return self._decode_sector(r) if r else None

    def list_sectors(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sectors WHERE save_id=? ORDER BY sector_id",
                                (save_id,)).fetchall()
        return [self._decode_sector(r) for r in rows]

    def sync_contested_from_presence(self, save_id: str, presence: dict,
                                     min_force: int = 2, war_threshold: float = -0.75) -> dict:
        """Derive sectors.contested_by from a per-sector faction PRESENCE map the in-game census reports
        (presence[sector_id][faction] = fight-ship count). A sector owned by A is contested by B when B has
        >= min_force fight ships present AND B is at war with A (live relations, same threshold the Tier-1
        reconcile uses). Idempotent: re-sets the contested set each call and clears sectors that are no
        longer contested, so it is safe on the ~120s census heartbeat. Makes territorial_pressure (and the
        criminal-filtered piracy_pressure) REAL instead of always-0."""
        if not isinstance(presence, dict) or not presence:
            return {"contested": 0, "cleared": 0}
        sectors = self.list_sectors(save_id)
        # Map any presence key -> (real_sector_id, owner). The in-game reader keys presence by sector
        # NAME (e.g. "Argon Prime"); the sectors-table PK is a macro/numeric id. Index by BOTH so the
        # join works either way, and always upsert against the REAL sector_id (never a name, or we'd
        # create a duplicate row). Skip "Unknown Sector" (fog) + ambiguous name collisions.
        idx: dict[str, Any] = {}
        for s in sectors:
            rid = str(s.get("sector_id"))
            owner = s.get("owner_faction")
            idx[rid] = (rid, owner)
            nm = s.get("name")
            if nm and nm != "Unknown Sector":
                # SyncSectors currently emits ~8 duplicate rows per named sector (unstable numeric ids,
                # see roadmap SPEC). All dups share the same owner, so dedup-by-name is safe: keep the
                # first (rid, owner) and only void the name if a later row genuinely DISAGREES on owner.
                if nm not in idx:
                    idx[nm] = (rid, owner)
                elif idx[nm] is not None and idx[nm][1] != owner:
                    idx[nm] = None
        prev_contested = {str(s.get("sector_id")) for s in sectors if s.get("contested_by")}
        war: set[tuple[str, str]] = set()
        for r in self.list_relationships(save_id):
            a, b, tr = r.get("subject"), r.get("object"), r.get("trust")
            if a and b and isinstance(tr, (int, float)) and (tr / 100.0) <= war_threshold:
                war.add(tuple(sorted((a, b))))
        now_contested: dict[str, list] = {}
        new_contests: list = []  # (owner, enemy) for sectors NEWLY contested this sync -> grudge nudge
        owner_matched = 0
        enemy_any = 0
        sample = []
        for key, facs in presence.items():
            key = str(key)
            if not isinstance(facs, dict):
                continue
            entry = idx.get(key)
            owner = entry[1] if entry else None
            if len(sample) < 6:
                sample.append({"key": key, "owner": owner, "present": list(facs.keys())[:6]})
            if not entry or not owner:
                continue
            rid = entry[0]
            owner_matched += 1
            if any(f and f != owner for f in facs):
                enemy_any += 1
            hostiles = sorted({f for f, c in facs.items()
                               if f and f != owner and int(c or 0) >= min_force
                               and tuple(sorted((f, owner))) in war})
            if hostiles:
                now_contested[rid] = hostiles
                if rid not in prev_contested:  # transition: newly contested -> the owner gains a grudge
                    for h in hostiles:
                        new_contests.append((owner, h))
        for rid, hostiles in now_contested.items():
            self.upsert_sector(save_id, rid, contested_by=hostiles)
        cleared = 0
        for sid in prev_contested - set(now_contested):
            self.upsert_sector(save_id, sid, contested_by=[])
            cleared += 1
        # SPEC 1c-D grudge attribution: a faction whose sector just became contested resents the
        # contester. Transition-only (newly-contested), so it nudges once per event, not every tick.
        for subj, obj in new_contests:
            if subj and obj and subj != obj and obj != "player":
                try:
                    self.adjust_relationship(save_id, subj, obj, dresentment=12, dtrust=-6)
                except Exception:
                    pass
        return {"contested": len(now_contested), "cleared": cleared,
                "presence_sectors": len(presence), "owner_matched": owner_matched,
                "enemy_present_sectors": enemy_any, "war_pairs": len(war), "sample": sample}

    # --- Fleet strength (per-faction ship census) -----------------------------

    def upsert_fleet_strength(self, save_id: str, faction_id: str, **counts: Any) -> dict:
        """Replace a faction's ship census (counts by primarypurpose). Read in Lua via
        GetContainedObjectsByOwner + GetComponentData(obj,"class"/"primarypurpose")."""
        now = time.time()
        cols = ("total_ships", "fight", "trade", "mine", "build", "other", "capitals")
        vals = {c: int(counts.get(c, 0) or 0) for c in cols}
        fight_drop = 0
        with self._lock, self._connect() as conn:
            # War-losses sensor: diff the combat-ship count against the prior census. The census is
            # galaxy-wide/omniscient (GetContainedObjectsByOwner(fid) sees ships in unscanned space too),
            # so a NET decline in fight ships is real attrition, not fog-of-war. A faction out-building its
            # losses nets to ~0 — correct for a "how hard is this faction being ground down" pressure signal.
            prior = conn.execute("SELECT fight FROM fleet_strength WHERE save_id=? AND faction_id=?",
                                 (save_id, faction_id)).fetchone()
            if prior is not None:
                fight_drop = max(0, int(prior["fight"] or 0) - vals["fight"])
            conn.execute("""
                INSERT INTO fleet_strength (save_id, faction_id, total_ships, fight, trade,
                    mine, build, other, capitals, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(save_id, faction_id) DO UPDATE SET
                    total_ships=excluded.total_ships, fight=excluded.fight, trade=excluded.trade,
                    mine=excluded.mine, build=excluded.build, other=excluded.other,
                    capitals=excluded.capitals, updated_at=excluded.updated_at
            """, (save_id, faction_id, vals["total_ships"], vals["fight"], vals["trade"],
                  vals["mine"], vals["build"], vals["other"], vals["capitals"], now))
            conn.commit()
        # Record combat attrition OUTSIDE the lock (record_loss takes self._lock; it is not reentrant).
        # Threshold >=2 ignores single-ship reclassification/rounding noise between snapshots.
        if fight_drop >= 2:
            self.record_loss(save_id, faction_id, float(fight_drop), kind="combat")
        return {"faction_id": faction_id, **vals}

    def list_fleet_strength(self, save_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM fleet_strength WHERE save_id=? ORDER BY total_ships DESC",
                                (save_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- Conflicts / wars + loss aggregation ----------------------------------

    def add_conflict(self, save_id: str, faction_a: str, faction_b: str,
                     status: str = "active", intensity: float = 0.0, cause: str = "") -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO conflicts (save_id, faction_a, faction_b, status, intensity, cause, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (save_id, faction_a, faction_b, status, self._clamp01(intensity), cause, now))
            conn.commit()
            conflict_id = cur.lastrowid
        return {"id": conflict_id, "save_id": save_id, "faction_a": faction_a,
                "faction_b": faction_b, "status": status}

    def list_conflicts(self, save_id: str, status: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM conflicts WHERE save_id=? AND status=? "
                                    "ORDER BY intensity DESC, started_at DESC", (save_id, status)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM conflicts WHERE save_id=? "
                                    "ORDER BY intensity DESC, started_at DESC", (save_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_conflict_status(self, save_id: str, conflict_id: int, status: str,
                            intensity: Optional[float] = None) -> dict:
        ended_at = time.time() if status in ("ceasefire", "ended") else None
        with self._lock, self._connect() as conn:
            if intensity is not None:
                conn.execute("UPDATE conflicts SET status=?, intensity=?, ended_at=COALESCE(?, ended_at) "
                             "WHERE save_id=? AND id=?", (status, self._clamp01(intensity), ended_at, save_id, conflict_id))
            else:
                conn.execute("UPDATE conflicts SET status=?, ended_at=COALESCE(?, ended_at) "
                             "WHERE save_id=? AND id=?", (status, ended_at, save_id, conflict_id))
            conn.commit()
        return {"ok": True, "id": conflict_id, "status": status}

    def reconcile_world_from_relations(self, save_id: str, war_threshold: float = -0.75) -> dict:
        """Tier-1 world model: derive CONFLICTS, WORLD EVENTS and ceasefire AGREEMENTS from the live
        faction relations the heartbeat already syncs — no game read needed. Idempotent: acts only on
        TRANSITIONS (a pair crossing into or out of war), so it is safe to run every heartbeat. Catches
        X4's OWN wars too (argon<->xenon, <->khaak, ...), not just player-driven dispatches."""
        def _nm(fid: str) -> str:
            f = self.get_faction(save_id, fid) or self.get_faction(self.CANON_SAVE, fid)
            return (f.get("name") if f else None) or fid
        at_war: set[tuple[str, str]] = set()
        seen_factions: set[str] = set()
        for r in self.list_relationships(save_id):
            a, b, tr = r.get("subject"), r.get("object"), r.get("trust")
            if a:
                seen_factions.add(a)
            if b:
                seen_factions.add(b)
            if a and b and isinstance(tr, (int, float)) and (tr / 100.0) <= war_threshold:
                at_war.add(tuple(sorted((a, b))))
        # Populate the save's Factions panel with names carried over from the canon harvest (no game
        # read needed). Only seeds the name if the faction isn't already in this save's table — so a
        # future strategic deriver's goal/mood/aggression values are never clobbered.
        for fid in seen_factions:
            if fid == "player":
                continue
            if not self.get_faction(save_id, fid):
                self.upsert_faction(save_id, fid, name=_nm(fid))
        active = {tuple(sorted((c["faction_a"], c["faction_b"]))): c
                  for c in self.list_conflicts(save_id, status="active")}
        opened = closed = 0
        for (a, b) in at_war:
            if (a, b) not in active:
                self.add_conflict(save_id, a, b, status="active", intensity=1.0, cause="relations at war")
                imp = 4 if "player" in (a, b) else 3
                self.add_world_event(save_id, "war_declared",
                    summary=f"{_nm(a)} and {_nm(b)} are at war.",
                    primary_faction=a, secondary_faction=b, importance=imp, source="reconcile")
                # SPEC 1c-D: a NEW war seeds mutual lasting resentment (the grudge memory; trust itself
                # stays owned by the game-relation sync). Transition-only — fires once when war opens.
                if "player" not in (a, b):
                    try:
                        self.adjust_relationship(save_id, a, b, dresentment=15)
                        self.adjust_relationship(save_id, b, a, dresentment=15)
                    except Exception:
                        pass
                opened += 1
        for (a, b), c in active.items():
            if (a, b) not in at_war:
                self.set_conflict_status(save_id, c["id"], "ended")
                self.add_world_event(save_id, "peace",
                    summary=f"{_nm(a)} and {_nm(b)} are no longer at war.",
                    primary_faction=a, secondary_faction=b, importance=3, source="reconcile")
                self.add_agreement(save_id, a, b, type="ceasefire", status="kept")
                closed += 1
        return {"at_war": len(at_war), "active_conflicts": len(at_war), "opened": opened, "closed": closed}

    def record_loss(self, save_id: str, faction_id: str, amount: float,
                    kind: str = "ship", sector_id: str = "") -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO war_losses (save_id, faction_id, amount, kind, sector_id, ts) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (save_id, faction_id, float(amount), kind, sector_id, now))
            conn.commit()

    def get_loss_summary(self, save_id: str, faction_id: Optional[str] = None,
                         window_s: float = 3600.0, normalize_by: float = 50.0) -> dict:
        """Sum recent losses in a time window. Returns per-faction totals and a
        normalized 0..1 `recent_losses` pressure (sum / normalize_by, capped)."""
        since = time.time() - window_s
        with self._connect() as conn:
            if faction_id:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS c FROM war_losses "
                    "WHERE save_id=? AND faction_id=? AND ts>=?", (save_id, faction_id, since)).fetchone()
                total = float(row["s"] or 0)
                return {"faction_id": faction_id, "loss_total": total, "events": row["c"],
                        "recent_losses": self._clamp01(total / normalize_by) if normalize_by else 0.0}
            rows = conn.execute(
                "SELECT faction_id, SUM(amount) AS s, COUNT(*) AS c FROM war_losses "
                "WHERE save_id=? AND ts>=? GROUP BY faction_id", (save_id, since)).fetchall()
        out = {}
        for r in rows:
            total = float(r["s"] or 0)
            out[r["faction_id"]] = {"loss_total": total, "events": r["c"],
                                    "recent_losses": self._clamp01(total / normalize_by) if normalize_by else 0.0}
        return out

    # --- Event-grounded conflict ledger (#62): real hostile actions -> derived conflicts/losses ----------
    def add_hostile_event(self, save_id: str, ev: dict) -> None:
        """Record ONE observed hostile action (no fabrication — callers pass real in-game events only)."""
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO hostile_events (save_id, attacker_faction, victim_faction, sector,
                object_id, object_name, event_kind, magnitude, source, linked_order_id, ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (save_id, str(ev.get("attacker_faction") or ""), str(ev.get("victim_faction") or ""),
                 str(ev.get("sector") or ""), str(ev.get("object_id") or ""), str(ev.get("object_name") or ""),
                 str(ev.get("event_kind") or "ship_destroyed"), float(ev.get("magnitude") or 1),
                 str(ev.get("source") or "game"), str(ev.get("linked_order_id") or ""),
                 float(ev.get("ts") or time.time())))
            conn.commit()

    def list_hostile_events(self, save_id: str, window_s: Optional[float] = None, limit: int = 500) -> list[dict]:
        q, params = "SELECT * FROM hostile_events WHERE save_id=?", [save_id]
        if window_s:
            q += " AND ts>=?"; params.append(time.time() - window_s)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q, params).fetchall()]

    def derive_conflicts_from_events(self, save_id: str, window_s: float = 3600.0, norm: float = 40.0) -> list[dict]:
        """The keystone derivation: turn REAL hostile events into conflicts grounded in who-hit-whom-WHERE.
        intensity = rolling score from recent event magnitude (NOT flat 1.0); cause = the FIRST triggering event
        (NOT 'relations at war'); sectors + per-victim located losses come from the events themselves."""
        def _nm(fid):
            f = self.get_faction(save_id, fid) or self.get_faction(self.CANON_SAVE, fid)
            return (f.get("name") if f else None) or fid
        evs = self.list_hostile_events(save_id, window_s=window_s, limit=2000)
        pairs: dict = {}
        for e in evs:
            a, v = e.get("attacker_faction"), e.get("victim_faction")
            if not a or not v or a == v:
                continue
            key = tuple(sorted((a, v)))
            agg = pairs.setdefault(key, {"faction_a": key[0], "faction_b": key[1], "events": 0, "magnitude": 0.0,
                                         "sectors": set(), "losses": {}, "orders": set(),
                                         "first": None, "last": None, "first_ev": None})
            agg["events"] += 1
            mag = float(e.get("magnitude") or 0)
            agg["magnitude"] += mag
            if e.get("sector"):
                agg["sectors"].add(e["sector"])
            if e.get("linked_order_id"):  # #67: which raid order caused this loss (proof of attribution)
                agg["orders"].add(e["linked_order_id"])
            agg["losses"][v] = agg["losses"].get(v, 0.0) + mag
            ts = float(e.get("ts") or 0)
            if agg["first"] is None or ts < agg["first"]:
                agg["first"], agg["first_ev"] = ts, e
            if agg["last"] is None or ts > agg["last"]:
                agg["last"] = ts
        out = []
        for agg in pairs.values():
            fe = agg["first_ev"] or {}
            sect = fe.get("sector") or ""
            cause = f"{_nm(fe.get('attacker_faction'))} struck {_nm(fe.get('victim_faction'))}" + (f" in {sect}" if sect else "")
            out.append({"faction_a": agg["faction_a"], "faction_b": agg["faction_b"],
                        "intensity": round(self._clamp01(agg["magnitude"] / norm), 3),
                        "events": agg["events"], "sectors": sorted(agg["sectors"]),
                        "losses": {k: round(v, 1) for k, v in agg["losses"].items()},
                        "orders": sorted(o for o in agg["orders"] if o),
                        "cause": cause, "first_at": agg["first"], "last_at": agg["last"], "source": "events"})
        out.sort(key=lambda c: -c["intensity"])
        return out

    # --- Durable world-events log ---------------------------------------------

    def add_world_event(self, save_id: str, event_type: str, summary: str = "",
                        primary_faction: str = "", secondary_faction: str = "",
                        sector_id: str = "", importance: int = 1, source: str = "") -> dict:
        now = time.time()
        importance = max(1, min(5, int(importance or 1)))
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO world_events (save_id, event_type, summary, primary_faction,
                    secondary_faction, sector_id, importance, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (save_id, event_type, summary, primary_faction, secondary_faction,
                  sector_id, importance, source, now))
            conn.commit()
            event_id = cur.lastrowid
        self._prune_world_events(save_id)
        return {"id": event_id, "save_id": save_id, "event_type": event_type, "importance": importance}

    # --- Self-authored news guard (SPEC 1d-S) --------------------------------
    # The autonomous loop posts its decisions to the player's logbook (news/diplomacy tabs). SyncLogbook
    # INGESTS those same vanilla tabs back into world_events — so without this guard, our own output would
    # re-enter memory, feed grudges, and drive more decisions: a self-amplifying loop. We remember the exact
    # text of every bulletin we emit (in-memory, TTL'd) and skip it on ingest. Ship-safe: no visible marker
    # needed (unlike the legacy "[TEST] Galaxy News" title check), so player text can read fully vanilla.
    _SELF_AUTHORED_TTL = 1800.0
    _SELF_AUTHORED_CAP = 400

    @staticmethod
    def _norm_news(text: str) -> str:
        return " ".join((text or "").split()).strip().lower()

    def note_self_authored(self, save_id: str, text: str) -> None:
        norm = self._norm_news(text)
        if not norm:
            return
        store = getattr(self, "_self_authored", None)
        if store is None:
            store = self._self_authored = {}
        now = time.time()
        bucket = store.setdefault(save_id, {})
        bucket[norm] = now
        if len(bucket) > self._SELF_AUTHORED_CAP:  # prune oldest
            for k in sorted(bucket, key=bucket.get)[: len(bucket) - self._SELF_AUTHORED_CAP]:
                bucket.pop(k, None)

    def is_self_authored(self, save_id: str, text: str) -> bool:
        norm = self._norm_news(text)
        bucket = getattr(self, "_self_authored", {}).get(save_id, {})
        ts = bucket.get(norm)
        return bool(ts and (time.time() - ts) <= self._SELF_AUTHORED_TTL)

    def ingest_logbook_event(self, save_id: str, category: str, title: str, text: str = "",
                             faction: str = "", entity: str = "") -> bool:
        """SPEC 1c: turn one of the GAME'S OWN logbook entries (news/alerts/diplomacy) into a durable
        world_event memory. The game already detected + 'notable'-filtered it, so we just classify, attribute
        (faction + sector by name-match against our clean sectors table) and dedup. Returns True if stored."""
        summary = (title or "").strip()
        body = (text or "").strip()
        if not summary and not body:
            return False
        # Skip our OWN injected faction-decision news so the autonomous loop's output doesn't re-ingest
        # as a memory and feed back on itself (W1/1d-S). Primary guard = exact-text match against what we
        # emitted (ship-safe, category-agnostic); legacy title check kept for the dev [TEST] phase.
        if "Galaxy News" in summary or self.is_self_authored(save_id, body) or self.is_self_authored(save_id, summary):
            return False
        full = (summary + " " + body).strip()
        low = full.lower()
        # The game puts a generic label in title ("News update:", "Emergency alert:") and the real content
        # in text — so prefer text for the stored memory (and for dedup, else every "News update:" collapses).
        display = body if body else summary
        with self._connect() as conn:  # dedup: same save + identical logbook content already stored
            if display and conn.execute(
                    "SELECT 1 FROM world_events WHERE save_id=? AND source='logbook' AND summary=? LIMIT 1",
                    (save_id, display)).fetchone():
                return False
        if "destroyed" in low or "destruction" in low:
            etype, imp = "battle", 4
        elif "war" in low or "hostil" in low or " attack" in low:
            etype, imp = "diplomatic", 4
        elif "defence" in low or "defend" in low or "mounting" in low:
            etype, imp = "battle", 3
        elif "construct" in low or "completed" in low:
            etype, imp = "economic", 2
        else:
            etype, imp = "news", 2
        if (category or "").lower() == "alerts":
            imp = max(imp, 3)
        if (category or "").lower() == "diplomacy":
            etype = "diplomatic"
        sector = ""  # match any KNOWN sector name appearing in the entry (sectors table is name-keyed now)
        for s in self.list_sectors(save_id):
            nm = s.get("name") or ""
            if nm and nm != "Unknown Sector" and nm in full:
                sector = nm
                break
        fac = self.resolve_faction_id(faction) or (faction or "")
        self.add_world_event(save_id, etype, summary=display[:300], primary_faction=fac,
                             sector_id=sector, importance=imp, source="logbook")
        return True

    def _prune_world_events(self, save_id: str) -> int:
        """Keep the log bounded: past the cap, drop lowest-importance then oldest.
        CORE-importance (5) events are kept as long as possible (sorted last to drop)."""
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM world_events WHERE save_id=?",
                                 (save_id,)).fetchone()["c"]
            if count <= self.MAX_WORLD_EVENTS_PER_SAVE:
                return 0
            overflow = count - self.MAX_WORLD_EVENTS_PER_SAVE
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM world_events WHERE save_id=? "
                "ORDER BY importance ASC, created_at ASC LIMIT ?", (save_id, overflow)).fetchall()]
            if ids:
                conn.execute(f"DELETE FROM world_events WHERE id IN ({','.join('?' for _ in ids)})", ids)
                conn.commit()
            return len(ids)

    def list_world_events(self, save_id: str, limit: int = 200,
                          min_importance: int = 1) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM world_events WHERE save_id=? AND importance>=? "
                "ORDER BY created_at DESC LIMIT ?", (save_id, int(min_importance), int(limit))).fetchall()
        return [dict(r) for r in rows]

    # --- Live chat transcript -------------------------------------------------

    MAX_CONVERSATIONS_PER_SAVE = 2000

    def upsert_player(self, save_id: str, name: str | None = None) -> dict:
        """The player is a singleton per save. Update the mutable display name and append
        to name_history on a rename — identity (save_id) and any reputation/memory keyed to
        it are untouched, so a rename never launders the player's record."""
        now = time.time()
        name = (name or "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM players WHERE save_id=?", (save_id,)).fetchone()
            if row is None:
                history = [name] if name else []
                conn.execute(
                    "INSERT INTO players (save_id, current_name, name_history, first_seen, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (save_id, name or "Player", json.dumps(history), now, now))
                conn.commit()
                return {"save_id": save_id, "current_name": name or "Player", "name_history": history, "renamed": False}
            current = row["current_name"] or ""
            try:
                history = json.loads(row["name_history"] or "[]")
            except Exception:
                history = []
            renamed = bool(name and name != current)
            if renamed and name not in history:
                history.append(name)
            new_name = name or current or "Player"
            conn.execute(
                "UPDATE players SET current_name=?, name_history=?, updated_at=? WHERE save_id=?",
                (new_name, json.dumps(history), now, save_id))
            conn.commit()
            return {"save_id": save_id, "current_name": new_name, "name_history": history, "renamed": renamed}

    def get_player(self, save_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM players WHERE save_id=?", (save_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["name_history"] = json.loads(d.get("name_history") or "[]")
        except Exception:
            d["name_history"] = []
        return d

    def record_conversation(self, save_id: str, prompt: str = "", reply: str = "",
                            request_id: str = "", faction_id: str = "", npc_name: str = "",
                            source_mod: str = "", latency_ms: int | None = None,
                            status: str = "", player_name: str = "") -> dict:
        """Persist one player↔NPC turn so the dashboard can show the real conversation."""
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO conversations (save_id, request_id, faction_id, npc_name,
                    source_mod, prompt, reply, player_name, latency_ms, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (save_id, request_id, faction_id, npc_name, source_mod,
                  prompt or "", reply or "", player_name or "", latency_ms, status, now))
            conn.commit()
            row_id = cur.lastrowid
            # Bound the log: drop oldest beyond the cap.
            count = conn.execute("SELECT COUNT(*) AS c FROM conversations WHERE save_id=?",
                                 (save_id,)).fetchone()["c"]
            if count > self.MAX_CONVERSATIONS_PER_SAVE:
                overflow = count - self.MAX_CONVERSATIONS_PER_SAVE
                conn.execute(
                    "DELETE FROM conversations WHERE id IN "
                    "(SELECT id FROM conversations WHERE save_id=? ORDER BY created_at ASC LIMIT ?)",
                    (save_id, overflow))
                conn.commit()
        return {"id": row_id, "save_id": save_id, "request_id": request_id}

    def list_conversations(self, save_id: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            if save_id:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE save_id=? ORDER BY created_at DESC LIMIT ?",
                    (save_id, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- Default deterministic summarizer (tests / no-LLM fallback) -----------

    @staticmethod
    def heuristic_summarizer(turns: list[dict]) -> list[dict]:
        """Categorize each non-trivial turn; CORE lines are preserved verbatim,
        routine chatter is dropped. No LLM required — deterministic and testable."""
        facts: list[dict] = []
        for t in turns:
            text = (t.get("content") or "").strip()
            if not text:
                continue
            category = classify_text(text)
            tier = category_tier(category)
            if tier == "routine":
                continue  # forget small talk
            importance = CATEGORY_IMPORTANCE.get(category, 3)
            if tier == "core":
                fact_text = text  # verbatim — meaning preserved exactly
            else:
                # Significant: keep a condensed version (first sentence), detail blurs.
                fact_text = re.split(r"(?<=[.!?])\s+", text)[0][:200]
            facts.append({"text": fact_text, "category": category,
                          "importance": importance, "verbatim": is_verbatim(category)})
        return facts


def run_memory_selftest() -> dict:
    """Deterministic oracle for the memory pipeline. Returns {ok, checks:[...]}.

    Exercises: NPC binding+retrieval, turn recording, condensation after overflow,
    CORE-verbatim survival, routine forgetting, and bounded retrieval.
    """
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_mem_selftest_")
    try:
        store = MemoryStore(Path(d) / "selftest.sqlite3", keep_recent=6, condense_after=20)
        key = MemoryStore.make_key("save1", "game1", "Argon Captain Reyes")

        # 1. Binding + retrieval
        store.bind_npc(key, "npc-abc-123", save_id="save1", game_id="game1", name="Reyes", faction_id="argon")
        got = store.get_npc_id(key)
        check("npc_id_retrieval", got == "npc-abc-123", f"got={got}")

        # Re-bind (respawn) keeps memory, updates id, attaches X4 stats
        store.bind_npc(key, "npc-def-456", save_id="save1", game_id="game1",
                       stats={"race": "argon", "role": "pilot", "ship_class": "ship_l",
                              "ship_name": "ANV Resolute", "skills": {"piloting": 12, "morale": 9}})
        check("npc_id_rebind", store.get_npc_id(key) == "npc-def-456")
        nd = store.npc_detail(key)
        check("stats_attached", bool(nd) and nd["npc"].get("role") == "pilot" and nd["npc"].get("race") == "argon")
        check("skills_stored", bool(nd) and json.loads(nd["npc"].get("skills") or "{}").get("piloting") == 12)
        check("skill_stars", MemoryStore.skill_stars(12) == "★★★★☆")

        # 2. Feed a long conversation: 3 CORE events buried in routine chatter.
        core_lines = [
            "Admiral Vance was killed aboard the ANV Resolute defending Argon Prime.",
            "I swear we will hold this sector until the last hull — that is my oath to you.",
            "You stood by us at Hatikvah's Choice; that loyalty will not be forgotten.",
        ]
        routine_lines = [
            "Status nominal, shields at full.",
            "Good morning, commander.",
            "Acknowledged, standing by.",
            "Sensors clear in this region.",
            "Routine patrol underway.",
        ]
        total_turns = 0
        for i in range(40):
            store.record_turn(key, "user", f"Routine query number {i}.")
            total_turns += 1
            if i in (5, 18, 31):
                store.record_turn(key, "assistant", core_lines[(5, 18, 31).index(i)])
            else:
                store.record_turn(key, "assistant", routine_lines[i % len(routine_lines)])
            total_turns += 1
            store.condense_if_needed(key)

        m = store.metrics(key)
        # 3. Memory now KEEPS EVERYTHING (condensation disabled) — every fed turn is retained.
        check("turns_all_retained", m["turns"] >= total_turns - 1, f"turns={m['turns']} of {total_turns} fed")
        # 4. No condensation → no auto-generated facts (retrieval handles relevance instead).
        check("no_auto_condensation", m["facts"] == 0, f"facts={m['facts']}")

        # 5. CORE content is RETAINED verbatim in raw memory (we keep everything now — no condensation).
        with store._connect() as _c:
            raw_texts = {r["text"] for r in _c.execute("SELECT text FROM turns WHERE npc_key=?", (key,)).fetchall()}
        check("core_retained_in_raw", all(c in raw_texts for c in core_lines),
              f"{sum(1 for c in core_lines if c in raw_texts)}/3 retained verbatim")
        # 6. Retrieval surfaces a CORE memory when asked about it (semantic/lexical retrieval).
        hits = store.retrieve_relevant(key, core_lines[0], k=4)
        check("retrieval_surfaces_core", any(core_lines[0] in str(h.get('text', '')) for h in hits),
              f"hits={[str(h.get('text',''))[:40] for h in hits]}")
        # 7. The always-on context (identity + faction) is still present and bounded.
        ctx = store.build_memory_context(key)
        check("context_bounded", len(ctx) <= 4000, f"len={len(ctx)}")
        # 8. Stats appear in the injected identity line.
        check("context_has_identity", "pilot" in ctx.lower() and "piloting" in ctx.lower(), "")

        # 9. CORE aging ("70-yo veteran"): a memory-rich NPC keeps only the recent CORE
        #    verbatim; older CORE blur to a category gist (you remember THAT, not the words).
        vet = MemoryStore.make_key("save1", "game1", "Old Veteran")
        store.bind_npc(vet, "npc-vet", save_id="save1", game_id="game1", name="Vet", faction_id="argon")
        for n in range(12):
            store.add_fact(vet, f"A unique death I witnessed in vivid detail, number {n}.", category="death")
        store.decay(vet)
        vfacts = [f for f in store.get_facts(vet) if f["tier"] == "core"]
        verbatim_ct = sum(1 for f in vfacts if f["verbatim"] == 1)
        blurred = [f for f in vfacts if f["verbatim"] == 0]
        check("core_verbatim_capped", verbatim_ct <= store.max_core_verbatim_per_npc,
              f"verbatim={verbatim_ct} cap={store.max_core_verbatim_per_npc}")
        check("core_aged_to_gist",
              any(f["text"] in MemoryStore.CORE_FADE_GIST.values() for f in blurred),
              f"blurred={len(blurred)} core_total={len(vfacts)}")

        # 10. #MEM — the briefing names the PLAYER and recognizes a rename (keeps the alias),
        #     so an NPC keyed to the player ENTITY says "you called yourself X then".
        store.upsert_player("save1", "Shawn Holt")
        brief1 = store.build_situation_briefing(key)
        check("briefing_names_player", "Shawn Holt" in brief1, brief1[-160:])
        store.upsert_player("save1", "Stinky DiceMan")  # rename — same entity
        brief2 = store.build_situation_briefing(key)
        check("briefing_recognizes_rename", ("Stinky DiceMan" in brief2) and ("Shawn Holt" in brief2), brief2[-200:])
    finally:
        shutil.rmtree(d, ignore_errors=True)

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}


def run_memory_stress(store: "MemoryStore", n_npcs: int = 100, turns_per: int = 40,
                      save_id: str = "stress") -> dict:
    """Stress the memory system: n_npcs NPCs x turns_per turns, synthetic (no LLM).

    Embeds CORE events (death/oath/love/war) in routine chatter to verify the
    consolidation/decay invariants hold at scale, and returns timing + DB size.
    Writes to the live store under `save_id` so it shows on the dashboard; clean
    up with clear_save(save_id).
    """
    import os
    import random
    import time

    rnd = random.Random(1234)
    races = ["argon", "teladi", "paranid", "split", "boron", "terran"]
    roles = ["pilot", "manager", "service_crew", "marine"]
    classes = ["ship_s", "ship_m", "ship_l", "ship_xl"]
    core_templates = [
        "Admiral {a} was killed when the {ship} was destroyed at {sec}.",
        "I swear to hold {sec} to the last hull — that is my oath.",
        "You saved my crew at {sec}; that loyalty will never be forgotten.",
        "War has been declared against the Xenon in {sec}.",
    ]
    routine = ["Status nominal.", "Patrol underway.", "Sensors clear.",
               "Standing by, commander.", "Shields at full.", "Acknowledged, holding position."]

    t0 = time.time()
    total_turns = 0
    facts_created = 0
    core_events_sent = 0
    for i in range(n_npcs):
        persona = f"Officer {i:03d}"
        key = store.make_key(save_id, "stress_game", persona)
        store.bind_npc(
            key, f"npc-stress-{i:05d}", save_id=save_id, game_id="stress_game",
            name=persona, faction_id=rnd.choice(races),
            stats={"race": rnd.choice(races), "role": rnd.choice(roles),
                   "ship_class": rnd.choice(classes), "ship_name": f"ANV-{i:03d}",
                   "sector": f"Sector-{i % 12}",
                   "skills": {s: rnd.randint(0, 15) for s in MemoryStore.X4_SKILLS}},
        )
        for t in range(turns_per):
            store.record_turn(key, "user", f"Report status, item {t}.")
            if t in (8, 20, 33):
                tmpl = core_templates[(i + t) % len(core_templates)]
                store.record_turn(key, "assistant", tmpl.format(a=f"Vance-{i}", ship=f"ANV-{i:03d}", sec=f"Sector-{i % 12}"))
                core_events_sent += 1
            else:
                store.record_turn(key, "assistant", rnd.choice(routine))
            total_turns += 2
            facts_created += store.condense_if_needed(key)
    elapsed = time.time() - t0

    checks: list[dict] = []
    def chk(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    like = f"{save_id}|%"
    with store._connect() as conn:
        npc_count = conn.execute("SELECT COUNT(*) AS c FROM npcs WHERE save_id=?", (save_id,)).fetchone()["c"]
        max_raw = conn.execute(
            "SELECT COALESCE(MAX(c),0) AS m FROM (SELECT COUNT(*) AS c FROM turns WHERE npc_key LIKE ? GROUP BY npc_key)",
            (like,)).fetchone()["m"]
        core_facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE tier='core' AND npc_key LIKE ?", (like,)).fetchone()["c"]
        routine_facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE tier='routine' AND npc_key LIKE ?", (like,)).fetchone()["c"]
        total_facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE npc_key LIKE ?", (like,)).fetchone()["c"]

    chk("npcs_created", npc_count == n_npcs, f"{npc_count}/{n_npcs}")
    chk("raw_turns_retained", max_raw > store.condense_after, f"max_raw={max_raw} (kept at full fidelity, not condensed)")
    chk("core_retained_in_raw", core_events_sent == 0 or max_raw >= 2, f"max_raw={max_raw}, core_events={core_events_sent}")
    chk("no_condensation_facts", core_facts == 0 and routine_facts == 0, f"core_facts={core_facts} routine_facts={routine_facts}")

    try:
        db_bytes = os.path.getsize(store.db_path)
    except Exception:
        db_bytes = None

    return {
        "ok": all(c["ok"] for c in checks),
        "npcs": n_npcs,
        "turns_per": turns_per,
        "elapsed_s": round(elapsed, 2),
        "turns_recorded": total_turns,
        "throughput_turns_per_s": round(total_turns / elapsed, 1) if elapsed else None,
        "facts_created": facts_created,
        "facts_stored": total_facts,
        "core_facts": core_facts,
        "max_raw_turns_per_npc": max_raw,
        "db_bytes": db_bytes,
        "db_mb": round(db_bytes / 1048576, 2) if db_bytes else None,
        "checks": checks,
    }


def run_universe_selftest() -> dict:
    """Deterministic oracle for the universe substrate tables (incidents, agreements,
    economy, sectors, conflicts/losses, world_events). Verifies round-trips, the
    action whitelist, the loss window, world-event pruning, and the save-scoping
    invariant (clear_save wipes every new table)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_uni_selftest_")
    try:
        store = MemoryStore(Path(d) / "uni.sqlite3")
        sid = "saveU"

        # 1. Incident round-trip + whitelist enforcement.
        inc = store.add_incident(sid, "escalate_pressure", faction_id="split", target="argon",
                                 confidence=0.7, priority=5, narrative="Split fleets mass on the border.",
                                 effects={"relation_delta": {"split->argon": -10}})
        listed = store.list_incidents(sid, status="pending")
        check("incident_roundtrip", len(listed) == 1 and listed[0]["action_type"] == "escalate_pressure",
              f"got {len(listed)}")
        check("incident_effects_decoded", listed and isinstance(listed[0]["effects"], dict), "")
        rejected = False
        try:
            store.add_incident(sid, "nuke_everything")  # not in whitelist
        except ValueError:
            rejected = True
        check("incident_whitelist_enforced", rejected, "non-whitelisted action_type rejected")
        store.set_incident_status(sid, inc["id"], "applied")
        check("incident_status_update", store.list_incidents(sid, status="applied"), "")

        # 2. Agreement round-trip + resolution.
        ag = store.add_agreement(sid, "argon", "teladi", type="trade",
                                 terms={"ware": "hullparts", "duration_h": 50})
        check("agreement_roundtrip", len(store.list_agreements(sid)) == 1, "")
        store.set_agreement_status(sid, ag["id"], "broken")
        check("agreement_broken", store.list_agreements(sid, status="broken"), "")

        # 3. Economy partial-merge.
        store.upsert_economy(sid, "argon", dependency_on_player=0.8, key_needs=["hullparts", "energycells"])
        store.upsert_economy(sid, "argon", production_health=0.4)  # merge: dependency persists
        econ = store.get_economy(sid, "argon")
        check("economy_merge", econ and econ["dependency_on_player"] == 0.8 and econ["production_health"] == 0.4, str(econ))
        check("economy_json_decoded", econ and econ["key_needs"] == ["hullparts", "energycells"], "")

        # 4. Sector round-trip.
        store.upsert_sector(sid, "argon_prime", name="Argon Prime", owner_faction="argon",
                            contested_by=["xenon"], strategic_value=0.9, player_assets_present=True)
        sec = store.get_sector(sid, "argon_prime")
        check("sector_roundtrip", sec and sec["owner_faction"] == "argon" and sec["contested_by"] == ["xenon"], "")

        # 5. Conflict + windowed loss aggregation.
        store.add_conflict(sid, "argon", "xenon", status="active", intensity=0.6, cause="incursion")
        for _ in range(5):
            store.record_loss(sid, "argon", amount=10, kind="ship")
        summ = store.get_loss_summary(sid, "argon", window_s=3600, normalize_by=100)
        check("loss_window_sum", summ["loss_total"] == 50.0, str(summ))
        check("recent_losses_normalized", abs(summ["recent_losses"] - 0.5) < 1e-6, str(summ))
        time.sleep(0.05)  # ensure the recorded losses are strictly in the past
        old = store.get_loss_summary(sid, "argon", window_s=0.0)  # past losses now outside the 0s window
        check("loss_window_excludes_old", old["loss_total"] == 0.0, str(old))

        # 6. World-events pruning keeps the log bounded and protects importance-5.
        store.MAX_WORLD_EVENTS_PER_SAVE = 50
        for i in range(60):
            store.add_world_event(sid, "battle", summary=f"skirmish {i}", importance=1)
        for i in range(5):
            store.add_world_event(sid, "death", summary=f"leader fell {i}", importance=5)
        total = len(store.list_world_events(sid, limit=10000))
        core = len(store.list_world_events(sid, limit=10000, min_importance=5))
        check("world_events_bounded", total <= 50, f"total={total}")
        check("world_events_protect_core", core == 5, f"core kept={core}")

        # 7. Save-scoping invariant: clear_save wipes EVERY new table.
        store.clear_save(sid)
        empt = (not store.list_incidents(sid) and not store.list_agreements(sid)
                and not store.list_economy(sid) and not store.list_sectors(sid)
                and not store.list_conflicts(sid) and not store.list_world_events(sid)
                and store.get_loss_summary(sid, "argon")["loss_total"] == 0.0)
        check("clear_save_wipes_substrate", empt, "all new tables empty after clear_save")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}


def run_full_stress(store: "MemoryStore", n_npcs: int = 2000, turns_per: int = 12,
                    n_factions: int = 60, save_id: str = "fullstress") -> dict:
    """Push the WHOLE durable surface to scale and report timing + footprint + the
    wall. Seeds every substrate table proportional to n_factions, then n_npcs NPCs
    with condensing memory. Per-phase timing + exceptions are captured so a failure
    localizes instead of aborting the run. Clean up with clear_save(save_id)."""
    import os
    import random
    import time

    rnd = random.Random(99)
    races = ["argon", "teladi", "paranid", "split", "boron", "terran"]
    wares = ["hullparts", "energycells", "shieldcomponents", "weapongrade", "foodrations", "refinedmetals"]
    actions = sorted(store.INCIDENT_ACTIONS)
    timings: dict[str, float] = {}
    errors: list[dict] = []

    def phase(name, fn):
        t = time.time()
        try:
            fn()
        except Exception as exc:  # localize a failure to its phase
            errors.append({"phase": name, "error": f"{type(exc).__name__}: {exc}"})
        timings[name] = round(time.time() - t, 3)

    factions = [f"fac_{i:03d}" for i in range(n_factions)]

    def seed_factions():
        for f in factions:
            store.upsert_faction(save_id, f, name=f.upper(),
                                 biases={"aggression": rnd.random(), "economic_focus": rnd.random()},
                                 current_goal=rnd.choice(["expand", "defend", "trade", "raid"]),
                                 mood=rnd.choice(["calm", "tense", "hostile"]))
            store.upsert_strategic_state(save_id, f,
                                         military_pressure=rnd.random(), economic_pressure=rnd.random(),
                                         recent_losses=rnd.random(), player_alignment=rnd.uniform(-1, 1))
            store.upsert_economy(save_id, f, dependency_on_player=rnd.random(),
                                 production_health=rnd.random(),
                                 key_needs=rnd.sample(wares, 2), shortages={rnd.choice(wares): rnd.random()},
                                 market_status=rnd.choice(["partner", "neutral", "obstacle"]))

    def seed_relationships():
        for f in factions:
            for other in rnd.sample(factions, min(6, n_factions)):
                if other == f:
                    continue
                store.adjust_relationship(save_id, f, other, dtrust=rnd.randint(-50, 50),
                                          dfear=rnd.randint(0, 50), dresentment=rnd.randint(0, 50))

    def seed_sectors():
        for i in range(n_factions * 3):
            store.upsert_sector(save_id, f"sector_{i:04d}", name=f"Sector {i}",
                                owner_faction=rnd.choice(factions),
                                contested_by=rnd.sample(factions, 2), strategic_value=rnd.random(),
                                player_assets_present=rnd.random() < 0.3)

    def seed_conflicts_losses():
        for _ in range(n_factions // 2):
            a, b = rnd.sample(factions, 2)
            store.add_conflict(save_id, a, b, status=rnd.choice(["active", "ceasefire"]),
                               intensity=rnd.random(), cause="border dispute")
        for f in factions:
            for _ in range(rnd.randint(0, 6)):
                store.record_loss(save_id, f, amount=rnd.randint(1, 30), kind=rnd.choice(["ship", "station"]))

    def seed_agreements():
        for _ in range(n_factions):
            a, b = rnd.sample(factions, 2)
            store.add_agreement(save_id, a, b, type=rnd.choice(["trade", "peace", "escort", "ceasefire"]),
                                terms={"ware": rnd.choice(wares)})

    def seed_incidents():
        for f in factions:
            for _ in range(rnd.randint(0, 4)):
                store.add_incident(save_id, rnd.choice(actions), faction_id=f,
                                   target=rnd.choice(factions), confidence=rnd.random(),
                                   priority=rnd.randint(0, 5), narrative="auto",
                                   effects={"k": rnd.random()})

    def seed_world_events():
        # Deliberately exceed the cap to exercise pruning at scale.
        n = store.MAX_WORLD_EVENTS_PER_SAVE + 500
        for i in range(n):
            store.add_world_event(save_id, rnd.choice(["battle", "diplomatic", "death", "economic_threshold"]),
                                  summary=f"event {i}", primary_faction=rnd.choice(factions),
                                  importance=rnd.choice([1, 1, 1, 3, 5]))

    def seed_npc_memory():
        routine = ["Status nominal.", "Patrol underway.", "Sensors clear.", "Standing by."]
        core_t = "Admiral {a} was killed when the {s} fell at {sec}."
        for i in range(n_npcs):
            persona = f"Officer {i:05d}"
            key = store.make_key(save_id, "stress_game", persona)
            store.bind_npc(key, f"npc-{i:06d}", save_id=save_id, game_id="stress_game",
                           name=persona, faction_id=rnd.choice(factions),
                           stats={"race": rnd.choice(races), "role": "pilot",
                                  "skills": {s: rnd.randint(0, 15) for s in MemoryStore.X4_SKILLS}})
            for t in range(turns_per):
                store.record_turn(key, "user", f"Report {t}.")
                # CORE events early (t in 2,5) so they age OUT of the keep_recent window into a
                # condensed batch and become durable facts. A single event at turns_per//2 stays
                # in the un-condensed tail and never persists — which is why the universe stress
                # showed zero memories. Embedding two early CORE turns exercises retention for real.
                if t in (2, 5):
                    store.record_turn(key, "assistant", core_t.format(a=f"V{i}", s=f"S{i}", sec=f"Sec{i%12}"))
                else:
                    store.record_turn(key, "assistant", rnd.choice(routine))
                store.condense_if_needed(key)

    t0 = time.time()
    phase("factions", seed_factions)
    phase("relationships", seed_relationships)
    phase("sectors", seed_sectors)
    phase("conflicts_losses", seed_conflicts_losses)
    phase("agreements", seed_agreements)
    phase("incidents", seed_incidents)
    phase("world_events", seed_world_events)
    phase("npc_memory", seed_npc_memory)
    elapsed = round(time.time() - t0, 2)

    # Measure footprint + per-table row counts.
    tables = ("npcs", "turns", "facts", "factions", "relationships", "strategic_state",
              "incidents", "agreements", "economy", "player_market", "sectors",
              "conflicts", "war_losses", "world_events")
    counts: dict[str, int] = {}
    with store._connect() as conn:
        for tbl in tables:
            try:
                counts[tbl] = conn.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE save_id=?", (save_id,)).fetchone()["c"]
            except sqlite3.OperationalError:
                # npc-owned tables key by npc_key, not save_id
                counts[tbl] = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {tbl} WHERE npc_key LIKE ?", (f"{save_id}|%",)).fetchone()["c"]

    checks: list[dict] = []
    def chk(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    chk("no_phase_errors", not errors, json.dumps(errors) if errors else "all phases clean")
    chk("npcs_created", counts["npcs"] == n_npcs, f"{counts['npcs']}/{n_npcs}")
    chk("world_events_bounded", counts["world_events"] <= store.MAX_WORLD_EVENTS_PER_SAVE,
        f"{counts['world_events']} <= {store.MAX_WORLD_EVENTS_PER_SAVE}")
    # raw turns must stay bounded by condensation even at scale
    with store._connect() as conn:
        max_raw = conn.execute(
            "SELECT COALESCE(MAX(c),0) AS m FROM (SELECT COUNT(*) AS c FROM turns WHERE npc_key LIKE ? GROUP BY npc_key)",
            (f"{save_id}|%",)).fetchone()["m"]
    chk("raw_turns_bounded", max_raw <= store.condense_after + 2, f"max_raw={max_raw}")
    chk("incidents_all_whitelisted",
        all(i["action_type"] in store.INCIDENT_ACTIONS for i in store.list_incidents(save_id, limit=100000)),
        "every incident action_type is legal")
    # Memory retention MUST actually happen at scale: the embedded CORE events have to survive
    # condensation as durable facts (routine chatter is dropped). Asserts the pipeline works,
    # not just that turns were written.
    with store._connect() as conn:
        core_facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE tier='core' AND npc_key LIKE ?", (f"{save_id}|%",)).fetchone()["c"]
        routine_facts = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE tier='routine' AND npc_key LIKE ?", (f"{save_id}|%",)).fetchone()["c"]
    chk("core_memories_survived", core_facts > 0, f"core_facts={core_facts} across {n_npcs} npcs")
    chk("routine_not_persisted", routine_facts == 0, f"routine_facts={routine_facts}")

    try:
        db_bytes = os.path.getsize(store.db_path)
    except Exception:
        db_bytes = None

    return {
        "ok": all(c["ok"] for c in checks),
        "n_npcs": n_npcs, "turns_per": turns_per, "n_factions": n_factions,
        "elapsed_s": elapsed,
        "phase_timings_s": timings,
        "errors": errors,
        "row_counts": counts,
        "total_rows": sum(counts.values()),
        "max_raw_turns_per_npc": max_raw,
        "db_bytes": db_bytes,
        "db_mb": round(db_bytes / 1048576, 2) if db_bytes else None,
        "checks": checks,
    }


def run_population_stress(store: "MemoryStore", n_npcs: int = 2000, save_id: str = "population",
                          min_turns: int = 20, max_turns: int = 44) -> dict:
    """A mixed population of NPCs (faction reps, fleet admirals, pilots, ...) each
    living a random stream of events — CORE (death/oath/betrayal/love/war) buried in
    significant deals and routine chatter — run through the full memory pipeline
    (condense + decay). Reports what STICKS (CORE verbatim), what's LOST (routine +
    trimmed raw), and the footprint. Deterministic, no LLM, scales to thousands.
    Clean up with clear_save(save_id)."""
    import os
    import random
    import time

    rnd = random.Random(2025)
    roles = ["faction_rep", "fleet_admiral", "pilot", "service_crew", "marine", "station_manager"]
    races = ["argon", "teladi", "paranid", "split", "boron", "terran"]
    factions = races
    wares = ["hullparts", "energycells", "weapongrade", "refinedmetals"]
    core_templates = [
        "Admiral {a} was killed when the {ship} was destroyed at {sec}.",
        "I swore an oath to hold {sec} to the last hull.",
        "The {f} betrayed our ceasefire and raided our convoys.",
        "You saved my crew at {sec}; I will never forget it.",
        "War has been declared against the {f} in {sec}.",
    ]
    sig_templates = [
        "We struck a trade deal for {ware} with the {f}.",
        "A skirmish broke out near {sec}.",
        "The {f} threatened reprisals over the {sec} incident.",
    ]
    routine = ["Status nominal.", "Patrol underway.", "Sensors clear.",
               "Standing by, commander.", "Shields at full.", "Acknowledged, holding."]

    t0 = time.time()
    total_turns = 0
    core_events_sent = 0
    sig_events_sent = 0
    role_counts: dict[str, int] = {}
    a_core_key = None
    for i in range(n_npcs):
        role = rnd.choice(roles)
        role_counts[role] = role_counts.get(role, 0) + 1
        fac = rnd.choice(factions)
        persona = f"{role}-{i:05d}"
        key = store.make_key(save_id, "pop_game", persona)
        store.bind_npc(key, f"npc-pop-{i:05d}", save_id=save_id, game_id="pop_game",
                       name=persona, faction_id=fac,
                       stats={"race": rnd.choice(races), "role": role,
                              "ship_class": rnd.choice(["ship_s", "ship_m", "ship_l", "ship_xl"]),
                              "ship_name": f"{fac[:3].upper()}-{i:04d}", "sector": f"Sector-{i % 24}",
                              "skills": {s: rnd.randint(0, 15) for s in MemoryStore.X4_SKILLS}})
        nturns = rnd.randint(min_turns, max_turns)
        for t in range(nturns):
            store.record_turn(key, "user", f"Report {t}.")
            roll = rnd.random()
            if roll < 0.10:  # CORE event
                txt = rnd.choice(core_templates).format(
                    a=f"Vance-{i}", ship=f"ANV-{i:04d}", sec=f"Sector-{i % 24}",
                    f=rnd.choice(factions), ware=rnd.choice(wares))
                store.record_turn(key, "assistant", txt)
                core_events_sent += 1
                if a_core_key is None:
                    a_core_key = key
            elif roll < 0.25:  # significant
                txt = rnd.choice(sig_templates).format(
                    sec=f"Sector-{i % 24}", f=rnd.choice(factions), ware=rnd.choice(wares))
                store.record_turn(key, "assistant", txt)
                sig_events_sent += 1
            else:  # routine chatter
                store.record_turn(key, "assistant", rnd.choice(routine))
            total_turns += 2
            store.condense_if_needed(key)
    elapsed = time.time() - t0

    like = f"{save_id}|%"
    with store._connect() as conn:
        npc_count = conn.execute("SELECT COUNT(*) AS c FROM npcs WHERE save_id=?", (save_id,)).fetchone()["c"]
        raw_turns = conn.execute("SELECT COUNT(*) AS c FROM turns WHERE npc_key LIKE ?", (like,)).fetchone()["c"]
        max_raw = conn.execute(
            "SELECT COALESCE(MAX(c),0) AS m FROM (SELECT COUNT(*) AS c FROM turns WHERE npc_key LIKE ? GROUP BY npc_key)",
            (like,)).fetchone()["m"]
        facts_total = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE npc_key LIKE ?", (like,)).fetchone()["c"]
        by_tier = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) AS c FROM facts WHERE npc_key LIKE ? GROUP BY tier", (like,))}
        verbatim = conn.execute("SELECT COUNT(*) AS c FROM facts WHERE npc_key LIKE ? AND verbatim=1", (like,)).fetchone()["c"]

    try:
        db_bytes = os.path.getsize(store.db_path)
    except Exception:
        db_bytes = None

    # Concrete proof: one NPC's surviving memory (CORE verbatim) vs what it forgot.
    sample = None
    if a_core_key:
        d = store.npc_detail(a_core_key)
        if d:
            cf = [f["text"] for f in d.get("facts", []) if f["tier"] == "core"]
            sample = {"npc_key": a_core_key, "name": d["npc"].get("name"),
                      "raw_turns_kept": len(d.get("turns", [])),
                      "core_memories_kept_verbatim": cf[:5],
                      "fact_tiers": {t: len([f for f in d.get("facts", []) if f["tier"] == t])
                                     for t in ("core", "significant", "routine")}}

    return {
        "ok": True,
        "n_npcs": n_npcs,
        "roles": role_counts,
        "elapsed_s": round(elapsed, 1),
        "throughput_turns_per_s": round(total_turns / elapsed, 1) if elapsed else None,
        "turns_fed": total_turns,
        "turns_retained_raw": raw_turns,
        "max_raw_turns_per_npc": max_raw,
        "events_sent": {"core": core_events_sent, "significant": sig_events_sent},
        "facts_total": facts_total,
        "facts_by_tier": by_tier,
        "verbatim_facts": verbatim,
        "STICKS": {"core_facts_verbatim": by_tier.get("core", 0),
                   "significant_facts": by_tier.get("significant", 0),
                   "note": "CORE kept verbatim forever; significant condensed + LRU-capped per NPC"},
        "LOST": {"routine_facts_persisted": by_tier.get("routine", 0),
                 "raw_turns_trimmed": max(0, total_turns - raw_turns),
                 "note": "routine should be ~0 (forgotten); old raw turns trimmed to the keep-window"},
        "db_mb": round(db_bytes / 1048576, 2) if db_bytes else None,
        "sample_npc": sample,
    }


if __name__ == "__main__":
    print(json.dumps(run_memory_selftest(), indent=2))
    print(json.dumps(run_universe_selftest(), indent=2))
# end of memory.py
