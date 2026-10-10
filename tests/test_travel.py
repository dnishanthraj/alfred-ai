"""Getting about: by the roads, over the bridges, taking as long as the roads take — never a jump."""
import math
import time
from types import SimpleNamespace

import pytest

from wayne import paths
from wayne.engine import places, presence, travel


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path)
    presence._registry.clear()
    yield
    presence._registry.clear()


def test_the_manor_to_the_tower_is_a_drive_over_the_bridge():
    manor, tower = places.resolve("Wayne Manor"), places.resolve("Wayne Tower")
    pts, minutes = travel.route((manor["x"], manor["y"]), (tower["x"], tower["y"]), "Wayne Manor", "Wayne Tower")
    assert len(pts) > 20                                  # along the roads, not a straight line
    assert 20 < minutes < 60
    # It never cuts straight across: every step is short, as roads are.
    assert max(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False)) < 8


def test_arkham_is_reached_by_its_one_bridge():
    asylum, dome = places.resolve("Arkham Asylum"), places.resolve("Knightsdome")
    pts, _ = travel.route((asylum["x"], asylum["y"]), (dome["x"], dome["y"]), "Arkham Asylum", "Knightsdome")
    bridge = (45.0, 47.4)                                 # the Arkham Bridge, over the channel
    assert min(math.dist(p, bridge) for p in pts) < 1.5


def test_someone_on_a_trip_is_somewhere_along_it():
    trip = {"pts": [[0, 0], [10, 0]], "start": 100, "end": 200}
    assert travel.position(trip, 50) == (0, 0)
    assert travel.position(trip, 150) == pytest.approx((5, 0))
    assert travel.position(trip, 250) == (10, 0)


def test_a_move_seen_is_travelled_and_one_unseen_just_is():
    dick = SimpleNamespace(id="nightwing", name="Dick", home="Wayne Manor", routine=(), shares_status=True,
                           texting_pace={}, initiative={})
    state = presence.of(dick)
    manor, tower = places.resolve("Wayne Manor"), places.resolve("Wayne Tower")
    assert state.trip("Wayne Manor", manor, t=1000) is None       # first look: he's simply there
    trip = state.trip("Wayne Tower", tower, t=1060)               # seen to change: he goes
    assert trip and trip["end"] > trip["start"] and len(trip["pts"]) > 2
    assert state.trip("Wayne Tower", tower, t=1100) is trip       # the same journey, still on it
    assert state.trip("Wayne Tower", tower, t=trip["end"] + 1) is None
    state._looked = None                                          # the console was off overnight
    assert state.trip("Wayne Manor", manor, t=trip["end"] + 9 * 3600) is None


def _at(hour, minute=0):
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))


def test_rung_just_after_leaving_he_is_on_the_way_not_already_there():
    # Called just after he left Burnside, Tim said he was at the university library.
    routine = ({"from": 9, "to": 14, "doing": "at Burnside College", "where": "Burnside", "status": "busy", "drift": 0},
               {"from": 14, "to": 18, "doing": "studying", "where": "Gotham University", "status": "busy", "drift": 0})
    tim = SimpleNamespace(id="robin", name="Tim", home="Wayne Manor", routine=routine, shares_status=True,
                          texting_pace={}, initiative={}, gets_about="drive")
    state = presence.of(tim)
    line = state.note(t=_at(14, 4))
    assert "on your way to Gotham University" in line and "aren't there yet" in line
    assert "you left Burnside" in line and "studying" in line
    # Long since there, he simply is.
    assert state.note(t=_at(16, 30)).startswith("Right now you're studying, at Gotham University")


def test_on_a_ferry_or_a_bridge_the_crossing_is_finished_before_turning_round():
    trip = {"pts": [[68.9, 22.0], [80.0, 16.0], [95.0, 8.0], [117.9, 3.5]], "start": 0, "end": 1000}   # out across the bay
    mid = next(t for t in range(0, 1000, 20) if travel.over_water(*travel.position(trip, t)))
    lead, landed, at = travel.landfall(trip, mid)
    assert not travel.over_water(*landed) and at > mid                 # ashore, later
    assert all(lead[i] in trip["pts"] for i in range(1, len(lead)))     # along the way they were going
    assert travel.landfall(trip, 1000) is None                          # arrived: nothing to finish


def test_out_on_the_water_the_card_says_what_its_on():
    assert travel.crossing(91.0, 22.5) == "over the Pioneers Bridge"
    tower = places.resolve("Wayne Tower")
    assert travel.crossing(tower["x"], tower["y"]) == ""                     # on dry land: nothing to say


def test_someone_still_on_the_way_is_not_with_anyone_yet():
    def contact(cid, name, home):
        return SimpleNamespace(id=cid, name=name, full_name=name, home=home, routine=(), week=(), circle=(),
                               shares_status=True, texting_pace={}, initiative={}, gets_about="drive", beat=())
    dick, tim = contact("nightwing", "Dick", "Home, Blüdhaven"), contact("robin", "Tim", "Houseboat, Gotham Marina")
    book = {"nightwing": dick, "robin": tim}

    class Book(dict):
        def __iter__(self):
            return iter(self.values())
    import wayne.contacts as contacts_pkg
    from wayne.contacts import profile
    t0 = time.time()
    d, s = presence.Presence(dick), presence.Presence(tim)
    presence._registry.update({"nightwing": d, "robin": s})
    d.set_activity("playing Mario Kart", presence.BUSY, 120, t=t0, where="Home, Blüdhaven", company=["robin"])
    original = profile.directory
    profile.directory = lambda: Book(book)
    contacts_pkg.directory = profile.directory
    try:
        where, company = d.whereabouts(t0 + 60)
        tim_trip = s.trip(where, places.resolve(where), t0 + 60)
        assert tim_trip and tim_trip["end"] > t0 + 600                       # he's driving over from the marina
        assert d.whereabouts(t0 + 120)[1] == []                               # so Dick isn't with him yet
        waiting = d.note(t0 + 120)
        assert "waiting on Tim" in waiting and "hasn't started" in waiting and "You're with Tim" not in waiting
        assert "middle of it" not in waiting
        assert "You're meeting Dick" in s.note(t0 + 120) and "Dick's place" in s.note(t0 + 120)   # not "home"
        assert s.label("Home, Blüdhaven") == "Dick's place, Blüdhaven"
        assert d.whereabouts(tim_trip["end"] + 30)[1] == ["robin"]           # arrived: together
    finally:
        profile.directory = original
        contacts_pkg.directory = original
