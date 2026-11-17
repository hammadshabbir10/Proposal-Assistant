"""Retrieval behaviour against the real vector store and Gemini embeddings.

These are integration tests: they need GEMINI_API_KEY and the proposals loaded (python -m app.rag.ingest).
Run only these with:  pytest -m integration
"""
import re
from pathlib import Path

import pytest

from app.rag import ingest, store

pytestmark = pytest.mark.integration

STOP = set("a an the and or of to in on for with is are was be it that this our we they their do does not no at by as from "
           "each other who what which where when how".split())


def _content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP and len(w) > 2}


@pytest.fixture(scope="module", autouse=True)
def loaded():
    stats = ingest.ingest_proposals()
    assert stats["total_chunks"] >= 60


def test_search_finds_meaning_without_shared_words():
    # None of these words appear in the target chunks, which talk about "real-time visibility" across "hubs"
    query = "colleagues ring one another to learn what goods sit at other buildings"
    hits, _ = store.search_proposals(query, k=4)
    assert {h.doc_id for h in hits[:3]} & {"logistics", "retail", "retail2", "distribution"}
    top = hits[0]
    assert len(_content_words(query) & _content_words(top.text)) <= 1, "top hit should match on meaning, not keywords"


def test_industry_filter_excludes_other_industries():
    hits, fallback = store.search_proposals("inventory across warehouses", industries=["retail", "logistics", "distribution"], k=6)
    assert not fallback
    assert {h.industry for h in hits} <= {"retail", "logistics", "distribution"}


def test_section_filter_uses_meaning_labels():
    # retail's single "Timeline And Cost" section must be reachable as a budget section
    hits, _ = store.search_proposals("total project cost breakdown", section_type="budget", industries=["retail"], k=4)
    assert any(h.doc_id == "retail" and h.section_title == "Timeline And Cost" for h in hits)


def test_sparse_filter_falls_back_to_all_industries():
    hits, fallback = store.search_proposals("multi-site inventory sync", industries=["healthcare"],
                                            section_type="proposed_solution", k=4)
    assert fallback and len(hits) >= 2


def test_ingest_is_idempotent():
    assert ingest.ingest_proposals()["skipped"] == 10


def test_transcript_search_is_scoped_to_its_session():
    raw = (Path(__file__).resolve().parents[1] / "data" / "client_transcript.txt").read_text(encoding="utf-8")
    sid = 990001
    try:
        store.add_transcript(sid, 1, raw)
        hits = store.search_transcript(sid, "when does the client need the system working?", k=3)
        assert hits and any(("Q3" in h.text or "Q4" in h.text) for h in hits)
        assert store.search_transcript(sid + 1, "deadline", k=3) == []
    finally:
        store.collection(store.TRANSCRIPTS).delete(where={"session_id": sid})
