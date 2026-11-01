from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from recall import auth
from recall.agent import create_run, resolve_approval, run_agent
from recall.ask import sse

router = APIRouter()


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)


class ApproveRequest(BaseModel):
    approve: bool


def stream(events):
    """Wrap an async iterator of event dicts as an SSE response."""
    async def body():
        async for event in events:
            yield sse(event["type"], event)
    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/agent/runs")
async def start_run(body: RunRequest, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    # Anything that should fail with a real HTTP status must happen before the stream starts:
    # once StreamingResponse begins, the 200 and headers are already sent.
    run_id = await create_run(request.app.state.engine, user_id, body.goal)
    if run_id is None:
        raise HTTPException(status_code=429, detail="Daily agent run limit reached for your plan")

    async def events():
        yield {"type": "run", "run_id": str(run_id)}
        async for event in run_agent(request.app, run_id, user_id):
            yield event

    return stream(events())


@router.post("/agent/runs/{run_id}/approve")
async def approve(run_id: UUID, body: ApproveRequest, request: Request,
                  user_id: str = Depends(auth.get_current_user_id)):
    # resolve_approval only matches runs owned by user_id, so someone else's run_id looks like
    # "nothing pending" (404) and reveals nothing about whether that run exists.
    first = await resolve_approval(request.app, run_id, user_id, body.approve)
    if first is None:
        raise HTTPException(status_code=404, detail="No pending approval")

    async def events():
        yield first
        if first["type"] == "error":
            return
        async for event in run_agent(request.app, run_id, user_id):
            yield event

    return stream(events())


@router.get("/agent/runs")
async def list_runs(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, goal, status, answer, error, created_at FROM agent_runs "
            "WHERE user_id = :uid AND goal <> '' ORDER BY created_at DESC LIMIT 20"
        ), {"uid": user_id})).all()
    return {"runs": [{"run_id": str(r.id), "goal": r.goal, "status": r.status, "answer": r.answer,
                      "error": r.error, "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/agent/runs/{run_id}")
async def get_run(run_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """One run with its steps, so a past run can be reviewed, or approved if it's still waiting."""
    async with request.app.state.engine.connect() as conn:
        run = (await conn.execute(text(
            "SELECT id, goal, status, answer, error, created_at FROM agent_runs WHERE id = :id AND user_id = :uid AND goal <> ''"
        ), {"id": run_id, "uid": user_id})).first()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        steps = (await conn.execute(text(
            "SELECT step_no, tool, args, output, status FROM agent_steps WHERE run_id = :id ORDER BY step_no"
        ), {"id": run_id})).all()
    return {"run_id": str(run.id), "goal": run.goal, "status": run.status, "answer": run.answer,
            "error": run.error, "created_at": run.created_at.isoformat(),
            "steps": [{"step_no": s.step_no, "tool": s.tool, "args": s.args, "output": s.output,
                       "status": s.status} for s in steps]}


@router.delete("/agent/runs/{run_id}", status_code=204)
async def delete_run(run_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.begin() as conn:
        # Keep a content-free row for the daily run quota; an empty goal cannot be created via the API.
        deleted = (await conn.execute(text(
            "UPDATE agent_runs SET goal = '', answer = NULL, error = NULL "
            "WHERE id = :id AND user_id = :uid AND goal <> '' "
            "AND status IN ('done', 'failed') RETURNING id"
        ), {"id": run_id, "uid": user_id})).scalar_one_or_none()
        if deleted is None:
            status = (await conn.execute(text(
                "SELECT status FROM agent_runs WHERE id = :id AND user_id = :uid AND goal <> ''"
            ), {"id": run_id, "uid": user_id})).scalar_one_or_none()
            if status is None:
                raise HTTPException(status_code=404, detail="Run not found")
            raise HTTPException(status_code=409, detail="Only completed or failed runs can be deleted")
        await conn.execute(text("DELETE FROM agent_steps WHERE run_id = :id"), {"id": run_id})
    return Response(status_code=204)
