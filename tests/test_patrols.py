"""Patrols: tonight's roster, a sector each, the runs across the roofs — and what a comms channel knows."""
import time
from types import SimpleNamespace

import pytest

from wayne import paths
from wayne.engine import patrols, presence


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path / cid)
    presence._registry.clear()
    patrols._rosters.clear()
    yield tmp_path
    presence._registry.clear()
    patrols._rosters.clear()


def _night(days_ago, hour=23, minute=30):
    lt = time.localtime(time.time() - days_ago * 86400)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def test_tonights_roster_never_puts_two_on_one_sector_unless_theyre_a_pair(private_data):
    paired = 0
    for days in range(10):
        roster = patrols.roster(_night(days))
        by_sector = {}
        for cid, on in roster.items():
            assert patrols.is_sector(on["sector"])
            by_sector.setdefault(on["sector"], []).append(cid)
            for mate in on["with"]:
                assert roster[mate]["with"] == [cid] and roster[mate]["sector"] == on["sector"]
        for sector, who in by_sector.items():
            assert len(who) == 1 or (len(who) == 2 and roster[who[0]]["with"] == [who[1]]), (sector, who)
        paired += sum(1 for on in roster.values() if on["with"])
    assert paired                                   # some nights, two of them go out together


def test_a_patrol_runs_across_the_roofs_then_watches(private_data):
    sector = "Crime Alley"
    t0 = (int(time.time()) // patrols.WATCH) * patrols.WATCH + 900
    seen = [patrols.where("orphan", sector, t0 + k * 30) for k in range(48)]
    assert any(s["leg"] for s in seen) and any(not s["leg"] for s in seen)     # moving, and stopped to look
    assert len({(s["x"], s["y"]) for s in seen if not s["leg"]}) >= 2           # from more than one roof
    centre = (67.4, 44.6)
    assert all(abs(s["x"] - centre[0]) < 9.5 and abs(s["y"] - centre[1]) < 9.5 for s in seen)


def test_a_game_on_the_sofa_is_played_at_home_not_on_the_docks(private_data):
    dick = SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson", shares_status=True,
                           shares_location=True, home="Home, Blüdhaven", texting_pace={},
                           beat=("Waterloo Docks, Blüdhaven",),
                           routine=({"from": 0, "to": 24, "doing": "on patrol in Blüdhaven", "status": "online",
                                     "drift": 0},))
    whereabouts = presence.of(dick)
    assert patrols.is_sector(whereabouts.whereabouts()[0])
    whereabouts.set_activity("playing Mario Kart", presence.BUSY, 60)
    assert whereabouts.whereabouts()[0] == "Home, Blüdhaven"       # nobody games on a rooftop at the docks


def _comms(monkeypatch, team, reply):
    from wayne.contacts import directory
    from wayne.engine import ContactSession, cases
    from wayne.memory import History, Vault
    from wayne.memory import groups as store
    monkeypatch.setattr(History, "save", lambda self: None)
    monkeypatch.setattr(Vault, "as_block", lambda self, prompt="": "")
    monkeypatch.setattr(Vault, "mentions", lambda self, prompt: False)
    report = {"id": "r-comms", "kind": "Armed robbery", "severity": 3, "place": "Ralli's Family Restaurant",
              "area": "Otisburg", "x": 49.0, "y": 30.0, "at": time.time() - 300}
    for member in team:
        cases.assign(report, member, by="self", travel=5)
    group = store.create("Comms · Armed robbery", [m for m in team if m != "bruce"])
    group.update_meta(lambda m: m.update({"case": "r-comms", "comms": True}))
    session = ContactSession(directory().get(team[0]))
    monkeypatch.setattr(session, "_chat_once", lambda payload, **kw: reply)
    return session, group


def test_a_comms_post_cut_off_mid_marker_is_never_sent_anywhere(private_data, monkeypatch):
    session, group = _comms(monkeypatch, ["redhood", "batgirl"],
                            "south alley clear. keep those drones high\n[dm: you're late. gordon better be halfway here")
    text = session.group_post(group, [{"from": "batgirl", "text": "Status?", "at": time.time()}])
    assert "[dm" not in text and "late" not in text and "drones high" in text
    assert not session._group_actions["dm"]


def test_comms_know_whether_he_is_on_it_and_where_he_is(private_data, monkeypatch):
    session, group = _comms(monkeypatch, ["redhood", "batgirl"], "on it")
    note = session._comms_note(group)
    assert "isn't on this one" in note and "Wayne Manor" in note and "Barbara" in note
    session, group = _comms(monkeypatch, ["batgirl", "bruce"], "on it")
    assert "on this one with you" in session._comms_note(group)


def test_alfred_sees_everything_on_the_console(private_data, monkeypatch):
    from wayne.contacts import directory
    from wayne.engine import ContactSession
    from wayne.memory import History
    monkeypatch.setattr(History, "save", lambda self: None)
    alfred = ContactSession(directory().get("alfred"))
    view = alfred._batcomputer("what's going on tonight")
    assert "Where everyone is" in view and "Bruce: at" in view and "Batwing" in view
    assert "scanner" in view.lower()


def test_going_to_meet_one_of_them_puts_them_where_that_one_is(private_data):
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", shares_status=True, shares_location=True,
                          home="Wayne Manor", texting_pace={}, beat=(), routine=())
    cass = SimpleNamespace(id="orphan", name="Cass", full_name="Cassandra Cain", shares_status=True,
                           shares_location=True, home="Home, Burnside", texting_pace={}, beat=(), routine=())
    presence.of(cass).set_activity("at the gym", presence.BUSY, 120, where="Dooley's Gym")
    presence.of(tim).set_activity("on the way to meet Cass", presence.BUSY, 90, follow="orphan")
    import wayne.contacts as contacts
    book = {"robin": tim, "orphan": cass}
    original = contacts.directory
    contacts.directory = lambda: type("B", (), {"get": staticmethod(book.get), "__iter__": lambda self: iter(book.values())})()
    try:
        assert presence.of(tim)._own_place(time.time()) == "Dooley's Gym"
        presence.of(cass).set_activity("at the Bijou", presence.BUSY, 120, where="The Bijou")
        assert presence.of(tim)._own_place(time.time()) == "The Bijou"          # where she goes, he goes
    finally:
        contacts.directory = original
