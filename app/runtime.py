"""Baut alle Teile zusammen (Hub, Node, Speicher, Automatik)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import WebSocket

from .automation import Automation, SendBudget
from .config import Settings
from .node import BaseNode, create_node
from .push import PushService
from .nodeconfig import NodeConfig
from .maintenance import Maintenance
from .peers import PeerWatch, parse_peers
from .room import Room
from .stats import StatsSampler
from .repeater import PasswordVault, RepeaterAdmin
from .usb import UsbMonitor
from .storage import AsyncStorage, Storage

log = logging.getLogger("meshcore-remote")

Listener = Callable[[dict], Awaitable[None]]
WS_SEND_TIMEOUT_S = 5.0


def split_sender(text: str) -> tuple[str | None, str]:
    """Kanalnachrichten kommen als „Absender: Text“; beides getrennt speichern."""
    head, sep, rest = text.partition(": ")
    if sep and 0 < len(head) <= 40 and "\n" not in head:
        return head, rest
    return None, text


class Hub:
    """Verteilt Events an Browser (WebSocket) und interne Listener.

    Listener laufen als eigene Tasks und werden NICHT abgewartet: Der Node ruft
    `emit` aus seinem Lese-Loop auf. Würde ein Listener dort selbst einen Befehl
    senden und auf die Antwort warten, blockierte er den Loop, der die Antwort
    liefern müsste.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.history: deque[dict] = deque(maxlen=200)
        self.listeners: list[Listener] = []
        self._tasks: set[asyncio.Task] = set()

    async def emit(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        self.history.append(event)
        for listener in self.listeners:
            task = asyncio.create_task(self._run(listener, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_json({"kind": "event", "event": event}), timeout=WS_SEND_TIMEOUT_S)   # hängender Browser blockiert nicht
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
            # Verbindung auch wirklich schließen: sonst bleibt der Browser verbunden, bekommt aber nie wieder Ereignisse
            # und verbindet nicht neu (Oberfläche „taub“). Im Hintergrund und begrenzt, damit emit nicht hängt.
            task = asyncio.create_task(self._close(ws))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _close(ws) -> None:
        try:
            await asyncio.wait_for(ws.close(code=1011), timeout=WS_SEND_TIMEOUT_S)
        except Exception:
            pass

    @staticmethod
    async def _run(listener: Listener, event: dict) -> None:
        try:
            await listener(event)
        except Exception:
            log.exception("Fehler in Event-Listener")

    async def drain(self) -> None:
        """Wartet auf laufende Listener (für Tests).

        Nur auf NOCH LAUFENDE Aufgaben warten und danach der Ereignisschleife einmal Luft geben: Ist eine Aufgabe fertig, ihr
        Aufräum-Rückruf aber noch nicht gelaufen, liefert `gather` sofort ein fertiges Ergebnis OHNE zu unterbrechen. Die alte
        Schleife `while self._tasks: await gather(...)` drehte sich dann endlos, ohne dass der Rückruf je drankam."""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)


@dataclass
class Runtime:
    settings: Settings
    hub: Hub
    node: BaseNode
    raw_store: Storage
    store: AsyncStorage
    automation: Automation
    state: dict = field(default_factory=lambda: {"last_error": None, "problems": []})
    push: PushService | None = None
    usb: UsbMonitor | None = None
    repeater: RepeaterAdmin | None = None
    nodecfg: NodeConfig | None = None
    stats: object | None = None
    room: object | None = None
    peers: object | None = None
    maintenance: object | None = None
    sendlog: object | None = None


def build(settings: Settings,
          node_factory: Callable[[Callable[[dict], Awaitable[None]]], BaseNode] | None = None,
          storage: Storage | None = None, budget: SendBudget | None = None) -> Runtime:
    hub = Hub()
    node = node_factory(hub.emit) if node_factory else create_node(settings, hub.emit)
    sendlog = None
    try:                                       # nur ein Zeitstempel je Sendung im Arbeitsspeicher; darf den Start nie verhindern
        from .sendlog import SendLog
        sendlog = SendLog()
        sendlog.instrument(node)
    except Exception:
        log.exception("Sendeprotokoll nicht verfügbar")
    problems: list[str] = []
    try:
        raw_store = storage or Storage(settings.db_path)
    except Exception as e:                     # Datei kaputt/gesperrt/Platte voll: trotzdem starten, nur im Speicher
        log.exception("Datenbank %s nicht nutzbar – arbeite im Arbeitsspeicher", settings.db_path)
        raw_store = Storage(":memory:")
        problems.append(f"Datenbank nicht nutzbar ({type(e).__name__}: {e}). Es wird nur im Arbeitsspeicher "
                        f"gespeichert, Daten gehen beim Neustart verloren.")
    store = AsyncStorage(raw_store)
    budget = budget or SendBudget(settings.auto_min_gap_s, settings.auto_max_per_hour)
    automation = Automation(node, store, hub.emit, budget, retention_days=settings.retention_days)
    automation.start_paused = raw_store.path == ":memory:" and settings.db_path != ":memory:"   # Rückfall auf RAM
    push = PushService(store, node)

    room = Room(node, store, budget, automation.log, hub.emit, paused=lambda: automation.paused)     # globaler Pausenschalter gilt auch für den Raum

    async def peer_notify(kind: str, text: str) -> None:
        await automation.log("warn" if kind == "down" else "info", text)
        await hub.emit({"type": "usb_alert", "payload": {"kind": kind, "text": text}})      # gleiches Banner wie bei USB-Ausfall
        push.notify_station(kind, text)

    peerwatch = PeerWatch(parse_peers(settings.peers), peer_notify) if settings.peers.strip() else None

    async def keyword_hit(text: str) -> str | None:
        try:
            words = await store.get_setting("push_keywords", [])
        except Exception:
            return None
        low = (text or "").lower()
        return next((w for w in words if isinstance(w, str) and w and w.lower() in low), None)

    async def on_event(event: dict) -> None:
        kind, payload = event["type"], event.get("payload") or {}
        if kind == "contact_message":
            prefix = payload.get("pubkey_prefix", "")
            contact = await automation.contact_by_prefix(prefix)
            try:                                   # Datenbank weg (Karte voll, gesperrt): Auto-Antwort, Push, Raum laufen trotzdem
                await store.add_message("in", "direct", prefix, payload.get("text", ""),
                                        name=contact["name"] if contact else None)
            except Exception:
                log.exception("Direktnachricht konnte nicht gespeichert werden")
            text0 = (payload.get("text") or "").strip()
            room_cmd = False
            try:
                room_cmd = text0.startswith("/") and bool((await room.config())["enabled"])
            except Exception:
                pass
            try:
                hit = await keyword_hit(payload.get("text", ""))
                if hit:
                    push.notify_keyword(hit, contact["name"] if contact else "Direktnachricht", None, payload.get("text", ""))
            except Exception:
                log.exception("Stichwort-Prüfung fehlgeschlagen")
            try:
                await room.handle_incoming(payload, contact)
            except Exception:
                log.exception("Raum-Bot fehlgeschlagen")
            if not room_cmd:                       # Raum-Befehle (/…) beantwortet nur der Raum-Bot, nie eine Auto-Antwort-Regel
                await automation.handle_incoming(payload, contact)
        elif kind == "channel_message":
            sender, text = split_sender(payload.get("text", ""))
            plen, mode = payload.get("path_len"), payload.get("path_hash_mode")
            msg_id = None
            try:                                   # Datenbank weg: Push, Stichwort und Auto-Antwort trotzdem
                msg_id = await store.add_message(
                    "in", "channel", str(payload.get("channel_idx", 0)), text, name=sender,
                    sender_ts=payload.get("sender_timestamp"), path=payload.get("path"),
                    path_hash_size=mode + 1 if isinstance(mode, int) and mode >= 0 else 1,
                    hops=plen if isinstance(plen, int) and 0 <= plen <= 63 else None,
                    snr=payload.get("SNR"), rssi=payload.get("RSSI"))
            except Exception:
                log.exception("Kanalnachricht konnte nicht gespeichert werden")
            try:
                hit = await keyword_hit(text)
                if hit:
                    push.notify_keyword(hit, f"Kanal {payload.get('channel_idx', 0)}", sender, text)
            except Exception:
                log.exception("Stichwort-Prüfung fehlgeschlagen")
            try:
                push.notify_channel(payload.get("channel_idx", 0), sender, text, msg_id)
            except Exception:
                log.exception("Push konnte nicht vorgemerkt werden")
            await automation.handle_channel_incoming(payload, sender, text)
        elif kind == "channel_rx":
            # Wir haben ein Funkpaket aus einem Kanal gehört. Gehört es zu einer von uns gesendeten
            # Nachricht (gleicher Kanal, gleicher Zeitstempel, gleicher Text), ist es eine Wiederholung.
            idx, sent_ts = payload.get("channel_idx"), payload.get("sender_timestamp")
            sent = await store.find_sent_channel_message(idx, sent_ts) if sent_ts is not None else None
            if sent and payload.get("message", "").partition(": ")[2] == sent["text"]:
                if await store.add_repeat(sent["id"], payload.get("path") or "",
                                          payload.get("path_hash_size") or 1,
                                          payload.get("snr"), payload.get("rssi")):
                    await hub.emit({"type": "channel_repeat",
                                    "payload": {"channel_idx": idx, "message_id": sent["id"]}})
        elif kind == "battery_low":
            try:
                push.notify_battery(payload.get("name", "?"), payload.get("mv", 0))
            except Exception:
                log.exception("Push konnte nicht vorgemerkt werden")
        elif kind in ("new_contact", "advertisement", "path_update"):
            automation.invalidate_contacts()

    async def usb_notify(kind: str, text: str) -> None:
        await automation.log("warn" if kind == "down" else "info", text)
        push.notify_usb(kind, text)

    monitor = UsbMonitor(store, hub.emit, usb_notify)
    vault_path = None if settings.db_path == ":memory:" else Path(settings.db_path).parent / "repeater_secrets.json"
    admin = RepeaterAdmin(node, automation.log, PasswordVault(vault_path), store=store)
    hub.listeners.append(on_event)
    return Runtime(settings, hub, node, raw_store, store, automation,
                   state={"last_error": None, "problems": problems}, push=push, usb=monitor, repeater=admin, nodecfg=NodeConfig(node, automation.log), stats=StatsSampler(node, store), room=room, peers=peerwatch, maintenance=Maintenance(store, hub.emit, automation.log), sendlog=sendlog)
