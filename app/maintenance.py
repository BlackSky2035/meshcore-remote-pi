"""Wartungsmodus: Node-Anschluss freigeben, Senden/Empfangen ruht, Ausfallmeldungen schweigen, Ende nach Ablauf von selbst.

Zweck: Node kurz abziehen, in der MeshCore-App oder an einem anderen Rechner benutzen (nur ein Programm darf den Anschluss halten) oder Senden/Empfang pausieren,
ohne Alarm auszulösen. Der Zeitpunkt des Endes wird gespeichert (überlebt einen Neustart); maximal 24 Stunden, damit er nicht vergessen wird.
Geplante Wartung zählt in der USB-Verfügbarkeit nicht als Ausfall.
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("meshcore-remote")
MIN_MIN, MAX_MIN = 5, 1440


class Maintenance:
    def __init__(self, store, emit: Callable[[dict], Awaitable[None]], log_line: Callable[[str, str], Awaitable[None]] | None = None,
                 wall: Callable[[], float] = time.time):
        self.store, self.emit, self._log, self._wall = store, emit, log_line, wall
        self.until: float | None = None
        self._loaded = False

    async def _say(self, text: str) -> None:
        if self._log:
            try:
                await self._log("info", text)
            except Exception:
                pass

    async def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            v = await self.store.get_setting("maintenance_until")
            self.until = float(v) if isinstance(v, (int, float)) else None
        except Exception:
            log.exception("Wartungszustand nicht lesbar")

    def active(self) -> bool:
        return self.until is not None and self._wall() < self.until

    def view(self) -> dict:
        a = self.active()
        return {"active": a, "until": self.until if a else None,
                "remaining_s": max(0, int(self.until - self._wall())) if a else 0}

    async def start(self, minutes: int) -> dict:
        await self.load()
        minutes = max(MIN_MIN, min(int(minutes), MAX_MIN))
        self.until = self._wall() + minutes * 60
        await self._save()
        await self._say(f"Wartungsmodus für {minutes} min gestartet: Anschluss frei, Senden und Meldungen ruhen")
        await self.emit({"type": "maintenance", "payload": self.view()})
        return self.view()

    async def end(self, why: str = "beendet") -> dict:
        await self.load()
        was = self.until is not None
        self.until = None
        await self._save()
        if was:
            await self._say(f"Wartungsmodus {why}")
            await self.emit({"type": "maintenance", "payload": self.view()})
        return self.view()

    async def tick(self) -> bool:
        """Vom Supervisor aufgerufen. True = Wartung läuft. Abgelaufene Wartung wird hier beendet."""
        await self.load()
        if self.until is None:
            return False
        if self.active():
            return True
        await self.end("abgelaufen")
        return False

    async def _save(self) -> None:
        try:
            if self.until is None:
                await self.store.delete_setting("maintenance_until")
            else:
                await self.store.set_setting("maintenance_until", self.until)
        except Exception:
            log.exception("Wartungszustand nicht gespeichert")       # läuft im Speicher weiter
