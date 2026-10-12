"""Gemeinsame Schnittstelle für echten Node und Simulator."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

Emit = Callable[[dict], Awaitable[None]]

# Kontakttypen laut MeshCore (meshcore.packets.AdvType)
TYPE_NAMES = {0: "unknown", 1: "client", 2: "repeater", 3: "room", 4: "sensor"}


class NodeError(Exception):
    """Fehler bei der Kommunikation mit dem Node."""


def jsonable(obj: Any) -> Any:
    """Wandelt bytes & Co. rekursiv in JSON-taugliche Werte um."""
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def normalize_contact(c: dict) -> dict:
    """Einheitliches Kontaktformat für API und Oberfläche.

    Felder: public_key, name, type (int), type_name, lat, lon (None wenn unbekannt),
    last_advert (Unix-Zeit oder None), path_len (-1 = Flood, None = unbekannt).
    """
    ctype = c.get("type", 0)
    path: list[str] = []
    plen, raw_path = c.get("path_len"), str(c.get("out_path") or "")
    if isinstance(plen, int) and plen > 0 and raw_path:
        hexs = raw_path[:plen * 2]                      # Annahme: 1 Byte je Zwischenstation (Pfad-Kennung 0)
        path = [hexs[i:i + 2] for i in range(0, len(hexs) - len(hexs) % 2, 2)]
    lat, lon = c.get("lat"), c.get("lon")
    if lat in (None, 0, 0.0) and lon in (None, 0, 0.0):
        lat = lon = None
    return {
        "public_key": c["public_key"],
        "name": c.get("name") or c["public_key"][:8],
        "type": ctype,
        "type_name": TYPE_NAMES.get(ctype, "unknown"),
        "lat": lat,
        "lon": lon,
        "last_advert": c.get("last_advert") or None,
        "path_len": c.get("path_len"),
        "path": path,
    }


CHANNEL_NAME_MAX_BYTES = 32


def derive_secret(name: str) -> bytes:
    """Schlüssel eines Hashtag-Kanals (#name): die ersten 16 Byte von SHA-256 des Namens."""
    import hashlib
    return hashlib.sha256(name.encode("utf-8")).digest()[:16]


def check_channel(name: str, secret: bytes | None) -> tuple[str, bytes]:
    """Prüft Name und Schlüssel und liefert den endgültigen Schlüssel.

    Name `#…` (Hashtag-Kanal): der Schlüssel ergibt sich aus dem Namen, ein mitgegebener
    wird ignoriert. Sonst muss ein 16-Byte-Schlüssel vorhanden sein.
    """
    name = (name or "").strip()
    if not name:
        raise NodeError("Kanalname fehlt")
    if len(name.encode("utf-8")) > CHANNEL_NAME_MAX_BYTES:
        raise NodeError(f"Kanalname zu lang (höchstens {CHANNEL_NAME_MAX_BYTES} Byte)")
    if name.startswith("#"):
        if len(name) < 2:
            raise NodeError("Hashtag-Kanal braucht einen Namen nach dem #")
        return name, derive_secret(name)
    if secret is None or len(secret) != 16:
        raise NodeError("Der Kanalschlüssel muss 16 Byte (32 Hex-Zeichen) lang sein")
    if not any(secret):
        raise NodeError("Der Kanalschlüssel darf nicht nur aus Nullen bestehen")
    return name, secret


def normalize_channel(idx: int, name: str, secret: bytes) -> dict:
    """Format für die interne Weitergabe. `secret` (Hex) wird von der API nicht in Listen ausgegeben."""
    return {"idx": idx, "name": name, "secret": secret.hex(),
            "hashtag": name.startswith("#") and secret == derive_secret(name)}


class BaseNode(ABC):
    """Jeder Node-Typ liefert Events über `emit` und bietet dieselben Aktionen."""

    kind = "base"
    uses_usb = False          # True, wenn der Node per USB-Kabel hängt (dann überwacht der Supervisor den Anschluss)
    port: str | None = None   # aktuell genutzter Anschluss
    public_key: str | None = None   # Kennung des eigenen Nodes, nach `connect()` gesetzt (identifiziert ihn eindeutig)

    def __init__(self, emit: Emit):
        self.emit = emit

    async def alive(self) -> bool:
        """Antwortet der Node wirklich? (Herzschlag-Prüfung). Standard: so gut wie „verbunden“."""
        return self.connected

    def usb_ports(self) -> list[dict] | None:
        """Nur der Simulator liefert hier eine eigene Liste; sonst None = echte Anschlüsse abfragen."""
        return None

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def status(self) -> dict: ...

    # ---- Kontakte --------------------------------------------------------
    @abstractmethod
    async def contacts(self) -> list[dict]:
        """Liste im Format von `normalize_contact`."""

    @abstractmethod
    async def import_contact(self, card: str) -> None:
        """Kontakt aus einer Kontaktkarte (Hex oder `meshcore://…`) hinzufügen."""

    @abstractmethod
    async def remove_contact(self, public_key: str) -> None: ...

    @abstractmethod
    async def reset_path(self, public_key: str) -> None: ...

    @abstractmethod
    async def export_contact(self, public_key: str | None = None) -> str:
        """Kontaktkarte als `meshcore://…`-URI; ohne Key die des eigenen Nodes."""

    # ---- Kanäle ----------------------------------------------------------
    @abstractmethod
    async def max_channels(self) -> int:
        """Anzahl der Kanalplätze (0 … n-1) des Nodes."""

    @abstractmethod
    async def channels(self) -> list[dict]:
        """Belegte Kanäle im Format von `normalize_channel`, nach Platz sortiert."""

    def invalidate_channels(self) -> None:
        """Zwingt den nächsten `channels()`-Aufruf zu einer echten Neuabfrage (z. B. nach einer
        unvollständig wirkenden Lesung). Nur für Node-Typen mit eigenem Zwischenspeicher nötig."""

    @abstractmethod
    async def set_channel(self, idx: int, name: str, secret: bytes | None) -> dict:
        """Kanal auf den Node schreiben (neu oder überschreiben). Gibt den Kanal zurück."""

    @abstractmethod
    async def remove_channel(self, idx: int) -> None: ...

    # ---- Nachrichten und Funk -------------------------------------------
    @abstractmethod
    async def send_message(self, public_key: str, text: str, confirm: bool = False) -> dict:
        """`confirm=True`: auf Empfangsbestätigung warten (mit Wiederholungen); Ergebnis enthält dann `acked`."""

    @abstractmethod
    async def send_channel_message(self, channel: int, text: str) -> dict:
        """Ergebnis enthält `timestamp` (Unix-Sekunden im Funkpaket); damit lassen sich gehörte
        Wiederholungen (Event `channel_rx`) der eigenen Nachricht zuordnen."""

    @abstractmethod
    async def send_advert(self, flood: bool = False) -> dict: ...

    # ---- Überwachung -----------------------------------------------------
    @abstractmethod
    async def repeater_status(self, public_key: str) -> dict:
        """Status eines entfernten Repeaters/Rooms. Enthält mindestens `bat` (mV).
        Wirft NodeError bei Timeout."""

    @abstractmethod
    async def repeater_telemetry(self, public_key: str) -> dict:
        """Telemetrie (Sensorwerte) eines Repeaters. Ergebnis: {"temperature": float|None, "lpp": [...]}.
        `temperature` ist None, wenn das Gerät keinen Temperaturwert meldet. Wirft NodeError bei Timeout."""

    async def node_stats(self) -> dict:
        """Zähler des eigenen Nodes (lokal per Kabel, kostet keine Sendezeit): {"core": {battery_mv, uptime_secs, errors, queue_len},
        "radio": {noise_floor, last_rssi, last_snr, tx_air_secs, rx_air_secs}, "packets": {recv, sent, flood_tx, direct_tx, flood_rx, direct_rx, recv_errors}}."""
        raise NodeError("Statistik wird von diesem Node nicht unterstützt")

    # ---- Einstellungen des eigenen Nodes ---------------------------------------------------------
    async def node_settings(self) -> dict:
        """Aktuelle Einstellungen: name, public_key, lat, lon, radio_freq/bw/sf/cr, tx_power, max_tx_power, adv_loc_policy,
        telemetry_mode_base/loc/env, manual_add_contacts, multi_acks, path_hash_mode (oder None), node_time (Unix-Zeit oder None)."""
        raise NodeError("Node-Einstellungen werden von diesem Node-Typ nicht unterstützt")

    async def node_apply(self, changes: dict) -> list[str]:
        """Geprüfte Änderungen schreiben (Schlüssel wie bei node_settings). Gibt die geänderten Schlüssel zurück."""
        raise NodeError("Node-Einstellungen werden von diesem Node-Typ nicht unterstützt")

    async def node_set_time(self, epoch: int) -> None:
        raise NodeError("Node-Einstellungen werden von diesem Node-Typ nicht unterstützt")

    async def node_reboot(self) -> None:
        raise NodeError("Node-Einstellungen werden von diesem Node-Typ nicht unterstützt")

    # ---- Repeater verwalten (Anmeldung + Textbefehle, wie in der MeshCore-App) ----------------
    async def repeater_login(self, public_key: str, password: str) -> dict:
        """Am Repeater/Room anmelden. Ergebnis {"ok": True, "admin": bool}. Wirft NodeError, wenn keine Bestätigung kommt
        (falsches Passwort oder keine Funkverbindung sind von außen nicht unterscheidbar)."""
        raise NodeError("Repeater-Verwaltung wird von diesem Node-Typ nicht unterstützt")

    async def repeater_logout(self, public_key: str) -> None:
        raise NodeError("Repeater-Verwaltung wird von diesem Node-Typ nicht unterstützt")

    async def repeater_command(self, public_key: str, command: str, wait: bool = True) -> dict:
        """Textbefehl (CLI) an den angemeldeten Repeater. Ergebnis {"replies": [str, ...]}; leer, wenn nichts kam."""
        raise NodeError("Repeater-Verwaltung wird von diesem Node-Typ nicht unterstützt")

    async def repeater_neighbours(self, public_key: str) -> dict:
        """Nachbarn laut Repeater: {"total": int, "neighbours": [{"prefix", "secs_ago", "snr"}]}."""
        raise NodeError("Repeater-Verwaltung wird von diesem Node-Typ nicht unterstützt")

    async def repeater_acl(self, public_key: str) -> list[dict]:
        """Zugriffsliste des Repeaters: [{"prefix": 12 Hex, "perm": int}]."""
        raise NodeError("Repeater-Verwaltung wird von diesem Node-Typ nicht unterstützt")

    @abstractmethod
    async def ping(self, public_key: str, mode: str = "status", text: str = "ping") -> dict:
        """Erreichbarkeitstest. mode `status` (unsichtbar, für Repeater/Rooms) oder
        `message` (sendet Text, wartet auf ACK) oder `trace` (Zero-Hop: Trace-Paket nur über dieses
        Gerät, der Repeater muss direkt erreichbar sein; liefert die Signalstärke SNR in dB).
        Ergebnis: {"ok": bool, "rtt_ms": float|None, optional "snr_out", "snr_in"}."""
