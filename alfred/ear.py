"""
Speech-to-text.

The backend is resolved lazily on first use. Importing this module used to
instantiate a Whisper model and print to stdout as a side effect, which made
the package impossible to import from a server (or a test) without paying for
a model load and polluting the output.
"""
import io
import os
import re
import sys
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from .config import WHISPER_HINT_PROMPT

SAMPLE_RATE = 16000
CHANNELS = 1
# Minimum recording length worth transcribing (0.3 seconds).
_MIN_SAMPLES = 4800
# Hard ceiling on a single push-to-talk take, so a stuck key can't record forever.
_MAX_RECORD_SECONDS = 120

_backend = None
_backend_lock = threading.Lock()


def _meaningful(text):
    """
    Whisper hallucinates punctuation from silence — an empty take comes back as
    "." or "Thank you." Anything with no letters or digits is not a prompt.
    """
    return bool(re.search(r"[A-Za-z0-9]", text or ""))


def _resolve_backend():
    """
    mlx-whisper uses Apple Silicon's Neural Engine (~5-10x faster than CPU).
    Falls back to faster-whisper on CPU where mlx isn't available.
    """
    global _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        try:
            import mlx_whisper  # noqa: F401
            _backend = ("mlx", "mlx-community/whisper-small.en-mlx")
        except ImportError:
            from faster_whisper import WhisperModel
            _backend = ("faster", WhisperModel("small.en", device="cpu", compute_type="int8"))
        return _backend


def backend_name():
    kind, _ = _resolve_backend()
    return "mlx-whisper (Neural Engine)" if kind == "mlx" else "faster-whisper (CPU)"


def transcribe_audio(audio):
    """Transcribe a float32 mono array at SAMPLE_RATE. Returns '' if too short."""
    if audio is None or len(audio) < _MIN_SAMPLES:
        return ""

    # Normalize to full scale — helps with quiet mic input.
    max_amp = np.max(np.abs(audio))
    if max_amp > 0:
        audio = audio / max_amp * 0.95

    kind, model = _resolve_backend()

    if kind == "mlx":
        import mlx_whisper
        audio_1d = audio.squeeze().astype(np.float32)
        # mlx-whisper writes progress noise to stderr regardless of verbose=False.
        with open(os.devnull, 'w') as devnull:
            old_stderr, sys.stderr = sys.stderr, devnull
            try:
                result = mlx_whisper.transcribe(
                    audio_1d,
                    path_or_hf_repo=model,
                    language="en",
                    verbose=False,
                    initial_prompt=WHISPER_HINT_PROMPT,
                )
            finally:
                sys.stderr = old_stderr
        text = result.get("text", "").strip()
        return text if _meaningful(text) else ""

    from scipy.io.wavfile import write as wav_write
    audio_int16 = np.int16(audio * 32767)
    virtual_file = io.BytesIO()
    wav_write(virtual_file, SAMPLE_RATE, audio_int16)
    virtual_file.seek(0)

    segments, _ = model.transcribe(
        virtual_file,
        language="en",
        beam_size=1,
        temperature=0,
        vad_filter=True,
        vad_parameters={
            "threshold": 0.4,
            "min_speech_duration_ms": 300,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 200,
        },
        initial_prompt=WHISPER_HINT_PROMPT,
        condition_on_previous_text=False,
    )
    text = " ".join(s.text for s in segments).strip()
    return text if _meaningful(text) else ""


def transcribe_pcm(raw_bytes):
    """
    Transcribe little-endian float32 PCM captured by the browser at SAMPLE_RATE.
    Sending raw PCM rather than webm/opus keeps ffmpeg out of the picture
    entirely — the page already has an AudioContext doing the resampling.
    """
    if not raw_bytes:
        return ""
    audio = np.frombuffer(raw_bytes, dtype=np.float32)
    if audio.size == 0:
        return ""
    return transcribe_audio(audio.copy())


def record_while(should_continue, max_seconds=_MAX_RECORD_SECONDS):
    """
    Capture microphone audio for as long as `should_continue()` is true.

    The previous implementation opened a second pynput listener and blocked on
    `listener.join()` until the hotkey was released. A tap fast enough to
    release before that listener attached meant the release event never
    arrived and the app hung indefinitely. Polling a predicate the caller
    already owns removes both the extra listener and the hang — a too-fast tap
    now simply yields a recording too short to transcribe.
    """
    q = queue.Queue()

    def callback(indata, _frames, _time, _status):
        q.put(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, callback=callback):
        deadline = time.time() + max_seconds
        while should_continue() and time.time() < deadline:
            time.sleep(0.01)

    frames = []
    while not q.empty():
        frames.append(q.get())

    if not frames:
        return None
    return np.concatenate(frames, axis=0)


def listen_for_hold(should_continue):
    """Record while the predicate holds, then transcribe. Returns '' on a short take."""
    audio = record_while(should_continue)
    return transcribe_audio(audio)
