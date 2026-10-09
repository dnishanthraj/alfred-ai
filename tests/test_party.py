"""Calls with more than one contact: who answers, who hears what."""
from types import SimpleNamespace

from wayne import events
from wayne.engine.party import Call


class FakeMember:
    """A contact that replies with a scripted line and records what it heard."""

    def __init__(self, cid, name, full_name, replies=None):
        self.contact = SimpleNamespace(id=cid, name=name, full_name=full_name)
        self.history = SimpleNamespace(messages=[])
        self.replies = list(replies or ["Mm."])
        self.call, self.heard, self.asked = None, [], []

    def ask(self, prompt, interrupted=False, confidence=1.0, follow_up=False):
        self.asked.append((prompt, follow_up, list(self.heard)))
        self.heard = []
        reply = self.replies.pop(0) if self.replies else "Mm."
        yield events.sentence(0, reply)
        yield events.reply_end(reply)


def _call(*members):
    call = Call(operator="Him")
    for m in members:
        call.join(m)
    return call


def _run(call, text):
    return [e for e in call.turn(text)]


def test_naming_someone_routes_to_them():
    alfred, lucius = FakeMember("alfred", "Alfred", "Alfred Pennyworth"), FakeMember("lucius", "Lucius", "Lucius Fox")
    call = _call(alfred, lucius)
    out = _run(call, "Lucius, what do you think?")
    assert lucius.asked and not alfred.asked
    assert {e.get("speaker") for e in out if e["type"] == "sentence"} == {"lucius"}
    # Alfred heard both lines, labelled, for when he next speaks.
    assert alfred.heard == ["Him: Lucius, what do you think?", "Lucius Fox: Mm."]


def test_the_room_gets_everyone_in_turn():
    alfred, lucius = FakeMember("alfred", "Alfred", "Alfred Pennyworth"), FakeMember("lucius", "Lucius", "Lucius Fox")
    call = _call(alfred, lucius)
    _run(call, "What do you both make of it?")
    assert alfred.asked and lucius.asked
    assert lucius.asked[0][1] is True          # second speaker answers as a follow-up


def test_a_reply_naming_someone_brings_them_in_once():
    alfred = FakeMember("alfred", "Alfred", "Alfred Pennyworth", ["Ask Lucius, he built it."])
    lucius = FakeMember("lucius", "Lucius", "Lucius Fox", ["Alfred flatters me.", "Again?"])
    call = _call(alfred, lucius)
    call.last_speaker = alfred
    _run(call, "Will it hold?")
    assert len(alfred.asked) == 1 and len(lucius.asked) == 1   # no ping-pong


def test_lines_for_someone_else_are_dropped():
    alfred, lucius = FakeMember("alfred", "Alfred", "Alfred Pennyworth"), FakeMember("lucius", "Lucius", "Lucius Fox")
    call = _call(alfred, lucius)
    assert call.own_words(alfred, "Lucius: Indeed I did.") == ""
    assert call.own_words(alfred, "Alfred: Quite right.") == "Quite right."


def test_a_late_joiner_hears_what_was_said_before():
    alfred = FakeMember("alfred", "Alfred", "Alfred Pennyworth")
    alfred.history.messages = [{"role": "user", "content": "The car's wrecked."},
                               {"role": "assistant", "content": "Again?"}]
    call = _call(alfred)
    lucius = FakeMember("lucius", "Lucius", "Lucius Fox")
    call.join(lucius)
    assert lucius.heard == ["(before you joined) Him: The car's wrecked.",
                            "(before you joined) Alfred Pennyworth: Again?"]


def test_one_contact_alone_is_an_ordinary_call():
    alfred = FakeMember("alfred", "Alfred", "Alfred Pennyworth")
    call = _call(alfred)
    assert alfred.call is None and not call.is_group
    lucius = FakeMember("lucius", "Lucius", "Lucius Fox")
    call.join(lucius)
    assert alfred.call is call and lucius.call is call
    call.leave(lucius)
    assert alfred.call is None


def test_the_first_person_named_is_the_one_spoken_to():
    alfred, lucius = FakeMember("alfred", "Alfred", "Alfred Pennyworth"), FakeMember("lucius", "Lucius", "Lucius Fox")
    call = _call(alfred, lucius)
    assert call.addressed("Lucius, Alfred says the suit's too heavy.") == [lucius]
    assert call.addressed("What do you two reckon?") == [alfred, lucius]
    assert call.addressed("Right then.") == []
