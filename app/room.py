"""Raum-Bot: ein Software-„Room“ ohne Extra-Firmware, nur durch die Festplatte begrenzt (statt 32 Beiträge wie die Room-Firmware).

So funktioniert es: Bekannte Kontakte (Clients) schreiben dem eigenen Node Direktnachrichten mit Befehlen:
  /p <Text>   Beitrag schreiben        /neu       neue Beiträge holen (höchstens 3 je Antwort)
  /verlauf [n] die letzten n (1–5)     /anmelden  neue Beiträge automatisch zugestellt bekommen    /abmelden   /hilfe
Alle Beiträge liegen unbegrenzt in der Datenbank (max_posts, Standard 5000) und sind auf der Webseite lesbar.
Grenzen (bewusst): Jede Antwort ist eine Funknachricht und zählt zum Sendebudget der Automatik (Stundenlimit, Mindestabstand); je Beitrag höchstens
100 Zeichen; unbekannte Absender werden ignoriert; je Mitglied höchstens 20 Beiträge pro Stunde und 20 s Pause zwischen zwei Befehlen (jeder Befehl,
nicht nur /p; Befehle in der Pause werden still ignoriert). Bei pausierter Automatik schweigt auch der Raum. Antworten tragen das Präfix „[auto] “,
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

from .automation import AUTO_PREFIX, SendBudget, cut_bytes, mention_tag
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
CONFIRM_TX_MAX = 3           # send_message(confirm=True) sendet bis zu 3 Mal (RealNode: send_msg_with_retry, max_attempts=3): so viele Plätze im Sendebudget
BACKOFF_MIN_S = 60.0         # nach einer unbestätigten Zustellung: Wartezeit für dieses Mitglied, verdoppelt sich bis BACKOFF_MAX_S, bei Erfolg zurück
BACKOFF_MAX_S = 1800.0
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
ROOM_SHARE = 3               # der Raum darf höchstens 1/ROOM_SHARE des Stunden-Sendebudgets nutzen (Rest bleibt Akku-Überwachung/Pings)
UNKNOWN_HELP_S = 3600        # auf unbekannte Befehle höchstens einmal je Stunde und Mitglied mit der Hilfe antworten
HELP = "Befehle: /p Text (schreiben), /neu, /verlauf [1-5], /anmelden, /abmelden"
LINE_MAX = 150      # so lang darf eine Nachricht an die MeshCore-App höchstens sein (Zähler dort: 150), gemessen in BYTES (Umlaute/Emojis zählen mehr);
                    # die Empfangszeit wird nur angehängt, wenn es passt, und jede Zeile wird zur Sicherheit auf LINE_MAX gekürzt


class RoomError(NodeError):
    pass


def _blen(text: str) -> int:
    return len(text.encode("utf-8"))


def cut_to_bytes(text: str, limit: int = LINE_MAX, mark: str = "...") -> str:
    """Kürzt `text` auf höchstens `limit` Byte (UTF-8), ohne ein Zeichen zu zerreißen, und hängt `mark` an. Kürzer bleibt unverändert."""
    return cut_bytes(text, limit, mark)


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
                 emit: Callable[[dict], Awaitable[None]], clock: Callable[[], float] = time.monotonic,
                 paused: Callable[[], bool] | None = None):
        self.node, self.store, self.budget, self._log, self.emit, self._clock = node, store, budget, log_line, emit, clock
        self._paused = paused or (lambda: False)        # globaler Pausenschalter der Automatik: dann sendet auch der Raum nichts
        self._last_post: dict[str, float] = {}
        self._last_cmd: dict[str, float] = {}           # je Mitglied: letzter beantworteter Befehl (Pause COOLDOWN_S für ALLE Befehle)
        self._backoff_s: dict[str, float] = {}          # je Mitglied: aktuelle Wartezeit nach unbestätigter Zustellung
        self._backoff_until: dict[str, float] = {}
        self._hourly: dict[str, deque] = defaultdict(deque)
        self._tries: dict[tuple[str, int], int] = {}
        self._locks: dict[str, asyncio.Lock] = {}   # je Mitglied: /neu, /verlauf und automatische Zustellung nie gleichzeitig
        self._room_sent: deque = deque()                # (Zeit, Plätze) der eigenen Sendungen in der letzten Stunde
        self._last_help: dict[str, float] = {}          # je Mitglied: letzte Hilfe-Antwort auf einen unbekannten Befehl

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

    async def _say(self, key: str, name: str, text: str, confirm: bool = False) -> str:
        """Status: "ok" = gesendet (bei `confirm` zusätzlich vom Empfänger bestätigt), "unacked" = gesendet, aber nicht bestätigt,
        "error" = nicht gesendet (Pause, Stundenlimit, Node-Fehler; Grund steht im Protokoll). Rückgabe je Aufruf, kein gemeinsamer Zustand:
        parallele Aufrufe (verschiedene Mitglieder) dürfen sich nicht gegenseitig das Ergebnis überschreiben.
        Bestätigtes Senden kann bis zu CONFIRM_TX_MAX Funkpakete kosten und verbucht darum so viele Plätze im Sendebudget."""
        if self._paused():
            return "error"
        n = CONFIRM_TX_MAX if confirm else 1
        if not self._share_left(n):                       # eigenes Teilbudget: der Raum darf Akku-Überwachung/Pings nicht aushungern
            await self._log("warn", f"Raum: Antwort an {name} übersprungen (Anteil des Raums am Sendebudget für diese Stunde erreicht)")
            return "error"
        if not await self.budget.acquire(n):
            await self._log("warn", f"Raum: Antwort an {name} übersprungen ({self.budget.why})")
            return "error"
        self._room_sent.append((self._clock(), n))
        line = self._line(text)
        try:
            res = await self.node.send_message(key, line, confirm=True) if confirm else await self.node.send_message(key, line)
            await self.store.add_message("out", "direct", key, line, name=name, auto=True)
            if confirm and not (res or {}).get("acked", True):
                await self._log("warn", f"Raum: keine Empfangsbestätigung von {name}")
                return "unacked"
            return "ok"
        except NodeError as e:
            await self._log("error", f"Raum: Senden an {name} fehlgeschlagen: {e}")
            return "error"

    def room_cap(self) -> int:
        return max(CONFIRM_TX_MAX, int(self.budget.max_per_hour) // ROOM_SHARE)

    def _share_left(self, n: int) -> bool:
        now = self._clock()
        while self._room_sent and now - self._room_sent[0][0] >= 3600:
            self._room_sent.popleft()
        return sum(k for _, k in self._room_sent) + n <= self.room_cap()

    def _note_unacked(self, key: str) -> None:
        """Unbestätigt: dieses Mitglied erst nach einer Wartezeit wieder automatisch beliefern (60 s, verdoppelt bis 30 min)."""
        wait = min(max(BACKOFF_MIN_S, self._backoff_s.get(key, 0.0) * 2), BACKOFF_MAX_S)
        self._backoff_s[key] = wait
        self._backoff_until[key] = self._clock() + wait

    def _note_acked(self, key: str) -> None:
        self._backoff_s.pop(key, None)
        self._backoff_until.pop(key, None)

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
        if self._paused():
            return                                           # Automatik pausiert: keine Antwort, Befehl wird nicht ausgeführt
        text = (payload.get("text") or "").strip()
        if not text.startswith("/") or text.startswith(AUTO_PREFIX.strip()):
            return
        cfg = await self.config()
        if not cfg["enabled"]:
            return
        key, name = contact["public_key"], contact["name"]
        now = self._clock()
        if now - self._last_cmd.get(key, float("-inf")) < COOLDOWN_S:
            log.info("Raum: Befehl von %s ignoriert (Pause zwischen Befehlen)", name)
            return                                           # still: auch ein Hinweis wäre eine Funknachricht
        self._last_cmd[key] = now
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
                    if await self._say(key, name, self.render(p, cfg['title']), confirm=True) != "ok":
                        break                                # nicht bestätigt: bleibt ungelesen, /neu holt es erneut
                    self._note_acked(key)
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
                    if await self._say(key, name, self.render(p, cfg['title']), confirm=True) != "ok":
                        break
                    self._note_acked(key)
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
        elif now - self._last_help.get(key, float("-inf")) >= UNKNOWN_HELP_S:   # unbekannt: Hilfe nur selten (kostet Funkzeit)
            self._last_help[key] = now
            await self._say(key, name, f"{cfg['title']}: {HELP}")
        else:
            log.info("Raum: unbekannter Befehl von %s ignoriert (Hilfe kam schon in dieser Stunde)", name)

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
        """Schickt Angemeldeten ihren nächsten Beitrag (höchstens DELIVER_PER_CYCLE Sendungen je Durchlauf, für alle Mitglieder zusammen, auch
        unbestätigte zählen). Mitglieder in der Wartezeit nach einer unbestätigten Zustellung werden übersprungen. Gibt die Zahl der zugestellten
        (bestätigten bzw. nach MAX_TRIES übersprungenen) Beiträge zurück."""
        cfg = await self.config()
        if not cfg["enabled"] or not self.node.connected or self._paused():
            return 0
        sent = tx = 0
        for m0 in await self.store.room_members():
            if not m0["subscribed"] or tx >= DELIVER_PER_CYCLE:
                continue
            if self._backoff_until.get(m0["public_key"], float("-inf")) > self._clock():
                continue                                  # zuletzt nicht erreichbar: noch warten
            async with self._lock(m0["public_key"]):
                m = next((x for x in await self.store.room_members() if x["public_key"] == m0["public_key"]), None)    # frischer Stand nach dem Warten
                if m is None or not m["subscribed"]:
                    continue
                status, n, t = await self._deliver_member(m, cfg)
            sent, tx = sent + n, tx + t
            if status == "error":
                return sent                               # Budget leer, Pause oder Node-Fehler: später weiter
            # "unacked": dieses Mitglied wartet jetzt, die anderen bekommen ihre Beiträge trotzdem
        return sent

    async def _deliver_member(self, m: dict, cfg: dict) -> tuple[str, int, int]:
        """Nächsten ungesehenen fremden Beitrag an ein Mitglied senden (Sperre des Mitglieds wird vom Aufrufer gehalten).
        -> (Status "ok" | "unacked" | "error", zugestellt, gesendet)"""
        key = m["public_key"]
        for p in await self.store.room_posts(m["last_seen_id"], 5):
            if p["author_key"] == key:
                await self.store.room_update_member(key, last_seen_id=p["id"])
                continue
            status = await self._say(key, m["name"], self.render(p, cfg['title']), confirm=True)
            if status == "error":
                return "error", 0, 0                      # Budget/Pause/Node-Fehler zählen nicht als Fehlversuch
            if status == "unacked":
                # Keine Bestätigung: der Beitrag bleibt ungesehen und kommt nach der Wartezeit erneut. Nach MAX_TRIES erfolglosen Durchläufen
                # wird er übersprungen, damit ein dauerhaft unerreichbarer Empfänger nichts blockiert.
                self._note_unacked(key)
                k = (key, p["id"])
                self._tries[k] = self._tries.get(k, 0) + 1
                if self._tries[k] < MAX_TRIES:
                    return "unacked", 0, 1
                await self._log("warn", f"Raum: Beitrag {p['id']} an {m['name']} nach {MAX_TRIES} Versuchen übersprungen")
                self._tries.pop(k, None)
                await self.store.room_update_member(key, last_seen_id=p["id"])
                return "unacked", 1, 1
            self._note_acked(key)
            self._tries.pop((key, p["id"]), None)
            await self.store.room_update_member(key, last_seen_id=p["id"])
            return "ok", 1, 1
        return "ok", 0, 0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(DELIVER_EVERY_S)
            try:
                await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Raum-Zustellung fehlgeschlagen")
