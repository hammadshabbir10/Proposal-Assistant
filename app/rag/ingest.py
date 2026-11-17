"""Load the past proposals into the vector store at startup.

Section types are assigned by meaning, not by heading text: one fast LLM call per proposal reads each section and
says which types it covers (a single "TIMELINE AND COST" section covers both timeline and budget). The result is
cached by file hash, so restarts do not repeat the calls, and an edited proposal is re-indexed automatically.

usage: python -m app.rag.ingest [--force]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from app import llm
from app.config import get_settings
from app.logs import log, setup_logging
from app.rag import store
from app.rag.chunking import ParsedProposal, chunk_proposal, parse_proposal
from app.schemas import SectionType

SECTION_TYPES = list(SectionType.__args__)  # type: ignore[attr-defined]


class SectionLabel(BaseModel):
    title: str
    types: list[SectionType] = Field(description="Every section type this section covers; usually one, sometimes two")


class ProposalMeta(BaseModel):
    industry: str = Field(description="One or two lowercase words for the client's industry, e.g. 'retail', 'logistics'")
    problem_summary: str = Field(description="One sentence: the client's core problem")
    budget_usd: int | None = Field(description="Total project cost in USD if stated, else null")
    duration_weeks: int | None = Field(description="Total project length in weeks if stated, else null")
    sections: list[SectionLabel]


_FALLBACK = {"summary": "executive_summary", "overview": "executive_summary", "solution": "proposed_solution",
             "build": "proposed_solution", "technical": "proposed_solution", "timeline": "timeline", "budget": "budget",
             "cost": "budget", "integration": "integrations", "deliverable": "deliverables", "assumption": "assumptions",
             "constraint": "assumptions", "risk": "risks", "team": "team", "next step": "next_steps"}


def _fallback_types(title: str) -> list[str]:
    """Used only if the LLM labelling fails, so startup never blocks on the model."""
    t = title.lower()
    found = [v for k, v in _FALLBACK.items() if k in t]
    return list(dict.fromkeys(found)) or ["other"]


def _label(p: ParsedProposal) -> ProposalMeta:
    listing = "\n\n".join(f"## {s.title}\n{' '.join(s.text.split()[:70])}" for s in p.sections)
    prompt = (f"Proposal for: {p.header.get('client', 'unknown')}\nTitle: {p.title}\n\n{listing}\n\n"
              f"For each section, list which of these types it covers, judging by its content, not its heading: "
              f"{', '.join(SECTION_TYPES)}. Also give the client's industry, core problem, total budget and duration.")
    r = llm.generate_structured(prompt, ProposalMeta, tier="fast",
                                system="You label sections of business proposals by what they contain. Be precise.")
    return r.parsed


def _meta_cache() -> tuple[Path, dict]:
    path = get_settings().chroma_path.parent / "proposal_meta.json"
    return path, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})


def ingest_proposals(force: bool = False) -> dict:
    s = get_settings()
    cache_path, cache = _meta_cache()
    col = store.collection(store.PROPOSALS)
    stats = {"loaded": 0, "skipped": 0, "chunks": 0}
    for path in sorted(Path(s.proposals_dir).glob("proposal_*.txt")):
        raw = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        doc_id = path.stem.removeprefix("proposal_")
        existing = col.get(where={"doc_id": doc_id}, limit=1, include=["metadatas"])
        if not force and existing["ids"] and existing["metadatas"][0].get("sha") == sha:
            stats["skipped"] += 1
            continue
        if existing["ids"]:
            col.delete(where={"doc_id": doc_id})

        parsed = parse_proposal(doc_id, raw)
        if sha in cache:
            meta = ProposalMeta.model_validate(cache[sha])
        else:
            try:
                meta = _label(parsed)
                cache[sha] = meta.model_dump()
            except llm.LLMError as e:
                log.warning("ingest.label_fallback", doc=doc_id, error=str(e)[:120])
                meta = ProposalMeta(industry=doc_id.rstrip("0123456789"), problem_summary="", budget_usd=None,
                                    duration_weeks=None,
                                    sections=[SectionLabel(title=x.title, types=_fallback_types(x.title)) for x in parsed.sections])
        labels = {lbl.title.lower(): lbl.types for lbl in meta.sections}
        industry = doc_id.rstrip("0123456789")  # the file name is the authoritative industry label
        client = parsed.header.get("client", "")

        chunks = chunk_proposal(parsed)
        for c in chunks:
            types = labels.get(c.metadata["section_title"].lower()) or _fallback_types(c.metadata["section_title"])
            c.metadata.update({
                "client": client, "industry": industry, "section_type": types[0], "sha": sha,
                "budget_usd": meta.budget_usd or -1, "duration_weeks": meta.duration_weeks or -1,
                "problem_summary": meta.problem_summary,
                **{f"t_{t}": (t in types) for t in SECTION_TYPES},
            })
        # embed with a short context header so a chunk carries who it was for and which section it is
        embed_texts = [f"{client} ({industry}) — {c.metadata['section_title']}\n{c.text}" for c in chunks]
        store.add_chunks(store.PROPOSALS, chunks, embed_texts)
        stats["loaded"] += 1
        stats["chunks"] += len(chunks)
        log.info("ingest.proposal", doc=doc_id, client=client, chunks=len(chunks),
                 sections={x.title: x.types for x in meta.sections})
    cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    stats["total_chunks"] = col.count()
    return stats


def list_industries() -> list[dict]:
    """One entry per past proposal: industry, client and its core problem. Used by the Research agent."""
    col = store.collection(store.PROPOSALS)
    rows = col.get(include=["metadatas"])
    seen: dict[str, dict] = {}
    for m in rows["metadatas"]:
        seen.setdefault(m["doc_id"], {"doc_id": m["doc_id"], "industry": m["industry"], "client": m["client"],
                                      "problem": m.get("problem_summary", "")})
    return list(seen.values())


if __name__ == "__main__":
    setup_logging(get_settings().log_level)
    print(ingest_proposals(force="--force" in sys.argv))
