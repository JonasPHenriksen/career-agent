"""Retrieval: find the facts, passages and past letters that matter for a given job or claim.

Hybrid search, all on this Mac:
  * keywords: BM25 over Danish/English word stems (exact tools, acronyms, employers)
  * meaning: embeddings from a local Ollama model (bge-m3 by default: Danish and English match)
The two rankings are merged with reciprocal rank fusion. Without an embedding model it falls
back to keywords only, so nothing breaks if Ollama isn't running.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from collections import Counter, OrderedDict

import httpx2
import numpy as np

from . import db, settings

log = logging.getLogger("career_agent.retrieval")

CHUNK_TARGET = 1100       # characters per passage
CHUNK_HARD_MAX = 1800
EMBED_BATCH = 32
DEFAULT_EMBED_MODEL = "bge-m3"

_STOP = set("""
og i jeg det at en den til er som på de med han af for ikke der var mig sig men et har om vi min havde ham hun nu
over da fra du ud sin dem os op man hans hvor eller hvad skal selv her alle vil blev kunne ind når være dog noget
ville jo deres efter ned skulle denne end dette mit også under have dig anden hende mine alt meget sit sine vor mod
disse hvis din nogle hos blive mange ad bliver hendes været jer sådan samt bl.a hvilket hvor både
the a an and or of to in for on with at by from as is are was were be been being this that these those it its we you
your our their they he she his her them us me my not no but if then than so such into over under about after before
can could will would shall should may might must do does did have has had which who whom what when where why how
all any each both few more most other some own same very just also only via per etc
""".split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tokens(text: str | None) -> list[str]:
    """Lower-cased word stems. Truncating long words to 7 letters is a crude but robust
    stemmer for Danish and English (udvikler/udviklere/udviklerne → udvikle)."""
    out = []
    for w in _WORD.findall((text or "").lower()):
        if len(w) < 2 or w in _STOP:
            continue
        out.append(w[:7])
    return out


# ---------------------------------------------------------------------------------------
# Passages (chunks)
# ---------------------------------------------------------------------------------------

def split_chunks(text: str, target: int = CHUNK_TARGET, hard_max: int = CHUNK_HARD_MAX) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= hard_max:
            pieces.append(p)
            continue
        buf = ""
        for s in re.split(r"(?<=[.!?])\s+|\n", p):
            s = s.strip()
            while len(s) > hard_max:  # no sentence breaks at all: cut hard
                pieces.append(s[:hard_max])
                s = s[hard_max:]
            if buf and len(buf) + len(s) + 1 > target:
                pieces.append(buf)
                buf = s
            else:
                buf = f"{buf} {s}".strip()
        if buf:
            pieces.append(buf)
    chunks, buf = [], ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 2 > target:
            chunks.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(buf)
    return chunks


def index_document(doc_id: int) -> int:
    """(Re)split a document into passages. Cheap: embeddings are added later in the background."""
    doc = db.q1("SELECT text FROM documents WHERE id = ?", (doc_id,))
    db.ex("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    if not doc:
        return 0
    parts = split_chunks(doc["text"])
    with db.transaction() as conn:
        conn.executemany("INSERT INTO chunks (document_id, idx, text) VALUES (?, ?, ?)",
                         [(doc_id, n, t) for n, t in enumerate(parts)])
    return len(parts)


def backfill_chunks() -> int:
    """Index documents imported before retrieval existed."""
    rows = db.q("SELECT id FROM documents d WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)")
    for r in rows:
        index_document(r["id"])
    return len(rows)


# ---------------------------------------------------------------------------------------
# Embeddings (local, via Ollama)
# ---------------------------------------------------------------------------------------

_avail_lock = threading.Lock()
_avail: dict = {"at": 0.0, "ok": False, "key": None}


def embed_model() -> str:
    return (settings.get("llm").get("embed_model") or DEFAULT_EMBED_MODEL).strip()


def _ollama_url() -> str:
    return (settings.get("llm").get("ollama_url") or "http://localhost:11434").rstrip("/")


def embeddings_available(force: bool = False) -> bool:
    model, url = embed_model(), _ollama_url()
    if not model:
        return False
    with _avail_lock:
        if not force and _avail["key"] == (model, url) and time.monotonic() - _avail["at"] < 60:
            return _avail["ok"]
    try:
        names = [m.get("name", "") for m in httpx2.get(f"{url}/api/tags", timeout=2).json().get("models", [])]
        ok = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
    except (httpx2.HTTPError, ValueError):
        ok = False
    with _avail_lock:
        _avail.update(at=time.monotonic(), ok=ok, key=(model, url))
    return ok


def _embed(texts: list[str]) -> np.ndarray:
    r = httpx2.post(f"{_ollama_url()}/api/embed",
                    json={"model": embed_model(), "input": texts, "truncate": True, "keep_alive": "30m"},
                    timeout=httpx2.Timeout(300.0, connect=5.0))
    r.raise_for_status()
    arr = np.asarray(r.json()["embeddings"], dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def ensure_embeddings(kind: str, items: list[tuple[int, str]]) -> dict[int, np.ndarray]:
    """Vectors for (id, text) items, embedding anything new or changed. {} if no embedding model."""
    if not items or not embeddings_available():
        return {}
    model = embed_model()
    ids = [i for i, _ in items]
    existing: dict[int, tuple[str, bytes]] = {}
    for start in range(0, len(ids), 900):
        part = ids[start:start + 900]
        marks = ",".join("?" for _ in part)
        for r in db.q(f"SELECT ref_id, hash, vec FROM embeddings WHERE kind = ? AND model = ? AND ref_id IN ({marks})",
                      [kind, model, *part]):
            existing[r["ref_id"]] = (r["hash"], r["vec"])
    out: dict[int, np.ndarray] = {}
    need = []
    for i, text in items:
        h = _hash(text)
        if i in existing and existing[i][0] == h:
            out[i] = np.frombuffer(existing[i][1], dtype=np.float32)
        else:
            need.append((i, text, h))
    try:
        for start in range(0, len(need), EMBED_BATCH):
            batch = need[start:start + EMBED_BATCH]
            vecs = _embed([t for _, t, _ in batch])
            with db.transaction() as conn:
                conn.executemany(
                    "INSERT INTO embeddings (kind, ref_id, model, hash, vec) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(kind, ref_id) DO UPDATE SET model = excluded.model, hash = excluded.hash, vec = excluded.vec",
                    [(kind, i, model, h, v.tobytes()) for (i, _, h), v in zip(batch, vecs)])
            for (i, _, _), v in zip(batch, vecs):
                out[i] = v
    except (httpx2.HTTPError, KeyError, ValueError) as e:
        log.warning("embedding failed, using keyword search only: %s", e)
    return out


_query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_query_lock = threading.Lock()


def embed_query(text: str) -> np.ndarray | None:
    if not text.strip() or not embeddings_available():
        return None
    key = _hash(embed_model() + "\x00" + text)
    with _query_lock:
        if key in _query_cache:
            _query_cache.move_to_end(key)
            return _query_cache[key]
    try:
        vec = _embed([text[:12000]])[0]
    except (httpx2.HTTPError, KeyError, ValueError, IndexError) as e:
        log.warning("query embedding failed: %s", e)
        return None
    with _query_lock:
        _query_cache[key] = vec
        while len(_query_cache) > 64:
            _query_cache.popitem(last=False)
    return vec


def fact_text(f: dict) -> str:
    return " ".join(x for x in (f.get("category"), f.get("title"), f.get("organization"), f.get("detail"),
                                f.get("tags")) if x)


def chunk_text(c: dict) -> str:
    return f"{c.get('title') or ''}\n{c['text']}".strip()


def embed_pending(limit: int = 5000) -> dict:
    """Background task: embed every passage and fact that doesn't have a current vector yet."""
    if not embeddings_available(force=True):
        return {"skipped": f"embedding model '{embed_model()}' isn't available in Ollama"}
    model = embed_model()
    chunks = db.q("SELECT c.id, c.text, d.title FROM chunks c JOIN documents d ON d.id = c.document_id "
                  "LEFT JOIN embeddings e ON e.kind = 'chunk' AND e.ref_id = c.id AND e.model = ? "
                  "WHERE e.ref_id IS NULL LIMIT ?", (model, limit))
    facts = db.q("SELECT * FROM facts WHERE hidden = 0")
    n_chunks = len(ensure_embeddings("chunk", [(c["id"], chunk_text(c)) for c in chunks]))
    n_facts = len(ensure_embeddings("fact", [(f["id"], fact_text(f)) for f in facts]))
    db.ex("DELETE FROM embeddings WHERE kind = 'chunk' AND ref_id NOT IN (SELECT id FROM chunks)")
    db.ex("DELETE FROM embeddings WHERE kind = 'fact' AND ref_id NOT IN (SELECT id FROM facts)")
    return {"passages": n_chunks, "facts": n_facts}


def status() -> dict:
    model = embed_model()
    total = db.scalar("SELECT COUNT(*) FROM chunks") or 0
    done = db.scalar("SELECT COUNT(*) FROM chunks c JOIN embeddings e ON e.kind = 'chunk' AND e.ref_id = c.id "
                     "AND e.model = ?", (model,)) or 0
    return {"model": model, "available": embeddings_available(), "passages": total, "embedded": done}


# ---------------------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------------------

class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.n = len(docs)
        self.lens = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg = float(self.lens.mean()) if self.n else 1.0
        self.k1, self.b = k1, b
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            for t, f in Counter(d).items():
                self.postings.setdefault(t, []).append((i, f))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        s = np.zeros(self.n, dtype=np.float32)
        norm = self.k1 * (1 - self.b + self.b * self.lens / (self.avg or 1.0))
        for t in set(query):
            for i, f in self.postings.get(t, ()):
                s[i] += self.idf[t] * f * (self.k1 + 1) / (f + norm[i])
        return s


def _fuse(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for r, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + r + 1)
    return scores


def _hybrid(query: str, ids: list[int], texts: list[str], vectors: dict[int, np.ndarray]) -> list[int]:
    """Order ids by relevance to the query; ids with no signal keep their original order at the end."""
    if not ids:
        return []
    bm = BM25([tokens(t) for t in texts]).scores(tokens(query))
    kw_rank = [ids[i] for i in np.argsort(-bm) if bm[i] > 0]
    rankings = [kw_rank]
    qv = embed_query(query) if vectors else None
    if qv is not None:
        have = [i for i in ids if i in vectors]
        if have:
            sims = np.stack([vectors[i] for i in have]) @ qv
            rankings.append([have[j] for j in np.argsort(-sims)])
    fused = _fuse(rankings)
    position = {i: n for n, i in enumerate(ids)}
    return sorted(ids, key=lambda i: (-fused.get(i, 0.0), position[i]))


def rank_items(kind: str, query: str, items: list[tuple[int, str]]) -> list[int]:
    """Order (id, text) items by relevance to the query; vectors are cached under `kind`."""
    vectors = ensure_embeddings(kind, items)
    return _hybrid(query, [i for i, _ in items], [t for _, t in items], vectors)


def rank_facts(query: str, facts: list[dict]) -> list[int]:
    vectors = ensure_embeddings("fact", [(f["id"], fact_text(f)) for f in facts])
    return _hybrid(query, [f["id"] for f in facts], [fact_text(f) for f in facts], vectors)


def search_chunks(query: str, k: int = 4, exclude_kinds: tuple[str, ...] = ("job_ad",)) -> list[dict]:
    """Best matching passages from your own documents."""
    marks = ",".join("?" for _ in exclude_kinds) or "''"
    rows = db.q(f"SELECT c.id, c.text, c.document_id, d.title, d.kind FROM chunks c JOIN documents d ON d.id = c.document_id "
                f"WHERE d.kind NOT IN ({marks})", exclude_kinds)
    if not rows:
        return []
    vectors = ensure_embeddings("chunk", [(r["id"], chunk_text(r)) for r in rows])
    order = _hybrid(query, [r["id"] for r in rows], [chunk_text(r) for r in rows], vectors)
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in order[:k]]


def rank_documents(query: str, docs: list[dict]) -> list[int]:
    """Order whole documents by how well they match the query (best passage counts)."""
    if len(docs) < 2:
        return [d["id"] for d in docs]
    doc_ids = [d["id"] for d in docs]
    marks = ",".join("?" for _ in doc_ids)
    chunks = db.q(f"SELECT c.id, c.text, c.document_id, d.title FROM chunks c JOIN documents d ON d.id = c.document_id "
                  f"WHERE c.document_id IN ({marks})", doc_ids)
    rankings = []
    bm = BM25([tokens(d.get("text") or "") for d in docs]).scores(tokens(query))
    rankings.append([doc_ids[i] for i in np.argsort(-bm) if bm[i] > 0])
    vectors = ensure_embeddings("chunk", [(c["id"], chunk_text(c)) for c in chunks])
    qv = embed_query(query) if vectors else None
    if qv is not None:
        best: dict[int, float] = {}
        for c in chunks:
            if c["id"] in vectors:
                best[c["document_id"]] = max(best.get(c["document_id"], -1.0), float(vectors[c["id"]] @ qv))
        rankings.append(sorted(best, key=lambda i: -best[i]))
    fused = _fuse(rankings)
    position = {i: n for n, i in enumerate(doc_ids)}
    return sorted(doc_ids, key=lambda i: (-fused.get(i, 0.0), position[i]))


def job_query(job: dict) -> str:
    d = job.get("d") or db.jload(job.get("details"), {}) or {}
    parts = [job.get("title"), job.get("summary"), " ".join(d.get("requirements") or []),
             " ".join(d.get("nice_to_have") or []), " ".join(d.get("responsibilities") or []),
             " ".join(d.get("keywords") or [])]
    q = "\n".join(p for p in parts if p)
    return q or (job.get("description") or "")[:4000]
