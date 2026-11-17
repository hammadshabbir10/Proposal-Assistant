"""Run the whole flow from the command line, without Slack (messages print to the console).
usage:
  python -m scripts.demo_cli upload data/client_transcript.txt
  python -m scripts.demo_cli answer "Use $50k baseline, up to $120k"     (or: answer --tbd)
  python -m scripts.demo_cli say "what did the client say about their deadline?"
  python -m scripts.demo_cli say "make the timeline section more detailed"
  python -m scripts.demo_cli status
State is kept in var/, so you can stop and continue later, exactly like the Slack bot.
"""
import json
import sys
from pathlib import Path

from app.db import repo
from app.graph import build, dispatch
from app.logs import setup_logging
from app.rag.ingest import ingest_proposals

USER, CHANNEL = "UCLI", "DCLI"


def main(argv: list[str]) -> None:
    setup_logging("WARNING")
    ingest_proposals()
    sid = repo.get_or_create_session(USER, CHANNEL).id
    cmd = argv[0] if argv else "status"
    if cmd == "upload":
        path = Path(argv[1])
        t, new = repo.add_transcript(sid, path.name, path.read_text(encoding="utf-8"))
        if not new:
            print("This transcript is already in the session.")
            return
        build.start_draft(sid, t.id)
    elif cmd == "answer":
        text = None if argv[1:] == ["--tbd"] else " ".join(argv[1:])
        if not build.resume_draft(sid, text):
            print("Nothing is waiting for answers.")
    elif cmd == "say":
        print("[routed as]", dispatch.handle_text(sid, " ".join(argv[1:])))
    elif cmd == "approve":
        build.chat(sid, "approve", "approve")
    print(json.dumps(repo.session_summary(sid), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
