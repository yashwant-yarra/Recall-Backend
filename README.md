# Recall

A GenAI knowledge app: upload documents, then ask questions and get streamed answers with `[n]` citations back to the source page. Voice notes and a tool-calling agent come next.

**Status:** Phase 1 (document chat) works end to end on Cloud Run. Phase 2 (voice notes) has started.

## How it works

```
Browser ──POST /api/v1/files──▶ api (Cloud Run) ──INSERT pending──▶ Neon Postgres
   │                               │
   │◀──── V4 signed PUT URL ───────┘
   │
   └──PUT file──▶ GCS bucket ──object.finalized──▶ Eventarc ──▶ worker (Cloud Run, private)
                                                                 │ claim row (atomic UPDATE)
                                                                 │ download → extract → chunk
                                                                 │ embed (Vertex, gemini-embedding-001)
                                                                 └ INSERT chunks + status=ready

Browser ──POST /api/v1/ask──▶ api ── hybrid search (pgvector + full-text, RRF) ──▶ Gemini ──SSE──▶ Browser
```

- **api** (`recall.main:app`) creates uploads, searches and answers.
- **worker** (`recall.worker:app`) turns each uploaded file into chunks with embeddings. Both services run from the same image.

## API

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Liveness, no DB. |
| `GET` | `/health/db` | `SELECT 1` against Neon; 503 if unreachable. |
| `POST` | `/api/v1/files` | `{mime_type, size_bytes}` → `{file_id, upload_url, upload_headers}`. PDF, DOCX, TXT and audio; max 25 MiB. |
| `GET` | `/api/v1/search?q=` | Top 8 chunks from hybrid search. |
| `POST` | `/api/v1/ask` | `{question, chat_id?}` → SSE stream: `chat`, `sources`, `token`… The chat and both messages are saved. |

Every query is scoped by `user_id`. Auth is a placeholder (`DEV_USER_ID`, local only), so production fails closed with 401 until real auth lands.

## Design decisions

- **Direct-to-GCS uploads.** The API never handles file bytes. The signed URL fixes `Content-Type`, the exact size (`x-goog-content-length-range`) and create-only (`x-goog-if-generation-match: 0`), so GCS enforces what the database row says. URLs are signed through IAM `signBlob`, so there's no key file.
- **Idempotent worker.** Eventarc delivers at least once. An atomic `UPDATE … WHERE status='pending' RETURNING` claim means only one delivery processes a file. A claim older than a 15-minute lease is treated as a crashed worker and can be reclaimed. A file still under a live claim returns 409 so Eventarc retries. After 3 attempts the file is marked `failed`.
- **All or nothing.** Chunks are inserted and the file set to `ready` in one transaction.
- **Hybrid search.** pgvector cosine (HNSW on `halfvec(768)`) plus Postgres full-text (`tsvector`, OR query), merged with reciprocal rank fusion (`1 / (60 + rank)`).
- **Grounded answers.** The model is told to answer only from numbered sources, cite each claim and treat document text as data, not instructions (a prompt-injection guard).
- **Least privilege.** Each service has its own service account, with grants scoped to one resource. The API can create objects but not read them; the worker can read but not write.
- **Migrations by hand.** Alembic with raw SQL (autogenerate handles `halfvec`, HNSW and `tsvector` badly), run before a deploy, never at startup.

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2 async + psycopg 3 · Neon Postgres + pgvector · Google Cloud Storage · Eventarc · Cloud Run · Vertex AI (`google-genai`) · Alembic · uv

## Run locally

Needs a `.env` with at least `DATABASE_URL`, `GCS_BUCKET`, `GOOGLE_CLOUD_PROJECT` and `DEV_USER_ID`. Run through Docker: psycopg async doesn't work with Windows' default event loop.

```powershell
docker build -t recall-api .
docker run --rm --env-file .env -p 8000:8080 `
  -v "$env:APPDATA\gcloud:/gcloud:ro" `
  -e GOOGLE_APPLICATION_CREDENTIALS=/gcloud/application_default_credentials.json `
  recall-api
```

Credentials are mounted at run time, never baked into the image.

Migrations: `uv run alembic upgrade head`

## Tests and eval

```powershell
uv run pytest -q tests
```

`eval/questions.jsonl` holds 20 questions with the expected page and answer (some expect the model to say it doesn't know). Run it against a live API:

```powershell
uv run python eval/run.py http://localhost:8000
```

It reports hit@1 and hit@3 for retrieval and the answer pass rate, per question type.

### Latest eval (2026-10-31)

Corpus: one 4-page PDF (14 chunks). Search returns the top 8 chunks; `gemini-3.5-flash-lite` answers.

| Type | n | hit@1 | hit@3 | Answer |
| --- | --- | --- | --- | --- |
| keyword | 5 | 5/5 | 5/5 | 5/5 |
| historical | 4 | 4/4 | 4/4 | 4/4 |
| paraphrase | 4 | 2/4 | 2/4 | 4/4 |
| computed | 3 | 2/3 | 3/3 | 3/3 |
| unanswerable | 3 | 3/3 | 3/3 | 2/3 |
| disambiguation | 1 | 1/1 | 1/1 | 1/1 |
| **Total** | **20** | **16/20** | **18/20** | **19/20** |

- **Retrieval is the same on every run.** Paraphrase is the weak spot (the right chunk ranks 5th and 6th): when a question shares few words with the text, full-text search adds little.
- **Answers pass partly because the corpus is small.** 8 of 14 chunks reach the model, so answer accuracy here overstates what a larger corpus would give. hit@3 is the better retrieval measure.
- **Unanswerable questions must start with a fixed sentence** ("I don't know based on your documents."), so grading is an exact check, not a phrase guess. The one miss (#20) has the right meaning but not the sentence.

## Roadmap

1. ~~Setup~~: FastAPI, Neon, Docker, Cloud Run, Secret Manager
2. ~~Document chat~~: signed uploads, extraction worker, chunks + embeddings, hybrid search, cited streaming answers, eval
3. **Voice notes**: faster-whisper transcription, Gemini summary + action items, transcript search
4. **Agent**: hand-written function-calling loop, approval for write tools, per-user quota
5. **Polish**: real auth, frontend (Next.js), rate limits, demo
