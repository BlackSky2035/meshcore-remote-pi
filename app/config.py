"""Konfiguration: optionale config.toml, Umgebungsvariablen (MC_*) haben Vorrang.

Beispiel config.toml (siehe config.example.toml):

    [server]
    host = "127.0.0.1"
    port = 8000

    [node]
    mode = "serial"          # sim | serial | tcp | ble
    serial_port = "auto"     # oder z.B. /dev/ttyUSB0
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("meshcore-remote")

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


@dataclass
class Settings:
    # server
    host: str = "127.0.0.1"
    port: int = 8000
    access_password: str = ""    # leer = keine Anmeldung; sonst gemeinsames Passwort für die Oberfläche
    station_name: str = ""       # Anzeigename dieser Station (leer = Name des Nodes)
    control_token: str = ""      # Geheimwort für den Watchdog, um den Wartungsmodus zu schalten (leer = nur über die Oberfläche)
    peer_token: str = ""         # Geheimwort, mit dem andere Stationen den Kurzstatus abfragen dürfen (leer = aus)
    peers: str = ""              # andere Stationen: "Name|https://adresse|token; Name2|…" (token = deren peer_token)
    watchdog_port: int = 8100   # Port des separaten Watchdogs (0 = Link ausblenden)
    allowed_hosts: str = ""     # weitere Rechnernamen, unter denen die Seite aufgerufen wird (IPs, localhost, *.ts.net, eigener Name/.local gehen immer)
    # node
    mode: str = "sim"  # sim | serial | tcp | ble
    serial_port: str = "auto"
    baudrate: int = 115200
    tcp_host: str = ""
    tcp_port: int = 5000
    ble_address: str = ""
    ble_pin: str = ""
    debug: bool = False
    # speicher
    db_path: str = str(ROOT / "data" / "meshcore.db")
    retention_days: int = 90
    # karte
    tile_url: str = DEFAULT_TILE_URL
    # automatik: Schutz vor zu viel Funkverkehr
    auto_min_gap_s: float = 15.0      # Mindestabstand zwischen automatischen Sendungen
    auto_max_per_hour: int = 30       # globale Obergrenze automatischer Sendungen pro Stunde
    manual_min_gap_s: float = 2.0     # Funkaktionen von Hand (Nachricht, Advert, Ping, Abfragen): Mindestabstand …
    manual_max_per_min: int = 20      # … und höchstens so viele Funkpakete je Minute (0 s/viel nur für Tests)
    battery_poll_gap_s: float = 20.0  # Pause zwischen zwei Repeatern bei „Jetzt abfragen“ auf der Akku-Seite (schont die Stromversorgung des Pi)
    # Hinweise beim Laden (Wert korrigiert, Datei fehlerhaft): erscheinen in der Übersicht unter „problems“; keine Einstellung
    config_notes: list = field(default_factory=list)


# Sicherheitsnetz gegen Tippfehler in config.toml/MC_*: Werte außerhalb werden auf die Grenze gesetzt (mit Hinweis).
# Die Standardwerte oben bleiben unverändert. Die Funk-Grenzen gelten nicht im Simulator (kein Funk; Tests brauchen 0 s).
LIMITS = {                            # Feld: (kleinster, größter Wert, gilt auch im Simulator)
    "auto_min_gap_s": (5.0, None, False),
    "auto_max_per_hour": (1, 60, False),
    "manual_min_gap_s": (1.0, None, False),
    "manual_max_per_min": (1, 30, False),
    "retention_days": (7, None, True),
}


_SECTIONS = {
    "server": ("host", "port", "watchdog_port", "access_password", "station_name", "peer_token", "peers", "control_token",
               "allowed_hosts"),
    "node": (
        "mode", "serial_port", "baudrate", "tcp_host", "tcp_port",
        "ble_address", "ble_pin", "debug",
    ),
    "storage": ("db_path", "retention_days"),
    "map": ("tile_url",),
    "automation": ("auto_min_gap_s", "auto_max_per_hour", "battery_poll_gap_s", "manual_min_gap_s", "manual_max_per_min"),
}


def _cast(value, like):
    if isinstance(like, bool):
        return str(value).lower() in ("1", "true", "yes")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return str(value)


_PASSWORD_LINE = re.compile(r"^\s*access_password\s*=\s*['\"]?[^'\"\s#]", re.M)


def _set(s: Settings, key: str, value, where: str) -> None:
    """Wert übernehmen; passt der Typ nicht (z. B. Text statt Zahl), bleibt der Standardwert und es gibt einen Hinweis."""
    try:
        setattr(s, key, _cast(value, getattr(s, key)))
    except (TypeError, ValueError):
        s.config_notes.append(f"{where}: {key} = {value!r} ist ungültig, verwendet wird {getattr(s, key)!r}.")


def apply_limits(s: Settings) -> None:
    """Werte außerhalb von LIMITS auf die Grenze setzen und einen Hinweis hinterlegen."""
    for key, (lo, hi, in_sim) in LIMITS.items():
        if s.mode == "sim" and not in_sim:
            continue
        v = getattr(s, key)
        fixed = max(lo, v) if lo is not None else v
        fixed = min(hi, fixed) if hi is not None else fixed
        if fixed != v:
            setattr(s, key, type(v)(fixed))
            s.config_notes.append(f"Einstellung {key} = {v} ist {'zu klein' if v < fixed else 'zu groß'}, "
                                  f"verwendet wird {type(v)(fixed)} (Schutz vor Tippfehlern).")


def load_settings() -> Settings:
    s = Settings()
    cfg_path = Path(os.environ.get("MC_CONFIG", ROOT / "config.toml"))
    if cfg_path.is_file():
        raw = cfg_path.read_bytes()
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            # Kaputte Datei: lieber mit Standardwerten starten (Oberfläche erreichbar, Warnung) als gar nicht. Ausnahme: steht dort
            # ein Passwort (und keins über MC_ACCESS_PASSWORD), liefe die App plötzlich ohne Anmeldung → dann bewusst NICHT starten.
            text = raw.decode("utf-8", errors="replace")
            if _PASSWORD_LINE.search(text) and not os.environ.get("MC_ACCESS_PASSWORD"):
                raise SystemExit(f"{cfg_path} ist fehlerhaft ({e}). Weil dort ein Zugangspasswort steht, startet das Programm "
                                 f"nicht ohne Anmeldung. Bitte die Datei korrigieren.")
            data = {}
            s.mode = "serial"                  # eine config.toml gibt es nur im Betrieb: echter Node statt Simulator (Standard „sim“ ist für die Entwicklung)
            s.config_notes.append(f"{cfg_path.name} ist fehlerhaft und wurde nicht gelesen ({e}). Es gelten die Standardwerte "
                                  f"(Node per USB automatisch, Port 8000 nur auf diesem Rechner, Grenzen …) – bitte die Datei korrigieren und neu starten.")
        for section, keys in _SECTIONS.items():
            block = data.get(section, {})
            if not isinstance(block, dict):
                continue
            for key in keys:
                if key in block:
                    _set(s, key, block[key], cfg_path.name)

    # Umgebungsvariablen: MC_<NAME> mit gleichem Namen wie das Feld
    for key in Settings.__dataclass_fields__:
        if key == "config_notes":
            continue
        env_value = os.environ.get(f"MC_{key.upper()}")
        if env_value is not None:
            _set(s, key, env_value, f"MC_{key.upper()}")

    s.mode = s.mode.lower()
    apply_limits(s)
    for note in s.config_notes:
        log.warning("Konfiguration: %s", note)
    return s
