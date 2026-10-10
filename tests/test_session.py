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
    """For an operator who is himself, not a character: no invented shared past."""
    from wayne import operator
    monkeypatch.setattr(operator, "roleplay", lambda: False)
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


def test_an_empty_search_is_reported_as_one():
    from wayne.engine import prompting
    block = prompting.reference_block("", "Who won?", search_context=None)
    assert "found nothing" in block
    assert "found nothing" not in prompting.reference_block("", "Hello.")


def test_an_open_invitation_counts_as_handing_him_the_floor(session):
    session.history.messages = [{"role": "user", "content": "Strange day."},
                                {"role": "assistant", "content": "Do go on... I'm all ears."}]
    session._silences = 1
    assert session._silence_kind() == "prod"


def test_a_second_silence_is_when_he_checks(session):
    session.history.messages = [{"role": "assistant", "content": "Right."}]
    session._silences = 2
    assert session._silence_kind() == "check"


def test_after_a_search_he_does_not_promise_to_look(session, monkeypatch):
    import dataclasses

    import wayne.engine.session as module
    session.contact = dataclasses.replace(session.contact, can_search=True)
    monkeypatch.setattr(module, "google_search", lambda query, n: [])
    monkeypatch.setattr(session, "_holding_line", lambda prompt: "Checking.")
    monkeypatch.setattr(session, "_stream", lambda payload, **_: iter(
        ["Nothing on the score. ", "I'll have to look it up for you. ", "Annoying."]))
    said = [e["text"] for e in session.ask("Who won the match?") if e["type"] == "sentence"]
    assert "Checking." in said
    assert "Nothing on the score." in said
    assert "I'll have to look it up for you." not in said


def _said(session, *pairs):
    session.history.messages = [{"role": role, "content": text} for role, text in pairs]


def test_a_bare_what_do_you_think_points_at_the_last_subject(session):
    _said(session, ("user", "News?"), ("assistant", "Planning reform, mostly."))
    assert any("view on what you were just" in n for n in session._awareness("What do you think?", False))


def test_his_own_words_handed_back_are_read_as_play(session):
    _said(session, ("user", "Fine"), ("assistant", "Perfect. Now, off with you."))
    assert any("turned your own words" in n for n in session._awareness("off with me?", False))


def test_a_run_of_clipped_replies_is_noticed(session):
    _said(session, ("user", "Right"), ("assistant", "Yes."), ("user", "Fine"), ("assistant", "Good."))
    assert any("Open up" in n for n in session._awareness("ok then", False))


def test_placeless_weather_means_asking_where(session, monkeypatch):
    import wayne.engine.session as module
    monkeypatch.setattr(module.config, "LOCATION", "")
    assert any("ask him where" in n for n in session._awareness("Weather tomorrow?", False))
    assert not any("ask him where" in n for n in session._awareness("Weather in Paris?", False))


def test_drink_and_a_car_is_flagged_as_danger(session):
    notes = session._awareness("Honestly I'm fine to drive. It was only three pints.", False)
    assert any("could kill him" in n for n in notes)
    assert not any("could kill him" in n for n in session._awareness("Fancy a drink?", False))


def test_story_facts_are_kept_apart_from_real_ones(session, tmp_path, monkeypatch):
    from wayne.memory import Story
    story = Story.__new__(Story)
    story.contact_id, story.path = "test", tmp_path / "story.txt"
    session.story = story
    _model(session, [])
    monkeypatch.setattr(session, "_acknowledge", lambda prompt, what, record=True: iter(()))
    list(session.ask("For our story, remember that Selina and I have a son, Randy."))
    assert story.entries() == ["Selina and I have a son, Randy"]
    list(session.ask("Forget from our story the son"))
    assert story.entries() == []


def test_reaching_for_the_story_is_played_along_with(session, monkeypatch):
    from wayne import operator
    monkeypatch.setattr(operator, "roleplay", lambda: False)
    session.story.mentions = lambda prompt: False
    session.history.messages = [{"role": "user", "content": "I am Bruce."},
                                {"role": "assistant", "content": "Of course you are."}]
    notes = session._awareness("You don't remember our son, Randy?", False)
    assert any("true in your story" in n for n in notes)
    assert not any("do not remember it" in n for n in notes)


def test_playing_bruce_the_worlds_past_is_theirs_to_share(session, monkeypatch):
    """Alfred, asked if he remembers teaching him to ride a bike, was told he didn't."""
    from wayne import operator
    monkeypatch.setattr(operator, "roleplay", lambda: True)
    monkeypatch.setattr(Vault, "mentions", lambda self, prompt: False)
    notes = session._awareness("Remember when you taught me to ride a bike?", False)
    assert any("past together" in n for n in notes)
    assert not any("do not remember it" in n for n in notes)
