"""Simulierter Node – zum Entwickeln und Testen ohne Hardware."""
from __future__ import annotations

import asyncio
import random
import time

from .base import (BaseNode, Emit, NodeError, check_channel, derive_secret, normalize_channel,
                   normalize_contact)


def _sim_contacts() -> dict[str, dict]:
    raw = [
        ("a1" * 32, "Alice (Sim)", 1, 52.5200, 13.4050),
        ("b2" * 32, "Bob (Sim)", 1, 52.5300, 13.3800),
        ("c3" * 32, "Repeater Hügel (Sim)", 2, 52.5400, 13.4500),
        ("d4" * 32, "Repeater Dach (Sim)", 2, 52.5100, 13.3600),
        ("e5" * 32, "Raum Stammtisch (Sim)", 3, 0.0, 0.0),
    ]
    paths = {"a1" * 32: ("c3", 1), "b2" * 32: ("d4c3", 2), "c3" * 32: ("", 0), "d4" * 32: ("c3", 1), "e5" * 32: ("", -1)}
    return {
        key: {"public_key": key, "name": name, "type": t, "lat": lat, "lon": lon,
              "last_advert": int(time.time()) - random.randint(60, 3600), "path_len": paths[key][1], "out_path": paths[key][0]}
        for key, name, t, lat, lon in raw
    }


class SimNode(BaseNode):
    kind = "sim"
    uses_usb = True       # simuliert einen USB-Node (abziehen/einstecken über die Oberfläche oder Tests)
    SIM_PORT = "/dev/ttyACM0 (Sim)"

    def __init__(self, emit: Emit, background: bool = True):
        super().__init__(emit)
        self._connected = False
        self.plugged = True          # „Kabel steckt“
        self.hung = False            # Node steckt, antwortet aber nicht
        self.no_ack = False          # Test-Hilfe: Nachrichten mit confirm=True kommen nie an
        self._background_enabled = background
        self._task: asyncio.Task | None = None
        self._battery = 4100
        self._contacts = _sim_contacts()
        # Akku je Repeater (mV); Tests können das direkt setzen
        self.repeater_mv = {"c3" * 32: 4010, "d4" * 32: 3620}
        self.repeater_temp: dict[str, float | None] = {"c3" * 32: 11.5, "d4" * 32: None}   # None = kein Sensor
        self.fail_status: set[str] = set()   # Keys, die nicht antworten (für Tests)
        self.sent: list[tuple[str, str]] = []  # (public_key, text) – für Tests
        self.max_ch = 40
        self._channels: dict[int, tuple[str, bytes]] = {
            0: ("Public", derive_secret("Public")),        # Platzhalter-Schlüssel, nicht der echte
            1: ("#test", derive_secret("#test")),
        }
        self.sent_channel: list[tuple[int, str]] = []      # für Tests
        # Einstellungen des (simulierten) eigenen Nodes
        self.cfg: dict = {"name": "SimNode", "lat": 52.52, "lon": 13.405, "radio_freq": 869.525, "radio_bw": 250.0,
                          "radio_sf": 11, "radio_cr": 5, "tx_power": 14, "max_tx_power": 22, "adv_loc_policy": 1,
                          "telemetry_mode_base": 0, "telemetry_mode_loc": 0, "telemetry_mode_env": 0,
                          "manual_add_contacts": False, "multi_acks": 0, "path_hash_mode": 0}
        self.clock_skew_s = -3600                           # der simulierte Node geht eine Stunde falsch
        self.node_reboots = 0
        self._born = time.time()
        self._stat_scale = 1.0          # Tests/Demo: schneller laufende Zähler
        # Simulierte Repeater-Verwaltung: Passwort je Repeater (Standard „sim“), Anmeldungen, Einstellungen, Befehlsliste
        self.rep_pw: dict[str, str] = {k: "sim" for k, c in self._contacts.items() if c["type"] in (2, 3)}
        self.rep_logged: set[str] = set()
        self.rep_cmds: list[tuple[str, str]] = []           # (public_key, Befehl) – für Tests
        self.rep_reboots: list[str] = []
        self.rep_cfg: dict[str, dict] = {k: self._rep_defaults(c) for k, c in self._contacts.items() if c["type"] in (2, 3)}

    @property
    def connected(self) -> bool:
        return self._connected and self.plugged

    @property
    def port(self) -> str | None:       # type: ignore[override]
        return self.SIM_PORT if self.connected else None

    def usb_ports(self) -> list[dict]:
        if not self.plugged:
            return []
        return [{"device": self.SIM_PORT, "description": "Heltec V4 (Simulation)", "vid": 0x303A, "pid": 0x1001,
                 "serial": "SIM0001", "score": 5}]

    async def alive(self) -> bool:
        return self.connected and not self.hung

    async def unplug(self) -> None:
        """Kabel abziehen: Verbindung ist sofort weg, der Hintergrundverkehr endet."""
        was = self.connected
        self.plugged = False
        if self._task:
            self._task.cancel()
            self._task = None
        if was:
            await self.emit({"type": "disconnected", "payload": {"reason": "usb_unplugged"}})

    def plug(self) -> None:
        self.plugged = True
        self.hung = False

    async def connect(self) -> None:
        if not self.plugged:
            raise NodeError("Kein USB-Node erkannt (Simulation: Kabel abgezogen).")
        if self.hung:
            raise NodeError("Der Node antwortet nicht (Simulation).")
        self._connected = True
        self.public_key = "ff" * 32
        await self.emit({"type": "connected", "payload": {"sim": True}})
        if self._background_enabled:
            self._task = asyncio.create_task(self._background())

    async def disconnect(self) -> None:
        self._connected = False
        if self._task:
            self._task.cancel()
            self._task = None
        await self.emit({"type": "disconnected", "payload": {"reason": "stopped"}})

    async def _background(self) -> None:
        while True:
            await asyncio.sleep(random.uniform(20, 40))
            self._battery = max(3300, self._battery - random.randint(0, 5))
            for key in self.repeater_mv:
                self.repeater_mv[key] = max(3300, self.repeater_mv[key] - random.randint(0, 3))
            for key, t in self.repeater_temp.items():
                if t is not None:
                    self.repeater_temp[key] = round(max(-5.0, min(35.0, t + random.uniform(-0.6, 0.6))), 2)
            clients = [c for c in self._contacts.values() if c["type"] == 1]
            who = random.choice(clients)
            roll = random.random()
            if roll < 0.25 and self._channels:
                idx = random.choice(list(self._channels))
                await self.inject_channel_message(idx, who["name"], random.choice(["Moin zusammen", "Test", "Jemand da?"]),
                                                  path=random.choice(["", "c3", "d4c3"]))
            elif roll < 0.7:
                await self.emit({
                    "type": "contact_message",
                    "payload": {
                        "pubkey_prefix": who["public_key"][:12],
                        "text": random.choice(["Hallo!", "ping", "Empfang gut?"]),
                        "txt_type": 0,
                        "sender_timestamp": int(time.time()),
                    },
                })
            else:
                await self.emit({
                    "type": "advertisement",
                    "payload": {"public_key": who["public_key"]},
                })

    def _require(self) -> None:
        if not self.connected:
            raise NodeError("Simulator nicht verbunden")

    def _need_contact(self, public_key: str) -> dict:
        if public_key not in self._contacts:
            raise NodeError("Unbekannter Kontakt")
        return self._contacts[public_key]

    async def inject_message(self, public_key: str, text: str, path_len: int | None = None,
                             path: str | None = None) -> None:
        """Für Tests: simuliert eine eingehende Direktnachricht (optional mit Weg)."""
        payload = {"pubkey_prefix": public_key[:12], "text": text, "txt_type": 0,
                   "sender_timestamp": int(time.time())}
        if path_len is not None:
            payload["path_len"] = path_len
        if path is not None:
            payload["path"] = path
        await self.emit({"type": "contact_message", "payload": payload})

    async def status(self) -> dict:
        return {
            "kind": self.kind,
            "connected": self._connected,
            "name": self.cfg["name"],
            "model": "Simulator",
            "firmware": "sim-0.0",
            "public_key": "ff" * 32,
            "battery_mv": self._battery,
        }

    async def contacts(self) -> list[dict]:
        self._require()
        return sorted((normalize_contact(c) for c in self._contacts.values()),
                      key=lambda c: c["name"].lower())

    async def import_contact(self, card: str) -> None:
        self._require()
        hexpart = card.strip().removeprefix("meshcore://")
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            raise NodeError("Kontaktkarte ist kein gültiger Hex-Text")
        if len(raw) < 32:
            raise NodeError("Kontaktkarte zu kurz")
        key = raw[:32].hex()
        self._contacts[key] = {"public_key": key, "name": f"Neu {key[:6]}", "type": 1,
                               "lat": 0.0, "lon": 0.0, "last_advert": int(time.time()), "path_len": -1}
        await self.emit({"type": "new_contact", "payload": {"public_key": key}})

    async def remove_contact(self, public_key: str) -> None:
        self._require()
        self._need_contact(public_key)
        del self._contacts[public_key]

    async def reset_path(self, public_key: str) -> None:
        self._require()
        self._need_contact(public_key)["path_len"] = -1

    async def export_contact(self, public_key: str | None = None) -> str:
        self._require()
        if public_key is not None:
            self._need_contact(public_key)
        return "meshcore://" + (public_key or "ff" * 32)

    async def send_message(self, public_key: str, text: str, confirm: bool = False) -> dict:
        self._require()
        self._need_contact(public_key)
        self.sent.append((public_key, text))
        await self.emit({"type": "message_sent", "payload": {"to": public_key, "text": text}})
        asyncio.create_task(self._ack_later())
        return {"ok": True, "acked": not self.no_ack} if confirm else {"ok": True}

    async def _ack_later(self) -> None:
        await asyncio.sleep(1)
        await self.emit({"type": "acknowledgement", "payload": {"code": "sim"}})

    async def max_channels(self) -> int:
        return self.max_ch

    async def channels(self) -> list[dict]:
        self._require()
        return [normalize_channel(i, n, s) for i, (n, s) in sorted(self._channels.items())]

    async def set_channel(self, idx: int, name: str, secret: bytes | None) -> dict:
        self._require()
        if not 0 <= idx < self.max_ch:
            raise NodeError(f"Kanalplatz {idx} gibt es nicht (0 bis {self.max_ch - 1})")
        name, secret = check_channel(name, secret)
        self._channels[idx] = (name, secret)
        return normalize_channel(idx, name, secret)

    async def remove_channel(self, idx: int) -> None:
        self._require()
        if idx not in self._channels:
            raise NodeError("Kanal nicht vorhanden")
        del self._channels[idx]

    async def inject_channel_message(self, idx: int, sender: str, text: str, path: str = "c3",
                                     snr: float | None = 5.5, rssi: float | None = -92) -> None:
        """Für Tests: simuliert eine eingehende Kanalnachricht (der Node liefert „Name: Text“).
        `path` = Hop-Kennungen als Hex (1 Byte je Hop), leer = direkt gehört."""
        payload = {"channel_idx": idx, "text": f"{sender}: {text}", "txt_type": 0,
                   "sender_timestamp": int(time.time()), "path_len": len(path) // 2, "path_hash_mode": 0}
        if snr is not None:
            payload.update(path=path, SNR=snr, RSSI=rssi)
        await self.emit({"type": "channel_message", "payload": payload})

    async def inject_repeat(self, idx: int, sender_ts: int, text: str, path: str = "c3",
                            snr: float = 4.0, rssi: float = -95, sender: str = "SimNode") -> None:
        """Für Tests: simuliert, dass ein Repeater unsere Kanalnachricht wiederholt und wir es hören."""
        await self.emit({"type": "channel_rx", "payload": {
            "channel_idx": idx, "message": f"{sender}: {text}", "sender_timestamp": sender_ts,
            "path": path, "path_hash_size": 1, "snr": snr, "rssi": rssi}})

    async def _repeats_later(self, idx: int, ts: int, text: str) -> None:
        for path in random.sample(["c3", "d4", "d4c3"], k=random.randint(1, 3)):
            await asyncio.sleep(random.uniform(1, 3))
            await self.inject_repeat(idx, ts, text, path, round(random.uniform(-3, 10) * 4) / 4,
                                     random.randint(-110, -80))

    async def send_channel_message(self, channel: int, text: str) -> dict:
        self._require()
        if channel not in self._channels:
            raise NodeError(f"Kanal {channel} ist auf dem Node nicht eingerichtet")
        self.sent_channel.append((channel, text))
        ts = int(time.time())
        if self._background_enabled:
            asyncio.create_task(self._repeats_later(channel, ts, text))
        await self.emit({"type": "channel_message_sent",
                         "payload": {"channel": channel, "text": text}})
        return {"ok": True, "timestamp": ts}

    async def send_advert(self, flood: bool = False) -> dict:
        self._require()
        await self.emit({"type": "advert_sent", "payload": {"flood": flood}})
        return {"ok": True}

    async def node_settings(self) -> dict:
        self._require()
        return {**self.cfg, "public_key": "ff" * 32, "firmware": "sim-0.0", "node_time": int(time.time()) + self.clock_skew_s}

    async def node_apply(self, changes: dict) -> list[str]:
        self._require()
        unknown = set(changes) - set(self.cfg)
        if unknown:
            raise NodeError(f"Unbekannte Einstellung: {', '.join(sorted(unknown))}")
        if ("lat" in changes) != ("lon" in changes):                # wie der echte Node: Koordinaten nur zusammen
            raise NodeError("Breiten- und Längengrad müssen zusammen geschrieben werden")
        self.cfg.update(changes)
        return list(changes)

    async def node_stats(self) -> dict:
        self._require()
        t = time.time() - self._born
        rnd = lambda a, b: random.uniform(a, b)
        return {"core": {"battery_mv": 4000, "uptime_secs": int(t), "errors": 0, "queue_len": 0},
                "radio": {"noise_floor": int(-112 + rnd(-3, 3)), "last_rssi": -85, "last_snr": 6.5,
                          "tx_air_secs": int(t * 0.004 * self._stat_scale), "rx_air_secs": int(t * 0.02 * self._stat_scale)},
                "packets": {"recv": int(t * 0.3 * self._stat_scale), "sent": int(t * 0.05 * self._stat_scale), "flood_tx": int(t * 0.02 * self._stat_scale),
                            "direct_tx": int(t * 0.03 * self._stat_scale), "flood_rx": int(t * 0.2 * self._stat_scale),
                            "direct_rx": int(t * 0.1 * self._stat_scale), "recv_errors": 0}}

    async def node_set_time(self, epoch: int) -> None:
        self._require()
        self.clock_skew_s = int(epoch) - int(time.time())

    async def node_reboot(self) -> None:
        self._require()
        self.node_reboots += 1

    @staticmethod
    def _rep_defaults(c: dict) -> dict:
        return {"name": c["name"], "lat": str(c["lat"]), "lon": str(c["lon"]), "radio": "869.525,250,11,5", "tx": "14",
                "repeat": "on", "flood.max": "64", "flood.advert.interval": "12", "advert.interval": "0",
                "txdelay": "0.5", "direct.txdelay": "0.2", "rxdelay": "0", "dutycycle": "50", "path.hash.mode": "0",
                "loop.detect": "off", "guest.password": "", "owner.info": "", "multi.acks": "0"}

    def _rep_target(self, public_key: str) -> dict:
        self._require()
        c = self._need_contact(public_key)
        if c["type"] not in (2, 3):
            raise NodeError("Verwaltung gibt es nur für Repeater und Rooms")
        return c

    async def repeater_login(self, public_key: str, password: str) -> dict:
        self._rep_target(public_key)
        await asyncio.sleep(0)
        if public_key in self.fail_status or self.rep_pw.get(public_key) != password:
            raise NodeError("Keine Anmeldebestätigung: Passwort falsch oder der Repeater ist nicht erreichbar.")
        self.rep_logged.add(public_key)
        return {"ok": True, "admin": True, "permissions": 3}

    async def repeater_logout(self, public_key: str) -> None:
        self._rep_target(public_key)
        self.rep_logged.discard(public_key)

    async def repeater_command(self, public_key: str, command: str, wait: bool = True, secret: bool = False) -> dict:
        self._rep_target(public_key)
        await asyncio.sleep(0)
        self.rep_cmds.append((public_key, command))
        if public_key in self.fail_status or public_key not in self.rep_logged:
            return {"replies": []}                          # ein echter Repeater schweigt, wenn man nicht angemeldet ist
        cfg, parts = self.rep_cfg[public_key], command.split()
        low = command.lower()
        if parts[0] in ("reboot", "poweroff", "shutdown", "clkreboot"):
            self.rep_reboots.append(public_key)
            self.rep_logged.discard(public_key)
            return {"replies": []}
        if not wait:
            return {"replies": []}
        if low == "ver":
            reply = "MeshCore v1.14.1 (Simulation)"
        elif low == "board":
            reply = "Sim-Board V4"
        elif low == "clock":
            reply = time.strftime("%H:%M - %d/%m/%Y UTC", time.gmtime())
        elif low == "clock sync" or parts[0] == "time":
            reply = "OK - clock set"
        elif parts[0] == "advert" or low in ("advert.zerohop", "discover.neighbors", "clear stats"):
            reply = "OK - Advert sent" if parts[0] == "advert" else "OK"
        elif low == "neighbors":
            reply = "a1a1a1a1:1700000000:26\nb2b2b2b2:1700000100:18"
        elif parts[0] == "get" and len(parts) == 2 and parts[1] in cfg:
            reply = f"> {cfg[parts[1]]}"
        elif parts[0] == "set" and len(parts) >= 3 and parts[1] in cfg:
            cfg[parts[1]] = command.split(None, 2)[2]
            reply = "OK"
        elif parts[0] == "password" and len(parts) >= 2:
            self.rep_pw[public_key] = command.split(None, 1)[1]
            reply = "password now: " + self.rep_pw[public_key]
        elif parts[0] == "region":
            reply = "OK"
        else:
            reply = "Unknown command"
        return {"replies": [reply]}

    async def repeater_neighbours(self, public_key: str) -> dict:
        self._rep_target(public_key)
        await asyncio.sleep(0)
        if public_key in self.fail_status or public_key not in self.rep_logged:
            raise NodeError("Keine Antwort auf die Nachbarn-Abfrage (angemeldet? erreichbar?)")
        return {"total": 2, "neighbours": [{"prefix": "a1" * 6, "secs_ago": 120, "snr": 6.5},
                                           {"prefix": "b2" * 6, "secs_ago": 900, "snr": -2.25}]}

    async def repeater_acl(self, public_key: str) -> list[dict]:
        self._rep_target(public_key)
        await asyncio.sleep(0)
        if public_key in self.fail_status or public_key not in self.rep_logged:
            raise NodeError("Keine Antwort auf die Zugriffslisten-Abfrage (angemeldet als Admin?)")
        return [{"prefix": "a1" * 6, "perm": 3}, {"prefix": "b2" * 6, "perm": 0}]

    async def repeater_status(self, public_key: str) -> dict:
        self._require()
        c = self._need_contact(public_key)
        if c["type"] not in (2, 3):
            raise NodeError("Status gibt es nur für Repeater und Rooms")
        if public_key in self.fail_status:
            raise NodeError("Keine Antwort (Timeout)")
        await asyncio.sleep(0)
        mv = self.repeater_mv.get(public_key, 4000)
        return {"bat": mv, "uptime": 123456, "noise_floor": -110, "last_rssi": -80,
                "last_snr": 7.5, "nb_recv": 100, "nb_sent": 80}

    async def repeater_telemetry(self, public_key: str) -> dict:
        self._require()
        c = self._need_contact(public_key)
        if c["type"] not in (2, 3):
            raise NodeError("Telemetrie gibt es nur für Repeater und Rooms")
        if public_key in self.fail_status:
            raise NodeError("Keine Antwort (Timeout)")
        await asyncio.sleep(0)
        t = self.repeater_temp.get(public_key)
        lpp = [{"channel": 1, "type": "voltage", "value": self.repeater_mv.get(public_key, 4000) / 1000}]
        temp = None
        if t is not None:
            temp = round(t + random.uniform(-0.4, 0.4), 1)
            lpp.append({"channel": 2, "type": "temperature", "value": temp})
            lpp.append({"channel": 3, "type": "humidity", "value": round(55 + random.uniform(-3, 3), 1)})
            lpp.append({"channel": 4, "type": "gps", "value": [52.5, 13.4, 40.0]})
        return {"temperature": temp, "lpp": lpp}

    async def ping(self, public_key: str, mode: str = "status", text: str = "ping") -> dict:
        self._require()
        self._need_contact(public_key)
        t0 = time.monotonic()
        if mode == "trace":
            c = self._need_contact(public_key)
            if c["type"] not in (2, 3) or public_key in self.fail_status:
                return {"ok": False, "rtt_ms": None}
            return {"ok": True, "rtt_ms": round(random.uniform(300, 900), 1),
                    "snr_out": round(random.uniform(-2, 11) * 4) / 4, "snr_in": round(random.uniform(-2, 11) * 4) / 4}
        try:
            if mode == "status":
                await self.repeater_status(public_key)
            else:
                await self.send_message(public_key, text)
        except NodeError:
            return {"ok": False, "rtt_ms": None}
        return {"ok": True, "rtt_ms": round((time.monotonic() - t0) * 1000, 1) + random.uniform(200, 900)}
