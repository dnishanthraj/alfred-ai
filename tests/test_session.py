"""The engine's turn: stopping the model when nothing more is wanted."""

import pytest

from wayne.contacts.profile import Contact
from wayne.engine import ContactSession
from wayne.memory import History, Vault


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.setattr(History, "save", lambda self: None)
    contact = Contact(
        id="test", name="Tester", full_name="Tester", role="", tagline="",
        model="none", voice_id="", accent="", max_reply_sentences=2,
        can_search=False, options={}, boot_prompts={}, system="You are a tester.")
    s = ContactSession.__new__(ContactSession)
    ContactSession.__init__(s, contact)
    s.history.messages = []
    s.vault = Vault.__new__(Vault)
    monkeypatch.setattr(Vault, "as_block", lambda self, prompt="": "")
    monkeypatch.setattr(Vault, "mentions", lambda self, prompt: False)
    s.already_greeted = True
    return s


def _model(session, read):
    """A fake model streaming numbered sentences forever, counting reads."""
    def stream(payload, **_):
        i = 0
        while True:
            i += 1
            read.append(i)
            yield f"Line number {i} is here. "
    session._stream = stream


def test_the_model_stops_being_read_at_the_sentence_cap(session):
    read = []
    _model(session, read)
    replies = [e["text"] for e in session.ask("Tell me a story.") if e["type"] == "reply_end"]
    assert replies == ["Line number 1 is here. Line number 2 is here."]
    assert len(read) <= 4


def test_an_interrupted_reply_is_remembered_as_far_as_it_got(session):
    read = []
    _model(session, read)
    turn = session.ask("Tell me a story.")
    for event in turn:
        if event["type"] == "sentence":
            break
    turn.close()
    assert session.history.messages[-1]["content"] == "Line number 1 is here."


def test_an_unknown_shared_memory_is_flagged_and_a_known_one_is_not(session, monkeypatch):
    monkeypatch.setattr(Vault, "mentions", lambda self, prompt: False)
    notes = session._awareness("You remember that weekend in Cornwall, right?", False)
    assert any("do not remember it" in n for n in notes)

    session.history.messages = [{"role": "user", "content": "We went to Cornwall in May."},
                                {"role": "assistant", "content": "So you did."}]
    notes = session._awareness("You remember that weekend in Cornwall, right?", False)
    assert not any("do not remember it" in n for n in notes)


def test_reminiscing_is_not_a_memory_command(session):
    _model(session, [])
    events = list(session.ask("Remember that night we got caught in the rain?"))
    assert not any(e.get("text") == "Noted. Stored to the vault." for e in events)
