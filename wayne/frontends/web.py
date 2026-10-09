"""
Local web console.

FastAPI serves a static page and one WebSocket. The page owns presentation and
audio playback; this module owns the sessions and translates engine events onto
the socket. No build step and no node toolchain — the console is plain
HTML/CSS/JS, so the launcher only has to start a Python process.

Audio is *not* played here. The bytes go to the browser, which plays them
through Web Audio so the visualizer can read the real frequency spectrum and
the transcript can be revealed in time with the speech.

Synthesis is pipelined: each sentence is sent to ElevenLabs the moment the
model finishes writing it, several in flight at once, but the resulting clips
are released to the page strictly in order.
"""
import asyncio
import logging
import random
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import config, events, paths
from .. import operator as wayne_operator
from ..audio import stt, system_voice
from ..audio.tts import get_voice_engine
from ..contacts import directory
from ..engine import ContactSession, grapevine, guards, world
from ..engine.party import MAX_CONTACTS, Call
from ..memory import migrate_legacy
from ..memory.store import atomic_write, read_text
from ..paths import WEB_DIR

# Synthesized clips waiting to be fetched. Bounded — a long session would
# otherwise hold every reply's audio in memory for the whole run.
_MAX_CACHED_CLIPS = 64

# What happened, when, and how long it took — for the times a reply goes quiet
# and there is otherwise nothing to look at. data/ is gitignored.
log = logging.getLogger("wayne")
if not log.handlers:
    paths.DATA_DIR.mkdir(exist_ok=True)
    _handler = logging.FileHandler(paths.DATA_DIR / "console.log")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

# How long a spoken line stays eligible to be recognised as an echo of itself.
# Long enough to cover a reply playing out plus the transcription round trip,
# short enough that legitimately repeating a phrase later still gets through.
ECHO_WINDOW_SECONDS = 25

# How long a call rings before it is answered. Varied, because a constant delay
# is only marginally less mechanical than none at all.
PICKUP_DELAY = (0.9, 3.4)


class Console:
    """
    The console as a whole: a directory of contacts, one live session per
    contact you have spoken to, and the fan-out to connected browser tabs.
    """

    # Every sentence sent to the page gets a key that is never reused. The
    # engine numbers sentences from 0 within each reply, and the page used
    # those numbers to remember what it had already shown — so after "One
    # moment." took slot 0, the real answer's first sentence (also 0) was
    # treated as already shown and never appeared, and a check-in after an
    # unanswered reply could vanish the same way.
    _said = 0

    def __init__(self):
        self.directory = directory()
        self.voice = get_voice_engine()
        self.clients = set()
        self.audio_clips = OrderedDict()
        self.turn_lock = asyncio.Lock()
        self.sessions = {}
        self.transcripts = {}
        self.current_id = None
        # What the contact has said lately, for recognising its own voice
        # arriving back through the microphone.
        self.recent_speech = deque(maxlen=12)
        # Bumped whenever something supersedes the turn in flight. A turn that
        # finds its epoch stale stops forwarding events and stops synthesising,
        # which is what makes interrupting him actually interrupt him rather
        # than queue behind whatever he was already saying.
        self.turn_epoch = 0
        # True while a turn is mid-flight, so the next input can tell whether it
        # cut him off or merely followed him.
        self.voice_busy = False
        self.boot_task = None
        self._release = None   # pending model release after a hang-up
        self.call = None       # the call in progress (see wayne.engine.party)
        self.migrated = migrate_legacy(config.DEFAULT_CONTACT)

    # --- contacts ---------------------------------------------------------

    @property
    def contact(self):
        return self.directory.get(self.current_id) if self.current_id else None

    def session_for(self, contact_id):
        if contact_id not in self.sessions:
            contact = self.directory.get(contact_id)
            if contact is None:
                raise KeyError(contact_id)
            self.sessions[contact_id] = ContactSession(contact)
            self.transcripts.setdefault(contact_id, [])
        return self.sessions[contact_id]

    def transcript(self, contact_id):
        return self.transcripts.setdefault(contact_id, [])

    # --- fan-out ----------------------------------------------------------

    def _record(self, event):
        """
        Fold events into a canonical list of displayed turns for replay.

        A streamed reply only produces sentence/reply_end events, so recording
        `message` alone would silently drop every normal answer from the
        replay. reply_end is authoritative; an interim holding line is recorded
        as its own turn because that is how it appeared on screen.
        """
        if self.current_id is None:
            return
        log = self.transcript(self.current_id)
        kind = event.get("type")
        if kind == "message":
            log.append({"role": event["role"], "text": event["text"]})
        elif kind == "reply_end":
            last = log[-1] if log else None
            if last and last["role"] == "assistant" and last["text"] == event["text"]:
                return
            log.append({"role": "assistant", "text": event["text"]})
        elif kind == "notice":
            log.append({"role": "system", "text": event["text"],
                        "level": event.get("level", "info")})
        elif kind == "sources":
            log.append({"role": "sources", "items": event["items"]})
        else:
            return
        del log[:-400]

    async def broadcast(self, event):
        self._record(event)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # --- speech pipeline --------------------------------------------------

    def _store_clip(self, audio):
        clip_id = uuid.uuid4().hex
        self.audio_clips[clip_id] = audio
        while len(self.audio_clips) > _MAX_CACHED_CLIPS:
            self.audio_clips.popitem(last=False)
        return clip_id

    def interrupt(self):
        """Supersede whatever is being generated or spoken right now."""
        self.turn_epoch += 1
        return self.turn_epoch

    async def drive(self, generator, contact, epoch=None, release_at=None):
        """
        Consume a blocking engine generator on a worker thread, forwarding its
        events to the socket and synthesizing each sentence as it appears.

        Once the turn is superseded, nothing more is sent to the page or
        synthesised, so a contact talked over goes quiet immediately — and the
        generator is *closed*, not merely abandoned. Abandoning it left the
        worker thread reading the model to the end of a reply nobody would
        hear, and Ollama serves one request at a time, so the next turn queued
        behind every remaining token of the last. Closing it closes the HTTP
        stream, which is what makes Ollama stop.

        `release_at` holds everything back from the page until that moment on
        the event loop's clock, while generation and synthesis carry on behind
        it. That is how a call is answered: the greeting is written and voiced
        while the line rings, and he speaks the instant he picks up instead of
        picking up and then thinking about what to say.
        """
        loop = asyncio.get_running_loop()
        began, first_at = time.monotonic(), None

        async def hold():
            if release_at is not None:
                await asyncio.sleep(max(0.0, release_at - loop.time()))
        queue = asyncio.Queue()
        done = object()

        def superseded():
            return epoch is not None and epoch != self.turn_epoch

        def pump():
            try:
                for event in generator:
                    if superseded():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # a worker crash must not kill the socket
                loop.call_soon_threadsafe(
                    queue.put_nowait, events.notice(f"Engine error: {exc}", "error")
                )
            finally:
                generator.close()
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=pump, daemon=True).start()

        speech = asyncio.Queue()
        worker = asyncio.create_task(self._speech_worker(speech, hold))
        self.voice_busy = True

        try:
            while True:
                event = await queue.get()
                if event is done:
                    break
                if superseded():
                    # Wait for the worker to close the generator before letting
                    # the next turn in: it is one token away from noticing, and
                    # two turns writing history at once is worse than a beat.
                    while await queue.get() is not done:
                        pass
                    break
                if event.get("type") == "sentence":
                    self._said += 1
                    event = {**event, "key": self._said}
                if event.get("type") == "sentence" and first_at is None:
                    first_at = time.monotonic()
                    log.info("%s first sentence after %.2fs", contact.id, first_at - began)
                # On a call with company, each sentence in its speaker's voice.
                speaker = self.directory.get(event.get("speaker") or "") or contact
                if event.get("type") == "sentence" and self._can_speak(speaker):
                    spoken = event.get("voice") or event["text"]
                    task = asyncio.create_task(
                        asyncio.to_thread(self._timed_synthesis, spoken, speaker)
                    )
                    speech.put_nowait((event["key"], event["text"], task, speaker.id))
                await hold()
                await self.broadcast(event)
        finally:
            speech.put_nowait(None)
            await worker
            self.voice_busy = False
            if epoch is None or epoch == self.turn_epoch:
                # Only now is the turn genuinely finished: the model has stopped
                # writing and every sentence has been synthesized and released.
                await self.broadcast(events.turn_complete())

    def _timed_synthesis(self, text, contact):
        started = time.monotonic()
        try:
            audio, words = self.voice.synthesize_timed(text, contact.voice_id)
        except Exception as exc:
            log.warning("%s synthesis FAILED after %.2fs: %s", contact.id,
                        time.monotonic() - started, str(exc)[:160])
            raise
        log.info("%s synthesis %.2fs, %d chars%s", contact.id, time.monotonic() - started,
                 len(text), "" if words else " (no timings)")
        return audio, words

    def _can_speak(self, contact):
        return self.voice.available and contact.has_voice

    async def _speech_worker(self, speech, hold):
        """
        Release clips in submission order. Synthesis runs concurrently, but a
        later sentence finishing first must never jump the queue — the page
        plays what it is handed.

        When synthesis fails — quota gone, network down, key expired — the
        console does not show a stack trace. The voice link degrades and the
        contact carries on in text, which is both truthful about what happened
        and in keeping with a console that is supposed to be a place, not a
        program. The text still reaches the screen: the page renders any
        sentence that never got audio.
        """
        reported = False
        while True:
            item = await speech.get()
            if item is None:
                return
            key, text, task, speaker = item
            try:
                audio, words = await task
            except Exception:
                if not reported:
                    reported = True
                    await self.broadcast(events.notice(
                        "Voice link degraded — switching to text.", "warn"))
                continue
            if audio:
                await hold()
                self.recent_speech.append((time.monotonic(), text))
                await self.broadcast({**events.speak(self._store_clip(audio), text, key, words),
                                      "speaker": speaker})

    # --- turns ------------------------------------------------------------

    async def connect(self, contact_id):
        """Switch the console to a contact, booting them on first connection."""
        contact = self.directory.get(contact_id)
        if contact is None:
            return
        if self._release:
            self._release.cancel()
            self._release = None
        if self.current_id and self.current_id != contact_id:
            # Switching lines mid-call: whoever was on it notices next time.
            for member in (self.call.members if self.call else [self.sessions[self.current_id]]):
                member.call_ended("switched")
            self._end_call()
            self.interrupt()
        async with self.turn_lock:
            self.current_id = contact_id
            session = self.session_for(contact_id)
            self.call = Call()
            self.call.join(session)
            await self.broadcast(events.contact_changed(contact_id))
            await self.broadcast(events.party([contact_id]))

            if not contact.availability.is_available():
                await self.broadcast(events.notice(
                    contact.availability.away_message, "warn"))
                return

            # Nobody answers the instant it rings, so the line rings for a
            # varying moment — but the greeting is generated and synthesised
            # during it rather than after, so the ring is the whole wait.
            pickup = asyncio.get_running_loop().time() + random.uniform(*PICKUP_DELAY)

            for problem in config.missing_requirements():
                await self.broadcast(events.notice(problem, "warn"))
            if self.voice.available and not contact.has_voice:
                await self.broadcast(events.notice(
                    f"No voice set for {contact.name} yet — text only.", "warn"))
            if self.migrated:
                await self.broadcast(events.notice(
                    f"Migrated existing {' and '.join(self.migrated)} into "
                    f"data/{config.DEFAULT_CONTACT}/.", "info"))
                self.migrated = []
            await self.drive(session.boot(), contact, self.interrupt(), release_at=pickup)

    def _is_own_echo(self, text):
        """
        Discard a transcript that is the contact's own voice fed back through
        the microphone. Only recent speech counts: repeating something Alfred
        said an hour ago is a legitimate thing for a person to do.
        """
        cutoff = time.monotonic() - ECHO_WINDOW_SECONDS
        recent = [line for stamp, line in self.recent_speech if stamp > cutoff]
        return guards.echoes(text, recent)

    async def disconnect(self):
        """
        Hang up. The session and its memory stay — this ends the call, it does
        not forget the conversation — but nothing is on the line afterwards, and
        a later call re-greets rather than resuming mid-sentence.
        """
        self.interrupt()
        async with self.turn_lock:
            contact = self.contact
            self.current_id = None
            self._end_call()
            self.recent_speech.clear()
        if contact:
            self._release = asyncio.create_task(self._release_after(contact))

    def _end_call(self):
        # What was said may travel (see wayne.engine.grapevine).
        members = self.call.members if self.call else (
            [self.sessions[self.current_id]] if self.current_id in self.sessions else [])
        present = {m.contact.id for m in members}
        for member in members:
            grapevine.note_call(member, list(self.directory), member.call_start, present)
        if self.call:
            for member in list(self.call.members):
                self.call.leave(member)
        self.call = None

    def _members(self):
        return [m.contact.id for m in self.call.members] if self.call else []

    async def add(self, contact_id):
        """
        Patch another contact into the call. They ring, pick up already knowing
        who is on the line and the last few things said, and greet the call.
        """
        contact = self.directory.get(contact_id)
        if (contact is None or not self.call or contact_id in self._members()
                or len(self.call.members) >= MAX_CONTACTS
                or not contact.availability.is_available()):
            return
        epoch = self.interrupt()
        async with self.turn_lock:
            newcomer = self.session_for(contact_id)
            if not self.call.join(newcomer):
                return
            await self.broadcast(events.party(self._members(), added=contact_id))
            # The console announces it and the line rings; the greeting is made
            # meanwhile and released when they "pick up".
            pickup = asyncio.get_running_loop().time() + 2.6 + random.uniform(0.3, 1.6)
            await self.drive(self.call.greet(newcomer), contact, epoch, release_at=pickup)

    async def drop(self, contact_id):
        """Let one contact off the call. They say goodbye; the call goes on."""
        if not self.call or contact_id not in self._members():
            return
        if len(self.call.members) == 1:
            return await self.disconnect()
        epoch = self.interrupt()
        async with self.turn_lock:
            member = self.sessions[contact_id]
            await self.drive(self.call.farewell(member), member.contact, epoch)
            self.call.leave(member)
            if self.current_id == contact_id:
                self.current_id = self.call.members[0].contact.id
            await self.broadcast(events.party(self._members(), removed=contact_id))

    async def text(self, contact_id, body):
        """
        A text message to a contact — any contact, on a call or not. Answered
        in writing, never spoken, and kept in the same memory as their calls.
        """
        contact = self.directory.get(contact_id)
        body = (body or "").strip()
        if contact is None or not body:
            return
        session = self.session_for(contact_id)
        # The same lock as the call's turns: a contact on the call who is also
        # texted must not be writing two replies into one memory at once.
        async with self.turn_lock:
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            done = object()

            def pump():
                try:
                    for event in session.ask(body, via="text"):
                        loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait,
                                              events.notice(f"Message failed: {exc}", "error"))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, done)

            threading.Thread(target=pump, daemon=True).start()
            await self.broadcast({"type": "text_typing", "speaker": contact_id})
            while True:
                event = await queue.get()
                if event is done:
                    break
                if event["type"] == "reply_end" and not event.get("interim"):
                    await self.broadcast({"type": "text_reply", "speaker": contact_id,
                                          "text": event["text"]})
                elif event["type"] == "notice":
                    await self.broadcast(event)

    async def _release_after(self, contact):
        """
        Free the model a while after the call ends. A 26B is 15 GB; resident
        all evening it pushed a 24 GB Mac into swap. A call loads it while the
        line rings (the boot reads the prefix anyway), so releasing between
        calls costs the next greeting a few seconds and nothing else.
        """
        try:
            await asyncio.sleep(config.HANG_UP_RELEASE)
        except asyncio.CancelledError:
            return
        if self.current_id is None:
            await asyncio.to_thread(_release_model, contact.model)

    async def nudge(self, kind="check_in"):
        """
        Break a long silence. Not a notification — a turn like any other, so
        what he says is generated in character and differs every time. It is
        never stored in history: a check-in that went unanswered shouldn't
        become part of what he remembers of the conversation.
        """
        if not self.current_id:
            return
        async with self.turn_lock:
            contact = self.contact
            session = self.session_for(self.current_id)
            await self.drive(getattr(session, kind)(), contact, self.interrupt())

    async def submit(self, text, spoken=False, confidence=1.0):
        """
        Run one turn. `spoken` marks input that came from a microphone, which is
        the only kind that can be an acoustic echo — typed text never is.
        """
        text = (text or "").strip()
        if not text or not self.current_id:
            return
        if spoken and self._is_own_echo(text):
            return
        if self.call:
            wanted = self._asked_to_add(text)
            if wanted:
                return await self.add(wanted)
            leaving = self._asked_to_drop(text)
            if leaving:
                return await self.drop(leaving)

        # Supersede first, then queue: the running turn sees a stale epoch,
        # stops, and releases the lock instead of making the new input wait for
        # a reply nobody is listening to any more.
        was_speaking = self.voice_busy
        epoch = self.interrupt()
        async with self.turn_lock:
            if epoch != self.turn_epoch:
                return             # something newer arrived while we waited
            contact = self.contact
            if self.call and self.call.is_group:
                turn = self.call.turn(text, interrupted=was_speaking, confidence=confidence)
            else:
                session = self.session_for(self.current_id)
                turn = session.ask(text, interrupted=was_speaking, confidence=confidence)
            await self.drive(turn, contact, epoch)

    def _asked_to_add(self, text):
        """'Alfred, get Lucius on the line' — a contact not on the call, asked for."""
        if not re.search(r"\b(get|bring|add|patch|loop|call|ring|grab|put)\b", text, re.I):
            return None
        for contact in self.directory:
            if contact.id in self._members():
                continue
            if any(re.search(rf"\b{re.escape(n)}\b", text, re.I)
                   for n in {contact.name, contact.full_name}):
                return contact.id
        return None

    def _asked_to_drop(self, text):
        """'Selina, you can drop off' — someone on the call, let go."""
        if not self.call or not self.call.is_group:
            return None
        if not re.search(r"\b(drop (off|out)|you can go|hang up|leave the call|"
                         r"let (her|him) go|sign off|head off)\b", text, re.I):
            return None
        named = self.call.addressed(text)
        return named[0].contact.id if len(named) == 1 else None


console = Console()


def _release_model(model):
    try:
        import ollama
        ollama.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass


def _warm_model():
    """
    Nudge the default contact's model into memory while the operator is still
    reading the boot screen. Loading a 14B costs around 25 seconds, and paying
    that after they have already spoken is the difference between a console and
    a progress bar. Failures are silent: this is an optimisation, not a step.

    The contact's own options are passed rather than a bare `num_predict`.
    Ollama keys a resident model on its context size, so warming at the default
    4096 and then asking at 8192 unloads and reloads it — the first real turn
    paid fifteen seconds, and the warm-up was the reason.

    It sends the real prefix — persona, directives, samples and the history
    window — rather than a single character. Loading the weights is only half
    the cold start; the other half is reading a couple of thousand tokens of
    prompt, and on a model that caches its prefix this pays for that before
    anyone has spoken instead of on the first reply.
    """
    if console.directory.get(config.DEFAULT_CONTACT) is None:
        return
    try:
        console.session_for(config.DEFAULT_CONTACT).warm()
    except Exception:
        pass


def _warm_speech():
    """
    Load and exercise the Whisper model before anyone speaks.

    Resolving the backend and decoding the first clip costs about two seconds;
    every clip after that takes a tenth of one. Left to happen on demand, that
    two seconds lands on the first thing the operator ever says — which is
    exactly when the console is being judged, and it reads as a slow microphone
    rather than a one-off load. A short buffer of silence is enough to force the
    whole path: backend resolution, weights, and the first decode.
    """
    try:
        import numpy as np

        from ..audio import stt
        stt.transcribe_audio(np.zeros(stt.SAMPLE_RATE, dtype=np.float32))
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    warm = [
        asyncio.create_task(asyncio.to_thread(_warm_model)),
        asyncio.create_task(asyncio.to_thread(_warm_speech)),
        asyncio.create_task(asyncio.to_thread(world.prime)),
        asyncio.create_task(asyncio.to_thread(
            system_voice.prime, console.voice, [c.full_name for c in console.directory])),
    ]
    yield
    for task in warm:
        task.cancel()


app = FastAPI(title="WayneTech Console", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _contact_payload(contact):
    """What the page is told about a contact — who they are, not what runs them."""
    return {
        "id": contact.id,
        "name": contact.name,
        "full_name": contact.full_name,
        "role": contact.role,
        "tagline": contact.tagline,
        "accent": contact.accent,
        "available": contact.availability.is_available(),
        "portrait": contact.portrait,
        "group": contact.group,
    }


def _asset_version():
    """
    A token that changes whenever any front-end file does.

    The console runs in a Chrome window with its own persistent profile, which
    caches `/static/js/app.js` by URL and keeps serving the old one after an
    edit — so a code change appeared not to take, and the obvious next move is
    to rebuild the .app, which changes nothing because the bundle is only a
    launcher. Stamping the URLs makes an edit take effect on reload, full stop.

    Recomputed per request rather than at import: during development the whole
    point is that the file just changed, and the cost is a handful of stats.
    """
    stamp = max(
        (path.stat().st_mtime for path in WEB_DIR.rglob("*")
         if path.is_file() and path.suffix in (".js", ".css")),
        default=0.0,
    )
    return f"{stamp:.0f}"


@app.get("/")
async def index():
    # Rewritten in memory; index.html on disk stays clean and directly openable.
    html = (WEB_DIR / "index.html").read_text()
    html = re.sub(r'(/static/[\w./-]+\.(?:js|css))"',
                  rf'\1?v={_asset_version()}"', html)
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/session")
async def session_info():
    """
    What the console needs to render itself.

    Deliberately spare. Which model answers, which speech backend is loaded and
    which synthesiser speaks are all facts about the machinery, and the console
    is not meant to feel like machinery — so they are not sent to the page at
    all. Resolving the speech backend also costs a model load, which is a poor
    thing to pay for a readout nobody needs.
    """
    return JSONResponse({
        "operator": wayne_operator.full_name(),
        "contacts": [_contact_payload(c) for c in console.directory],
        "current": console.current_id,
        "default": config.DEFAULT_CONTACT,
    })


@app.post("/api/unlock")
async def unlock(request: Request):
    """
    Checked here only so the page has something to call; this is stagecraft,
    not access control. The server does not gate any other route on it and the
    passcode sits in plain text in .env — see config.CONSOLE_PASSCODE.
    """
    body = await request.json()
    supplied = (body.get("passcode") or "").strip().lower()
    return JSONResponse({"ok": supplied == config.CONSOLE_PASSCODE.strip().lower()})


@app.get("/api/contacts/{contact_id}/bio")
async def read_bio(contact_id: str):
    """
    The relationship in the operator's own words. Falls back to whatever the
    profile shipped with, so the file reads as written rather than empty.
    """
    contact = console.directory.get(contact_id)
    if contact is None:
        return Response(status_code=404)
    stored = read_text(paths.bio_file(contact_id))
    return JSONResponse({"bio": stored or contact.bio})


@app.post("/api/contacts/{contact_id}/bio")
async def write_bio(contact_id: str, request: Request):
    if console.directory.get(contact_id) is None:
        return Response(status_code=404)
    body = await request.json()
    atomic_write(paths.bio_file(contact_id), (body.get("bio") or "").strip())
    return JSONResponse({"ok": True})


@app.get("/api/system/{event}")
async def system_line(event: str, contact: str = ""):
    """
    The Batcomputer's line for a moment, as mp3, with its text in a header so
    the page can show what was said. 204 when there is no system voice.
    """
    if not system_voice.available():
        return Response(status_code=204)
    found = console.directory.get(contact) if contact else None
    text = system_voice.line(event, found.full_name if found else "")
    if not text:
        return Response(status_code=404)
    try:
        clip = await asyncio.to_thread(system_voice.audio, text, console.voice)
    except Exception:
        return Response(status_code=204)
    return Response(content=clip, media_type="audio/mpeg",
                    headers={"X-Line": text, "Cache-Control": "no-store"})


@app.get("/api/contacts/{contact_id}/messages")
async def messages(contact_id: str):
    """The text thread with a contact, oldest first."""
    if console.directory.get(contact_id) is None:
        return Response(status_code=404)
    thread = console.session_for(contact_id).history.texts()
    return JSONResponse({"messages": [
        {"from": "them" if m["role"] == "assistant" else "me", "text": m["content"],
         "at": m.get("at")} for m in thread]})


@app.get("/api/audio/{clip_id}")
async def audio(clip_id: str):
    clip = console.audio_clips.get(clip_id)
    if clip is None:
        return Response(status_code=404)
    return Response(content=clip, media_type="audio/mpeg")


def _speech_hint():
    """
    Words this particular conversation is likely to contain, fed to the decoder
    so it stops inventing spellings for them. Whoever is on the line, whoever is
    speaking, and any proper nouns from the last thing said.

    Names only — deliberately. Whisper's `initial_prompt` is not a glossary, it
    is a prefix the decoder continues from, so it biases output toward *whatever
    is in it*. This used to pass the contact's entire last sentence, which meant
    every take was nudged toward the words he had just spoken: his phrasing
    appeared in transcripts of things the operator never said, the reply
    answered that, and the conversation walked off in a direction nobody had
    asked for. It also gave acoustic echo a helping hand, since the hint agreed
    with whatever the microphone picked up off the speakers.

    Capitalised words survive because a name is what Whisper actually gets wrong
    and what a hint genuinely fixes. Prose is what does the damage.
    """
    # The configured hints too. Building this list per request used to replace
    # them entirely, so names added to WAYNE_WHISPER_HINTS were never used.
    parts = wayne_operator.whisper_hints() + [
        h.strip() for h in config.WHISPER_HINT_PROMPT.split(",") if h.strip()]
    contact = console.contact
    if contact:
        parts += [contact.name, contact.full_name]
    if console.recent_speech:
        spoken = console.recent_speech[-1][1] or ""
        # Mid-sentence capitals: proper nouns, minus whatever starts a sentence.
        parts += re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-zA-Z'’-]{2,}", spoken)[:6]
    seen, unique = set(), []
    for part in parts:
        key = (part or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(part.strip())
    return ", ".join(unique)[:200]


@app.post("/api/transcribe")
async def transcribe(request: Request):
    """Raw little-endian float32 PCM at stt.SAMPLE_RATE, captured in the page."""
    body = await request.body()
    text, confidence = await asyncio.to_thread(stt.transcribe_pcm, body, _speech_hint())
    return JSONResponse({"text": text, "confidence": round(confidence, 2)})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    console.clients.add(websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            kind = payload.get("type")
            if kind == "prompt":
                asyncio.create_task(console.submit(
                    payload.get("text", ""),
                    spoken=bool(payload.get("spoken")),
                    confidence=float(payload.get("confidence", 1.0))))
            elif kind == "connect":
                asyncio.create_task(console.connect(payload.get("id", "")))
            elif kind == "disconnect":
                asyncio.create_task(console.disconnect())
            elif kind == "nudge":
                asyncio.create_task(console.nudge())
            elif kind == "signoff":
                asyncio.create_task(console.nudge("sign_off"))
            elif kind == "text":
                asyncio.create_task(console.text(payload.get("id", ""), payload.get("text", "")))
            elif kind == "add":
                asyncio.create_task(console.add(payload.get("id", "")))
            elif kind == "drop":
                asyncio.create_task(console.drop(payload.get("id", "")))
            elif kind == "resume":
                asyncio.create_task(console.nudge("resume"))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        console.clients.discard(websocket)


def serve():
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT,
                log_level="warning", access_log=False)
