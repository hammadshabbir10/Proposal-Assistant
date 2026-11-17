"""Intake agent: transcript(s) -> ClientProfile, with every fact backed by a verified quote.

The LLM proposes the profile. Code then checks every quote against the transcript; a quote that cannot be found is
removed, and a field left with no verified evidence is downgraded to "missing". The prompt asks the model not to
guess; this check makes sure of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from app import llm
from app.db import repo
from app.logs import log
from app.rag.chunking import Turn, parse_transcript
from app.schemas import ClientProfile, ExtractedField, FieldStatus, Quote

PROMPT = (Path(__file__).parent / "prompts" / "intake.md").read_text(encoding="utf-8")
MATCH_THRESHOLD = 90  # fuzzy match score (0-100) a quote needs against some turn to count as real


@dataclass
class VerificationReport:
    quotes_checked: int = 0
    quotes_removed: list[str] = field(default_factory=list)
    fields_downgraded: list[str] = field(default_factory=list)


@dataclass
class IntakeResult:
    profile: ClientProfile
    report: VerificationReport
    turns: list[Turn]
    profile_version: int | None = None


def combine_transcripts(raw_texts: list[str]) -> list[Turn]:
    """Merge every transcript in the session into one numbered list of turns (later uploads continue the numbering)."""
    turns: list[Turn] = []
    for raw in raw_texts:
        header, ts = parse_transcript(raw)
        if header:  # company, date, participants with job titles: kept as a quotable line, not a speaker
            info = "; ".join(f"{k.title()}: {v}" for k, v in header.items())
            turns.append(Turn(index=len(turns) + 1, speaker="Header", text=info))
        for t in ts:
            turns.append(Turn(index=len(turns) + 1, speaker=t.speaker, text=t.text))
    return turns


def _render(turns: list[Turn]) -> str:
    return "\n".join(f"[turn {t.index}] {t.speaker}: {t.text}" for t in turns)


def _find(quote: Quote, turns: list[Turn]) -> Turn | None:
    """The turn that contains this quote (fuzzy, to tolerate punctuation or dash differences), or None."""
    q = quote.text.strip().strip('"“”')
    if len(q) < 4:
        return None
    best, best_score = None, 0.0
    for t in turns:
        score = fuzz.partial_ratio(q.lower(), t.text.lower())
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= MATCH_THRESHOLD else None


def _verify_field(name: str, f: ExtractedField, turns: list[Turn], rep: VerificationReport) -> ExtractedField:
    kept = []
    for q in f.evidence:
        rep.quotes_checked += 1
        t = _find(q, turns)
        if t is None:
            rep.quotes_removed.append(f"{name}: {q.text[:60]}")
            continue
        kept.append(Quote(speaker=t.speaker, text=q.text.strip(), turn=t.index))  # trust the transcript, not the model
    f = f.model_copy(update={"evidence": kept})
    if f.status != FieldStatus.missing and not kept:
        rep.fields_downgraded.append(name)
        return ExtractedField(value=None, status=FieldStatus.missing,
                              note="The model could not quote the transcript for this, so it is treated as missing.")
    speakers = {q.speaker for q in kept if q.speaker != "Header"}
    if f.status == FieldStatus.conflicting and len(f.alternatives) < 2:
        f = f.model_copy(update={"status": FieldStatus.ambiguous})  # a conflict needs at least two competing values
    elif f.status == FieldStatus.ambiguous and len(f.alternatives) >= 2 and len(speakers) >= 2:
        # two people quoted giving different values is a conflict, even if the model saw a compromise
        f = f.model_copy(update={"status": FieldStatus.conflicting})
    return f


def verify(profile: ClientProfile, turns: list[Turn]) -> tuple[ClientProfile, VerificationReport]:
    rep = VerificationReport()
    data = {}
    for name in ("company", "industry", "problem", "goals", "budget", "timeline"):
        data[name] = _verify_field(name, getattr(profile, name), turns, rep)
    for name in ("integrations", "constraints", "out_of_scope"):
        items = [_verify_field(f"{name}[{i}]", f, turns, rep) for i, f in enumerate(getattr(profile, name))]
        data[name] = [f for f in items if f.status != FieldStatus.missing or f.note]
    stakeholders = []
    for s in profile.stakeholders:
        ev = []
        for q in s.evidence:
            rep.quotes_checked += 1
            t = _find(q, turns)
            if t:
                ev.append(Quote(speaker=t.speaker, text=q.text, turn=t.index))
            else:
                rep.quotes_removed.append(f"stakeholder {s.name}: {q.text[:60]}")
        # keep a stakeholder only if their name actually appears in the transcript
        if ev or any(s.name.split()[0].lower() in (t.speaker + " " + t.text).lower() for t in turns):
            stakeholders.append(s.model_copy(update={"evidence": ev}))
    data["stakeholders"] = stakeholders
    return ClientProfile(**data), rep


def run_intake(raw_texts: list[str], session_id: int | None = None) -> IntakeResult:
    turns = combine_transcripts(raw_texts)
    if not turns:
        raise ValueError("No speaker turns found. Expected lines like 'Name: what they said'.")
    prompt = f"Transcript:\n\n{_render(turns)}\n\nExtract the client profile."
    try:
        r = llm.generate_structured(prompt, ClientProfile, system=PROMPT, tier="strong", temperature=0.1)
    except llm.LLMError as e:
        repo.log_agent_run("intake", session_id=session_id, error=str(e))
        raise
    profile, rep = verify(r.parsed, turns)
    version = repo.save_profile(session_id, profile) if session_id else None
    repo.log_agent_run("intake", session_id=session_id, model=r.model, output=profile.model_dump(mode="json"),
                       tokens_in=r.tokens_in, tokens_out=r.tokens_out, latency_ms=r.latency_ms,
                       error=None if not rep.quotes_removed else f"removed {len(rep.quotes_removed)} unverified quotes")
    log.info("intake.done", model=r.model, quotes=rep.quotes_checked, removed=len(rep.quotes_removed),
             downgraded=rep.fields_downgraded)
    return IntakeResult(profile=profile, report=rep, turns=turns, profile_version=version)


def clarification_questions(profile: ClientProfile) -> list[tuple[str, FieldStatus, str]]:
    """Plain questions for the consultant about each open core field: (field, status, question)."""
    out = []
    for name, f in profile.open_issues():
        label = name.replace("_", " ")
        if f.status == FieldStatus.conflicting:
            who = "; ".join(f'{q.speaker} (turn {q.turn}): "{q.text}"' for q in f.evidence[:3])
            q = f"The {label} is stated differently: {who}. Which should the proposal use, or show both?"
        elif f.status == FieldStatus.ambiguous:
            q = f"The {label} is unclear ({f.note or ', '.join(f.alternatives)}). What should the proposal state?"
        else:
            q = f"The {label} was not discussed. {f.note or 'What should the proposal say?'}"
        out.append((name, f.status, q))
    return out
