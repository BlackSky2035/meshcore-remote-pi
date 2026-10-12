"""Netzkarte: Pfade auflösen, Nachbarlisten speichern, API."""
from app import network as NW
from app.node.base import normalize_contact

C = [normalize_contact(x) for x in (
    {"public_key": "a1" * 32, "name": "Alice", "type": 1, "path_len": 2, "out_path": "c3d4"},
    {"public_key": "c3" * 32, "name": "Hügel", "type": 2, "path_len": 0},
    {"public_key": "d4" * 32, "name": "Dach", "type": 2, "path_len": 1, "out_path": "c3"},
    {"public_key": "b2" * 32, "name": "Bob", "type": 1, "path_len": 1, "out_path": "99"},
    {"public_key": "e5" * 32, "name": "Raum", "type": 3, "path_len": -1},
)]


def test_normalize_contact_path():
    assert C[0]["path"] == ["c3", "d4"] and C[1]["path"] == [] and C[4]["path"] == []
    assert normalize_contact({"public_key": "a" * 64, "path_len": 3, "out_path": "aabb"})["path"] == ["aa", "bb"]    # kürzer als angegeben: nimmt, was da ist
    assert normalize_contact({"public_key": "a" * 64, "path_len": 1, "out_path": "zz1"})["path"] == ["zz"]       # kaputte Daten werfen nicht


def test_graph_resolves_hops_and_unknown():
    g = NW.build_graph(C, [])
    names = {n["id"]: n["name"] for n in g["nodes"]}
    alice = next(r for r in g["routes"] if r["name"] == "Alice")
    assert alice["hops"] == ["Hügel", "Dach"]
    bob = next(r for r in g["routes"] if r["name"] == "Bob")
    assert bob["hops"] == ["? 99"] and "?99" in names
    assert any(e["a"] == "me" and e["b"] == "c3" * 32 and e["kind"] == "path" for e in g["edges"])      # Hügel direkt
    assert next(r for r in g["routes"] if r["name"] == "Raum")["path_len"] == -1
    # Pfad noch unbekannt (Flood): trotzdem eine eigene Kante, damit der Kontakt nicht unverbunden wirkt.
    assert any(e["a"] == "me" and e["b"] == "e5" * 32 and e["kind"] == "flood" for e in g["edges"])


def test_graph_with_neighbour_reports_and_ambiguity():
    g = NW.build_graph(C, [{"repeater": "c3" * 32, "prefix": "d4" * 6, "snr": 6.5, "ts": 1.0},
                           {"repeater": "c3" * 32, "prefix": "ff" * 6, "snr": -3.0, "ts": 1.0},
                           {"repeater": "nicht-bekannt", "prefix": "aa", "snr": 1, "ts": 1}])
    nb = [e for e in g["edges"] if e["kind"] == "neighbour"]
    assert len(nb) == 2 and {e["snr"] for e in nb} == {6.5, -3.0}
    dup = [normalize_contact({"public_key": "c3" + "1" * 62, "name": "X", "type": 2}), *C]
    g2 = NW.build_graph(dup, [])
    assert any("mehrdeutig" in n["name"] for n in g2["nodes"])


def test_split_path_respects_hash_size():
    assert NW._split_path("a1b2c3d4", 1) == ["a1", "b2", "c3", "d4"]
    assert NW._split_path("a1b2c3d4", 2) == ["a1b2", "c3d4"]
    assert NW._split_path("", 1) == []
    assert NW._split_path("a1b", 1) == ["a1"]      # ungerader Rest wird verworfen, nicht geraten


def test_graph_adds_heard_edges_from_observed_message_paths():
    """Auch ohne vom Node gemeldeten Pfad soll eine tatsächlich gehörte Kanalnachricht eine Linie ergeben."""
    g = NW.build_graph(C, [], heard=[{"path": "c3d4", "path_hash_size": 1}])
    names = {n["id"]: n["name"] for n in g["nodes"]}
    heard_edges = [e for e in g["edges"] if e["kind"] == "heard"]
    assert len(heard_edges) == 2        # Hügel(c3)->Dach(d4), Dach(d4)->me
    assert any(e["a"] == "c3" * 32 and e["b"] == "d4" * 32 for e in heard_edges)
    assert any(e["b"] == "me" and names[e["a"]] == "Dach" for e in heard_edges)


def test_graph_drops_heard_edges_touching_unknown_hops():
    """Eine nicht auflösbare Zwischenstation ("ff") bekommt keinen eigenen Punkt und wird nicht überbrückt -
    nur die Teilstrecke zwischen zwei unmittelbar aufeinanderfolgenden, bekannten Stationen bleibt übrig."""
    g = NW.build_graph(C, [], heard=[{"path": "c3ffd4", "path_hash_size": 1}])     # c3 (bekannt) -> ff (unbekannt) -> d4 (bekannt)
    assert "?ff" not in {n["id"] for n in g["nodes"]}      # "Bob" hat unabhängig davon eine eigene "?99"-Station im Pfad
    heard_edges = [e for e in g["edges"] if e["kind"] == "heard"]
    assert len(heard_edges) == 1
    assert {heard_edges[0]["a"], heard_edges[0]["b"]} == {"d4" * 32, "me"}


def test_graph_ignores_empty_or_missing_heard_paths():
    assert NW.build_graph(C, [], heard=[{"path": "", "path_hash_size": 1}, {}])["edges"] == \
        NW.build_graph(C, [])["edges"]
    assert NW.build_graph(C, [])["edges"] == NW.build_graph(C, [], heard=None)["edges"]


async def test_recent_incoming_paths_only_returns_incoming_with_a_path(rt):
    await rt.store.add_message("in", "channel", "0", "hallo", path="c3d4", path_hash_size=1)
    await rt.store.add_message("in", "channel", "0", "ohne pfad")          # path=None: nicht enthalten
    await rt.store.add_message("out", "channel", "0", "meine eigene")      # ausgehend: nicht enthalten
    paths = await rt.store.recent_incoming_paths()
    assert paths == [{"path": "c3d4", "path_hash_size": 1}]


async def test_api_network_and_neighbour_storage(client, rt):
    key = "c3" * 32
    assert (await client.post(f"/api/repeater/{key}/login", json={"password": "sim"})).status_code == 200
    assert (await client.get(f"/api/repeater/{key}/neighbours")).status_code == 200
    g = (await client.get("/api/network")).json()
    assert g["nodes"][0]["id"] == "me" and g["reports"][0]["repeater"] == key
    assert any(e["kind"] == "neighbour" for e in g["edges"])
    assert any(r["name"].startswith("Bob") and r["hops"] for r in g["routes"])


async def test_api_network_includes_heard_edges_from_message_history(client, rt):
    await rt.store.add_message("in", "channel", "0", "hallo", path="c3d4", path_hash_size=1)
    g = (await client.get("/api/network")).json()
    assert any(e["kind"] == "heard" for e in g["edges"])


async def test_neighbour_report_replaces_old_and_survives_db_failure(rt):
    await rt.store.add_neighbour_report(1.0, "c3" * 32, [{"prefix": "a1", "snr": 1}])
    await rt.store.add_neighbour_report(2.0, "c3" * 32, [{"prefix": "b2", "snr": 2}])
    assert [r["prefix"] for r in await rt.store.neighbour_reports()] == ["b2"]
    async def boom(*a, **k): raise RuntimeError("db kaputt")
    rt.store.add_neighbour_report = boom
    key = "c3" * 32
    await rt.repeater.login(key, "sim", False) if hasattr(rt.repeater, "login") else None
    res = await rt.repeater.neighbours(key)                # Speichern scheitert → Abfrage gelingt trotzdem
    assert res["neighbours"]


async def test_network_api_node_not_connected(client, rt):
    await rt.node.disconnect()
    assert (await client.get("/api/network")).status_code == 502
