"""Turn an incoming text message into the right graph run. Shared by the Slack handlers and the CLI."""
from __future__ import annotations

from app.agents import router
from app.db import repo
from app.graph import build
from app.schemas import Intent


def handle_text(session_id: int, text: str) -> str:
    """Route a free-text message. Returns the intent that was acted on (for logging and tests)."""
    repo.add_message(session_id, "user", text)
    waiting = build.awaiting_answers(session_id)
    d = router.route(text, session_id, waiting)
    # While the draft is paused for answers, anything that is not a question is treated as the answers:
    # the consultant is replying to what we asked, even if it reads like an instruction.
    if waiting and d.intent != Intent.question:
        build.resume_draft(session_id, text)
        return "answer_clarification"
    if d.intent == Intent.answer_clarification:
        d.intent = Intent.edit if repo.latest_proposal(session_id) else Intent.chitchat
    build.chat(session_id, d.intent.value, text, d.target_hint)
    return d.intent.value
