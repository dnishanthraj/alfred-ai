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
import json
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
from ..engine import ContactSession, grapevine, guards, initiative, presence, world
from ..engine.party import MAX_CONTACTS, Call
from ..memory import migrate_legacy
from ..memory.store import atomic_write, read_text
from ..memory.texts import TextLog
from ..paths import WEB_DIR

# Synthesized clips waiting to be fetched. Bounded — a long session would
# otherwise hold every reply's audio in memory for the whole run.
_MAX_CACHED_CLIPS = 64

# What happened, when, and how long it took — for the times a reply goes quiet
# and there is otherwise nothing to look at. data/ is gitignored.
log = logging.getLogger("wayne")
if not log.handlers:
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
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
# Longer when they're in the middle of something, longer still when the phone
# was nowhere near them.
PICKUP_EXTRA = {presence.BUSY: (1.5, 5.0), presence.OFFLINE: (4.0, 9.0)}

# How long an incoming call rings before it counts as missed.
RING_FOR = 28
# A call they won't take: rejected after a ring or two, or left to ring out.
DECLINE_AFTER = (3.0, 8.0)
NO_ANSWER_AFTER = (16.0, 24.0)
# Ringing back within this long reads as urgent (see presence.answers).
RING_AGAIN = 240

# How often the console's clock ticks: presence, promises, the odd text.
PULSE_SECONDS = 30
# Nobody texts out of the blue twice within this long, across everyone.
INITIATIVE_GAP = 45 * 60
# How often someone whose phone is out of reach is looked at again.
RECHECK_SECONDS = 15


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
        # Texts waiting to be read, and the task answering each contact's.
        self._pending_texts = {}
        self._texters = {}
        # A contact ringing him: {"id", "about"}, and the timer that gives up.
        self._incoming = None
        self._ring_timer = None
        # The status each contact's dot last showed, to send only changes.
        self._shown_presence = {}
        # A call ringing out that won't be answered, and when each contact
        # last let him ring out — so ringing straight back reads as urgent.
        self._ringing_out = None
        self._refused_at = {}
        # Set from the moment a call is placed until it's up, so nobody rings
        # him in the gap while the line is still being opened.
        self._connecting = False
        self._tasks = set()
        self._writing_lines = False
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

        # Events go to the page through a releaser that waits for `release_at`
        # once; synthesis starts the moment each sentence exists. Holding the
        # main loop for pickup instead held synthesis too — the first event is
        # never a sentence — so every call had a voice round trip of silence
        # after "pickup", the opposite of the point.
        outbox, released, closed = asyncio.Queue(), set(), asyncio.Event()
        released_changed = asyncio.Condition()

        async def releaser():
            try:
                await hold()
                while True:
                    event = await outbox.get()
                    if event is done:
                        return
                    if superseded():
                        continue
                    await self.broadcast(event)
                    if event.get("type") == "sentence":
                        async with released_changed:
                            released.add(event["key"])
                            released_changed.notify_all()
            finally:
                closed.set()
                async with released_changed:
                    released_changed.notify_all()

        async def shown(key):
            """Until the page has the sentence a clip belongs to (or never will)."""
            async with released_changed:
                await released_changed.wait_for(
                    lambda: key in released or closed.is_set() or superseded())
            return key in released and not superseded()

        speech = asyncio.Queue()
        sender = asyncio.create_task(releaser())
        worker = asyncio.create_task(self._speech_worker(speech, shown, superseded))
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
                outbox.put_nowait(event)
        finally:
            outbox.put_nowait(done)
            speech.put_nowait(None)
            await sender
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

    async def _speech_worker(self, speech, shown, superseded):
        """
        Release clips in submission order. Synthesis runs concurrently, but a
        later sentence finishing first must never jump the queue — the page
        plays what it is handed.

        Once the turn is superseded, what is still being synthesised is
        cancelled where it can be and discarded where it can't: a contact
        talked over must not carry on three sentences later, and requests
        nobody will hear were holding ElevenLabs slots the next reply needed —
        which is how talking over someone degraded the voice link.

        When synthesis fails — quota gone, network down, key expired — the
        voice link degrades and the contact carries on in text. The text still
        reaches the screen: the page renders any sentence that never got audio.
        """
        reported = False
        while True:
            item = await speech.get()
            if item is None:
                return
            key, text, task, speaker = item
            if superseded():
                task.cancel()
                continue
            try:
                audio, words = await task
            except asyncio.CancelledError:
                continue
            except Exception:
                if not reported and not superseded():
                    reported = True
                    await self.broadcast(events.notice(
                        "Voice link degraded. Continuing in text.", "warn"))
                continue
            if audio and await shown(key):
                self.recent_speech.append((time.monotonic(), text))
                await self.broadcast({**events.speak(self._store_clip(audio), text, key, words),
                                      "speaker": speaker})

    async def connect(self, contact_id, incoming=None):
        """
        Switch the console to a contact, booting them on first connection.
        `incoming` is why they rang, when it was them who called.
        """
        contact = self.directory.get(contact_id)
        if contact is None:
            return
        if not incoming and self._incoming and self._incoming["id"] == contact_id:
            return await self.answer(contact_id)     # they're ringing him: that's answering
        if self._release:
            self._release.cancel()
            self._release = None
        if self._incoming and not incoming:
            # He rang someone else while a call was coming in: that one is missed.
            # One line at a time, in either direction.
            if self._ring_timer:
                self._ring_timer.cancel()
                self._ring_timer = None
            asyncio.get_running_loop().create_task(self._unanswered("missed"))
        self._connecting = True
        try:
            return await self._connect(contact, contact_id, incoming)
        finally:
            self._connecting = False

    async def _connect(self, contact, contact_id, incoming):
        switching = False
        if not self._abandon_ring() and self.current_id and self.current_id != contact_id:
            switching = True
            self.interrupt()
        async with self.turn_lock:
            if switching and self.current_id:
                # Switching lines mid-call: whoever was on it notices next time.
                # Inside the lock, so the turn in flight has finished first —
                # ending the call under it pulled its `call` away mid-sentence.
                for member in (self.call.members if self.call else [self.sessions[self.current_id]]):
                    member.call_ended("switched")
                self._end_call()
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

            state = presence.of(contact).now()
            whereabouts = state["status"]
            refused = None if incoming else presence.answers(
                contact, state, again=time.time() - self._refused_at.get(contact_id, 0) < RING_AGAIN)
            if refused:
                # Not picking up. The ring happens outside the lock, so texts
                # to anyone else carry on meanwhile.
                self._ringing_out = contact_id
                epoch = self.turn_epoch
        if refused:
            return await self._let_it_ring(contact, refused, state, epoch)
        async with self.turn_lock:
            if self.current_id != contact_id:
                return
            session = self.session_for(contact_id)
            # Nobody answers the instant it rings, so the line rings for a
            # varying moment — but the greeting is generated and synthesised
            # during it rather than after, so the ring is the whole wait.
            ring = (random.uniform(0.2, 0.6) if incoming else
                    random.uniform(*PICKUP_DELAY) + random.uniform(*PICKUP_EXTRA.get(whereabouts, (0, 0))))
            pickup = asyncio.get_running_loop().time() + ring

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
            await self.drive(session.boot(incoming), contact, self.interrupt(), release_at=pickup)
            presence.of(contact).touch()
            # Talking now: any plan to get back to him about a missed call is moot.
            presence.of(contact).drop("callback")
            await self._presence_changed(contact)

    def _abandon_ring(self):
        """
        He gave up on a call that was never going to be answered. Nothing was
        said, so nothing is remembered as a call — but they'll see they missed
        him, and may get back to him. True if there was such a ring.
        """
        contact = self.directory.get(self._ringing_out) if self._ringing_out else None
        if contact is None or self.current_id != contact.id:
            return False
        self._ringing_out = None
        self._refused_at[contact.id] = time.time()    # ringing straight back is urgent
        self.current_id = None
        if self.call:
            for member in list(self.call.members):
                self.call.leave(member)
        self.call = None
        self.interrupt()
        state = presence.of(contact).now()
        asyncio.get_running_loop().create_task(self._after_refusal(contact, "no_answer", state))
        return True

    async def _let_it_ring(self, contact, how, state, epoch):
        """
        A call they won't take. It rings — briefly if they reject it, until it
        gives up if they don't — then the line closes, and what they do about
        it afterwards is theirs: a quick "can't talk", a call back once
        they're free, a text later with or without a reason, or nothing.
        """
        await asyncio.sleep(random.uniform(*(DECLINE_AFTER if how == "declined" else NO_ANSWER_AFTER)))
        if self._ringing_out == contact.id:
            self._ringing_out = None
        if self.turn_epoch != epoch or self.current_id != contact.id:
            return          # he hung up, or rang someone else, first
        self._refused_at[contact.id] = time.time()
        self.current_id = None
        if self.call:
            for member in list(self.call.members):
                self.call.leave(member)
        self.call = None
        log.info("%s %s the call (%s)", contact.id, how, state["status"])
        entry = TextLog(contact.id).add("me", "", kind="refused_call" if how == "declined" else "unanswered_call")
        await self.broadcast({"type": "call_refused", "speaker": contact.id, "how": how})
        await self.broadcast({"type": "text_sent", "speaker": contact.id, "message": entry})
        asyncio.get_running_loop().create_task(self._after_refusal(contact, how, state))

    async def _after_refusal(self, contact, how, state):
        leaning = contact.initiative or {}
        doing = state["doing"] or ("asleep" if state["status"] == presence.OFFLINE else "")
        reason = f"you were {doing}" if doing else "you just didn't pick up"
        if (how == "declined" and state["status"] == presence.BUSY
                and random.random() < leaning.get("busy_text", 0.4)):
            await asyncio.sleep(random.uniform(6, 25))
            await self._send_unprompted(contact, reason, "busy_now")
        if random.random() >= leaning.get("callback", 0.8):
            return          # some people just don't
        whereabouts = presence.of(contact)
        now, current = time.time(), whereabouts.now()
        if current["source"] == "conversation" and current["until"]:
            due = current["until"] + random.uniform(2, 15) * 60
        elif current["status"] in (presence.BUSY, presence.OFFLINE):
            due = now + random.uniform(25, 90) * 60
        else:
            due = now + random.uniform(4, 35) * 60
        action = "call" if random.random() < leaning.get("callback_call", 0.5) else "text"
        whereabouts.intend(action, reason, due, origin="callback")

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
        if self._abandon_ring():
            return
        async with self.turn_lock:
            contact = self.contact
            self.current_id = None
            self._end_call()
            self.recent_speech.clear()
        if contact:
            self._release = asyncio.create_task(self._release_after(contact))

    def _end_call(self):
        members = self.call.members if self.call else (
            [self.sessions[self.current_id]] if self.current_id in self.sessions else [])
        present = {m.contact.id for m in members}
        for member in members:
            self._wrap_up(member, present)
        if self.call:
            for member in list(self.call.members):
                self.call.leave(member)
        self.call = None

    def _wrap_up(self, member, present):
        """
        One contact off the phone: what was said may travel (see
        wayne.engine.grapevine), they were just on the phone with him, and
        whatever the call set in motion is noted. The same whether the call
        ended or they were let off it early.
        """
        grapevine.note_call(member, list(self.directory), member.call_index(), present)
        presence.of(member.contact).touch()
        said = member.call_messages()
        if any(m["role"] == "user" for m in said):
            self._spawn(self._afterthought(member, said, "call"))

    def _spawn(self, coroutine):
        """
        A background task that is kept and whose failure is logged — a bare
        create_task's exception vanished, and the task could be collected
        mid-flight.
        """
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)

        def finished(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.warning("background task failed: %r", t.exception())
        task.add_done_callback(finished)
        return task

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
        state = presence.of(contact).now()
        how = presence.answers(contact, state,
                               again=time.time() - self._refused_at.get(contact_id, 0) < RING_AGAIN)
        if how:
            # Patched in, it rings — and they don't take it. The call goes on.
            await self.broadcast(events.party(self._members() + [contact_id], added=contact_id))
            await asyncio.sleep(random.uniform(*(DECLINE_AFTER if how == "declined" else NO_ANSWER_AFTER)))
            self._refused_at[contact_id] = time.time()
            await self.broadcast({"type": "call_refused", "speaker": contact_id, "how": how})
            await self.broadcast(events.party(self._members()))
            asyncio.get_running_loop().create_task(self._after_refusal(contact, how, state))
            return
        epoch = self.interrupt()
        async with self.turn_lock:
            newcomer = self.session_for(contact_id)
            if not self.call or not self.call.join(newcomer):
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
            # Re-checked: a hang-up, a switch or a second drop may have landed
            # while this waited.
            if not self.call or contact_id not in self._members() or len(self.call.members) < 2:
                return
            member = self.sessions[contact_id]
            await self.drive(self.call.farewell(member), member.contact, epoch)
            if not self.call or member not in self.call.members:
                return
            self.call.leave(member)
            self._wrap_up(member, {contact_id} | set(self._members()))
            if self.current_id == contact_id and self.call.members:
                self.current_id = self.call.members[0].contact.id
            await self.broadcast(events.party(self._members(), removed=contact_id))

    async def text(self, contact_id, body):
        """
        A text message to a contact — any contact, on a call or not.

        Texts behave like texts: delivered at once, read when the contact gets
        to it (promptly for Alfred, eventually for Jason, sometimes not for a
        while because someone is busy), answered after a typing delay that fits
        the reply — and several sent in a row are read and answered together.
        To whoever you're on a call with, a text lands mid-call and they answer
        it out loud: "sent you the address" — "got it, the docks."
        """
        contact = self.directory.get(contact_id)
        body = (body or "").strip()
        if contact is None or not body:
            return
        log = TextLog(contact_id)
        message = log.add("me", body)
        await self.broadcast({"type": "text_sent", "speaker": contact_id, "message": message})

        if contact_id in self._members() or contact_id == self.current_id:
            read_at = log.mark_read([message["id"]])
            await self.broadcast({"type": "text_read", "speaker": contact_id,
                                  "ids": [message["id"]], "at": read_at})
            return await self._text_into_call(contact_id, body)

        self._pending_texts.setdefault(contact_id, []).append(message)
        self._start_answering(contact)

    def _start_answering(self, contact):
        if contact.id not in self._texters:
            self._texters[contact.id] = asyncio.create_task(self._answer_texts(contact))

    async def _answer_texts(self, contact):
        """
        Read when they get to it, then think, type and reply. When they get to
        it is whatever they're doing (see wayne.engine.presence): seconds if
        the phone is in their hand, minutes if it's down, an hour in a meeting,
        the morning if they're asleep — and a glance from the middle of
        something now and then, which gets a line rather than an answer.
        """
        loop = asyncio.get_running_loop()
        whereabouts = presence.of(contact)
        pace = contact.texting_pace or {}
        try:
            await self._wait_to_read(contact)
            batch = self._pending_texts.pop(contact.id, [])
            if not batch:
                return
            log = TextLog(contact.id)
            ids = [m["id"] for m in batch]
            read_at = log.mark_read(ids)
            await self.broadcast({"type": "text_read", "speaker": contact.id,
                                  "ids": ids, "at": read_at})
            if contact.id in self._members():
                # He rang them before they got to it: they read it now and
                # answer out loud, as with any text that lands mid-call.
                return await self._text_into_call(contact.id, "\n".join(m["text"] for m in batch))
            glancing = whereabouts.now()["status"] == presence.BUSY
            if not glancing:
                whereabouts.touch()
            await self._presence_changed(contact)
            if not glancing and random.random() < pace.get("on_read", 0):
                # Left on read, for a while. Some people do.
                await asyncio.sleep(random.uniform(*pace.get("on_read_for", [180, 1200])))
            else:
                await asyncio.sleep(random.uniform(0.8, 3.0))
            await self.broadcast({"type": "text_typing", "speaker": contact.id})
            started = loop.time()
            reply = await self._write_text(contact, "\n".join(m["text"] for m in batch))
            if reply:
                await self._deliver(contact, reply, started)
                whereabouts.drop("callback")     # back in touch; no need to ring him back
                if not glancing:
                    whereabouts.touch()
                    if random.random() < (contact.texting_pace or {}).get("drift", 0):
                        # And then they put the phone down, mid-conversation,
                        # as people do: the next text waits.
                        whereabouts.touch(time.time() - presence.ENGAGED_FOR)
                session = self.session_for(contact.id)
                loop.create_task(self._afterthought(session, session.history.messages[-6:], "text"))
                if (not glancing and random.random()
                        < (contact.texting_style or {}).get("second_thought", 0)):
                    loop.create_task(self._second_thought(contact))
            else:
                await self.broadcast({"type": "text_idle", "speaker": contact.id})
            self._release_later(contact)
        finally:
            self._texters.pop(contact.id, None)
            if self._pending_texts.get(contact.id):
                # More arrived while they were replying: another round.
                self._start_answering(contact)

    async def _second_thought(self, contact):
        """'oh and —': one more text a minute later, unless he's already replied."""
        await asyncio.sleep(random.uniform(20, 90))
        last = TextLog(contact.id).last()
        if not last or last["from"] != "them" or contact.id in self._texters:
            return
        await self._send_unprompted(contact, "", "second_thought")

    async def _wait_to_read(self, contact):
        """
        Until they'd look at their phone. Re-judged whenever what they're doing
        changes — a meeting ending, or him ringing them — so a text sent to
        someone asleep is read when they wake, not on a timer set at 3am.
        """
        whereabouts = presence.of(contact)
        deadline, last = None, None
        while True:
            state = whereabouts.now()
            if state["status"] != last:
                last = state["status"]
                delay = presence.read_delay(contact, state)
                if delay is None:
                    deadline = None      # gone to sleep before getting to it
                else:
                    candidate = time.time() + delay
                    deadline = candidate if deadline is None else min(deadline, candidate)
            if deadline is not None and time.time() >= deadline:
                return
            await asyncio.sleep(RECHECK_SECONDS if deadline is None
                                else min(RECHECK_SECONDS, max(0.05, deadline - time.time())))

    async def _deliver(self, contact, reply, started, origin=None):
        """
        A reply sent as they'd send it: one composed message, or three in a
        row, each typed at their own speed (less the time spent thinking).
        """
        loop = asyncio.get_running_loop()
        per_second = max(1.0, (contact.texting_pace or {}).get("wpm", 50) * 5 / 60)
        log = TextLog(contact.id)
        recent = [m["text"] for m in log.page(limit=8) if m["from"] == "them"][-3:]
        parts = initiative.bubbles(contact, initiative.untic(contact, reply, recent))
        for i, part in enumerate(parts):
            if i:
                await self.broadcast({"type": "text_typing", "speaker": contact.id})
            typing = min(len(part) / per_second, 20)
            if i == 0:
                typing -= loop.time() - started
            if typing > 0:
                await asyncio.sleep(typing)
            sent = log.add("them", part, origin=origin)
            await self.broadcast({"type": "text_reply", "speaker": contact.id, "message": sent})
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.4, 1.4))

    async def _afterthought(self, session, exchanges, by):
        """What the exchange set in motion — see wayne.engine.initiative."""
        try:
            found = await asyncio.to_thread(initiative.afterthought, session, exchanges, by)
            if found:
                # What, never the words: the log sits beside encrypted memory.
                log.info("%s afterthought: doing=%s contact=%s", session.contact.id,
                         bool(found.get("doing")), bool(found.get("contact")))
            await self._presence_changed(session.contact)
        except Exception as exc:
            log.warning("%s afterthought failed: %s", session.contact.id, str(exc)[:160])

    def _release_later(self, contact):
        """Free the model a while after a text, as after a call — unless a call is up."""
        if self.current_id is None:
            if self._release:
                self._release.cancel()
            self._release = asyncio.create_task(self._release_after(contact))

    async def _write_text(self, contact, body):
        """The contact writes a text reply. Held to the same lock as call turns."""
        session = self.session_for(contact.id)
        loop = asyncio.get_running_loop()
        result = {"text": ""}

        def run():
            for event in session.ask(body, via="text"):
                if event["type"] == "reply_end" and not event.get("interim"):
                    result["text"] = event["text"]

        async with self.turn_lock:
            try:
                await loop.run_in_executor(None, run)
            except Exception as exc:
                log.warning("%s text FAILED: %s", contact.id, str(exc)[:160])
        return result["text"]

    async def _text_into_call(self, contact_id, body):
        """A text to someone on the line: they see it, and answer on the call."""
        epoch = self.interrupt()
        session = self.session_for(contact_id)
        async with self.turn_lock:
            if epoch != self.turn_epoch or contact_id not in self._members():
                # Something newer came first. The text was still read: it
                # rides along with their next turn instead of vanishing.
                session.unseen_texts.append(body)
                return
            if self.call and self.call.is_group:
                turn = self.call.text_turn(session, body)
            else:
                turn = session.ask(body, via="text_on_call")
            await self.drive(turn, self.contact or session.contact, epoch)

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

    # --- the console's clock ---------------------------------------------

    async def pulse(self):
        """
        Every half minute: whose dot has changed, which promises have fallen
        due, and whether anyone has a reason to get in touch. This is what
        makes the directory a set of people getting on with their evenings
        rather than seven phones waiting for him.
        """
        await asyncio.sleep(5)
        self._resume_unread()
        while True:
            try:
                await self._tick()
            except Exception as exc:
                log.warning("pulse: %s", str(exc)[:200])
            await asyncio.sleep(PULSE_SECONDS)

    def _resume_unread(self):
        """Texts still unread from before a restart are waiting to be read."""
        for contact in self.directory:
            waiting = TextLog(contact.id).unread()
            pending = self._pending_texts.setdefault(contact.id, [])
            known = {m["id"] for m in pending}
            pending[:0] = [m for m in waiting if m["id"] not in known]
            if pending:
                self._start_answering(contact)
            else:
                self._pending_texts.pop(contact.id, None)

    async def _tick(self):
        now = time.time()
        for contact in self.directory:
            await self._presence_changed(contact)
        await self._write_status_lines()
        if not self.clients:
            return      # nobody at the console to hear about it
        for contact in self.directory:
            for intent in presence.of(contact).due(now):
                # Spawned, not awaited: one contact typing out a long reply
                # stalled every dot and every other promise behind it.
                presence.of(contact).postpone(intent["id"], 120, count=False)  # claimed
                self._spawn(self._carry_out(contact, intent))
        await self._maybe_reach_out(now)

    async def _write_status_lines(self):
        """
        Anyone whose situation has changed writes themselves a new status line —
        but only while the model is already loaded for something else. Loading
        fifteen gigabytes to write "on patrol 🦇" is how a laptop gets hot.
        """
        # Never in the middle of anything: a status line generated during a call
        # queues the next spoken reply behind it at the model.
        if (self._writing_lines or not self.clients or self.current_id or self._texters
                or self.turn_lock.locked()):
            return
        stale = [c for c in self.directory if not presence.of(c).has_current_line()]
        if not stale or not await asyncio.to_thread(_model_loaded, stale[0].model):
            return
        self._writing_lines = True
        try:
            for contact in stale[:3]:
                whereabouts = presence.of(contact)
                key = whereabouts.line_key()
                line = await asyncio.to_thread(initiative.status_line, contact, whereabouts.now())
                if line:
                    whereabouts.set_line(key, line)
                    await self._presence_changed(contact)
        finally:
            self._writing_lines = False

    async def _presence_changed(self, contact):
        shown = presence.of(contact).public()
        key = (shown["status"], shown["doing"], shown.get("line", ""))
        if self._shown_presence.get(contact.id) != key:
            self._shown_presence[contact.id] = key
            await self.broadcast({"type": "presence", "speaker": contact.id, "presence": shown})

    async def _carry_out(self, contact, intent):
        """A promise falls due: they text, or they ring."""
        whereabouts = presence.of(contact)
        if contact.id in self._members():
            whereabouts.done(intent["id"])   # on the line with him; they'll just say it
            return
        if whereabouts.now()["status"] == presence.OFFLINE:
            whereabouts.postpone(intent["id"], 600, count=False)
            return
        callback = intent["origin"] == "callback"
        if intent["action"] == "call":
            if not (self.current_id or self._incoming or self._connecting):
                whereabouts.done(intent["id"])
                about = (f"calling him back — he rang you earlier and you didn't pick up "
                         f"({intent['about']})") if callback else intent["about"]
                return await self._ring(contact, about)
            if intent.get("tries", 0) < 2:
                whereabouts.postpone(intent["id"], 300)   # he's on another call
                return
        whereabouts.done(intent["id"])
        await self._send_unprompted(contact, intent["about"], "callback" if callback else "promise")

    def _quiet(self, now):
        try:
            start, end = (int(h) for h in config.QUIET_HOURS.split("-"))
        except ValueError:
            return False
        hour = time.localtime(now).tm_hour
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    def _initiative_log(self):
        try:
            return json.loads(read_text(paths.DATA_DIR / "_initiative.json") or "[]")
        except ValueError:
            return []

    async def _maybe_reach_out(self, now):
        """
        The unprompted text: chasing a question he left hanging, or something
        on their mind. Budgeted — a few a day across everyone, never two
        within the hour, none while he sleeps, none from someone already
        waiting on him — so it reads as people, not notifications.
        """
        if config.INITIATIVE_PER_DAY <= 0 or self._quiet(now):
            return
        sent = [t for t in self._initiative_log() if now - t < 86400]
        if len(sent) >= config.INITIATIVE_PER_DAY or (sent and now - max(sent) < INITIATIVE_GAP):
            return
        chases, impulses = [], []
        for contact in self.directory:
            if contact.id in self._members() or contact.id in self._texters:
                continue
            whereabouts = presence.of(contact)
            if whereabouts.now()["status"] not in (presence.ONLINE, presence.IDLE):
                continue
            leaning = contact.initiative or {}
            last = TextLog(contact.id).last()
            if last and last["from"] == "me":
                continue        # he's waiting on them, not the other way round
            if last and last["from"] == "them":
                # Their question, unanswered: chase it — once, if they're the type.
                chase = whereabouts.get("chase") or {}
                if (last["text"].rstrip().endswith("?") and chase.get("id") != last["id"]
                        and last.get("origin") != "chase"):
                    lo, hi = leaning.get("chase_after", [30, 180])
                    chase = {"id": last["id"], "at": last["at"] + random.uniform(lo, hi) * 60
                             if random.random() < leaning.get("double_text", 0.3) else None}
                    whereabouts.put("chase", chase)
                if chase.get("id") == last["id"] and chase.get("at") and now >= chase["at"]:
                    chases.append(contact)
                    continue
                if now - last["at"] < 12 * 3600:
                    continue    # they spoke last, and recently; it's his turn
            # Something on their mind: a few times a day for the chatty ones.
            if random.random() < leaning.get("per_day", 0.4) * PULSE_SECONDS / (16 * 3600):
                impulses.append(contact)
        if chases:
            contact = random.choice(chases)
            presence.of(contact).put("chase", {"id": (presence.of(contact).get("chase") or {}).get("id")})
            self._count_initiative()
            self._spawn(self._send_unprompted(contact, "", "chase"))
        elif impulses:
            contact = random.choice(impulses)
            about = initiative.impulse(self.session_for(contact.id))
            if about:
                self._count_initiative()
                self._spawn(self._send_unprompted(contact, about, "impulse"))

    def _count_initiative(self):
        """Counted when decided, not when sent — or the next tick decides again."""
        entries = [t for t in self._initiative_log() if time.time() - t < 86400]
        atomic_write(paths.DATA_DIR / "_initiative.json", json.dumps(entries + [time.time()]))

    async def _send_unprompted(self, contact, about, why):
        """They text first: written, then typed, then sent."""
        session = self.session_for(contact.id)
        loop = asyncio.get_running_loop()
        async with self.turn_lock:
            if contact.id in self._members():
                return      # he's rung them meanwhile; they'll say it, not text it
            text = await loop.run_in_executor(None, session.reach_out, about, why)
        if not text:
            return
        log.info("%s texted first (%s), %d chars", contact.id, why, len(text))
        presence.of(contact).touch()
        await self._presence_changed(contact)
        await self.broadcast({"type": "text_typing", "speaker": contact.id})
        await self._deliver(contact, text, loop.time(), origin=why)
        self._release_later(contact)

    # --- calls to him -------------------------------------------------------

    async def _ring(self, contact, about):
        """They call him. The page rings; he answers, declines, or misses it."""
        self._incoming = {"id": contact.id, "about": about}
        log.info("%s ringing him", contact.id)
        await self.broadcast({"type": "call_incoming", "speaker": contact.id})
        self._ring_timer = asyncio.create_task(self._ring_out(contact.id))

    async def _ring_out(self, contact_id):
        await asyncio.sleep(RING_FOR)
        if self._incoming and self._incoming["id"] == contact_id:
            self._ring_timer = None
            await self._unanswered("missed")

    async def answer(self, contact_id):
        incoming = self._incoming
        if not incoming or incoming["id"] != contact_id:
            # Too late — it had already rung out. Tell the page, which is
            # otherwise left waiting on a line that isn't there.
            await self.broadcast({"type": "call_unanswered", "speaker": contact_id,
                                  "how": "missed", "message": None})
            return
        self._incoming = None
        if self._ring_timer:
            self._ring_timer.cancel()
            self._ring_timer = None
        await self.connect(contact_id, incoming=incoming["about"])

    async def decline(self, contact_id):
        if self._incoming and self._incoming["id"] == contact_id:
            if self._ring_timer:
                self._ring_timer.cancel()
                self._ring_timer = None
            await self._unanswered("declined")

    async def _unanswered(self, how):
        """A declined or missed call goes in the thread — and they may text instead."""
        incoming, self._incoming = self._incoming, None
        contact = self.directory.get(incoming["id"]) if incoming else None
        if contact is None:
            return
        entry = TextLog(contact.id).add("them", "", kind=f"{how}_call")
        await self.broadcast({"type": "call_unanswered", "speaker": contact.id,
                              "how": how, "message": entry})
        if random.random() < (contact.initiative or {}).get("react", 0.6):
            await asyncio.sleep(random.uniform(6, 30) if how == "declined" else random.uniform(20, 80))
            await self._send_unprompted(contact, incoming["about"], how)

    async def nudge(self, kind="check_in"):
        """
        Break a long silence. Not a notification — a turn like any other, so
        what he says is generated in character and differs every time. It is
        never stored in history: a check-in that went unanswered shouldn't
        become part of what he remembers of the conversation.
        """
        if not self.current_id or self._ringing_out:
            return
        # The epoch is taken before waiting, as submit does: bumping it after
        # the wait cancelled whatever he'd said in the meantime, and his words
        # were replaced by a check-in.
        epoch = self.interrupt()
        async with self.turn_lock:
            if epoch != self.turn_epoch or not self.current_id:
                return
            await self.drive(getattr(self.session_for(self.current_id), kind)(), self.contact, epoch)

    async def submit(self, text, spoken=False, confidence=1.0):
        """
        Run one turn. `spoken` marks input that came from a microphone, which is
        the only kind that can be an acoustic echo — typed text never is.
        """
        text = (text or "").strip()
        if not text or not self.current_id or self._ringing_out == self.current_id:
            return      # nobody on the line — it's ringing, and they won't answer
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


def _model_loaded(model):
    """Whether Ollama has this model in memory right now."""
    try:
        import ollama
        return any(m.get("model") == model or m.get("name") == model
                   for m in ollama.ps().get("models", []))
    except Exception:
        return False


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
        asyncio.create_task(console.pulse()),
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
        "presence": presence.of(contact).public(),
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
async def messages(contact_id: str, before: float = 0, limit: int = 40):
    """
    A page of the text thread, oldest first: the newest `limit` messages older
    than `before` (a timestamp), so the page can load further back on scroll.
    """
    if console.directory.get(contact_id) is None:
        return Response(status_code=404)
    page = TextLog(contact_id).page(before or None, max(1, min(limit, 200)))
    return JSONResponse({"messages": page, "typing": contact_id in console._texters})


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
            try:
                payload = await websocket.receive_json()
            except (ValueError, TypeError):
                continue        # one malformed message must not close the socket
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "prompt":
                try:
                    confidence = float(payload.get("confidence") or 1.0)
                except (TypeError, ValueError):
                    confidence = 1.0
                asyncio.create_task(console.submit(
                    payload.get("text", ""), spoken=bool(payload.get("spoken")),
                    confidence=confidence))
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
            elif kind == "answer":
                asyncio.create_task(console.answer(payload.get("id", "")))
            elif kind == "decline":
                asyncio.create_task(console.decline(payload.get("id", "")))
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
