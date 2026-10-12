"""Automatik: Repeater-Akku abfragen, Pings, Auto-Antworten.

Läuft im selben Prozess wie die Node-Anbindung (nur ein Prozess darf den Node
ansprechen). Schutzmaßnahmen für das gemeinsame Funkmedium:

- globales Sendebudget: Mindestabstand und Obergrenze pro Stunde
- Mindestintervalle für Regeln (siehe schemas.py)
- Auto-Antworten (Regel-Typ auto_reply) beginnen immer mit einer Erwähnung des
  ursprünglichen Absenders ("@[Name] …", wird in der MeshCore-App als Erwähnung
  dargestellt) und werden nie auf Nachrichten beantwortet, die selbst schon mit
  so einer Erwähnung oder dem älteren Präfix "[auto] " beginnen (kein
  Endlos-Austausch zweier Auto-Antworter). Der Raum-Bot (room.py) verwendet
  weiterhin nur "[auto] ", da seine Antworten keinen einzelnen Absender
  referenzieren.
- Abkühlzeit pro Absender und Regel
- globaler Pausenschalter (bleibt über Neustarts erhalten)
- Startverzögerung und Staffelung, damit nach einem Neustart nicht alles auf einmal sendet
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .node.base import BaseNode, NodeError
from .battery import battery_pct
from .storage import AsyncStorage

log = logging.getLogger(__name__)

AUTO_PREFIX = "[auto] "        # Raum-Bot (room.py) und Altbestand; auto_reply-Regeln hängen stattdessen "@[Name] " voran
MENTION_RE = re.compile(r"^@\[[^\]]+\]\s*")   # erkennt "@[Name] " am Anfang (Schleifenschutz für auto_reply)


def mention_tag(name: str) -> str:
    """Erwähnung "@[Name]". Eckige Klammern im Namen würden sie zerreißen (MeshCore erlaubt sie seit App 1.27 in Namen nicht mehr) und werden entfernt.
    Leer, wenn nichts übrig bleibt."""
    n = " ".join(str(name or "").replace("[", "").replace("]", "").split())
    return f"@[{n}]" if n else ""
TICK_S = 5.0
STARTUP_DELAY_S = 20.0
STAGGER_S = 10.0
CONTACT_CACHE_TTL_S = 60.0
LOW_BATTERY_HYSTERESIS_MV = 100
NO_TEMP_BACKOFF_S = 24 * 3600


class SendBudget:
    """Begrenzt automatische Sendungen: Mindestabstand und Anzahl pro Stunde."""

    def __init__(self, min_gap_s: float, max_per_hour: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.min_gap_s = min_gap_s
        self.max_per_hour = max_per_hour
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _trim(self, now: float) -> None:
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()

    async def acquire(self) -> bool:
        """True = darf senden (Zeitpunkt wird verbucht), False = Stundenlimit erreicht."""
        async with self._lock:
            now = self._clock()
            self._trim(now)
            if len(self._sent) >= self.max_per_hour:
                return False
            if self._sent:
                wait = self.min_gap_s - (now - self._sent[-1])
                if wait > 0:
                    await self._sleep(wait)
            self._sent.append(self._clock())
            return True

    def stats(self) -> dict:
        self._trim(self._clock())
        return {"used_last_hour": len(self._sent), "max_per_hour": self.max_per_hour,
                "min_gap_s": self.min_gap_s}


class Automation:
    def __init__(self, node: BaseNode, store: AsyncStorage,
                 emit: Callable[[dict], Awaitable[None]], budget: SendBudget,
                 retention_days: int = 90,
                 clock: Callable[[], float] = time.monotonic):
        self.node = node
        self.store = store
        self.emit = emit
        self.budget = budget
        self.retention_days = retention_days
        self._clock = clock
        self.paused = False
        self._next_run: dict[int, float] = {}
        self._reply_last: dict[tuple[int, str], float] = {}
        self._reply_times: dict[int, deque] = {}      # je Regel: Zeitpunkte der Antworten (rollierend 60 min)
        self._cap_logged: set[int] = set()
        self._no_temp: dict[str, float] = {}           # Repeater ohne Temperatursensor: bis wann nicht fragen
        self._alerted: set[str] = set()
        self._contacts_cache: tuple[float, list[dict]] | None = None
        self._last_prune = 0.0
        self._task: asyncio.Task | None = None

    # ---- Lebenszyklus ----------------------------------------------------
    async def start(self) -> None:
        self.paused = bool(await self.store.get_setting("automation_paused", False))
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self.store.set_setting("automation_paused", paused)
        await self.log("warn" if paused else "info",
                       "Automatik pausiert" if paused else "Automatik läuft wieder")

    def reschedule(self, rule_id: int) -> None:
        """Nach Änderung einer Regel: Zeitplan neu beginnen."""
        self._next_run.pop(rule_id, None)

    async def log(self, level: str, text: str, rule_id: int | None = None) -> None:
        await self.store.add_log(level, text, rule_id)
        await self.emit({"type": "automation",
                         "payload": {"level": level, "text": text, "rule_id": rule_id}})

    # ---- Kontakte (mit kleinem Cache) ------------------------------------
    async def _contacts(self, force: bool = False) -> list[dict]:
        now = self._clock()
        if (not force and self._contacts_cache
                and now - self._contacts_cache[0] < CONTACT_CACHE_TTL_S):
            return self._contacts_cache[1]
        contacts = await self.node.contacts()
        self._contacts_cache = (now, contacts)
        return contacts

    def invalidate_contacts(self) -> None:
        self._contacts_cache = None

    async def contact_by_prefix(self, prefix: str) -> dict | None:
        if not prefix:
            return None
        for force in (False, True):
            try:
                for c in await self._contacts(force=force):
                    if c["public_key"].startswith(prefix.lower()):
                        return c
            except NodeError:
                return None
        return None

    # ---- Eingehende Nachrichten: Auto-Antwort ----------------------------
    @staticmethod
    def _hit(cfg: dict, text: str) -> bool:
        """„enthält“ matcht nur als ganzes Wort (Wortgrenzen) – „ping“ soll nicht in „pinguin“ feuern."""
        haystack, needle = text.lower(), cfg["match"].strip().lower()
        if cfg["mode"] == "equals":
            return haystack == needle
        return bool(re.search(rf"\b{re.escape(needle)}\b", haystack))

    def _reserve(self, rule: dict, sender_key: str) -> str | None:
        """Prüft Abkühlzeit je Absender und Stundenobergrenze der Regel und verbucht beides sofort.

        Muss ohne `await` laufen: Kommen viele Nachrichten gleichzeitig an, darf jede Task nur
        einen Platz bekommen, sonst würde die Obergrenze unterlaufen. Rückgabe: None = darf
        antworten, sonst der Grund (`cooldown` | `cap`).
        """
        cfg, rid, now = rule["config"], rule["id"], self._clock()
        last = self._reply_last.get((rid, sender_key))
        if last is not None and now - last < cfg["cooldown_s"]:
            return "cooldown"
        window = self._reply_times.setdefault(rid, deque())
        while window and now - window[0] >= 3600:
            window.popleft()
        if len(window) >= cfg.get("max_per_hour", 10):
            return "cap"
        self._reply_last[(rid, sender_key)] = now
        window.append(now)
        self._cap_logged.discard(rid)
        return None

    def _release(self, rule: dict, sender_key: str, undo_cooldown: bool = True) -> None:
        """Verbuchten Platz zurückgeben (Senden war nicht möglich)."""
        window = self._reply_times.get(rule["id"])
        if window:
            window.pop()
        if undo_cooldown:
            self._reply_last.pop((rule["id"], sender_key), None)

    @staticmethod
    def path_text(path: str | None, size: int = 1, hops: int | None = None) -> str:
        """Weg der empfangenen Nachricht als Text für {path}: „a1,b2,c3“ (Kurzkennungen der Repeater in der
        Reihenfolge, wie das Paket sie durchlief), „direkt“ ohne Zwischenstation, sonst Hop-Zahl oder „?“."""
        if hops == 0 or (path == "" and hops in (None, 0)):
            return "direkt"
        if path:
            step = max(1, size) * 2
            return ",".join(path[i:i + step] for i in range(0, len(path), step))
        if hops is not None and hops > 0:
            return f"{hops} Hop{'s' if hops != 1 else ''}"
        return "?"

    @classmethod
    def _fill(cls, template: str, name: str, hops: int | None, path: str = "?") -> str:
        """Platzhalter ersetzen. {name} wird als Erwähnung "@[Name]" eingesetzt (nicht als einfacher Text)."""
        return (template.replace("{name}", mention_tag(name) or str(name)).replace("{time}", time.strftime("%H:%M"))
                .replace("{hops}", str(hops) if hops is not None and hops >= 0 else "?")
                .replace("{path}", path))

    @classmethod
    def _reply_text(cls, template: str, name: str, hops: int | None, path: str = "?") -> str:
        """Antworttext mit Erwähnung vorn. Beginnt der Text nach dem Füllen schon mit einer Erwähnung (z. B. "{name} pong"), kommt keine zweite davor.
        Die Erwähnung am Anfang ist zugleich der Schleifenschutz (siehe MENTION_RE) und darf nie fehlen."""
        text = cls._fill(template, name, hops, path)
        if not MENTION_RE.match(text):
            text = f"{mention_tag(name) or '@[?]'} {text}"
        return text[:200]

    async def handle_incoming(self, payload: dict, contact: dict | None) -> None:
        """Prüft Auto-Antwort-Regeln für Direktnachrichten. Wird NICHT aus dem Node-Callback
        awaited, sondern als eigene Task gestartet (siehe Hub), um Blockaden zu vermeiden."""
        if self.paused or not self.node.connected:
            return
        if payload.get("txt_type", 0) != 0:   # nur normaler Text, keine CLI-Daten
            return
        text = (payload.get("text") or "").strip()
        if not text or text.startswith(AUTO_PREFIX.strip()) or MENTION_RE.match(text):
            return                             # Schleifenschutz: [auto]-Marker oder vorangestellte @[…]-Erwähnung
        if contact is None:
            return                             # ohne bekannten Kontakt keine Antwort
        sender = contact["public_key"]

        for rule in await self.store.rules():
            if not rule["enabled"] or rule["kind"] != "auto_reply":
                continue
            cfg = rule["config"]
            if not cfg.get("direct", True) or not self._hit(cfg, text):
                continue
            if cfg.get("only_from") and sender not in cfg["only_from"]:
                continue
            why = self._reserve(rule, sender)
            if why == "cooldown":
                return                         # Abkühlzeit läuft, still ignorieren
            if why == "cap":
                await self._log_cap(rule)
                return
            if not await self.budget.acquire():
                self._release(rule, sender)
                await self.log("warn", f"Auto-Antwort an {contact['name']} übersprungen: "
                                       f"Stundenlimit erreicht", rule["id"])
                return
            plen = payload.get("path_len")
            hops = plen if isinstance(plen, int) and 0 <= plen <= 63 else None
            if hops is None and isinstance(contact.get("path_len"), int) and contact["path_len"] >= 0:
                hops = contact["path_len"]               # Ersatz: bekannter Weg zu diesem Kontakt
            where = self.path_text(payload.get("path"), 1, hops)
            reply = self._reply_text(cfg["reply"], contact["name"], hops, where)
            try:
                await self.node.send_message(sender, reply)
                await self.store.add_message("out", "direct", sender, reply,
                                             name=contact["name"], auto=True)
                await self.log("info", f"Auto-Antwort an {contact['name']}: {reply}", rule["id"])
            except NodeError as e:
                self._release(rule, sender)
                await self.log("error", f"Auto-Antwort an {contact['name']} fehlgeschlagen: {e}",
                               rule["id"])
            return                             # pro Nachricht höchstens eine Antwort

    async def handle_channel_incoming(self, payload: dict, sender: str | None, text: str) -> None:
        """Auto-Antwort auf Kanalnachrichten (nur Regeln, die diesen Kanal gewählt haben).

        Absender sind hier nur Namen („Name: Text“), keine Schlüssel; die Abkühlzeit gilt je Name.
        Die Antwort geht in den Kanal und ist damit für alle sichtbar: darum die Stundenobergrenze."""
        if self.paused or not self.node.connected:
            return
        if payload.get("txt_type", 0) != 0:
            return
        text = (text or "").strip()
        if not text or text.startswith(AUTO_PREFIX.strip()) or MENTION_RE.match(text):
            return                             # Schleifenschutz, siehe handle_incoming
        idx = payload.get("channel_idx")
        who = (sender or "?").strip()
        plen = payload.get("path_len")
        hops = plen if isinstance(plen, int) and 0 <= plen <= 63 else None
        for rule in await self.store.rules():
            if not rule["enabled"] or rule["kind"] != "auto_reply":
                continue
            cfg = rule["config"]
            if idx not in cfg.get("channels", []) or not self._hit(cfg, text):
                continue
            key = f"ch{idx}:{who.lower()}"
            why = self._reserve(rule, key)
            if why == "cooldown":
                return
            if why == "cap":
                await self._log_cap(rule)
                return
            if not await self.budget.acquire():
                self._release(rule, key)
                await self.log("warn", f"Kanal-Antwort an {who} übersprungen: Stundenlimit erreicht", rule["id"])
                return
            mode = payload.get("path_hash_mode")
            where = self.path_text(payload.get("path"), mode + 1 if isinstance(mode, int) and mode >= 0 else 1, hops)
            reply = self._reply_text(cfg["reply"], who, hops, where)
            try:
                res = await self.node.send_channel_message(idx, reply)
                await self.store.add_message("out", "channel", str(idx), reply, auto=True,
                                             sender_ts=res.get("timestamp"))
                await self.log("info", f"Kanal {idx}: Antwort auf {who}: {reply}", rule["id"])
            except NodeError as e:
                self._release(rule, key)
                await self.log("error", f"Kanal-Antwort an {who} fehlgeschlagen: {e}", rule["id"])
            return

    async def _log_cap(self, rule: dict) -> None:
        """Einmal je Fenster melden, dass die Obergrenze erreicht ist (nicht bei jeder Nachricht)."""
        if rule["id"] in self._cap_logged:
            return
        self._cap_logged.add(rule["id"])
        await self.log("warn", f"Regel „{rule['name']}“: Höchstzahl von {rule['config'].get('max_per_hour', 10)} "
                               f"Antworten pro Stunde erreicht, weitere Nachrichten werden ignoriert", rule["id"])

    # ---- Messungen (auch für manuelle Aktionen genutzt) ------------------
    async def poll_node(self, public_key: str, low_mv: int | None = None, temperature: bool = False) -> dict:
        """Fragt den Status eines Repeaters/Rooms ab und speichert Akku und Erreichbarkeit,
        auf Wunsch zusätzlich die Temperatur (zweite Funkanfrage per Telemetrie)."""
        t0 = time.monotonic()
        try:
            status = await self.node.repeater_status(public_key)
        except NodeError:
            await self.store.add_ping(public_key, False, None, "status")
            raise
        rtt = round((time.monotonic() - t0) * 1000, 1)
        mv = status.get("bat")
        if isinstance(mv, int) and mv > 0:
            await self.store.add_battery(public_key, mv)
        await self.store.add_ping(public_key, True, rtt, "status")
        if low_mv is not None and isinstance(mv, int) and mv > 0:
            await self._check_battery(public_key, mv, low_mv)
        status = dict(status)
        if temperature:
            status["temperature"] = await self._poll_temperature(public_key)
        await self.emit({"type": "node_update", "payload": {"public_key": public_key, "mv": mv}})
        return status

    async def _poll_temperature(self, public_key: str) -> float | None:
        """Temperatur holen und speichern. Meldet ein Repeater keine, wird 24 h lang nicht mehr gefragt
        (spart Funkzeit); bei einem Timeout wird beim nächsten Durchlauf normal weiter versucht."""
        now = self._clock()
        if self._no_temp.get(public_key, 0) > now:
            return None
        try:
            tel = await self.node.repeater_telemetry(public_key)
        except NodeError as e:
            await self.log("warn", f"Temperatur {public_key[:8]}: {e}")
            return None
        try:
            await self.store.add_sensor_readings(public_key, tel.get("lpp") or [])
        except Exception:
            log.exception("Sensorwerte nicht gespeichert")
        t = tel.get("temperature")
        if isinstance(t, (int, float)):
            await self.store.add_temperature(public_key, float(t))
            return float(t)
        self._no_temp[public_key] = now + NO_TEMP_BACKOFF_S
        await self.log("info", f"{public_key[:8]} meldet keine Temperatur – erst in 24 h wieder fragen")
        return None

    async def ping_node(self, public_key: str, mode: str = "status", text: str = "ping") -> dict:
        # Ping-Text wird bewusst OHNE „[auto]“ gesendet: Bots im Netz reagieren nur auf das nackte
        # Wort. Schleifen gibt es trotzdem nicht: unsere Antworten tragen das Präfix und werden ignoriert.
        if mode == "message":
            target = next((c for c in await self._contacts() if c["public_key"] == public_key), None)
            if target is not None and target.get("type") != 1:
                raise NodeError("Nachrichten gehen nur an Clients. Repeater und Rooms nehmen keine einfachen "
                                "Textnachrichten an (keine Bestätigung): Direkt-Ping oder Statusabfrage nutzen.")
        res = await self.node.ping(public_key, mode, text)
        await self.store.add_ping(public_key, res["ok"], res.get("rtt_ms"), mode,
                                  snr_out=res.get("snr_out"), snr_in=res.get("snr_in"))
        if mode == "message":
            await self.store.add_message("out", "direct", public_key, text, auto=True)
        await self.emit({"type": "node_update", "payload": {"public_key": public_key}})
        return res

    async def _check_battery(self, public_key: str, mv: int, low_mv: int) -> None:
        if mv < low_mv and public_key not in self._alerted:
            self._alerted.add(public_key)
            name = next((c["name"] for c in (self._contacts_cache or (0, []))[1]
                         if c["public_key"] == public_key), public_key[:8])
            await self.log("warn", f"Akku niedrig: {name} bei {battery_pct(mv)} % ({mv} mV), Grenze {battery_pct(low_mv)} % ({low_mv} mV)")
            await self.emit({"type": "battery_low",
                             "payload": {"public_key": public_key, "name": name, "mv": mv}})
        elif mv >= low_mv + LOW_BATTERY_HYSTERESIS_MV:
            self._alerted.discard(public_key)

    # ---- Regeln mit Zeitplan --------------------------------------------
    async def _repeater_keys(self, targets) -> list[str]:
        if targets == "all_repeaters":
            return [c["public_key"] for c in await self._contacts() if c["type"] == 2]
        return list(targets)

    async def run_rule(self, rule: dict) -> None:
        cfg, rid = rule["config"], rule["id"]
        if rule["kind"] == "battery_poll":
            for key in await self._repeater_keys(cfg["targets"]):
                if not await self.budget.acquire():
                    await self.log("warn", "Akku-Abfrage abgebrochen: Stundenlimit erreicht", rid)
                    return
                want_temp = bool(cfg.get("temperature", False)) and await self.budget.acquire()
                try:
                    await self.poll_node(key, low_mv=cfg["low_mv"], temperature=want_temp)
                except NodeError as e:
                    await self.log("warn", f"Akku-Abfrage {key[:8]} fehlgeschlagen: {e}", rid)
        elif rule["kind"] == "advert":
            if not await self.budget.acquire():
                await self.log("warn", "Advert abgebrochen: Stundenlimit erreicht", rid)
                return
            try:
                await self.node.send_advert(bool(cfg.get("flood")))
                await self.log("info", "Advert gesendet" + (" (netzweit)" if cfg.get("flood") else " (direkte Nachbarn)"), rid)
            except NodeError as e:
                await self.log("warn", f"Advert fehlgeschlagen: {e}", rid)
        elif rule["kind"] == "channel_ping":
            if not await self.budget.acquire():
                await self.log("warn", "Kanal-Ping abgebrochen: Stundenlimit erreicht", rid)
                return
            try:
                await self.ping_channel(cfg["channel"], cfg["text"])
            except NodeError as e:
                await self.log("warn", f"Kanal-Ping Kanal {cfg['channel']} fehlgeschlagen: {e}", rid)
        elif rule["kind"] == "ping":
            for key in cfg["targets"]:
                if not await self.budget.acquire():
                    await self.log("warn", "Ping abgebrochen: Stundenlimit erreicht", rid)
                    return
                try:
                    res = await self.ping_node(key, cfg["mode"], cfg["text"])
                    if not res["ok"]:
                        await self.log("warn", f"Ping {key[:8]}: keine Antwort", rid)
                except NodeError as e:
                    await self.log("warn", f"Ping {key[:8]} fehlgeschlagen: {e}", rid)

    async def ping_channel(self, channel: int, text: str = "ping") -> dict:
        """Postet `text` unverändert in den Kanal (andere Bots antworten darauf)."""
        res = await self.node.send_channel_message(channel, text)
        await self.store.add_message("out", "channel", str(channel), text, auto=True,
                                     sender_ts=res.get("timestamp"))
        return res

    async def tick(self) -> None:
        """Ein Durchlauf des Zeitplans (auch direkt aus Tests aufrufbar)."""
        now = self._clock()
        if self.paused or not self.node.connected:
            return
        rules = [r for r in await self.store.rules()
                 if r["enabled"] and r["kind"] in ("battery_poll", "ping", "channel_ping", "advert")]
        live_ids = {r["id"] for r in rules}
        for stale in set(self._next_run) - live_ids:
            del self._next_run[stale]
        for index, rule in enumerate(rules):
            due = self._next_run.get(rule["id"])
            if due is None:                    # erste Planung: verzögert und gestaffelt
                self._next_run[rule["id"]] = now + STARTUP_DELAY_S + index * STAGGER_S
                continue
            if now >= due:
                # Nächsten Termin VOR der Ausführung setzen: Fehler führen nie zu Dauerschleifen.
                self._next_run[rule["id"]] = now + rule["config"]["interval_min"] * 60
                try:
                    await self.run_rule(rule)
                except Exception as e:         # Automatik darf nie den Server beenden
                    log.exception("Regel %s fehlgeschlagen", rule["id"])
                    await self.log("error", f"Regel '{rule['name']}' Fehler: {e}", rule["id"])
        if now - self._last_prune > 86400:
            self._last_prune = now
            removed = await self.store.prune(self.retention_days)
            if removed:
                log.info("%d alte Einträge gelöscht", removed)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Fehler im Automatik-Zeitplan")
            await asyncio.sleep(TICK_S)

    def status(self) -> dict:
        return {"paused": self.paused, "budget": self.budget.stats()}
