"""
Local web console for Alfred.

FastAPI serves a static page and one WebSocket. The page owns presentation and
audio playback; this module owns the session and translates engine events onto
the socket. Deliberately no build step and no node toolchain — the console is
plain HTML/CSS/JS, so `launch.command` only ever has to start a Python process.

Audio is *not* played here. The bytes go to the browser, which plays them
through Web Audio so the visualizer can read the real frequency spectrum
rather than animating a guess.
"""
import asyncio
import threading
import uuid
from collections import OrderedDict

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, ear, events
from .core import AlfredCore
from .paths import WEB_DIR
from .voice import SynthesisError, get_voice_engine

# Synthesized clips waiting to be fetched by the page. Bounded — a long session
# would otherwise hold every reply's audio in memory for the whole run.
_MAX_CACHED_CLIPS = 24


class ConsoleSession:
    """
    One Alfred conversation, shared by every connected browser tab. Turns are
    serialized: the engine holds mutable history, so two concurrent turns would
    interleave and corrupt it.
    """

    def __init__(self):
        self.core = AlfredCore()
        self.voice = get_voice_engine()
        self.clients = set()
        self.transcript = []
        self.audio_clips = OrderedDict()
        self.turn_lock = asyncio.Lock()
        self.booted = False
        self.boot_task = None

    # --- fan-out ----------------------------------------------------------

    def _record(self, event):
        """
        Fold events into a canonical list of displayed turns for replay.

        A streamed reply only ever produces reply_start/token/reply_end, so
        recording `message` events alone silently dropped every normal answer
        from the replay. reply_end is the authoritative final text, but boot
        and canned replies emit *both* a message and a matching reply_end — so
        an identical consecutive assistant turn is folded rather than repeated.
        """
        kind = event.get("type")
        if kind == "message":
            self.transcript.append({"role": event["role"], "text": event["text"]})
        elif kind == "reply_end":
            last = self.transcript[-1] if self.transcript else None
            if last and last["role"] == "assistant" and last["text"] == event["text"]:
                return
            self.transcript.append({"role": "assistant", "text": event["text"]})
        elif kind == "notice":
            self.transcript.append({
                "role": "system", "text": event["text"], "level": event.get("level", "info"),
            })
        else:
            return
        del self.transcript[:-400]

    async def broadcast(self, event):
        # Replayed on reconnect so a page refresh doesn't lose the conversation.
        self._record(event)

        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def drive(self, generator):
        """
        Consume a blocking engine generator on a worker thread while forwarding
        its events to the socket as they are produced.
        """
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        done = object()

        def pump():
            try:
                for event in generator:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # never let a worker crash take the socket down
                loop.call_soon_threadsafe(
                    queue.put_nowait, events.notice(f"Engine error: {exc}", "error")
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        threading.Thread(target=pump, daemon=True).start()

        final_reply = None
        while True:
            event = await queue.get()
            if event is done:
                break
            if event.get("type") == "reply_end":
                final_reply = event.get("text")
            await self.broadcast(event)

        if final_reply:
            await self.synthesize(final_reply)

    # --- speech -----------------------------------------------------------

    async def synthesize(self, text):
        if not self.voice.available:
            return
        try:
            audio = await asyncio.to_thread(self.voice.synthesize, text)
        except Exception as exc:
            await self.broadcast(events.notice(f"Voice synthesis failed: {exc}", "warn"))
            return
        if not audio:
            return

        clip_id = uuid.uuid4().hex
        self.audio_clips[clip_id] = audio
        while len(self.audio_clips) > _MAX_CACHED_CLIPS:
            self.audio_clips.popitem(last=False)

        await self.broadcast(events.speak(clip_id, text))

    # --- turns ------------------------------------------------------------

    async def boot(self):
        async with self.turn_lock:
            if self.booted:
                return
            self.booted = True
            for problem in config.missing_requirements():
                await self.broadcast(events.notice(problem, "warn"))
            await self.drive(self.core.boot())

    async def submit(self, text):
        text = (text or "").strip()
        if not text:
            return
        async with self.turn_lock:
            await self.drive(self.core.ask(text))


session = ConsoleSession()
app = FastAPI(title="Alfred Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/session")
async def session_info():
    """Everything the console needs to render its header and replay the log."""
    # Resolving the STT backend can load a model, so keep it off the event loop.
    try:
        stt = await asyncio.to_thread(ear.backend_name)
    except Exception as exc:
        stt = f"unavailable ({exc})"

    return JSONResponse({
        "assistant": config.ASSISTANT_NAME,
        "user": config.USER_NAME,
        "model": config.OLLAMA_MODEL,
        "stt": stt,
        "voice": session.voice.available,
        "transcript": session.transcript,
    })


@app.get("/api/audio/{clip_id}")
async def audio(clip_id: str):
    clip = session.audio_clips.get(clip_id)
    if clip is None:
        return Response(status_code=404)
    return Response(content=clip, media_type="audio/mpeg")


@app.post("/api/transcribe")
async def transcribe(request: Request):
    """Raw little-endian float32 PCM at ear.SAMPLE_RATE, captured in the page."""
    body = await request.body()
    text = await asyncio.to_thread(ear.transcribe_pcm, body)
    return JSONResponse({"text": text})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session.clients.add(websocket)

    # Boot lazily on the first connection so nothing is generated (or spoken)
    # until someone is actually watching. The task is owned by the session, not
    # this socket — a refresh mid-greeting shouldn't cancel the boot.
    if session.boot_task is None:
        session.boot_task = asyncio.create_task(session.boot())

    try:
        while True:
            payload = await websocket.receive_json()
            kind = payload.get("type")
            if kind == "prompt":
                asyncio.create_task(session.submit(payload.get("text", "")))
            elif kind == "transcript":
                # The page transcribed a hold-to-talk take and is submitting it.
                asyncio.create_task(session.submit(payload.get("text", "")))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        session.clients.discard(websocket)


def serve():
    import uvicorn
    uvicorn.run(
        app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
        access_log=False,
    )
