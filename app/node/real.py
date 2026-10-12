"""Echter MeshCore-Node über die Python-Bibliothek `meshcore`.

Unterstützt Verbindung per USB-Seriell, TCP oder BLE (siehe config.py).
Feldnamen und Signaturen sind gegen meshcore 2.3.15 geprüft (Quelltext gelesen),
aber noch NICHT mit echter Hardware getestet.

Anpassungen an der Bibliothek sollen nur hier passieren, nicht in main.py.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any

from ..config import Settings
from .base import (BaseNode, Emit, NodeError, check_channel, jsonable, normalize_channel,
                   normalize_contact)

log = logging.getLogger(__name__)

# Events, die 1:1 an die Web-Oberfläche weitergereicht werden.
FORWARDED = [
    "CONTACT_MSG_RECV", "CHANNEL_MSG_RECV", "ADVERTISEMENT", "NEW_CONTACT",
    "ACK", "MSG_SENT", "PATH_UPDATE", "BATTERY", "CONNECTED", "DISCONNECTED",
]

STATUS_MIN_TIMEOUT_S = 15  # LoRa über mehrere Hops ist langsam
TRACE_MIN_TIMEOUT_S = 10   # Zero-Hop ist direkt, braucht aber trotzdem Luft für Wiederholungen
BATTERY_CACHE_S = 1800.0   # eigener Akkustand ändert sich langsam: nicht bei jeder Status-Abfrage (alle paar Sekunden) neu vom Node holen


def find_serial_port() -> str:
    """Bester erkannter USB-Node (siehe usb.py)."""
    from .. import usb

    return usb.resolve_port("auto", usb.list_ports())


def _locked(fn):
    """Serialisiert alle Befehle an den Node über eine einzige Verbindung (seriell/TCP/BLE kann immer
    nur eine Anfrage gleichzeitig beantworten). Die `meshcore`-Bibliothek sperrt nur je Befehlsart
    einzeln, nicht Befehlsarten gegeneinander - zwei gleichzeitige Anfragen von verschiedenen Seiten der
    Oberfläche (z. B. der alle 15 s laufende Status-Abruf und eine gerade geöffnete Seite) konnten sich
    sonst überschneiden und den Node in einen Fehlerzustand bringen (ERR_CODE_BAD_STATE)."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        async with self._cmd_lock:
            return await fn(self, *args, **kwargs)
    return wrapper


class RealNode(BaseNode):
    kind = "meshcore"

    def __init__(self, emit: Emit, settings: Settings, clock=time.time):
        super().__init__(emit)
        self.s = settings
        self.mc: Any = None
        self._EventType: Any = None
        self._max_channels: int | None = None
        self._chan_idx_by_name: dict[str, int] = {}
        self._channels_cache: list[dict] | None = None
        self.port = None
        self._rep_lock = asyncio.Lock()          # nur ein Repeater-Befehl gleichzeitig (Antworten sonst nicht zuordenbar)
        self._cmd_lock = asyncio.Lock()          # nur ein Befehl an den Node gleichzeitig, siehe _locked()
        self._clock = clock
        self._battery: tuple[float, int | None] | None = None   # (zuletzt abgefragt, mV) für status()

    @property
    def uses_usb(self) -> bool:             # type: ignore[override]
        return self.s.mode == "serial"

    @property
    def connected(self) -> bool:
        return bool(self.mc and self.mc.is_connected)

    @_locked
    async def alive(self) -> bool:
        """Herzschlag: kleine Abfrage, die der Node beantworten muss. Fehler/Zeitüberschreitung = nicht lebendig."""
        if not self.connected:
            return False
        try:
            res = await asyncio.wait_for(self.mc.commands.send_device_query(), 10)
            return res is not None and res.type != self._EventType.ERROR
        except Exception:
            return False

    async def connect(self) -> None:
        from meshcore import EventType, MeshCore

        self._EventType = EventType
        self._channels_cache = None         # neue Verbindung: Kanäle einmal frisch lesen, nicht die alte Liste wiederverwenden
        self.public_key = None
        s = self.s
        try:
            if s.mode == "serial":
                from .. import usb

                port = usb.resolve_port(s.serial_port, await asyncio.to_thread(usb.list_ports))
                log.info("Verbinde seriell mit %s", port)
                # auto_reconnect aus: Wiedereinstecken (auch unter neuem Namen) erledigt der Supervisor
                self.mc = await MeshCore.create_serial(port, s.baudrate, debug=s.debug, auto_reconnect=False)
                self.port = port
            elif s.mode == "tcp":
                if not s.tcp_host:
                    raise NodeError("tcp_host nicht gesetzt")
                self.mc = await MeshCore.create_tcp(
                    s.tcp_host, s.tcp_port, auto_reconnect=True
                )
            elif s.mode == "ble":
                self.mc = await MeshCore.create_ble(
                    s.ble_address or None, pin=s.ble_pin or None
                )
            else:
                raise NodeError(f"Unbekannter Modus: {s.mode}")
        except NodeError:
            raise
        except Exception as e:  # Bibliotheksfehler gesammelt weitergeben
            raise NodeError(f"Verbindung fehlgeschlagen: {e}") from e

        if self.mc is None:
            self.port = None
            raise NodeError("Verbindung fehlgeschlagen (kein Node geantwortet)")

        for name in FORWARDED:
            et = getattr(EventType, name, None)
            if et is not None:
                self.mc.subscribe(et, self._forward)
        await self.mc.start_auto_message_fetching()
        try:
            async with self._cmd_lock:
                me = self._check(await self.mc.commands.send_appstart(), "appstart")
            self.public_key = jsonable(me.get("public_key"))
        except Exception as e:
            log.warning("Eigene Kennung nicht lesbar: %s", e)
        # Funk-Log entschlüsseln: liefert Pfad/SNR eingehender Kanalnachrichten und die gehörten
        # Wiederholungen unserer eigenen (gegen meshcore 2.3.15 geprüft, noch nicht an Hardware).
        try:
            self.mc.set_decrypt_channel_logs(True)
            rx = getattr(EventType, "RX_LOG_DATA", None)
            if rx is not None:
                self.mc.subscribe(rx, self._on_rx_log)
            await self.channels()            # die Bibliothek braucht die Kanalschlüssel zum Entschlüsseln
        except Exception as e:
            log.warning("Funk-Log-Entschlüsselung nicht verfügbar: %s", e)

    async def _on_rx_log(self, event: Any) -> None:
        """Reicht nur entschlüsselte Gruppen-Textpakete weiter (alles andere wäre zu viel Verkehr)."""
        p = event.payload or {}
        if p.get("payload_type") != 5 or p.get("message") is None:
            return
        idx = self._chan_idx_by_name.get(p.get("chan_name"))
        if idx is None:
            return
        await self.emit({"type": "channel_rx", "payload": {
            "channel_idx": idx, "message": p["message"], "sender_timestamp": p.get("sender_timestamp"),
            "path": p.get("path") or "", "path_hash_size": p.get("path_hash_size") or 1,
            "snr": p.get("snr"), "rssi": p.get("rssi")}})

    async def _forward(self, event: Any) -> None:
        etype = getattr(event.type, "value", str(event.type))
        if etype == "contact_message" and (event.payload or {}).get("txt_type") == 1:
            return          # Antwort eines Repeaters auf einen Textbefehl: gehört der Verwaltung, nicht in Chat/Protokoll/Auto-Antwort
        await self.emit({"type": etype, "payload": jsonable(event.payload)})

    async def disconnect(self) -> None:
        mc, self.mc, self.port = self.mc, None, None
        if mc:
            try:
                await asyncio.wait_for(mc.stop_auto_message_fetching(), 3)
            except Exception:
                pass
            try:
                await asyncio.wait_for(mc.disconnect(), 5)      # bei abgezogenem Kabel kann das hängen
            except Exception as e:
                log.debug("Trennen: %s", e)

    def _check(self, result: Any, what: str) -> Any:
        if result is None:
            raise NodeError(f"{what}: keine Antwort vom Node")
        if result.type == self._EventType.ERROR:
            raise NodeError(f"{what}: {jsonable(result.payload)}")
        return result.payload

    def _require(self) -> None:
        if not self.connected:
            raise NodeError("Node nicht verbunden")

    # ---- Status ----------------------------------------------------------
    @_locked
    async def status(self) -> dict:
        info: dict = {"kind": self.kind, "connected": self.connected}
        if not self.connected:
            return info
        # Jeder Teil einzeln, damit ein Fehler nicht den ganzen Status kippt.
        try:
            me = self._check(await self.mc.commands.send_appstart(), "appstart")
            info["name"] = me.get("name")
            info["public_key"] = jsonable(me.get("public_key"))
            info["self_info"] = jsonable(me)
        except Exception as e:
            info["self_info_error"] = str(e)
        try:
            dev = self._check(await self.mc.commands.send_device_query(), "device_query")
            info["model"] = dev.get("model")
            info["firmware"] = dev.get("ver") or dev.get("fw ver")
            info["device_info"] = jsonable(dev)
        except Exception as e:
            info["device_info_error"] = str(e)
        try:
            now = self._clock()
            if self._battery is None or now - self._battery[0] >= BATTERY_CACHE_S:
                bat = self._check(await self.mc.commands.get_bat(), "battery")
                self._battery = (now, bat.get("level"))
            info["battery_mv"] = self._battery[1]
        except Exception as e:
            info["battery_error"] = str(e)
        return info

    # ---- Kontakte --------------------------------------------------------
    async def _fetch_contacts(self) -> dict:
        payload = self._check(await self.mc.commands.get_contacts(), "get_contacts")
        return payload

    @_locked
    async def contacts(self) -> list[dict]:
        self._require()
        payload = await self._fetch_contacts()
        out = []
        for key, c in payload.items():
            out.append(normalize_contact({
                "public_key": key,
                "name": c.get("adv_name"),
                "type": c.get("type", 0),
                "lat": c.get("adv_lat"),
                "lon": c.get("adv_lon"),
                "last_advert": c.get("last_advert"),
                "path_len": c.get("out_path_len"),
                "out_path": c.get("out_path"),
            }))
        out.sort(key=lambda c: c["name"].lower())
        return out

    async def _contact_dict(self, public_key: str) -> dict:
        """Das Roh-Kontaktobjekt, das die Bibliothek für Anfragen braucht."""
        c = self.mc.contacts.get(public_key)
        if c is None:
            c = (await self._fetch_contacts()).get(public_key)
        if c is None:
            raise NodeError("Unbekannter Kontakt")
        return c

    @_locked
    async def import_contact(self, card: str) -> None:
        self._require()
        hexpart = card.strip().removeprefix("meshcore://")
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            raise NodeError("Kontaktkarte ist kein gültiger Hex-Text")
        self._check(await self.mc.commands.import_contact(raw), "import_contact")

    @_locked
    async def remove_contact(self, public_key: str) -> None:
        self._require()
        self._check(await self.mc.commands.remove_contact(public_key), "remove_contact")

    @_locked
    async def reset_path(self, public_key: str) -> None:
        self._require()
        self._check(await self.mc.commands.reset_path(public_key), "reset_path")

    @_locked
    async def export_contact(self, public_key: str | None = None) -> str:
        self._require()
        payload = self._check(await self.mc.commands.export_contact(public_key), "export_contact")
        return payload["uri"]

    # ---- Kanäle ----------------------------------------------------------
    # Hinweis: gegen den Quelltext von meshcore 2.3.15 geprüft, aber noch nicht an echter Hardware.
    # Ein Kanalplatz gilt als leer, wenn Name leer und Schlüssel nur Nullen sind.
    DEFAULT_MAX_CHANNELS = 40
    CHANNEL_RETRY_DELAY_S = 0.3

    @_locked
    async def max_channels(self) -> int:
        return await self._max_channels_impl()

    async def _max_channels_impl(self) -> int:
        """Ohne eigene Sperre: nur für Aufrufer gedacht, die `_cmd_lock` schon halten (siehe `channels()` etc.)."""
        if self._max_channels is None:
            self._require()
            try:
                dev = self._check(await self.mc.commands.send_device_query(), "device_query")
                self._max_channels = int(dev.get("max_channels") or self.DEFAULT_MAX_CHANNELS)
            except Exception as e:
                log.warning("max_channels unbekannt (%s) – nehme %s", e, self.DEFAULT_MAX_CHANNELS)
                return self.DEFAULT_MAX_CHANNELS
        return self._max_channels

    async def _get_channel(self, idx: int, tries: int = 3):
        """Eine ausbleibende Antwort heißt nicht „Kanal leer“ – erst nach mehreren Versuchen aufgeben
        (echte Hardware antwortet gelegentlich kurz nicht, siehe USB-Überwachung)."""
        last = None
        for attempt in range(tries):
            res = await self.mc.commands.get_channel(idx)
            if res is not None and res.type != self._EventType.ERROR:
                return res
            last = res
            if attempt < tries - 1:
                await asyncio.sleep(self.CHANNEL_RETRY_DELAY_S)
        raise NodeError(f"Kanal {idx}: keine Antwort vom Node nach {tries} Versuchen"
                        + (f" ({jsonable(last.payload)})" if last else ""))

    @_locked
    async def channels(self) -> list[dict]:
        """Wird nur einmal pro Verbindung wirklich vom Node gelesen (`connect()` leert den
        Zwischenspeicher); danach kommen weitere Aufrufe aus dem Speicher. `set_channel`/`remove_channel`
        halten den Zwischenspeicher selbst aktuell, indem sie nur den geänderten Platz neu lesen."""
        self._require()
        if self._channels_cache is not None:
            return list(self._channels_cache)
        out = []
        for idx in range(await self._max_channels_impl()):
            res = await self._get_channel(idx)
            p = res.payload
            name, secret = p.get("channel_name") or "", bytes(p.get("channel_secret") or b"")
            if not name and not any(secret):
                continue
            out.append(normalize_channel(idx, name, secret))
        self._chan_idx_by_name = {c["name"]: c["idx"] for c in out}
        self._channels_cache = out
        return list(out)

    def invalidate_channels(self) -> None:
        self._channels_cache = None

    @_locked
    async def set_channel(self, idx: int, name: str, secret: bytes | None) -> dict:
        self._require()
        top = await self._max_channels_impl()
        if not 0 <= idx < top:
            raise NodeError(f"Kanalplatz {idx} gibt es nicht (0 bis {top - 1})")
        name, secret = check_channel(name, secret)
        self._check(await self.mc.commands.set_channel(idx, name, secret), "set_channel")
        return await self._refresh_channel(idx, name, secret)

    async def _refresh_channel(self, idx: int, name: str | None, secret: bytes | None) -> dict:
        """Kanalplatz einmal zurücklesen: bestätigt, dass wirklich geschrieben wurde (nicht nur
        angenommen), und hält Namenstabelle + Liste aktuell, ohne alle anderen Plätze neu abzufragen."""
        res = await self._get_channel(idx)
        p = res.payload
        got_name, got_secret = p.get("channel_name") or "", bytes(p.get("channel_secret") or b"")
        want_name, want_secret = name or "", secret or bytes(16)
        if got_name != want_name or got_secret != want_secret:
            raise NodeError(f"Kanal {idx}: Schreiben nicht bestätigt (Node zeigt anderen Inhalt)")
        self._chan_idx_by_name = {n: i for n, i in self._chan_idx_by_name.items() if i != idx}
        ch = normalize_channel(idx, got_name, got_secret) if got_name or any(got_secret) else None
        if ch:
            self._chan_idx_by_name[got_name] = idx
        if self._channels_cache is not None:
            self._channels_cache = [c for c in self._channels_cache if c["idx"] != idx] + ([ch] if ch else [])
        return ch or normalize_channel(idx, "", bytes(16))

    @_locked
    async def remove_channel(self, idx: int) -> None:
        self._require()
        top = await self._max_channels_impl()
        if not 0 <= idx < top:
            raise NodeError(f"Kanalplatz {idx} gibt es nicht")
        self._check(await self.mc.commands.set_channel(idx, "", bytes(16)), "set_channel (löschen)")
        await self._refresh_channel(idx, None, None)

    # ---- Nachrichten und Funk -------------------------------------------
    @_locked
    async def send_message(self, public_key: str, text: str, confirm: bool = False) -> dict:
        """`confirm=True`: auf die Empfangsbestätigung warten und bei Ausbleiben bis zu 3 Mal senden (ab dem 3. Versuch per Flut);
        `acked` im Ergebnis sagt, ob sie kam. Ohne `confirm` heißt ok nur: der Node hat die Nachricht angenommen."""
        self._require()
        if confirm:
            res = await self.mc.commands.send_msg_with_retry(public_key, text, max_attempts=3, max_flood_attempts=2, flood_after=2)
            return {"ok": True, "acked": res is not None}
        payload = self._check(
            await self.mc.commands.send_msg(public_key, text), "send_msg"
        )
        return {"ok": True, "result": jsonable(payload)}

    @_locked
    async def send_channel_message(self, channel: int, text: str) -> dict:
        self._require()
        ts = int(time.time())            # wir geben den Zeitstempel vor, um Wiederholungen zuzuordnen
        self._check(await self.mc.commands.send_chan_msg(channel, text, ts), "send_chan_msg")
        return {"ok": True, "timestamp": ts}

    @_locked
    async def send_advert(self, flood: bool = False) -> dict:
        self._require()
        self._check(await self.mc.commands.send_advert(flood=flood), "send_advert")
        return {"ok": True}

    # ---- Überwachung -----------------------------------------------------
    @_locked
    async def repeater_status(self, public_key: str) -> dict:
        return await self._repeater_status_impl(public_key)

    async def _repeater_status_impl(self, public_key: str) -> dict:
        """Ohne eigene Sperre: nur für Aufrufer gedacht, die `_cmd_lock` schon halten (siehe `ping()`)."""
        self._require()
        contact = await self._contact_dict(public_key)
        res = await self.mc.commands.req_status_sync(
            contact, timeout=0, min_timeout=STATUS_MIN_TIMEOUT_S
        )
        if res is None:
            raise NodeError("Keine Antwort vom Repeater (Timeout)")
        return jsonable(res)

    @_locked
    async def repeater_telemetry(self, public_key: str) -> dict:
        """Sensorwerte per Telemetrie-Anfrage (Cayenne-LPP). Gegen meshcore 2.3.15 geprüft, nicht an
        Hardware: ob und wie ein Repeater die Temperatur meldet, hängt von dessen Firmware/Board ab."""
        self._require()
        contact = await self._contact_dict(public_key)
        lpp = await self.mc.commands.req_telemetry_sync(contact, timeout=0, min_timeout=STATUS_MIN_TIMEOUT_S)
        if lpp is None:
            raise NodeError("Keine Antwort auf die Telemetrie-Anfrage (Timeout)")
        lpp = jsonable(lpp)
        temp = next((e.get("value") for e in lpp if isinstance(e, dict) and e.get("type") == "temperature"), None)
        if isinstance(temp, (list, tuple)):
            temp = temp[0] if temp else None
        return {"temperature": float(temp) if isinstance(temp, (int, float)) else None, "lpp": lpp}

    # ---- Einstellungen des eigenen Nodes -------------------------------------------------------
    # Gegen meshcore 2.3.15 (Quelltext) geprüft, NICHT an Hardware getestet.
    OTHER_PARAMS = ("adv_loc_policy", "telemetry_mode_base", "telemetry_mode_loc", "telemetry_mode_env",
                    "manual_add_contacts", "multi_acks")

    @_locked
    async def node_settings(self) -> dict:
        self._require()
        me = self._check(await self.mc.commands.send_appstart(), "appstart")
        out = {"name": me.get("name"), "public_key": jsonable(me.get("public_key")), "lat": me.get("adv_lat"),
               "lon": me.get("adv_lon"), "radio_freq": me.get("radio_freq"), "radio_bw": me.get("radio_bw"),
               "radio_sf": me.get("radio_sf"), "radio_cr": me.get("radio_cr"), "tx_power": me.get("tx_power"),
               "max_tx_power": me.get("max_tx_power")}
        for k in self.OTHER_PARAMS:
            out[k] = me.get(k)
        out["path_hash_mode"], out["node_time"], out["firmware"] = None, None, None
        try:
            dev = self._check(await self.mc.commands.send_device_query(), "device_query")
            out["path_hash_mode"] = dev.get("path_hash_mode")
            out["firmware"] = dev.get("ver") or dev.get("fw ver")
        except Exception as e:
            log.debug("device_query: %s", e)
        try:
            out["node_time"] = self._check(await self.mc.commands.get_time(), "get_time").get("time")
        except Exception as e:
            log.debug("get_time: %s", e)
        return out

    @_locked
    async def node_apply(self, changes: dict) -> list[str]:
        self._require()
        c, cmds, done = dict(changes), self.mc.commands, []
        try:
            if "name" in c:
                self._check(await cmds.set_name(c["name"]), "set_name")
                done.append("name")
            if "lat" in c and "lon" in c:
                self._check(await cmds.set_coords(c["lat"], c["lon"]), "set_coords")
                done += ["lat", "lon"]
            if any(k in c for k in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")):
                cur = self._check(await cmds.send_appstart(), "appstart")
                r = {k: c.get(k, cur.get(k)) for k in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")}
                self._check(await cmds.set_radio(r["radio_freq"], r["radio_bw"], r["radio_sf"], r["radio_cr"]), "set_radio")
                done += [k for k in r if k in c]
            if "tx_power" in c:
                self._check(await cmds.set_tx_power(int(c["tx_power"])), "set_tx_power")
                done.append("tx_power")
            if any(k in c for k in self.OTHER_PARAMS):
                infos = dict(self._check(await cmds.send_appstart(), "appstart"))
                infos.update({k: c[k] for k in self.OTHER_PARAMS if k in c})
                infos["manual_add_contacts"] = int(bool(infos["manual_add_contacts"]))
                self._check(await cmds.set_other_params_from_infos(infos), "set_other_params")
                done += [k for k in self.OTHER_PARAMS if k in c]
            if "path_hash_mode" in c:
                self._check(await cmds.set_path_hash_mode(int(c["path_hash_mode"])), "set_path_hash_mode")
                done.append("path_hash_mode")
        except NodeError as e:
            raise NodeError(f"{e}" + (f" (schon geändert: {', '.join(done)})" if done else "")) from e
        return done

    @_locked
    async def node_stats(self) -> dict:
        """Zähler des Nodes (meshcore 2.3.15: get_stats_core/radio/packets). Nicht an Hardware getestet; ältere Firmware kennt das evtl. nicht."""
        self._require()
        c = self.mc.commands
        return {"core": dict(self._check(await c.get_stats_core(), "stats_core")),
                "radio": dict(self._check(await c.get_stats_radio(), "stats_radio")),
                "packets": dict(self._check(await c.get_stats_packets(), "stats_packets"))}

    @_locked
    async def node_set_time(self, epoch: int) -> None:
        self._require()
        self._check(await self.mc.commands.set_time(int(epoch)), "set_time")

    @_locked
    async def node_reboot(self) -> None:
        self._require()
        try:
            await asyncio.wait_for(self.mc.commands.reboot(), 5)       # der Node antwortet nicht mehr; Zeitlimit ist erwartet
        except Exception as e:
            log.debug("Neustart gesendet (%s)", e)

    # ---- Repeater verwalten ----------------------------------------------------
    # Gegen meshcore 2.3.15 (Quelltext) und die MeshCore-CLI-Doku geprüft, NICHT an Hardware getestet.
    # Antworten auf Textbefehle kommen als CONTACT_MSG_RECV mit txt_type 1 (CLI_DATA).
    @_locked
    async def repeater_login(self, public_key: str, password: str) -> dict:
        self._require()
        contact = await self._contact_dict(public_key)
        async with self._rep_lock:
            ev = await self.mc.commands.send_login_sync(contact, password, timeout=0, min_timeout=STATUS_MIN_TIMEOUT_S)
        if ev is None:
            raise NodeError("Keine Anmeldebestätigung: Passwort falsch oder der Repeater ist nicht erreichbar.")
        return {"ok": True, "admin": bool((ev.payload or {}).get("is_admin")),
                "permissions": (ev.payload or {}).get("permissions")}

    @_locked
    async def repeater_logout(self, public_key: str) -> None:
        self._require()
        contact = await self._contact_dict(public_key)
        res = await self.mc.commands.send_logout(contact["public_key"])
        if res is not None and res.type == self._EventType.ERROR:
            raise NodeError(f"Abmelden: {jsonable(res.payload)}")

    @_locked
    async def repeater_command(self, public_key: str, command: str, wait: bool = True) -> dict:
        self._require()
        contact = await self._contact_dict(public_key)
        prefix, replies = public_key[:12], []
        async with self._rep_lock:
            sent = self._check(await self.mc.commands.send_cmd(contact, command), "send_cmd")
            if not wait:
                return {"replies": []}
            timeout = max((sent or {}).get("suggested_timeout", 0) / 800, STATUS_MIN_TIMEOUT_S)
            filters = {"pubkey_prefix": prefix, "txt_type": 1}
            ev = await self.mc.wait_for_event(self._EventType.CONTACT_MSG_RECV, attribute_filters=filters, timeout=timeout)
            while ev is not None and len(replies) < 4:           # lange Antworten kommen in mehreren Teilen
                replies.append((ev.payload or {}).get("text", ""))
                ev = await self.mc.wait_for_event(self._EventType.CONTACT_MSG_RECV, attribute_filters=filters, timeout=2.0)
        return {"replies": replies}

    @_locked
    async def repeater_neighbours(self, public_key: str) -> dict:
        self._require()
        contact = await self._contact_dict(public_key)
        res = await self.mc.commands.req_neighbours_sync(contact, count=15, pubkey_prefix_length=6,
                                                        timeout=0, min_timeout=STATUS_MIN_TIMEOUT_S)
        if res is None:
            raise NodeError("Keine Antwort auf die Nachbarn-Abfrage (angemeldet? erreichbar?)")
        return {"total": res.get("neighbours_count", 0),
                "neighbours": [{"prefix": n["pubkey"], "secs_ago": n["secs_ago"], "snr": n["snr"]}
                               for n in res.get("neighbours", [])]}

    @_locked
    async def repeater_acl(self, public_key: str) -> list[dict]:
        self._require()
        contact = await self._contact_dict(public_key)
        res = await self.mc.commands.req_acl_sync(contact, timeout=0, min_timeout=STATUS_MIN_TIMEOUT_S)
        if res is None:
            raise NodeError("Keine Antwort auf die Zugriffslisten-Abfrage (angemeldet als Admin?)")
        return [{"prefix": e["key"], "perm": e["perm"]} for e in res]

    async def _trace_ping(self, public_key: str, t0: float) -> dict:
        """Zero-Hop-Ping: Trace über genau einen Hop (den Repeater, 1-Byte-Hash des Public Keys).

        Antwort nur, wenn der Repeater direkt erreichbar ist. Mehrere Repeater mit gleichem ersten
        Byte im Schlüssel können nicht unterschieden werden. Gegen den Quelltext von meshcore 2.3.15
        geprüft (send_trace / TRACE_DATA), noch nicht an echter Hardware.
        """
        tag = random.randint(1, 0xFFFFFFFF)
        sent = self._check(
            await self.mc.commands.send_trace(auth_code=0, tag=tag, path=public_key[:2]), "send_trace")
        timeout = max(sent.get("suggested_timeout", 0) / 800, TRACE_MIN_TIMEOUT_S)
        got = await self.mc.wait_for_event(
            self._EventType.TRACE_DATA, attribute_filters={"tag": tag}, timeout=timeout)
        if got is None:
            return {"ok": False, "rtt_ms": None}
        path = (got.payload or {}).get("path") or []
        snr_out = path[0].get("snr") if path else None          # so hört der Repeater uns
        snr_in = path[-1].get("snr") if len(path) > 1 else None  # so hören wir den Repeater
        return {"ok": True, "rtt_ms": round((time.monotonic() - t0) * 1000, 1),
                "snr_out": snr_out, "snr_in": snr_in}

    @_locked
    async def ping(self, public_key: str, mode: str = "status", text: str = "ping") -> dict:
        self._require()
        t0 = time.monotonic()
        if mode == "trace":
            return await self._trace_ping(public_key, t0)
        if mode == "status":
            try:
                await self._repeater_status_impl(public_key)
            except NodeError:
                return {"ok": False, "rtt_ms": None}
            return {"ok": True, "rtt_ms": round((time.monotonic() - t0) * 1000, 1)}

        sent = self._check(await self.mc.commands.send_msg(public_key, text), "send_msg")
        ack = sent["expected_ack"].hex()
        timeout = max(sent.get("suggested_timeout", 0) / 800, STATUS_MIN_TIMEOUT_S)
        got = await self.mc.wait_for_event(
            self._EventType.ACK, attribute_filters={"code": ack}, timeout=timeout
        )
        if got is None:
            return {"ok": False, "rtt_ms": None}
        return {"ok": True, "rtt_ms": round((time.monotonic() - t0) * 1000, 1)}
