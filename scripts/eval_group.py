"""
Run scripted group calls through the real engine and print who said what.

    venv/bin/python scripts/eval_group.py

The same engine as the console — each contact's character, the call's routing
and what each one hears — but in-process, with nothing written to anyone's
memory and nothing spoken aloud. Read the transcript: the questions are whether
the right person answered, whether they knew who else was on the line and what
had been said, and whether anyone spoke for anyone else.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayne.contacts import directory  # noqa: E402
from wayne.engine import ContactSession, grapevine, presence  # noqa: E402
from wayne.engine.party import Call  # noqa: E402
from wayne.memory import History, Story, Vault  # noqa: E402

# (description, opening contact, steps) — a step is a line he says, or
# ("+", id) to add someone, or ("-", id) to let them go.
SCRIPTS = [
    ("Addressing: the first person named answers", "alfred", [
        ("+", "lucius"),
        "Lucius, Alfred says the new suit is too heavy. Is he right?",
        "What do you both think I should do tonight?",
    ]),
    ("Late joiner picks up the thread", "alfred", [
        "I had an awful day at work. My manager took credit for my project again.",
        ("+", "catwoman"),
        "Selina, what would you do?",
    ]),
    ("Three on the line, then one leaves", "lucius", [
        ("+", "alfred"), ("+", "catwoman"),
        "Right, all of you — one word each for my week.",
        ("-", "catwoman"),
        "Alfred, was that rude of me?",
    ]),
]


def quiet():
    History.save = lambda self: None
    History.clear = lambda self: None
    grapevine.clear = lambda contact_id: None
    grapevine.block = lambda contact_id: ""
    grapevine.note_call = lambda *a, **k: None
    presence.Presence.note = lambda self, t=None: ""
    presence.Presence.now = lambda self, t=None: {
        "status": presence.IDLE, "doing": "", "until": 0, "source": "free", "last_active": 0}
    for cls in (Vault, Story):
        cls.memorize = lambda self, text: text
        cls.forget = lambda self, needle: []
        cls.clear = lambda self: None
    Vault.as_block = lambda self, prompt="": ""
    Story.entries = lambda self: []


def show(events_, started):
    speaker, line = None, []
    for event in events_:
        if event["type"] == "sentence":
            who = event.get("speaker") or "(single)"
            if who != speaker and line:
                print(f"    {speaker:9} {' '.join(line)}")
                line = []
            speaker = who
            line.append(event["text"])
    if line:
        print(f"    {speaker:9} {' '.join(line)}")
    print(f"    ({time.time() - started:.1f}s)")


def main():
    quiet()
    book = directory()
    for title, first, steps in SCRIPTS:
        print(f"\n=== {title}")
        call = Call()
        sessions = {}

        def session(cid, sessions=sessions):
            if cid not in sessions:
                sessions[cid] = ContactSession(book.get(cid))
                sessions[cid].history.messages = []
                sessions[cid].already_greeted = True
            return sessions[cid]

        call.join(session(first))
        for step in steps:
            started = time.time()
            if isinstance(step, tuple):
                op, cid = step
                print(f"  [{'adds' if op == '+' else 'drops'} {cid}]")
                if op == "+":
                    call.join(session(cid))
                    show(call.greet(session(cid)), started)
                else:
                    show(call.farewell(session(cid)), started)
                    call.leave(session(cid))
                continue
            print(f"  HIM: {step}")
            turn = call.turn(step) if call.is_group else session(first).ask(step)
            show(turn, started)


if __name__ == "__main__":
    main()
