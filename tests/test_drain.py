"""`drain()` darf sich nie endlos drehen (früher: seltener Hänger bei 98 % CPU in test_push.py).

Ursache war `while self._tasks: await gather(*self._tasks)`: Ist eine Aufgabe fertig, ihr Aufräum-Rückruf (`discard`) aber noch nicht gelaufen,
liefert `gather` sofort ein fertiges Ergebnis, ohne der Ereignisschleife je die Kontrolle zu geben. Dann kommt der Rückruf nie dran.
Die Prüfung läuft in einem eigenen Thread mit Frist, weil sich die alte Schleife nicht per `wait_for` abbrechen lässt (sie unterbricht nie).
"""
import asyncio
import threading

from app.push import PushService
from app.runtime import Hub


def finishes_within(make_coro, seconds: float = 3.0) -> bool:
    out = {}

    def target():
        try:
            asyncio.run(make_coro())
            out["ok"] = True
        except BaseException as e:             # noqa: BLE001
            out["err"] = e
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(seconds)
    return (not th.is_alive()) and out.get("ok", False)


async def _scenario(owner) -> None:
    async def quick():
        return 1
    done = asyncio.ensure_future(quick())
    owner._tasks.add(done)                     # absichtlich OHNE discard-Rückruf: fertig, aber noch eingetragen
    await asyncio.sleep(0.01)
    assert done.done() and owner._tasks
    await owner.drain()


def test_hub_drain_does_not_spin_on_finished_registered_task():
    assert finishes_within(lambda: _scenario(Hub()))


def test_push_drain_does_not_spin_on_finished_registered_task():
    assert finishes_within(lambda: _scenario(PushService(None)))


async def _running(owner) -> list:
    order = []

    async def slow(n):
        await asyncio.sleep(0.02)
        order.append(n)
    for n in range(3):
        t = asyncio.ensure_future(slow(n))
        owner._tasks.add(t)
        t.add_done_callback(owner._tasks.discard)
    await owner.drain()
    return order


async def test_hub_drain_still_waits_for_running_tasks():
    assert sorted(await _running(Hub())) == [0, 1, 2]


async def test_push_drain_still_waits_for_running_tasks():
    assert sorted(await _running(PushService(None))) == [0, 1, 2]


async def test_drain_survives_failing_task():
    h = Hub()

    async def boom():
        raise RuntimeError("kaputt")
    t = asyncio.ensure_future(boom())
    h._tasks.add(t)
    t.add_done_callback(h._tasks.discard)
    await h.drain()                            # darf nicht werfen
