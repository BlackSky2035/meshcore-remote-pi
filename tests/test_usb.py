"""USB-Erkennung und -Überwachung: Auswahl, Verfügbarkeit, Abziehen/Einstecken, Ausfälle (alles ohne Hardware)."""
import asyncio
from types import SimpleNamespace

import pytest

from app import usb
from app.config import Settings
from app.node import NodeError
from app.node.real import RealNode
from app.supervisor import Supervisor
from tests.conftest import FakeClock


def port(dev, vid=None, desc="", pid=None):
    return {"device": dev, "description": desc, "vid": vid, "pid": pid, "serial": None,
            "score": usb._score(dev, vid, desc)}


# ---- Erkennung ---------------------------------------------------------------
def test_candidates_are_ranked_and_noise_is_ignored():
    ports = [port("/dev/ttyS0"), port("/dev/ttyUSB0"), port("/dev/ttyACM0", vid=0x303A, desc="Heltec V4"),
             port("/dev/cu.Bluetooth-Incoming-Port")]
    found = [c["device"] for c in usb.candidates(ports)]
    assert found == ["/dev/ttyACM0", "/dev/ttyUSB0"]            # ttyS0 und Bluetooth sind keine Kandidaten


def test_resolve_port_auto_configured_and_missing():
    ports = [port("/dev/ttyUSB0"), port("/dev/ttyACM0", vid=0x303A)]
    assert usb.resolve_port("auto", ports) == "/dev/ttyACM0"
    assert usb.resolve_port("/dev/ttyUSB0", ports) == "/dev/ttyUSB0"
    with pytest.raises(NodeError, match="nicht vorhanden"):
        usb.resolve_port("/dev/ttyUSB9", ports)
    with pytest.raises(NodeError, match="Kein USB-Node"):
        usb.resolve_port("auto", [port("/dev/ttyS0")])


def test_list_ports_never_raises(monkeypatch):
    import serial.tools.list_ports as lp

    def boom():
        raise OSError("sysfs weg")
    monkeypatch.setattr(lp, "comports", boom)
    assert usb.list_ports() == []


# ---- Verfügbarkeit -------------------------------------------------------------
def test_availability_math():
    # 1000 s Fenster: 0-600 ok, 600-700 aus, 700-1000 ok  -> 90 %
    a = usb.availability([(600, False), (700, True)], 0, 1000, True)
    assert a["percent"] == 90.0 and a["downtime_s"] == 100 and a["outages"] == 1
    # Zustand vor dem Fenster unbekannt: Zählung beginnt erst beim ersten Ereignis
    a = usb.availability([(500, True), (900, False)], 0, 1000, None)
    assert a["observed_s"] == 500 and a["percent"] == 80.0
    assert usb.availability([], 0, 1000, None) is None


# ---- Supervisor mit Simulator ----------------------------------------------------
@pytest.fixture
def sup(rt):
    clock = FakeClock()
    rt.usb._clock = clock
    rt.usb.alert_after_s = 60
    told = []

    async def notify(kind, text):
        told.append((kind, text))
    rt.usb.notify = notify
    s = Supervisor(rt, clock=clock, settle_s=0, heartbeat_s=30)
    s.clock, s.told = clock, told
    return s


async def test_unplug_and_replug_is_detected_and_reconnected(rt, sup):
    await sup.step()
    assert rt.usb.state == usb.OK and rt.node.connected
    await rt.node.unplug()
    sup.clock.advance(2)
    await sup.step()
    assert rt.usb.state == usb.NO_DEVICE and not rt.node.connected
    rt.node.plug()
    sup.clock.advance(2)
    await sup.step()
    assert rt.usb.state == usb.OK and rt.node.connected
    states = [e["state"] for e in await rt.store.usb_events(0)]
    assert states == ["ok", "no_device", "ok"]


async def test_outage_is_reported_once_and_recovery_once(rt, sup):
    await sup.step()
    await rt.node.unplug()
    for _ in range(5):                                   # 5 × 20 s = 100 s Ausfall
        sup.clock.advance(20)
        await sup.step()
    assert [k for k, _ in sup.told] == ["down"]          # nur eine Meldung, nicht eine pro Durchlauf
    rt.node.plug()
    sup.clock.advance(2)
    await sup.step()
    sup.clock.advance(2)
    await sup.step()
    assert [k for k, _ in sup.told] == ["down", "up"]


async def test_short_blip_does_not_alert(rt, sup):
    await sup.step()
    await rt.node.unplug()
    sup.clock.advance(10)
    await sup.step()
    rt.node.plug()
    sup.clock.advance(5)
    await sup.step()
    assert rt.usb.state == usb.OK and sup.told == []


async def test_unresponsive_node_is_reconnected_then_reported(rt, sup):
    await sup.step()
    rt.node.hung = True
    for _ in range(3):                                   # zwei Herzschläge ohne Antwort
        sup.clock.advance(31)
        await sup.step()
    assert rt.usb.state == usb.NO_RESPONSE and not rt.node.connected
    assert "antwortet nicht" in (rt.state["last_error"] or "")
    rt.node.hung = False
    sup.clock.advance(60)
    await sup.step()
    assert rt.usb.state == usb.OK


# ---- Kanal-Überprüfung nach dem Verbinden -----------------------------------------
async def test_step_persists_known_channels_after_connecting(rt, sup):
    await rt.node.disconnect()              # rt verbindet beim Aufbau schon einmal: hier für einen echten,
    await sup.step()                        # neuen Verbindungsaufbau erst wieder trennen
    info = await rt.store.get_setting("known_channels")
    assert info["public_key"] == rt.node.public_key
    assert info["idxs"] == sorted(c["idx"] for c in await rt.node.channels())


async def test_verify_channels_retries_once_when_fewer_than_remembered(rt, sup):
    pub = "aa" * 32
    rt.node.public_key = pub
    await rt.store.set_setting("known_channels", {"public_key": pub, "idxs": [0, 1, 2]})
    calls = []

    async def flaky_channels():
        calls.append(1)
        return [{"idx": 0}, {"idx": 1}] if len(calls) == 1 else [{"idx": 0}, {"idx": 1}, {"idx": 2}]
    rt.node.channels = flaky_channels
    invalidated = []
    rt.node.invalidate_channels = lambda: invalidated.append(1)
    await sup._verify_channels()
    assert len(calls) == 2 and invalidated == [1]            # einmal erneut gelesen, nicht öfter
    assert (await rt.store.get_setting("known_channels"))["idxs"] == [0, 1, 2]


async def test_verify_channels_ignores_an_unrelated_remembered_identity(rt, sup):
    """Ein anderer Node (andere Kennung) war vorher angeschlossen: dessen Kanalliste darf keinen falschen Alarm auslösen."""
    await rt.store.set_setting("known_channels", {"public_key": "bb" * 32, "idxs": [0, 1, 2]})
    rt.node.public_key = "aa" * 32
    calls = []

    async def channels_now():
        calls.append(1)
        return [{"idx": 0}]
    rt.node.channels = channels_now
    invalidated = []
    rt.node.invalidate_channels = lambda: invalidated.append(1)
    await sup._verify_channels()
    assert len(calls) == 1 and invalidated == []
    assert (await rt.store.get_setting("known_channels")) == {"public_key": "aa" * 32, "idxs": [0]}


async def test_verify_channels_does_nothing_without_a_public_key(rt, sup):
    rt.node.public_key = None
    await sup._verify_channels()
    assert await rt.store.get_setting("known_channels", None) is None


async def test_verify_channels_never_raises(rt, sup):
    rt.node.public_key = "aa" * 32

    async def boom():
        raise RuntimeError("kaputt")
    rt.node.channels = boom
    await sup._verify_channels()                             # darf nicht werfen


async def test_connect_retries_back_off(rt, sup):
    calls = []

    async def failing():
        calls.append(sup.clock())
        raise NodeError("kaputt")
    await rt.node.disconnect()
    rt.node.connect = failing
    for _ in range(40):
        sup.clock.advance(2)
        await sup.step()
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert gaps[0] >= 3 and gaps[-1] >= 24 and len(calls) < 12      # wird seltener, kein Dauerfeuer
    assert rt.usb.state == usb.NO_RESPONSE


async def test_permission_problem_is_named(rt, sup, monkeypatch):
    await rt.node.disconnect()
    rt.node.hung = True                                  # Verbinden schlägt fehl, Gerät ist aber da
    monkeypatch.setattr(usb, "port_access_problem", lambda dev: usb.PERMISSION_HINT)
    await sup.step()
    assert rt.usb.state == usb.NO_PERMISSION and "dialout" in rt.usb.hint


async def test_port_listing_failure_does_not_stop_supervisor(rt, sup):
    rt.node.usb_ports = lambda: None                     # wie ein echter Node: echte Abfrage nutzen
    rt.node.uses_usb = True

    def boom():
        raise OSError("weg")
    sup._ports = boom
    await sup.step()                                     # darf nicht werfen
    assert rt.usb.state in (usb.NO_DEVICE, usb.OK)


async def test_monitor_survives_database_failure(rt, sup):
    async def broken(*a, **k):
        raise RuntimeError("DB tot")
    rt.store.add_usb_event = broken
    rt.store.usb_events = broken
    await sup.step()
    await rt.node.unplug()
    sup.clock.advance(100)
    await sup.step()
    assert rt.usb.state == usb.NO_DEVICE                 # Zustand und Meldung funktionieren auch ohne Datenbank
    assert (await rt.usb.stats())["24h"] is None


async def test_stats_reflect_history(rt, sup):
    await sup.step()
    await rt.node.unplug()
    sup.clock.advance(100)
    await sup.step()
    rt.node.plug()
    sup.clock.advance(100)
    await sup.step()
    sup.clock.advance(100)
    await sup.step()
    st = await rt.usb.stats()
    assert st["24h"]["outages"] == 1 and 50 < st["24h"]["percent"] < 100


# ---- API --------------------------------------------------------------------------
async def test_api_usb_and_sim_controls(client, rt, sup):
    await sup.step()
    j = (await client.get("/api/usb")).json()
    assert j["enabled"] and j["state"] == "ok" and j["ports"][0]["device"].startswith("/dev/")
    assert (await client.get("/api/status")).json()["usb"]["state"] == "ok"
    r = await client.post("/api/sim/usb", json={"plugged": False})
    assert r.json() == {"plugged": False, "hung": False}
    sup.clock.advance(2)
    await sup.step()
    assert (await client.get("/api/usb")).json()["state"] == "no_device"
    await client.post("/api/sim/usb", json={"plugged": True})


async def test_sim_usb_endpoint_not_available_outside_simulator(client, rt):
    rt.settings.mode = "serial"
    assert (await client.post("/api/sim/usb", json={"plugged": False})).status_code == 404


# ---- Echter Node (Fake-Bibliothek) -----------------------------------------------------
async def test_real_node_alive_checks_node_answer():
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")

    class Cmds:
        mode = "ok"

        async def send_device_query(self):
            if self.mode == "hang":
                await asyncio.sleep(60)
            return None if self.mode == "none" else SimpleNamespace(type="ok", payload={})
    n.mc = SimpleNamespace(is_connected=True, commands=Cmds())
    assert await n.alive()
    n.mc.commands.mode = "none"
    assert not await n.alive()
    n.mc.is_connected = False
    assert not await n.alive()
    assert n.uses_usb
