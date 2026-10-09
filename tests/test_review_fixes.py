"""Regressions found in review: each test is a bug that shipped once."""
import time
from types import SimpleNamespace

import pytest

from wayne import paths
from wayne.engine import guards, initiative, presence
from wayne.memory import store
from wayne.memory.vault import Vault


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path)
    presence._registry.clear()
    yield
    presence._registry.clear()


def test_forget_about_it_is_not_an_instruction_to_forget_everything(tmp_path):
    vault = Vault.__new__(Vault)
    vault.path = tmp_path / "vault.txt"
    store.atomic_write(vault.path, "- Dinner with Selina at the city club\n- The kitchen needs fixing\n")
    assert vault.forget("it") == []
    assert len(vault.entries()) == 2
    assert vault.forget("dinner with selina") and len(vault.entries()) == 1


@pytest.mark.parametrize("line", ["Long night.", "I'm tired of waiting, what did you find?",
                                  "I'll see you at the gala, what should I wear?"])
def test_remarks_that_sound_like_goodbyes_do_not_end_the_call(line):
    assert not guards.user_is_leaving(line)


@pytest.mark.parametrize("line", ["Night.", "goodnight", "Bye.", "Going to bed."])
def test_real_goodbyes_still_do(line):
    assert guards.user_is_leaving(line)


def test_the_address_guard_leaves_ordinary_nouns_alone():
    terms = ("kid", "son")
    assert guards.strip_forbidden_address("Tim's a good kid.", terms) == "Tim's a good kid."
    assert guards.strip_forbidden_address("He is your son.", terms) == "He is your son."
    assert guards.strip_forbidden_address("Thanks, kid.", terms) == "Thanks."


def test_unreadable_memory_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "history.json"
    path.write_bytes(b"gAAAAA" + b"x" * 80)          # ciphertext no key can open
    assert store.read_text(path) == ""
    assert not path.exists()
    assert list(tmp_path.glob("history.json.undecryptable-*"))


def test_a_tic_used_lately_is_left_out_of_the_next_text():
    contact = SimpleNamespace(texting_style={"tics": ["lol", "😂"]})
    assert initiative.untic(contact, "on my way lol", ["haha ok lol"]) == "on my way"
    assert initiative.untic(contact, "on my way lol", ["sure thing"]) == "on my way lol"
    assert initiative.untic(contact, "lol", ["lol"]) == "lol"        # never empties a text


def test_someone_who_does_not_share_their_status_shows_as_unknown():
    contact = SimpleNamespace(id="redhood", routine=(), texting_pace={}, shares_status=False)
    state = presence.of(contact)
    state.touch()
    assert state.public()["status"] == "unknown" and state.public()["doing"] == ""
    state.set_line(state.line_key(), "no")
    assert state.public()["line"] == "no"


def test_the_call_start_survives_the_history_being_trimmed():
    from wayne.engine.session import ContactSession
    session = ContactSession.__new__(ContactSession)
    old = time.time() - 100
    session.history = SimpleNamespace(messages=[
        {"role": "user", "content": "old", "at": old}, {"role": "assistant", "content": "x", "at": old}])
    session.mark_call_start()
    session.history.messages += [
        {"role": "user", "content": "Alfred?", "at": time.time(), "marker": True},
        {"role": "assistant", "content": "Sir.", "at": time.time()},
        {"role": "user", "content": "the docks", "at": time.time()}]
    del session.history.messages[:2]                 # trimmed from the front, as save() does
    said = session.call_messages()
    assert [m["content"] for m in said] == ["Sir.", "the docks"]
    assert session._said_this_call() == 1
