from fastapi.testclient import TestClient

from recall.main import app

# No `with` block: the lifespan (database, GCS, Gemini) doesn't run. CORS is answered by the
# middleware before any route, so these tests need none of it.
client = TestClient(app)


def preflight(origin: str):
    # What a browser sends before a POST with an Authorization header from another origin.
    return client.options("/api/v1/ask", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })


def test_allowed_origin_preflight():
    r = preflight("http://localhost:3000")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


def test_unknown_origin_gets_no_cors_headers():
    # The browser then blocks the response: another site can't call the API from a user's browser.
    r = preflight("https://evil.example")
    assert "access-control-allow-origin" not in r.headers


def test_simple_request_from_allowed_origin():
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_delete_allowed_from_allowed_origin():
    r = client.options("/api/v1/files/00000000-0000-0000-0000-000000000000", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": "authorization",
    })
    assert r.status_code == 200
    assert "DELETE" in r.headers["access-control-allow-methods"]
