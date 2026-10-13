"""SQLite-Speicher für Nachrichten, Messwerte, Regeln und Einstellungen.

Bewusst einfach: eine Datei, kurze Zugriffe, ein Lock. Die async-API ruft die
synchronen Methoden über `asyncio.to_thread` auf. Schreibzugriffe sind selten
und klein, damit die SD-Karte des Pi geschont wird (WAL-Modus, kein fsync pro Zeile).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,          -- in | out
    kind TEXT NOT NULL,               -- direct | channel
    peer TEXT NOT NULL,               -- Public Key (Hex, ggf. nur Präfix) oder Kanalnummer
    name TEXT,
    text TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_peer_ts ON messages(peer, ts);

CREATE TABLE IF NOT EXISTS msg_repeats (       -- gehörte Wiederholungen unserer Kanalnachrichten
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    path TEXT NOT NULL,               -- Hop-Kennungen als Hex (ggf. leer = direkt gehört)
    path_hash_size INTEGER NOT NULL DEFAULT 1,
    snr REAL,
    rssi REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_repeats_unique ON msg_repeats(message_id, path);

CREATE TABLE IF NOT EXISTS battery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    public_key TEXT NOT NULL,
    mv INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_battery_key_ts ON battery(public_key, ts);

CREATE TABLE IF NOT EXISTS temperature (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    public_key TEXT NOT NULL,
    celsius REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temperature_key_ts ON temperature(public_key, ts);

CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    public_key TEXT NOT NULL,
    ok INTEGER NOT NULL,
    rtt_ms REAL,
    mode TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pings_key_ts ON pings(public_key, ts);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- battery_poll | ping | channel_ping | auto_reply | advert
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL              -- JSON
);

CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    channels TEXT NOT NULL DEFAULT '[]',   -- JSON: Kanalplätze, für die dieses Gerät Push bekommt
    battery INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usb_events (          -- Zustandswechsel des USB-Nodes
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    up INTEGER NOT NULL,              -- 1 = ok, 0 = nicht verfügbar
    state TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS room_posts (          -- Raum-Bot: Beiträge (unbegrenzt, nur durch max_posts gekappt)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    author_key TEXT NOT NULL,         -- Public Key, oder 'web' für Beiträge von der Webseite
    author TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS room_members (        -- wer den Raum benutzt hat
    public_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    last_seen_id INTEGER NOT NULL DEFAULT 0,    -- bis zu diesem Beitrag ist alles zugestellt/gelesen
    subscribed INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS radio_stats (         -- Zählerstände des eigenen Nodes (laufend, setzen sich beim Neustart zurück)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    uptime INTEGER, noise_floor INTEGER, tx_air INTEGER, rx_air INTEGER,
    recv INTEGER, sent INTEGER, flood_tx INTEGER, direct_tx INTEGER, flood_rx INTEGER, direct_rx INTEGER, recv_errors INTEGER, queue_len INTEGER
);
CREATE INDEX IF NOT EXISTS idx_radio_stats_ts ON radio_stats(ts);
CREATE TABLE IF NOT EXISTS sensor_readings (     -- alle Zahlenwerte aus Telemetrie-Antworten (Cayenne LPP)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    public_key TEXT NOT NULL,
    type TEXT NOT NULL,
    channel INTEGER NOT NULL DEFAULT 0,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensor_key_ts ON sensor_readings(public_key, type, channel, ts);
CREATE TABLE IF NOT EXISTS neighbour_reports (   -- Nachbarlisten, die Repeater gemeldet haben
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    repeater TEXT NOT NULL,           -- Public Key des meldenden Repeaters
    prefix TEXT NOT NULL,             -- Anfang des Public Keys des Nachbarn
    snr REAL
);
CREATE TABLE IF NOT EXISTS automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    rule_id INTEGER,
    level TEXT NOT NULL,              -- info | warn | error
    text TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Ältere Datenbanken um neue Spalten ergänzen (nur additiv)."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(pings)")}
        for col in ("snr_out", "snr_in"):
            if col not in have:
                self._db.execute(f"ALTER TABLE pings ADD COLUMN {col} REAL")
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(messages)")}
        for col, typ in (("sender_ts", "INTEGER"), ("path", "TEXT"), ("path_hash_size", "INTEGER"),
                         ("hops", "INTEGER"), ("snr", "REAL"), ("rssi", "REAL")):
            if col not in have:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")

    # ---- allgemein -------------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur.lastrowid

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- Nachrichten -----------------------------------------------------
    def add_message(self, direction: str, kind: str, peer: str, text: str,
                    name: str | None = None, auto: bool = False, ts: float | None = None,
                    sender_ts: int | None = None, path: str | None = None,
                    path_hash_size: int | None = None, hops: int | None = None,
                    snr: float | None = None, rssi: float | None = None) -> int:
        return self._exec(
            "INSERT INTO messages(ts, direction, kind, peer, name, text, auto, sender_ts, path, "
            "path_hash_size, hops, snr, rssi) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), direction, kind, peer, name, text, int(auto), sender_ts, path,
             path_hash_size, hops, snr, rssi),
        )

    def search_messages(self, query: str, kind: str | None = None, limit: int = 100) -> list[dict]:
        """Textsuche ohne Groß-/Kleinschreibung (Teilwort). Neueste zuerst."""
        q = query.strip()
        if not q:
            return []
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT id, ts, direction, kind, peer, name, text, auto FROM messages WHERE text LIKE ? ESCAPE '\\'"
        params: list = [f"%{esc}%"]
        if kind in ("direct", "channel"):
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return self._all(sql, tuple(params))

    def find_sent_channel_message(self, idx: int, sender_ts: int) -> dict | None:
        rows = self._all("SELECT * FROM messages WHERE kind='channel' AND direction='out' AND peer=? "
                         "AND sender_ts=? ORDER BY id DESC LIMIT 1", (str(idx), sender_ts))
        return rows[0] if rows else None

    def add_repeat(self, message_id: int, path: str, path_hash_size: int = 1,
                   snr: float | None = None, rssi: float | None = None) -> bool:
        """Neue gehörte Wiederholung. False, wenn derselbe Weg schon gespeichert ist."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO msg_repeats(message_id, ts, path, path_hash_size, snr, rssi) "
                "VALUES(?,?,?,?,?,?)", (message_id, time.time(), path, path_hash_size, snr, rssi))
            self._db.commit()
            return cur.rowcount > 0

    def channel_messages(self, idx: int, limit: int = 200) -> list[dict]:
        limit = max(1, min(limit, 1000))
        rows = list(reversed(self._all(
            "SELECT * FROM messages WHERE kind='channel' AND peer=? ORDER BY ts DESC, id DESC LIMIT ?",
            (str(idx), limit))))
        ids = [r["id"] for r in rows if r["direction"] == "out"]
        by_msg: dict[int, list[dict]] = {i: [] for i in ids}
        if ids:
            marks = ",".join("?" * len(ids))
            for rep in self._all(f"SELECT message_id, ts, path, path_hash_size, snr, rssi FROM msg_repeats "
                                 f"WHERE message_id IN ({marks}) ORDER BY ts, id", tuple(ids)):
                by_msg[rep.pop("message_id")].append(rep)
        for r in rows:
            if r["direction"] == "out":
                r["repeats"] = by_msg[r["id"]]
        return rows

    def recent_incoming_paths(self, limit: int = 300) -> list[dict]:
        """Zuletzt tatsächlich beobachtete Wege eingehender Nachrichten (bisher nur Kanäle: Direktnachrichten
        speichern den Pfad noch nicht). Für die Netz-Grafik zusätzlich zu den vom Node selbst gemeldeten
        Pfaden - zeigt auch Repeater, über die Nachrichten ankommen, auch wenn der Node (noch) keinen
        eigenen Pfad zum jeweiligen Absender gelernt hat."""
        limit = max(1, min(limit, 2000))
        return self._all(
            "SELECT path, path_hash_size FROM messages WHERE direction='in' AND path IS NOT NULL "
            "AND path != '' ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def last_channel_activity(self) -> dict[str, float]:
        return {r["peer"]: r["ts"] for r in self._all(
            "SELECT peer, MAX(ts) AS ts FROM messages WHERE kind='channel' GROUP BY peer")}

    # ---- Gelesen/ungelesen (Kanäle) --------------------------------------
    def _read_markers(self) -> tuple[int, dict[int, int]]:
        """(Grundlinie, Marker je Kanal). Die Grundlinie wird beim ersten Aufruf auf die aktuell höchste
        Nachrichten-ID gesetzt: alter Verlauf zählt nicht als ungelesen."""
        base = self.get_setting("unread_baseline")
        if base is None:
            rows = self._all("SELECT MAX(id) AS m FROM messages")
            base = int(rows[0]["m"] or 0)
            self.set_setting("unread_baseline", base)
        marks = {}
        for r in self._all("SELECT key, value FROM settings WHERE key LIKE 'channel_read_%'"):
            try:
                marks[int(r["key"].rsplit("_", 1)[1])] = int(json.loads(r["value"]))
            except (ValueError, TypeError):
                continue
        return int(base), marks

    def channel_unread(self) -> dict[int, int]:
        """Anzahl ungelesener eingehender Nachrichten je Kanalplatz (nur Plätze mit mindestens einer)."""
        base, marks = self._read_markers()
        out = {}
        for r in self._all("SELECT peer, id FROM messages WHERE kind='channel' AND direction='in'"):
            try:
                idx = int(r["peer"])
            except ValueError:
                continue
            if r["id"] > marks.get(idx, base):
                out[idx] = out.get(idx, 0) + 1
        return out

    def channel_first_unread(self, idx: int) -> int | None:
        """ID der ältesten ungelesenen eingehenden Nachricht eines Kanalplatzes (None = keine)."""
        base, marks = self._read_markers()
        rows = self._all("SELECT MIN(id) AS m FROM messages WHERE kind='channel' AND direction='in' AND peer=? AND id>?",
                         (str(idx), marks.get(idx, base)))
        return rows[0]["m"] if rows and rows[0]["m"] is not None else None

    def mark_channel_read(self, idx: int) -> int:
        """Markiert den Kanal als gelesen und liefert die ALTE Markierung (Nachrichten-ID, bis zu der vorher gelesen war).
        Die Webseite setzt damit „Ungelesene Nachrichten“ genau an die erste neuere eingehende Nachricht."""
        base, marks = self._read_markers()              # stellt auch die Grundlinie sicher
        before = marks.get(idx, base)
        rows = self._all("SELECT MAX(id) AS m FROM messages WHERE kind='channel' AND peer=?", (str(idx),))
        self.set_setting(f"channel_read_{idx}", max(int(rows[0]["m"] or 0), before))
        return before

    def delete_channel_messages(self, idx: int) -> int:
        with self._lock:
            self._db.execute("DELETE FROM msg_repeats WHERE message_id IN "
                             "(SELECT id FROM messages WHERE kind='channel' AND peer=?)", (str(idx),))
            cur = self._db.execute("DELETE FROM messages WHERE kind='channel' AND peer=?", (str(idx),))
            self._db.commit()
            return cur.rowcount

    def messages(self, peer: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(limit, 1000))
        if peer:
            rows = self._all(
                "SELECT * FROM messages WHERE peer LIKE ? ORDER BY ts DESC, id DESC LIMIT ?",
                (peer[:12] + "%", limit),
            )
        else:
            rows = self._all("SELECT * FROM messages ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    # ---- Messwerte -------------------------------------------------------
    def add_battery(self, public_key: str, mv: int, ts: float | None = None) -> None:
        self._exec("INSERT INTO battery(ts, public_key, mv) VALUES(?,?,?)",
                   (ts or time.time(), public_key, int(mv)))

    def battery_history(self, public_key: str, since: float) -> list[dict]:
        return self._all(
            "SELECT ts, mv FROM battery WHERE public_key=? AND ts>=? ORDER BY ts",
            (public_key, since),
        )

    def add_temperature(self, public_key: str, celsius: float, ts: float | None = None) -> None:
        self._exec("INSERT INTO temperature(ts, public_key, celsius) VALUES(?,?,?)",
                   (ts or time.time(), public_key, celsius))

    def temperature_history(self, public_key: str, since: float) -> list[dict]:
        return self._all("SELECT ts, celsius FROM temperature WHERE public_key=? AND ts>=? ORDER BY ts",
                         (public_key, since))

    def latest_temperature(self) -> dict[str, dict]:
        rows = self._all(
            "SELECT t.public_key, t.ts, t.celsius FROM temperature t "
            "JOIN (SELECT public_key, MAX(ts) AS m FROM temperature GROUP BY public_key) x "
            "ON t.public_key=x.public_key AND t.ts=x.m")
        return {r["public_key"]: {"ts": r["ts"], "celsius": r["celsius"]} for r in rows}

    def latest_battery(self) -> dict[str, dict]:
        rows = self._all(
            "SELECT b.public_key, b.ts, b.mv FROM battery b "
            "JOIN (SELECT public_key, MAX(ts) AS m FROM battery GROUP BY public_key) x "
            "ON b.public_key=x.public_key AND b.ts=x.m"
        )
        return {r["public_key"]: {"ts": r["ts"], "mv": r["mv"]} for r in rows}

    def add_ping(self, public_key: str, ok: bool, rtt_ms: float | None, mode: str,
                 ts: float | None = None, snr_out: float | None = None,
                 snr_in: float | None = None) -> None:
        self._exec("INSERT INTO pings(ts, public_key, ok, rtt_ms, mode, snr_out, snr_in) VALUES(?,?,?,?,?,?,?)",
                   (ts or time.time(), public_key, int(ok), rtt_ms, mode, snr_out, snr_in))

    def ping_history(self, public_key: str, since: float) -> list[dict]:
        return self._all(
            "SELECT ts, ok, rtt_ms, mode, snr_out, snr_in FROM pings WHERE public_key=? AND ts>=? ORDER BY ts",
            (public_key, since),
        )

    def latest_pings(self) -> dict[str, dict]:
        rows = self._all(
            "SELECT p.public_key, p.ts, p.ok, p.rtt_ms, p.mode, p.snr_out, p.snr_in FROM pings p "
            "JOIN (SELECT public_key, MAX(ts) AS m FROM pings GROUP BY public_key) x "
            "ON p.public_key=x.public_key AND p.ts=x.m"
        )
        return {r["public_key"]: {"ts": r["ts"], "ok": bool(r["ok"]),
                                  "rtt_ms": r["rtt_ms"], "mode": r["mode"],
                                  "snr_out": r["snr_out"], "snr_in": r["snr_in"]} for r in rows}

    # ---- Regeln ----------------------------------------------------------
    @staticmethod
    def _rule(row: dict) -> dict:
        return {"id": row["id"], "kind": row["kind"], "name": row["name"],
                "enabled": bool(row["enabled"]), "config": json.loads(row["config"])}

    def rules(self) -> list[dict]:
        return [self._rule(r) for r in self._all("SELECT * FROM rules ORDER BY id")]

    def rule(self, rule_id: int) -> dict | None:
        rows = self._all("SELECT * FROM rules WHERE id=?", (rule_id,))
        return self._rule(rows[0]) if rows else None

    def add_rule(self, kind: str, name: str, config: dict, enabled: bool = True) -> int:
        return self._exec("INSERT INTO rules(kind, name, enabled, config) VALUES(?,?,?,?)",
                          (kind, name, int(enabled), json.dumps(config)))

    def update_rule(self, rule_id: int, name: str, config: dict, enabled: bool) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE rules SET name=?, enabled=?, config=? WHERE id=?",
                (name, int(enabled), json.dumps(config), rule_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    def delete_rule(self, rule_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self._db.commit()
            return cur.rowcount > 0

    # ---- Einstellungen ---------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._all("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO settings(key, value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # ---- Push-Abos (ein Eintrag je Gerät/Browser) ------------------------
    @staticmethod
    def _modes(raw) -> dict[int, str]:
        """Push-Art je Kanal: {Platz: 'every' | 'once'}. Ältere Einträge (nur Liste) bedeuten 'every'."""
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            return {}
        if isinstance(data, list):
            return {int(i): "every" for i in data if isinstance(i, int)}
        out = {}
        for k, v in (data.items() if isinstance(data, dict) else []):
            try:
                out[int(k)] = v if v in ("every", "once") else "every"
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    def _sub(cls, row: dict) -> dict:
        return {"endpoint": row["endpoint"], "p256dh": row["p256dh"], "auth": row["auth"],
                "channels": cls._modes(row["channels"]), "battery": bool(row["battery"]),
                "label": row["label"], "created": row["created"]}

    @classmethod
    def _modes_json(cls, channels) -> str:
        if isinstance(channels, dict):
            modes = {int(k): (v if v in ("every", "once") else "every") for k, v in channels.items()}
        else:
            modes = {int(i): "every" for i in channels}
        return json.dumps({str(k): modes[k] for k in sorted(modes)})

    def push_subs(self) -> list[dict]:
        return [self._sub(r) for r in self._all("SELECT * FROM push_subs ORDER BY created")]

    def push_sub(self, endpoint: str) -> dict | None:
        rows = self._all("SELECT * FROM push_subs WHERE endpoint=?", (endpoint,))
        return self._sub(rows[0]) if rows else None

    def save_push_sub(self, endpoint: str, p256dh: str, auth: str, channels,
                      battery: bool, label: str = "") -> None:
        self._exec("INSERT INTO push_subs(endpoint, p256dh, auth, channels, battery, label, created) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, "
                   "channels=excluded.channels, battery=excluded.battery, label=excluded.label",
                   (endpoint, p256dh, auth, self._modes_json(channels), int(battery), label[:80], time.time()))

    def update_push_prefs(self, endpoint: str, channels, battery: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE push_subs SET channels=?, battery=? WHERE endpoint=?",
                                   (self._modes_json(channels), int(battery), endpoint))
            self._db.commit()
            return cur.rowcount > 0

    def delete_push_sub(self, endpoint: str) -> None:
        self._exec("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key=?", (key,))

    def channel_views(self) -> dict[int, dict]:
        """Anzeigeeinstellungen je Kanalplatz (z. B. Pfade/Repeats einblenden)."""
        out = {}
        for r in self._all("SELECT key, value FROM settings WHERE key LIKE 'channel_view_%'"):
            try:
                out[int(r["key"].rsplit("_", 1)[1])] = json.loads(r["value"])
            except (ValueError, IndexError):
                pass
        return out

    # ---- Automatik-Log ---------------------------------------------------
    def add_log(self, level: str, text: str, rule_id: int | None = None) -> None:
        self._exec("INSERT INTO automation_log(ts, rule_id, level, text) VALUES(?,?,?,?)",
                   (time.time(), rule_id, level, text))

    def log(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(limit, 500))
        return list(reversed(self._all(
            "SELECT * FROM automation_log ORDER BY id DESC LIMIT ?", (limit,))))

    # ---- USB-Ereignisse --------------------------------------------------
    def add_usb_event(self, ts: float, up: bool, state: str, text: str) -> None:
        self._exec("INSERT INTO usb_events(ts, up, state, text) VALUES(?,?,?,?)", (ts, 1 if up else 0, state, text))

    def usb_events(self, since: float, limit: int = 200) -> list[dict]:
        """Wechsel seit `since`, älteste zuerst (die letzten `limit`)."""
        limit = max(1, min(limit, 5000))
        return list(reversed(self._all(
            "SELECT ts, up, state, text FROM usb_events WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?", (since, limit))))

    def usb_state_before(self, ts: float) -> bool | None:
        rows = self._all("SELECT up FROM usb_events WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (ts,))
        return bool(rows[0]["up"]) if rows else None

    # ---- Raum-Bot -----------------------------------------------------------
    def room_add_post(self, author_key: str, author: str, text: str, max_posts: int = 5000) -> int:
        pid = self._exec("INSERT INTO room_posts(ts, author_key, author, text) VALUES(?,?,?,?)", (time.time(), author_key, author[:40], text))
        with self._lock:
            self._db.execute("DELETE FROM room_posts WHERE id <= (SELECT MAX(id) FROM room_posts) - ?", (max(10, max_posts),))
            self._db.commit()
        return pid

    def room_posts(self, after_id: int = 0, limit: int = 100, newest: bool = False) -> list[dict]:
        limit = max(1, min(limit, 500))
        if newest:
            return list(reversed(self._all("SELECT * FROM room_posts WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit))))
        return self._all("SELECT * FROM room_posts WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))

    def room_max_id(self) -> int:
        return self._all("SELECT COALESCE(MAX(id), 0) AS m FROM room_posts")[0]["m"]

    def room_delete_post(self, post_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM room_posts WHERE id=?", (post_id,))
            self._db.commit()
            return cur.rowcount > 0

    def room_member(self, key: str, name: str | None = None) -> dict:
        """Mitglied holen; legt es beim ersten Mal an (ab dann zählen nur noch neue Beiträge als ungelesen).
        Erst „INSERT OR IGNORE“, dann lesen: zwei gleichzeitige erste Nachrichten (zwei Threads) führen so nie zu einem IntegrityError."""
        self._exec("INSERT OR IGNORE INTO room_members(public_key, name, last_seen_id, subscribed, first_seen) VALUES(?,?,?,0,?)",
                   (key, (name or key[:8])[:40], 0, time.time()))
        rows = self._all("SELECT * FROM room_members WHERE public_key=?", (key,))
        if name and rows[0]["name"] != name[:40]:
            self._exec("UPDATE room_members SET name=? WHERE public_key=?", (name[:40], key))
            rows[0]["name"] = name[:40]
        return rows[0]

    def room_members(self) -> list[dict]:
        return self._all("SELECT * FROM room_members ORDER BY first_seen")

    def room_update_member(self, key: str, last_seen_id: int | None = None, subscribed: bool | None = None) -> None:
        if last_seen_id is not None:
            self._exec("UPDATE room_members SET last_seen_id=MAX(last_seen_id, ?) WHERE public_key=?", (last_seen_id, key))
        if subscribed is not None:
            self._exec("UPDATE room_members SET subscribed=? WHERE public_key=?", (int(subscribed), key))

    def room_delete_member(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM room_members WHERE public_key=?", (key,))
            self._db.commit()
            return cur.rowcount > 0

    # ---- Funkstatistik -----------------------------------------------------
    STAT_COLS = ("uptime", "noise_floor", "tx_air", "rx_air", "recv", "sent", "flood_tx", "direct_tx", "flood_rx", "direct_rx",
                 "recv_errors", "queue_len")

    def add_radio_stats(self, row: dict, ts: float | None = None) -> None:
        vals = [row.get(c) if isinstance(row.get(c), (int, float)) else None for c in self.STAT_COLS]
        self._exec(f"INSERT INTO radio_stats(ts, {', '.join(self.STAT_COLS)}) VALUES({', '.join('?' * (len(self.STAT_COLS) + 1))})",
                   (ts or time.time(), *vals))

    def radio_stats(self, since: float, limit: int = 5000) -> list[dict]:
        return list(reversed(self._all("SELECT * FROM radio_stats WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, max(1, min(limit, 20000))))))

    # ---- Sensorwerte -------------------------------------------------------
    def add_sensor_readings(self, public_key: str, items: list[dict], ts: float | None = None) -> int:
        """Speichert alle Zahlenwerte (type, channel, value). Andere Werte (z. B. GPS-Listen) werden übersprungen."""
        rows = []
        for it in items[:64]:
            v = it.get("value") if isinstance(it, dict) else None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e9:
                continue
            rows.append((ts or time.time(), public_key, str(it.get("type", "?"))[:32], int(it.get("channel") or 0), float(v)))
        if rows:
            with self._lock:
                self._db.executemany("INSERT INTO sensor_readings(ts, public_key, type, channel, value) VALUES(?,?,?,?,?)", rows)
                self._db.commit()
        return len(rows)

    def sensor_latest(self, public_key: str) -> list[dict]:
        return self._all(
            "SELECT s.type, s.channel, s.value, s.ts FROM sensor_readings s JOIN (SELECT type, channel, MAX(id) AS m FROM sensor_readings "
            "WHERE public_key=? GROUP BY type, channel) x ON s.id=x.m ORDER BY s.type, s.channel", (public_key,))

    def sensor_history(self, public_key: str, type_: str, channel: int, since: float) -> list[dict]:
        return self._all("SELECT ts, value FROM sensor_readings WHERE public_key=? AND type=? AND channel=? AND ts>=? ORDER BY ts LIMIT 5000",
                         (public_key, type_, channel, since))

    # ---- Nachbarlisten (Netzkarte) ---------------------------------------
    def add_neighbour_report(self, ts: float, repeater: str, items: list[dict]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM neighbour_reports WHERE repeater = ?", (repeater,))     # nur die neueste Meldung je Repeater
            self._db.executemany("INSERT INTO neighbour_reports(ts, repeater, prefix, snr) VALUES(?,?,?,?)",
                                 [(ts, repeater, str(i["prefix"])[:24], i.get("snr")) for i in items[:200]])
            self._db.commit()

    def neighbour_reports(self) -> list[dict]:
        return self._all("SELECT ts, repeater, prefix, snr FROM neighbour_reports ORDER BY repeater, id")

    # ---- Aufräumen -------------------------------------------------------
    def prune(self, retention_days: int) -> int:
        """Löscht alte Einträge; gibt die Zahl gelöschter Zeilen zurück."""
        cutoff = time.time() - retention_days * 86400
        total = 0
        with self._lock:
            for table in ("messages", "battery", "temperature", "pings", "automation_log", "usb_events", "neighbour_reports", "sensor_readings", "radio_stats"):
                cur = self._db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                total += cur.rowcount
            self._db.execute("DELETE FROM msg_repeats WHERE message_id NOT IN (SELECT id FROM messages)")
            self._db.commit()
        return total


class AsyncStorage:
    """Dünner async-Wrapper: jede Methode von Storage als awaitable."""

    def __init__(self, storage: Storage):
        self._s = storage

    def __getattr__(self, name: str):
        attr = getattr(self._s, name)
        if not callable(attr):
            return attr

        async def call(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        return call
