"""Push-Benachrichtigungen (Web-Push) für einzelne Kanäle und Akku-Warnungen.

Sicherheit und Ausfallsicherheit:
- Senden blockiert nie den Ereignispfad: jede Benachrichtigung läuft als eigene Task, das eigentliche Senden in einem Thread
  mit Zeitlimit und höchstens 4 gleichzeitig.
- Jeder Fehler wird geschluckt und protokolliert. Abos, die der Push-Dienst als beendet meldet (404/410), werden gelöscht.
- Je Gerät höchstens 120 Benachrichtigungen pro Stunde (Schutz vor Fluten); gleiche `tag`s ersetzen sich auf dem Gerät.
- Der Inhalt ist für das Gerät verschlüsselt (RFC 8291); der Push-Dienst von Apple/Google sieht ihn nicht.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from . import webpush
from .battery import battery_pct
from .storage import AsyncStorage

log = logging.getLogger("meshcore-remote")

MAX_PER_HOUR = 120
SEND_TIMEOUT_S = 12.0
DEFAULT_CONTACT = "mailto:meshcore-remote@example.com"   # Pflichtangabe von VAPID; Apple verlangt eine gültig aussehende Adresse
NAMES_TTL_S = 300


class PushService:
    def __init__(self, store: AsyncStorage, node=None, send=webpush.send, clock=time.time, contact: str = DEFAULT_CONTACT):
        self.store, self.node, self._send, self._clock, self.contact = store, node, send, clock, contact
        self._vapid: dict | None = None
        self._sent: dict[str, deque] = defaultdict(deque)
        self._sem = asyncio.Semaphore(4)
        self._tasks: set[asyncio.Task] = set()
        self._names: tuple[float, dict[int, str]] = (0.0, {})
        self.problem: str | None = None

    # ---- Schlüssel -------------------------------------------------------
    async def vapid(self) -> dict:
        if self._vapid is None:
            saved = await self.store.get_setting("push_vapid")
            if not (isinstance(saved, dict) and saved.get("private_pem") and saved.get("public_key")):
                saved = webpush.generate_vapid()
                await self.store.set_setting("push_vapid", saved)
            self._vapid = saved
        return self._vapid

    async def public_key(self) -> str:
        return (await self.vapid())["public_key"]

    # ---- Abos ------------------------------------------------------------
    async def subscribe(self, endpoint: str, p256dh: str, auth: str, channels, battery: bool, label: str = "") -> None:
        webpush.validate_endpoint(endpoint)
        if len(webpush.b64u_decode(p256dh)) != 65 or len(webpush.b64u_decode(auth)) != 16:
            raise ValueError("ungültige Schlüssel des Geräts")
        await self.store.save_push_sub(endpoint, p256dh, auth, channels, battery, label)

    # ---- Benachrichtigen -----------------------------------------------------
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wartet auf laufende Sendeaufgaben (für Tests). Siehe `Hub.drain`: nur auf laufende warten und danach der Schleife Luft geben,
        sonst Endlosschleife, wenn eine fertige Aufgabe noch eingetragen ist."""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    def notify_channel(self, idx: int, sender: str | None, text: str) -> None:
        """Sofort zurückkehren; das Senden passiert im Hintergrund."""
        self._spawn(self._guard(self._channel(idx, sender, text)))

    def notify_keyword(self, word: str, where: str, sender: str | None, text: str) -> None:
        """Stichwort gefunden: geht an alle angemeldeten Geräte, auch wenn der Kanal dort stummgeschaltet ist."""
        self._spawn(self._guard(self._keyword(word, where, sender, text)))

    async def _keyword(self, word: str, where: str, sender: str | None, text: str) -> None:
        subs = await self.store.push_subs()
        if subs:
            body = f"{sender}: {text}" if sender else text
            await self._deliver(subs, {"title": f"Stichwort „{word}“ in {where}", "body": body[:160], "tag": f"kw-{word}", "url": "/#/search"})

    def notify_battery(self, name: str, mv: int) -> None:
        self._spawn(self._guard(self._battery(name, mv)))

    def notify_usb(self, kind: str, text: str) -> None:
        """Ausfall/Rückkehr des USB-Nodes an alle Geräte mit „Warnungen“ (gleiche Einstellung wie Akku)."""
        self._spawn(self._guard(self._usb(kind, text)))

    def notify_station(self, kind: str, text: str) -> None:
        """Ausfall/Rückkehr einer anderen Station (gleiche Einstellung „Warnungen“ wie Akku und USB)."""
        self._spawn(self._guard(self._station(kind, text)))

    async def _station(self, kind: str, text: str) -> None:
        subs = [s for s in await self.store.push_subs() if s["battery"]]
        if subs:
            await self._deliver(subs, {"title": "Station nicht erreichbar" if kind == "down" else "Station wieder da",
                                       "body": text, "tag": "station", "url": "/#/dashboard"})

    async def _usb(self, kind: str, text: str) -> None:
        subs = [s for s in await self.store.push_subs() if s["battery"]]
        if subs:
            await self._deliver(subs, {"title": "USB-Node nicht verfügbar" if kind == "down" else "USB-Node wieder da",
                                       "body": text, "tag": "usb", "url": "/#/dashboard"})

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("Push: unerwarteter Fehler")

    async def _channel_name(self, idx: int) -> str:
        at, names = self._names
        if self._clock() - at > NAMES_TTL_S and self.node is not None:
            try:
                names = {c["idx"]: c["name"] for c in await asyncio.wait_for(self.node.channels(), 5)}
                self._names = (self._clock(), names)
            except Exception:
                self._names = (self._clock(), names)          # nicht bei jeder Nachricht neu versuchen
        return names.get(idx) or f"Kanal {idx}"

    async def _channel(self, idx: int, sender: str | None, text: str) -> None:
        subs = [s for s in await self.store.push_subs() if idx in s["channels"]]
        if not subs:
            return
        # „einmal“: nur melden, wenn diese Nachricht die erste ungelesene ist. Danach schweigt der Kanal, bis er auf
        # der Webseite angesehen wurde (dann ist der Zähler wieder 0). Zustandslos: der Zähler steht in der Datenbank.
        if any(s["channels"][idx] == "once" for s in subs):
            unread = (await self.store.channel_unread()).get(idx, 0)
            subs = [s for s in subs if s["channels"][idx] == "every" or unread <= 1]
            if not subs:
                return
        name = await self._channel_name(idx)
        body = f"{sender}: {text}" if sender else text
        payload = {"title": name, "body": body[:160], "tag": f"ch{idx}", "url": f"/#/channels?c={idx}"}
        await self._deliver(subs, payload)

    async def _battery(self, name: str, mv: int) -> None:
        subs = [s for s in await self.store.push_subs() if s["battery"]]
        if subs:
            await self._deliver(subs, {"title": "Akku niedrig", "body": f"{name}: {battery_pct(mv)} % ({mv} mV)", "tag": f"batt-{name}", "url": "/#/battery"})

    async def send_test(self, endpoint: str) -> str:
        sub = await self.store.push_sub(endpoint)
        if sub is None:
            raise KeyError("Dieses Gerät ist nicht angemeldet")
        res = await self._one(sub, {"title": "MeshCore Remote", "body": "Test: Benachrichtigungen funktionieren.",
                                    "tag": "test", "url": "/#/channels"})
        if res != "ok":
            raise RuntimeError(res)
        return res

    async def _deliver(self, subs: list[dict], payload: dict) -> None:
        await asyncio.gather(*(self._one(s, payload) for s in subs), return_exceptions=True)

    def _allowed(self, endpoint: str) -> bool:
        q, now = self._sent[endpoint], self._clock()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= MAX_PER_HOUR:
            return False
        q.append(now)
        return True

    async def _one(self, sub: dict, payload: dict) -> str:
        if not self._allowed(sub["endpoint"]):
            return "Limit erreicht (zu viele Benachrichtigungen pro Stunde)"
        try:
            vapid = await self.vapid()
            async with self._sem:
                await asyncio.wait_for(asyncio.to_thread(self._send, sub, payload, vapid, self.contact), SEND_TIMEOUT_S)
            self.problem = None
            return "ok"
        except webpush.PushGone:
            await self.store.delete_push_sub(sub["endpoint"])           # Gerät hat sich abgemeldet
            return "Abo ist abgelaufen und wurde entfernt"
        except asyncio.TimeoutError:
            self.problem = "Push-Dienst antwortet nicht (Internet?)"
        except webpush.PushError as e:
            self.problem = f"Push-Dienst meldet Fehler: {e}"
        except Exception as e:
            log.exception("Push senden fehlgeschlagen")
            self.problem = f"{type(e).__name__}: {e}"
        log.warning("Push: %s", self.problem)
        return self.problem or "Fehler"
