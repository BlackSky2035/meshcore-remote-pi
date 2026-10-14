"""Repeater verwalten wie in der MeshCore-App: Anmelden (Admin-Passwort) und Textbefehle (CLI).

Alle Regeln liegen hier, nicht im Node und nicht in der Oberfläche:
- Nur Repeater, Rooms und Sensoren (Kontakttyp 2, 3, 4).
- Befehle werden geprüft (Länge, Zeichen) und eingestuft: GESPERRT (nie), RISKANT (nur mit ausdrücklicher Bestätigung),
  OHNE ANTWORT (Neustart & Co.: es wird nicht gewartet), GEHEIM (Passwörter: nie im Protokoll/Verlauf, Antwort wird geschwärzt).
- Einstellungen werden aus einem Katalog gebaut und geprüft (Wertebereiche laut MeshCore-CLI-Doku), die Oberfläche setzt nie rohe Befehle zusammen.
- Pro Repeater höchstens ein Funkpaket alle 2 s (Funkzeit ist begrenzt), ein Befehl gleichzeitig. Gilt für ALLE Funkwege
  (Anmelden, Befehle, Nachbarn, Zugriffsliste, Abmelden) über `_radio_slot`.
- Passwörter: standardmäßig nur im Arbeitsspeicher. Nur auf ausdrücklichen Wunsch in `repeater_secrets.json` (Rechte 600, neben der
  Datenbank, NICHT in der Datenbank und damit nicht in deren Sicherungen). Die Schnittstelle gibt nie ein Passwort zurück.
Es gibt im Programm keine Benutzeranmeldung: Wer die Oberfläche erreicht und ein gespeichertes Passwort freigibt, kann den Repeater verwalten.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .node.base import BaseNode, NodeError

log = logging.getLogger("meshcore-remote")

MANAGEABLE_TYPES = ("repeater", "room", "sensor")
MAX_COMMAND_BYTES = 150
MIN_GAP_S = 2.0
HISTORY = 100
NO_REPLY = ("reboot", "poweroff", "shutdown", "clkreboot")
NOT_RISKY = ("set txdelay",)                                         # beginnt mit „set tx“, ist aber nur die Sendeverzögerung
_INVISIBLE = {"Cf", "Zs", "Zl", "Zp", "Cc", "Co", "Cn"}               # unsichtbare/Format-Zeichen (Zero-Width, NBSP …)
BLOCKED = ("erase", "set prv.key", "get prv.key")                    # Werkseinstellung/Identitätsschlüssel: nie per Funk
SECRET_PREFIXES = ("password", "set guest.password", "get guest.password")   # lesen: Antwort enthält das Gast-Passwort
RISKY: tuple[tuple[str, str], ...] = (
    ("reboot", "Der Repeater startet neu und ist kurz nicht erreichbar."),
    ("clkreboot", "Der Repeater setzt die Uhr zurück und startet neu."),
    ("poweroff", "Der Repeater schaltet sich aus und ist erst nach manuellem Einschalten wieder erreichbar!"),
    ("shutdown", "Der Repeater schaltet sich aus und ist erst nach manuellem Einschalten wieder erreichbar!"),
    ("start ota", "Startet den Firmware-Update-Modus; der Repeater leitet währenddessen nichts weiter."),
    ("password", "Ändert das Admin-Passwort. Ohne das neue Passwort kommst du nicht mehr an den Repeater."),
    ("set guest.password", "Ändert das Gast-Passwort."),
    ("set radio", "Funkparameter ändern: Nach einem Neustart erreicht den Repeater nur noch, wer dieselben Werte nutzt (auch du)."),
    ("tempradio", "Ändert die Funkparameter vorübergehend. Stimmen sie nicht mit denen der Gegenseite überein, bricht die Verbindung ab."),
    ("set freq", "Frequenz ändern: Nach einem Neustart erreicht den Repeater nur noch, wer dieselbe Frequenz nutzt."),
    ("set tx", "Sendeleistung ändern. Zu viel ist in vielen Ländern nicht erlaubt, zu wenig kappt die Reichweite."),
    ("set repeat", "Schaltet das Weiterleiten von Paketen um. Aus = der Repeater leitet nichts mehr weiter."),
    ("set path.hash.mode", "Ändert die Größe der Pfad-Kennungen; Geräte mit anderen Einstellungen können Pfade falsch deuten."),
    ("setperm", "Ändert die Zugriffsrechte einer Person auf dem Repeater."),
    ("region", "Ändert die Regionen (Weiterleitungsregeln) des Repeaters."),
    ("log erase", "Löscht das gespeicherte Funkprotokoll des Repeaters."),
)

# Katalog der Einstellungen: key → (Beschriftung, Gruppe, Art, Hinweis). Art: text, int:min:max, float:min:max, onoff, choice:a|b|c, radio
SETTINGS: list[dict] = [
    {"key": "name", "label": "Name", "group": "Allgemein", "kind": "text", "max_bytes": 31,
     "hint": "Höchstens 31 Byte (mit Standort 23)."},
    {"key": "lat", "label": "Breitengrad", "group": "Allgemein", "kind": "float", "min": -90, "max": 90},
    {"key": "lon", "label": "Längengrad", "group": "Allgemein", "kind": "float", "min": -180, "max": 180},
    {"key": "owner.info", "label": "Besitzer-Info", "group": "Allgemein", "kind": "text", "max_bytes": 100,
     "hint": "Mit | wird eine neue Zeile erzeugt."},
    {"key": "radio", "label": "Funkparameter", "group": "Funk", "kind": "radio",
     "hint": "Frequenz MHz, Bandbreite kHz, Spreizfaktor, Coding Rate. Wirkt erst nach einem Neustart."},
    {"key": "tx", "label": "Sendeleistung (dBm)", "group": "Funk", "kind": "int", "min": 1, "max": 22},
    {"key": "repeat", "label": "Pakete weiterleiten", "group": "Weiterleitung", "kind": "onoff"},
    {"key": "flood.max", "label": "Flood: höchstens Hops", "group": "Weiterleitung", "kind": "int", "min": 0, "max": 64},
    {"key": "path.hash.mode", "label": "Pfad-Kennung (0–3)", "group": "Weiterleitung", "kind": "int", "min": 0, "max": 3,
     "hint": "0 = 1 Byte, 1 = 2 Byte, 2 = 3 Byte (Firmware ab 1.14)."},
    {"key": "loop.detect", "label": "Schleifenerkennung", "group": "Weiterleitung", "kind": "choice",
     "choices": ["off", "minimal", "moderate", "strict"], "hint": "Firmware ab 1.14."},
    {"key": "txdelay", "label": "Sendeverzögerung Flood", "group": "Weiterleitung", "kind": "float", "min": 0, "max": 2},
    {"key": "direct.txdelay", "label": "Sendeverzögerung direkt", "group": "Weiterleitung", "kind": "float", "min": 0, "max": 2},
    {"key": "rxdelay", "label": "Empfangsverzögerung", "group": "Weiterleitung", "kind": "float", "min": 0, "max": 20,
     "hint": "Experimentell."},
    {"key": "dutycycle", "label": "Sendeanteil (%)", "group": "Weiterleitung", "kind": "int", "min": 1, "max": 100,
     "hint": "Firmware ab 1.15."},
    {"key": "flood.advert.interval", "label": "Advert-Abstand (Flood, Std.)", "group": "Adverts", "kind": "int", "min": 3, "max": 168},
    {"key": "advert.interval", "label": "Advert-Abstand (direkt, Min.)", "group": "Adverts", "kind": "int", "min": 0, "max": 240,
     "hint": "0 = aus, sonst 60–240."},
    {"key": "guest.password", "label": "Gast-Passwort", "group": "Zugang", "kind": "secret", "max_bytes": 15,
     "hint": "Wird nicht angezeigt oder gespeichert."},
]
SETTING_BY_KEY = {s["key"]: s for s in SETTINGS}
RADIO_BW = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)

QUICK = [   # Schnellaktionen: Beschriftung, Befehl, Art ("read" harmlos, "act" ändert etwas)
    {"label": "Version", "command": "ver", "kind": "read"},
    {"label": "Hardware", "command": "board", "kind": "read"},
    {"label": "Uhrzeit lesen", "command": "clock", "kind": "read"},
    {"label": "Uhr abgleichen", "command": "clock sync", "kind": "act"},
    {"label": "Advert (Flood)", "command": "advert", "kind": "act"},
    {"label": "Advert (direkt)", "command": "advert.zerohop", "kind": "act"},
    {"label": "Nachbarn suchen", "command": "discover.neighbors", "kind": "act"},
    {"label": "Zähler zurücksetzen", "command": "clear stats", "kind": "act"},
    {"label": "Neustart", "command": "reboot", "kind": "act"},
]

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class RepeaterError(NodeError):
    """Eingabe oder Regelverstoß (kein Funkproblem)."""


class NeedsConfirm(RepeaterError):
    """Befehl ist riskant und wurde nicht ausdrücklich bestätigt."""


def _norm(cmd: str) -> str:
    return " ".join(cmd.strip().split()).lower()


def classify(cmd: str) -> dict:
    """Prüft einen Befehl. Wirft RepeaterError bei ungültig/gesperrt; sonst {command, risky, reason, secret, no_reply}."""
    raw = (cmd or "").strip()
    if not raw:
        raise RepeaterError("Befehl ist leer")
    if _CTRL.search(raw):                                             # vor dem Bereinigen prüfen: Zeilenumbrüche sind nicht erlaubt
        raise RepeaterError("Der Befehl enthält Steuerzeichen oder Zeilenumbrüche")
    cmd = " ".join(raw.split())
    if len(cmd.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise RepeaterError(f"Befehl zu lang (höchstens {MAX_COMMAND_BYTES} Byte)")
    if any(ch != " " and unicodedata.category(ch) in _INVISIBLE for ch in cmd):
        raise RepeaterError("Der Befehl enthält unsichtbare Sonderzeichen (z. B. geschütztes Leerzeichen)")
    low = _norm(cmd)
    # Reiner Wortanfang, OHNE Leerzeichen danach: die Firmware vergleicht Befehle teils nur am Anfang (memcmp), dann wirkt
    # „erase!“ wie „erase“ und „rebootnow“ wie „reboot“. Früher galt nur „erase“ oder „erase …“ – mit angehängten Zeichen
    # rutschte ein gesperrter/riskanter Befehl als harmlos durch.
    for b in BLOCKED:
        if low.startswith(b):
            raise RepeaterError(f"„{b}“ ist über Funk gesperrt (nur am Gerät per USB-Kabel möglich).")
    risky = next((why for prefix, why in RISKY
                  if low.startswith(prefix) and not any(low.startswith(ok) for ok in NOT_RISKY)), None)
    if low == "set repeat on":
        risky = None                                              # nur genau das Einschalten ist harmlos
    secret = any(low.startswith(p) for p in SECRET_PREFIXES)
    no_reply = any(low.startswith(p) for p in NO_REPLY)
    return {"command": cmd, "risky": risky, "secret": secret, "no_reply": no_reply}


def build_setting(key: str, value) -> str:
    """Baut `set <key> <wert>` aus dem Katalog und prüft den Wert. Wirft RepeaterError."""
    spec = SETTING_BY_KEY.get(key)
    if spec is None:
        raise RepeaterError(f"Unbekannte Einstellung: {key}")
    text = str(value if value is not None else "").strip()
    kind = spec["kind"]
    if kind in ("text", "secret"):
        if kind == "text" and key != "owner.info" and not text:
            raise RepeaterError(f"{spec['label']}: Wert fehlt")
        if len(text.encode("utf-8")) > spec["max_bytes"]:
            raise RepeaterError(f"{spec['label']}: zu lang (höchstens {spec['max_bytes']} Byte)")
        if _CTRL.search(text):
            raise RepeaterError(f"{spec['label']}: Steuerzeichen sind nicht erlaubt")
    elif kind in ("int", "float"):
        try:
            num = int(text) if kind == "int" else float(text.replace(",", "."))
        except ValueError:
            raise RepeaterError(f"{spec['label']}: „{text}“ ist keine Zahl")
        if not spec["min"] <= num <= spec["max"]:
            raise RepeaterError(f"{spec['label']}: erlaubt sind {spec['min']} bis {spec['max']}")
        if key == "advert.interval" and 0 < num < 60:
            raise RepeaterError("Advert-Abstand (direkt): 0 (aus) oder 60 bis 240 Minuten")
        text = str(num)
    elif kind == "onoff":
        if text.lower() not in ("on", "off"):
            raise RepeaterError(f"{spec['label']}: nur on oder off")
        text = text.lower()
    elif kind == "choice":
        if text not in spec["choices"]:
            raise RepeaterError(f"{spec['label']}: erlaubt sind {', '.join(spec['choices'])}")
    elif kind == "radio":
        parts = [p.strip() for p in text.replace(";", ",").split(",")]
        if len(parts) != 4:
            raise RepeaterError("Funkparameter: vier Werte „Frequenz,Bandbreite,SF,CR“, z. B. 869.525,250,11,5")
        try:
            f, bw, sf, cr = float(parts[0]), float(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            raise RepeaterError("Funkparameter: alle vier Werte müssen Zahlen sein")
        if not 150 <= f <= 960:
            raise RepeaterError("Funkparameter: Frequenz muss zwischen 150 und 960 MHz liegen")
        if bw not in RADIO_BW:
            raise RepeaterError(f"Funkparameter: Bandbreite muss eine von {', '.join(str(b) for b in RADIO_BW)} kHz sein")
        if not 5 <= sf <= 12 or not 5 <= cr <= 8:
            raise RepeaterError("Funkparameter: Spreizfaktor 5–12, Coding Rate 5–8")
        text = f"{f:g},{bw:g},{sf},{cr}"
    return f"set {key} {text}".rstrip()


def read_command(key: str) -> str:
    if key not in SETTING_BY_KEY or SETTING_BY_KEY[key]["kind"] == "secret":
        raise RepeaterError(f"Einstellung {key} kann nicht gelesen werden")
    return f"get {key}"


class PasswordVault:
    """Gemerkte Passwörter in einer eigenen Datei (Rechte 600). Wirft nie; ohne Pfad nur im Arbeitsspeicher."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._data: dict[str, str] = {}
        try:
            if self.path and self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception as e:
            log.warning("Passwortdatei nicht lesbar (%s) – gemerkte Passwörter sind nicht verfügbar", e)

    def has(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, password: str) -> None:
        self._data[key] = password
        self._save()

    def delete(self, key: str) -> None:
        if self._data.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("Passwort konnte nicht gespeichert werden: %s", e)
            raise RepeaterError("Das Passwort konnte nicht dauerhaft gespeichert werden (Dateirechte/Speicherplatz).") from e


Logger = Callable[[str, str], Awaitable[None]]


class RepeaterAdmin:
    def __init__(self, node: BaseNode, log_line: Logger | None = None, vault: PasswordVault | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time, store=None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.store = store
        self.node, self._log, self.vault = node, log_line, vault or PasswordVault(None)
        self._clock, self._wall, self._sleep = clock, wall, sleep
        self._sessions: dict[str, dict] = {}
        self._mem_pw: dict[str, str] = {}
        self._last_cmd: dict[str, tuple[float, str]] = {}          # key → (Zeitpunkt des letzten Funkpakets, Art)
        self._history: dict[str, deque] = {}

    # ---- Hilfen -------------------------------------------------------------
    async def _radio_slot(self, key: str, kind: str) -> None:
        """Gemeinsames Tempolimit aller Funkwege zu einem Repeater: höchstens ein Paket alle MIN_GAP_S.
        Folgt der Schritt direkt auf eine Anmeldung (auch die stille in `command`) oder ist es eine Abmeldung, wird die
        Restzeit gewartet (höchstens MIN_GAP_S), damit der Befehl des Nutzers trotzdem klappt; sonst „Zu schnell“.
        Der Zeitpunkt wird vor dem Warten reserviert, damit parallele Anfragen nicht gemeinsam losfunken."""
        last, last_kind = self._last_cmd.get(key, (-1e9, ""))
        now = self._clock()
        wait = MIN_GAP_S - (now - last)
        if wait > 0 and (wait > MIN_GAP_S or (last_kind != "login" and kind != "logout")):
            raise RepeaterError("Zu schnell: pro Repeater höchstens ein Befehl alle 2 Sekunden (Funkzeit ist knapp).")
        wait = max(0.0, wait)
        self._last_cmd[key] = (now + wait, kind)
        if wait > 0:
            await self._sleep(wait)

    async def _target(self, key: str) -> dict:
        contacts = await self.node.contacts()
        c = next((c for c in contacts if c["public_key"] == key), None)
        if c is None:
            raise RepeaterError("Unbekannter Kontakt")
        if c["type_name"] not in MANAGEABLE_TYPES:
            raise RepeaterError("Verwaltung gibt es nur für Repeater, Rooms und Sensoren")
        return c

    async def _say(self, level: str, text: str) -> None:
        if self._log:
            try:
                await self._log(level, text)
            except Exception:
                log.exception("Repeater-Protokoll")

    def _add_history(self, key: str, command: str, reply: str, ok: bool) -> None:
        self._history.setdefault(key, deque(maxlen=HISTORY)).append(
            {"ts": self._wall(), "command": command, "reply": reply, "ok": ok})

    def history(self, key: str) -> list[dict]:
        return list(self._history.get(key, []))

    def status(self, key: str) -> dict:
        s = self._sessions.get(key)
        return {"logged_in": bool(s), "admin": bool(s and s.get("admin")), "since": s["wall"] if s else None,
                "saved_password": self.vault.has(key), "session_password": key in self._mem_pw}

    # ---- Anmeldung ----------------------------------------------------------------
    async def login(self, key: str, password: str | None = None, remember: bool = False) -> dict:
        c = await self._target(key)
        pw = password if password else (self._mem_pw.get(key) or self.vault.get(key))
        if not pw:
            raise RepeaterError("Passwort fehlt")
        if len(pw.encode("utf-8")) > 15:
            raise RepeaterError("Passwörter sind bei MeshCore höchstens 15 Zeichen lang")
        await self._radio_slot(key, "login")
        try:
            res = await self.node.repeater_login(key, pw)
        except NodeError:
            self._sessions.pop(key, None)
            await self._say("warn", f"Repeater {c['name']}: Anmeldung fehlgeschlagen")
            raise
        self._sessions[key] = {"admin": bool(res.get("admin")), "wall": self._wall()}
        self._mem_pw[key] = pw
        warning = None
        if remember:
            try:
                self.vault.set(key, pw)
            except RepeaterError as e:                                  # angemeldet sind wir trotzdem
                warning = str(e)
        await self._say("info", f"Repeater {c['name']}: angemeldet ({'Admin' if res.get('admin') else 'Gast'})")
        return {**self.status(key), **({"warning": warning} if warning else {})}

    async def logout(self, key: str) -> dict:
        c = await self._target(key)
        self._sessions.pop(key, None)
        self._mem_pw.pop(key, None)
        try:
            await self._radio_slot(key, "logout")
            await self.node.repeater_logout(key)
        except NodeError as e:                                          # auch „Zu schnell“: lokal trotzdem abgemeldet
            log.info("Abmelden am Repeater nicht bestätigt: %s", e)      # lokal sind wir ohnehin abgemeldet
        await self._say("info", f"Repeater {c['name']}: abgemeldet")
        return self.status(key)

    def forget_password(self, key: str) -> dict:
        self.vault.delete(key)
        self._mem_pw.pop(key, None)
        return self.status(key)

    # ---- Befehle --------------------------------------------------------------------
    async def command(self, key: str, command: str, confirm: bool = False) -> dict:
        c = await self._target(key)
        info = classify(command)
        if info["risky"] and not confirm:
            raise NeedsConfirm(f"Bestätigung nötig: {info['risky']}")
        if key not in self._sessions:
            if self._mem_pw.get(key) or self.vault.has(key):
                await self.login(key)                                  # Sitzung weg (z. B. Neustart des Programms): still erneuern
            else:
                raise RepeaterError("Nicht angemeldet: erst am Repeater anmelden.")
        await self._radio_slot(key, "command")                         # nach (stiller) Anmeldung: Restzeit warten
        shown = "(geheim)" if info["secret"] else info["command"]
        try:
            res = await self.node.repeater_command(key, info["command"], wait=not info["no_reply"], secret=info["secret"])
        except NodeError as e:
            self._add_history(key, shown, str(e), False)
            await self._say("warn", f"Repeater {c['name']}: Befehl „{shown}“ fehlgeschlagen: {e}")
            raise
        replies = [str(r) for r in res.get("replies", [])]
        if info["secret"]:                                              # Antworten können das neue Passwort enthalten
            replies = ["(geheim – Antwort ausgeblendet)" for _ in replies]
        if info["no_reply"]:
            self._sessions.pop(key, None)                               # nach Neustart/Ausschalten ist die Sitzung weg
        ok = bool(replies) or info["no_reply"]
        text = "\n".join(replies) if replies else (
            "Gesendet (bei diesem Befehl kommt keine Antwort)." if info["no_reply"]
            else "Keine Antwort. Evtl. ist die Anmeldung abgelaufen oder der Repeater nicht erreichbar.")
        self._add_history(key, shown, text, ok)
        await self._say("info" if ok else "warn", f"Repeater {c['name']}: Befehl „{shown}“" + ("" if ok else " ohne Antwort"))
        if not replies and not info["no_reply"]:
            self._sessions.pop(key, None)                               # sicherheitshalber neu anmelden lassen
        return {"command": shown, "replies": replies, "ok": ok, "text": text, "no_reply": info["no_reply"]}

    async def setting(self, key: str, name: str, value, confirm: bool = False) -> dict:
        return await self.command(key, build_setting(name, value), confirm=confirm)

    async def read_setting(self, key: str, name: str) -> dict:
        res = await self.command(key, read_command(name))
        text = (res["replies"][0] if res["replies"] else "").strip()
        res["value"] = text[2:].strip() if text.startswith(">") else text
        return res

    async def neighbours(self, key: str) -> dict:
        await self._target(key)
        if key not in self._sessions:
            raise RepeaterError("Nicht angemeldet: erst am Repeater anmelden.")
        await self._radio_slot(key, "neighbours")
        res = await self.node.repeater_neighbours(key)
        res = await self._with_names(res, "neighbours")
        if self.store is not None:
            try:
                await self.store.add_neighbour_report(self._wall(), key, res.get("neighbours", []))
            except Exception:
                log.exception("Nachbarliste nicht gespeichert")       # nie die Abfrage selbst scheitern lassen
        return res

    async def acl(self, key: str) -> dict:
        await self._target(key)
        if key not in self._sessions:
            raise RepeaterError("Nicht angemeldet: erst am Repeater anmelden.")
        await self._radio_slot(key, "acl")
        entries = await self.node.repeater_acl(key)
        res = await self._with_names({"entries": entries}, "entries")
        for e in res["entries"]:
            e["perm_label"] = {0: "Gast", 1: "nur lesen", 2: "lesen/schreiben", 3: "Admin"}.get(e["perm"] & 3, str(e["perm"]))
        return res

    async def _with_names(self, res: dict, field: str) -> dict:
        contacts = await self.node.contacts()
        for item in res.get(field, []):
            hit = next((c for c in contacts if c["public_key"].startswith(item["prefix"])), None)
            item["name"] = hit["name"] if hit else None
        return res


def catalog() -> dict:
    return {"settings": SETTINGS, "quick": QUICK, "radio_bandwidths": list(RADIO_BW), "min_gap_s": MIN_GAP_S}
