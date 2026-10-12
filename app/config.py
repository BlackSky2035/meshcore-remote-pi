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

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
    battery_poll_gap_s: float = 20.0  # Pause zwischen zwei Repeatern bei „Jetzt abfragen“ auf der Akku-Seite (schont die Stromversorgung des Pi)


_SECTIONS = {
    "server": ("host", "port", "watchdog_port", "access_password", "station_name", "peer_token", "peers", "control_token"),
    "node": (
        "mode", "serial_port", "baudrate", "tcp_host", "tcp_port",
        "ble_address", "ble_pin", "debug",
    ),
    "storage": ("db_path", "retention_days"),
    "map": ("tile_url",),
    "automation": ("auto_min_gap_s", "auto_max_per_hour", "battery_poll_gap_s"),
}


def _cast(value, like):
    if isinstance(like, bool):
        return str(value).lower() in ("1", "true", "yes")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return str(value)


def load_settings() -> Settings:
    s = Settings()
    cfg_path = Path(os.environ.get("MC_CONFIG", ROOT / "config.toml"))
    if cfg_path.is_file():
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)
        for section, keys in _SECTIONS.items():
            block = data.get(section, {})
            for key in keys:
                if key in block:
                    setattr(s, key, _cast(block[key], getattr(s, key)))

    # Umgebungsvariablen: MC_<NAME> mit gleichem Namen wie das Feld
    for key in Settings.__dataclass_fields__:
        env_value = os.environ.get(f"MC_{key.upper()}")
        if env_value is not None:
            setattr(s, key, _cast(env_value, getattr(s, key)))

    s.mode = s.mode.lower()
    return s
