"""His mail: the night report, the folders, and what the model writes him."""
import json
import time

import pytest

from wayne import paths
from wayne.engine import cases, inbox


@pytest.fixture(autouse=True)
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def test_the_night_report_is_put_together_from_the_night_and_sent_once():
    lt = time.localtime()
    six = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 6, 30, 0, 0, 0, -1))
    report = {"id": "t-night", "kind": "Armed robbery", "severity": 3, "place": "Ralli's Family Restaurant",
              "area": "Otisburg", "x": 49.0, "y": 30.2, "at": six - 6 * 3600}
    case = cases.assign(report, "nightwing", by="him", travel=5)
    cases.mark(case["id"], opened_at=six - 6 * 3600)
    cases.close(case["id"], "Two in cuffs.", {"ok": True, "how": "caught", "caught": "", "hurt": {"nightwing": "a cracked rib"}})
    made = inbox.write_night_report(six)
    assert made and made["folder"] == "secure" and made["kind"] == "report"
    assert "Armed robbery" in made["body"] and "Dick" in made["body"] and "a cracked rib" in made["body"]
    assert inbox.write_night_report(six + 60) is None                       # once a morning


def test_mail_is_filed_read_and_binned():
    m = inbox.deliver("lucius", "Q3 numbers", "Bruce — see attached.")
    assert m["folder"] == "inbox" and inbox.unread() == 1
    inbox.update(m["id"], read=True)
    assert inbox.unread() == 0
    inbox.update(m["id"], folder="archive")
    assert inbox.mail("archive")[0]["id"] == m["id"] and not inbox.mail("inbox")
    inbox.delete(m["id"])
    assert not inbox.mail()
    assert inbox.deliver("batcomputer", "x", "y", key="k1") and inbox.deliver("batcomputer", "x", "y", key="k1") is None


def test_a_capture_brings_word_from_gordon_on_the_secure_line(monkeypatch):
    from wayne.engine import model as llm
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {"message": {"content": json.dumps(
        {"from_name": "Jim", "from_address": "jg@x", "subject": "Croc", "body": "He's in a cell. — G"})}})
    inbox.queue({"kind": "case", "id": "t-croc", "how": "caught", "caught": "Killer Croc", "suspect": "Killer Croc",
                 "place": "Waterloo Docks", "what": "mauling", "severity": 3, "chase": False})
    made = inbox.write_one("m", {})
    assert made and made["kind"] == "gordon" and made["folder"] == "secure" and "cell" in made["body"]
