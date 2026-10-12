"""Wartungsmodus: Anschluss frei, keine Meldungen, Ende von selbst, Zugriff per Geheimwort, Vorrang der Raum-Befehle."""
import pytest
from httpx import ASGITransport, AsyncClient

from app import usb
from app.main import create_app
from app.supervisor import Supervisor
from tests.conftest import ALICE, FakeClock


@pytest.fixture
def sup(rt):
    clock = FakeClock()
    wall = FakeClock()
    rt.usb._clock = clock
    rt.usb.alert_after_s = 60
    rt.maintenance._wall = wall
    told = []

    async def notify(kind, text):
        told.append((kind, text))
    rt.usb.notify = notify
    s = Supervisor(rt, clock=clock, settle_s=0, heartbeat_s=30)
    s.clock, s.wall, s.told = clock, wall, told
    return s


async def test_maintenance_frees_port_silences_alerts_and_ends(rt, sup):
    await sup.step()
    assert rt.node.connected
    await rt.maintenance.start(30)
    await sup.step()
    assert not rt.node.connected and rt.usb.state == usb.MAINTENANCE
    for _ in range(10):                                  # 10 min: sonst wäre längst eine Ausfallmeldung gekommen
        sup.clock.advance(60); sup.wall.advance(60)
        await sup.step()
    assert sup.told == [] and not rt.node.connected      # bleibt getrennt, kein Wiederverbinden
    sup.clock.advance(1300); sup.wall.advance(1300)      # Ende erreicht
    await sup.step(); sup.clock.advance(2); await sup.step()
    assert rt.node.connected and rt.usb.state == usb.OK and not rt.maintenance.active()
    assert sup.told == []


async def test_maintenance_does_not_count_as_downtime(rt, sup):
    await sup.step()
    await rt.maintenance.start(10)
    await sup.step()
    sup.clock.advance(300); sup.wall.advance(300)
    s = await rt.usb.stats()
    assert s["24h"] is None or s["24h"]["outages"] == 0


async def test_maintenance_survives_restart_and_is_limited(rt):
    wall = FakeClock(); rt.maintenance._wall = wall
    v = await rt.maintenance.start(99999)
    assert v["remaining_s"] == 1440 * 60
    from app.maintenance import Maintenance
    other = Maintenance(rt.store, rt.hub.emit, wall=wall)
    await other.load()
    assert other.active()
    await rt.maintenance.end()
    again = Maintenance(rt.store, rt.hub.emit, wall=wall)
    await again.load()
    assert not again.active()


async def test_api_and_status(client, rt):
    assert (await client.post("/api/maintenance", json={"minutes": 2})).status_code == 422
    r = await client.post("/api/maintenance", json={"minutes": 60})
    assert r.status_code == 200 and r.json()["active"]
    assert (await client.get("/api/status")).json()["maintenance"]["active"]
    assert (await client.get("/api/version")).json()["maintenance_until"]
    assert (await client.delete("/api/maintenance")).json()["active"] is False
    assert (await client.get("/api/version")).json()["maintenance_until"] is None


async def test_control_token_with_login_enabled(rt):
    rt.settings.access_password = "pw"
    rt.settings.control_token = "ctl"
    async with AsyncClient(transport=ASGITransport(app=create_app(rt)), base_url="http://t") as c:
        assert (await c.post("/api/maintenance", json={"minutes": 30})).status_code == 401
        assert (await c.post("/api/maintenance", json={"minutes": 30}, headers={"X-Control-Token": "falsch"})).status_code == 401
        assert (await c.post("/api/maintenance", json={"minutes": 30}, headers={"X-Control-Token": "ctl"})).status_code == 200
        assert (await c.get("/api/contacts", headers={"X-Control-Token": "ctl"})).status_code == 401      # Token gilt nur für Wartung
        assert (await c.delete("/api/maintenance", headers={"X-Control-Token": "ctl"})).status_code == 200


async def test_empty_control_token_never_matches(rt):
    rt.settings.access_password = "pw"
    rt.settings.control_token = ""
    async with AsyncClient(transport=ASGITransport(app=create_app(rt)), base_url="http://t") as c:
        assert (await c.post("/api/maintenance", json={"minutes": 30}, headers={"X-Control-Token": ""})).status_code == 401


async def test_storage_failure_does_not_break(rt):
    async def boom(*a, **k): raise RuntimeError("db")
    rt.store.set_setting = boom
    v = await rt.maintenance.start(10)
    assert v["active"]


async def test_room_command_is_not_answered_by_auto_reply_rule(rt):
    await rt.room.set_config(True, "Raum", 5000)
    await rt.store.add_rule("auto_reply", "alles", {"match": "p", "mode": "contains", "reply": "AUTO", "cooldown_s": 30, "max_per_hour": 10,
                                                     "direct": True, "channels": [], "only_from": None})
    await rt.node.inject_message(ALICE, "/p Hallo"); await rt.hub.drain()
    assert not any("AUTO" in t for _, t in rt.node.sent) and any("gespeichert" in t for _, t in rt.node.sent)
    await rt.room.set_config(False, "Raum", 5000)               # Raum aus: Auto-Antwort darf wieder greifen
    await rt.node.inject_message(ALICE, "/p Hallo"); await rt.hub.drain()
    assert any("AUTO" in t for _, t in rt.node.sent)
