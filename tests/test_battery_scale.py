"""Akku-Skala (battery.py): EINE Umrechnung mV <-> Prozent für Regeln, Push und Protokoll, identisch zur Oberfläche (util.js)."""
import pytest

from app import battery as B
from app import push
from tests.conftest import REPEATER_LOW
from tests.test_push import ENDPOINT, sent, subscribe   # noqa: F401  (sent ist eine Fixture)


def test_scale_points_match_the_meshcore_app():
    assert (B.battery_pct(3000), B.battery_pct(3600), B.battery_pct(4200)) == (0, 50, 100)
    assert B.battery_pct(3700) == 58 and B.battery_pct(3480) == 40 and B.battery_pct(3240) == 20
    assert B.battery_mv(0) == 3000 and B.battery_mv(40) == 3480 and B.battery_mv(100) == 4200


def test_clamped_outside_the_range():
    assert B.battery_pct(2500) == 0 and B.battery_pct(4300) == 100
    assert B.battery_mv(-5) == 3000 and B.battery_mv(150) == 4200


def test_every_whole_percent_roundtrips_exactly():
    # Prozent -> mV -> Prozent verliert nichts: sonst würde eine eingestellte Warnstufe beim Speichern wandern
    assert all(B.battery_pct(B.battery_mv(p)) == p for p in range(0, 101))


def test_rounding_is_half_up_like_javascript():
    # 3006 mV = 0,5 %: JavaScript (Math.round) gibt 1, Pythons round() gäbe 0. Seite und Server dürfen nie um 1 % abweichen.
    assert B.battery_pct(3006) == 1 and B.battery_pct(3018) == 2 and B.battery_pct(3030) == 3


def test_push_uses_the_same_scale_as_everything_else():
    assert push.battery_pct is B.battery_pct                       # keine zweite, abweichende Kurve mehr (früher LiPo-Kurve: 3700 mV = 12 %)
    assert not hasattr(push, "_LIPO")


async def test_alert_log_names_percent_and_millivolts(rt):
    events = []

    async def collect(e):
        events.append(e)
    rt.hub.listeners.append(collect)
    await rt.automation.poll_node(REPEATER_LOW, low_mv=3700)       # Repeater steht bei 3620 mV
    await rt.hub.drain()
    line = next(e["payload"]["text"] for e in events if e["type"] == "automation" and "Akku niedrig" in e["payload"]["text"])
    assert "52 % (3620 mV)" in line and "Grenze 58 % (3700 mV)" in line


async def test_push_body_shows_percent_first(rt, client, sent):
    await subscribe(client, endpoint=ENDPOINT, battery=True)
    await rt.hub.emit({"type": "battery_low", "payload": {"name": "Hügel", "mv": 3620}})
    await rt.hub.drain(); await rt.push.drain()
    assert sent and sent[0][1]["body"] == "Hügel: 52 % (3620 mV)"
