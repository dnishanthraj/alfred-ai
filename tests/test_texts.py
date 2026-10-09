"""Texting: the thread, presence, pacing, style, promises and calls to him."""
import asyncio
import random
import time
from types import SimpleNamespace

import pytest

import wayne.frontends.web as web
from wayne import paths
from wayne.engine import initiative, presence
from wayne.memory.texts import TextLog


def _log(tmp_path):
    log = TextLog.__new__(TextLog)
    log.path = tmp_path / "texts.json"
    return log


def _contact(**kw):
    base = dict(id="nightwing", name="Dick", full_name="Dick Grayson", routine=(),
                texting_pace={"phone": 0.0, "online_read": [0.05, 0.05], "wpm": 10000},
                texting_style={}, initiative={}, shares_status=True, can_search=False,
                texting="")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    """Presence files go to a temp dir, and nobody shares state between tests."""
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path)
    presence._registry.clear()
    yield
    presence._registry.clear()


def test_the_thread_pages_back_and_records_reads(tmp_path):
    log = _log(tmp_path)
    sent = [log.add("me", f"m{i}", at=100 + i) for i in range(5)]
    assert [m["text"] for m in log.page(limit=2)] == ["m3", "m4"]
    assert [m["text"] for m in log.page(before=103, limit=2)] == ["m1", "m2"]
    log.mark_read([sent[4]["id"]], at=200)
    assert log.page(limit=1)[0]["read_at"] == 200
    assert [m["text"] for m in log.unread()] == ["m0", "m1", "m2", "m3"]
    log.add("them", "", kind="missed_call")
    assert log.last()["text"] == "m4"     # a missed call isn't a message


# --- presence ------------------------------------------------------------------

def test_what_the_conversation_set_outranks_being_online_and_the_routine():
    contact = _contact(routine=({"from": 0, "to": 24, "doing": "asleep", "status": "offline"},))
    state = presence.of(contact)
    assert state.now()["status"] == presence.OFFLINE and state.now()["doing"] == "asleep"
    state.touch()
    assert state.now()["status"] == presence.ONLINE          # texting him: phone in hand
    state.set_activity("checking the docks", "busy", 60)
    now = state.now()
    assert now["status"] == presence.BUSY and now["source"] == "conversation"
    assert "checking the docks" in state.note()
    state.clear_activity()
    assert state.now()["status"] == presence.ONLINE


def test_engagement_wears_off_into_the_routine():
    contact = _contact(routine=({"from": 0, "to": 24, "doing": "on patrol", "status": "busy"},))
    state = presence.of(contact)
    state.touch(time.time() - presence.ENGAGED_FOR - 1)
    assert state.now()["status"] == presence.BUSY


def test_a_routine_block_is_decided_once_per_day_not_per_refresh():
    contact = _contact(routine=({"from": 0, "to": 24, "doing": "on patrol", "status": "busy",
                                 "chance": 0.5},))
    state = presence.of(contact)
    t = time.mktime((2026, 10, 9, 12, 0, 0, 0, 0, -1))
    seen = {state.now(t + minute * 60)["source"] for minute in range(0, 600, 7)}
    assert len(seen) == 1


def test_a_night_past_midnight_belongs_to_the_night_it_began():
    contact = _contact(routine=({"from": 22, "to": 3, "doing": "on patrol", "status": "busy",
                                 "days": [4]},))     # Fridays only
    state = presence.of(contact)
    saturday_1am = time.mktime((2026, 10, 10, 1, 0, 0, 0, 0, -1))
    sunday_1am = time.mktime((2026, 10, 11, 1, 0, 0, 0, 0, -1))
    assert state.now(saturday_1am)["doing"] == "on patrol"
    assert state.now(sunday_1am)["doing"] == ""


def test_how_soon_they_read_depends_on_what_theyre_doing():
    contact = _contact(texting_pace={"online_read": [1, 2], "idle_read": [100, 200],
                                     "busy_read": [1000, 2000], "glance": 0})
    assert 1 <= presence.read_delay(contact, {"status": "online"}) <= 2
    assert 100 <= presence.read_delay(contact, {"status": "idle"}) <= 200
    assert 1000 <= presence.read_delay(contact, {"status": "busy"}) <= 2000
    assert presence.read_delay(contact, {"status": "offline"}) is None


# --- style -----------------------------------------------------------------------

def test_texting_style_is_applied_in_code():
    contact = _contact(texting_style={"lower": 1.0, "period": False, "burst": 1.0})
    rng = random.Random(1)
    parts = initiative.bubbles(contact, "On it. Give me twenty. The GCPD can wait.", rng)
    joined = " ".join(parts)
    assert joined == joined.lower().replace("gcpd", "GCPD")
    assert len(parts) >= 2 and not any(p.endswith(".") for p in parts)
    composed = _contact(texting_style={"burst": 0})
    assert initiative.bubbles(composed, "Noted.\nDinner will keep.") == ["Noted. Dinner will keep."]
    # Lines the model wrote apart are sent apart.
    assert initiative.bubbles(contact, "on it\ngive me 20", rng) == ["on it", "give me 20"]
    sparing = _contact(texting_style={"emoji": 0.0})
    assert initiative.styled(sparing, "You'll have it Thursday. 👍") == "You'll have it Thursday."


# --- promises --------------------------------------------------------------------

def test_an_errand_makes_them_busy_and_leaves_a_promise_to_report_back():
    contact = _contact()
    session = SimpleNamespace(contact=contact)
    initiative.apply(session, {"doing": "checking the docks", "status": "busy", "minutes": 45,
                               "contact": {"by": "call", "in_minutes": None,
                                           "about": "what he found at the docks"}})
    state = presence.of(contact)
    assert state.now()["status"] == presence.BUSY
    (intent,) = state.intents()
    assert intent["action"] == "call"
    # "When it's done" means when the errand is over, not before.
    assert intent["due"] > time.time() + 30 * 60
    assert not state.due() and state.due(intent["due"] + 1)


def test_saying_theyre_free_ends_the_activity():
    contact = _contact()
    session = SimpleNamespace(contact=contact)
    presence.of(contact).set_activity("in a meeting", "busy", 60)
    initiative.apply(session, {"free": True, "doing": None, "contact": None})
    assert presence.of(contact).now()["source"] != "conversation"


# --- the console -----------------------------------------------------------------

def _console(tmp_path, monkeypatch, contact):
    console = web.Console.__new__(web.Console)
    console._pending_texts, console._texters, console.call, console.current_id = {}, {}, None, None
    console.sessions, console._shown_presence, console._incoming = {}, {}, None
    console._ring_timer, console._release, console.clients = None, None, {object()}
    console._tasks, console._connecting, console._writing_lines = set(), False, False
    console._text_bursts, console._call_attempts = {}, {}
    console._typing_now = set()
    console.turn_lock = None
    events = []

    async def broadcast(event):
        events.append(event)
    console.broadcast = broadcast
    console._members = lambda: []
    class Directory:
        def get(self, _id):
            return contact

        def __iter__(self):
            return iter([contact])
    console.directory = Directory()
    monkeypatch.setattr(web, "TextLog", lambda cid: _log(tmp_path))
    console._release_later = lambda c: None

    async def afterthought(*a):
        return None
    console._afterthought = afterthought
    console.session_for = lambda cid: SimpleNamespace(history=SimpleNamespace(messages=[]))
    return console, events


def test_texts_in_a_row_are_read_and_answered_together(tmp_path, monkeypatch):
    contact = _contact()
    presence.of(contact).touch()          # mid-conversation: reads in moments
    console, events = _console(tmp_path, monkeypatch, contact)
    written = []

    async def write(c, body):
        written.append(body)
        return "on it"
    console._write_text = write
    monkeypatch.setattr(web.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(web.random, "random", lambda: 0.99)

    async def run():
        await console.text("nightwing", "you around?")
        await console.text("nightwing", "need a hand at the docks")
        await asyncio.sleep(1.5)

    asyncio.run(run())
    assert written == ["you around?\nneed a hand at the docks"]
    kinds = [e["type"] for e in events if e["type"] in ("text_read", "text_typing", "text_reply")]
    assert kinds == ["text_read", "text_typing", "text_reply"]


def test_a_text_to_someone_asleep_waits_until_they_can_see_it(tmp_path, monkeypatch):
    contact = _contact(routine=({"from": 0, "to": 24, "doing": "asleep", "status": "offline"},))
    console, events = _console(tmp_path, monkeypatch, contact)

    async def write(c, body):
        return "morning"
    console._write_text = write
    monkeypatch.setattr(web, "RECHECK_SECONDS", 0.1)
    monkeypatch.setattr(web.random, "uniform", lambda a, b: a)

    async def run():
        await console.text("nightwing", "you up?")
        await asyncio.sleep(0.3)
        assert not [e for e in events if e["type"] == "text_read"]
        presence.of(contact).touch()      # he rang and woke them
        await asyncio.sleep(1.5)

    asyncio.run(run())
    assert [e for e in events if e["type"] == "text_reply"]


def test_a_declined_call_goes_in_the_thread_and_they_may_text_instead(tmp_path, monkeypatch):
    contact = _contact(initiative={"react": 1.0})
    console, events = _console(tmp_path, monkeypatch, contact)
    texted = []

    async def unprompted(c, about, why):
        texted.append((about, why))
    console._send_unprompted = unprompted
    monkeypatch.setattr(web.random, "uniform", lambda a, b: 0)

    async def run():
        await console._ring(contact, "the docks")
        await console.decline("nightwing")

    asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "call_incoming" and "call_unanswered" in kinds
    assert texted == [("the docks", "declined")]
    assert console._incoming is None


def test_nobody_texts_out_of_the_blue_past_the_daily_budget(tmp_path, monkeypatch):
    contact = _contact(initiative={"per_day": 10 ** 6})
    presence.of(contact).touch()
    console, events = _console(tmp_path, monkeypatch, contact)
    sent = []

    async def unprompted(c, about, why):
        sent.append(why)
    console._send_unprompted = unprompted
    monkeypatch.setattr(initiative, "impulse", lambda s: "something")
    monkeypatch.setattr(web.config, "QUIET_HOURS", "0-0")
    monkeypatch.setattr(web.config, "INITIATIVE_PER_DAY", 2)
    console._tasks, console._count_initiative = set(), lambda: None

    async def tick():
        await console._maybe_reach_out(time.time())
        await asyncio.sleep(0.01)      # let the spawned send run
    console._initiative_log = lambda: [time.time() - 3600 * 5, time.time() - 3600 * 3]
    asyncio.run(tick())
    assert sent == []
    console._initiative_log = lambda: []
    asyncio.run(tick())
    assert sent == ["impulse"]


# --- not picking up ----------------------------------------------------------------

def test_who_picks_up_depends_on_what_theyre_doing():
    contact = _contact(initiative={"answers": {"online": 1.0, "busy": 0.0, "offline": 0.0}})
    assert presence.answers(contact, {"status": "online"}) is None
    assert presence.answers(contact, {"status": "offline"}) == "no_answer"
    assert presence.answers(contact, {"status": "busy"}) in ("declined", "no_answer")
    # Ringing straight back reads as urgent: odds of answering go up.
    rng = random.Random(0)
    half = _contact(initiative={"answers": {"busy": 0.5}})
    first = sum(presence.answers(half, {"status": "busy"}, rng=rng) is None for _ in range(2000))
    again = sum(presence.answers(half, {"status": "busy"}, True, rng) is None for _ in range(2000))
    assert again > first + 400


def test_a_declined_call_closes_the_line_and_leaves_a_callback(tmp_path, monkeypatch):
    contact = _contact(initiative={"busy_text": 0.0, "callback": 1.0, "callback_call": 1.0},
                       availability=SimpleNamespace(is_available=lambda: True))
    console, events = _console(tmp_path, monkeypatch, contact)
    console.turn_lock, console.turn_epoch, console._refused_at = asyncio.Lock(), 0, {}
    console._ringing_out, console.migrated = None, []

    class FakeCall:
        def __init__(self):
            self.members = []

        def join(self, s):
            self.members.append(s)

        def leave(self, s):
            self.members.remove(s)
    monkeypatch.setattr(web, "Call", FakeCall)
    monkeypatch.setattr(web, "DECLINE_AFTER", (0.01, 0.01))
    monkeypatch.setattr(presence, "answers", lambda *a, **k: "declined")
    presence.of(contact).set_activity("in a board meeting", "busy", 30)

    async def run():
        await console.connect("nightwing")
        assert console._ringing_out is None and console.current_id is None
        await console.submit("hello?")            # nobody on the line
        await asyncio.sleep(0.05)

    asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert "call_refused" in kinds and "reply_end" not in kinds
    (intent,) = presence.of(contact).intents()
    assert intent["origin"] == "callback" and intent["action"] == "call"
    assert "board meeting" in intent["about"]
    # Due once the meeting's over, not before.
    assert intent["due"] > time.time() + 25 * 60


# --- texting habits --------------------------------------------------------------------

def test_typos_are_realistic_and_sometimes_corrected():
    rng = random.Random(5)
    contact = _contact(texting_style={"typos": 1.0, "corrects": 1.0})
    sent, correction = initiative.slip(contact, "heading to the docks now", rng)
    assert sent != "heading to the docks now" and correction.startswith("*")
    assert correction[1:] in "heading to the docks now"
    careful = _contact(texting_style={"typos": 0.0})
    assert initiative.slip(careful, "heading to the docks now") == ("heading to the docks now", None)


def test_reply_length_is_drawn_from_their_own_spread():
    terse = _contact(texting_style={"length": {"word": 1.0}})
    assert "a word or two" in initiative.length_hint(terse)
    assert initiative.length_hint(_contact()) == ""


def test_corrections_come_in_each_persons_own_form():
    rng = random.Random(3)
    prefix = _contact(texting_style={"typos": 1.0, "corrects": 1.0, "correct_style": "prefix"})
    suffix = _contact(texting_style={"typos": 1.0, "corrects": 1.0, "correct_style": "suffix"})
    assert initiative.slip(prefix, "heading to the docks now", rng)[1].startswith("*")
    assert initiative.slip(suffix, "heading to the docks now", rng)[1].split()[0].endswith("*")


def test_autocorrect_swaps_in_a_real_wrong_word():
    rng = random.Random(1)
    fat_thumbs = _contact(texting_style={"typos": 1.0, "autocorrect": 1.0, "corrects": 0.0})
    sent, correction = initiative.slip(fat_thumbs, "meet me at the docks", rng)
    assert sent == "meet me at the ducks" and correction is None
