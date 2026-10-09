"""Group chats: one record, read in each member's own time, kept consistent everywhere."""
import asyncio
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
        "catwoman": _contact("catwoman", "Selina")}


class Directory:
    def get(self, cid):
        return BOOK.get(cid)

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
    group.add("robin", "on it", at=110)
    assert group.read_upto("robin") == 110          # you've read what you wrote
    assert [m["text"] for m in group.unread_for("nightwing")] == ["docks at midnight?", "on it"]
    group.mark_read("nightwing", 105)
    assert [m["text"] for m in group.seen_by("nightwing")] == ["docks at midnight?"]
    assert group.readers_of(first) == ["nightwing", "robin"]


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
    assert "catwoman" in groupchat.secrets_note(group, "nightwing")
    assert groupchat.secrets_note(group, "catwoman") == ""     # she isn't told there's a secret


def test_named_members_answer_and_a_thread_runs_out_of_energy():
    group = store.create("Night shift", ["nightwing", "robin"])
    tim = BOOK["robin"]
    named = [{"from": "me", "text": "Tim, you there?", "at": 1}]
    assert groupchat.reply_odds(tim, group, named, energy=0.01) == 1.0
    chatter = [{"from": "nightwing", "text": "lol", "at": 1}]
    assert groupchat.reply_odds(tim, group, chatter, 1.0) > groupchat.reply_odds(tim, group, chatter, 0.2)
    for _ in range(4):
        groupchat.spend(group, "nightwing")
    assert groupchat.energy(group) < groupchat.QUIET
    groupchat.spend(group, "me")
    assert groupchat.energy(group) == groupchat.FULL


def test_he_posts_and_each_member_reads_in_their_own_time(monkeypatch):
    console = web.Console.__new__(web.Console)
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
    assert group.unread_for("robin")[-1]["text"] == "where is everyone"   # system lines aren't "unread"
    assert group.remove_member("robin") and "robin" not in group.members


def test_a_member_walking_out_or_bringing_someone_in(monkeypatch):
    console = web.Console.__new__(web.Console)
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
