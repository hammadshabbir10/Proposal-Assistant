"""Research agent on the Brightline profile (saved from a real Intake run, so this test skips the Intake call)."""
from pathlib import Path

import pytest

from app.agents import research
from app.schemas import ClientProfile

pytestmark = pytest.mark.integration
PROFILE = ClientProfile.model_validate_json(
    (Path(__file__).parent / "fixtures" / "brightline_profile.json").read_text(encoding="utf-8"))


def test_research_covers_every_required_section_with_comparable_work():
    pack = research.run_research(PROFILE)
    for sec in research.MUST_COVER:
        assert pack.by_section.get(sec), f"no reference material for {sec}"
    used_docs = {c.doc_id for chunks in pack.by_section.values() for c in chunks[:2]}
    assert used_docs & {"logistics", "retail", "retail2", "distribution"}
    assert not used_docs & {"healthcare", "fintech"}, used_docs
    assert pack.reference_outlines, "the writer needs at least one reference structure"
