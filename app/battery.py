"""Akku-Skala: Ruhespannung (mV) <-> Ladestand (%). EINE Quelle für Regeln, Push und Protokoll; die Oberfläche (`static/lib/util.js`: batteryPct/batteryMv)
rechnet exakt gleich.

Lineare Skala wie in der offiziellen MeshCore-App: 3000 mV = 0 %, 4200 mV = 100 %. Das ist KEINE echte Ladestandsmessung (eine LiPo-Zelle ist über weite
Strecken flacher als linear), dient aber dazu, dass überall dieselben Prozentwerte stehen. Gerundet wird wie in JavaScript (0,5 rundet auf), nicht wie
Pythons round() (das 0,5 zur geraden Zahl rundet), damit Seite und Server nie um 1 % auseinanderliegen.
"""
from __future__ import annotations

import math

MIN_MV = 3000
MAX_MV = 4200


def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def battery_pct(mv: float) -> int:
    return _half_up(max(0.0, min(100.0, (float(mv) - MIN_MV) / (MAX_MV - MIN_MV) * 100)))


def battery_mv(pct: float) -> int:
    return _half_up(MIN_MV + max(0.0, min(100.0, float(pct))) / 100 * (MAX_MV - MIN_MV))
