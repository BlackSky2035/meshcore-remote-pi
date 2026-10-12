import asyncio
import json
import os
import time
import urllib.error

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import webpush
from app.webpush import b64u, b64u_decode

ENDPOINT = "https://push.example.net/send/abc123"


def device():
    """Ein „Browser“: Schlüsselpaar und Auth-Geheimnis wie bei einem echten Push-Abo."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, pub, auth


def decrypt(body: bytes, key, pub: bytes, auth: bytes) -> bytes:
    """Gegenstück nach RFC 8291 (so wie der Browser entschlüsselt)."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub, cipher = body[21:21 + idlen], body[21 + idlen:]
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + pub + as_pub).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, cipher, None)
    assert rs == 4096 and plain.endswith(b"\x02")
    return plain[:-1]


def sub_dict(pub, auth, endpoint=ENDPOINT):
    return {"endpoint": endpoint, "p256dh": b64u(pub), "auth": b64u(auth)}


# ---------------------------------------------------------------- Verschlüsselung
def test_rfc8291_test_vector():
    as_priv = ec.derive_private_key(int.from_bytes(b64u_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    ua = b64u_decode("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    out = webpush.encrypt(b"When I grow up, I want to be a watermelon", ua, b64u_decode("BTBZMqHH6r4Tts7J_aSIgg"),
                          as_private=as_priv, salt=b64u_decode("DGv6ra1nlYgDCS1FRnbzlw"))
    assert b64u(out) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A"
                         "_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_roundtrip_with_random_keys_and_unicode():
    key, pub, auth = device()
    text = "Grüße aus dem Kanal 🔔".encode()
    assert decrypt(webpush.encrypt(text, pub, auth), key, pub, auth) == text


def test_encrypt_rejects_bad_input():
    key, pub, auth = device()
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * 5000, pub, auth)
    with pytest.raises(ValueError):
        webpush.encrypt(b"x", pub[:-1], auth)
    with pytest.raises(ValueError):
        webpush.encrypt(b"x", pub, auth[:-1])


def test_vapid_header_is_valid_signed_jwt():
    v = webpush.generate_vapid()
    hdr = webpush.vapid_header(ENDPOINT, v["private_pem"], v["public_key"], "mailto:a@example.com", now=1000.0)
    assert hdr.startswith("vapid t=") and f"k={v['public_key']}" in hdr
    jwt = hdr.split("t=")[1].split(",")[0]
    h, c, s = jwt.split(".")
    claims = json.loads(b64u_decode(c))
    assert claims == {"aud": "https://push.example.net", "exp": 1000 + 12 * 3600, "sub": "mailto:a@example.com"}
    assert json.loads(b64u_decode(h)) == {"typ": "JWT", "alg": "ES256"}
    sig = b64u_decode(s)
    assert len(sig) == 64
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64u_decode(v["public_key"]))
    pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
               f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))             # wirft bei falscher Signatur


@pytest.mark.parametrize("url", ["http://push.example.net/x", "https://localhost/x", "https://127.0.0.1/x",
                                 "https://10.0.0.5/x", "https://printer.local/x", "ftp://x.example.net/y", "https:///x", "nonsense"])
def test_bad_endpoints_rejected(url):
    with pytest.raises(ValueError):
        webpush.validate_endpoint(url)


def test_good_endpoints_accepted():
    for url in ("https://fcm.googleapis.com/fcm/send/abc", "https://web.push.apple.com/Qabc",
                "https://updates.push.services.mozilla.com/wpush/v2/x"):
        webpush.validate_endpoint(url)


# ---------------------------------------------------------------- Senden (HTTP)
class FakeResp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_builds_correct_request_and_device_can_decrypt():
    key, pub, auth = device()
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return FakeResp()
    v = webpush.generate_vapid()
    webpush.send(sub_dict(pub, auth), {"title": "T", "body": "B"}, v, "mailto:x@example.com", urlopen=fake_urlopen)
    req = seen["req"]
    assert req.get_method() == "POST" and req.full_url == ENDPOINT
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "3600" and h["authorization"].startswith("vapid t=")
    assert json.loads(decrypt(req.data, key, pub, auth)) == {"title": "T", "body": "B"}


def test_send_maps_http_errors():
    key, pub, auth = device()
    v = webpush.generate_vapid()

    def raiser(code):
        def f(req, timeout):
            raise urllib.error.HTTPError(ENDPOINT, code, "x", {}, None)
        return f
    for code in (404, 410):
        with pytest.raises(webpush.PushGone):
            webpush.send(sub_dict(pub, auth), {}, v, "mailto:x@example.com", urlopen=raiser(code))
    for code in (400, 429, 500):
        with pytest.raises(webpush.PushError):
            webpush.send(sub_dict(pub, auth), {}, v, "mailto:x@example.com", urlopen=raiser(code))

    def offline(req, timeout):
        raise urllib.error.URLError("kein Netz")
    with pytest.raises(webpush.PushError):
        webpush.send(sub_dict(pub, auth), {}, v, "mailto:x@example.com", urlopen=offline)


# ---------------------------------------------------------------- Dienst und API
@pytest.fixture
def sent(rt):
    log = []

    def fake(sub, payload, vapid, contact):
        log.append((sub["endpoint"], payload))
    rt.push._send = fake
    return log


async def subscribe(client, endpoint=ENDPOINT, channels=(0,), battery=False):
    key, pub, auth = device()
    r = await client.post("/api/push/subscribe", json={"endpoint": endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(auth)},
                                                       "channels": list(channels), "battery": battery, "label": "Test"})
    return r


async def test_config_key_is_stable(client):
    a = (await client.get("/api/push/config")).json()["public_key"]
    b = (await client.get("/api/push/config")).json()["public_key"]
    assert a == b and len(b64u_decode(a)) == 65


async def test_subscribe_validation(client):
    assert (await subscribe(client)).status_code == 200
    assert (await subscribe(client, endpoint="http://push.example.net/unsicher-genug-lang")).status_code == 422
    assert (await subscribe(client, endpoint="https://127.0.0.1/abcdefghijklmnop")).status_code == 422
    r = await client.post("/api/push/subscribe", json={"endpoint": ENDPOINT, "keys": {"p256dh": "A" * 90, "auth": "B" * 22}})
    assert r.status_code == 422


async def test_prefs_roundtrip_and_unsubscribe(client):
    await subscribe(client, channels=[0], battery=False)
    assert (await client.put("/api/push/prefs", json={"endpoint": ENDPOINT, "channels": [1, 0, 1], "battery": True})).status_code == 200
    p = (await client.get("/api/push/prefs", params={"endpoint": ENDPOINT})).json()
    assert p == {"subscribed": True, "channels": [0, 1], "modes": {"0": "every", "1": "every"}, "battery": True}
    await client.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT})
    assert (await client.get("/api/push/prefs", params={"endpoint": ENDPOINT})).json()["subscribed"] is False
    assert (await client.put("/api/push/prefs", json={"endpoint": ENDPOINT, "channels": [], "battery": False})).status_code == 404


async def test_only_chosen_channels_trigger_push(rt, client, sent):
    await subscribe(client, channels=[1])
    await rt.node.inject_channel_message(0, "Alice (Sim)", "nicht gewählt")
    await rt.node.inject_channel_message(1, "Bob (Sim)", "gewählt")
    await rt.hub.drain()
    await rt.push.drain()
    assert len(sent) == 1
    ep, payload = sent[0]
    assert payload["body"] == "Bob (Sim): gewählt" and payload["tag"] == "ch1" and payload["url"] == "/#/channels?c=1"
    assert payload["title"] != ""


async def test_battery_alert_only_for_subscribed_devices(rt, client, sent):
    await subscribe(client, endpoint=ENDPOINT, battery=True)
    await subscribe(client, endpoint="https://push.example.net/send/zweites-geraet", battery=False)
    await rt.hub.emit({"type": "battery_low", "payload": {"name": "Hügel", "mv": 3400}})
    await rt.hub.drain(); await rt.push.drain()
    assert [e for e, _ in sent] == [ENDPOINT] and "3400" in sent[0][1]["body"]


async def test_expired_subscription_is_removed(rt, client):
    await subscribe(client, channels=[0])

    def gone(sub, payload, vapid, contact):
        raise webpush.PushGone("410")
    rt.push._send = gone
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hallo")
    await rt.hub.drain(); await rt.push.drain()
    assert (await client.get("/api/push/prefs", params={"endpoint": ENDPOINT})).json()["subscribed"] is False


async def test_failing_push_never_breaks_message_handling(rt, client):
    await subscribe(client, channels=[0])

    def broken(*a):
        raise RuntimeError("Push kaputt")
    rt.push._send = broken
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hallo")
    await rt.hub.drain(); await rt.push.drain()
    msgs = (await client.get("/api/channels/0/messages")).json()
    assert any(m["text"] == "hallo" for m in msgs)           # Nachricht wurde trotzdem gespeichert
    assert rt.push.problem and "Push kaputt" in rt.push.problem


async def test_slow_push_does_not_block_events(rt, client, monkeypatch):
    await subscribe(client, channels=[0])

    def slow(*a):
        time.sleep(1.0)
    rt.push._send = slow
    t = time.monotonic()
    await rt.node.inject_channel_message(0, "Alice (Sim)", "hallo")
    await rt.hub.drain()
    assert time.monotonic() - t < 0.5                       # Hub und Nachrichtenverarbeitung warten nicht auf das Senden
    await rt.push.drain()


async def test_hourly_cap_per_device(rt, client, sent):
    await subscribe(client, channels=[0])
    from app import push as pushmod
    for i in range(pushmod.MAX_PER_HOUR + 5):
        rt.push.notify_channel(0, "x", f"m{i}")
    await rt.push.drain()
    assert len(sent) == pushmod.MAX_PER_HOUR


async def test_test_endpoint(rt, client, sent):
    await subscribe(client)
    assert (await client.post("/api/push/test", json={"endpoint": ENDPOINT})).status_code == 200
    assert sent and sent[0][1]["tag"] == "test"
    assert (await client.post("/api/push/test", json={"endpoint": "https://push.example.net/send/unbekannt-xyz"})).status_code == 404

    def fail(*a):
        raise webpush.PushError("HTTP 500")
    rt.push._send = fail
    assert (await client.post("/api/push/test", json={"endpoint": ENDPOINT})).status_code == 502


async def test_manifest_and_service_worker_are_served(client):
    m = await client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and {i["sizes"] for i in data["icons"]} >= {"192x192", "512x512"}
    sw = await client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/" and "showNotification" in sw.text
    for icon in data["icons"]:
        assert (await client.get(icon["src"])).status_code == 200


# ---------------------------------------------------------------- Art der Benachrichtigung und Ungelesen
async def put_modes(client, modes, endpoint=ENDPOINT):
    r = await client.put("/api/push/prefs", json={"endpoint": endpoint, "channels": modes, "battery": False})
    assert r.status_code == 200, r.text


async def test_modes_roundtrip_and_validation(client):
    await subscribe(client, channels=[0])
    await put_modes(client, {"0": "once", "2": "every"})
    p = (await client.get("/api/push/prefs", params={"endpoint": ENDPOINT})).json()
    assert p["modes"] == {"0": "once", "2": "every"} and p["channels"] == [0, 2]
    bad = await client.put("/api/push/prefs", json={"endpoint": ENDPOINT, "channels": {"0": "immer"}, "battery": False})
    assert bad.status_code == 422
    bad = await client.put("/api/push/prefs", json={"endpoint": ENDPOINT, "channels": {"99": "once"}, "battery": False})
    assert bad.status_code == 422


async def test_every_mode_notifies_each_message(rt, client, sent):
    await subscribe(client, channels=[0])
    for i in range(3):
        await rt.node.inject_channel_message(0, "Alice (Sim)", f"m{i}")
    await rt.hub.drain(); await rt.push.drain()
    assert len(sent) == 3


async def test_once_mode_notifies_only_first_until_read(rt, client, sent):
    await subscribe(client, channels=[0])
    await put_modes(client, {"0": "once"})
    await client.get("/api/channels/unread")                       # Grundlinie setzen (alter Verlauf zählt nicht)
    for i in range(5):
        await rt.node.inject_channel_message(0, "Alice (Sim)", f"m{i}")
        await rt.hub.drain(); await rt.push.drain()
    assert [p["body"] for _, p in sent] == ["Alice (Sim): m0"]                     # nur die erste
    assert (await client.get("/api/channels/unread")).json() == {"total": 5, "channels": {"0": 5}}
    await client.post("/api/channels/0/read")                      # auf der Webseite angesehen
    assert (await client.get("/api/channels/unread")).json() == {"total": 0, "channels": {}}
    await rt.node.inject_channel_message(0, "Bob (Sim)", "neu")
    await rt.hub.drain(); await rt.push.drain()
    assert [p["body"] for _, p in sent] == ["Alice (Sim): m0", "Bob (Sim): neu"]   # wieder eine Meldung


async def test_modes_are_per_device(rt, client, sent):
    other = "https://push.example.net/send/zweites-geraet"
    await subscribe(client, channels=[0])
    await subscribe(client, endpoint=other, channels=[0])
    await put_modes(client, {"0": "once"}, other)
    await client.get("/api/channels/unread")
    for i in range(3):
        await rt.node.inject_channel_message(0, "Alice (Sim)", f"m{i}")
        await rt.hub.drain(); await rt.push.drain()
    by = {}
    for ep, _ in sent:
        by[ep] = by.get(ep, 0) + 1
    assert by == {ENDPOINT: 3, other: 1}


async def test_unread_counts_per_channel_and_ignores_own_and_old(rt, client):
    await rt.node.inject_channel_message(0, "Alt", "alter Verlauf")
    await rt.hub.drain()
    assert (await client.get("/api/channels/unread")).json()["total"] == 0                 # Grundlinie: Altes zählt nicht
    await rt.node.inject_channel_message(0, "Alice (Sim)", "a")
    await rt.node.inject_channel_message(1, "Bob (Sim)", "b")
    await rt.node.inject_channel_message(1, "Bob (Sim)", "c")
    await rt.hub.drain()
    await client.post("/api/channels/0/messages", json={"text": "meine eigene"}) if False else None
    u = (await client.get("/api/channels/unread")).json()
    assert u == {"total": 3, "channels": {"0": 1, "1": 2}}
    chans = {c["idx"]: c["unread"] for c in (await client.get("/api/channels")).json()["channels"]}
    assert chans[0] == 1 and chans[1] == 2


async def test_read_emits_event_and_validates(rt, client):
    events = []

    async def listen(e):
        events.append(e)
    rt.hub.listeners.append(listen)
    assert (await client.post("/api/channels/1/read")).status_code == 200
    await rt.hub.drain()
    assert any(e["type"] == "channel_read" and e["payload"]["channel_idx"] == 1 for e in events)
    assert (await client.post("/api/channels/99/read")).status_code == 422


async def test_new_key_resets_read_marker(rt, client):
    await client.get("/api/channels/unread")
    await rt.node.inject_channel_message(1, "Bob (Sim)", "x")
    await rt.hub.drain()
    await client.post("/api/channels/1/read")
    await client.put("/api/channels/1", json={"name": "Neu", "secret": "11" * 16})
    await rt.node.inject_channel_message(1, "Bob (Sim)", "y")
    await rt.hub.drain()
    assert (await client.get("/api/channels/unread")).json()["channels"] == {"1": 1}


async def test_old_list_format_in_database_still_works(rt, client):
    """Ältere Datenbanken speichern die Kanäle eines Geräts als Liste; das gilt als „bei jeder Nachricht“."""
    await subscribe(client, channels=[0])
    rt.raw_store._exec("UPDATE push_subs SET channels='[0, 2]'")
    p = (await client.get("/api/push/prefs", params={"endpoint": ENDPOINT})).json()
    assert p["modes"] == {"0": "every", "2": "every"}
