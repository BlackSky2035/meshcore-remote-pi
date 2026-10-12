from app.node.base import check_channel, derive_secret
from app.node import NodeError
import pytest


async def test_list_default_channels(client):
    r = await client.get("/api/channels")
    assert r.status_code == 200
    data = r.json()
    assert data["max_channels"] == 40
    names = {c["idx"]: c["name"] for c in data["channels"]}
    assert names == {0: "Public", 1: "#test"}
    assert all("secret" not in c for c in data["channels"])      # Schlüssel stehen nie in der Liste
    assert next(c for c in data["channels"] if c["idx"] == 1)["hashtag"] is True


async def test_add_private_channel_takes_first_free_slot_and_generates_key(client, rt):
    r = await client.post("/api/channels", json={"name": "Familie"})
    assert r.status_code == 200 and r.json()["idx"] == 2
    secret = (await client.get("/api/channels/2/secret")).json()["secret"]
    assert len(secret) == 32 and secret != "00" * 16
    assert rt.node._channels[2][0] == "Familie"                  # wirklich "auf den Node geschrieben"


async def test_add_with_given_secret_and_slot(client):
    key = "0123456789abcdef0123456789abcdef"
    r = await client.post("/api/channels", json={"idx": 5, "name": "Funkgruppe", "secret": key.upper()})
    assert r.status_code == 200 and r.json()["idx"] == 5
    assert (await client.get("/api/channels/5/secret")).json()["secret"] == key


async def test_hashtag_channel_derives_key_from_name(client):
    r = await client.post("/api/channels", json={"name": "#beispiel", "secret": "11" * 16})
    assert r.status_code == 200 and r.json()["hashtag"] is True
    s = (await client.get(f"/api/channels/{r.json()['idx']}/secret")).json()["secret"]
    assert s == derive_secret("#beispiel").hex()                   # mitgegebener Schlüssel wird ignoriert


async def test_add_validation(client):
    assert (await client.post("/api/channels", json={"name": "  "})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "x" * 40})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "#"})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "a", "secret": "zz"})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "a", "secret": "ab" * 8})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "a", "secret": "00" * 16})).status_code == 422
    assert (await client.post("/api/channels", json={"name": "a", "idx": 0})).status_code == 409   # belegt
    assert (await client.post("/api/channels", json={"name": "a", "idx": 40})).status_code == 422    # über der Obergrenze


async def test_slots_run_out(client):
    for i in range(38):
        assert (await client.post("/api/channels", json={"name": f"k{i}"})).status_code == 200
    r = await client.post("/api/channels", json={"name": "zu viel"})
    assert r.status_code == 409 and "belegt" in r.json()["detail"]


async def test_rename_keeps_key_and_history(client, rt):
    await client.post("/api/channels", json={"name": "Alt"})
    before = (await client.get("/api/channels/2/secret")).json()["secret"]
    await rt.node.inject_channel_message(2, "Alice (Sim)", "hallo")
    await rt.hub.drain()
    r = await client.put("/api/channels/2", json={"name": "Neu"})
    assert r.status_code == 200 and r.json()["name"] == "Neu"
    assert (await client.get("/api/channels/2/secret")).json()["secret"] == before
    assert len((await client.get("/api/channels/2/messages")).json()) == 1


async def test_new_key_clears_history(client, rt):
    await client.post("/api/channels", json={"name": "Alt"})
    await rt.node.inject_channel_message(2, "Bob", "x")
    await rt.hub.drain()
    await client.put("/api/channels/2", json={"name": "Alt", "secret": "ab" * 16})
    assert (await client.get("/api/channels/2/messages")).json() == []


async def test_change_unknown_channel_404(client):
    assert (await client.put("/api/channels/7", json={"name": "x"})).status_code == 404
    assert (await client.delete("/api/channels/7")).status_code == 404
    assert (await client.get("/api/channels/7/secret")).status_code == 404


async def test_delete_removes_from_node_and_history(client, rt):
    await rt.node.inject_channel_message(1, "Bob", "weg damit")
    await rt.hub.drain()
    assert (await client.delete("/api/channels/1")).status_code == 200
    assert 1 not in rt.node._channels
    assert [c["idx"] for c in (await client.get("/api/channels")).json()["channels"]] == [0]
    assert (await client.get("/api/channels/1/messages")).json() == []


async def test_incoming_channel_message_is_split_and_stored(client, rt):
    await rt.node.inject_channel_message(0, "Alice (Sim)", "Hallo: Welt")
    await rt.hub.drain()
    msgs = (await client.get("/api/channels/0/messages")).json()
    assert len(msgs) == 1
    assert (msgs[0]["name"], msgs[0]["text"], msgs[0]["direction"]) == ("Alice (Sim)", "Hallo: Welt", "in")


async def test_messages_are_separated_per_channel(client, rt):
    await rt.node.inject_channel_message(0, "A", "eins")
    await rt.node.inject_channel_message(1, "B", "zwei")
    await rt.hub.drain()
    assert [m["text"] for m in (await client.get("/api/channels/0/messages")).json()] == ["eins"]
    assert [m["text"] for m in (await client.get("/api/channels/1/messages")).json()] == ["zwei"]


async def test_send_to_channel_stores_outgoing(client, rt):
    r = await client.post("/api/channel", json={"channel": 1, "text": "Servus"})
    assert r.status_code == 200
    assert rt.node.sent_channel == [(1, "Servus")]
    msgs = (await client.get("/api/channels/1/messages")).json()
    assert [(m["direction"], m["text"]) for m in msgs] == [("out", "Servus")]


async def test_send_to_unconfigured_channel_fails(client):
    assert (await client.post("/api/channel", json={"channel": 6, "text": "x"})).status_code == 502


async def test_last_activity_in_list(client, rt):
    await rt.node.inject_channel_message(0, "A", "x")
    await rt.hub.drain()
    ch = {c["idx"]: c for c in (await client.get("/api/channels")).json()["channels"]}
    assert ch[0]["last_ts"] is not None and ch[1]["last_ts"] is None


def test_check_channel_unit():
    assert check_channel("#a", None)[1] == derive_secret("#a")
    with pytest.raises(NodeError):
        check_channel("Privat", None)


# ---- Pfade und gehörte Wiederholungen ---------------------------------------------
async def test_incoming_message_keeps_path_hops_and_signal(client, rt):
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hi", path="d4c3", snr=6.25, rssi=-88)
    await rt.hub.drain()
    m = (await client.get("/api/channels/0/messages")).json()[0]
    assert (m["path"], m["hops"], m["snr"], m["rssi"], m["path_hash_size"]) == ("d4c3", 2, 6.25, -88, 1)


async def test_incoming_without_path_info_is_still_stored(client, rt):
    await rt.node.inject_channel_message(0, "Bob", "ohne", path="", snr=None)
    await rt.hub.drain()
    m = (await client.get("/api/channels/0/messages")).json()[0]
    assert m["path"] is None and m["hops"] == 0 and m["snr"] is None


async def _send(client, rt, idx, text):
    assert (await client.post("/api/channel", json={"channel": idx, "text": text})).status_code == 200
    msg = (await client.get(f"/api/channels/{idx}/messages")).json()[-1]
    assert msg["sender_ts"] is not None
    return msg


async def test_heard_repeats_are_attached_to_sent_message(client, rt):
    msg = await _send(client, rt, 1, "Servus")
    await rt.node.inject_repeat(1, msg["sender_ts"], "Servus", path="c3", snr=5.0, rssi=-90)
    await rt.node.inject_repeat(1, msg["sender_ts"], "Servus", path="d4c3", snr=1.5, rssi=-101)
    await rt.hub.drain()
    out = (await client.get("/api/channels/1/messages")).json()[-1]
    # Die Ereignisse werden nebenläufig verarbeitet: Reihenfolge nicht garantiert, Inhalt schon.
    assert {(r["path"], r["snr"]) for r in out["repeats"]} == {("c3", 5.0), ("d4c3", 1.5)}
    assert any(e["type"] == "channel_repeat" for e in rt.hub.history)


async def test_same_repeat_path_is_not_counted_twice(client, rt):
    msg = await _send(client, rt, 1, "doppelt")
    for _ in range(3):
        await rt.node.inject_repeat(1, msg["sender_ts"], "doppelt", path="c3")
    await rt.hub.drain()
    assert len((await client.get("/api/channels/1/messages")).json()[-1]["repeats"]) == 1


async def test_foreign_or_mismatching_packets_are_not_repeats(client, rt):
    msg = await _send(client, rt, 1, "echt")
    await rt.node.inject_repeat(1, msg["sender_ts"], "anderer text")             # anderer Text
    await rt.node.inject_repeat(1, msg["sender_ts"] + 5, "echt")                  # anderer Zeitstempel
    await rt.node.inject_repeat(0, msg["sender_ts"], "echt")                      # anderer Kanal
    await rt.hub.drain()
    assert (await client.get("/api/channels/1/messages")).json()[-1]["repeats"] == []


async def test_repeats_are_removed_with_the_channel(client, rt):
    msg = await _send(client, rt, 1, "weg")
    await rt.node.inject_repeat(1, msg["sender_ts"], "weg")
    await rt.hub.drain()
    await client.delete("/api/channels/1")
    assert rt.raw_store._all("SELECT * FROM msg_repeats") == []


async def test_view_setting_default_off_per_channel_and_persisted(client):
    chans = {c["idx"]: c for c in (await client.get("/api/channels")).json()["channels"]}
    assert chans[0]["show_details"] is False and chans[1]["show_details"] is False
    assert (await client.put("/api/channels/1/view", json={"show_details": True})).status_code == 200
    chans = {c["idx"]: c for c in (await client.get("/api/channels")).json()["channels"]}
    assert chans[1]["show_details"] is True and chans[0]["show_details"] is False
    assert (await client.put("/api/channels/6/view", json={"show_details": True})).status_code == 404
    assert (await client.put("/api/channels/1/view", json={})).status_code == 422


async def test_view_setting_resets_when_channel_is_replaced(client):
    await client.put("/api/channels/1/view", json={"show_details": True})
    await client.put("/api/channels/1", json={"name": "#test", "secret": "11" * 16})   # Hashtag: Schlüssel gleich
    assert [c for c in (await client.get("/api/channels")).json()["channels"] if c["idx"] == 1][0]["show_details"] is True
    await client.put("/api/channels/0", json={"name": "Public", "secret": "ab" * 16})  # neuer Schlüssel
    await client.put("/api/channels/0/view", json={"show_details": True})
    await client.put("/api/channels/0", json={"name": "Public", "secret": "cd" * 16})
    assert [c for c in (await client.get("/api/channels")).json()["channels"] if c["idx"] == 0][0]["show_details"] is False
    await client.delete("/api/channels/1")
    await client.post("/api/channels", json={"idx": 1, "name": "Neu"})
    assert [c for c in (await client.get("/api/channels")).json()["channels"] if c["idx"] == 1][0]["show_details"] is False


def test_old_message_table_gets_new_columns(tmp_path):
    import sqlite3
    from app.storage import Storage
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, direction TEXT NOT NULL, "
               "kind TEXT NOT NULL, peer TEXT NOT NULL, name TEXT, text TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO messages(ts, direction, kind, peer, text) VALUES(1,'in','channel','0','alt')")
    db.commit(); db.close()
    s = Storage(path)
    s.add_message("in", "channel", "0", "neu", path="c3", hops=1, snr=2.0)
    rows = s.channel_messages(0)
    assert [(r["text"], r["path"]) for r in rows] == [("alt", None), ("neu", "c3")]


def test_prune_removes_orphan_repeats(tmp_path):
    from app.storage import Storage
    s = Storage(tmp_path / "p.db")
    old = s.add_message("out", "channel", "0", "x", ts=1.0, sender_ts=1)
    new = s.add_message("out", "channel", "0", "y", sender_ts=2)
    s.add_repeat(old, "c3"); s.add_repeat(new, "c3")
    s.prune(30)
    assert [r["message_id"] for r in s._all("SELECT message_id FROM msg_repeats")] == [new]
