"""Web-Push ohne Fremdbibliothek (nur `cryptography` und Standardbibliothek).

Umsetzung von RFC 8291 (Nachrichtenverschlüsselung, aes128gcm) und RFC 8292 (VAPID-Anmeldung).
Warum selbst: die übliche Bibliothek `pywebpush` zieht `http-ece`, das sich mit aktuellem setuptools oft nicht
installieren lässt. Der Inhalt wird für das Gerät verschlüsselt; Apple/Google/Mozilla leiten nur Bytes weiter.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
MAX_PAYLOAD = 3800            # Push-Dienste erlauben etwa 4 KB


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- Schlüssel --------------------------------------------------------------
def generate_vapid() -> dict:
    """Neues VAPID-Schlüsselpaar: privat als PEM (bleibt auf dem Gerät), öffentlich als base64url (für den Browser)."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private_pem": pem, "public_key": b64u(pub)}


def vapid_header(endpoint: str, private_pem: str, public_key: str, subject: str, now: float | None = None) -> str:
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{head}.{body}".encode()
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={public_key}"


# ---- Verschlüsselung (RFC 8291) ------------------------------------------------
def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes,
            *, as_private: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None) -> bytes:
    """Verpackt `payload` für ein Gerät (öffentlicher Schlüssel `ua_public`, 65 Byte; `auth_secret`, 16 Byte).
    `as_private`/`salt` nur für Tests mit festen Werten."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Nachricht zu lang")
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("ungültige Schlüssel des Geräts")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = as_private.public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)      # 0x02 = letzter (einziger) Datensatz
    return salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public + cipher


def validate_endpoint(endpoint: str) -> None:
    """Nur https-Adressen mit Hostnamen: schützt davor, dass der Server beliebige interne Ziele ansteuert."""
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or "." not in host or host.replace(".", "").isdigit() or host.endswith(".local") \
            or host in ("localhost",) or len(endpoint) > 1000:
        raise ValueError("ungültige Push-Adresse")


# ---- Senden -------------------------------------------------------------------
class PushGone(Exception):
    """Das Abo existiert nicht mehr (404/410) und kann gelöscht werden."""


class PushError(Exception):
    """Vorübergehender oder sonstiger Fehler beim Senden."""


def send(sub: dict, payload: dict, vapid: dict, subject: str, ttl: int = 3600, timeout: float = 10.0,
         urlopen=urllib.request.urlopen) -> None:
    """Sendet eine Benachrichtigung. Wirft PushGone (Abo löschen) oder PushError; sonst kehrt sie normal zurück."""
    endpoint = sub["endpoint"]
    validate_endpoint(endpoint)
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), b64u_decode(sub["p256dh"]), b64u_decode(sub["auth"]))
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
        "Urgency": "normal", "Authorization": vapid_header(endpoint, vapid["private_pem"], vapid["public_key"], subject)})
    try:
        with urlopen(req, timeout=timeout) as r:
            if r.status >= 300:
                raise PushError(f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise PushGone(str(e.code)) from e
        raise PushError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise PushError(str(e)) from e
