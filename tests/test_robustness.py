"""Robustheit aus dem Review (mittel): taube WebSockets, Datenbankfehler bei eingehenden Nachrichten,
Pausenzustand nach Datenbankschaden, Sendebudget nur bei echter Funksendung (Temperatur)."""
import asyncio

from app.automation import Automation, SendBudget
from app.config import Settings
from app.node.sim import SimNode
from app.runtime import Hub, build
from tests.conftest import ALICE, REPEATER_LOW, REPEATER_OK


# ---- 9: Browser, dessen Senden hängt, wird nicht nur vergessen, sondern auch getrennt (damit er neu verbindet) ----
class SlowWS:
    def __init__(self):
        self.closed = None

    async def send_json(self, _):
        await asyncio.sleep(60)

    async def close(self, code=1000):
        self.closed = code


async def test_hanging_websocket_is_closed_so_the_browser_reconnects(monkeypatch):
    monkeypatch.setattr("app.runtime.WS_SEND_TIMEOUT_S", 0.05)
    hub, ws = Hub(), SlowWS()
    hub.clients.add(ws)
    await hub.emit({"type": "x"})
    await hub.drain()
    assert ws not in hub.clients and ws.closed == 1011


async def test_closing_a_broken_websocket_never_raises(monkeypatch):
    monkeypatch.setattr("app.runtime.WS_SEND_TIMEOUT_S", 0.05)

    class Broken(SlowWS):
        async def close(self, code=1000):
            raise RuntimeError("schon zu")
    hub = Hub()
    hub.clients.add(Broken())
    await hub.emit({"type": "x"})
    await hub.drain()
    assert not hub.clients


# ---- 11: Datenbank fällt aus → Auto-Antwort/Push laufen trotzdem ------------------------------------------------------
async def test_incoming_messages_are_handled_even_if_saving_fails(rt, monkeypatch):
    seen = []

    async def boom(*a, **k):
        raise OSError("Platte voll")
    monkeypatch.setattr(rt.store, "add_message", boom)

    async def spy_direct(payload, contact):
        seen.append("direct")

    async def spy_channel(payload, sender, text):
        seen.append("channel")
    monkeypatch.setattr(rt.automation, "handle_incoming", spy_direct)
    monkeypatch.setattr(rt.automation, "handle_channel_incoming", spy_channel)
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hallo")
    await rt.hub.drain()
    await rt.node.inject_message(ALICE, "hi")
    await rt.hub.drain()
    assert seen == ["channel", "direct"]


# ---- 12: Pausenzustand unbekannt → pausiert starten -------------------------------------------------------------------
async def test_unreadable_pause_state_starts_paused(rt, monkeypatch):
    async def boom(*a, **k):
        raise OSError("Datenbank weg")
    monkeypatch.setattr(rt.store, "get_setting", boom)
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000))
    await auto.start()
    try:
        assert auto.paused is True and "pausiert" in auto.start_note
    finally:
        await auto.stop()


def test_memory_fallback_starts_automation_paused(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    rt = build(Settings(mode="sim", db_path=str(blocker / "unter" / "x.db")),
               node_factory=lambda emit: SimNode(emit, background=False))
    assert rt.automation.start_paused is True


async def test_normal_start_reads_the_saved_pause_state(rt):
    await rt.store.set_setting("automation_paused", False)
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000))
    await auto.start()
    try:
        assert auto.paused is False and auto.start_note is None
    finally:
        await auto.stop()


# ---- 13: Temperatur bucht nur, wenn wirklich gefunkt wird --------------------------------------------------------------
async def test_temperature_backoff_does_not_use_send_budget(rt):
    budget = SendBudget(0, 1000)
    auto = Automation(rt.node, rt.store, rt.hub.emit, budget)
    for key in (REPEATER_OK, REPEATER_LOW):
        auto._no_temp[key] = float("inf")                 # beide ohne Sensor: 24-h-Pause
    rule = {"id": 1, "kind": "battery_poll", "name": "t", "enabled": True,
            "config": {"targets": [REPEATER_OK, REPEATER_LOW], "interval_min": 60, "low_mv": 3500, "temperature": True}}
    await auto.run_rule(rule)
    assert budget.stats()["used_last_hour"] == 2               # nur die beiden Statusabfragen


async def test_temperature_is_booked_when_it_is_really_sent(rt):
    budget = SendBudget(0, 1000)
    auto = Automation(rt.node, rt.store, rt.hub.emit, budget)
    rule = {"id": 1, "kind": "battery_poll", "name": "t", "enabled": True,
            "config": {"targets": [REPEATER_OK], "interval_min": 60, "low_mv": 3500, "temperature": True}}
    await auto.run_rule(rule)
    assert budget.stats()["used_last_hour"] == 2               # Status + Telemetrie


# ---- 8: hängender Push/Stationsabruf blockiert nie die Datenbank (eigener Pool, Platz erst nach Thread-Ende frei) ----
import threading
import time as _time

from app.push import PushService


async def test_hanging_push_never_blocks_the_database(rt, monkeypatch):
    monkeypatch.setattr("app.push.SEND_TIMEOUT_S", 0.1)
    monkeypatch.setattr("app.push.SLOT_WAIT_S", 0.05)
    release = threading.Event()

    def hang(*a, **k):
        release.wait(5)                                # z. B. DNS ohne Internet
    push = PushService(rt.store, send=hang)
    sub = {"endpoint": "https://push.example/x", "p256dh": "x", "auth": "y"}
    try:
        results = [await push._one({**sub, "endpoint": f"https://push.example/{i}"}, {"t": 1}) for i in range(12)]
        assert all(r != "ok" for r in results)
        t0 = _time.monotonic()
        await rt.store.get_setting("irgendwas")        # Datenbank-Thread ist trotzdem sofort da
        assert _time.monotonic() - t0 < 0.5
        assert push._pool._max_workers == 4            # hängende Sendungen bleiben in ihrem eigenen Pool
    finally:
        release.set()


async def test_push_pauses_after_repeated_network_failures(rt, monkeypatch):
    from app import webpush
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise webpush.PushError("<urlopen error [Errno -3] Temporary failure in name resolution>")
    push = PushService(rt.store, send=fail)
    for i in range(6):
        await push._one({"endpoint": f"https://push.example/{i}", "p256dh": "x", "auth": "y"}, {"t": 1})
    assert len(calls) == 3                              # nach 3 Netzfehlern Pause, keine weiteren Versuche


async def test_http_errors_do_not_trigger_the_pause(rt):
    from app import webpush
    calls = []

    def http_err(*a, **k):
        calls.append(1)
        raise webpush.PushError("HTTP 500")
    push = PushService(rt.store, send=http_err)
    for i in range(5):
        await push._one({"endpoint": f"https://push.example/{i}", "p256dh": "x", "auth": "y"}, {"t": 1})
    assert len(calls) == 5                              # Dienst antwortet: kein Netzproblem, keine Pause


async def test_hanging_peer_fetch_does_not_pile_up_threads():
    from app.peers import Peer, PeerWatch
    started, release = [], threading.Event()

    def hang(p):
        started.append(1)
        release.wait(5)
        return {}
    notes = []

    async def notify(kind, text):
        notes.append(kind)
    w = PeerWatch([Peer("B", "https://b.example", "t")], notify, fetch=hang)
    import app.peers as P
    old = P.TIMEOUT_S
    P.TIMEOUT_S = -4.9                                  # wait_for-Grenze ≈ 0,1 s
    try:
        for _ in range(4):
            await w.poll_once()
        assert len(started) == 1                        # keine neuen Threads, solange der erste hängt
        assert w.peers[0].fails == 4 and notes == ["down"]
    finally:
        P.TIMEOUT_S = old
        release.set()
