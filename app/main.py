"""FastAPI app. On startup it loads the past proposals into the vector store (skipping unchanged ones) and starts
the Slack bot over Socket Mode in a background thread. HTTP endpoints are for health checks and inspection.

run:  uvicorn app.main:api --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.config import get_settings
from app.db import repo
from app.logs import log, setup_logging
from app.rag.ingest import ingest_proposals
from app.slack import handlers, notify

state: dict = {}


def start_slack() -> None:
    s = get_settings()
    app = App(token=s.slack_bot_token)
    handlers.register(app)
    notify.set_notifier(notify.SlackNotifier(app.client))
    handler = SocketModeHandler(app, s.slack_app_token)
    state["slack"] = handler
    handler.connect()  # non-blocking; start() would also install signal handlers, which only works on the main thread
    log.info("slack.connected", bot=handlers.app_user_id(app.client))


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    setup_logging(s.log_level)
    state["ingest"] = ingest_proposals()
    log.info("startup.ingest", **state["ingest"])
    start_slack()
    yield
    if state.get("slack"):
        state["slack"].close()


api = FastAPI(title="Proposal Assistant", lifespan=lifespan)


@api.get("/health")
def health() -> dict:
    return {"ok": True, "proposals_indexed": state.get("ingest", {}).get("total_chunks"), "slack": "slack" in state}


@api.get("/sessions/{session_id}")
def session(session_id: int) -> dict:
    s = repo.session_summary(session_id)
    if not s:
        raise HTTPException(404, "no such session")
    p = repo.latest_profile(session_id)
    s["open_issues"] = [{"field": n, "status": f.status.value} for n, f in (p.open_issues() if p else [])]
    return s


@api.post("/ingest")
def reingest(force: bool = False) -> dict:
    return ingest_proposals(force=force)
