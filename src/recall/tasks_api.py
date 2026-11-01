"""The student's own tasks and drafts: what the agent created for them, after they approved it.

Read, tick off and delete here. Creating happens only through the agent's approval flow, so there is
no create endpoint. Every query is scoped by user_id, so someone else's id is just a 404.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from recall import auth

router = APIRouter()


class DoneRequest(BaseModel):
    done: bool


@router.get("/tasks")
async def list_tasks(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """Open tasks first (soonest due, undated last), then finished ones. At most 200."""
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT t.id, t.title, t.due, t.done, t.source_file_id, f.display_name, t.created_at "
            "FROM tasks t LEFT JOIN files f ON f.id = t.source_file_id WHERE t.user_id = :uid "
            "ORDER BY t.done, t.due NULLS LAST, t.created_at DESC LIMIT 200"
        ), {"uid": user_id})).all()
    return {"tasks": [{
        "id": str(r.id), "title": r.title, "due": r.due.isoformat() if r.due else None, "done": r.done,
        "file_id": str(r.source_file_id) if r.source_file_id else None, "file_name": r.display_name,
        "created_at": r.created_at.isoformat(),
    } for r in rows]}


@router.post("/tasks/{task_id}/done", status_code=204)
async def set_task_done(task_id: UUID, body: DoneRequest, request: Request,
                        user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.begin() as conn:
        updated = (await conn.execute(text(
            "UPDATE tasks SET done = :done WHERE id = :id AND user_id = :uid RETURNING id"
        ), {"done": body.done, "id": task_id, "uid": user_id})).scalar_one_or_none()
    if updated is None:
        raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.begin() as conn:
        deleted = (await conn.execute(text(
            "DELETE FROM tasks WHERE id = :id AND user_id = :uid RETURNING id"
        ), {"id": task_id, "uid": user_id})).scalar_one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Task not found")


@router.get("/drafts")
async def list_drafts(request: Request, user_id: str = Depends(auth.get_current_user_id)):
    """The 100 newest drafts, with their full text."""
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, title, body, created_at FROM drafts WHERE user_id = :uid "
            "ORDER BY created_at DESC LIMIT 100"
        ), {"uid": user_id})).all()
    return {"drafts": [{"id": str(r.id), "title": r.title, "body": r.body,
                        "created_at": r.created_at.isoformat()} for r in rows]}


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(draft_id: UUID, request: Request, user_id: str = Depends(auth.get_current_user_id)):
    async with request.app.state.engine.begin() as conn:
        deleted = (await conn.execute(text(
            "DELETE FROM drafts WHERE id = :id AND user_id = :uid RETURNING id"
        ), {"id": draft_id, "uid": user_id})).scalar_one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Draft not found")
