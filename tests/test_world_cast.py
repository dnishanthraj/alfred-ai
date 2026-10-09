"""The operator profile, who knows what, and news that travels."""
import time
from types import SimpleNamespace

from wayne import operator
from wayne.engine import grapevine


def test_bruce_is_the_default_operator():
    assert operator.full_name() == "Bruce Wayne"


def test_secret_identities_reach_only_those_who_know():
    family = operator.briefing("alfred")
    selina = operator.briefing("catwoman")
    assert "Nightwing" in family and "Batwing" in family
    assert "Randy Wayne flies as Batwing" not in selina
    assert "Bruce is Batman" in selina or "He is Batman" in selina


def test_each_contact_has_their_own_way_of_addressing_him():
    assert "Master Bruce" in operator.briefing("alfred")
    assert "Mr. Wayne" in operator.briefing("lucius")
    assert "old man" in operator.briefing("redhood").lower()


def test_news_about_a_mask_never_reaches_selina():
    assert grapevine._crosses_secret("Bruce said Batwing crashed the jet", "catwoman")
    assert not grapevine._crosses_secret("Bruce said Batwing crashed the jet", "alfred")
    assert not grapevine._crosses_secret("Bruce said he's tired", "catwoman")


def test_closeness_is_symmetric():
    assert grapevine.closeness("nightwing", "batgirl") == grapevine.closeness("batgirl", "nightwing") > 0.5


def test_a_call_he_asked_to_keep_quiet_doesnt_travel(monkeypatch):
    called = []
    monkeypatch.setattr(grapevine, "_notable", lambda s, e: called.append(1) or ["Bruce said X"])
    session = SimpleNamespace(contact=SimpleNamespace(id="nightwing"), history=SimpleNamespace(messages=[
        {"role": "user", "content": "Keep this between us, okay?"}, {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "I'm thinking of retiring."}, {"role": "assistant", "content": "Whoa."}]))
    grapevine.note_call(session, [], 0)
    time.sleep(0.05)
    assert not called
