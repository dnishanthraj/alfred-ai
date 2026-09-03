"""
Terminal frontend.

Renders engine events as ANSI and plays audio through afplay. It holds no
conversation logic — everything it knows about what a contact says comes from
`ContactSession`, the same engine the web console drives.
"""
import os
import queue
import select
import sys
import threading
import time

from pynput import keyboard as pynput_keyboard

from .. import config, events
from ..audio import stt
from ..audio.tts import get_voice_engine
from ..contacts import directory
from ..engine import ContactSession
from ..memory import migrate_legacy


class Colors:
    RESET         = "\033[0m"
    BOLD          = "\033[1m"
    ITALIC        = "\033[3m"
    DIM           = "\033[38;2;76;86;106m"
    OPERATOR      = "\033[38;2;216;222;233m"
    CONTACT       = "\033[38;2;111;182;232m"
    FRAME         = "\033[38;2;60;110;150m"
    RED           = "\033[38;2;191;97;106m"
    GREEN         = "\033[38;2;163;190;140m"
    YELLOW        = "\033[38;2;235;203;139m"


KEY_STATE = {'ptt': False, 'esc': False}


def _on_press(key):
    if str(key) == config.PTT_KEY_STR:
        KEY_STATE['ptt'] = True
    elif key == pynput_keyboard.Key.esc:
        KEY_STATE['esc'] = True


def _on_release(key):
    if str(key) == config.PTT_KEY_STR:
        KEY_STATE['ptt'] = False
    elif key == pynput_keyboard.Key.esc:
        KEY_STATE['esc'] = False


class SpeechQueue:
    """
    Sentences arrive faster than they can be spoken, so they queue. Synthesis
    happens on this thread too — one sentence ahead of playback is enough to
    keep the audio seamless without racing the model.
    """

    def __init__(self, voice, interrupt_check):
        self.voice = voice
        self.interrupt_check = interrupt_check
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                continue
            text, voice_id = item
            try:
                audio = self.voice.synthesize(text, voice_id)
            except Exception:
                continue
            self.voice.play_bytes(audio, self.interrupt_check)

    def say(self, text, voice_id):
        self.queue.put((text, voice_id))

    def drop_pending(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class TerminalConsole:
    def __init__(self, contact):
        self.contact = contact
        self.session = ContactSession(contact)
        self.voice = get_voice_engine()
        self.speech = SpeechQueue(self.voice, self._interrupted)
        self.line_open = False

    def _interrupted(self):
        return KEY_STATE['ptt'] or KEY_STATE['esc']

    # --- painting ---------------------------------------------------------

    def header(self):
        os.system('cls' if os.name == 'nt' else 'clear')
        rule = "═" * 65
        print(f"{Colors.FRAME}{Colors.BOLD}╔{rule}╗{Colors.RESET}")
        print(f"{Colors.FRAME}{Colors.BOLD}║{'WAYNETECH CONSOLE'.center(65)}║{Colors.RESET}")
        print(f"{Colors.FRAME}{Colors.BOLD}╚{rule}╝{Colors.RESET}")
        print(f"{Colors.DIM}   Connected: {Colors.CONTACT}{self.contact.full_name}"
              f"{Colors.DIM} | {self.contact.role}"
              f" | Model: {Colors.CONTACT}{self.contact.model}{Colors.RESET}\n")
        print(f"{Colors.DIM}Controls:{Colors.RESET}")
        print(f"  [{Colors.YELLOW}HOLD {config.PTT_KEY_STR}{Colors.RESET}]   :: Voice")
        print(f"  [{Colors.YELLOW}TYPE & ENTER{Colors.RESET}]      :: Text")
        print(f"  [{Colors.RED}ESC{Colors.RESET}]               :: Interrupt / disconnect\n")
        print(f"{Colors.DIM}{'─' * 67}{Colors.RESET}\n")

    def prompt(self):
        print(f"\r\033[K{Colors.OPERATOR}{Colors.BOLD}[{config.USER_NAME.upper()}]:"
              f"{Colors.RESET} ", end="", flush=True)

    def render(self, generator):
        for event in generator:
            kind = event["type"]

            if kind == "state":
                if event["value"] == events.SEARCHING:
                    print(f"\r\033[K{Colors.ITALIC}{Colors.YELLOW}"
                          f"[Scanning...]{Colors.RESET}", end="", flush=True)
                elif event["value"] == events.THINKING:
                    print(f"\r\033[K{Colors.ITALIC}{Colors.DIM}"
                          f"[Processing...]{Colors.RESET}", end="", flush=True)

            elif kind == "message":
                if event["role"] == "user":
                    print(f"\r\033[K{Colors.OPERATOR}{Colors.BOLD}"
                          f"[{config.USER_NAME.upper()}]:{Colors.RESET} {event['text']}")

            elif kind == "sentence":
                # Sentences are printed as they are spoken, one per line-start,
                # which keeps the terminal in step with the audio.
                if not self.line_open:
                    print(f"\r\033[K{Colors.CONTACT}{Colors.BOLD}"
                          f"[{self.contact.name.upper()}]:{Colors.RESET} ", end="", flush=True)
                    self.line_open = True
                else:
                    print(" ", end="", flush=True)
                print(event["text"], end="", flush=True)
                self.speech.say(event["text"], self.contact.voice_id)

            elif kind == "sources":
                if event["items"]:
                    print(f"\n{Colors.DIM}   sources:{Colors.RESET}")
                    for item in event["items"][:3]:
                        print(f"{Colors.DIM}     · {item['title'][:60]}{Colors.RESET}")

            elif kind == "reply_end":
                if self.line_open:
                    print()
                    self.line_open = False
                if not event.get("interim"):
                    print()

            elif kind == "notice":
                color = Colors.RED if event["level"] == "error" else Colors.YELLOW
                print(f"\r\033[K{color}[{event['level'].upper()}] "
                      f"{event['text']}{Colors.RESET}")

    # --- loop -------------------------------------------------------------

    def interrupt(self):
        self.speech.drop_pending()
        self.voice.stop_playback()

    def run(self):
        self.header()
        for problem in config.missing_requirements():
            print(f"{Colors.YELLOW}[WARN] {problem}{Colors.RESET}")

        print(f"\033[3m{Colors.DIM}[Establishing link...]{Colors.RESET}", end="", flush=True)
        self.render(self.session.boot())
        self.prompt()

        while True:
            try:
                if KEY_STATE['ptt']:
                    self.interrupt()
                    print(f"\r\033[K{Colors.RED}[LISTENING...]{Colors.RESET}",
                          end="", flush=True)
                    audio = stt.record_while(lambda: KEY_STATE['ptt'])
                    print(f"\r\033[K{Colors.DIM}[Transcribing...]{Colors.RESET}",
                          end="", flush=True)
                    text = stt.transcribe_audio(audio)
                    print("\r\033[K", end="")
                    if text:
                        self.render(self.session.ask(text))
                    self.prompt()
                    continue

                readable, _, _ = select.select([sys.stdin], [], [], 0.0)
                if readable:
                    self.interrupt()
                    line = sys.stdin.readline().strip()
                    if line.lower() in ('exit', 'quit'):
                        print(f"\n{Colors.CONTACT}[{self.contact.name.upper()}]: "
                              f"Signing off.{Colors.RESET}")
                        return
                    if line:
                        sys.stdout.write("\033[F")  # overwrite the echoed line
                        self.render(self.session.ask(line))
                    self.prompt()
                    continue

                if KEY_STATE['esc']:
                    if self.voice.is_playing:
                        self.interrupt()
                        print(f"\r\033[K{Colors.YELLOW}[Silenced]{Colors.RESET}\n")
                        self.prompt()
                        while KEY_STATE['esc']:
                            time.sleep(0.1)
                    else:
                        print(f"\n\n{Colors.CONTACT}[{self.contact.name.upper()}]: "
                              f"Disconnecting.{Colors.RESET}")
                        return

                time.sleep(0.02)

            except KeyboardInterrupt:
                return


def main(contact_id=None):
    migrate_legacy(config.DEFAULT_CONTACT)
    book = directory()
    contact = book.get(contact_id or config.DEFAULT_CONTACT)
    if contact is None:
        print(f"No such contact: {contact_id}. Known: {', '.join(book.ids())}")
        return 1

    listener = pynput_keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.daemon = True
    listener.start()
    try:
        TerminalConsole(contact).run()
    finally:
        listener.stop()
    return 0
