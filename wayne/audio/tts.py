"""
Text-to-speech via ElevenLabs.

Synthesis (network) and playback (local) are deliberately separate concerns:
the web console fetches the raw bytes and plays them through Web Audio so the
visualizer can read real FFT data, while the terminal frontend hands the same
bytes to `afplay`. Nothing here prints — callers decide how to report errors.
"""
import base64
import json
import os
import subprocess
import tempfile
import threading
import time

import requests

from ..config import ELEVENLABS_API_KEY, ELEVENLABS_MODEL

PLAYBACK_SPEED = 1.0
_REQUEST_TIMEOUT = 30
# Per sentence, in the live console: (connect, silence between bytes). The flat
# 30 seconds meant one slow request held every sentence queued behind it — the
# voice went quiet for half a minute and then the reply arrived as text. A
# sentence that has not started arriving in a few seconds is not coming.
_SENTENCE_TIMEOUT = (3.05, 6)

# One pooled connection for every sentence. A bare `requests.post` opened a new
# TLS connection each time — a handshake to ElevenLabs on every line, paid
# again for the first sentence of every reply, which is the one being waited
# on. Sentences are synthesised several at once, so the pool is sized for that.
_http = requests.Session()
_http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=8))


class SynthesisError(RuntimeError):
    pass


class AlfredVoiceService:
    def __init__(self):
        self.current_process = None
        self._lock = threading.Lock()
        self._playing = False

    @property
    def is_playing(self):
        return self._playing

    @property
    def available(self):
        return bool(ELEVENLABS_API_KEY)

    # --- synthesis --------------------------------------------------------

    def synthesize(self, text, voice_id, timeout=_REQUEST_TIMEOUT):
        """
        Turn text into mp3 bytes in a given contact's voice. Raises
        SynthesisError rather than printing, so the caller can route the
        failure to a terminal or a web console.
        """
        if not text or not text.strip():
            return b""
        if not ELEVENLABS_API_KEY:
            raise SynthesisError("ELEVENLABS_API_KEY is not set")
        if not voice_id:
            raise SynthesisError("This contact has no voice ID configured")

        # The /stream endpoint, read to the end. It returns the same mp3, but
        # sooner: measured on the same sentence, eleven_v4 took 1.0–1.4s here
        # against 1.6–1.8s from the plain endpoint, and v4 Turbo 0.55s
        # against about 1s. Starting playback on the first chunk would save
        # more again; that needs a streaming player in the page.
        response = _http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
            json={"text": text, "model_id": ELEVENLABS_MODEL},
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": ELEVENLABS_API_KEY,
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            raise SynthesisError(f"ElevenLabs returned {response.status_code}: {response.text[:200]}")

        audio = response.content
        if len(audio) < 100:
            raise SynthesisError("ElevenLabs returned empty audio — check API key or quota")
        return audio

    def synthesize_timed(self, text, voice_id):
        """Timed synthesis, falling back once to the plain endpoint. See _timed."""
        try:
            return self._timed(text, voice_id)
        except SynthesisError:
            raise
        except Exception:
            # Timed out or dropped. One more go without the alignment, which is
            # the lighter request; the page estimates word timing instead.
            return self.synthesize(text, voice_id, timeout=_SENTENCE_TIMEOUT), []

    def _timed(self, text, voice_id):
        """
        mp3 bytes plus when each word starts, as [[word, ms], ...].

        The page used to spread a sentence's words evenly across its clip,
        weighted by length — close enough until the voice could sigh. A sigh
        puts most of a second of breath before the first word, and the words
        ran ahead of the voice by exactly that much. ElevenLabs reports the
        start of every character on the same streaming call, at no measurable
        cost (0.59s against 0.55s), so the subtitles can follow the voice
        rather than an estimate of it.
        """
        if not text or not text.strip():
            return b"", []
        if not ELEVENLABS_API_KEY:
            raise SynthesisError("ELEVENLABS_API_KEY is not set")
        if not voice_id:
            raise SynthesisError("This contact has no voice ID configured")

        response = _http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream/with-timestamps",
            json={"text": text, "model_id": ELEVENLABS_MODEL},
            headers={"Content-Type": "application/json", "xi-api-key": ELEVENLABS_API_KEY},
            timeout=_SENTENCE_TIMEOUT, stream=True,
        )
        if response.status_code != 200:
            raise SynthesisError(f"ElevenLabs returned {response.status_code}: {response.text[:200]}")

        audio, chars, starts = bytearray(), [], []
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("audio_base64"):
                audio += base64.b64decode(chunk["audio_base64"])
            alignment = chunk.get("alignment") or {}
            chars += alignment.get("characters") or []
            starts += alignment.get("character_start_times_seconds") or []
        if len(audio) < 100:
            raise SynthesisError("ElevenLabs returned empty audio — check API key or quota")
        return bytes(audio), word_starts(chars, starts)

    # --- local playback (terminal frontend) -------------------------------

    def play_bytes(self, audio, interrupt_check=None):
        """Write mp3 bytes to a temp file and play them through afplay."""
        if not audio:
            return True
        fd, path = tempfile.mkstemp(suffix='.mp3', prefix='alfred_')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(audio)
            return self._play_file(path, interrupt_check)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def speak(self, text, voice_id, interrupt_check=None, on_error=None):
        """
        Synthesize and play on a background thread. Synthesis is a network call
        — doing it on the caller's thread stalls the input loop for as long as
        ElevenLabs takes.
        """
        def _run():
            try:
                audio = self.synthesize(text, voice_id)
            except (SynthesisError, requests.RequestException) as exc:
                if on_error:
                    on_error(str(exc))
                return
            self.play_bytes(audio, interrupt_check)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    def _play_file(self, path, interrupt_check=None):
        try:
            process = subprocess.Popen(
                ["afplay", "-v", "1", "-r", str(PLAYBACK_SPEED), path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                self.current_process = process
                self._playing = True

            while process.poll() is None:
                if interrupt_check and interrupt_check():
                    self.stop_playback()
                    return False
                time.sleep(0.05)
            return True
        except Exception:
            return False
        finally:
            with self._lock:
                self._playing = False
                self.current_process = None

    def stop_playback(self):
        with self._lock:
            process = self.current_process
            self.current_process = None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
        except Exception:
            pass


_service = None
_service_lock = threading.Lock()


def word_starts(chars, starts):
    """
    Group a character alignment into words with their start times in ms,
    skipping stage cues — they are performed, not shown, so they have no word
    on screen to time.
    """
    words, current, began, in_cue = [], "", None, False
    for char, start in zip(chars, starts, strict=False):
        if char == "[":
            in_cue = True
        if in_cue:
            in_cue = char != "]"
            continue
        if char.isspace():
            if current:
                words.append([current, round(began * 1000)])
            current, began = "", None
            continue
        if not current:
            began = start
        current += char
    if current:
        words.append([current, round(began * 1000)])
    return words


def get_voice_engine():
    global _service
    with _service_lock:
        if _service is None:
            _service = AlfredVoiceService()
    return _service
