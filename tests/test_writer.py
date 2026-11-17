"""Writer on the saved Brightline profile: structure, honesty rules and the rendered document."""
import re
from pathlib import Path

import pytest
from docx import Document

from app.agents import research, writer
from app.docx.render import render_proposal
from app.schemas import REQUIRED_SECTION_TYPES, ClientProfile

pytestmark = pytest.mark.integration
PROFILE = ClientProfile.model_validate_json(
    (Path(__file__).parent / "fixtures" / "brightline_profile.json").read_text(encoding="utf-8"))


def test_writer_plans_required_sections_and_stays_honest(tmp_path):
    pack = research.run_research(PROFILE)
    outline, drafts = writer.generate(PROFILE, pack)
    assert not outline.missing_required()
    assert {s.section_type for s in outline.sections} >= set(REQUIRED_SECTION_TYPES)
    assert all(d.content_md.strip() for d in drafts), "every section has text"

    body = "\n".join(d.content_md for d in drafts)
    # conflicting budget: both values shown, never one silently picked
    budget = next(d.content_md for d, s in zip(drafts, outline.sections) if s.section_type == "budget")
    assert re.search(r"50,?000|50k", budget, re.I) and re.search(r"120,?000|120k", budget, re.I)
    # missing shipping software is flagged, not invented
    assert re.search(r"\[TBD:[^\]]*shipping", body, re.I)
    # no recurring fees borrowed from other clients' proposals
    assert not re.search(r"\$[\d,]+\s*(?:/|per)\s*month", body, re.I)

    out = render_proposal(title=outline.title, client="Brightline Retail Group", out_path=tmp_path / "p.docx",
                          version=1, status="draft",
                          sections=[{"key": s.key, "title": d.title, "section_type": s.section_type, "content_md": d.content_md}
                                    for s, d in zip(outline.sections, drafts)])
    doc = Document(out)
    assert len([p for p in doc.paragraphs if p.style.name == "Heading 1"]) == len(outline.sections) - 1  # cover is the title page
