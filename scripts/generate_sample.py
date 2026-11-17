"""Generate a proposal from the saved Brightline profile and print an audit of the result.
usage: python -m scripts.generate_sample
"""
import re
import time
from pathlib import Path

from docx import Document

from app.agents import research, writer
from app.db import repo
from app.docx.render import render_from_db
from app.logs import setup_logging
from app.schemas import ClientProfile

setup_logging("ERROR")
profile = ClientProfile.model_validate_json(Path("tests/fixtures/brightline_profile.json").read_text(encoding="utf-8"))
session = repo.get_or_create_session("UTEST", "DTEST")

t = time.time()
pack = research.run_research(profile, session.id)
outline, drafts = writer.generate(profile, pack, session.id)
print(f"generated in {time.time() - t:.0f}s")
proposal = repo.create_proposal(session.id, outline, drafts)
path = render_from_db(proposal, profile.company.value)

doc = Document(path)
text = "\n".join(p.text for p in doc.paragraphs) + "\n" + "\n".join(
    c.text for tb in doc.tables for row in tb.rows for c in row.cells)
print("sections:", [p.text for p in doc.paragraphs if p.style.name == "Heading 1"])
print("dollar amounts:", sorted(set(re.findall(r"\$[\d,]+(?:\.\d+)?k?", text))))
print("monthly fees:", re.findall(r"[^.\n]*(?:/month|per month|monthly rate)[^.\n]*", text)[:3])
print("TBDs:", re.findall(r"\[TBD:[^\]]*\]", text)[:6])
paras = doc.paragraphs
i = next(k for k, p in enumerate(paras) if p.style.name == "Heading 1" and "Next" in p.text)
for p in paras[i:i + 9]:
    print("   ", p.style.name[:12].ljust(12), p.text[:120])
print(path)
