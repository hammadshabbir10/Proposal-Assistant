"""Router agent: decides what a Slack message is asking for. One fast, cheap call with structured output.

File uploads never reach the model (code routes them), and neither do button clicks; only free text does.
"""
from __future__ import annotations

from app import llm
from app.db import repo
from app.schemas import Intent, RouterDecision

SYSTEM = """You route messages for a Slack assistant that turns client call transcripts into proposals.
Pick one intent:
- question: the consultant asks about the client, the transcript or the current draft ("what did they say about...").
- edit: the consultant wants the proposal changed ("make the timeline more detailed", "shorten the summary").
- approve: the consultant accepts the current draft ("looks good, approve it", "final").
- answer_clarification: the assistant asked about missing or conflicting facts and this message supplies them.
  When the assistant is waiting for answers, a message that states facts or decisions (budget, timeline, scope)
  is answer_clarification, not edit.
- chitchat: greetings, thanks, or anything else.
For question and edit, put the section or topic the consultant means in target_hint (e.g. "timeline", "budget")."""


def route(text: str, session_id: int, awaiting_answers: bool) -> RouterDecision:
    s = repo.session_summary(session_id)
    history = "\n".join(f"{m.role}: {m.text[:200]}" for m in repo.recent_messages(session_id, 6))
    prompt = (f"Session: draft version {s.get('proposal_version') or 'none yet'}; "
              f"sections: {', '.join(s.get('sections') or []) or 'none'}; "
              f"assistant is waiting for answers to open questions: {'yes' if awaiting_answers else 'no'}.\n"
              f"Recent conversation:\n{history or '(none)'}\n\nNew message: {text}")
    try:
        r = llm.generate_structured(prompt, RouterDecision, system=SYSTEM, tier="fast", temperature=0)
        decision = r.parsed
        repo.log_agent_run("router", session_id=session_id, model=r.model, output=decision.model_dump(mode="json"),
                           latency_ms=r.latency_ms)
    except llm.LLMError as e:
        # without the model, a question mark is the safest guess: answering is harmless, editing is not
        decision = RouterDecision(intent=Intent.question if "?" in text else Intent.chitchat, reason=f"fallback: {e}"[:200])
        repo.log_agent_run("router", session_id=session_id, error=str(e))
    return decision
