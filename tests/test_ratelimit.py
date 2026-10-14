"""Tempolimit für Funkaktionen von Hand (app/ratelimit.py und die Endpunkte in api.py)."""
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.ratelimit import ManualLimit
from tests.conftest import ALICE, FakeClock, REPEATER_OK


def test_min_gap_and_per_minute_cap():
    clock = FakeClock()
    lim = ManualLimit(2.0, 5, clock=clock)
    assert lim.take() == 0
    assert 1.9 < lim.take() <= 2.0                      # gleich danach: zu schnell
    for _ in range(4):
        clock.advance(2)
        assert lim.take() == 0
    clock.advance(2)
    assert lim.take() > 0                               # 5 in der Minute erreicht
    clock.advance(60)
    assert lim.take() == 0                              # Minute vorbei


def test_several_packets_at_once_are_all_or_nothing():
    clock = FakeClock()
    lim = ManualLimit(0, 3, clock=clock)
    assert lim.take(2) == 0
    assert lim.take(2) > 0                              # nur noch 1 frei: nichts verbucht
    assert lim.take(1) == 0


async def test_endpoints_refuse_too_fast_sending(rt, monkeypatch):
    rt.settings.manual_min_gap_s, rt.settings.manual_max_per_min = 2.0, 20     # Standardwerte statt der Test-Lockerung
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/api/channel", json={"channel": 0, "text": "eins"})).status_code == 200
        r = await c.post("/api/channel", json={"channel": 0, "text": "zwei"})
        assert r.status_code == 429 and "warten" in r.json()["detail"]
        for path, body in (("/api/message", {"to": ALICE, "text": "x"}), ("/api/advert", {"flood": False}),
                           (f"/api/nodes/{REPEATER_OK}/ping", {"mode": "status"}), (f"/api/nodes/{REPEATER_OK}/poll", {})):
            assert (await c.post(path, json=body)).status_code == 429, path
    assert len(rt.node.sent_channel) == 1                 # nur die erste Nachricht ging raus
