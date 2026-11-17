"""Typed contracts passed between agents. Every agent reads and writes these, never free-form dicts."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Intake ----------

class FieldStatus(str, Enum):
    present = "present"          # stated clearly, backed by a quote
    missing = "missing"          # not in the transcript
    ambiguous = "ambiguous"      # stated, but vague or hedged
    conflicting = "conflicting"  # stated differently by different people


class Quote(BaseModel):
    speaker: str
    text: str = Field(description="Exact words copied from the transcript")
    turn: int | None = Field(default=None, description="Turn number in the transcript, if known")


class ExtractedField(BaseModel):
    value: str | None = Field(default=None, description="Best summary of what was said; null if missing")
    status: FieldStatus
    evidence: list[Quote] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list, description="The competing values when ambiguous or conflicting")
    note: str | None = Field(default=None, description="Why it is ambiguous, conflicting or missing")


class Stakeholder(BaseModel):
    name: str
    role: str | None = None
    organisation: str | None = None
    influence: str | None = Field(default=None, description="e.g. decision maker, daily user, technical owner")
    evidence: list[Quote] = Field(default_factory=list)


class ClientProfile(BaseModel):
    company: ExtractedField
    industry: ExtractedField
    problem: ExtractedField
    goals: ExtractedField
    budget: ExtractedField
    timeline: ExtractedField
    stakeholders: list[Stakeholder] = Field(default_factory=list)
    integrations: list[ExtractedField] = Field(default_factory=list)
    constraints: list[ExtractedField] = Field(default_factory=list)
    out_of_scope: list[ExtractedField] = Field(default_factory=list, description="Things raised but deprioritised")

    def open_issues(self) -> list[tuple[str, ExtractedField]]:
        """Core fields that still need the consultant's attention."""
        core = {"company": self.company, "industry": self.industry, "problem": self.problem,
                "goals": self.goals, "budget": self.budget, "timeline": self.timeline}
        return [(k, f) for k, f in core.items() if f.status != FieldStatus.present]


# ---------- Research ----------

class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    industry: str
    section_type: str
    section_title: str
    text: str
    score: float


class ResearchPack(BaseModel):
    query: str
    industries_searched: list[str]
    used_fallback: bool = Field(description="True if the industry filter returned too little and the search was widened")
    by_section: dict[str, list[RetrievedChunk]] = Field(default_factory=dict)
    reference_outlines: dict[str, list[str]] = Field(default_factory=dict, description="doc_id -> its section titles, in order")


# ---------- Writer / Editor ----------

SectionType = Literal["cover", "executive_summary", "problem", "proposed_solution", "integrations",
                      "deliverables", "timeline", "budget", "assumptions", "risks", "team", "next_steps", "other"]

REQUIRED_SECTION_TYPES: tuple[str, ...] = ("cover", "executive_summary", "proposed_solution", "timeline", "budget", "next_steps")


class OutlineSection(BaseModel):
    key: str = Field(description="Stable snake_case id, e.g. 'timeline'")
    title: str
    section_type: SectionType
    purpose: str = Field(description="What this section must achieve for this client")
    profile_fields: list[str] = Field(default_factory=list, description="ClientProfile fields this section draws on")


class Outline(BaseModel):
    title: str
    rationale: str = Field(description="Why this structure suits this client, citing the reference proposals")
    sections: list[OutlineSection]

    def missing_required(self) -> list[str]:
        have = {s.section_type for s in self.sections}
        return [t for t in REQUIRED_SECTION_TYPES if t not in have]


class SectionDraft(BaseModel):
    key: str
    title: str
    content_md: str = Field(description="Section body in the restricted Markdown the renderer understands")
    sources: list[str] = Field(default_factory=list, description="chunk_ids or 'transcript' that informed this section")


# ---------- Router ----------

class Intent(str, Enum):
    upload = "upload"
    question = "question"
    edit = "edit"
    approve = "approve"
    answer_clarification = "answer_clarification"
    chitchat = "chitchat"


class RouterDecision(BaseModel):
    intent: Intent
    target_hint: str | None = Field(default=None, description="For edits/questions: which section or topic the user means")
    reason: str
