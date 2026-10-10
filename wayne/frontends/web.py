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
import logging.handlers
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
from ..engine import (
    ContactSession,
    culture,
    grapevine,
    groupchat,
    guards,
    initiative,
    places,
    presence,
    travel,
    world,
)
from ..engine.party import MAX_CONTACTS, Call
from ..engine.session import ASKS_REACTION
from ..memory import groups as group_store
from ..memory import migrate_legacy
from ..memory.history import describe_gap
from ..memory.store import atomic_write, read_text
from ..memory.texts import TextLog, quoted_by
from ..paths import WEB_DIR
from .groupchats import GroupChats

# Synthesized clips waiting to be fetched. Bounded — a long session would
# otherwise hold every reply's audio in memory for the whole run.
_MAX_CACHED_CLIPS = 64

# What happened, when, and how long it took — for the times a reply goes quiet
# and there is otherwise nothing to look at. data/ is gitignored.
log = logging.getLogger("wayne")
# How often two people on a group call start into the same pause together.
TALK_OVER = 0.12
# How often someone on a call remarks on a line ringing in.
RING_REMARK = 0.55
if not log.handlers:
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # A line per sentence adds up over months of evenings: two megabytes, then
    # it rolls over, keeping the last two.
    _handler = logging.handlers.RotatingFileHandler(paths.DATA_DIR / "console.log", maxBytes=2_000_000,
                                                    backupCount=2)
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
# Several texts inside this window is a burst — enough to make someone look.
BURST_WINDOW = 300
# Words that make even someone ghosting him answer.
_URGENT = re.compile(r"(?i)\b(help|emergency|hurt|hospital|bleeding|urgent|now|please call|call me|"
                     r"are you (ok|okay|alive|safe)|answer)\b")

# How often the console's clock ticks: presence, promises, the odd text.
PULSE_SECONDS = 30
# Nobody texts out of the blue twice within this long, across everyone.
# The least time between unprompted texts from anyone: often enough to feel
# like a life going on around him, not so often it's a feed.
INITIATIVE_GAP = 22 * 60
# How often someone whose phone is out of reach is looked at again.
RECHECK_SECONDS = 15


class Console(GroupChats):
    """
    The console as a whole: a directory of contacts, one live session per
    contact you have spoken to, and the fan-out to connected browser tabs.
    """
    # The ring in progress (to one, or to a group), and who a group ring invites.
    _ring_token = _group_ring = _inviting = call = None

    # Every sentence sent to the page gets a key that is never reused. The
    # engine numbers sentences from 0 within each reply, and the page used
    # those numbers to remember what it had already shown — so after "One
    # moment." took slot 0, the real answer's first sentence (also 0) was
    # treated as already shown and never appeared, and a check-in after an
    # unanswered reply could vanish the same way.
    _said = 0
    PULSE_SECONDS = PULSE_SECONDS

    def __init__(self):
        self.directory = directory()
        self.voice = get_voice_engine()
        self.clients = set()
        self.audio_clips = OrderedDict()
        self.turn_lock = asyncio.Lock()
        self.sessions = {}
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
        # When he last rang or texted each contact — for "he keeps calling".
        self._call_attempts = {}
        # How much life a group call has left for talk nobody prompted: each
        # line said into a pause uses some up, his next word restores it.
        self._lull_energy = 1.0
        self._lulls = 0
        self._last_lull = None
        self._text_bursts = {}
        # Set from the moment a call is placed until it's up, so nobody rings
        # him in the gap while the line is still being opened.
        self._connecting = False
        self._tasks = set()
        self._adding, self._dropped_adds = set(), set()     # rung in and still ringing; stopped
        self._closing = set()        # cases being wound up, so the tick doesn't start another
        self._writing_lines = False
        self._typing_now = set()
        self._init_groups()
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
        return self.sessions[contact_id]

    # --- fan-out ----------------------------------------------------------

    def _note_typing(self, event):
        """
        Who is actually typing right now — not who merely has a reply pending.
        A thread opened mid-wait asked "is anyone typing?" and was told yes for
        anyone waiting to read, which for Randy can be hours of dots.
        """
        kind = event.get("type")
        if kind == "text_typing":
            self._typing_now.add(("text", event["speaker"]))
        elif kind in ("text_reply", "text_idle"):
            self._typing_now.discard(("text", event["speaker"]))
        elif kind == "group_typing":
            self._typing_now.add((event["group"], event["speaker"]))
        elif kind == "group_idle":
            self._typing_now.discard((event["group"], event["speaker"]))
        elif kind == "group_message":
            self._typing_now.discard((event["group"], event["message"]["from"]))

    async def broadcast(self, event):
        self._note_typing(event)
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
                if event.get("type") in ("sentence", "reply_end") and not event.get("speaker"):
                    # Whoever's line it is, said: on a group call the first to
                    # pick up greeted it unlabelled, and their seat stayed blank.
                    event = {**event, "speaker": contact.id}
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

    async def connect(self, contact_id, incoming=None, ensure=False, also_ringing=()):
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
            self._spawn(self._unanswered("missed"))
        self._connecting = True
        try:
            return await self._connect(contact, contact_id, incoming, ensure, also_ringing)
        finally:
            self._connecting = False

    async def _connect(self, contact, contact_id, incoming, ensure=False, also_ringing=()):
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
            # Everyone rung together is invited from the start, so a quick
            # decliner can still dial back in.
            self.call.invited.update(self._inviting or ())
            self._inviting = None
            await self.broadcast(events.contact_changed(contact_id))
            await self.broadcast(events.party([contact_id]))

            if not contact.availability.is_available():
                await self.broadcast(events.notice(
                    contact.availability.away_message, "warn"))
                return

            state = presence.of(contact).now()
            whereabouts = state["status"]
            rung = 0 if incoming else self._attempt(self._call_attempts, contact_id, RING_AGAIN)
            refused = None if (incoming or ensure) else presence.answers(contact, state, again=rung)
            if refused:
                # Not picking up. The ring happens outside the lock, so texts
                # to anyone else carry on meanwhile. The ring has a token of its
                # own: a text or an add meanwhile used to bump the shared epoch,
                # the refusal was forgotten, and they "answered" after all.
                self._ringing_out = contact_id
                ring = self._ring_token = object()
        if refused:
            return await self._let_it_ring(contact, refused, state, ring)
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
            await self.drive(session.boot(incoming, rung, also_ringing), contact, self.interrupt(),
                             release_at=pickup)
            if self.current_id != contact_id:
                return          # he'd moved on to another line while this one booted
            await self.broadcast({"type": "picked_up", "speaker": contact_id})
            self._call_attempts.pop(contact_id, None)
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
        self._ringing_out = self._ring_token = None
        self._refused_at[contact.id] = time.time()    # ringing straight back is urgent
        self.current_id = None
        if self.call:
            for member in list(self.call.members):
                self.call.leave(member)
        self.call = None
        self.interrupt()
        state = presence.of(contact).now()
        self._spawn(self._after_refusal(contact, "no_answer", state))
        return True

    async def _let_it_ring(self, contact, how, state, ring):
        """
        A call they won't take. It rings — briefly if they reject it, until it
        gives up if they don't — then the line closes, and what they do about
        it afterwards is theirs: a quick "can't talk", a call back once
        they're free, a text later with or without a reason, or nothing.
        """
        await asyncio.sleep(random.uniform(*(DECLINE_AFTER if how == "declined" else NO_ANSWER_AFTER)))
        if self._ring_token is not ring or self.current_id != contact.id:
            return          # he hung up, or rang someone else (or them again), first
        self._ringing_out = self._ring_token = None
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
        self._spawn(self._after_refusal(contact, how, state))

    async def _after_refusal(self, contact, how, state):
        leaning = contact.initiative or {}
        doing = state["doing"] or ("asleep" if state["status"] == presence.OFFLINE else "")
        reason = f"you were {doing}" if doing else "you just didn't pick up"
        call = self.call
        if (how == "declined" and state["status"] == presence.BUSY
                and random.random() < leaning.get("busy_text", 0.4)):
            await asyncio.sleep(random.uniform(6, 25))
            await self._send_unprompted(contact, reason, "busy_now")
        if random.random() >= leaning.get("callback", 0.8):
            return          # some people just don't
        if call is not None and call is self.call and contact.id in call.invited:
            # A group call that's still going: they may dial back into it
            # rather than ring him about it afterwards.
            return await self._join_late(contact, call, reason)
        self._plan_callback(contact, reason)

    async def _join_late(self, contact, call, reason):
        """
        Missed the ring of a call that's still going. Free in a minute or two,
        they come on — saying where they were, or not; busy, it waits until
        they are; and if it's over by then, they get back to him as they would.
        """
        whereabouts = presence.of(contact)
        current = whereabouts.now()
        if current["status"] == presence.OFFLINE:
            return self._plan_callback(contact, reason)
        wait = random.uniform(40, 240)
        if current["source"] == "conversation" and current["until"]:
            wait = max(wait, current["until"] - time.time())
        elif current["status"] == presence.BUSY:
            wait += random.uniform(120, 600)
        await asyncio.sleep(wait)
        if self.call is not call or contact.id in self._members():
            return self._plan_callback(contact, reason)
        if len(call.members) >= MAX_CONTACTS:
            return
        log.info("%s dialling back into the call they missed", contact.id)
        await self.add(contact.id, ensure=True, willing=True,
                       note=f"you missed his call a few minutes ago ({reason}) and have just "
                            "dialled back into it")

    def _plan_callback(self, contact, reason):
        leaning = contact.initiative or {}
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
        self._group_ring = None
        if self._abandon_ring():
            return
        async with self.turn_lock:
            contact = self.contact
            self.current_id = None
            self._end_call()
            self.recent_speech.clear()
        if contact:
            self._release = self._spawn(self._release_after(contact))

    def _end_call(self):
        members = self.call.members if self.call else (
            [self.sessions[self.current_id]] if self.current_id in self.sessions else [])
        present = {m.contact.id for m in members}
        if len(present) > 1:
            self._spawn(self._after_group_call(present))
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
                log.error("background task failed", exc_info=t.exception())
        task.add_done_callback(finished)
        return task

    def _members(self):
        return [m.contact.id for m in self.call.members] if self.call else []

    async def add(self, contact_id, ensure=False, note="", willing=False, by_him=False, remark=True):
        """
        Patch another contact into the call. They ring, pick up already knowing
        who is on the line and the last few things said, and greet the call.
        """
        contact = self.directory.get(contact_id)
        if (contact is None or not self.call or contact_id in self._members() or self._ringing_out
                or not contact.availability.is_available()):
            return
        call = self.call
        if len(self.call.members) >= MAX_CONTACTS:
            await self.broadcast(events.notice(
                f"The line's full — {contact.name} can't be patched in until someone drops off.", "warn"))
            return
        self.call.invited.add(contact_id)
        self._adding.add(contact_id)
        try:
            return await self._ring_in(contact, call, ensure, note, willing, by_him, remark)
        finally:
            self._adding.discard(contact_id)
            self._dropped_adds.discard(contact_id)

    async def _ring_in(self, contact, call, ensure, note, willing, by_him, remark):
        """The ring itself, for add(): every step checks it's still that call, and still wanted."""
        contact_id = contact.id

        def gone():
            return self.call is not call or contact_id in self._dropped_adds
        state = presence.of(contact).now()
        how = presence.answers(contact, state,
                               again=self._attempt(self._call_attempts, contact_id, RING_AGAIN))
        if how and not ensure:
            # Patched in, it rings — and they don't take it. The call goes on.
            await self.broadcast(events.party(self._members() + [contact_id], added=contact_id))
            rang = asyncio.get_running_loop().time()
            await self._while_it_rings(contact)
            wait = random.uniform(*(DECLINE_AFTER if how == "declined" else NO_ANSWER_AFTER))
            await asyncio.sleep(max(0.0, wait - (asyncio.get_running_loop().time() - rang)))
            self._refused_at[contact_id] = time.time()
            if gone():
                return          # hung up meanwhile, or he stopped ringing them
            await self.broadcast({"type": "call_refused", "speaker": contact_id, "how": how})
            await self.broadcast(events.party(self._members()))
            self._spawn(self._after_refusal(contact, how, state))
            # Someone on the line may well remark on it.
            missed = "didn't pick up" if how == "no_answer" else "declined"
            await self._react(f"{contact.name} {missed} when he tried to add them to the call.",
                              absent=contact)
            return
        # He asked for it: whoever's mid-sentence stops. Rung in by someone else
        # — a chase, a call back, the rest of a group ring — it waits its turn.
        epoch = self.interrupt() if by_him else self.turn_epoch
        loop = asyncio.get_running_loop()
        await self.broadcast(events.party(self._members() + [contact_id], added=contact_id))
        rang = loop.time()
        if remark:
            await self._while_it_rings(contact)
        async with self.turn_lock:
            newcomer = self.session_for(contact_id)
            if gone() or not self.call.join(newcomer):
                # The call's gone, he stopped the ring, or the line filled: their seat goes.
                if self.call is call:
                    await self.broadcast(events.party(self._members(), removed=contact_id))
                return
            # The greeting is made while it rings and released when they "pick up".
            pickup = max(rang + 2.6 + random.uniform(0.3, 1.6), loop.time() + 0.5)
            await self.drive(self.call.greet(newcomer, note, willing), contact,
                             self.turn_epoch if epoch != self.turn_epoch else epoch, release_at=pickup)
        if self.call is not call or newcomer not in self.call.members:
            return
        # Whatever happened to the greeting, they've picked up — and the page is
        # told who's on the line now, whatever else it was told meanwhile.
        await self.broadcast({"type": "picked_up", "speaker": contact_id})
        await self.broadcast(events.party(self._members()))
        if getattr(newcomer, "_hanging_up", False):
            # Picked up only to say no, and gone: the others may say something.
            await self._hang_ups(reason="said their piece on joining and hung up")
            return
        # And someone already on the line may greet them back — unless they
        # already have, answering the greeting.
        if self.call and not self.call.aside_answered and random.random() < 0.6:
            joined = (f"{contact.name} has just picked up too — he rang you all at once" if "all at once" in note else
                      f"{contact.name} has just dialled back into the call" if "dialled back" in note else
                      f"{contact.name} has just joined the call")
            await self._react(joined + ".", but=contact_id)

    async def drop(self, contact_id):
        """Let one contact off the call. They say goodbye; the call goes on."""
        if self.call and contact_id in self._adding and contact_id not in self._members():
            # Still ringing: he's thought better of it.
            self._dropped_adds.add(contact_id)
            await self.broadcast(events.party(self._members(), removed=contact_id))
            return
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
        if self.call and self.call.is_group and random.random() < 0.5:
            await self._react(f"{member.contact.name} has just dropped off the call.")

    async def _while_it_rings(self, contact):
        """
        While a line rings, someone already on the call may say something about
        it — "ugh, why are you ringing him?" — their way, or nothing.
        """
        if not self.call or not self.call.members or random.random() >= RING_REMARK:
            return
        member = random.choice(self.call.members)
        epoch = self.turn_epoch
        async with self.turn_lock:
            if epoch != self.turn_epoch or not self.call or member not in self.call.members:
                return
            await self.drive(self.call.ringing(member, contact.name), member.contact, epoch)

    async def _react(self, what_happened, but=None, absent=None, who=None):
        """
        One person on the call, picked at random, reacts to it — or doesn't. If
        it's someone who didn't pick up, they may chase them ("I'll text her"),
        and the one chased may then join.
        """
        if not self.call or not self.call.members:
            return
        candidates = [m for m in self.call.members if m.contact.id != but
                      and (who is None or m.contact.id == who)]
        if not candidates:
            return
        member = random.choice(candidates)
        # Waits for whoever's speaking — a greeting, a reply — rather than
        # cutting them off; anything he says first makes it moot.
        epoch = self.turn_epoch
        async with self.turn_lock:
            if epoch != self.turn_epoch or not self.call or member not in self.call.members:
                return
            await self.drive(self.call.react(member, what_happened, chase=absent is not None),
                             member.contact, epoch)
        if absent is not None and getattr(member, "_nudging", False):
            member._nudging = False
            self._spawn(self._chased(absent, member.contact))
        # A reaction can be "I'm out too" or "I'll get Barbara on".
        await self._hang_ups()
        await self._call_adds()

    async def _chased(self, contact, by):
        """
        Someone on the call chased them. A minute later they've seen it — and,
        far likelier now than when the phone just rang, they join, knowing who
        got them there.
        """
        await asyncio.sleep(random.uniform(20, 60))
        if (not self.call or contact.id in self._members()
                or len(self.call.members) >= MAX_CONTACTS):
            return
        state = presence.of(contact).now()
        if presence.answers(contact, state, again=2):
            log.info("%s didn't respond to %s chasing them", contact.id, by.id)
            if random.random() < 0.65 and by.id in self._members():
                await self._react(self._chase_came_back(contact, state, by), who=by.id)
            return
        log.info("%s joined after %s chased them", contact.id, by.id)
        await self.add(contact.id, ensure=True, willing=True,
                       note=f"{by.name} texted you to get on this call — you've only just seen it.")

    @staticmethod
    def _chase_came_back(contact, state, by=None):
        """
        What the one who chased them has heard back — from where they really
        are, since that's what their reply would say. The private say nothing,
        and nobody tells someone outside the secret that they're on patrol.
        """
        doing = state.get("doing") or ""
        if (not contact.shares_status or not doing or state["status"] == presence.OFFLINE
                or (by is not None and groupchat.outside(by.id)) or random.random() < 0.3):
            return (f"You texted {contact.name} to get on the call a minute ago; nothing back yet. "
                    "Tell the call, if you'd bother.")
        return (f"{contact.name} has just texted you back: they can't get on right now — they're "
                f"{doing}. Pass it on the way you would.")

    async def _call_adds(self):
        """Someone on the call rang someone else in, because he asked or they're needed."""
        if not self.call:
            return
        for member in list(self.call.members):
            for name in getattr(member, "_call_add", [])[:1]:
                member._call_add = []
                wanted = self.directory.find(name)
                if wanted and wanted.id not in self._members() and len(self.call.members) < MAX_CONTACTS:
                    log.info("%s is ringing %s into the call", member.contact.id, wanted.id)
                    await self.add(wanted.id)

    async def _hang_ups(self, reason="has just hung up and left the call"):
        """
        Anyone on a group call who said goodbye and [hang up] drops off; the call
        goes on without them. The last one out ends it.
        """
        if not self.call or not self.call.is_group:
            return
        for member in list(self.call.members):
            if getattr(member, "_hanging_up", False) and len(self.call.members) > 1:
                member._hanging_up = False
                self.call.leave(member)
                self._wrap_up(member, {member.contact.id} | set(self._members()))
                if self.current_id == member.contact.id and self.call.members:
                    self.current_id = self.call.members[0].contact.id
                await self.broadcast(events.party(self._members(), removed=member.contact.id))
                # Whoever was in the room with them saw why they went — and may be going too.
                beside = [m for m in self.call.members if member.contact.name in self.call.in_the_room(m)]
                if beside:
                    await self._react(f"{member.contact.name}, who's right there with you, {reason}. You saw why; "
                                      "if it's yours to deal with too, you'd be going as well.", who=beside[0].contact.id)
                elif random.random() < 0.6:
                    await self._react(f"{member.contact.name} {reason}.")

    async def call_many(self, ids):
        """
        Ring several at once. Each picks up — or doesn't — in their own time;
        the first to answer opens the line, knowing who else is being rung, and
        the rest join as they pick up. Nobody answers: nobody answers.
        """
        contacts = [c for c in (self.directory.get(i) for i in dict.fromkeys(ids)) if c][:MAX_CONTACTS]
        if not contacts:
            return
        if len(contacts) == 1:
            return await self.connect(contacts[0].id)
        if self.current_id or self.call:
            # Ringing a group from a call is leaving that call: it ends here,
            # whether or not anyone picks up — not left running under the ring.
            await self.disconnect()
        ring = self._group_ring = object()
        decisions = {c.id: presence.answers(c, presence.of(c).now()) for c in contacts}
        # Those who are together share the moment: busy for one is busy for both,
        # and one picking up brings the other on — "Tim's here, you're on speaker".
        for c in contacts:
            _, group = presence.together(presence.of(c))
            beside = [o for o in contacts if o is not c and o.id in {p.contact.id for p in group}]
            if not beside:
                continue
            if decisions[c.id] == "declined" and random.random() < 0.7:
                for o in beside:
                    decisions[o.id] = decisions[o.id] or "declined"
            elif decisions[c.id] is None:
                for o in beside:
                    if decisions[o.id] == "no_answer" and random.random() < 0.8:
                        decisions[o.id] = None
        await self.broadcast({"type": "group_ringing", "members": [c.id for c in contacts]})
        answering = [c for c in contacts if decisions[c.id] is None]
        for c in contacts:
            if decisions[c.id]:
                self._spawn(self._refuse_group_ring(c, decisions[c.id], ring))
        if not answering:
            return
        first, rest = answering[0], answering[1:]
        others = [c.name for c in contacts if c is not first]
        self._inviting = {c.id for c in contacts}
        await self.connect(first.id, ensure=True, also_ringing=others)
        for contact in rest:
            if self.call and self.current_id:
                together = [c.name for c in contacts if c is not contact]
                # Rung together, so nobody remarks on the ringing — they're all being rung.
                await self.add(contact.id, ensure=True, remark=False,
                               note=f"he rang you along with {' and '.join(together)}, all at once")

    async def _refuse_group_ring(self, contact, how, ring):
        await asyncio.sleep(random.uniform(*(DECLINE_AFTER if how == "declined" else NO_ANSWER_AFTER)))
        self._refused_at[contact.id] = time.time()
        if self._group_ring is not ring:
            return          # that ring's long over: he hung up, or rang again
        await self.broadcast({"type": "call_refused", "speaker": contact.id, "how": how, "group": True})
        self._spawn(self._after_refusal(contact, how, presence.of(contact).now()))
        if self.call and self.call.members:
            missed = "didn't pick up" if how == "no_answer" else "declined"
            await self._react(f"{contact.name} {missed} — he'd rung them too.", absent=contact)

    async def text(self, contact_id, body, reply_to=None):
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
        thread = TextLog(contact_id)
        message = thread.add("me", body, reply_to=reply_to)
        await self.broadcast({"type": "text_sent", "speaker": contact_id, "message": message})

        if (contact_id in self._members() or contact_id == self.current_id) and self._ringing_out != contact_id:
            read_at = thread.mark_read([message["id"]])
            await self.broadcast({"type": "text_read", "speaker": contact_id,
                                  "ids": [message["id"]], "at": read_at})
            return await self._text_into_call(contact_id, body)

        self._pending_texts.setdefault(contact_id, []).append(message)
        burst = self._attempt(self._text_bursts, contact_id, BURST_WINDOW) + 1
        self.session_for(contact_id).pestered = burst
        whereabouts = presence.of(contact)
        if burst >= 3 and whereabouts.now()["status"] != presence.ONLINE:
            # A phone buzzing over and over gets looked at — more likely with
            # every message past the second, even through sleep.
            if random.random() < (contact.texting_pace or {}).get("wake", 0.2) * (burst - 2):
                log.info("%s picked up their phone after %d texts", contact_id, burst)
                whereabouts.touch()
                await self._presence_changed(contact)
        self._start_answering(contact)

    @staticmethod
    def _attempt(attempts, contact_id, window):
        """Record an attempt; return how many came before it inside the window."""
        now = time.time()
        recent = [t for t in attempts.get(contact_id, []) if now - t < window]
        attempts[contact_id] = recent + [now]
        return len(recent)

    def _start_answering(self, contact):
        if contact.id not in self._texters:
            self._texters[contact.id] = self._spawn(self._answer_texts(contact))

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
            thread = TextLog(contact.id)
            ids = [m["id"] for m in batch]
            body = "\n".join(m["text"] for m in batch)
            # Dry spells leave more on read and more unanswered; a flowing hour, less.
            dry = max(0.35, 1.6 - 0.6 * presence.engagement(contact))
            ghost = min(0.95, pace.get("ghost", 0) * dry)
            if _URGENT.search(body):
                ghost *= 0.1        # even Randy answers "are you okay"
            if random.random() < ghost:
                # Ghosted. Some open it and say nothing; some never open it at all.
                if random.random() < pace.get("ghost_unread", 0.3):
                    thread.ignore(ids)
                else:
                    read_at = thread.mark_read(ids)
                    await self.broadcast({"type": "text_read", "speaker": contact.id,
                                          "ids": ids, "at": read_at})
                log.info("%s ghosted %d text(s)", contact.id, len(batch))
                return
            read_at = thread.mark_read(ids)
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
            if not glancing and random.random() < min(0.95, pace.get("on_read", 0) * dry):
                # Left on read, for a while. Some people do — and having done it,
                # they aren't offered it again when they get round to answering.
                self.session_for(contact.id).left_on_read = True
                await asyncio.sleep(random.uniform(*pace.get("on_read_for", [180, 1200])))
            else:
                await asyncio.sleep(random.uniform(0.8, 3.0))
            # Written first, typed after: the dots only appear once there's
            # something to type. Shown while the model worked, they sat there
            # for minutes whenever it was busy with something else.
            turn = await self._write_text(contact, "\n".join(_as_read(m) for m in batch))
            reply, session = turn["text"], self.session_for(contact.id)
            if turn["choice"] == "none":
                log.info("%s chose not to answer", contact.id)
                return
            if turn["choice"] == "later" and turn["deferred"]:
                # Their choice: they'll answer, just not now. If he texts again
                # meanwhile, this is dropped and they answer everything at once.
                waiting = len(self._pending_texts.get(contact.id, []))
                await asyncio.sleep(random.uniform(*pace.get("on_read_for", [480, 2700])))
                if len(self._pending_texts.get(contact.id, [])) > waiting:
                    # He texted again meanwhile: what they held back goes in with
                    # the new ones, answered together — not dropped.
                    self._pending_texts[contact.id] = batch + self._pending_texts.get(contact.id, [])
                    return
                said, held = turn["deferred"]
                session.history.record_exchange(said, held, via="text")
                log.info("%s answered later", contact.id)
            group_task, meant = turn["group_task"], turn["meant"]
            if meant is not None and not group_task and reply:
                if groupchat.speaks_to_group(reply, meant, self.directory, contact.id):
                    # Written to the group, in the wrong thread: it goes where it
                    # was meant, and their DM with him keeps only what was his.
                    log.info("%s wrote the group's message in the DM; moved", contact.id)
                    self._spawn(self._post_as(meant, contact, given=reply))
                    session.history.amend_last_reply(f"(Posted in “{meant.name}”: {reply})")
                    reply = ""
                else:
                    group_task = (meant.name, "\n".join(m["text"] for m in batch))
            if group_task and reply.strip(" .").lower() in ("", "mm"):
                # Nothing to say to him: they've gone to say it in the group.
                session.history.amend_last_reply(f"(Went to say it in “{group_task[0]}”.)")
                reply = ""
            if turn["take"]:
                self._spawn(self._take_by_name(contact, turn["take"]))
            asked = ASKS_REACTION.search(batch[-1]["text"] or "")
            if asked and not turn["react"]:
                # Asked to react, they sent the emoji as a text: it's the reaction he asked for.
                lone = [ln for ln in reply.splitlines() if ln.strip() in _TAPBACKS]
                if lone:
                    turn["react"] = lone[0].strip()
                    reply = "\n".join(ln for ln in reply.splitlines() if ln.strip() not in _TAPBACKS).strip()
            if turn["react"]:
                if reply.strip(" .").lower() in ("", "mm"):
                    reply = ""          # the reaction was the reply
                # "Thumbs up my message" means the one before it, if there is one.
                target = batch[-1]["id"]
                if asked:
                    earlier = [m for m in thread.page(limit=12) if m["from"] == "me" and m["id"] != batch[-1]["id"]
                               and not m.get("kind")]
                    target = earlier[-1]["id"] if earlier else target
                await self._tapback_text(contact, target, "them", turn["react"])
            if reply:
                await self._deliver(contact, reply, loop.time(), reply_to=_quoting(thread, batch, turn["quote"]))
                whereabouts.drop("callback")     # back in touch; no need to ring him back
            else:
                await self.broadcast({"type": "text_idle", "speaker": contact.id})
            if group_task:
                self._spawn(self.carry_out_group_task(contact, *group_task))
            if reply or turn["react"] or group_task:
                if not glancing:
                    whereabouts.touch()
                    if random.random() < min(0.9, (contact.texting_pace or {}).get("drift", 0)
                                             * max(0.4, 1.5 - 0.5 * presence.engagement(contact))):
                        # And then they put the phone down, mid-conversation,
                        # as people do: the next text waits.
                        whereabouts.touch(time.time() - presence.ENGAGED_FOR)
                # What the exchange set in motion — an errand, a promise, worry, a
                # case closed — read after every one, not only the group requests.
                self._spawn(self._afterthought(session, session.history.messages[-6:], "text"))
                if (reply and not glancing and random.random()
                        < (contact.texting_style or {}).get("second_thought", 0)):
                    self._spawn(self._second_thought(contact))
            self._release_later(contact)
        finally:
            self._texters.pop(contact.id, None)
            try:
                again = bool(self._pending_texts.get(contact.id)) and not asyncio.current_task().cancelling()
            except RuntimeError:
                again = False       # the loop is gone: shutting down
            if again:
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

    async def _deliver(self, contact, reply, started, origin=None, reply_to=None):
        """
        A reply sent as they'd send it: one composed message, or three in a
        row, each typed at their own speed (less the time spent thinking) —
        the first quoting `reply_to`, when it answers one text in particular.
        """
        loop = asyncio.get_running_loop()
        per_second = max(1.0, (contact.texting_pace or {}).get("wpm", 50) * 5 / 60)
        thread = TextLog(contact.id)
        recent = [m["text"] for m in thread.page(limit=8) if m["from"] == "them"][-3:]
        parts = initiative.bubbles(contact, initiative.untic(contact, reply, recent))
        for i, part in enumerate(parts):
            await self.broadcast({"type": "text_typing", "speaker": contact.id})
            await self._type_out(contact.id, part, per_second,
                                 already=(loop.time() - started) if i == 0 else 0)
            sent = thread.add("them", part, origin=origin, reply_to=reply_to if i == 0 else None)
            await self.broadcast({"type": "text_reply", "speaker": contact.id, "message": sent})
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.4, 1.4))

    async def _type_out(self, contact_id, text, per_second, already=0, group=None, cap=20):
        """
        Typing that looks like a person: roughly as long as the message takes at
        their speed — never exactly, nobody types at a steady rate — and on a
        longer one the dots stop and start as they pause mid-thought.
        """
        speed = per_second * random.lognormvariate(0, 0.22)
        total = min(len(text) / max(0.5, speed), cap) - already
        if total <= 0:
            return
        typing = {"type": "group_typing", "group": group, "speaker": contact_id} if group else \
            {"type": "text_typing", "speaker": contact_id}
        idle = {"type": "group_idle", "group": group, "speaker": contact_id} if group else \
            {"type": "text_idle", "speaker": contact_id}
        if len(text) > 60 and total > 4 and random.random() < 0.6:
            first = total * random.uniform(0.3, 0.6)
            await asyncio.sleep(first)
            await self.broadcast(idle)
            await asyncio.sleep(random.uniform(1.2, 4.0))
            await self.broadcast(typing)
            await asyncio.sleep(total - first)
        else:
            await asyncio.sleep(total)

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
            self._release = self._spawn(self._release_after(contact))

    async def _tapback_text(self, contact, message_id, who, emoji):
        """A reaction on a message in a DM — theirs on his, or his on theirs."""
        if who == "them":
            await asyncio.sleep(random.uniform(0.6, 2.5))
        message = TextLog(contact.id).react(message_id, who, (emoji or "")[:16] or None)
        if message is None:
            return
        await self.broadcast({"type": "text_reaction", "speaker": contact.id, "message": message})
        if who == "me" and emoji:
            # They'll see it next time they look at the thread.
            session = self.session_for(contact.id)
            session.tapbacks_seen = (getattr(session, "tapbacks_seen", []) +
                                     [f"he reacted {emoji} to your text “{message['text'][:60]}”"])[-3:]
            if random.random() < initiative.tapback_odds(contact, emoji):
                self._spawn(self._answer_tapback(contact, message, emoji))

    async def _answer_tapback(self, contact, message, emoji):
        """
        His reaction on their text: seen when they next look — and now and
        then it gets something back, a word or "what's the ❓ for", in their
        own way. Not if he's said something since: that's what they answer.
        """
        delay = presence.read_delay(contact, presence.of(contact).now())
        if delay is None:
            return
        await asyncio.sleep(delay + random.uniform(3, 25))
        thread = TextLog(contact.id)
        last = thread.last()
        mine = next((m for m in thread.page(limit=12) if m["id"] == message["id"]), None)
        if (mine is None or (mine.get("reactions") or {}).get("me") != emoji or last is None
                or last["id"] != message["id"] or self._pending_texts.get(contact.id)):
            return
        session = self.session_for(contact.id)
        session.tapbacks_seen = [t for t in getattr(session, "tapbacks_seen", []) if emoji not in t]
        await self._send_unprompted(contact, f"{emoji} to your text “{message['text'][:80]}”", "tapback")

    async def text_react(self, contact_id, message_id, emoji):
        contact = self.directory.get(contact_id)
        if contact:
            await self._tapback_text(contact, message_id, "me", emoji)

    async def _write_text(self, contact, body):
        """
        The contact writes a text reply, held to the same lock as call turns.
        Returns the reply and everything the turn decided — answer later, act
        in a group, take a case, react — read while the lock is still held:
        read afterwards, a call turn in between had already reset them, and
        a reply held "for later" came back as nothing at all.
        """
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
            result.update(choice=getattr(session, "_reply_choice", None),
                          deferred=getattr(session, "_deferred", None),
                          group_task=getattr(session, "_group_task", None),
                          meant=getattr(session, "_meant_group", None),
                          take=getattr(session, "_take_case", None),
                          react=getattr(session, "_text_react", None),
                          quote=getattr(session, "_text_quote", None))
            session._deferred = session._take_case = session._text_react = session._text_quote = None
        return result

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
        self._resume_group_reads()
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
            if not contact.shares_status and places.is_patrol(presence.of(contact).now(now)["doing"]) \
                    and random.random() < 0.6 * PULSE_SECONDS / 3600:
                # Out on the rooftops, someone who keeps their whereabouts to
                # themselves gets seen now and then: a sighting on the scanner.
                where, _ = presence.of(contact).whereabouts(now)
                presence.of(contact).seen_at(where, "a sighting on the scanner", now)
        await self._write_status_lines()
        await self._keep_up()
        await self._write_day_plans()
        await self._write_dispatches()
        if not self.clients:
            return      # nobody at the console to hear about it
        for contact in self.directory:
            for intent in presence.of(contact).due(now):
                # Spawned, not awaited: one contact typing out a long reply
                # stalled every dot and every other promise behind it.
                presence.of(contact).postpone(intent["id"], 120, count=False)  # claimed
                self._spawn(self._carry_out(contact, intent))
        await self._maybe_reach_out(now)
        await self._group_tick(now)
        await self._case_tick(now)

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

    async def _write_dispatches(self):
        """The scanner's calls get their dispatch text, two at a time, while the model's idle."""
        from ..engine import incidents
        if (self._writing_lines or not self.clients or self.current_id or self._texters
                or self.turn_lock.locked()):
            return
        waiting = incidents.unwritten()[:2]
        model = next(iter(self.directory)).model if waiting else None
        if not waiting or not await asyncio.to_thread(_model_loaded, model):
            return
        self._writing_lines = True
        try:
            options = next(iter(self.directory)).options
            for report in waiting:
                await asyncio.to_thread(incidents.write_dispatch, report, model, options)
            await self.broadcast({"type": "scanner"})
        finally:
            self._writing_lines = False

    # --- cases ------------------------------------------------------------------

    async def _take_by_name(self, contact, named):
        """'[take: Diamond District]' — the open report they meant, by place or kind."""
        import difflib

        from ..engine import cases, incidents
        taken = {c["id"] for c in cases.board() if c["status"] != "closed"}
        open_ = [r for r in incidents.at() if r["id"] not in taken and r["status"] != "resolved"]
        if not open_:
            return
        said = named.lower().strip(" .")
        if said in ("", "the place", "it", "that", "this", "one"):
            return              # the instruction's own placeholder, or nothing to go on
        # By what it names — the place, the district, the kind of call — first;
        # only then a close match. At a cutoff of 0.1, "the docks" took a
        # mugging at the cathedral.
        report = next((r for r in open_ if r["place"].lower() in said or said in r["place"].lower()), None) \
            or next((r for r in open_ if r["area"].lower() in said or said in r["area"].lower()), None) \
            or next((r for r in open_ if r["kind"].lower() in said), None)
        if report is None:
            labels = {f"{r['place']} {r['area']} {r['kind']}".lower(): r for r in open_}
            hit = difflib.get_close_matches(said, list(labels), 1, 0.6)
            report = labels[hit[0]] if hit else None
        if report:
            await self.assign_case(report["id"], contact.id, by="him", tell=False)

    async def assign_case(self, report_id, contact_id, by="him", tell=True):
        """
        Put someone on a report: they head there — the further away, the longer
        it takes — it's what they're doing, and if he did it from the map they
        text him back about it. From the map, asleep, they get it as a text
        from him to read when they wake; awake, they take it as readily as
        they take orders (`takes_orders`: Dick nearly always, Randy rarely).
        Returns the case, or {"texted": True} / {"declined": True}.
        """
        from ..engine import cases, incidents
        contact = self.directory.get(contact_id)
        report = incidents.get(report_id) or cases.for_report(report_id)
        if contact is None or report is None or contact_id not in cases.FIELD:
            return None
        about = f"{report['kind'].lower()} at {report['place']}" + (
            f" — dispatch said: {report['dispatch']}" if report.get("dispatch") else "")
        if by == "him" and tell and contact_id not in self._members():
            if presence.of(contact).now()["status"] == presence.OFFLINE:
                await self.text(contact_id, f"{report['kind']} at {report['place']}. Can you take it?")
                return {"texted": True}
            if random.random() > (contact.initiative or {}).get("takes_orders", 0.95):
                self._spawn(self._later(random.uniform(8, 40), self._send_unprompted(contact, about, "case_declined")))
                return {"declined": True}
        current = cases.active(contact_id)
        if current and current["id"] != report_id:
            cases.drop(current["id"])          # off the last one, on to this
        # From wherever they are, by the roads, as long as the roads take — and the
        # map shows them on their way there, arriving when the case does.
        whereabouts = presence.of(contact)
        here = places.resolve(whereabouts.whereabouts()[0])
        minutes = (travel.route((here["x"], here["y"]), (report["x"], report["y"]), here["name"], report["place"])[1]
                   if here else 12.0)
        case = cases.assign(report, contact_id, by=by, travel=minutes)
        whereabouts.set_activity(f"on the way to the {report['kind'].lower()} at {report['place']}",
                                 presence.BUSY, max(90, int(minutes) + 60), where=report["place"])
        if not contact.shares_status:
            whereabouts.seen_at(report["place"], "on a case")
        log.info("%s on case %s (%s at %s, %s)", contact_id, report_id, report["kind"], report["place"], by)
        await self._presence_changed(contact)
        await self.broadcast({"type": "cases"})
        if tell and contact_id not in self._members():
            why = "case_assigned" if by == "him" else "case_taken"
            chatty = min(1.0, (contact.initiative or {}).get("per_day", 0.4))
            if by == "him":
                self._spawn(self._later(random.uniform(8, 40), self._send_unprompted(contact, about, why)))
            elif self._may_reach_out(time.time()) and random.random() < 0.4 * chatty:
                self._count_initiative()
                self._spawn(self._later(random.uniform(8, 40), self._send_unprompted(contact, about, why)))
        return case

    async def _case_tick(self, now):
        """Patrols take what's on their beat; cases reach the scene, and run their course."""
        from ..engine import cases, incidents, places
        busy = {c["assignee"] for c in cases.board() if c["status"] != "closed"}
        taken = {c["id"] for c in cases.everything()}
        open_ = [r for r in incidents.at(now) if r["id"] not in taken and r["severity"] >= 2
                 and r["status"] in ("reported", "units responding")]
        for cid in cases.FIELD:
            contact = self.directory.get(cid)
            if contact is None or cid in busy or cid in self._members():
                continue
            whereabouts = presence.of(contact)
            if not places.is_patrol(whereabouts.now()["doing"]) or now < (whereabouts.get("case_rest") or 0):
                continue
            areas = {(places.resolve(b) or {}).get("area") for b in contact.beat}
            mine = [r for r in open_ if r["area"] in areas]
            # Now and then, not every half-minute: a patrol takes a call off the
            # scanner a few times a night, with a breather after each.
            if mine and random.random() < 0.01:
                whereabouts.put("case_rest", now + random.uniform(45, 120) * 60)
                await self.assign_case(mine[0]["id"], cid, by="self")
                busy.add(cid)
        for case in cases.advance(now, arrived=self._on_scene):
            if self.current_id:
                break           # he's on a call: the write-up waits, rather than taking the model from it
            if case["id"] not in self._closing:
                self._closing.add(case["id"])
                self._spawn(self._close_case(case))

    def _on_scene(self, case):
        """They've got there: now they're working it, where it is."""
        contact = self.directory.get(case["assignee"])
        if contact is not None:
            presence.of(contact).set_activity(f"working the {case['kind'].lower()} at {case['place']}",
                                              presence.BUSY, 90, where=case["place"])
            self._spawn(self._presence_changed(contact))

    async def _close_case(self, case):
        """
        It's run its course: they write how it ended, and may tell him — as
        one of the day's unprompted texts, not on top of them, and not in his
        quiet hours or about a case that ended hours ago while the console was shut.
        """
        from ..engine import cases
        try:
            contact = self.directory.get(case["assignee"])
            if contact is None:
                return cases.close(case["id"], "handled")
            session = self.session_for(contact.id)
            async with self.turn_lock:
                outcome = await asyncio.get_running_loop().run_in_executor(None, session.case_outcome, case)
            closed = cases.close(case["id"], outcome or "handled")
            if not closed:
                return
            whereabouts = presence.of(contact)
            whereabouts.clear_activity()
            whereabouts.put("case_rest", time.time() + random.uniform(45, 120) * 60)
            await self._presence_changed(contact)
            await self.broadcast({"type": "cases"})
            log.info("%s closed case %s: %s", contact.id, case["id"], outcome)
            now, chatty = time.time(), min(1.0, (contact.initiative or {}).get("per_day", 0.4))
            if (contact.id not in self._members() and now - case.get("due", now) < 2 * 3600
                    and self._may_reach_out(now) and random.random() < (0.3 + 0.12 * case["severity"]) * chatty):
                self._count_initiative()
                await self._send_unprompted(contact, f"{case['kind'].lower()} at {case['place']} — {outcome}",
                                            "case_closed")
        finally:
            self._closing.discard(case["id"])

    async def _keep_up(self):
        """
        One contact catches up on what they follow — searched as of today, read
        by the model — while it's loaded and nothing else is happening. A day
        later they catch up again, so they're never months behind.
        """
        if (self._writing_lines or not self.clients or self.current_id or self._texters
                or self.turn_lock.locked()):
            return
        behind = [c for c in self.directory if culture.stale(c)]
        if not behind or not await asyncio.to_thread(_model_loaded, behind[0].model):
            return
        self._writing_lines = True
        try:
            contact = random.choice(behind)
            items = await asyncio.to_thread(culture.refresh, contact)
            if items:
                log.info("%s caught up on what they follow: %d things", contact.id, len(items))
            else:
                # Nothing came back (offline, rate-limited): try someone else
                # next time rather than hammering the same searches.
                culture.mark_tried(contact)
        finally:
            self._writing_lines = False

    async def _write_day_plans(self):
        """
        Each contact sketches their own day, once, while the model is already
        loaded and nothing else is happening — one contact a tick, so it never
        holds anything up.
        """
        if (self._writing_lines or not self.clients or self.current_id or self._texters
                or self.turn_lock.locked()):
            return
        waiting = [c for c in self.directory if not presence.of(c).has_plan()]
        if not waiting or not await asyncio.to_thread(_model_loaded, waiting[0].model):
            return
        self._writing_lines = True
        try:
            contact = waiting[0]
            plan = await asyncio.to_thread(initiative.day_plan, contact, None,
                                           self._plans_so_far(contact), list(self.directory))
            if plan:
                presence.of(contact).set_plan(plan)
                log.info("%s planned the day: %d blocks", contact.id, len(plan))
                await self._presence_changed(contact)
        finally:
            self._writing_lines = False

    def _plans_so_far(self, contact):
        """
        What the others who share his secret have planned today, for someone
        sketching theirs — so two who'd patrol together can, and say so both
        sides. Nobody outside the secret sees anyone's nights, or shows theirs.
        """
        from .. import operator
        known = set((operator.profile().get("secrets") or {}).get("known_by") or [])
        if contact.id not in known:
            return ""
        lines = []
        for other in self.directory:
            if other.id == contact.id or other.id not in known:
                continue
            for block in presence.of(other).plan_today():
                if block.get("where"):
                    lines.append(f"{other.name}: {block['doing']} — {block['where']}, "
                                 f"{_clock(block['from'])}–{_clock(block['to'])}")
        return "\n".join(lines[:14])

    async def _presence_changed(self, contact):
        shown = presence.of(contact).public()
        # Where they are counts: a patrol moving along its beat is news to the map.
        key = (shown["status"], shown["doing"], shown.get("line", ""), shown.get("where"))
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
        why = "callback" if callback else "worry" if intent.get("origin") == "worry" else "promise"
        await self._send_unprompted(contact, intent["about"], why)

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
        if not self._may_reach_out(now):
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
                # He's waiting on them, not the other way round — until enough time
                # has gone that they'd just text about something else as if
                # nothing happened (Dick in hours, Jason a day on, Randy never).
                after = leaning.get("resurface_after", 8)
                if after is None or now - last["at"] < after * 3600:
                    continue
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
                # They spoke last: it's his turn — for a while. How long a
                # while is theirs: Dick's onto the next thing in hours, Randy
                # never texts into a silence he didn't break.
                if now - last["at"] < 12 * 3600 * (1 - leaning.get("double_text", 0.3)) + 3600:
                    continue
            # Something on their mind: a few times a day for the chatty ones.
            if random.random() < leaning.get("per_day", 0.4) * PULSE_SECONDS / (16 * 3600):
                impulses.append(contact)
        if chases:
            contact = random.choice(chases)
            presence.of(contact).put("chase", {"id": (presence.of(contact).get("chase") or {}).get("id")})
            self._count_initiative()
            last = TextLog(contact.id).last() or {}
            # Their phone shows whether he's opened it, as anyone's does.
            seen = f"read {describe_gap(now - last['seen_at'])}" if last.get("seen_at") else "not opened"
            self._spawn(self._send_unprompted(contact, seen, "chase"))
        elif impulses:
            contact = random.choice(impulses)
            about = initiative.impulse(self.session_for(contact.id))
            last = TextLog(contact.id).last()
            if about and last and last["from"] == "them":
                about += (f" (he never answered your last text — {'he read it' if last.get('seen_at') else 'never even opened it'}; "
                          "mention that, or don't, whatever you'd actually do)")
            if about:
                self._count_initiative()
                self._spawn(self._send_unprompted(contact, about, "impulse"))

    def _may_reach_out(self, now):
        """
        Room in the day for one more unprompted text: under the daily budget,
        not within INITIATIVE_GAP of the last, not in his quiet hours — the
        one rule for every text nobody asked for, whatever prompted it.
        """
        if config.INITIATIVE_PER_DAY <= 0 or self._quiet(now) or not self.clients:
            return False
        sent = [t for t in self._initiative_log() if now - t < 86400]
        return len(sent) < config.INITIATIVE_PER_DAY and not (sent and now - max(sent) < INITIATIVE_GAP)

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
        await self._deliver(contact, text, loop.time(), origin=why)
        self._release_later(contact)

    # --- calls to him -------------------------------------------------------

    async def _ring(self, contact, about):
        """They call him. The page rings; he answers, declines, or misses it."""
        self._incoming = {"id": contact.id, "about": about}
        log.info("%s ringing him", contact.id)
        await self.broadcast({"type": "call_incoming", "speaker": contact.id})
        self._ring_timer = self._spawn(self._ring_out(contact.id))

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
        if self.call and self.call.is_group:
            if kind == "sign_off":
                # The quiet's run its course: the page rings off on this.
                return await self.broadcast(events.turn_complete())
            return await self._lull()
        # The epoch is taken before waiting, as submit does: bumping it after
        # the wait cancelled whatever he'd said in the meantime, and his words
        # were replaced by a check-in.
        epoch = self.interrupt()
        async with self.turn_lock:
            if epoch != self.turn_epoch or not self.current_id:
                return
            await self.drive(getattr(self.session_for(self.current_id), kind)(), self.contact, epoch)

    async def _lull(self):
        """
        A pause on a group call (the page says when: it knows when the last
        voice stopped). Someone may fill it — the chatty likelier, not whoever
        just spoke — at odds that fall with each line said into the quiet, so a
        call he's gone silent on drifts off rather than carrying on without him.
        """
        self._lulls += 1
        if random.random() >= self._lull_energy:
            # Nobody fills it — and the page, waiting to hear, is told so, or its
            # timer stopped and the call never drifted to an end.
            return await self.broadcast(events.turn_complete())
        members = list(self.call.members)
        weights = [(0.25 + (m.contact.initiative or {}).get("per_day", 0.5) * 0.4)
                   * (1 - (m.contact.texting_pace or {}).get("on_read", 0) * 0.7)
                   * (0.4 if m is self.call.last_speaker else 1.0)
                   # Whoever filled the last pause leaves the next to someone else, mostly.
                   * (0.25 if m.contact.id == self._last_lull else 1.0) for m in members]
        member = random.choices(members, weights=weights)[0]
        # Now and then two of them go for the same pause at once.
        rest = [(m, w) for m, w in zip(members, weights, strict=True) if m is not member]
        second = (random.choices([m for m, _ in rest], weights=[w for _, w in rest])[0]
                  if rest and random.random() < TALK_OVER else None)
        epoch = self.turn_epoch
        async with self.turn_lock:
            if epoch != self.turn_epoch or not self.call or member not in self.call.members:
                return await self.broadcast(events.turn_complete())
            turn = (self.call.collide(member, second, self._lulls)
                    if second is not None and second in self.call.members
                    else self.call.lull(member, self._lulls))
            line = await self._drive_for(turn, member.contact, epoch)
        if line:
            self._last_lull = member.contact.id
        self._lull_energy *= 0.55 if line else 0.8
        await self._hang_ups()
        await self._call_adds()

    async def _drive_for(self, generator, contact, epoch):
        """Drive a generator and hand back what it returned."""
        result = {}

        def keep():
            result["value"] = yield from generator
        await self.drive(keep(), contact, epoch)
        return result.get("value")

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
        wanted = self._asked_to_add(text) if self.call else None
        if self.call:
            leaving = self._asked_to_drop(text)
            if leaving:
                return await self.drop(leaving)

        # Supersede first, then queue: the running turn sees a stale epoch,
        # stops, and releases the lock instead of making the new input wait for
        # a reply nobody is listening to any more.
        was_speaking = self.voice_busy
        self._lull_energy, self._lulls = 1.0, 0
        epoch = self.interrupt()
        async with self.turn_lock:
            if epoch != self.turn_epoch:
                return             # something newer arrived while we waited
            contact = self.contact
            group = bool(self.call and self.call.is_group)
            if group:
                turn = self.call.turn(text, interrupted=was_speaking, confidence=confidence)
            else:
                session = self.session_for(self.current_id)
                turn = session.ask(text, interrupted=was_speaking, confidence=confidence)
            await self.drive(turn, contact, epoch)
        if group:
            await self._hang_ups()
        await self._call_adds()
        if wanted and self.call and wanted not in self._members():
            # He asked for them: whoever he asked has answered him; now it rings.
            await self.add(wanted)
        for member in (self.call.members if self.call else []):
            taking = getattr(member, "_take_case", None)
            if taking:
                member._take_case = None
                self._spawn(self._take_by_name(member.contact, taking))

    def _asked_to_add(self, text):
        """
        'Alfred, get Lucius on the line' — a contact not on the call, asked for
        plainly: ringing someone in, by name, onto the line. "Did Dick call you?"
        and "get me what Barbara found" aren't, and nor is "don't add Tim".
        """
        if re.search(r"(?i)\b(don'?t|do not|no need to|never mind)\b", text):
            return None
        if not re.search(r"(?i)\b(get|bring|add|patch|loop|ring|put|pull|conference)\b", text):
            return None
        if not re.search(r"(?i)\b(on|onto|into|to|in on) (the |this )?(line|call)\b|"
                         r"\b(patch|loop|bring|pull|get|ring)\b[^.?!]{0,30}\bin\b", text):
            return None
        if text.rstrip().endswith("?") and not re.match(r"(?i)\W*(\w+,\s*)?(can|could|would|will) you\b", text):
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
        if re.search(r"(?i)\b(don'?t|do not|no need to|not yet)\b", text) or text.rstrip().endswith("?"):
            return None         # "Tim, don't hang up yet" keeps Tim
        if not re.search(r"\b(drop (off|out)|you can go|hang up|leave the call|"
                         r"let (her|him) go|sign off|head off)\b", text, re.I):
            return None
        named = self.call.addressed(text)
        return named[0].contact.id if len(named) == 1 else None


console = Console()


# The reactions a phone offers at a tap, sent alone as a text when asked to react.
_TAPBACKS = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "❤️", "😂", "‼️", "❓", "👎"}


def _quoting(thread, batch, said):
    """
    Which of his texts their reply quotes — the one of `batch` the words in
    [reply: …] are from. None if none is, or it's his last text anyway.
    """
    target = quoted_by(batch, said) if said else None
    last = thread.last()
    return target["id"] if target and last and last["id"] != target["id"] else None


def _as_read(message):
    """One of his texts as they read it — with the one it answers, when he replied to a particular one."""
    quoted = message.get("reply_to")
    if not quoted:
        return message["text"]
    whose = "your" if quoted["from"] == "them" else "his own earlier"
    return f'(replying to {whose} text: "{quoted["text"]}") {message["text"]}'


def _clock(hours):
    return f"{int(hours) % 24:02d}:{int(round(hours % 1 * 60)) % 60:02d}"


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
        # Gotham, for the little map on a status card.
        "map": {k: v for k, v in places.gazetteer().items() if k in ("places", "regions", "areas")},
    })


def _pins_path():
    return paths.DATA_DIR / "_pins.json"


def _load_pins():
    try:
        return json.loads(read_text(_pins_path()) or "[]")
    except ValueError:
        return []


@app.get("/api/map/pins")
async def map_pins():
    """His own pins on the map."""
    return JSONResponse({"pins": _load_pins()})


@app.post("/api/map/pins")
async def save_pin(request: Request):
    """Drop a pin, or rename or move one he dropped."""
    body = await request.json()
    pins = _load_pins()
    try:
        x, y = float(body.get("x")), float(body.get("y"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "where?"}, status_code=400)
    pin = {"id": str(body.get("id") or uuid.uuid4().hex[:10]), "label": str(body.get("label") or "Pin")[:40],
           "x": max(-20.0, min(120.0, x)), "y": max(-20.0, min(120.0, y)), "at": time.time()}
    pins = [p for p in pins if p["id"] != pin["id"]] + [pin]
    atomic_write(_pins_path(), json.dumps(pins[-200:]))
    return JSONResponse({"pin": pin})


@app.delete("/api/map/pins/{pin_id}")
async def delete_pin(pin_id: str):
    atomic_write(_pins_path(), json.dumps([p for p in _load_pins() if p["id"] != pin_id]))
    return JSONResponse({"ok": True})


@app.get("/api/map/incidents")
async def map_incidents():
    """What the scanner says is happening in the city right now — and who's on what."""
    from ..engine import cases, incidents
    by_id = {c["id"]: c for c in cases.board()}
    reports = []
    for r in incidents.at():
        case = by_id.get(r["id"])
        if case:
            r = {**r, "assignee": case["assignee"], "case": case["status"], "outcome": case.get("outcome", "")}
        reports.append(r)
    return JSONResponse({"incidents": reports})


@app.get("/api/cases")
async def case_board():
    from ..engine import cases
    return JSONResponse({"cases": cases.board()})


@app.post("/api/cases/assign")
async def assign_case(request: Request):
    """He puts someone on a report, from the map."""
    body = await request.json()
    result = await console.assign_case(str(body.get("report", "")), str(body.get("contact", "")), by="him")
    if not result:
        return JSONResponse({"error": "can't assign that"}, status_code=400)
    return JSONResponse(result if ("texted" in result or "declined" in result) else {"case": result})


@app.get("/api/map/trail/{contact_id}")
async def map_trail(contact_id: str, hours: float = 2.0):
    """Where someone has been lately, for the map — nothing for those who don't share."""
    contact = console.directory.get(contact_id)
    if contact is None or not contact.shares_location:
        return JSONResponse({"trail": []})
    hours = max(0.25, min(hours, 12))
    whereabouts = presence.of(contact)
    trail = await asyncio.to_thread(whereabouts.trail, hours)
    # And the roads they took between those places, as they took them.
    return JSONResponse({"trail": trail, "trips": whereabouts.trips(time.time() - hours * 3600)})


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
    return JSONResponse({"messages": page, "typing": ("text", contact_id) in console._typing_now})


@app.get("/api/groups")
async def list_groups():
    return JSONResponse({"groups": [console.group_payload(g) for g in group_store.all_groups()]})


@app.post("/api/groups")
async def create_group(request: Request):
    body = await request.json()
    group = await console.group_create(body.get("name", ""), body.get("members", []))
    if group is None:
        return JSONResponse({"error": "a group needs at least two contacts"}, status_code=400)
    return JSONResponse({"group": console.group_payload(group)})


@app.patch("/api/groups/{group_id}")
async def update_group(group_id: str, request: Request):
    body = await request.json()
    group = await console.group_update(group_id, body.get("name"), body.get("add") or [],
                                       body.get("remove") or [])
    if group is None:
        return Response(status_code=404)
    return JSONResponse({"group": console.group_payload(group)})


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str):
    await console.group_delete(group_id)
    return JSONResponse({"ok": True})


@app.get("/api/groups/{group_id}/messages")
async def group_messages(group_id: str, before: float = 0, limit: int = 40):
    group = group_store.of(group_id)
    if group is None:
        return Response(status_code=404)
    page = group.page(before or None, max(1, min(limit, 200)))
    meta = group.summary()
    typing = [cid for (gid, cid) in console._typing_now if gid == group_id]
    return JSONResponse({"messages": page, "reads": meta["reads"], "members": meta["members"],
                         "name": meta["name"], "typing": typing})


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
                console._spawn(console.submit(
                    payload.get("text", ""), spoken=bool(payload.get("spoken")),
                    confidence=confidence))
            elif kind == "connect":
                console._spawn(console.connect(payload.get("id", "")))
            elif kind == "disconnect":
                console._spawn(console.disconnect())
            elif kind == "nudge":
                console._spawn(console.nudge())
            elif kind == "signoff":
                console._spawn(console.nudge("sign_off"))
            elif kind == "text":
                console._spawn(console.text(payload.get("id", ""), payload.get("text", ""), payload.get("reply_to")))
            elif kind == "answer":
                console._spawn(console.answer(payload.get("id", "")))
            elif kind == "decline":
                console._spawn(console.decline(payload.get("id", "")))
            elif kind == "group_text":
                console._spawn(console.group_text(payload.get("id", ""), payload.get("text", ""), payload.get("reply_to")))
            elif kind == "text_react":
                console._spawn(console.text_react(payload.get("id", ""), payload.get("message", ""),
                                                       payload.get("emoji", "")))
            elif kind == "seen":
                TextLog(str(payload.get("id", ""))).mark_seen()
            elif kind == "group_seen":
                seen = group_store.of(str(payload.get("id", "")))
                if seen is not None:
                    seen.mark_read("me")
            elif kind == "group_react":
                console._spawn(console.group_react(payload.get("id", ""), payload.get("message", ""),
                                                        payload.get("emoji", "")))
            elif kind == "call_group":
                console._spawn(console.call_many(payload.get("ids") or []))
            elif kind == "add":
                console._spawn(console.add(payload.get("id", ""), by_him=True))
            elif kind == "drop":
                console._spawn(console.drop(payload.get("id", "")))
            elif kind == "resume":
                console._spawn(console.nudge("resume"))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("the console's socket failed")
    finally:
        console.clients.discard(websocket)


def serve():
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT,
                log_level="warning", access_log=False)
