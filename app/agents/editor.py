"""Editor agent: applies a change request to the proposal by rewriting only the sections it concerns.

Step 1 (plan): one structured call picks the target section keys from the current outline and turns the request
into a precise instruction per section; if the request is too vague it asks the consultant instead of guessing.
Step 2 (rewrite): each target section is rewritten with the Writer's section routine, given its current text and
the instruction. Every other section is copied unchanged into the new version by the repository, so an edit cannot
alter text it was not asked to touch.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app import llm
from app.agents import writer
from app.db import repo
from app.schemas import Outline, ResearchPack


class SectionChange(BaseModel):
    key: str = Field(description="Key of an existing section")
    instruction: str = Field(description="Precise instruction for rewriting this section")


class EditPlan(BaseModel):
    changes: list[SectionChange] = Field(default_factory=list)
    needs_clarification: bool = False
    question: str | None = Field(default=None, description="What to ask the consultant if the request is unclear")
    change_note: str = Field(description="One short line describing the change, e.g. 'Timeline expanded with phases'")


SYSTEM = """You plan edits to a client proposal. Given the consultant's request and the list of sections, choose the
smallest set of sections that must change (usually one) and write a precise instruction for each. Only use section
keys from the list. If the request could mean several different sections and the hint does not settle it, set
needs_clarification and ask a short question naming the likely sections."""


@dataclass
class EditResult:
    proposal: object | None
    reply: str
    changed_keys: list[str]


def apply_edit(request: str, session_id: int, target_hint: str | None = None) -> EditResult:
    current = repo.latest_proposal(session_id)
    if current is None:
        return EditResult(None, "There is no draft yet. Upload a transcript first and I will write one.", [])
    profile = repo.latest_profile(session_id)
    pack = repo.latest_pack(session_id) or ResearchPack(query="", industries_searched=[], used_fallback=False)
    outline = Outline.model_validate(current.outline_json)
    listing = "\n".join(f"- {s.key}: {s.title} ({s.section_type}) — {s.purpose}" for s in outline.sections)
    prompt = f"Sections:\n{listing}\n\nConsultant request: {request}\nRouter hint: {target_hint or 'none'}"
    try:
        r = llm.generate_structured(prompt, EditPlan, system=SYSTEM, tier="strong", temperature=0)
    except llm.LLMError as e:
        repo.log_agent_run("editor", session_id=session_id, error=str(e))
        return EditResult(None, "I could not reach the language model to plan that change. Please try again shortly.", [])
    plan: EditPlan = r.parsed
    valid = {s.key for s in outline.sections}
    changes = [c for c in plan.changes if c.key in valid]
    repo.log_agent_run("editor", session_id=session_id, model=r.model, output=plan.model_dump(mode="json"), latency_ms=r.latency_ms)
    if plan.needs_clarification or not changes:
        return EditResult(None, plan.question or "Which section should I change?", [])

    by_key = {s.key: s for s in outline.sections}
    current_text = {s.key: s.content_md for s in current.sections}
    updates = []
    for c in changes:
        d = writer.write_section(by_key[c.key], outline, profile, pack, instruction=c.instruction,
                                 current=current_text[c.key], session_id=session_id, agent="editor_section")
        updates.append(d)
    new = repo.revise_sections(session_id, updates, plan.change_note)
    titles = ", ".join(by_key[c.key].title for c in changes)
    return EditResult(new, f"v{new.version}: {plan.change_note}. Changed: {titles}. Every other section is unchanged.",
                      [c.key for c in changes])
