"""Q&A agent: answers the consultant's questions from the transcript and the current draft, using tools.
Read-only: it never changes the proposal."""
from __future__ import annotations

from app import llm
from app.db import repo
from app.rag import store

SYSTEM = """You answer a consultant's questions about a client engagement.
Use the tools: search the transcript for what the client said, and read the current proposal when asked about it.
Rules:
- Quote the client's exact words with the speaker's name and turn number when you report what someone said.
- If the transcript does not cover it, say so plainly; never guess.
- Keep answers short: 2-6 sentences or a few bullets. Plain text, no Markdown headings."""


def answer(question: str, session_id: int) -> str:
    def search_transcript(query: str) -> list[dict]:
        """Search the client call transcript(s) by meaning. Returns passages with speaker turns."""
        return [{"turns": c.section_title, "text": c.text} for c in store.search_transcript(session_id, query, k=4)]

    def list_proposal_sections() -> list[str]:
        """List the current proposal's section keys and titles."""
        p = repo.latest_proposal(session_id)
        return [f"{s.key}: {s.title}" for s in p.sections] if p else ["no proposal yet"]

    def read_proposal_section(key: str) -> str:
        """Return the text of one section of the current proposal, by its key."""
        p = repo.latest_proposal(session_id)
        sec = next((s for s in p.sections if s.key == key), None) if p else None
        return sec.content_md if sec else f"no section '{key}'"

    def client_profile_field(name: str) -> str:
        """Return one extracted client fact with its status and quotes: company, industry, problem, goals, budget,
        timeline, stakeholders, integrations, constraints or out_of_scope."""
        prof = repo.latest_profile(session_id)
        return str(getattr(prof, name, "unknown field")) if prof else "no profile yet"

    history = "\n".join(f"{m.role}: {m.text[:200]}" for m in repo.recent_messages(session_id, 6))
    prompt = f"Recent conversation:\n{history or '(none)'}\n\nQuestion: {question}"
    try:
        r = llm.run_with_tools(prompt, [search_transcript, list_proposal_sections, read_proposal_section, client_profile_field],
                               system=SYSTEM, tier="strong", max_calls=6)
    except llm.LLMError as e:
        repo.log_agent_run("qa", session_id=session_id, error=str(e))
        return "I could not reach the language model just now. Please ask again in a minute."
    repo.log_agent_run("qa", session_id=session_id, model=r.model, tool_calls=r.tool_calls,
                       output={"answer": r.text[:2000]}, latency_ms=r.latency_ms)
    return r.text or "I could not find that in the transcript."
