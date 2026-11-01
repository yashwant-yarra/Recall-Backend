# Recall

A GenAI knowledge backend with three connected features over one shared index:

1. **Document chat (RAG):** upload PDF, DOCX or TXT, then ask questions and get streamed answers with `[n]` citations back to the source page.
2. **Voice notes:** upload a recording; it's transcribed, summarized into action items and topics, and indexed next to your documents.
3. **Agent:** give it a goal; a hand-written function-calling loop looks things up with tools, and asks for your approval before it creates a task or saves a draft.

**Status:** all three work end to end on Cloud Run, behind Firebase Auth. Backend only; there's no frontend.

## How it works

```
Browser ──POST /api/v1/files──▶ api (Cloud Run) ──INSERT pending──▶ Neon Postgres + pgvector
   │                               │
   │◀──── V4 signed PUT URL ───────┘
   │
   └──PUT file──▶ GCS bucket ──object.finalized──▶ Eventarc ──▶ worker (Cloud Run, private)
                                                                 │ claim row (atomic UPDATE)
                                                                 │ doc:   download → extract → chunk
                                                                 │ audio: download → transcribe → summarize → chunk
                                                                 │ embed (Vertex, gemini-embedding-001)
                                                                 └ one transaction: chunks (+ voice note) + status=ready

Browser ──POST /api/v1/ask────────▶ api ── hybrid search (pgvector + full-text, RRF) ──▶ Gemini ──SSE──▶ Browser
Browser ──POST /api/v1/agent/runs─▶ api ── loop: Gemini picks a tool → run it (reads) or pause for approval (writes) ──SSE──▶ Browser
```

- **api** (`recall.main:app`) handles auth, uploads, search, answers and the agent.
- **worker** (`recall.worker:app`) turns each upload into searchable chunks. Both services run from the same image.

## API

Every `/api/v1` route needs `Authorization: Bearer <Firebase ID token>`; every query is scoped to that user.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Liveness, no DB. |
| `GET` | `/health/db` | `SELECT 1` against Neon; 503 if unreachable. |
| `GET` | `/me` | The current user's ID. |
| `POST` | `/api/v1/files` | `{mime_type, size_bytes}` → `{file_id, upload_url, upload_headers}`. PDF, DOCX, TXT and audio (webm, mp3, m4a, wav); max 25 MiB. |
| `GET` | `/api/v1/search?q=` | Top 8 chunks from hybrid search, each with `kind` (`doc` / `audio`) and `page`. |
| `POST` | `/api/v1/ask` | `{question, chat_id?}` → SSE: `chat`, `sources`, `token`…, `done`. Chat and messages are saved. |
| `POST` | `/api/v1/agent/runs` | `{goal}` → SSE: `run`, `step`…, then `answer`, `approval_needed` or `error`. |
| `POST` | `/api/v1/agent/runs/{id}/approve` | `{approve: true\|false}` → runs or rejects the pending write, then streams the rest of the run. 404 if nothing is pending. |
| `GET` | `/api/v1/agent/runs` | The user's 20 most recent runs. |

Daily limits per user (UTC day, all configurable): 50 uploads, 200 questions, 20 agent runs. Over the limit → 429.

## Design decisions

**Uploads and processing**
- **Direct-to-GCS uploads.** The API never handles file bytes. The signed URL fixes `Content-Type`, the exact size (`x-goog-content-length-range`) and create-only (`x-goog-if-generation-match: 0`), so GCS enforces what the database row says. URLs are signed through IAM `signBlob`, so there's no key file.
- **Idempotent worker.** Eventarc delivers at least once (one event did arrive twice in production). An atomic `UPDATE … WHERE status='pending' RETURNING` claim means only one delivery processes a file. A claim older than a 15-minute lease counts as a crashed worker and can be reclaimed; a live claim returns 409 so Eventarc retries; after 3 attempts the file is `failed`.
- **All or nothing.** Chunks, the voice note row and `status = ready` commit in one transaction.

**Voice notes**
- **Gemini Transcribe on Vertex, not faster-whisper.** faster-whisper worked, but needed a model in the image, CPU time and a memory budget. `gemini-3.5-transcribe` is one API call (about 6 s end to end for a short note). The trade-off: on Vertex it returns plain text only, so there are no timestamps; voice notes are cited as a whole, not "at 2:15". Timestamps would need the Gemini Developer API with an API key, which breaks the no-keys design.
- **Structured summaries.** `summarize()` uses a pydantic `response_schema`, so Gemini's output is always valid JSON with `summary`, `action_items` and `topics`.
- **One index.** Voice-note chunks live in the same `chunks` table as documents, so search and `/ask` cover both with no extra code.

**Search and answers**
- **Hybrid search.** pgvector cosine (HNSW on `halfvec(768)`) plus Postgres full-text (`tsvector`, OR query), merged with reciprocal rank fusion (`1 / (60 + rank)`).
- **Grounded answers.** The model answers only from numbered sources and cites each claim. When the sources lack the specific fact, the answer must start with "I don't know based on your documents." (a fixed sentence, so the eval can check it exactly).

**Agent**
- **Hand-written loop.** Automatic function calling is off; the server runs each tool. Max 8 steps per run.
- **Approval for writes.** `create_task` and `save_draft` pause the run (`awaiting_approval`); nothing waits in memory. A later request rebuilds the conversation from `agent_steps`, including Gemini's thought signatures, and continues. Approval is an atomic claim on the pending step, so a double click can't run a tool twice.
- **Identity never passes through the model.** Tools get `user_id` from the server, never from the model's arguments, so injected text in a document can't make the agent read another user's data. Someone else's file looks like "No such file".
- **Errors the model can fix are results, not crashes.** A bad date or unknown file returns `{"error": …}` and the model corrects itself; system failures fail the run.

**Security and operations**
- **Firebase Auth.** The API verifies ID tokens with `google-auth` (signature, expiry, issuer, audience = this project) and creates the `users` row on first sight. No token → 401; production has no development bypass.
- **Least privilege.** Each service has its own service account, with grants scoped to one resource. The API can create objects but not read them; the worker can read but not write.
- **Fail fast on bad config.** The bucket name is validated at startup, so a mangled deploy command fails the deploy instead of breaking uploads.
- **Migrations by hand.** Alembic with raw SQL (autogenerate handles `halfvec`, HNSW and `tsvector` badly), run before a deploy, never at startup.
- **Cleanup.** `python -m recall.cleanup` fails uploads never completed after a day and agent runs abandoned mid-stream; `--dry-run` shows the counts and rolls back.

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2 async + psycopg 3 · Neon Postgres + pgvector · Google Cloud Storage · Eventarc · Cloud Run · Vertex AI (`google-genai`: Gemini 3.5, Gemini Transcribe, gemini-embedding-001) · Firebase Auth · Alembic · uv

## Run locally

Needs a `.env` with at least `DATABASE_URL`, `GCS_BUCKET` and `GOOGLE_CLOUD_PROJECT`. Locally, `DEV_USER_ID` lets requests without a token act as that user; it's never set in production. Run through Docker: psycopg async doesn't work with Windows' default event loop.

```powershell
docker build -t recall-api .
docker run --rm --env-file .env -p 8000:8080 `
  -v "$env:APPDATA\gcloud:/gcloud:ro" `
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcloud/application_default_credentials.json `
  -e GOOGLE_CLOUD_PROJECT=<project> `
  recall-api
```

Credentials are mounted at run time, never baked into the image.

- Migrations: `uv run python -m alembic upgrade head`
- Try the agent from the terminal: `uv run python -m recall.agent <user_id> "your goal"` (asks y/N for writes)
- Call a tool directly: `uv run python -m recall.tools list_voice_notes <user_id> '{"limit": 5}'`

## Tests and eval

```powershell
uv run pytest -q tests
```

`eval/questions.jsonl` holds 20 questions with the expected page and answer (some expect the model to say it doesn't know). Run it against a live API:

```powershell
uv run python eval/run.py http://localhost:8000
```

It reports hit@1 and hit@3 for retrieval and the answer pass rate, per question type.

### Latest eval (2026-11-01, 3 runs)

Corpus: one 4-page PDF (14 chunks) **plus one voice note** (1 chunk) in the same index. Search returns the top 8 chunks; `gemini-3.5-flash-lite` answers.

| Type | n | hit@1 | hit@3 | Answer (3 runs) |
| --- | --- | --- | --- | --- |
| keyword | 5 | 5/5 | 5/5 | 5, 5, 5 |
| historical | 4 | 4/4 | 4/4 | 4, 4, 4 |
| paraphrase | 4 | 2/4 | 2/4 | 4, 4, 4 |
| computed | 3 | 2/3 | 3/3 | 2, 3, 3 |
| unanswerable | 3 | 3/3 | 3/3 | 2, 2, 1 |
| disambiguation | 1 | 1/1 | 1/1 | 1, 0, 0 |
| **Total** | **20** | **16/20** | **18/20** | **18, 18, 17** |

The PDF-only run on 2026-10-31 gave the same retrieval (16/20, 18/20) and 19/20 answers.

- **Retrieval is deterministic and unchanged by the voice note.** All three runs, and the PDF-only run, rank identically. Paraphrase is the weak spot (the right chunk ranks 5th and 6th): when a question shares few words with the text, full-text search adds little.
- **Answer accuracy varies by a question or two between runs**, so a single run is not a reliable number. Two patterns: the strict refusal rule sometimes makes the model refuse a *computed* question it could answer (#15, total storage); and #18 sometimes misses the exact region code.
- **A voice note can steer an answer.** For #16 the test voice note (itself a question about editor-pack pricing) ranks 8th, and in every run the model also answers *that* question before the real one. It still passes, but voice notes phrased as questions are a risk for a mixed index.
- **Answers pass partly because the corpus is small.** 8 of 15 chunks reach the model, so answer accuracy here overstates what a larger corpus would give. hit@3 is the better retrieval measure.
- **Unanswerable questions must start with a fixed sentence** ("I don't know based on your documents."), so grading is an exact check. #20 always has the right meaning but not the sentence; it's left as is, rather than tuning the prompt to the eval.

## Roadmap

1. ~~Setup~~: FastAPI, Neon, Docker, Cloud Run, Secret Manager
2. ~~Document chat~~: signed uploads, extraction worker, chunks + embeddings, hybrid search, cited streaming answers, eval
3. ~~Voice notes~~: Gemini transcription, structured summary + action items, transcripts in the shared index
4. ~~Agent~~: hand-written function-calling loop, approval for write tools, daily quota
5. ~~Backend polish~~: Firebase Auth, daily limits, cleanup command, config validation

Next ideas: a frontend (Next.js) with in-browser recording, a reranker or query rewriting for paraphrase retrieval, a larger eval corpus, scheduled cleanup (Cloud Scheduler), CI/CD with Workload Identity Federation.
