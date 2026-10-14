"""Tempolimit für Funkaktionen, die man von Hand in der Oberfläche auslöst (Nachricht, Kanal, Advert, Ping, Abfragen).

Die Automatik hat ihr eigenes Sendebudget (`SendBudget`); von Hand gab es bisher keine Grenze. Ein Fehler in der
Oberfläche, ein hängender Knopf oder eine fremde Webseite hätten so in einer Schleife senden lassen können
(LoRa ist ein geteiltes Medium mit Sendezeitgrenzen). Zu schnell → HTTP 429 mit Wartezeit, nichts wird gesendet.
Die Repeater-Verwaltung hat ihr eigenes Tempo (`repeater._radio_slot`) und läuft nicht hierüber.
"""
from __future__ import annotations

import time
from collections import deque

MANUAL_MIN_GAP_S = 2.0       # Standard: Mindestabstand zwischen zwei Funkaktionen von Hand (config: manual_min_gap_s)
MANUAL_MAX_PER_MIN = 20      # Standard: höchstens so viele Funkpakete je Minute, rollend (config: manual_max_per_min)


class ManualLimit:
    def __init__(self, min_gap_s: float | None = None, max_per_min: int | None = None, clock=time.monotonic):
        self.min_gap_s = MANUAL_MIN_GAP_S if min_gap_s is None else min_gap_s
        self.max_per_min = MANUAL_MAX_PER_MIN if max_per_min is None else max_per_min
        self._clock = clock
        self._sent: deque[float] = deque()

    def take(self, n: int = 1) -> float:
        """0 = darf senden (verbucht n Pakete), sonst Sekunden bis zum nächsten erlaubten Versuch. Ohne await: kein Rennen."""
        n = max(1, int(n))
        now = self._clock()
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        if self._sent and now - self._sent[-1] < self.min_gap_s:
            return self.min_gap_s - (now - self._sent[-1])
        if len(self._sent) + n > self.max_per_min:
            return 60 - (now - self._sent[0]) if self._sent else 60.0
        self._sent.extend([now] * n)
        return 0.0
