"""Raum-Bot: ein Software-„Room“ ohne Extra-Firmware, nur durch die Festplatte begrenzt (statt 32 Beiträge wie die Room-Firmware).

So funktioniert es: Bekannte Kontakte (Clients) schreiben dem eigenen Node Direktnachrichten mit Befehlen:
  /p <Text>   Beitrag schreiben        /neu       neue Beiträge holen (höchstens 3 je Antwort)
  /verlauf [n] die letzten n (1–5)     /anmelden  neue Beiträge automatisch zugestellt bekommen    /abmelden   /hilfe
Alle Beiträge liegen unbegrenzt in der Datenbank (max_posts, Standard 5000) und sind auf der Webseite lesbar.
Grenzen (bewusst): Jede Antwort ist eine Funknachricht und zählt zum Sendebudget der Automatik (Stundenlimit, Mindestabstand); je Beitrag höchstens
100 Zeichen; unbekannte Absender werden ignoriert; je Mitglied höchstens 20 Beiträge pro Stunde und 20 s Pause. Antworten tragen das Präfix „[auto] “,
damit sich zwei solche Bots (z. B. auf beiden Pis) nie gegenseitig antworten. In der MeshCore-App erscheint der Node als normaler Kontakt, nicht als Room.
An echter Hardware nicht getestet.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from .automation import AUTO_PREFIX, SendBudget
from .node.base import BaseNode, NodeError

log = logging.getLogger("meshcore-remote")

DEFAULT = {"enabled": False, "title": "Raum", "max_posts": 5000}
POST_MAX = 100     # ein Beitrag plus „[auto] Raum [12 14:32] @[Name] “ muss in eine Nachricht von höchstens LINE_MAX passen
MAX_SUBSCRIBERS = 10
PER_REQUEST = 3
HISTORY_MAX = 5
COOLDOWN_S = 20.0
POSTS_PER_HOUR = 20
DELIVER_EVERY_S = 20.0
DELIVER_PER_CYCLE = 3
MAX_TRIES = 3                # erfolglose Zustellversuche (je Durchlauf) je Beitrag und Empfänger
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
HELP = "Befehle: /p Text (schreiben), /neu, /verlauf [1-5], /anmelden, /abmelden"
LINE_MAX = 150      # so lang darf eine Nachricht an die MeshCore-App höchstens sein (Zähler dort: 150), gemessen in BYTES (Umlaute/Emojis zählen mehr);
                    # die Empfangszeit wird nur angehängt, wenn es passt, und jede Zeile wird zur Sicherheit auf LINE_MAX gekürzt


def mention_tag(name: str) -> str:
    """Erwähnung "@[Name]". Eckige Klammern im Namen würden sie zerreißen (MeshCore erlaubt sie seit App 1.27 in Namen nicht mehr) und werden entfernt.
    Leer, wenn nichts übrig bleibt."""
    n = " ".join(str(name or "").replace("[", "").replace("]", "").split())
    return f"@[{n}]" if n else ""


class RoomError(NodeError):
    pass


def _blen(text: str) -> int:
    return len(text.encode("utf-8"))


def cut_to_bytes(text: str, limit: int = LINE_MAX, mark: str = "...") -> str:
    """Kürzt `text` auf höchstens `limit` Byte (UTF-8), ohne ein Zeichen zu zerreißen, und hängt `mark` an. Kürzer bleibt unverändert."""
    if _blen(text) <= limit:
        return text
    cut = text.encode("utf-8")[: max(0, limit - len(mark))].decode("utf-8", "ignore")
    return cut.rstrip() + mark


def clean_text(text: str) -> str:
    t = " ".join(str(text).split())
    if not t:
        raise RoomError("Der Beitrag ist leer.")
    if _CTRL.search(t):
        raise RoomError("Steuerzeichen sind nicht erlaubt.")
    if len(t) > POST_MAX:
        raise RoomError(f"Beitrag zu lang (höchstens {POST_MAX} Zeichen).")
    return t


class Room:
    def __init__(self, node: BaseNode, store, budget: SendBudget, log_line: Callable[[str, str], Awaitable[None]],
                 emit: Callable[[dict], Awaitable[None]], clock: Callable[[], float] = time.monotonic):
        self.node, self.store, self.budget, self._log, self.emit, self._clock = node, store, budget, log_line, emit, clock
        self._last_post: dict[str, float] = {}
        self._hourly: dict[str, deque] = defaultdict(deque)
        self._tries: dict[tuple[str, int], int] = {}
        self._locks: dict[str, asyncio.Lock] = {}   # je Mitglied: /neu, /verlauf und automatische Zustellung nie gleichzeitig
        self._unacked = False          # letzter _say: gesendet, aber nicht bestätigt

    # ---- Einstellungen ---------------------------------------------------------
    async def config(self) -> dict:
        saved = await self.store.get_setting("room_config", {})
        return {**DEFAULT, **(saved if isinstance(saved, dict) else {})}

    async def set_config(self, enabled: bool, title: str, max_posts: int) -> dict:
        cfg = {"enabled": bool(enabled), "title": " ".join(title.split())[:30] or "Raum", "max_posts": max(100, min(int(max_posts), 20000))}
        await self.store.set_setting("room_config", cfg)
        return cfg

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # ---- Senden (mit Budget) -------------------------------------------------------
    def _line(self, text: str) -> str:
        return cut_to_bytes(AUTO_PREFIX + text)                      # nie länger als LINE_MAX: der Node soll nie eine zu lange Nachricht bekommen

    async def _say(self, key: str, name: str, text: str, confirm: bool = False) -> bool:
        """True = gesendet (bei `confirm` zusätzlich vom Empfänger bestätigt). Sonst False, Grund steht im Protokoll."""
        if not await self.budget.acquire():
            await self._log("warn", f"Raum: Antwort an {name} übersprungen (Stundenlimit erreicht)")
            return False
        line = self._line(text)
        self._unacked = False
        try:
            res = await self.node.send_message(key, line, confirm=True) if confirm else await self.node.send_message(key, line)
            await self.store.add_message("out", "direct", key, line, name=name, auto=True)
            if confirm and not (res or {}).get("acked", True):
                self._unacked = True
                await self._log("warn", f"Raum: keine Empfangsbestätigung von {name}")
                return False
            return True
        except NodeError as e:
            await self._log("error", f"Raum: Senden an {name} fehlgeschlagen: {e}")
            return False

    @staticmethod
    def render(post: dict, title: str, now: float | None = None) -> str:
        """Beitrag als Funkzeile: "<Titel> [Nr Zeit] @[Autor] Text". Der Autor ist eine echte Erwähnung (bei Beiträgen von der Webseite, die keinen
        Kontakt haben, bleibt es „Web: Text“). Die Uhrzeit, zu der der Beitrag beim Raum ankam, kommt dazu, solange die ganze Nachricht (mit „[auto] “)
        höchstens LINE_MAX Byte lang ist: erst mit Datum (bei Beiträgen von einem früheren Tag), dann nur Uhrzeit, sonst ganz ohne. Passt es auch dann nicht
        (ältere, längere Beiträge, lange Namen), wird der Text mit "..." gekürzt, statt dem Node eine zu lange Nachricht zu geben."""
        plain = " ".join(str(post["author"] or "").replace("[", "").replace("]", "").split()) or "Unbekannt"      # nie mit Klammern: sie wirken in der App wie eine Erwähnung
        who = (mention_tag(post["author"]) if post.get("author_key") != "web" else "") or f"{plain}:"
        body = f"{who} {post['text']}"
        stamps: list[str] = []
        try:
            t, n = time.localtime(post["ts"]), time.localtime(now if now is not None else time.time())
            hm = time.strftime("%H:%M", t)
            stamps = [hm] if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday) else [time.strftime("%d.%m. %H:%M", t), hm]
        except Exception:
            stamps = []
        for st in stamps:
            line = f"{title} [{post['id']} {st}] {body}"
            if _blen(AUTO_PREFIX + line) <= LINE_MAX:
                return line
        line = f"{title} [{post['id']}] {body}"
        return cut_to_bytes(AUTO_PREFIX + line)[len(AUTO_PREFIX):]

    # ---- Eingehende Befehle ---------------------------------------------------------
    async def handle_incoming(self, payload: dict, contact: dict | None) -> None:
        """Nur Direktnachrichten, die mit „/“ beginnen, von bekannten Clients. Wirft nie nach außen (der Aufrufer fängt zusätzlich ab)."""
        if payload.get("txt_type", 0) != 0 or contact is None or contact.get("type_name") != "client" or not self.node.connected:
            return
        text = (payload.get("text") or "").strip()
        if not text.startswith("/") or text.startswith(AUTO_PREFIX.strip()):
            return
        cfg = await self.config()
        if not cfg["enabled"]:
            return
        key, name = contact["public_key"], contact["name"]
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        member = await self.store.room_member(key, name)
        if cmd in ("hilfe", "help", "?"):
            await self._say(key, name, f"{cfg['title']}: {HELP}")
        elif cmd == "p":
            await self._post(key, name, arg, cfg)
        elif cmd in ("neu", "new"):
            async with self._lock(key):                      # sonst holt die automatische Zustellung denselben Beitrag parallel (doppelt)
                member = await self.store.room_member(key, name)
                posts = await self.store.room_posts(member["last_seen_id"], PER_REQUEST + 1)
                more = len(posts) > PER_REQUEST
                posts = posts[:PER_REQUEST]
                if not posts:
                    await self._say(key, name, f"{cfg['title']}: keine neuen Beiträge")
                    return
                for p in posts:
                    if not await self._say(key, name, self.render(p, cfg['title']), confirm=True):
                        break                                # nicht bestätigt: bleibt ungelesen, /neu holt es erneut
                    await self.store.room_update_member(key, last_seen_id=p["id"])
                else:
                    if more:
                        await self._say(key, name, f"{cfg['title']}: es gibt weitere, /neu")
        elif cmd in ("verlauf", "history"):
            n = int(arg) if arg.isdigit() else 3
            async with self._lock(key):
                posts = await self.store.room_posts(0, max(1, min(n, HISTORY_MAX)), newest=True)
                if not posts:
                    await self._say(key, name, f"{cfg['title']}: noch keine Beiträge")
                for p in posts:
                    if not await self._say(key, name, self.render(p, cfg['title']), confirm=True):
                        break
                    await self.store.room_update_member(key, last_seen_id=p["id"])
        elif cmd == "anmelden":
            subs = [m for m in await self.store.room_members() if m["subscribed"] and m["public_key"] != key]
            if len(subs) >= MAX_SUBSCRIBERS:
                await self._say(key, name, f"{cfg['title']}: Anmeldeliste voll, bitte /neu benutzen")
            else:
                # ab jetzt nur noch Neues: alles bis hierher gilt als gesehen
                await self.store.room_update_member(key, last_seen_id=await self.store.room_max_id(), subscribed=True)
                await self._say(key, name, f"{cfg['title']}: angemeldet, neue Beiträge kommen automatisch (/abmelden)")
        elif cmd == "abmelden":
            await self.store.room_update_member(key, subscribed=False)
            await self._say(key, name, f"{cfg['title']}: abgemeldet")
        else:
            await self._say(key, name, f"{cfg['title']}: {HELP}")

    async def _post(self, key: str, name: str, text: str, cfg: dict) -> None:
        now = self._clock()
        try:
            text = clean_text(text)
            if now - self._last_post.get(key, -1e9) < COOLDOWN_S:
                raise RoomError("Bitte kurz warten (20 s Pause zwischen Beiträgen).")
            hist = self._hourly[key]
            while hist and now - hist[0] > 3600:
                hist.popleft()
            if len(hist) >= POSTS_PER_HOUR:
                raise RoomError("Stundenlimit für Beiträge erreicht.")
        except RoomError as e:
            await self._say(key, name, f"{cfg['title']}: {e}")
            return
        self._last_post[key] = now
        self._hourly[key].append(now)
        pid = await self.store.room_add_post(key, name, text, cfg["max_posts"])
        await self.store.room_update_member(key, last_seen_id=pid)          # eigener Beitrag zählt als gesehen
        await self._log("info", f"Raum: Beitrag {pid} von {name}")
        await self.emit({"type": "room_post", "payload": {"id": pid}})
        await self._say(key, name, f"{cfg['title']}: gespeichert als [{pid}]")

    async def post_from_web(self, text: str, author: str = "Web") -> dict:
        cfg = await self.config()
        text = clean_text(text)
        pid = await self.store.room_add_post("web", author, text, cfg["max_posts"])
        await self.emit({"type": "room_post", "payload": {"id": pid}})
        return {"id": pid}

    # ---- Zustellung an Angemeldete ---------------------------------------------------------
    async def deliver_once(self) -> int:
        """Schickt Angemeldeten ihren nächsten Beitrag (höchstens DELIVER_PER_CYCLE Sendungen je Durchlauf). Gibt die Zahl gesendeter Nachrichten zurück."""
        cfg = await self.config()
        if not cfg["enabled"] or not self.node.connected:
            return 0
        sent = 0
        for m0 in await self.store.room_members():
            if not m0["subscribed"] or sent >= DELIVER_PER_CYCLE:
                continue
            async with self._lock(m0["public_key"]):
                m = next((x for x in await self.store.room_members() if x["public_key"] == m0["public_key"]), None)    # frischer Stand nach dem Warten
                if m is None or not m["subscribed"]:
                    continue
                ok, n = await self._deliver_member(m, cfg)
            sent += n
            if not ok:
                return sent                               # Budget leer oder Fehler: später weiter
        return sent

    async def _deliver_member(self, m: dict, cfg: dict) -> tuple[bool, int]:
        """Nächsten ungesehenen fremden Beitrag an ein Mitglied senden (Sperre des Mitglieds wird vom Aufrufer gehalten). -> (weitermachen, gesendet)"""
        key = m["public_key"]
        for p in await self.store.room_posts(m["last_seen_id"], 5):
            if p["author_key"] == key:
                await self.store.room_update_member(key, last_seen_id=p["id"])
                continue
            if not await self._say(key, m["name"], self.render(p, cfg['title']), confirm=True):
                # Budget leer, Fehler oder keine Bestätigung: der Beitrag bleibt ungesehen und kommt im nächsten Durchlauf erneut.
                # Nach MAX_TRIES erfolglosen Durchläufen wird er übersprungen, damit ein dauerhaft unerreichbarer Empfänger nichts blockiert.
                if not self._unacked:                     # Budget/Node-Fehler zählen nicht als Fehlversuch
                    return False, 0
                k = (key, p["id"])
                self._tries[k] = self._tries.get(k, 0) + 1
                if self._tries[k] < MAX_TRIES:
                    return False, 0
                await self._log("warn", f"Raum: Beitrag {p['id']} an {m['name']} nach {MAX_TRIES} Versuchen übersprungen")
            self._tries.pop((key, p["id"]), None)
            await self.store.room_update_member(key, last_seen_id=p["id"])
            return True, 1
        return True, 0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(DELIVER_EVERY_S)
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Raum-Zustellung fehlgeschlagen")
