"""Prüft die Kanal-Logik von RealNode gegen einen Fake der meshcore-Bibliothek (keine Hardware)."""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.node import NodeError
from app.node.base import derive_secret
from app.node.real import RealNode

ERROR, OK, CHAN = "error", "ok", "channel_info"


class FakeCommands:
    def __init__(self, slots):
        self.slots = slots                    # idx -> (name, secret)
        self.set_calls = []
        self.get_calls = []

    async def send_device_query(self):
        return SimpleNamespace(type=OK, payload={"max_channels": 4, "model": "x"})

    async def get_channel(self, idx):
        self.get_calls.append(idx)
        if idx not in range(4):
            return SimpleNamespace(type=ERROR, payload={"reason": "idx"})
        name, secret = self.slots.get(idx, ("", bytes(16)))
        return SimpleNamespace(type=CHAN, payload={"channel_idx": idx, "channel_name": name, "channel_secret": secret})

    async def set_channel(self, idx, name, secret=None):
        self.set_calls.append((idx, name, secret))
        self.slots[idx] = (name, secret)
        return SimpleNamespace(type=OK, payload={})


def make(slots):
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"))
    n._EventType = SimpleNamespace(ERROR=ERROR)
    n.mc = SimpleNamespace(is_connected=True, commands=FakeCommands(slots))
    n.CHANNEL_RETRY_DELAY_S = 0          # Tests sollen nicht auf echte Wartezeiten warten
    return n


async def test_lists_only_used_slots_and_reads_limit():
    n = make({0: ("Public", b"\x01" * 16), 2: ("#x", derive_secret("#x"))})
    assert await n.max_channels() == 4
    chans = await n.channels()
    assert [(c["idx"], c["name"], c["hashtag"]) for c in chans] == [(0, "Public", False), (2, "#x", True)]


async def test_set_and_remove_write_to_node():
    n = make({})
    ch = await n.set_channel(1, "Familie", b"\x07" * 16)
    assert ch["idx"] == 1 and n.mc.commands.set_calls == [(1, "Familie", b"\x07" * 16)]
    await n.remove_channel(1)
    assert n.mc.commands.set_calls[-1] == (1, "", bytes(16))
    assert await n.channels() == []                    # gelöschter Platz zählt als leer


async def test_transient_no_response_is_retried_not_treated_as_empty():
    """Eine kurze Aussetzung des echten Nodes (siehe USB-Überwachung) darf nicht als
    „Kanalplatz leer“ missverstanden werden – nur eine echte, bestätigt leere Antwort zählt so."""
    n = make({0: ("Public", b"\x01" * 16)})
    calls = {1: 0}

    async def flaky(idx):
        if idx == 1:
            calls[1] += 1
            if calls[1] == 1:
                return None                 # erster Versuch: keine Antwort
        return await FakeCommands.get_channel(n.mc.commands, idx)
    n.mc.commands.get_channel = flaky
    chans = await n.channels()
    assert calls[1] == 2                    # beim zweiten Versuch hat es geklappt
    assert [c["idx"] for c in chans] == [0]  # Platz 1 ist wirklich leer (nicht nur unlesbar)


async def test_channel_unreadable_after_retries_raises_instead_of_hiding_it():
    n = make({})

    async def always_none(idx):
        return None if idx == 2 else await FakeCommands.get_channel(n.mc.commands, idx)
    n.mc.commands.get_channel = always_none
    with pytest.raises(NodeError, match="Kanal 2"):
        await n.channels()


async def test_channels_are_cached_after_first_read():
    """Nur der erste Aufruf fragt wirklich den Node; alle danach kommen aus dem Speicher
    (schont die Verbindung, siehe die USB-Aussetzer in der Praxis)."""
    n = make({0: ("Public", b"\x01" * 16)})
    await n.channels()
    first = len(n.mc.commands.get_calls)
    assert first == 4                       # einmal pro Platz
    await n.channels()
    await n.channels()
    assert len(n.mc.commands.get_calls) == first   # keine weiteren Abfragen


async def test_set_channel_only_rereads_the_changed_slot():
    n = make({0: ("Public", b"\x01" * 16)})
    await n.channels()                      # Zwischenspeicher füllen
    before = len(n.mc.commands.get_calls)
    ch = await n.set_channel(1, "Familie", b"\x07" * 16)
    assert len(n.mc.commands.get_calls) == before + 1   # nur Platz 1 neu gelesen, nicht alle vier
    assert n.mc.commands.get_calls[-1] == 1
    cached = await n.channels()             # kommt weiter aus dem Speicher, jetzt mit dem neuen Kanal
    assert len(n.mc.commands.get_calls) == before + 1
    assert {c["idx"] for c in cached} == {0, 1}
    assert ch["name"] == "Familie"


async def test_write_not_confirmed_by_readback_raises():
    n = make({})

    async def lying_set(idx, name, secret=None):
        n.mc.commands.set_calls.append((idx, name, secret))
        return SimpleNamespace(type=OK, payload={})   # meldet Erfolg, schreibt aber nichts in `slots`
    n.mc.commands.set_channel = lying_set
    with pytest.raises(NodeError, match="nicht bestätigt"):
        await n.set_channel(0, "Familie", b"\x07" * 16)


async def test_new_connection_reads_channels_fresh_again():
    n = make({0: ("Public", b"\x01" * 16)})
    await n.channels()
    assert len(n.mc.commands.get_calls) == 4
    n._channels_cache = None                # das macht connect() bei jeder neuen Verbindung
    await n.channels()
    assert len(n.mc.commands.get_calls) == 8


async def test_rejects_bad_input_before_talking_to_node():
    n = make({})
    for args in [(9, "ok", b"\x01" * 16), (0, "", b"\x01" * 16), (0, "ok", b"\x01" * 5), (0, "ok", None)]:
        with pytest.raises(NodeError):
            await n.set_channel(*args)
    assert n.mc.commands.set_calls == []


async def test_error_from_node_is_reported():
    n = make({})

    async def boom(*a, **k):
        return SimpleNamespace(type=ERROR, payload={"code": 3})
    n.mc.commands.set_channel = boom
    with pytest.raises(NodeError, match="set_channel"):
        await n.set_channel(0, "x", b"\x01" * 16)


# ---- Zero-Hop-Ping (Trace) ----------------------------------------------------
async def test_trace_ping_uses_one_byte_hash_and_reads_snr():
    n = make({})
    n._EventType = SimpleNamespace(ERROR=ERROR, TRACE_DATA="trace")
    calls = {}

    async def send_trace(auth_code=0, tag=None, flags=None, path=None):
        calls.update(tag=tag, path=path)
        return SimpleNamespace(type=OK, payload={"suggested_timeout": 0})

    async def wait_for_event(etype, attribute_filters=None, timeout=None):
        calls["filter"] = attribute_filters
        return SimpleNamespace(payload={"path": [{"hash": "c3", "snr": 5.25}, {"snr": 3.0}]})
    n.mc.commands.send_trace = send_trace
    n.mc.wait_for_event = wait_for_event
    res = await n.ping("c3" * 32, "trace")
    assert calls["path"] == "c3" and calls["filter"] == {"tag": calls["tag"]}
    assert res["ok"] is True and (res["snr_out"], res["snr_in"]) == (5.25, 3.0)


async def test_trace_ping_timeout_is_not_ok():
    n = make({})
    n._EventType = SimpleNamespace(ERROR=ERROR, TRACE_DATA="trace")

    async def send_trace(**k):
        return SimpleNamespace(type=OK, payload={})

    async def wait_for_event(*a, **k):
        return None
    n.mc.commands.send_trace = send_trace
    n.mc.wait_for_event = wait_for_event
    assert (await n.ping("c3" * 32, "trace")) == {"ok": False, "rtt_ms": None}


# ---- Funk-Log: gehörte Pakete ------------------------------------------------------
async def test_rx_log_forwards_only_decrypted_group_text():
    events = []

    async def emit(e):
        events.append(e)
    n = RealNode(emit, Settings(mode="serial"))
    n._chan_idx_by_name = {"Familie": 2}
    ok = {"payload_type": 5, "message": "Ich: hallo", "chan_name": "Familie", "sender_timestamp": 77,
          "path": "c3d4", "path_hash_size": 1, "snr": 3.5, "rssi": -99}
    for payload in [ok,
                    {**ok, "payload_type": 4},                    # Advert: ignorieren
                    {**ok, "message": None},                      # nicht entschlüsselbar
                    {**ok, "chan_name": "Unbekannt"}]:            # Kanal nicht bei uns
        await n._on_rx_log(SimpleNamespace(payload=payload))
    assert len(events) == 1 and events[0]["type"] == "channel_rx"
    p = events[0]["payload"]
    assert (p["channel_idx"], p["message"], p["sender_timestamp"], p["path"], p["snr"]) == (2, "Ich: hallo", 77, "c3d4", 3.5)


async def test_send_channel_message_passes_and_returns_timestamp():
    n = make({})
    seen = {}

    async def send_chan_msg(chan, msg, timestamp=None):
        seen.update(chan=chan, msg=msg, ts=timestamp)
        return SimpleNamespace(type=OK, payload={})
    n.mc.commands.send_chan_msg = send_chan_msg
    res = await n.send_channel_message(1, "hallo")
    assert res["ok"] is True and res["timestamp"] == seen["ts"] and isinstance(seen["ts"], int)


async def test_channel_name_table_follows_set_and_remove():
    n = make({})
    await n.channels()
    await n.set_channel(1, "Familie", b"\x07" * 16)
    assert n._chan_idx_by_name == {"Familie": 1}
    await n.remove_channel(1)
    assert n._chan_idx_by_name == {}
