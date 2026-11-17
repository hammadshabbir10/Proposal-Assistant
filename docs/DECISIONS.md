# Decisions, trade-offs and what is unfinished

This is a short account of how the system is built, why, and where it falls short. Everything here is what the code does today. Where something is unfinished, I say so and describe how I would finish it.

## 1. The shape of the system

**Six agents, each with one job** (Router, Intake, Research, Writer, Q&A, Editor), connected by typed contracts in [app/schemas.py](../app/schemas.py). I split by *responsibility*, not by prompt size. Each agent can be tested and retried on its own, and each has the right tools and model tier for its job.

**LangGraph for orchestration** ([app/graph/build.py](../app/graph/build.py)). The two things the brief needs most, persistence and a human-in-the-loop pause, are built into LangGraph: the SQLite checkpointer saves every step, and `interrupt()` stops a run until the consultant answers, even across a restart. A hand-written loop would have meant building checkpoint-and-resume myself.

**Two threads per session** (`draft` and `chat`). If questions ran on the same thread as the paused draft, asking "what did they say about the deadline?" while the draft waits for answers would cancel the pause. Separate threads keep both alive.

**Large data lives in tables, not in graph state.** Profiles, research packs and proposal versions are SQLite rows ([app/db/models.py](../app/db/models.py)). Graph state holds only IDs and routing flags. This keeps checkpoints small, lets me query history directly (`/sessions/{id}`), and means a failed graph run never loses saved work.

**The DOCX is rendered by code, not by the model** ([app/docx/render.py](../app/docx/render.py)). The model writes Markdown per section; a deterministic renderer turns it into headings, lists, real tables and highlighted open items. Formatting is then consistent and testable.

## 2. Not guessing: the core requirement

The brief says the system must flag missing or unclear information rather than guess. I treated this as something to **enforce in code**, not only to ask for in a prompt:

- **Evidence or nothing.** Intake must back each fact with exact quotes. Code fuzzy-matches each quote against the transcript (threshold 90). A quote that cannot be found is removed; a field left with no evidence becomes `missing`. Speaker and turn number are taken from the transcript, never from the model. ([app/agents/intake.py](../app/agents/intake.py), `verify()`)
- **Conflicts are decided by a rule.** If two different people are quoted with different values, the field is `conflicting`, even when the model proposed a compromise. On the sample transcript this is what catches the $50k (CEO) vs up to $120k (Operations Manager) budget.
- **The Writer may not resolve what the client did not.** Conflicting facts are written with both values, missing ones as `[TBD: …]`, and prices or dates from other clients' proposals may not be reused ([writer_section.md](../app/agents/prompts/writer_section.md)). During testing the Writer once invented a $2,500/month support fee taken from a past proposal; the rule was added and the Writer test now checks for it.
- **The consultant closes the gaps, not the model.** The draft pauses and asks. Answers given in Slack are stored as facts with the quote *"Consultant (Slack)"*, so it is always visible which facts came from the call and which from the consultant.

## 3. Is anything hardcoded?

No client data is hardcoded. Searching `app/` for the sample client's names, people, tools or amounts finds nothing; all of it comes from the uploaded transcript. What is fixed in code, on purpose:

| Fixed in code | Why | Where |
|---|---|---|
| The 6 required section types (cover, executive summary, proposed solution, timeline, budget, next steps) | The brief requires them; code checks they are present | `REQUIRED_SECTION_TYPES` in [app/schemas.py](../app/schemas.py) |
| The list of section types Research may search by | They are the metadata labels in the vector store | `SEARCHABLE`, `MUST_COVER` in [app/agents/research.py](../app/agents/research.py) |
| A keyword map for section labels | Used **only** if the LLM labelling fails at startup | `_FALLBACK` in [app/rag/ingest.py](../app/rag/ingest.py) |
| Industry per past proposal | Taken from the file name (`proposal_<industry>.txt`) | [app/rag/ingest.py](../app/rag/ingest.py) |

Everything else is decided by the model at run time and checked by code: the outline (titles, order, extra sections) is reasoned from the closest past proposals and differed between test runs; which industries count as comparable is chosen by the Research agent; section labels for past proposals come from an LLM pass over their content.

## 4. Retrieval choices

- **Chunk by structure, not by size.** Proposals are split by section so a chunk never mixes budget and timeline; transcripts by speaker turn so a quote is never cut. Each proposal chunk is embedded with a context line (client, industry, section).
- **Section labels by meaning.** Headings differ between proposals ("BUDGET" vs "TIMELINE AND COST"), so an LLM labels each section with the types it covers, cached by file hash.
- **Filter, then fall back.** Search filters by industry and section type. If that leaves too few good results (score below 0.35), it widens to all industries while keeping the section filter; a close match from a neighbouring industry is better than nothing. MMR then removes near-duplicates.
- **Chroma over pgvector** for the assessment: no server to run, metadata filters, stored on disk. For production I would use Postgres with pgvector, so vectors, sessions and proposals live in one database with transactions and joins.

## 5. Reliability

The LLM provider was the least reliable part during development: models returned 404 (retired), 503 (overloaded) or hung. [app/llm.py](../app/llm.py) handles this:
- a chain of models, tried in order, with a 5-minute cooldown on a model that just failed;
- the SDK's own hidden retries turned off (they caused waits of several minutes);
- a 45 s timeout, and one more pass over the chain after 20 s if every model was busy;
- a section that still fails gets a second attempt, then a visible placeholder, so the consultant gets a document rather than an error.

Fallback models are weaker and sometimes dropped Markdown line breaks; a normaliser repairs that before saving.

## 6. What is not finished, and how I would finish it

| Gap | Effect today | How I would finish it |
|---|---|---|
| **Q&A quotes are not verified** | Intake's quotes are checked against the transcript; Q&A's are not. Q&A is told to quote exactly and uses transcript search, but a paraphrase could be shown in quotation marks. | Run Q&A's answer through the same `verify()` matcher: extract quoted spans, check each against the session's transcripts, and remove or mark any that do not match. |
| **The Editor cannot add or remove sections** | The edit plan may only name existing section keys, so "add a risks section" can only be met by expanding an existing section, or the bot asks which section to change. | Add `add_section` / `remove_section` operations to `EditPlan`, write the new section with the Writer routine, insert it at the planned position, and keep the required-type check. |
| **Repeated TBDs** | The same open item (e.g. the shipping software) can appear as `[TBD]` in several sections. | Collect all TBDs after writing, put them in one "Open items" section, and refer to it from the other sections. |
| **Single process** | Work runs on a thread pool inside the web process. A crash mid-draft loses that run (saved data and the pause are kept). | Move graph runs to a job queue (e.g. Celery/RQ or a Postgres-backed queue) with workers; the checkpointer already allows resuming from the last completed step. |
| **Socket Mode** | Fine for one workspace and a demo. | Switch to Slack's HTTP mode behind a public endpoint with request signing for production. |
| **No access control** | Anyone who can message the bot can use it; sessions are per user and channel. | Restrict to allowed workspaces/users, and add per-client permissions if several consultants share the bot. |
| **No evaluation set** | Quality was checked by tests on one transcript and by hand in Slack. | Build a small set of transcripts with expected statuses and section facts, and score each change against it (quote accuracy, TBD correctness, no borrowed prices). |
| **Bot cannot start a DM itself** | The app has no `users:read` or `im:write` scope, so the consultant opens the DM. | Add both scopes if the bot should message people first. |

## 7. Known behaviour worth stating

- Integration tests use a live LLM, so wording changes between runs; tests check facts and structure, not text.
- A full draft takes a few minutes, mostly LLM time (one call per section, four in parallel). The bot posts progress messages while it works.
- Restarting the bot keeps everything: transcripts, profile, answers, every proposal version and the pause, which were all verified in Slack.
