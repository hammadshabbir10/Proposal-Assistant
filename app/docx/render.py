"""Proposal DOCX renderer. Deterministic: the same sections always produce the same document, so formatting is
never left to an LLM. Understands a small Markdown subset: ### subheadings, - bullets, 1. numbered items,
| tables |, **bold**. Text marked [TBD: ...] or "(to be confirmed)" is highlighted for the consultant.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from app.config import get_settings

INK = RGBColor(0x1F, 0x29, 0x37)
ACCENT = RGBColor(0x0F, 0x5C, 0x6E)
MUTED = RGBColor(0x6B, 0x72, 0x80)
_FLAG = re.compile(r"(\[TBD:[^\]]*\]|\(to be confirmed\))", re.I)
_BOLD = re.compile(r"(\*\*[^*]+\*\*)")


def _style(doc: Document) -> None:
    base = doc.styles["Normal"]
    base.font.name, base.font.size, base.font.color.rgb = "Calibri", Pt(11), INK
    base.paragraph_format.space_after = Pt(6)
    base.paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 18), ("Heading 2", 14), ("Heading 3", 12)):
        st = doc.styles[name]
        st.font.name, st.font.size, st.font.bold, st.font.color.rgb = "Calibri", Pt(size), True, ACCENT
        st.paragraph_format.space_before, st.paragraph_format.space_after = Pt(14 if size > 12 else 10), Pt(6)


def _runs(par, text: str) -> None:
    """Add text with **bold** and highlighted [TBD]/(to be confirmed) spans."""
    for piece in _BOLD.split(text):
        if not piece:
            continue
        bold = piece.startswith("**") and piece.endswith("**")
        body = piece[2:-2] if bold else piece
        for part in _FLAG.split(body):
            if not part:
                continue
            r = par.add_run(part)
            r.bold = bold or None
            if _FLAG.fullmatch(part):
                r.font.highlight_color = WD_COLOR_INDEX.YELLOW
                r.bold = True


def _shade(cell, hex_fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _table(doc: Document, rows: list[list[str]]) -> None:
    width = max(len(r) for r in rows)
    t = doc.add_table(rows=0, cols=width)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        cells = t.add_row().cells
        for j in range(width):
            text = row[j] if j < len(row) else ""
            par = cells[j].paragraphs[0]
            _runs(par, text)
            if i == 0:
                for r in par.runs:
                    r.bold, r.font.color.rgb = True, RGBColor(0xFF, 0xFF, 0xFF)
                _shade(cells[j], "0F5C6E")
    doc.add_paragraph()


def normalize_markdown(md: str) -> str:
    """Restore line breaks some models drop ("text. ### Heading 1. Item 2. Item" on one line).
    Applied to every section before it is stored, so the saved text and the document always agree."""
    md = md.replace("\r\n", "\n").replace("\\n", "\n").strip()
    md = re.sub(r"[ \t]+(#{1,3}\s+\S)", r"\n\n\1", md)                         # inline headings
    md = re.sub(r"(?<=[.:!?)\]])[ \t]+(\d+[.)]\s+(?=\S))", r"\n\1", md)          # inline numbered items
    md = re.sub(r"(?<=[.:!?)\]])[ \t]+([-•]\s+(?=[A-Z*\[]))", r"\n\1", md)       # inline bullets
    md = re.sub(r"(?m)^(#{1,3}\s+[^\n]+?)\s+(?=\d+[.)]\s)", r"\1\n", md)         # heading followed by list on same line
    return re.sub(r"\n{3,}", "\n\n", md)


def render_markdown(doc: Document, md: str) -> None:
    lines = normalize_markdown(md).split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("|"):  # table block
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                _table(doc, rows)
            continue
        if s.startswith("#"):
            doc.add_heading(s.lstrip("#").strip(), level=3)
        elif re.match(r"^[-*•]\s+", s):
            _runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*•]\s+", "", s))
        elif re.match(r"^\d+[.)]\s+", s):
            _runs(doc.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", s))
        else:
            para = [s]
            while i + 1 < len(lines) and lines[i + 1].strip() and not re.match(r"^(\||#|[-*•]\s|\d+[.)]\s)", lines[i + 1].strip()):
                i += 1
                para.append(lines[i].strip())
            _runs(doc.add_paragraph(), " ".join(para))
        i += 1


def _page_number_footer(doc: Document, client: str) -> None:
    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = footer.add_run(f"{get_settings().firm_name}  ·  Proposal for {client}  ·  Page ")
    r.font.size, r.font.color.rgb = Pt(9), MUTED
    run = footer.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    run.font.size = Pt(9)


def render_proposal(*, title: str, client: str, sections: list[dict], version: int, status: str,
                    out_path: Path, prepared_for: str | None = None) -> Path:
    """sections: dicts with key, title, section_type, content_md, in display order."""
    doc = Document()
    _style(doc)
    s = get_settings()

    # cover page
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("PROPOSAL")
    r.bold, r.font.size, r.font.color.rgb = True, Pt(12), ACCENT
    p = doc.add_paragraph()
    r = p.add_run(title)
    r.bold, r.font.size, r.font.color.rgb = True, Pt(28), INK
    meta = [("Prepared for", prepared_for or client), ("Prepared by", s.firm_name),
            ("Date", date.today().strftime("%B %d, %Y")), ("Version", f"v{version} · {status.title()}")]
    for k, v in meta:
        p = doc.add_paragraph()
        a = p.add_run(f"{k}: ")
        a.bold, a.font.color.rgb = True, MUTED
        p.add_run(v)
    cover = next((x for x in sections if x["section_type"] == "cover"), None)
    if cover and cover["content_md"].strip():
        doc.add_paragraph()
        render_markdown(doc, cover["content_md"])
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # body
    for sec in sections:
        if sec["section_type"] == "cover":
            continue
        doc.add_heading(sec["title"], level=1)
        render_markdown(doc, sec["content_md"])

    _page_number_footer(doc, client)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


def render_from_db(proposal, client: str, prepared_for: str | None = None) -> Path:
    """Render a stored Proposal row (with its sections) to var/output/<client>_Proposal_v<N>.docx."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", client).strip("_") or "Client"
    out = get_settings().output_dir / f"{safe}_Proposal_v{proposal.version}.docx"
    secs = [{"key": x.key, "title": x.title, "section_type": x.section_type, "content_md": x.content_md}
            for x in sorted(proposal.sections, key=lambda x: x.order_idx)]
    return render_proposal(title=proposal.outline_json.get("title", "Proposal"), client=client, sections=secs,
                           version=proposal.version, status=proposal.status, out_path=out, prepared_for=prepared_for)
