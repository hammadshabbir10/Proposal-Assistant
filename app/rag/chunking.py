"""Splitting documents into retrieval chunks. Pure functions, no network, fully unit-tested.

Proposals: split by section (the "---" separators and ALL-CAPS headings), so a chunk never mixes, say, the budget
with the timeline. A long section is further cut into overlapping windows. Section *types* are assigned later by
meaning (see ingest.py), because headings vary between proposals ("BUDGET" vs "TIMELINE AND COST").

Transcripts: split by speaker turn and grouped into small windows, never cutting a turn in half, so an answer to
"what did the client say about X" keeps the speaker and their full sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TARGET_WORDS = 260       # ~350 tokens
OVERLAP_WORDS = 40
TRANSCRIPT_WINDOW_WORDS = 200

_HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 &/,'()\-—–]{2,}$")
_TURN = re.compile(r"^([A-Z][A-Za-z.'\- ]{0,40}?):\s+(.*)$")
_HEADER_KEYS = {"client": "client", "prepared for": "client", "prepared by": "prepared_by", "date": "date"}
_TRANSCRIPT_HEADER_KEYS = {"company", "client", "date", "participants", "attendees", "meeting", "subject"}


@dataclass
class ProposalSection:
    title: str
    text: str
    order: int


@dataclass
class ParsedProposal:
    doc_id: str
    title: str
    header: dict[str, str]
    sections: list[ProposalSection]


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict = field(default_factory=dict)


def _words(s: str) -> int:
    return len(s.split())


def _windows(text: str, size: int, overlap: int) -> list[str]:
    """Split long text into overlapping windows on paragraph boundaries where possible."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], []
    for p in paras:
        if cur and _words(" ".join(cur + [p])) > size:
            out.append("\n\n".join(cur))
            tail = " ".join(" ".join(cur).split()[-overlap:])
            cur = [tail] if overlap else []
        cur.append(p)
    if cur:
        out.append("\n\n".join(cur))
    # a single very long paragraph: fall back to word windows
    final = []
    for w in out:
        ws = w.split()
        if len(ws) <= size * 1.5:
            final.append(w)
            continue
        step = size - overlap
        final.extend(" ".join(ws[i:i + size]) for i in range(0, len(ws), step))
    return final


def parse_proposal(doc_id: str, raw: str) -> ParsedProposal:
    blocks = [b.strip() for b in re.split(r"^\s*-{3,}\s*$", raw, flags=re.M) if b.strip()]
    title, header = "", {}
    sections: list[ProposalSection] = []
    for b in blocks:
        lines = b.splitlines()
        first = lines[0].strip()
        if not sections and not header and (first.upper().startswith("PROPOSAL") or ":" in first):
            # header block: "PROPOSAL — X", "Client:", "Prepared by:", "Date:"
            for ln in lines:
                ln = ln.strip()
                if ln.upper().startswith("PROPOSAL"):
                    title = re.sub(r"^PROPOSAL\s*[—–-]\s*", "", ln, flags=re.I).strip()
                elif ":" in ln:
                    k, v = ln.split(":", 1)
                    key = _HEADER_KEYS.get(k.strip().lower())
                    if key:
                        header[key] = v.strip()
            continue
        if _HEADING.match(first):
            body = "\n".join(lines[1:]).strip()
            sections.append(ProposalSection(title=first.title(), text=body, order=len(sections)))
        elif sections:  # stray block without heading: belongs to the previous section
            sections[-1].text += "\n\n" + b
    return ParsedProposal(doc_id=doc_id, title=title, header=header, sections=sections)


def chunk_proposal(p: ParsedProposal) -> list[Chunk]:
    chunks = []
    for s in p.sections:
        for i, piece in enumerate(_windows(s.text, TARGET_WORDS, OVERLAP_WORDS)):
            chunks.append(Chunk(
                chunk_id=f"{p.doc_id}::{s.order:02d}::{i}",
                text=piece,
                metadata={"doc_id": p.doc_id, "section_title": s.title, "section_order": s.order, "part": i},
            ))
    return chunks


@dataclass
class Turn:
    index: int
    speaker: str
    text: str


def parse_transcript(raw: str) -> tuple[dict[str, str], list[Turn]]:
    """Returns (header fields such as Company/Date/Participants, list of speaker turns)."""
    header: dict[str, str] = {}
    turns: list[Turn] = []
    body = raw
    parts = re.split(r"^\s*-{3,}\s*$", raw, maxsplit=1, flags=re.M)
    if len(parts) == 2:
        found = {}
        for ln in parts[0].splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                if k.strip().lower() in _TRANSCRIPT_HEADER_KEYS:
                    found[k.strip().lower()] = v.strip()
        if found:  # a header block sits above the first "---": keep it out of the speaker turns
            header, body = found, parts[1]
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln or re.fullmatch(r"-{3,}|END OF TRANSCRIPT", ln, flags=re.I):
            continue
        m = _TURN.match(ln)
        if m:
            turns.append(Turn(index=len(turns) + 1, speaker=m.group(1).strip(), text=m.group(2).strip()))
        elif turns:
            turns[-1].text += " " + ln  # continuation line of the same turn
    return header, turns


def chunk_transcript(transcript_id: int, session_id: int, turns: list[Turn]) -> list[Chunk]:
    """Group whole turns into ~200-word windows, overlapping by one turn so context carries across chunks."""
    chunks: list[Chunk] = []
    i = 0
    while i < len(turns):
        group = [turns[i]]
        j = i + 1
        while j < len(turns) and _words(" ".join(t.text for t in group + [turns[j]])) <= TRANSCRIPT_WINDOW_WORDS:
            group.append(turns[j])
            j += 1
        text = "\n".join(f"[turn {t.index}] {t.speaker}: {t.text}" for t in group)
        chunks.append(Chunk(
            chunk_id=f"t{transcript_id}::{group[0].index}-{group[-1].index}",
            text=text,
            metadata={"session_id": session_id, "transcript_id": transcript_id, "turn_start": group[0].index,
                      "turn_end": group[-1].index, "speakers": ",".join(sorted({t.speaker for t in group}))},
        ))
        if j >= len(turns):
            break
        i = max(j - 1, i + 1)  # one turn of overlap
    return chunks
