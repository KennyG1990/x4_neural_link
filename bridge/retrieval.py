"""Lightweight retrieval (vector RAG v0) for the Neural Link bridge.

Per-turn retrieval: instead of dumping the whole situation briefing into every prompt, retrieve only
the knowledge chunks relevant to THIS message and inject those — Miliardo's "inject context on demand",
and the simplified core of RoleRAG's graph-guided retrieval (RAG ladder: vector → hybrid → graphRAG).

v0 uses pure-stdlib TF-IDF cosine, so it adds ZERO dependencies and runs anywhere the bridge runs
(the host has no torch/sentence-transformers/Player2-embeddings yet). The index/retrieve interface is
deliberately scorer-agnostic: swap `_doc_vector`/`_query_vector` for a real embedding model later and
nothing else changes. Lexical now → semantic when an embedder is available.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: Any) -> list[str]:
    return _TOKEN.findall(str(text or "").lower())


class TfidfRetriever:
    """Index a set of {id, text, ...} docs and retrieve the top-k most relevant to a query.

    Pure stdlib. Degrades safely on empty/garbage input (returns [] / 0, never throws)."""

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []
        self._vecs: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}

    def index(self, docs: list[dict[str, Any]]) -> int:
        self._docs = [d for d in (docs or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
        n = len(self._docs)
        df: Counter = Counter()
        toks_list: list[list[str]] = []
        for d in self._docs:
            toks = tokenize(d.get("text"))
            toks_list.append(toks)
            for t in set(toks):
                df[t] += 1
        # smoothed idf
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self._vecs = [self._vector(toks) for toks in toks_list]
        return n

    def _vector(self, toks: list[str]) -> dict[str, float]:
        if not toks:
            return {}
        tf = Counter(toks)
        ln = len(toks)
        return {t: (f / ln) * self._idf.get(t, 0.0) for t, f in tf.items()}

    def _query_vector(self, query: Any) -> dict[str, float]:
        toks = tokenize(query)
        if not toks:
            return {}
        tf = Counter(toks)
        ln = len(toks)
        # query terms unseen at index time get a default idf so a query-only term still contributes 0
        return {t: (f / ln) * self._idf.get(t, 0.0) for t, f in tf.items()}

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        common = a.keys() & b.keys()
        if not common:
            return 0.0
        dot = sum(a[t] * b[t] for t in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    def retrieve(self, query: Any, k: int = 4, min_score: float = 0.0) -> list[dict[str, Any]]:
        q = self._query_vector(query)
        if not q:
            return []
        scored = []
        for i, vec in enumerate(self._vecs):
            s = self._cosine(q, vec)
            if s > min_score:
                scored.append((s, self._docs[i]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": round(s, 4), **d} for s, d in scored[:max(0, k)]]


def run_retrieval_selftest() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok = lambda name, passed, detail=None: checks.append({"name": name, "pass": bool(passed), "detail": detail})

    r = TfidfRetriever()
    docs = [
        {"id": "1", "text": "The Split faction declared war on the Argon Federation last week."},
        {"id": "2", "text": "Teladi trade stations sell energy cells and hull parts cheaply."},
        {"id": "3", "text": "The Xenon raid border sectors with drones and capital ships."},
        {"id": "4", "text": "Boron diplomats prefer peace and avoid open conflict."},
    ]
    indexed = r.index(docs)
    ok("indexed_all", indexed == 4, indexed)

    top = r.retrieve("are we at war with the split?", k=2)
    ok("relevant_ranks_first", bool(top) and top[0]["id"] == "1", top)
    ok("returns_topk", len(top) == 2, len(top))

    trade = r.retrieve("where can I buy energy cells?", k=1)
    ok("trade_query_finds_teladi", bool(trade) and trade[0]["id"] == "2", trade)

    empty = TfidfRetriever()
    empty.index([])
    ok("empty_index_safe", empty.retrieve("anything") == [], None)

    irrelevant = r.retrieve("zzzqqq totally unrelated gibberish", k=4, min_score=0.01)
    ok("irrelevant_filtered_by_min_score", irrelevant == [], irrelevant)

    passed = sum(1 for c in checks if c["pass"])
    total = len(checks)
    return {"allPassed": passed == total, "pass": passed == total, "passed": passed, "total": total, "checks": checks}


# --- Optional semantic embedder (model2vec) -------------------------------------------------------
# Drop-in upgrade from lexical TF-IDF to semantic vectors. model2vec is a tiny STATIC embedder (no
# torch). If it is not installed, everything falls back to TfidfRetriever and the bridge runs exactly
# as before. To enable: `pip install model2vec` into the bridge's Python env, then restart the bridge.
_EMBED_MODEL_NAME = "minishlab/potion-base-8M"
_embed_model: Any = None
_embed_tried = False


def _m2v_importable() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("model2vec") is not None
    except Exception:
        return False


def _get_embedder() -> Any:
    """Lazily load the static embedding model once (downloads ~tens of MB on first use). Returns the
    model, or None if model2vec is unavailable / load fails (→ caller falls back to TF-IDF)."""
    global _embed_model, _embed_tried
    if _embed_tried:
        return _embed_model
    _embed_tried = True
    try:
        from model2vec import StaticModel  # type: ignore
        _embed_model = StaticModel.from_pretrained(_EMBED_MODEL_NAME)
    except Exception:
        _embed_model = None
    return _embed_model


class EmbeddingRetriever:
    """Semantic retrieval via model2vec static embeddings — same index/retrieve interface as
    TfidfRetriever. Degrades to empty results (never throws) if the embedder is unavailable."""

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []
        self._mat: Any = None

    def index(self, docs: list[dict[str, Any]]) -> int:
        self._docs = [d for d in (docs or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
        self._mat = None
        m = _get_embedder()
        if m is None or not self._docs:
            return len(self._docs)
        try:
            self._mat = m.encode([str(d.get("text") or "") for d in self._docs])
        except Exception:
            self._mat = None
        return len(self._docs)

    def retrieve(self, query: Any, k: int = 4, min_score: float = 0.0) -> list[dict[str, Any]]:
        m = _get_embedder()
        if m is None or self._mat is None:
            return []
        try:
            import numpy as np
            q = m.encode([str(query)])[0]
            qn = float(np.linalg.norm(q)) or 1.0
            sims = []
            for i in range(len(self._docs)):
                v = self._mat[i]
                vn = float(np.linalg.norm(v)) or 1.0
                sims.append((float(np.dot(q, v)) / (qn * vn), self._docs[i]))
            sims = [(s, d) for s, d in sims if s > min_score]
            sims.sort(key=lambda x: x[0], reverse=True)
            return [{"score": round(s, 4), **d} for s, d in sims[:max(0, k)]]
        except Exception:
            return []


def make_retriever() -> Any:
    """Best available retriever: semantic (model2vec) if installed, else lexical TF-IDF."""
    return EmbeddingRetriever() if _m2v_importable() else TfidfRetriever()


def retriever_mode() -> str:
    """Cheap check (import only, no model load) for status/logging."""
    return "embedding(model2vec)" if _m2v_importable() else "lexical(tfidf)"
