"""LangGraph orchestration.

Two flows share one compiled graph and one SQLite checkpointer, but run on separate threads per session:
- draft thread  "s<id>:draft": ingest -> intake -> present_gaps -> [await_answers: interrupt] -> research -> write -> deliver
- chat thread   "s<id>:chat" : dispatch -> qa | edit -> deliver | approve -> deliver | chitchat
Separate threads let the consultant ask a question while the draft flow is paused for their answers, without
cancelling that pause. The checkpointer keeps a paused draft across restarts, so an answer given the next day
resumes the same run. Large objects (profile, research pack, proposal) live in SQLite tables, not graph state.
"""
from __future__ import annotations

import sqlite3
import threading
from functools import lru_cache, wraps
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents import editor, intake, qa, research, writer
from app.config import get_settings
from app.db import repo
from app.docx.render import render_from_db
from app.logs import log
from app.rag import store
from app.slack import blocks, notify


class GraphState(TypedDict, total=False):
    session_id: int
    flow: Literal["draft", "chat"]
    transcript_id: int
    intent: str
    text: str
    target_hint: str | None
    note: str
    failed: bool


def guarded(name: str):
    """Turn any failure inside a node into a clear message to the consultant and stop the run.
    The pause raised by interrupt() is not a failure, so it passes through."""
    def deco(fn):
        @wraps(fn)
        def run(state: GraphState):
            try:
                return fn(state)
            except GraphInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("graph.node_failed", node=name)
                repo.log_agent_run(name, session_id=state.get("session_id"), error=f"{type(e).__name__}: {e}"[:500])
                notify.get().say(state["session_id"], f"Something went wrong while {name.replace('_', ' ')}: {e}. "
                                 "Your transcript and any earlier draft are saved; send the same request again to retry.")
                return {"failed": True}
        return run
    return deco


def _client_name(session_id: int) -> str:
    p = repo.latest_profile(session_id)
    return (p.company.value if p and p.company.value else "Client")


# ---------- draft flow ----------

@guarded("reading the transcript")
def ingest(state: GraphState):
    sid = state["session_id"]
    t = next(t for t in repo.list_transcripts(sid) if t.id == state["transcript_id"])
    n = store.add_transcript(sid, t.id, t.raw_text)
    count = len(repo.list_transcripts(sid))
    extra = f" It is transcript {count} in this session, so I will read them together." if count > 1 else ""
    notify.get().say(sid, f"Got *{t.filename}* ({n} passages indexed).{extra} Reading it now…")
    return {}


@guarded("extracting the client details")
def run_intake(state: GraphState):
    sid = state["session_id"]
    res = intake.run_intake([t.raw_text for t in repo.list_transcripts(sid)], session_id=sid)
    note = (f"{res.report.quotes_checked} quotes checked against the transcript"
            + (f", {len(res.report.quotes_removed)} unverified ones removed" if res.report.quotes_removed else ""))
    return {"note": note}


@guarded("summarising what I found")
def present_gaps(state: GraphState):
    sid = state["session_id"]
    profile = repo.latest_profile(sid)
    questions = intake.clarification_questions(profile)
    notify.get().say(sid, "Here is what I took from the transcript.", blocks=blocks.intake_summary(profile, questions, sid))
    repo.set_session_status(sid, "awaiting_clarification" if questions else "drafting")
    return {}


def has_gaps(state: GraphState) -> str:
    if state.get("failed"):
        return "stop"
    profile = repo.latest_profile(state["session_id"])
    return "ask" if profile and profile.open_issues() else "research"


@guarded("applying your answers")
def await_answers(state: GraphState):
    sid = state["session_id"]
    reply = interrupt({"waiting_for": "answers to open questions"})  # pauses here until resume_draft()
    answers = (reply or {}).get("answers")
    if answers:
        intake.apply_consultant_answers(repo.latest_profile(sid), answers, session_id=sid)
        notify.get().say(sid, "Thanks, I have updated the client details. Drafting now…")
    else:
        notify.get().say(sid, "Drafting now, with open items marked *TBD* or *to be confirmed*…")
    repo.set_session_status(sid, "drafting")
    return {}


@guarded("researching past proposals")
def run_research(state: GraphState):
    sid = state["session_id"]
    pack = research.run_research(repo.latest_profile(sid), session_id=sid)
    repo.save_pack(sid, pack)
    closest = ", ".join(list(pack.reference_outlines)[:3]) or "general references"
    notify.get().say(sid, f"Found relevant past proposals ({closest}). Writing the sections…")
    return {}


@guarded("writing the proposal")
def run_writer(state: GraphState):
    sid = state["session_id"]
    profile, pack = repo.latest_profile(sid), repo.latest_pack(sid)
    outline, drafts = writer.generate(profile, pack, session_id=sid)
    p = repo.create_proposal(sid, outline, drafts, change_note="First draft")
    repo.set_session_status(sid, "drafted")
    return {"note": f"Here is draft *v{p.version}* ({len(outline.sections)} sections). {outline.rationale}"}


@guarded("building the document")
def deliver(state: GraphState):
    sid = state["session_id"]
    p = repo.latest_proposal(sid)
    client = _client_name(sid)
    path = render_from_db(p, client)
    fid = notify.get().upload(sid, path, f"Proposal for {client} v{p.version}", f"Proposal v{p.version} ({p.status})")
    repo.set_proposal_file(p.id, str(path), fid)
    if p.status != "approved":
        notify.get().say(sid, state.get("note") or f"Draft v{p.version} is ready.",
                         blocks=blocks.draft_delivered(p.version, sid, state.get("note") or f"Draft v{p.version} is ready."))
    repo.add_message(sid, "assistant", state.get("note") or f"Delivered v{p.version}")
    return {}


# ---------- chat flow ----------

@guarded("answering your question")
def run_qa(state: GraphState):
    sid = state["session_id"]
    ans = qa.answer(state["text"], sid)
    notify.get().say(sid, ans)
    repo.add_message(sid, "assistant", ans)
    return {}


@guarded("editing the proposal")
def run_edit(state: GraphState):
    sid = state["session_id"]
    notify.get().say(sid, "Working on that change…")
    res = editor.apply_edit(state["text"], sid, state.get("target_hint"))
    if res.proposal is None:
        notify.get().say(sid, res.reply)
        repo.add_message(sid, "assistant", res.reply)
        return {"failed": True}  # nothing to deliver
    return {"note": res.reply}


@guarded("approving the proposal")
def run_approve(state: GraphState):
    sid = state["session_id"]
    p = repo.latest_proposal(sid)
    if p is None:
        notify.get().say(sid, "There is no draft to approve yet.")
        return {"failed": True}
    repo.approve_proposal(p.id)
    repo.set_session_status(sid, "approved")
    notify.get().say(sid, f"Approved v{p.version}. Here is the final document.")
    return {"note": f"Approved v{p.version}"}


@guarded("replying")
def chitchat(state: GraphState):
    sid = state["session_id"]
    msg = ("Upload a client call transcript (.txt, .md or .docx) and I will draft a proposal. Once there is a draft, "
           "ask me about the client or tell me what to change.")
    if repo.latest_proposal(sid):
        msg = "Ask me anything about the client, tell me what to change in the draft, or click Approve when it is final."
    notify.get().say(sid, msg)
    return {}


def start_route(state: GraphState) -> str:
    if state["flow"] == "draft":
        return "ingest"
    return {"question": "qa", "edit": "edit", "approve": "approve"}.get(state.get("intent", ""), "chitchat")


def ok_or_stop(next_node: str):
    return lambda s: END if s.get("failed") else next_node


def build_graph(checkpointer):
    g = StateGraph(GraphState)
    for name, fn in [("ingest", ingest), ("intake", run_intake), ("present_gaps", present_gaps),
                     ("await_answers", await_answers), ("research", run_research), ("write", run_writer),
                     ("deliver", deliver), ("qa", run_qa), ("edit", run_edit), ("approve", run_approve),
                     ("chitchat", chitchat)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, start_route, ["ingest", "qa", "edit", "approve", "chitchat"])
    g.add_conditional_edges("ingest", ok_or_stop("intake"), ["intake", END])
    g.add_conditional_edges("intake", ok_or_stop("present_gaps"), ["present_gaps", END])
    g.add_conditional_edges("present_gaps", has_gaps, {"ask": "await_answers", "research": "research", "stop": END})
    g.add_conditional_edges("await_answers", ok_or_stop("research"), ["research", END])
    g.add_conditional_edges("research", ok_or_stop("write"), ["write", END])
    g.add_conditional_edges("write", ok_or_stop("deliver"), ["deliver", END])
    g.add_conditional_edges("edit", ok_or_stop("deliver"), ["deliver", END])
    g.add_conditional_edges("approve", ok_or_stop("deliver"), ["deliver", END])
    for n in ("deliver", "qa", "chitchat"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)


@lru_cache
def graph():
    path = get_settings().db_path.parent / "graph.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return build_graph(SqliteSaver(conn))


# ---------- entry points used by Slack (and the CLI) ----------

_locks: dict[int, threading.Lock] = {}


def _lock(session_id: int) -> threading.Lock:
    return _locks.setdefault(session_id, threading.Lock())


def _cfg(session_id: int, flow: str) -> dict:
    return {"configurable": {"thread_id": f"s{session_id}:{flow}"}}


def awaiting_answers(session_id: int) -> bool:
    st = graph().get_state(_cfg(session_id, "draft"))
    return bool(st and st.next and "await_answers" in st.next)


def start_draft(session_id: int, transcript_id: int) -> None:
    with _lock(session_id):
        graph().invoke({"session_id": session_id, "flow": "draft", "transcript_id": transcript_id, "failed": False,
                        "note": ""}, _cfg(session_id, "draft"))


def resume_draft(session_id: int, answers: str | None) -> bool:
    """Continue a paused draft with the consultant's answers (or None = draft with gaps marked TBD)."""
    with _lock(session_id):
        if not awaiting_answers(session_id):
            return False
        graph().invoke(Command(resume={"answers": answers}), _cfg(session_id, "draft"))
        return True


def chat(session_id: int, intent: str, text: str, target_hint: str | None = None) -> None:
    with _lock(session_id):
        graph().invoke({"session_id": session_id, "flow": "chat", "intent": intent, "text": text,
                        "target_hint": target_hint, "failed": False, "note": ""}, _cfg(session_id, "chat"))
