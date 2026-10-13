"""Sicherung/Wiederherstellung: Inhalt, Trockenlauf, Prüfung, Bestätigung, Konflikte."""
import json

import pytest

from app import configbackup as CB


async def _setup(client):
    await client.post("/api/channels", json={"idx": 3, "name": "Familie", "secret": "ab" * 16})
    await client.post("/api/channels", json={"idx": 4, "name": "#lokal"})
    await client.post("/api/rules", json={"kind": "advert", "name": "Adv", "config": {"interval_min": 360}})


async def test_export_without_and_with_keys(client):
    await _setup(client)
    r = await client.get("/api/backup/export")
    assert "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["format"] == CB.FORMAT and d["node"]["radio_freq"] == 869.525 and d["rules"][0]["kind"] == "advert"
    assert "name" not in d["node"] and "lat" not in d["node"] and "lon" not in d["node"]       # gerätespezifisch, bewusst nicht dabei
    assert all("secret" not in c for c in d["channels"]) and "private" not in json.dumps(d).lower()
    assert all("secret" in c for c in (await client.get("/api/backup/export?channel_keys=true")).json()["channels"])
    assert any(c["name"].startswith("Alice") for c in d["contacts"])


async def test_roundtrip_restore_after_changes(client, rt):
    await _setup(client)
    backup = (await client.get("/api/backup/export?channel_keys=true")).json()
    await client.put("/api/node/settings", json={"changes": {"multi_acks": 1, "tx_power": 5}})
    await client.delete("/api/channels/3")
    rid = (await client.get("/api/rules")).json()[0]["id"]
    await client.delete(f"/api/rules/{rid}")
    dry = (await client.post("/api/backup/restore", json={"data": backup, "dry_run": True})).json()["plan"]
    assert set(dry["node"]) == {"multi_acks", "tx_power"} and "_secret" not in json.dumps(dry)
    r = await client.post("/api/backup/restore", json={"data": backup})
    assert r.status_code == 200, r.text
    assert rt.node.cfg["multi_acks"] == 0 and rt.node.cfg["tx_power"] == 14
    assert any(c["name"] == "Familie" for c in (await client.get("/api/channels")).json()["channels"])
    assert len((await client.get("/api/rules")).json()) == 1
    again = (await client.post("/api/backup/restore", json={"data": backup})).json()
    assert again["report"] == ["Nichts zu tun: alles ist schon so."]


async def test_risky_restore_needs_confirm(client, rt):
    backup = (await client.get("/api/backup/export")).json()
    backup["node"]["radio_freq"] = 868.0
    r = await client.post("/api/backup/restore", json={"data": backup, "sections": ["node"]})
    assert r.status_code == 409 and rt.node.cfg["radio_freq"] == 869.525
    assert (await client.post("/api/backup/restore", json={"data": backup, "sections": ["node"], "confirm": True})).status_code == 200
    assert rt.node.cfg["radio_freq"] == 868.0


async def test_channel_conflict_and_missing_key(client):
    await _setup(client)
    backup = (await client.get("/api/backup/export")).json()                      # ohne Schlüssel
    plan = (await client.post("/api/backup/restore", json={"data": backup, "sections": ["channels"], "dry_run": True})).json()["plan"]
    acts = {c["name"]: c["action"] for c in plan["channels"]}
    assert acts["Familie"] == "skip" and acts["#lokal"] == "same"
    full = (await client.get("/api/backup/export?channel_keys=true")).json()
    for c in full["channels"]:
        if c["name"] == "Familie":
            c["secret"] = "cd" * 16                                                     # Platz belegt mit anderem Schlüssel
    rep = (await client.post("/api/backup/restore", json={"data": full, "sections": ["channels"]})).json()["report"]
    assert any("übersprungen" in x for x in rep)
    rep = (await client.post("/api/backup/restore", json={"data": full, "sections": ["channels"], "overwrite_channels": True})).json()["report"]
    assert any("geschrieben" in x for x in rep)


@pytest.mark.parametrize("mut", [
    lambda d: d.update(format="x"), lambda d: d.update(version=99), lambda d: d["node"].update(tx_power=99),
    lambda d: d["node"].update(radio_bw=100), lambda d: d["rules"].append({"kind": "advert", "name": "x", "config": {"interval_min": 5}}),
    lambda d: d["rules"].append({"kind": "nope", "name": "x", "config": {}}), lambda d: d["rules"].append("kaputt"),
    lambda d: d["channels"].append({"idx": 99, "name": "x", "secret": "ab" * 16}), lambda d: d["channels"].append({"idx": 5, "name": "x", "secret": "zz"}),
    lambda d: d.update(rules=[{"kind": "advert", "name": "r", "config": {"interval_min": 360}}] * 101),
])
async def test_invalid_files_are_rejected_before_anything_changes(client, rt, mut):
    await _setup(client)
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    d["node"]["multi_acks"] = 1
    mut(d)
    before = len((await client.get("/api/rules")).json())
    r = await client.post("/api/backup/restore", json={"data": d})
    assert r.status_code == 422
    assert rt.node.cfg["multi_acks"] == 0 and len((await client.get("/api/rules")).json()) == before        # nichts geschrieben


async def test_unknown_section_and_non_object(client):
    d = (await client.get("/api/backup/export")).json()
    assert (await client.post("/api/backup/restore", json={"data": d, "sections": ["alles"]})).status_code == 422
    assert (await client.post("/api/backup/restore", json={"data": []})).status_code == 422


async def test_not_connected_is_502(client, rt):
    d = (await client.get("/api/backup/export")).json()
    await rt.node.disconnect()
    assert (await client.get("/api/backup/export")).status_code == 502
    assert (await client.post("/api/backup/restore", json={"data": d})).status_code == 502


# ---- Form der Datei, Kanalplätze, Reihenfolge und Aufräumen ------------------------------------------
@pytest.mark.parametrize("mut", [
    lambda d: d.update(node=[1, 2]), lambda d: d.update(node="x"), lambda d: d.update(rules={"a": 1}),
    lambda d: d.update(rules=5), lambda d: d.update(channels="abc"), lambda d: d.update(channels=7),
    lambda d: d["node"].update(radio_bw="breit"), lambda d: d["node"].update(radio_bw=None),
    lambda d: d["node"].update(radio_bw=[250]), lambda d: d["node"].update(radio_freq={"x": 1}),
])
async def test_wrong_types_are_422_not_500(client, rt, mut):
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    mut(d)
    r = await client.post("/api/backup/restore", json={"data": d, "dry_run": True})
    assert r.status_code == 422, r.text


async def test_channel_slot_checked_against_node_limit(client, rt):
    rt.node.max_ch = 8
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    d["channels"].append({"idx": 8, "name": "x", "secret": "ab" * 16})            # Node hat nur 0–7
    r = await client.post("/api/backup/restore", json={"data": d, "sections": ["channels"], "dry_run": True})
    assert r.status_code == 422 and "8" in r.json()["detail"]


async def test_duplicate_channel_slot_is_rejected(client, rt):
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    d["channels"] = [{"idx": 5, "name": "a", "secret": "ab" * 16}, {"idx": 5, "name": "b", "secret": "cd" * 16}]
    r = await client.post("/api/backup/restore", json={"data": d, "sections": ["channels"], "dry_run": True})
    assert r.status_code == 422 and "doppelt" in r.json()["detail"]


async def test_channel_failure_leaves_node_settings_untouched(client, rt):
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    d["node"]["multi_acks"] = 1
    d["channels"].append({"idx": 6, "name": "Neu", "secret": "ab" * 16})

    async def broken(*a, **k):
        from app.node import NodeError
        raise NodeError("Kanal 6: Schreiben nicht bestätigt")
    rt.node.set_channel = broken
    r = await client.post("/api/backup/restore", json={"data": d, "sections": ["node", "channels"]})
    assert r.status_code == 502
    assert rt.node.cfg["multi_acks"] == 0                                          # Node-Einstellungen erst ganz zum Schluss


async def test_restore_clears_view_and_read_markers_of_written_slot(client, rt):
    await rt.store.set_setting("channel_view_6", {"paths": True})
    await rt.store.set_setting("channel_read_6", 17)
    d = (await client.get("/api/backup/export?channel_keys=true")).json()
    d["channels"].append({"idx": 6, "name": "Neu", "secret": "ab" * 16})
    r = await client.post("/api/backup/restore", json={"data": d, "sections": ["channels"]})
    assert r.status_code == 200, r.text
    assert await rt.store.get_setting("channel_view_6", None) is None
    assert await rt.store.get_setting("channel_read_6", None) is None
