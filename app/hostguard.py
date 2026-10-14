"""Schutz gegen DNS-Rebinding und Zugriffe fremder Webseiten (Cross-Site).

Ohne Passwort kann sonst eine präparierte Webseite im Browser des Nutzers (der im LAN bzw. Tailnet ist) die App steuern:
- DNS-Rebinding: der Name der fremden Seite zeigt plötzlich auf die IP des Pi, der Browser hält das für „gleiche Herkunft“.
  Dann steht im Host-Header aber der fremde Name → nur bekannte Namen zulassen (wie im Watchdog).
- Cross-Site: eine fremde Seite schickt Formulare/Bilder/WebSockets an die App. Der Browser kennzeichnet das selbst
  (`Sec-Fetch-Site: cross-site`) bzw. schickt die fremde Herkunft im `Origin`-Kopf mit.
Hinter `tailscale serve` (Proxy) können Origin und Host verschieden sein: deshalb zählen auch die eigenen Namen des Pi
(Rechnername, name.local, name.<tailnet>.ts.net) und `allowed_hosts` als eigene Herkunft.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def host_name(header: str | None) -> str:
    """Host-Header ohne Port, klein geschrieben ("[::1]:8000" → "::1")."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def parse_allowed(value: str) -> tuple[str, ...]:
    """`allowed_hosts` aus der Konfiguration: durch Komma/Leerzeichen getrennt; ".beispiel.de" = alle Unternamen; "*" = Prüfung aus."""
    return tuple(p.strip().lower().rstrip(".") for p in str(value or "").replace(",", " ").split() if p.strip())


def _own_names() -> tuple[str, str]:
    try:
        me = socket.gethostname().lower().rstrip(".")
    except Exception:
        me = ""
    return me, me.split(".")[0]


def _is_own_name(host: str) -> bool:
    me, short = _own_names()
    return bool(me) and host in (me, me + ".local", short, short + ".local")


def host_allowed(header: str | None, extra: tuple[str, ...] = ()) -> bool:
    """IP-Adressen, localhost, *.ts.net (Tailscale), der eigene Rechnername (auch .local) und `allowed_hosts`.
    Fehlender Header: kein Browser (z. B. Werkzeuge), kein Rebinding möglich."""
    if "*" in extra:
        return True
    host = host_name(header)
    if not host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".ts.net"):
        return True
    if _is_own_name(host):
        return True
    return _in_extra(host, extra)


def _in_extra(host: str, extra: tuple[str, ...]) -> bool:
    return any(e and (host == e or (e.startswith(".") and host.endswith(e))) for e in extra)


def origin_ok(origin: str, host_header: str | None, forwarded_host: str | None = None, extra: tuple[str, ...] = ()) -> bool:
    """Gehört die Herkunft (Origin) zur eigenen Oberfläche? Streng: genau dieselbe Adresse wie im Host-Kopf (bzw.
    X-Forwarded-Host eines Proxys), oder ein eigener Name des Pi (Rechnername, name.local, name.<tailnet>.ts.net) oder
    `allowed_hosts`. NICHT reicht: irgendeine IP, localhost oder irgendein *.ts.net – Webseiten können unter einer IP oder
    über Tailscale Funnel öffentlich unter *.ts.net laufen."""
    if "*" in extra:
        return True
    try:
        parts = urlsplit(origin)
        netloc, hostname = parts.netloc.lower(), (parts.hostname or "").rstrip(".")
    except ValueError:
        return False
    if not netloc:
        return False
    for h in (host_header, forwarded_host):
        if h and netloc == h.strip().lower():
            return True
    if _is_own_name(hostname):
        return True
    _, short = _own_names()
    if short and hostname.endswith(".ts.net") and hostname.split(".")[0] == short:
        return True
    return _in_extra(hostname, extra)


def cross_site(headers, extra: tuple[str, ...] = ()) -> bool:
    """Kommt die Anfrage von einer fremden Webseite?
    - Moderne Browser kennzeichnen Abrufe selbst (Sec-Fetch-Site): „same-origin“ ist sicher die eigene Seite.
    - Sonst (WebSocket: Chromium schickt dort KEIN Sec-Fetch-Site, nur Origin; ältere Browser) entscheidet der Origin streng.
    - Ohne beide Köpfe: kein Browser (Watchdog, curl, andere Station) → erlaubt."""
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site == "cross-site":
        return True
    if site in ("same-origin", "none"):
        return False
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return False
    if origin == "null":                       # z. B. Datei, Sandbox-iframe: nie die eigene Oberfläche
        return True
    return not origin_ok(origin, headers.get("host"), headers.get("x-forwarded-host"), extra)
