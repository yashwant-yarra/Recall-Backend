from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text

from recall import auth
from recall.embeddings import embed

SEARCH_K = 8  # results returned
CANDIDATES = 40  # per method, before fusion

# Reciprocal rank fusion: score = sum of 1 / (60 + rank) over both rankings.
HYBRID_SQL = text("""
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY dist) AS r FROM (
        SELECT id, embedding <=> CAST(:qvec AS halfvec) AS dist
        FROM chunks WHERE user_id = :uid AND (CAST(:fid AS uuid) IS NULL OR file_id = CAST(:fid AS uuid))
        ORDER BY dist LIMIT :n
    ) s
),
fts AS (
    SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM (
        SELECT id, ts_rank_cd(tsv, q) AS rank
        FROM chunks, to_tsquery('english', replace(plainto_tsquery('english', :q)::text, ' & ', ' | ')) AS q
        WHERE user_id = :uid AND (CAST(:fid AS uuid) IS NULL OR file_id = CAST(:fid AS uuid)) AND tsv @@ q
        ORDER BY rank DESC LIMIT :n
    ) s
)
SELECT c.id, c.file_id, c.page, c.content, f.kind, f.display_name AS name,
       COALESCE(1.0 / (60 + vec.r), 0) + COALESCE(1.0 / (60 + fts.r), 0) AS score
FROM vec FULL OUTER JOIN fts USING (id)
JOIN chunks c USING (id)
JOIN files f ON f.id = c.file_id
ORDER BY score DESC
LIMIT :k
""")

router = APIRouter()

async def hybrid_search(app, user_id: str, q: str, file_id: UUID | None = None) -> list[dict]:
    """Top chunks for q among the user's files, or only within file_id when given."""
    [qvec] = await embed(app.state.genai, [q], task_type="RETRIEVAL_QUERY")
    async with app.state.engine.begin() as conn:
        # HNSW returns its nearest candidates first and filters after, so a filter (this user, this
        # file) could leave too few or none. Iterative scans (pgvector >= 0.8) keep scanning until
        # enough rows pass. relaxed_order may return candidates slightly out of order; the SQL
        # re-sorts them by distance. SET LOCAL lasts only for this transaction.
        await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        result = await conn.execute(HYBRID_SQL, {"uid": user_id, "q": q, "qvec": str(qvec), "fid": file_id,
                                                 "n": CANDIDATES, "k": SEARCH_K})
        return [dict(r) for r in result.mappings()]


@router.get("/search")
async def search(request: Request, q: str = Query(min_length=1, max_length=500), file_id: UUID | None = None,
                 user_id: str = Depends(auth.get_current_user_id)):
    return {"results": await hybrid_search(request.app, user_id, q, file_id)}
