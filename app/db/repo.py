"""Data access. Agents and Slack handlers call these functions; no SQL anywhere else."""
from __future__ import annotations

import hashlib
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import models as m
from app.schemas import ClientProfile, Outline, ResearchPack, SectionDraft


@lru_cache
def _factory():
    return m.make_session_factory(str(get_settings().db_path))


def db():
    return _factory()()


# ---------- sessions & transcripts ----------

def get_or_create_session(slack_user_id: str, slack_channel_id: str, thread_ts: str | None = None) -> m.Session:
    with db() as s:
        row = s.scalar(select(m.Session).where(m.Session.slack_user_id == slack_user_id,
                                               m.Session.slack_channel_id == slack_channel_id))
        if row is None:
            row = m.Session(slack_user_id=slack_user_id, slack_channel_id=slack_channel_id, thread_ts=thread_ts)
            s.add(row)
            s.commit()
        return row


def set_session_status(session_id: int, status: str, thread_ts: str | None = None) -> None:
    with db() as s:
        row = s.get(m.Session, session_id)
        row.status = status
        if thread_ts:
            row.thread_ts = thread_ts
        s.commit()


def add_transcript(session_id: int, filename: str, text: str, slack_file_id: str | None = None) -> tuple[m.Transcript, bool]:
    """Add a transcript to the session. Returns (row, is_new); the same file uploaded twice is not duplicated."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with db() as s:
        existing = s.scalar(select(m.Transcript).where(m.Transcript.session_id == session_id, m.Transcript.sha256 == digest))
        if existing:
            return existing, False
        row = m.Transcript(session_id=session_id, filename=filename, raw_text=text, sha256=digest, slack_file_id=slack_file_id)
        s.add(row)
        s.commit()
        return row, True


def list_transcripts(session_id: int) -> list[m.Transcript]:
    with db() as s:
        return list(s.scalars(select(m.Transcript).where(m.Transcript.session_id == session_id).order_by(m.Transcript.id)))


# ---------- client profile & clarifications ----------

def save_profile(session_id: int, profile: ClientProfile) -> int:
    with db() as s:
        version = (s.scalar(select(func.max(m.ClientProfileRow.version)).where(m.ClientProfileRow.session_id == session_id)) or 0) + 1
        s.add(m.ClientProfileRow(session_id=session_id, version=version, profile_json=profile.model_dump(mode="json")))
        s.commit()
        return version


def latest_profile(session_id: int) -> ClientProfile | None:
    with db() as s:
        row = s.scalar(select(m.ClientProfileRow).where(m.ClientProfileRow.session_id == session_id)
                       .order_by(m.ClientProfileRow.version.desc()).limit(1))
        return ClientProfile.model_validate(row.profile_json) if row else None


def add_clarification(session_id: int, field: str, status: str, question: str) -> int:
    with db() as s:
        row = m.Clarification(session_id=session_id, field=field, status=status, question=question)
        s.add(row)
        s.commit()
        return row.id


def resolve_clarification(clarification_id: int, answer: str | None, resolved_by: str) -> None:
    with db() as s:
        row = s.get(m.Clarification, clarification_id)
        row.answer, row.resolved_by, row.resolved_at = answer, resolved_by, m.utcnow()
        s.commit()


def open_clarifications(session_id: int) -> list[m.Clarification]:
    with db() as s:
        return list(s.scalars(select(m.Clarification).where(m.Clarification.session_id == session_id,
                                                            m.Clarification.resolved_at.is_(None))))


# ---------- proposals ----------

def create_proposal(session_id: int, outline: Outline, drafts: list[SectionDraft], change_note: str | None = None) -> m.Proposal:
    by_key = {d.key: d for d in drafts}
    with db() as s:
        version = (s.scalar(select(func.max(m.Proposal.version)).where(m.Proposal.session_id == session_id)) or 0) + 1
        p = m.Proposal(session_id=session_id, version=version, outline_json=outline.model_dump(mode="json"), change_note=change_note)
        for i, sec in enumerate(outline.sections):
            d = by_key[sec.key]
            p.sections.append(m.ProposalSection(key=sec.key, title=d.title, section_type=sec.section_type, order_idx=i,
                                                content_md=d.content_md, sources_json=d.sources))
        s.add(p)
        s.commit()
        return _load_proposal(s, p.id)


def latest_proposal(session_id: int) -> m.Proposal | None:
    with db() as s:
        pid = s.scalar(select(m.Proposal.id).where(m.Proposal.session_id == session_id).order_by(m.Proposal.version.desc()).limit(1))
        return _load_proposal(s, pid) if pid else None


def revise_sections(session_id: int, updates: list[SectionDraft], change_note: str) -> m.Proposal:
    """New version: rewritten sections get the new text, every other section is copied unchanged."""
    current = latest_proposal(session_id)
    if current is None:
        raise ValueError("No proposal to revise yet")
    new = {u.key: u for u in updates}
    unknown = set(new) - {sec.key for sec in current.sections}
    if unknown:
        raise ValueError(f"Unknown section keys: {sorted(unknown)}")
    with db() as s:
        p = m.Proposal(session_id=session_id, version=current.version + 1, outline_json=current.outline_json, change_note=change_note)
        for sec in current.sections:
            u = new.get(sec.key)
            p.sections.append(m.ProposalSection(
                key=sec.key, title=u.title if u else sec.title, section_type=sec.section_type, order_idx=sec.order_idx,
                content_md=u.content_md if u else sec.content_md,
                sources_json=u.sources if u else sec.sources_json,
                revision=sec.revision + 1 if u else sec.revision))
        s.add(p)
        s.commit()
        return _load_proposal(s, p.id)


def set_proposal_file(proposal_id: int, docx_path: str, slack_file_id: str | None = None) -> None:
    with db() as s:
        p = s.get(m.Proposal, proposal_id)
        p.docx_path, p.slack_file_id = docx_path, slack_file_id
        s.commit()


def approve_proposal(proposal_id: int) -> None:
    with db() as s:
        s.get(m.Proposal, proposal_id).status = "approved"
        s.commit()


def _load_proposal(s, proposal_id: int) -> m.Proposal:
    p = s.get(m.Proposal, proposal_id)
    _ = list(p.sections)  # load sections before the session closes
    return p


# ---------- research packs ----------

def save_pack(session_id: int, pack: ResearchPack) -> None:
    with db() as s:
        s.add(m.ResearchPackRow(session_id=session_id, pack_json=pack.model_dump(mode="json")))
        s.commit()


def latest_pack(session_id: int) -> ResearchPack | None:
    with db() as s:
        row = s.scalar(select(m.ResearchPackRow).where(m.ResearchPackRow.session_id == session_id)
                       .order_by(m.ResearchPackRow.id.desc()).limit(1))
        return ResearchPack.model_validate(row.pack_json) if row else None


def get_session(session_id: int) -> m.Session | None:
    with db() as s:
        return s.get(m.Session, session_id)


def session_summary(session_id: int) -> dict:
    """Small status view used by the /sessions endpoint and the Router's context."""
    with db() as s:
        sess = s.get(m.Session, session_id)
        if sess is None:
            return {}
        n_tr = s.scalar(select(func.count()).select_from(m.Transcript).where(m.Transcript.session_id == session_id))
        prop = s.scalar(select(m.Proposal).where(m.Proposal.session_id == session_id).order_by(m.Proposal.version.desc()).limit(1))
        runs = s.scalar(select(func.count()).select_from(m.AgentRun).where(m.AgentRun.session_id == session_id))
        return {"session_id": session_id, "status": sess.status, "transcripts": n_tr,
                "proposal_version": prop.version if prop else None, "proposal_status": prop.status if prop else None,
                "sections": [x.title for x in prop.sections] if prop else [], "agent_runs": runs}


# ---------- memory, tracing, idempotency ----------

def add_message(session_id: int, role: str, text: str, slack_ts: str | None = None) -> None:
    with db() as s:
        s.add(m.Message(session_id=session_id, role=role, text=text, slack_ts=slack_ts))
        s.commit()


def recent_messages(session_id: int, limit: int = 12) -> list[m.Message]:
    with db() as s:
        rows = list(s.scalars(select(m.Message).where(m.Message.session_id == session_id).order_by(m.Message.id.desc()).limit(limit)))
        return rows[::-1]


def log_agent_run(agent: str, *, session_id: int | None = None, model: str | None = None, output: dict | None = None,
                  tool_calls: list | None = None, tokens_in: int = 0, tokens_out: int = 0, latency_ms: int = 0,
                  error: str | None = None) -> None:
    with db() as s:
        s.add(m.AgentRun(agent=agent, session_id=session_id, model=model, output_json=output, tool_calls=tool_calls,
                         tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms, error=error))
        s.commit()


def first_time_seen(event_id: str) -> bool:
    """True the first time an event id arrives; False for Slack's retries of the same event."""
    with db() as s:
        s.add(m.SlackEvent(event_id=event_id))
        try:
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            return False
