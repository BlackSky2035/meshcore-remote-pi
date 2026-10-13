"""REST-Endpunkte."""
from __future__ import annotations

import functools
import secrets
import subprocess
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from . import __version__
from .config import ROOT
from .node import NodeError
from . import configbackup as CB
from . import peers as peers_mod
from . import room as R
from .network import build_graph
from .stats import derive
from .nodeconfig import ConfigError, NeedsConfirm as CfgNeedsConfirm, spec as nodecfg_spec
from .repeater import NeedsConfirm, RepeaterError, catalog as repeater_catalog
from .runtime import Runtime
from .node.base import check_channel
from .schemas import (
    MaintenanceStart,
    RoomConfig, RoomPost,
    Keywords,
    BackupRestore,
    Confirm, NodeSettingsChange, RepeaterCommand, RepeaterLogin, RepeaterSetting, SimUsb,
    Advert, BatteryDefaultIn, BatterySelectionIn, ChannelIn, ChannelView, ChannelMessage, EnabledRequest, ImportCard, Message, PauseRequest, PingRequest, PollRequest, PushEndpoint, PushPrefs, PushSubscribe,
    RuleIn, check_key, validate_config,
)


@functools.lru_cache(maxsize=1)
def git_commit() -> str:
    """Einmal je Prozess ermitteln (ändert sich nur mit einem Neustart): git blockiert, darf also nicht in jeder Anfrage laufen."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _key(value: str) -> str:
    try:
        return check_key(value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def make_router(rt: Runtime) -> APIRouter:
    r = APIRouter(prefix="/api")
    node, store, auto = rt.node, rt.store, rt.automation
    git_commit()                               # schon beim Aufbau ermitteln, nicht in der ersten Anfrage (Ereignisschleife)

    def wrap(e: NodeError) -> HTTPException:
        return HTTPException(status_code=502, detail=str(e))

    # ---- Allgemein -------------------------------------------------------
    @r.get("/health")
    async def health() -> dict:
        """Kostet nichts und greift auf nichts zu (Selbsttest, Überwachung)."""
        return {"ok": True}

    @r.get("/version")
    async def version() -> dict:
        m = rt.maintenance.view() if rt.maintenance else {"until": None}
        return {"app": __version__, "commit": git_commit(), "mode": rt.settings.mode, "maintenance_until": m["until"]}

    @r.get("/config")
    async def config() -> dict:
        return {"tile_url": rt.settings.tile_url, "mode": rt.settings.mode,
                "watchdog_port": rt.settings.watchdog_port}

    @r.get("/status")
    async def status() -> dict:
        try:
            info = await node.status()
        except Exception as e:                 # Node-Status kaputt: Seite soll trotzdem laden und das zeigen
            info = {"kind": getattr(node, "kind", "?"), "connected": False,
                    "name": None, "model": None, "node_error": f"{type(e).__name__}: {e}"}
        info["usb"] = rt.usb.summary() if rt.usb else {"enabled": False}
        info["maintenance"] = rt.maintenance.view() if rt.maintenance else {"active": False}
        info["problems"] = list(rt.state.get("problems", []))
        info["last_error"] = rt.state["last_error"]
        info["app_version"] = __version__
        info["commit"] = git_commit()
        return info

    # ---- USB-Überwachung ---------------------------------------------------
    @r.get("/usb")
    async def usb_info() -> dict:
        mon = rt.usb
        if mon is None:
            return {"enabled": False}
        events = await store.usb_events(time.time() - 7 * 86400, 30)
        return {**mon.summary(), "detail": mon.detail, "hint": mon.hint, "ports": mon.ports,
                "configured": rt.settings.serial_port, "mode": rt.settings.mode,
                "availability": await mon.stats(), "events": list(reversed(events)),
                "alert_after_s": mon.alert_after_s}

    @r.post("/sim/usb")
    async def sim_usb(body: SimUsb) -> dict:
        """Nur im Simulator: Kabel abziehen/einstecken oder den Node „hängen“ lassen (zum Ausprobieren der Meldungen)."""
        if rt.settings.mode != "sim" or not hasattr(node, "plug"):
            raise HTTPException(status_code=404, detail="Nur im Simulator verfügbar")
        if body.plugged is False:
            await node.unplug()
        elif body.plugged is True:
            node.plug()
        if body.hung is not None:
            node.hung = body.hung
        return {"plugged": node.plugged, "hung": node.hung}

    # ---- Sicherung der Einstellungen -------------------------------------------
    @r.get("/backup/export")
    async def backup_export(channel_keys: bool = False):
        try:
            data = await CB.export(rt, channel_keys)
        except NodeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        name = time.strftime("meshcore-remote-einstellungen-%Y%m%d.json")
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @r.post("/backup/restore")
    async def backup_restore(body: BackupRestore) -> dict:
        try:
            if body.dry_run:
                return {"plan": CB.public_plan(await CB.plan(rt, body.data, body.sections))}
            return await CB.restore(rt, body.data, body.sections, body.confirm, body.overwrite_channels)
        except CfgNeedsConfirm as e:
            raise HTTPException(status_code=409, detail=str(e))
        except CB.BackupError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except NodeError as e:
            raise HTTPException(status_code=502, detail=str(e))

    # ---- Suche und Stichworte ------------------------------------------------
    @r.get("/search")
    async def search(q: str, kind: str | None = None, limit: int = 100) -> dict:
        if len(q.strip()) < 2:
            raise HTTPException(status_code=422, detail="Bitte mindestens 2 Zeichen eingeben")
        rows = await store.search_messages(q[:100], kind, limit)
        try:
            names = {c["idx"]: c["name"] for c in await node.channels()}
        except NodeError:
            names = {}
        for m in rows:
            m["where"] = (names.get(int(m["peer"])) or f"Kanal {m['peer']}") if m["kind"] == "channel" and m["peer"].isdigit() else (m["name"] or m["peer"][:8])
        return {"results": rows}

    @r.get("/push/keywords")
    async def keywords_get() -> dict:
        return {"words": await store.get_setting("push_keywords", [])}

    @r.put("/push/keywords")
    async def keywords_put(body: Keywords) -> dict:
        await store.set_setting("push_keywords", body.words)
        return {"words": body.words}

    # ---- Raum-Bot -------------------------------------------------------------
    def _room():
        if rt.room is None:
            raise HTTPException(status_code=503, detail="Raum ist nicht verfügbar")
        return rt.room

    @r.get("/room")
    async def room_state(limit: int = 100) -> dict:
        room = _room()
        return {"config": await room.config(), "posts": await store.room_posts(0, limit, newest=True),
                "members": await store.room_members(), "max_subscribers": R.MAX_SUBSCRIBERS, "post_max": R.POST_MAX}

    @r.put("/room/config")
    async def room_config(body: RoomConfig) -> dict:
        return await _room().set_config(body.enabled, body.title, body.max_posts)

    @r.post("/room/posts")
    async def room_post(body: RoomPost) -> dict:
        try:
            return await _room().post_from_web(body.text, body.author)
        except R.RoomError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @r.delete("/room/posts/{post_id}")
    async def room_delete_post(post_id: int) -> dict:
        if not await store.room_delete_post(post_id):
            raise HTTPException(status_code=404, detail="Beitrag nicht gefunden")
        return {"ok": True}

    @r.delete("/room/members/{public_key}")
    async def room_delete_member(public_key: str) -> dict:
        if not await store.room_delete_member(_key(public_key)):
            raise HTTPException(status_code=404, detail="Mitglied nicht gefunden")
        return {"ok": True}

    # ---- Wartungsmodus ---------------------------------------------------------
    def _control_auth(request: Request, token: str | None) -> None:
        """Erlaubt: gültige Sitzung (bzw. keine Anmeldung eingerichtet) ODER das Geheimwort `control_token` (für den Watchdog)."""
        auth = getattr(request.app.state, "auth", None)
        if auth is not None and auth.enabled and not auth.valid(request.cookies.get("mc_session")) \
                and not peers_mod.token_ok(rt.settings.control_token, token):
            raise HTTPException(status_code=401, detail="Nicht angemeldet.")

    def _maint(request: Request, token: str | None):
        _control_auth(request, token)
        if rt.maintenance is None:
            raise HTTPException(status_code=503, detail="Wartungsmodus ist nicht verfügbar")
        return rt.maintenance

    @r.get("/maintenance")
    async def maintenance_get(request: Request, x_control_token: str | None = Header(default=None)) -> dict:
        m = _maint(request, x_control_token)
        await m.load()
        return m.view()

    @r.post("/maintenance")
    async def maintenance_start(body: MaintenanceStart, request: Request, x_control_token: str | None = Header(default=None)) -> dict:
        return await _maint(request, x_control_token).start(body.minutes)

    @r.delete("/maintenance")
    async def maintenance_end(request: Request, x_control_token: str | None = Header(default=None)) -> dict:
        return await _maint(request, x_control_token).end()

    # ---- Sendezeiten (für den Watchdog: Unterspannung neben Sendungen legen) -----------
    @r.get("/sends")
    async def sends(request: Request, since: float = 0.0, x_control_token: str | None = Header(default=None)) -> dict:
        """Zeitpunkte der letzten Sendungen (nur Zeit und Art, kein Inhalt). Gleiche Zugangsregel wie der Wartungsmodus."""
        _control_auth(request, x_control_token)
        sl = getattr(rt, "sendlog", None)
        return {"now": time.time(), "capacity": getattr(sl, "maxlen", 0), "sends": sl.since(since) if sl else []}

    # ---- Mehrere Stationen ------------------------------------------------------
    @r.get("/peer/summary")
    async def peer_summary(x_peer_token: str | None = Header(default=None)) -> dict:
        """Kurzstatus für andere Stationen. Ohne gesetztes Geheimwort oder bei falschem Geheimwort: 404 (verrät nichts)."""
        if not peers_mod.token_ok(rt.settings.peer_token, x_peer_token):
            raise HTTPException(status_code=404, detail="Nicht gefunden")
        try:
            name = (await node.status()).get("name")
        except Exception:
            name = None
        return {"station": rt.settings.station_name or name or "Station", "app": __version__, "time": int(time.time()),
                "node_connected": bool(node.connected), "last_error": rt.state.get("last_error"),
                "usb": rt.usb.summary() if rt.usb else None, "problems": len(rt.state.get("problems") or []),
                "maintenance": rt.maintenance.view()["active"] if rt.maintenance else False}

    @r.get("/peers")
    async def peers_status() -> dict:
        return {"peers": rt.peers.status() if rt.peers else []}

    # ---- Funkstatistik -----------------------------------------------------
    @r.get("/stats")
    async def stats(hours: int = 24) -> dict:
        hours = max(1, min(hours, 24 * 90))
        rows = await store.radio_stats(time.time() - hours * 3600)
        return {"series": derive(rows), "latest": rows[-1] if rows else None, "samples": len(rows),
                "error": rt.stats.last_error if rt.stats else None,
                **(rt.stats.status() if rt.stats else {"enabled": False, "every_s": None})}

    @r.post("/stats/sample")
    async def stats_sample() -> dict:
        if rt.stats is None:
            raise HTTPException(status_code=503, detail="Statistik ist nicht verfügbar")
        row = await rt.stats.sample()
        if row is None:
            raise HTTPException(status_code=502, detail=rt.stats.last_error or "Node nicht verbunden")
        return {"row": row}

    @r.post("/stats/enabled")
    async def stats_enabled(body: EnabledRequest) -> dict:
        if rt.stats is None:
            raise HTTPException(status_code=503, detail="Statistik ist nicht verfügbar")
        await rt.stats.set_enabled(body.enabled)
        return rt.stats.status()

    # ---- Sensorwerte -------------------------------------------------------
    @r.post("/sensors/{public_key}/read")
    async def sensors_read(public_key: str) -> dict:
        key = _key(public_key)
        try:
            tel = await node.repeater_telemetry(key)
        except NodeError as e:
            raise wrap(e)
        n = await store.add_sensor_readings(key, tel.get("lpp") or [])
        if isinstance(tel.get("temperature"), (int, float)):
            await store.add_temperature(key, float(tel["temperature"]))
        return {"stored": n, "latest": await store.sensor_latest(key)}

    @r.get("/sensors/{public_key}")
    async def sensors_latest(public_key: str) -> dict:
        return {"latest": await store.sensor_latest(_key(public_key))}

    @r.get("/sensors/{public_key}/history")
    async def sensors_history(public_key: str, type: str, channel: int = 0, hours: int = 168) -> dict:
        hours = max(1, min(hours, 24 * 90))
        return {"rows": await store.sensor_history(_key(public_key), type, channel, time.time() - hours * 3600)}

    # ---- Netzkarte -------------------------------------------------------
    @r.get("/network")
    async def network() -> dict:
        try:
            contacts = await node.contacts()
        except NodeError as e:
            raise wrap(e)
        reports = await rt.store.neighbour_reports()
        heard = await rt.store.recent_incoming_paths()
        graph = build_graph(contacts, reports, heard)
        graph["reports"] = sorted({(x["repeater"], x["ts"]) for x in reports}, key=lambda t: t[0])
        graph["reports"] = [{"repeater": k, "ts": t} for k, t in graph["reports"]]
        return graph

    # ---- Kontakte --------------------------------------------------------
    @r.get("/contacts")
    async def contacts() -> list[dict]:
        try:
            return await node.contacts()
        except NodeError as e:
            raise wrap(e)

    @r.post("/contacts/import")
    async def import_contact(body: ImportCard) -> dict:
        try:
            await node.import_contact(body.card)
        except NodeError as e:
            raise wrap(e)
        auto.invalidate_contacts()
        return {"ok": True}

    @r.get("/contacts/export")
    async def export_self() -> dict:
        try:
            return {"uri": await node.export_contact(None)}
        except NodeError as e:
            raise wrap(e)

    @r.get("/contacts/{public_key}/export")
    async def export_contact(public_key: str) -> dict:
        try:
            return {"uri": await node.export_contact(_key(public_key))}
        except NodeError as e:
            raise wrap(e)

    @r.delete("/contacts/{public_key}")
    async def delete_contact(public_key: str) -> dict:
        try:
            await node.remove_contact(_key(public_key))
        except NodeError as e:
            raise wrap(e)
        auto.invalidate_contacts()
        return {"ok": True}

    @r.post("/contacts/{public_key}/reset-path")
    async def reset_path(public_key: str) -> dict:
        try:
            await node.reset_path(_key(public_key))
        except NodeError as e:
            raise wrap(e)
        auto.invalidate_contacts()
        return {"ok": True}

    # ---- Nachrichten -----------------------------------------------------
    @r.post("/message")
    async def send_message(m: Message) -> dict:
        try:
            result = await node.send_message(m.to, m.text)
        except NodeError as e:
            raise wrap(e)
        await store.add_message("out", "direct", m.to, m.text)
        return result

    @r.post("/channel")
    async def send_channel(m: ChannelMessage) -> dict:
        try:
            result = await node.send_channel_message(m.channel, m.text)
        except NodeError as e:
            raise wrap(e)
        await store.add_message("out", "channel", str(m.channel), m.text,
                                sender_ts=result.get("timestamp"))
        return result

    # ---- Kanäle ----------------------------------------------------------
    def _public(ch: dict, last: dict, views: dict, unread: dict | None = None) -> dict:
        return {"idx": ch["idx"], "name": ch["name"], "hashtag": ch["hashtag"],
                "last_ts": last.get(str(ch["idx"])), "unread": (unread or {}).get(ch["idx"], 0),
                "show_details": bool(views.get(ch["idx"], {}).get("show_details"))}

    def _prepared(body: ChannelIn, existing: dict | None) -> tuple[str, bytes]:
        """Name/Schlüssel prüfen. Ohne Schlüssel: bestehenden behalten (Umbenennen) oder neu erzeugen."""
        secret = bytes.fromhex(body.secret) if body.secret else None
        if secret is None and existing and not body.name.startswith("#"):
            secret = bytes.fromhex(existing["secret"])
        if secret is None and not body.name.startswith("#"):
            secret = secrets.token_bytes(16)
        try:
            return check_channel(body.name, secret)
        except NodeError as e:
            raise HTTPException(status_code=422, detail=str(e))

    async def _channel_or_404(idx: int) -> dict:
        try:
            chans = await node.channels()
        except NodeError as e:
            raise wrap(e)
        ch = next((c for c in chans if c["idx"] == idx), None)
        if ch is None:
            raise HTTPException(status_code=404, detail="Kanal nicht vorhanden")
        return ch

    @r.get("/channels")
    async def channels() -> dict:
        try:
            chans = await node.channels()
            top = await node.max_channels()
        except NodeError as e:
            raise wrap(e)
        last, views = await store.last_channel_activity(), await store.channel_views()
        unread = await store.channel_unread()
        return {"max_channels": top, "channels": [_public(c, last, views, unread) for c in chans]}

    @r.get("/channels/unread")
    async def channels_unread() -> dict:
        """Billig (nur Datenbank, kein Funkzugriff): für die Glocke in der Kopfzeile."""
        per = await store.channel_unread()
        return {"total": sum(per.values()), "channels": {str(k): v for k, v in per.items()}}

    @r.post("/channels/{idx}/read")
    async def channel_read(idx: int) -> dict:
        """Kanal als gelesen markieren (die Webseite ruft das beim Öffnen und bei neuen Nachrichten im offenen Kanal auf)."""
        if not 0 <= idx <= 63:
            raise HTTPException(status_code=422, detail="Kanalplatz ungültig")
        await store.mark_channel_read(idx)
        await rt.hub.emit({"type": "channel_read", "payload": {"channel_idx": idx}})
        return {"ok": True}

    @r.post("/channels")
    async def add_channel(body: ChannelIn) -> dict:
        try:
            chans = await node.channels()
            top = await node.max_channels()
        except NodeError as e:
            raise wrap(e)
        used = {c["idx"] for c in chans}
        idx = body.idx
        if idx is None:
            idx = next((i for i in range(top) if i not in used), None)
            if idx is None:
                raise HTTPException(status_code=409, detail="Alle Kanalplätze sind belegt")
        elif idx in used:
            raise HTTPException(status_code=409, detail=f"Kanalplatz {idx} ist schon belegt")
        name, secret = _prepared(body, None)
        try:
            ch = await node.set_channel(idx, name, secret)
        except NodeError as e:
            raise wrap(e)
        await store.delete_channel_messages(idx)       # alter Verlauf des Platzes gehört nicht dazu
        await store.delete_setting(f"channel_view_{idx}")
        await store.delete_setting(f"channel_read_{idx}")
        return _public(ch, {}, {})

    @r.put("/channels/{idx}")
    async def change_channel(idx: int, body: ChannelIn) -> dict:
        old = await _channel_or_404(idx)
        name, secret = _prepared(body, old)
        try:
            ch = await node.set_channel(idx, name, secret)
        except NodeError as e:
            raise wrap(e)
        if ch["secret"] != old["secret"]:               # anderer Schlüssel = anderer Kanal
            await store.delete_channel_messages(idx)
            await store.delete_setting(f"channel_view_{idx}")
            await store.delete_setting(f"channel_read_{idx}")
        return _public(ch, await store.last_channel_activity(), await store.channel_views(),
                       await store.channel_unread())

    @r.delete("/channels/{idx}")
    async def delete_channel(idx: int) -> dict:
        await _channel_or_404(idx)
        try:
            await node.remove_channel(idx)
        except NodeError as e:
            raise wrap(e)
        await store.delete_channel_messages(idx)
        await store.delete_setting(f"channel_view_{idx}")
        await store.delete_setting(f"channel_read_{idx}")
        return {"ok": True}

    @r.put("/channels/{idx}/view")
    async def channel_view(idx: int, body: ChannelView) -> dict:
        """Nur die Anzeige: gespeichert werden Pfade und Repeats immer, hier wird nur das Einblenden gewählt."""
        await _channel_or_404(idx)
        await store.set_setting(f"channel_view_{idx}", {"show_details": body.show_details})
        return {"idx": idx, "show_details": body.show_details}

    @r.get("/channels/{idx}/secret")
    async def channel_secret(idx: int) -> dict:
        ch = await _channel_or_404(idx)
        return {"name": ch["name"], "secret": ch["secret"], "hashtag": ch["hashtag"]}

    @r.get("/channels/{idx}/messages")
    async def channel_messages(idx: int, limit: int = 200) -> list[dict]:
        return await store.channel_messages(idx, limit)

    @r.get("/messages")
    async def messages(peer: str | None = None, limit: int = 100) -> list[dict]:
        if peer is not None:
            peer = peer.lower()
            if not all(c in "0123456789abcdef" for c in peer) or not 6 <= len(peer) <= 64:
                raise HTTPException(status_code=422, detail="peer muss ein Hex-Präfix sein")
        return await store.messages(peer, limit)

    @r.post("/advert")
    async def advert(a: Advert) -> dict:
        try:
            return await node.send_advert(a.flood)
        except NodeError as e:
            raise wrap(e)

    # ---- Einstellungen des eigenen Nodes ---------------------------------------------
    def _cfg():
        if rt.nodecfg is None:
            raise HTTPException(status_code=503, detail="Node-Einstellungen sind nicht verfügbar")
        return rt.nodecfg

    def _cfg_error(e: NodeError) -> HTTPException:
        if isinstance(e, CfgNeedsConfirm):
            return HTTPException(status_code=409, detail=str(e))
        if isinstance(e, ConfigError):
            return HTTPException(status_code=422, detail=str(e))
        return HTTPException(status_code=502, detail=str(e))

    @r.get("/node/spec")
    async def node_spec() -> dict:
        return nodecfg_spec()

    @r.get("/node/settings")
    async def node_get_settings() -> dict:
        try:
            return await _cfg().get()
        except NodeError as e:
            raise _cfg_error(e)

    @r.put("/node/settings")
    async def node_put_settings(body: NodeSettingsChange) -> dict:
        try:
            return await _cfg().apply(body.changes, body.confirm)
        except NodeError as e:
            raise _cfg_error(e)

    @r.post("/node/time-sync")
    async def node_time_sync() -> dict:
        try:
            return await _cfg().sync_time()
        except NodeError as e:
            raise _cfg_error(e)

    @r.post("/node/reboot")
    async def node_reboot(body: Confirm) -> dict:
        try:
            return await _cfg().reboot(body.confirm)
        except NodeError as e:
            raise _cfg_error(e)

    # ---- Repeater verwalten --------------------------------------------------
    def _admin():
        if rt.repeater is None:
            raise HTTPException(status_code=503, detail="Repeater-Verwaltung ist nicht verfügbar")
        return rt.repeater

    def _rep_error(e: NodeError) -> HTTPException:
        if isinstance(e, NeedsConfirm):
            return HTTPException(status_code=409, detail=str(e))
        if isinstance(e, RepeaterError):
            return HTTPException(status_code=422, detail=str(e))
        return HTTPException(status_code=502, detail=str(e))

    @r.get("/repeater/catalog")
    async def rep_catalog() -> dict:
        return repeater_catalog()

    @r.get("/repeater/{public_key}")
    async def rep_status(public_key: str) -> dict:
        return _admin().status(_key(public_key))

    @r.post("/repeater/{public_key}/login")
    async def rep_login(public_key: str, body: RepeaterLogin) -> dict:
        try:
            return await _admin().login(_key(public_key), body.password, body.remember)
        except NodeError as e:
            raise _rep_error(e)

    @r.post("/repeater/{public_key}/logout")
    async def rep_logout(public_key: str) -> dict:
        try:
            return await _admin().logout(_key(public_key))
        except NodeError as e:
            raise _rep_error(e)

    @r.delete("/repeater/{public_key}/password")
    async def rep_forget(public_key: str) -> dict:
        return _admin().forget_password(_key(public_key))

    @r.post("/repeater/{public_key}/command")
    async def rep_command(public_key: str, body: RepeaterCommand) -> dict:
        try:
            return await _admin().command(_key(public_key), body.command, body.confirm)
        except NodeError as e:
            raise _rep_error(e)

    @r.post("/repeater/{public_key}/setting")
    async def rep_setting(public_key: str, body: RepeaterSetting) -> dict:
        try:
            return await _admin().setting(_key(public_key), body.name, body.value, body.confirm)
        except NodeError as e:
            raise _rep_error(e)

    @r.get("/repeater/{public_key}/setting/{name}")
    async def rep_read_setting(public_key: str, name: str) -> dict:
        try:
            return await _admin().read_setting(_key(public_key), name)
        except NodeError as e:
            raise _rep_error(e)

    @r.get("/repeater/{public_key}/neighbours")
    async def rep_neighbours(public_key: str) -> dict:
        try:
            return await _admin().neighbours(_key(public_key))
        except NodeError as e:
            raise _rep_error(e)

    @r.get("/repeater/{public_key}/acl")
    async def rep_acl(public_key: str) -> dict:
        try:
            return await _admin().acl(_key(public_key))
        except NodeError as e:
            raise _rep_error(e)

    @r.get("/repeater/{public_key}/history")
    async def rep_history(public_key: str) -> list[dict]:
        return _admin().history(_key(public_key))

    # ---- Knoten (Repeater, Rooms, Sensoren) ------------------------------
    @r.get("/nodes")
    async def nodes() -> list[dict]:
        try:
            contacts = await node.contacts()
        except NodeError as e:
            raise wrap(e)
        batt = await store.latest_battery()
        pings = await store.latest_pings()
        out = []
        for c in contacts:
            c = dict(c)
            c["battery"] = batt.get(c["public_key"])
            c["last_ping"] = pings.get(c["public_key"])
            out.append(c)
        return out

    @r.post("/nodes/{public_key}/poll")
    async def poll(public_key: str, body: PollRequest | None = None) -> dict:
        try:
            return await auto.poll_node(_key(public_key), temperature=bool(body and body.temperature))
        except NodeError as e:
            raise wrap(e)

    @r.post("/nodes/{public_key}/ping")
    async def ping(public_key: str, body: PingRequest) -> dict:
        try:
            return await auto.ping_node(_key(public_key), body.mode)
        except NodeError as e:
            raise wrap(e)

    @r.get("/nodes/{public_key}/history")
    async def history(public_key: str, hours: int = 168) -> dict:
        key = _key(public_key)
        since = time.time() - max(1, min(hours, 24 * 90)) * 3600
        return {"battery": await store.battery_history(key, since),
                "temperature": await store.temperature_history(key, since),
                "pings": await store.ping_history(key, since)}

    # ---- Push-Benachrichtigungen ------------------------------------------
    def _push():
        if rt.push is None:
            raise HTTPException(status_code=503, detail="Push ist nicht verfügbar")
        return rt.push

    @r.get("/push/config")
    async def push_config() -> dict:
        p = _push()
        return {"public_key": await p.public_key(), "problem": p.problem}

    @r.post("/push/subscribe")
    async def push_subscribe(body: PushSubscribe) -> dict:
        try:
            await _push().subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, body.channels, body.battery, body.label)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True}

    @r.get("/push/prefs")
    async def push_prefs(endpoint: str) -> dict:
        sub = await store.push_sub(endpoint)
        modes = sub["channels"] if sub else {}
        return {"subscribed": sub is not None, "channels": sorted(modes), "modes": {str(k): v for k, v in modes.items()},
                "battery": sub["battery"] if sub else False}

    @r.put("/push/prefs")
    async def push_set_prefs(body: PushPrefs) -> dict:
        if not await store.update_push_prefs(body.endpoint, body.channels, body.battery):
            raise HTTPException(status_code=404, detail="Dieses Gerät ist nicht angemeldet")
        return {"ok": True}

    @r.post("/push/unsubscribe")
    async def push_unsubscribe(body: PushEndpoint) -> dict:
        await store.delete_push_sub(body.endpoint)
        return {"ok": True}

    @r.post("/push/test")
    async def push_test(body: PushEndpoint) -> dict:
        try:
            await _push().send_test(body.endpoint)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0]))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True}

    # ---- Akku-Seite ------------------------------------------------------
    TEMP_JOIN_S = 120   # Temperatur gehört zu einer Akku-Messung, wenn sie höchstens so weit entfernt ist

    async def _battery_selection() -> list:
        """Gespeicherte Auswahl (3 Plätze). Ohne gespeicherte Liste: der frühere Standard-Repeater (und ein früher gespeicherter Vergleich) belegen die ersten Plätze."""
        saved = await store.get_setting("battery_selection", None)
        if isinstance(saved, list):
            return (list(saved) + [None] * 3)[:3]
        return [await store.get_setting("battery_default_node", None), await store.get_setting("battery_compare_node", None), None]

    @r.get("/battery/nodes")
    async def battery_nodes() -> dict:
        """Repeater, für die eine aktive Akku-Überwachung läuft (samt letzter Werte)."""
        try:
            contacts = {c["public_key"]: c for c in await node.contacts()}
        except NodeError:
            contacts = {}
        low: dict[str, int] = {}
        for rule in await store.rules():
            if rule["kind"] != "battery_poll" or not rule["enabled"]:
                continue
            cfg = rule["config"]
            keys = ([k for k, c in contacts.items() if c.get("type") == 2]
                    if cfg["targets"] == "all_repeaters" else cfg["targets"])
            for k in keys:
                low[k] = min(low.get(k, 99999), int(cfg.get("low_mv", 3500)))
        batt, temp = await store.latest_battery(), await store.latest_temperature()
        out = [{"public_key": k, "name": contacts.get(k, {}).get("name") or k[:8],
                "low_mv": mv, "battery": batt.get(k), "temperature": temp.get(k)}
               for k, mv in low.items()]
        out.sort(key=lambda n: n["name"].lower())
        return {"nodes": out, "default": await store.get_setting("battery_default_node", None),
                "selection": await _battery_selection(),
                "poll_gap_s": max(0.0, min(float(rt.settings.battery_poll_gap_s), 300.0))}

    @r.put("/battery/default")
    async def battery_default(body: BatteryDefaultIn) -> dict:
        """Welcher Repeater auf der Akku-Seite beim Öffnen standardmäßig ausgewählt ist."""
        await store.set_setting("battery_default_node", body.public_key)
        return {"ok": True}

    @r.put("/battery/selection")
    async def battery_selection(body: BatterySelectionIn) -> dict:
        """Welche (bis zu 3) Repeater auf der Akku-Seite ausgewählt bleiben, bis sie geändert werden. Plätze: 1, 2, 3; leer = frei."""
        keys: list[str | None] = []
        for k in body.keys:
            k = _key(k) if k else None
            keys.append(None if k in keys else k)                       # derselbe Repeater nur einmal
        keys += [None] * (3 - len(keys))
        await store.set_setting("battery_selection", keys)
        return {"ok": True, "selection": keys}

    async def _battery_rows(key: str, hours: int) -> list[dict]:
        since = time.time() - max(1, min(hours, 24 * 90)) * 3600
        batt = await store.battery_history(key, since)
        temps = await store.temperature_history(key, since)
        rows, used = [], set()
        for b in batt:
            best = None
            for i, t in enumerate(temps):
                if i not in used and abs(t["ts"] - b["ts"]) <= TEMP_JOIN_S and \
                        (best is None or abs(t["ts"] - b["ts"]) < abs(temps[best]["ts"] - b["ts"])):
                    best = i
            if best is not None:
                used.add(best)
            rows.append({"ts": b["ts"], "mv": b["mv"], "temp": temps[best]["celsius"] if best is not None else None})
        rows += [{"ts": t["ts"], "mv": None, "temp": t["celsius"]} for i, t in enumerate(temps) if i not in used]
        rows.sort(key=lambda x: x["ts"])
        return rows

    @r.get("/battery/{public_key}")
    async def battery_data(public_key: str, hours: int = 168) -> dict:
        return {"rows": await _battery_rows(_key(public_key), hours)}

    @r.get("/battery/{public_key}/export.csv", response_class=PlainTextResponse)
    async def battery_csv(public_key: str, hours: int = 168) -> str:
        lines = ["zeit_utc;akku_mv;temperatur_c"]
        for row in await _battery_rows(_key(public_key), hours):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["ts"]))
            lines.append(f"{when};{'' if row['mv'] is None else row['mv']};"
                         f"{'' if row['temp'] is None else str(row['temp']).replace('.', ',')}")
        return "\n".join(lines) + "\n"

    # ---- Automatik -------------------------------------------------------
    @r.get("/automation")
    async def automation_state() -> dict:
        return {**auto.status(), "log": await store.log(100)}

    @r.post("/automation/pause")
    async def pause(body: PauseRequest) -> dict:
        await auto.set_paused(body.paused)
        return auto.status()

    @r.get("/rules")
    async def rules() -> list[dict]:
        return await store.rules()

    def _validated(body: RuleIn) -> dict:
        try:
            return validate_config(body.kind, body.config)
        except ValidationError as e:
            parts = []
            for err in e.errors():
                field = ".".join(str(p) for p in err["loc"])
                msg = err["msg"].removeprefix("Value error, ")
                parts.append(f"{field}: {msg}" if field else msg)
            raise HTTPException(status_code=422, detail="; ".join(parts))

    @r.post("/rules")
    async def add_rule(body: RuleIn) -> dict:
        cfg = _validated(body)
        rid = await store.add_rule(body.kind, body.name, cfg, body.enabled)
        return await store.rule(rid)

    @r.put("/rules/{rule_id}")
    async def update_rule(rule_id: int, body: RuleIn) -> dict:
        existing = await store.rule(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Regel nicht gefunden")
        if existing["kind"] != body.kind:
            raise HTTPException(status_code=422, detail="Regelart kann nicht geändert werden")
        cfg = _validated(body)
        await store.update_rule(rule_id, body.name, cfg, body.enabled)
        auto.reschedule(rule_id)
        return await store.rule(rule_id)

    @r.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: int) -> dict:
        if not await store.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="Regel nicht gefunden")
        auto.reschedule(rule_id)
        return {"ok": True}

    return r
