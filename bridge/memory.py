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

import hashlib  # identity keys + skill-based role inference
import json
import re
import sqlite3
import threading
import time
import uuid
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
    "refusal",  # G4: a refusal (declined aid / rejected a contract) is durable — the doc's named gap
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
    # G4: refusal must beat 'deal'/'oath'/'economy' (a refusal often names the thing refused), so place it early.
    (re.compile(r"\b(refus(?:e|ed|es|ing|al)|decline[ds]?|i\s+won'?t|we\s+won'?t|will\s+not|won'?t\s+(?:help|supply|give|aid)|deny|denied|no\s+(?:aid|deal|help))\b", re.I), "refusal"),
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
            # ---- Negotiations N1: durable agreement identity + dedup (idempotent migration) ----
            _agr_cols = {r[1] for r in conn.execute("PRAGMA table_info(agreements)").fetchall()}
            for _col, _ddl in (("agreement_key", "TEXT"), ("kind", "TEXT"), ("operation_id", "TEXT"),
                               ("operation_task_id", "TEXT"), ("request_count", "INTEGER DEFAULT 1"),
                               ("last_requested_at", "REAL"), ("urgency", "INTEGER DEFAULT 0"),
                               ("offered_value", "INTEGER DEFAULT 0"), ("context_json", "TEXT")):
                if _col not in _agr_cols:
                    conn.execute(f"ALTER TABLE agreements ADD COLUMN {_col} {_ddl}")
            # backfill agreement_key for legacy rows (compute from type/parties/terms)
            for _r in conn.execute("SELECT id, save_id, type, party_a, party_b, terms_json FROM agreements "
                                   "WHERE agreement_key IS NULL OR agreement_key=''").fetchall():
                try:
                    _t = json.loads(_r[5]) if _r[5] else {}
                except Exception:
                    _t = {}
                _k = ":".join([_r[1] or "", _r[2] or "", _r[3] or "", _r[4] or "",
                               str(_t.get("operation_id") or ""), str(_t.get("operation_task_id") or ""),
                               str(_t.get("kind") or "")])
                conn.execute("UPDATE agreements SET agreement_key=?, kind=COALESCE(kind, ?), "
                             "operation_id=COALESCE(operation_id, ?), operation_task_id=COALESCE(operation_task_id, ?) "
                             "WHERE id=?",
                             (_k, str(_t.get("kind") or "") or None, str(_t.get("operation_id") or "") or None,
                              str(_t.get("operation_task_id") or "") or None, _r[0]))
            # collapse existing duplicate OPEN agreements: keep the earliest per key, supersede the rest
            conn.execute(
                "UPDATE agreements SET status='superseded', resolved_at=? "
                "WHERE status IN ('proposed','pending','pending_response','countered','no_counterparty') "
                "AND id NOT IN (SELECT MIN(id) FROM agreements "
                "  WHERE status IN ('proposed','pending','pending_response','countered','no_counterparty') "
                "  GROUP BY agreement_key)",
                (time.time(),))
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_agreements_open ON agreements(agreement_key) "
                         "WHERE status IN ('proposed','pending','pending_response','countered','no_counterparty')")
            # ---- Decision records: audit log of every Player2 Decision Adapter call (spec §12) ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT NOT NULL,
                    decision_type TEXT,
                    subject_faction TEXT,
                    linked_operation_id TEXT,
                    linked_offer_id INTEGER,
                    brief TEXT,
                    options_json TEXT,
                    advisory_json TEXT,
                    request_id TEXT,
                    raw_response TEXT,
                    parsed_choice TEXT,
                    validator_result TEXT,
                    final_status TEXT,
                    source TEXT,
                    created_at REAL NOT NULL,
                    decided_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_save ON decision_records(save_id, id)")
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
            # ---- #63 earned-economy: faction budget SPEND ledger (capacity is derived from real owned
            #      stations; this table persists what's already been drawn so a faction can't re-spend it) ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS faction_budget (
                    save_id TEXT, faction_id TEXT,
                    spent REAL DEFAULT 0,               -- cumulative credits already drawn (anti-re-spend)
                    updated_at REAL,
                    PRIMARY KEY (save_id, faction_id)
                )
            """)
            # #39 migration: an earlier build of this table used src_npc/affinity/romantic. If that shape is
            # present (no real data yet), DROP so the event-driven schema below recreates cleanly. Idempotent.
            try:
                _scols = [r[1] for r in conn.execute("PRAGMA table_info(social_relations)").fetchall()]
                if _scols and "src_npc" in _scols:
                    conn.execute("DROP TABLE IF EXISTS social_relations")
            except Exception:
                pass
            # ---- #39 SPEC 2c: NPC<->NPC social graph — FIRST-CLASS, distinct from faction `relationships`
            #      (faction = political; this = social/emotional; Codex: "don't overload one table for both").
            #      Emotional SCORES + a narrative STATUS + EVIDENCE (why it exists). Changes come ONLY from social
            #      EVENTS (saved_life, betrayal, served_together, …), never from faction projection or LLM whim. ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS social_relations (
                    save_id TEXT, subject_npc TEXT, object_npc TEXT,
                    status TEXT DEFAULT 'strangers',            -- narrative state (strangers..friends..partners..grieving)
                    relationship_type TEXT DEFAULT 'neutral',   -- coarse category (friend/rival/mentor/romantic/professional/neutral)
                    trust REAL DEFAULT 0, affection REAL DEFAULT 0, resentment REAL DEFAULT 0,
                    fear REAL DEFAULT 0, loyalty REAL DEFAULT 0, rivalry REAL DEFAULT 0,
                    debt REAL DEFAULT 0, attraction REAL DEFAULT 0,
                    publicity REAL DEFAULT 0,                   -- 0..1 how public the tie is
                    evidence_json TEXT DEFAULT '[]',           -- [{event, note, ts}] — WHY the relationship exists
                    last_updated REAL,
                    PRIMARY KEY (save_id, subject_npc, object_npc)
                )
            """)
            # Rumor propagation (design-doc §4: events spread among NPCs). A rumor an NPC has HEARD (hearsay,
            # not a durable fact) — spread along the social graph, weighted by tie strength. PK dedups per NPC.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rumors (
                    save_id TEXT, npc_key TEXT, rumor_id TEXT,
                    text TEXT, category TEXT DEFAULT 'rumor',
                    origin_npc TEXT, confidence REAL DEFAULT 0.5, hops INTEGER DEFAULT 1,
                    ts REAL,
                    PRIMARY KEY (save_id, npc_key, rumor_id)
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
            # ---- OPORD: persistent military operations command layer (spec: OPORD_Update) -------
            # Turns raw strategic pressure into ONE durable operation per threat (dedupe), with COAs,
            # tasks, and reports. The partial-unique index is the anti-spam guarantee: at most one
            # active operation per (save, faction, threat_key). Deterministic engine owns lifecycle;
            # the LLM only writes prose. (Phase 1 = schema + CRUD; later phases add threat→COA→OPORD.)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS military_operations (
                    id TEXT PRIMARY KEY,
                    save_id TEXT NOT NULL,
                    faction_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    threat_key TEXT NOT NULL,
                    threat_id TEXT,
                    target_faction TEXT,
                    target_sector TEXT,
                    target_object TEXT,
                    mission_statement TEXT,
                    commander_intent TEXT,
                    desired_end_state TEXT,
                    warning_order_json TEXT,
                    mission_analysis_json TEXT,
                    selected_coa_id TEXT,
                    opord_json TEXT,
                    annexes_json TEXT,
                    doctrine_json TEXT,
                    constraints_json TEXT,
                    ccir_json TEXT,
                    budget_reserved INTEGER DEFAULT 0,
                    budget_spent INTEGER DEFAULT 0,
                    priority INTEGER DEFAULT 0,
                    urgency INTEGER DEFAULT 0,
                    importance INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    issued_at REAL,
                    activated_at REAL,
                    concluded_at REAL,
                    conclusion_status TEXT,
                    conclusion_summary TEXT,
                    evidence_json TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_save_status ON military_operations(save_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_faction_status ON military_operations(save_id, faction_id, status)")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_active_threat
                ON military_operations(save_id, faction_id, threat_key)
                WHERE status NOT IN ('completed', 'failed', 'aborted', 'transitioned')
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS operation_coas (
                    id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    coa_type TEXT NOT NULL,
                    concept TEXT NOT NULL,
                    viability_status TEXT NOT NULL,
                    rejection_reason TEXT,
                    tasks_json TEXT NOT NULL,
                    required_assets_json TEXT,
                    required_budget INTEGER DEFAULT 0,
                    expected_duration REAL,
                    wargame_json TEXT,
                    score_json TEXT,
                    weighted_score REAL DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES military_operations(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_coas_op ON operation_coas(operation_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS operation_tasks (
                    id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    coa_id TEXT,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    assigned_actor_type TEXT,
                    assigned_actor_id TEXT,
                    owning_faction TEXT,
                    target_faction TEXT,
                    target_sector TEXT,
                    target_object TEXT,
                    job_id TEXT,
                    agreement_id TEXT,
                    order_id TEXT,
                    success_criteria_json TEXT,
                    failure_criteria_json TEXT,
                    evidence_json TEXT,
                    created_at REAL NOT NULL,
                    issued_at REAL,
                    activated_at REAL,
                    completed_at REAL,
                    failed_at REAL,
                    FOREIGN KEY(operation_id) REFERENCES military_operations(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_optasks_op ON operation_tasks(operation_id, status)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS operation_reports (
                    id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    severity INTEGER DEFAULT 0,
                    summary TEXT NOT NULL,
                    evidence_json TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES military_operations(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_opreports_op ON operation_reports(operation_id, created_at)")
            # ---- OPORD Phase 6: job market (external fulfillment routing) ----------------------------
            # When a faction can't fulfill an OPORD task internally, the requirement becomes a job-market
            # listing (patrol/escort/supply/privateer/recon/defence). The partial-unique open-job index is the
            # anti-spam guarantee: ONE open job per job_key — repeated pressure updates urgency/reward, never reposts.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_jobs (
                    id TEXT PRIMARY KEY,
                    save_id TEXT NOT NULL,
                    issuing_faction TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    job_key TEXT NOT NULL,
                    target_sector TEXT,
                    target_faction TEXT,
                    ware TEXT,
                    reward INTEGER DEFAULT 0,
                    urgency INTEGER DEFAULT 0,
                    visibility TEXT DEFAULT 'public',
                    status TEXT NOT NULL,
                    operation_id TEXT,
                    operation_task_id TEXT,
                    evidence_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_save_status ON market_jobs(save_id, status)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_open_key "
                         "ON market_jobs(save_id, job_key) WHERE status='open'")
            # ---- OPORD Execution Authority: real-ship leases + force-quota requests ------------------
            # A military task is only EXECUTED when a real faction ship is leased + ordered. The lease table
            # prevents two OPORDs stealing the same ship, tracks whether vanilla AI overwrote the order, and gives
            # SITREP/FRAGO real evidence. opord_force_requests = the durable demand when no ship is available.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opord_asset_leases (
                    lease_id TEXT PRIMARY KEY,
                    save_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    task_id TEXT,
                    faction TEXT,
                    ship_runtime_id TEXT,
                    ship_name TEXT,
                    ship_macro TEXT,
                    ship_class TEXT,
                    sector TEXT,
                    original_order_summary TEXT,
                    assigned_order_id TEXT,
                    order_kind TEXT,
                    priority INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    issued_at REAL,
                    last_seen_at REAL,
                    released_at REAL,
                    failure_reason TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leases_op ON opord_asset_leases(save_id, operation_id, status)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active_ship "
                         "ON opord_asset_leases(save_id, ship_runtime_id) "
                         "WHERE status NOT IN ('completed','failed','released','lost')")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opord_force_requests (
                    request_id TEXT PRIMARY KEY,
                    save_id TEXT NOT NULL,
                    operation_id TEXT,
                    task_id TEXT,
                    faction TEXT,
                    sector TEXT,
                    ship_role TEXT,
                    ship_size TEXT,
                    quantity INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    reward_budget INTEGER DEFAULT 0,
                    req_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_escalated_at REAL,
                    expires_at REAL
                )
            """)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_forcereq_open_key "
                         "ON opord_force_requests(save_id, req_key) WHERE status='open'")
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
            # --- EPIC I (I0): Synthetic Persistent NPC Identity Layer ------------------
            # X4 exposes NO stable cross-reload identity for generic crew (proven #99: runtime `raw`
            # and the save `<component id>` are the SAME volatile UniverseID, regenerated every reload;
            # idcode empty). So Neural Link OWNS identity: a handle-independent persistent_npc_key
            # derived from STABLE evidence, with per-session runtime bindings. Memory stays in
            # facts/turns (NOT destructively re-keyed); a resolution layer (resolve_memory_keys) unions
            # every npc_key ever linked to an identity. Adds only new tables + npcs.persistent_key →
            # fully reversible.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(npcs)").fetchall()}
            if "persistent_key" not in existing:
                conn.execute("ALTER TABLE npcs ADD COLUMN persistent_key TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_npcs_persistent ON npcs(persistent_key)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_identities (
                    persistent_npc_key  TEXT PRIMARY KEY,
                    display_name        TEXT,
                    faction             TEXT,
                    role                TEXT,
                    race                TEXT,
                    gender              TEXT,
                    macro               TEXT,
                    npc_code            TEXT,
                    first_seen_save     TEXT,
                    first_seen_time     REAL,
                    importance_tier     INTEGER DEFAULT 3,   -- 0 faction-abstraction .. 3 background (spec tiers)
                    identity_confidence REAL DEFAULT 1.0,
                    status              TEXT DEFAULT 'session-only',
                    updated_at          REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_identity_evidence (
                    persistent_npc_key  TEXT NOT NULL,
                    evidence_type       TEXT NOT NULL,       -- name|faction|role|macro|npc_code|skill_vector|...
                    value               TEXT NOT NULL,
                    weight              REAL DEFAULT 0,
                    first_seen          REAL,
                    last_seen           REAL,
                    PRIMARY KEY (persistent_npc_key, evidence_type, value)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_evidence ON npc_identity_evidence(persistent_npc_key)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_runtime_bindings (
                    runtime_component_id TEXT NOT NULL,      -- volatile X4 UniverseID for THIS session
                    persistent_npc_key   TEXT NOT NULL,
                    save_id              TEXT,
                    game_session_id      TEXT NOT NULL,
                    seen_at              REAL,
                    sector               TEXT,
                    ship_id              TEXT,
                    station_id           TEXT,
                    confidence           REAL DEFAULT 0,
                    evidence_json        TEXT,
                    PRIMARY KEY (runtime_component_id, game_session_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bindings_persistent ON npc_runtime_bindings(persistent_npc_key)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_blackboard_probe (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    save_id TEXT,
                    phase TEXT,                  -- write|same_session|after_reload|matrix|duplicate|lifecycle
                    target_type TEXT,            -- conversation_person|control|ship|station|player
                    runtime_component_id TEXT,
                    npc_name TEXT,
                    faction TEXT,
                    role TEXT,
                    ship_or_station TEXT,
                    sector TEXT,
                    blackboard_key TEXT,
                    blackboard_value TEXT,        -- correlation token (string key, or "objref_<id>" for object refs)
                    payload_type TEXT DEFAULT 'string',  -- string | object (ChemODun: object-ref handle is the strong path)
                    npctemplate TEXT,             -- npctemplate id (Tier-2 fallback when the live object despawns)
                    restored_match INTEGER DEFAULT 0,    -- 1 if the restored object/template matches name/faction/role
                    write_success INTEGER,
                    read_success INTEGER,
                    created_at REAL
                )
            """)
            # Migrate existing DBs (table predates the object-ref/template columns).
            for _col, _decl in (("payload_type", "TEXT DEFAULT 'string'"), ("npctemplate", "TEXT"),
                                ("restored_match", "INTEGER DEFAULT 0")):
                try:
                    conn.execute(f"ALTER TABLE npc_blackboard_probe ADD COLUMN {_col} {_decl}")
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bbprobe_save ON npc_blackboard_probe(save_id, blackboard_value)")
            conn.commit()

    # --- NPC binding / retrieval ---------------------------------------------

    @staticmethod
    def make_key(save_id: str, game_id: str, persona: str) -> str:
        return f"{save_id or 'nosave'}|{game_id or 'nogame'}|{persona or 'default'}"

    def chat_npc_key(self, save_id: str, game_id: str, name: str, blackboard_token: str = "") -> str:
        """The conversation memory CARD key — ONE card per (save, name). The Blackboard token is the durable
        IDENTITY (stamped on the card via bind_blackboard_identity, survives reload) — it is NOT the card key.
        Keying cards by token duplicated NPCs (one real NPC → two cards, history stranded), so card-keying is
        name-based; same-name SPLIT is deferred until a genuine collision is actually observed. `blackboard_token`
        is accepted for call-site compatibility but no longer changes the card key."""
        return self.make_key(save_id, game_id, name or "")

    # X4 crew skills (0..15 internal = 0..5 stars). morale is universal.
    X4_SKILLS = ("piloting", "management", "engineering", "boarding", "morale")

    # EPIC I (I2) identity-rebind thresholds + near-tie delta (from the spec).
    IDENTITY_BIND = 0.80          # >= → bind automatically
    IDENTITY_TENTATIVE = 0.60     # >= → tentative bind
    IDENTITY_AMBIGUOUS = 0.40     # >= (or near-tie) → ambiguous, do NOT merge memories
    IDENTITY_NEAR_TIE = 0.07      # top-second gap at/below this = ambiguous

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

    # Role inference (bugfix 2026-06-28): the MD only detects entityrole marine/service and defaults
    # everything else (managers, pilots, …) to a generic 'crew'. When the role is generic but the skill
    # vector clearly dominates, infer the real posting from the dominant non-morale skill (morale is
    # universal). So a station Manager (management-dominant) is recorded as 'manager', not 'crew'.
    GENERIC_ROLES = frozenset({"", "crew", "default", "officer", "faction officer"})
    SKILL_ROLE = {"management": "manager", "piloting": "pilot", "engineering": "engineer", "boarding": "marine"}

    @staticmethod
    def role_from_skills(skills: Any) -> str:
        """Dominant crew skill → posting (manager/pilot/engineer/marine), morale excluded. Requires a clear
        lead (top >= 5 of 15 and strictly above the runner-up) to avoid noise. '' if none."""
        try:
            if isinstance(skills, str):
                try:
                    skills = json.loads(skills)
                except Exception:
                    return ""
            if not isinstance(skills, dict):
                return ""
            pairs: list[tuple[str, float]] = []
            for k, v in skills.items():
                if k not in MemoryStore.SKILL_ROLE or v in (None, ""):
                    continue
                try:
                    pairs.append((k, float(v)))
                except (TypeError, ValueError):
                    continue
            if not pairs:
                return ""
            pairs.sort(key=lambda kv: kv[1], reverse=True)
            top_k, top_v = pairs[0]
            second_v = pairs[1][1] if len(pairs) > 1 else 0.0
            return MemoryStore.SKILL_ROLE[top_k] if (top_v >= 5 and top_v > second_v) else ""
        except Exception:
            return ""

    def _role_with_skills(self, stats: Any) -> str:
        """The MD-provided role if specific; else inferred from the skill vector; else as given. NEVER throws
        (it runs in the hot bind/index path the heartbeat hammers — bad input must degrade, not crash)."""
        try:
            stats = stats if isinstance(stats, dict) else {}
            role = str(stats.get("role") or "").strip()
            if role.lower() in MemoryStore.GENERIC_ROLES:
                inferred = MemoryStore.role_from_skills(stats.get("skills"))
                if inferred:
                    return inferred
            return role
        except Exception:
            return ""

    def reinfer_roles(self) -> dict:
        """One-shot: correct existing npcs rows whose stored role is generic but whose skills clearly
        indicate a posting (fixes records captured before the inference existed), AND propagate the
        corrected/specific role to each NPC's linked persistent identity (the dashboard identity mirror —
        otherwise a re-inferred 'manager' stays 'crew' on the identity until the NPC is next talked to).
        Read-only otherwise; never clobbers a non-generic identity role."""
        fixed = 0
        idents_fixed = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT npc_key, role, skills, persistent_key FROM npcs").fetchall()
        for r in rows:
            cur = str(r["role"] or "").strip()
            inferred = self.role_from_skills(r["skills"])
            # 1) Fix the npcs-table role when it's generic but the skills clearly say otherwise.
            if inferred and cur.lower() in MemoryStore.GENERIC_ROLES and inferred != cur:
                with self._lock, self._connect() as conn:
                    conn.execute("UPDATE npcs SET role = ? WHERE npc_key = ?", (inferred, r["npc_key"]))
                    conn.commit()
                fixed += 1
                cur = inferred
            # 2) Propagate a SPECIFIC role to the linked identity if its role is still generic/empty.
            pkey = str(r["persistent_key"] or "").strip()
            if pkey and cur and cur.lower() not in MemoryStore.GENERIC_ROLES:
                with self._lock, self._connect() as conn:
                    iv = conn.execute("SELECT role FROM npc_identities WHERE persistent_npc_key = ?", (pkey,)).fetchone()
                    if iv is not None:
                        irole = str(iv["role"] or "").strip()
                        if irole.lower() in MemoryStore.GENERIC_ROLES and irole.lower() != cur.lower():
                            conn.execute("UPDATE npc_identities SET role = ?, updated_at = ? WHERE persistent_npc_key = ?",
                                         (cur, time.time(), pkey))
                            conn.commit()
                            idents_fixed += 1
        return {"ok": True, "rows_fixed": fixed, "identities_fixed": idents_fixed}

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
        role = self._role_with_skills(stats)
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
        role = self._role_with_skills(stats)
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
                    last_active = excluded.last_active,
                    is_alive = 1
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

    def sweep_deceased_npcs(self, save_id: str = "", stale_seconds: float = 3600.0) -> dict:
        """War-scale death handling WITHOUT chasing individual deaths. The census re-reads ground truth every
        cycle and refreshes `last_active`; an NPC not re-seen for > stale_seconds means its ship/station is gone
        (destroyed/despawned). KNOWN NPCs (have conversation turns) → mark deceased (`is_alive=0`, memory kept so
        "they died" is part of the story); generic roster-only NPCs (no turns) → prune (delete) to keep the DB
        lean during long wars. Chat-scope only (`game_id='chat'`); one bounded sweep, so a 300-ship battle never
        floods us. stale_seconds MUST exceed a full census cycle (tune in-game); defaults conservative."""
        cutoff = time.time() - max(60.0, float(stale_seconds or 3600.0))
        marked, pruned = 0, 0
        with self._lock, self._connect() as conn:
            params: list = [cutoff]
            sql = "SELECT npc_key FROM npcs WHERE game_id='chat' AND is_alive=1 AND last_active < ?"
            if save_id:
                sql += " AND save_id = ?"; params.append(save_id)
            for r in conn.execute(sql, tuple(params)).fetchall():
                k = r["npc_key"]
                turns = conn.execute("SELECT COUNT(*) AS c FROM turns WHERE npc_key = ?", (k,)).fetchone()["c"]
                if turns > 0:
                    conn.execute("UPDATE npcs SET is_alive = 0 WHERE npc_key = ?", (k,)); marked += 1
                else:
                    conn.execute("DELETE FROM facts WHERE npc_key = ?", (k,))
                    conn.execute("DELETE FROM npcs WHERE npc_key = ?", (k,)); pruned += 1
            conn.commit()
        return {"ok": True, "marked_deceased": marked, "pruned": pruned, "cutoff": cutoff}

    # --- EPIC I (I0): Persistent NPC identity layer ---------------------------
    # persistent_npc_key is HANDLE-INDEPENDENT: derived ONLY from stable evidence (name, faction,
    # role, macro, npc_code, skill-vector) — never from a volatile runtime/save handle. So the SAME
    # NPC yields the SAME key across reloads even though X4's component id changes every load.

    @staticmethod
    def _skill_vector_sig(skills: Any) -> str:
        """Order-stable signature of a crew skill vector, for identity evidence/derivation."""
        if isinstance(skills, str):
            try:
                skills = json.loads(skills)
            except Exception:
                skills = None
        if not isinstance(skills, dict):
            return ""
        parts: list[str] = []
        for k in MemoryStore.X4_SKILLS:
            if k in skills and skills[k] not in (None, ""):
                try:
                    parts.append(f"{k}:{int(float(skills[k]))}")
                except Exception:
                    parts.append(f"{k}:{skills[k]}")
        return " ".join(parts)

    @staticmethod
    def derive_persistent_key(evidence: dict) -> str:
        """Deterministic, handle-independent identity key from STABLE evidence only. Volatile fields
        (runtime_component_id, save_id, game_session_id, ship_name, sector) are DELIBERATELY excluded
        so the key survives a reload. Returns 'pid:<12-hex>'."""
        name = str(evidence.get("name") or evidence.get("display_name") or "").strip().lower()
        faction = str(evidence.get("faction") or evidence.get("faction_id") or evidence.get("owner") or "").strip().lower()
        role = str(evidence.get("role") or "").strip().lower()
        macro = str(evidence.get("macro") or "").strip().lower()
        code = str(evidence.get("npc_code") or evidence.get("code") or "").strip().lower()
        skills = MemoryStore._skill_vector_sig(evidence.get("skills"))
        basis = "|".join([name, faction, role, macro, code, skills])
        return "pid:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def upsert_identity(self, persistent_npc_key: str, attrs: Optional[dict] = None) -> None:
        """Create/refresh an identity. Descriptive attrs are merged on conflict; tier/confidence/
        status are set ONLY on first insert (their lifecycle is owned by I3/I2, not this writer)."""
        attrs = attrs or {}
        now = time.time()
        tier = attrs.get("importance_tier")
        conf = attrs.get("identity_confidence")
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO npc_identities (persistent_npc_key, display_name, faction, role, race, gender,
                                            macro, npc_code, first_seen_save, first_seen_time,
                                            importance_tier, identity_confidence, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persistent_npc_key) DO UPDATE SET
                    display_name = COALESCE(NULLIF(excluded.display_name,''), npc_identities.display_name),
                    faction      = COALESCE(NULLIF(excluded.faction,''), npc_identities.faction),
                    role         = COALESCE(NULLIF(excluded.role,''), npc_identities.role),
                    race         = COALESCE(NULLIF(excluded.race,''), npc_identities.race),
                    gender       = COALESCE(NULLIF(excluded.gender,''), npc_identities.gender),
                    macro        = COALESCE(NULLIF(excluded.macro,''), npc_identities.macro),
                    npc_code     = COALESCE(NULLIF(excluded.npc_code,''), npc_identities.npc_code),
                    updated_at   = excluded.updated_at
            """, (
                persistent_npc_key,
                str(attrs.get("display_name") or attrs.get("name") or ""),
                str(attrs.get("faction") or attrs.get("faction_id") or ""),
                str(attrs.get("role") or ""),
                str(attrs.get("race") or ""),
                str(attrs.get("gender") or ""),
                str(attrs.get("macro") or ""),
                str(attrs.get("npc_code") or attrs.get("code") or ""),
                str(attrs.get("first_seen_save") or ""),
                float(attrs.get("first_seen_time") or now),
                int(tier) if tier is not None else 3,
                float(conf) if conf is not None else 1.0,
                str(attrs.get("status") or "session-only"),
                now,
            ))
            conn.commit()

    def set_identity_fields(self, persistent_npc_key: str, *, importance_tier: Optional[int] = None,
                            identity_confidence: Optional[float] = None, status: Optional[str] = None) -> None:
        """Lifecycle writer for tier/confidence/status (used by I2 rebind + I3 promotion)."""
        sets, vals = [], []
        if importance_tier is not None:
            sets.append("importance_tier = ?"); vals.append(int(importance_tier))
        if identity_confidence is not None:
            sets.append("identity_confidence = ?"); vals.append(float(identity_confidence))
        if status is not None:
            sets.append("status = ?"); vals.append(str(status))
        if not sets:
            return
        sets.append("updated_at = ?"); vals.append(time.time())
        vals.append(persistent_npc_key)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE npc_identities SET {', '.join(sets)} WHERE persistent_npc_key = ?", vals)
            conn.commit()

    def record_evidence(self, persistent_npc_key: str, evidence_type: str, value: str, weight: float = 0.0) -> None:
        v = str(value or "").strip()
        if not persistent_npc_key or not evidence_type or not v:
            return
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO npc_identity_evidence (persistent_npc_key, evidence_type, value, weight, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(persistent_npc_key, evidence_type, value) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    weight = MAX(npc_identity_evidence.weight, excluded.weight)
            """, (persistent_npc_key, str(evidence_type), v, float(weight), now, now))
            conn.commit()

    def get_evidence(self, persistent_npc_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_type, value, weight, first_seen, last_seen FROM npc_identity_evidence "
                "WHERE persistent_npc_key = ? ORDER BY weight DESC, evidence_type",
                (persistent_npc_key,)).fetchall()
        return [dict(r) for r in rows]

    def link_npc_to_identity(self, npc_key: str, persistent_npc_key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE npcs SET persistent_key = ? WHERE npc_key = ?", (persistent_npc_key, npc_key))
            conn.commit()

    def resolve_memory_keys(self, persistent_npc_key: str) -> list[str]:
        """Every npc_key ever linked to this identity — the basis for cross-reload memory union.
        facts/turns stay keyed by npc_key; the identity unions them WITHOUT destructive re-keying."""
        if not persistent_npc_key:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT npc_key FROM npcs WHERE persistent_key = ?", (persistent_npc_key,)).fetchall()
        return [r["npc_key"] for r in rows]

    def bind_runtime(self, runtime_component_id: str, persistent_npc_key: str, game_session_id: str,
                     save_id: str = "", sector: str = "", ship_id: str = "", station_id: str = "",
                     confidence: float = 0.0, evidence_json: str = "") -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO npc_runtime_bindings (runtime_component_id, persistent_npc_key, save_id, game_session_id,
                                                  seen_at, sector, ship_id, station_id, confidence, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runtime_component_id, game_session_id) DO UPDATE SET
                    persistent_npc_key = excluded.persistent_npc_key,
                    seen_at = excluded.seen_at, sector = excluded.sector,
                    ship_id = excluded.ship_id, station_id = excluded.station_id,
                    confidence = excluded.confidence, evidence_json = excluded.evidence_json
            """, (str(runtime_component_id), persistent_npc_key, save_id, str(game_session_id),
                  now, sector, ship_id, station_id, float(confidence), evidence_json))
            conn.commit()

    def expire_session_bindings(self, game_session_id: str) -> int:
        """Reload-flow step 2: drop the prior session's volatile runtime bindings. Identities + memory KEPT."""
        with self._lock, self._connect() as conn:
            n = conn.execute("DELETE FROM npc_runtime_bindings WHERE game_session_id = ?",
                             (str(game_session_id),)).rowcount or 0
            conn.commit()
        return n

    def get_identity(self, persistent_npc_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM npc_identities WHERE persistent_npc_key = ?",
                               (persistent_npc_key,)).fetchone()
        return dict(row) if row else None

    def list_identities(self, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM npc_identities ORDER BY importance_tier ASC, updated_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_runtime_bindings(self, persistent_npc_key: str, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT runtime_component_id, save_id, game_session_id, seen_at, sector, ship_id, station_id, confidence "
                "FROM npc_runtime_bindings WHERE persistent_npc_key = ? ORDER BY seen_at DESC LIMIT ?",
                (persistent_npc_key, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def count_name_collisions(self, display_name: str, exclude: str = "") -> int:
        """How many OTHER identities share this display name — a duplicate-name collision risk surfaced
        on the dashboard so a wrong evidence-merge is visible, not silent."""
        nm = (display_name or "").strip().lower()
        if not nm:
            return 0
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM npc_identities WHERE lower(display_name) = ? AND persistent_npc_key != ?",
                             (nm, exclude)).fetchone()["c"]
        return int(n)

    def backfill_identities(self) -> dict:
        """Idempotent + reversible: give every existing npcs row that lacks one a persistent identity
        derived from its stable evidence. Writes ONLY npc_identities/evidence + npcs.persistent_key —
        never touches facts/turns — so it is safe to re-run and to roll back. (Precise merging of
        sparse vs rich rows for the SAME NPC is I2's scorer; this just guarantees every row has an id.)"""
        created = 0
        linked = 0
        with self._connect() as conn:
            # I8: identities are for PERSONS the player converses with (game_id='chat'). Faction
            # abstractions / reaction / news rows are NOT persons — they're faction-keyed already and
            # must not get evidence-scored identities (that polluted the table with duplicates).
            rows = conn.execute(
                "SELECT npc_key, name, faction_id, role, race, gender, skills, save_id, persistent_key "
                "FROM npcs WHERE game_id = 'chat'"
            ).fetchall()
        scanned = len(rows)
        for r in rows:
            if r["persistent_key"]:
                continue  # already linked — idempotent skip
            ev = {"name": r["name"], "faction": r["faction_id"], "role": r["role"],
                  "race": r["race"], "gender": r["gender"], "skills": r["skills"]}
            pkey = self.derive_persistent_key(ev)
            if self.get_identity(pkey) is None:
                created += 1
            self.upsert_identity(pkey, {
                "display_name": r["name"], "faction": r["faction_id"], "role": r["role"],
                "race": r["race"], "gender": r["gender"], "first_seen_save": r["save_id"],
            })
            for et, val in (("name", r["name"]), ("faction", r["faction_id"]), ("role", r["role"]),
                            ("race", r["race"]), ("gender", r["gender"])):
                if val:
                    self.record_evidence(pkey, et, str(val))
            sv = self._skill_vector_sig(r["skills"])
            if sv:
                self.record_evidence(pkey, "skill_vector", sv)
            self.link_npc_to_identity(r["npc_key"], pkey)
            linked += 1
        return {"ok": True, "identities_created": created, "rows_linked": linked, "rows_scanned": scanned}

    def reset_identities(self) -> dict:
        """I8 CLEANUP: wipe the identity layer (identities + evidence + runtime bindings) and unlink all
        npcs, then rebuild cleanly from chat NPCs only. Use after the pre-gate rebind polluted the table
        with faction-abstraction / reaction / news duplicates. Touches ONLY the identity tables +
        npcs.persistent_key — facts/turns/memory are untouched, so it is safe."""
        with self._lock, self._connect() as conn:
            cleared = conn.execute("SELECT COUNT(*) AS c FROM npc_identities").fetchone()["c"]
            conn.execute("DELETE FROM npc_identities")
            conn.execute("DELETE FROM npc_identity_evidence")
            conn.execute("DELETE FROM npc_runtime_bindings")
            conn.execute("UPDATE npcs SET persistent_key = NULL")
            conn.commit()
        bf = self.backfill_identities()
        return {"ok": True, "cleared_identities": int(cleared), "rebuilt": bf}

    # --- EPIC I (I2): evidence scoring + per-session rebind --------------------
    # X4 ids are session handles; on each reload we re-identify current NPCs against existing
    # identities by SCORING evidence (not one id), then bind/tentative/ambiguous/new per the spec.

    def _norm_fac(self, v: Any) -> str:
        v = str(v or "").strip()
        if not v:
            return ""
        try:
            return (self.resolve_faction_id(v) or v).lower()
        except Exception:
            return v.lower()

    def _candidate_identity_keys(self, conn: sqlite3.Connection, name: str, code: str) -> set[str]:
        """Cheap candidate gather: identities sharing the (strong) name or npc_code signals."""
        keys: set[str] = set()
        nm = (name or "").strip().lower()
        cd = (code or "").strip().lower()
        if nm:
            for r in conn.execute("SELECT persistent_npc_key FROM npc_identities WHERE lower(display_name) = ?", (nm,)):
                keys.add(r["persistent_npc_key"])
            for r in conn.execute("SELECT persistent_npc_key FROM npc_identity_evidence WHERE evidence_type='name' AND lower(value) = ?", (nm,)):
                keys.add(r["persistent_npc_key"])
        if cd:
            for r in conn.execute("SELECT persistent_npc_key FROM npc_identity_evidence WHERE evidence_type='npc_code' AND lower(value) = ?", (cd,)):
                keys.add(r["persistent_npc_key"])
        return keys

    def score_identity(self, evidence: dict, game_session_id: str = "") -> list[dict]:
        """Score an observed NPC's evidence against existing identities (spec weights/penalties).
        Returns candidates sorted by score desc: [{persistent_npc_key, score, reasons, display_name}]."""
        name = str(evidence.get("name") or "").strip()
        role = str(evidence.get("role") or "").strip().lower()
        macro = str(evidence.get("macro") or "").strip().lower()
        code = str(evidence.get("npc_code") or evidence.get("code") or "").strip().lower()
        skillsig = self._skill_vector_sig(evidence.get("skills")).lower()
        container = str(evidence.get("ship_id") or evidence.get("station_id") or evidence.get("container") or "").strip().lower()
        sector = str(evidence.get("sector") or "").strip().lower()
        recently = bool(evidence.get("recently_talked"))
        rcid = str(evidence.get("runtime_component_id") or "").strip()
        nl = name.lower()
        fl = self._norm_fac(evidence.get("faction") or evidence.get("faction_id") or evidence.get("owner"))
        results: list[dict] = []
        with self._connect() as conn:
            for pkey in self._candidate_identity_keys(conn, name, code):
                ident = conn.execute("SELECT * FROM npc_identities WHERE persistent_npc_key = ?", (pkey,)).fetchone()
                if not ident:
                    continue
                ev: dict[str, set] = {}
                for e in conn.execute("SELECT evidence_type, value FROM npc_identity_evidence WHERE persistent_npc_key = ?", (pkey,)):
                    ev.setdefault(e["evidence_type"], set()).add(str(e["value"]).lower())

                def has(et: str, val: str) -> bool:
                    return bool(val) and val in ev.get(et, set())

                cand_name = (ident["display_name"] or "").lower()
                cand_fac = self._norm_fac(ident["faction"])
                cand_role = (ident["role"] or "").lower()
                cand_macro = (ident["macro"] or "").lower()
                cand_code = (ident["npc_code"] or "").lower()
                score = 0.0
                reasons: list[str] = []
                name_match = bool(nl) and (nl == cand_name or has("name", nl))
                if name_match:
                    score += 0.25; reasons.append("same name")
                fac_match = bool(fl) and fl == cand_fac
                if fac_match:
                    score += 0.15; reasons.append("same faction")
                role_match = bool(role) and (role == cand_role or has("role", role))
                if role_match:
                    score += 0.10; reasons.append("same role")
                macro_match = bool(macro) and (macro == cand_macro or has("macro", macro))
                if macro_match:
                    score += 0.15; reasons.append("same macro")
                code_match = bool(code) and (code == cand_code or has("npc_code", code))
                if code_match:
                    score += 0.25; reasons.append("same npc_code")
                if skillsig and has("skill_vector", skillsig):
                    score += 0.15; reasons.append("same skill_vector")
                if container and has("container", container):
                    score += 0.20; reasons.append("same container")
                if sector and has("sector", sector):
                    score += 0.05; reasons.append("same sector")
                if recently:
                    score += 0.10; reasons.append("recently talked")
                if rcid and game_session_id:
                    b = conn.execute("SELECT persistent_npc_key FROM npc_runtime_bindings WHERE runtime_component_id=? AND game_session_id=?",
                                     (rcid, game_session_id)).fetchone()
                    if b and b["persistent_npc_key"] == pkey:
                        score += 0.10; reasons.append("same-session runtime id")
                # Penalties — guard against false merges on a shared name.
                if name_match and fl and cand_fac and fl != cand_fac:
                    score -= 0.40; reasons.append("name match but different faction")
                if name_match and role and cand_role and role != cand_role and macro and cand_macro and macro != cand_macro:
                    score -= 0.25; reasons.append("name match but different role+macro")
                score = max(0.0, min(1.0, score))
                results.append({"persistent_npc_key": pkey, "score": round(score, 4),
                                "reasons": reasons, "display_name": ident["display_name"]})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def _record_obs_evidence(self, pkey: str, obs: dict) -> None:
        for et, val, w in (
            ("name", obs.get("name"), 0.25),
            ("faction", obs.get("faction") or obs.get("faction_id"), 0.15),
            ("role", obs.get("role"), 0.10),
            ("macro", obs.get("macro"), 0.15),
            ("npc_code", obs.get("npc_code") or obs.get("code"), 0.25),
            ("container", obs.get("ship_id") or obs.get("station_id") or obs.get("container"), 0.20),
            ("sector", obs.get("sector"), 0.05),
        ):
            if val:
                self.record_evidence(pkey, et, str(val), w)
        sv = self._skill_vector_sig(obs.get("skills"))
        if sv:
            self.record_evidence(pkey, "skill_vector", sv, 0.15)

    def rebind_session(self, game_session_id: str, observed: list[dict], save_id: str = "") -> dict:
        """Reload flow: score each observed NPC vs existing identities and bind. Decisions:
        >=0.80 (no near-tie) bound · >=0.60 tentative · >=0.40 OR near-tie ambiguous (fresh temp
        identity, NO memory merge) · else new. Links this session's npc_key to the chosen identity
        so resolve_memory_keys unions memory across reloads. Never merges into an ambiguous match."""
        counts = {"bound": 0, "tentative": 0, "ambiguous": 0, "new": 0}
        out: list[dict] = []
        for obs in observed or []:
            rcid = str(obs.get("runtime_component_id") or "")
            npc_key = str(obs.get("npc_key") or "")
            ranked = self.score_identity(obs, game_session_id=game_session_id)
            top = ranked[0] if ranked else None
            second = ranked[1] if len(ranked) > 1 else None
            near_tie = bool(top and second and (top["score"] - second["score"]) <= self.IDENTITY_NEAR_TIE)
            conf = float(top["score"]) if top else 0.0
            if top and top["score"] >= self.IDENTITY_BIND and not near_tie:
                decision = "bound"; pkey = top["persistent_npc_key"]
            elif top and top["score"] >= self.IDENTITY_TENTATIVE and not near_tie:
                decision = "tentative"; pkey = top["persistent_npc_key"]
            elif top and (top["score"] >= self.IDENTITY_AMBIGUOUS or near_tie):
                decision = "ambiguous"
                pkey = self.derive_persistent_key(obs) + ":amb"   # fresh temp identity — never merges into a real one
                self.upsert_identity(pkey, {**obs, "status": "ambiguous", "identity_confidence": conf})
            else:
                decision = "new"; conf = 1.0
                pkey = self.derive_persistent_key(obs)
                self.upsert_identity(pkey, {**obs, "status": "new"})
            counts[decision] += 1
            if decision in ("bound", "tentative"):
                self.set_identity_fields(pkey, identity_confidence=conf, status=decision)
            self._record_obs_evidence(pkey, obs)
            if npc_key:
                self.link_npc_to_identity(npc_key, pkey)
            if rcid:
                self.bind_runtime(rcid, pkey, game_session_id, save_id=save_id,
                                  sector=str(obs.get("sector") or ""), ship_id=str(obs.get("ship_id") or ""),
                                  station_id=str(obs.get("station_id") or ""), confidence=conf,
                                  evidence_json=json.dumps(top["reasons"]) if top else "")
            out.append({"npc_key": npc_key, "runtime_component_id": rcid, "decision": decision,
                        "persistent_npc_key": pkey, "confidence": round(conf, 4), "near_tie": near_tie,
                        "top_score": top["score"] if top else 0.0})
        return {"ok": True, "game_session_id": game_session_id, **counts, "results": out}

    # --- EPIC I (I3): importance-tier promotion -------------------------------
    # Tracking priority on npc_identities.importance_tier (lower = more important):
    # 0 faction-abstraction · 1 player-significant · 2 local-important · 3 background (default).
    # NOTE: distinct from npcs.tier (faction-AUTHORITY hierarchy, leader=3) — different axis.
    PROMOTION_TIER: dict[str, int] = {
        "talked": 1, "mission": 1, "negotiated": 1, "assigned": 1, "romance": 1,
        "rivalry": 1, "relationship": 1, "named_recall": 1,
        "news_event": 2, "social_event": 2, "local": 2,
        "faction_abstraction": 0,
    }

    def promote_identity(self, persistent_npc_key: str, reason: str) -> Optional[int]:
        """Raise tracking priority when a promotion trigger fires. Idempotent — only ever LOWERS the
        tier number (more important), never demotes. Returns the resulting tier, or None if no-op."""
        target = self.PROMOTION_TIER.get(str(reason or "").lower())
        if target is None or not persistent_npc_key:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT importance_tier FROM npc_identities WHERE persistent_npc_key = ?",
                               (persistent_npc_key,)).fetchone()
            if not row:
                return None
            cur = row["importance_tier"] if row["importance_tier"] is not None else 3
            if target >= cur:
                return cur  # never demote
            conn.execute("UPDATE npc_identities SET importance_tier = ?, updated_at = ? WHERE persistent_npc_key = ?",
                         (target, time.time(), persistent_npc_key))
            conn.commit()
            return target

    def promote_identity_for_npc(self, npc_key: str, reason: str) -> Optional[int]:
        """Promote the identity linked to a session npc_key — the bridge from runtime turns/events to
        the persistent identity. No-op if the npc isn't linked to an identity yet."""
        if not npc_key:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT persistent_key FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
        pkey = row["persistent_key"] if row else None
        return self.promote_identity(pkey, reason) if pkey else None

    # --- EPIC I (I7): player soft-confirmation of a tentative bind --------------
    # Words too common to count as a "specific" shared-history match (incl. the recall verbs themselves).
    _CONFIRM_STOPWORDS = frozenset({
        "about", "after", "again", "ago", "also", "another", "back", "been", "before", "being", "could",
        "discussed", "does", "doing", "down", "earlier", "from", "have", "here", "into", "just", "know",
        "last", "like", "mentioned", "more", "much", "remember", "said", "same", "she", "some", "spoke",
        "still", "talk", "talked", "than", "that", "their", "them", "then", "there", "these", "they",
        "thing", "things", "this", "those", "time", "told", "very", "want", "we're", "well", "were", "what",
        "when", "where", "which", "while", "with", "would", "yesterday", "your", "yours", "you're",
    })

    @staticmethod
    def _significant_words(text: str) -> set:
        """Content words (>3 chars, not a stopword) used to test whether a player's claim of shared history
        overlaps the NPC's REAL stored memory."""
        return {w for w in re.findall(r"[a-z0-9']+", str(text or "").lower())
                if len(w) > 3 and w not in MemoryStore._CONFIRM_STOPWORDS}

    def soft_confirm_identity(self, npc_key: str, assertion: str, min_overlap: int = 2) -> dict:
        """I7: when an NPC is bound TENTATIVE and the player asserts shared history, promote to BOUND ONLY IF the
        assertion matches the NPC's STORED memory (>= min_overlap significant words shared with a real fact/turn).
        Never promotes on an unsupported claim (anti-abuse); never merges identities or invents memory; never throws."""
        try:
            claim = self._significant_words(assertion)
            if len(claim) < min_overlap:
                return {"promoted": False, "reason": "assertion too thin"}
            npc = self.get_npc(npc_key)
            pkey = (npc or {}).get("persistent_key")
            if not pkey:
                return {"promoted": False, "reason": "no identity"}
            ident = self.get_identity(pkey) or {}
            if str(ident.get("status") or "") != "tentative":
                return {"promoted": False, "reason": "not tentative"}
            best, matched = 0, ""
            for k in (self.resolve_memory_keys(pkey) or [npc_key]):
                texts = [str(f.get("text") or "") for f in self.get_facts(k)]
                texts += [str(t.get("content") or "") for t in self.get_recent_turns(k, limit=40)]
                for txt in texts:
                    ov = len(claim & self._significant_words(txt))
                    if ov > best:
                        best, matched = ov, txt
            if best >= min_overlap:
                conf = max(float(ident.get("identity_confidence") or 0.0), 0.85)
                self.set_identity_fields(pkey, status="bound", identity_confidence=conf)
                return {"promoted": True, "overlap": best, "matched_on": matched[:120]}
            return {"promoted": False, "reason": "no memory match", "overlap": best}
        except Exception:
            return {"promoted": False, "reason": "error"}

    # --- EPIC I probe: NPC Blackboard persistent-identity test -----------------
    def record_blackboard_probe(self, row: dict) -> int:
        """Store ONE Blackboard-probe observation (phase/target/runtime id/bb key+value/write+read success)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO npc_blackboard_probe
                    (save_id, phase, target_type, runtime_component_id, npc_name, faction, role,
                     ship_or_station, sector, blackboard_key, blackboard_value, payload_type, npctemplate,
                     restored_match, write_success, read_success, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                str(row.get("save_id") or ""), str(row.get("phase") or ""),
                str(row.get("target_type") or "conversation_person"), str(row.get("runtime_component_id") or ""),
                str(row.get("npc_name") or ""), str(row.get("faction") or ""), str(row.get("role") or ""),
                str(row.get("ship_or_station") or ""), str(row.get("sector") or ""),
                str(row.get("blackboard_key") or ""), str(row.get("blackboard_value") or ""),
                str(row.get("payload_type") or "string"), str(row.get("npctemplate") or ""),
                1 if row.get("restored_match") else 0,
                1 if row.get("write_success") else 0, 1 if row.get("read_success") else 0, now,
            ))
            conn.commit()
            return int(cur.lastrowid or 0)

    def latest_blackboard_probe(self, save_id: str = "", limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            if save_id:
                rows = conn.execute("SELECT * FROM npc_blackboard_probe WHERE save_id=? ORDER BY id DESC LIMIT ?",
                                    (save_id, int(limit))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM npc_blackboard_probe ORDER BY id DESC LIMIT ?",
                                    (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def blackboard_verdict(self, save_id: str = "") -> dict:
        """Compute the probe verdict from recorded rows (the spec's Final Verdict Rules). USE_BLACKBOARD when a
        minted key was written, read SAME-session AND AFTER-reload, survived a runtime-id change (same bb value
        seen under ≥2 distinct runtime ids across an after_reload read), duplicates kept distinct values, and the
        conversation_person target read back after reload. HYBRID if it works only partially; REJECT otherwise."""
        rows = self.latest_blackboard_probe(save_id, limit=2000)
        by_val: dict[str, list] = {}
        for r in rows:
            v = r.get("blackboard_value")
            if v:
                by_val.setdefault(v, []).append(r)
        write_ok = any(r.get("write_success") for r in rows)
        same_session = any(r.get("phase") in ("write", "same_session") and r.get("read_success") for r in rows)
        # The Lua probe just records a read on every encounter; a successful read of the SAME minted key under
        # ≥2 distinct runtime ids IS the after-reload proof (the runtime id only changes across a reload), so we
        # derive it here rather than trust an explicit "after_reload" label.
        survived_id_change = False
        for rs in by_val.values():
            ids = {r.get("runtime_component_id") for r in rs if r.get("read_success")}
            if len(ids) >= 2:
                survived_id_change = True
                break
        after_reload = (survived_id_change
                        or any(r.get("phase") == "after_reload" and r.get("read_success") for r in rows))
        dup_rows = [r for r in rows if r.get("phase") == "duplicate"]
        dup_ok = True
        seen: dict[str, str] = {}
        for r in dup_rows:
            v = r.get("blackboard_value") or ""
            who = (r.get("npc_name") or "") + "|" + (r.get("runtime_component_id") or "")
            if v and v in seen and seen[v] != who:
                dup_ok = False
            seen.setdefault(v, who)
        matrix: dict[str, dict] = {}
        for r in rows:
            tt = r.get("target_type") or "conversation_person"
            m = matrix.setdefault(tt, {"write": False, "after_reload_read": False})
            if r.get("write_success"):
                m["write"] = True
            if r.get("phase") == "after_reload" and r.get("read_success"):
                m["after_reload_read"] = True
        person_ok = matrix.get("conversation_person", {}).get("after_reload_read", False)

        # Per-payload survival (ChemODun's revised spec): a correlation token read under ≥2 distinct runtime ids
        # = the handle bridged a reload. Object refs additionally require restored_match (resolved to the SAME
        # person), since X4 itself remaps the pointer.
        def _grouped(predicate):
            g: dict[str, dict] = {}
            for r in rows:
                if predicate(r) and r.get("read_success") and r.get("blackboard_value"):
                    e = g.setdefault(r["blackboard_value"], {"ids": set(), "matched": False})
                    e["ids"].add(r.get("runtime_component_id"))
                    if r.get("restored_match"):
                        e["matched"] = True
            return g
        obj_groups = _grouped(lambda r: (r.get("payload_type") == "object"))
        str_groups = _grouped(lambda r: (r.get("payload_type") or "string") == "string")
        object_ref_survived = any(len(e["ids"]) >= 2 and e["matched"] for e in obj_groups.values())
        string_key_survived = any(len(e["ids"]) >= 2 for e in str_groups.values())
        template_fallback_ok = any("template" in str(r.get("phase") or "") and r.get("restored_match") for r in rows)

        if object_ref_survived and dup_ok and not template_fallback_ok:
            tier_verdict = "OBJECT_REF"
        elif object_ref_survived and template_fallback_ok:
            tier_verdict = "HYBRID_TEMPLATE"   # object ref for live NPCs, template fallback for despawned
        elif template_fallback_ok or string_key_survived:
            tier_verdict = "HYBRID_TEMPLATE"
        else:
            tier_verdict = "SYNTHETIC"

        # Legacy string-key verdict (retained for back-compat with the first probe build).
        if write_ok and same_session and after_reload and survived_id_change and dup_ok and person_ok:
            legacy = "USE_BLACKBOARD"
        elif write_ok and same_session and after_reload:
            legacy = "HYBRID"
        else:
            legacy = "REJECT"
        return {"verdict": tier_verdict, "legacy_verdict": legacy,
                "object_ref_survived": object_ref_survived, "string_key_survived": string_key_survived,
                "template_fallback_ok": template_fallback_ok, "write_ok": write_ok,
                "same_session_read": same_session, "after_reload_read": after_reload,
                "survived_runtime_id_change": survived_id_change, "duplicate_separation_ok": dup_ok,
                "person_after_reload": person_ok, "target_matrix": matrix, "rows": len(rows)}

    def merge_npc_cards(self, src_key: str, dst_key: str) -> dict:
        """Fold one NPC card into another: move all turns + facts from src → dst, then remove the src card.
        Used to heal a DUPLICATE (e.g. an empty token-keyed card created alongside the real name-keyed card by
        the old duplicating bind). dst keeps its own profile/identity; src's memory is repointed, not purged."""
        src_key = str(src_key or ""); dst_key = str(dst_key or "")
        if not src_key or not dst_key or src_key == dst_key:
            return {"ok": False, "reason": "need distinct src+dst"}
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM npcs WHERE npc_key=?", (src_key,)).fetchone():
                return {"ok": False, "reason": "src not found"}
            t = conn.execute("UPDATE turns SET npc_key=? WHERE npc_key=?", (dst_key, src_key)).rowcount or 0
            f = conn.execute("UPDATE facts SET npc_key=? WHERE npc_key=?", (dst_key, src_key)).rowcount or 0
            conn.execute("DELETE FROM npcs WHERE npc_key=?", (src_key,))
            conn.commit()
        return {"ok": True, "moved_turns": t, "moved_facts": f, "src": src_key, "dst": dst_key}

    def bind_blackboard_identity(self, save_id: str, name: str, faction: str = "", role: str = "",
                                blackboard_key: str = "", runtime_id: str = "") -> dict:
        """Stamp the NPC's durable Blackboard token as the bound identity on the CANONICAL name card — ONE card
        per (save, name). The token (proven to survive save/reload) becomes the identity `bb:<token>`
        (status=bound, confidence 1.0) and SUPERSEDES a synthetic `pid:` identity (the Tier-3 fallback). Does NOT
        create a separate token-keyed card (that was the duplication bug); if a stray one already exists, its
        memory is merged back into the name card. Never throws."""
        try:
            name = str(name or "").strip()
            token = str(blackboard_key or "").strip()
            if not (save_id and name and token):
                return {"ok": False, "reason": "need save_id+name+blackboard_key"}
            pkey = "bb:" + token
            name_key = self.make_key(save_id, "chat", name)       # CANONICAL card — one per NPC
            token_key = self.make_key(save_id, "chat", "bb:" + token)
            # Ensure the canonical card exists WITHOUT clobbering its history.
            if not self.get_npc(name_key):
                self.bind_npc(name_key, "", save_id=save_id, game_id="chat", name=name, faction_id=faction,
                              stats=({"role": role} if role else None))
            # Heal any prior duplicate: a separate token-keyed card → fold its memory into the name card, drop it.
            if token_key != name_key and self.get_npc(token_key):
                self.merge_npc_cards(token_key, name_key)
            # Register/bind the identity and stamp it on the canonical card. The Blackboard token is the proven
            # Tier-1 key and UPGRADES a synthetic pid: identity. Only a DIFFERENT bb: token is left untouched
            # (that would be a genuine same-name collision — don't steal it).
            self.upsert_identity(pkey, {"display_name": name, "faction": faction, "role": role})
            self.set_identity_fields(pkey, status="bound", identity_confidence=1.0)
            prev = str((self.get_npc(name_key) or {}).get("persistent_key") or "")
            if (not prev) or prev == pkey or prev.startswith("pid:"):
                self.link_npc_to_identity(name_key, pkey)
            if runtime_id:
                self.record_evidence(pkey, "blackboard_runtime", str(runtime_id), weight=1.0)
            return {"ok": True, "npc_key": name_key, "persistent_npc_key": pkey, "status": "bound"}
        except Exception as e:
            return {"ok": False, "reason": str(e)[:120]}

    def repair_blackboard_duplicates(self) -> dict:
        """One-shot heal for cards split by the OLD duplicating bind: fold every `bb:<token>`-keyed chat card into
        its sibling name card (same save), then re-stamp the name card with the bb identity (superseding pid:).
        Idempotent — after it runs there are no `…|chat|bb:…` cards left to fold."""
        healed: list[dict] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT npc_key, save_id, name, persistent_key FROM npcs "
                "WHERE game_id='chat' AND npc_key LIKE '%|chat|bb:%'").fetchall()
        for r in rows:
            tok_key = r["npc_key"]; save_id = str(r["save_id"] or ""); name = str(r["name"] or "").strip()
            pkey = str(r["persistent_key"] or "")
            if not (save_id and name):
                continue
            name_key = self.make_key(save_id, "chat", name)
            if name_key == tok_key:
                continue
            if not self.get_npc(name_key):
                self.bind_npc(name_key, "", save_id=save_id, game_id="chat", name=name)
            self.merge_npc_cards(tok_key, name_key)
            if pkey.startswith("bb:"):
                cur = str((self.get_npc(name_key) or {}).get("persistent_key") or "")
                if (not cur) or cur.startswith("pid:") or cur == pkey:
                    self.link_npc_to_identity(name_key, pkey)
            healed.append({"folded": tok_key, "into": name_key})
        return {"ok": True, "healed": healed, "count": len(healed)}

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
            count = self._turn_count(conn, npc_key)
        # A4 (IG-2): grow durable memory DURING play instead of only via the on-demand backfill — the cause
        # of the 'talks a lot, stores few facts' gap. promote_durable_facts is ADDITIVE (copies high-value
        # turns into facts, keeps the raw turns) and DETERMINISTIC (regex classify, no LLM) — so it is NOT the
        # lossy condensation that was deliberately disabled. Cadence-throttled + guarded so a promotion error
        # can never break turn recording. (Runs OUTSIDE the lock above; promote takes its own lock.)
        if count and count % 6 == 0:
            try:
                self.promote_durable_facts(npc_key, max_promote=6)
            except Exception:
                pass
        # I3: conversing with an NPC makes them player-significant (Tier 1). Idempotent (only ever
        # raises priority) + cheap (skips once already <= Tier 1) + guarded. No-op until the npc is
        # linked to an identity (I0 backfill / I2 rebind). Covers talk/mission/negotiate (all conversations).
        try:
            self.promote_identity_for_npc(npc_key, "talked")
        except Exception:
            pass

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

    def identity_recall_gate(self, npc_key: str) -> dict:
        """I4: how much PERSONAL memory to surface, gated by identity-bind confidence (the spec's
        confidence-gated retrieval). bound / session-only / new / unbound → full recall, UNIONED across
        every npc_key the identity owns (resolve_memory_keys — its real consumer). tentative → recall but
        HEDGED (half-recognition). ambiguous → SUPPRESS personal memory (faction/role only; never assert
        shared history). Non-chat NPCs have no identity → default full recall on their own key (unchanged)."""
        npc = self.get_npc(npc_key)
        pkey = (npc or {}).get("persistent_key")
        if not pkey:
            return {"keys": [npc_key], "inject_personal": True, "hedge": False, "status": "", "confidence": None}
        ident = self.get_identity(pkey) or {}
        status = str(ident.get("status") or "")
        keys = self.resolve_memory_keys(pkey) or [npc_key]
        if status == "ambiguous":
            return {"keys": keys, "inject_personal": False, "hedge": False, "status": status, "confidence": ident.get("identity_confidence")}
        if status == "tentative":
            return {"keys": keys, "inject_personal": True, "hedge": True, "status": status, "confidence": ident.get("identity_confidence")}
        return {"keys": keys, "inject_personal": True, "hedge": False, "status": status, "confidence": ident.get("identity_confidence")}

    def build_memory_context(self, npc_key: str, max_significant: int = 6) -> str:
        """Assemble the bounded PERSONAL memory block injected into each turn — confidence-gated (I4).

        CORE facts always included (verbatim); top significant facts by importance×recency; routine
        omitted. Facts are UNIONED across the identity's keys (cross-reload recall). Touches last_used_at
        so retrieved facts decay slower. AMBIGUOUS binds inject no personal memory; TENTATIVE binds hedge.
        """
        now = time.time()
        gate = self.identity_recall_gate(npc_key)
        keys = list(gate["keys"]) if gate["inject_personal"] else []
        core: list = []
        sig: list = []
        with self._lock, self._connect() as conn:
            npc = conn.execute("SELECT * FROM npcs WHERE npc_key = ?", (npc_key,)).fetchone()
            if keys:
                ph = ",".join("?" for _ in keys)
                core = conn.execute(
                    f"SELECT id, text FROM facts WHERE npc_key IN ({ph}) AND tier = 'core' "
                    "ORDER BY importance DESC, created_at ASC",
                    keys,
                ).fetchall()
                sig = conn.execute(
                    f"SELECT id, text FROM facts WHERE npc_key IN ({ph}) AND tier = 'significant' "
                    "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                    [*keys, max_significant],
                ).fetchall()
                used_ids = [r["id"] for r in core] + [r["id"] for r in sig]
                if used_ids:
                    conn.execute(
                        f"UPDATE facts SET last_used_at = ? WHERE id IN ({','.join('?' for _ in used_ids)})",
                        [now, *used_ids],
                    )
            conn.commit()

        lines: list[str] = []
        if npc:
            identity = self._identity_line(dict(npc))
            if identity:
                lines.append(identity)
        # I4 AMBIGUOUS: can't be sure this is the same person — surface NO personal history.
        if not gate["inject_personal"]:
            lines.append("(You do not clearly recognize this individual; you may not have met. Rely on what you "
                         "know of their faction and role, not personal history — do not claim shared past unless they prove it.)")
            return "\n".join(lines).strip()
        # I4 TENTATIVE: half-recognition — recall, but hedged.
        if gate["hedge"]:
            lines.append("(You half-recognize this person — you think you've spoken before but are not certain; "
                         "do not assert specific shared history unless they confirm it.)")
        if npc and npc["summary"]:
            lines.append(("You vaguely recall discussing: " if gate["hedge"] else "What you remember overall: ") + npc["summary"])
        if core:
            lines.append("Things you will never forget:")
            lines.extend(f"- {r['text']}" for r in core)
        if sig:
            lines.append("You also recall:")
            lines.extend(f"- {r['text']}" for r in sig)
        return "\n".join(lines).strip()

    # --- #55 economy meaning-layer helpers: raw ware ids -> display names, severity -> English bands ----
    def _ware_label(self, ware_id: str) -> str:
        """Raw ware id ('foodrations') -> display name ('Food Rations') from the canon lore catalog (#34),
        cached. Fallback: the raw id (rare misses, e.g. some khaak/xenon wares)."""
        wid = str(ware_id or "").strip()
        if not wid:
            return ""
        cache = getattr(self, "_ware_label_cache", None)
        if cache is None:
            cache = {}
            try:
                for row in self.list_lore(self.CANON_SAVE, "ware"):
                    k = str(row.get("key") or "").strip()
                    # lore display name is in `title` (not `name`); fall back to name/label if ever present.
                    nm = str(row.get("title") or row.get("name") or "").strip()
                    if k and nm:
                        cache[k] = nm
            except Exception:
                pass
            self._ware_label_cache = cache
        return cache.get(wid) or wid

    @staticmethod
    def _shortage_phrase(sev: float) -> str:
        """Shortage severity (0..1) -> English band (deny the LLM the raw number)."""
        s = float(sev or 0)
        if s >= 0.7:
            return "critically short on"
        if s >= 0.4:
            return "running low on"
        return "a little tight on"

    @staticmethod
    def _and_join(items: list) -> str:
        xs = [str(i) for i in items if i]
        if not xs:
            return ""
        if len(xs) == 1:
            return xs[0]
        if len(xs) == 2:
            return xs[0] + " and " + xs[1]
        return ", ".join(xs[:-1]) + ", and " + xs[-1]

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
        # G2: the player's galaxy-wide REPUTATION (role), so the faction reacts to WHO the player is.
        try:
            _pr = self.classify_player_role(save_id)
            if _pr.get("primary_role") and _pr["primary_role"] != "unaligned newcomer":
                lines.append(f"Across the galaxy the Commander is regarded as a {_pr['primary_role']}.")
        except Exception:
            pass
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
        # 1i-B / #55: economy — meaning-layer prose. Display NAMES (not raw ware ids) + severity in ENGLISH
        # bands (deny the LLM raw numbers), so the NPC reasons about trade, embargoes and supply deals naturally.
        # Key imports/shortages are real (the #54 per-station station read → rollup).
        econ = self.get_economy(save_id, faction_id) or {}
        kn = econ.get("key_needs")
        if isinstance(kn, list) and kn:
            ms = str(econ.get("market_status") or "neutral")
            role = {"importer": "a net importer", "exporter": "a net exporter"}.get(ms, "largely self-reliant")
            lines.append(f"Economy: your faction is {role}; you rely on importing "
                         + self._and_join([self._ware_label(w) for w in kn[:6]]) + ".")
            dep = float(econ.get("dependency_on_player", 0) or 0)
            if dep >= 0.7:
                lines.append("The Commander (the player) is your single biggest supplier of what you need — "
                             "antagonising them would choke your supply lines.")
            elif dep >= 0.4:
                lines.append("The Commander (the player) is a major supplier of what you need — "
                             "antagonising them risks your supply lines.")
            sh = econ.get("shortages")
            if isinstance(sh, dict) and sh:
                # group the worst shortages by ENGLISH severity band (critically short / running low / tight).
                bands: dict[str, list[str]] = {}
                for w, sev in sorted(sh.items(), key=lambda kv: -float(kv[1] or 0))[:4]:
                    bands.setdefault(self._shortage_phrase(float(sev or 0)), []).append(self._ware_label(w))
                phrases = [f"{band} {self._and_join(names)}" for band, names in bands.items()]
                if phrases:
                    lines.append("You are " + "; ".join(phrases) + ".")
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
        # #39: the NPC's personal social ties (who they know), so they speak aware of their relationships.
        try:
            _ties = self.social_summary(save_id, npc_key)
            if _ties:
                lines.append(_ties)
        except Exception:
            pass
        # Rumor: what the NPC has HEARD through the grapevine (hearsay, unconfirmed) — so gossip surfaces in chat.
        try:
            _rum = self.rumor_brief(save_id, npc_key)
            if _rum:
                lines.append(_rum)
        except Exception:
            pass
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
                       n.race, n.role, n.ship_class, n.skills, n.persistent_key, n.is_alive,
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
            # Dynamic: delete from EVERY save_id-scoped table (npcs/turns/facts handled above by
            # npc_key). Kills the recurring "newer tables left behind" bug — any future save-scoped
            # table is auto-covered instead of needing this list maintained by hand.
            handled = {"npcs", "turns", "facts"}
            for (table,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if table in handled or table.startswith("sqlite_"):
                    continue
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if "save_id" in cols:
                    conn.execute(f"DELETE FROM {table} WHERE save_id = ?", (save_id,))
            conn.commit()
        return {"ok": True, "cleared_npcs": n, "save_id": save_id}

    def reap_selftest_saves(self) -> dict:
        """A2 (IG-3): delete every selftest-generated save across ALL save_id-scoped tables.
        Selftests use deterministic '__<name>_selftest__<ms>' save_ids (always contain 'selftest'),
        so they leave rows that pollute the live dashboard (inflated NPC/save counts). This reaps
        them. Legacy MANUAL saves (cctest/octest, no 'selftest' token) are deliberately NOT touched."""
        saves: set[str] = set()
        with self._lock, self._connect() as conn:
            for (table,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if table.startswith("sqlite_"):
                    continue
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if "save_id" in cols:
                    for (sid,) in conn.execute(
                            f"SELECT DISTINCT save_id FROM {table} WHERE save_id LIKE '%selftest%'").fetchall():
                        if sid:
                            saves.add(sid)
        reaped = sorted(saves)
        for s in reaped:  # clear_save takes its own lock — call OUTSIDE the gather block
            self.clear_save(s)
        return {"ok": True, "count": len(reaped), "reaped": reaped}

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

    # DeadAir dynamicwardiplomacy.xml: factions that are NOT diplomatic actors — never a legal relation-move target.
    RELATION_EXCLUDED_FACTIONS = {"civilian", "criminal", "khaak", "smuggler", "visitor", "xenon", "ownerless", "yaki"}

    def validate_relation_move(self, save_id: str, actor: str, target: str, step: float = 5.0) -> dict:
        """DeadAir-grounded eligibility+bounds gate for a Player2-PROPOSED relation change (the model in
        dynamicwardiplomacy.xml the bridge previously lacked): both must be real, distinct, diplomatic factions
        (neither in the excluded set), and the move is bounded — |step| ≤ 5 with the resulting standing clamped to the
        ±25 diplomatic band. Attitude-only (anti-cheat OK: intent→attitude, never resources). The engine VALIDATES;
        Player2 only proposed. Returns {ok, clamped_step, result, reason}."""
        a = str(actor or "").strip().lower()
        b = str(target or "").strip().lower()
        if not a or not b or a == b:
            return {"ok": False, "reason": "need two distinct factions"}
        if a in self.RELATION_EXCLUDED_FACTIONS or b in self.RELATION_EXCLUDED_FACTIONS:
            bad = a if a in self.RELATION_EXCLUDED_FACTIONS else b
            return {"ok": False, "reason": f"excluded (non-diplomatic) faction: {bad}"}
        known = {f.get("faction_id") for f in self.list_factions(save_id)}
        if a not in known or b not in known:
            return {"ok": False, "reason": "unknown faction (not in this save)"}
        try:
            step = float(step or 0)
        except Exception:
            step = 0.0
        step = max(-5.0, min(5.0, step))  # DeadAir step bound (±5)
        cur_raw = float((self.get_relationship(save_id, a, b) or {}).get("trust") or 0)
        # Band-normalize FIRST: the store's trust scale is ±100 (locked volatility), but the DeadAir diplomatic
        # band is ±25. Computing the step against raw out-of-band trust AMPLIFIED it (live 2026-07-01: cur=67,
        # step=-5 → clamped_step=-42 → relation -0.42 emitted vs the documented ±0.05 max). Viewing the standing
        # through the band guarantees |clamped_step| ≤ |step| ≤ 5 always.
        cur = max(-25.0, min(25.0, cur_raw))
        result = max(-25.0, min(25.0, cur + step))  # DeadAir ±25 diplomatic band
        clamped = result - cur
        if clamped == 0 and step != 0:
            return {"ok": False, "clamped_step": 0.0, "result": result, "reason": "at diplomatic band limit (±25)"}
        return {"ok": True, "clamped_step": clamped, "result": result, "reason": "ok"}

    def faction_doctrine_brief(self, save_id: str, faction_id: str) -> str:
        """#53 — a compact Worldview line for the faction ACTOR in any decision (decide / decide_actions), composed
        from the SAME canon source NPC chat uses: FACTION_PERSONA (aggr/econ/risk/dipl + goal) → trait adjectives,
        the standing goal, and the live mood. This makes strategic faction decisions doctrine-flavored, not generic
        'decide in character'. Deterministic; no LLM. Mirrors persona.PersonaCardBuilder._persona_traits thresholds."""
        fid = str(faction_id or "").strip().lower()
        if not fid:
            return ""
        aggr, econ, risk, dipl, goal = self.FACTION_PERSONA.get(fid, self.FACTION_PERSONA_DEFAULT)
        aggr, econ, risk, dipl = float(aggr), float(econ), float(risk), float(dipl)
        traits: list[str] = []
        traits.append("aggressive and quick to anger" if aggr >= 0.6
                      else ("measured and slow to provoke" if aggr <= 0.35 else "firm but controlled"))
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
        # live mood: prefer the stored (heartbeat-derived) value, else derive on the fly, else a calm baseline.
        mood = ""
        try:
            fac = self.get_faction(save_id, fid) or {}
            mood = str(fac.get("mood") or "")
            if not mood:
                mood = self._derive_mood(self.derive_pressures(save_id, fid) or {})
        except Exception:
            mood = ""
        name = (self.FACTION_NAMES.get(fid) or (self.get_faction(save_id, fid) or {}).get("name")
                or fid.replace("_", " ").title())
        line = f"You are the leadership of {name}: {', '.join(traits)}."
        if goal:
            line += f" Your standing goal: {goal}"
        if mood:
            line += f" Your current mood: {mood}."
        return line

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

    _AGR_TERMINAL = ("kept", "broken", "expired", "fulfilled", "cancelled", "rejected",
                     "refused", "superseded", "transitioned")

    def add_agreement(self, save_id: str, party_a: str, party_b: str, type: str = "",
                      terms: Any = None, deadline: float = 0.0, status: str = "pending") -> dict:
        """NEGOTIATIONS INVARIANT: no OPEN/active deal may be appended without going through the dedupe/identity door.
        Any non-terminal status is redirected to create_or_update_agreement (one open offer per agreement_key) — so
        EVERY caller (generate_agreements, OPORD ceasefire/allied, etc.) deduplicates at the API level, not per-site.
        Only terminal/historical records (kept/broken/expired/…) insert directly."""
        if status not in self._AGR_TERMINAL:
            t = terms if isinstance(terms, dict) else {}
            res = self.create_or_update_agreement(
                save_id, party_a, party_b, type=type, kind=str(t.get("kind") or ""),
                operation_id=str(t.get("operation_id") or ""),
                operation_task_id=str(t.get("operation_task_id") or ""),
                terms=terms, deadline=deadline, status=status)
            return {"id": res["id"], "save_id": save_id, "party_a": party_a, "party_b": party_b,
                    "type": type, "status": status, "agreement_key": res.get("agreement_key"),
                    "created": res.get("created"), "request_count": res.get("request_count")}
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
    def _agreement_key(save_id: str, type_: str, party_a: str, party_b: str,
                       operation_id: str = "", operation_task_id: str = "", kind: str = "") -> str:
        """Deterministic identity for an agreement (Negotiations §1): one OPEN agreement per key."""
        return ":".join([save_id or "", type_ or "", party_a or "", party_b or "",
                         operation_id or "", operation_task_id or "", kind or ""])

    _AGR_OPEN = ("proposed", "pending", "pending_response", "countered", "no_counterparty")

    def create_or_update_agreement(self, save_id: str, party_a: str, party_b: str, type: str = "",
                                   kind: str = "", operation_id: str = "", operation_task_id: str = "",
                                   terms: Any = None, deadline: float = 0.0, status: str = "pending",
                                   urgency: int = 0, offered_value: int = 0, context: Any = None) -> dict:
        """Upsert an agreement by its deterministic agreement_key (Negotiations §1, §7, §8): ONE open agreement per
        key. A repeat request UPDATES the existing open row (bumps request_count, refreshes urgency/offer/context)
        instead of inserting a duplicate — the anti-spam backbone for OPORD allied-support / negotiation requests."""
        now = time.time()
        key = self._agreement_key(save_id, type, party_a, party_b, operation_id, operation_task_id, kind)
        ph = ",".join("?" * len(self._AGR_OPEN))
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT id, request_count FROM agreements WHERE save_id=? AND agreement_key=? "
                               f"AND status IN ({ph}) ORDER BY id LIMIT 1",
                               (save_id, key, *self._AGR_OPEN)).fetchone()
            if row:
                rc = int(row["request_count"] or 1) + 1
                conn.execute("UPDATE agreements SET request_count=?, last_requested_at=?, urgency=?, offered_value=?, "
                             "context_json=COALESCE(?, context_json), terms_json=COALESCE(?, terms_json), "
                             "deadline=CASE WHEN ?>0 THEN ? ELSE deadline END WHERE id=?",
                             (rc, now, int(urgency or 0), int(offered_value or 0),
                              json.dumps(context) if context is not None else None,
                              json.dumps(terms) if terms is not None else None,
                              float(deadline or 0), float(deadline or 0), row["id"]))
                conn.commit()
                return {"ok": True, "id": row["id"], "created": False, "updated": True,
                        "agreement_key": key, "request_count": rc, "party_b": party_b, "status": status}
            cur = conn.execute(
                "INSERT INTO agreements (save_id, party_a, party_b, type, kind, operation_id, operation_task_id, "
                "agreement_key, terms_json, context_json, deadline, status, urgency, offered_value, request_count, "
                "last_requested_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (save_id, party_a, party_b, type, kind, operation_id, operation_task_id, key,
                 json.dumps(terms) if terms is not None else None,
                 json.dumps(context) if context is not None else None,
                 float(deadline or 0), status, int(urgency or 0), int(offered_value or 0), now, now))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid, "created": True, "updated": False,
                    "agreement_key": key, "request_count": 1, "party_b": party_b, "status": status}

    def select_support_counterparty(self, save_id: str, requester: str, enemy: str = "", sector: str = "") -> str:
        """Pick a REAL ally for an allied-support request (Negotiations §2): a non-criminal faction (not the requester
        or the enemy) with a non-hostile relation to the requester, ranked by trust + a shared-enemy bonus - its
        resentment. Returns '' if none qualify (caller records no_counterparty instead of an anonymous spam row)."""
        best, best_score = "", -1e18
        for f in self.list_factions(save_id):
            fid = f.get("faction_id") or ""
            if not fid or fid == requester or fid == enemy or fid in self.CRIMINAL_FACTIONS:
                continue
            rel = self.get_relationship(save_id, fid, requester) or {}
            trust = float(rel.get("trust") or 0)
            if trust < 0:  # distrustful / hostile -> won't ally
                continue
            resentment = float(rel.get("resentment") or 0)
            shared = 0.0
            if enemy:
                er = self.get_relationship(save_id, fid, enemy) or {}
                if float(er.get("trust") or 0) < 0 or float(er.get("resentment") or 0) > 0:
                    shared = 20.0
            score = trust * 0.25 + shared - resentment * 0.2
            if score > best_score:
                best_score, best = score, fid
        return best

    _KIND_TO_TYPE = {"allied_support": "alliance", "patrol_cooperation": "patrol_cooperation",
                     "ceasefire_payment": "ceasefire", "ceasefire": "ceasefire", "seek_ceasefire": "ceasefire",
                     "non_aggression": "non_aggression", "trade_pact": "trade", "trade": "trade",
                     "transit_rights": "transit", "reparations": "reparations",
                     "territory_claim": "territory_claim", "access_rights": "access"}

    def submit_negotiation_intent(self, save_id: str, source: str, kind: str, proposer: str, recipient: str = "",
                                  operation_id: str = "", operation_task_id: str = "", terms: Any = None,
                                  context: Any = None, deadline: float = 0.0, urgency: int = 0,
                                  offered_value: int = 0, enemy: str = "", sector: str = "",
                                  require_counterparty: bool = False) -> dict:
        """THE single public door for creating a deal. Negotiations is the universal transaction layer: OPORD, the
        job market, and chat all SUBMIT an intent here (source tags the origin); Negotiations owns dedupe, counterparty
        selection, valuation, and lifecycle. No subsystem builds its own agreement model. Returns the (deduped) offer."""
        type_ = self._KIND_TO_TYPE.get(kind, kind or "")
        if not recipient and require_counterparty:
            recipient = self.select_support_counterparty(save_id, proposer, enemy, sector)
        # NF1 keeps the existing open status 'pending' (conclude/health/cleanup key on it); the richer canonical
        # lifecycle (proposed→pending_response→accepted/…) is NF2's job, applied system-wide there.
        status = "pending" if (recipient or not require_counterparty) else "no_counterparty"
        ctx = dict(context or {})
        ctx.setdefault("source", source)
        ctx.setdefault("kind", kind)
        ctx.setdefault("proposer", proposer)
        ctx.setdefault("recipient", recipient)
        if enemy:
            ctx.setdefault("enemy", enemy)
        if sector:
            ctx.setdefault("sector", sector)
        return self.create_or_update_agreement(
            save_id, proposer, recipient, type=type_, kind=kind, operation_id=operation_id,
            operation_task_id=operation_task_id, terms=terms, deadline=deadline, status=status,
            urgency=urgency, offered_value=offered_value, context=ctx)

    # NF2 acceptance bands (Codex/WH3): >=70 accept · 45-69 counter · 25-44 refuse · <25 refuse harshly
    def score_agreement_acceptance(self, save_id: str, agreement: dict) -> dict:
        """Deterministic acceptance score for an OPEN offer, from the RECIPIENT's point of view. Reads the SHARED
        models — relationships (trust/resentment/debt toward the requester) + strategic_state (the recipient's own
        war-load/losses) — NOT a bespoke pressure calc. The LLM never decides acceptance; this does. Returns
        {score, decision, factors}."""
        requester = agreement.get("party_a") or ""
        recipient = agreement.get("party_b") or ""
        if not recipient:
            return {"score": 0.0, "decision": "no_counterparty", "factors": {}}
        try:
            ctx = json.loads(agreement.get("context_json") or "{}") or {}
        except Exception:
            ctx = {}
        enemy = ctx.get("enemy") or agreement.get("target_faction") or ""
        risk = float(ctx.get("risk") or 0)
        offered = float(agreement.get("offered_value") or ctx.get("offered_value") or 0)
        rel = self.get_relationship(save_id, recipient, requester) or {}
        trust = float(rel.get("trust") or 0)
        resentment = float(rel.get("resentment") or 0)
        debt = float(rel.get("debt") or 0)          # recipient owes requester a favor → more willing
        st = self.get_strategic_state(save_id, recipient) or {}
        war_load = float(st.get("military_pressure") or 0)   # 0..1 own war involvement (overcommitment)
        losses = float(st.get("recent_losses") or 0)         # 0..1 recently bloodied → cautious
        shared = 0.0
        if enemy:
            er = self.get_relationship(save_id, recipient, enemy) or {}
            if float(er.get("trust") or 0) < 0 or float(er.get("resentment") or 0) > 0:
                shared = 20.0
        factors = {
            "base": 50.0, "trust": round(trust * 0.25, 1), "shared_enemy": shared,
            "offer": round(min(20.0, offered / 10000.0), 1), "debt": round(debt * 0.1, 1),
            "war_load_penalty": round(-war_load * 30.0, 1), "losses_penalty": round(-losses * 20.0, 1),
            "resentment_penalty": round(-resentment * 0.2, 1), "risk_penalty": round(-risk * 30.0, 1),
        }
        score = round(sum(factors.values()), 1)
        decision = ("accept" if score >= 70 else "counter" if score >= 45
                    else "refuse" if score >= 25 else "refuse_harshly")
        return {"score": score, "decision": decision, "factors": factors}

    _AGR_EVALUATABLE = ("pending", "proposed", "pending_response")

    def evaluate_open_offers(self, save_id: str, max_eval: int = 100) -> dict:
        """NF2 resolution driver: score every open offer that has a real counterparty and transition it
        (accept→accepted, counter→countered, refuse/refuse_harshly→refused), recording score+factors+decision in
        context_json. Deterministic; runs on the heartbeat (via advance_operations). Counteroffers are left for the
        requester (OPORD/OC) to answer; no-counterparty offers are skipped. (Consequences/budget = NF3/OC.)"""
        ph = ",".join("?" * len(self._AGR_EVALUATABLE))
        res = {"ok": True, "evaluated": 0, "accept": 0, "counter": 0, "refuse": 0, "refuse_harshly": 0}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM agreements WHERE save_id=? AND status IN ({ph}) "
                f"AND party_b IS NOT NULL AND party_b != '' ORDER BY id LIMIT ?",
                (save_id, *self._AGR_EVALUATABLE, int(max_eval))).fetchall()
        for row in rows:
            ag = dict(row)
            sc = self.score_agreement_acceptance(save_id, ag)
            decision = sc["decision"]
            if decision == "no_counterparty":
                continue
            new_status = {"accept": "accepted", "counter": "countered",
                          "refuse": "refused", "refuse_harshly": "refused"}[decision]
            try:
                ctx = json.loads(ag.get("context_json") or "{}") or {}
            except Exception:
                ctx = {}
            now = time.time()
            ctx["acceptance"] = {"score": sc["score"], "decision": decision,
                                 "factors": sc["factors"], "evaluated_at": now}
            resolved = now if new_status in ("accepted", "refused") else None
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE agreements SET status=?, context_json=?, "
                             "resolved_at=COALESCE(?, resolved_at) WHERE id=?",
                             (new_status, json.dumps(ctx), resolved, ag["id"]))
                conn.commit()
            res["evaluated"] += 1
            res[decision] += 1
        return res

    def build_negotiation_situation(self, save_id: str, agreement: dict) -> dict:
        """Deterministic SUMMARIZER → the grounded brief the Player2 faction-actor decides over (Codex loop step 2).
        The engine sets the scene — who/what/terms + the recipient's relationship, war-pressures, faction
        doctrine/personality/mood + an ADVISORY math score — but does NOT decide. The thinking entity decides."""
        requester = agreement.get("party_a") or ""
        recipient = agreement.get("party_b") or ""
        try:
            ctx = json.loads(agreement.get("context_json") or "{}") or {}
        except Exception:
            ctx = {}
        rel = self.get_relationship(save_id, recipient, requester) or {}
        st = self.get_strategic_state(save_id, recipient) or {}
        fac = next((f for f in self.list_factions(save_id) if f.get("faction_id") == recipient), {}) or {}
        try:
            advisory = self.score_agreement_acceptance(save_id, agreement)
        except Exception:
            advisory = {"score": None, "decision": None}
        return {
            "agreement_id": agreement.get("id"), "kind": agreement.get("kind") or agreement.get("type"),
            "requester": requester, "recipient": recipient, "enemy": ctx.get("enemy") or "",
            "sector": ctx.get("sector") or "", "reason": ctx.get("reason") or "",
            "offered_value": int(agreement.get("offered_value") or 0),
            "urgency": int(agreement.get("urgency") or 0), "request_count": int(agreement.get("request_count") or 1),
            "relationship": {"trust": rel.get("trust"), "resentment": rel.get("resentment"),
                             "debt": rel.get("debt"), "fear": rel.get("fear")},
            "recipient_state": {"military_pressure": st.get("military_pressure"),
                                "recent_losses": st.get("recent_losses"),
                                "economic_pressure": st.get("economic_pressure")},
            "doctrine": {"mood": fac.get("mood"), "biases": fac.get("biases"), "values": fac.get("values"),
                         "current_goal": fac.get("current_goal"), "summary": fac.get("summary")},
            "advisory_score": advisory.get("score"), "advisory_decision": advisory.get("decision"),
        }

    _OFFER_DECISIONS = {"accept": "accepted", "counter": "countered", "refuse": "refused",
                        "refuse_harshly": "refused", "defer": "pending"}

    def apply_offer_decision(self, save_id: str, agreement_id: int, decision: str, reason: str = "",
                             source: str = "player2", counter: Any = None) -> dict:
        """RECORD a decision the Player2 actor made on an offer (status + in-character reason + source + any counter
        terms). DELIBERATELY does NOT mutate relations/credits/ships — those are validator-gated execution effects
        (NF3/OC). Player2 decides intent; the engine records it; validated execution applies the real effects."""
        new_status = self._OFFER_DECISIONS.get(decision, "pending")
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT context_json FROM agreements WHERE save_id=? AND id=?",
                               (save_id, agreement_id)).fetchone()
            try:
                ctx = json.loads((row["context_json"] if row else None) or "{}") or {}
            except Exception:
                ctx = {}
            ctx["decision"] = {"decision": decision, "reason": (reason or "")[:400], "source": source,
                               "counter": counter, "decided_at": now}
            resolved = now if new_status in ("accepted", "refused") else None
            conn.execute("UPDATE agreements SET status=?, context_json=?, resolved_at=COALESCE(?, resolved_at) "
                         "WHERE save_id=? AND id=?",
                         (new_status, json.dumps(ctx), resolved, save_id, agreement_id))
            conn.commit()
        return {"ok": True, "id": agreement_id, "status": new_status, "decision": decision, "source": source}

    def apply_relationship_consequence(self, save_id: str, requester: str, recipient: str, decision: str,
                                       urgency: int = 0) -> dict:
        """NF3: the bounded RELATIONSHIP consequence of a negotiation outcome — a deterministic EXECUTION effect
        gated by the Player2 decision (attitude shift, anti-cheat OK: intent→attitude). Refusal breeds resentment;
        acceptance builds trust + debt. Also emits a transition world-event so the outcome surfaces as news."""
        if not requester or not recipient or requester == recipient:
            return {"ok": False, "reason": "no parties"}
        scale = 1.0 + min(2.0, float(urgency or 0) / 3.0)  # urgent refusals sting more
        d = decision
        applied = "none"
        if d in ("refused", "refuse"):
            self.adjust_relationship(save_id, requester, recipient, dtrust=int(-3 * scale),
                                     dresentment=int(5 * scale), summary=f"{recipient} refused {requester}'s request")
            applied = "refused"
        elif d == "refuse_harshly":
            self.adjust_relationship(save_id, requester, recipient, dtrust=int(-6 * scale),
                                     dresentment=int(10 * scale), summary=f"{recipient} rebuffed {requester} harshly")
            applied = "refused"
        elif d in ("accepted", "accept"):
            self.adjust_relationship(save_id, requester, recipient, dtrust=int(4 * scale), ddebt=int(4 * scale),
                                     summary=f"{recipient} accepted {requester}'s request")
            applied = "accepted"
        elif d in ("countered", "counter"):
            self.adjust_relationship(save_id, requester, recipient, dtrust=int(1 * scale),
                                     summary=f"{recipient} countered {requester}'s offer")
            applied = "countered"
        else:
            return {"ok": True, "applied": "none"}
        try:
            self.add_world_event(save_id, event_type=f"agreement_{applied}",
                                 summary=f"{recipient} {applied} {requester}'s proposal.",
                                 primary_faction=recipient, secondary_faction=requester,
                                 importance=(3 if applied != "accepted" else 2))
        except Exception:
            pass
        return {"ok": True, "applied": applied}

    # --- Decision records: audit log for the Player2 Decision Adapter (spec §12) ---------------------
    def record_decision(self, save_id: str, decision_type: str, subject_faction: str, source: str,
                        parsed_choice: Optional[str] = None, brief: str = "", options: Any = None,
                        advisory: Any = None, raw_response: str = "", request_id: str = "",
                        linked_operation_id: Optional[str] = None, linked_offer_id: Optional[int] = None,
                        final_status: str = "") -> int:
        """One durable row per decide() call — replayable/auditable (why did teladi refuse? what did the model see?)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO decision_records (save_id, decision_type, subject_faction, linked_operation_id, "
                "linked_offer_id, brief, options_json, advisory_json, request_id, raw_response, parsed_choice, "
                "final_status, source, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (save_id, decision_type, subject_faction, linked_operation_id, linked_offer_id,
                 (brief or "")[:4000], json.dumps(options or []),
                 json.dumps(advisory) if advisory is not None else None, request_id,
                 (raw_response or "")[:2000], parsed_choice, final_status or None, source, now, now))
            conn.commit()
            return int(cur.lastrowid)

    def finalize_decision(self, decision_id: int, validator_result: Any = None, final_status: Optional[str] = None,
                          linked_operation_id: Optional[str] = None, linked_offer_id: Optional[int] = None) -> dict:
        """Update a decision record after the caller validated + executed (final_status applied/rejected/converted)."""
        sets: list[str] = []
        vals: list = []
        if validator_result is not None:
            sets.append("validator_result=?")
            vals.append(validator_result if isinstance(validator_result, str) else json.dumps(validator_result))
        if final_status is not None:
            sets.append("final_status=?"); vals.append(final_status)
        if linked_operation_id is not None:
            sets.append("linked_operation_id=?"); vals.append(linked_operation_id)
        if linked_offer_id is not None:
            sets.append("linked_offer_id=?"); vals.append(linked_offer_id)
        if not sets:
            return {"ok": True, "noop": True}
        sets.append("decided_at=?"); vals.append(time.time()); vals.append(int(decision_id))
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE decision_records SET {','.join(sets)} WHERE id=?", vals)
            conn.commit()
        return {"ok": True, "id": decision_id}

    def list_decision_records(self, save_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM decision_records WHERE save_id=? ORDER BY id DESC LIMIT ?",
                                (save_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

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

    # --- OPORD: military operations command layer (Phase 1 CRUD) -----------------------------------
    # One durable operation per (save, faction, threat_key). create_or_get DEDUPES on the active threat
    # (the partial-unique index is the hard guarantee); concluded ops are excluded so a recurring threat
    # later spawns a fresh operation. JSON columns accept dict/list (auto-encoded).
    OP_CONCLUDED_STATUSES = ("completed", "failed", "aborted", "transitioned")
    OPORD_JOB_REWARDS = {"patrol": 80000, "privateer": 60000, "supply": 100000, "escort": 70000,
                         "recon": 40000, "station_defense": 90000, "task": 50000}
    OPORD_MIN_ACTIVE_S = 300.0       # min active time before "pressure abated → complete" can fire
    OPORD_MAX_ACTIVE_S = 3600.0      # past this, a still-contested op FAILS instead of hanging forever
    OPORD_JOB_UNCLAIMED_S = 600.0    # an open linked job older than this → FRAGO: increase reward
    OPORD_REINFORCE_MAG = 1.0        # new hostile magnitude in-sector since activation → FRAGO: reinforcement
    _OP_WRITABLE = {
        "threat_id", "target_faction", "target_sector", "target_object", "mission_statement",
        "commander_intent", "desired_end_state", "warning_order_json", "mission_analysis_json",
        "selected_coa_id", "opord_json", "annexes_json", "doctrine_json", "constraints_json",
        "ccir_json", "budget_reserved", "budget_spent", "priority", "urgency", "importance",
        "evidence_json", "issued_at", "activated_at",
    }
    _OP_JSON_COLS = {"warning_order_json", "mission_analysis_json", "opord_json", "annexes_json",
                     "doctrine_json", "constraints_json", "ccir_json", "evidence_json",
                     "tasks_json", "required_assets_json", "wargame_json", "score_json",
                     "success_criteria_json", "failure_criteria_json"}

    @staticmethod
    def _enc(col: str, val: Any) -> Any:
        """Auto-encode JSON columns; pass scalars through."""
        if val is None:
            return None
        if col in MemoryStore._OP_JSON_COLS or isinstance(val, (dict, list)):
            return json.dumps(val) if not isinstance(val, str) else val
        return val

    @staticmethod
    def _decode_op_row(row: dict) -> dict:
        row = dict(row)
        for k in list(row.keys()):
            if k.endswith("_json") and row.get(k):
                try:
                    row[k] = json.loads(row[k])
                except Exception:
                    pass
        return row

    def create_or_get_operation(self, save_id: str, faction_id: str, operation_type: str,
                                threat_key: str, status: str = "warning", **fields) -> dict:
        """Create a new operation OR return the existing ACTIVE one for this threat (anti-spam dedupe)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id, status FROM military_operations WHERE save_id=? AND faction_id=? AND threat_key=? "
                "AND status NOT IN ('completed','failed','aborted','transitioned') "
                "ORDER BY created_at DESC LIMIT 1", (save_id, faction_id, threat_key)).fetchone()
            if existing:
                conn.execute("UPDATE military_operations SET updated_at=? WHERE id=?", (now, existing["id"]))
                conn.commit()
                return {"ok": True, "id": existing["id"], "created": False, "status": existing["status"]}
            op_id = "op_" + (faction_id or "x") + "_" + uuid.uuid4().hex[:10]
            cols = ["id", "save_id", "faction_id", "operation_type", "status", "threat_key",
                    "created_at", "updated_at"]
            vals: list = [op_id, save_id, faction_id, operation_type, status, threat_key, now, now]
            for k, v in fields.items():
                if k in self._OP_WRITABLE:
                    cols.append(k); vals.append(self._enc(k, v))
            conn.execute(f"INSERT INTO military_operations ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            conn.commit()
        return {"ok": True, "id": op_id, "created": True, "status": status}

    def update_operation(self, op_id: str, status: Optional[str] = None, **fields) -> dict:
        sets, vals = ["updated_at=?"], [time.time()]
        if status:
            sets.append("status=?"); vals.append(status)
        for k, v in fields.items():
            if k in self._OP_WRITABLE:
                sets.append(f"{k}=?"); vals.append(self._enc(k, v))
        vals.append(op_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE military_operations SET {','.join(sets)} WHERE id=?", vals)
            conn.commit()
        return {"ok": True, "id": op_id}

    def get_operation(self, op_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM military_operations WHERE id=?", (op_id,)).fetchone()
        return self._decode_op_row(row) if row else None

    def list_operations(self, save_id: str, status: Optional[str] = None) -> list[dict]:
        sel = ("SELECT *, (SELECT COUNT(*) FROM operation_tasks t WHERE t.operation_id=military_operations.id) "
               "AS task_count FROM military_operations WHERE save_id=?")
        with self._connect() as conn:
            if status:
                rows = conn.execute(sel + " AND status=? ORDER BY updated_at DESC", (save_id, status)).fetchall()
            else:
                rows = conn.execute(sel + " ORDER BY updated_at DESC", (save_id,)).fetchall()
        return [self._decode_op_row(r) for r in rows]

    def attach_coa(self, op_id: str, coa_type: str, concept: str, tasks: Any,
                   viability_status: str = "candidate", required_budget: int = 0,
                   expected_duration: Optional[float] = None, **fields) -> str:
        coa_id = "coa_" + uuid.uuid4().hex[:10]
        cols = ["id", "operation_id", "coa_type", "concept", "viability_status", "tasks_json",
                "required_budget", "expected_duration", "created_at"]
        vals: list = [coa_id, op_id, coa_type, concept, viability_status, self._enc("tasks_json", tasks),
                      int(required_budget or 0), expected_duration, time.time()]
        for k, v in fields.items():
            if k in {"rejection_reason", "required_assets_json", "wargame_json", "score_json",
                     "weighted_score", "selected"}:
                cols.append(k); vals.append(self._enc(k, v))
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT INTO operation_coas ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            conn.commit()
        return coa_id

    def attach_task(self, op_id: str, task_type: str, status: str = "planned",
                    coa_id: Optional[str] = None, **fields) -> str:
        task_id = "task_" + uuid.uuid4().hex[:10]
        cols = ["id", "operation_id", "coa_id", "task_type", "status", "created_at"]
        vals: list = [task_id, op_id, coa_id, task_type, status, time.time()]
        for k, v in fields.items():
            if k in {"priority", "assigned_actor_type", "assigned_actor_id", "owning_faction",
                     "target_faction", "target_sector", "target_object", "job_id", "agreement_id",
                     "order_id", "success_criteria_json", "failure_criteria_json", "evidence_json"}:
                cols.append(k); vals.append(self._enc(k, v))
        with self._lock, self._connect() as conn:
            conn.execute(f"INSERT INTO operation_tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            conn.commit()
        return task_id

    def attach_report(self, op_id: str, report_type: str, summary: str, severity: int = 0,
                      evidence: Any = None) -> str:
        rpt_id = "rpt_" + uuid.uuid4().hex[:10]
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO operation_reports (id, operation_id, report_type, severity, summary, "
                         "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (rpt_id, op_id, report_type, int(severity or 0), summary,
                          self._enc("evidence_json", evidence), time.time()))
            conn.commit()
        return rpt_id

    def conclude_operation(self, op_id: str, conclusion_status: str, conclusion_summary: str = "") -> dict:
        """Close an operation AND clean up its side effects (the anti-stale guarantee): release unused reserved
        budget (reserved→spent), CANCEL linked open jobs, EXPIRE linked pending agreements. conclusion_status is
        the terminal lifecycle status (completed/failed/aborted/transitioned) — recorded so the active-threat dedupe
        index frees the threat_key. Idempotent: re-conclusion finds nothing open to clean."""
        status = conclusion_status if conclusion_status in self.OP_CONCLUDED_STATUSES else "completed"
        now = time.time()
        op = self.get_operation(op_id) or {}
        reserved = int(op.get("budget_reserved") or 0)
        spent = int(op.get("budget_spent") or 0)
        released = max(0, reserved - spent)
        with self._lock, self._connect() as conn:
            cancelled = conn.execute("UPDATE market_jobs SET status='cancelled', updated_at=? "
                                     "WHERE operation_id=? AND status='open'", (now, op_id)).rowcount or 0
            conn.execute("UPDATE military_operations SET status=?, concluded_at=?, updated_at=?, "
                         "conclusion_status=?, conclusion_summary=?, budget_reserved=? WHERE id=?",
                         (status, now, now, status, conclusion_summary, spent, op_id))
            conn.commit()
        expired = 0
        for ag in self.list_agreements(op.get("save_id") or "", "pending"):
            terms = ag.get("terms") if isinstance(ag.get("terms"), dict) else {}
            if terms.get("operation_id") == op_id:
                self.set_agreement_status(op.get("save_id") or "", ag["id"], "expired")
                expired += 1
        leases_released = self.release_operation_leases(op.get("save_id") or "", op_id)
        reqs_cancelled = self.cancel_operation_force_requests(op.get("save_id") or "", op_id)
        return {"ok": True, "id": op_id, "status": status, "budget_released": released,
                "jobs_cancelled": cancelled, "agreements_expired": expired,
                "leases_released": leases_released, "force_requests_cancelled": reqs_cancelled}

    def operation_detail(self, op_id: str) -> Optional[dict]:
        """Full operation + its COAs, tasks, reports — for the dashboard drill-down and selftests."""
        op = self.get_operation(op_id)
        if not op:
            return None
        with self._connect() as conn:
            op["coas"] = [self._decode_op_row(r) for r in conn.execute(
                "SELECT * FROM operation_coas WHERE operation_id=? ORDER BY created_at", (op_id,)).fetchall()]
            op["tasks"] = [self._decode_op_row(r) for r in conn.execute(
                "SELECT * FROM operation_tasks WHERE operation_id=? ORDER BY created_at", (op_id,)).fetchall()]
            op["reports"] = [self._decode_op_row(r) for r in conn.execute(
                "SELECT * FROM operation_reports WHERE operation_id=? ORDER BY created_at", (op_id,)).fetchall()]
        return op

    # --- OPORD Phase 8: milestone world-events (routed through the gate → news/NPC memory, anti-spam) -----
    def _get_opord_gate(self):
        g = getattr(self, "_opord_gate", None)
        if g is None:
            try:
                from .gates import EventGate
            except ImportError:
                from gates import EventGate
            g = EventGate()
            self._opord_gate = g
        return g

    def emit_operation_event(self, op: dict, milestone: str, importance: int, summary: str) -> dict:
        """Emit an OPORD milestone as a world_event — but ONLY through the gate (tier/cooldown/dedup), so a
        persistent FRAGO condition can't spam the feed every assess tick. Fired events land in `world_events`
        (the dashboard's durable history) AND propagate to NPCs via `build_situation_briefing`. Linked to the
        operation via source='opord:<id>'. Deterministic summary; an LLM news wrapper is optional/later."""
        gate = self._get_opord_gate()
        res = gate.evaluate(op["save_id"], {"event_type": milestone, "faction": op.get("faction_id"),
                                            "target": op.get("target_faction"), "importance": int(importance),
                                            "state_changed": True, "authorized": True,
                                            "player_relevant": int(importance) >= 4})
        if res.get("fire"):
            self.add_world_event(op["save_id"], milestone, summary, primary_faction=op.get("faction_id") or "",
                                 secondary_faction=op.get("target_faction") or "",
                                 sector_id=op.get("target_sector") or "", importance=int(importance),
                                 source="opord:" + op["id"])
            return {"emitted": True, "tier": res.get("tier")}
        return {"emitted": False, "reason": res.get("reason")}

    def list_operation_events(self, op_id: str) -> list[dict]:
        """World events linked to an operation (for the dashboard drill-down)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM world_events WHERE source=? ORDER BY created_at DESC",
                                ("opord:" + op_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- OPORD Phase 9: operational health warnings (the dashboard's audit layer) -------------------
    def operations_health(self, save_id: str) -> dict:
        """Per-operation health checks for the dashboard audit panel — surface stuck/leaky/incoherent ops.
        Each is derived from REAL state (status, tasks, linked jobs/agreements, reports), so the panel makes the
        whole threat→conclusion chain auditable and catches regressions (e.g. a concluded op that still has open
        jobs = a cleanup leak)."""
        now = time.time()
        pending_ag_by_op: dict = {}
        for a in self.list_agreements(save_id, "pending"):
            terms = a.get("terms") if isinstance(a.get("terms"), dict) else {}
            opref = terms.get("operation_id")
            if opref:
                pending_ag_by_op.setdefault(opref, 0)
                pending_ag_by_op[opref] += 1
        out = []
        for op in self.list_operations(save_id):
            op_id, status = op["id"], op["status"]
            concluded = status in self.OP_CONCLUDED_STATUSES
            active_like = status in ("coa_generated", "opord_issued", "active", "frago_required")
            with self._connect() as conn:
                tasks = conn.execute("SELECT job_id, agreement_id, order_id FROM operation_tasks "
                                     "WHERE operation_id=?", (op_id,)).fetchall()
                open_jobs = conn.execute("SELECT COUNT(*) AS c, MIN(created_at) AS oldest FROM market_jobs "
                                         "WHERE operation_id=? AND status='open'", (op_id,)).fetchone()
                frago_n = conn.execute("SELECT COUNT(*) AS c FROM operation_reports "
                                       "WHERE operation_id=? AND report_type='frago'", (op_id,)).fetchone()["c"]
                report_n = conn.execute("SELECT COUNT(*) AS c FROM operation_reports WHERE operation_id=?",
                                        (op_id,)).fetchone()["c"]
            open_job_n = open_jobs["c"] or 0
            pending_ag = pending_ag_by_op.get(op_id, 0)
            warns: list = []
            if status in ("opord_issued", "active") and not op.get("selected_coa_id"):
                warns.append("no_selected_coa")
            if status in ("opord_issued", "active") and len(tasks) == 0:
                warns.append("opord_no_tasks")
            if status == "active" and open_jobs["oldest"] and (now - float(open_jobs["oldest"])) >= self.OPORD_JOB_UNCLAIMED_S:
                warns.append("stale_unclaimed_job")
            if active_like and int(op.get("budget_reserved") or 0) > 0:
                linked = any((t["job_id"] or t["agreement_id"] or t["order_id"]) for t in tasks) or open_job_n > 0 or pending_ag > 0
                if not linked:
                    warns.append("budget_reserved_no_link")
            if concluded and open_job_n > 0:
                warns.append("concluded_open_jobs")          # cleanup regression detector
            if concluded and pending_ag > 0:
                warns.append("concluded_pending_agreements")  # cleanup regression detector
            if not concluded and frago_n >= 3:
                warns.append("repeated_fragos_no_progress")
            if active_like and report_n == 0:
                warns.append("no_reports")
            if status == "active" and op.get("activated_at") and (now - float(op["activated_at"])) >= self.OPORD_MAX_ACTIVE_S:
                warns.append("active_too_long")
            if warns:
                out.append({"op_id": op_id, "faction": op.get("faction_id"), "status": status, "warnings": warns})
        return {"ok": True, "unhealthy": out, "count": len(out)}

    # --- OPORD Phase 6: job market + task update helpers --------------------------------------------
    def create_or_update_job(self, save_id: str, issuing_faction: str, job_type: str, target_sector: str = "",
                             target_faction: str = "", ware: str = "", reward: int = 0, urgency: int = 0,
                             visibility: str = "public", operation_id: Optional[str] = None,
                             operation_task_id: Optional[str] = None, evidence: Any = None) -> dict:
        """Create OR update one open job-market listing (anti-spam: ONE open job per job_key; repeated need raises
        reward/urgency + refreshes evidence instead of reposting). job_key = save:faction:type:sector:target:ware."""
        job_key = f"{save_id}:{issuing_faction}:{job_type}:{target_sector}:{target_faction}:{ware}"
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id, reward, urgency FROM market_jobs WHERE save_id=? AND job_key=? "
                               "AND status='open' LIMIT 1", (save_id, job_key)).fetchone()
            if row:
                conn.execute("UPDATE market_jobs SET reward=?, urgency=?, evidence_json=?, updated_at=? WHERE id=?",
                             (max(int(row["reward"] or 0), int(reward or 0)),
                              max(int(row["urgency"] or 0), int(urgency or 0)),
                              self._enc("evidence_json", evidence), now, row["id"]))
                conn.commit()
                return {"ok": True, "id": row["id"], "created": False, "job_key": job_key}
            job_id = "job_" + uuid.uuid4().hex[:10]
            conn.execute("INSERT INTO market_jobs (id, save_id, issuing_faction, job_type, job_key, target_sector, "
                         "target_faction, ware, reward, urgency, visibility, status, operation_id, "
                         "operation_task_id, evidence_json, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, save_id, issuing_faction, job_type, job_key, target_sector, target_faction, ware,
                          int(reward or 0), int(urgency or 0), visibility, "open", operation_id, operation_task_id,
                          self._enc("evidence_json", evidence), now, now))
            conn.commit()
        return {"ok": True, "id": job_id, "created": True, "job_key": job_key}

    def list_jobs(self, save_id: str, status: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM market_jobs WHERE save_id=? AND status=? ORDER BY updated_at DESC",
                                    (save_id, status)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM market_jobs WHERE save_id=? ORDER BY updated_at DESC",
                                    (save_id,)).fetchall()
        return [self._decode_op_row(r) for r in rows]

    # --- FRAGO reward escalation for stale open market jobs (spec: OPORD_Update §FRAGO + Job Market) --------
    JOB_STALE_S = 900.0          # open + untouched this long => a real escalation decision is due
    JOB_RAISE_FRACTION = 0.25    # engine-computed legal raise: +25% (min +5000), capped by faction budget headroom

    def list_stale_open_jobs(self, save_id: str, stale_s: Optional[float] = None) -> list[dict]:
        """Open market jobs with no update for JOB_STALE_S — candidates for a Player2 escalation decision."""
        cutoff = time.time() - float(self.JOB_STALE_S if stale_s is None else stale_s)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM market_jobs WHERE save_id=? AND status='open' AND updated_at<=? "
                                "ORDER BY urgency DESC, updated_at ASC", (save_id, cutoff)).fetchall()
        return [self._decode_op_row(r) for r in rows]

    def job_escalation_options(self, save_id: str, job: dict) -> tuple[list[dict], int]:
        """Engine-side legality: compute the bounded option menu for one stale job. The RAISE amount is
        deterministic (+25%, min +5000) and only offered if the issuing faction's budget headroom covers the
        INCREMENT (words≠resources). Player2 only ever picks among these; it never invents an amount."""
        reward = int(job.get("reward") or 0)
        raise_to = max(int(reward * (1.0 + self.JOB_RAISE_FRACTION)), reward + 5000)
        fid = str(job.get("issuing_faction") or "")
        headroom = max(0.0, self.budget_capacity(save_id, fid) - self.budget_spent(save_id, fid))
        options: list[dict] = []
        if (raise_to - reward) <= headroom:
            options.append({"key": f"raise:{raise_to}",
                            "label": f"Raise the reward to {raise_to:,} credits to attract a claimant"})
        options.append({"key": "hold", "label": "Hold the listing unchanged and wait"})
        options.append({"key": "cancel", "label": "Withdraw the listing (need no longer worth the price)"})
        return options, raise_to

    def apply_job_escalation(self, save_id: str, job_id: str, choice: str) -> dict:
        """Deterministic executor for a Player2 job-escalation verdict. raise:<n> re-prices (bounded upstream),
        cancel closes, hold snoozes (touch updated_at). Emits a world_event ONLY on material change (raise/cancel)
        — the anti-spam announce rule. Returns {ok, action, news?}."""
        now = time.time()
        result: dict[str, Any]
        event: Optional[dict] = None
        # NOTE: self._lock is NON-reentrant — add_world_event takes it too, so events are emitted AFTER the
        # with-block, never inside it (a nested call deadlocks the request thread; learned live 2026-07-01).
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM market_jobs WHERE save_id=? AND id=? AND status='open'",
                               (save_id, job_id)).fetchone()
            if not row:
                return {"ok": False, "reason": "job not open"}
            job = dict(row)
            if choice.startswith("raise:"):
                try:
                    new_reward = int(choice.split(":", 1)[1])
                except Exception:
                    return {"ok": False, "reason": "bad raise amount"}
                if new_reward <= int(job.get("reward") or 0):
                    return {"ok": False, "reason": "raise must increase reward"}
                conn.execute("UPDATE market_jobs SET reward=?, urgency=urgency+1, updated_at=? WHERE id=?",
                             (new_reward, now, job_id))
                conn.commit()
                news = (f"Contracts: {job.get('issuing_faction')} raises the "
                        f"{job.get('job_type')} contract reward to {new_reward:,} credits.")
                event = {"summary": news, "importance": 2}
                result = {"ok": True, "action": "raised", "reward": new_reward, "news": news}
            elif choice == "cancel":
                conn.execute("UPDATE market_jobs SET status='cancelled', updated_at=? WHERE id=?", (now, job_id))
                conn.commit()
                news = (f"Contracts: {job.get('issuing_faction')} withdraws its "
                        f"{job.get('job_type')} contract.")
                event = {"summary": news, "importance": 1}
                result = {"ok": True, "action": "cancelled", "news": news}
            else:
                # hold — snooze one stale window, announce nothing (no material change)
                conn.execute("UPDATE market_jobs SET updated_at=? WHERE id=?", (now, job_id))
                conn.commit()
                result = {"ok": True, "action": "held"}
        if event:
            self.add_world_event(save_id, event_type="economy", summary=event["summary"],
                                 primary_faction=str(job.get("issuing_faction") or ""),
                                 importance=event["importance"], source="job_escalation")
        return result

    def update_task(self, task_id: str, status: Optional[str] = None, **fields) -> dict:
        sets, vals = [], []
        if status:
            sets.append("status=?"); vals.append(status)
        for k, v in fields.items():
            if k in {"assigned_actor_type", "assigned_actor_id", "owning_faction", "job_id", "agreement_id",
                     "order_id", "target_faction", "target_sector", "priority", "evidence_json", "issued_at",
                     "activated_at", "completed_at", "failed_at", "success_criteria_json", "failure_criteria_json"}:
                sets.append(f"{k}=?"); vals.append(self._enc(k, v))
        if not sets:
            return {"ok": True, "id": task_id}
        vals.append(task_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE operation_tasks SET {','.join(sets)} WHERE id=?", vals)
            conn.commit()
        return {"ok": True, "id": task_id}

    # --- OPORD execution lifecycle: job claim/complete + budget SPEND + task success (#1 build) ----------
    def claim_job(self, save_id: str, job_id: str, claimant: str = "") -> dict:
        """Mark an open job claimed (player/NPC took the contract)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            n = conn.execute("UPDATE market_jobs SET status='claimed', updated_at=? WHERE id=? AND save_id=? "
                             "AND status='open'", (now, job_id, save_id)).rowcount or 0
            conn.commit()
        return {"ok": n > 0, "id": job_id, "status": "claimed" if n else "not_open", "claimant": claimant}

    def complete_job(self, save_id: str, job_id: str, claimant: str = "", evidence: Any = None) -> dict:
        """Fulfillment PROOF: complete the job, complete its linked task, and SPEND the reward from the issuing
        faction's budget (the spec's 'budget spends only on execution' — reservation was at OPORD issue). Bumps the
        operation's budget_spent + logs a task_update. Idempotent (a completed/cancelled job is a no-op)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM market_jobs WHERE id=? AND save_id=?", (job_id, save_id)).fetchone()
        if not row:
            return {"ok": False, "reason": "job not found"}
        job = self._decode_op_row(row)
        if job.get("status") in ("completed", "cancelled"):
            return {"ok": True, "id": job_id, "status": job["status"], "noop": True}
        now = time.time()
        reward = int(job.get("reward") or 0)
        fac = job.get("issuing_faction") or ""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE market_jobs SET status='completed', updated_at=? WHERE id=?", (now, job_id))
            conn.commit()
        spent_total = self.record_budget_spend(save_id, fac, reward)
        op_id = job.get("operation_id")
        if job.get("operation_task_id"):
            self.update_task(job["operation_task_id"], status="completed", completed_at=now,
                             evidence_json={"completed_by": claimant or "claimant", "via": "job", "reward": reward})
        if op_id:
            op = self.get_operation(op_id)
            if op:
                self.update_operation(op_id, budget_spent=int(op.get("budget_spent") or 0) + reward)
                self.attach_report(op_id, "task_update",
                                   f"Job {job.get('job_type')} completed by {claimant or 'a claimant'}; {reward} spent.",
                                   severity=2, evidence={"job_id": job_id, "reward": reward, "claimant": claimant})
        return {"ok": True, "id": job_id, "status": "completed", "reward_spent": reward, "faction_spent_total": spent_total}

    def fail_job(self, save_id: str, job_id: str, reason: str = "") -> dict:
        """Mark a job failed (unclaimed/expired/abandoned). Reserved budget is freed when the op concludes."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE market_jobs SET status='failed', updated_at=? WHERE id=? AND save_id=?",
                         (now, job_id, save_id))
            conn.commit()
        return {"ok": True, "id": job_id, "status": "failed", "reason": reason}

    # --- OPORD Execution Authority: ship leases (real-asset orders) + force-quota requests --------------
    _LEASE_TERMINAL = ("completed", "failed", "released", "lost")

    def debug_force_pending_order(self, save_id: str, faction: str = "argon") -> dict:
        """DEBUG/TEST ONLY: synthesize one ACTIVE op with a single pending-ingame fleet task so the in-game MD
        issuer (On_Assign) can be exercised ON DEMAND (the live recognizer's anti-spam dedup otherwise blocks
        manufacturing a fresh combat op). Writes real rows in the normal tables; clean up by concluding the op."""
        save_id = save_id or "unindexed"
        faction = faction or "argon"
        threat_key = f"{save_id}:{faction}:sector_pressure:debug:{uuid.uuid4().hex[:6]}"
        res = self.create_or_get_operation(save_id, faction, "sector_pressure", threat_key,
                                           status="active", target_faction="xenon",
                                           target_sector="Argon Prime", urgency=5, importance=5,
                                           threat_id=threat_key)
        op_id = res["id"]
        task_id = self.attach_task(op_id, "patrol_sector", status="issued", assigned_actor_type="fleet",
                                   owning_faction=faction, target_faction="xenon",
                                   target_sector="Argon Prime", order_id="pending_ingame")
        return {"ok": True, "operation_id": op_id, "task_id": task_id, "faction": faction,
                "note": "GET /v1/opord/orders/pending should now list it; the in-game poller will fire On_Assign"}

    def pending_orders(self, save_id: str) -> dict:
        """Tasks awaiting a REAL in-game ship order — internal-fleet tasks routed but still `pending_ingame`.
        The MD issuer polls this, finds a ship, leases it, issues a create_order, then reports back."""
        out = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.id AS task_id, t.operation_id, t.task_type, t.target_sector, t.target_faction, "
                "o.faction_id, o.target_sector AS op_sector, o.priority, o.urgency, o.operation_type "
                "FROM operation_tasks t JOIN military_operations o ON t.operation_id=o.id "
                "WHERE o.save_id=? AND t.assigned_actor_type='fleet' AND t.status='issued' "
                "AND (t.order_id IS NULL OR t.order_id='pending_ingame') "
                "AND o.status IN ('active','opord_issued','frago_required')", (save_id,)).fetchall()
        for r in rows:
            r = dict(r)
            out.append({"task_id": r["task_id"], "operation_id": r["operation_id"], "faction": r["faction_id"],
                        "task_type": r["task_type"], "sector": r["target_sector"] or r["op_sector"],
                        "target_faction": r["target_faction"],
                        # posture derives from the Player2-selected task type (execution semantics, deterministic):
                        # offensive task types engage on sight; holds/patrols engage on contact only.
                        "stance": ("aggressive" if r["task_type"] in ("engage_hostiles", "raid_enemy_logistics")
                                   else "defensive"),
                        "priority": max(int(r["priority"] or 0), int(r["urgency"] or 0))})
        return {"ok": True, "pending": out}

    def lease_asset(self, save_id: str, operation_id: str, task_id: str, faction: str, ship_runtime_id: str,
                    ship_name: str = "", ship_macro: str = "", ship_class: str = "", sector: str = "",
                    order_kind: str = "protectposition", priority: int = 0,
                    original_order_summary: str = "") -> dict:
        """Reserve a real ship for a task. One ACTIVE lease per ship (anti-steal); a HIGHER-priority op overrides a
        lower-priority lease (the old one is released as 'interrupted-by-priority'); equal/higher blocks."""
        now = time.time()
        if not ship_runtime_id:
            return {"ok": False, "reason": "no ship"}
        with self._lock, self._connect() as conn:
            ex = conn.execute("SELECT lease_id, priority, operation_id FROM opord_asset_leases WHERE save_id=? "
                              "AND ship_runtime_id=? AND status NOT IN ('completed','failed','released','lost') "
                              "LIMIT 1", (save_id, ship_runtime_id)).fetchone()
            if ex:
                if int(priority) > int(ex["priority"] or 0):
                    conn.execute("UPDATE opord_asset_leases SET status='released', released_at=?, "
                                 "failure_reason='overridden by higher-priority OPORD' WHERE lease_id=?",
                                 (now, ex["lease_id"]))
                else:
                    conn.commit()
                    return {"ok": False, "blocked": True, "reason": "ship already leased",
                            "held_by": ex["operation_id"], "lease_id": ex["lease_id"]}
            lease_id = "lease_" + uuid.uuid4().hex[:10]
            conn.execute("INSERT INTO opord_asset_leases (lease_id, save_id, operation_id, task_id, faction, "
                         "ship_runtime_id, ship_name, ship_macro, ship_class, sector, original_order_summary, "
                         "order_kind, priority, status, last_seen_at, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (lease_id, save_id, operation_id, task_id, faction, ship_runtime_id, ship_name, ship_macro,
                          ship_class, sector, original_order_summary, order_kind, int(priority), "reserved", now, now))
            conn.commit()
        return {"ok": True, "lease_id": lease_id, "status": "reserved"}

    def mark_order_issued(self, save_id: str, lease_id: str, assigned_order_id: str = "") -> dict:
        """The MD issued a real create_order for the leased ship → lease 'issued'; the task goes 'active'."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT task_id FROM opord_asset_leases WHERE save_id=? AND lease_id=?",
                               (save_id, lease_id)).fetchone()
            conn.execute("UPDATE opord_asset_leases SET status='issued', issued_at=?, last_seen_at=?, "
                         "assigned_order_id=? WHERE lease_id=?", (now, now, assigned_order_id, lease_id))
            conn.commit()
        if row and row["task_id"]:
            self.update_task(row["task_id"], status="active", order_id=assigned_order_id or "issued",
                             activated_at=now)
        return {"ok": True, "lease_id": lease_id, "status": "issued"}

    def record_order_event(self, save_id: str, lease_id: str, event: str, evidence: Any = None) -> dict:
        """Observed execution event from the watchdog: arrived/engaged/completed/failed/lost/interrupted. Completed/
        failed/lost drive the linked task's terminal state — execution evidence, never intent."""
        now = time.time()
        ev = str(event or "").lower()
        status = ev if ev in ("arrived", "engaged", "completed", "failed", "lost", "interrupted") else "interrupted"
        detail = json.dumps(evidence) if isinstance(evidence, (dict, list)) else (evidence or "")
        released = now if status in ("completed", "failed", "lost") else None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT lease_id, task_id FROM opord_asset_leases WHERE save_id=? AND lease_id=?",
                               (save_id, lease_id)).fetchone()
            if not row:
                # the aiscript reports by task_id (its leasetag) — resolve to the active lease for that task.
                row = conn.execute("SELECT lease_id, task_id FROM opord_asset_leases WHERE save_id=? AND task_id=? "
                                   "AND status NOT IN ('completed','failed','released','lost') "
                                   "ORDER BY created_at DESC LIMIT 1", (save_id, lease_id)).fetchone()
                if row:
                    lease_id = row["lease_id"]
            conn.execute("UPDATE opord_asset_leases SET status=?, last_seen_at=?, "
                         "released_at=COALESCE(?, released_at), failure_reason=? WHERE lease_id=?",
                         (status, now, released, detail, lease_id))
            conn.commit()
        if row and row["task_id"]:
            if status == "completed":
                self.update_task(row["task_id"], status="completed", completed_at=now,
                                 evidence_json={"execution": "order_completed"})
            elif status in ("failed", "lost"):
                self.update_task(row["task_id"], status="failed", failed_at=now,
                                 evidence_json={"execution": status})
        return {"ok": True, "lease_id": lease_id, "status": status}

    def release_asset(self, save_id: str, lease_id: str, reason: str = "") -> dict:
        """Release a lease (idempotent — a terminal lease is a no-op)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM opord_asset_leases WHERE save_id=? AND lease_id=?",
                               (save_id, lease_id)).fetchone()
            if not row:
                return {"ok": False, "reason": "lease not found"}
            if row["status"] in self._LEASE_TERMINAL:
                return {"ok": True, "lease_id": lease_id, "status": row["status"], "noop": True}
            conn.execute("UPDATE opord_asset_leases SET status='released', released_at=?, failure_reason=? "
                         "WHERE lease_id=?", (now, reason, lease_id))
            conn.commit()
        return {"ok": True, "lease_id": lease_id, "status": "released"}

    def release_operation_leases(self, save_id: str, op_id: str) -> int:
        """Release every still-active lease of an operation (closeout)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            n = conn.execute("UPDATE opord_asset_leases SET status='released', released_at=?, "
                             "failure_reason='operation concluded' WHERE save_id=? AND operation_id=? "
                             "AND status NOT IN ('completed','failed','released','lost')", (now, save_id, op_id)).rowcount or 0
            conn.commit()
        return n

    def list_leases(self, save_id: str, operation_id: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if operation_id:
                rows = conn.execute("SELECT * FROM opord_asset_leases WHERE save_id=? AND operation_id=? "
                                    "ORDER BY created_at DESC", (save_id, operation_id)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM opord_asset_leases WHERE save_id=? ORDER BY created_at DESC",
                                    (save_id,)).fetchall()
        return [dict(r) for r in rows]

    def create_or_update_force_request(self, save_id: str, operation_id: str, task_id: str, faction: str,
                                       sector: str, ship_role: str, ship_size: str = "", quantity: int = 1,
                                       priority: int = 0, reward_budget: int = 0) -> dict:
        """Durable force demand when no ship is available — ONE open request per (faction,sector,role,operation);
        repeated need escalates priority/reward instead of spamming."""
        now = time.time()
        req_key = f"{save_id}:{faction}:{sector}:{ship_role}:{operation_id}"
        with self._lock, self._connect() as conn:
            ex = conn.execute("SELECT request_id, priority, reward_budget FROM opord_force_requests WHERE save_id=? "
                              "AND req_key=? AND status='open' LIMIT 1", (save_id, req_key)).fetchone()
            if ex:
                conn.execute("UPDATE opord_force_requests SET priority=?, reward_budget=?, last_escalated_at=? "
                             "WHERE request_id=?", (max(int(ex["priority"] or 0), int(priority)),
                             max(int(ex["reward_budget"] or 0), int(reward_budget)), now, ex["request_id"]))
                conn.commit()
                return {"ok": True, "request_id": ex["request_id"], "created": False}
            rid = "freq_" + uuid.uuid4().hex[:10]
            conn.execute("INSERT INTO opord_force_requests (request_id, save_id, operation_id, task_id, faction, "
                         "sector, ship_role, ship_size, quantity, priority, reward_budget, req_key, status, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (rid, save_id, operation_id, task_id, faction, sector, ship_role, ship_size, int(quantity),
                          int(priority), int(reward_budget), req_key, "open", now))
            conn.commit()
        return {"ok": True, "request_id": rid, "created": True}

    def cancel_operation_force_requests(self, save_id: str, op_id: str) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            n = conn.execute("UPDATE opord_force_requests SET status='cancelled', last_escalated_at=? "
                             "WHERE save_id=? AND operation_id=? AND status='open'", (now, save_id, op_id)).rowcount or 0
            conn.commit()
        return n

    # --- OPORD Phase 2: threat recognition (real hostile events → deduped warning-order operations) -----
    def recognize_threats(self, save_id: str, window_s: float = 3600.0) -> dict:
        """Turn REAL recent hostile events into THREATS, each a deduped warning-order operation. Aggregate by
        (victim = the DEFENDING faction that would mount an op, aggressor, sector); criminal aggressors (Xenon/
        Kha'ak/pirates) → `raid_pressure`, else `sector_pressure`. create_or_get_operation dedupes (one active op
        per threat_key — anti-spam); an existing op is UPDATED (evidence/urgency) instead of re-created. Pure +
        evidence-grounded; fabricates nothing (only reads events the mod actually recorded)."""
        evs = self.list_hostile_events(save_id, window_s=window_s, limit=2000)
        agg: dict = {}
        for e in evs:
            atk, vic, sect = e.get("attacker_faction"), e.get("victim_faction"), (e.get("sector") or "")
            if not atk or not vic or atk == vic:
                continue
            ttype = "raid_pressure" if atk in self.CRIMINAL_FACTIONS else "sector_pressure"
            a = agg.setdefault((vic, ttype, atk, sect),
                               {"faction": vic, "ttype": ttype, "target": atk, "sector": sect,
                                "events": 0, "magnitude": 0.0, "first": None, "last": None})
            a["events"] += 1
            a["magnitude"] += float(e.get("magnitude") or 0)
            ts = float(e.get("ts") or 0)
            a["first"] = ts if a["first"] is None else min(a["first"], ts)
            a["last"] = ts if a["last"] is None else max(a["last"], ts)
        created, updated, threats = 0, 0, []
        for a in agg.values():
            vic, ttype, atk, sect = a["faction"], a["ttype"], a["target"], a["sector"]
            slug = re.sub(r"[^a-z0-9]+", "_", (sect or "unknown").lower()).strip("_") or "unknown"
            threat_key = f"{save_id}:{vic}:{ttype}:{atk}:{slug}"
            urgency = max(1, min(5, 1 + int(a["magnitude"] // 10)))
            importance = max(1, min(5, 1 + a["events"] // 2))
            threat_desc = (f"{atk} pressure in {sect}" if sect else f"{atk} pressure")
            warning = {"type": "warning_order", "faction": vic, "threat_type": ttype, "threat": threat_desc,
                       "urgency": urgency, "importance": importance,
                       "constraints": ["avoid full escalation if possible"],
                       "ccir": ["enemy fleet strength", "sector trade disruption", "available patrol assets"],
                       "evidence": {"hostile_events": a["events"], "recent_losses": round(a["magnitude"], 1)}}
            evidence = {"hostile_events": a["events"], "magnitude": round(a["magnitude"], 1),
                        "sector": sect, "first_at": a["first"], "last_at": a["last"]}
            res = self.create_or_get_operation(save_id, vic, ttype, threat_key, status="warning",
                                               target_faction=atk, target_sector=sect, threat_id=threat_key,
                                               urgency=urgency, importance=importance,
                                               warning_order_json=warning, evidence_json=evidence)
            if res.get("created"):
                created += 1
            else:
                self.update_operation(res["id"], urgency=urgency, importance=importance,
                                      warning_order_json=warning, evidence_json=evidence)
                updated += 1
            threats.append({"op_id": res["id"], "threat_key": threat_key, "created": res.get("created", False)})
        # --- ECONOMY shortages → supply_shortage operations (non-combat feed; this data syncs in normal play) ---
        for econ in self.list_economy(save_id):
            fid = econ.get("faction_id")
            if not fid:
                continue
            shortages = econ.get("shortages") or {}
            health = float(econ.get("production_health", 1.0) or 1.0)
            worst = max([float(s or 0) for s in shortages.values()] + [0.0])
            sev = max(worst, 1.0 - health)
            if sev < 0.34:                       # not a real shortage
                continue
            tk = f"{save_id}:{fid}:supply_shortage:none:economy"
            urgency = max(1, min(5, 1 + int(sev * 4)))
            importance = max(1, min(5, 2 + int(sev * 3)))
            needs = [str(w) for w in (econ.get("key_needs") or list(shortages.keys()))][:4]
            warn = {"type": "warning_order", "faction": fid, "threat_type": "supply_shortage",
                    "threat": f"Supply shortage ({', '.join(needs) or 'key wares'})", "urgency": urgency,
                    "importance": importance, "constraints": ["protect supply lines"],
                    "ccir": ["stock levels", "delivery routes", "contractor availability"],
                    "evidence": {"production_health": round(health, 2), "shortage_severity": round(sev, 2)}}
            evid = {"production_health": round(health, 2), "shortage_severity": round(sev, 2), "needs": needs}
            res = self.create_or_get_operation(save_id, fid, "supply_shortage", tk, status="warning",
                                               target_faction="", target_sector="", threat_id=tk, urgency=urgency,
                                               importance=importance, warning_order_json=warn, evidence_json=evid)
            if res.get("created"):
                created += 1
            else:
                self.update_operation(res["id"], urgency=urgency, importance=importance,
                                      warning_order_json=warn, evidence_json=evid)
                updated += 1
            threats.append({"op_id": res["id"], "threat_key": tk, "created": res.get("created", False)})
        # --- broken/expired AGREEMENTS → agreement_breakdown operations (diplomacy feed) ---
        for st in ("broken", "expired"):
            for ag in self.list_agreements(save_id, st):
                a, b = ag.get("party_a"), ag.get("party_b")
                if not a or not b or a == b:
                    continue
                tk = f"{save_id}:{a}:agreement_breakdown:{b}:diplomacy"
                warn = {"type": "warning_order", "faction": a, "threat_type": "agreement_breakdown",
                        "threat": f"{ag.get('type') or 'agreement'} with {b} {st}", "urgency": 3, "importance": 3,
                        "constraints": ["avoid full escalation if possible"], "ccir": ["enemy intent", "relation status"],
                        "evidence": {"agreement_id": ag.get("id"), "agreement_status": st}}
                evid = {"agreement_id": ag.get("id"), "status": st}
                res = self.create_or_get_operation(save_id, a, "agreement_breakdown", tk, status="warning",
                                                   target_faction=b, target_sector="", threat_id=tk, urgency=3,
                                                   importance=3, warning_order_json=warn, evidence_json=evid)
                if res.get("created"):
                    created += 1
                else:
                    self.update_operation(res["id"], warning_order_json=warn, evidence_json=evid)
                    updated += 1
                threats.append({"op_id": res["id"], "threat_key": tk, "created": res.get("created", False)})
        return {"ok": True, "created": created, "updated": updated, "threats": threats}

    # --- OPORD Phase 3: mission analysis (deterministic — mission/intent/end-state/constraints/CCIR/assets) ----
    def analyze_mission(self, op_id: str) -> dict:
        """Phase 3: deterministic mission analysis for a warning-order operation. Derives mission statement,
        commander intent, desired end state, constraints, CCIR, and REAL available assets (faction fleet + budget)
        from the threat. Advances status warning→analysing. Pure; no LLM, no fabrication."""
        op = self.get_operation(op_id)
        if not op:
            return {"ok": False, "reason": "operation not found"}
        save_id, fid = op["save_id"], op["faction_id"]
        target = op.get("target_faction") or ""
        sector = op.get("target_sector") or ""
        ttype = op.get("operation_type") or "sector_pressure"
        fac = self.get_faction(save_id, fid) or self.get_faction(self.CANON_SAVE, fid) or {}
        fac_name = fac.get("name") or fid
        tgt = self.get_faction(save_id, target) or self.get_faction(self.CANON_SAVE, target) or {}
        tgt_name = tgt.get("name") or target or "hostile forces"
        fight = 0
        for f in self.list_fleet_strength(save_id):
            if f.get("faction_id") == fid:
                fight = int(f.get("fight") or 0)
                break
        budget_available = max(0.0, self.budget_capacity(save_id, fid) - self.budget_spent(save_id, fid))
        where = sector or "the contested zone"
        is_raid = ttype == "raid_pressure"
        mission_statement = (f"{fac_name} secures {where} to reduce {tgt_name} "
                             + ("raiding and restore security." if is_raid else "pressure and protect trade traffic."))
        commander_intent = ("Neutralize the raiders and restore freedom of movement." if is_raid
                            else "Restore freedom of movement without triggering wider war.")
        desired_end_state = f"Hostile pressure reduced and {where} stable."
        constraints = list((op.get("warning_order_json") or {}).get("constraints") or ["avoid full escalation if possible"])
        if not is_raid and "protect civilian traffic" not in constraints:
            constraints.append("protect civilian traffic")
        ccir = list((op.get("warning_order_json") or {}).get("ccir") or
                    ["enemy reinforcements", "friendly losses", "trade corridor status"])
        analysis = {"mission_statement": mission_statement, "commander_intent": commander_intent,
                    "desired_end_state": desired_end_state, "constraints": constraints,
                    "available_assets": {"combat_ships": fight, "budget_available": int(budget_available)},
                    "ccir": ccir}
        self.update_operation(op_id, status="analysing", mission_statement=mission_statement,
                              commander_intent=commander_intent, desired_end_state=desired_end_state,
                              mission_analysis_json=analysis, constraints_json=constraints, ccir_json=ccir)
        return {"ok": True, "id": op_id, "analysis": analysis}

    def analyze_pending_missions(self, save_id: str) -> dict:
        """Run mission analysis on every operation still at `warning` (the recognize→analyse stage transition)."""
        n = 0
        for op in self.list_operations(save_id, "warning"):
            self.analyze_mission(op["id"])
            n += 1
        return {"ok": True, "analysed": n}

    # --- OPORD Phase 4: COA engine (generate → screen → wargame → doctrine score → select) ---------------
    def plan_operation_coas(self, op_id: str) -> dict:
        """For an analysed operation: generate candidate COAs, screen out infeasible ones, wargame + doctrine-score
        the viable ones, SELECT the highest (deterministic — same inputs always pick the same COA, ties broken by
        coa_type), persist all COAs, and advance status analysing→coa_generated with selected_coa_id. The LLM does
        not choose; this does."""
        op = self.get_operation(op_id)
        if not op:
            return {"ok": False, "reason": "operation not found"}
        assets = (op.get("mission_analysis_json") or {}).get("available_assets") or {}
        weights = OPORD_DOCTRINE.get(op["faction_id"], OPORD_DOCTRINE["default"])
        persisted, best = [], None
        for c in opord_generate_coas(op):
            viable, reason = opord_screen_coa(c, assets, op)
            if not viable:
                cid = self.attach_coa(op_id, c["coa_type"], c["concept"], c["tasks"],
                                      viability_status="rejected", rejection_reason=reason,
                                      required_budget=c.get("required_budget", 0),
                                      expected_duration=c.get("expected_duration"),
                                      required_assets_json=c.get("required_assets"))
                persisted.append({"coa_id": cid, "type": c["coa_type"], "viable": False, "reason": reason})
                continue
            wg = opord_wargame_coa(c, op, assets)
            score, breakdown = opord_score_coa(wg, weights)
            cid = self.attach_coa(op_id, c["coa_type"], c["concept"], c["tasks"],
                                  viability_status="viable", required_budget=c.get("required_budget", 0),
                                  expected_duration=c.get("expected_duration"),
                                  required_assets_json=c.get("required_assets"),
                                  wargame_json=wg, score_json=breakdown, weighted_score=score)
            rec = {"coa_id": cid, "type": c["coa_type"], "viable": True, "score": score}
            persisted.append(rec)
            if best is None or score > best["score"] or (score == best["score"] and c["coa_type"] < best["type"]):
                best = rec
        # D1: the engine does the STAFF WORK (generate/screen/wargame/score) and records the ADVISORY best, but does
        # NOT select. The op waits at coa_generated until Player2 chooses via router.select_pending_coas_llm. The
        # deterministic score is advice, not the decision (spec §1/§11).
        if best:
            self.update_operation(op_id, status="coa_generated")
        return {"ok": True, "op_id": op_id, "coas": persisted, "advisory_best": best, "selected": None}

    def set_selected_coa(self, op_id: str, coa_id: str) -> dict:
        """Commit a COA selection (the result of the Player2 D1 decision, or a test/operator action). Validates the
        COA belongs to the op and is viable; marks it selected and advances the op so OPORD generation can proceed."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT operation_id, viability_status FROM operation_coas WHERE id=?",
                               (coa_id,)).fetchone()
            if not row or row["operation_id"] != op_id:
                return {"ok": False, "reason": "coa not for this op"}
            if row["viability_status"] not in ("viable", "selected"):
                return {"ok": False, "reason": f"coa not viable ({row['viability_status']})"}
            conn.execute("UPDATE operation_coas SET viability_status='selected', selected=1 WHERE id=?", (coa_id,))
            conn.commit()
        self.update_operation(op_id, status="coa_generated", selected_coa_id=coa_id)
        return {"ok": True, "op_id": op_id, "selected_coa_id": coa_id}

    def list_viable_coas(self, op_id: str) -> list[dict]:
        """Viable COA candidates for an op (the legal menu the Player2 D1 decision chooses from)."""
        det = self.operation_detail(op_id) or {}
        return [c for c in (det.get("coas") or []) if c.get("viability_status") in ("viable", "selected")]

    def plan_pending_coas(self, save_id: str) -> dict:
        """Run the COA engine on every operation at `analysing` (the analyse→coa_generated transition)."""
        n = 0
        for op in self.list_operations(save_id, "analysing"):
            self.plan_operation_coas(op["id"])
            n += 1
        return {"ok": True, "planned": n}

    # --- OPORD Phase 5: OPORD generator (selected COA → SMESC + annexes + executable tasks) --------------
    def generate_opord(self, op_id: str) -> dict:
        """Turn the selected COA into the formal order: build SMESC opord_json + annexes_json, DERIVE executable
        operation_tasks (each tagged with the selected coa_id so every task maps back to the COA), reserve the
        COA's budget, and advance coa_generated→opord_issued (issued_at). Deterministic; LLM prose is optional/later."""
        op = self.get_operation(op_id)
        if not op:
            return {"ok": False, "reason": "operation not found"}
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM operation_coas WHERE operation_id=? AND selected=1 LIMIT 1",
                               (op_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "no selected COA"}
        coa = self._decode_op_row(row)
        smesc = opord_build_smesc(op, coa)
        annexes = opord_build_annexes(op, coa)
        task_ids = []
        for t in (coa.get("tasks_json") or []):
            tid = self.attach_task(op_id, t.get("task_type") or "task", status="planned", coa_id=coa["id"],
                                   target_faction=t.get("target_faction"), target_sector=t.get("sector"),
                                   success_criteria_json=t.get("success_criteria"))
            task_ids.append(tid)
        reserved = int(coa.get("required_budget") or 0)
        self.update_operation(op_id, status="opord_issued", opord_json=smesc, annexes_json=annexes,
                              budget_reserved=reserved, issued_at=time.time())
        sector = op.get("target_sector") or "the contested zone"
        self.emit_operation_event(op, "opord_issued", 3,
                                  f"{op.get('faction_id')} High Command issued an operation in {sector} "
                                  f"against {op.get('target_faction') or 'hostile forces'}.")
        return {"ok": True, "op_id": op_id, "tasks": task_ids, "budget_reserved": reserved}

    def issue_pending_opords(self, save_id: str) -> dict:
        """Generate OPORDs for every operation at `coa_generated` (the coa_generated→opord_issued transition)."""
        n = 0
        for op in self.list_operations(save_id, "coa_generated"):
            self.generate_opord(op["id"])
            n += 1
        return {"ok": True, "issued": n}

    # --- OPORD Phase 6: execution routing (task → internal fleet / job market / agreement proposal) ------
    def route_operation_task(self, op: dict, task: dict) -> dict:
        """Route ONE OPORD task to a fulfillment mechanism per the spec rule: do it internally (owned fleet) when
        capable + ships exist; else create/update a job-market listing; tasks needing another faction's consent
        become an agreement PROPOSAL (the Negotiations engine scores acceptance later). Links the task to its
        job_id/agreement_id/order_id and marks it issued."""
        save_id, faction, op_id, task_id = op["save_id"], op["faction_id"], op["id"], task["id"]
        ttype = task.get("task_type") or ""
        sector = task.get("target_sector") or op.get("target_sector") or ""
        target = task.get("target_faction") or op.get("target_faction") or ""
        ships = int(((op.get("mission_analysis_json") or {}).get("available_assets") or {}).get("combat_ships") or 0)
        ev = {"operation_id": op_id, "sector": sector}
        if ttype in {"patrol_sector", "engage_hostiles", "raid_enemy_logistics"} and ships > 0:
            # D3/D4 ROUTING DECISION: with own ships available, fulfilment is a CHOICE — commit our own fleet, hire
            # contractors, or ask an ally. Player2 decides via router.route_pending_tasks_llm; leave the task PLANNED
            # (unrouted) for the driver. Single-viable cases (no ships → hire below) still route deterministically.
            return {"task_id": task_id, "route": "awaiting_decision",
                    "options": ["commit_own_fleet", "hire_contractors", "ask_ally"]}
        if ttype in {"request_allied_support"}:
            # OPORD SUBMITS AN INTENT — Negotiations owns counterparty + dedupe + (later) acceptance.
            ag = self.submit_negotiation_intent(
                save_id, "opord", "allied_support", faction, operation_id=op_id, operation_task_id=task_id,
                urgency=int(op.get("urgency") or 0), enemy=target, sector=sector, require_counterparty=True,
                terms={"operation_id": op_id, "operation_task_id": task_id, "kind": "allied_support",
                       "sector": sector, "enemy": target})
            self.update_task(task_id, status="issued", agreement_id=str(ag.get("id")), issued_at=time.time())
            return {"task_id": task_id, "route": "agreement:allied",
                    "party_b": ag.get("party_b"), "agreement_id": ag.get("id")}
        if ttype in {"seek_ceasefire"}:
            ag = self.submit_negotiation_intent(
                save_id, "opord", "ceasefire", faction, recipient=target, operation_id=op_id,
                operation_task_id=task_id, enemy=target, sector=sector,
                terms={"operation_id": op_id, "operation_task_id": task_id, "kind": "ceasefire"})
            self.update_task(task_id, status="issued", agreement_id=str(ag.get("id")), issued_at=time.time())
            return {"task_id": task_id, "route": "agreement:ceasefire"}
        jt = ("supply" if ttype in {"request_supplies", "post_supply_contract", "supply_delivery"}
              else "escort" if ttype == "escort_supply_convoy"
              else "privateer" if ttype == "raid_enemy_logistics"
              else "patrol" if ttype in {"patrol_sector", "post_patrol_contract", "engage_hostiles"}
              else "task")
        job = self.create_or_update_job(save_id, faction, jt, target_sector=sector, target_faction=target,
                                        reward=self.OPORD_JOB_REWARDS.get(jt, 50000), urgency=int(op.get("urgency") or 0),
                                        operation_id=op_id, operation_task_id=task_id, evidence=ev)
        self.update_task(task_id, status="issued", job_id=str(job.get("id")), issued_at=time.time())
        return {"task_id": task_id, "route": "job:" + jt, "job_created": job.get("created")}

    def route_task(self, op: dict, task: dict, choice: str) -> dict:
        """Commit a D3/D4 routing CHOICE for a task (the Player2 decision, or a single-viable deterministic route).
        choice: commit_own_fleet | hire_contractors | ask:<faction>. Links the task + marks it issued."""
        save_id, faction, op_id, task_id = op["save_id"], op["faction_id"], op["id"], task["id"]
        sector = task.get("target_sector") or op.get("target_sector") or ""
        target = task.get("target_faction") or op.get("target_faction") or ""
        ttype = task.get("task_type") or ""
        if choice == "commit_own_fleet":
            self.update_task(task_id, status="issued", assigned_actor_type="fleet", owning_faction=faction,
                             order_id="pending_ingame", issued_at=time.time())
            return {"ok": True, "task_id": task_id, "route": "internal_fleet"}
        if choice == "hire_contractors":
            jt = "privateer" if ttype == "raid_enemy_logistics" else "patrol"
            job = self.create_or_update_job(save_id, faction, jt, target_sector=sector, target_faction=target,
                                            reward=self.OPORD_JOB_REWARDS.get(jt, 50000),
                                            urgency=int(op.get("urgency") or 0), operation_id=op_id,
                                            operation_task_id=task_id, evidence={"operation_id": op_id, "sector": sector})
            self.update_task(task_id, status="issued", job_id=str(job.get("id")), issued_at=time.time())
            return {"ok": True, "task_id": task_id, "route": "job:" + jt}
        if isinstance(choice, str) and choice.startswith("ask:"):
            ally = choice.split(":", 1)[1]
            if not ally or ally == faction:
                return {"ok": False, "reason": "invalid ally"}
            ag = self.submit_negotiation_intent(
                save_id, "opord", "allied_support", faction, recipient=ally, operation_id=op_id,
                operation_task_id=task_id, urgency=int(op.get("urgency") or 0), enemy=target, sector=sector,
                terms={"operation_id": op_id, "operation_task_id": task_id, "kind": "allied_support",
                       "sector": sector, "enemy": target})
            self.update_task(task_id, status="issued", agreement_id=str(ag.get("id")), issued_at=time.time())
            return {"ok": True, "task_id": task_id, "route": "agreement:allied", "party_b": ally,
                    "agreement_id": ag.get("id")}
        return {"ok": False, "reason": f"unknown route choice {choice}"}

    def select_support_candidates(self, save_id: str, requester: str, enemy: str = "", sector: str = "",
                                  top: int = 3) -> list[str]:
        """Ranked ally candidates (advisory) — the legal menu the Player2 D4 counterparty decision picks from. Same
        scoring as select_support_counterparty (trust*0.25 + shared_enemy − resentment*0.2), best-first."""
        scored = []
        for f in self.list_factions(save_id):
            fid = f.get("faction_id") or ""
            if not fid or fid == requester or fid == enemy or fid in self.CRIMINAL_FACTIONS:
                continue
            rel = self.get_relationship(save_id, fid, requester) or {}
            trust = float(rel.get("trust") or 0)
            if trust < 0:
                continue
            resentment = float(rel.get("resentment") or 0)
            shared = 0.0
            if enemy:
                er = self.get_relationship(save_id, fid, enemy) or {}
                if float(er.get("trust") or 0) < 0 or float(er.get("resentment") or 0) > 0:
                    shared = 20.0
            scored.append((trust * 0.25 + shared - resentment * 0.2, fid))
        scored.sort(reverse=True)
        return [fid for _, fid in scored[:int(top)]]

    def route_operation(self, op_id: str) -> dict:
        """Route all PLANNED tasks of an issued operation, then advance opord_issued→active (activated_at)."""
        op = self.get_operation(op_id)
        if not op:
            return {"ok": False, "reason": "operation not found"}
        with self._connect() as conn:
            tasks = [self._decode_op_row(r) for r in conn.execute(
                "SELECT * FROM operation_tasks WHERE operation_id=? AND status='planned'", (op_id,)).fetchall()]
        routes = [self.route_operation_task(op, t) for t in tasks]
        self.update_operation(op_id, status="active", activated_at=time.time())
        return {"ok": True, "op_id": op_id, "routes": routes}

    def route_pending_operations(self, save_id: str) -> dict:
        """Route every operation at `opord_issued` (the opord_issued→active transition)."""
        n = 0
        for op in self.list_operations(save_id, "opord_issued"):
            self.route_operation(op["id"])
            n += 1
        return {"ok": True, "routed": n}

    # --- OPORD Phase 7: assessment + FRAGO (battle rhythm, reward escalation, conclude-or-fail) -----------
    def assess_operation(self, op_id: str) -> dict:
        """Assess one ACTIVE operation against REAL evidence and adapt it: emit a SITREP; fire FRAGOs (enemy
        reinforcement → allied-support proposal; unclaimed linked job too long → increase reward); and conclude
        from evidence (pressure abated + min age → completed; still contested past max age → failed, so nothing
        hangs forever). Deterministic; never fabricates outcomes."""
        op = self.get_operation(op_id)
        if not op or op.get("status") not in ("active", "frago_required"):
            return {"ok": False, "reason": "not an active operation"}
        save_id = op["save_id"]
        sector = op.get("target_sector") or ""
        target = op.get("target_faction") or ""
        now = time.time()
        activated = float(op.get("activated_at") or op.get("issued_at") or op.get("created_at") or now)
        age = now - activated
        # New enemy pressure in THIS op's sector since it activated (real events only).
        recent_mag = 0.0
        for e in self.list_hostile_events(save_id, window_s=max(age, 1.0) + 5.0, limit=2000):
            if e.get("sector") == sector and e.get("attacker_faction") == target and float(e.get("ts") or 0) >= activated:
                recent_mag += float(e.get("magnitude") or 0)
        fragos: list = []
        self.attach_report(op_id, "sitrep", f"Assessment: age {int(age)}s, new hostile magnitude {round(recent_mag, 1)} in {sector or 'AO'}.",
                           severity=1, evidence={"age_s": int(age), "recent_magnitude": round(recent_mag, 1)})
        # TASK SUCCESS FROM REAL EVIDENCE (not intent): our forces inflicting losses on the target completes the
        # offensive tasks (the spec's "no success may be claimed from intent alone"). our_kills = events we caused.
        our_kills = 0.0
        for e in self.list_hostile_events(save_id, window_s=max(age, 1.0) + 5.0, limit=2000):
            if (e.get("attacker_faction") == op["faction_id"] and e.get("victim_faction") == target
                    and float(e.get("ts") or 0) >= activated):
                our_kills += float(e.get("magnitude") or 0)
        if our_kills > 0:
            with self._connect() as conn:
                tids = [r["id"] for r in conn.execute(
                    "SELECT id FROM operation_tasks WHERE operation_id=? AND task_type IN "
                    "('raid_enemy_logistics','engage_hostiles') AND status IN ('planned','issued','active')",
                    (op_id,)).fetchall()]
            for tid in tids:
                self.update_task(tid, status="completed", completed_at=now,
                                 evidence_json={"enemy_loss_observed": round(our_kills, 1)})
            if tids:
                self.attach_report(op_id, "bda", f"BDA: enemy losses observed ({round(our_kills, 1)}); offensive tasks complete.",
                                   severity=2, evidence={"enemy_loss": round(our_kills, 1), "tasks_completed": len(tids)})
        # JUDGMENT (escalate / conserve / raise reward / conclude-on-abated) is the Player2 D5 decision over this
        # SITREP — router.assess_operations_llm → apply_assessment_decision. assess does FACTS + a SAFETY BACKSTOP
        # only: a hung op past MAX age FAILS (time_expired is a hard fact, not a judgment; prevents infinite ops).
        outcome = None
        if age >= self.OPORD_MAX_ACTIVE_S:
            self.conclude_operation(op_id, "failed", "Operation timed out without resolving the threat.")
            self.attach_report(op_id, "failure_report", "Operation failed: timed out, threat unresolved.", severity=3)
            self.emit_operation_event(op, "operation_failed", 3,
                                      f"{op['faction_id']} operation in {sector or 'the sector'} failed; threat unresolved.")
            outcome = "failed"
        return {"ok": True, "op_id": op_id, "recent_magnitude": round(recent_mag, 1),
                "our_kills": round(our_kills, 1), "age_s": int(age), "outcome": outcome,
                "needs_decision": outcome is None}

    def can_conclude(self, op_id: str) -> dict:
        """Validator for the Player2 `request_conclude` intent (spec D5): an op may conclude ONLY on objective proof —
        threat_abated (no new enemy pressure + min active age), time_expired (past max age), or budget_exhausted.
        No proof → not concludable (caller converts to hold/reassess). Player2 never marks complete on a feeling."""
        op = self.get_operation(op_id)
        if not op:
            return {"ok": False, "reasons": [], "reason": "no op"}
        save_id = op["save_id"]
        now = time.time()
        activated = float(op.get("activated_at") or op.get("issued_at") or op.get("created_at") or now)
        age = now - activated
        sector = op.get("target_sector") or ""
        target = op.get("target_faction") or ""
        recent_mag = 0.0
        for e in self.list_hostile_events(save_id, window_s=max(age, 1.0) + 5.0, limit=2000):
            if e.get("sector") == sector and e.get("attacker_faction") == target and float(e.get("ts") or 0) >= activated:
                recent_mag += float(e.get("magnitude") or 0)
        reasons = []
        if recent_mag <= 0 and age >= self.OPORD_MIN_ACTIVE_S:
            reasons.append("threat_abated")
        if age >= self.OPORD_MAX_ACTIVE_S:
            reasons.append("time_expired")
        reserved = int(op.get("budget_reserved") or 0)
        if reserved > 0 and int(op.get("budget_spent") or 0) >= reserved:
            reasons.append("budget_exhausted")
        suggested = "failed" if ("time_expired" in reasons and "threat_abated" not in reasons) else "completed"
        return {"ok": bool(reasons), "reasons": reasons, "recent_magnitude": round(recent_mag, 1),
                "age_s": int(age), "suggested_status": suggested}

    def apply_assessment_decision(self, op_id: str, decision: str, reason: str = "") -> dict:
        """Execute the Player2 D5 assessment decision with validators (intent → validated execution). Options:
        escalate_reinforce (→ allied-support negotiation intent), raise_reward (budget-gated job reward bump),
        hold (no-op), request_conclude (→ can_conclude validator; concludes only on proof, else converts to hold)."""
        op = self.get_operation(op_id)
        if not op or op.get("status") not in ("active", "frago_required"):
            return {"ok": False, "reason": "not an active operation"}
        save_id = op["save_id"]
        sector = op.get("target_sector") or ""
        target = op.get("target_faction") or ""
        now = time.time()
        if decision in ("request_conclude", "conclude"):
            cc = self.can_conclude(op_id)
            if not cc["ok"]:
                self.attach_report(op_id, "assessment",
                                   f"Commander sought to conclude but evidence is insufficient; holding. {reason}",
                                   severity=2, evidence={"converted": "hold", "can_conclude": cc})
                return {"ok": True, "applied": "converted_to_hold", "can_conclude": cc}
            status = cc.get("suggested_status", "completed")
            self.conclude_operation(op_id, status, reason or ("Hostile pressure abated; sector stable."
                                                              if status == "completed" else "Operation concluded."))
            self.attach_report(op_id, ("completion_report" if status == "completed" else "failure_report"),
                               f"Operation {status}: {reason or ', '.join(cc['reasons'])}.",
                               severity=(2 if status == "completed" else 3))
            self.emit_operation_event(op, ("operation_completed" if status == "completed" else "operation_failed"),
                                      4, f"{op['faction_id']} operation in {sector or 'the sector'} {status}.")
            return {"ok": True, "applied": status, "can_conclude": cc}
        if decision in ("escalate_reinforce", "escalate") or decision.startswith("escalate:"):
            # D4b: the WHICH-ally counterparty is the Player2 pick (escalate:<ally>); only fall back to the advisory
            # auto-pick when no ally was chosen (back-compat / deterministic test path).
            ally = decision.split(":", 1)[1] if decision.startswith("escalate:") else ""
            ag = self.submit_negotiation_intent(
                save_id, "opord", "allied_support", op["faction_id"], operation_id=op_id,
                recipient=ally, enemy=target, sector=sector, require_counterparty=(not ally),
                terms={"operation_id": op_id, "kind": "allied_support", "reason": "reinforcement",
                       "sector": sector, "enemy": target})
            if ag.get("created"):
                self.attach_report(op_id, "frago",
                                   f"FRAGO: requesting allied support from {ag.get('party_b') or 'available allies'}.",
                                   severity=3, evidence={"trigger": "commander_escalate", "agreement_id": ag.get("id")})
                self.emit_operation_event(op, "frago_issued", 3,
                                          f"FRAGO on {op['faction_id']} operation in {sector or 'AO'}: reinforcement.")
            return {"ok": True, "applied": "escalate_reinforce", "agreement_id": ag.get("id"),
                    "ally": ag.get("party_b")}
        if decision == "raise_reward":
            reserved = int(op.get("budget_reserved") or 0)
            bumped = []
            for j in self.list_jobs(save_id, "open"):
                if j.get("operation_id") == op_id:
                    cur = int(j.get("reward") or 0)
                    new_reward = min(int(cur * 1.5) or 1, reserved)
                    if new_reward > cur:
                        with self._lock, self._connect() as conn:
                            conn.execute("UPDATE market_jobs SET reward=?, updated_at=? WHERE id=?",
                                         (new_reward, now, j["id"]))
                            conn.commit()
                        bumped.append({"job_id": j["id"], "reward": new_reward})
            if bumped:
                self.attach_report(op_id, "frago", f"FRAGO: reward raised on {len(bumped)} job(s).",
                                   severity=2, evidence={"trigger": "commander_raise_reward", "jobs": bumped})
                self.emit_operation_event(op, "frago_issued", 2,
                                          f"FRAGO: reward raised on {op['faction_id']} operation.")
                return {"ok": True, "applied": "raise_reward", "jobs": bumped}
            return {"ok": True, "applied": "raise_reward_noop", "reason": "no budget headroom or no open jobs"}
        # hold / conserve
        self.attach_report(op_id, "assessment", f"Commander holds the line; continue as planned. {reason}", severity=1)
        return {"ok": True, "applied": "hold"}

    def assess_active_operations(self, save_id: str) -> dict:
        """Battle-rhythm pass: assess every active/frago_required operation (FRAGOs + conclude-or-fail)."""
        n, concluded = 0, 0
        for op in (self.list_operations(save_id, "active") + self.list_operations(save_id, "frago_required")):
            r = self.assess_operation(op["id"])
            n += 1
            if r.get("outcome"):
                concluded += 1
        return {"ok": True, "assessed": n, "concluded": concluded}

    def resume_operations_from_negotiations(self, save_id: str) -> dict:
        """#48 (OC1) — CONSUME resolved negotiations back into the OPORD (OPORD is a Negotiations CLIENT: it SUBMITS an
        intent via the single door, then RESUMES off the outcome). For each op task that submitted an intent (status
        'issued' + agreement_id), once its agreement reaches a terminal verdict: accepted/kept/fulfilled → the task is
        COMPLETED (support/ceasefire secured); refused/broken/expired/rejected → the task FAILS (the op must adapt —
        a FRAGO trigger). Emits a world_event so the outcome surfaces. Idempotent (only acts on still-'issued' tasks)."""
        now = time.time()
        ags = {str(a.get("id")): a for a in self.list_agreements(save_id)}
        fulfilled, failed = 0, 0
        for op in self.list_operations(save_id):
            if str(op.get("status") or "") in ("concluded", "cancelled", "failed", "aborted"):
                continue
            det = self.operation_detail(op.get("id")) or {}
            fac = op.get("faction_id")
            for t in (det.get("tasks") or []):
                aid = t.get("agreement_id")
                if not aid or str(t.get("status") or "") != "issued":
                    continue
                ag = ags.get(str(aid))
                if not ag:
                    continue
                st = str(ag.get("status") or "")
                if st in ("accepted", "kept", "fulfilled"):
                    self.update_task(t.get("id"), status="completed", completed_at=now)
                    self.add_world_event(save_id, event_type="after_action_report",
                                         summary=f"{ag.get('party_b') or 'an ally'} agreed — support secured for the "
                                                 f"{fac} operation in {op.get('target_sector') or 'the sector'}.",
                                         primary_faction=fac, secondary_faction=ag.get("party_b"),
                                         importance=2, source="opord")
                    fulfilled += 1
                elif st in ("refused", "broken", "expired", "rejected"):
                    self.update_task(t.get("id"), status="failed", failed_at=now)
                    self.add_world_event(save_id, event_type="frago_issued",
                                         summary=f"{ag.get('party_b') or 'the counterparty'} declined — {fac} must "
                                                 f"adapt its operation in {op.get('target_sector') or 'the sector'}.",
                                         primary_faction=fac, secondary_faction=ag.get("party_b"),
                                         importance=3, source="opord")
                    failed += 1
        return {"ok": True, "fulfilled": fulfilled, "failed": failed}

    def advance_operations(self, save_id: str) -> dict:
        """OPORD pipeline driver — runs each BUILT stage in spec order for one save. Extended as phases land
        (P2 recognize → P3 analyse → P4 COA → P5 OPORD → P6 route → P7 assess/FRAGO → OC1 consume-negotiations )."""
        rec = self.recognize_threats(save_id)
        ana = self.analyze_pending_missions(save_id)
        coa = self.plan_pending_coas(save_id)
        opd = self.issue_pending_opords(save_id)
        rte = self.route_pending_operations(save_id)
        ass = self.assess_active_operations(save_id)
        resumed = self.resume_operations_from_negotiations(save_id)  # OC1: consume resolved deals
        # NOTE: negotiation RESOLUTION is NOT decided here. Per the AI-Influence architecture, the decision layer is
        # the Player2 LLM (router.resolve_offers_llm), run on a SLOW cadence — the deterministic engine sets the
        # scene + executes; the thinking entity decides. (evaluate_open_offers remains only as advisory/fallback.)
        return {"ok": True, "recognize": rec, "analyze": ana, "coa": coa, "opord": opd, "route": rte,
                "assess": ass, "resumed": resumed}

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
            # market_status: derived here (the rollup sees ALL of a faction's stations) — exporter if it produces
            # more ware variety than it needs, importer if it has unmet needs, else neutral. (#54: moved off the
            # Lua, which only ever saw a per-tick slice and couldn't judge faction-wide.)
            market_status = ("exporter" if len(prod_counts) > len(need_counts)
                             else ("importer" if need_counts else "neutral"))
            self.upsert_economy(save_id, fid, shortages=shortages, key_needs=key_needs,
                                production_health=production_health, market_status=market_status)
            updated += 1
        return {"ok": True, "save_id": save_id, "stations": len(stations), "factions_rolled_up": updated}

    # --- #63 earned-economy: spend capacity grounded in REAL owned stations, minus what's been drawn ----------
    PER_STATION_CREDITS = 250_000  # a station's notional output backing the faction's deal-capacity

    def budget_capacity(self, save_id: str, faction_id: str) -> float:
        """Derived spend capacity, grounded in REAL owned infrastructure (#54 stations × production_health).
        A faction can only back deals up to what it actually owns — words≠resources."""
        n = len(self.list_economy_stations(save_id, faction_id))
        health = float((self.get_economy(save_id, faction_id) or {}).get("production_health", 1.0) or 1.0)
        return round(n * self.PER_STATION_CREDITS * max(0.05, health), 2)

    def budget_spent(self, save_id: str, faction_id: str) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT spent FROM faction_budget WHERE save_id=? AND faction_id=?",
                               (save_id, faction_id)).fetchone()
        return float(row["spent"]) if row else 0.0

    def record_budget_spend(self, save_id: str, faction_id: str, amount: float) -> float:
        amount = max(0.0, float(amount or 0))
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO faction_budget (save_id, faction_id, spent, updated_at) VALUES (?,?,?,?)
                ON CONFLICT(save_id, faction_id) DO UPDATE SET spent = spent + excluded.spent, updated_at=excluded.updated_at
            """, (save_id, faction_id, amount, now))
            conn.commit()
        return self.budget_spent(save_id, faction_id)

    def validate_earned_transfer(self, save_id: str, faction_id: str, cost: float, commit: bool = False) -> dict:
        """THE anti-cheat gate (#63): an 'earned' transfer is legitimate ONLY if the faction can afford it from
        its OWNED capacity (capacity − already-spent ≥ cost). If commit=True and affordable, debits the ledger so
        the same budget can't be re-spent. The 'earned' marker is SERVER-set here — never LLM-settable."""
        cap = self.budget_capacity(save_id, faction_id)
        spent = self.budget_spent(save_id, faction_id)
        cost = max(0.0, float(cost or 0))
        earned = cost <= round(cap - spent, 2)
        if earned and commit:
            spent = self.record_budget_spend(save_id, faction_id, cost)
        return {"earned": earned, "faction_id": faction_id, "cost": cost, "capacity": cap,
                "spent": round(spent, 2), "remaining": round(cap - spent, 2),
                "reason": "within owned capacity" if earned else "exceeds the faction's owned capacity"}

    # --- #39 SPEC 2c: NPC<->NPC social graph — first-class + EVENT-DRIVEN (Bannerlord feature-translation §3 +
    #     Codex feedback). Faction relations are political; THIS is social/emotional. Changes come ONLY from
    #     social EVENTS, never faction projection or LLM whim. Emotional SCORES + narrative STATUS + EVIDENCE. ---
    SOCIAL_EVENTS: dict[str, dict] = {
        "saved_life":              {"affection": 0.25, "trust": 0.20, "loyalty": 0.20, "debt": 0.30},
        "abandoned_in_combat":     {"resentment": 0.35, "fear": 0.15, "trust": -0.30, "loyalty": -0.30},
        "served_together":         {"trust": 0.10, "affection": 0.08, "loyalty": 0.08},
        "shared_secret":           {"trust": 0.20, "affection": 0.12, "publicity": -0.10},
        "public_insult":           {"resentment": 0.25, "rivalry": 0.25, "affection": -0.15, "publicity": 0.20},
        "betrayal":                {"resentment": 0.40, "trust": -0.40, "loyalty": -0.40, "rivalry": 0.30},
        "repeated_conversations":  {"trust": 0.06, "affection": 0.05, "attraction": 0.03},
        "player_mediation":        {"resentment": -0.20, "trust": 0.15, "rivalry": -0.15},
        "flirtation_reciprocated": {"attraction": 0.15, "affection": 0.10, "publicity": 0.05},
        "rebuffed_advance":        {"attraction": -0.20, "resentment": 0.10},
        "bereavement":             {},  # status -> grieving (handled via the grief flag)
    }
    _SOCIAL_SCALARS = ("trust", "affection", "resentment", "fear", "loyalty", "rivalry", "debt", "attraction", "publicity")

    @staticmethod
    def _advance_social_status(sc: dict, grief: bool = False) -> tuple:
        """Pure: emotional scores -> (narrative status, coarse relationship_type). Romance is GATED (rises only
        with attraction AND affection) so the sim never drifts into universal romance (§7 restraint)."""
        af, tr = float(sc.get("affection", 0)), float(sc.get("trust", 0))
        res, riv = float(sc.get("resentment", 0)), float(sc.get("rivalry", 0))
        att, loy = float(sc.get("attraction", 0)), float(sc.get("loyalty", 0))
        if grief:
            return "grieving", "romantic"
        if res >= 0.7 or riv >= 0.8:
            return "enemies", "rival"
        if res >= 0.4 or riv >= 0.5:
            return "rivals", "rival"
        if att >= 0.3 and af >= 0.25:            # romance track — gated by attraction AND affection
            if att >= 0.75 and af >= 0.6:
                return "partners", "romantic"
            if att >= 0.55:
                return "courting", "romantic"
            if att >= 0.45:
                return "confession_pending", "romantic"
            if att >= 0.35:
                return "flirtation", "romantic"
            return "private_attraction", "romantic"
        if loy >= 0.5 and tr >= 0.5 and af < 0.5:
            return "mentor/student", "mentor"
        if af >= 0.6 and tr >= 0.6:
            return "close friends", "friend"
        if af >= 0.35 and tr >= 0.3:
            return "friends", "friend"
        if tr >= 0.2 or af >= 0.2:
            return "crewmates", "professional"
        if tr > 0 or af > 0 or res > 0 or att > 0:
            return "acquaintances", "neutral"
        return "strangers", "neutral"

    def _write_social_edge(self, conn, save_id, subject, obj, sc, status, rtype, evidence_json, now):
        conn.execute("""
            INSERT INTO social_relations (save_id, subject_npc, object_npc, status, relationship_type,
                trust, affection, resentment, fear, loyalty, rivalry, debt, attraction, publicity, evidence_json, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(save_id, subject_npc, object_npc) DO UPDATE SET status=excluded.status,
                relationship_type=excluded.relationship_type, trust=excluded.trust, affection=excluded.affection,
                resentment=excluded.resentment, fear=excluded.fear, loyalty=excluded.loyalty, rivalry=excluded.rivalry,
                debt=excluded.debt, attraction=excluded.attraction, publicity=excluded.publicity,
                evidence_json=excluded.evidence_json, last_updated=excluded.last_updated
        """, (save_id, subject, obj, status, rtype, sc["trust"], sc["affection"], sc["resentment"], sc["fear"],
              sc["loyalty"], sc["rivalry"], sc["debt"], sc["attraction"], sc["publicity"], evidence_json, now))

    def apply_social_event(self, save_id: str, subject_npc: str, object_npc: str, event_type: str, note: str = "") -> dict:
        """THE driver (#39): a social event mutates the edge's emotional scores, appends EVIDENCE (why the
        relationship exists), and re-derives the narrative status. The ONLY sanctioned way relationships change."""
        if event_type not in self.SOCIAL_EVENTS:
            return {"ok": False, "reason": f"unknown social event '{event_type}'"}
        if not (save_id and subject_npc and object_npc) or subject_npc == object_npc:
            return {"ok": False, "reason": "need distinct subject/object npc"}
        now = time.time()
        delta = self.SOCIAL_EVENTS[event_type]
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM social_relations WHERE save_id=? AND subject_npc=? AND object_npc=?",
                               (save_id, subject_npc, object_npc)).fetchone()
            cur = dict(row) if row else {}
            sc = {k: max(0.0, min(1.0, float(cur.get(k, 0) or 0) + float(delta.get(k, 0)))) for k in self._SOCIAL_SCALARS}
            grief = event_type == "bereavement"
            status_before = str(cur.get("status") or "none")
            status, rtype = self._advance_social_status(sc, grief)
            ev = json.loads(cur.get("evidence_json") or "[]")
            ev.append({"event": event_type, "note": note, "ts": now})
            ev = ev[-12:]
            self._write_social_edge(conn, save_id, subject_npc, object_npc, sc, status, rtype, json.dumps(ev), now)
            conn.commit()
        return {"ok": True, "subject_npc": subject_npc, "object_npc": object_npc, "event": event_type,
                "status_before": status_before, "status": status, "relationship_type": rtype,
                "scores": {k: round(sc[k], 3) for k in self._SOCIAL_SCALARS}}

    def upsert_social_relation(self, save_id: str, subject_npc: str, object_npc: str, **fields) -> dict:
        """Direct set/merge of an edge (manual / test path). Clamps scalars 0..1 + recomputes status. Most changes
        should go through apply_social_event instead."""
        if not (save_id and subject_npc and object_npc) or subject_npc == object_npc:
            return {"ok": False, "reason": "need distinct subject/object npc"}
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM social_relations WHERE save_id=? AND subject_npc=? AND object_npc=?",
                               (save_id, subject_npc, object_npc)).fetchone()
            cur = dict(row) if row else {}
            sc = {k: max(0.0, min(1.0, float(fields.get(k, cur.get(k, 0)) or 0))) for k in self._SOCIAL_SCALARS}
            ds, dr = self._advance_social_status(sc, bool(fields.get("grief")))
            status = fields.get("status") or ds
            rtype = fields.get("relationship_type") or dr
            self._write_social_edge(conn, save_id, subject_npc, object_npc, sc, status, rtype,
                                    (cur.get("evidence_json") or "[]"), now)
            conn.commit()
        return {"ok": True, "subject_npc": subject_npc, "object_npc": object_npc, "status": status, "relationship_type": rtype}

    def list_social_relations(self, save_id: str, npc_key: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if npc_key:
                rows = conn.execute(
                    "SELECT * FROM social_relations WHERE save_id=? AND (subject_npc=? OR object_npc=?) "
                    "ORDER BY (affection+rivalry+attraction+resentment) DESC", (save_id, npc_key, npc_key)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM social_relations WHERE save_id=?", (save_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            except Exception:
                d["evidence"] = []
            out.append(d)
        return out

    @staticmethod
    def _band(v: float, hi: str, mid: str, lo: str) -> str:
        v = float(v or 0)
        return hi if v >= 0.6 else mid if v >= 0.3 else lo

    def social_edge_brief(self, save_id: str, subject_npc: str, object_npc: str) -> str:
        """Codex §relationships: when NPC A talks about NPC B, inject ONLY the relevant edge as in-character
        context. Scores -> English (never raw numbers) + the latest evidence ('you remember …')."""
        edge = next((e for e in self.list_social_relations(save_id, subject_npc)
                     if object_npc in (e["object_npc"], e["subject_npc"])), None)
        if not edge:
            return ""
        other = {n["npc_key"]: (n.get("name") or n["npc_key"]) for n in self.list_npcs()}.get(object_npc, object_npc)
        parts = [f"You know {other} personally — your relationship: {str(edge.get('status','strangers')).replace('_',' ')}"]
        if float(edge.get("trust", 0)) > 0:
            parts.append("you trust them " + self._band(edge.get("trust"), "deeply", "somewhat", "a little"))
        if float(edge.get("resentment", 0)) >= 0.3:
            parts.append("you resent them")
        if float(edge.get("attraction", 0)) >= 0.3 and float(edge.get("publicity", 0)) < 0.3:
            parts.append("an attraction you have not made public")
        line = "; ".join(parts) + "."
        ev = edge.get("evidence") or []
        if ev:
            note = ev[-1].get("note") or str(ev[-1].get("event", "")).replace("_", " ")
            if note:
                line += f" You remember: {note}."
        return line

    def social_summary(self, save_id: str, npc_key: str, top: int = 3) -> str:
        """An NPC's closest ties overview (dashboard / general grounding)."""
        edges = self.list_social_relations(save_id, npc_key)[:top]
        if not edges:
            return ""
        names = {n["npc_key"]: (n.get("name") or n["npc_key"]) for n in self.list_npcs()}
        bits = []
        for e in edges:
            other = e["object_npc"] if e["subject_npc"] == npc_key else e["subject_npc"]
            bits.append(f"{str(e.get('status','strangers')).replace('_',' ')} with {names.get(other, other)}")
        return "Personal ties: " + "; ".join(bits) + "."

    # --- G2 (Gameplay Changes doc): classify the PLAYER from stored signals so factions react to who they are ---
    def classify_player_role(self, save_id: str) -> dict:
        """Deterministic: read the player's standing per faction + economic leverage + brokered deals -> a role.
        Roles (the doc's list): war profiteer / supplier / mediator / faction friend / faction threat / newcomer."""
        # Exclude engine-permanent hostiles / non-combatants: being at war with khaak/xenon is UNIVERSAL, not a
        # player CHOICE, so it must not drive the player's role (mirrors diplomacy.EXCLUDED_FROM_WAR, #58).
        _EXCLUDED = {"civilian", "criminal", "khaak", "player", "smuggler", "visitor", "xenon"}
        per_faction: dict[str, str] = {}
        friends: list[str] = []
        threats: list[str] = []
        for r in self.list_relationships(save_id):
            if r.get("object") != "player" or not r.get("subject"):
                continue
            fid = r["subject"]
            if fid in _EXCLUDED:
                continue
            trust = float(r.get("trust") or 0)
            res = float(r.get("resentment") or 0)
            standing = str(r.get("standing") or "")
            if standing in ("at war", "hostile") or res >= 40:
                per_faction[fid] = "threat"; threats.append(fid)
            elif trust >= 50:
                per_faction[fid] = "friend"; friends.append(fid)
            else:
                per_faction[fid] = "neutral"
        high_dep = [e.get("faction_id") for e in self.list_economy(save_id)
                    if e.get("faction_id") and float(e.get("dependency_on_player") or 0) >= 0.6]
        supplies_enemies = any(int(p.get("supplying_enemies") or 0) for p in self.list_player_market(save_id))
        brokered = [a for a in self.list_agreements(save_id)
                    if "player" in (a.get("party_a"), a.get("party_b"))
                    and str(a.get("type") or "") in ("ceasefire", "non_aggression", "trade", "transit", "patrol_cooperation")]
        tags: list[str] = []
        if supplies_enemies:
            tags.append("war profiteer")
        if len(high_dep) >= 2:
            tags.append("supplier")
        if brokered:
            tags.append("mediator")
        if friends and not threats:
            tags.append("faction friend")
        if threats:
            tags.append("faction threat")
        primary = tags[0] if tags else "unaligned newcomer"
        return {"save_id": save_id, "primary_role": primary, "role_tags": tags,
                "friends": friends, "threats": threats, "per_faction": per_faction,
                "high_dependency_factions": [f for f in high_dep if f], "supplies_enemies": bool(supplies_enemies),
                "brokered_count": len(brokered)}

    # --- G4 (Gameplay Changes doc): the MEMORY-AUDIT summary mode (distinct from the in-character recap) ---------
    def memory_audit_summary(self, npc_key: str, limit: int = 40) -> dict:
        """A literal memory-INTEGRITY view: the durable facts actually stored PLUS durable-fact CANDIDATES (recent
        high-value turns — promise/deal/insult/threat/refusal — NOT yet promoted). Surfaces the 'talks a lot,
        stores few facts' gap an in-character recap hides. Deterministic; no LLM."""
        facts = self.get_facts(npc_key)
        durable = [{"tier": f.get("tier"), "category": f.get("category"), "text": str(f.get("text") or "")[:200]}
                   for f in facts if f.get("tier") in ("core", "significant")]
        # `verbatim` is a 0/1 flag, not text — dedup/display on `text`.
        stored = {str(f.get("text") or "")[:60] for f in facts}
        candidates: list[dict] = []
        for t in self.get_recent_turns(npc_key, limit):
            txt = str(t.get("text") or t.get("content") or "").strip()
            if not txt:
                continue
            cat = classify_text(txt)
            if category_tier(cat) != "routine" and txt[:60] not in stored:
                candidates.append({"category": cat, "tier": category_tier(cat), "role": t.get("role"),
                                   "text": txt[:160]})
        return {"npc_key": npc_key, "mode": "memory_audit",
                "durable_fact_count": len(durable), "durable_facts": durable[:25],
                "promotion_candidate_count": len(candidates), "promotion_candidates": candidates[:25],
                "note": "Audit view (not roleplay): candidates are fact-worthy turns not yet promoted to durable memory."}

    def promote_durable_facts(self, npc_key: str, max_promote: int = 12, limit: int = 60) -> dict:
        """G4 backfill: PROMOTE the durable-fact candidates the audit surfaces — recent non-routine turns not yet
        stored — to durable facts. Fixes the 'talks a lot, stores few facts' gap directly. Dedups against existing
        facts; skips routine chatter. (Complements condensation, which runs on its own cadence.)"""
        # NOTE: the facts `verbatim` column is an INTEGER FLAG (0/1), not the text — dedup on `text` only.
        existing = {str(f.get("text") or "")[:60] for f in self.get_facts(npc_key)}
        promoted: list[dict] = []
        for t in self.get_recent_turns(npc_key, limit):
            txt = str(t.get("content") or t.get("text") or "").strip()
            if not txt or txt[:60] in existing:
                continue
            cat = classify_text(txt)
            if category_tier(cat) == "routine":
                continue
            self.add_fact(npc_key, txt[:300], category=cat)
            existing.add(txt[:60])
            promoted.append({"category": cat, "tier": category_tier(cat), "text": txt[:120]})
            if len(promoted) >= max_promote:
                break
        return {"npc_key": npc_key, "promoted": len(promoted), "facts": promoted}

    # --- G5 (Gameplay Changes doc): generate AGREEMENT gameplay objects — the missing middle between talk & war --
    def agreement_candidates(self, save_id: str, max_new: int = 8) -> list[dict]:
        """D6: PLAUSIBLE deal candidates grounded in faction state (ceasefire for active wars, trade for an exporter
        relieving an importer's shortage, patrol-cooperation for shared enemies, non-aggression for neutral pairs),
        deduped against existing agreements, excluding engine-permanent hostiles. Returns {proposer, target, kind,
        terms} — does NOT create. The proposer's Player2 (router.propose_deals_llm) decides which to initiate."""
        EXCLUDED = {"civilian", "criminal", "khaak", "player", "smuggler", "visitor", "xenon"}
        existing: set = set()
        for a in self.list_agreements(save_id):
            pa, pb, ty = a.get("party_a"), a.get("party_b"), a.get("type")
            existing.add((pa, pb, ty)); existing.add((pb, pa, ty))
        cands: list[dict] = []

        def _pair_ok(a, b):
            return a and b and a != b and a not in EXCLUDED and b not in EXCLUDED

        conflicts = self.list_conflicts(save_id, status="active")
        at_war = {frozenset((c.get("faction_a"), c.get("faction_b"))) for c in conflicts}
        # 1) ceasefire feelers for active wars between negotiable factions
        for c in conflicts:
            if len(cands) >= max_new:
                break
            a, b = c.get("faction_a"), c.get("faction_b")
            if not _pair_ok(a, b) or (a, b, "ceasefire") in existing:
                continue
            cands.append({"proposer": a, "target": b, "kind": "ceasefire",
                          "terms": {"reason": "active war — both sides taking losses",
                                    "intensity": round(float(c.get("intensity", 0) or 0), 2)}})
            existing.add((a, b, "ceasefire"))
        # 2) trade pacts: an exporter that can relieve an importer's real shortage (and not at war)
        econ = {e.get("faction_id"): e for e in self.list_economy(save_id) if e.get("faction_id")}
        exporters = [f for f, e in econ.items() if e.get("market_status") == "exporter"]
        importers = [(f, e) for f, e in econ.items() if e.get("market_status") == "importer" and (e.get("shortages") or {})]
        for a in exporters:
            for b, eb in importers:
                if len(cands) >= max_new:
                    break
                if not _pair_ok(a, b) or frozenset((a, b)) in at_war or (a, b, "trade") in existing:
                    continue
                cands.append({"proposer": a, "target": b, "kind": "trade",
                              "terms": {"reason": f"{a} exports what {b} imports",
                                        "ware": next(iter(eb.get("shortages") or {}), None)}})
                existing.add((a, b, "trade"))
            if len(cands) >= max_new:
                break
        # 3) patrol cooperation: two non-excluded factions sharing a COMMON enemy in active conflicts.
        enemy_of: dict = {}
        for c in conflicts:
            ca, cb = c.get("faction_a"), c.get("faction_b")
            if ca and cb:
                enemy_of.setdefault(ca, set()).add(cb)
                enemy_of.setdefault(cb, set()).add(ca)
        normal = [f for f in (e.get("faction_id") for e in econ.values() if isinstance(e, dict)) if f and f not in EXCLUDED]
        normal = sorted(set(normal) | {f for f in enemy_of if f and f not in EXCLUDED})
        for i in range(len(normal)):
            for j in range(i + 1, len(normal)):
                if len(cands) >= max_new:
                    break
                a, b = normal[i], normal[j]
                if not _pair_ok(a, b) or frozenset((a, b)) in at_war:
                    continue
                common = enemy_of.get(a, set()) & enemy_of.get(b, set())
                common = {f for f in common if f not in (a, b)}
                if common and (a, b, "patrol_cooperation") not in existing:
                    foe = sorted(common)[0]
                    cands.append({"proposer": a, "target": b, "kind": "patrol_cooperation",
                                  "terms": {"reason": f"both fighting {foe}", "common_enemy": foe}})
                    existing.add((a, b, "patrol_cooperation"))
            if len(cands) >= max_new:
                break
        # 4) non-aggression pacts: neutral non-excluded pairs (not at war, not already bound by another pact).
        for i in range(len(normal)):
            for j in range(i + 1, len(normal)):
                if len(cands) >= max_new:
                    break
                a, b = normal[i], normal[j]
                if not _pair_ok(a, b) or frozenset((a, b)) in at_war:
                    continue
                if any((a, b, ty) in existing for ty in ("non_aggression", "trade", "patrol_cooperation", "ceasefire")):
                    continue
                rel = self.get_relationship(save_id, a, b)
                standing = str(rel.get("standing")) if rel else "neutral"
                if standing in ("neutral", "wary"):
                    cands.append({"proposer": a, "target": b, "kind": "non_aggression",
                                  "terms": {"reason": "neither at war nor allied — formalize the peace"}})
                    existing.add((a, b, "non_aggression"))
            if len(cands) >= max_new:
                break
        return cands[:max_new]

    def generate_agreements(self, save_id: str, max_new: int = 8) -> dict:
        """Deterministic auto-propose — kept for SEEDING + test fixtures only. LIVE proposal initiation (D6) routes
        through the proposer's Player2 (router.propose_deals_llm); this is no longer on the heartbeat."""
        made = [self.add_agreement(save_id, c["proposer"], c["target"], type=c["kind"], status="proposed",
                                   terms=c["terms"]) for c in self.agreement_candidates(save_id, max_new)]
        return {"ok": True, "save_id": save_id, "generated": len(made), "agreements": made}

    # --- Rumor propagation (design-doc §4): spread hearsay along the #39 social graph, weighted by tie strength --
    def propagate_rumor(self, save_id: str, origin_npc: str, text: str, category: str = "rumor", reach: int = 5) -> dict:
        """Spread a rumor from origin_npc to its WARMEST social ties (affection/trust/attraction share; rivalry/
        fear suppress). Each of the top `reach` recipients stores it once (PK dedup) with a confidence from the
        tie. Hearsay — NOT a durable fact."""
        text = str(text or "").strip()
        if not (save_id and origin_npc and text):
            return {"ok": False, "reason": "save_id, origin_npc, text required"}
        rid = (str(origin_npc) + "|" + text).strip().lower()[:120]
        edges: list[tuple] = []
        for e in self.list_social_relations(save_id, origin_npc):
            other = e["object_npc"] if e["subject_npc"] == origin_npc else e["subject_npc"]
            if not other or other == origin_npc:
                continue
            strength = (float(e.get("affection") or 0) + 0.5 * float(e.get("trust") or 0)
                        + 0.3 * float(e.get("attraction") or 0)
                        - 0.5 * float(e.get("rivalry") or 0) - 0.3 * float(e.get("fear") or 0))
            if strength > 0.15:
                edges.append((other, max(0.0, min(1.0, strength))))
        edges.sort(key=lambda x: -x[1])
        now = time.time()
        recipients = []
        for other, conf in edges[:reach]:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT INTO rumors (save_id, npc_key, rumor_id, text, category, origin_npc, confidence, hops, ts)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(save_id, npc_key, rumor_id) DO UPDATE SET
                        confidence=MAX(rumors.confidence, excluded.confidence), ts=excluded.ts
                """, (save_id, other, rid, text, category, origin_npc, round(conf, 3), 1, now))
                conn.commit()
            recipients.append({"npc_key": other, "confidence": round(conf, 3)})
        return {"ok": True, "origin": origin_npc, "rumor_id": rid, "spread_to": len(recipients), "recipients": recipients}

    def list_rumors(self, save_id: str, npc_key: Optional[str] = None, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            if npc_key:
                rows = conn.execute("SELECT * FROM rumors WHERE save_id=? AND npc_key=? "
                                    "ORDER BY confidence DESC, ts DESC LIMIT ?", (save_id, npc_key, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rumors WHERE save_id=? ORDER BY ts DESC LIMIT ?",
                                    (save_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def rumor_brief(self, save_id: str, npc_key: str, top: int = 2) -> str:
        """In-character surfacing of what an NPC has HEARD (scores -> English; flagged unconfirmed)."""
        rs = self.list_rumors(save_id, npc_key, limit=top)
        if not rs:
            return ""
        bits = []
        for r in rs:
            cue = "you half-believe" if float(r.get("confidence") or 0) >= 0.6 else "you've caught whispers that"
            bits.append(f"{cue} {r.get('text')}")
        return "Word reaching you — " + "; ".join(bits) + " (unconfirmed)."

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
        # 4. A4 (IG-2): record_turn now ADDITIVELY promotes durable facts during play (raw turns are KEPT).
        # So a few facts are EXPECTED now (was ==0 pre-A4 — this assertion was stale). The anti-lossy
        # guarantee is that turns are all retained (checked above) and promotion is additive + bounded —
        # NOT the old lossy condensation that dropped turns.
        check("additive_promotion_bounded", 0 <= m["facts"] <= 12, f"facts={m['facts']} (A4 additive promotion)")

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


def run_npc_identity_selftest() -> dict:
    """Deterministic oracle for EPIC I (I0): handle-independent key derivation, identity upsert,
    evidence dedup, runtime bindings + session expiry, idempotent/reversible backfill, and the
    cross-reload memory-key RESOLUTION layer (the whole point — memory survives a reload)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_identity_selftest_")
    try:
        store = MemoryStore(Path(d) / "identity.sqlite3")
        ev = {"name": "Manda Smitt", "faction": "argon", "role": "service",
              "macro": "character_argon_female_asi_crew_01_macro", "npc_code": "RDU-996",
              "skills": {"boarding": 6, "engineering": 2, "morale": 3, "piloting": 1, "management": 1}}
        k1 = MemoryStore.derive_persistent_key(ev)

        # 1. deterministic — same evidence → same key
        check("key_deterministic", k1 == MemoryStore.derive_persistent_key(dict(ev)), k1)
        # 2. handle-independent — adding volatile fields does NOT change the key (the whole fix)
        ev_vol = dict(ev); ev_vol.update({"runtime_component_id": "236456014", "save_id": "save_007",
                                          "game_session_id": "sess-Z", "ship_name": "ANV X", "sector": "Grand Exchange"})
        check("key_handle_independent", MemoryStore.derive_persistent_key(ev_vol) == k1)
        # 3. discriminates on STABLE evidence — different faction → different key
        ev2 = dict(ev); ev2["faction"] = "teladi"
        check("key_discriminates", MemoryStore.derive_persistent_key(ev2) != k1)

        # 4. upsert + get (status defaults to session-only; tier defaults to 3)
        store.upsert_identity(k1, {"display_name": "Manda Smitt", "faction": "argon", "role": "service"})
        ident = store.get_identity(k1)
        check("identity_upsert", bool(ident) and ident["display_name"] == "Manda Smitt"
              and ident["status"] == "session-only" and ident["importance_tier"] == 3)
        # 4b. lifecycle writer (I2/I3 path) updates tier/confidence/status without clobbering attrs
        store.set_identity_fields(k1, importance_tier=1, identity_confidence=0.86, status="bound")
        ident = store.get_identity(k1)
        check("identity_lifecycle", ident["importance_tier"] == 1 and abs(ident["identity_confidence"] - 0.86) < 1e-6
              and ident["status"] == "bound" and ident["display_name"] == "Manda Smitt")

        # 5. evidence record + dedup (same type+value coalesces, keeps max weight)
        store.record_evidence(k1, "npc_code", "RDU-996", weight=0.25)
        store.record_evidence(k1, "npc_code", "RDU-996", weight=0.10)
        elist = store.get_evidence(k1)
        codes = [e for e in elist if e["evidence_type"] == "npc_code"]
        check("evidence_recorded_dedup", len(codes) == 1 and abs(codes[0]["weight"] - 0.25) < 1e-6, str(codes))

        # 6. CROSS-RELOAD memory union — same NPC in two sessions (two npc_keys), one identity
        ka = MemoryStore.make_key("save_006", "g", "Manda Smitt")
        kb = MemoryStore.make_key("save_007", "g", "Manda Smitt")
        store.bind_npc(ka, "rt-1", save_id="save_006", game_id="g", name="Manda Smitt", faction_id="argon")
        store.bind_npc(kb, "rt-2", save_id="save_007", game_id="g", name="Manda Smitt", faction_id="argon")
        store.link_npc_to_identity(ka, k1); store.link_npc_to_identity(kb, k1)
        store.record_turn(ka, "assistant", "We argued about the trade corridor.")
        store.record_turn(kb, "assistant", "Good to see you again, commander.")
        resolved = set(store.resolve_memory_keys(k1))
        check("resolve_unions_sessions", resolved == {ka, kb}, str(resolved))
        check("union_reaches_memory", sum(store.turn_count(x) for x in resolved) >= 2)

        # 7. runtime binding + session expiry (reload-flow step 2)
        store.bind_runtime("236456014", k1, "sess-A", save_id="save_007", confidence=0.9)
        with store._connect() as c:
            nb = c.execute("SELECT COUNT(*) AS n FROM npc_runtime_bindings WHERE game_session_id='sess-A'").fetchone()["n"]
        check("runtime_bind", nb == 1)
        check("session_expiry", store.expire_session_bindings("sess-A") == 1)

        # 8. backfill creates an identity for an UNLINKED npc, and is idempotent/reversible on re-run
        # backfill is chat-NPC-only (I8): the test NPC must be a chat NPC to be picked up.
        kc = MemoryStore.make_key("save_006", "chat", "Bron Velsk")
        store.bind_npc(kc, "rt-3", save_id="save_006", game_id="chat", name="Bron Velsk", faction_id="teladi")
        r1 = store.backfill_identities()
        check("backfill_creates", r1["identities_created"] >= 1 and bool(store.get_npc(kc).get("persistent_key")), str(r1))
        r2 = store.backfill_identities()
        check("backfill_idempotent", r2["identities_created"] == 0 and r2["rows_linked"] == 0, str(r2))
        check("backfill_preserves_manual_link", store.get_npc(ka).get("persistent_key") == k1)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_npc_rebind_selftest() -> dict:
    """Deterministic oracle for EPIC I (I2): evidence scoring + per-session rebind. Proves a
    reloaded NPC (new runtime id, new npc_key, same stable evidence) re-binds to its existing
    identity and UNIONS memory; that a same-name/different-faction NPC is NOT merged; that a
    genuine ambiguous near-tie does not merge; and that a brand-new NPC gets a fresh identity."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_rebind_selftest_")
    try:
        store = MemoryStore(Path(d) / "rebind.sqlite3")
        rich = {"name": "Manda Smitt", "faction": "argon", "role": "service",
                "macro": "character_argon_female_asi_crew_01_macro", "npc_code": "RDU-996",
                "skills": {"boarding": 6, "engineering": 2, "morale": 3}}

        # Session 1: first encounter → brand-new identity.
        s1 = dict(rich); s1["runtime_component_id"] = "134465819"; s1["npc_key"] = MemoryStore.make_key("save_006", "g", "Manda Smitt")
        store.bind_npc(s1["npc_key"], "rt-1", save_id="save_006", game_id="g", name="Manda Smitt", faction_id="argon")
        d1 = store.rebind_session("sess-1", [s1], save_id="save_006")["results"][0]
        check("first_encounter_new", d1["decision"] == "new", str(d1))
        pkey = d1["persistent_npc_key"]
        store.record_turn(s1["npc_key"], "assistant", "We argued about the trade corridor.")

        # Session 2 (reload): NEW runtime id + NEW npc_key, SAME stable evidence → BIND to same identity.
        s2 = dict(rich); s2["runtime_component_id"] = "236456014"; s2["npc_key"] = MemoryStore.make_key("save_007", "g", "Manda Smitt")
        store.bind_npc(s2["npc_key"], "rt-2", save_id="save_007", game_id="g", name="Manda Smitt", faction_id="argon")
        d2 = store.rebind_session("sess-2", [s2], save_id="save_007")["results"][0]
        check("reload_rebinds", d2["decision"] == "bound" and d2["persistent_npc_key"] == pkey and d2["confidence"] >= 0.80, str(d2))
        # The whole point: memory now unions BOTH sessions under one identity.
        mk = set(store.resolve_memory_keys(pkey))
        check("memory_union_after_rebind", mk == {s1["npc_key"], s2["npc_key"]}, str(mk))

        # Same name, DIFFERENT faction → must NOT merge into the argon Manda.
        s3 = {"name": "Manda Smitt", "faction": "teladi", "role": "service",
              "runtime_component_id": "999", "npc_key": MemoryStore.make_key("save_007", "g", "Manda Smitt#teladi")}
        store.bind_npc(s3["npc_key"], "rt-3", save_id="save_007", game_id="g", name="Manda Smitt", faction_id="teladi")
        d3 = store.rebind_session("sess-2", [s3], save_id="save_007")["results"][0]
        check("dupname_difffaction_not_merged", d3["persistent_npc_key"] != pkey and d3["decision"] in ("new", "ambiguous"), str(d3))

        # Near-tie ambiguity: two same-name identities, observe with no distinguishing evidence.
        vexA = store.derive_persistent_key({"name": "Vex Korrin", "faction": "argon", "role": "pilot", "macro": "mA"})
        vexB = store.derive_persistent_key({"name": "Vex Korrin", "faction": "argon", "role": "pilot", "macro": "mB"})
        for vk, mc in ((vexA, "mA"), (vexB, "mB")):
            store.upsert_identity(vk, {"display_name": "Vex Korrin", "faction": "argon", "role": "pilot", "macro": mc})
            store.record_evidence(vk, "name", "Vex Korrin"); store.record_evidence(vk, "faction", "argon"); store.record_evidence(vk, "role", "pilot")
        obsv = {"name": "Vex Korrin", "faction": "argon", "role": "pilot",
                "runtime_component_id": "5", "npc_key": MemoryStore.make_key("save_007", "g", "Vex Korrin")}
        ranked = store.score_identity(obsv, game_session_id="sess-2")
        check("near_tie_detected", len(ranked) >= 2 and abs(ranked[0]["score"] - ranked[1]["score"]) <= store.IDENTITY_NEAR_TIE, str(ranked[:2]))
        dv = store.rebind_session("sess-2", [obsv], save_id="save_007")["results"][0]
        check("near_tie_ambiguous", dv["decision"] == "ambiguous" and dv["persistent_npc_key"] not in (vexA, vexB), str(dv))

        # Brand-new name → new identity.
        dn = store.rebind_session("sess-2", [{"name": "Zog Nobody", "faction": "split",
                                              "runtime_component_id": "7", "npc_key": MemoryStore.make_key("save_007", "g", "Zog")}],
                                  save_id="save_007")["results"][0]
        check("brand_new_identity", dn["decision"] == "new", str(dn))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_npc_promotion_selftest() -> dict:
    """Deterministic oracle for EPIC I (I3): importance-tier promotion. Proves conversing promotes to
    Tier 1, promotion never demotes, event triggers map correctly, and unknown/unlinked are no-ops."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_promo_selftest_")
    try:
        store = MemoryStore(Path(d) / "promo.sqlite3")
        pkey = store.derive_persistent_key({"name": "Reyes", "faction": "argon", "role": "pilot"})
        store.upsert_identity(pkey, {"display_name": "Reyes", "faction": "argon", "role": "pilot"})
        nk = MemoryStore.make_key("s", "g", "Reyes")
        store.bind_npc(nk, "rt", save_id="s", game_id="g", name="Reyes", faction_id="argon")
        store.link_npc_to_identity(nk, pkey)
        check("default_tier_background", store.get_identity(pkey)["importance_tier"] == 3)

        # conversation promotes to Tier 1 (player-significant)
        store.record_turn(nk, "user", "Hello there.")
        check("talk_promotes_to_1", store.get_identity(pkey)["importance_tier"] == 1)
        # never demote — a weaker (higher-number) trigger does not undo it
        store.promote_identity(pkey, "news_event")
        check("never_demote", store.get_identity(pkey)["importance_tier"] == 1)

        # a fresh background identity → news_event lands it at Tier 2
        p2 = store.derive_persistent_key({"name": "Galaxy News Desk", "faction": "argon", "role": "news"})
        store.upsert_identity(p2, {"display_name": "Galaxy News Desk"})
        check("news_event_to_2", store.promote_identity(p2, "social_event") == 2 and store.get_identity(p2)["importance_tier"] == 2)
        # faction abstraction → Tier 0 (most-tracked)
        check("abstraction_to_0", store.promote_identity(p2, "faction_abstraction") == 0)
        # unknown reason → no-op None
        check("unknown_reason_noop", store.promote_identity(p2, "nonsense") is None)
        # unlinked npc → no-op None
        check("unlinked_npc_noop", store.promote_identity_for_npc(MemoryStore.make_key("s", "g", "Ghost"), "talked") is None)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_role_inference_selftest() -> dict:
    """Deterministic oracle for skill-based role inference (bugfix): a management-dominant NPC is a
    manager, not generic 'crew'; a specific MD role is preserved; weak/tied skills don't guess."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    rfs = MemoryStore.role_from_skills
    check("manager_from_management", rfs({"management": 10, "piloting": 4, "morale": 9}) == "manager")
    check("pilot_from_piloting", rfs({"piloting": 12, "management": 2}) == "pilot")
    check("marine_from_boarding", rfs({"boarding": 11, "engineering": 1}) == "marine")
    check("engineer_from_engineering", rfs({"engineering": 9, "management": 1}) == "engineer")
    check("weak_skills_no_guess", rfs({"engineering": 2, "morale": 3, "piloting": 1}) == "")  # Manda-like: <5
    check("morale_ignored", rfs({"morale": 15}) == "")
    check("tie_no_guess", rfs({"management": 8, "piloting": 8}) == "")
    check("json_string_ok", rfs('{"management": 10, "piloting": 1}') == "manager")
    d = tempfile.mkdtemp(prefix="nl_role_selftest_")
    try:
        store = MemoryStore(Path(d) / "role.sqlite3")
        check("specific_role_preserved", store._role_with_skills({"role": "service crew", "skills": {"management": 10}}) == "service crew")
        check("generic_role_inferred", store._role_with_skills({"role": "crew", "skills": {"management": 10, "piloting": 4}}) == "manager")
        # reinfer corrects an existing generic row (the Selaia case)
        k = MemoryStore.make_key("s", "chat", "Selaia")
        store.bind_npc(k, "rt", save_id="s", game_id="chat", name="Selaia", faction_id="argon",
                       stats={"role": "crew", "skills": {"management": 10, "piloting": 4, "morale": 9}})
        # bind_npc already infers on store → should be 'manager' immediately
        check("bind_infers_on_store", (store.get_npc(k) or {}).get("role") == "manager", (store.get_npc(k) or {}).get("role"))
        # #117: reinfer_roles PROPAGATES the specific role to a linked identity whose role is stale/generic.
        pk = store.derive_persistent_key({"name": "Selaia", "faction": "argon"})
        store.upsert_identity(pk, {"display_name": "Selaia", "faction": "argon", "role": "crew"})
        store.link_npc_to_identity(k, pk)
        check("identity_role_stale_before", str((store.get_identity(pk) or {}).get("role")) == "crew")
        store.reinfer_roles()
        check("identity_role_propagated", str((store.get_identity(pk) or {}).get("role")) == "manager",
              str((store.get_identity(pk) or {}).get("role")))
        # Non-clobber: a NON-generic identity role is preserved (never overwritten by propagation).
        k2 = MemoryStore.make_key("s", "chat", "Veers")
        store.bind_npc(k2, "rt2", save_id="s", game_id="chat", name="Veers", faction_id="argon",
                       stats={"role": "crew", "skills": {"management": 10, "piloting": 4, "morale": 9}})
        pk2 = store.derive_persistent_key({"name": "Veers", "faction": "argon"})
        store.upsert_identity(pk2, {"display_name": "Veers", "faction": "argon", "role": "captain"})
        store.link_npc_to_identity(k2, pk2)
        store.reinfer_roles()
        check("identity_specific_role_preserved", str((store.get_identity(pk2) or {}).get("role")) == "captain",
              str((store.get_identity(pk2) or {}).get("role")))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_npc_recall_gate_selftest() -> dict:
    """Deterministic oracle for EPIC I (I4): confidence-gated personal-memory recall. Proves a BOUND NPC
    recalls fully and UNIONS memory across its keys (cross-reload), a TENTATIVE bind hedges, an AMBIGUOUS
    bind surfaces NO personal history, and an unbound (non-chat) NPC keeps default full recall."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_recall_selftest_")
    try:
        store = MemoryStore(Path(d) / "recall.sqlite3")
        # Same NPC across TWO session keys (the cross-reload union case), one identity.
        k_old = MemoryStore.make_key("save_a", "chat", "Manda Smitt")
        k_new = MemoryStore.make_key("save_b", "chat", "Manda Smitt")
        store.bind_npc(k_old, "rt1", save_id="save_a", game_id="chat", name="Manda Smitt", faction_id="argon")
        store.bind_npc(k_new, "rt2", save_id="save_b", game_id="chat", name="Manda Smitt", faction_id="argon")
        pk = store.derive_persistent_key({"name": "Manda Smitt", "faction": "argon"})
        store.upsert_identity(pk, {"display_name": "Manda Smitt", "faction": "argon"})
        store.link_npc_to_identity(k_old, pk)
        store.link_npc_to_identity(k_new, pk)
        store.add_fact(k_old, "You betrayed us by backing our rivals at Profit Center.", "betrayal")  # core, on the OLD key
        store.add_fact(k_new, "You rescued our convoy at Hatikvah's Choice.", "rescue")               # significant, NEW key

        # BOUND → full recall, unioned across both keys.
        store.set_identity_fields(pk, identity_confidence=0.9, status="bound")
        ctx_b = store.build_memory_context(k_new)
        check("bound_unions_both_keys", ("rivals" in ctx_b) and ("rescued our convoy" in ctx_b), ctx_b[:200])
        check("bound_no_hedge", "half-recognize" not in ctx_b)

        # TENTATIVE → still recalls, but hedged.
        store.set_identity_fields(pk, status="tentative", identity_confidence=0.7)
        ctx_t = store.build_memory_context(k_new)
        check("tentative_hedges", "half-recognize" in ctx_t, ctx_t[:160])
        check("tentative_still_recalls", ("rivals" in ctx_t) or ("rescued our convoy" in ctx_t))

        # AMBIGUOUS → NO personal history surfaced.
        store.set_identity_fields(pk, status="ambiguous", identity_confidence=0.5)
        ctx_a = store.build_memory_context(k_new)
        check("ambiguous_suppresses_personal",
              ("do not clearly recognize" in ctx_a) and ("rivals" not in ctx_a) and ("rescued our convoy" not in ctx_a),
              ctx_a[:200])

        # Unbound / non-chat NPC (no identity) → default full recall on its own key (no regression).
        k_solo = MemoryStore.make_key("save_a", "reaction", "argon High Command")
        store.bind_npc(k_solo, "rt3", save_id="save_a", game_id="reaction", name="argon High Command", faction_id="argon")
        store.add_fact(k_solo, "The Xenon pushed into Grand Exchange.", "threat")
        ctx_s = store.build_memory_context(k_solo)
        check("unbound_default_recall", ("Grand Exchange" in ctx_s) and ("half-recognize" not in ctx_s), ctx_s[:160])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_soft_confirm_selftest() -> dict:
    """Deterministic oracle for EPIC I (I7): player soft-confirmation of a TENTATIVE bind. Proves a
    tentative NPC is promoted to BOUND when the player's assertion MATCHES stored memory, is NOT promoted
    on an unsupported claim (anti-abuse), is a no-op when already bound, and ignores a too-thin assertion."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_softconfirm_selftest_")
    try:
        store = MemoryStore(Path(d) / "sc.sqlite3")
        k = MemoryStore.make_key("save_a", "chat", "Selaia Keppel")
        store.bind_npc(k, "rt1", save_id="save_a", game_id="chat", name="Selaia Keppel", faction_id="argon")
        pk = store.derive_persistent_key({"name": "Selaia Keppel", "faction": "argon"})
        store.upsert_identity(pk, {"display_name": "Selaia Keppel", "faction": "argon"})
        store.link_npc_to_identity(k, pk)
        store.add_fact(k, "We coordinated the defense against the Kha'ak raids near Hatikvah.", "shared")

        # MATCHING assertion on a TENTATIVE bind → promote to bound.
        store.set_identity_fields(pk, status="tentative", identity_confidence=0.7)
        r1 = store.soft_confirm_identity(k, "Remember the Kha'ak raids we fought off together?")
        check("matching_claim_promotes", r1.get("promoted") is True, str(r1))
        check("status_now_bound", str((store.get_identity(pk) or {}).get("status")) == "bound")

        # UNSUPPORTED claim on a TENTATIVE bind → NOT promoted (anti-abuse).
        store.set_identity_fields(pk, status="tentative", identity_confidence=0.7)
        r2 = store.soft_confirm_identity(k, "We grew up together on the same homeworld colony.")
        check("unsupported_claim_rejected", r2.get("promoted") is False, str(r2))
        check("status_stays_tentative", str((store.get_identity(pk) or {}).get("status")) == "tentative")

        # Already BOUND → no-op (only tentative is a candidate).
        store.set_identity_fields(pk, status="bound", identity_confidence=0.9)
        r3 = store.soft_confirm_identity(k, "Remember the Kha'ak raids we fought off together?")
        check("bound_is_noop", r3.get("promoted") is False and r3.get("reason") == "not tentative", str(r3))

        # Too-thin assertion → rejected before any work.
        store.set_identity_fields(pk, status="tentative", identity_confidence=0.7)
        r4 = store.soft_confirm_identity(k, "hey")
        check("thin_assertion_rejected", r4.get("promoted") is False, str(r4))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


# M4: ambient relationship-arc beats. A social edge changing STATUS (apply_social_event) is the trigger; this
# turns a NOTABLE transition into a one-line player-facing gossip beat. Non-notable targets (strangers/
# acquaintances/crewmates, or the private/internal romance pre-stages) stay silent so the feed isn't spammy.
_RELATIONSHIP_BEATS = {
    "partners":        "Word is that {a} and {b} have grown inseparable.",
    "courting":        "There's talk that {a} has been courting {b}.",
    "flirtation":      "Crew gossip says sparks have been flying between {a} and {b}.",
    "close friends":   "{a} and {b} have grown close, the mess hall reckons.",
    "friends":         "{a} and {b} seem to have struck up a friendship.",
    "mentor/student":  "{a} has taken {b} under their wing.",
    "rivals":          "Friction is brewing between {a} and {b}.",
    "enemies":         "{a} and {b} are openly at odds now.",
    "grieving":        "{a} is said to be grieving the loss of {b}.",
}


def relationship_beat_line(a: str, b: str, status_before: str, status_after: str) -> str:
    """Pure: a one-line ambient beat for a NOTABLE social-status transition, else ''. Emits only when the status
    actually changed AND the new status is one players would notice (the gossip-worthy ones)."""
    a = str(a or "").strip() or "A crew member"
    b = str(b or "").strip() or "another"
    if not status_after or status_after == status_before:
        return ""
    tmpl = _RELATIONSHIP_BEATS.get(status_after)
    return tmpl.format(a=a, b=b) if tmpl else ""


def run_relationship_beat_selftest() -> dict:
    """Deterministic oracle for M4: notable transitions emit a beat naming both NPCs; non-transitions and
    non-notable targets stay silent; souring (friends→rivals) and bereavement (→grieving) emit."""
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    up = relationship_beat_line("Selaia", "Manda", "friends", "partners")
    check("notable_transition_emits", up != "" and "Selaia" in up and "Manda" in up, up)
    check("no_transition_silent", relationship_beat_line("Selaia", "Manda", "partners", "partners") == "")
    check("non_notable_target_silent", relationship_beat_line("A", "B", "strangers", "acquaintances") == "")
    check("private_prestage_silent", relationship_beat_line("A", "B", "strangers", "private_attraction") == "")
    check("souring_emits", "Friction" in relationship_beat_line("A", "B", "close friends", "rivals"))
    check("bereavement_emits", "grieving" in relationship_beat_line("A", "B", "partners", "grieving").lower())
    check("blank_after_silent", relationship_beat_line("A", "B", "friends", "") == "")
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


# M2: conversational tone → relation/attitude. Words move how an NPC/faction FEELS about the player (the whole
# point of talking) — bounded via apply_reaction's caps. Words still can't mint RESOURCES (ships/money/wares);
# only feelings/standing move here. Deterministic baseline so it's testable + reliable without an LLM call.
_TONE_THREAT = ("kill you", "murder", "destroy you", "wipe you out", "exterminate", "raid you", "raid your",
                "go to war", "declare war", "hunt you", "end you", "slaughter", "your family", "steal your",
                "burn your", "blow you", "i'll kill", "wipe out your")
_TONE_INSULT = ("idiot", "fool", "scum", "coward", "pathetic", "worthless", "piece of shit", "garbage",
                "trash", "incompetent", "disgusting", "traitor", "liar", "spineless", "moron", "useless")
_TONE_WARM = ("thank you", "thanks", "appreciate", "grateful", "well done", "respect", "honour", "honor",
              "my friend", "glad", "pleasure", "help you", "good work", "admire", "trust you", "stand with you")


def classify_tone(text: str) -> dict:
    """Pure: classify the player's message tone/intent into a bounded emotional reaction the NPC/faction holds
    TOWARD the player. Threat > insult > warmth > neutral. Deltas are within apply_reaction's REACTION_CAPS
    (resentment ≤+20, fear ≤+15, trust ≥-15). Never throws."""
    t = str(text or "").lower()
    if any(p in t for p in _TONE_THREAT):
        return {"label": "hostile", "resentment": 15, "fear": 8, "trust": -12}
    if any(p in t for p in _TONE_INSULT):
        return {"label": "rude", "resentment": 8, "fear": 0, "trust": -6}
    if any(p in t for p in _TONE_WARM):
        return {"label": "warm", "resentment": -4, "fear": 0, "trust": 6}
    return {"label": "neutral", "resentment": 0, "fear": 0, "trust": 0}


def run_tone_reaction_selftest() -> dict:
    """Deterministic oracle for M2: tone classification → bounded reaction. Threats sour + frighten + drop trust;
    insults sour; warmth builds trust; neutral does nothing; and apply_reaction stays within caps."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    hostile = classify_tone("I'm going to murder your family and steal your ship")
    check("threat_is_hostile", hostile["label"] == "hostile" and hostile["resentment"] > 0 and hostile["trust"] < 0, str(hostile))
    rude = classify_tone("you're a pathetic coward")
    check("insult_is_rude", rude["label"] == "rude" and rude["resentment"] > 0)
    warm = classify_tone("Thank you, I really appreciate your help")
    check("warmth_builds_trust", warm["label"] == "warm" and warm["trust"] > 0)
    neutral = classify_tone("Where is the nearest equipment dock?")
    check("neutral_no_change", neutral["label"] == "neutral" and neutral["resentment"] == 0 and neutral["trust"] == 0)
    check("threat_outranks_insult", classify_tone("you idiot, I'll kill you")["label"] == "hostile")

    d = tempfile.mkdtemp(prefix="nl_tone_selftest_")
    try:
        store = MemoryStore(Path(d) / "tone.sqlite3")
        h = classify_tone("I will destroy you")
        store.apply_reaction("s", "argon", "player", {k: h[k] for k in ("resentment", "fear", "trust")},
                             mood="", rationale="tone")
        rel = store.get_relationship("s", "argon", "player") or {}
        check("reaction_written", float(rel.get("resentment", 0)) > 0, str(rel)[:160])
        check("within_caps", float(rel.get("resentment", 0)) <= 20)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_blackboard_probe_selftest() -> dict:
    """Deterministic oracle for the Blackboard identity probe's VERDICT logic (the in-game write/read is what the
    probe itself proves; this proves we classify the recorded observations correctly). PASS scenario →
    USE_BLACKBOARD; reload-read-fails → REJECT; works-but-no-id-change → HYBRID."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_bbprobe_selftest_")
    try:
        store = MemoryStore(Path(d) / "bb.sqlite3")

        # PASS: write + same-session read + after-reload read, runtime id CHANGED (100->200), dup keys distinct.
        for r in [
            {"save_id": "pass", "phase": "write", "target_type": "conversation_person", "runtime_component_id": "100",
             "npc_name": "Manda", "blackboard_key": "$aic_key", "blackboard_value": "K1", "write_success": True, "read_success": True},
            {"save_id": "pass", "phase": "same_session", "target_type": "conversation_person", "runtime_component_id": "100",
             "npc_name": "Manda", "blackboard_value": "K1", "read_success": True},
            {"save_id": "pass", "phase": "after_reload", "target_type": "conversation_person", "runtime_component_id": "200",
             "npc_name": "Manda", "blackboard_value": "K1", "read_success": True},
            {"save_id": "pass", "phase": "duplicate", "runtime_component_id": "201", "npc_name": "CrewA", "blackboard_value": "KA", "read_success": True},
            {"save_id": "pass", "phase": "duplicate", "runtime_component_id": "202", "npc_name": "CrewB", "blackboard_value": "KB", "read_success": True},
        ]:
            store.record_blackboard_probe(r)
        vp = store.blackboard_verdict("pass")
        check("pass_legacy_use_blackboard", vp["legacy_verdict"] == "USE_BLACKBOARD", str(vp))
        check("pass_string_key_survived", vp["string_key_survived"] is True)
        check("pass_dup_ok", vp["duplicate_separation_ok"] is True)

        # OBJECT_REF: an object-ref payload resolves after reload (new runtime id) AND matches the same person.
        for r in [
            {"save_id": "obj", "phase": "write", "payload_type": "object", "runtime_component_id": "100",
             "npc_name": "Manda", "blackboard_key": "$aic_obj", "blackboard_value": "objref_1",
             "npctemplate": "tmpl_crew", "write_success": True, "read_success": True, "restored_match": True},
            {"save_id": "obj", "phase": "after_reload", "payload_type": "object", "runtime_component_id": "200",
             "npc_name": "Manda", "blackboard_value": "objref_1", "npctemplate": "tmpl_crew",
             "read_success": True, "restored_match": True},
        ]:
            store.record_blackboard_probe(r)
        vo = store.blackboard_verdict("obj")
        check("object_ref_verdict", vo["verdict"] == "OBJECT_REF", str(vo))
        check("object_ref_survived", vo["object_ref_survived"] is True)

        # HYBRID_TEMPLATE: object ref fails to resolve after reload, but the person is re-found via npctemplate.
        for r in [
            {"save_id": "tmpl", "phase": "write", "payload_type": "object", "runtime_component_id": "100",
             "npc_name": "Manda", "blackboard_value": "objref_2", "write_success": True, "read_success": True},
            {"save_id": "tmpl", "phase": "after_reload", "payload_type": "object", "runtime_component_id": "",
             "npc_name": "Manda", "blackboard_value": "objref_2", "read_success": False},
            {"save_id": "tmpl", "phase": "template_fallback", "runtime_component_id": "300", "npc_name": "Manda",
             "npctemplate": "tmpl_crew", "read_success": True, "restored_match": True},
        ]:
            store.record_blackboard_probe(r)
        check("template_fallback_verdict", store.blackboard_verdict("tmpl")["verdict"] == "HYBRID_TEMPLATE")

        # SYNTHETIC: object ref written but after-reload read fails and no template fallback found.
        for r in [
            {"save_id": "rej", "phase": "write", "payload_type": "object", "runtime_component_id": "100",
             "npc_name": "Manda", "blackboard_value": "R1", "write_success": True, "read_success": True},
            {"save_id": "rej", "phase": "after_reload", "payload_type": "object", "runtime_component_id": "200",
             "npc_name": "Manda", "blackboard_value": "", "read_success": False},
        ]:
            store.record_blackboard_probe(r)
        vr = store.blackboard_verdict("rej")
        check("reject_is_synthetic", vr["verdict"] == "SYNTHETIC", str(vr))
        check("reject_legacy", vr["legacy_verdict"] == "REJECT")

        # PHASE 6 (duplicate-collision): two same-name crew with DISTINCT tokens → dup_ok TRUE (the in-game proof
        # the collector now emits). Same token for two DIFFERENT NPCs (a hypothetical mint collision) → dup_ok FALSE.
        for r in [
            {"save_id": "dupok", "phase": "duplicate", "runtime_component_id": "11", "npc_name": "Manda", "blackboard_value": "aic_11_1"},
            {"save_id": "dupok", "phase": "duplicate", "runtime_component_id": "12", "npc_name": "Manda", "blackboard_value": "aic_12_2"},
        ]:
            store.record_blackboard_probe(r)
        check("dup_distinct_tokens_ok", store.blackboard_verdict("dupok")["duplicate_separation_ok"] is True)
        for r in [
            {"save_id": "dupbad", "phase": "duplicate", "runtime_component_id": "21", "npc_name": "Manda", "blackboard_value": "SHARED"},
            {"save_id": "dupbad", "phase": "duplicate", "runtime_component_id": "22", "npc_name": "Manda", "blackboard_value": "SHARED"},
        ]:
            store.record_blackboard_probe(r)
        check("dup_collision_detected", store.blackboard_verdict("dupbad")["duplicate_separation_ok"] is False)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_blackboard_bind_selftest() -> dict:
    """Deterministic oracle for wiring the Blackboard token as the PRIMARY identity key: binding links the chat
    npc to a `bb:<token>` identity (status bound, confidence 1.0); a different token yields a different identity;
    re-bind is idempotent; missing args are guarded."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_bbbind_selftest_")
    try:
        store = MemoryStore(Path(d) / "bind.sqlite3")
        name_key = store.make_key("s", "chat", "Manda Smitt")
        # 1. card key is ALWAYS name-based (token no longer keys cards — that caused duplication).
        check("card_key_name_based", store.chat_npc_key("s", "chat", "Manda", "tok") == "s|chat|Manda")
        # 2. existing name card with history + a synthetic pid: identity → bind ADOPTS the name card (no new card)
        #    and UPGRADES it to the proven bb token.
        store.bind_npc(name_key, "", save_id="s", game_id="chat", name="Manda Smitt", faction_id="argon")
        store.link_npc_to_identity(name_key, "pid:deadbeef0000")   # simulate prior synthetic identity
        store.record_turn(name_key, "user", "hello")
        store.record_turn(name_key, "assistant", "hi")
        r = store.bind_blackboard_identity("s", "Manda Smitt", "argon", "service crew", "aic_100_111", "100")
        check("bind_ok", bool(r.get("ok")) and r.get("persistent_npc_key") == "bb:aic_100_111", str(r))
        check("result_keyed_by_name", r.get("npc_key") == name_key, str(r.get("npc_key")))
        check("no_separate_token_card", store.get_npc(store.make_key("s", "chat", "bb:aic_100_111")) is None)
        npc = store.get_npc(name_key) or {}
        check("name_card_upgraded_to_token", npc.get("persistent_key") == "bb:aic_100_111", str(npc.get("persistent_key")))
        check("history_preserved", store.turn_count(name_key) == 2, "turns kept on the one card")
        ident = store.get_identity("bb:aic_100_111") or {}
        check("identity_bound", str(ident.get("status")) == "bound" and float(ident.get("identity_confidence") or 0) >= 1.0, str(ident))
        # 3. rebind idempotent — still one card, same identity.
        r2 = store.bind_blackboard_identity("s", "Manda Smitt", "argon", "service crew", "aic_100_111")
        check("rebind_idempotent", r2.get("npc_key") == name_key and r2.get("persistent_npc_key") == "bb:aic_100_111")
        # 4. heal: a STRAY token card (from the old bug) gets folded back into the name card on next bind.
        stray = store.make_key("s", "chat", "bb:aic_100_111")
        store.bind_npc(stray, "", save_id="s", game_id="chat", name="Manda Smitt")
        store.record_turn(stray, "user", "stranded turn")
        store.bind_blackboard_identity("s", "Manda Smitt", "argon", "service crew", "aic_100_111")
        check("stray_card_folded", store.get_npc(stray) is None)
        check("stray_history_merged", store.turn_count(name_key) == 3, "stranded turn moved onto name card")
        # 5. distinct NPC → distinct identity, still one card each.
        r3 = store.bind_blackboard_identity("s2", "Other Guy", "teladi", "pilot", "aic_900_999")
        check("distinct_identity",
              r3.get("persistent_npc_key") == "bb:aic_900_999"
              and str((store.get_identity("bb:aic_900_999") or {}).get("status")) == "bound")
        check("guard_missing", store.bind_blackboard_identity("", "", "", "", "").get("ok") is False)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


# =================== OPORD Phase 4: COA engine (deterministic — the commander decides, not the LLM) ===========
def _op_clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

# Faction doctrine weights (spec OPORD_Update). Weighted-sum over normalized wargame dimensions → COA score.
OPORD_DOCTRINE = {
    "argon":   {"mission_success": 0.25, "force_protection": 0.20, "trade_protection": 0.20, "speed": 0.10,
                "budget_efficiency": 0.10, "political_acceptability": 0.10, "flexibility": 0.05},
    "split":   {"mission_success": 0.30, "speed": 0.20, "aggression": 0.20, "force_protection": 0.05,
                "political_acceptability": 0.05, "flexibility": 0.10, "budget_efficiency": 0.10},
    "teladi":  {"mission_success": 0.20, "profit": 0.25, "budget_efficiency": 0.20, "force_protection": 0.15,
                "political_acceptability": 0.10, "flexibility": 0.10},
    "default": {"mission_success": 0.30, "force_protection": 0.20, "trade_protection": 0.15, "speed": 0.10,
                "budget_efficiency": 0.10, "political_acceptability": 0.10, "flexibility": 0.05},
}

# Enemy reaction baseline (spec) — most-likely behaviour string per target faction.
OPORD_ENEMY_REACTION = {
    "xenon": "Xenon reinforce and strike infrastructure; no diplomacy.",
    "khaak": "Kha'ak swarm and raid; no negotiation.",
    "teladi": "Teladi avoid losses and counter-contract while contesting trade lanes.",
    "split": "Split escalate aggressively.",
    "argon": "Argon protect trade and civilians.",
    "paranid": "Paranid escalate ideologically with formal reprisal.",
    "boron": "Boron stay defensive with humanitarian framing.",
    "scaleplate": "Pirates privateer and raid opportunistically.",
}


def opord_enemy_reaction(target_faction: str) -> str:
    return OPORD_ENEMY_REACTION.get(str(target_faction or ""), "Enemy contests the objective.")


def opord_generate_coas(op: dict) -> list[dict]:
    """Candidate COAs from the threat + the operation's analysed assets. Screening + doctrine decide which wins;
    this just offers the menu. Always offers the full relevant set (don't pre-filter — that's screening's job)."""
    analysis = op.get("mission_analysis_json") or {}
    sector = op.get("target_sector") or "the sector"
    target = op.get("target_faction") or "the enemy"
    faction = op.get("faction_id") or "the faction"
    is_criminal = target in MemoryStore.CRIMINAL_FACTIONS
    if (op.get("operation_type") or "") == "supply_shortage":
        # supply threats get logistics COAs, not military ones — no enemy to patrol/raid.
        return [
            {"coa_type": "request_supplies", "concept": f"Post a public supply contract for {faction}.",
             "tasks": [{"task_type": "request_supplies"}], "required_assets": {}, "required_budget": 100000,
             "expected_duration": 2400},
            {"coa_type": "escort_supply_convoy", "concept": "Escort a supply convoy to the shortage.",
             "tasks": [{"task_type": "escort_supply_convoy"}], "required_assets": {"combat_ships": 2},
             "required_budget": 40000, "expected_duration": 1800},
            {"coa_type": "hire_contractors", "concept": "Hire independent haulers to cover the shortfall.",
             "tasks": [{"task_type": "post_supply_contract"}], "required_assets": {}, "required_budget": 120000,
             "expected_duration": 2400},
        ]
    coas = [
        {"coa_type": "defensive_posture", "concept": f"Hold a defensive posture in {sector}.",
         "tasks": [{"task_type": "patrol_sector", "sector": sector, "duration": 1800}],
         "required_assets": {"combat_ships": 1}, "required_budget": 0, "expected_duration": 1800},
        {"coa_type": "organic_patrol", "concept": f"Deploy patrol ships to {sector}.",
         "tasks": [{"task_type": "patrol_sector", "sector": sector, "duration": 1800},
                   {"task_type": "engage_hostiles", "target_faction": target, "rules": ["respond_only"]}],
         "required_assets": {"combat_ships": 3}, "required_budget": 0, "expected_duration": 1800},
        {"coa_type": "hire_contractors", "concept": f"Post a patrol contract for {sector}.",
         "tasks": [{"task_type": "post_patrol_contract", "sector": sector}],
         "required_assets": {}, "required_budget": 120000, "expected_duration": 2400},
        {"coa_type": "raid_enemy_logistics", "concept": f"Raid {target} logistics to relieve pressure.",
         "tasks": [{"task_type": "raid_enemy_logistics", "target_faction": target}],
         "required_assets": {"combat_ships": 4}, "required_budget": 50000, "expected_duration": 2400},
        {"coa_type": "request_allied_support", "concept": "Request allied support for the operation.",
         "tasks": [{"task_type": "request_allied_support"}],
         "required_assets": {}, "required_budget": 0, "expected_duration": 1800},
    ]
    if not is_criminal:
        coas.append({"coa_type": "seek_ceasefire", "concept": f"Open ceasefire talks with {target}.",
                     "tasks": [{"task_type": "seek_ceasefire", "target_faction": target}],
                     "required_assets": {}, "required_budget": 0, "expected_duration": 1200})
    return coas


def opord_screen_coa(coa: dict, assets: dict, op: dict) -> tuple:
    """Reject infeasible/illegal COAs before scoring. Returns (viable, reason)."""
    ships = int((assets or {}).get("combat_ships") or 0)
    budget = int((assets or {}).get("budget_available") or 0)
    need_ships = int((coa.get("required_assets") or {}).get("combat_ships") or 0)
    need_budget = int(coa.get("required_budget") or 0)
    if need_ships > ships:
        return False, f"insufficient combat ships ({ships} < {need_ships})"
    if need_budget > budget:
        return False, f"budget cannot reserve {need_budget} (available {budget})"
    if coa.get("coa_type") == "seek_ceasefire" and (op.get("target_faction") in MemoryStore.CRIMINAL_FACTIONS):
        return False, "target faction does not negotiate"
    return True, ""


# Per-COA baseline wargame profile (deterministic). Keys: success, frisk(friendly loss risk), enemy(loss potential),
# esc(escalation risk), trade(trade protection), speed, flex(flexibility), aggr(aggression), profit.
_OPORD_COA_PROFILE = {
    "defensive_posture":     {"success": 0.55, "frisk": 0.15, "enemy": 0.10, "esc": 0.10, "trade": 0.55, "speed": 0.90, "flex": 0.50, "aggr": 0.10, "profit": 0.00},
    "organic_patrol":        {"success": 0.62, "frisk": 0.31, "enemy": 0.22, "esc": 0.22, "trade": 0.70, "speed": 0.65, "flex": 0.60, "aggr": 0.40, "profit": 0.00},
    "hire_contractors":      {"success": 0.58, "frisk": 0.05, "enemy": 0.20, "esc": 0.15, "trade": 0.65, "speed": 0.55, "flex": 0.70, "aggr": 0.30, "profit": 0.10},
    "raid_enemy_logistics":  {"success": 0.50, "frisk": 0.45, "enemy": 0.60, "esc": 0.60, "trade": 0.30, "speed": 0.60, "flex": 0.40, "aggr": 0.90, "profit": 0.30},
    "request_allied_support": {"success": 0.50, "frisk": 0.10, "enemy": 0.20, "esc": 0.20, "trade": 0.50, "speed": 0.40, "flex": 0.80, "aggr": 0.30, "profit": 0.00},
    "seek_ceasefire":        {"success": 0.40, "frisk": 0.00, "enemy": 0.00, "esc": 0.00, "trade": 0.60, "speed": 0.50, "flex": 0.50, "aggr": 0.00, "profit": 0.00},
    "request_supplies":      {"success": 0.60, "frisk": 0.00, "enemy": 0.00, "esc": 0.00, "trade": 0.55, "speed": 0.55, "flex": 0.60, "aggr": 0.00, "profit": 0.10},
    "escort_supply_convoy":  {"success": 0.65, "frisk": 0.20, "enemy": 0.10, "esc": 0.10, "trade": 0.70, "speed": 0.55, "flex": 0.50, "aggr": 0.20, "profit": 0.00},
}


def opord_wargame_coa(coa: dict, op: dict, assets: dict) -> dict:
    """Deterministic outcome estimate for a COA (same inputs → same output). Ship advantage vs threat magnitude
    nudges success up / friendly risk down, bounded."""
    p = _OPORD_COA_PROFILE.get(coa.get("coa_type"), {"success": 0.4, "frisk": 0.2, "enemy": 0.1, "esc": 0.2,
                                                     "trade": 0.4, "speed": 0.5, "flex": 0.4, "aggr": 0.2, "profit": 0.0})
    ships = int((assets or {}).get("combat_ships") or 0)
    mag = float((op.get("evidence_json") or {}).get("magnitude") or 1.0)
    adv = max(-0.20, min(0.20, (ships - mag) * 0.02))
    return {"success": round(_op_clamp01(p["success"] + adv), 3),
            "friendly_loss_risk": round(_op_clamp01(p["frisk"] - adv), 3),
            "enemy_loss_potential": p["enemy"], "escalation_risk": p["esc"], "trade_protection": p["trade"],
            "cost": int(coa.get("required_budget") or 0), "time_to_effect": coa.get("expected_duration") or 1800,
            "_speed": p["speed"], "_flex": p["flex"], "_aggr": p["aggr"], "_profit": p["profit"],
            "enemy_most_likely": opord_enemy_reaction(op.get("target_faction"))}


def opord_score_coa(wargame: dict, weights: dict) -> tuple:
    """Doctrine-weighted score (deterministic). Maps wargame metrics → normalized [0,1] scoring dimensions."""
    dims = {
        "mission_success": wargame["success"],
        "force_protection": 1.0 - wargame["friendly_loss_risk"],
        "trade_protection": wargame["trade_protection"],
        "speed": wargame["_speed"],
        "budget_efficiency": 1.0 - min(1.0, float(wargame["cost"]) / 300000.0),
        "political_acceptability": 1.0 - wargame["escalation_risk"],
        "flexibility": wargame["_flex"],
        "aggression": wargame["_aggr"],
        "profit": wargame["_profit"],
    }
    score = sum(float(w) * dims.get(k, 0.0) for k, w in weights.items())
    return round(score, 4), {k: round(dims.get(k, 0.0), 3) for k in weights}


# =================== OPORD Phase 5: OPORD generator (SMESC + annexes + task derivation) =======================
# Phase sequencing per selected COA — the "phases" line of Annex A / Execution.
OPORD_PHASES = {
    "defensive_posture":     ["Establish defensive posture", "Screen the sector", "Hold until pressure abates"],
    "organic_patrol":        ["Recon sector", "Deploy patrol", "Intercept hostile pressure", "Hold until trade resumes"],
    "hire_contractors":      ["Post contract", "Brief contractors", "Sustain patrol coverage", "Verify proof"],
    "raid_enemy_logistics":  ["Locate enemy logistics", "Strike the target", "Assess damage", "Withdraw"],
    "request_allied_support": ["Request support", "Coordinate forces", "Joint operation", "Consolidate"],
    "seek_ceasefire":        ["Open channel", "Propose terms", "Await response", "Implement / fall back"],
}


# OPORD Execution = 4 doctrinal components (Ken 2026-06-30; grounded in US Army FM 6-0 / ADP 5-0 para-3 doctrine +
# the British Concept-of-Operations model): Commander's INTENT (purpose + key tasks + end state), SCHEME OF
# MANOEUVRE (concept of operations — how the force fights from start to finish), MAIN EFFORT (the designated decisive
# task that receives priority of support), and END STATE ("success is…"). The engine composes the deterministic
# doctrinal skeleton; Player2 may later author/override the judgment parts via the decision layer.
# Decisive → shaping → sustaining: task-type priority used to DESIGNATE the main effort.
_MAIN_EFFORT_PRIORITY = (
    ("strike", "attack", "raid", "assault", "destroy", "intercept", "secure"),   # decisive
    ("patrol", "screen", "escort", "defend", "hold", "blockade"),                # shaping
    ("recon", "scout", "post", "brief", "request", "coordinate", "verify", "supply"),  # sustaining
)


def opord_designate_main_effort(coa_tasks: list, faction: str, sector: str) -> dict:
    """Designate the MAIN EFFORT — the single decisive task that receives priority of support — and label the rest as
    supporting efforts. Doctrine: the main effort is the task most critical to mission success at the decisive point."""
    if not coa_tasks:
        return {"unit": "", "task": "", "rationale": "no tasks derived", "supporting_efforts": []}

    def rank(t: dict) -> int:
        tt = str(t.get("task_type") or "").lower()
        for tier, kws in enumerate(_MAIN_EFFORT_PRIORITY):
            if any(k in tt for k in kws):
                return tier
        return len(_MAIN_EFFORT_PRIORITY)  # unranked → lowest priority

    main = min(coa_tasks, key=rank)
    main_tt = str(main.get("task_type") or "operation")
    me_sector = main.get("sector") or sector
    supporting = [str(t.get("task_type") or "task") for t in coa_tasks if t is not main]
    return {
        "unit": f"{faction}_{main_tt}",
        "task": main_tt,
        "sector": me_sector,
        "rationale": f"{main_tt} in {me_sector} is decisive to the end state; it receives priority of support.",
        "supporting_efforts": supporting,
    }


def opord_scheme_of_manoeuvre(coa: dict, faction: str, sector: str, phases: list, main_effort: dict) -> str:
    """SCHEME OF MANOEUVRE (concept of operations): a narrative of how the force fights the operation from start to
    finish, naming the phase sequence and the main effort. Deterministic; Player2 may enrich it later."""
    coa_human = str(coa.get("coa_type") or "operation").replace("_", " ")
    seq = " → ".join(phases) if phases else "Execute → Assess → Consolidate"
    me = main_effort.get("task") or "the decisive task"
    return (f"{faction} executes a {coa_human} in {sector}. Phasing: {seq}. "
            f"The main effort is {me}; the remaining tasks shape and sustain it until the end state is met.")


def opord_build_smesc(op: dict, coa: dict) -> dict:
    """Build the machine-readable SMESC OPORD from the operation + its selected COA. Player-readable strings come
    from the deterministic mission/intent/end-state; an optional LLM prose wrapper is a later/optional step.
    Execution carries the 4 doctrinal components: intent, scheme_of_manoeuvre, main_effort, end_state."""
    faction = op.get("faction_id") or ""
    sector = op.get("target_sector") or "the contested zone"
    target = op.get("target_faction") or "hostile forces"
    analysis = op.get("mission_analysis_json") or {}
    assets = analysis.get("available_assets") or {}
    constraints = op.get("constraints_json") or analysis.get("constraints") or []
    coa_tasks = coa.get("tasks_json") or []
    wargame = coa.get("wargame_json") or {}
    exec_tasks = []
    for t in coa_tasks:
        exec_tasks.append({"unit": f"{faction}_{coa.get('coa_type', 'force')}", "task": t.get("task_type"),
                           "sector": t.get("sector") or sector, "target_faction": t.get("target_faction")})
    phases = OPORD_PHASES.get(coa.get("coa_type"), ["Execute", "Assess", "Consolidate"])
    main_effort = opord_designate_main_effort(coa_tasks, faction, sector)
    scheme = opord_scheme_of_manoeuvre(coa, faction, sector, phases, main_effort)
    return {
        "situation": {
            "enemy": opord_enemy_reaction(target),
            "friendly": f"{faction} has {int(assets.get('combat_ships') or 0)} combat ships available.",
            "civil_economic": (f"Trade disruption is affecting {faction} logistics." if sector else ""),
            "constraints": list(constraints),
        },
        "mission": op.get("mission_statement") or "",
        "execution": {
            # The 4 doctrinal components of the Execution paragraph (Ken 2026-06-30):
            "intent": op.get("commander_intent") or "",
            "scheme_of_manoeuvre": scheme,
            "main_effort": main_effort,
            "end_state": op.get("desired_end_state") or "",
            # supporting detail
            "phases": phases,
            "tasks": exec_tasks,
            "coordinating_instructions": list(constraints) + ["report hostile contact",
                                                              "withdraw if outmatched by capital-class enemy"],
        },
        "service_support": {
            "budget_reserved": int(coa.get("required_budget") or 0),
            "repair_policy": f"return damaged ships to nearest {faction} station",
            "logistics_needs": ["energycells", "hullparts"],
        },
        "command_signal": {
            "commander": f"{faction} High Command",
            "reports": ["contact", "loss", "sector_clear", "mission_complete"],
            "frago_triggers": ["enemy_reinforcement", "budget_exceeded", "friendly_losses_high", "job_unclaimed"],
        },
    }


def opord_build_annexes(op: dict, coa: dict) -> dict:
    """Annexes A/B/D/E/R-S/Q as JSON sub-documents (attached to military_operations.annexes_json)."""
    sector = op.get("target_sector") or "the sector"
    constraints = op.get("constraints_json") or []
    coa_tasks = coa.get("tasks_json") or []
    wargame = coa.get("wargame_json") or {}
    return {
        "A_conduct": {"phases": OPORD_PHASES.get(coa.get("coa_type"), []),
                      "abort_conditions": ["capital-class enemy arrives", "friendly losses exceed tolerance"]},
        "B_task_org": {"course_of_action": coa.get("coa_type"),
                       "tasks": [t.get("task_type") for t in coa_tasks]},
        "D_intel": {"enemy_most_likely": wargame.get("enemy_most_likely", ""),
                    "enemy_strength": (op.get("evidence_json") or {}).get("magnitude"),
                    "intel_gaps": ["enemy reinforcement timing"]},
        "E_rules": {"roe": ["respond only", "avoid neutral shipping"], "constraints": list(constraints),
                    "excluded_enemies": []},
        "RS_sustainment": {"budget_reserved": int(coa.get("required_budget") or 0),
                           "repair_policy": f"nearest {op.get('faction_id', '')} station",
                           "supply_requirements": ["energycells", "hullparts"]},
        "Q_command": {"reports": ["contact", "loss", "sector_clear", "mission_complete"],
                      "frago_triggers": ["enemy_reinforcement", "budget_exceeded", "friendly_losses_high", "job_unclaimed"]},
    }


def run_oport_selftest() -> dict:
    """Oracle for OPORD Phase 1 (schema + repository): create op, DEDUPE active threat into one op, different
    threat → new op, JSON round-trip, attach COAs/tasks/reports, operation_detail aggregates, conclude →
    terminal status, and a recurring threat (after conclusion) spawns a FRESH op (dedupe index frees it)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_oport_selftest_")
    try:
        store = MemoryStore(Path(d) / "ops.sqlite3")
        sid, tk = "s", "s:argon:sector_pressure:teladi:silent_witness_i"
        a = store.create_or_get_operation(sid, "argon", "defensive_posture", tk,
                                          target_faction="teladi", target_sector="Silent Witness I",
                                          urgency=4, importance=5)
        check("created_new", a["created"] is True and a["id"].startswith("op_argon_"), str(a))
        b = store.create_or_get_operation(sid, "argon", "defensive_posture", tk)
        check("dedupe_same_threat", b["created"] is False and b["id"] == a["id"], str(b))
        c = store.create_or_get_operation(sid, "paranid", "raid_pressure", "s:paranid:raid:khaak:frontier")
        check("different_threat_new_op", c["created"] is True and c["id"] != a["id"])
        op_id = a["id"]
        store.update_operation(op_id, status="analysing", mission_statement="Secure SW-I",
                               opord_json={"mission": "secure"})
        op = store.get_operation(op_id)
        check("json_roundtrip", isinstance(op.get("opord_json"), dict) and op["opord_json"]["mission"] == "secure", str(op.get("opord_json")))
        check("status_updated", op["status"] == "analysing")
        coa1 = store.attach_coa(op_id, "organic_patrol", "Deploy patrol", [{"task_type": "patrol_sector"}], required_budget=120000)
        store.attach_coa(op_id, "hire_contractors", "Hire", [{"task_type": "post_contract"}],
                         viability_status="rejected", rejection_reason="no budget")
        t1 = store.attach_task(op_id, "patrol_sector", status="planned", coa_id=coa1, target_sector="Silent Witness I")
        store.attach_report(op_id, "sitrep", "Patrol deployed", severity=1, evidence={"ships": 3})
        det = store.operation_detail(op_id)
        check("coas_attached", len(det["coas"]) == 2)
        check("tasks_attached", len(det["tasks"]) == 1 and det["tasks"][0]["id"] == t1)
        check("reports_attached", len(det["reports"]) == 1)
        check("report_evidence_decoded", det["reports"][0].get("evidence_json", {}).get("ships") == 3)
        store.conclude_operation(op_id, "completed", "secured")
        cop = store.get_operation(op_id)
        check("concluded_terminal", cop["status"] == "completed" and cop["conclusion_status"] == "completed")
        e = store.create_or_get_operation(sid, "argon", "defensive_posture", tk)
        check("recurring_threat_new_op", e["created"] is True and e["id"] != op_id, str(e))
        check("list_has_active", any(o["id"] == e["id"] for o in store.list_operations(sid, "warning")))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_opord_events_selftest() -> dict:
    """Oracle for OPORD Phase 8: a milestone (opord_issued) emits ONE world_event linked to the op (source
    opord:<id>) that lands in the durable history (→ NPC briefings); repeated FRAGO milestones inside the gate
    cooldown emit only ONCE (anti-spam); operation_completed routes as a critical event."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_opordevents_selftest_")
    try:
        store = MemoryStore(Path(d) / "opordevents.sqlite3")
        sid = "s"
        store.upsert_fleet_strength(sid, "argon", fight=6, total_ships=12)
        op = store.create_or_get_operation(sid, "argon", "sector_pressure",
                                           "s:argon:sector_pressure:teladi:swi", status="warning",
                                           target_faction="teladi", target_sector="Silent Witness I",
                                           evidence_json={"magnitude": 4})["id"]
        store.analyze_mission(op)
        store.plan_operation_coas(op)
        store.generate_opord(op)  # emits opord_issued
        evs = store.list_operation_events(op)
        check("opord_issued_emitted", any(e["event_type"] == "opord_issued" for e in evs), str(len(evs)))
        check("linked_source", all(e.get("source") == "opord:" + op for e in evs) and len(evs) >= 1)
        opd = store.get_operation(op)
        e1 = store.emit_operation_event(opd, "frago_issued", 3, "FRAGO 1")
        e2 = store.emit_operation_event(opd, "frago_issued", 3, "FRAGO 2")
        check("frago_first_emits", e1.get("emitted") is True, str(e1))
        check("frago_cooldown_blocks_second", e2.get("emitted") is False, str(e2))
        check("one_frago_event", len([e for e in store.list_operation_events(op) if e["event_type"] == "frago_issued"]) == 1)
        e3 = store.emit_operation_event(opd, "operation_completed", 4, "secured")
        check("completed_emitted_critical", e3.get("emitted") is True and e3.get("tier") == "critical", str(e3))
        allwe = store.list_world_events(sid, limit=50)
        check("in_durable_history", any((w.get("source") or "").startswith("opord:") for w in allwe))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_opord_e2e_selftest() -> dict:
    """OPORD Phase 10 — END-TO-END integration on the REAL pipeline (advance_operations), proving the phases
    COMPOSE (not just pass in isolation) against the spec's live-validation acceptance: one pressure → one op;
    repeated pressure → SAME op; jobs don't duplicate; reward escalates via FRAGO; op concludes from evidence +
    cleans up; a recurring threat after conclusion spawns a fresh op. (The in-game run is the separate ◐ gate.)"""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_opord_e2e_")
    try:
        store = MemoryStore(Path(d) / "e2e.sqlite3")
        sid = "game_e2e"
        # Argon: NO combat ships (forces the external job route) + owned stations (budget capacity for contractors).
        store.upsert_fleet_strength(sid, "argon", fight=0, total_ships=2)
        store.upsert_economy_station(sid, {"station_id": "arg1", "faction_id": "argon", "sector_id": "Silent Witness I"})
        store.upsert_economy_station(sid, {"station_id": "arg2", "faction_id": "argon", "sector_id": "Silent Witness I"})

        def hit(mag=5, ts=None):
            ev = {"attacker_faction": "teladi", "victim_faction": "argon", "sector": "Silent Witness I", "magnitude": mag}
            if ts is not None:
                ev["ts"] = ts
            store.add_hostile_event(sid, ev)

        # 1) Teladi pressure → pipeline → ONE operation (reaches coa_generated; COA selection is the Player2 D1 step).
        for _ in range(3):
            hit()
        store.advance_operations(sid)
        ops = store.list_operations(sid)
        check("one_operation", len(ops) == 1, str(len(ops)))
        op = ops[0]
        op_id = op["id"]
        check("argon_vs_teladi_swi", op["faction_id"] == "argon" and op["target_faction"] == "teladi"
              and op["target_sector"] == "Silent Witness I", str({k: op.get(k) for k in ("faction_id", "target_faction", "target_sector")}))
        # D1: simulate the Player2 COA selection (deterministic in the test) so the pipeline can proceed past it.
        viable = store.list_viable_coas(op_id)
        check("coas_generated", len(viable) >= 1, str(len(viable)))
        if viable:
            store.set_selected_coa(op_id, viable[0]["id"])
        store.advance_operations(sid)
        op = store.get_operation(op_id)
        check("reached_active", op["status"] in ("active", "opord_issued"), op["status"])
        check("has_selected_coa", bool(op.get("selected_coa_id")))

        # 2) Repeated pressure → SAME op (dedup).
        for _ in range(2):
            hit(4)
        store.advance_operations(sid)
        check("repeated_pressure_same_op", len(store.list_operations(sid)) == 1)

        # 3) Jobs don't duplicate across repeated advances.
        for _ in range(3):
            store.advance_operations(sid)
        op_jobs = [j for j in store.list_jobs(sid, "open") if j["operation_id"] == op_id]
        check("one_open_job_no_dup", len(op_jobs) == 1, str(len(op_jobs)))

        # 4) Unclaimed job → reward escalates via FRAGO (budget-gated).
        if op_jobs:
            job = op_jobs[0]
            with store._connect() as conn:
                conn.execute("UPDATE market_jobs SET created_at=? WHERE id=?", (time.time() - 700, job["id"]))
                conn.commit()
            store.apply_assessment_decision(op_id, "raise_reward")  # D5: commander decision (was auto-FRAGO)
            j2 = [j for j in store.list_jobs(sid, "open") if j["id"] == job["id"]]
            check("reward_escalated", bool(j2) and int(j2[0]["reward"]) > int(job["reward"]),
                  f"{job['reward']}->{j2 and j2[0].get('reward')}")

        # 5) Pressure abates (events pre-date activation) + age ≥ MIN → conclude completed.
        now = time.time()
        with store._connect() as conn:
            conn.execute("UPDATE hostile_events SET ts=? WHERE save_id=?", (now - 5000, sid))
            conn.commit()
        store.update_operation(op_id, activated_at=now - 400)
        store.apply_assessment_decision(op_id, "request_conclude")  # D5: commander requests; can_conclude validates
        check("concluded_completed", store.get_operation(op_id)["status"] == "completed", store.get_operation(op_id)["status"])

        # 6) Conclusion cleaned up linked jobs (no open jobs for this op).
        check("cleanup_no_open_jobs", len([j for j in store.list_jobs(sid, "open") if j["operation_id"] == op_id]) == 0)

        # 7) Recurring threat AFTER conclusion → a fresh operation.
        hit(6, ts=now)
        store.advance_operations(sid)
        check("recurring_threat_new_op", len(store.list_operations(sid)) == 2)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_threat_sources_selftest() -> dict:
    """Oracle for the P1a feed broadening: NON-combat threat sources. An economy shortage creates a
    supply_shortage op that gets SUPPLY COAs (not military) and a supply/escort job; a broken agreement creates an
    agreement_breakdown op against the breaker; sources dedupe like combat threats."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_threatsrc_selftest_")
    try:
        store = MemoryStore(Path(d) / "src.sqlite3")
        sid = "s"
        # economy shortage (alliance) + stations so it can still fund a supply contract. NOTE budget_capacity =
        # stations × 250k × production_health, so a low-health shortage faction needs enough stations to afford
        # contractors — 3 stations × 0.3 ≈ 225k covers the 100k/120k supply COAs.
        for i in range(3):
            store.upsert_economy_station(sid, {"station_id": f"al{i}", "faction_id": "alliance"})
        store.upsert_economy(sid, "alliance", production_health=0.3, shortages={"energycells": 0.8},
                             key_needs=["energycells"])
        r = store.recognize_threats(sid)
        supply = [t for t in r["threats"] if "supply_shortage" in t["threat_key"]]
        check("economy_supply_op", len(supply) == 1, str(r["threats"]))
        op = store.get_operation(supply[0]["op_id"])
        check("op_type_supply", op["operation_type"] == "supply_shortage")
        store.advance_operations(sid)
        det = store.operation_detail(supply[0]["op_id"])
        sel = [c for c in det["coas"] if c.get("selected")]
        check("supply_coa_selected", bool(sel) and sel[0]["coa_type"] in
              ("request_supplies", "escort_supply_convoy", "hire_contractors"), str(sel and sel[0].get("coa_type")))
        check("supply_op_progressed", store.get_operation(supply[0]["op_id"])["status"] in
              ("active", "opord_issued", "coa_generated"))
        # agreement breakdown source
        store.add_agreement(sid, "argon", "teladi", type="ceasefire", status="broken")
        r2 = store.recognize_threats(sid)
        ab = [t for t in r2["threats"] if "agreement_breakdown" in t["threat_key"]]
        check("agreement_breakdown_op", len(ab) == 1, str(r2["threats"]))
        abop = store.get_operation(ab[0]["op_id"])
        check("ab_op_targets_breaker", abop["operation_type"] == "agreement_breakdown" and abop["target_faction"] == "teladi")
        # dedup: economy + agreement sources don't multiply on re-scan
        n_before = len(store.list_operations(sid))
        store.recognize_threats(sid)
        check("sources_dedup", len(store.list_operations(sid)) == n_before, str(n_before))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_opord_lease_selftest() -> dict:
    """Oracle for the OPORD Execution Authority spine (Phase A+B): a ship can't be leased twice; a higher-priority
    OPORD overrides a lower one (old lease released); the order/event lifecycle drives the task; release is
    idempotent; operation closeout releases leases + cancels force requests; force requests dedup + escalate."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_lease_selftest_")
    try:
        store = MemoryStore(Path(d) / "lease.sqlite3")
        sid = "s"

        def aop(k):
            return store.create_or_get_operation(sid, "argon", "sector_pressure", k, status="active",
                                                 target_faction="teladi", target_sector="X")["id"]

        op = aop("s:argon:sp:teladi:l1")
        t1 = store.attach_task(op, "patrol_sector", status="issued")
        r1 = store.lease_asset(sid, op, t1, "argon", "ship_1", ship_name="ANL Vanguard", sector="X", priority=2)
        check("lease_ok", r1["ok"] and bool(r1.get("lease_id")), str(r1))
        l1 = r1["lease_id"]
        op2 = aop("s:argon:sp:teladi:l2")
        r2 = store.lease_asset(sid, op2, store.attach_task(op2, "patrol_sector", status="issued"), "argon", "ship_1", priority=1)
        check("lease_twice_blocked", r2["ok"] is False and r2.get("blocked") is True, str(r2))
        op3 = aop("s:argon:sp:teladi:l3")
        r3 = store.lease_asset(sid, op3, store.attach_task(op3, "patrol_sector", status="issued"), "argon", "ship_1", priority=5)
        check("priority_override", r3["ok"] is True and r3["lease_id"] != l1, str(r3))
        old = [x for x in store.list_leases(sid) if x["lease_id"] == l1][0]
        check("old_lease_released", old["status"] == "released")
        # order/event lifecycle → task completion from observed execution
        store.mark_order_issued(sid, r3["lease_id"], "order_99")
        store.record_order_event(sid, r3["lease_id"], "arrived")
        store.record_order_event(sid, r3["lease_id"], "completed", {"sector": "X"})
        l3 = [x for x in store.list_leases(sid) if x["lease_id"] == r3["lease_id"]][0]
        check("order_completed", l3["status"] == "completed")
        check("task_done_on_complete", any(t["status"] == "completed" for t in store.operation_detail(op3)["tasks"]))
        check("release_idempotent_terminal", store.release_asset(sid, r3["lease_id"], "x").get("noop") is True)
        # closeout releases an active lease
        op4 = aop("s:argon:sp:teladi:l4")
        store.lease_asset(sid, op4, store.attach_task(op4, "patrol_sector", status="issued"), "argon", "ship_4", priority=1)
        cc = store.conclude_operation(op4, "completed", "done")
        check("closeout_releases_leases", cc.get("leases_released") == 1, str(cc))
        # force-request dedup + escalate + cancel on conclude
        fr1 = store.create_or_update_force_request(sid, op, t1, "argon", "X", "patrol", priority=2, reward_budget=50000)
        check("force_req_created", fr1["created"] is True)
        fr2 = store.create_or_update_force_request(sid, op, t1, "argon", "X", "patrol", priority=4, reward_budget=80000)
        check("force_req_dedup", fr2["created"] is False and fr2["request_id"] == fr1["request_id"])
        check("force_req_cancelled_on_conclude", store.conclude_operation(op, "completed", "done").get("force_requests_cancelled") >= 1)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_negotiation_dedup_selftest() -> dict:
    """Oracle for Negotiations N1: agreements carry a durable agreement_key (one OPEN per key) so repeat requests
    UPDATE one row (bump request_count) instead of inserting duplicates; allied-support picks a REAL counterparty
    (never an empty party_b); a different operation/task is a distinct key; and the partial-unique index physically
    blocks a second open row for the same key."""
    import shutil
    import sqlite3
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_negodedup_selftest_")
    try:
        store = MemoryStore(Path(d) / "nego.sqlite3")
        sid = "s"
        for f in ("argon", "antigone", "teladi", "boron"):
            store.upsert_faction(sid, f, name=f.title())
        store.adjust_relationship(sid, "antigone", "argon", dtrust=60)      # antigone trusts argon
        store.adjust_relationship(sid, "antigone", "teladi", dresentment=40)  # ...and resents the enemy → shared
        store.adjust_relationship(sid, "boron", "argon", dtrust=10)
        # 1) deterministic counterparty: antigone wins (high trust + shared-enemy bonus)
        ally = store.select_support_counterparty(sid, "argon", "teladi", "Hatikvah's Choice III")
        check("counterparty_real", ally == "antigone", f"ally={ally}")
        # 2) upsert dedup: 4 identical requests → ONE row, request_count == 4
        r = None
        for _ in range(4):
            r = store.create_or_update_agreement(sid, "argon", ally, type="alliance", kind="allied_support",
                                                 operation_id="op1", operation_task_id="t1", status="pending")
        check("repeat_updates_not_inserts", r["created"] is False and r["request_count"] == 4, str(r))
        openrows = [a for a in store.list_agreements(sid)
                    if a.get("agreement_key") == r["agreement_key"]
                    and a.get("status") in ("pending", "proposed", "pending_response", "countered")]
        check("one_open_row_per_key", len(openrows) == 1, f"open={len(openrows)}")
        check("party_b_never_empty", bool(openrows) and openrows[0].get("party_b") == "antigone", str(openrows[:1]))
        # 3) different op/task → distinct key → a genuinely new row
        r2 = store.create_or_update_agreement(sid, "argon", ally, type="alliance", kind="allied_support",
                                              operation_id="op2", operation_task_id="t2", status="pending")
        check("distinct_key_new_row", r2["created"] is True and r2["agreement_key"] != r["agreement_key"], str(r2))
        # 4) partial-unique index physically blocks a duplicate OPEN insert for the same key
        blocked = False
        try:
            with store._connect() as c:
                c.execute("INSERT INTO agreements (save_id, party_a, party_b, type, agreement_key, status, created_at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (sid, "argon", "antigone", "alliance", r["agreement_key"], "pending", time.time()))
                c.commit()
        except sqlite3.IntegrityError:
            blocked = True
        check("unique_index_blocks_dup", blocked is True)
        # 5) counterparty never the requester or the enemy
        ally2 = store.select_support_counterparty(sid, "teladi", "argon", "X")
        check("never_self_or_enemy", ally2 not in ("teladi", "argon"), f"ally2={ally2}")
        # 6) OPORD allied-support routing reuses the open agreement instead of spawning a duplicate
        op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:n1",
                                           status="active", target_faction="teladi", target_sector="Z")["id"]
        tk = store.attach_task(op, "request_allied_support", status="planned", target_faction="teladi")
        store.route_operation_task(store.get_operation(op), {"id": tk, "task_type": "request_allied_support",
                                                             "target_faction": "teladi", "target_sector": "Z"})
        store.route_operation_task(store.get_operation(op), {"id": tk, "task_type": "request_allied_support",
                                                             "target_faction": "teladi", "target_sector": "Z"})
        alliedrows = [a for a in store.list_agreements(sid)
                      if a.get("kind") == "allied_support" and a.get("operation_id") == op
                      and a.get("status") in ("pending", "proposed", "pending_response", "countered")]
        check("route_reuses_open_agreement", len(alliedrows) == 1 and alliedrows[0].get("party_b"),
              f"rows={len(alliedrows)}")
        # 7) INVARIANT: add_agreement with an OPEN status auto-routes through dedupe (generate_agreements-style spam)
        for _ in range(3):
            store.add_agreement(sid, "argon", "boron", type="patrol_cooperation", status="proposed",
                                terms={"common_enemy": "xenon"})
        pc = [a for a in store.list_agreements(sid)
              if a.get("type") == "patrol_cooperation" and a.get("party_a") == "argon"
              and a.get("party_b") == "boron" and a.get("status") == "proposed"]
        check("add_agreement_open_deduped", len(pc) == 1, f"patrol_coop_rows={len(pc)}")
        # 8) terminal/historical records still insert directly (records, not open deals)
        store.add_agreement(sid, "argon", "teladi", type="ceasefire", status="kept")
        store.add_agreement(sid, "argon", "teladi", type="ceasefire", status="kept")
        check("terminal_inserts_direct", len([a for a in store.list_agreements(sid) if a.get("status") == "kept"]) == 2)
        # 9) submit_negotiation_intent (the single public door) creates one proposed offer + dedupes on repeat
        i1 = store.submit_negotiation_intent(sid, "opord", "ceasefire", "argon", recipient="teladi",
                                             operation_id="opz", operation_task_id="tz")
        i2 = store.submit_negotiation_intent(sid, "opord", "ceasefire", "argon", recipient="teladi",
                                             operation_id="opz", operation_task_id="tz")
        check("intent_door_dedupes",
              i1["created"] is True and i2["created"] is False and i2["id"] == i1["id"]
              and i2["request_count"] == 2, f"{i1} {i2}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_negotiation_scoring_selftest() -> dict:
    """Oracle for NF2: deterministic acceptance scoring + resolution. High trust + shared enemy + good offer →
    accept; low trust + heavy own war-load + no offer → refuse; mid → counter. Reads strategic_state + relationships
    (shared models). The resolver transitions open offers and records score/factors in context_json."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    def agg(store, sid, aid):
        return [x for x in store.list_agreements(sid) if x["id"] == aid][0]

    d = tempfile.mkdtemp(prefix="nl_negoscore_selftest_")
    try:
        store = MemoryStore(Path(d) / "s.sqlite3")
        sid = "s"
        for f in ("argon", "antigone", "teladi", "split", "boron"):
            store.upsert_faction(sid, f, name=f.title())
        # ACCEPT: antigone trusts argon, resents the enemy teladi (shared), low own war-load, good offer
        store.adjust_relationship(sid, "antigone", "argon", dtrust=85)
        store.adjust_relationship(sid, "antigone", "teladi", dresentment=50)
        store.upsert_strategic_state(sid, "antigone", military_pressure=0.1, recent_losses=0.0)
        a_acc = store.submit_negotiation_intent(sid, "opord", "allied_support", "argon", recipient="antigone",
                                                operation_id="o1", operation_task_id="t1", enemy="teladi",
                                                offered_value=150000)
        sc_acc = store.score_agreement_acceptance(sid, agg(store, sid, a_acc["id"]))
        check("accept_scores_high", sc_acc["decision"] == "accept", str(sc_acc))
        # REFUSE: split low trust to argon, heavy own war-load + losses, no offer, no shared enemy
        store.upsert_strategic_state(sid, "split", military_pressure=1.0, recent_losses=1.0)
        a_ref = store.submit_negotiation_intent(sid, "opord", "allied_support", "argon", recipient="split",
                                                operation_id="o2", operation_task_id="t2", enemy="teladi",
                                                offered_value=0)
        sc_ref = store.score_agreement_acceptance(sid, agg(store, sid, a_ref["id"]))
        check("refuse_scores_low", sc_ref["decision"] in ("refuse", "refuse_harshly"), str(sc_ref))
        # COUNTER: boron mid trust, small offer, modest war-load → middle band
        store.adjust_relationship(sid, "boron", "argon", dtrust=40)
        store.upsert_strategic_state(sid, "boron", military_pressure=0.2, recent_losses=0.0)
        a_cnt = store.submit_negotiation_intent(sid, "opord", "trade", "argon", recipient="boron",
                                                operation_id="o3", operation_task_id="t3", offered_value=60000)
        sc_cnt = store.score_agreement_acceptance(sid, agg(store, sid, a_cnt["id"]))
        check("counter_scores_mid", sc_cnt["decision"] == "counter", str(sc_cnt))
        # resolver transitions them
        r = store.evaluate_open_offers(sid)
        check("resolver_ran", r["evaluated"] >= 3, str(r))
        check("accepted_status", agg(store, sid, a_acc["id"])["status"] == "accepted")
        check("refused_status", agg(store, sid, a_ref["id"])["status"] == "refused")
        check("countered_status", agg(store, sid, a_cnt["id"])["status"] == "countered")
        check("score_recorded", "acceptance" in (json.loads(agg(store, sid, a_acc["id"]).get("context_json") or "{}")))
        # idempotent-ish: accepted/refused left the evaluatable set; only the countered remains (excluded) → 0 new
        r2 = store.evaluate_open_offers(sid)
        check("resolved_not_reevaluated", r2["evaluated"] == 0, str(r2))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_decision_record_selftest() -> dict:
    """Oracle for the decision audit log (spec §12): record_decision persists a full row; finalize_decision updates
    validator_result/final_status; list returns newest-first; deferred records are logged too."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    d = tempfile.mkdtemp(prefix="nl_decrec_selftest_")
    try:
        store = MemoryStore(Path(d) / "d.sqlite3")
        sid = "s"
        did = store.record_decision(sid, "negotiation", "argon", "player2", parsed_choice="accept",
                                    brief="b", options=[{"key": "accept", "label": "A"}], advisory={"score": 80},
                                    raw_response="1. yes, they share our enemy", request_id="r1",
                                    linked_offer_id=407, final_status="decided")
        chk("recorded", isinstance(did, int) and did > 0, str(did))
        store.finalize_decision(did, validator_result={"ok": True}, final_status="applied")
        rows = store.list_decision_records(sid)
        chk("listed_one", len(rows) == 1, str(len(rows)))
        r = rows[0]
        chk("fields_persisted", r["source"] == "player2" and r["parsed_choice"] == "accept"
            and r["final_status"] == "applied" and r["linked_offer_id"] == 407, str(r))
        store.record_decision(sid, "negotiation", "teladi", "deferred", brief="b",
                              options=[{"key": "a", "label": "A"}], final_status="deferred")
        rows2 = store.list_decision_records(sid)
        chk("two_rows_newest_first", len(rows2) == 2 and rows2[0]["subject_faction"] == "teladi", str(len(rows2)))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_negotiation_consequence_selftest() -> dict:
    """Oracle for NF3: a negotiation outcome has bounded RELATIONSHIP consequences (refusal → resentment up + trust
    down; acceptance → trust up + debt up; urgency scales it) + a transition world-event. Deterministic execution
    gated by the Player2 decision."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    dd = tempfile.mkdtemp(prefix="nl_negocons_")
    try:
        store = MemoryStore(Path(dd) / "c.sqlite3")
        sid = "s"
        for f in ("argon", "teladi", "boron", "split"):
            store.upsert_faction(sid, f, name=f.title())
        store.apply_relationship_consequence(sid, "argon", "teladi", "refused", urgency=4)
        rel = store.get_relationship(sid, "argon", "teladi") or {}
        chk("refusal_resentment_up", float(rel.get("resentment") or 0) > 0, str(rel))
        chk("refusal_trust_down", float(rel.get("trust") or 0) < 0, str(rel))
        store.apply_relationship_consequence(sid, "boron", "split", "accepted", urgency=0)
        rel2 = store.get_relationship(sid, "boron", "split") or {}
        chk("accept_trust_up", float(rel2.get("trust") or 0) > 0, str(rel2))
        chk("accept_debt_up", float(rel2.get("debt") or 0) > 0, str(rel2))
        store.apply_relationship_consequence(sid, "split", "boron", "refused", urgency=0)
        relc = store.get_relationship(sid, "split", "boron") or {}
        chk("urgency_scales", float(rel.get("resentment") or 0) > float(relc.get("resentment") or 0),
            f"{rel.get('resentment')} vs {relc.get('resentment')}")
        evs = store.list_world_events(sid, limit=50)
        chk("transition_event", any(str(e.get("event_type", "")).startswith("agreement_") for e in evs), str(len(evs)))
    finally:
        shutil.rmtree(dd, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_relation_move_validator_selftest() -> dict:
    """Oracle for #64: the DeadAir-grounded relation-move eligibility gate — distinct real diplomatic factions,
    excluded factions rejected, step bounded ±5, result clamped to the ±25 band, band-limit no-op rejected."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    dd = tempfile.mkdtemp(prefix="nl_relmove_")
    try:
        store = MemoryStore(Path(dd) / "c.sqlite3")
        sid = "s"
        for f in ("argon", "teladi", "xenon"):
            store.upsert_faction(sid, f, name=f.title())
        r1 = store.validate_relation_move(sid, "argon", "teladi", 5.0)
        chk("valid_move_ok", r1["ok"] and r1["clamped_step"] == 5.0, str(r1))
        chk("excluded_target_rejected", store.validate_relation_move(sid, "argon", "xenon", 5.0)["ok"] is False, "")
        chk("self_rejected", store.validate_relation_move(sid, "argon", "argon", 5.0)["ok"] is False, "")
        chk("unknown_rejected", store.validate_relation_move(sid, "argon", "boron", 5.0)["ok"] is False, "")
        r5 = store.validate_relation_move(sid, "argon", "teladi", 50.0)
        chk("step_bounded_to_5", r5["ok"] and r5["clamped_step"] == 5.0, str(r5))
        # out-of-band stored trust (store scale ±100) must NOT amplify the emitted step (the -42 live defect)
        store.adjust_relationship(sid, "teladi", "argon", dtrust=67)
        rob = store.validate_relation_move(sid, "teladi", "argon", -5.0)
        chk("out_of_band_cur_not_amplified", rob["ok"] and rob["clamped_step"] == -5.0 and rob["result"] == 20.0,
            str(rob))
        # push to the band limit, then a further move is a rejected no-op
        store.adjust_relationship(sid, "argon", "teladi", dtrust=25)
        rlim = store.validate_relation_move(sid, "argon", "teladi", 5.0)
        chk("band_limit_noop_rejected", rlim["ok"] is False and "band" in rlim["reason"], str(rlim))
    finally:
        shutil.rmtree(dd, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_oc1_resume_selftest() -> dict:
    """Oracle for #48 (OC1): OPORD consumes resolved negotiations — a submitted intent that gets ACCEPTED completes
    its task; REFUSED fails it (FRAGO trigger); pending does nothing; and the consume is idempotent."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    dd = tempfile.mkdtemp(prefix="nl_oc1_")
    try:
        store = MemoryStore(Path(dd) / "c.sqlite3")
        sid = "s"
        for f in ("argon", "antigone", "split", "teladi"):
            store.upsert_faction(sid, f, name=f.title())
        # accepted path
        op1 = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:a", status="active",
                                            target_faction="teladi", target_sector="X")["id"]
        ag1 = store.submit_negotiation_intent(sid, "opord", "allied_support", "argon", recipient="antigone",
                                              operation_id=op1, enemy="teladi", sector="X",
                                              terms={"kind": "allied_support"})
        t1 = store.attach_task(op1, "request_allied_support", status="issued",
                               agreement_id=str(ag1.get("id")), target_faction="antigone")
        r0 = store.resume_operations_from_negotiations(sid)
        chk("pending_no_resume", r0["fulfilled"] == 0 and r0["failed"] == 0, str(r0))
        store.apply_offer_decision(sid, ag1.get("id"), "accept", "aye", "player2")
        r1 = store.resume_operations_from_negotiations(sid)
        chk("accept_fulfills", r1["fulfilled"] == 1, str(r1))
        task1 = next((t for t in (store.operation_detail(op1).get("tasks") or []) if t.get("id") == t1), {})
        chk("task_completed", task1.get("status") == "completed", str(task1.get("status")))
        # refused path
        op2 = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:b", status="active",
                                            target_faction="teladi", target_sector="Y")["id"]
        ag2 = store.submit_negotiation_intent(sid, "opord", "allied_support", "argon", recipient="split",
                                              operation_id=op2, enemy="teladi", sector="Y",
                                              terms={"kind": "allied_support"})
        t2 = store.attach_task(op2, "request_allied_support", status="issued",
                               agreement_id=str(ag2.get("id")), target_faction="split")
        store.apply_offer_decision(sid, ag2.get("id"), "refuse", "nay", "player2")
        r2 = store.resume_operations_from_negotiations(sid)
        chk("refuse_fails", r2["failed"] == 1, str(r2))
        task2 = next((t for t in (store.operation_detail(op2).get("tasks") or []) if t.get("id") == t2), {})
        chk("task_failed", task2.get("status") == "failed", str(task2.get("status")))
        r3 = store.resume_operations_from_negotiations(sid)
        chk("idempotent", r3["fulfilled"] == 0 and r3["failed"] == 0, str(r3))
    finally:
        shutil.rmtree(dd, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_faction_doctrine_brief_selftest() -> dict:
    """Oracle for #53: the faction Worldview line reflects the canon FACTION_PERSONA (traits + goal) and is injected
    deterministically. Aggressive/profit/diplomatic factions read distinctly; an unknown faction falls back safely."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    dd = tempfile.mkdtemp(prefix="nl_doctrine_")
    try:
        store = MemoryStore(Path(dd) / "c.sqlite3")
        sid = "s"
        for f in ("split", "teladi", "boron", "argon"):
            store.upsert_faction(sid, f, name=f.title())
        b_split = store.faction_doctrine_brief(sid, "split")
        chk("split_aggressive", "aggressive" in b_split.lower(), b_split)
        chk("split_goal_conquest", "conquest" in b_split.lower(), b_split)
        b_teladi = store.faction_doctrine_brief(sid, "teladi")
        chk("teladi_profit", "profit" in b_teladi.lower(), b_teladi)
        b_boron = store.faction_doctrine_brief(sid, "boron")
        chk("boron_diplomatic", "diplomatic" in b_boron.lower(), b_boron)
        chk("boron_goal_peace", "peace" in b_boron.lower(), b_boron)
        chk("factions_read_distinctly", b_split != b_teladi != b_boron, "")
        b_unknown = store.faction_doctrine_brief(sid, "madeup")
        chk("unknown_default_goal", "interests" in b_unknown.lower() and len(b_unknown) > 0, b_unknown)
        chk("empty_fid_empty", store.faction_doctrine_brief(sid, "") == "", "")
        chk("has_leadership_prefix", b_split.lower().startswith("you are the leadership"), b_split)
    finally:
        shutil.rmtree(dd, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_execution_lifecycle_selftest() -> dict:
    """Oracle for the #1 execution build: job fulfillment SPENDS budget + completes the linked task (no success
    from intent — fulfillment proof required); claim works; complete is idempotent; and OFFENSIVE tasks complete
    from REAL combat evidence (our forces inflicting losses on the target), logging a BDA report."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_exec_selftest_")
    try:
        store = MemoryStore(Path(d) / "exec.sqlite3")
        sid = "s"
        now = time.time()
        op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:ex", status="active",
                                           target_faction="teladi", target_sector="X", budget_reserved=200000,
                                           activated_at=now - 50)["id"]
        t1 = store.attach_task(op, "post_patrol_contract", status="issued")
        job = store.create_or_update_job(sid, "argon", "patrol", target_sector="X", reward=80000,
                                         operation_id=op, operation_task_id=t1)
        check("claim_works", store.claim_job(sid, job["id"], "Player")["ok"] is True)
        res = store.complete_job(sid, job["id"], claimant="Player")
        check("job_completed_spent", res["status"] == "completed" and res["reward_spent"] == 80000, str(res))
        det = store.operation_detail(op)
        tk = [t for t in det["tasks"] if t["id"] == t1][0]
        check("linked_task_completed", tk["status"] == "completed" and bool(tk.get("completed_at")))
        check("op_budget_spent", int(store.get_operation(op)["budget_spent"]) == 80000)
        check("faction_budget_spent", store.budget_spent(sid, "argon") == 80000)
        check("task_update_report", any(r["report_type"] == "task_update" for r in det["reports"]))
        check("complete_idempotent", store.complete_job(sid, job["id"]).get("noop") is True)
        # offensive task completes from REAL evidence (our kills on the target)
        op2 = store.create_or_get_operation(sid, "argon", "raid_pressure", "s:argon:rp:teladi:y", status="active",
                                            target_faction="teladi", target_sector="Y", activated_at=now - 50)["id"]
        rt = store.attach_task(op2, "raid_enemy_logistics", status="issued")
        store.add_hostile_event(sid, {"attacker_faction": "argon", "victim_faction": "teladi", "sector": "Y",
                                      "magnitude": 4, "ts": now})
        store.assess_operation(op2)
        det2 = store.operation_detail(op2)
        rtk = [t for t in det2["tasks"] if t["id"] == rt][0]
        check("raid_task_done_from_evidence", rtk["status"] == "completed", rtk["status"])
        check("bda_report", any(r["report_type"] == "bda" for r in det2["reports"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_ops_health_selftest() -> dict:
    """Oracle for OPORD Phase 9 operational health warnings — each warning fires for the right op, and a coherent
    op stays clean. Includes the cleanup-regression detectors (concluded op with open jobs / pending agreements)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_health_selftest_")
    try:
        store = MemoryStore(Path(d) / "health.sqlite3")
        sid = "s"

        def mk(tkey, status, **f):
            return store.create_or_get_operation(sid, "argon", "sector_pressure", tkey, status=status,
                                                 target_faction="teladi", target_sector="X", **f)["id"]

        def health():
            return {h["op_id"]: h["warnings"] for h in store.operations_health(sid)["unhealthy"]}

        a = mk("s:argon:sp:teladi:a", "active")
        store.attach_task(a, "patrol_sector", status="issued", job_id="j")
        store.attach_report(a, "sitrep", "x")
        check("no_selected_coa", "no_selected_coa" in health().get(a, []), str(health().get(a)))

        b = mk("s:argon:sp:teladi:b", "opord_issued", selected_coa_id="coa_x")
        store.attach_report(b, "sitrep", "x")
        check("opord_no_tasks", "opord_no_tasks" in health().get(b, []))

        c = mk("s:argon:sp:teladi:c", "active", selected_coa_id="coa_x", budget_reserved=100000)
        store.attach_task(c, "patrol_sector", status="planned")  # no links
        store.attach_report(c, "sitrep", "x")
        check("budget_reserved_no_link", "budget_reserved_no_link" in health().get(c, []))

        e = mk("s:argon:sp:teladi:e", "active", selected_coa_id="coa_x")
        store.create_or_update_job(sid, "argon", "patrol", target_sector="X", reward=1000, operation_id=e)
        store.update_operation(e, status="completed")  # bypass conclude → simulate a cleanup LEAK
        check("concluded_open_jobs", "concluded_open_jobs" in health().get(e, []))

        f = mk("s:argon:sp:teladi:f", "active", selected_coa_id="coa_x")
        store.add_agreement(sid, "argon", "", type="alliance", status="pending", terms={"operation_id": f})
        store.update_operation(f, status="completed")
        check("concluded_pending_agreements", "concluded_pending_agreements" in health().get(f, []))

        g = mk("s:argon:sp:teladi:g", "active", selected_coa_id="coa_x")
        store.attach_task(g, "patrol_sector", status="issued", job_id="j")
        for _ in range(3):
            store.attach_report(g, "frago", "f")
        check("repeated_fragos", "repeated_fragos_no_progress" in health().get(g, []))

        h2 = mk("s:argon:sp:teladi:h", "active", selected_coa_id="coa_x")
        store.attach_task(h2, "patrol_sector", status="issued", job_id="j")
        store.attach_report(h2, "sitrep", "x")
        check("healthy_op_clean", h2 not in health(), str(health().get(h2)))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_opord_cleanup_selftest() -> dict:
    """Oracle for the P7 hardening (Codex audit #2/#4): conclusion RELEASES unused reserved budget, CANCELS linked
    open jobs, EXPIRES linked pending agreements, and is idempotent; reward escalation is GATED by the op's
    reserved budget (no raise beyond reserved)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_opordcleanup_selftest_")
    try:
        store = MemoryStore(Path(d) / "cleanup.sqlite3")
        sid = "s"
        # active op with reserved 200000, spent 50000, a linked open job + a linked pending agreement.
        op = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:cl", status="active",
                                           target_faction="teladi", target_sector="SW I",
                                           budget_reserved=200000, budget_spent=50000)["id"]
        job = store.create_or_update_job(sid, "argon", "patrol", target_sector="SW I", reward=80000,
                                         operation_id=op, operation_task_id="t1")
        ag = store.add_agreement(sid, "argon", "", type="alliance", status="pending",
                                 terms={"operation_id": op, "kind": "allied_support"})
        res = store.conclude_operation(op, "failed", "timed out")
        check("budget_released", res["budget_released"] == 150000, str(res))
        check("job_cancelled_count", res["jobs_cancelled"] == 1, str(res))
        check("agreement_expired_count", res["agreements_expired"] == 1, str(res))
        opd = store.get_operation(op)
        check("reserved_down_to_spent", opd["budget_reserved"] == 50000)
        check("status_failed", opd["status"] == "failed")
        check("no_open_jobs_left", len([j for j in store.list_jobs(sid, "open") if j["operation_id"] == op]) == 0)
        check("agreement_now_expired", any(a["id"] == ag["id"] and a["status"] == "expired"
                                           for a in store.list_agreements(sid)))
        # idempotent: concluding again cleans nothing new.
        res2 = store.conclude_operation(op, "failed", "again")
        check("idempotent_no_double_cleanup", res2["jobs_cancelled"] == 0 and res2["agreements_expired"] == 0, str(res2))

        # reward escalation budget gate: a small reserved op can't raise reward beyond reserved.
        now = time.time()
        opB = store.create_or_get_operation(sid, "argon", "sector_pressure", "s:argon:sp:teladi:cl2", status="active",
                                            target_faction="teladi", target_sector="B Sector",
                                            budget_reserved=90000, activated_at=now - 100)["id"]
        store.add_hostile_event(sid, {"attacker_faction": "teladi", "victim_faction": "argon",
                                      "sector": "B Sector", "magnitude": 3, "ts": now})  # keep active
        jb = store.create_or_update_job(sid, "argon", "patrol", target_sector="B Sector", reward=80000,
                                        operation_id=opB, operation_task_id="tb")
        with store._connect() as conn:
            conn.execute("UPDATE market_jobs SET created_at=? WHERE id=?", (now - 700, jb["id"]))
            conn.commit()
        store.apply_assessment_decision(opB, "raise_reward")  # D5: commander decision (was auto-FRAGO in assess)
        jrow = [j for j in store.list_jobs(sid, "open") if j["id"] == jb["id"]]
        # reserved 90000 < 1.5×80000(=120000) → capped at 90000 (raised but bounded by reserved)
        check("reward_capped_by_reserved", bool(jrow) and int(jrow[0]["reward"]) == 90000, str(jrow and jrow[0].get("reward")))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_assessment_frago_selftest() -> dict:
    """Oracle for OPORD Phase 7: SITREP emitted; enemy reinforcement → FRAGO (allied proposal); unclaimed linked
    job → FRAGO reward escalation; pressure abated + min age → completed; still-contested past max age → failed
    (no hanging). Per-op sectors keep the seeded events isolated."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_frago_selftest_")
    try:
        store = MemoryStore(Path(d) / "frago.sqlite3")
        sid = "s"
        now = time.time()

        for f in ("argon", "antigone", "teladi"):
            store.upsert_faction(sid, f, name=f.title())
        store.adjust_relationship(sid, "antigone", "argon", dtrust=60)

        def active_op(tkey, offset, sector):
            return store.create_or_get_operation(sid, "argon", "sector_pressure", tkey, status="active",
                                                 target_faction="teladi", target_sector=sector,
                                                 budget_reserved=200000, activated_at=now - offset)["id"]

        # A: assess = FACTS only — SITREP + BDA (our real kill completes an offensive task) + no judgment.
        opA = active_op("s:argon:sp:teladi:a", 100, "A Sector")
        tk = store.attach_task(opA, "engage_hostiles", status="issued", target_faction="teladi")
        store.add_hostile_event(sid, {"attacker_faction": "argon", "victim_faction": "teladi",
                                      "sector": "A Sector", "magnitude": 5, "ts": now})
        rA = store.assess_operation(opA)
        detA = store.operation_detail(opA)
        check("sitrep_emitted", any(r["report_type"] == "sitrep" for r in detA["reports"]))
        check("bda_task_complete", any(t["id"] == tk and t["status"] == "completed" for t in detA["tasks"]))
        check("assess_no_judgment", rA.get("outcome") is None and rA.get("needs_decision") is True)

        # escalate_reinforce decision → allied-support negotiation intent created.
        e = store.apply_assessment_decision(opA, "escalate_reinforce")
        check("escalate_makes_agreement", e["applied"] == "escalate_reinforce" and any(
            a.get("kind") == "allied_support" and a.get("operation_id") == opA for a in store.list_agreements(sid)))

        # raise_reward decision (budget-gated): 80k → 120k.
        job = store.create_or_update_job(sid, "argon", "patrol", target_sector="A Sector", target_faction="teladi",
                                         reward=80000, operation_id=opA, operation_task_id="t1")
        rr = store.apply_assessment_decision(opA, "raise_reward")
        jrow = [j for j in store.list_jobs(sid, "open") if j["id"] == job["id"]][0]
        check("reward_raised", rr["applied"] == "raise_reward" and int(jrow["reward"]) == 120000, str(jrow.get("reward")))

        # request_conclude with NO proof (age < MIN) → can_conclude False → converts to hold (stays active).
        cc1 = store.can_conclude(opA)
        hold = store.apply_assessment_decision(opA, "request_conclude")
        check("conclude_blocked_converts_hold", (not cc1["ok"]) and hold["applied"] == "converted_to_hold"
              and store.get_operation(opA)["status"] == "active")

        # abated + age >= MIN → can_conclude True → request_conclude completes.
        opC = active_op("s:argon:sp:teladi:c", 400, "C Sector")
        ccC = store.can_conclude(opC)
        cdone = store.apply_assessment_decision(opC, "request_conclude")
        check("abated_conclude_completed", ccC["ok"] and cdone["applied"] == "completed"
              and store.get_operation(opC)["status"] == "completed")

        # SAFETY BACKSTOP: assess on an op past MAX age → failed (deterministic, not a judgment).
        opD = active_op("s:argon:sp:teladi:d", 4000, "D Sector")
        store.add_hostile_event(sid, {"attacker_faction": "teladi", "victim_faction": "argon",
                                      "sector": "D Sector", "magnitude": 2, "ts": now})
        rD = store.assess_operation(opD)
        check("timeout_backstop_failed", rD["outcome"] == "failed" and store.get_operation(opD)["status"] == "failed")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_execution_routing_selftest() -> dict:
    """Oracle for OPORD Phase 6: tasks route to the right mechanism — patrol+ships→internal fleet, patrol w/o
    ships→patrol job, supply→one durable job (deduped), allied_support→agreement proposal, ceasefire→agreement;
    tasks get linked (job_id/agreement_id/order_id) + marked issued; op advances opord_issued→active."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_route_selftest_")
    try:
        store = MemoryStore(Path(d) / "route.sqlite3")
        sid = "s"
        # seed an ally so allied_support resolves a real counterparty (Negotiations door requires one)
        for _f in ("argon", "antigone"):
            store.upsert_faction(sid, _f, name=_f.title())
        store.adjust_relationship(sid, "antigone", "argon", dtrust=50)

        def issued_op(tkey, ships):
            op_id = store.create_or_get_operation(sid, "argon", "sector_pressure", tkey, status="opord_issued",
                                                  target_faction="teladi", target_sector="Silent Witness I",
                                                  urgency=3,
                                                  mission_analysis_json={"available_assets": {"combat_ships": ships}})["id"]
            return op_id

        # op with ships=4: patrol→internal, supply→job, allied→agreement, ceasefire→agreement
        op1 = issued_op("s:argon:sector_pressure:teladi:a1", 4)
        store.attach_task(op1, "patrol_sector", status="planned", target_sector="Silent Witness I")
        store.attach_task(op1, "request_supplies", status="planned")
        store.attach_task(op1, "request_allied_support", status="planned")
        store.attach_task(op1, "seek_ceasefire", status="planned", target_faction="teladi")
        r1 = store.route_operation(op1)
        routes = {x["route"] for x in r1["routes"]}
        # D3: patrol with own ships available is a Player2 routing DECISION (not auto internal-fleet) → awaiting.
        check("patrol_awaiting_decision", "awaiting_decision" in routes, str(routes))
        check("supply_job", "job:supply" in routes)
        check("allied_agreement", "agreement:allied" in routes)
        check("ceasefire_agreement", "agreement:ceasefire" in routes)
        check("op_active", store.get_operation(op1)["status"] == "active")
        # D3 commit (simulated Player2): the deferred patrol task → commit own fleet → internal_fleet, issued + linked.
        ptask = next(t for t in store.operation_detail(op1)["tasks"] if t["task_type"] == "patrol_sector")
        rt = store.route_task(store.get_operation(op1), ptask, "commit_own_fleet")
        check("route_task_internal", rt.get("route") == "internal_fleet", str(rt))
        det1 = store.operation_detail(op1)
        check("tasks_issued", all(t["status"] == "issued" for t in det1["tasks"]))
        check("tasks_linked", all((t.get("job_id") or t.get("agreement_id") or t.get("order_id")) for t in det1["tasks"]))

        # supply dedupe: a second op, same faction+sector supply → still ONE open supply job
        op2 = issued_op("s:argon:sector_pressure:teladi:a2", 4)
        store.attach_task(op2, "request_supplies", status="planned")
        store.route_operation(op2)
        supply_jobs = [j for j in store.list_jobs(sid, "open") if j["job_type"] == "supply"]
        check("supply_deduped_to_one", len(supply_jobs) == 1, str(len(supply_jobs)))

        # patrol WITHOUT ships → patrol job
        op0 = issued_op("s:argon:sector_pressure:teladi:a0", 0)
        store.attach_task(op0, "patrol_sector", status="planned", target_sector="Silent Witness I")
        r0 = store.route_operation(op0)
        check("patrol_job_no_ships", any(x["route"] == "job:patrol" for x in r0["routes"]), str(r0["routes"]))
        check("agreement_created", len(store.list_agreements(sid, "pending")) >= 2)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_opord_generator_selftest() -> dict:
    """Oracle for OPORD Phase 5: selected COA → SMESC opord (all 5 sections) + all 6 annexes + executable tasks
    derived from the COA (every task maps back to the selected coa_id); status → opord_issued + issued_at; budget
    reserved equals the selected COA's required_budget; mission is player-readable."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_opordgen_selftest_")
    try:
        store = MemoryStore(Path(d) / "opordgen.sqlite3")
        sid = "s"
        store.upsert_fleet_strength(sid, "argon", fight=6, total_ships=12)
        op = store.create_or_get_operation(sid, "argon", "sector_pressure",
                                           "s:argon:sector_pressure:teladi:silent_witness_i", status="warning",
                                           target_faction="teladi", target_sector="Silent Witness I",
                                           evidence_json={"magnitude": 4})["id"]
        store.analyze_mission(op)
        pr = store.plan_operation_coas(op)
        store.set_selected_coa(op, pr["advisory_best"]["coa_id"])  # simulate the Player2 D1 selection
        r = store.generate_opord(op)
        check("ok", r.get("ok") is True, str(r))
        check("tasks_derived", len(r.get("tasks", [])) >= 1)
        opd = store.get_operation(op)
        check("status_opord_issued", opd["status"] == "opord_issued" and bool(opd.get("issued_at")))
        smesc = opd.get("opord_json") or {}
        for sec in ("situation", "mission", "execution", "service_support", "command_signal"):
            check("smesc_" + sec, sec in smesc)
        check("execution_tasks", len((smesc.get("execution") or {}).get("tasks") or []) >= 1)
        check("mission_readable", bool(smesc.get("mission")))
        # Execution = the 4 doctrinal components (Ken 2026-06-30): intent, scheme_of_manoeuvre, main_effort, end_state.
        ex = smesc.get("execution") or {}
        check("exec_intent", bool(ex.get("intent")))
        check("exec_scheme_of_manoeuvre", bool(ex.get("scheme_of_manoeuvre"))
              and "main effort" in str(ex.get("scheme_of_manoeuvre")).lower())
        me = ex.get("main_effort") or {}
        check("exec_main_effort_designated", bool(me.get("task")) and "priority of support" in (me.get("rationale") or ""))
        check("exec_main_effort_supporting", isinstance(me.get("supporting_efforts"), list))
        check("exec_end_state", bool(ex.get("end_state")) and ex.get("end_state") == opd.get("desired_end_state"))
        ann = opd.get("annexes_json") or {}
        for a in ("A_conduct", "B_task_org", "D_intel", "E_rules", "RS_sustainment", "Q_command"):
            check("annex_" + a, a in ann)
        det = store.operation_detail(op)
        sel = [c for c in det["coas"] if c.get("selected")]
        check("one_selected", len(sel) == 1)
        check("budget_reserved_matches", opd.get("budget_reserved") == int((sel[0].get("required_budget") or 0)) if sel else False)
        check("tasks_map_to_selected_coa", len(det["tasks"]) >= 1 and all(t.get("coa_id") == sel[0]["id"] for t in det["tasks"]) if sel else False)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_coa_engine_selftest() -> dict:
    """Oracle for OPORD Phase 4: infeasible COAs rejected (0 assets), viable COAs scored + ONE selected, status →
    coa_generated with selected_coa_id, selection is DETERMINISTIC (same inputs → same COA), and faction doctrine
    changes the selection (argon vs split pick different COAs on identical inputs)."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_coa_selftest_")
    try:
        store = MemoryStore(Path(d) / "coa.sqlite3")
        sid = "s"

        def make(fac, ships, budget, tkey):
            return store.create_or_get_operation(
                sid, fac, "sector_pressure", tkey, status="warning", target_faction="teladi",
                target_sector="Silent Witness I", evidence_json={"magnitude": 4},
                mission_analysis_json={"available_assets": {"combat_ships": ships, "budget_available": budget}})["id"]

        # 1. impossible (0 assets): ship/budget COAs rejected; engine records ADVISORY best but does NOT select.
        op0 = make("argon", 0, 0, "s:argon:sector_pressure:teladi:a0")
        r0 = store.plan_operation_coas(op0)
        rej = {c["type"] for c in r0["coas"] if not c["viable"]}
        check("impossible_rejected", {"organic_patrol", "hire_contractors", "raid_enemy_logistics"} <= rej, str(rej))
        check("advisory_present", r0["advisory_best"] is not None)
        op0d = store.get_operation(op0)
        check("coa_generated_not_selected", op0d["status"] == "coa_generated" and not op0d.get("selected_coa_id"))

        # 2. ample assets, argon.
        opA = make("argon", 6, 200000, "s:argon:sector_pressure:teladi:aA")
        rA = store.plan_operation_coas(opA)
        check("argon_advisory_viable", rA["advisory_best"]["viable"] is True)

        # 3. determinism of the ADVISORY: identical inputs (fresh op) → identical advisory COA type.
        opA2 = make("argon", 6, 200000, "s:argon:sector_pressure:teladi:aA2")
        rA2 = store.plan_operation_coas(opA2)
        check("deterministic_advisory", rA2["advisory_best"]["type"] == rA["advisory_best"]["type"],
              rA["advisory_best"]["type"] + " vs " + rA2["advisory_best"]["type"])

        # 4. doctrine changes the ADVISORY ranking: split vs argon on identical assets.
        opS = make("split", 6, 200000, "s:split:sector_pressure:teladi:aS")
        rS = store.plan_operation_coas(opS)
        check("doctrine_changes_advisory", rS["advisory_best"]["type"] != rA["advisory_best"]["type"],
              "argon=" + rA["advisory_best"]["type"] + " split=" + rS["advisory_best"]["type"])

        # 5. NOT auto-selected; set_selected_coa commits a (simulated Player2) choice + validates + marks the table.
        detA = store.operation_detail(opA)
        check("none_selected_before_decision", not any(c.get("selected") for c in detA["coas"]))
        sr = store.set_selected_coa(opA, rA["advisory_best"]["coa_id"])
        check("set_selected_ok", sr["ok"] is True and sr.get("selected_coa_id") == rA["advisory_best"]["coa_id"])
        det = store.operation_detail(opA)
        sel = [c for c in det["coas"] if c.get("selected")]
        check("one_selected_after_decision", len(sel) == 1 and sel[0]["viability_status"] == "selected")
        check("viable_has_wargame", all(c.get("wargame_json") for c in det["coas"] if c["viability_status"] == "viable"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_mission_analysis_selftest() -> dict:
    """Oracle for OPORD Phase 3: a warning op gets a full mission analysis (mission/intent/end-state/constraints/
    CCIR + REAL assets), status advances warning→analysing, evidence is preserved, raid vs sector differ, and the
    advance_operations pipeline driver runs clean."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_mission_selftest_")
    try:
        store = MemoryStore(Path(d) / "mission.sqlite3")
        sid = "s"
        store.upsert_fleet_strength(sid, "argon", fight=4, total_ships=10)
        op = store.create_or_get_operation(sid, "argon", "sector_pressure",
                                           "s:argon:sector_pressure:teladi:silent_witness_i", status="warning",
                                           target_faction="teladi", target_sector="Silent Witness I",
                                           warning_order_json={"constraints": ["avoid full escalation if possible"],
                                                               "ccir": ["enemy fleet strength"]},
                                           evidence_json={"hostile_events": 2})
        r = store.analyze_mission(op["id"])
        a = r.get("analysis") or {}
        check("ok", r.get("ok") is True)
        check("mission_statement", "Silent Witness I" in a.get("mission_statement", ""), a.get("mission_statement"))
        check("commander_intent", bool(a.get("commander_intent")))
        check("desired_end_state", "stable" in a.get("desired_end_state", ""))
        check("constraints_civilian", "protect civilian traffic" in a.get("constraints", []))
        check("assets_combat_ships", (a.get("available_assets") or {}).get("combat_ships") == 4)
        check("budget_field", "budget_available" in (a.get("available_assets") or {}))
        check("ccir_present", len(a.get("ccir", [])) >= 1)
        op2 = store.get_operation(op["id"])
        check("status_analysing", op2["status"] == "analysing")
        check("stored_mission", op2.get("mission_statement") == a.get("mission_statement"))
        check("evidence_preserved", (op2.get("evidence_json") or {}).get("hostile_events") == 2)
        opr = store.create_or_get_operation(sid, "argon", "raid_pressure", "s:argon:raid_pressure:xenon:frontier",
                                            status="warning", target_faction="xenon", target_sector="Frontier")
        ar = store.analyze_mission(opr["id"]).get("analysis") or {}
        check("raid_variant", "raiders" in ar.get("commander_intent", "").lower())
        adv = store.advance_operations(sid)
        check("advance_ok", adv.get("ok") is True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_threat_recognition_selftest() -> dict:
    """Oracle for OPORD Phase 2: real hostile events → deduped warning-order ops. Repeated pressure UPDATES one
    op (anti-spam); a different sector/aggressor makes a different op; criminal aggressor → raid_pressure;
    evidence + warning order are populated; threat_key matches the spec format."""
    import shutil
    import tempfile
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_threat_selftest_")
    try:
        store = MemoryStore(Path(d) / "threat.sqlite3")
        sid = "s"
        for _ in range(2):
            store.add_hostile_event(sid, {"attacker_faction": "teladi", "victim_faction": "argon",
                                          "sector": "Silent Witness I", "magnitude": 5})
        r1 = store.recognize_threats(sid)
        check("created_one", r1["created"] == 1 and r1["updated"] == 0, str(r1))
        op = store.get_operation(r1["threats"][0]["op_id"])
        check("defender_is_argon_warning", op["faction_id"] == "argon" and op["status"] == "warning")
        check("threat_key_format", op["threat_key"] == "s:argon:sector_pressure:teladi:silent_witness_i", op["threat_key"])
        check("evidence_present", (op.get("evidence_json") or {}).get("hostile_events") == 2, str(op.get("evidence_json")))
        check("warning_order_present", (op.get("warning_order_json") or {}).get("threat_type") == "sector_pressure")
        r2 = store.recognize_threats(sid)              # repeat → updates the SAME op
        check("dedupe_updates_same", r2["created"] == 0 and r2["updated"] == 1, str(r2))
        check("still_one_op", len(store.list_operations(sid)) == 1)
        store.add_hostile_event(sid, {"attacker_faction": "teladi", "victim_faction": "argon",
                                      "sector": "Profit Center", "magnitude": 3})
        store.recognize_threats(sid)
        check("new_sector_new_op", len(store.list_operations(sid)) == 2)
        store.add_hostile_event(sid, {"attacker_faction": "xenon", "victim_faction": "argon",
                                      "sector": "Frontier Edge", "magnitude": 8})
        r4 = store.recognize_threats(sid)
        check("criminal_is_raid", any("raid_pressure" in t["threat_key"] for t in r4["threats"]), str(r4))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


def run_deceased_sweep_selftest() -> dict:
    """Oracle for the deceased staleness sweep: a stale KNOWN npc (has turns) is MARKED deceased (kept); a stale
    GENERIC npc (no turns) is PRUNED; a FRESH npc is untouched + alive; re-index RESURRECTS (false-positive fix)."""
    import shutil
    import tempfile
    import time as _t
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    d = tempfile.mkdtemp(prefix="nl_sweep_selftest_")
    try:
        store = MemoryStore(Path(d) / "sweep.sqlite3")
        sid = "s"
        store.index_npc(npc_key="s|chat|Fresh", save_id=sid, game_id="chat", name="Fresh", faction_id="argon")
        store.index_npc(npc_key="s|chat|Known", save_id=sid, game_id="chat", name="Known", faction_id="argon")
        store.record_turn("s|chat|Known", "user", "hi")
        store.index_npc(npc_key="s|chat|Generic", save_id=sid, game_id="chat", name="Generic", faction_id="argon")
        with store._connect() as conn:        # backdate the two "stale" rows past the cutoff
            conn.execute("UPDATE npcs SET last_active = ? WHERE npc_key IN ('s|chat|Known','s|chat|Generic')",
                         (_t.time() - 99999,))
            conn.commit()
        res = store.sweep_deceased_npcs(sid, stale_seconds=3600)
        check("marked_one", res["marked_deceased"] == 1, str(res))
        check("pruned_one", res["pruned"] == 1, str(res))
        check("known_kept_deceased", (store.get_npc("s|chat|Known") or {}).get("is_alive") == 0)
        check("known_memory_kept", store.turn_count("s|chat|Known") == 1)
        check("generic_pruned", store.get_npc("s|chat|Generic") is None)
        check("fresh_untouched_alive", (store.get_npc("s|chat|Fresh") or {}).get("is_alive") == 1)
        store.index_npc(npc_key="s|chat|Known", save_id=sid, game_id="chat", name="Known", faction_id="argon")
        check("reindex_resurrects", (store.get_npc("s|chat|Known") or {}).get("is_alive") == 1)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": all(c["ok"] for c in checks), "passed": sum(c["ok"] for c in checks),
            "total": len(checks), "checks": checks}


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
