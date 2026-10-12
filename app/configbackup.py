"""Sicherung und Wiederherstellung der Einstellungen (ohne Privatschlüssel, ohne Passwörter, ohne Nachrichtenverlauf).

Inhalt: Node-Einstellungen, die auf allen Stationen eines Mesh gleich sein sollten (Funk, Datenschutz …) - bewusst OHNE Name und
Standort, die je Gerät verschieden sind -, Kanäle (Name; Schlüssel nur auf Wunsch), Automatik-Regeln und eine Kontaktliste
(nur zur Übersicht: Kontakte kann das Programm daraus nicht wiederherstellen; Visitenkarten dafür in der Kontaktseite teilen).
Die Datei mit Kanalschlüsseln ist wie ein Passwort zu behandeln. Wiederherstellen prüft ALLES zuerst (Trockenlauf) und schreibt erst dann;
Funkwerte und Ähnliches brauchen weiter die Bestätigung. Nicht an echter Hardware getestet.
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from .node.base import NodeError, check_channel
from .nodeconfig import ConfigError, NeedsConfirm, validate
from .schemas import validate_config

FORMAT = "meshcore-remote-config"
VERSION = 1
MAX_RULES, MAX_CHANNELS = 100, 40
NODE_KEYS = ("radio_freq", "radio_bw", "radio_sf", "radio_cr", "tx_power", "adv_loc_policy", "telemetry_mode_base",
             "telemetry_mode_loc", "telemetry_mode_env", "manual_add_contacts", "multi_acks", "path_hash_mode")
# Name und Standort absichtlich NICHT dabei: die sind je Gerät verschieden und würden beim Wiederherstellen
# auf einer anderen Station deren eigenen Namen/Standort überschreiben. Die Funkparameter dagegen MÜSSEN
# auf allen Stationen im selben Mesh gleich sein, darum sind die genau die richtigen zum Übertragen.
SECTIONS = ("node", "channels", "rules")


class BackupError(NodeError):
    pass


async def export(rt, channel_keys: bool = False) -> dict:
    out: dict[str, Any] = {"format": FORMAT, "version": VERSION, "created": int(time.time()), "channel_keys_included": channel_keys}
    try:
        settings = await rt.node.node_settings()
        out["node"] = {k: settings.get(k) for k in NODE_KEYS if settings.get(k) is not None}
        out["channels"] = [{"idx": c["idx"], "name": c["name"], "hashtag": c["hashtag"],
                            **({"secret": c["secret"]} if channel_keys else {})} for c in await rt.node.channels()]
        out["contacts"] = [{"public_key": c["public_key"], "name": c["name"], "type": c["type_name"]} for c in await rt.node.contacts()]
    except NodeError as e:
        raise BackupError(f"Node nicht lesbar: {e}")
    out["rules"] = [{"kind": r["kind"], "name": r["name"], "enabled": r["enabled"], "config": r["config"]} for r in await rt.store.rules()]
    return out


def _check_shape(data: Any) -> None:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("Das ist keine Sicherungsdatei dieses Programms.")
    if data.get("version") != VERSION:
        raise BackupError(f"Unbekannte Version {data.get('version')}.")
    if len(data.get("rules") or []) > MAX_RULES or len(data.get("channels") or []) > MAX_CHANNELS:
        raise BackupError("Zu viele Einträge in der Datei.")


async def plan(rt, data: Any, sections: list[str]) -> dict:
    """Trockenlauf: prüft alles und beschreibt, was passieren würde. Wirft BackupError bei ungültigen Daten."""
    _check_shape(data)
    bad = [s for s in sections if s not in SECTIONS]
    if bad:
        raise BackupError(f"Unbekannter Bereich: {', '.join(bad)}")
    res: dict[str, Any] = {"node": None, "channels": [], "rules": [], "risks": []}
    if "node" in sections:
        want = {k: v for k, v in (data.get("node") or {}).items() if k in NODE_KEYS}
        if not want:
            raise BackupError("Die Datei enthält keine Node-Einstellungen.")
        cur = await rt.node.node_settings()
        try:
            clean, risks = validate(want, cur.get("max_tx_power"))
        except ConfigError as e:
            raise BackupError(f"Node-Einstellungen ungültig: {e}")
        from .nodeconfig import _differs
        changes = {k: v for k, v in clean.items() if _differs(cur.get(k), v)}
        res["node"] = changes
        res["risks"] = validate(changes, cur.get("max_tx_power"))[1] if changes else []
    if "channels" in sections:
        have = {c["idx"]: c for c in await rt.node.channels()}
        for ch in data.get("channels") or []:
            try:
                idx, name = int(ch["idx"]), str(ch["name"])
                secret = bytes.fromhex(ch["secret"]) if ch.get("secret") else None
                if not 0 <= idx <= 39:
                    raise ValueError("Platz außerhalb 0–39")
                if secret is None and not name.startswith("#"):
                    res["channels"].append({"idx": idx, "name": name, "action": "skip", "why": "Schlüssel fehlt in der Datei"})
                    continue
                name, secret = check_channel(name, secret)
            except (KeyError, ValueError, TypeError, NodeError) as e:
                raise BackupError(f"Kanal ungültig: {e}")
            cur = have.get(idx)
            if cur and cur["name"] == name and cur["secret"] == secret.hex():
                action, why = "same", ""
            elif cur:
                action, why = "conflict", f"Platz belegt mit „{cur['name']}“"
            else:
                action, why = "add", ""
            res["channels"].append({"idx": idx, "name": name, "action": action, "why": why, "_secret": secret.hex()})
    if "rules" in sections:
        existing = {(r["kind"], r["name"], repr(sorted(r["config"].items()))) for r in await rt.store.rules()}
        for r in data.get("rules") or []:
            try:
                cfg = validate_config(r["kind"], r["config"])
                name = str(r["name"])[:60]
                if not name:
                    raise ValueError("Name fehlt")
            except (KeyError, TypeError, ValueError, ValidationError) as e:
                raise BackupError(f"Regel „{r.get('name', '?') if isinstance(r, dict) else '?'}“ ungültig: {e}")
            same = (r["kind"], name, repr(sorted(cfg.items()))) in existing
            res["rules"].append({"kind": r["kind"], "name": name, "enabled": bool(r.get("enabled", True)), "config": cfg,
                                 "action": "same" if same else "add"})
    return res


async def restore(rt, data: Any, sections: list[str], confirm: bool = False, overwrite_channels: bool = False) -> dict:
    p = await plan(rt, data, sections)
    if p["risks"] and not confirm:
        raise NeedsConfirm("Bestätigung nötig: " + " ".join(p["risks"]))
    report: list[str] = []
    if p["node"]:
        await rt.nodecfg.apply(p["node"], confirm=True)
        report.append("Node-Einstellungen: " + ", ".join(p["node"]))
    for ch in p["channels"]:
        label = f"Kanal {ch['idx']} „{ch['name']}“"
        if ch["action"] == "skip" or ch["action"] == "same":
            continue
        if ch["action"] == "conflict" and not overwrite_channels:
            report.append(f"{label}: übersprungen ({ch['why']})")
            continue
        await rt.node.set_channel(ch["idx"], ch["name"], bytes.fromhex(ch["_secret"]))
        await rt.store.delete_channel_messages(ch["idx"])
        report.append(f"{label}: geschrieben")
    for r in p["rules"]:
        if r["action"] == "add":
            await rt.store.add_rule(r["kind"], r["name"], r["config"], r["enabled"])
            report.append(f"Regel „{r['name']}“ angelegt")
    return {"report": report or ["Nichts zu tun: alles ist schon so."]}


def public_plan(p: dict) -> dict:
    """Plan ohne Schlüssel für die Anzeige."""
    return {**p, "channels": [{k: v for k, v in c.items() if k != "_secret"} for c in p["channels"]]}
