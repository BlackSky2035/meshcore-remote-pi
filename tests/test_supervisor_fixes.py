"""Supervisor und echter Node (Fake-Bibliothek): Herzschlag bei belegtem Node, Anschluss per Symlink,
fehlgeschlagene Port-Abfrage, Verbinden mit stummem Kanalplatz, Ping-Ergebnis, Bibliotheks-Protokoll bei Geheimnissen."""
import asyncio
import logging
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from app import usb
from app.config import Settings
from app.node import NodeError
from app.node.real import RealNode
from app.supervisor import Supervisor
from tests.conftest import FakeClock, REPEATER_OK


async def _emit(_):
    ...


def _ok(payload):
    return SimpleNamespace(type="ok", payload=payload)


# ---- 1. Herzschlag: ein laufender Befehl ist kein toter Node -----------------------------------------
async def test_alive_returns_true_while_a_long_command_holds_the_lock():
    n = RealNode(_emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    release = asyncio.Event()

    async def slow_status(contact, timeout=0, min_timeout=0):
        await release.wait()                                  # z. B. Repeater-Status über mehrere Hops (≥ 15 s)
        return {"bat": 4000}

    async def query():
        return _ok({})
    n.mc = SimpleNamespace(is_connected=True, contacts={REPEATER_OK: {"public_key": REPEATER_OK}},
                           commands=SimpleNamespace(req_status_sync=slow_status, send_device_query=query))
    n._heard()                                                # Node hat eben noch geantwortet
    busy = asyncio.create_task(n.repeater_status(REPEATER_OK))
    await asyncio.sleep(0)
    assert n._cmd_lock.locked()
    assert await asyncio.wait_for(n.alive(), 0.5) is True    # wartet nicht auf die Sperre
    release.set()
    await busy
    assert await n.alive() is True                            # danach wieder echte Abfrage


async def test_alive_limits_only_the_device_query():
    n = RealNode(_emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    n.ALIVE_TIMEOUT_S = 0.05

    async def hang():
        await asyncio.sleep(10)
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace(send_device_query=hang))
    assert await asyncio.wait_for(n.alive(), 1) is False
    assert not n._cmd_lock.locked()


async def test_alive_detects_a_hung_node_even_while_the_lock_is_always_busy(monkeypatch):
    """Hängende Firmware, Browser-Tab fragt alle 15 s den Status ab: die Sperre ist praktisch immer belegt.
    Das darf nur so lange als Lebenszeichen zählen, wie der Node vor Kurzem noch etwas geschickt hat."""
    n = RealNode(_emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    now = [1000.0]
    monkeypatch.setattr("app.node.real.time.monotonic", lambda: now[0])
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace())
    n._heard()
    await n._cmd_lock.acquire()                               # hängender Befehl hält die Sperre
    try:
        now[0] += 30
        assert await n.alive() is True                        # eben noch gehört: langer Befehl ist normal
        now[0] += n.ALIVE_BUSY_GRACE_S
        assert await n.alive() is False                       # seit 2 min nichts mehr vom Node: hängt
        n._heard()                                            # irgendein Ereignis kommt wieder an
        assert await n.alive() is True
    finally:
        n._cmd_lock.release()


async def test_never_heard_and_busy_counts_as_not_alive():
    n = RealNode(_emit, Settings(mode="serial"))
    n.mc = SimpleNamespace(is_connected=True)
    await n._cmd_lock.acquire()
    try:
        assert await n.alive() is False
    finally:
        n._cmd_lock.release()


# ---- 2. Anschluss-Prüfung des Supervisors ---------------------------------------------------------------
@pytest.fixture
def sup(rt):
    clock = FakeClock()
    rt.usb._clock = clock
    s = Supervisor(rt, clock=clock, settle_s=0, heartbeat_s=30)
    s.clock = clock
    return s


async def test_symlinked_port_is_not_seen_as_vanished(rt, sup, tmp_path, monkeypatch):
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    link = tmp_path / "usb-Heltec_V4-if00"
    link.symlink_to(dev)
    ports = [{"device": str(dev), "description": "Heltec", "vid": 0x303A, "pid": 1, "serial": "x", "score": 5}]
    rt.node.usb_ports = lambda: None
    sup._ports = lambda: ports
    rt.settings.serial_port = str(link)
    monkeypatch.setattr(type(rt.node), "port", property(lambda s: str(link) if s.connected else None))
    drops = []
    orig = rt.node.disconnect

    async def counting():
        drops.append(1)
        await orig()
    rt.node.disconnect = counting
    for _ in range(3):
        sup.clock.advance(2)
        await sup.step()
    assert drops == [] and rt.node.connected and rt.usb.state == usb.OK


async def test_port_present_helper():
    assert usb.port_present("/dev/ttyACM0", [{"device": "/dev/ttyACM0"}])
    assert not usb.port_present("/dev/ttyACM9 (nicht da)", [{"device": "/dev/ttyACM0"}])


async def test_device_file_still_there_counts_as_present(tmp_path):
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    assert usb.port_present(str(dev), [])                  # Port-Liste leer (Abfrage intern gescheitert), Gerät existiert


async def test_failed_port_query_does_not_drop_a_healthy_connection(rt, sup):
    await sup.step()
    assert rt.node.connected and rt.usb.state == usb.OK
    rt.node.usb_ports = lambda: None

    def boom():
        raise OSError("Zeitüberschreitung")
    sup._ports = boom
    sup.clock.advance(2)
    await sup.step()
    assert rt.node.connected and rt.usb.state == usb.OK


# ---- 7. Verbinden: ein stummer Kanalplatz darf das Verbinden nicht abbrechen -------------------------------
def _fake_meshcore(monkeypatch, get_channel):
    mod = ModuleType("meshcore")

    class EventType:
        ERROR = "error"

    class FakeMC:
        is_connected = True

        def __init__(self):
            async def appstart():
                return _ok({"public_key": b"\x01" * 32})

            async def query():
                return _ok({"max_channels": 3})
            self.commands = SimpleNamespace(send_appstart=appstart, send_device_query=query, get_channel=get_channel)

        def subscribe(self, *a):
            pass

        async def start_auto_message_fetching(self):
            pass

        def set_decrypt_channel_logs(self, on):
            pass

    class MeshCore:
        @staticmethod
        async def create_serial(port, baud, debug=False, auto_reconnect=False):
            return FakeMC()
    mod.EventType, mod.MeshCore = EventType, MeshCore
    monkeypatch.setitem(sys.modules, "meshcore", mod)


async def test_connect_survives_a_silent_channel_slot_and_resets_device_state(monkeypatch, tmp_path):
    async def get_channel(idx):
        if idx == 1:
            await asyncio.sleep(100)                           # Platz antwortet nie
        return SimpleNamespace(type="channel_info", payload={"channel_name": f"K{idx}", "channel_secret": b"\x01" * 16})
    _fake_meshcore(monkeypatch, get_channel)
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    n = RealNode(_emit, Settings(mode="serial", serial_port=str(dev)))
    n.CHANNEL_TIMEOUT_S, n.CHANNEL_RETRY_DELAY_S = 0.05, 0
    n._max_channels, n._battery = 40, (time.time(), 4200)        # Werte eines anderen, vorher angeschlossenen Nodes
    await asyncio.wait_for(n.connect(), 3)
    assert n.connected and n._battery is None
    assert await n.max_channels() == 3


# ---- 8. Ping: Sendefehler liefert dieselbe Form wie im Simulator -----------------------------------------
async def test_real_ping_send_failure_returns_not_ok():
    n = RealNode(_emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")

    async def fail(*a, **k):
        return SimpleNamespace(type="error", payload={"reason": "kaputt"})
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace(send_msg=fail, send_trace=fail))
    assert await n.ping(REPEATER_OK, mode="message") == {"ok": False, "rtt_ms": None}
    assert await n.ping(REPEATER_OK, mode="trace") == {"ok": False, "rtt_ms": None}


# ---- 9. Bibliotheks-Protokoll (MC_DEBUG) darf Passwörter nicht als Hex mitschreiben ------------------------
async def test_library_debug_log_is_muted_during_login_and_secret_commands():
    lib = logging.getLogger("meshcore")
    old = lib.level
    lib.setLevel(logging.DEBUG)
    seen = []
    try:
        n = RealNode(_emit, Settings(mode="serial"))
        n._EventType = SimpleNamespace(ERROR="error", CONTACT_MSG_RECV="c")

        async def login(contact, pwd, timeout=0, min_timeout=0):
            seen.append(lib.getEffectiveLevel())
            return SimpleNamespace(type="login_success", payload={"is_admin": True})

        async def send_cmd(contact, cmd):
            seen.append(lib.getEffectiveLevel())
            raise RuntimeError("Funkfehler")

        async def wait_for_event(*a, **k):
            return None
        n.mc = SimpleNamespace(is_connected=True, wait_for_event=wait_for_event,
                               commands=SimpleNamespace(send_login_sync=login, send_cmd=send_cmd))

        async def contact(key):
            return {"public_key": key}
        n._contact_dict = contact
        await n.repeater_login(REPEATER_OK, "geheim")
        assert lib.level == logging.DEBUG
        with pytest.raises(RuntimeError):
            await n.repeater_command(REPEATER_OK, "password neu", secret=True)
        assert lib.level == logging.DEBUG                         # auch nach einem Fehler zurückgesetzt
        with pytest.raises(RuntimeError):
            await n.repeater_command(REPEATER_OK, "ver")
        assert seen == [logging.INFO, logging.INFO, logging.DEBUG]
    finally:
        lib.setLevel(old)


async def test_alive_gives_up_waiting_for_the_lock_without_leaking_it():
    """Gerade freigegebene Sperre mit wartendem Befehl: alive() wartet höchstens kurz und hinterlässt die Sperre frei."""
    n = RealNode(_emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR="error")
    n.ALIVE_LOCK_WAIT_S = 0.05

    async def query():
        return _ok({})
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace(send_device_query=query))
    n._heard()
    await n._cmd_lock.acquire()
    waiter = asyncio.create_task(n._cmd_lock.acquire())          # nächster Befehl steht schon an
    await asyncio.sleep(0)
    n._cmd_lock.release()                                         # frei, aber der Wartende ist zuerst dran
    beat = asyncio.create_task(n.alive())
    await asyncio.sleep(0.01)
    await waiter
    await asyncio.sleep(0.1)                                      # „Befehl“ läuft länger als die Wartezeit
    assert await beat is True
    n._cmd_lock.release()
    await asyncio.sleep(0)
    assert not n._cmd_lock.locked()
    assert await n.alive() is True
