"""Research agent: finds relevant past-proposal material for this client by calling search tools.

The model decides which industries are comparable and what to search for (tool calling). Every result the tools
return is collected here, so the pack the Writer receives is built from real search results, not from the model's
summary. Code then checks that each section the Writer must produce has reference material, and runs a search
itself for any it missed (recorded in `auto_filled`, so it shows in the trace).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from app import llm
from app.db import repo
from app.logs import log
from app.rag import ingest, store
from app.schemas import REQUIRED_SECTION_TYPES, ClientProfile, ResearchPack, RetrievedChunk

PROMPT = (Path(__file__).parent / "prompts" / "research.md").read_text(encoding="utf-8")
SEARCHABLE = ["executive_summary", "problem", "proposed_solution", "integrations", "deliverables", "timeline",
              "budget", "assumptions", "risks", "team", "next_steps"]
MUST_COVER = [t for t in REQUIRED_SECTION_TYPES if t != "cover"]  # the cover is built from the profile, not references


def profile_brief(p: ClientProfile) -> str:
    """Short plain-text description of the client, used as the base for search queries and prompts."""
    def v(f):
        return f.value or "(not stated)"
    lines = [f"Company: {v(p.company)}", f"Industry: {v(p.industry)}", f"Problem: {v(p.problem)}", f"Goals: {v(p.goals)}",
             f"Budget: {v(p.budget)} [{p.budget.status.value}]", f"Timeline: {v(p.timeline)} [{p.timeline.status.value}]"]
    if p.integrations:
        lines.append("Integrations: " + "; ".join(f"{f.value} [{f.status.value}]" for f in p.integrations if f.value))
    if p.constraints:
        lines.append("Constraints: " + "; ".join(f.value for f in p.constraints if f.value))
    if p.out_of_scope:
        lines.append("Deprioritised: " + "; ".join(f.value for f in p.out_of_scope if f.value))
    return "\n".join(lines)


class _Collector:
    def __init__(self) -> None:
        self.by_section: dict[str, dict[str, RetrievedChunk]] = defaultdict(dict)
        self.industries: set[str] = set()
        self.used_fallback = False
        self.outlines: dict[str, list[str]] = {}

    def add(self, section: str, chunks: list[RetrievedChunk]) -> None:
        for c in chunks:
            self.by_section[section].setdefault(c.chunk_id, c)


def _outline(doc_id: str) -> list[str]:
    rows = store.collection(store.PROPOSALS).get(where={"doc_id": doc_id}, include=["metadatas"])
    seen: dict[int, str] = {}
    for m in rows["metadatas"]:
        types = [k[2:] for k, val in m.items() if k.startswith("t_") and val is True]
        seen.setdefault(m["section_order"], f"{m['section_title']} [{', '.join(types)}]")
    return [seen[k] for k in sorted(seen)]


def run_research(profile: ClientProfile, session_id: int | None = None, per_section: int = 3) -> ResearchPack:
    col = _Collector()
    brief = profile_brief(profile)

    # ---- tools the model can call ----
    def list_past_proposals() -> list[dict]:
        """List every past proposal: doc_id, industry, client name and the client's core problem."""
        return ingest.list_industries()

    def search_past_proposals(query: str, section_type: str = "", industries: str = "") -> list[dict]:
        """Search past proposals by meaning.

        Args:
            query: plain-language description of what you are looking for, based on this client's situation.
            section_type: optional part of a proposal to search within, one of: executive_summary, problem,
                proposed_solution, integrations, deliverables, timeline, budget, assumptions, risks, team, next_steps.
            industries: optional comma-separated industries to restrict to, e.g. "retail,logistics".
        """
        sec = section_type.strip() or None
        if sec and sec not in SEARCHABLE:
            return [{"error": f"unknown section_type '{sec}'. Use one of: {', '.join(SEARCHABLE)}"}]
        inds = [i.strip().lower() for i in industries.split(",") if i.strip()] or None
        chunks, fb = store.search_proposals(query, section_type=sec, industries=inds, k=per_section)
        col.add(sec or "general", chunks)
        col.industries.update(inds or [])
        col.used_fallback |= fb
        return [{"chunk_id": c.chunk_id, "doc_id": c.doc_id, "industry": c.industry, "section": c.section_title,
                 "score": c.score, "excerpt": c.text[:280]} for c in chunks]

    def get_proposal_outline(doc_id: str) -> list[str]:
        """Return a past proposal's section titles in order, each with the section types it covers."""
        out = _outline(doc_id)
        if out:
            col.outlines[doc_id] = out
        return out or [f"no proposal with doc_id '{doc_id}'"]

    prompt = f"New client:\n{brief}\n\nFind the most useful past-proposal material for this client's proposal."
    rationale, model, calls, err = "", None, [], None
    try:
        r = llm.run_with_tools(prompt, [list_past_proposals, search_past_proposals, get_proposal_outline],
                               system=PROMPT, tier="strong", max_calls=16)
        rationale, model, calls = r.text, r.model, r.tool_calls
    except llm.LLMError as e:
        err = str(e)  # the agent failed; code below still builds a usable pack from its own searches
        log.warning("research.agent_failed", error=err[:160])

    # ---- guarantee coverage for every section the Writer must produce ----
    auto: list[str] = []
    inds = sorted(col.industries) or None
    for sec in MUST_COVER:
        if not col.by_section.get(sec):
            chunks, fb = store.search_proposals(f"{brief}\nSection needed: {sec.replace('_', ' ')}", section_type=sec,
                                                industries=inds, k=per_section)
            col.add(sec, chunks)
            col.used_fallback |= fb
            auto.append(sec)

    # outlines of the closest past proposals, if the agent did not fetch them
    if len(col.outlines) < 2:
        score: dict[str, float] = defaultdict(float)
        for chunks in col.by_section.values():
            for c in chunks.values():
                score[c.doc_id] += c.score
        for doc_id in sorted(score, key=lambda d: -score[d])[:3]:
            col.outlines.setdefault(doc_id, _outline(doc_id))

    pack = ResearchPack(
        query=brief, industries_searched=inds or ["all"], used_fallback=col.used_fallback,
        by_section={k: sorted(v.values(), key=lambda c: -c.score) for k, v in col.by_section.items()},
        reference_outlines=col.outlines, rationale=rationale, auto_filled=auto)
    repo.log_agent_run("research", session_id=session_id, model=model, tool_calls=calls, error=err,
                       output={"industries": pack.industries_searched, "sections": {k: len(v) for k, v in pack.by_section.items()},
                               "outlines": list(pack.reference_outlines), "auto_filled": auto, "fallback": pack.used_fallback})
    log.info("research.done", model=model, tool_calls=len(calls), industries=pack.industries_searched,
             sections=sorted(pack.by_section), auto_filled=auto)
    return pack
