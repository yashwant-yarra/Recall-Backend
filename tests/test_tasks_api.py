from fastapi import FastAPI
from fastapi.testclient import TestClient

from recall import auth, tasks_api


def client():
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: "test-user"
    return TestClient(app)


def test_routes_exist_and_validate_input():
    c = client()
    # A malformed id or body is rejected before any database access.
    assert c.delete("/tasks/not-a-uuid").status_code == 422
    assert c.delete("/drafts/not-a-uuid").status_code == 422
    assert c.post("/tasks/3f2b1c0e-1111-4111-8111-111111111111/done", json={}).status_code == 422
    assert c.post("/tasks/3f2b1c0e-1111-4111-8111-111111111111/done", json={"done": "maybe"}).status_code == 422
