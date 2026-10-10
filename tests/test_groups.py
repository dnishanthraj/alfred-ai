"""Group chats: one record, read in each member's own time, kept consistent everywhere."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import wayne.frontends.web as web
from wayne import paths
from wayne.engine import groupchat, presence
from wayne.memory import groups as store


def _contact(cid, name, per_day=0.5):
    return SimpleNamespace(id=cid, name=name, full_name=name, routine=(), shares_status=True,
                           texting_pace={"phone": 1.0, "online_read": [0.01, 0.01], "wpm": 10000},
                           texting_style={}, initiative={"per_day": per_day}, texting="")


BOOK = {"nightwing": _contact("nightwing", "Dick"), "robin": _contact("robin", "Tim"),
        "catwoman": _contact("catwoman", "Selina"), "batwing": _contact("batwing", "Randy")}


class Directory:
    def get(self, cid):
        return BOOK.get(cid)

    def find(self, name):
        said = name.strip().lower()
        return next((c for c in BOOK.values() if said in (c.name.lower(), c.full_name.lower(), c.id)), None)

    def __iter__(self):
        return iter(BOOK.values())


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path / cid)
    presence._registry.clear()
    yield
    presence._registry.clear()


def test_what_a_member_knows_is_what_they_have_read():
    group = store.create("Night shift", ["nightwing", "robin"])
    first = group.add("me", "docks at midnight?", at=100)
    group.mark_read("robin", 105)                   # Tim reads it, and answers
    group.add("robin", "on it", at=110)
    assert group.read_upto("robin") == 110          # you've read what you wrote
    assert [m["text"] for m in group.unread_for("nightwing")] == ["docks at midnight?", "on it"]
    group.mark_read("nightwing", 105)
    assert [m["text"] for m in group.seen_by("nightwing")] == ["docks at midnight?"]
    assert group.readers_of(first) == ["nightwing", "robin"]
    group.add("me", "and bring the cable", at=111)  # lands while Tim is typing his next
    group.add("robin", "on my way", at=112)
    assert group.read_upto("robin") == 110          # ...so it's still unread: posting isn't reading


def test_their_group_chats_ride_along_with_everything_they_say():
    group = store.create("Night shift", ["nightwing", "robin"])
    group.add("me", "docks at midnight?")
    group.add("robin", "bringing coffee")
    block = groupchat.block("nightwing", Directory())
    assert block == ""                                # Dick hasn't read it yet
    group.mark_read("nightwing")
    block = groupchat.block("nightwing", Directory())
    assert "Night shift" in block and "Tim: bringing coffee" in block


def test_someone_outside_the_secret_is_flagged_to_those_inside_it():
    group = store.create("Sunday", ["nightwing", "catwoman"])
    assert "Selina" in groupchat.secrets_note(group, "nightwing")      # by name, not by id
    assert groupchat.secrets_note(group, "catwoman") == ""     # she isn't told there's a secret


def test_named_members_answer_and_a_thread_runs_out_of_energy():
    group = store.create("Night shift", ["nightwing", "robin"])
    tim = BOOK["robin"]
    named = [{"from": "me", "text": "Tim, you there?", "at": 1}]
    assert groupchat.reply_odds(tim, group, named, energy=0.01) == 1.0
    chatter = [{"from": "nightwing", "text": "lol", "at": 1}]
    assert groupchat.reply_odds(tim, group, chatter, 1.0) > groupchat.reply_odds(tim, group, chatter, 0.2)
    for _ in range(3):
        groupchat.spend(group, "nightwing")
    assert groupchat.energy(group) > groupchat.QUIET          # a few messages in, still going
    for _ in range(4):
        groupchat.spend(group, "nightwing")
    assert groupchat.energy(group) < groupchat.QUIET          # a handful more and it's run its course
    groupchat.spend(group, "me")
    assert groupchat.energy(group) == groupchat.FULL


def test_after_a_notice_about_them_his_message_is_theirs_to_answer():
    group = store.create("Family", ["nightwing", "robin"])
    group.add_member("batwing")
    group.system("You added Randy", said="Bruce added Randy")
    asked = group.add("me", "where do you think you're going?")
    randy, tim = BOOK["batwing"], BOOK["robin"]
    assert groupchat.follows_notice(group, randy, [asked])
    assert not groupchat.follows_notice(group, tim, [asked])
    assert groupchat.reply_odds(randy, group, [asked], energy=0.01) == 1.0


def test_he_posts_and_each_member_reads_in_their_own_time(monkeypatch):
    console = web.Console.__new__(web.Console)
    console._adding, console._dropped_adds, console._closing = set(), set(), set()
    console.directory, console.clients, console._tasks = Directory(), {object()}, set()
    console._typing_now = set()
    console._init_groups()
    console.GROUP_SETTLE = 0
    events, posted = [], []

    async def broadcast(e):
        events.append(e)
    console.broadcast = broadcast

    async def post_as(group, contact, unread=(), must=False, opening=None):
        posted.append((contact.id, must))
    console._post_as = post_as
    monkeypatch.setattr(web, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(web.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(web.random, "random", lambda: 0.99)    # no leeway, no asides
    group = store.create("Night shift", ["nightwing", "robin"])

    async def run():
        for c in BOOK.values():
            presence.of(c).touch()
        await console.group_text(group.id, "Tim — status?")
        await asyncio.sleep(1.5)

    asyncio.run(run())
    reads = {e["member"] for e in events if e["type"] == "group_read"}
    assert reads == {"nightwing", "robin"}
    assert ("robin", True) in posted                 # named: he answers
    assert store.of(group.id).unread_for("robin") == []


def test_someone_who_ghosts_mostly_reads_and_says_nothing():
    group = store.create("Family", ["nightwing", "robin"])
    chatter = [{"from": "nightwing", "text": "sunday dinner?", "at": 1}]
    sociable = _contact("a", "A", per_day=0.5)
    ghost = _contact("b", "B", per_day=0.5)
    ghost.texting_pace = {**ghost.texting_pace, "on_read": 0.6}
    assert groupchat.reply_odds(ghost, group, chatter, 1.0) < groupchat.reply_odds(sociable, group, chatter, 1.0) / 2


def test_members_come_and_go_and_the_thread_says_so():
    group = store.create("Night shift", ["nightwing", "robin"])
    group.add("me", "where is everyone", at=10)
    assert group.add_member("catwoman") and not group.add_member("catwoman")
    assert [m["text"] for m in group.unread_for("catwoman")] == ["where is everyone"]
    group.system("Dick added Selina")
    assert group.unread_for("robin")[-1]["text"] == "Dick added Selina"   # something to react to
    assert group.remove_member("robin") and "robin" not in group.members


def test_nobody_puts_back_someone_he_removed_but_he_can_ask_for_anyone():
    group = store.create("Night shift", ["nightwing", "robin"])
    group.remove_member("robin", by_bruce=True)
    assert not groupchat.may_add(group, BOOK["nightwing"], BOOK["robin"])
    group.add("me", "Dick, add Tim back in", at=time.time())
    assert groupchat.may_add(group, BOOK["nightwing"], BOOK["robin"])
    assert not groupchat.may_remove(group, BOOK["nightwing"], BOOK["catwoman"])    # not on a whim
    group.add("me", "Dick, remove Selina", at=time.time())
    assert groupchat.may_remove(group, BOOK["nightwing"], BOOK["catwoman"])
    other = store.create("Quiet", ["nightwing", "robin"])
    other.add("me", "keep it between us", at=time.time())
    assert not groupchat.may_add(other, BOOK["nightwing"], BOOK["catwoman"])


def test_a_member_walking_out_or_bringing_someone_in(monkeypatch):
    console = web.Console.__new__(web.Console)
    console._adding, console._dropped_adds, console._closing = set(), set(), set()
    console.directory, console.clients, console._tasks = Directory(), {object()}, set()
    console._typing_now = set()
    console._init_groups()
    events = []

    async def broadcast(e):
        events.append(e)
    console.broadcast = broadcast
    console._read_later = lambda group, member: None
    group = store.create("Night shift", ["nightwing", "robin"])

    async def run():
        await console._group_actions(group, BOOK["nightwing"], {"add": ["Selina"], "leave": False})
        await console._group_actions(group, BOOK["robin"], {"add": [], "leave": True})

    asyncio.run(run())
    group = store.of(group.id)
    assert group.members == ["nightwing", "catwoman"]
    lines = [e["message"]["text"] for e in events if e["type"] == "group_message"]
    assert lines == ["Dick added Selina", "Tim left"]


def test_a_chat_is_livelier_when_more_of_them_are_around(monkeypatch):
    for c in BOOK.values():
        monkeypatch.setitem(c.texting_pace, "phone", 0.0)
        monkeypatch.setitem(c.texting_pace, "phone_night", 0.0)
    group = store.create("Night shift", ["nightwing", "robin"])
    quiet = groupchat.liveliness(group, Directory())
    for c in BOOK.values():
        presence.of(c).touch()
    assert groupchat.liveliness(group, Directory()) > quiet


def test_a_reply_quotes_whoever_they_name_unless_its_right_above():
    group = store.create("Night shift", ["nightwing", "robin", "catwoman"])
    car = group.add("robin", "who took the car", at=100)
    group.add("robin", "also we're out of coffee", at=101)
    group.add("me", "I did", at=102)
    group.add("catwoman", "bold of you", at=103)
    book = Directory()
    assert groupchat.quoting(group, "Tim: the car", book, "nightwing") == car["id"]
    assert groupchat.quoting(group, "@timmy", book, "nightwing") == group.messages()[1]["id"]   # his latest
    assert groupchat.quoting(group, "Bruce", book, "nightwing") == group.messages()[2]["id"]
    assert groupchat.quoting(group, "Selina", book, "nightwing") is None      # right above: no quote needed
    assert groupchat.quoting(group, "Jason", book, "nightwing") is None       # nobody here


def test_a_dm_reply_quotes_the_text_of_his_it_answers():
    from wayne.memory.texts import TextLog, quoted_by
    log = TextLog("robin")
    first = log.add("me", "did you eat", at=100)
    last = log.add("me", "also where is the car", at=101)
    assert quoted_by([first, last], "did you eat")["id"] == first["id"]
    assert web._quoting(log, [first, last], "did you eat") == first["id"]
    assert web._quoting(log, [first, last], "the car") is None        # his last: nothing to quote
    assert web._quoting(log, [first, last], "something else") is None
    assert web._quoting(log, [first, last], None) is None


def test_they_always_know_what_they_wrote_themselves():
    group = store.create("Night shift", ["nightwing", "robin"])
    group.add("robin", "anyone up", at=100)
    group.add("nightwing", "told randy to come home", at=101)    # Tim's message still unread for Dick
    assert group.read_upto("nightwing") < 100
    assert "told randy to come home" in [m["text"] for m in group.seen_by("nightwing")]
