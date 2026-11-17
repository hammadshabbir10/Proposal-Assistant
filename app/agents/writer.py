"""Writer agent: plans the proposal structure, then writes each section in its own call.

Planning is a separate step so the structure is reasoned from the reference proposals and the client, not
hardcoded: the prompt only names the section *types* that must appear, and code checks them. Writing each section
separately means a revision can rewrite one section without touching the rest, and a failure loses one section,
not the whole document.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import llm
from app.agents.research import profile_brief
from app.db import repo
from app.docx.render import normalize_markdown
from app.logs import log
from app.schemas import REQUIRED_SECTION_TYPES, ClientProfile, Outline, OutlineSection, ResearchPack, SectionDraft

PLAN_PROMPT = (Path(__file__).parent / "prompts" / "writer_plan.md").read_text(encoding="utf-8")
SECTION_PROMPT = (Path(__file__).parent / "prompts" / "writer_section.md").read_text(encoding="utf-8")
PARALLEL = 4

# related reference material per section type, when the type itself has little in the pack
_RELATED = {"cover": ["executive_summary"], "problem": ["executive_summary"], "risks": ["assumptions"],
            "team": ["deliverables"], "deliverables": ["proposed_solution"], "other": ["proposed_solution"]}


def _profile_detail(profile: ClientProfile, fields: list[str]) -> str:
    """The profile fields a section needs, with status and quotes, so the writer can follow the honesty rules."""
    data = profile.model_dump(mode="json")
    keep = fields or list(data)
    lines = []
    for k in keep:
        if k not in data:
            continue
        lines.append(f"{k}: {data[k]}")
    return "\n".join(lines)


def _outline_text(pack: ResearchPack) -> str:
    return "\n\n".join(f"{doc}:\n" + "\n".join(f"  - {s}" for s in secs) for doc, secs in pack.reference_outlines.items())


def plan(profile: ClientProfile, pack: ResearchPack, session_id: int | None = None) -> Outline:
    prompt = (f"Client profile:\n{profile_brief(profile)}\n\nStakeholders: "
              f"{[(s.name, s.role) for s in profile.stakeholders]}\n\n"
              f"Closest past proposals and their structure:\n{_outline_text(pack)}\n\n"
              f"Research note:\n{pack.rationale or '(none)'}\n\nPlan this proposal.")
    r = llm.generate_structured(prompt, Outline, system=PLAN_PROMPT, tier="strong")
    outline: Outline = r.parsed
    missing = outline.missing_required()
    if missing:  # one chance to fix it with feedback, then fill the gap in code
        r = llm.generate_structured(prompt + f"\n\nYour previous outline lacked these required section types: {missing}. "
                                    "Revise the outline to include them.", Outline, system=PLAN_PROMPT, tier="strong")
        outline = r.parsed
        for t in outline.missing_required():
            outline.sections.append(OutlineSection(key=t, title=t.replace("_", " ").title(), section_type=t,
                                                   purpose=f"Required {t.replace('_', ' ')} section"))
    # unique keys, cover first
    seen, secs = set(), []
    for s in outline.sections:
        key = s.key if s.key not in seen else f"{s.key}_{len(seen)}"
        seen.add(key)
        secs.append(s.model_copy(update={"key": key}))
    secs.sort(key=lambda s: s.section_type != "cover")
    outline = outline.model_copy(update={"sections": secs})
    repo.log_agent_run("writer_plan", session_id=session_id, model=r.model, output=outline.model_dump(mode="json"),
                       tokens_in=r.tokens_in, tokens_out=r.tokens_out, latency_ms=r.latency_ms)
    return outline


def _references(pack: ResearchPack, section_type: str, limit: int = 3):
    chunks = list(pack.by_section.get(section_type, []))
    for rel in _RELATED.get(section_type, []):
        chunks += pack.by_section.get(rel, [])
    if not chunks:
        chunks = pack.by_section.get("general", [])
    return chunks[:limit]


def write_section(sec: OutlineSection, outline: Outline, profile: ClientProfile, pack: ResearchPack,
                  instruction: str | None = None, current: str | None = None,
                  session_id: int | None = None, agent: str = "writer_section") -> SectionDraft:
    refs = _references(pack, sec.section_type)
    ref_text = "\n\n".join(f"[{c.doc_id} / {c.section_title}]\n{c.text}" for c in refs) or "(no close reference)"
    others = ", ".join(f"{s.title}" for s in outline.sections if s.key != sec.key)
    prompt = (f"Proposal: {outline.title}\nOther sections in this proposal (do not repeat their content): {others}\n\n"
              f"Section to write: {sec.title} (type: {sec.section_type})\nPurpose: {sec.purpose}\n\n"
              f"Client profile (relevant fields, with status and quotes):\n{_profile_detail(profile, sec.profile_fields)}\n\n"
              f"Client summary:\n{profile_brief(profile)}\n\nExcerpts from similar past proposals:\n{ref_text}")
    if current is not None:
        prompt += f"\n\nCurrent text of this section:\n{current}\n\nRevise it as follows: {instruction}"
    r = llm.generate_structured(prompt, SectionDraft, system=SECTION_PROMPT, tier="strong", temperature=0.4)
    d: SectionDraft = r.parsed
    draft = SectionDraft(key=sec.key, title=d.title or sec.title, content_md=normalize_markdown(d.content_md),
                         sources=[c.chunk_id for c in refs] + ["transcript"])  # sources come from code, not the model
    repo.log_agent_run(agent, session_id=session_id, model=r.model, output={"key": sec.key, "chars": len(draft.content_md)},
                       tokens_in=r.tokens_in, tokens_out=r.tokens_out, latency_ms=r.latency_ms)
    return draft


def write_all(outline: Outline, profile: ClientProfile, pack: ResearchPack, session_id: int | None = None) -> list[SectionDraft]:
    def one(sec: OutlineSection) -> SectionDraft:
        try:
            return write_section(sec, outline, profile, pack, session_id=session_id)
        except llm.LLMError as e:  # one failed section must not lose the whole document
            log.warning("writer.section_failed", key=sec.key, error=str(e)[:120])
            return SectionDraft(key=sec.key, title=sec.title,
                                content_md="[TBD: this section could not be generated. Ask me to regenerate it.]")
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        return list(ex.map(one, outline.sections))


def generate(profile: ClientProfile, pack: ResearchPack, session_id: int | None = None) -> tuple[Outline, list[SectionDraft]]:
    outline = plan(profile, pack, session_id)
    drafts = write_all(outline, profile, pack, session_id)
    log.info("writer.done", sections=[s.key for s in outline.sections])
    return outline, drafts
