"""The scanner: what's called in, how far each call has got, whose work it looks like, and its log."""
import json
import time

import pytest

from wayne import paths
from wayne.engine import incidents


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def _reports(hours, days=6):
    """Every report open on the hour at the given hours of the day, over the last few days."""
    base = time.time() // 86400 * 86400
    out = []
    for day in range(1, days + 1):
        for hour in hours:
            t = base - day * 86400 + hour * 3600 - time.localtime(base).tm_gmtoff
            out.extend(incidents.at(t))
    return out


def test_a_call_goes_reported_responding_contained_resolved():
    stages = [incidents._status(p / 100) for p in range(100)]
    order = ["reported", "units responding", "contained", "resolved"]
    assert [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s] == order
    # One more car than they've got: backup's called for between responding and contained.
    stages = [incidents._status(p / 100, backup=True) for p in range(100)]
    assert [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s] == \
        ["reported", "units responding", "backup requested", "contained", "resolved"]


def test_only_someone_out_can_be_the_suspect_and_only_for_their_kind_of_crime():
    rogues = {g["name"]: g for g in incidents.rogues()}
    reports = [r for r in _reports(range(0, 24, 2)) if r.get("suspect") and r["suspect"] != incidents.CATWOMAN]
    assert reports
    for r in reports:                                  # (Selina's jobs are hers, on her nights — see test_chases)
        rogue = rogues[r["suspect"]]
        came_in_as = (r.get("was") or [r["kind"]])[0]          # what it was before it turned
        assert rogue["status"] == "at large" and came_in_as in rogue["kinds"]


def test_the_locked_up_are_somewhere_and_the_loose_are_nowhere():
    for g in incidents.rogues():
        assert g["status"] in ("at large", "locked up", "unknown")
        assert bool(g["held"]) == (g["status"] == "locked up")
        if g["held"]:
            assert g["held"] in ("Arkham Asylum", "Blackgate Penitentiary")


def test_the_worst_of_it_comes_after_dark():
    night, day = _reports([23, 1, 2]), _reports([10, 12, 14])
    assert len(night) > len(day)

    def share(rs):
        return sum(r["severity"] >= 3 for r in rs) / max(1, len(rs))
    assert share(night) > share(day)


REPORT = {"id": "r9", "kind": "Armed robbery", "severity": 3, "place": "GCPD Central", "area": "Diamond District",
          "at": int(time.time()) - 1800, "status": "units responding", "dispatch": "Two masked men, shots fired."}


def test_a_log_is_written_once_and_grows_as_the_case_does(private_data, monkeypatch):
    from wayne.engine import model as llm
    asked = []

    def fake(**kw):
        asked.append(kw["messages"][0]["content"])
        entry = {"time": f"21:0{len(asked)}", "who": "Dispatch", "text": f"Entry {len(asked)}."}
        return {"message": {"content": json.dumps({"log": [entry]})}}
    monkeypatch.setattr(llm.ollama, "chat", fake)
    first = incidents.write_log(REPORT, "m", {})
    assert [e["text"] for e in first] == ["Entry 1."]
    assert "nothing's contained yet" in asked[0]                    # no further than it's got
    assert incidents.write_log(REPORT, "m", {}) == first and len(asked) == 1    # opened again: the same log
    later = incidents.write_log(dict(REPORT, status="contained"), "m", {})
    assert [e["text"] for e in later] == ["Entry 1.", "Entry 2."]   # the case moved on: the log with it
    assert "The log so far" in asked[1] and "Entry 1." in asked[1]
    assert incidents.logs()["r9"]["status"] == "contained"


def test_a_log_written_before_the_dispatch_gives_the_dispatch(private_data, monkeypatch):
    from wayne.engine import model as llm
    entry = {"time": "21:01", "who": "Dispatch", "text": "Units to GCPD Central, two masked men."}
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {"message": {"content": json.dumps({"log": [entry]})}})
    incidents.write_log({k: v for k, v in REPORT.items() if k != "dispatch"}, "m", {})
    assert incidents.dispatches()["r9"] == entry["text"]


def test_a_log_that_will_not_write_is_simply_empty(private_data, monkeypatch):
    from wayne.engine import model as llm

    def broken(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(llm.ollama, "chat", broken)
    assert incidents.write_log(REPORT, "m", {}) == []


def test_the_toll_is_the_same_every_time_and_the_dead_are_known_by_the_time_it_is_contained():
    report = {"id": "s2-1-0", "kind": "Machete attack", "severity": 3, "area": "The Narrows", "status": "reported"}
    first, again = incidents._toll(report), incidents._toll(dict(report))
    assert first == again                                            # seeded by the report: one story
    later, _ = incidents._toll(dict(report, status="contained"))
    assert later["dead"] >= first[0]["dead"]
    assert later["dead"] + later["hurt"] <= first[0]["dead"] + first[0]["hurt"] + 1
    assert incidents.toll_text({"dead": 2, "hurt": 3}) == "2 dead, 3 hurt"
    assert incidents.toll_text({"dead": 0, "hurt": 0}) == ""


def test_a_monsters_signature_crime_needs_the_monster_out(monkeypatch):
    from wayne.engine import codex
    monkeypatch.setattr(codex, "rogue_status", lambda name, now=None: "locked up")
    kinds = {r["kind"] for r in _reports(range(0, 24, 3), days=3)}
    assert kinds and not kinds & incidents.ROGUE_ONLY
    assert not any(r.get("suspect") for r in _reports([1], days=2))


def test_a_gang_crime_names_the_crew_whose_streets_they_are():
    reports = [r for r in _reports(range(0, 24, 2)) if r.get("gang")]
    assert reports
    for r in reports:
        came_in_as = (r.get("was") or [r["kind"]])[0]        # a drive-by that became a chase is still theirs
        assert came_in_as in incidents._GANG_KINDS
        assert all(crew in incidents.GANGS[r.get("origin_area", r["area"])] for crew in r["gang"].split(" and "))


def test_nobody_in_the_family_has_a_call_come_from_their_home():
    from wayne.contacts.profile import directory
    homes = {(c.home or "").lower() for c in directory()}
    incidents._spot_cache = None
    places_used = {r["place"].lower() for r in _reports(range(0, 24, 2), days=4)}
    assert places_used and not places_used & homes
