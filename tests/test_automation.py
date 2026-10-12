import pytest

from app.automation import AUTO_PREFIX, MENTION_RE, Automation, SendBudget
from app.node.base import NodeError

from .conftest import ALICE, REPEATER_LOW, REPEATER_OK, FakeClock


# ---------------------------------------------------------------- SendBudget
async def test_budget_hourly_cap_and_recovery():
    clock = FakeClock()
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        clock.advance(s)

    b = SendBudget(min_gap_s=0, max_per_hour=3, clock=clock, sleep=fake_sleep)
    assert [await b.acquire() for _ in range(4)] == [True, True, True, False]
    clock.advance(3601)
    assert await b.acquire() is True


async def test_budget_enforces_min_gap():
    clock = FakeClock()
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        clock.advance(s)

    b = SendBudget(min_gap_s=15, max_per_hour=100, clock=clock, sleep=fake_sleep)
    await b.acquire()
    clock.advance(5)
    await b.acquire()
    assert sleeps == [pytest.approx(10)]


# ---------------------------------------------------------------- Auto-Antwort
async def add_reply_rule(rt, **over):
    cfg = {"match": "ping", "mode": "equals", "reply": "pong {name}", "cooldown_s": 60,
           "only_from": None}
    cfg.update(over)
    return await rt.store.add_rule("auto_reply", "Pong", cfg)


async def incoming(rt, key, text):
    await rt.node.inject_message(key, text)
    await rt.hub.drain()


async def test_auto_reply_sends_prefixed_answer(rt):
    await add_reply_rule(rt)
    await incoming(rt, ALICE, "Ping")  # Groß-/Kleinschreibung egal
    assert rt.node.sent == [(ALICE, "@[Alice (Sim)] pong Alice (Sim)")]
    msgs = await rt.store.messages(ALICE)
    assert [m["direction"] for m in msgs] == ["in", "out"]
    assert msgs[1]["auto"] == 1


async def test_contains_mode_matches_whole_word_only(rt):
    """"enthält" darf nicht als Teilstring matchen: "ping" soll nicht in "pinguin" feuern."""
    await add_reply_rule(rt, match="ping", mode="contains")
    await incoming(rt, ALICE, "schau mal, ein Pinguin!")
    assert rt.node.sent == []
    await incoming(rt, ALICE, "ping")
    assert len(rt.node.sent) == 1


async def test_no_reply_to_auto_messages(rt):
    await add_reply_rule(rt, match="ping", mode="contains")
    await incoming(rt, ALICE, "[auto] ping")
    assert rt.node.sent == []


async def test_no_reply_to_messages_that_start_with_a_mention(rt):
    """Schleifenschutz für das neue Format: @[Name] am Anfang gilt ebenfalls als Auto-Antwort."""
    await add_reply_rule(rt, match="ping", mode="contains")
    await incoming(rt, ALICE, "@[Bob] ping")
    assert rt.node.sent == []


async def test_cooldown_per_sender(rt):
    await add_reply_rule(rt)
    await incoming(rt, ALICE, "ping")
    await incoming(rt, ALICE, "ping")
    assert len(rt.node.sent) == 1


async def test_paused_means_no_reply(rt):
    await add_reply_rule(rt)
    await rt.automation.set_paused(True)
    await incoming(rt, ALICE, "ping")
    assert rt.node.sent == []
    await rt.automation.set_paused(False)
    await incoming(rt, ALICE, "ping")
    assert len(rt.node.sent) == 1


async def test_pause_survives_restart(rt):
    await rt.automation.set_paused(True)
    other = Automation(rt.node, rt.store, rt.hub.emit, rt.automation.budget)
    await other.start()
    try:
        assert other.paused is True
    finally:
        await other.stop()


async def test_unknown_sender_gets_no_reply(rt):
    await add_reply_rule(rt)
    await incoming(rt, "99" * 32, "ping")
    assert rt.node.sent == []


async def test_only_from_filter(rt):
    await add_reply_rule(rt, only_from=["b2" * 32])
    await incoming(rt, ALICE, "ping")
    assert rt.node.sent == []


async def test_non_plain_text_ignored(rt):
    await add_reply_rule(rt)
    await rt.automation.handle_incoming(
        {"text": "ping", "txt_type": 1, "pubkey_prefix": ALICE[:12]},
        (await rt.node.contacts())[0],
    )
    assert rt.node.sent == []


async def test_hourly_limit_blocks_reply_and_logs(rt):
    rt.automation.budget = SendBudget(min_gap_s=0, max_per_hour=0)
    await add_reply_rule(rt)
    await incoming(rt, ALICE, "ping")
    assert rt.node.sent == []
    log = await rt.store.log(10)
    assert any("Stundenlimit" in e["text"] for e in log)


async def test_reply_placeholders_do_not_allow_format_injection(rt):
    await add_reply_rule(rt, reply="hi {name} {__class__} {0}")
    await incoming(rt, ALICE, "ping")
    assert rt.node.sent[0][1].endswith("{__class__} {0}")


# ---------------------------------------------------------------- Akku-Überwachung
async def test_poll_stores_battery_and_alerts_once(rt):
    events = []
    rt.hub.listeners.append(lambda e: events.append(e) or _noop())
    await rt.automation.poll_node(REPEATER_LOW, low_mv=3700)
    await rt.automation.poll_node(REPEATER_LOW, low_mv=3700)
    await rt.hub.drain()
    low = [e for e in events if e["type"] == "battery_low"]
    assert len(low) == 1 and low[0]["payload"]["mv"] == 3620
    assert (await rt.store.latest_battery())[REPEATER_LOW]["mv"] == 3620


async def _noop():
    return None


async def test_poll_failure_is_recorded_and_raises(rt):
    rt.node.fail_status.add(REPEATER_OK)
    with pytest.raises(NodeError):
        await rt.automation.poll_node(REPEATER_OK)
    ping = (await rt.store.latest_pings())[REPEATER_OK]
    assert ping["ok"] is False


async def test_battery_rule_covers_all_repeaters_and_survives_failure(rt):
    rt.node.fail_status.add(REPEATER_OK)
    rule = {"id": 1, "kind": "battery_poll", "name": "t", "enabled": True,
            "config": {"targets": "all_repeaters", "interval_min": 60, "low_mv": 3500}}
    await rt.automation.run_rule(rule)
    latest = await rt.store.latest_battery()
    assert REPEATER_LOW in latest and REPEATER_OK not in latest
    assert any("fehlgeschlagen" in e["text"] for e in await rt.store.log(10))


# ---------------------------------------------------------------- Zeitplan
async def test_tick_delays_first_run_then_respects_interval(rt):
    clock = FakeClock()
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000), clock=clock)
    await rt.store.add_rule("battery_poll", "Akku", {
        "targets": [REPEATER_LOW], "interval_min": 10, "low_mv": 3500})

    await auto.tick()                       # plant nur
    assert await rt.store.latest_battery() == {}

    clock.advance(10)
    await auto.tick()                       # Startverzögerung noch nicht um
    assert await rt.store.latest_battery() == {}

    clock.advance(30)
    await auto.tick()                       # läuft
    assert REPEATER_LOW in await rt.store.latest_battery()
    first = (await rt.store.latest_battery())[REPEATER_LOW]["ts"]

    clock.advance(60)
    await auto.tick()                       # Intervall (10 min) noch nicht um
    assert (await rt.store.latest_battery())[REPEATER_LOW]["ts"] == first


async def test_tick_does_nothing_when_paused(rt):
    clock = FakeClock()
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000), clock=clock)
    auto.paused = True
    await rt.store.add_rule("battery_poll", "Akku", {
        "targets": [REPEATER_LOW], "interval_min": 5, "low_mv": 3500})
    for _ in range(5):
        clock.advance(600)
        await auto.tick()
    assert await rt.store.latest_battery() == {}


async def test_ping_message_is_sent_without_auto_prefix(rt):
    """Andere Bots reagieren nur auf das nackte Wort, darum kein „[auto]“ beim Ping."""
    res = await rt.automation.ping_node(ALICE, "message", "ping")
    assert res["ok"] is True
    assert rt.node.sent[-1] == (ALICE, "ping")


async def test_unprefixed_ping_does_not_cause_reply_loops(rt):
    """Unsere Auto-Antwort trägt die Erwähnung und wird bei Empfang ignoriert, auch wenn sie „ping“ enthält."""
    await rt.store.add_rule("auto_reply", "Pong", {"match": "ping", "mode": "contains", "reply": "pong (ping ok)",
                                                   "cooldown_s": 30, "only_from": None})
    await rt.node.inject_message(ALICE, "ping")
    await rt.hub.drain()
    assert MENTION_RE.match(rt.node.sent[-1][1])
    n = len(rt.node.sent)
    await rt.node.inject_message(ALICE, rt.node.sent[-1][1])        # unsere eigene Antwort kommt „zurück“
    await rt.hub.drain()
    assert len(rt.node.sent) == n


async def test_trace_ping_repeater_returns_snr_and_is_stored(rt):
    res = await rt.automation.ping_node(REPEATER_OK, "trace")
    assert res["ok"] is True and res["snr_out"] is not None and res["snr_in"] is not None
    last = (await rt.store.latest_pings())[REPEATER_OK]
    assert last["mode"] == "trace" and last["snr_out"] == res["snr_out"]
    assert rt.node.sent == []                                      # kein Text gesendet


async def test_trace_ping_unreachable_and_not_for_clients(rt):
    rt.node.fail_status.add(REPEATER_LOW)
    assert (await rt.automation.ping_node(REPEATER_LOW, "trace"))["ok"] is False
    assert (await rt.automation.ping_node(ALICE, "trace"))["ok"] is False


async def test_channel_ping_rule_posts_plain_text_and_logs(rt):
    rule = {"id": 1, "kind": "channel_ping", "name": "K", "enabled": True,
            "config": {"channel": 1, "interval_min": 15, "text": "ping"}}
    await rt.automation.run_rule(rule)
    assert rt.node.sent_channel == [(1, "ping")]
    msgs = await rt.store.channel_messages(1)
    assert [(m["direction"], m["text"], m["auto"]) for m in msgs] == [("out", "ping", 1)]


async def test_channel_ping_on_missing_channel_is_logged_not_raised(rt):
    rule = {"id": 2, "kind": "channel_ping", "name": "K", "enabled": True,
            "config": {"channel": 7, "interval_min": 15, "text": "ping"}}
    await rt.automation.run_rule(rule)
    log = await rt.store.log(10)
    assert any("Kanal-Ping" in e["text"] for e in log)
    assert rt.node.sent_channel == []


async def test_channel_ping_rule_is_scheduled_by_tick(rt):
    clock = FakeClock()
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000), clock=clock)
    await rt.store.add_rule("channel_ping", "K", {"channel": 0, "interval_min": 15, "text": "ping"})
    await auto.tick()                         # plant
    clock.advance(60)
    await auto.tick()                         # Startverzögerung vorbei -> gesendet
    assert rt.node.sent_channel == [(0, "ping")]
    clock.advance(600)
    await auto.tick()                         # noch keine 15 Minuten
    assert len(rt.node.sent_channel) == 1


# ---- Auto-Antwort in Kanälen: Pause je Absender und Stundenobergrenze -----------------------------
def _auto(rt, clock):
    return Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 1000), clock=clock)


def _chan_payload(idx=0, plen=1):
    return {"channel_idx": idx, "txt_type": 0, "path_len": plen}


async def _channel_rule(rt, **over):
    cfg = {"match": "ping", "mode": "equals", "reply": "pong {name}", "cooldown_s": 1800,
           "max_per_hour": 10, "direct": False, "channels": [0], "only_from": None, **over}
    return await rt.store.add_rule("auto_reply", "Kanal-Pong", cfg)


async def test_channel_reply_goes_into_the_channel_with_prefix_and_placeholder(rt):
    await _channel_rule(rt)
    auto = _auto(rt, FakeClock())
    await auto.handle_channel_incoming(_chan_payload(plen=2), "Alice", "ping")
    assert rt.node.sent_channel == [(0, "@[Alice] pong Alice")]
    assert rt.node.sent == []                                        # keine Direktnachricht
    msgs = await rt.store.channel_messages(0)
    assert [(m["direction"], m["auto"]) for m in msgs] == [("out", 1)]


async def test_each_person_is_ignored_for_the_cooldown_after_one_reply(rt):
    await _channel_rule(rt, cooldown_s=1800)
    clock = FakeClock()
    auto = _auto(rt, clock)
    await auto.handle_channel_incoming(_chan_payload(), "Alice", "ping")
    clock.advance(600)
    await auto.handle_channel_incoming(_chan_payload(), "Alice", "ping")     # nach 10 min: ignoriert
    await auto.handle_channel_incoming(_chan_payload(), "Bob", "ping")       # andere Person: beantwortet
    assert len(rt.node.sent_channel) == 2
    clock.advance(1300)                                                       # insgesamt 31 min seit Alice
    await auto.handle_channel_incoming(_chan_payload(), "Alice", "ping")
    assert len(rt.node.sent_channel) == 3


async def test_hourly_cap_answers_only_the_first_n_of_a_crowd(rt):
    await _channel_rule(rt, max_per_hour=10)
    auto = _auto(rt, FakeClock())
    for i in range(100):
        await auto.handle_channel_incoming(_chan_payload(), f"Person{i}", "ping")
    assert len(rt.node.sent_channel) == 10
    assert rt.node.sent_channel[0][1].endswith("Person0") and rt.node.sent_channel[-1][1].endswith("Person9")
    warns = [e for e in await rt.store.log(50) if "Höchstzahl" in e["text"]]
    assert len(warns) == 1                                                    # nur einmal gemeldet, kein Log-Spam


async def test_cap_window_rolls_and_frees_slots(rt):
    await _channel_rule(rt, max_per_hour=2, cooldown_s=30)
    clock = FakeClock()
    auto = _auto(rt, clock)
    for who in ("A", "B", "C"):
        await auto.handle_channel_incoming(_chan_payload(), who, "ping")
    assert len(rt.node.sent_channel) == 2
    clock.advance(3601)
    await auto.handle_channel_incoming(_chan_payload(), "C", "ping")
    assert len(rt.node.sent_channel) == 3


async def test_simultaneous_messages_cannot_exceed_the_cap(rt):
    import asyncio
    await _channel_rule(rt, max_per_hour=5)
    auto = _auto(rt, FakeClock())
    await asyncio.gather(*[auto.handle_channel_incoming(_chan_payload(), f"P{i}", "ping") for i in range(50)])
    assert len(rt.node.sent_channel) == 5


async def test_only_chosen_channels_and_matching_text_trigger(rt):
    await _channel_rule(rt, channels=[1])
    auto = _auto(rt, FakeClock())
    await auto.handle_channel_incoming(_chan_payload(0), "A", "ping")        # falscher Kanal
    await auto.handle_channel_incoming(_chan_payload(1), "A", "hallo")       # falscher Text
    assert rt.node.sent_channel == []
    await auto.handle_channel_incoming(_chan_payload(1), "A", "ping")
    assert len(rt.node.sent_channel) == 1


async def test_channel_reply_ignores_auto_messages_and_unknown_sender_shares_one_slot(rt):
    await _channel_rule(rt)
    auto = _auto(rt, FakeClock())
    await auto.handle_channel_incoming(_chan_payload(), "Bot", AUTO_PREFIX + "ping")
    assert rt.node.sent_channel == []
    await auto.handle_channel_incoming(_chan_payload(), None, "ping")
    await auto.handle_channel_incoming(_chan_payload(), None, "ping")        # derselbe unbekannte Absender
    assert len(rt.node.sent_channel) == 1


async def test_direct_only_rule_does_not_answer_in_channels_and_vice_versa(rt):
    await rt.store.add_rule("auto_reply", "DM", {"match": "ping", "mode": "equals", "reply": "pong",
                                                 "cooldown_s": 30, "only_from": None})   # alte Regel ohne neue Felder
    auto = _auto(rt, FakeClock())
    await auto.handle_channel_incoming(_chan_payload(), "A", "ping")
    assert rt.node.sent_channel == []
    await auto.handle_incoming({"pubkey_prefix": ALICE[:12], "text": "ping", "txt_type": 0}, {
        "public_key": ALICE, "name": "Alice", "path_len": 1})
    assert rt.node.sent == [(ALICE, "@[Alice] pong")]


async def test_direct_replies_also_respect_hourly_cap(rt):
    await rt.store.add_rule("auto_reply", "DM", {"match": "ping", "mode": "equals", "reply": "pong", "cooldown_s": 30,
                                                 "max_per_hour": 2, "direct": True, "channels": [], "only_from": None})
    auto = _auto(rt, FakeClock())
    for key in (ALICE, "b2" * 32, "c3" * 32):
        await auto.handle_incoming({"text": "ping", "txt_type": 0}, {"public_key": key, "name": key[:4], "path_len": 1})
    assert len(rt.node.sent) == 2


async def test_failed_send_gives_the_slot_back(rt):
    await _channel_rule(rt, max_per_hour=1)
    auto = _auto(rt, FakeClock())
    await rt.node.disconnect(); await rt.node.connect()
    orig = rt.node.send_channel_message

    async def fail(*a, **k):
        raise NodeError("kaputt")
    rt.node.send_channel_message = fail
    await auto.handle_channel_incoming(_chan_payload(), "A", "ping")
    rt.node.send_channel_message = orig
    await auto.handle_channel_incoming(_chan_payload(), "A", "ping")         # darf wieder, trotz Cap 1 und Pause
    assert len(rt.node.sent_channel) == 1


async def test_paused_automation_stays_silent_in_channels(rt):
    await _channel_rule(rt)
    auto = _auto(rt, FakeClock())
    auto.paused = True
    await auto.handle_channel_incoming(_chan_payload(), "A", "ping")
    assert rt.node.sent_channel == []


async def test_incoming_channel_message_event_triggers_reply_end_to_end(rt):
    await _channel_rule(rt, channels=[0])
    await rt.node.inject_channel_message(0, "Alice (Sim)", "ping")
    await rt.hub.drain()
    assert rt.node.sent_channel and MENTION_RE.match(rt.node.sent_channel[0][1])


# ---------------------------------------------------------------- {path}
async def test_path_placeholder_channel(rt):
    await rt.store.add_rule("auto_reply", "P", {"match": "ping", "mode": "equals", "reply": "pong via {path} ({hops})",
                                                "cooldown_s": 60, "only_from": None, "direct": False, "channels": [0]})
    await rt.node.inject_channel_message(0, "Alice (Sim)", "ping", path="a1b2c3")
    await rt.hub.drain()
    assert rt.node.sent_channel[-1][1] == "@[Alice (Sim)] pong via a1,b2,c3 (3)"


async def test_path_placeholder_direct_and_unknown(rt):
    await add_reply_rule(rt, reply="{path}")
    await rt.node.inject_message(ALICE, "ping", path_len=0)
    await rt.hub.drain()
    assert rt.node.sent[-1][1] == "@[Alice (Sim)] direkt"


def test_path_text_variants():
    from app.automation import Automation as A
    assert A.path_text("", 1, 0) == "direkt"
    assert A.path_text("a1b2", 1, 2) == "a1,b2"
    assert A.path_text("a1b2c3d4", 2, 2) == "a1b2,c3d4"
    assert A.path_text(None, 1, 2) == "2 Hops" and A.path_text(None, 1, 1) == "1 Hop"
    assert A.path_text(None, 1, None) == "?"


async def test_message_ping_to_repeater_is_refused(rt):
    from app.node import NodeError
    from .conftest import REPEATER_OK
    import pytest
    with pytest.raises(NodeError, match="nur an Clients"):
        await rt.automation.ping_node(REPEATER_OK, "message", "ping")
    assert rt.node.sent == []


# ---- Advert-Zeitplan ---------------------------------------------------------------------------
async def test_advert_rule_sends_and_logs(rt):
    rule = {"id": 5, "kind": "advert", "name": "A", "enabled": True, "config": {"interval_min": 360, "flood": True}}
    await rt.automation.run_rule(rule)
    log = await rt.store.log(10)
    assert any("Advert gesendet" in e["text"] and "netzweit" in e["text"] for e in log)


def test_advert_config_limits():
    from pydantic import ValidationError
    from app.schemas import validate_config
    assert validate_config("advert", {"interval_min": 60}) == {"interval_min": 60, "flood": False}
    for bad in ({"interval_min": 30}, {"interval_min": 120, "flood": True}, {"interval_min": 20000}):
        with pytest.raises(ValidationError):
            validate_config("advert", bad)


async def test_advert_rule_respects_budget_and_is_scheduled(rt):
    clock = FakeClock()
    auto = Automation(rt.node, rt.store, rt.hub.emit, SendBudget(0, 0), clock=clock)     # Stundenlimit 0
    await rt.store.add_rule("advert", "A", {"interval_min": 60, "flood": False})
    await auto.tick(); clock.advance(60); await auto.tick()
    assert any("Stundenlimit" in e["text"] for e in await rt.store.log(10))


async def test_advert_rule_via_api(client):
    r = await client.post("/api/rules", json={"kind": "advert", "name": "A", "config": {"interval_min": 120, "flood": True}})
    assert r.status_code == 422
    r = await client.post("/api/rules", json={"kind": "advert", "name": "A", "config": {"interval_min": 360, "flood": True}})
    assert r.status_code in (200, 201)
