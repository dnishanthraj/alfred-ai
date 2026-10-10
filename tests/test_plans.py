"""Their week, and the plans he makes with them: read, answered, kept — and turned up to."""
import datetime
import json
import time
from types import SimpleNamespace

import pytest

from wayne import paths
from wayne.engine import plans, presence, week


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path / cid)
    presence._registry.clear()
    presence._LEADS.clear()
    yield tmp_path
    presence._registry.clear()


def _contact(cid, name, habits=(), home="Wayne Manor"):
    return SimpleNamespace(id=cid, name=name, full_name=name, routine=(), week=tuple(habits), circle=(),
                           home=home, gets_about="drive", shares_status=True, texting_pace={"phone": 1.0},
                           texting_style={}, initiative={}, texting="", beat=())


def _at(days_ahead, hour, minute=0):
    day = datetime.date.today() + datetime.timedelta(days=days_ahead)
    return datetime.datetime(day.year, day.month, day.day, hour, minute).timestamp()


def test_a_habit_falls_on_its_days_and_every_few_weeks_on_its_own_count():
    gym = {"doing": "the gym", "days": [0, 2, 4], "from": "18:00", "to": "19:30", "where": "Dooley's Gym", "chance": 1.0}
    beach = {"doing": "the beach", "days": [6], "from": "12:00", "to": "16:00", "where": "The Boardwalk", "every": 3,
             "chance": 1.0}
    c = _contact("robin", "Tim", [gym, beach])
    days = [_at(k, 12) for k in range(28)]
    on_gym = [datetime.date.fromtimestamp(t).weekday() for t in days if gym in week.on(c, t)]
    assert on_gym and set(on_gym) <= {0, 2, 4}
    sundays = [t for t in days if datetime.date.fromtimestamp(t).weekday() == 6]
    beach_days = [t for t in sundays if beach in week.on(c, t)]
    assert 1 <= len(beach_days) <= 2                                    # one Sunday in three
    t = next(t for t in days if gym in week.on(c, t))
    assert week.block(c, t + 6.5 * 3600)["where"] == "Dooley's Gym"      # 18:30 that day
    assert week.block(c, t) is None                                      # noon: free


def test_a_shared_every_other_sunday_lands_on_the_same_sundays_for_everyone():
    dinner = {"doing": "dinner at the Manor", "days": [6], "from": "17:30", "to": "20:30", "where": "Wayne Manor",
              "every": 2, "phase": 0, "chance": 1.0}
    dick, tim = _contact("nightwing", "Dick", [dinner]), _contact("robin", "Tim", [dinner])
    for k in range(28):
        t = _at(k, 12)
        assert (dinner in week.on(dick, t)) == (dinner in week.on(tim, t))


def test_only_a_when_and_a_lets_make_a_plan_worth_reading():
    assert plans.maybe_proposal("Everyone, dinner at Ralli's Friday at 8?")
    assert plans.maybe_proposal("let's grab drinks tonight")
    assert not plans.maybe_proposal("dinner was good")
    assert not plans.maybe_proposal("see you tomorrow")


def test_a_plan_is_read_answered_and_kept_to(private_data, monkeypatch):
    from wayne.engine import model as llm
    friday = datetime.date.fromtimestamp(_at(3, 12)).isoformat()
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {"message": {"content": json.dumps(
        {"plan": True, "what": "dinner at Ralli's", "where": "Ralli's Family Restaurant", "date": friday,
         "time": "20:00", "hours": 2, "who": ["everyone"]})}})
    asked = [("nightwing", "Dick"), ("robin", "Tim"), ("redhood", "Jason")]
    plan = plans.read("Everyone — Ralli's, Friday at 8?", asked, "m", {})
    assert plan["where"] == "Ralli's Family Restaurant" and plan["who"] == ["nightwing", "robin", "redhood"]
    kept = plans.add(plan, made_in="g:1")
    assert plans.add(dict(plan), made_in="g:1")["id"] == kept["id"]     # said twice: one plan

    dick, tim, jason = (_contact(c, n, home=h) for c, n, h in
                        (("nightwing", "Dick", "Home, Blüdhaven"), ("robin", "Tim", "Houseboat, Gotham Marina"),
                         ("redhood", "Jason", "Jason's safehouse")))
    assert "[rsvp: yes]" in plans.note(tim) and "You haven't said yet" in plans.note(tim)
    plans.answer(kept["id"], "nightwing", "yes", "Wouldn't miss it.")
    plans.answer(kept["id"], "redhood", "no", "Got a thing.")
    plans.answer(kept["id"], "robin", "yes", "in")
    book = {c.id: c for c in (dick, tim, jason)}
    said = plans.note(dick, book)
    assert "You've said you'll come to dinner at Ralli's" in said and "Tim" in said and "Jason can't make it" in said
    assert "Family Restaurant" not in said                              # the place isn't said twice
    assert not plans.pending("robin") and plans.kept("robin")

    # On the night: Dick and Tim set off in time and are there, together; Jason isn't.
    at = kept["at"]
    for c in (dick, tim):
        state = presence.Presence(c)
        assert state.now(at + 1800)["doing"] == "dinner at Ralli's"
        assert state._own_place(at + 1800) == "Ralli's Family Restaurant"
        lead = state._lead(kept)
        assert 10 <= lead <= 90
        assert state._own_place(at - (lead - 2) * 60) == "Ralli's Family Restaurant"      # already on the way
    assert presence.Presence(jason).now(at + 1800)["doing"] != "dinner at Ralli's"


def test_a_maybe_is_a_coin_tossed_on_the_day(private_data):
    plan = plans.add({"what": "bowling", "where": "", "at": _at(1, 20), "until": _at(1, 22), "who": ["robin"]})
    plans.answer(plan["id"], "robin", "maybe")
    stored = plans.all_plans()[0]
    assert plans.going(stored, "robin") == plans.going(stored, "robin")   # the same answer however often asked
    assert plans.when(_at(1, 20), _at(0, 9)).startswith("tomorrow at 8")


def test_they_know_the_landmarks_and_their_own_corners_and_only_some_of_the_rest():
    from wayne.engine import places
    tim = _contact("robin", "Tim", home="Houseboat, Gotham Marina")
    venues = [{**v, "kind": "venue"} for v in places.venues()]
    mine = places.resolve("Houseboat, Gotham Marina")["area"]
    assert all(places.knows(tim, v) for v in venues if v["area"] == mine)          # round where he lives: all of them
    share = sum(places.knows(tim, v) for v in venues) / len(venues)
    assert 0.5 < share < 0.95                                                      # elsewhere, some
    assert places.knows(tim, places.resolve("Wayne Manor"))                       # a landmark: everyone
    unknown = next(v for v in venues if not places.knows(tim, v))
    assert "you don't know it" in places.note(f"meet at {unknown['name']}", contact=tim)
