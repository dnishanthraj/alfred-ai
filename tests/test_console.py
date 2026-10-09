"""The console's turn driver: events held back until he picks up."""
import asyncio
import time
from collections import OrderedDict, deque
from types import SimpleNamespace

from wayne import events
from wayne.frontends.web import Console, log

# The console logs to data/console.log; the tests' fake calls don't belong there.
log.handlers.clear()
log.disabled = True


class _Voice:
    available = True

    def __init__(self):
        self.calls = []

    def synthesize_timed(self, text, voice_id):
        self.calls.append(time.monotonic())
        return b"x" * 200, [[w, i * 100] for i, w in enumerate(text.split())]


def _console():
    console = Console.__new__(Console)
    console.voice = _Voice()
    console.clients = set()
    console.audio_clips = OrderedDict()
    console.transcripts = {}
    console.current_id = None
    console.recent_speech = deque(maxlen=12)
    console.turn_epoch = 0
    console.voice_busy = False
    console.call = None
    console.sessions = {}
    console._ringing_out, console._refused_at, console._incoming = None, {}, None
    console._text_bursts, console._call_attempts = {}, {}
    console._typing_now = set()
    console.directory = SimpleNamespace(get=lambda _id: None, __iter__=lambda self: iter(()))
    sent = []

    async def broadcast(event):
        sent.append((time.monotonic(), event))
    console.broadcast = broadcast
    return console, sent


def test_the_greeting_is_ready_before_pickup_and_heard_only_at_it():
    console, sent = _console()
    contact = SimpleNamespace(id="t", has_voice=True, voice_id="v")

    def greeting():
        yield events.sentence(0, "Evening.")
        yield events.reply_end("Evening.")

    async def run():
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        await console.drive(greeting(), contact, release_at=loop.time() + 0.3)
        return started

    started = asyncio.run(run())
    # Synthesis ran during the ring...
    assert console.voice.calls and console.voice.calls[0] - started < 0.2
    # ...but nothing reached the page until he picked up.
    assert sent and min(t for t, _ in sent) - started >= 0.29
    assert any(e["type"] == "speak" for _, e in sent)


def test_a_superseded_turn_closes_its_generator():
    # Abandoning it left the worker reading the model to the end of a reply
    # nobody would hear, with the next turn queued behind it.
    console, sent = _console()
    contact = SimpleNamespace(id="t", has_voice=False, voice_id="")
    closed, produced = [], []

    def reply():
        try:
            for i in range(200):
                produced.append(i)
                yield events.sentence(i, f"Sentence {i}.")
                time.sleep(0.01)
        finally:
            closed.append(True)

    async def run():
        task = asyncio.create_task(console.drive(reply(), contact, epoch=console.turn_epoch))
        await asyncio.sleep(0.1)
        console.interrupt()
        await task

    asyncio.run(run())
    assert closed
    assert len(produced) < 50


def test_sentence_keys_never_repeat_across_replies():
    # The holding line and the answer's first sentence are both index 0.
    console, sent = _console()
    contact = SimpleNamespace(id="t", has_voice=True, voice_id="v")

    def turn():
        yield events.sentence(0, "One moment.")
        yield events.reply_end("One moment.", interim=True)
        yield events.reply_start()
        yield events.sentence(0, "It's raining.")

    asyncio.run(console.drive(turn(), contact))
    keys = [e["key"] for _, e in sent if e["type"] == "sentence"]
    assert len(keys) == len(set(keys)) == 2
    spoken = {e["index"]: e for _, e in sent if e["type"] == "speak"}
    assert set(spoken) == set(keys)
    assert spoken[keys[1]]["words"][0] == ["It's", 0]


def test_the_model_is_released_after_hang_up_unless_he_rings_back(monkeypatch):
    import wayne.frontends.web as web
    released = []
    monkeypatch.setattr(web, "_release_model", lambda model: released.append(model))
    monkeypatch.setattr(web.config, "HANG_UP_RELEASE", 0.05)

    async def run(ring_back):
        console, _ = _console()
        console.turn_lock = asyncio.Lock()
        console.current_id = "alfred"
        console._release = None
        console.directory = SimpleNamespace(get=lambda _id: SimpleNamespace(model="m"))
        await console.disconnect()
        if ring_back:
            console._release.cancel()
        await asyncio.sleep(0.15)

    asyncio.run(run(ring_back=False))
    assert released == ["m"]
    released.clear()
    asyncio.run(run(ring_back=True))
    assert released == []
