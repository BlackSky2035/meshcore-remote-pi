"""Sendeprotokoll im Arbeitsspeicher: wann hat der Node zuletzt gesendet?

Der Watchdog legt damit Einbrüche der Versorgungsspannung neben Sendezeiten (der Heltec zieht beim Senden bis ca. 750 mA).
Bewusst minimal, damit der Pi nicht belastet wird: je Sendung ein Eintrag (Zeit, Art) in einem Ringpuffer fester Größe, keine
Datenbank, keine Hintergrundaufgabe, kein Nachrichteninhalt. Der Watchdog fragt nur nach einem Einbruch ab (`/api/sends`).

`instrument(node)` umhüllt die sendenden Methoden des Nodes an EINER Stelle (echter Node und Simulator gleich). Neue Methoden, die Funk
auslösen, hier in `TX_KINDS` eintragen. Wirft nie: ein Fehler im Protokoll darf kein Senden verhindern.
"""
from __future__ import annotations

import functools
import logging
import time
from collections import deque
from typing import Callable

log = logging.getLogger("meshcore-remote")

MAXLEN = 300

# Methode des Nodes -> Beschriftung (alles, was Funkzeit braucht, also auch Anfragen, die eine Antwort abwarten)
TX_KINDS = {
    "send_message": "Nachricht",
    "send_channel_message": "Kanal-Nachricht",
    "send_advert": "Advert",
    "ping": "Ping",
    "repeater_status": "Statusabfrage",
    "repeater_telemetry": "Telemetrie",
    "repeater_login": "Repeater-Anmeldung",
    "repeater_logout": "Repeater-Abmeldung",
    "repeater_command": "Repeater-Befehl",
    "repeater_neighbours": "Nachbarliste",
    "repeater_acl": "Zugriffsliste",
    "reset_path": "Pfad zurücksetzen",
}


class SendLog:
    def __init__(self, maxlen: int = MAXLEN, wall: Callable[[], float] = time.time):
        self._d: deque[tuple[float, str]] = deque(maxlen=maxlen)
        self._wall = wall
        self.maxlen = maxlen

    def add(self, kind: str) -> None:
        try:
            self._d.append((self._wall(), kind))
        except Exception:
            pass

    def since(self, ts: float, limit: int = 100) -> list[dict]:
        try:
            return [{"ts": round(t, 2), "kind": k} for t, k in list(self._d) if t >= ts][-limit:]
        except Exception:
            return []

    def instrument(self, node) -> int:
        """Umhüllt die vorhandenen sendenden Methoden von `node`; liefert, wie viele es waren. Doppeltes Umhüllen wird verhindert."""
        n = 0
        for name, kind in TX_KINDS.items():
            orig = getattr(node, name, None)
            if orig is None or getattr(orig, "_sendlog", False):
                continue

            def make(orig=orig, kind=kind):
                @functools.wraps(orig)
                async def wrapper(*a, **kw):
                    self.add(kind)                      # Zeitpunkt der Sendung, nicht der Antwort
                    return await orig(*a, **kw)
                wrapper._sendlog = True
                return wrapper
            try:
                setattr(node, name, make())
                n += 1
            except Exception:
                log.warning("Sendeprotokoll: %s nicht umhüllt", name)
        return n
