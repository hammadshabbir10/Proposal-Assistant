# Proposal Assistant

A Slack bot that turns a client discovery call transcript into a tailored proposal (DOCX), using past proposals as reference. Multi-agent (LangGraph), RAG over a vector database, state kept across sessions.

> Work in progress. Setup steps, architecture and the demo flow are filled in as each part lands.

## Requirements

- Python 3.11+
- A Slack workspace where you can install an app
- A Gemini API key

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
copy .env.example .env            # then fill in the values
```

Put the provided files in `data/`:

```
data/client_transcript.txt         # used for testing; in the demo it is uploaded in Slack
data/proposals/proposal_*.txt      # the 10 past proposals, loaded into the vector database at startup
```
