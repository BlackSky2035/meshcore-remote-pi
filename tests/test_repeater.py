"""Repeater verwalten: Regeln (Sperren, Bestätigung, Schwärzen, Tempo), Anmeldung, Passwort-Ablage, API und echter Node (Fake-Bibliothek)."""
import json
import os
import stat
from types import SimpleNamespace

import pytest

from app import repeater as R
from app.repeater import RepeaterError, classify
from app.config import Settings
from app.node import NodeError
from app.node.real import RealNode
from tests.conftest import ALICE, FakeClock, REPEATER_OK, REPEATER_LOW


@pytest.fixture
def adm(rt, tmp_path):
    clock = FakeClock()
    a = rt.repeater
    a._clock = clock
    a.vault = R.PasswordVault(tmp_path / "secrets.json")
    a.tick = lambda s=3: clock.advance(s)

    async def fake_sleep(s):
        clock.advance(s)                                              # Wartezeit des Tempolimits ohne echtes Warten
    a._sleep = fake_sleep
    return a


# ---- Befehle einstufen ------------------------------------------------------------------
def test_classify_blocked_risky_secret_and_noreply():
    for bad in ("erase", "set prv.key abcd", "get prv.key", "ERASE"):
        with pytest.raises(R.RepeaterError, match="gesperrt"):
            R.classify(bad)
    assert R.classify("reboot")["risky"] and R.classify("reboot")["no_reply"]
    assert R.classify("set radio 869.525,250,11,5")["risky"]
    assert R.classify("set repeat off")["risky"] and R.classify("set repeat on")["risky"] is None
    assert R.classify("password neu")["secret"] and R.classify("set guest.password x")["secret"]
    assert R.classify("get name")["risky"] is None and not R.classify("get name")["secret"]
    assert R.classify("  get   name  ")["command"] == "get name"           # Leerzeichen bereinigt


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "x" * 151, "get \x07name"])
def test_classify_rejects_invalid(bad):
    with pytest.raises(R.RepeaterError):
        R.classify(bad)


def test_build_setting_validates_ranges():
    assert R.build_setting("tx", "14") == "set tx 14"
    assert R.build_setting("txdelay", "0,5") == "set txdelay 0.5"
    assert R.build_setting("radio", "869.525, 250, 11, 5") == "set radio 869.525,250,11,5"
    assert R.build_setting("name", "Hügel 1") == "set name Hügel 1"
    assert R.build_setting("repeat", "OFF") == "set repeat off"
    for name, value in [("tx", "30"), ("tx", "abc"), ("flood.max", "65"), ("radio", "869,251,11,5"), ("radio", "869,250,13,5"),
                        ("radio", "1,2,3"), ("repeat", "maybe"), ("loop.detect", "wild"), ("advert.interval", "30"),
                        ("name", "x" * 40), ("name", ""), ("nope", "1"), ("lat", "91")]:
        with pytest.raises(R.RepeaterError):
            R.build_setting(name, value)
    assert R.build_setting("advert.interval", "0") == "set advert.interval 0"


# ---- Anmeldung und Befehle -------------------------------------------------------------------
async def test_login_wrong_password_and_success(adm):
    with pytest.raises(NodeError, match="Anmeldebestätigung"):
        await adm.login(REPEATER_OK, "falsch")
    assert not adm.status(REPEATER_OK)["logged_in"]
    st = await adm.login(REPEATER_OK, "sim")
    assert st["logged_in"] and st["admin"] and not st["saved_password"]


async def test_command_requires_login_and_only_for_repeaters(adm):
    with pytest.raises(R.RepeaterError, match="Nicht angemeldet"):
        await adm.command(REPEATER_OK, "ver")
    with pytest.raises(R.RepeaterError, match="nur für Repeater"):
        await adm.login(ALICE, "x")                                   # normaler Client
    with pytest.raises(R.RepeaterError, match="Unbekannter"):
        await adm.login("00" * 32, "x")


async def test_commands_and_settings_roundtrip(adm, rt):
    await adm.login(REPEATER_OK, "sim")
    r = await adm.command(REPEATER_OK, "ver")
    assert r["ok"] and "MeshCore" in r["text"]
    adm.tick()
    assert (await adm.setting(REPEATER_OK, "flood.max", 12))["replies"] == ["OK"]
    adm.tick()
    got = await adm.read_setting(REPEATER_OK, "flood.max")
    assert got["value"] == "12"
    assert rt.node.rep_cfg[REPEATER_OK]["flood.max"] == "12"


async def test_risky_needs_confirmation_and_noreply_ends_session(adm, rt):
    await adm.login(REPEATER_OK, "sim")
    with pytest.raises(R.NeedsConfirm, match="startet neu"):
        await adm.command(REPEATER_OK, "reboot")
    assert rt.node.rep_reboots == []                                  # nichts wurde gesendet
    r = await adm.command(REPEATER_OK, "reboot", confirm=True)
    assert r["ok"] and r["no_reply"] and rt.node.rep_reboots == [REPEATER_OK]
    assert not adm.status(REPEATER_OK)["logged_in"]


async def test_passwords_are_never_shown_logged_or_kept(adm, rt):
    await adm.login(REPEATER_OK, "sim")
    r = await adm.command(REPEATER_OK, "password Geheim123", confirm=True)
    assert "Geheim123" not in json.dumps(r) and r["command"] == "(geheim)"
    assert "Geheim123" not in json.dumps(adm.history(REPEATER_OK))
    logs = await rt.store.log(50)
    assert "Geheim123" not in json.dumps(logs)
    assert rt.node.rep_pw[REPEATER_OK] == "Geheim123"               # gesendet wurde es trotzdem


async def test_rate_limit_per_repeater(adm):
    await adm.login(REPEATER_OK, "sim")
    await adm.command(REPEATER_OK, "ver")
    with pytest.raises(R.RepeaterError, match="Zu schnell"):
        await adm.command(REPEATER_OK, "board")
    adm.tick()
    assert (await adm.command(REPEATER_OK, "board"))["ok"]


async def test_no_reply_is_reported_and_session_renewed(adm, rt):
    await adm.login(REPEATER_OK, "sim")
    rt.node.fail_status.add(REPEATER_OK)
    r = await adm.command(REPEATER_OK, "ver")
    assert not r["ok"] and "Keine Antwort" in r["text"]
    assert not adm.status(REPEATER_OK)["logged_in"]
    rt.node.fail_status.clear()
    adm.tick()
    assert (await adm.command(REPEATER_OK, "ver"))["ok"]            # still neu angemeldet mit Sitzungs-Passwort


async def test_neighbours_and_acl_resolve_names(adm):
    with pytest.raises(R.RepeaterError, match="Nicht angemeldet"):
        await adm.neighbours(REPEATER_OK)
    await adm.login(REPEATER_OK, "sim")
    n = await adm.neighbours(REPEATER_OK)
    assert n["neighbours"][0]["name"] == "Alice (Sim)"
    adm.tick()                                                        # Tempolimit gilt auch für Nachbarn/Zugriffsliste
    a = await adm.acl(REPEATER_OK)
    assert a["entries"][0]["perm_label"] == "Admin" and a["entries"][1]["perm_label"] == "Gast"


# ---- Passwort-Ablage ---------------------------------------------------------------------------
async def test_remember_password_file_is_private_and_separate(adm, tmp_path, rt):
    await adm.login(REPEATER_OK, "sim", remember=True)
    f = tmp_path / "secrets.json"
    assert json.loads(f.read_text()) == {REPEATER_OK: "sim"}
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert adm.status(REPEATER_OK)["saved_password"] is True
    adm._sessions.clear()
    adm._mem_pw.clear()                                               # „Neustart“: nur die Datei bleibt
    assert (await adm.login(REPEATER_OK))["logged_in"]                # Anmeldung ohne Eingabe
    adm.forget_password(REPEATER_OK)
    assert json.loads(f.read_text()) == {}


async def test_unwritable_vault_does_not_break_login(adm, tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    adm.vault = R.PasswordVault(blocker / "unterordner" / "s.json")
    st = await adm.login(REPEATER_OK, "sim", remember=True)
    assert st["logged_in"] and "nicht dauerhaft" in st["warning"]


def test_corrupt_vault_file_is_ignored(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{kaputt")
    assert R.PasswordVault(f).get("x") is None


# ---- API ------------------------------------------------------------------------------------------
async def test_api_flow_and_no_password_in_responses(client, rt, adm):
    base = f"/api/repeater/{REPEATER_OK}"
    assert (await client.get("/api/repeater/catalog")).json()["settings"][0]["key"] == "name"
    r = await client.post(f"{base}/login", json={"password": "falsch"})
    assert r.status_code == 502
    r = await client.post(f"{base}/login", json={"password": "sim", "remember": True})
    assert r.status_code == 200 and "sim" not in json.dumps(r.json()).replace("sim_", "").replace("Sim", "")
    assert (await client.get(base)).json()["saved_password"] is True
    r = await client.post(f"{base}/command", json={"command": "reboot"})
    assert r.status_code == 409 and "Bestätigung" in r.json()["detail"]
    assert (await client.post(f"{base}/command", json={"command": "erase"})).status_code == 422
    assert (await client.post(f"{base}/command", json={"command": "ver"})).json()["ok"] is True
    adm.tick()
    assert (await client.get(f"{base}/setting/name")).json()["value"] == "Repeater Hügel (Sim)"
    adm.tick()
    assert (await client.post(f"{base}/setting", json={"name": "tx", "value": 99})).status_code == 422
    assert (await client.get(f"{base}/neighbours")).json()["total"] == 2
    assert (await client.get(f"{base}/history")).json()[0]["command"] == "ver"
    assert (await client.delete(f"{base}/password")).json()["saved_password"] is False


async def test_api_for_client_contact_is_rejected(client):
    r = await client.post(f"/api/repeater/{ALICE}/login", json={"password": "x"})
    assert r.status_code == 422


# ---- Echter Node mit Fake-Bibliothek ------------------------------------------------------------------
class FakeMC:
    def __init__(self, replies):
        self.is_connected = True
        self.replies = list(replies)
        self.sent = []
        self.commands = SimpleNamespace(send_cmd=self.send_cmd, send_login_sync=self.login, send_logout=self.logout)

    async def send_cmd(self, contact, cmd):
        self.sent.append((contact["public_key"], cmd))
        return SimpleNamespace(type="msg_sent", payload={"suggested_timeout": 4000})

    async def login(self, contact, pwd, timeout=0, min_timeout=0):
        return SimpleNamespace(type="login_success", payload={"is_admin": pwd == "ok", "permissions": 3})

    async def logout(self, key):
        return SimpleNamespace(type="ok", payload={})

    async def wait_for_event(self, etype, attribute_filters=None, timeout=None):
        assert attribute_filters == {"pubkey_prefix": REPEATER_OK[:12], "txt_type": 1}
        return SimpleNamespace(payload={"text": self.replies.pop(0)}) if self.replies else None


def real_node(replies):
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error", CONTACT_MSG_RECV="contact_msg_recv")
    n.mc = FakeMC(replies)

    async def contact(key):
        return {"public_key": key, "type": 2}
    n._contact_dict = contact
    return n


async def test_real_node_collects_multi_part_replies():
    n = real_node(["> teil eins", "teil zwei"])
    res = await n.repeater_command(REPEATER_OK, "neighbors")
    assert res["replies"] == ["> teil eins", "teil zwei"] and n.mc.sent == [(REPEATER_OK, "neighbors")]


async def test_real_node_no_reply_and_nowait():
    n = real_node([])
    assert (await n.repeater_command(REPEATER_OK, "ver"))["replies"] == []
    assert (await n.repeater_command(REPEATER_OK, "reboot", wait=False))["replies"] == []


async def test_real_node_login_admin_flag_and_failure():
    n = real_node([])
    assert (await n.repeater_login(REPEATER_OK, "ok"))["admin"] is True

    async def none(*a, **k):
        return None
    n.mc.commands.send_login_sync = none
    with pytest.raises(NodeError, match="Anmeldebestätigung"):
        await n.repeater_login(REPEATER_OK, "falsch")


async def test_real_node_does_not_forward_repeater_replies_to_chat():
    got = []

    async def emit(ev):
        got.append(ev)
    n = RealNode(emit, Settings(mode="serial"))
    ev = lambda txt_type: SimpleNamespace(type=SimpleNamespace(value="contact_message"), payload={"txt_type": txt_type, "text": "x"})
    await n._forward(ev(1))
    await n._forward(ev(0))
    assert len(got) == 1 and got[0]["payload"]["txt_type"] == 0


# ---- Nachträge: „set repeat on“ exakt, Gast-Passwort lesen, Tempolimit auf allen Funkwegen ----------------
@pytest.mark.parametrize("cmd", ["set repeat offon", "set repeat off on", "set repeat xon", "set repeat on off"])
def test_only_exact_set_repeat_on_is_harmless(cmd):
    assert R.classify(cmd)["risky"]


def test_set_repeat_on_with_extra_spaces_is_harmless():
    assert R.classify("  SET   repeat   on ")["risky"] is None


async def test_reading_guest_password_is_redacted(adm, rt):
    rt.node.rep_cfg[REPEATER_OK]["guest.password"] = "Gast4711"
    await adm.login(REPEATER_OK, "sim")
    r = await adm.command(REPEATER_OK, "get guest.password")
    assert "Gast4711" not in json.dumps(r) and "Gast4711" not in json.dumps(adm.history(REPEATER_OK))
    assert "Gast4711" not in json.dumps(await rt.store.log(50))


async def test_rate_limit_applies_to_login_neighbours_and_acl(adm):
    await adm.login(REPEATER_OK, "sim")
    adm.tick()
    await adm.command(REPEATER_OK, "ver")
    for call in (lambda: adm.neighbours(REPEATER_OK), lambda: adm.acl(REPEATER_OK), lambda: adm.login(REPEATER_OK, "sim")):
        with pytest.raises(R.RepeaterError, match="Zu schnell"):
            await call()
    adm.tick()
    assert (await adm.neighbours(REPEATER_OK))["total"] == 2


async def test_auto_relogin_waits_before_sending_the_command(adm, rt):
    await adm.login(REPEATER_OK, "sim")
    adm.tick()
    adm._sessions.clear()                                             # Sitzung weg, Passwort im Speicher
    sends = []
    orig_login, orig_cmd = rt.node.repeater_login, rt.node.repeater_command

    async def login(*a, **k):
        sends.append(("login", adm._clock()))
        return await orig_login(*a, **k)

    async def cmd(*a, **k):
        sends.append(("cmd", adm._clock()))
        return await orig_cmd(*a, **k)
    rt.node.repeater_login, rt.node.repeater_command = login, cmd
    assert (await adm.command(REPEATER_OK, "ver"))["ok"]
    assert [k for k, _ in sends] == ["login", "cmd"] and sends[1][1] - sends[0][1] >= R.MIN_GAP_S


# ---- Wortanfang reicht: angehängte Zeichen umgehen die Sperren nicht (Firmware vergleicht teils nur den Anfang) ----
@pytest.mark.parametrize("cmd", ["erase!", "erasex", "ERASE now", "get prv.key!", "set prv.key00", "get  prv.key"])
def test_blocked_commands_cannot_be_bypassed_with_suffixes(cmd):
    with pytest.raises(RepeaterError, match="gesperrt"):
        classify(cmd)


@pytest.mark.parametrize("cmd", ["rebootnow", "reboot!", "poweroff1", "shutdown-now", "set tx99", "passwordx", "region put x", "set repeatoff"])
def test_risky_commands_with_suffixes_still_need_confirmation(cmd):
    assert classify(cmd)["risky"], cmd


def test_harmless_commands_stay_harmless():
    for cmd in ("set txdelay 0.5", "set direct.txdelay 0.2", "get tx", "set repeat on", "ver", "get name", "set name Hügel"):
        assert not classify(cmd)["risky"], cmd


def test_invisible_characters_are_rejected():
    for cmd in ("set prv.key​ 00", "erase​", "re­boot"):
        with pytest.raises(RepeaterError):
            classify(cmd)


def test_no_reply_and_secret_also_match_by_prefix():
    assert classify("rebootnow")["no_reply"] is True
    assert classify("password! neu")["secret"] is True
