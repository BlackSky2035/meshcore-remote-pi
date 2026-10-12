"""USB-Erkennung und Überwachung des Nodes.

- `list_ports()` liefert alle seriellen Anschlüsse, `candidates()` die wahrscheinlichen Nodes (nach Punkten).
- `UsbMonitor` merkt sich den Zustand (ok / kein Gerät / keine Antwort / keine Berechtigung), schreibt jede Änderung
  dauerhaft in die Datenbank, rechnet die Verfügbarkeit aus und meldet einen Ausfall einmal (nach `alert_after_s`)
  und die Rückkehr einmal.

Nichts hier darf das Programm beenden: Fehler in der Port-Abfrage gelten als „nicht erkennbar“, nicht als Absturz.
NICHT an echter Hardware getestet (Hotplug, Namen, VID/PID des Heltec V4).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Awaitable, Callable

from .node.base import NodeError

log = logging.getLogger("meshcore-remote")

# Gerätenamen, wie sie Linux vergibt (macOS-Namen stehen zusätzlich da, ungetestet)
NAME_HINTS = ("usbmodem", "usbserial", "ttyUSB", "ttyACM")
# Bekannte USB-Chips auf LoRa-Boards (Espressif nativ, Silicon Labs CP210x, WCH CH34x, FTDI, Nordic, Adafruit, Seeed)
KNOWN_VIDS = {0x303A: "Espressif", 0x10C4: "Silicon Labs", 0x1A86: "WCH", 0x0403: "FTDI",
              0x1915: "Nordic", 0x239A: "Adafruit", 0x2886: "Seeed"}
TEXT_HINTS = ("heltec", "meshcore", "esp32", "lora", "cp210", "ch340", "ch9102")

ALERT_AFTER_S = 60.0

OK, NO_DEVICE, NO_RESPONSE, NO_PERMISSION, UNKNOWN = "ok", "no_device", "no_response", "no_permission", "unknown"
MAINTENANCE = "maintenance"
LABELS = {
    OK: "verbunden",
    NO_DEVICE: "kein USB-Gerät erkannt",
    NO_RESPONSE: "Gerät erkannt, antwortet aber nicht",
    NO_PERMISSION: "keine Berechtigung für den Anschluss",
    UNKNOWN: "noch nicht geprüft",
    MAINTENANCE: "Wartung (Anschluss freigegeben)",
}
PERMISSION_HINT = ("Der Benutzer darf den Anschluss nicht öffnen. Unter Linux: „sudo usermod -aG dialout <benutzer>“, "
                   "danach neu anmelden bzw. den Dienst neu starten (systemd: User= prüfen).")


def _score(device: str, vid: int | None, description: str) -> int:
    score = 0
    if any(h in device for h in NAME_HINTS):
        score += 2
    if vid in KNOWN_VIDS:
        score += 2
    if any(h in (description or "").lower() for h in TEXT_HINTS):
        score += 1
    return score


def list_ports() -> list[dict]:
    """Alle seriellen Anschlüsse als Wörterbücher. Fehler → leere Liste (nie werfen)."""
    try:
        from serial.tools import list_ports as lp
        ports = lp.comports()
    except Exception as e:
        log.warning("Serielle Anschlüsse nicht lesbar: %s", e)
        return []
    out = []
    for p in ports:
        try:
            desc = " ".join(x for x in (p.manufacturer, p.product or p.description) if x and x != "n/a")
            out.append({
                "device": p.device, "description": desc, "vid": p.vid, "pid": p.pid, "serial": p.serial_number,
                "score": _score(p.device, p.vid, desc),
            })
        except Exception:
            continue
    return out


def candidates(ports: list[dict]) -> list[dict]:
    """Wahrscheinliche Nodes, beste zuerst (bei Gleichstand nach Name, damit die Wahl stabil bleibt)."""
    return sorted((p for p in ports if p.get("score", 0) > 0), key=lambda p: (-p["score"], p["device"]))


def resolve_port(configured: str, ports: list[dict]) -> str:
    """Welcher Anschluss soll genutzt werden? `configured` = Pfad oder „auto“."""
    if configured and configured != "auto":
        if not any(p["device"] == configured for p in ports) and not os.path.exists(configured):
            raise NodeError(f"Der eingestellte Anschluss {configured} ist nicht vorhanden.")
        return configured
    found = candidates(ports)
    if not found:
        raise NodeError("Kein USB-Node erkannt. Node anschließen oder serial_port in config.toml bzw. MC_SERIAL_PORT setzen.")
    if len(found) > 1:
        log.warning("Mehrere mögliche Nodes %s – nehme %s", [c["device"] for c in found], found[0]["device"])
    return found[0]["device"]


def port_access_problem(device: str) -> str | None:
    """Gibt einen Hinweis zurück, wenn der Anschluss existiert, aber nicht les-/schreibbar ist (Linux; macOS ungetestet)."""
    if not sys.platform.startswith(("linux", "darwin")):
        return None
    try:
        if os.path.exists(device) and not os.access(device, os.R_OK | os.W_OK):
            return PERMISSION_HINT
    except Exception:
        pass
    return None


# ---- Verfügbarkeit ---------------------------------------------------------
def availability(events: list[tuple[float, bool]], since: float, now: float, prior: bool | None) -> dict | None:
    """Anteil der Zeit mit Zustand „ok“ im Fenster [since, now].

    `events` = Zustandswechsel (Zeit, ok?) aufsteigend innerhalb des Fensters, `prior` = Zustand vor dem Fenster
    (None = unbekannt: dann zählt erst ab dem ersten Ereignis). Liefert None, wenn nichts beobachtet wurde.
    """
    points = list(events)
    if prior is None:
        if not points:
            return None
        t, state = points.pop(0)
    else:
        t, state = since, prior
    obs = up = 0.0
    outages = 0
    for ts, ok in points:
        d = max(0.0, ts - t)
        obs += d
        up += d if state else 0.0
        if state and not ok:
            outages += 1
        t, state = ts, ok
    d = max(0.0, now - t)
    obs += d
    up += d if state else 0.0
    if obs <= 0:
        return None
    return {"percent": round(100 * up / obs, 2), "downtime_s": round(obs - up), "outages": outages,
            "observed_s": round(obs)}


Notify = Callable[[str, str], Awaitable[None]]      # (kind: "down"|"up", text)


class UsbMonitor:
    def __init__(self, store, emit: Callable[[dict], Awaitable[None]] | None = None, notify: Notify | None = None,
                 alert_after_s: float = ALERT_AFTER_S, clock: Callable[[], float] = time.time):
        self.store, self.emit, self.notify = store, emit, notify
        self.alert_after_s, self._clock = alert_after_s, clock
        self.enabled = False
        self.state = UNKNOWN
        self.since: float | None = None
        self.port: str | None = None
        self.detail = ""
        self.hint = ""
        self.ports: list[dict] = []
        self._down_since: float | None = None
        self._alerted = False
        self.problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == OK

    def summary(self) -> dict:
        return {"enabled": self.enabled, "state": self.state, "label": LABELS.get(self.state, self.state),
                "port": self.port, "since": self.since, "alerted": self._alerted}

    async def update(self, state: str, port: str | None = None, detail: str = "", hint: str = "",
                     ports: list[dict] | None = None) -> None:
        """Zustand melden. Wirft nie; Datenbank-/Sendefehler werden protokolliert."""
        try:
            await self._update(state, port, detail, hint, ports)
        except Exception:
            log.exception("USB-Überwachung: Zustand konnte nicht verarbeitet werden")

    async def _update(self, state, port, detail, hint, ports) -> None:
        now = self._clock()
        self.enabled = True
        self.port, self.detail, self.hint = port, detail, hint
        if ports is not None:
            self.ports = ports
        changed = state != self.state
        was_ok = self.state == OK
        if changed:
            self.state, self.since = state, now
            try:
                await self.store.add_usb_event(now, state in (OK, MAINTENANCE), state, detail or LABELS.get(state, state))
            except Exception:
                log.exception("USB-Ereignis nicht gespeichert")
            if state in (OK, MAINTENANCE):
                self._down_since = None
                if state == MAINTENANCE:
                    self._alerted = False            # geplant: keine Ausfallmeldung, auch keine „wieder da“-Meldung
            elif was_ok or self._down_since is None:
                self._down_since = now
            if self.emit:
                await self.emit({"type": "usb_state", "payload": {
                    "state": state, "label": LABELS.get(state, state), "port": port, "detail": detail}})
        if state == MAINTENANCE:
            return
        if state == OK:
            if self._alerted:
                self._alerted = False
                text = f"USB-Node wieder erreichbar ({port})"
                await self._tell("up", text)
            return
        if not self._alerted and self._down_since is not None and now - self._down_since >= self.alert_after_s:
            self._alerted = True
            await self._tell("down", f"USB-Node nicht verfügbar: {LABELS.get(state, state)}"
                                     + (f" ({detail})" if detail else ""))

    async def _tell(self, kind: str, text: str) -> None:
        if self.emit:
            await self.emit({"type": "usb_alert", "payload": {"kind": kind, "text": text}})
        if self.notify:
            try:
                await self.notify(kind, text)
            except Exception:
                log.exception("USB-Meldung nicht zugestellt")

    async def stats(self) -> dict:
        now = self._clock()
        out: dict[str, Any] = {}
        for key, secs in (("24h", 86400), ("7d", 7 * 86400)):
            since = now - secs
            try:
                prior = await self.store.usb_state_before(since)
                evs = [(e["ts"], bool(e["up"])) for e in await self.store.usb_events(since, 1000)]
                out[key] = availability(evs, since, now, prior)
            except Exception:
                log.exception("USB-Statistik nicht berechenbar")
                out[key] = None
        return out
