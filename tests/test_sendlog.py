"""Sendeprotokoll im Arbeitsspeicher (sendlog.py) und der geschützte Endpunkt /api/sends für den Watchdog."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.sendlog import SendLog, TX_KINDS
from tests.conftest import ALICE, REPEATER_OK


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def test_node_sends_are_recorded(rt, client):
    before = len(rt.sendlog.since(0))
    await client.post("/api/advert", json={"flood": False})
    await client.post(f"/api/nodes/{REPEATER_OK}/poll")
    kinds = [s["kind"] for s in rt.sendlog.since(0)][before:]
    assert "Advert" in kinds and "Statusabfrage" in kinds


async def test_ping_and_message_are_recorded(rt):
    await rt.node.ping(REPEATER_OK, "status")
    await rt.node.send_message(ALICE, "hallo")
    kinds = [s["kind"] for s in rt.sendlog.since(0)]
    assert "Ping" in kinds and "Nachricht" in kinds


async def test_failed_send_is_still_recorded_and_error_propagates():
    class N:
        async def send_message(self, k, t):
            raise RuntimeError("Funk weg")
    n, log = N(), SendLog()
    log.instrument(n)
    with pytest.raises(RuntimeError, match="Funk weg"):
        await n.send_message("x", "y")
    assert [s["kind"] for s in log.since(0)] == ["Nachricht"]        # gesendet wurde versucht, also Funkzeit/Strom


async def test_return_value_and_arguments_pass_through():
    class N:
        async def send_advert(self, flood=False):
            return {"flood": flood}
    n, log = N(), SendLog()
    log.instrument(n)
    assert await n.send_advert(flood=True) == {"flood": True}


def test_ring_buffer_is_bounded():
    clock = Clock()
    log = SendLog(maxlen=5, wall=clock)
    for i in range(50):
        clock.t += 1
        log.add("x")
    assert len(log._d) == 5 and log.since(0)[0]["ts"] == 1046.0


def test_since_filters_and_limits():
    clock = Clock()
    log = SendLog(wall=clock)
    for i in range(10):
        clock.t = 1000 + i
        log.add("k")
    assert [s["ts"] for s in log.since(1007)] == [1007.0, 1008.0, 1009.0]
    assert len(log.since(0, limit=4)) == 4


def test_instrument_skips_missing_methods_and_never_double_wraps():
    class N:
        async def send_advert(self, flood=False):
            return {}
    n, log = N(), SendLog()
    assert log.instrument(n) == 1                       # nur was es gibt
    assert log.instrument(n) == 0                       # zweites Mal: nichts doppelt
    assert set(TX_KINDS) >= {"send_message", "send_channel_message", "send_advert", "ping"}


def test_log_never_raises():
    def boom():
        raise RuntimeError("Uhr kaputt")
    log = SendLog(wall=boom)
    log.add("x")                                        # darf nicht werfen
    assert log.since(0) == []
    log._d = None                                       # kaputter Zustand
    assert log.since(0) == []


def test_unwrappable_node_does_not_break():
    class Frozen:
        __slots__ = ()
    assert SendLog().instrument(Frozen()) == 0          # keine Methoden, kein Fehler


# ---- Endpunkt ----------------------------------------------------------------------------------
async def test_endpoint_returns_recent_sends_without_login(client, rt):
    await client.post("/api/advert", json={"flood": False})
    r = await client.get("/api/sends?since=0")
    assert r.status_code == 200
    body = r.json()
    assert body["capacity"] == 300 and any(s["kind"] == "Advert" for s in body["sends"])
    assert set(body["sends"][0]) == {"ts", "kind"}      # nur Zeit und Art, nie ein Nachrichtentext


async def test_endpoint_protected_when_login_enabled(rt):
    rt.settings.access_password = "pw"
    rt.settings.control_token = "ctl"
    async with AsyncClient(transport=ASGITransport(app=create_app(rt)), base_url="http://t") as c:
        assert (await c.get("/api/sends")).status_code == 401
        assert (await c.get("/api/sends", headers={"X-Control-Token": "falsch"})).status_code == 401
        assert (await c.get("/api/sends", headers={"X-Control-Token": "ctl"})).status_code == 200
        assert (await c.get("/api/contacts", headers={"X-Control-Token": "ctl"})).status_code == 401   # Token gilt nur für diese Pfade


async def test_empty_control_token_never_matches(rt):
    rt.settings.access_password = "pw"
    rt.settings.control_token = ""
    async with AsyncClient(transport=ASGITransport(app=create_app(rt)), base_url="http://t") as c:
        assert (await c.get("/api/sends", headers={"X-Control-Token": ""})).status_code == 401


async def test_endpoint_survives_missing_sendlog(rt, client):
    rt.sendlog = None
    r = await client.get("/api/sends")
    assert r.status_code == 200 and r.json()["sends"] == []
