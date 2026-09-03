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
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import config, events
from ..audio import stt
from ..audio.tts import get_voice_engine
from ..contacts import directory
from ..engine import ContactSession, guards
from ..memory import migrate_legacy
from ..paths import WEB_DIR

# Synthesized clips waiting to be fetched. Bounded — a long session would
# otherwise hold every reply's audio in memory for the whole run.
_MAX_CACHED_CLIPS = 64

# How long a spoken line stays eligible to be recognised as an echo of itself.
# Long enough to cover a reply playing out plus the transcription round trip,
# short enough that legitimately repeating a phrase later still gets through.
ECHO_WINDOW_SECONDS = 25


class Console:
    """
    The console as a whole: a directory of contacts, one live session per
    contact you have spoken to, and the fan-out to connected browser tabs.
    """

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
        self.boot_task = None
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

    async def drive(self, generator, contact):
        """
        Consume a blocking engine generator on a worker thread, forwarding its
        events to the socket and synthesizing each sentence as it appears.
        """
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        done = object()

        def pump():
            try:
                for event in generator:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # a worker crash must not kill the socket
                loop.call_soon_threadsafe(
                    queue.put_nowait, events.notice(f"Engine error: {exc}", "error")
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=pump, daemon=True).start()

        speech = asyncio.Queue()
        worker = asyncio.create_task(self._speech_worker(speech))

        try:
            while True:
                event = await queue.get()
                if event is done:
                    break
                if event.get("type") == "sentence" and self._can_speak(contact):
                    task = asyncio.create_task(
                        asyncio.to_thread(self.voice.synthesize, event["text"], contact.voice_id)
                    )
                    speech.put_nowait((event["index"], event["text"], task))
                await self.broadcast(event)
        finally:
            speech.put_nowait(None)
            await worker
            # Only now is the turn genuinely finished: the model has stopped
            # writing and every sentence has been synthesized and released.
            await self.broadcast(events.turn_complete())

    def _can_speak(self, contact):
        return self.voice.available and contact.has_voice

    async def _speech_worker(self, speech):
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
            index, text, task = item
            try:
                audio = await task
            except Exception:
                if not reported:
                    reported = True
                    await self.broadcast(events.notice(
                        "Voice link degraded — switching to text.", "warn"))
                continue
            if audio:
                self.recent_speech.append((time.monotonic(), text))
                await self.broadcast(events.speak(self._store_clip(audio), text, index))

    # --- turns ------------------------------------------------------------

    async def connect(self, contact_id):
        """Switch the console to a contact, booting them on first connection."""
        contact = self.directory.get(contact_id)
        if contact is None:
            return
        async with self.turn_lock:
            self.current_id = contact_id
            session = self.session_for(contact_id)
            await self.broadcast(events.contact_changed(contact_id))

            if not contact.availability.is_available():
                await self.broadcast(events.notice(
                    contact.availability.away_message, "warn"))
                return

            for problem in config.missing_requirements():
                await self.broadcast(events.notice(problem, "warn"))
            if self.migrated:
                await self.broadcast(events.notice(
                    f"Migrated existing {' and '.join(self.migrated)} into "
                    f"data/{config.DEFAULT_CONTACT}/.", "info"))
                self.migrated = []
            await self.drive(session.boot(), contact)

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
        async with self.turn_lock:
            self.current_id = None
            self.recent_speech.clear()

    async def submit(self, text, spoken=False):
        """
        Run one turn. `spoken` marks input that came from a microphone, which is
        the only kind that can be an acoustic echo — typed text never is.
        """
        text = (text or "").strip()
        if not text or not self.current_id:
            return
        if spoken and self._is_own_echo(text):
            return
        async with self.turn_lock:
            contact = self.contact
            session = self.session_for(self.current_id)
            await self.drive(session.ask(text), contact)


console = Console()


def _warm_model():
    """
    Nudge the default contact's model into memory while the operator is still
    reading the boot screen. Loading a 14B costs around 25 seconds, and paying
    that after they have already spoken is the difference between a console and
    a progress bar. Failures are silent: this is an optimisation, not a step.
    """
    contact = console.directory.get(config.DEFAULT_CONTACT)
    if contact is None:
        return
    try:
        import ollama
        ollama.chat(
            model=contact.model,
            messages=[{"role": "user", "content": "."}],
            options={"num_predict": 1},
            keep_alive=config.MODEL_KEEP_ALIVE,
        )
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    warm = asyncio.create_task(asyncio.to_thread(_warm_model))
    yield
    warm.cancel()


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
    }


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


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
        "operator": config.USER_NAME,
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


@app.get("/api/audio/{clip_id}")
async def audio(clip_id: str):
    clip = console.audio_clips.get(clip_id)
    if clip is None:
        return Response(status_code=404)
    return Response(content=clip, media_type="audio/mpeg")


@app.post("/api/transcribe")
async def transcribe(request: Request):
    """Raw little-endian float32 PCM at stt.SAMPLE_RATE, captured in the page."""
    body = await request.body()
    text = await asyncio.to_thread(stt.transcribe_pcm, body)
    return JSONResponse({"text": text})


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
                    payload.get("text", ""), spoken=bool(payload.get("spoken"))))
            elif kind == "connect":
                asyncio.create_task(console.connect(payload.get("id", "")))
            elif kind == "disconnect":
                asyncio.create_task(console.disconnect())
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
