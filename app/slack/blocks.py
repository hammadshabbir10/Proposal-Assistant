"""Block Kit messages: the intake summary (with the human-in-the-loop choice) and the draft delivery buttons."""
from __future__ import annotations

from app.schemas import ClientProfile, FieldStatus

ICON = {FieldStatus.present: "✅", FieldStatus.ambiguous: "⚠️", FieldStatus.conflicting: "⚠️", FieldStatus.missing: "❓"}


def intake_summary(profile: ClientProfile, questions: list[tuple[str, FieldStatus, str]], session_id: int) -> list:
    lines = []
    for name in ("company", "industry", "problem", "goals", "budget", "timeline"):
        f = getattr(profile, name)
        val = f.value or "not stated"
        if f.status in (FieldStatus.conflicting, FieldStatus.ambiguous) and f.alternatives:
            val = " vs ".join(f.alternatives)
        lines.append(f"{ICON[f.status]} *{name.title()}* ({f.status.value}): {val[:180]}")
    people = ", ".join(f"{s.name}" + (f" ({s.role})" if s.role else "") for s in profile.stakeholders) or "none found"
    lines.append(f"👥 *Stakeholders*: {people}")
    integ = ", ".join(f"{f.value} ({f.status.value})" for f in profile.integrations if f.value)
    if integ:
        lines.append(f"🔌 *Integrations*: {integ}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Here is what I took from the transcript:*\n" + "\n".join(lines)}}]
    if questions:
        qs = "\n".join(f"{i}. {q}" for i, (_, _, q) in enumerate(questions, 1))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Before I draft, a few things to confirm:*\n{qs}"}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       "Reply in this thread with the answers, or let me draft now with these marked *TBD / to be confirmed*."}]})
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Draft now, mark gaps TBD"}, "style": "primary",
             "action_id": "generate_tbd", "value": str(session_id)}]})
    return blocks


def draft_delivered(version: int, session_id: int, note: str) -> list:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": note}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
         "Ask me anything about the client, or tell me what to change (for example: _make the timeline more detailed_)."}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"Approve v{version}"}, "style": "primary",
             "action_id": "approve_proposal", "value": str(session_id)}]},
    ]
