"""Schutz gegen DNS-Rebinding und fremde Webseiten (app/hostguard.py, Middleware und WebSocket in main.py)."""
import socket

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import hostguard
from app.main import create_app
from tests.conftest import REAL_HOST_ALLOWED


@pytest.fixture
def real_hosts(monkeypatch):
    """Echte Prüfung statt der Test-Namen aus conftest; eigener Rechnername fest „station“."""
    monkeypatch.setattr(hostguard, "host_allowed", REAL_HOST_ALLOWED)
    monkeypatch.setattr(socket, "gethostname", lambda: "station")


@pytest.mark.parametrize("host", [
    "192.168.1.20:8000", "127.0.0.1", "[::1]:8000", "100.101.102.103", "localhost:8000", "station", "station.local",
    "STATION.LOCAL.", "pi.tail1234.ts.net", "pi.tail1234.ts.net:443", None, ""])
def test_known_hosts_are_allowed(real_hosts, host):
    assert hostguard.host_allowed(host)


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "station.evil.example", "ts.net.evil.example", "localhost.evil.example"])
def test_foreign_names_are_rejected(real_hosts, host):
    assert not hostguard.host_allowed(host)


def test_allowed_hosts_from_config(real_hosts):
    extra = hostguard.parse_allowed("funk.heim.lan, .meinedomain.de")
    assert hostguard.host_allowed("funk.heim.lan:8000", extra)
    assert hostguard.host_allowed("pi.meinedomain.de", extra)
    assert not hostguard.host_allowed("evil.example", extra)
    assert hostguard.host_allowed("evil.example", hostguard.parse_allowed("*"))     # Prüfung bewusst aus


LAN = {"host": "192.168.1.20:8000"}


@pytest.mark.parametrize("headers,cross", [
    ({}, False),                                                     # kein Browser (Watchdog, curl)
    ({"sec-fetch-site": "same-origin"}, False),
    ({"sec-fetch-site": "none"}, False),                             # Adresse selbst eingetippt
    ({"sec-fetch-site": "cross-site"}, True),
    ({**LAN, "origin": "http://192.168.1.20:8000"}, False),          # WebSocket der eigenen Oberfläche (ohne Sec-Fetch-Site)
    ({**LAN, "origin": "http://evil.example"}, True),
    ({**LAN, "origin": "http://203.0.113.5"}, True),                 # fremde Seite unter einer IP: IP allein reicht nicht
    ({"host": "127.0.0.1:8768", "origin": "http://localhost:8899"}, True),   # im Browser nachgestellter Fall
    ({**LAN, "origin": "https://station.tail1234.ts.net"}, False),   # hinter tailscale serve: eigener Tailscale-Name
    ({"host": "127.0.0.1:8000", "x-forwarded-host": "funk.example", "origin": "https://funk.example"}, False),
    ({**LAN, "origin": "https://other.tail9999.ts.net"}, True),      # fremde Seite über Tailscale Funnel
    ({**LAN, "origin": "http://station.local:8000"}, False),
    ({**LAN, "sec-fetch-site": "same-site", "origin": "http://evil.station.example"}, True),
    ({**LAN, "origin": "null"}, True),
])
def test_cross_site_detection(real_hosts, headers, cross):
    assert hostguard.cross_site(headers) is cross


async def test_rebinding_and_cross_site_requests_are_rejected(rt, real_hosts):
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://192.168.1.20:8000") as c:
        assert (await c.get("/api/status")).status_code == 200
        r = await c.get("/api/status", headers={"host": "evil.example"})             # DNS-Rebinding
        assert r.status_code == 403 and "allowed_hosts" in r.json()["detail"]
        assert (await c.get("/", headers={"host": "evil.example"})).status_code == 403
        r = await c.post("/api/advert", headers={"sec-fetch-site": "cross-site"})      # Formular einer fremden Seite
        assert r.status_code == 403
        r = await c.post("/api/advert", headers={"origin": "http://evil.example"})
        assert r.status_code == 403
        assert (await c.post("/api/advert", json={}, headers={"sec-fetch-site": "same-origin"})).status_code == 200
        r = await c.post("/api/advert", json={}, headers={"origin": "http://203.0.113.5"})     # fremde Seite unter IP
        assert r.status_code == 403


async def test_allowed_hosts_setting_lets_a_custom_name_in(rt, real_hosts):
    rt.settings.allowed_hosts = "funk.heim.lan"
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://funk.heim.lan") as c:
        assert (await c.get("/api/status")).status_code == 200


def test_websocket_from_foreign_page_is_refused(rt, real_hosts):
    app = create_app(rt)
    with TestClient(app, base_url="http://192.168.1.20:8000") as tc:
        lan = {"host": "192.168.1.20:8000"}                                          # TestClient schickt sonst immer „testserver“
        with tc.websocket_connect("/ws", headers={**lan, "origin": "http://192.168.1.20:8000"}) as ws:   # eigene Oberfläche
            assert ws.receive_json()["kind"] == "history"
        for headers in ({**lan, "origin": "http://evil.example"}, {"host": "evil.example"}, {**lan, "sec-fetch-site": "cross-site"}):
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect("/ws", headers=headers) as ws:
                    ws.receive_json()


def test_broken_hostname_lookup_does_not_crash(monkeypatch):
    """Ausfall: Rechnername nicht lesbar → nur dieser Weg fällt weg, IPs/ts.net gehen weiter, keine Ausnahme."""
    monkeypatch.setattr(hostguard, "host_allowed", REAL_HOST_ALLOWED)

    def boom():
        raise OSError("kaputt")
    monkeypatch.setattr(socket, "gethostname", boom)
    assert hostguard.host_allowed("10.0.0.1")
    assert hostguard.host_allowed("pi.tail1234.ts.net")
    assert not hostguard.host_allowed("evil.example")
