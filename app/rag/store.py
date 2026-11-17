"""Vector store (Chroma, saved to disk) with two collections, one per RAG source:
- proposals: past proposals, loaded at startup, filterable by industry and section type
- transcripts: each session's uploaded transcripts, always filtered to that session

Retrieval is meaning-based: queries and chunks are embedded with the same model and compared by cosine similarity.
Filters narrow the candidates; MMR then picks a relevant but non-repetitive set.
"""
from __future__ import annotations

from functools import lru_cache

import chromadb
import numpy as np

from app import llm
from app.config import get_settings
from app.logs import log
from app.rag.chunking import Chunk
from app.schemas import RetrievedChunk

PROPOSALS = "proposals"
TRANSCRIPTS = "transcripts"


@lru_cache
def _client() -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(get_settings().chroma_path))


def collection(name: str):
    return _client().get_or_create_collection(name, metadata={"hnsw:space": "cosine"}, embedding_function=None)


def add_chunks(name: str, chunks: list[Chunk], embed_texts: list[str] | None = None) -> None:
    """Store chunks. embed_texts lets the caller embed a richer string (e.g. with a context header) than it stores."""
    if not chunks:
        return
    vectors = llm.embed(embed_texts or [c.text for c in chunks], task="RETRIEVAL_DOCUMENT")
    collection(name).upsert(ids=[c.chunk_id for c in chunks], documents=[c.text for c in chunks],
                            metadatas=[c.metadata for c in chunks], embeddings=vectors)


def _where(*conds: dict | None) -> dict | None:
    conds = [c for c in conds if c]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


def _mmr(query: np.ndarray, cands: np.ndarray, k: int, lam: float = 0.7) -> list[int]:
    """Maximal marginal relevance: balance similarity to the query against similarity to what is already picked."""
    if len(cands) == 0:
        return []
    q = query / np.linalg.norm(query)
    c = cands / np.linalg.norm(cands, axis=1, keepdims=True)
    rel = c @ q
    picked: list[int] = [int(np.argmax(rel))]
    while len(picked) < min(k, len(c)):
        redundancy = (c @ c[picked].T).max(axis=1)
        score = lam * rel - (1 - lam) * redundancy
        score[picked] = -np.inf
        picked.append(int(np.argmax(score)))
    return picked


def _search(name: str, query: str, where: dict | None, k: int) -> list[tuple[str, str, dict, float]]:
    col = collection(name)
    if col.count() == 0:
        return []
    qv = np.array(llm.embed([query], task="RETRIEVAL_QUERY")[0])
    res = col.query(query_embeddings=[qv.tolist()], n_results=min(k * 3, col.count()), where=where,
                    include=["documents", "metadatas", "distances", "embeddings"])
    ids = res["ids"][0]
    if not ids:
        return []
    embs = np.array(res["embeddings"][0])
    order = _mmr(qv, embs, k)
    return [(ids[i], res["documents"][0][i], res["metadatas"][0][i], 1 - res["distances"][0][i]) for i in order]


def search_proposals(query: str, *, section_type: str | None = None, industries: list[str] | None = None,
                     k: int | None = None) -> tuple[list[RetrievedChunk], bool]:
    """Search past proposals. Returns (chunks, used_fallback).

    The industry filter is tried first. If it leaves too few good matches, the search is widened to every
    industry (keeping the section filter), because a close match in a neighbouring industry beats nothing.
    """
    s = get_settings()
    k = k or s.retrieval_k
    sec = {f"t_{section_type}": True} if section_type else None
    ind = {"industry": {"$in": industries}} if industries else None

    hits = _search(PROPOSALS, query, _where(ind, sec), k)
    good = [h for h in hits if h[3] >= s.retrieval_min_score]
    used_fallback = False
    if industries and len(good) < max(2, k // 2):
        used_fallback = True
        wider = _search(PROPOSALS, query, _where(sec), k)
        seen = {h[0] for h in good}
        good += [h for h in wider if h[3] >= s.retrieval_min_score and h[0] not in seen]
        good = sorted(good, key=lambda h: -h[3])[:k]
        log.info("retrieval.fallback", query=query[:80], industries=industries, section=section_type, found=len(good))
    return [RetrievedChunk(chunk_id=i, doc_id=m["doc_id"], industry=m["industry"], section_type=m["section_type"],
                           section_title=m["section_title"], text=d, score=round(sc, 3)) for i, d, m, sc in good], used_fallback


def search_transcript(session_id: int, query: str, k: int = 4) -> list[RetrievedChunk]:
    hits = _search(TRANSCRIPTS, query, {"session_id": session_id}, k)
    return [RetrievedChunk(chunk_id=i, doc_id=f"transcript-{m['transcript_id']}", industry="", section_type="transcript",
                           section_title=f"turns {m['turn_start']}-{m['turn_end']}", text=d, score=round(sc, 3))
            for i, d, m, sc in hits]


def add_transcript(session_id: int, transcript_id: int, raw_text: str) -> int:
    from app.rag.chunking import chunk_transcript, parse_transcript
    _, turns = parse_transcript(raw_text)
    chunks = chunk_transcript(transcript_id, session_id, turns)
    add_chunks(TRANSCRIPTS, chunks)
    return len(chunks)
