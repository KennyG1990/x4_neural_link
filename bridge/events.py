"""Neural Link bridge — event queue with green-light batched LLM flush.

Problem: pushing every X4 event to the LLM as it happens is unaffordable, thrashes
the single-model gate, and bloats memory. Solution: buffer events cheaply, and let a
*group* through the LLM on an interval — a traffic light.

  enqueue(event)  -> pending_events (SQLite, no LLM)
  worker loop     -> every flush_interval_s, or when a target piles up, or on a
                     priority-5 event, the light turns green:
  flush()         -> pop up to batch_size pending events, coalesce dupes, send ONE
                     consolidated prompt to a resolver (the Strategic-AI NPC), log the
                     single resolution, condense into memory. N events -> 1 LLM call.

A single drain lane (one flush at a time, behind the chat gate) gives backpressure:
a flood of 1,000 events drains in controlled groups instead of thrashing.

Stdlib only. The resolver is injectable (router wires it to Player2; tests pass a stub).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


class EventQueue:
    def __init__(
        self,
        db_path: Path | str,
        resolver: Optional[Callable[[list[dict]], dict]] = None,
        memory: Any = None,
        flush_interval_s: float = 12.0,
        batch_size: int = 25,
        priority_importance: int = 5,
    ) -> None:
        self.db_path = Path(db_path)
        self.resolver = resolver
        self.memory = memory
        self.flush_interval_s = flush_interval_s
        self.batch_size = batch_size
        self.priority_importance = priority_importance
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()   # one flush at a time (the single lane)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_flush_ts: float = 0.0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT,
                    etype TEXT,
                    summary TEXT NOT NULL,
                    importance INTEGER DEFAULT 2,
                    sector TEXT,
                    faction TEXT,
                    ts REAL NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_events(status, importance, ts)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS flush_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    reason TEXT,
                    batch_size INTEGER,
                    coalesced INTEGER,
                    latency_ms INTEGER,
                    ok INTEGER,
                    resolution TEXT
                )
            """)
            conn.commit()

    # --- ingest --------------------------------------------------------------

    def enqueue(self, summary: str, target: str = "global", etype: str = "report",
                importance: int = 2, sector: Optional[str] = None,
                faction: Optional[str] = None) -> None:
        if not summary:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_events (target, etype, summary, importance, sector, faction, ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (target, etype, summary, int(importance), sector, faction, time.time()),
            )
            conn.commit()
        # Priority preempt: an importance-5 event is an ambulance — flush now.
        if int(importance) >= self.priority_importance:
            threading.Thread(target=self.flush, kwargs={"reason": "priority"}, daemon=True).start()

    def pending_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM pending_events WHERE status='pending'").fetchone()["c"]

    # --- worker (the traffic light) ------------------------------------------

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="event-flush", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            try:
                self.flush(reason="interval")
            except Exception as exc:  # worker must never die
                self._log_flush("error", 0, 0, 0, False, f"worker error: {exc}")

    # --- flush (let a group through) -----------------------------------------

    @staticmethod
    def _coalesce(events: list[dict]) -> list[str]:
        """Merge identical summaries into '(xN) ...' so the prompt isn't repetitive."""
        counts: dict[tuple, int] = {}
        order: list[tuple] = []
        meta: dict[tuple, dict] = {}
        for e in events:
            key = (e.get("etype"), e.get("summary"))
            if key not in counts:
                counts[key] = 0
                order.append(key)
                meta[key] = e
            counts[key] += 1
        lines = []
        for key in order:
            etype, summary = key
            n = counts[key]
            prefix = f"[{etype}] " if etype else ""
            lines.append((f"(x{n}) " if n > 1 else "") + prefix + str(summary))
        return lines

    def flush(self, max_batch: Optional[int] = None, reason: str = "manual") -> dict:
        # One flush at a time — the single drain lane.
        if not self._flush_lock.acquire(blocking=False):
            return {"flushed": 0, "skipped": "a flush is already in progress"}
        try:
            limit = max_batch or self.batch_size
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM pending_events WHERE status='pending' "
                    "ORDER BY importance DESC, ts ASC LIMIT ?",
                    (limit,),
                ).fetchall()
                events = [dict(r) for r in rows]
                ids = [r["id"] for r in rows]
            if not events:
                return {"flushed": 0}

            coalesced_lines = self._coalesce(events)
            start = time.time()
            if self.resolver:
                try:
                    res = self.resolver(events)
                except Exception as exc:
                    res = {"ok": False, "resolution": f"resolver error: {exc}", "latency_ms": int((time.time() - start) * 1000)}
            else:
                res = {"ok": True, "resolution": "(no resolver) " + " | ".join(coalesced_lines)[:400],
                       "latency_ms": int((time.time() - start) * 1000)}

            # Mark processed (delete — they live on as the logged resolution + memory).
            with self._lock, self._connect() as conn:
                if ids:
                    conn.execute(f"DELETE FROM pending_events WHERE id IN ({','.join('?' for _ in ids)})", ids)
                conn.commit()

            resolution = str(res.get("resolution") or "")
            latency = int(res.get("latency_ms") or int((time.time() - start) * 1000))
            ok = bool(res.get("ok"))
            self._log_flush(reason, len(events), len(coalesced_lines), latency, ok, resolution)
            self._last_flush_ts = time.time()

            # Condense the batch into a memory fact for any non-global target(s).
            if self.memory and resolution:
                targets = {e.get("target") for e in events if e.get("target") and e.get("target") != "global"}
                for tgt in targets:
                    try:
                        self.memory.add_fact(tgt, resolution[:400], category="diplomacy")
                    except Exception:
                        pass

            return {"flushed": len(events), "coalesced": len(coalesced_lines), "ok": ok,
                    "latency_ms": latency, "resolution": resolution}
        finally:
            self._flush_lock.release()

    def _log_flush(self, reason: str, batch: int, coalesced: int, latency_ms: int, ok: bool, resolution: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO flush_log (ts, reason, batch_size, coalesced, latency_ms, ok, resolution) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), reason, batch, coalesced, latency_ms, int(ok), resolution[:1000]),
            )
            conn.commit()

    # --- simulation + dashboard ----------------------------------------------

    def simulate(self, n_npcs: int = 500, events_per: int = 1) -> dict:
        """Flood the queue with synthetic conversation/events from n_npcs NPCs."""
        import random
        rnd = random.Random(7)
        kinds = [
            ("report", 2, "Officer {i} reports patrol status in Sector-{s}."),
            ("trade", 2, "Officer {i} flags a supply shortfall in Sector-{s}."),
            ("battle", 3, "Officer {i}'s wing engaged Xenon in Sector-{s}."),
            ("death", 5, "Officer {i} reports a capital ship lost in Sector-{s}."),
            ("war", 4, "Officer {i} warns of escalating conflict in Sector-{s}."),
        ]
        n = 0
        for i in range(n_npcs):
            for _ in range(events_per):
                etype, imp, tmpl = rnd.choice(kinds)
                self.enqueue(
                    summary=tmpl.format(i=i, s=i % 12),
                    target=f"events|stress_game|Officer {i:03d}",
                    etype=etype, importance=imp, sector=f"Sector-{i % 12}",
                )
                n += 1
        return {"ok": True, "enqueued": n, "pending": self.pending_count()}

    def clear(self) -> dict:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM pending_events")
            conn.execute("DELETE FROM flush_log")
            conn.commit()
        return {"ok": True}

    def state(self, history: int = 20) -> dict:
        with self._connect() as conn:
            pending = conn.execute("SELECT COUNT(*) AS c FROM pending_events WHERE status='pending'").fetchone()["c"]
            by_type = {r["etype"]: r["c"] for r in conn.execute(
                "SELECT etype, COUNT(*) AS c FROM pending_events WHERE status='pending' GROUP BY etype")}
            flushes = [dict(r) for r in conn.execute(
                "SELECT ts, reason, batch_size, coalesced, latency_ms, ok, resolution "
                "FROM flush_log ORDER BY id DESC LIMIT ?", (history,)).fetchall()]
            total_flushes = conn.execute("SELECT COUNT(*) AS c FROM flush_log").fetchone()["c"]
            total_resolved = conn.execute("SELECT COALESCE(SUM(batch_size),0) AS s FROM flush_log").fetchone()["s"]
        return {
            "pending": pending,
            "pending_by_type": by_type,
            "config": {"flush_interval_s": self.flush_interval_s, "batch_size": self.batch_size,
                       "priority_importance": self.priority_importance, "worker_running": bool(self._worker and self._worker.is_alive())},
            "last_flush_ts_ms": int(self._last_flush_ts * 1000) if self._last_flush_ts else None,
            "total_flushes": total_flushes,
            "total_events_resolved": total_resolved,
            "flushes": [{**f, "ts_ms": int(f["ts"] * 1000)} for f in flushes],
        }
