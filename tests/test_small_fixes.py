"""Kleinere Absicherungen: Kanal-Ping nur auf eingerichtete Plätze, API-Beschreibung nur angemeldet, keine Weiterleitungen,
Rechte der Passwortdatei, Pfad-Kennung 1/2/3 Byte, Konfigurationsgrenzen, Index für die Ungelesen-Zählung, Sendeprotokoll."""
import http.server
import os
import stat
import threading
import urllib.error

import pytest
from httpx import ASGITransport, AsyncClient

from app import config as C
from app.main import create_app
from app.network import build_graph
from app.node.base import hop_bytes, normalize_contact, split_path, trace_hash_bytes
from app.repeater import PasswordVault
from app.sendlog import TX_KINDS
from app.webpush import no_redirect_open


# ---- Kanal-Ping ----------------------------------------------------------------
async def test_channel_ping_on_unconfigured_slot_is_rejected(client, rt):
    used = {c["idx"] for c in await rt.node.channels()}
    free = next(i for i in range(40) if i not in used)
    body = {"kind": "channel_ping", "name": "K", "config": {"channel": free, "interval_min": 15, "text": "ping"}}
    r = await client.post("/api/rules", json=body)
    assert r.status_code == 422 and "nicht eingerichtet" in r.json()["detail"]
    body["config"]["channel"] = min(used)
    assert (await client.post("/api/rules", json=body)).status_code == 200


async def test_channel_ping_without_channel_value_is_rejected(client):
    body = {"kind": "channel_ping", "name": "K", "config": {"channel": None, "interval_min": 15, "text": "ping"}}
    assert (await client.post("/api/rules", json=body)).status_code == 422      # leer wird nie still zu Platz 0


async def test_channel_check_failure_does_not_block_saving(client, rt):
    """Ausfall: Kanalliste nicht lesbar → Speichern trotzdem möglich (Prüfung fängt nur Fehlbedienung ab)."""
    async def boom():
        raise RuntimeError("Node antwortet nicht")
    rt.node.channels = boom
    body = {"kind": "channel_ping", "name": "K", "config": {"channel": 7, "interval_min": 15, "text": "ping"}}
    assert (await client.post("/api/rules", json=body)).status_code == 200


async def test_toggling_existing_rule_skips_channel_check(client, rt):
    rid = await rt.store.add_rule("channel_ping", "K", {"channel": 9, "interval_min": 15, "text": "ping"})   # Platz inzwischen gelöscht
    body = {"kind": "channel_ping", "name": "K", "enabled": False, "config": {"channel": 9, "interval_min": 15, "text": "ping"}}
    assert (await client.put(f"/api/rules/{rid}", json=body)).status_code == 200


# ---- API-Beschreibung ----------------------------------------------------------------
async def test_api_docs_need_login_when_password_is_set(rt):
    rt.settings.access_password = "geheim"
    app = create_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 401, path
        assert (await c.post("/api/auth/login", json={"password": "geheim"})).status_code == 200
        assert (await c.get("/openapi.json")).status_code == 200


async def test_api_docs_open_without_password(client):
    assert (await client.get("/openapi.json")).status_code == 200


# ---- Keine Weiterleitungen (Push, Stationen) ----------------------------------------------
@pytest.fixture
def redirect_server():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("X-Peer-Token")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/intern")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


def test_redirect_is_not_followed(redirect_server):
    import urllib.request
    base, hits = redirect_server
    req = urllib.request.Request(base + "/start", headers={"X-Peer-Token": "geheim"})
    with pytest.raises(urllib.error.HTTPError) as e:
        no_redirect_open(req, timeout=3)
    assert e.value.code == 302
    assert hits == [("/start", "geheim")]                 # das Ziel der Weiterleitung wurde nie aufgerufen


def test_peer_fetch_does_not_follow_redirect(redirect_server):
    from app import peers as P
    base, hits = redirect_server
    with pytest.raises(urllib.error.HTTPError):
        P.fetch_summary(P.Peer("x", base + "/start#", "geheim"))     # „#“: Pfad /api/peer/summary wird Teil des Fragments
    assert [h[0] for h in hits] == ["/start"]


# ---- Passwortdatei ----------------------------------------------------------------
def test_password_file_is_600_even_if_old_tmp_was_open(tmp_path):
    path = tmp_path / "repeater_secrets.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text("{}")
    os.chmod(tmp, 0o644)                                  # übrig gebliebene Datei mit offenen Rechten
    v = PasswordVault(path)
    v.set("ab" * 32, "pw")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---- Pfad-Kennung ----------------------------------------------------------------
@pytest.mark.parametrize("mode,size", [(0, 1), (1, 2), (2, 3), (None, 1), (-1, 1), (3, 1), ("x", 1)])
def test_hop_bytes(mode, size):
    assert hop_bytes(mode) == size


def test_split_path_by_hash_size():
    assert split_path("a1b2c3d4", 1) == ["a1", "b2", "c3", "d4"]
    assert split_path("a1b2c3d4", 2) == ["a1b2", "c3d4"]
    assert split_path("a1b2c3d4e5f6", 3) == ["a1b2c3", "d4e5f6"]
    assert split_path("a1b2c3d4", 2, hops=1) == ["a1b2"]
    assert split_path("a1b2c", 2) == ["a1b2"]            # unvollständiger Rest fällt weg


@pytest.mark.parametrize("mode,hops", [(0, ["c3", "d4"]), (1, ["c3c3", "d4d4"]), (2, ["c3c3c3", "d4d4d4"])])
def test_contact_path_uses_the_contacts_hash_size(mode, hops):
    raw = "".join(h for h in hops)
    c = normalize_contact({"public_key": "b2" * 32, "name": "Bob", "type": 1, "path_len": 2, "out_path": raw, "path_hash_mode": mode})
    assert c["path"] == hops and c["path_hash_size"] == mode + 1


def test_network_resolves_two_byte_contact_path():
    contacts = [normalize_contact(c) for c in (
        {"public_key": "c3" * 32, "name": "Hügel", "type": 2, "path_len": 0, "out_path": ""},
        {"public_key": "d4" * 32, "name": "Dach", "type": 2, "path_len": 1, "out_path": "c3"},
        {"public_key": "b2" * 32, "name": "Bob", "type": 1, "path_len": 2, "out_path": "d4d4c3c3", "path_hash_mode": 1},
    )]
    g = build_graph(contacts, [])
    bob = next(r for r in g["routes"] if r["name"] == "Bob")
    assert bob["hops"] == ["Dach", "Hügel"]
    assert not any(n["id"].startswith("?") for n in g["nodes"])          # nichts falsch zerschnitten


@pytest.mark.parametrize("mode,expected", [(0, (1, 0)), (1, (2, 1)), (2, (4, 2)), (None, (1, 0))])
def test_trace_hash_bytes(mode, expected):
    assert trace_hash_bytes(mode) == expected


async def test_sim_contacts_include_a_two_byte_path(rt):
    bob = next(c for c in await rt.node.contacts() if c["name"].startswith("Bob"))
    assert bob["path"] == ["d4d4", "c3c3"] and bob["path_hash_size"] == 2


def test_auto_reply_path_text_uses_message_hash_size():
    from app.automation import Automation
    assert Automation.path_text("d4d4c3c3", hop_bytes(1), 2) == "d4d4,c3c3"


# ---- Konfiguration ----------------------------------------------------------------
def _load(tmp_path, monkeypatch, text, **env):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MC_CONFIG", str(p))
    for k in [k for k in os.environ if k.startswith("MC_") and k != "MC_CONFIG"]:
        monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return C.load_settings()


def test_typos_are_clamped_with_a_note(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[node]\nmode = "serial"\n[automation]\nauto_min_gap_s = 0\nauto_max_per_hour = 3000\n'
                                     'manual_min_gap_s = 0\nmanual_max_per_min = 500\n[storage]\nretention_days = 0\n')
    assert (s.auto_min_gap_s, s.auto_max_per_hour, s.manual_min_gap_s, s.manual_max_per_min, s.retention_days) == (5.0, 60, 1.0, 30, 7)
    assert len(s.config_notes) == 5 and all("Schutz vor Tippfehlern" in n for n in s.config_notes)


def test_normal_values_stay_and_produce_no_note(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[node]\nmode = "serial"\n[automation]\nauto_min_gap_s = 20\nauto_max_per_hour = 40\n')
    assert (s.auto_min_gap_s, s.auto_max_per_hour, s.retention_days) == (20.0, 40, 90)
    assert s.config_notes == []


def test_defaults_are_within_limits():
    s = C.Settings(mode="serial")
    C.apply_limits(s)
    assert s.config_notes == []


def test_radio_limits_do_not_apply_in_simulator(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[automation]\nmanual_min_gap_s = 0\n[storage]\nretention_days = 1\n')
    assert s.mode == "sim" and s.manual_min_gap_s == 0 and s.retention_days == 7     # Aufbewahrung gilt immer


def test_wrong_type_keeps_default(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[node]\nmode = "serial"\n[automation]\nauto_max_per_hour = "viel"\n')
    assert s.auto_max_per_hour == 30 and "ungültig" in s.config_notes[0]


def test_broken_file_without_password_starts_with_defaults(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[server\nport = 8000\n')
    assert s.mode == "serial" and s.access_password == ""
    assert "fehlerhaft" in s.config_notes[0]


def test_broken_file_with_password_refuses_to_start(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="Zugangspasswort"):
        _load(tmp_path, monkeypatch, '[server\naccess_password = "geheim"\n')


def test_broken_file_with_password_from_environment_starts(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[server\naccess_password = "geheim"\n', MC_ACCESS_PASSWORD="geheim")
    assert s.access_password == "geheim"


def test_broken_file_with_empty_password_starts(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, '[server\naccess_password = ""\n')
    assert s.access_password == ""


async def test_config_notes_show_up_as_problems(rt):
    rt.settings.config_notes = ["Einstellung x ist zu groß"]
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert "Einstellung x ist zu groß" in (await c.get("/api/status")).json()["problems"]


# ---- Index, Sendeprotokoll ----------------------------------------------------------------
def test_unread_queries_use_an_index(tmp_path):
    from app.storage import Storage
    s = Storage(tmp_path / "x.db")
    plan = " ".join(str(tuple(r)) for r in s._db.execute(
        "EXPLAIN QUERY PLAN SELECT MIN(id) FROM messages WHERE kind='channel' AND direction='in' AND peer='1' AND id>5"))
    assert "idx_messages_kind_dir_peer_id" in plan
    s.close()


def test_repeater_logout_is_in_send_log():
    assert "repeater_logout" in TX_KINDS
