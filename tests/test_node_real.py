"""RealNode.status() gegen einen Fake der meshcore-Bibliothek (keine Hardware)."""
import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.node.real import RealNode


def make_node(clock, bat_level=4000):
    async def emit(_): ...
    n = RealNode(emit, Settings(mode="serial"), clock=clock)
    n._EventType = SimpleNamespace(ERROR="error")

    async def _co(x):
        return x
    ok = lambda p: (lambda: _co(SimpleNamespace(type="ok", payload=p)))
    n.mc = SimpleNamespace(is_connected=True, commands=SimpleNamespace(
        send_appstart=ok({"name": "n", "public_key": b"\x01"}),
        send_device_query=ok({"model": "m", "ver": "1"}),
        get_bat=ok({"level": bat_level}),
        get_contacts=ok({})))
    return n


async def test_battery_is_not_requeried_within_the_cache_window():
    now = [1000.0]
    n = make_node(lambda: now[0])
    calls = []
    orig = n.mc.commands.get_bat

    async def counted():
        calls.append(1)
        return await orig()
    n.mc.commands.get_bat = counted

    assert (await n.status())["battery_mv"] == 4000
    now[0] += 60                              # weit innerhalb der 30 min: kein neuer Abruf
    assert (await n.status())["battery_mv"] == 4000
    assert len(calls) == 1


async def test_battery_is_requeried_after_the_cache_window():
    now = [1000.0]
    n = make_node(lambda: now[0])
    await n.status()
    now[0] += 1800
    n.mc.commands.get_bat = lambda: (lambda: _co2(SimpleNamespace(type="ok", payload={"level": 3999})))()

    async def _co2(x):
        return x
    assert (await n.status())["battery_mv"] == 3999


async def test_battery_error_on_first_query_reports_it_and_retries_next_call():
    now = [1000.0]
    n = make_node(lambda: now[0])

    async def boom():
        raise RuntimeError("kaputt")
    n.mc.commands.get_bat = boom
    s = await n.status()
    assert "battery_error" in s and "battery_mv" not in s     # Fehler wird gemeldet, kein Wert vorgetäuscht
    now[0] += 5                                                # weit vor Ablauf des Caches

    async def _co(x):
        return x
    n.mc.commands.get_bat = lambda: (lambda: _co(SimpleNamespace(type="ok", payload={"level": 4001})))()
    assert (await n.status())["battery_mv"] == 4001            # nach einem Fehler wird beim nächsten Mal sofort erneut versucht


async def test_commands_never_overlap_on_the_single_connection():
    """Zwei gleichzeitige Aufrufe (z. B. der 15s-Status-Abruf und eine gerade geöffnete Seite) dürfen sich
    nie überschneiden - das echte Gerät kann immer nur eine Anfrage gleichzeitig beantworten und reagiert
    auf überlappende Befehle sonst mit ERR_CODE_BAD_STATE."""
    n = make_node(lambda: 1000.0)
    concurrent, max_concurrent = [0], [0]

    async def track(payload):
        concurrent[0] += 1
        max_concurrent[0] = max(max_concurrent[0], concurrent[0])
        await asyncio.sleep(0.01)
        concurrent[0] -= 1
        return SimpleNamespace(type="ok", payload=payload)

    n.mc.commands.get_contacts = lambda: track({})
    n.mc.commands.send_appstart = lambda: track({"name": "n", "public_key": b"\x01"})
    n.mc.commands.send_device_query = lambda: track({"model": "m", "ver": "1"})
    n.mc.commands.get_bat = lambda: track({"level": 4000})

    await asyncio.gather(n.contacts(), n.status(), n.status())
    assert max_concurrent[0] == 1
