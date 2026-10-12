"""Supervisor: hält die Verbindung zum Node am Leben und überwacht den USB-Anschluss.

Pro Durchlauf (alle `cycle_s` Sekunden):
1. Anschlüsse lesen (Zeitlimit; Fehler = „nichts erkannt“).
2. Verschwand der genutzte Anschluss (Kabel gezogen), wird die Verbindung sofort getrennt – nicht erst nach einem Zeitlimit.
3. Alle `HEARTBEAT_S` Sekunden fragt er den Node etwas; zweimal keine Antwort in Folge → trennen und neu verbinden.
4. Ist der Node nicht verbunden, aber ein Gerät da, verbindet er nach kurzer Wartezeit (das Gerät braucht nach dem
   Einstecken einen Moment) mit wachsendem Abstand bei Fehlern (3 s … 30 s).
5. Zustand an den `UsbMonitor` melden (Verlauf, Verfügbarkeit, Meldung bei Ausfall).

Der Durchlauf fängt jeden Fehler ab; die Schleife läuft immer weiter.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from . import usb
from .node import NodeError
from .runtime import Runtime

log = logging.getLogger("meshcore-remote")

CYCLE_S = 2.0
HEARTBEAT_S = 30.0
SETTLE_S = 2.0                # nach dem Einstecken kurz warten, bis das Gerät bereit ist
BACKOFF_S = (3.0, 6.0, 12.0, 24.0, 30.0)
STEP_TIMEOUT_S = 25.0


class Supervisor:
    def __init__(self, rt: Runtime, ports: Callable[[], list[dict]] | None = None,
                 clock: Callable[[], float] = time.monotonic, cycle_s: float = CYCLE_S,
                 heartbeat_s: float = HEARTBEAT_S, settle_s: float = SETTLE_S):
        self.rt, self.node = rt, rt.node
        self._ports = ports
        self._clock = clock
        self.cycle_s, self.heartbeat_s, self.settle_s = cycle_s, heartbeat_s, settle_s
        self._fails = 0
        self._next_try = 0.0
        self._seen_since: float | None = None      # seit wann ist ein Gerät sichtbar?
        self._last_beat = clock()
        self._beat_fails = 0
        self._last_detail = ""

    # ---- Hilfen ------------------------------------------------------------
    async def _read_ports(self) -> list[dict]:
        sim = self.node.usb_ports()
        if sim is not None:
            return sim
        try:
            fn = self._ports or usb.list_ports
            return await asyncio.wait_for(asyncio.to_thread(fn), 5)
        except Exception as e:
            log.warning("Anschlüsse nicht lesbar: %s", e)
            return []

    async def _drop(self, why: str, announce: bool = True) -> None:
        """Verbindung kappen (hängt nie länger als ein paar Sekunden)."""
        try:
            await asyncio.wait_for(self.node.disconnect(), 8)
        except Exception as e:
            log.debug("Trennen (%s): %s", why, e)
        if announce:
            try:
                await self.rt.hub.emit({"type": "disconnected", "payload": {"reason": why}})
            except Exception:
                pass

    def _problem(self) -> str | None:
        return self.rt.state.get("last_error")

    async def _verify_channels(self) -> None:
        """Nach dem Verbinden: eine einzelne Kanal-Abfrage kann bei echter Hardware vereinzelt leer statt mit
        Fehler antworten - dann fehlt ein Kanal in der Liste, obwohl er auf dem Node existiert. Vergleicht mit
        der zuletzt für GENAU diesen Node (eindeutige Kennung) gespeicherten Liste; fehlt etwas gegenüber
        vorher, wird einmal zusätzlich neu gelesen, bevor die Liste als Stand der Dinge übernommen wird."""
        try:
            pub = self.node.public_key
            if not pub:
                return
            idxs = sorted(c["idx"] for c in await self.node.channels())
            known = await self.rt.store.get_setting("known_channels", None)
            if isinstance(known, dict) and known.get("public_key") == pub:
                missing = sorted(set(known.get("idxs") or []) - set(idxs))
                if missing:
                    log.warning("Nach dem Verbinden fehlen Kanäle %s gegenüber dem letzten Mal – erneut gelesen", missing)
                    self.node.invalidate_channels()
                    idxs = sorted(c["idx"] for c in await self.node.channels())
            await self.rt.store.set_setting("known_channels", {"public_key": pub, "idxs": idxs})
        except Exception:
            log.exception("Kanal-Überprüfung nach dem Verbinden fehlgeschlagen")

    # ---- Ein Durchlauf -----------------------------------------------------
    async def step(self) -> None:
        rt, node, mon = self.rt, self.node, self.rt.usb
        now = self._clock()
        uses_usb = bool(getattr(node, "uses_usb", False))
        maint = getattr(rt, "maintenance", None)
        if maint is not None and await maint.tick():
            # Wartung: Anschluss freigeben, nichts neu verbinden, keine Ausfallmeldung
            try:
                if node.connected:
                    await self._drop("maintenance")
            except Exception:
                pass
            self._fails, self._next_try, self._seen_since = 0, 0.0, None
            self._last_beat, self._beat_fails = now, 0
            if mon is not None and uses_usb:
                await mon.update(usb.MAINTENANCE, None, "", "", [])
            return
        ports = await self._read_ports() if uses_usb else []

        try:
            connected = bool(node.connected)
        except Exception:
            connected = False

        # 2. Kabel gezogen oder Port weg?
        if connected and uses_usb and node.port and not any(p["device"] == node.port for p in ports):
            log.warning("USB-Anschluss %s ist verschwunden", node.port)
            await self._drop("usb_unplugged")
            connected = False

        # 3. Herzschlag
        if connected and now - self._last_beat >= self.heartbeat_s:
            self._last_beat = now
            try:
                beat = await asyncio.wait_for(node.alive(), 15)
            except Exception:
                beat = False
            self._beat_fails = 0 if beat else self._beat_fails + 1
            if self._beat_fails >= 2:
                log.warning("Node antwortet nicht – Verbindung wird neu aufgebaut")
                rt.state["last_error"] = "Der Node antwortet nicht mehr; Verbindung wird neu aufgebaut."
                await self._drop("no_response")
                connected = False
                self._beat_fails = 0
                self._next_try = now           # sofort neu versuchen

        # Gerät sichtbar?
        present = True
        target = None
        hint = ""
        if uses_usb:
            try:
                target = usb.resolve_port(rt.settings.serial_port, ports)
            except NodeError as e:
                present, self._last_detail = False, str(e)
            if present and target:
                hint = usb.port_access_problem(target) or ""
        if present:
            self._seen_since = self._seen_since if self._seen_since is not None else now
        else:
            self._seen_since = None
            self._fails, self._next_try = 0, 0.0      # nächstes Einstecken: sofort wieder frisch versuchen

        # 4. Verbinden
        if not connected and present and now >= self._next_try and (
                not uses_usb or now - (self._seen_since or now) >= self.settle_s):
            try:
                await asyncio.wait_for(node.connect(), STEP_TIMEOUT_S)
                connected = bool(node.connected)
                rt.state["last_error"] = None
                self._fails, self._last_beat, self._beat_fails = 0, now, 0
                log.info("Node verbunden (%s)", node.kind)
                await self._verify_channels()
            except Exception as e:
                self._fails += 1
                self._next_try = now + BACKOFF_S[min(self._fails - 1, len(BACKOFF_S) - 1)]
                msg = str(e) if isinstance(e, NodeError) else f"{type(e).__name__}: {e}"
                if isinstance(e, asyncio.TimeoutError):
                    msg = "Zeitüberschreitung beim Verbinden"
                rt.state["last_error"] = msg
                self._last_detail = msg
                log.warning("Node-Verbindung fehlgeschlagen (Versuch %d): %s", self._fails, msg)
                await self._drop("connect_failed", announce=False)

        # 5. Zustand melden
        if mon is not None and uses_usb:
            if connected:
                await mon.update(usb.OK, node.port or target, "", "", ports)
            elif not present:
                await mon.update(usb.NO_DEVICE, None, self._last_detail, "", ports)
            elif hint:
                await mon.update(usb.NO_PERMISSION, target, hint, hint, ports)
            else:
                await mon.update(usb.NO_RESPONSE, target, rt.state.get("last_error") or "", "", ports)
        elif mon is not None:
            mon.enabled = False

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.step(), STEP_TIMEOUT_S + 30)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Supervisor-Durchlauf fehlgeschlagen")
            await asyncio.sleep(self.cycle_s)
