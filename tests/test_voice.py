"""The voice of the moment: how a case stands, and what someone sounds like in it."""
import time
from types import SimpleNamespace

import pytest

from wayne import delivery
from wayne.contacts.profile import Contact
from wayne.engine import ContactSession, cases, presence
from wayne.memory import History, Vault

NOW = time.time()


def _case(**kw):
    return {"id": "c1", "kind": "Armed robbery", "severity": 3, "status": "on scene", "travel": 6,
            "updated_at": NOW - 60, "due": NOW + 3000, **kw}


def test_a_case_moves_from_the_road_to_the_scene_to_the_end():
    assert cases.phase(_case(status="assigned", updated_at=NOW - 60))[0] == "en route"
    assert cases.phase(_case(updated_at=NOW - 60, due=NOW + 3000))[0] == "arriving"
    middle = cases.phase(_case(updated_at=NOW - 1500, due=NOW + 1500))[0]
    assert middle in ("in it", "gone wrong")
    assert cases.phase(_case(updated_at=NOW - 2700, due=NOW + 300))[0] == "wrapping up"
    assert cases.phase(_case(status="closed"))[0] == "closed"


def test_whether_it_goes_wrong_is_settled_once_and_only_for_the_serious_ones():
    assert not cases.goes_wrong(_case(severity=2))
    serious = [_case(id=f"c{i}", severity=4) for i in range(400)]
    share = sum(cases.goes_wrong(c) for c in serious) / len(serious)
    assert 0.15 < share < 0.45
    assert [cases.goes_wrong(c) for c in serious[:20]] == [cases.goes_wrong(c) for c in serious[:20]]


@pytest.fixture
def tim(monkeypatch):
    monkeypatch.setattr(History, "save", lambda self: None)
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    contact = Contact(id="robin", name="Tim", full_name="Tim Drake", role="", tagline="", model="none",
                      voice_id="", accent="", max_reply_sentences=2, can_search=False, options={},
                      boot_prompts={}, system="You are Tim.")
    s = ContactSession.__new__(ContactSession)
    ContactSession.__init__(s, contact)
    s.history.messages = []
    s.vault = Vault.__new__(Vault)
    return s


def _living(monkeypatch, doing, status=presence.BUSY, where="Burnside", case=None):
    fake = SimpleNamespace(now=lambda t=None: {"status": status, "doing": doing},
                           whereabouts=lambda t=None: (where, []), get=lambda k, d=None: d)
    monkeypatch.setattr(presence, "of", lambda contact: fake)
    monkeypatch.setattr(cases, "active", lambda cid: case)


def test_mid_fight_they_may_not_get_a_sentence_out(tim, monkeypatch):
    _living(monkeypatch, "working the armed robbery", case=_case(updated_at=NOW - 1500, due=NOW + 1500))
    assert "[grunts]" in tim._voice_note()


def test_woken_they_can_sound_it(tim, monkeypatch):
    _living(monkeypatch, "asleep", status=presence.OFFLINE, where="Wayne Manor")
    assert "[yawns]" in tim._voice_note()


def test_on_a_stakeout_they_whisper_and_at_a_club_they_shout(tim, monkeypatch):
    _living(monkeypatch, "on a stakeout above the docks")
    assert "[whispers]" in tim._voice_note()
    _living(monkeypatch, "at a club with friends")
    assert "[shouting]" in tim._voice_note()


def test_an_ordinary_moment_has_no_cue_to_offer(tim, monkeypatch):
    _living(monkeypatch, "reading", where="home")
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v3")      # no scene sounds either
    assert tim._voice_note() == ""


def test_the_room_behind_the_voice_follows_what_they_are_doing(monkeypatch):
    from wayne.audio import ambience

    def living(doing="", trip=None, case=None, where="Burnside"):
        fake = SimpleNamespace(now=lambda t=None: {"status": presence.BUSY, "doing": doing},
                               whereabouts=lambda t=None: (where, []),
                               get=lambda k, d=None: trip if k == "trip" else d)
        monkeypatch.setattr(presence, "of", lambda contact: fake)
        monkeypatch.setattr(cases, "active", lambda cid: case)
    tim = SimpleNamespace(id="robin", home="Gotham Marina", beat=("Burnside",))
    living(case=_case(updated_at=NOW - 1500, due=NOW + 1500))
    assert ambience.scene_for(tim, NOW) == "scuffle"
    living(trip={"end": NOW + 600, "pts": [[0, 0], [5, 5]], "by": "driving"})
    assert ambience.scene_for(tim, NOW) == "car"
    living(doing="picking up groceries")
    assert ambience.scene_for(tim, NOW) == "grocery"
    assert {s["name"] for s in ambience.shots_for("grocery")} >= {"trolley", "checkout"}


def test_no_key_no_sounds(monkeypatch):
    from wayne.audio import ambience
    monkeypatch.setattr(ambience.config, "ELEVENLABS_API_KEY", "")
    assert ambience.clip("rooftop") is None and ambience.clip("siren") is None
