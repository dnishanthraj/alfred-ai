import os
import subprocess
import tempfile
import time

import requests

from .config import ALFRED_VOICE_ID, ELEVENLABS_API_KEY

PLAYBACK_SPEED = 1.0


class AlfredVoiceService:
    def __init__(self):
        print("\033[90m[SYSTEM] Voice Synthesis: ONLINE (ElevenLabs)\033[0m")
        self.current_process = None
        self.is_playing = False

    def _play_audio(self, path, interrupt_check=None):
        try:
            self.current_process = subprocess.Popen(
                ["afplay", "-v", "1", "-r", str(PLAYBACK_SPEED), path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            while self.current_process and self.current_process.poll() is None:
                if interrupt_check and interrupt_check():
                    self.stop_playback()
                    print("\033[3m[Audio Interrupted by User]\033[0m", end="\r")
                    return False
                time.sleep(0.05)

            if self.current_process and self.current_process.returncode != 0:
                _, err = self.current_process.communicate()
                if err:
                    print(f"\033[91m[Audio Error]: {err.decode()}\033[0m")

            return True
        except Exception as e:
            print(f"\033[91m[Playback Exception]: {e}\033[0m")
            return True

    def batch_synthesize(self, full_text):
        if not full_text or not full_text.strip():
            return []

        fd, out_path = tempfile.mkstemp(suffix='.mp3', prefix='alfred_')
        os.close(fd)

        url     = f"https://api.elevenlabs.io/v1/text-to-speech/{ALFRED_VOICE_ID}"
        headers = {
            "Accept":       "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key":   ELEVENLABS_API_KEY,
        }
        data = {
            "text":     full_text,
            "model_id": "eleven_turbo_v2_5",
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=10)
            if response.status_code == 200:
                with open(out_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                if os.path.getsize(out_path) < 100:
                    os.unlink(out_path)
                    print("\033[91m[ElevenLabs Error]: Empty audio — check API key/quota\033[0m")
                    return []
                return [out_path]
            else:
                os.unlink(out_path)
                print(f"\033[91m[ElevenLabs Error {response.status_code}]: {response.text}\033[0m")
                return []
        except Exception as e:
            try:
                os.unlink(out_path)
            except Exception:
                pass
            print(f"\033[91m[Synthesis Failed]: {e}\033[0m")
            return []

    def play_sequence(self, file_paths, interrupt_check=None):
        self.is_playing = True
        try:
            if not file_paths:
                return True

            for path in file_paths:
                if interrupt_check and interrupt_check():
                    return False
                if not self._play_audio(path, interrupt_check):
                    return False
            return True
        finally:
            self.is_playing = False
            self.current_process = None
            for path in (file_paths or []):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

    def stop_playback(self):
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                self.current_process.kill()
            except Exception:
                pass
            self.current_process = None
        self.is_playing = False


_service = None

def get_voice_engine():
    global _service
    if _service is None:
        _service = AlfredVoiceService()
    return _service
