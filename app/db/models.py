"""Structured state in SQLite. Versioned rows (profiles, proposals, sections) are never overwritten,
so the consultant can come back tomorrow and every earlier draft is still there."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """One ongoing piece of work per consultant per Slack conversation."""
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("slack_user_id", "slack_channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(32))
    slack_channel_id: Mapped[str] = mapped_column(String(32))
    thread_ts: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="new")  # new|awaiting_clarification|drafted|approved
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    transcripts: Mapped[list[Transcript]] = relationship(back_populates="session", order_by="Transcript.id")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    filename: Mapped[str] = mapped_column(String(255))
    slack_file_id: Mapped[str | None] = mapped_column(String(32))
    raw_text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(default=utcnow)

    session: Mapped[Session] = relationship(back_populates="transcripts")
    __table_args__ = (UniqueConstraint("session_id", "sha256"),)  # same file twice in a session is ignored


class ClientProfileRow(Base):
    __tablename__ = "client_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    profile_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Clarification(Base):
    """A gap or conflict the consultant was asked about, and how it was resolved."""
    __tablename__ = "clarifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    field: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))          # missing|ambiguous|conflicting
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(16))  # user|marked_tbd
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (UniqueConstraint("session_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    outline_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|approved
    change_note: Mapped[str | None] = mapped_column(Text)
    docx_path: Mapped[str | None] = mapped_column(String(512))
    slack_file_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    sections: Mapped[list[ProposalSection]] = relationship(back_populates="proposal", order_by="ProposalSection.order_idx")


class ProposalSection(Base):
    """One row per section per version. An edit copies untouched rows and rewrites only the target ones."""
    __tablename__ = "proposal_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"))
    key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    section_type: Mapped[str] = mapped_column(String(32))
    order_idx: Mapped[int] = mapped_column(Integer)
    content_md: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)   # bumped only when this section is rewritten

    proposal: Mapped[Proposal] = relationship(back_populates="sections")


class ResearchPackRow(Base):
    """The Research agent's output, kept so a later revision writes from the same references as the draft."""
    __tablename__ = "research_packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    pack_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Message(Base):
    """Conversation memory, so follow-ups the next day have context."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    role: Mapped[str] = mapped_column(String(16))  # user|assistant
    text: Mapped[str] = mapped_column(Text)
    slack_ts: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AgentRun(Base):
    """Trace of every agent step: which model answered, tokens, time, tool calls, errors."""
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    agent: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    output_json: Mapped[dict | None] = mapped_column(JSON)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SlackEvent(Base):
    """Slack re-sends events it thinks failed; we keep ids to process each one once."""
    __tablename__ = "slack_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


def make_session_factory(db_path: str) -> sessionmaker:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
