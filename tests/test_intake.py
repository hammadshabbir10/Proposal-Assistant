"""Intake: the quote check (no network) and the real transcript's known traps (integration)."""
from pathlib import Path

import pytest

from app.agents import intake
from app.schemas import ClientProfile, ExtractedField, FieldStatus, Quote, Stakeholder

RAW = (Path(__file__).resolve().parents[1] / "data" / "client_transcript.txt").read_text(encoding="utf-8")
TURNS = intake.combine_transcripts([RAW])


def _f(value, status, *quotes, alts=()):
    return ExtractedField(value=value, status=status, alternatives=list(alts),
                          evidence=[Quote(speaker=s, text=t) for s, t in quotes])


def _profile(**over):
    base = dict(
        company=_f("Brightline", FieldStatus.present, ("Marcus", "we've been running for about eight years")),
        industry=_f("retail", FieldStatus.present, ("Marcus", "three warehouse locations")),
        problem=_f("x", FieldStatus.present, ("Jenny", "they basically don't talk to each other")),
        goals=_f("x", FieldStatus.present, ("Marcus", "All five locations visible in one place")),
        budget=_f("50k", FieldStatus.present, ("Marcus", "We've set aside fifty thousand for this")),
        timeline=_f("Q3", FieldStatus.present, ("Marcus", "end of Q3 this year")),
    )
    base.update(over)
    return ClientProfile(**base)


def test_real_quotes_are_kept_and_get_the_right_turn_and_speaker():
    p, rep = intake.verify(_profile(), TURNS)
    assert p.budget.status == FieldStatus.present
    q = p.budget.evidence[0]
    assert q.speaker == "Marcus" and q.turn is not None
    assert not rep.quotes_removed


def test_invented_quote_is_removed_and_field_downgraded_to_missing():
    fake = _f("$80,000", FieldStatus.present, ("Marcus", "our budget is eighty thousand dollars flat"))
    p, rep = intake.verify(_profile(budget=fake), TURNS)
    assert p.budget.status == FieldStatus.missing and p.budget.value is None
    assert "budget" in rep.fields_downgraded


def test_conflict_needs_two_alternatives():
    one = _f("50k", FieldStatus.conflicting, ("Marcus", "We've set aside fifty thousand for this"), alts=["50k"])
    p, _ = intake.verify(_profile(budget=one), TURNS)
    assert p.budget.status == FieldStatus.ambiguous


def test_two_people_with_different_values_is_a_conflict():
    amb = _f("50k, flexible", FieldStatus.ambiguous,
             ("Marcus", "We've set aside fifty thousand for this"),
             ("Jenny", "I thought we discussed up to a hundred and twenty"), alts=["$50,000", "$120,000"])
    p, _ = intake.verify(_profile(budget=amb), TURNS)
    assert p.budget.status == FieldStatus.conflicting


def test_stakeholder_not_in_transcript_is_dropped():
    p, _ = intake.verify(_profile(stakeholders=[Stakeholder(name="Priya Shah", role="CFO")]), TURNS)
    assert p.stakeholders == []


@pytest.mark.integration
def test_brightline_transcript_traps():
    res = intake.run_intake([RAW])
    p = res.profile
    assert "brightline" in (p.company.value or "").lower()
    assert p.budget.status == FieldStatus.conflicting, p.budget
    assert any("50" in a for a in p.budget.alternatives) and any("120" in a for a in p.budget.alternatives)
    assert p.timeline.status in (FieldStatus.ambiguous, FieldStatus.conflicting), p.timeline
    names = " ".join(s.name.lower() for s in p.stakeholders)
    assert "marcus" in names and "jenny" in names and "raj" in names
    integ = {(f.value or "").lower(): f.status for f in p.integrations}
    assert any("quickbooks" in k for k in integ) and any("shopify" in k for k in integ)
    shipping = [s for k, s in integ.items() if "shipping" in k]
    assert shipping and shipping[0] != FieldStatus.present
    scope = " ".join((f.value or "").lower() for f in p.out_of_scope)
    assert "schedul" in scope
    assert len(res.report.quotes_removed) <= 3, res.report.quotes_removed
