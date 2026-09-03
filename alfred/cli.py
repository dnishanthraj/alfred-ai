"""
Terminal frontend.

Renders engine events as ANSI and plays audio through afplay. It holds no
conversation logic — everything it knows about what Alfred says comes from
`AlfredCore`, the same engine the web console drives.
"""
import os
import select
import shutil
import sys
import time

from pynput import keyboard as pynput_keyboard

from . import config, ear, events
from .config import ASSISTANT_NAME, OLLAMA_MODEL, PTT_KEY_STR, USER_NAME
from .core import AlfredCore
from .voice import get_voice_engine


class Colors:
    RESET         = "\033[0m"
    BOLD          = "\033[1m"
    ITALIC        = "\033[3m"
    POLAR_NIGHT_4 = "\033[38;2;76;86;106m"
    SNOW_STORM_1  = "\033[38;2;216;222;233m"
    FROST_2       = "\033[38;2;136;192;208m"
    FROST_4       = "\033[38;2;94;129;172m"
    AURORA_RED    = "\033[38;2;191;97;106m"
    AURORA_GREEN  = "\033[38;2;163;190;140m"
    AURORA_YELLOW = "\033[38;2;235;203;139m"


KEY_STATE = {'ptt': False, 'esc': False}


def _on_press(key):
    if str(key) == PTT_KEY_STR:
        KEY_STATE['ptt'] = True
    elif key == pynput_keyboard.Key.esc:
        KEY_STATE['esc'] = True


def _on_release(key):
    if str(key) == PTT_KEY_STR:
        KEY_STATE['ptt'] = False
    elif key == pynput_keyboard.Key.esc:
        KEY_STATE['esc'] = False


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    clear_screen()
    line = "═" * 65
    print(f"{Colors.FROST_4}{Colors.BOLD}╔{line}╗{Colors.RESET}")
    print(f"{Colors.FROST_4}{Colors.BOLD}║{'WAYNETECH B.A.T. CONSOLE'.center(65)}║{Colors.RESET}")
    print(f"{Colors.FROST_4}{Colors.BOLD}╚{line}╝{Colors.RESET}")
    print(f"{Colors.POLAR_NIGHT_4}   System: {Colors.AURORA_GREEN}ONLINE{Colors.POLAR_NIGHT_4}"
          f" | Memory: {Colors.AURORA_GREEN}HYBRID{Colors.POLAR_NIGHT_4}"
          f" | Model: {Colors.FROST_2}{OLLAMA_MODEL.upper()}{Colors.RESET}\n")
    print(f"{Colors.POLAR_NIGHT_4}Controls:{Colors.RESET}")
    print(f"  [{Colors.AURORA_YELLOW}HOLD PTT KEY{Colors.RESET}]       :: Voice Command")
    print(f"  [{Colors.AURORA_YELLOW}TYPE & ENTER{Colors.RESET}]       :: Text Command")
    print(f"  [{Colors.AURORA_RED}PRESS ESC{Colors.RESET}]          :: Disconnect\n")
    print(f"{Colors.POLAR_NIGHT_4}{'─' * 67}{Colors.RESET}\n")


def print_prompt():
    print(f"\r\033[K{Colors.SNOW_STORM_1}{Colors.BOLD}[{USER_NAME.upper()}]:{Colors.RESET} ",
          end="", flush=True)


class TerminalConsole:
    def __init__(self):
        self.core = AlfredCore()
        self.voice = get_voice_engine()
        self.streaming = False
        self.streamed = ""

    def _interrupted(self):
        return KEY_STATE['ptt'] or KEY_STATE['esc']

    def _erase_streamed(self):
        """
        Clear the streamed reply, however many terminal lines it wrapped onto.
        A bare `\\r\\033[K` only clears the last one, which left the earlier
        wrapped lines on screen above the corrected text.
        """
        width = max(shutil.get_terminal_size((80, 24)).columns, 1)
        printed = len(f"[{ASSISTANT_NAME.upper()}]: ") + len(self.streamed)
        rows = max(1, -(-printed // width))  # ceil division
        sys.stdout.write("\r\033[K")
        sys.stdout.write("\033[F\033[K" * (rows - 1))

    def _speak(self, text):
        self.voice.speak(
            text,
            interrupt_check=self._interrupted,
            on_error=lambda msg: print(f"\r\033[K{Colors.AURORA_RED}[Voice] {msg}{Colors.RESET}"),
        )

    def render(self, generator):
        """Consume engine events and paint them."""
        for event in generator:
            kind = event["type"]

            if kind == "state":
                if event["value"] == events.SEARCHING:
                    print(f"{Colors.ITALIC}{Colors.AURORA_YELLOW}"
                          f"[Scanning Global Grid...]{Colors.RESET}", end="", flush=True)
                elif event["value"] == events.THINKING:
                    print(f"\r\033[K{Colors.ITALIC}{Colors.POLAR_NIGHT_4}"
                          f"[Processing...]{Colors.RESET}", end="", flush=True)

            elif kind == "message":
                who = USER_NAME if event["role"] == "user" else ASSISTANT_NAME
                color = Colors.SNOW_STORM_1 if event["role"] == "user" else Colors.FROST_2
                print(f"\r\033[K{color}{Colors.BOLD}[{who.upper()}]:{Colors.RESET} {event['text']}")
                if event["role"] == "assistant":
                    self._speak(event["text"])

            elif kind == "reply_start":
                print(f"\r\033[K{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:"
                      f"{Colors.RESET} ", end="", flush=True)
                self.streaming = True
                self.streamed = ""

            elif kind == "token" and self.streaming:
                print(event["text"], end="", flush=True)
                self.streamed += event["text"]

            elif kind == "reply_end":
                if self.streaming:
                    self.streaming = False
                    # The guards can trim what was streamed, so the terminal has
                    # to be corrected to match what is actually spoken. Only
                    # redraw when it genuinely changed — erasing costs a repaint
                    # and risks clipping if the width changed mid-reply.
                    if event["text"] != self.streamed:
                        self._erase_streamed()
                        print(f"{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:"
                              f"{Colors.RESET} {event['text']}")
                    else:
                        print()
                    self._speak(event["text"])
                print()

            elif kind == "notice":
                color = Colors.AURORA_RED if event["level"] == "error" else Colors.AURORA_YELLOW
                print(f"\r\033[K{color}[{event['level'].upper()}] {event['text']}{Colors.RESET}")

    def run(self):
        print_header()
        for problem in config.missing_requirements():
            print(f"{Colors.AURORA_YELLOW}[WARN] {problem}{Colors.RESET}")

        print(f"\033[3m{Colors.POLAR_NIGHT_4}[Initializing Systems...]{Colors.RESET}",
              end="", flush=True)
        self.render(self.core.boot())
        print_prompt()

        while True:
            try:
                if KEY_STATE['ptt']:
                    self.voice.stop_playback()
                    print(f"\r\033[K{Colors.AURORA_RED}[LISTENING...]{Colors.RESET}",
                          end="", flush=True)

                    audio = ear.record_while(lambda: KEY_STATE['ptt'])
                    print(f"\r\033[K{Colors.POLAR_NIGHT_4}[Processing speech...]{Colors.RESET}",
                          end="", flush=True)
                    text = ear.transcribe_audio(audio)
                    print("\r\033[K", end="")

                    if text:
                        self.render(self.core.ask(text))
                    print_prompt()
                    continue

                readable, _, _ = select.select([sys.stdin], [], [], 0.0)
                if readable:
                    self.voice.stop_playback()
                    user_input = sys.stdin.readline().strip()

                    if user_input.lower() in ('exit', 'quit'):
                        self.voice.stop_playback()
                        print(f"\n{Colors.FROST_2}[{ASSISTANT_NAME.upper()}]: "
                              f"Signing off.{Colors.RESET}")
                        return

                    if user_input:
                        sys.stdout.write("\033[F")  # overwrite the echoed line
                        self.render(self.core.ask(user_input))
                    print_prompt()
                    continue

                if KEY_STATE['esc']:
                    if self.voice.is_playing:
                        self.voice.stop_playback()
                        print(f"\r\033[K{Colors.AURORA_YELLOW}"
                              f"[Silence Command Accepted]{Colors.RESET}\n")
                        print_prompt()
                        while KEY_STATE['esc']:
                            time.sleep(0.1)
                    else:
                        print(f"\n\n{Colors.FROST_2}[{ASSISTANT_NAME.upper()}]: "
                              f"Protocol Zero. Shutting down.{Colors.RESET}")
                        return

                time.sleep(0.02)

            except KeyboardInterrupt:
                return


def main():
    listener = pynput_keyboard.Listener(on_press=_on_press, on_release=_on_release)
    listener.daemon = True
    listener.start()
    try:
        TerminalConsole().run()
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
