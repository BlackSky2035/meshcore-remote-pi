"""Gegenseitige Überwachung: Abfrage-Endpunkt (Token), Parser, Ausfall-/Rückkehrmeldung, Ausfälle."""
import pytest
from httpx import ASGITransport, AsyncClient

from app import peers as P
from app.main import create_app


def test_parse_peers():
    ps = P.parse_peers("Station A|https://a.example.net|abc; Station B|http://192.0.2.7:8000|def\n Kaputt|ftp://x|t; Leer||x; nur-name")
    assert [(p.name, p.url, p.token) for p in ps] == [("Station A", "https://a.example.net", "abc"), ("Station B", "http://192.0.2.7:8000", "def")]
    assert P.parse_peers("") == [] and len(P.parse_peers(";".join(f"n{i}|https://h{i}.example.net|t" for i in range(9)))) == 5


def test_token_check():
    assert P.token_ok("geheim", "geheim") and not P.token_ok("geheim", "falsch") and not P.token_ok("", "") and not P.token_ok("x", None)


async def test_summary_endpoint_needs_token(rt):
    rt.settings.peer_token = "tok"
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/peer/summary")).status_code == 404
        assert (await c.get("/api/peer/summary", headers={"X-Peer-Token": "x"})).status_code == 404
        d = (await c.get("/api/peer/summary", headers={"X-Peer-Token": "tok"})).json()
        assert d["node_connected"] and d["station"] and "contacts" not in d and "password" not in str(d).lower()


async def test_summary_off_without_token_and_open_despite_login(rt):
    rt.settings.access_password = "pw"
    rt.settings.peer_token = ""
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/peer/summary", headers={"X-Peer-Token": ""})).status_code == 404       # nicht 401: kein Hinweis auf Anmeldung
        assert (await c.get("/api/peers")).status_code == 401
    rt.settings.peer_token = "tok"
    async with AsyncClient(transport=ASGITransport(app=create_app(rt)), base_url="http://t") as c:
        assert (await c.get("/api/peer/summary", headers={"X-Peer-Token": "tok"})).status_code == 200


async def test_alert_once_after_three_failures_and_recovery():
    told, state = [], {"up": False}

    def fetch(p):
        if not state["up"]:
            raise OSError("keine Verbindung")
        return {"station": "Haus", "node_connected": True}

    async def notify(kind, text):
        told.append((kind, text))
    w = P.PeerWatch([P.Peer("Haus", "https://h", "t")], notify, fetch)
    for _ in range(2):
        await w.poll_once()
    assert told == [] and w.status()[0]["ok"] is False
    await w.poll_once(); await w.poll_once(); await w.poll_once()
    assert [k for k, _ in told] == ["down"]                      # genau eine Meldung
    state["up"] = True
    await w.poll_once(); await w.poll_once()
    assert [k for k, _ in told] == ["down", "up"] and w.status()[0]["summary"]["station"] == "Haus"


async def test_notify_failure_and_bad_response_do_not_stop_watch():
    async def bad_notify(kind, text): raise RuntimeError("push kaputt")
    w = P.PeerWatch([P.Peer("A", "https://a", "t"), P.Peer("B", "https://b", "t")], bad_notify,
                    lambda p: (_ for _ in ()).throw(ValueError("Müll")) if p.name == "A" else {"station": "B"})
    for _ in range(4):
        await w.poll_once()
    st = {s["name"]: s for s in w.status()}
    assert st["A"]["ok"] is False and st["A"]["alerted"] and st["B"]["ok"] is True


async def test_fetch_limits(monkeypatch):
    class R:
        def __init__(self, data): self.data = data
        def read(self, n): return self.data[:n]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(P, "no_redirect_open", lambda req, timeout: R(b"x" * (P.MAX_BYTES + 5)))
    with pytest.raises(ValueError):
        P.fetch_summary(P.Peer("A", "https://a", "t"))
    monkeypatch.setattr(P, "no_redirect_open", lambda req, timeout: R(b"[1]"))
    with pytest.raises(ValueError):
        P.fetch_summary(P.Peer("A", "https://a", "t"))
    monkeypatch.setattr(P, "no_redirect_open", lambda req, timeout: R(b'{"a": 1}'))
    assert P.fetch_summary(P.Peer("A", "https://a", "t")) == {"a": 1}


async def test_peers_status_api_empty(client):
    assert (await client.get("/api/peers")).json() == {"peers": []}
