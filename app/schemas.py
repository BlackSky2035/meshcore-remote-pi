"""Eingabe-Modelle und Validierung (API und Regel-Konfigurationen)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
RULE_KINDS = ("battery_poll", "ping", "channel_ping", "auto_reply", "advert")

# Untergrenzen schützen das Funknetz vor zu viel automatischem Verkehr.
MIN_INTERVAL_MIN = 5
MIN_ADVERT_INTERVAL_MIN = 60           # Advert = Visitenkarte, belegt Funkzeit; selten genug halten
MIN_FLOOD_ADVERT_INTERVAL_MIN = 360   # im ganzen Netz weitergeleitet: noch seltener
MIN_CHANNEL_INTERVAL_MIN = 15      # ein Kanalpost erreicht alle im Kanal: strenger als ein Einzel-Ping
MAX_REPLY_LEN = 150


def check_key(value: str) -> str:
    if not HEX64.match(value):
        raise ValueError("Public Key muss 64 Hex-Zeichen lang sein")
    return value.lower()


# ---- Einfache API-Eingaben -------------------------------------------------
class Message(BaseModel):
    to: str
    text: str = Field(min_length=1, max_length=139)

    @field_validator("to")
    @classmethod
    def _key(cls, v: str) -> str:
        return check_key(v)


class ChannelMessage(BaseModel):
    channel: int = Field(ge=0, le=39)
    text: str = Field(min_length=1, max_length=139)


HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


class ChannelIn(BaseModel):
    idx: int | None = Field(default=None, ge=0, le=39)     # leer = ersten freien Platz nehmen
    name: str = Field(min_length=1, max_length=64)
    secret: str | None = None                              # 32 Hex-Zeichen; leer = zufällig erzeugen

    @field_validator("secret")
    @classmethod
    def _secret(cls, v: str | None) -> str | None:
        v = (v or "").strip().lower().replace(" ", "")
        if not v:
            return None
        if not HEX32.match(v):
            raise ValueError("Schlüssel muss aus 32 Hex-Zeichen bestehen")
        return v


class ChannelView(BaseModel):
    show_details: bool


class PollRequest(BaseModel):
    temperature: bool = False


class Advert(BaseModel):
    flood: bool = False


class ImportCard(BaseModel):
    card: str = Field(min_length=10, max_length=2000)


PingMode = Literal["status", "message", "trace"]


class PingRequest(BaseModel):
    mode: PingMode = "status"


class PauseRequest(BaseModel):
    paused: bool


class EnabledRequest(BaseModel):
    enabled: bool


class BatteryDefaultIn(BaseModel):
    public_key: str | None = None


class BatterySelectionIn(BaseModel):
    keys: list[str | None] = Field(default_factory=list, max_length=3)      # bis zu 3 Plätze; None oder leer = Platz frei


# ---- Regel-Konfigurationen ---------------------------------------------------
class BatteryPollConfig(BaseModel):
    targets: Literal["all_repeaters"] | list[str] = "all_repeaters"
    interval_min: int = Field(60, ge=MIN_INTERVAL_MIN, le=1440)
    low_mv: int = Field(3700, ge=2500, le=4300)
    temperature: bool = True          # zusätzlich Temperatur (Telemetrie) abfragen, eine weitere Funkanfrage

    @field_validator("targets")
    @classmethod
    def _targets(cls, v):
        if isinstance(v, list):
            if not v or len(v) > 30:
                raise ValueError("1 bis 30 Ziele angeben")
            return [check_key(k) for k in v]
        return v


class PingConfig(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=20)
    interval_min: int = Field(60, ge=MIN_INTERVAL_MIN, le=1440)
    mode: PingMode = "status"
    text: str = Field("ping", min_length=1, max_length=60)

    @field_validator("targets")
    @classmethod
    def _targets(cls, v):
        return [check_key(k) for k in v]


class ChannelPingConfig(BaseModel):
    channel: int = Field(ge=0, le=39)
    interval_min: int = Field(60, ge=MIN_CHANNEL_INTERVAL_MIN, le=1440)
    text: str = Field("ping", min_length=1, max_length=60)


class AdvertConfig(BaseModel):
    interval_min: int = Field(360, ge=MIN_ADVERT_INTERVAL_MIN, le=10080)
    flood: bool = False

    @model_validator(mode="after")
    def _flood_rare(self):
        if self.flood and self.interval_min < MIN_FLOOD_ADVERT_INTERVAL_MIN:
            raise ValueError(f"Netzweite Adverts höchstens alle {MIN_FLOOD_ADVERT_INTERVAL_MIN // 60} Stunden (mindestens {MIN_FLOOD_ADVERT_INTERVAL_MIN} Minuten)")
        return self


class AutoReplyConfig(BaseModel):
    match: str = Field(min_length=1, max_length=100)
    mode: Literal["equals", "contains"] = "equals"
    reply: str = Field(min_length=1, max_length=MAX_REPLY_LEN)
    cooldown_s: int = Field(300, ge=30, le=86400)       # Pause je Absender
    max_per_hour: int = Field(10, ge=1, le=60)          # höchstens so viele Antworten dieser Regel in 60 min
    direct: bool = True                                  # auf Direktnachrichten antworten
    channels: list[int] = Field(default_factory=list, max_length=40)   # und/oder in diesen Kanälen
    only_from: list[str] | None = None                  # nur Direktnachrichten: nur diese Kontakte

    @field_validator("channels")
    @classmethod
    def _channels(cls, v):
        if any(not 0 <= c <= 39 for c in v):
            raise ValueError("Kanalplatz muss zwischen 0 und 39 liegen")
        return sorted(set(v))

    @model_validator(mode="after")
    def _somewhere(self):
        if not self.direct and not self.channels:
            raise ValueError("Mindestens Direktnachrichten oder einen Kanal auswählen")
        return self

    @field_validator("only_from")
    @classmethod
    def _only_from(cls, v):
        return None if v is None else [check_key(k) for k in v]


_CONFIG_MODELS = {
    "battery_poll": BatteryPollConfig,
    "ping": PingConfig,
    "channel_ping": ChannelPingConfig,
    "auto_reply": AutoReplyConfig,
    "advert": AdvertConfig,
}


class RuleIn(BaseModel):
    kind: Literal["battery_poll", "ping", "channel_ping", "auto_reply", "advert"]
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    config: dict


def validate_config(kind: str, config: dict) -> dict:
    """Prüft die Konfiguration passend zur Regelart und liefert das bereinigte Dict."""
    return _CONFIG_MODELS[kind](**config).model_dump()


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=120)
    auth: str = Field(min_length=20, max_length=30)


PushMode = Literal["every", "once"]


def _modes(v):
    """Liste von Plätzen (alle „every“) oder Zuordnung Platz -> Art; Plätze 0..63."""
    if isinstance(v, list):
        v = {i: "every" for i in v}
    if len(v) > 64 or any(not isinstance(k, int) or not 0 <= k <= 63 for k in v):
        raise ValueError("ungültige Kanalplätze")
    return v


class PushSubscribe(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)
    keys: PushKeys
    channels: dict[int, PushMode] | list[int] = Field(default_factory=dict)
    battery: bool = False
    label: str = Field("", max_length=80)

    @field_validator("channels")
    @classmethod
    def _ch(cls, v):
        return _modes(v)


class PushPrefs(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)
    channels: dict[int, PushMode] | list[int] = Field(default_factory=dict)
    battery: bool = False

    @field_validator("channels")
    @classmethod
    def _ch(cls, v):
        return _modes(v)


class PushEndpoint(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)


class SimUsb(BaseModel):
    plugged: bool | None = None
    hung: bool | None = None


class RepeaterLogin(BaseModel):
    password: str | None = None        # leer = gemerktes/Sitzungs-Passwort nutzen
    remember: bool = False             # Passwort dauerhaft auf diesem Gerät (Pi) speichern


class RepeaterCommand(BaseModel):
    command: str
    confirm: bool = False


class RepeaterSetting(BaseModel):
    name: str
    value: str | int | float | None = None
    confirm: bool = False


class NodeSettingsChange(BaseModel):
    changes: dict[str, str | int | float | bool | None]
    confirm: bool = False


class Confirm(BaseModel):
    confirm: bool = False


class BackupRestore(BaseModel):
    data: dict
    sections: list[str] = Field(default_factory=lambda: ["node", "channels", "rules"], max_length=3)
    confirm: bool = False
    overwrite_channels: bool = False
    dry_run: bool = False


class Keywords(BaseModel):
    words: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("words")
    @classmethod
    def _words(cls, v):
        out = []
        for w in v:
            w = " ".join(str(w).split())
            if not 2 <= len(w) <= 30:
                raise ValueError("Jedes Stichwort braucht 2 bis 30 Zeichen")
            if w.lower() not in [x.lower() for x in out]:
                out.append(w)
        return out


class RoomConfig(BaseModel):
    enabled: bool = False
    title: str = Field("Raum", min_length=1, max_length=30)
    max_posts: int = Field(5000, ge=100, le=20000)


class RoomPost(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    author: str = Field("Web", min_length=1, max_length=40)


class MaintenanceStart(BaseModel):
    minutes: int = Field(120, ge=5, le=1440)
