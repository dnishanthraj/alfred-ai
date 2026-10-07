"""
Put a model through a real conversation with a contact and time it.

    venv/bin/python scripts/bench_model.py gemma4:12b
    venv/bin/python scripts/bench_model.py gpt-oss:20b --think low
    venv/bin/python scripts/bench_model.py qwen3.5:9b --contact alfred

Runs the actual engine — persona, primer, guards, search — over a scripted
exchange, so what it measures is what the console would feel like rather than
what a bare prompt costs. The number that matters is time to the first
sentence: that is when synthesis starts, and so when he starts talking.

Nothing is written to the contact's memory. History and vault writes are
disabled for the run, and the conversation starts from the contact's existing
history so the prompt is the size it really is.
"""
import argparse
import dataclasses
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayne.contacts import directory  # noqa: E402
from wayne.engine import ContactSession  # noqa: E402
from wayne.memory import History, Vault  # noqa: E402

# A spread of registers: small talk, a one-word turn, something serious, an
# opinion, a lookup, and a request for a longer answer.
SCRIPT = [
    "Evening Alfred. Long day.",
    "Hm.",
    "What do you make of people who never apologise?",
    "I think I messed up a presentation at work today. Properly.",
    "Tell me something interesting. Anything.",
    "What's the weather like in London today?",
    "Fair. Talk me out of ordering takeaway again.",
    "Right, I'm off to bed.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("model", nargs="?", help="Ollama model tag (default: the profile's)")
    parser.add_argument("--contact", default="alfred")
    parser.add_argument("--think", help="true, false, or a level such as low")
    args = parser.parse_args()

    History.save = lambda self: None
    Vault.memorize = lambda self, text: None

    contact = directory().get(args.contact)
    if contact is None:
        sys.exit(f"No contact called {args.contact!r}")
    changes = {}
    if args.model:
        changes["model"] = args.model
    if args.think is not None:
        changes["think"] = {"true": True, "false": False}.get(args.think.lower(), args.think)
    contact = dataclasses.replace(contact, **changes)

    session = ContactSession(contact)
    print(f"{contact.model}  (think={contact.think})")

    started = time.time()
    session.warm()
    print(f"  load + prefix: {time.time() - started:.1f}s\n")
    session.already_greeted = True

    firsts = []
    for prompt in SCRIPT:
        started = time.time()
        first, reply, searched = None, "", False
        for event in session.ask(prompt):
            kind = event["type"]
            if kind == "state" and event["value"] == "searching":
                searched = True
            if kind == "sentence" and first is None and not searched:
                first = time.time() - started
            if kind == "reply_end" and not event.get("interim"):
                reply = event["text"]
        total = time.time() - started
        if first is not None:
            firsts.append(first)
        label = "search" if searched else f"{first:.2f}s" if first is not None else "—"
        print(f"  [{label:>6} | {total:4.1f}s]  {prompt}")
        print(f"                    → {reply}\n")

    if firsts:
        print(f"  first sentence: median {statistics.median(firsts):.2f}s, "
              f"worst {max(firsts):.2f}s  (search turns excluded)")


if __name__ == "__main__":
    main()
