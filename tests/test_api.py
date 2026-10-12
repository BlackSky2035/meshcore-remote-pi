from .conftest import ALICE, REPEATER_LOW

RULE_REPLY = {"kind": "auto_reply", "name": "Pong", "enabled": True,
              "config": {"match": "ping", "reply": "pong"}}


async def test_contacts_have_types(client):
    r = await client.get("/api/contacts")
    assert r.status_code == 200
    types = {c["name"]: c["type_name"] for c in r.json()}
    assert types["Alice (Sim)"] == "client"
    assert types["Repeater Hügel (Sim)"] == "repeater"
    room = next(c for c in r.json() if c["type_name"] == "room")
    assert room["lat"] is None            # 0/0 gilt als "keine Position"


async def test_import_and_delete_contact(client):
    new_key = "ab" * 32
    r = await client.post("/api/contacts/import", json={"card": "meshcore://" + new_key + "00" * 4})
    assert r.status_code == 200
    assert new_key in [c["public_key"] for c in (await client.get("/api/contacts")).json()]

    assert (await client.delete(f"/api/contacts/{new_key}")).status_code == 200
    assert new_key not in [c["public_key"] for c in (await client.get("/api/contacts")).json()]
    assert (await client.delete(f"/api/contacts/{new_key}")).status_code == 502


async def test_import_rejects_garbage(client):
    r = await client.post("/api/contacts/import", json={"card": "das-ist-kein-hex!!"})
    assert r.status_code == 502


async def test_invalid_key_is_rejected(client):
    assert (await client.delete("/api/contacts/xyz")).status_code == 422
    r = await client.post("/api/message", json={"to": "abc", "text": "hi"})
    assert r.status_code == 422


async def test_reset_path_and_export(client):
    assert (await client.post(f"/api/contacts/{ALICE}/reset-path")).status_code == 200
    r = await client.get(f"/api/contacts/{ALICE}/export")
    assert r.json()["uri"].startswith("meshcore://")
    assert (await client.get("/api/contacts/export")).json()["uri"].startswith("meshcore://")


async def test_message_is_stored(client):
    assert (await client.post("/api/message", json={"to": ALICE, "text": "Hallo"})).status_code == 200
    msgs = (await client.get("/api/messages", params={"peer": ALICE[:12]})).json()
    assert msgs[-1]["text"] == "Hallo" and msgs[-1]["direction"] == "out"
    assert (await client.get("/api/messages", params={"peer": "nicht-hex"})).status_code == 422


async def test_nodes_poll_and_history(client):
    assert (await client.post(f"/api/nodes/{REPEATER_LOW}/poll")).json()["bat"] == 3620
    nodes = {n["public_key"]: n for n in (await client.get("/api/nodes")).json()}
    assert nodes[REPEATER_LOW]["battery"]["mv"] == 3620
    assert nodes[REPEATER_LOW]["last_ping"]["ok"] is True
    hist = (await client.get(f"/api/nodes/{REPEATER_LOW}/history")).json()
    assert len(hist["battery"]) == 1 and len(hist["pings"]) == 1


async def test_poll_on_client_fails_cleanly(client):
    assert (await client.post(f"/api/nodes/{ALICE}/poll")).status_code == 502


async def test_rules_crud_and_validation(client):
    r = await client.post("/api/rules", json=RULE_REPLY)
    assert r.status_code == 200
    rule = r.json()
    assert rule["config"]["cooldown_s"] == 300          # Standardwert ergänzt

    update = {**RULE_REPLY, "name": "Pong 2", "enabled": False}
    assert (await client.put(f"/api/rules/{rule['id']}", json=update)).json()["enabled"] is False
    wrong_kind = {"kind": "ping", "name": "x", "config": {"targets": [ALICE]}}
    assert (await client.put(f"/api/rules/{rule['id']}", json=wrong_kind)).status_code == 422
    assert (await client.put("/api/rules/999", json=RULE_REPLY)).status_code == 404

    assert len((await client.get("/api/rules")).json()) == 1
    assert (await client.delete(f"/api/rules/{rule['id']}")).status_code == 200
    assert (await client.delete(f"/api/rules/{rule['id']}")).status_code == 404


async def test_rule_limits_protect_the_network(client):
    too_fast = {"kind": "ping", "name": "x", "config": {"targets": [ALICE], "interval_min": 1}}
    assert (await client.post("/api/rules", json=too_fast)).status_code == 422
    too_long = {**RULE_REPLY, "config": {"match": "ping", "reply": "x" * 400}}
    assert (await client.post("/api/rules", json=too_long)).status_code == 422
    short_cooldown = {**RULE_REPLY, "config": {"match": "ping", "reply": "x", "cooldown_s": 1}}
    assert (await client.post("/api/rules", json=short_cooldown)).status_code == 422
    bad_target = {"kind": "ping", "name": "x", "config": {"targets": ["zz"]}}
    assert (await client.post("/api/rules", json=bad_target)).status_code == 422


async def test_pause_endpoint(client):
    r = await client.post("/api/automation/pause", json={"paused": True})
    assert r.json()["paused"] is True
    state = (await client.get("/api/automation")).json()
    assert state["paused"] is True and state["budget"]["max_per_hour"] == 1000
    assert any("pausiert" in e["text"] for e in state["log"])


async def test_rule_errors_are_readable(client):
    too_fast = {"kind": "ping", "name": "x", "config": {"targets": [ALICE], "interval_min": 1}}
    detail = (await client.post("/api/rules", json=too_fast)).json()["detail"]
    assert "interval_min" in detail and "validation error for" not in detail
    bad_key = {"kind": "ping", "name": "x", "config": {"targets": ["zz"]}}
    detail = (await client.post("/api/rules", json=bad_key)).json()["detail"]
    assert "64 Hex-Zeichen" in detail and "Value error" not in detail


async def test_config_exposes_watchdog_port(client):
    r = await client.get("/api/config")
    assert r.json()["watchdog_port"] == 8100


async def test_channel_ping_rule_validation(client):
    ok = {"kind": "channel_ping", "name": "K", "config": {"channel": 1, "interval_min": 15, "text": "ping"}}
    assert (await client.post("/api/rules", json=ok)).status_code == 200
    too_fast = {**ok, "config": {**ok["config"], "interval_min": 5}}
    r = await client.post("/api/rules", json=too_fast)
    assert r.status_code == 422 and "interval_min" in r.text
    assert (await client.post("/api/rules", json={**ok, "config": {"channel": 99, "interval_min": 15}})).status_code == 422


async def test_trace_ping_via_api_and_in_nodes_list(client):
    r = await client.post(f"/api/nodes/{REPEATER_LOW}/ping", json={"mode": "trace"})
    assert r.status_code == 200 and r.json()["ok"] is True
    nodes = {n["public_key"]: n for n in (await client.get("/api/nodes")).json()}
    assert nodes[REPEATER_LOW]["last_ping"]["mode"] == "trace"
    assert nodes[REPEATER_LOW]["last_ping"]["snr_out"] is not None
    assert (await client.post(f"/api/nodes/{REPEATER_LOW}/ping", json={"mode": "bogus"})).status_code == 422


def test_old_database_gets_snr_columns(tmp_path):
    import sqlite3
    from app.storage import Storage
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE pings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, public_key TEXT NOT NULL, "
               "ok INTEGER NOT NULL, rtt_ms REAL, mode TEXT NOT NULL)")
    db.execute("INSERT INTO pings(ts, public_key, ok, rtt_ms, mode) VALUES(1, 'k', 1, 5.0, 'status')")
    db.commit(); db.close()
    s = Storage(path)
    s.add_ping("k", True, 1.0, "trace", snr_out=3.5, snr_in=2.0)
    hist = s.ping_history("k", 0)
    assert hist[0]["snr_out"] is None and hist[1]["snr_out"] == 3.5


async def test_auto_reply_rule_with_channels_and_limits(client):
    rule = {"kind": "auto_reply", "name": "K", "config": {
        "match": "ping", "reply": "pong", "cooldown_s": 1800, "max_per_hour": 10, "direct": False, "channels": [1, 0, 1]}}
    r = await client.post("/api/rules", json=rule)
    assert r.status_code == 200 and r.json()["config"]["channels"] == [0, 1]
    nothing = {**rule, "config": {**rule["config"], "channels": []}}
    r = await client.post("/api/rules", json=nothing)
    assert r.status_code == 422 and "Kanal" in r.text
    assert (await client.post("/api/rules", json={**rule, "config": {**rule["config"], "channels": [40]}})).status_code == 422
    assert (await client.post("/api/rules", json={**rule, "config": {**rule["config"], "max_per_hour": 0}})).status_code == 422
    old = {"kind": "auto_reply", "name": "alt", "config": {"match": "ping", "reply": "pong"}}
    cfg = (await client.post("/api/rules", json=old)).json()["config"]
    assert (cfg["direct"], cfg["channels"], cfg["max_per_hour"]) == (True, [], 10)
