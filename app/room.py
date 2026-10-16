"""Raum-Bot: ein Software-„Room“ ohne Extra-Firmware, nur durch die Festplatte begrenzt (statt 32 Beiträge wie die Room-Firmware).

So funktioniert es: Bekannte Kontakte (Clients) schreiben dem eigenen Node Direktnachrichten mit Befehlen:
  /p <Text>   Beitrag schreiben        /neu       neue Beiträge holen (älteste zuerst, höchstens 3, danach „noch N neue“)
  /verlauf [n] die letzten n (1–5)     /anmelden  neue Beiträge automatisch zugestellt bekommen    /abmelden   /hilfe
Alle Beiträge liegen unbegrenzt in der Datenbank (max_posts, Standard 5000) und sind auf der Webseite lesbar.
Grenzen (bewusst): Jede Antwort ist eine Funknachricht und zählt zum Sendebudget der Automatik (Stundenlimit, Mindestabstand); je Beitrag höchstens
100 Zeichen; unbekannte Absender werden ignoriert; je Mitglied höchstens 20 Beiträge pro Stunde und 5 s Abstand zwischen zwei Befehlen, auch Beiträgen (ein
früherer Befehl wird gemerkt, nur der letzte, und danach ausgeführt). Bei pausierter Automatik schweigt auch der Raum. Antworten tragen das Präfix „[auto] “,
damit sich zwei solche Bots (z. B. auf beiden Pis) nie gegenseitig antworten. In der MeshCore-App erscheint der Node als normaler Kontakt, nicht als Room.
Zustellung wie ein Postfach: Ein Beitrag gilt erst mit Empfangsbestätigung als gesehen und wird nie übersprungen. Wer 3 Durchläufe lang nicht bestätigt, gilt als
offline (kein Funk mehr), bis sich sein Node meldet (Nachricht, Advert, neuer Pfad); dann werden höchstens die letzten 32 Beiträge nachgeliefert.
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
COOLDOWN_S = 5.0            # Pause zwischen zwei Beiträgen (/p) je Mitglied (gleich dem Befehlsabstand: ein zu früher /p wird gemerkt und danach gespeichert)
CMD_GAP_S = 5.0             # Mindestabstand zwischen zwei Befehlen je Mitglied; ein früherer Befehl wird gemerkt (nur der letzte) und danach ausgeführt
POSTS_PER_HOUR = 20
DELIVER_EVERY_S = 20.0
DELIVER_PER_CYCLE = 3
MAX_TRIES = 3                # so viele unbestätigte Durchläufe hintereinander, dann gilt das Mitglied als offline (Zustellung ruht bis zur nächsten Meldung)
BACKLOG_MAX = 32             # höchstens so viele ungesehene Beiträge je Mitglied werden nachgeliefert (wie die Room-Firmware); Älteres zeigt /verlauf
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
        self._last_cmd: dict[str, float] = {}           # je Mitglied: letzter ausgeführter Befehl (Abstand CMD_GAP_S für ALLE Befehle)
        self._pending: dict[str, tuple[dict, dict]] = {}    # je Mitglied: zu früh gekommener Befehl (nur der letzte), wird nach CMD_GAP_S ausgeführt
        self._timers: dict[str, asyncio.Task] = {}
        self._sleep = asyncio.sleep                       # in Tests ersetzbar
        self._backoff_s: dict[str, float] = {}          # je Mitglied: aktuelle Wartezeit nach unbestätigter Zustellung
        self._backoff_until: dict[str, float] = {}
        self._hourly: dict[str, deque] = defaultdict(deque)
        self._fails: dict[str, int] = {}               # je Mitglied: unbestätigte Durchläufe hintereinander
        self._offline: set[str] = set()                 # Mitglieder, die als offline gelten: keine automatische Zustellung bis zur nächsten Meldung
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
        self._fails.pop(key, None)
        self._offline.discard(key)

    def is_offline(self, key: str) -> bool:
        return key in self._offline

    async def mark_alive(self, key_or_prefix: str) -> None:
        """Der Node eines Mitglieds hat sich gemeldet (Direktnachricht, Advert, neuer Pfad): wieder erreichbar, Wartezeit weg, die automatische
        Zustellung liefert Fehlendes nach (wie das Anmelden an einem Room-Server). Wirft nie."""
        k = (key_or_prefix or "").lower()
        if len(k) < 6:
            return
        try:
            members = await self.store.room_members()
        except Exception:
            return
        for m in members:
            key = m["public_key"]
            if not key.lower().startswith(k):
                continue
            was = key in self._offline
            self._offline.discard(key)
            self._fails.pop(key, None)
            self._backoff_s.pop(key, None)
            self._backoff_until.pop(key, None)
            if was:
                await self._log("info", f"Raum: {m['name']} wieder erreichbar, fehlende Beiträge werden nachgeliefert")
                await self._members_changed()

    async def _trim_backlog(self, m: dict) -> None:
        """Mehr als BACKLOG_MAX ungesehene Beiträge: nur die neuesten BACKLOG_MAX nachliefern (wie die Room-Firmware), Älteres zählt als gesehen."""
        newest = await self.store.room_posts(m["last_seen_id"], BACKLOG_MAX + 1, newest=True)
        if len(newest) > BACKLOG_MAX:
            await self.store.room_update_member(m["public_key"], last_seen_id=newest[0]["id"])
            m["last_seen_id"] = max(m["last_seen_id"], newest[0]["id"])
            await self._log("info", f"Raum: für {m['name']} nur die letzten {BACKLOG_MAX} Beiträge nachgeliefert, Älteres mit /verlauf")

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
    async def handle_incoming(self, payload: dict, contact: dict | None, _from_timer: bool = False) -> None:
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
        wait = self._last_cmd.get(key, float("-inf")) + CMD_GAP_S - now
        if wait > 0 or (key in self._timers and not _from_timer):
            # Zu früh: merken statt verwerfen (nur den letzten, ältere gemerkte fallen weg) und nach Ablauf des Abstands ausführen.
            # So kann niemand etwas aufstauen: höchstens ein Befehl je CMD_GAP_S und Mitglied, eine Funknachricht als Hinweis gibt es nicht.
            self._pending[key] = (payload, contact)
            if key not in self._timers:
                self._timers[key] = asyncio.create_task(self._run_pending(key, max(wait, 0.0)))
            log.info("Raum: Befehl von %s gemerkt, wird in %.0f s ausgeführt", name, wait)
            return
        self._last_cmd[key] = now
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        try:
            await self._handle_cmd(key, name, cmd, arg, cfg, now)
        finally:
            await self._members_changed()

    async def _run_pending(self, key: str, wait: float) -> None:
        """Gemerkte Befehle dieses Mitglieds nacheinander ausführen, jeweils nach dem Mindestabstand. Ein Zeitgeber je Mitglied;
        er trägt sich erst am Ende aus, damit Befehle, die währenddessen kommen, nur gemerkt (und von ihm erledigt) werden. Wirft nie."""
        try:
            while True:
                await self._sleep(max(0.0, wait))
                item = self._pending.pop(key, None)
                if item is None:
                    return
                wait = self._last_cmd.get(key, float("-inf")) + CMD_GAP_S - self._clock()
                if wait > 0:                                  # inzwischen lief ein anderer Befehl: weiter warten
                    self._pending.setdefault(key, item)
                    continue
                try:
                    await self.handle_incoming(*item, _from_timer=True)
                except Exception:
                    log.exception("Raum: gemerkter Befehl fehlgeschlagen")
                wait = self._last_cmd.get(key, float("-inf")) + CMD_GAP_S - self._clock()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Raum: Zeitgeber für gemerkte Befehle fehlgeschlagen")
        finally:
            if self._timers.get(key) is asyncio.current_task():
                self._timers.pop(key, None)

    async def drain_pending(self) -> None:
        """Nur für Tests: auf gemerkte Befehle warten."""
        while self._timers:
            await asyncio.gather(*list(self._timers.values()), return_exceptions=True)
            await asyncio.sleep(0)

    async def _members_changed(self) -> None:
        """Webseite: Mitgliederliste neu laden (angemeldet/abgemeldet/offline). Wirft nie."""
        try:
            await self.emit({"type": "room_members", "payload": {}})
        except Exception:
            log.exception("Raum: Meldung an die Webseite fehlgeschlagen")

    async def _handle_cmd(self, key: str, name: str, cmd: str, arg: str, cfg: dict, now: float) -> None:
        member = await self.store.room_member(key, name)
        if cmd in ("hilfe", "help", "?"):
            await self._say(key, name, f"{cfg['title']}: {HELP}")
        elif cmd == "p":
            await self._post(key, name, arg, cfg)
        elif cmd in ("neu", "new"):
            async with self._lock(key):                      # sonst holt die automatische Zustellung denselben Beitrag parallel (doppelt)
                member = await self.store.room_member(key, name)
                await self._trim_backlog(member)
                posts = await self.store.room_posts(member["last_seen_id"], BACKLOG_MAX + 1)
                foreign = [p for p in posts if p["author_key"] != key]       # eigene Beiträge werden nie zurückgeschickt
                if not foreign:
                    if posts:
                        await self.store.room_update_member(key, last_seen_id=posts[-1]["id"])
                    await self._say(key, name, f"{cfg['title']}: keine neuen Beiträge")
                    return
                sent = 0
                for p in posts:                                  # immer der älteste zuerst
                    if sent >= PER_REQUEST:
                        break
                    if p["author_key"] == key:
                        await self.store.room_update_member(key, last_seen_id=p["id"])
                        continue
                    if await self._say(key, name, self.render(p, cfg['title']), confirm=True) != "ok":
                        return                               # nicht bestätigt: bleibt ungelesen, /neu holt es erneut
                    self._note_acked(key)
                    await self.store.room_update_member(key, last_seen_id=p["id"])
                    sent += 1
                left = len(foreign) - sent
                if left > 0:                                 # Rest immer mit Zahl: man weiß, wie viel noch fehlt
                    word = "neuer Beitrag" if left == 1 else "neue Beiträge"
                    await self._say(key, name, f"{cfg['title']}: noch {left} {word} (/neu)")
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
                # Wer schon mitliest (Markierung > 0), behält seine ungelesenen Beiträge: die automatische Zustellung liefert sie nach.
                # Ganz neue Mitglieder starten beim neuesten Beitrag (sonst kämen bis zu BACKLOG_MAX alte Beiträge auf einmal per Funk).
                fresh = member["last_seen_id"] == 0
                if fresh:
                    await self.store.room_update_member(key, last_seen_id=await self.store.room_max_id(), subscribed=True)
                else:
                    await self.store.room_update_member(key, subscribed=True)
                pending = 0 if fresh else len([p for p in await self.store.room_posts(member["last_seen_id"], BACKLOG_MAX)
                                               if p["author_key"] != key])
                extra = f", {pending} ungelesene folgen" if pending else ""
                await self._say(key, name, f"{cfg['title']}: angemeldet, neue Beiträge kommen automatisch{extra} (/abmelden)")
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
                raise RoomError(f"Bitte kurz warten ({COOLDOWN_S:.0f} s Pause zwischen Beiträgen).")
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
        # Der eigene Beitrag setzt die Markierung NICHT weiter: sonst gälten ältere, noch nicht zugestellte fremde Beiträge als gesehen.
        # Eigene Beiträge werden beim Zustellen und bei /neu übersprungen.
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
            if m0["public_key"] in self._offline:
                continue                                  # gilt als offline: erst wieder nach einer Meldung des Nodes (mark_alive)
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
        await self._trim_backlog(m)
        for p in await self.store.room_posts(m["last_seen_id"], 5):
            if p["author_key"] == key:
                await self.store.room_update_member(key, last_seen_id=p["id"])
                continue
            status = await self._say(key, m["name"], self.render(p, cfg['title']), confirm=True)
            if status == "error":
                return "error", 0, 0                      # Budget/Pause/Node-Fehler zählen nicht als Fehlversuch
            if status == "unacked":
                # Keine Bestätigung: der Beitrag bleibt ungesehen (wird NIE übersprungen) und kommt nach der Wartezeit erneut. Nach MAX_TRIES
                # erfolglosen Durchläufen gilt das Mitglied als offline: keine Funkversuche mehr, bis sich sein Node meldet (mark_alive) oder /neu kommt.
                self._note_unacked(key)
                self._fails[key] = self._fails.get(key, 0) + 1
                if self._fails[key] >= MAX_TRIES:
                    self._offline.add(key)
                    await self._log("info", f"Raum: {m['name']} gilt als offline (keine Empfangsbestätigung), Zustellung ruht bis zur nächsten Meldung")
                    await self._members_changed()
                return "unacked", 0, 1
            self._note_acked(key)
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
