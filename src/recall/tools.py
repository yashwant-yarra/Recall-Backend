import asyncio
import json
import selectors
import sys
from types import SimpleNamespace
from uuid import UUID
from datetime import date
from google.genai import types
from sqlalchemy import text

from recall.db import make_engine
from recall.embeddings import make_genai_client
from recall.search import hybrid_search

MAX_FILE_CHARS = 20_000  # cap what one tool call can put into the model's context


def parse_uuid(value) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def search_knowledge(app, user_id: str, args: dict) -> dict:
    hits = await hybrid_search(app, user_id, args["query"])
    return {"results": [{"file_id": str(h["file_id"]), "kind": h["kind"], "page": h["page"],
                         "content": h["content"]} for h in hits]}


async def list_voice_notes(app, user_id: str, args: dict) -> dict:
    limit = min(int(args.get("limit", 10)), 50)
    async with app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT f.id, f.created_at, v.summary, v.topics "
            "FROM files f JOIN voice_notes v ON v.file_id = f.id "
            "WHERE f.user_id = :uid "
            "ORDER BY f.created_at DESC LIMIT :limit"
        ), {"uid": user_id, "limit": limit})).all()
    return {"voice_notes": [{"file_id": str(r.id), "created_at": r.created_at.isoformat(),
                             "summary": r.summary, "topics": r.topics} for r in rows]}


async def get_file_text(app, user_id: str, args: dict) -> dict:
    """Backs both get_document and get_transcript: a file's text, rebuilt from its chunks."""
    file_id = parse_uuid(args.get("file_id"))
    if file_id is None:
        return {"error": "file_id is not a valid UUID"}
    async with app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT c.content FROM chunks c "
            "JOIN files f ON f.id = c.file_id "
            "WHERE c.file_id = :fid AND f.user_id = :uid "
            "ORDER BY c.chunk_no"
        ), {"fid": file_id, "uid": user_id})).all()
    if not rows:
        return {"error": "No such file"}
    text_ = "\n".join(r.content for r in rows)
    return {"file_id": str(file_id), "text": text_[:MAX_FILE_CHARS]}


async def create_task(app, user_id: str, args: dict) -> dict:
    title = str(args.get("title", "")).strip()
    if not title:
        return {"error": "title is required"}
    due = None
    if args.get("due"):
        try:
            due = date.fromisoformat(args["due"])
        except ValueError:
            return {"error": "due must be YYYY-MM-DD"}
    source = None
    if args.get("source_file_id"):
        source = parse_uuid(args["source_file_id"])
        if source is None:
            return {"error": "source_file_id is not a valid UUID"}
    async with app.state.engine.begin() as conn:
        if source is not None:
            row = (await conn.execute(text(
                "SELECT 1 FROM files WHERE id = :fid AND user_id = :uid"
            ), {"fid": source, "uid": user_id})).first()
            if row is None:
                return {"error": "No such file"}
        task_id = (await conn.execute(text(
            "INSERT INTO tasks (user_id, title, due, source_file_id) "
            "VALUES (:uid, :title, :due, :source) RETURNING id"
        ), {"uid": user_id, "title": title, "due": due, "source": source})).scalar_one()
    return {"task_id": str(task_id), "title": title}


async def save_draft(app, user_id: str, args: dict) -> dict:
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", "")).strip()
    if not title or not body:
        return {"error": "title and body are required"}
    async with app.state.engine.begin() as conn:
        draft_id = (await conn.execute(text(
            "INSERT INTO drafts (user_id, title, body) VALUES (:uid, :title, :body) RETURNING id"
        ), {"uid": user_id, "title": title, "body": body})).scalar_one()
    return {"draft_id": str(draft_id), "title": title}


# name -> (function, needs_approval). Write tools change data, so the loop pauses for the user.
# get_document and get_transcript share one implementation: both return a file's text.
TOOLS = {
    "search_knowledge": (search_knowledge, False),
    "get_document": (get_file_text, False),
    "list_voice_notes": (list_voice_notes, False),
    "get_transcript": (get_file_text, False),
    "create_task": (create_task, True),
    "save_draft": (save_draft, True),
}

_FILE_ID = {"type": "string", "description": "The file's UUID, as returned by another tool."}

# What Gemini sees. user_id is never a parameter: the server supplies it.
DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_knowledge",
        description="Hybrid search over the user's documents and voice-note transcripts. "
                    "Returns the most relevant passages with their file_id, kind and page.",
        parameters_json_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for, in natural language."},
        }, "required": ["query"]},
    ),
    types.FunctionDeclaration(
        name="get_document",
        description="Full text of one uploaded document (PDF, DOCX or TXT), up to 20,000 characters.",
        parameters_json_schema={"type": "object", "properties": {"file_id": _FILE_ID},
                                "required": ["file_id"]},
    ),
    types.FunctionDeclaration(
        name="list_voice_notes",
        description="The user's voice notes, newest first, with summary and topics.",
        parameters_json_schema={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many to return (default 10, max 50)."},
        }},
    ),
    types.FunctionDeclaration(
        name="get_transcript",
        description="Full transcript of one voice note.",
        parameters_json_schema={"type": "object", "properties": {"file_id": _FILE_ID},
                                "required": ["file_id"]},
    ),
    types.FunctionDeclaration(
        name="create_task",
        description="Create a to-do task for the user. Needs the user's approval before it runs.",
        parameters_json_schema={"type": "object", "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "due": {"type": "string", "description": "Due date as YYYY-MM-DD, if known."},
            "source_file_id": {"type": "string",
                               "description": "UUID of the document or voice note the task came from."},
        }, "required": ["title"]},
    ),
    types.FunctionDeclaration(
        name="save_draft",
        description="Save a text draft (an email, note or summary) for the user. "
                    "Needs the user's approval before it runs.",
        parameters_json_schema={"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["title", "body"]},
    ),
]


async def main() -> None:
    app = SimpleNamespace(state=SimpleNamespace(engine=make_engine(), genai=make_genai_client()))
    tool, user_id, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    result = await globals()[tool](app, user_id, args)
    print(json.dumps(result, indent=2)[:2000])
    await app.state.engine.dispose()


if __name__ == "__main__":
    # psycopg async needs a selector event loop on Windows (uvicorn's default loop won't work).
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))