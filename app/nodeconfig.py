"""Einstellungen des eigenen Nodes (wie im Einstellungsbereich der MeshCore-App).

Alle Prüfungen liegen hier. Die Oberfläche bekommt aus `spec()` die Felder und Wertebereiche, der Server prüft jede Änderung
selbst. Riskante Änderungen (Funkparameter, Pfad-Kennung, Neustart) verlangen eine ausdrückliche Bestätigung.
Bewusst NICHT enthalten: Privatschlüssel exportieren/importieren und Werkseinstellung (zu gefährlich für ein Programm ohne Benutzeranmeldung).
Die Bedeutung der Werte von Telemetrie und Standort-Weitergabe (0/1/2) beruht auf Kenntnis der Firmware und ist nicht gegen
eine Quelle geprüft; die Zahlen werden deshalb immer mit angezeigt. Nicht an echter Hardware getestet.
"""
from __future__ import annotations

import re
import time
from typing import Awaitable, Callable

from .node.base import BaseNode, NodeError

RADIO_BW = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)
NAME_MAX_BYTES = 31
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

POLICY = {0: "nicht senden", 1: "immer senden", 2: "nach Einstellung des Nodes"}
TELEMETRY = {0: "aus", 1: "nur erlaubte Kontakte", 2: "alle"}

FIELDS: list[dict] = [
    {"key": "name", "label": "Name", "group": "Allgemein", "kind": "text", "hint": f"Höchstens {NAME_MAX_BYTES} Byte."},
    {"key": "lat", "label": "Breitengrad", "group": "Standort", "kind": "float", "min": -90, "max": 90},
    {"key": "lon", "label": "Längengrad", "group": "Standort", "kind": "float", "min": -180, "max": 180,
     "hint": "0 und 0 bedeutet „kein Standort“."},
    {"key": "radio_freq", "label": "Frequenz (MHz)", "group": "Funk", "kind": "float", "min": 150, "max": 960},
    {"key": "radio_bw", "label": "Bandbreite (kHz)", "group": "Funk", "kind": "choice", "choices": list(RADIO_BW)},
    {"key": "radio_sf", "label": "Spreizfaktor", "group": "Funk", "kind": "int", "min": 5, "max": 12},
    {"key": "radio_cr", "label": "Coding Rate", "group": "Funk", "kind": "int", "min": 5, "max": 8},
    {"key": "tx_power", "label": "Sendeleistung (dBm)", "group": "Funk", "kind": "int", "min": 1, "max": 22,
     "hint": "Zulässige Leistung hängt von Land und Band ab."},
    {"key": "adv_loc_policy", "label": "Standort in Adverts", "group": "Datenschutz", "kind": "choice", "choices": [0, 1, 2],
     "labels": POLICY},
    {"key": "telemetry_mode_base", "label": "Telemetrie: Basis (Akku)", "group": "Datenschutz", "kind": "choice",
     "choices": [0, 1, 2], "labels": TELEMETRY},
    {"key": "telemetry_mode_loc", "label": "Telemetrie: Standort", "group": "Datenschutz", "kind": "choice",
     "choices": [0, 1, 2], "labels": TELEMETRY},
    {"key": "telemetry_mode_env", "label": "Telemetrie: Umwelt", "group": "Datenschutz", "kind": "choice",
     "choices": [0, 1, 2], "labels": TELEMETRY},
    {"key": "manual_add_contacts", "label": "Kontakte nur manuell hinzufügen", "group": "Kontakte", "kind": "bool",
     "hint": "Aus: neue Adverts werden automatisch zu Kontakten."},
    {"key": "multi_acks", "label": "Mehrfach-Bestätigungen", "group": "Kontakte", "kind": "choice", "choices": [0, 1],
     "labels": {0: "aus", 1: "an"}},
    {"key": "path_hash_mode", "label": "Pfad-Kennung (0–3)", "group": "Erweitert", "kind": "int", "min": 0, "max": 3,
     "hint": "Firmware ab 1.14. 0 = 1 Byte, 1 = 2 Byte, 2 = 3 Byte."},
]
BY_KEY = {f["key"]: f for f in FIELDS}
RADIO_KEYS = ("radio_freq", "radio_bw", "radio_sf", "radio_cr")


class ConfigError(NodeError):
    """Ungültige Eingabe."""


class NeedsConfirm(ConfigError):
    """Riskante Änderung ohne Bestätigung."""


def _num(spec: dict, value, integer: bool):
    try:
        n = int(str(value).strip()) if integer else float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ConfigError(f"{spec['label']}: „{value}“ ist keine {'ganze ' if integer else ''}Zahl")
    if not spec["min"] <= n <= spec["max"]:
        raise ConfigError(f"{spec['label']}: erlaubt sind {spec['min']} bis {spec['max']}")
    return n


def validate(changes: dict, max_tx: int | None = None) -> tuple[dict, list[str]]:
    """Prüft Änderungen. Liefert (bereinigte Werte, Liste der Risiko-Gründe). Wirft ConfigError."""
    if not changes:
        raise ConfigError("Keine Änderung angegeben")
    out: dict = {}
    for key, raw in changes.items():
        spec = BY_KEY.get(key)
        if spec is None:
            raise ConfigError(f"Unbekannte Einstellung: {key}")
        kind = spec["kind"]
        if kind == "text":
            text = str(raw if raw is not None else "").strip()
            if not text:
                raise ConfigError(f"{spec['label']}: darf nicht leer sein")
            if len(text.encode("utf-8")) > NAME_MAX_BYTES:
                raise ConfigError(f"{spec['label']}: zu lang (höchstens {NAME_MAX_BYTES} Byte)")
            if _CTRL.search(text):
                raise ConfigError(f"{spec['label']}: Steuerzeichen sind nicht erlaubt")
            out[key] = text
        elif kind == "float":
            out[key] = _num(spec, raw, False)
        elif kind == "int":
            out[key] = _num(spec, raw, True)
        elif kind == "bool":
            if isinstance(raw, str):
                raw = raw.strip().lower() in ("1", "true", "ja", "on")
            out[key] = bool(raw)
        elif kind == "choice":
            val = float(raw) if isinstance(spec["choices"][0], float) else _num({**spec, "min": min(spec["choices"]), "max": max(spec["choices"])}, raw, True)
            if val not in spec["choices"]:
                raise ConfigError(f"{spec['label']}: erlaubt sind {', '.join(str(c) for c in spec['choices'])}")
            out[key] = val
    if ("lat" in out) != ("lon" in out):
        raise ConfigError("Breiten- und Längengrad müssen zusammen geändert werden")
    if max_tx is not None and out.get("tx_power", 0) > max_tx:
        raise ConfigError(f"Sendeleistung: dieser Node erlaubt höchstens {max_tx} dBm")
    risks: list[str] = []
    if any(k in out for k in RADIO_KEYS):
        risks.append("Funkparameter ändern: Der Node hört danach nur noch Geräte mit denselben Werten (Frequenz, Bandbreite, "
                     "Spreizfaktor, Coding Rate) – auch Repeater und Freunde. Falsche Werte bedeuten kein Empfang mehr.")
    if "path_hash_mode" in out:
        risks.append("Pfad-Kennung ändern: Geräte mit anderer Einstellung können Funkwege falsch deuten.")
    if out.get("adv_loc_policy") == 1:
        risks.append("Dein Standort wird dann in jedem Advert öffentlich mitgesendet.")
    return out, risks


def spec() -> dict:
    return {"fields": FIELDS, "radio_bandwidths": list(RADIO_BW)}


class NodeConfig:
    def __init__(self, node: BaseNode, log_line: Callable[[str, str], Awaitable[None]] | None = None,
                 wall: Callable[[], float] = time.time):
        self.node, self._log, self._wall = node, log_line, wall

    async def _say(self, text: str) -> None:
        if self._log:
            try:
                await self._log("info", text)
            except Exception:
                pass

    async def get(self) -> dict:
        cur = await self.node.node_settings()
        now = self._wall()
        nt = cur.get("node_time")
        return {"settings": cur, "pi_time": int(now), "time_diff_s": (int(nt) - int(now)) if nt is not None else None}

    async def apply(self, changes: dict, confirm: bool = False) -> dict:
        cur = await self.node.node_settings()
        clean, risks = validate(changes, cur.get("max_tx_power"))
        clean = {k: v for k, v in clean.items() if _differs(cur.get(k), v)} or {}
        if not clean:
            raise ConfigError("Nichts zu ändern: die Werte sind schon so eingestellt.")
        risks = [r for r in risks if r]
        if risks and not confirm:
            raise NeedsConfirm("Bestätigung nötig: " + " ".join(risks))
        done = await self.node.node_apply(clean)
        await self._say("Eigener Node: geändert " + ", ".join(done))
        return await self.get()

    async def sync_time(self) -> dict:
        await self.node.node_set_time(int(self._wall()))
        await self._say("Eigener Node: Uhr mit dem Rechner abgeglichen")
        return await self.get()

    async def reboot(self, confirm: bool = False) -> dict:
        if not confirm:
            raise NeedsConfirm("Bestätigung nötig: Der Node startet neu und ist kurz nicht erreichbar; die Verbindung wird danach "
                               "automatisch wieder aufgebaut.")
        await self.node.node_reboot()
        await self._say("Eigener Node: Neustart ausgelöst")
        return {"ok": True}


def _differs(old, new) -> bool:
    if old is None:
        return True
    if isinstance(new, float) or isinstance(old, float):
        try:
            return abs(float(old) - float(new)) > 1e-6
        except (TypeError, ValueError):
            return True
    return old != new
