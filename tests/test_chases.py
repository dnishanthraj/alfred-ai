"""Getting there in the suit, the Batwing, and the calls that turn and run."""
import time

import pytest

from wayne import paths
from wayne.engine import batman, cases, incidents, jet, outcomes, travel


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def _at(hour, minute=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def test_each_of_them_takes_the_fastest_way_they_have(monkeypatch):
    t = _at(14)
    randy = travel.fastest("batwing", (60.0, 45.0), (20.0, 110.0), t=t, jet=False)
    assert randy["by"] == "flying" and len(randy["pts"]) == 2       # straight there, water or not
    hop = travel.fastest("orphan", (66.0, 49.0), (67.4, 44.6), t=t, jet=False)
    assert hop["by"] == "over the rooftops"
    far = travel.fastest("nightwing", (66.0, 49.0), (40.0, 108.0), t=t, jet=False)
    assert far["by"] == "on the bike"
    bruce = travel.fastest("bruce", (66.0, 49.0), (40.0, 108.0), t=t, jet=False)
    assert bruce["by"] == "in the Batmobile" and bruce["end"] < far["end"]


def test_only_he_flies_the_batwing_and_he_can_give_one_of_them_a_lift():
    t = _at(23)
    home = jet.home()
    near = (home[0] + 20, home[1] + 10)
    far = (near[0] + 10, near[1] + 70)
    assert travel.fastest("nightwing", near, far, t=t, book=True)["by"] != "on the Batwing"   # not theirs to call
    tim_at, drop = (near[0] + 6, near[1] + 4), (far[0] - 3, far[1] - 2)
    his, theirs = jet.lift("robin", near, tim_at, drop, t)
    assert theirs["start"] > t and theirs["pts"][0] == list(tim_at)        # Tim waits where he is till it's there
    assert theirs["end"] == his["end"] and his["pts"][-1] == list(drop)     # dropped off together; he stays with it
    assert jet.lift("orphan", near, tim_at, drop, t + 30) is None           # one flight at a time
    assert jet.state(his["end"] + 60)["status"].startswith("hanging where you got out")
    assert jet.state(his["end"] + jet.IDLE + 600)["parked"]                 # and home on its own


def test_he_goes_as_batman_to_a_case_and_at_night_and_as_bruce_by_day(monkeypatch):
    day = _at(13)
    trip = batman.go("a pin", 30.0, 95.0, t=day)
    assert trip["by"] == "driving" and not trip["suit"]
    case_trip = batman.go("Crime Alley", 67.4, 44.6, case="r1", t=day + 3600)
    assert case_trip["suit"] and case_trip["by"] in ("over the rooftops", "in the Batmobile", "on the Batwing")
    assert batman.state(day + 3600)["suit"]
    batman.off_case("r1")
    night = _at(23)
    assert batman.go("a pin", 20.0, 100.0, t=night)["suit"]


def _report(kind, rid="t-run-1", at=None, place=("Ralli's Family Restaurant", "Otisburg", 49.0, 30.2)):
    at = at or time.time() - 60
    return {"id": rid, "kind": kind, "severity": incidents.SEVERITY[kind], "place": place[0], "area": place[1],
            "x": place[2], "y": place[3], "at": at, "ends": at + 3 * 3600, "status": "reported",
            "toll": {"dead": 0, "hurt": 0}, "crew": 2}


def test_a_getaway_nobody_stops_is_gone_and_one_they_stop_stays_stopped():
    report = _report("Vehicle pursuit")
    moving = incidents._as_it_stands(report, time.time(), {})
    assert moving["moving"] and moving["route"]["by"] == "by car"
    end = moving["route"]["end"]
    assert incidents._as_it_stands(report, end + 5, {}) is None          # clean away: off the scanner
    stop = {"start": round(moving["route"]["start"]), "x": 51.0, "y": 33.0, "at": time.time() + 120, "caught": True}
    held = incidents._as_it_stands(report, end + 5, {report["id"]: {"id": report["id"], "chase": stop}})
    assert held["stopped"] and (held["x"], held["y"]) == (51.0, 33.0)


def test_a_call_turns_the_same_way_every_time_unless_someone_was_there_in_time():
    found = None
    for i in range(400):
        report = _report("Armed robbery", rid=f"t-turn-{i}", at=time.time() - 3600)
        stages = incidents.turns(report)
        assert stages == incidents.turns(dict(report))
        if len(stages) > 1:
            found = (report, stages)
            break
    assert found, "an armed robbery should turn now and then"
    report, stages = found
    early = {"id": report["id"], "kind": report["kind"], "severity": 3, "suspect": "", "crew": 2,
             "team": ["bruce", "orphan", "nightwing"],
             "members": {m: {"joined": report["at"], "travel": 0.1} for m in ("bruce", "orphan", "nightwing")}}
    held = [len(incidents.turns({**report, "id": f"{report['id']}-{k}"}, {**early, "id": f"{report['id']}-{k}"})) == 1
            for k in range(30)]
    assert any(held)                                                     # there in time, it didn't get that far


def test_thirty_of_banes_men_are_too_many_for_one_of_them():
    base = {"id": "t-bane", "kind": "Riot", "severity": 3, "suspect": "Bane", "crew": 30, "opened_at": time.time()}
    tim = outcomes.chance({**base, "team": ["robin"]})
    crew = outcomes.chance({**base, "team": ["orphan", "nightwing", "bruce"]})
    assert tim < 0.15 < crew
    assert outcomes.outnumbered({**base, "team": ["robin"]}) > outcomes.outnumbered({**base, "team": ["orphan", "nightwing"]})


def test_a_chase_ends_where_it_was_stopped_or_it_doesnt():
    case = {"id": "t-chase", "kind": "Foot chase", "severity": 2, "suspect": "", "team": ["robin"], "crew": 1,
            "members": {"robin": {"joined": time.time(), "travel": 3}}}
    assert outcomes.decide(case)["how"] == "too late"                      # nobody ever got ahead of it
    caught = outcomes.decide({**case, "chase": {"caught": True}})
    assert caught["ok"] and caught["how"] == "caught"
    assert outcomes.decide({**case, "chase": {"caught": False}})["how"] == "got away"


def test_cases_keep_the_crew_and_what_the_call_was():
    report = {**_report("Hostage situation"), "crew": 6, "was": ["Armed robbery"], "gang": ""}
    case = cases.assign(report, "nightwing", by="him", travel=5)
    assert case["crew"] == 6 and case["was"] == ["Armed robbery"]


def test_he_can_call_the_batwing_and_it_waits_for_him(monkeypatch):
    t = _at(23)
    home = jet.home()
    here = (home[0] + 15, home[1] + 20)
    assert jet.summon("nightwing", here, t) is None                          # only his to call
    state = jet.summon("bruce", here, t)
    assert state and jet.waiting_for(t + 120) == "bruce"
    ride = travel.fastest("bruce", here, (here[0] + 4, here[1] + 3), t=t + 120, book=True)
    assert ride["by"] == "on the Batwing"                                     # he called it: he takes it
    assert jet.waiting_for(t + 130) == ""


def test_he_goes_to_one_of_them_and_then_goes_where_they_go(monkeypatch):
    from types import SimpleNamespace

    from wayne.engine import presence
    spot = {"t": (40.0, 60.0)}
    monkeypatch.setattr(batman, "_with", lambda cid, t: spot["t"])
    import wayne.contacts as contacts
    monkeypatch.setattr(contacts, "directory", lambda: {"robin": SimpleNamespace(id="robin", name="Tim")})
    t = _at(23)
    trip = batman.join("robin", t)
    assert trip and batman.state(t)["follow"] == "robin"
    spot["t"] = (44.0, 70.0)
    assert batman.position(trip["end"] + 5) == (44.0, 70.0)                       # with him, wherever he's got to
    batman.go("Wayne Manor", t=trip["end"] + 60)
    assert batman.state(trip["end"] + 61)["follow"] == ""
    assert presence                                                                # (imported for the registry)


def test_selina_is_out_on_her_jobs_only_on_her_nights_and_never_kept(monkeypatch):
    monkeypatch.setattr(incidents, "_selina_out", lambda t: False)
    quiet = [r for h in range(0, 48, 2) for r in incidents.at(time.time() - h * 1800) if r["kind"] == "Cat burglary"]
    assert not quiet
    case = {"id": "t-cat", "kind": "Cat burglary", "severity": 2, "suspect": "Catwoman", "crew": 1,
            "team": ["bruce", "orphan", "nightwing"], "members": {}, "opened_at": time.time(), "began": time.time(),
            "travel": 1}
    endings = [outcomes.decide({**case, "id": f"t-cat-{i}"}) for i in range(60)]
    assert not any(e["caught"] for e in endings)                                 # nobody keeps her
    assert sum(e["ok"] for e in endings) < 45                                    # and she's not easy


def test_tonights_finished_calls_are_kept_a_while_for_the_map():
    done = incidents.ended(time.time(), hours=3)
    assert done and all(time.time() - 3 * 3600 <= r["done_at"] < time.time() for r in done)
    assert all(r["status"] == "resolved" for r in done)


def test_whoever_is_on_it_decides_how_long_it_takes():
    base = {"id": "t-work", "kind": "Mugging", "severity": 1, "suspect": "", "crew": 1}
    assert outcomes.work(base, ["bruce"]) < outcomes.work(base, ["robin"])         # Batman on a mugging is quick
    army = {"id": "t-army", "kind": "Riot", "severity": 3, "suspect": "Bane", "crew": 30}
    assert outcomes.work(army, ["robin"]) > outcomes.work(army, ["bruce", "orphan", "nightwing"])


def test_on_scene_he_is_committed_and_on_the_way_he_can_change_his_mind():
    t = _at(23)
    report = {**_report("Armed robbery"), "x": 49.0, "y": 30.2}
    cases.assign(report, "bruce", by="him", travel=0.01)
    batman.go(report["place"], 49.0, 30.2, case=report["id"], t=t)
    cases.advance(time.time() + 5)
    assert batman.on_scene() and "pull out" in batman.committed()
    cases.leave(report["id"], "bruce")
    assert not batman.on_scene() and batman.committed() == ""
    other = {**_report("Mugging", rid="t-run-2"), "x": 60.0, "y": 50.0}
    cases.assign(other, "bruce", by="him", travel=30)
    assert batman.leave_case_for()["id"] == "t-run-2" and cases.for_report("t-run-2") is None


def test_an_afterthought_that_doesnt_know_isnt_something_anyone_is_doing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from wayne.engine import presence
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path / cid)
    presence._registry.clear()
    cass = SimpleNamespace(id="orphan", name="Cass", full_name="Cassandra Cain", shares_status=True,
                           shares_location=True, home="Home, Burnside", texting_pace={}, beat=(), routine=())
    whereabouts = presence.of(cass)
    whereabouts.set_activity("not specified", presence.BUSY, 60)
    assert whereabouts.now()["doing"] != "not specified"
    presence._registry.clear()


def test_a_rogue_who_gets_away_comes_back_worse_and_a_capture_ends_the_run():
    from wayne.engine import arcs
    t = time.time()
    case = {"id": "t-arc-1", "kind": "Laughing-gas attack", "severity": 3, "suspect": "The Joker", "place": "The Funhouse",
            "area": "Amusement Mile", "x": 64.0, "y": 24.6}
    back = arcs.record(case, {"ok": False, "how": "got away"}, t)
    assert back and back["suspect"] == "The Joker" and back["severity"] >= 4 and back["at"] > t
    assert arcs.heat("The Joker", t) > 0.2 and arcs.streak("The Joker") == 1
    assert any(r["id"] == back["id"] for r in incidents.at(back["at"] + 60))          # on the scanner when it comes
    assert any("The Joker" in line for line in arcs.lines(t))
    arcs.record({**case, "id": "t-arc-2"}, {"ok": True, "how": "caught", "caught": "The Joker"}, t + 3600)
    assert arcs.heat("The Joker", t + 3600) == 0.0 and arcs.streak("The Joker") == 0
