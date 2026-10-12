import time

from .conftest import REPEATER_LOW, REPEATER_OK

RULE = {"kind": "battery_poll", "name": "Akku", "enabled": True,
        "config": {"targets": "all_repeaters", "interval_min": 60, "low_mv": 3500, "temperature": True}}


async def test_poll_with_temperature_stores_it(rt):
    res = await rt.automation.poll_node(REPEATER_OK, temperature=True)
    assert isinstance(res["temperature"], float)
    assert (await rt.store.latest_temperature())[REPEATER_OK]["celsius"] == res["temperature"]
    assert len(await rt.store.battery_history(REPEATER_OK, 0)) == 1


async def test_poll_without_temperature_skips_telemetry(rt):
    await rt.automation.poll_node(REPEATER_OK)
    assert await rt.store.latest_temperature() == {}


async def test_no_sensor_backoff(rt):
    calls = []
    orig = rt.node.repeater_telemetry

    async def spy(key):
        calls.append(key)
        return await orig(key)
    rt.node.repeater_telemetry = spy
    t = [1000.0]
    rt.automation._clock = lambda: t[0]
    assert (await rt.automation.poll_node(REPEATER_LOW, temperature=True))["temperature"] is None
    await rt.automation.poll_node(REPEATER_LOW, temperature=True)
    assert len(calls) == 1                      # zweite Anfrage gespart
    t[0] += 25 * 3600
    await rt.automation.poll_node(REPEATER_LOW, temperature=True)
    assert len(calls) == 2


async def test_api_poll_and_history_include_temperature(client):
    r = await client.post(f"/api/nodes/{REPEATER_OK}/poll", json={"temperature": True})
    assert r.status_code == 200 and r.json()["temperature"] is not None
    hist = (await client.get(f"/api/nodes/{REPEATER_OK}/history")).json()
    assert len(hist["temperature"]) == 1


async def test_battery_nodes_lists_only_monitored(client):
    assert (await client.get("/api/battery/nodes")).json() == {"nodes": [], "default": None}
    rid = (await client.post("/api/rules", json=RULE)).json()["id"]
    nodes = (await client.get("/api/battery/nodes")).json()["nodes"]
    keys = {n["public_key"] for n in nodes}
    assert {REPEATER_OK, REPEATER_LOW} <= keys and all(n["low_mv"] == 3500 for n in nodes)
    await client.put(f"/api/rules/{rid}", json={**RULE, "enabled": False})
    assert (await client.get("/api/battery/nodes")).json() == {"nodes": [], "default": None}


async def test_battery_default_node_is_stored_and_reported(client):
    assert (await client.put("/api/battery/default", json={"public_key": REPEATER_OK})).status_code == 200
    assert (await client.get("/api/battery/nodes")).json()["default"] == REPEATER_OK
    await client.put("/api/battery/default", json={"public_key": None})
    assert (await client.get("/api/battery/nodes")).json()["default"] is None


async def test_battery_nodes_picked_targets(client):
    cfg = {**RULE["config"], "targets": [REPEATER_OK], "low_mv": 3800}
    await client.post("/api/rules", json={**RULE, "config": cfg})
    nodes = (await client.get("/api/battery/nodes")).json()["nodes"]
    assert [n["public_key"] for n in nodes] == [REPEATER_OK] and nodes[0]["low_mv"] == 3800


async def test_battery_rows_merge_and_csv(client, rt):
    now = time.time()
    await rt.store.add_battery(REPEATER_OK, 4000, now - 600)
    await rt.store.add_temperature(REPEATER_OK, 10.5, now - 590)      # gehört zur Messung davor
    await rt.store.add_temperature(REPEATER_OK, 12.0, now - 300)      # allein
    await rt.store.add_battery(REPEATER_OK, 3990, now - 10)
    rows = (await client.get(f"/api/battery/{REPEATER_OK}?hours=1")).json()["rows"]
    assert [(r["mv"], r["temp"]) for r in rows] == [(4000, 10.5), (None, 12.0), (3990, None)]
    csv = await client.get(f"/api/battery/{REPEATER_OK}/export.csv?hours=1")
    lines = csv.text.strip().split("\n")
    assert lines[0] == "zeit_utc;akku_mv;temperatur_c" and lines[1].endswith(";4000;10,5") and len(lines) == 4
    assert (await client.get("/api/battery/xyz")).status_code == 422
