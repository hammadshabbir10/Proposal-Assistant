"""End to end through the LangGraph flow (no Slack): upload -> pause for answers -> resume -> draft -> edit.
Uses a temporary database and graph checkpoint, the real vector store and Gemini."""
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import repo
from app.graph import build
from app.slack import notify

pytestmark = pytest.mark.integration
RAW = (Path(__file__).resolve().parents[1] / "data" / "client_transcript.txt").read_text(encoding="utf-8")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "db_path", tmp_path / "state.db")
    monkeypatch.setattr(s, "output_dir", tmp_path / "out")
    repo._factory.cache_clear()
    build.graph.cache_clear()
    n = notify.ConsoleNotifier()
    notify.set_notifier(n)
    yield n
    repo._factory.cache_clear()
    build.graph.cache_clear()


def test_full_flow_pause_resume_and_scoped_edit(isolated):
    sid = repo.get_or_create_session("UT", "DT").id
    t, _ = repo.add_transcript(sid, "client_transcript.txt", RAW)

    build.start_draft(sid, t.id)
    assert build.awaiting_answers(sid), "conflicting budget should pause the draft for the consultant"
    assert repo.latest_proposal(sid) is None

    # a fresh graph object (as after a restart) still sees the pause
    build.graph.cache_clear()
    assert build.awaiting_answers(sid)

    assert build.resume_draft(sid, None)  # "Draft now, mark gaps TBD"
    v1 = repo.latest_proposal(sid)
    assert v1 and v1.version == 1 and isolated.files, "draft v1 rendered and delivered"

    build.chat(sid, "edit", "make the timeline section more detailed", "timeline")
    v2 = repo.latest_proposal(sid)
    assert v2.version == 2
    before = {s.key: s.content_md for s in v1.sections}
    after = {s.key: s.content_md for s in v2.sections}
    changed = [k for k in before if before[k] != after[k]]
    assert len(changed) == 1 and "timeline" in changed[0], changed
    assert len(isolated.files) == 2
