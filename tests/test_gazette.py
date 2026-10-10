"""Gotham's own news: the running stories, this morning's paper, and who's who."""
import datetime
import json
from types import SimpleNamespace

import pytest

from wayne import paths
from wayne.engine import gazette


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def test_the_running_stories_move_on_with_the_calendar():
    october = gazette.storylines(datetime.date(2026, 10, 10))
    assert any("mayoral election, in 24 days" in s for s in october)          # Election Day, 3 November 2026
    assert any("Knights in the postseason" in s for s in october)
    assert gazette.storylines(datetime.date(2026, 10, 10)) == october          # the same day reads the same
    assert not any("election" in s for s in gazette.storylines(datetime.date(2027, 3, 1)))


def test_the_morning_paper_is_written_once_and_read_by_everyone(private_data, monkeypatch):
    from wayne.engine import model as llm
    items = [{"outlet": "Gotham Gazette", "by": "Evelyn Adams", "headline": "Machete attack in the Narrows",
              "dek": "Two hurt outside a bodega on Ninth."},
             {"outlet": "GBS", "by": "Jack Ryder", "headline": "Nakano: the masks must go",
              "dek": "The mayor doubles down a month out."},
             {"outlet": "Gotham Globe", "by": "the Globe", "headline": "WAYNE'S NEW FLAME?",
              "dek": "Bruce Wayne seen leaving the Gilded Cage at 2am."},
             {"outlet": "GCN", "by": "Summer Gleeson", "headline": "Knights take game three", "dek": "Extra innings."}]
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {"message": {"content": json.dumps({"items": items})}})
    assert len(gazette.write("m", {})) == 4 and gazette.written()
    tim = SimpleNamespace(id="robin")
    note = gazette.note(tim, "did you see the knights game")
    assert note.count("“") == 3 and "Knights take game three" in note.split(";")[0]   # what he asked about, first
    assert gazette.note(tim) == gazette.note(tim)                                          # the same paper all day


def test_the_citys_figures_are_known_by_name_or_title():
    assert "Christopher Nakano" in gazette.who("what do you make of the mayor?")
    assert "James Gordon" in gazette.who("Gordon called")
    assert "Simon Saint" not in gazette.who("meet me at Saint Mike's")                     # a bar, not the man
    assert gazette.who("nice weather") == ""
