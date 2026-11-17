"""DOCX renderer: offline, checks the document structure python-docx reads back."""
from docx import Document

from app.docx.render import normalize_markdown, render_proposal


def test_normalize_restores_dropped_line_breaks():
    # real output from a fallback model: heading and numbered list run into one line
    raw = ("To meet the Q3 target, we propose the following. ### Immediate Action Items 1. Marcus Webb to sign off "
           "(to be confirmed). 2. Jenny Tran to schedule discovery with Raj. 3. Kickoff.")
    lines = normalize_markdown(raw).split("\n")
    assert "### Immediate Action Items" in lines
    assert [ln[:2] for ln in lines if ln[:1].isdigit()] == ["1.", "2.", "3."]
    # well-formed text is left alone
    ok = "Intro.\n\n### Heading\n\n- one\n- two"
    assert normalize_markdown(ok) == ok

SECTIONS = [
    {"key": "cover", "title": "Cover", "section_type": "cover", "content_md": "A unified inventory platform for Brightline."},
    {"key": "summary", "title": "Executive Summary", "section_type": "executive_summary",
     "content_md": "Brightline runs **three warehouses**.\n\n- Real-time visibility\n- Automated reporting"},
    {"key": "solution", "title": "Proposed Solution", "section_type": "proposed_solution",
     "content_md": "### Phase 1\nIntegration layer.\n\n1. Connect sites\n2. Central database"},
    {"key": "timeline", "title": "Timeline", "section_type": "timeline",
     "content_md": "| Phase | Weeks |\n|---|---|\n| Discovery | 1-2 |\n| Build | 3-10 |\n\nStable by end of Q3 (to be confirmed)."},
    {"key": "budget", "title": "Investment", "section_type": "budget",
     "content_md": "A $50,000 baseline, up to $120,000 (to be confirmed)."},
    {"key": "next", "title": "Next Steps", "section_type": "next_steps", "content_md": "[TBD: shipping software]"},
]


def test_render_produces_headings_tables_and_highlights(tmp_path):
    out = render_proposal(title="Unified Inventory Platform", client="Brightline Retail Group", sections=SECTIONS,
                          version=2, status="draft", out_path=tmp_path / "p.docx")
    doc = Document(out)
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Executive Summary", "Proposed Solution", "Timeline", "Investment", "Next Steps"]
    assert any(p.text == "Phase 1" and p.style.name == "Heading 3" for p in doc.paragraphs)
    assert len(doc.tables) == 1 and doc.tables[0].rows[1].cells[0].text == "Discovery"
    bullets = [p.text for p in doc.paragraphs if p.style.name == "List Bullet"]
    assert bullets == ["Real-time visibility", "Automated reporting"]
    highlighted = [r.text for p in doc.paragraphs for r in p.runs if r.font.highlight_color]
    assert "(to be confirmed)" in highlighted and "[TBD: shipping software]" in highlighted
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Prepared for: Brightline Retail Group" in text and "v2 · Draft" in text
