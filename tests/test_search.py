"""Suche und Stichwort-Benachrichtigung."""
from tests.test_push import subscribe, sent  # noqa: F401  (Fixture + Hilfe)
from tests.conftest import ALICE


async def test_search_finds_substrings_case_insensitive_and_escapes(client, rt):
    await rt.store.add_message("in", "channel", "0", "Treffen am Hügel 100% sicher", name="Alice")
    await rt.store.add_message("in", "direct", ALICE[:12], "hallo_welt", name="Alice")
    await rt.store.add_message("out", "direct", ALICE[:12], "nix")
    r = (await client.get("/api/search", params={"q": "TREFFEN"})).json()["results"]
    assert len(r) == 1 and r[0]["where"] == "Public"
    assert len((await client.get("/api/search", params={"q": "100%"})).json()["results"]) == 1
    assert len((await client.get("/api/search", params={"q": "o_w"})).json()["results"]) == 1      # _ ist kein Joker
    assert len((await client.get("/api/search", params={"q": "ll", "kind": "channel"})).json()["results"]) == 0
    assert (await client.get("/api/search", params={"q": "x"})).status_code == 422
    assert (await client.get("/api/search", params={"q": "' OR 1=1 --"})).json()["results"] == []


async def test_keywords_validation(client):
    assert (await client.put("/api/push/keywords", json={"words": ["Notfall", "notfall", " CQ "]})).json()["words"] == ["Notfall", "CQ"]
    assert (await client.get("/api/push/keywords")).json()["words"] == ["Notfall", "CQ"]
    assert (await client.put("/api/push/keywords", json={"words": ["x"]})).status_code == 422
    assert (await client.put("/api/push/keywords", json={"words": ["abc"] * 11})).status_code == 422


async def test_keyword_push_even_for_muted_channel(rt, client, sent):
    await subscribe(client, channels=[])                       # Kanal nicht gewählt
    await client.put("/api/push/keywords", json={"words": ["notfall"]})
    await rt.node.inject_channel_message(0, "Alice (Sim)", "harmlos")
    await rt.node.inject_channel_message(0, "Alice (Sim)", "Das ist ein NOTFALL!")
    await rt.node.inject_message(ALICE, "Notfall bei mir")
    await rt.hub.drain(); await rt.push.drain()
    bodies = sorted(p["body"] for _, p in sent)
    assert bodies == ["Alice (Sim): Das ist ein NOTFALL!", "Notfall bei mir"] and {p["tag"] for _, p in sent} == {"kw-notfall"}


async def test_keyword_check_failure_does_not_break_messages(rt, client, sent):
    async def boom(*a, **k): raise RuntimeError("db")
    rt.store.get_setting = boom
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hallo")
    await rt.hub.drain()
    assert len(await rt.store.channel_messages(0)) == 1
