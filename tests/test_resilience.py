"""Das Programm muss laufen und erreichbar bleiben, auch wenn Teile ausfallen."""
import asyncio
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app import runtime as runtime_mod
from app.config import Settings
from app.main import create_app
from app.node.sim import SimNode
from app.runtime import Hub, build

REPEATER_OK_KEY = "c3" * 32


@pytest.fixture
async def quiet_client(rt):
    """Wie `client`, gibt aber Serverfehler als Antwort zurück statt sie zu werfen (wie im echten Betrieb)."""
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        yield c, app


async def test_health_costs_nothing(client):
    assert (await client.get("/api/health")).json() == {"ok": True}


async def test_unexpected_error_becomes_500_message(rt, quiet_client):
    client, _ = quiet_client

    async def boom(*a, **k):
        raise RuntimeError("kaputt")
    rt.node.contacts = boom
    r = await client.get("/api/contacts")
    assert r.status_code == 500 and "Interner Fehler" in r.json()["detail"]
    assert (await client.get("/api/health")).status_code == 200      # läuft weiter


async def test_database_error_becomes_503(rt, quiet_client, monkeypatch):
    client, _ = quiet_client

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(rt.raw_store, "rules", broken)
    r = await client.get("/api/rules")
    assert r.status_code == 503 and "Datenbank-Problem" in r.json()["detail"]
    assert (await client.get("/api/status")).status_code == 200


async def test_status_survives_broken_node_status(rt, quiet_client):
    client, _ = quiet_client

    async def boom():
        raise RuntimeError("Node-Status kaputt")
    rt.node.status = boom
    r = await client.get("/api/status")
    assert r.status_code == 200 and "Node-Status kaputt" in r.json()["node_error"]


def test_unusable_database_falls_back_to_memory(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    rt = build(Settings(mode="sim", db_path=str(blocker / "unter" / "x.db")),
               node_factory=lambda emit: SimNode(emit, background=False))
    assert rt.state["problems"] and "Datenbank nicht nutzbar" in rt.state["problems"][0]
    rt.raw_store.add_battery("ab" * 32, 3900)                         # funktioniert im Speicher


def test_corrupt_database_file_falls_back(tmp_path):
    bad = tmp_path / "kaputt.db"
    bad.write_bytes(b"kein sqlite" * 200)
    rt = build(Settings(mode="sim", db_path=str(bad)), node_factory=lambda emit: SimNode(emit, background=False))
    assert rt.state["problems"]
    assert rt.raw_store.rules() == []


async def test_app_starts_even_if_automation_cannot(rt, monkeypatch):
    async def boom():
        raise RuntimeError("Zeitplan kaputt")
    monkeypatch.setattr(rt.automation, "start", boom)
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/status")
            assert r.status_code == 200
            assert any("Automatik nicht gestartet" in p for p in r.json()["problems"])


async def test_hanging_browser_does_not_block_events(monkeypatch):
    monkeypatch.setattr(runtime_mod, "WS_SEND_TIMEOUT_S", 0.2)
    hub = Hub()

    class Stuck:
        async def send_json(self, _):
            await asyncio.sleep(60)
    hub.clients.add(Stuck())
    await asyncio.wait_for(hub.emit({"type": "x"}), timeout=3)
    assert not hub.clients                                            # hängender Client wird entfernt


async def test_failing_listener_does_not_stop_others():
    hub = Hub()
    seen = []

    async def bad(_):
        raise RuntimeError("x")

    async def good(e):
        seen.append(e["type"])
    hub.listeners += [bad, good]
    await hub.emit({"type": "t"})
    await hub.drain()
    assert seen == ["t"]


async def test_missing_index_has_fallback(rt, monkeypatch, tmp_path):
    import app.main as m
    monkeypatch.setattr(m, "STATIC", tmp_path)
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "fehlt" in r.text


async def test_unexpected_error_in_repeater_node_gives_json_error_not_crash(rt, quiet_client, monkeypatch):
    client, _ = quiet_client
    await rt.repeater.login(REPEATER_OK_KEY, "sim")

    async def boom(*a, **k):
        raise RuntimeError("Bibliothek abgestürzt")
    monkeypatch.setattr(rt.node, "repeater_command", boom)
    r = await client.post(f"/api/repeater/{REPEATER_OK_KEY}/command", json={"command": "ver"})
    assert r.status_code == 500 and "Interner Fehler" in r.json()["detail"]
    assert (await client.get("/api/health")).status_code == 200      # Programm läuft weiter
