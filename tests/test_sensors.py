"""Sensorwerte: Speichern aller Zahlenwerte, API, Ausfälle."""
from tests.conftest import REPEATER_OK, REPEATER_LOW, ALICE


async def test_store_only_numbers(rt):
    n = await rt.store.add_sensor_readings(REPEATER_OK, [
        {"type": "temperature", "channel": 1, "value": 20.5}, {"type": "gps", "channel": 2, "value": [1, 2, 3]},
        {"type": "x", "value": True}, {"type": "y", "value": float("nan")}, {"type": "z", "value": "text"}, "kaputt", {"type": "ok", "value": 3}])
    assert n == 2
    assert {(r["type"], r["value"]) for r in await rt.store.sensor_latest(REPEATER_OK)} == {("temperature", 20.5), ("ok", 3.0)}


async def test_read_and_history_api(client, rt):
    r = await client.post(f"/api/sensors/{REPEATER_OK}/read")
    assert r.status_code == 200 and r.json()["stored"] >= 3
    types = {x["type"] for x in r.json()["latest"]}
    assert {"voltage", "temperature", "humidity"} <= types and "gps" not in types
    h = (await client.get(f"/api/sensors/{REPEATER_OK}/history", params={"type": "temperature", "channel": 2})).json()
    assert len(h["rows"]) == 1
    assert (await client.get(f"/api/sensors/{REPEATER_OK}")).json()["latest"]


async def test_read_errors(client, rt):
    assert (await client.post(f"/api/sensors/{ALICE}/read")).status_code == 502         # Client: keine Telemetrie
    rt.node.fail_status.add(REPEATER_LOW)
    assert (await client.post(f"/api/sensors/{REPEATER_LOW}/read")).status_code == 502
    assert (await client.post("/api/sensors/zzz/read")).status_code == 422
    assert (await client.get(f"/api/sensors/{REPEATER_OK}/history", params={"type": "x", "hours": 99999})).json() == {"rows": []}


async def test_battery_rule_with_temperature_also_stores_sensors(rt):
    await rt.automation.poll_node(REPEATER_OK, low_mv=3700, temperature=True)
    assert any(r["type"] == "humidity" for r in await rt.store.sensor_latest(REPEATER_OK))


async def test_sensor_save_failure_does_not_break_poll(rt):
    async def boom(*a, **k): raise RuntimeError("db")
    rt.store.add_sensor_readings = boom
    res = await rt.automation.poll_node(REPEATER_OK, low_mv=3700, temperature=True)
    assert res["temperature"] is not None
