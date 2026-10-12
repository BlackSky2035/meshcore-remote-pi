"""Anmeldung: offene Pfade, Schutz, Sitzung, Sperre nach Fehlversuchen, WebSocket."""
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from app import auth as A
from app.main import create_app


@pytest.fixture
async def secured(rt):
    rt.settings.access_password = "geheim"
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


async def test_no_password_means_open(client):
    assert (await client.get("/api/contacts")).status_code == 200
    assert (await client.get("/api/auth/status")).json() == {"required": False, "logged_in": True}


async def test_protected_and_open_paths(secured):
    c, _ = secured
    assert (await c.get("/api/contacts")).status_code == 401
    assert (await c.put("/api/node/settings", json={"changes": {"name": "x"}})).status_code == 401
    for ok in ("/api/health", "/api/version", "/api/auth/status", "/", "/static/app.js", "/sw.js"):
        assert (await c.get(ok)).status_code == 200, ok
    assert (await c.get("/api/versionx")).status_code == 401


async def test_login_flow_and_logout(secured):
    c, _ = secured
    assert (await c.post("/api/auth/login", json={"password": "falsch"})).status_code == 401
    r = await c.post("/api/auth/login", json={"password": "geheim"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert (await c.get("/api/contacts")).status_code == 200
    assert (await c.get("/api/auth/status")).json()["logged_in"]
    await c.post("/api/auth/logout")
    c.cookies.clear()
    assert (await c.get("/api/contacts")).status_code == 401


async def test_forged_or_expired_token(rt):
    a = A.Auth("pw", clock=lambda: 1000.0)
    assert a.valid(a.make_token())
    assert not a.valid("9999999999.deadbeef") and not a.valid("") and not a.valid(None) and not a.valid("x")
    later = A.Auth("pw", clock=lambda: 1000.0 + A.SESSION_S + 1)
    assert not later.valid(a.make_token())
    assert not A.Auth("anderes", clock=lambda: 1000.0).valid(a.make_token())     # anderes Passwort = anderer Schlüssel


async def test_lockout_after_failures(secured):
    c, _ = secured
    codes = [(await c.post("/api/auth/login", json={"password": "x"})).status_code for _ in range(5)]
    assert codes[:4] == [401] * 4 and codes[4] == 429
    assert (await c.post("/api/auth/login", json={"password": "geheim"})).status_code == 429     # auch richtig gesperrt


def test_lockout_expires():
    t = [0.0]
    a = A.Auth("pw", clock=lambda: t[0])
    for _ in range(5):
        a.check_password("x")
    assert a.locked() > 0 and not a.check_password("pw")
    t[0] = A.LOCK_S + 1
    assert a.check_password("pw")


async def test_websocket_needs_session(rt):
    rt.settings.access_password = "geheim"
    app = create_app(rt)
    with TestClient(app) as tc:
        with pytest.raises(Exception):
            with tc.websocket_connect("/ws"):
                pass
        assert tc.post("/api/auth/login", json={"password": "geheim"}).status_code == 200
        with tc.websocket_connect("/ws") as ws:
            assert ws.receive_json()["kind"] == "history"
