"""How cases go: teams, the odds, the endings — and arriving too late."""
import time

import pytest

from wayne import paths
from wayne.engine import cases, outcomes


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def _case(kind, severity, suspect="", team=("nightwing",), travel=8, began_ago=60):
    now = time.time()
    return {"id": f"t-{kind}-{suspect}-{'-'.join(team)}", "kind": kind, "severity": severity, "suspect": suspect,
            "team": list(team), "assignee": team[0], "opened_at": now, "began": now - began_ago, "travel": travel,
            "members": {m: {"joined": now, "travel": travel, "by": "him", "status": "assigned"} for m in team}}


def test_the_odds_follow_who_went_and_who_was_behind_it():
    joker = outcomes.chance(_case("Laughing-gas attack", 4, "The Joker"))
    with_tim = outcomes.chance(_case("Laughing-gas attack", 4, "The Joker", ("nightwing", "robin")))
    with_bruce = outcomes.chance(_case("Laughing-gas attack", 4, "The Joker", ("bruce", "nightwing", "robin")))
    assert joker < with_tim < with_bruce                               # more of them, better odds
    assert outcomes.chance(_case("Mugging", 1)) > with_bruce           # a mugging isn't the Joker
    riddle_cass = outcomes.chance(_case("Riddle left at a crime scene", 3, "The Riddler", ("orphan",)))
    riddle_tim = outcomes.chance(_case("Riddle left at a crime scene", 3, "The Riddler", ("robin",)))
    assert riddle_tim > riddle_cass + 0.3                              # the detective, not the fighter
    assert outcomes.DIFFICULTY["The Joker"] > outcomes.DIFFICULTY["Calendar Man"]


def test_an_ending_is_the_same_however_often_its_asked_and_names_who_was_caught():
    case = _case("Mauling", 3, "Killer Croc", ("orphan", "redhood"))
    first = outcomes.decide(case)
    assert first == outcomes.decide(dict(case))
    assert first["how"] in ("caught", "got away", "worse")
    assert (first["caught"] == "Killer Croc") == (first["ok"] and first["how"] == "caught")


def test_a_snatch_is_over_before_anyone_far_off_gets_there():
    late = outcomes.decide(_case("Phone snatch", 1, travel=25))
    assert late["how"] == "too late" and not late["ok"]
    assert outcomes.decide(_case("Body found", 3, travel=40))["how"] != "too late"        # a body waits


def test_a_case_has_a_team_and_each_arrives_in_their_own_time():
    report = {"id": "r1", "kind": "Armed robbery", "severity": 3, "place": "Ralli's Family Restaurant",
              "area": "Otisburg", "x": 1.0, "y": 2.0, "at": time.time() - 120}
    cases.assign(report, "nightwing", by="him", travel=0.01)
    case = cases.assign(report, "robin", by="self", travel=30)
    assert cases.team(case) == ["nightwing", "robin"] and case["assignee"] == "nightwing"
    landed = []
    cases.advance(time.time() + 5, arrived=lambda c, who: landed.append(who))
    assert landed == ["nightwing"]                                       # Tim's still driving
    assert cases.active("robin")["status"] == "on scene"
    cases.leave("r1", "nightwing")
    assert cases.team(cases.for_report("r1")) == ["robin"]
    cases.leave("r1", "robin")
    assert cases.for_report("r1") is None                               # nobody left on it: gone
