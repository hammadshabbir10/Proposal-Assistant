"""Database behaviour the rest of the system relies on, run against a temporary SQLite file."""
import pytest

from app.config import get_settings
from app.db import repo
from app.schemas import ClientProfile, ExtractedField, FieldStatus, Outline, OutlineSection, SectionDraft


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "db_path", tmp_path / "test.db")
    repo._factory.cache_clear()
    yield
    repo._factory.cache_clear()


def _field(v, status=FieldStatus.present):
    return ExtractedField(value=v, status=status)


def _profile():
    f = _field
    return ClientProfile(company=f("Brightline"), industry=f("retail"), problem=f("no visibility"), goals=f("one view"),
                         budget=f(None, FieldStatus.conflicting), timeline=f("Q3", FieldStatus.ambiguous))


def _outline():
    secs = [OutlineSection(key=k, title=k.title(), section_type=t, purpose="x") for k, t in
            [("cover", "cover"), ("summary", "executive_summary"), ("solution", "proposed_solution"),
             ("timeline", "timeline"), ("budget", "budget"), ("next", "next_steps")]]
    return Outline(title="Proposal", rationale="test", sections=secs)


def test_session_is_reused_for_same_user_and_channel():
    a = repo.get_or_create_session("U1", "D1")
    b = repo.get_or_create_session("U1", "D1")
    assert a.id == b.id


def test_same_transcript_twice_is_not_duplicated():
    s = repo.get_or_create_session("U1", "D1")
    _, new1 = repo.add_transcript(s.id, "t.txt", "hello")
    _, new2 = repo.add_transcript(s.id, "t.txt", "hello")
    _, new3 = repo.add_transcript(s.id, "t2.txt", "second call")
    assert (new1, new2, new3) == (True, False, True)
    assert len(repo.list_transcripts(s.id)) == 2


def test_profile_versions_increase():
    s = repo.get_or_create_session("U1", "D1")
    assert repo.save_profile(s.id, _profile()) == 1
    assert repo.save_profile(s.id, _profile()) == 2
    assert repo.latest_profile(s.id).budget.status == FieldStatus.conflicting


def test_revision_changes_only_the_target_section():
    s = repo.get_or_create_session("U1", "D1")
    outline = _outline()
    drafts = [SectionDraft(key=sec.key, title=sec.title, content_md=f"v1 {sec.key}") for sec in outline.sections]
    v1 = repo.create_proposal(s.id, outline, drafts)
    v2 = repo.revise_sections(s.id, [SectionDraft(key="timeline", title="Timeline", content_md="v2 detailed")], "Timeline expanded")
    assert v2.version == v1.version + 1
    before = {x.key: x.content_md for x in v1.sections}
    after = {x.key: x.content_md for x in v2.sections}
    assert after["timeline"] == "v2 detailed"
    assert {k: v for k, v in after.items() if k != "timeline"} == {k: v for k, v in before.items() if k != "timeline"}


def test_slack_retries_are_ignored():
    assert repo.first_time_seen("Ev1") is True
    assert repo.first_time_seen("Ev1") is False
