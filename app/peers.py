"""Gegenseitige Überwachung mehrerer Stationen.

Jede Station kann ihren Kurzstatus unter /api/peer/summary herausgeben (nur mit dem Geheimwort `peer_token` im Kopf „X-Peer-Token“; ohne
gesetztes Geheimwort ist der Endpunkt aus). `PeerWatch` fragt die unter `peers` eingetragenen Stationen jede Minute ab. Fällt eine drei Abfragen
in Folge aus, gibt es EINE Meldung (Protokoll, Banner, Push an Geräte mit „Warnungen“), und eine bei Rückkehr. Das behebt die Schwäche, dass ein
Pi ohne Netz nichts mehr melden kann: Die andere Station merkt es. Es fließt nur der Kurzstatus (keine Nachrichten, Schlüssel oder Kontakte).
Abfragen gehen nur an Adressen aus der eigenen Konfiguration, mit Zeitlimit und Größenbegrenzung. Nicht im echten Netz getestet (Tailscale/https).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

from .webpush import no_redirect_open

log = logging.getLogger("meshcore-remote")
POLL_S = 60.0
FAILS_BEFORE_ALERT = 3
MAX_BYTES = 65536
TIMEOUT_S = 8


@dataclass
class Peer:
    name: str
    url: str
    token: str
    ok: bool | None = None
    fails: int = 0
    alerted: bool = False
    last_ok: float | None = None
    error: str = ""
    summary: dict = field(default_factory=dict)


def parse_peers(raw: str) -> list[Peer]:
    out: list[Peer] = []
    for part in (raw or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split("|")]
        if len(bits) != 3 or not all(bits):
            log.warning("Station ignoriert (Format „Name|https://adresse|Geheimwort“): %s", bits[0] if bits else part)
            continue
        u = urlparse(bits[1])
        if u.scheme not in ("http", "https") or not u.hostname:
            log.warning("Station %s ignoriert: ungültige Adresse", bits[0])
            continue
        out.append(Peer(bits[0][:40], bits[1].rstrip("/"), bits[2]))
    return out[:5]


def token_ok(expected: str, given: str | None) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(expected.encode(), given.encode())


def fetch_summary(peer: Peer) -> dict:
    """Blockierend (läuft im Thread). Wirft bei jedem Fehler."""
    req = urllib.request.Request(peer.url + "/api/peer/summary", headers={"X-Peer-Token": peer.token, "Accept": "application/json"})
    with no_redirect_open(req, timeout=TIMEOUT_S) as r:                           # keine Weiterleitung: das Token geht nur an die eingetragene Adresse
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Antwort zu groß")
    d = json.loads(data)
    if not isinstance(d, dict):
        raise ValueError("unerwartete Antwort")
    return d


class PeerWatch:
    def __init__(self, peers: list[Peer], notify: Callable[[str, str], Awaitable[None]],
                 fetch: Callable[[Peer], dict] = fetch_summary, clock: Callable[[], float] = time.time):
        self.peers, self.notify, self._fetch, self._clock = peers, notify, fetch, clock
        # eigener kleiner Pool und je Station höchstens EINE Abfrage gleichzeitig: hängt eine (DNS ohne Internet), stapeln sich
        # nicht jede Minute neue Threads im gemeinsamen Pool, den auch die Datenbank nutzt
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(len(peers), 4)), thread_name_prefix="peers")
        self._running: dict[str, object] = {}

    def status(self) -> list[dict]:
        return [{"name": p.name, "url": p.url, "ok": p.ok, "last_ok": p.last_ok, "error": p.error, "alerted": p.alerted,
                 "summary": p.summary} for p in self.peers]

    async def poll_once(self) -> None:
        await asyncio.gather(*(self._poll(p) for p in self.peers), return_exceptions=True)

    async def _poll(self, p: Peer) -> None:
        try:
            prev = self._running.get(p.name)
            if prev is not None and not prev.done():   # vorige Abfrage hängt noch: keine zweite starten, zählt als Fehlschlag
                raise TimeoutError("vorige Abfrage hängt noch")
            fut = asyncio.get_running_loop().run_in_executor(self._pool, self._fetch, p)
            self._running[p.name] = fut
            p.summary = await asyncio.wait_for(asyncio.shield(fut), TIMEOUT_S + 5)
            p.ok, p.fails, p.error, p.last_ok = True, 0, "", self._clock()
            if p.alerted:
                p.alerted = False
                await self._tell("up", f"Station „{p.name}“ ist wieder erreichbar")
        except Exception as e:
            p.ok, p.fails = False, p.fails + 1
            p.error = f"{type(e).__name__}: {e}"[:200]
            if p.fails >= FAILS_BEFORE_ALERT and not p.alerted:
                p.alerted = True
                await self._tell("down", f"Station „{p.name}“ nicht erreichbar ({p.error})")

    async def _tell(self, kind: str, text: str) -> None:
        try:
            await self.notify(kind, text)
        except Exception:
            log.exception("Stationsmeldung nicht zugestellt")

    async def run(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Stationsabfrage fehlgeschlagen")
            await asyncio.sleep(POLL_S)
