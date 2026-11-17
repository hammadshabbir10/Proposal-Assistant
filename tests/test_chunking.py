"""Chunking rules that retrieval quality depends on. Runs on the real data files, no network."""
from pathlib import Path

from app.rag.chunking import chunk_proposal, chunk_transcript, parse_proposal, parse_transcript

DATA = Path(__file__).resolve().parents[1] / "data"
PROPOSALS = sorted((DATA / "proposals").glob("proposal_*.txt"))


def _load(p: Path):
    return parse_proposal(p.stem.removeprefix("proposal_"), p.read_text(encoding="utf-8"))


def test_every_proposal_parses_into_sections_with_a_client():
    assert len(PROPOSALS) == 10
    for p in PROPOSALS:
        parsed = _load(p)
        assert len(parsed.sections) >= 4, p.name
        assert parsed.header.get("client"), f"{p.name}: client not found in header"


def test_retail_uses_its_own_headings_and_still_parses():
    parsed = _load(DATA / "proposals" / "proposal_retail.txt")
    titles = [s.title for s in parsed.sections]
    assert "Overview" in titles and "Timeline And Cost" in titles


def test_proposal_chunks_never_cross_a_section():
    for p in PROPOSALS:
        parsed = _load(p)
        by_title = {s.title: s.text for s in parsed.sections}
        for c in chunk_proposal(parsed):
            first_words = " ".join(c.text.split()[:8])
            assert first_words in " ".join(by_title[c.metadata["section_title"]].split()), (p.name, c.chunk_id)


def test_transcript_turns_are_kept_whole():
    header, turns = parse_transcript((DATA / "client_transcript.txt").read_text(encoding="utf-8"))
    assert header["company"] == "Brightline Retail Group"
    assert len(turns) >= 30
    speakers = {t.speaker for t in turns}
    assert {"Sarah", "Marcus", "Jenny"} <= speakers
    chunks = chunk_transcript(1, 1, turns)
    joined = "\n".join(c.text for c in chunks)
    for t in turns:  # every turn appears complete in at least one chunk
        assert f"{t.speaker}: {t.text}" in joined
    # consecutive chunks overlap by one turn so context carries across
    for a, b in zip(chunks, chunks[1:]):
        assert b.metadata["turn_start"] <= a.metadata["turn_end"]
