"""
Text-to-speech via ElevenLabs.

Synthesis (network) and playback (local) are deliberately separate concerns:
the web console fetches the raw bytes and plays them through Web Audio so the
visualizer can read real FFT data, while the terminal frontend hands the same
bytes to `afplay`. Nothing here prints — callers decide how to report errors.
"""
import os
import subprocess
import tempfile
import threading
import time

import requests

from ..config import ELEVENLABS_API_KEY, ELEVENLABS_MODEL

PLAYBACK_SPEED = 1.0
_REQUEST_TIMEOUT = 30


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

    def synthesize(self, text, voice_id):
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

        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            json={"text": text, "model_id": ELEVENLABS_MODEL},
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": ELEVENLABS_API_KEY,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise SynthesisError(f"ElevenLabs returned {response.status_code}: {response.text[:200]}")

        audio = response.content
        if len(audio) < 100:
            raise SynthesisError("ElevenLabs returned empty audio — check API key or quota")
        return audio

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


def get_voice_engine():
    global _service
    with _service_lock:
        if _service is None:
            _service = AlfredVoiceService()
    return _service
