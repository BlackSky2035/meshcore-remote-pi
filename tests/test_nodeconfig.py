"""Eigener Node: Prüfregeln, Bestätigung, API (Simulator) und echter Node mit Fake-Bibliothek."""
from types import SimpleNamespace

import pytest

from app import nodeconfig as N
from app.config import Settings
from app.node import NodeError
from app.node.real import RealNode


# ---- Prüfregeln ------------------------------------------------------------------
def test_validate_ranges_and_types():
    clean, risks = N.validate({"name": " Hügel ", "tx_power": "14", "radio_sf": 11}, 22)
    assert clean == {"name": "Hügel", "tx_power": 14, "radio_sf": 11} and risks
    assert N.validate({"tx_power": 10}, 22)[1] == []
    assert N.validate({"lat": "12,5", "lon": "-33.1"})[0] == {"lat": 12.5, "lon": -33.1}
    assert N.validate({"manual_add_contacts": "ja"})[0] == {"manual_add_contacts": True}


@pytest.mark.parametrize("changes,max_tx", [
    ({}, None), ({"nope": 1}, None), ({"name": ""}, None), ({"name": "x" * 32}, None), ({"name": "a\nb"}, None),
    ({"name": "ü" * 16}, None),                       # 32 Byte
    ({"lat": 10}, None), ({"lat": 91, "lon": 0}, None), ({"lat": "abc", "lon": 0}, None),
    ({"radio_bw": 100}, None), ({"radio_sf": 13}, None), ({"radio_cr": 4}, None), ({"radio_freq": 100}, None),
    ({"tx_power": 23}, None), ({"tx_power": 20}, 17), ({"adv_loc_policy": 3}, None), ({"path_hash_mode": 4}, None),
])
def test_validate_rejects(changes, max_tx):
    with pytest.raises(N.ConfigError):
        N.validate(changes, max_tx)


def test_risks_radio_pathhash_and_public_location():
    assert any("Funk" in r for r in N.validate({"radio_freq": 869.5})[1])
    assert N.validate({"path_hash_mode": 1})[1]
    assert N.validate({"adv_loc_policy": 1})[1] and not N.validate({"adv_loc_policy": 0})[1]


# ---- API mit Simulator ------------------------------------------------------------------
async def test_get_spec_and_settings(client):
    spec = (await client.get("/api/node/spec")).json()
    assert {f["key"] for f in spec["fields"]} >= {"name", "radio_freq", "tx_power", "path_hash_mode"}
    d = (await client.get("/api/node/settings")).json()
    assert d["settings"]["name"] == "SimNode" and d["time_diff_s"] < -3000


async def test_apply_plain_change_and_nothing_to_change(client, rt):
    r = await client.put("/api/node/settings", json={"changes": {"name": "Neu", "tx_power": 10}})
    assert r.status_code == 200 and rt.node.cfg["name"] == "Neu" and rt.node.cfg["tx_power"] == 10
    r = await client.put("/api/node/settings", json={"changes": {"name": "Neu"}})
    assert r.status_code == 422 and "Nichts zu ändern" in r.json()["detail"]


async def test_risky_change_needs_confirm(client, rt):
    body = {"changes": {"radio_freq": 868.0}}
    r = await client.put("/api/node/settings", json=body)
    assert r.status_code == 409 and rt.node.cfg["radio_freq"] == 869.525
    r = await client.put("/api/node/settings", json={**body, "confirm": True})
    assert r.status_code == 200 and rt.node.cfg["radio_freq"] == 868.0


async def test_invalid_value_is_422(client, rt):
    r = await client.put("/api/node/settings", json={"changes": {"tx_power": 99}})
    assert r.status_code == 422 and rt.node.cfg["tx_power"] == 14


async def test_time_sync_and_reboot(client, rt):
    d = (await client.post("/api/node/time-sync")).json()
    assert abs(d["time_diff_s"]) < 5
    assert (await client.post("/api/node/reboot", json={})).status_code == 409 and rt.node.node_reboots == 0
    assert (await client.post("/api/node/reboot", json={"confirm": True})).status_code == 200 and rt.node.node_reboots == 1


async def test_node_not_connected_is_502(client, rt):
    await rt.node.disconnect()
    for r in (await client.get("/api/node/settings"),
              await client.put("/api/node/settings", json={"changes": {"name": "X"}}),
              await client.post("/api/node/time-sync")):
        assert r.status_code == 502


async def test_unexpected_error_becomes_500(rt):
    from httpx import ASGITransport, AsyncClient
    from app.main import create_app
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        await _boom_check(rt, client)


async def _boom_check(rt, client):

    async def boom():
        raise RuntimeError("kaputt")
    rt.node.node_settings = boom
    r = await client.get("/api/node/settings")
    assert r.status_code == 500 and (await client.get("/api/health")).status_code == 200


# ---- Echter Node mit Fake-Bibliothek ------------------------------------------------------------------
class FakeMC:
    def __init__(self, fail_on=None):
        self.me = {"name": "N", "public_key": "ab" * 32, "adv_lat": 1.0, "adv_lon": 2.0, "radio_freq": 869.525, "radio_bw": 250.0,
                   "radio_sf": 11, "radio_cr": 5, "tx_power": 14, "max_tx_power": 22, "adv_loc_policy": 0,
                   "telemetry_mode_base": 0, "telemetry_mode_loc": 0, "telemetry_mode_env": 0, "manual_add_contacts": False,
                   "multi_acks": 0}
        self.is_connected = True
        self.calls, self.fail_on = [], fail_on
        ok = lambda p=None: SimpleNamespace(type="ok", payload=p or {})
        self._ok = ok

        def mk(name, payload=None):
            async def f(*a, **k):
                self.calls.append((name, a))
                if name == self.fail_on:
                    return SimpleNamespace(type="error", payload={"reason": "nein"})
                return ok(payload() if callable(payload) else payload)
            return f
        self.commands = SimpleNamespace(
            send_appstart=mk("appstart", lambda: dict(self.me)), send_device_query=mk("query", {"ver": "v1.14", "path_hash_mode": 0}),
            get_time=mk("get_time", {"time": 1700000000}), set_name=mk("set_name"), set_coords=mk("set_coords"),
            set_radio=mk("set_radio"), set_tx_power=mk("set_tx_power"), set_other_params_from_infos=mk("other"),
            set_path_hash_mode=mk("hash"), set_time=mk("set_time"), reboot=mk("reboot"))


def real_node(**kw):
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    n.mc = FakeMC(**kw)
    return n


async def test_real_node_settings_and_apply_sequence():
    n = real_node()
    s = await n.node_settings()
    assert s["lat"] == 1.0 and s["firmware"] == "v1.14" and s["node_time"] == 1700000000 and s["max_tx_power"] == 22
    done = await n.node_apply({"name": "Z", "lat": 3.0, "lon": 4.0, "radio_sf": 9, "manual_add_contacts": True, "tx_power": 10})
    assert done == ["name", "lat", "lon", "radio_sf", "tx_power", "manual_add_contacts"]
    calls = dict(n.mc.calls)
    assert calls["set_radio"] == (869.525, 250.0, 9, 5)                  # fehlende Funkwerte aus dem Node
    other = [c for c in n.mc.calls if c[0] == "other"]
    assert other and other[0][1][0]["manual_add_contacts"] == 1          # bool -> int


async def test_real_node_partial_failure_names_what_was_changed():
    n = real_node(fail_on="set_radio")
    with pytest.raises(NodeError, match=r"schon geändert: name"):
        await n.node_apply({"name": "Z", "radio_sf": 9})


async def test_real_node_reboot_never_raises():
    n = real_node()
    await n.node_reboot()
    assert ("reboot", ()) in n.mc.calls
