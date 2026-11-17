"""Slack event and button handlers (Bolt, Socket Mode).

Slack expects an answer within 3 seconds, so every handler only does quick checks, then hands the real work to a
background thread. Duplicate deliveries (Slack retries) are dropped by event id.
"""
from __future__ import annotations

import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from docx import Document
from slack_bolt import App

from app.config import get_settings
from app.db import repo
from app.graph import build, dispatch
from app.logs import log

pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="work")
ACCEPTED = {".txt", ".md", ".docx"}
MAX_BYTES = 2_000_000


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {get_settings().slack_bot_token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(MAX_BYTES + 1)


def file_to_text(name: str, data: bytes) -> str:
    if name.lower().endswith(".docx"):
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    return data.decode("utf-8", errors="replace")


def _run(fn, *args):
    def safe():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - nodes already report their own failures; this is the last resort
            log.exception("worker.failed", fn=getattr(fn, "__name__", str(fn)))
    pool.submit(safe)


def handle_files(client, session_id: int, files: list[dict]) -> None:
    for f in files:
        name = f.get("name", "transcript")
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ACCEPTED:
            client.chat_postMessage(channel=repo.get_session(session_id).slack_channel_id,
                                    thread_ts=repo.get_session(session_id).thread_ts,
                                    text=f"I can read .txt, .md and .docx transcripts; *{name}* is a different format.")
            continue
        info = client.files_info(file=f["id"])["file"]
        data = _download(info["url_private_download"])
        if len(data) > MAX_BYTES:
            client.chat_postMessage(channel=repo.get_session(session_id).slack_channel_id,
                                    text=f"*{name}* is larger than 2 MB, which is more than a call transcript should be.")
            continue
        text = file_to_text(name, data)
        t, new = repo.add_transcript(session_id, name, text, slack_file_id=f["id"])
        if not new:
            client.chat_postMessage(channel=repo.get_session(session_id).slack_channel_id,
                                    thread_ts=repo.get_session(session_id).thread_ts,
                                    text=f"*{name}* is already in this session, so I skipped it.")
            continue
        build.start_draft(session_id, t.id)


def register(app: App) -> None:
    def on_message(event: dict, body: dict, client) -> None:
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        if not repo.first_time_seen(body.get("event_id", event.get("client_msg_id", event.get("ts")))):
            return
        user, channel = event["user"], event["channel"]
        thread = event.get("thread_ts") or event.get("ts")
        session = repo.get_or_create_session(user, channel, thread)
        repo.set_session_status(session.id, session.status, thread_ts=thread)
        text = (event.get("text") or "").replace(f"<@{app_user_id(client)}>", "").strip()
        if event.get("files"):
            try:  # a quick "seen it" signal; optional, needs the reactions:write scope
                client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
            except Exception:  # noqa: BLE001
                pass
            _run(handle_files, client, session.id, event["files"])
        elif text:
            _run(dispatch.handle_text, session.id, text)

    @app.event("message")
    def message(event, body, client):
        if event.get("channel_type") == "im":
            on_message(event, body, client)

    @app.event("app_mention")
    def mention(event, body, client):
        on_message(event, body, client)

    @app.event("file_shared")
    def file_shared(event):  # file uploads also arrive as a message with files; that one is handled
        return

    @app.action("generate_tbd")
    def generate_tbd(ack, body, client):
        ack()
        sid = int(body["actions"][0]["value"])
        _run(build.resume_draft, sid, None)

    @app.action("approve_proposal")
    def approve(ack, body):
        ack()
        sid = int(body["actions"][0]["value"])
        _run(build.chat, sid, "approve", "approve")


_bot_user: str | None = None


def app_user_id(client) -> str:
    global _bot_user
    if _bot_user is None:
        _bot_user = client.auth_test()["user_id"]
    return _bot_user
