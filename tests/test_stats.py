"""Funkstatistik: Zähler lesen, Differenzen, Neustart-Erkennung, Ausfälle, echter Node."""
from types import SimpleNamespace

import pytest

from app import stats as S
from app.config import Settings
from app.node import NodeError
from app.node.real import RealNode


def row(ts, tx, rx, recv=0, sent=0, up=0):
    return {"ts": ts, "tx_air": tx, "rx_air": rx, "recv": recv, "sent": sent, "noise_floor": -110, "uptime": up}


def test_derive_percentages_gaps_and_restart():
    out = S.derive([row(0, 0, 0), row(600, 6, 60, 30, 5), row(1200, 12, 120, 60, 10)])
    assert len(out) == 2 and out[0]["tx_pct"] == 1.0 and out[0]["rx_pct"] == 10.0 and out[0]["recv_per_h"] == 180.0
    assert S.derive([row(0, 0, 0), row(S.MAX_GAP_S + 1, 5, 5)]) == []                    # zu große Lücke
    restart = S.derive([row(0, 500, 500, 100, 50), row(300, 2, 3, 1, 1)])               # Zähler kleiner → Neustart
    assert restart[0]["tx_pct"] is None and restart[0]["recv_per_h"] is None
    assert S.derive([]) == [] and S.derive([row(0, 0, 0)]) == []


def test_flatten_handles_missing_parts():
    assert S.flatten({})["tx_air"] is None
    f = S.flatten({"radio": {"tx_air_secs": 3, "noise_floor": -100}, "packets": {"recv": 7}})
    assert (f["tx_air"], f["noise_floor"], f["recv"]) == (3, -100, 7)


async def test_api_sample_and_series(client, rt):
    assert (await client.get("/api/stats")).json()["samples"] == 0
    for i in range(3):
        rt.node._stat_scale = 1.0 + i
        assert (await client.post("/api/stats/sample")).status_code == 200
    d = (await client.get("/api/stats")).json()
    assert d["samples"] == 3 and d["latest"]["noise_floor"] is not None


async def test_sampler_never_raises(rt):
    s = S.StatsSampler(rt.node, rt.store)
    async def boom(): raise RuntimeError("kaputt")
    rt.node.node_stats = boom
    assert await s.sample() is None and "kaputt" in s.last_error
    async def ne(): raise NodeError("nicht unterstützt")
    rt.node.node_stats = ne
    assert await s.sample() is None and s.last_error == "nicht unterstützt"
    await rt.node.disconnect()
    assert await s.sample() is None


async def test_sample_endpoint_errors(client, rt):
    await rt.node.disconnect()
    assert (await client.post("/api/stats/sample")).status_code == 502


async def test_db_failure_in_sample_is_caught(rt):
    async def boom(*a, **k): raise RuntimeError("db")
    rt.store.add_radio_stats = boom
    assert await S.StatsSampler(rt.node, rt.store).sample() is None


async def test_set_enabled_persists_and_updates_status(rt):
    s = S.StatsSampler(rt.node, rt.store)
    assert s.status() == {"enabled": True, "every_s": S.SAMPLE_EVERY_S}
    await s.set_enabled(False)
    assert s.status()["enabled"] is False
    assert await rt.store.get_setting("radio_stats_enabled", True) is False


async def test_run_loads_enabled_from_store_and_skips_sampling_when_off(rt, monkeypatch):
    await rt.store.set_setting("radio_stats_enabled", False)
    s = S.StatsSampler(rt.node, rt.store)
    calls = []

    async def fake_sample():
        calls.append(1)
        return None
    s.sample = fake_sample

    class StopLoop(Exception):
        pass

    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= 2:
            raise StopLoop
    monkeypatch.setattr(S.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopLoop):
        await s.run()
    assert s.enabled is False
    assert calls == []                      # Automatik war aus: keine Messung ausgelöst


async def test_enabled_endpoint_toggles_and_is_reported_in_stats(client, rt):
    assert (await client.get("/api/stats")).json()["enabled"] is True
    r = await client.post("/api/stats/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await client.get("/api/stats")).json()["enabled"] is False


async def test_real_node_stats():
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    ok = lambda p: (lambda: _co(SimpleNamespace(type="ok", payload=p)))
    async def _co(x): return x
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        get_stats_core=ok({"uptime_secs": 5}), get_stats_radio=ok({"noise_floor": -99, "tx_air_secs": 1}), get_stats_packets=ok({"recv": 4})))
    st = await n.node_stats()
    assert S.flatten(st)["noise_floor"] == -99 and st["packets"]["recv"] == 4
    n.mc.commands.get_stats_radio = lambda: _co(SimpleNamespace(type="error", payload={"reason": "x"}))
    with pytest.raises(NodeError):
        await n.node_stats()


async def test_run_survives_broken_settings(rt):
    """Früher beendete ein Datenbankfehler beim Lesen der Einstellung die Aufgabe still (keine Statistik mehr bis zum Neustart)."""
    import asyncio

    async def boom(*a, **k):
        raise RuntimeError("db")
    rt.store.get_setting = boom
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
    s = S.StatsSampler(rt.node, rt.store, sleep=fake_sleep)
    calls = []
    s.sample = lambda: calls.append(1) or asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert s.enabled is True and len(calls) == 2                  # Standard „an“, misst weiter
