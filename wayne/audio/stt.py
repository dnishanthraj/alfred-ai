"""
Speech-to-text.

The backend is resolved lazily on first use. Importing this module used to
instantiate a Whisper model and print to stdout as a side effect, which made
the package impossible to import from a server (or a test) without paying for
a model load and polluting the output.
"""
import io
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import sounddevice as sd

from ..config import WHISPER_HINT_PROMPT, WHISPER_MODEL

SAMPLE_RATE = 16000
CHANNELS = 1
# Minimum recording length worth transcribing (0.3 seconds).
_MIN_SAMPLES = 4800
# Hard ceiling on a single push-to-talk take, so a stuck key can't record forever.
_MAX_RECORD_SECONDS = 120

_backend = None
_backend_lock = threading.Lock()


# Whisper loops when it is unsure, emitting the same clause over and over:
# "the film was made in the early 90s, and the film was made in the early 90s".
# That reached the model as a real sentence and got answered as one.
_LOOP = re.compile(r"(\b[\w' ]{8,60}?\b)[,.]?\s*(?:and\s+)?(?:\1[,.]?\s*(?:and\s+)?){2,}", re.I)


def collapse_loops(text):
    """Fold Whisper's stutter back into a single occurrence of the phrase."""
    return _LOOP.sub(lambda m: m.group(1).strip() + " ", text or "").strip()


def _confidence(result):
    """
    How much to trust this transcription, from the decoder's own signals.

    `avg_logprob` is the model's confidence; `compression_ratio` climbs when the
    output repeats itself; `no_speech_prob` says it may have been silence.
    Passing this on lets a contact hedge — "I didn't catch that" — instead of
    confidently answering something that was never said.
    """
    segments = result.get("segments") or []
    if not segments:
        return 1.0
    logprob = sum(s.get("avg_logprob", -0.3) for s in segments) / len(segments)
    repetition = max((s.get("compression_ratio", 1.0) for s in segments), default=1.0)
    silence = max((s.get("no_speech_prob", 0.0) for s in segments), default=0.0)

    score = 1.0
    if logprob < -0.9:
        score -= 0.5
    elif logprob < -0.6:
        score -= 0.25
    if repetition > 2.4:
        score -= 0.4
    if silence > 0.6:
        score -= 0.3
    return max(0.0, min(1.0, score))


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
            _backend = ("mlx", WHISPER_MODEL)
        except ImportError:
            from faster_whisper import WhisperModel
            _backend = ("faster", WhisperModel("small.en", device="cpu", compute_type="int8"))
        return _backend


def backend_name():
    kind, _ = _resolve_backend()
    return "mlx-whisper (Neural Engine)" if kind == "mlx" else "faster-whisper (CPU)"


def transcribe_audio(audio, hint=None):
    """
    Transcribe a float32 mono array at SAMPLE_RATE.

    Returns (text, confidence). Confidence is the decoder's own uncertainty,
    passed along so a contact can hedge rather than confidently answer
    something that was never said.

    `hint` biases decoding toward words the speaker is likely to use. Whisper
    mangles proper nouns it has no reason to expect — names, places, whatever
    this particular conversation is about — and feeding it those words is far
    more effective than a larger model. Measured on the same clip, a model four
    times the size was three times slower and no more accurate; the hint is
    where the accuracy actually is.
    """
    if audio is None or len(audio) < _MIN_SAMPLES:
        return "", 1.0

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
                    initial_prompt=hint or WHISPER_HINT_PROMPT,
                )
            finally:
                sys.stderr = old_stderr
        text = collapse_loops(result.get("text", "").strip())
        return (text, _confidence(result)) if _meaningful(text) else ("", 1.0)

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
        initial_prompt=hint or WHISPER_HINT_PROMPT,
        condition_on_previous_text=False,
    )
    text = collapse_loops(" ".join(s.text for s in segments).strip())
    # faster-whisper exposes the same fields on its segment objects.
    payload = {"segments": [{"avg_logprob": getattr(s, "avg_logprob", -0.3),
                             "compression_ratio": getattr(s, "compression_ratio", 1.0),
                             "no_speech_prob": getattr(s, "no_speech_prob", 0.0)}
                            for s in segments]}
    return (text, _confidence(payload)) if _meaningful(text) else ("", 1.0)


def transcribe_pcm(raw_bytes, hint=None):
    """
    Transcribe little-endian float32 PCM captured by the browser at SAMPLE_RATE.
    Sending raw PCM rather than webm/opus keeps ffmpeg out of the picture
    entirely — the page already has an AudioContext doing the resampling.
    """
    if not raw_bytes:
        return "", 1.0
    audio = np.frombuffer(raw_bytes, dtype=np.float32)
    if audio.size == 0:
        return "", 1.0
    return transcribe_audio(audio.copy(), hint)


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
    text, _confidence_score = transcribe_audio(record_while(should_continue))
    return text
