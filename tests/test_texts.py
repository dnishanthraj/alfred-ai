"""Texting: the long-term thread, read receipts, pacing and batching."""
import asyncio
from types import SimpleNamespace

import wayne.frontends.web as web
from wayne.memory.texts import TextLog


def _log(tmp_path):
    log = TextLog.__new__(TextLog)
    log.path = tmp_path / "texts.json"
    return log


def test_the_thread_pages_back_and_records_reads(tmp_path):
    log = _log(tmp_path)
    sent = [log.add("me", f"m{i}", at=100 + i) for i in range(5)]
    assert [m["text"] for m in log.page(limit=2)] == ["m3", "m4"]
    assert [m["text"] for m in log.page(before=103, limit=2)] == ["m1", "m2"]
    log.mark_read([sent[4]["id"]], at=200)
    assert log.page(limit=1)[0]["read_at"] == 200


def test_texts_in_a_row_are_read_and_answered_together(tmp_path, monkeypatch):
    console = web.Console.__new__(web.Console)
    console._pending_texts, console._texters, console.call, console.current_id = {}, {}, None, None
    console.sessions = {}
    sent_events, written = [], []

    async def broadcast(event):
        sent_events.append(event["type"])
    console.broadcast = broadcast
    console._members = lambda: []
    contact = SimpleNamespace(id="nightwing", texting_pace={"read": [0.05, 0.05], "busy": 0, "wpm": 10000})
    console.directory = SimpleNamespace(get=lambda _id: contact)
    monkeypatch.setattr(web, "TextLog", lambda cid: _log(tmp_path))

    async def write(c, body):
        written.append(body)
        return "on it"
    console._write_text = write
    monkeypatch.setattr(web.random, "uniform", lambda a, b: a)

    async def run():
        await console.text("nightwing", "you around?")
        await console.text("nightwing", "need a hand at the docks")
        await asyncio.sleep(1.2)

    asyncio.run(run())
    assert written == ["you around?\nneed a hand at the docks"]
    assert sent_events.count("text_reply") == 1
    order = [e for e in sent_events if e in ("text_read", "text_typing", "text_reply")]
    assert order == ["text_read", "text_typing", "text_reply"]
