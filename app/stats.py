"""Funkstatistik: Zähler des eigenen Nodes regelmäßig sichern und daraus Verläufe rechnen.

Die Abfrage geht nur über das USB-Kabel an den eigenen Node (keine Funkzeit). Zähler laufen hoch und setzen sich beim Neustart des Nodes
zurück; Differenzen mit negativem Ergebnis (Neustart) werden übersprungen. „Sendezeit-Anteil“ = Sendezeit im Zeitraum geteilt durch den Zeitraum.
Gesetzliche Grenzen (EU 868 MHz: je nach Teilband 1 % oder 10 %) kennt das Programm nicht; der Wert dient als Richtgröße.
Nicht an echter Hardware getestet (Firmware muss die Statistik-Befehle kennen, sonst bleibt die Seite leer).
Läuft standardmäßig alle 30 Minuten automatisch (SAMPLE_EVERY_S), ist über die Einstellung "radio_stats_enabled"
ein-/ausschaltbar (Standard: an); „Jetzt messen“ auf der Seite löst unabhängig davon immer eine Messung aus.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .node.base import BaseNode, NodeError

log = logging.getLogger("meshcore-remote")
SAMPLE_EVERY_S = 1800.0
MAX_GAP_S = 3600.0          # größere Lücken (App war aus) liefern keinen Durchschnittswert


def flatten(stats: dict) -> dict:
    row: dict = {}
    core, radio, pk = stats.get("core") or {}, stats.get("radio") or {}, stats.get("packets") or {}
    row.update(uptime=core.get("uptime_secs"), queue_len=core.get("queue_len"), noise_floor=radio.get("noise_floor"),
               tx_air=radio.get("tx_air_secs"), rx_air=radio.get("rx_air_secs"))
    for k in ("recv", "sent", "flood_tx", "direct_tx", "flood_rx", "direct_rx", "recv_errors"):
        row[k] = pk.get(k)
    return row


def derive(rows: list[dict]) -> list[dict]:
    """Aus Zählerständen Werte je Zeitraum: Rauschpegel, Sende-/Empfangszeit in %, Pakete je Stunde."""
    out = []
    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > MAX_GAP_S:
            continue
        def d(k):
            x, y = a.get(k), b.get(k)
            return None if x is None or y is None or y < x else y - x       # y < x: Node wurde neu gestartet
        tx, rx, recv, sent = d("tx_air"), d("rx_air"), d("recv"), d("sent")
        out.append({"ts": b["ts"], "noise_floor": b.get("noise_floor"),
                    "tx_pct": None if tx is None else round(100 * tx / dt, 3), "rx_pct": None if rx is None else round(100 * rx / dt, 2),
                    "recv_per_h": None if recv is None else round(recv * 3600 / dt, 1),
                    "sent_per_h": None if sent is None else round(sent * 3600 / dt, 1)})
    return out


class StatsSampler:
    def __init__(self, node: BaseNode, store, every_s: float = SAMPLE_EVERY_S, clock: Callable[[], float] = time.time):
        self.node, self.store, self.every_s, self._clock = node, store, every_s, clock
        self.last_error: str | None = None
        self.enabled = True

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self.store.set_setting("radio_stats_enabled", enabled)

    def status(self) -> dict:
        return {"enabled": self.enabled, "every_s": self.every_s}

    async def sample(self) -> dict | None:
        """Ein Messpunkt; wirft nie. Gibt die gespeicherte Zeile zurück oder None."""
        try:
            if not self.node.connected:
                return None
            row = flatten(await asyncio.wait_for(self.node.node_stats(), 20))
            await self.store.add_radio_stats(row, self._clock())
            self.last_error = None
            return row
        except NodeError as e:
            self.last_error = str(e)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Funkstatistik nicht lesbar: %s", self.last_error)
        return None

    async def run(self) -> None:
        self.enabled = bool(await self.store.get_setting("radio_stats_enabled", True))
        await asyncio.sleep(30)
        while True:
            if self.enabled:
                await self.sample()
            await asyncio.sleep(self.every_s)
