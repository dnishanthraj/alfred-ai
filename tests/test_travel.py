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
