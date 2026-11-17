"""How graph nodes talk to the consultant. Nodes call say()/upload(); the active notifier decides where it goes:
Slack in production, the console in tests and the CLI demo. Keeps the graph free of Slack details."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.db import repo
from app.logs import log


class Notifier(Protocol):
    def say(self, session_id: int, text: str, blocks: list | None = None) -> None: ...
    def upload(self, session_id: int, path: Path, title: str, comment: str) -> str | None: ...


class ConsoleNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.files: list[Path] = []

    def say(self, session_id: int, text: str, blocks: list | None = None) -> None:
        self.messages.append(text)
        print(f"[bot → session {session_id}] {text}")

    def upload(self, session_id: int, path: Path, title: str, comment: str) -> str | None:
        self.files.append(path)
        print(f"[bot → session {session_id}] FILE {path} — {comment}")
        return None


class SlackNotifier:
    def __init__(self, client) -> None:
        self.client = client

    def _where(self, session_id: int) -> tuple[str, str | None]:
        s = repo.get_session(session_id)
        return s.slack_channel_id, s.thread_ts

    def say(self, session_id: int, text: str, blocks: list | None = None) -> None:
        channel, thread = self._where(session_id)
        try:
            self.client.chat_postMessage(channel=channel, thread_ts=thread, text=text, blocks=blocks)
        except Exception as e:  # noqa: BLE001 - a failed post must not crash the graph run
            log.error("slack.post_failed", error=str(e)[:200])

    def upload(self, session_id: int, path: Path, title: str, comment: str) -> str | None:
        channel, thread = self._where(session_id)
        try:
            r = self.client.files_upload_v2(channel=channel, thread_ts=thread, file=str(path), filename=path.name,
                                            title=title, initial_comment=comment)
            f = r.get("file") or (r.get("files") or [{}])[0]
            return f.get("id")
        except Exception as e:  # noqa: BLE001
            log.error("slack.upload_failed", error=str(e)[:200])
            self.say(session_id, f"I generated {path.name} but could not upload it to Slack ({e}).")
            return None


_active: Notifier = ConsoleNotifier()


def set_notifier(n: Notifier) -> None:
    global _active
    _active = n


def get() -> Notifier:
    return _active
