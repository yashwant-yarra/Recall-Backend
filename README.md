# Recall

A GenAI knowledge backend with three connected features over one shared index:

1. **Document chat (RAG), built for students:** upload PDFs (scanned ones too), pick one, ask doubts and get streamed answers with `[n]` citations back to the page, either as a step-by-step tutor explanation or a strict quick answer. Generate practice quizzes from a document and get them graded.
2. **Voice notes:** upload a recording; it's transcribed, summarized into action items and topics, and indexed next to your documents.
3. **Agent:** give it a goal; a hand-written function-calling loop looks things up with tools, and asks for your approval before it creates a task or saves a draft.

A **Pro subscription** (Razorpay) raises each user's daily limits.

**Status:** all of it works end to end on Cloud Run, behind Firebase Auth. Backend only; there's no frontend.

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
| `GET` | `/me` | The current user's ID and plan (`free` / `pro`). |
| `POST` | `/api/v1/files` | `{mime_type, size_bytes, display_name?}` → `{file_id, upload_url, upload_headers}`. PDF, DOCX, TXT and audio (webm, mp3, m4a, wav); max 25 MiB. |
| `GET` | `/api/v1/files` | The user's files with status, name, preview text and voice-note summary. |
| `GET` | `/api/v1/files/{id}/url` | A 5-minute signed link to view the original (PDF viewer, audio player). |
| `GET` | `/api/v1/files/{id}/text` | Extracted text by page (Word preview, voice-note transcript). |
| `DELETE` | `/api/v1/files/{id}` | Deletes the file, its chunks and voice note, and the stored object. |
| `GET` | `/api/v1/search?q=&file_id=` | Top 8 chunks from hybrid search, optionally within one file, each with `kind`, `page` and `name`. |
| `POST` | `/api/v1/ask` | `{question, chat_id?, file_id?, mode: explain\|answer}` → SSE: `chat`, `sources`, `token`…, `done`. Follow-ups see the last 6 messages. |
| `GET` | `/api/v1/chats`, `/api/v1/chats/{id}/messages` | Chat history, with the document each chat is about. |
| `POST` | `/api/v1/quizzes` | `{file_id, count, kind: mcq\|short\|mixed, topic?}` → questions with pages; answers stay on the server. |
| `POST` | `/api/v1/quizzes/{id}/grade` | `{answers}` → score, feedback, answers and explanations (graded once). |
| `GET` | `/api/v1/quizzes`, `/api/v1/quizzes/{id}` | Quiz history. |
| `POST` | `/api/v1/agent/runs` | `{goal}` → SSE: `run`, `step`…, then `answer`, `approval_needed` or `error`. |
| `POST` | `/api/v1/agent/runs/{id}/approve` | `{approve: true\|false}` → runs or rejects the pending write, then streams the rest of the run. 404 if nothing is pending. |
| `GET` | `/api/v1/agent/runs` | The user's 20 most recent runs. |
| `POST` | `/api/v1/billing/checkout` | Creates a Razorpay subscription for Pro → `{subscription_id, url}` (Razorpay-hosted payment page). |
| `POST` | `/api/v1/billing/webhook` | Called by Razorpay, not users: no login, but the HMAC signature must match. |

Daily limits per user (UTC day, all configurable): free 10 uploads, 40 questions, 5 quizzes, 15 agent runs; Pro 50 uploads, 500 questions, 30 quizzes, 50 agent runs. Over the limit → 429.

## Design decisions

**Uploads and processing**
- **Direct-to-GCS uploads.** The API never handles file bytes. The signed URL fixes `Content-Type`, the exact size (`x-goog-content-length-range`) and create-only (`x-goog-if-generation-match: 0`), so GCS enforces what the database row says. URLs are signed through IAM `signBlob`, so there's no key file.
- **Idempotent worker.** Eventarc delivers at least once (one event did arrive twice in production). An atomic `UPDATE … WHERE status='pending' RETURNING` claim means only one delivery processes a file. A claim older than a 15-minute lease counts as a crashed worker and can be reclaimed; a live claim returns 409 so Eventarc retries; after 3 attempts the file is `failed`.
- **All or nothing.** Chunks, the voice note row and `status = ready` commit in one transaction.

**Voice notes**
- **Gemini Transcribe on Vertex, not faster-whisper.** faster-whisper worked, but needed a model in the image, CPU time and a memory budget. `gemini-3.5-transcribe` is one API call (about 6 s end to end for a short note). The trade-off: on Vertex it returns plain text only, so there are no timestamps; voice notes are cited as a whole, not "at 2:15". Timestamps would need the Gemini Developer API with an API key, which breaks the no-keys design.
- **Structured summaries.** `summarize()` uses a pydantic `response_schema`, so Gemini's output is always valid JSON with `summary`, `action_items` and `topics`.
- **One index.** Voice-note chunks live in the same `chunks` table as documents, so search and `/ask` cover both with no extra code.

**For students**
- **Per-document chat.** Search can be limited to one file. pgvector's HNSW index filters after it finds neighbors, so a strict filter could return nothing; iterative scans (`hnsw.iterative_scan`, pgvector 0.8) keep scanning until enough rows match.
- **Tutor mode.** "Explain" answers step by step in simple words, still citing pages, and puts anything not from the document under "Beyond your document:". "Quick answer" keeps the strict facts-only mode.
- **Quizzes.** Generated with a structured schema (question, options, answer, explanation, page). Answers stay on the server until the student submits, so they can't be read in the browser first. Short answers are graded in one model call per quiz.
- **Scanned PDFs.** When pypdf finds almost no text (under 40 characters per page), Gemini reads the page images instead.
- **Names are for display only.** The client's file name is cleaned (last path part, printable characters, 200 max) and never used in storage paths.

**Search and answers**
- **Hybrid search.** pgvector cosine (HNSW on `halfvec(768)`) plus Postgres full-text (`tsvector`, OR query), merged with reciprocal rank fusion (`1 / (60 + rank)`).
- **Grounded answers.** The model answers only from numbered sources and cites each claim. When the sources lack the specific fact, the answer must start with "I don't know based on your documents." (a fixed sentence, so the eval can check it exactly).

**Agent**
- **Hand-written loop.** Automatic function calling is off; the server runs each tool. Max 8 steps per run.
- **Approval for writes.** `create_task` and `save_draft` pause the run (`awaiting_approval`); nothing waits in memory. A later request rebuilds the conversation from `agent_steps`, including Gemini's thought signatures, and continues. Approval is an atomic claim on the pending step, so a double click can't run a tool twice.
- **Identity never passes through the model.** Tools get `user_id` from the server, never from the model's arguments, so injected text in a document can't make the agent read another user's data. Someone else's file looks like "No such file".
- **Errors the model can fix are results, not crashes.** A bad date or unknown file returns `{"error": …}` and the model corrects itself; system failures fail the run.

**Billing (Razorpay)**
- **The webhook is the only thing that changes a plan.** The payment page's redirect proves nothing (anyone can open a success URL); a request signed with the webhook secret does. The signature is HMAC-SHA256 over the raw body, compared in constant time.
- **At-least-once, out of order.** Each event ID is recorded in `payment_events` in the same transaction as the plan change, so a redelivery is skipped. Events can arrive out of order, so the handler fetches the subscription's current state instead of trusting the event's snapshot.
- **Only the current subscription can downgrade.** A user's abandoned, unpaid link being cancelled can't take Pro away from the subscription they did pay for.
- **No SDK.** Two REST calls with `httpx` (async) and a stdlib `hmac` check. Stripe was built first and swapped out (it's invite-only in India); only `billing.py` and config changed, because "who is Pro" lives in `users.plan`.

**Security and operations**
- **Firebase Auth.** The API verifies ID tokens with `google-auth` (signature, expiry, issuer, audience = this project) and creates the `users` row on first sight. No token → 401; production has no development bypass.
- **Least privilege.** Each service has its own service account, with grants scoped to one resource. The API can create, read and delete objects in the bucket (uploads, previews, deletion: `storage.objectUser` on that bucket only); the worker can only read.
- **Fail fast on bad config.** The bucket name is validated at startup, so a mangled deploy command fails the deploy instead of breaking uploads.
- **Migrations by hand.** Alembic with raw SQL (autogenerate handles `halfvec`, HNSW and `tsvector` badly), run before a deploy, never at startup.
- **Cleanup.** `python -m recall.cleanup` fails uploads never completed after a day and agent runs abandoned mid-stream; `--dry-run` shows the counts and rolls back.

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2 async + psycopg 3 · Neon Postgres + pgvector · Google Cloud Storage · Eventarc · Cloud Run · Vertex AI (`google-genai`: Gemini 3.5, Gemini Transcribe, gemini-embedding-001) · Firebase Auth · Razorpay · Alembic · uv

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

## Deployment

Both services run on Cloud Run in `asia-southeast1`, next to the Neon database, from **one image** tagged with the git commit (never `latest`), so every revision maps to exact code.

| Service | Entry point | Access | Runs as | Current (2026-11-01) |
| --- | --- | --- | --- | --- |
| `recall-api` | `recall.main:app` | public; Firebase token checked in code | own service account: create, read and delete objects in the bucket, read its secrets, call Vertex AI | revision `00021`, image `92b623f` |
| `recall-worker` | `recall.worker:app` | private; only Eventarc (as the worker's service account) can call it | own service account: read objects, call Vertex AI | revision `00008`, image `86c66e3` |

The worker image is older because none of the later commits (billing, limits) changed worker code; the API is redeployed alone in that case.

**Configuration.** Plain settings are env vars (`GCS_BUCKET`, `GOOGLE_CLOUD_PROJECT`, `RAZORPAY_KEY_ID`, `RAZORPAY_PLAN_ID`, any limit overrides like `DAILY_QUESTIONS`). Secrets come from Secret Manager, one grant per secret: `DATABASE_URL`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`. Nothing secret is in the image or the repo.

**Deploying a change**

1. Commit first, so the image tag matches the code.
2. If there's a new migration, apply it **before** deploying (`uv run python -m alembic upgrade head`); migrations only add, so the running revision keeps working.
3. Build once, then deploy whichever services changed:

```powershell
$sha = git rev-parse --short HEAD
gcloud builds submit --tag <region>-docker.pkg.dev/<project>/recall/recall-api:$sha --region=<region> .
gcloud run deploy recall-api --image <region>-docker.pkg.dev/<project>/recall/recall-api:$sha --region <region>
```

A deploy that only changes `--image` keeps every env var, secret and setting from the previous revision. If a new revision fails to start (bad config, a secret it can't read), Cloud Run keeps serving the old one.

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
6. ~~Billing~~: Razorpay Pro subscription, signed idempotent webhooks, plan-aware limits

The browser frontend (Next.js) lives in a separate repo, `Recall-UI`.

Next ideas: a reranker or query rewriting for paraphrase retrieval, a larger eval corpus, scheduled cleanup (Cloud Scheduler), CI/CD with Workload Identity Federation.
