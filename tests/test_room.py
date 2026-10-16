"""Raum-Bot: Befehle, Grenzen, Zustellung, API, Ausfälle."""
import pytest

from app import room as R
from app.automation import AUTO_PREFIX, SendBudget
from tests.conftest import ALICE, BOB, REPEATER_OK, FakeClock


@pytest.fixture
async def room(rt):
    clock = FakeClock()
    rt.room._clock = clock
    rt.room.tick = lambda s=30: clock.advance(s)

    async def fake_sleep(sec):                                       # Wartezeit gemerkter Befehle: nur die Testuhr vorstellen
        clock.advance(sec)
    rt.room._sleep = fake_sleep
    await rt.room.set_config(True, "Raum", 5000)
    return rt.room


async def say(rt, who, text):
    await rt.node.inject_message(who, text)
    await rt.hub.drain()


def texts(rt, to=None):
    return [t for k, t in rt.node.sent if to is None or k == to]


async def test_post_new_history_and_prefix(rt, room):
    await say(rt, ALICE, "/p Hallo zusammen")
    assert await rt.store.room_posts() and texts(rt, ALICE)[0].startswith(AUTO_PREFIX) and "[1]" in texts(rt, ALICE)[0]
    await say(rt, BOB, "/neu")
    assert any("@[Alice (Sim)] Hallo zusammen" in t for t in texts(rt, BOB))                    # Autor als echte Erwähnung
    assert any(f"[1 {__import__('time').strftime('%H:%M')}]" in t for t in texts(rt, BOB))      # Uhrzeit, zu der der Beitrag ankam
    room.tick(R.COOLDOWN_S)                                      # 20 s Pause zwischen Befehlen je Mitglied
    await say(rt, BOB, "/neu")                                   # nichts Neues mehr
    assert "keine neuen" in texts(rt, BOB)[-1]
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/verlauf 5")
    assert "Hallo zusammen" in texts(rt, BOB)[-1]


async def test_ignores_unknown_disabled_repeaters_and_plain_text(rt, room):
    await say(rt, ALICE, "ganz normaler Text")
    await say(rt, REPEATER_OK, "/p von Repeater")
    assert rt.node.sent == [] and await rt.store.room_posts() == []
    await room.set_config(False, "Raum", 5000)
    await say(rt, ALICE, "/p aus")
    assert rt.node.sent == [] and await rt.store.room_posts() == []


async def test_loop_protection_for_auto_prefix(rt, room):
    await say(rt, ALICE, AUTO_PREFIX + "/p Schleife")
    assert rt.node.sent == [] and await rt.store.room_posts() == []


async def test_post_validation_cooldown_and_hourly_limit(rt, room):
    await say(rt, ALICE, "/p " + "x" * 141)
    assert "zu lang" in texts(rt, ALICE)[-1] and await rt.store.room_posts() == []
    room.tick(R.COOLDOWN_S)
    await say(rt, ALICE, "/p")
    assert "leer" in texts(rt, ALICE)[-1]
    room.tick(R.COOLDOWN_S)
    await say(rt, ALICE, "/p eins")
    n = len(texts(rt, ALICE))
    await say(rt, ALICE, "/p zwei")                                    # zu früh: gemerkt, nach 5 s gespeichert (nicht mehr verworfen)
    await room.drain_pending()
    assert len(await rt.store.room_posts()) == 2 and "gespeichert" in texts(rt, ALICE)[-1]
    for i in range(R.POSTS_PER_HOUR):
        room.tick(25)
        await say(rt, ALICE, f"/p nr {i}")
    assert len(await rt.store.room_posts()) <= R.POSTS_PER_HOUR
    assert "Stundenlimit" in texts(rt, ALICE)[-1] or len(await rt.store.room_posts()) == R.POSTS_PER_HOUR


async def test_max_three_per_request_and_hint(rt, room):
    for i in range(5):
        await rt.store.room_add_post("web", "Web", f"b{i}")
    await say(rt, BOB, "/neu")
    out = texts(rt, BOB)
    assert len(out) == 4 and "noch 2 neue Beiträge (/neu)" in out[-1] and "b0" in out[0] and "b2" in out[2]
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/neu")
    assert "b3" in texts(rt, BOB)[4]


async def test_subscribe_delivery_and_limits(rt, room):
    await say(rt, BOB, "/anmelden")
    assert "angemeldet" in texts(rt, BOB)[-1]
    await say(rt, ALICE, "/p Neuigkeit")
    await room.post_from_web("von der Webseite")
    assert await room.deliver_once() == 1 and await room.deliver_once() == 1        # je Durchlauf ein Beitrag je Angemeldeten
    got = [t for t in texts(rt, BOB) if "Neuigkeit" in t or "Webseite" in t]
    assert len(got) == 2
    assert await room.deliver_once() == 0                        # nichts mehr offen
    assert not any("Neuigkeit" in t for t in texts(rt, ALICE)[1:])  # Autor bekommt den eigenen Beitrag nicht
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/abmelden")
    await room.post_from_web("noch einer")
    assert await room.deliver_once() == 0


async def test_subscribe_does_not_flood_with_old_posts(rt, room):
    for i in range(30):
        await rt.store.room_add_post("web", "Web", f"alt {i}")
    await say(rt, BOB, "/anmelden")
    assert await room.deliver_once() == 0


async def test_delivery_respects_budget(rt, room):
    rt.room.budget = SendBudget(0, 1)
    await say(rt, BOB, "/anmelden")                              # verbraucht das einzige Stundenkontingent
    await room.post_from_web("wartet")
    assert await room.deliver_once() == 0
    assert any("Stundenlimit" in e["text"] or "Anteil des Raums" in e["text"] for e in await rt.store.log(20))


# ---- eigenes Teilbudget: der Raum darf Akku-Überwachung und Pings nicht aushungern ---------------------------------
async def test_room_uses_at_most_a_third_of_the_hourly_budget(rt, room):
    rt.room.budget = SendBudget(0, 30)                            # Standard: 30/h → Raum höchstens 10 Plätze
    assert room.room_cap() == 10
    for i in range(20):
        await rt.store.room_add_post("web", "Web", f"b{i}")
    await say(rt, BOB, "/verlauf 5")                             # 5 bestätigte Sendungen à 3 Plätze → nur 3 passen (9 Plätze)
    assert rt.room.budget.stats()["used_last_hour"] <= 10
    assert any("Anteil des Raums" in e["text"] for e in await rt.store.log(20))
    assert await rt.room.budget.acquire()                         # für die Automatik bleibt genug übrig


async def test_unknown_commands_get_help_only_once_per_hour(rt, room):
    sent0 = len(rt.node.sent)
    await say(rt, BOB, "/quatsch")
    assert len(rt.node.sent) == sent0 + 1                        # einmal Hilfe
    room._last_cmd.clear()                                        # Befehlspause (20 s) überspringen
    await say(rt, BOB, "/nochmal")
    assert len(rt.node.sent) == sent0 + 1                        # in derselben Stunde: still


async def test_posts_survive_pruning_and_max_posts(rt, room):
    await room.set_config(True, "Raum", 100)
    for i in range(150):
        await rt.store.room_add_post("web", "Web", f"p{i}", 100)
    assert len(await rt.store.room_posts(0, 500)) == 100


async def test_api(client, rt):
    assert (await client.put("/api/room/config", json={"enabled": True, "title": "Nordraum", "max_posts": 200})).json()["title"] == "Nordraum"
    assert (await client.post("/api/room/posts", json={"text": "Gruß vom Web"})).status_code == 200
    assert (await client.post("/api/room/posts", json={"text": "x" * 101})).status_code == 422
    assert (await client.post("/api/room/posts", json={"text": "x" * 100})).status_code == 200
    assert (await client.put("/api/room/config", json={"enabled": True, "title": "x", "max_posts": 5})).status_code == 422
    st = (await client.get("/api/room")).json()
    assert st["config"]["enabled"] and st["posts"][0]["text"] == "Gruß vom Web"
    pid = st["posts"][0]["id"]
    assert (await client.delete(f"/api/room/posts/{pid}")).status_code == 200
    assert (await client.delete(f"/api/room/posts/{pid}")).status_code == 404
    await rt.node.inject_message(ALICE, "/anmelden"); await rt.hub.drain()
    assert (await client.get("/api/room")).json()["members"][0]["subscribed"] == 1
    assert (await client.delete(f"/api/room/members/{ALICE}")).status_code == 200


async def test_failures_are_contained(rt, room):
    async def boom(*a, **k): raise RuntimeError("db")
    rt.store.room_member = boom
    await say(rt, ALICE, "/p test")                              # Fehler im Bot darf nichts zerstören
    assert len(await rt.store.channel_messages(0)) == 0
    assert (await rt.store.get_setting("room_config"))["enabled"]



# ---- Darstellung: echte Erwähnung + Empfangszeit (nur wenn Platz ist) ------------------------------------------------
import time as _time

NOON = _time.mktime((2025, 6, 15, 12, 0, 0, 0, 0, -1))                  # 15.06.2025 12:00 Ortszeit
POST = {"id": 12, "ts": NOON + 120, "author_key": ALICE, "author": "Anna", "text": "Test wie geht's"}


def line(post, now=NOON + 3600, title="Raum"):
    return AUTO_PREFIX + R.Room.render(post, title, now)


def test_render_mention_and_time_today():
    assert line(POST) == "[auto] Raum [12 12:02] @[Anna] Test wie geht's"


def test_render_older_post_gets_the_date():
    old = {**POST, "ts": NOON - 3 * 86400}
    assert line(old) == "[auto] Raum [12 12.06. 12:00] @[Anna] Test wie geht's"


def test_render_drops_date_then_time_when_too_long():
    base = len("[auto] Raum [12] @[Anna] ")
    old = {**POST, "ts": NOON - 3 * 86400}
    # Platz für Uhrzeit (6 Zeichen " hh:mm") aber nicht für das Datum (+7)
    t_hm = "x" * (R.LINE_MAX - base - 6)
    assert line({**old, "text": t_hm}) == f"[auto] Raum [12 12:00] @[Anna] {t_hm}"
    # kein Platz mehr für die Uhrzeit: wie früher, ohne Zeit, Text vollständig
    t_none = "x" * (R.LINE_MAX - base - 5)
    assert line({**old, "text": t_none}) == f"[auto] Raum [12] @[Anna] {t_none}"
    assert len(line({**old, "text": t_hm})) <= R.LINE_MAX


def test_render_exact_boundary_drops_time_before_cutting_text():
    base = len("[auto] Raum [12 12:02] @[Anna] ")
    fit = {**POST, "text": "y" * (R.LINE_MAX - base)}
    assert len(line(fit)) == R.LINE_MAX and "12:02" in line(fit)                # genau 150: noch mit Uhrzeit
    one_more = {**POST, "text": "y" * (R.LINE_MAX - base + 1)}
    assert "12:02" not in line(one_more) and one_more["text"] in line(one_more)  # 151: Uhrzeit entfällt, Text bleibt ganz
    old_long = {**POST, "text": "z" * 140}                                       # älterer Beitrag aus der Zeit, als 140 erlaubt waren
    out = line(old_long)
    assert len(out.encode()) <= 150 and out.endswith("...") and out.startswith("[auto] Raum [12] @[Anna] zzz")   # gekürzt statt zu lang


def test_render_web_posts_keep_plain_author():
    web = {**POST, "author_key": "web", "author": "Web", "text": "Hallo"}
    assert line(web) == "[auto] Raum [12 12:02] Web: Hallo"                     # kein Kontakt: keine Erwähnung


def test_render_brackets_in_name_are_removed_and_empty_name_falls_back():
    assert "@[Anna Sim]" in line({**POST, "author": "Anna [Sim]"})
    assert R.mention_tag("[]") == "" and R.mention_tag("  ") == "" and R.mention_tag(None) == ""
    assert "[]:" not in line({**POST, "author": "[]"}) and "[]: " not in line({**POST, "author": "[]"})
    assert line({**POST, "author": "[]"}).endswith("Unbekannt: Test wie geht's")  # nichts übrig: einfache Form „Name: Text“, ohne Klammern


def test_render_broken_timestamp_never_raises():
    for ts in (None, "kaputt", float("nan"), 1e30):
        out = R.Room.render({**POST, "ts": ts}, "Raum", NOON)                   # darf nicht werfen
        assert "@[Anna] Test wie geht's" in out and out.startswith("Raum [12")


async def test_delivery_to_subscriber_uses_new_format(rt, room):
    await say(rt, BOB, "/anmelden")
    await say(rt, ALICE, "/p Hallo Bob")
    await room.deliver_once()
    got = [t for t in texts(rt, BOB) if "Hallo Bob" in t]
    assert got and "@[Alice (Sim)] Hallo Bob" in got[0] and len(got[0]) <= R.LINE_MAX


def test_line_limit_is_the_150_of_the_meshcore_app_counter():
    assert R.LINE_MAX == 150                                                    # Zähler „0/150“ in der MeshCore-App
    base = len("[auto] Raum [12 12:02] @[Anna] ")
    assert len(line({**POST, "text": "a" * (150 - base)})) == 150 and "12:02" in line({**POST, "text": "a" * (150 - base)})
    assert "12:02" not in line({**POST, "text": "a" * (151 - base)})


async def test_verlauf_resends_posts_that_were_already_pushed_to_a_subscriber(rt, room):
    """Gewollt: /verlauf ist eine ausdrückliche Anfrage und schickt Beiträge noch einmal, auch wenn sie angemeldeten Mitgliedern schon automatisch
    zugestellt wurden. Dadurch kommen dieselben Beiträge zweimal an (so im Betrieb beobachtet)."""
    import re
    await say(rt, BOB, "/anmelden")
    await say(rt, ALICE, "/p Test 1")
    room.tick(30)
    await say(rt, ALICE, "/p Test 2")
    for _ in range(2):
        room.tick(30)
        await room.deliver_once()
    nr = lambda: [int(re.search(r"\[(\d+)[ \]]", t).group(1)) for t in texts(rt, BOB) if "@[Alice (Sim)] Test" in t]
    assert nr() == [1, 2]                                                      # automatische Zustellung
    room.tick(30)
    await say(rt, BOB, "/verlauf 3")
    assert nr() == [1, 2, 1, 2]                                                # Verlauf schickt sie noch einmal
    room.tick(30)
    await room.deliver_once()
    assert nr() == [1, 2, 1, 2]                                                # danach nichts mehr automatisch (kein dritter Versand)



# ---- Länge: Beitrag höchstens 100, Zeile höchstens 150 Byte -----------------------------------------------------------------
def test_post_limit_is_100():
    assert R.POST_MAX == 100
    assert R.clean_text("x" * 100) == "x" * 100
    with pytest.raises(R.RoomError, match="höchstens 100"):
        R.clean_text("x" * 101)


def test_cut_to_bytes_never_splits_a_character_and_marks_the_cut():
    assert R.cut_to_bytes("kurz", 150) == "kurz"
    out = R.cut_to_bytes("ä" * 100, 51)                                          # 2 Byte je „ä“
    assert out.endswith("...") and len(out.encode()) <= 51 and out[:-3] == "ä" * ((51 - 3) // 2)
    out.encode("utf-8").decode("utf-8")                                          # gültiges UTF-8, nichts zerrissen
    assert R.cut_to_bytes("😀" * 60, 150).encode().decode()                       # 4-Byte-Zeichen: kein Fehler


def test_render_counts_bytes_not_characters():
    umlauts = {**POST, "text": "ä" * 90}                                         # 90 Zeichen, aber 180 Byte
    out = line(umlauts)
    assert len(out.encode()) <= 150 and out.endswith("...")
    assert len(out) < 150                                                        # nach Zeichen gezählt wäre es kürzer gewesen: es zählt Byte


def test_render_worst_case_names_and_title_still_fit():
    post = {"id": 54321, "ts": NOON - 9 * 86400, "author_key": ALICE, "author": "N" * 40, "text": "w" * 100}
    out = line(post, title="T" * 30)
    assert len(out.encode()) <= 150 and out.startswith("[auto] " + "T" * 30 + " [54321")


def test_every_100_char_post_fits_with_typical_names_and_time():
    for name in ("Alice_2", "Alice (Sim)", "Anna"):
        out = line({**POST, "author": name, "text": "m" * 100, "id": 1234})
        assert len(out.encode()) <= 150 and not out.endswith("...") and "12:02" in out    # 100 Zeichen passen mit Uhrzeit, ohne gekürzt zu werden


async def test_no_message_to_the_node_is_longer_than_150_bytes(rt, room):
    await rt.store.room_add_post(ALICE, "Alice (Sim)", "q" * 140, 5000)            # alter, zu langer Beitrag aus der 140er-Zeit
    await say(rt, BOB, "/neu")
    sent = [t for t in texts(rt, BOB) if "qqq" in t]
    assert sent and all(len(t.encode()) <= 150 for t in sent) and sent[0].endswith("...")


async def _subscribed_bob_with_web_post(rt, room):
    await say(rt, BOB, "/anmelden")
    rt.node.sent.clear()
    await room.post_from_web("Hallo Bob")
    room.budget.min_gap = 0


async def test_unacked_delivery_stays_unseen_and_is_retried(rt, room):
    """Fehlerbild: Der Node nahm die Nachricht an, sie kam aber nie an. Früher galt der Beitrag trotzdem als zugestellt."""
    await _subscribed_bob_with_web_post(rt, room)
    rt.node.no_ack = True
    assert await room.deliver_once() == 0
    member = [m for m in await rt.store.room_members() if m["public_key"] == BOB][0]
    assert member["last_seen_id"] < 1                                # nicht als gesehen markiert
    rt.node.no_ack = False
    room.tick(R.BACKOFF_MIN_S)                                       # Wartezeit nach dem Fehlschlag
    assert await room.deliver_once() == 1                            # nächster Durchlauf klappt
    member = [m for m in await rt.store.room_members() if m["public_key"] == BOB][0]
    assert member["last_seen_id"] == 1


async def test_unreachable_member_goes_offline_and_never_loses_a_post(rt, room):
    """Früher wurden Beiträge nach 3 unbestätigten Durchläufen übersprungen, /neu meldete danach „keine neuen Beiträge“.
    Jetzt: nichts wird übersprungen; das Mitglied gilt als offline und bekommt keine Funkversuche mehr, bis sich sein Node meldet."""
    await _subscribed_bob_with_web_post(rt, room)
    rt.node.no_ack = True
    for _ in range(R.MAX_TRIES):
        await room.deliver_once()
        room.tick(R.BACKOFF_MAX_S)
    assert member_of(await rt.store.room_members(), BOB)["last_seen_id"] == 0      # NICHT übersprungen
    assert room.is_offline(BOB)
    sent = len(rt.node.sent)
    for _ in range(5):                                                # offline: kein Funk mehr, auch nach langer Zeit
        room.tick(R.BACKOFF_MAX_S)
        await room.deliver_once()
    assert len(rt.node.sent) == sent == R.MAX_TRIES


async def test_neu_does_not_mark_unacked_posts_as_seen(rt, room):
    await say(rt, ALICE, "/p Hallo")
    rt.node.no_ack = True
    await say(rt, BOB, "/neu")
    rt.node.no_ack = False
    rt.node.sent.clear()
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/neu")
    assert any("Hallo" in t for t in texts(rt, BOB))                 # kommt beim zweiten /neu erneut


async def test_neu_and_automatic_delivery_never_send_the_same_post_twice(rt, room):
    """Fehlerbild: /neu und die automatische Zustellung liefen gleichzeitig und schickten denselben Beitrag beide."""
    import asyncio
    await say(rt, BOB, "/anmelden")
    room.budget.min_gap = 0
    for i in range(3):
        await room.post_from_web(f"Beitrag {i}")
    rt.node.sent.clear()
    room.tick(R.COOLDOWN_S)
    await asyncio.gather(room.deliver_once(), say(rt, BOB, "/neu"), room.deliver_once())
    bodies = [t for t in texts(rt, BOB) if "Beitrag" in t]
    assert len(bodies) == len(set(bodies)) == 3


# ---- Korrekturen: Budget je Sendeversuch, Wartezeit nach Fehlschlag, Pause, Befehlspause, Status je Senden ----------------------
def member_of(members, key):
    return next(m for m in members if m["public_key"] == key)


async def test_confirmed_send_reserves_one_budget_slot_per_possible_transmission(rt, room):
    """send_message(confirm=True) sendet bis zu CONFIRM_TX_MAX Mal (RealNode: send_msg_with_retry): so viele Plätze im Sendebudget."""
    assert R.CONFIRM_TX_MAX == 3
    await say(rt, BOB, "/anmelden")
    await room.post_from_web("Hallo")
    used = room.budget.stats()["used_last_hour"]
    assert await room.deliver_once() == 1
    assert room.budget.stats()["used_last_hour"] - used == R.CONFIRM_TX_MAX


async def test_confirmed_send_needs_all_slots(rt, room):
    rt.room.budget = SendBudget(0, 3)
    await say(rt, BOB, "/anmelden")                                    # 1 Platz
    await room.post_from_web("Hallo")
    rt.node.sent.clear()
    assert await room.deliver_once() == 0 and rt.node.sent == []       # 2 frei, 3 nötig: nicht senden
    assert room.budget.stats()["used_last_hour"] == 1


async def test_unreachable_member_gets_growing_backoff_and_reset_on_success(rt, room):
    await _subscribed_bob_with_web_post(rt, room)
    rt.node.no_ack = True
    await room.deliver_once()
    assert len(rt.node.sent) == 1
    await room.deliver_once()
    assert len(rt.node.sent) == 1                                      # Wartezeit: nicht alle 20 s erneut
    room.tick(R.BACKOFF_MIN_S + 1)
    await room.deliver_once()
    assert len(rt.node.sent) == 2
    room.tick(R.BACKOFF_MIN_S + 1)
    await room.deliver_once()
    assert len(rt.node.sent) == 2                                      # verdoppelt (120 s)
    room.tick(R.BACKOFF_MIN_S)
    rt.node.no_ack = False
    await room.post_from_web("zweiter")
    assert await room.deliver_once() == 1                              # klappt: Wartezeit zurückgesetzt
    assert await room.deliver_once() == 1                              # sofort der nächste Beitrag


def test_backoff_is_capped():
    r = R.Room.__new__(R.Room)
    r._backoff_s, r._backoff_until, r._clock = {}, {}, lambda: 0.0
    for _ in range(30):
        r._note_unacked("k")
    assert r._backoff_s["k"] == R.BACKOFF_MAX_S == 1800


async def test_unacked_member_does_not_stop_delivery_to_others(rt, room):
    await say(rt, BOB, "/anmelden")                                    # Bob zuerst angemeldet: kommt zuerst dran
    room.tick(30)
    await say(rt, ALICE, "/anmelden")
    await room.post_from_web("für alle")
    orig = rt.node.send_message

    async def send(key, text, confirm=False):
        res = await orig(key, text, confirm=confirm)
        return {**res, "acked": False} if confirm and key == BOB else res
    rt.node.send_message = send
    rt.node.sent.clear()
    assert await room.deliver_once() == 1
    assert [k for k, _ in rt.node.sent] == [BOB, ALICE]
    assert member_of(await rt.store.room_members(), ALICE)["last_seen_id"] == 1
    assert member_of(await rt.store.room_members(), BOB)["last_seen_id"] == 0


async def _bob_offline(rt, room):
    await _subscribed_bob_with_web_post(rt, room)
    rt.node.no_ack = True
    for _ in range(R.MAX_TRIES):
        await room.deliver_once()
        room.tick(R.BACKOFF_MAX_S)
    assert room.is_offline(BOB)
    rt.node.no_ack = False
    rt.node.sent.clear()


async def test_advert_from_offline_member_resumes_delivery(rt, room):
    await _bob_offline(rt, room)
    await rt.node.emit({"type": "advertisement", "payload": {"public_key": BOB}})
    await rt.hub.drain()
    assert not room.is_offline(BOB)
    assert await room.deliver_once() == 1                             # sofort, ohne Restwartezeit
    assert any("Hallo Bob" in t for t in texts(rt, BOB))
    assert member_of(await rt.store.room_members(), BOB)["last_seen_id"] == 1


async def test_path_update_with_prefix_also_counts_as_alive(rt, room):
    await _bob_offline(rt, room)
    await rt.node.emit({"type": "path_update", "payload": {"public_key": BOB[:12]}})
    await rt.hub.drain()
    assert not room.is_offline(BOB)


async def test_any_direct_message_from_offline_member_resumes_delivery(rt, room):
    await _bob_offline(rt, room)
    await say(rt, BOB, "bin wieder da")                               # kein Befehl, trotzdem ein Lebenszeichen
    assert not room.is_offline(BOB)
    assert await room.deliver_once() == 1


async def test_neu_after_offline_brings_the_missed_posts(rt, room):
    await _bob_offline(rt, room)
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/neu")
    assert any("Hallo Bob" in t for t in texts(rt, BOB))
    assert not any("keine neuen" in t for t in texts(rt, BOB))


async def test_short_or_unknown_key_in_event_is_ignored(rt, room):
    await _bob_offline(rt, room)
    await room.mark_alive("")
    await room.mark_alive("ab")
    await rt.node.emit({"type": "advertisement", "payload": {}})
    await rt.hub.drain()
    assert room.is_offline(BOB)


async def test_mark_alive_never_raises_when_the_database_fails(rt, room):
    await _bob_offline(rt, room)
    async def boom():
        raise RuntimeError("Datenbank weg")
    rt.store.room_members = boom
    await room.mark_alive(BOB)                                        # wirft nicht


async def test_backlog_is_capped_at_32_like_the_room_firmware(rt, room):
    await say(rt, BOB, "/anmelden")
    room.budget.min_gap = 0
    room.budget.max_per_hour = 100000
    for i in range(R.BACKLOG_MAX + 8):
        await room.post_from_web(f"B{i}")
    rt.node.sent.clear()
    assert await room.deliver_once() == 1
    first = texts(rt, BOB)[0]
    assert f"B{8}" in first and "B7 " not in first                    # die 8 ältesten zählen als gesehen, es geht mit dem 9. los
    assert member_of(await rt.store.room_members(), BOB)["last_seen_id"] == 9


async def test_offline_member_does_not_block_others(rt, room):
    await say(rt, BOB, "/anmelden")
    room.tick(30)
    await say(rt, ALICE, "/anmelden")
    room.budget.min_gap = 0
    orig = rt.node.send_message

    async def send(key, text, confirm=False):
        res = await orig(key, text, confirm=confirm)
        return {**res, "acked": False} if confirm and key == BOB else res
    rt.node.send_message = send
    for i in range(R.MAX_TRIES):
        await room.post_from_web(f"P{i}")
        await room.deliver_once()
        room.tick(R.BACKOFF_MAX_S)
    assert room.is_offline(BOB) and not room.is_offline(ALICE)
    assert member_of(await rt.store.room_members(), ALICE)["last_seen_id"] == R.MAX_TRIES


async def test_api_shows_offline_members(client, rt):
    rt.room._offline.add(BOB)
    await rt.store.room_member(BOB, "Bob")
    r = await client.get("/api/room")
    m = [x for x in r.json()["members"] if x["public_key"] == BOB][0]
    assert m["offline"] is True


async def test_room_is_silent_while_automation_is_paused(rt, room):
    await say(rt, BOB, "/anmelden")
    await room.post_from_web("wartet")
    await rt.automation.set_paused(True)
    rt.node.sent.clear()
    room.tick(30)
    await say(rt, BOB, "/hilfe")
    await say(rt, ALICE, "/p in der Pause")
    assert await room.deliver_once() == 0
    assert rt.node.sent == [] and len(await rt.store.room_posts()) == 1     # Befehle in der Pause: ignoriert, keine Antwort
    await rt.automation.set_paused(False)
    assert await room.deliver_once() == 1


async def test_commands_too_fast_are_remembered_and_run_later(rt, room):
    """Kein Befehl geht verloren. Zu früh (< 5 s) wird gemerkt, nur der letzte, und nach dem Abstand ausgeführt."""
    room._sleep = lambda s: __import__("asyncio").sleep(3600)        # Zeitgeber zunächst anhalten
    await say(rt, BOB, "/hilfe")
    for _ in range(5):
        await say(rt, BOB, "/verlauf")
    await say(rt, BOB, "/abmelden")
    assert len(texts(rt, BOB)) == 1                                    # sofort nur der erste
    await say(rt, ALICE, "/hilfe")
    assert len(texts(rt, ALICE)) == 1                                  # Abstand gilt je Mitglied
    for t in room._timers.values():
        t.cancel()
    room._timers.clear()
    room.tick(R.CMD_GAP_S)
    await room.handle_incoming(*room._pending.pop(BOB), _from_timer=True)   # was der Zeitgeber tun würde
    out = texts(rt, BOB)
    assert len(out) == 2 and "abgemeldet" in out[-1]                   # nur der letzte gemerkte Befehl lief


async def test_remembered_command_runs_after_the_gap_via_timer(rt, room):
    room.budget.min_gap = 0
    await say(rt, BOB, "/hilfe")
    await say(rt, BOB, "/abmelden")
    await room.drain_pending()
    out = texts(rt, BOB)
    assert len(out) == 2 and "abgemeldet" in out[-1]


async def test_spamming_commands_cannot_queue_up(rt, room):
    room._sleep = lambda s: __import__("asyncio").sleep(3600)
    await say(rt, BOB, "/hilfe")
    for i in range(50):
        await say(rt, BOB, f"/verlauf {i % 5 + 1}")
    assert len(room._pending) == 1 and len(room._timers) == 1           # höchstens ein gemerkter Befehl und ein Zeitgeber je Mitglied
    for t in room._timers.values():
        t.cancel()


async def test_failing_remembered_command_never_raises(rt, room):
    await say(rt, BOB, "/hilfe")
    async def boom(*a, **k):
        raise RuntimeError("kaputt")
    await say(rt, BOB, "/neu")
    room.handle_incoming = boom
    await room.drain_pending()                                          # kein Fehler nach außen


async def test_say_returns_status_per_call_even_in_parallel(rt, room):
    """Früher setzte jedes _say ein gemeinsames Merkmal `_unacked`: parallele Aufrufe überschrieben es gegenseitig."""
    import asyncio
    orig = rt.node.send_message

    async def send(key, text, confirm=False):
        await asyncio.sleep(0.02 if key == BOB else 0)
        res = await orig(key, text, confirm=confirm)
        return {**res, "acked": key != BOB}
    rt.node.send_message = send
    a, b = await asyncio.gather(room._say(BOB, "Bob", "x", confirm=True), room._say(ALICE, "Alice", "y", confirm=True))
    assert (a, b) == ("unacked", "ok") and not hasattr(room, "_unacked")
    rt.room.budget = SendBudget(0, 0)
    assert await room._say(BOB, "Bob", "z") == "error"


def test_room_member_first_contact_is_atomic():
    """Zwei gleichzeitige erste Nachrichten desselben Mitglieds: früher IntegrityError (beide lasen „gibt es nicht“ und fügten ein)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.storage import Storage
    s = Storage(":memory:")
    orig = s._all
    barrier = threading.Barrier(2, timeout=5)

    def slow(sql, params=()):
        rows = orig(sql, params)
        if "FROM room_members WHERE public_key" in sql:      # beide haben gelesen, bevor einer schreibt
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return rows
    s._all = slow
    with ThreadPoolExecutor(2) as ex:
        res = list(ex.map(lambda _: s.room_member(ALICE, "Alice"), range(2)))
    assert [r["public_key"] for r in res] == [ALICE, ALICE]
    s._all = orig
    assert len(s.room_members()) == 1
    assert s.room_member(ALICE, "Alice2")["name"] == "Alice2"
    s.close()


async def test_member_changes_notify_the_web_page(rt, room):
    """Früher: Nach /abmelden zeigte die Webseite das Mitglied weiter als angemeldet (sie lud nur bei neuen Beiträgen neu)."""
    seen = []
    orig = room.emit

    async def emit(ev):
        seen.append(ev["type"])
        await orig(ev)
    room.emit = emit
    await say(rt, BOB, "/anmelden")
    assert "room_members" in seen
    seen.clear()
    room.tick(R.COOLDOWN_S)
    await say(rt, BOB, "/abmelden")
    assert "room_members" in seen
    assert not member_of(await rt.store.room_members(), BOB)["subscribed"]


async def test_failing_web_notification_never_breaks_a_command(rt, room):
    async def boom(ev):
        raise RuntimeError("WebSocket weg")
    room.emit = boom
    await say(rt, BOB, "/anmelden")
    assert member_of(await rt.store.room_members(), BOB)["subscribed"]
    assert any("angemeldet" in t for t in texts(rt, BOB))


async def test_neu_tells_how_many_are_left_oldest_first(rt, room):
    """Bei 10 neuen kommen mit /neu 3 (älteste zuerst) und danach „noch 7“."""
    for i in range(10):
        await rt.store.room_add_post("web", "Web", f"n{i}")
    await say(rt, BOB, "/neu")
    out = texts(rt, BOB)
    assert ["n0" in out[0], "n1" in out[1], "n2" in out[2]] == [True] * 3
    assert out[3].endswith("noch 7 neue Beiträge (/neu)")
    for left in (4, 1):
        room.tick(R.COOLDOWN_S)
        rt.node.sent.clear()
        await say(rt, BOB, "/neu")
    assert texts(rt, BOB)[-1].endswith("noch 1 neuer Beitrag (/neu)")
    room.tick(R.COOLDOWN_S)
    rt.node.sent.clear()
    await say(rt, BOB, "/neu")
    assert len(texts(rt, BOB)) == 1 and "n9" in texts(rt, BOB)[0]     # letzter: keine Restzeile


async def test_own_post_does_not_hide_older_unseen_posts(rt, room):
    """Echter Fehler: ein eigener Beitrag setzte die Markierung auf sich selbst, ältere fremde, noch ungesehene galten dann als gesehen."""
    await rt.store.room_add_post("web", "Web", "vorher")
    await say(rt, BOB, "/p meins")
    room.tick(R.COOLDOWN_S)
    rt.node.sent.clear()
    await say(rt, BOB, "/neu")
    out = texts(rt, BOB)
    assert any("vorher" in t for t in out) and not any("meins" in t for t in out)


async def test_subscribing_keeps_unread_posts_of_an_existing_reader(rt, room):
    """Wer vorher mit /neu gelesen hat und noch Beiträge offen hat, verliert sie beim /anmelden nicht mehr."""
    for i in range(5):
        await rt.store.room_add_post("web", "Web", f"a{i}")
    await say(rt, BOB, "/neu")                                        # a0..a2, „noch 2“
    room.tick(R.COOLDOWN_S)
    rt.node.sent.clear()
    await say(rt, BOB, "/anmelden")
    assert "2 ungelesene folgen" in texts(rt, BOB)[-1]
    room.budget.min_gap = 0
    rt.node.sent.clear()
    await room.deliver_once()
    assert any("a3" in t for t in texts(rt, BOB))
