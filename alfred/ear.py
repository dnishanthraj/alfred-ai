import sys
import os
import queue
import numpy as np
import sounddevice as sd
from pynput import keyboard

from .config import WHISPER_HINT_PROMPT

# --- CONFIG ---
SAMPLE_RATE = 16000
CHANNELS = 1
# Minimum recording length to bother transcribing (0.3 seconds)
_MIN_SAMPLES = 4800

# --- BACKEND SELECTION ---
# mlx-whisper uses Apple Silicon's Neural Engine (~5-10x faster than CPU).
# Falls back to faster-whisper on CPU if mlx is not installed.
try:
    import mlx_whisper
    _USE_MLX = True
    _MLX_MODEL = "mlx-community/whisper-small.en-mlx"
    print("\033[90m[SYSTEM] STT Backend: mlx-whisper (Neural Engine)\033[0m")
except ImportError:
    from faster_whisper import WhisperModel
    import io
    from scipy.io.wavfile import write as wav_write
    _USE_MLX = False
    _fw_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    print("\033[90m[SYSTEM] STT Backend: faster-whisper (CPU)\033[0m")


def transcribe_audio(audio):
    if audio is None or len(audio) < _MIN_SAMPLES:
        return ""

    # Normalize to full scale — helps with quiet mic input
    max_amp = np.max(np.abs(audio))
    if max_amp > 0:
        audio = audio / max_amp * 0.95

    if _USE_MLX:
        audio_1d = audio.squeeze().astype(np.float32)
        with open(os.devnull, 'w') as devnull:
            old_stderr, sys.stderr = sys.stderr, devnull
            try:
                result = mlx_whisper.transcribe(
                    audio_1d,
                    path_or_hf_repo=_MLX_MODEL,
                    language="en",
                    verbose=False,
                    initial_prompt=WHISPER_HINT_PROMPT,
                )
            finally:
                sys.stderr = old_stderr
        return result.get("text", "").strip()
    else:
        audio_int16 = np.int16(audio * 32767)
        virtual_file = io.BytesIO()
        wav_write(virtual_file, SAMPLE_RATE, audio_int16)
        virtual_file.seek(0)

        segments, _ = _fw_model.transcribe(
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
        return " ".join([s.text for s in segments]).strip()


def listen_for_hold(hotkey_char='Key.shift_r'):
    """Records audio while the specific key is held."""
    q = queue.Queue()

    def callback(indata, _frames, _time, _status):
        q.put(indata.copy())

    def on_release(key):
        if str(key) == hotkey_char or key == keyboard.Key.esc:
            return False

    print(f"\n\033[3m(Channel Open - Transmitting)...\033[0m", end="\r", flush=True)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, callback=callback):
        with keyboard.Listener(on_release=on_release) as listener:
            listener.join()

    print(" " * 40, end="\r")

    recorded_frames = []
    while not q.empty():
        recorded_frames.append(q.get())

    if not recorded_frames:
        return ""

    audio_data = np.concatenate(recorded_frames, axis=0)

    print("\033[90m[Processing speech...]\033[0m", end="\r", flush=True)
    text = transcribe_audio(audio_data)
    print(" " * 40, end="\r")
    return text
