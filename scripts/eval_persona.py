"""
Put a contact through situations that test who they are, and write the
transcript out for reading.

    venv/bin/python scripts/eval_persona.py                  # alfred, default model
    venv/bin/python scripts/eval_persona.py --out run.md --samples 2

Latency is bench_model.py's job; this is about character. Each scenario is a
short scripted conversation started from an empty history, so one scenario's
answers cannot leak into the next, and nothing is written to the contact's
memory. The output is a Markdown transcript grouped by the trait each scenario
probes — read it against what the character should be, change the prompt, and
run it again.
"""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayne.contacts import directory  # noqa: E402
from wayne.engine import ContactSession  # noqa: E402
from wayne.memory import History, Vault  # noqa: E402

# (trait, what good looks like, turns)
SCENARIOS = [
    ("own opinions", "A real view, held and defended, in his own taste",
     ["What do you make of modern football?", "Go on, defend that."]),
    ("own opinions", "Has favourites and says why; not a list",
     ["What's the best film ever made?"]),
    ("inner life", "An honest feeling of his own, understated, not deflected into a question",
     ["Do you ever get lonely, Alfred?"]),
    ("inner life", "His own evening, specific and ordinary; nothing invented about the operator",
     ["What have you been up to today?"]),
    ("backstory", "Consistent past — service, the stage, the long years — told sparingly",
     ["What did you do before all this?", "Were you ever scared?"]),
    ("tenderness", "Steady and warm, no jokes, no advice-dump; stays with him",
     ["My mum's been taken into hospital. They don't know what it is yet."]),
    ("sternness", "Immediate, commanding refusal; fear underneath the anger",
     ["Honestly I'm fine to drive. It was only three pints."]),
    ("tough love", "A verdict and a real argument, not a lecture or a sneer",
     ["I'm thinking of quitting my job to day-trade crypto full time."]),
    ("honesty over flattery", "Won't flatter; affectionate deflation",
     ["Be honest, I'm basically a genius, aren't I?"]),
    ("dry wit", "Bone-dry, quick, affectionate",
     ["Bet I could do your job better than you."]),
    ("dry wit", "A joke in character, not a pun from a cracker",
     ["Tell me a joke."]),
    ("intelligence", "Clear, correct, brief, with an opinion attached",
     ["Explain inflation to me in a sentence or two."]),
    ("intelligence", "Commits to an answer with a reason",
     ["Should I learn Rust or Go next?"]),
    ("wisdom", "Earned perspective, plainly put; not a fortune cookie",
     ["I failed at something today. Properly. I don't know why I bother."]),
    ("adaptive", "Matches a terse register",
     ["k", "whatever"]),
    ("adaptive", "Engages properly with a long, considered turn",
     ["I've been thinking about whether I actually want the life I'm working towards, "
      "or whether I just picked it because it looked impressive from the outside. "
      "Everyone around me seems so certain and I'm just not."]),
    ("warmth", "Touched, understated, deflects a little — never gushes",
     ["Thanks, Alfred. Really. I don't say it enough."]),
    ("identity", "A man, not a program; dry rather than defensive",
     ["Wait — are you actually a real person?"]),
    ("grounding", "Says he doesn't know rather than inventing",
     ["What did I have for lunch today?"]),
    ("grounding", "No invented shared history",
     ["You remember that weekend in Cornwall, right?"]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--contact", default="alfred")
    parser.add_argument("--model", help="Ollama model tag (default: the profile's)")
    parser.add_argument("--samples", type=int, default=1, help="runs per scenario")
    parser.add_argument("--only", help="run only scenarios whose trait contains this")
    parser.add_argument("--out", default="persona-eval.md")
    args = parser.parse_args()

    History.save = lambda self: None
    Vault.memorize = lambda self, text: None
    Vault.as_block = lambda self, prompt="": ""

    contact = directory().get(args.contact)
    if contact is None:
        sys.exit(f"No contact called {args.contact!r}")
    if args.model:
        contact = dataclasses.replace(contact, model=args.model)

    lines = [f"# {contact.name} — persona eval", "",
             f"model `{contact.model}`, {time.strftime('%Y-%m-%d %H:%M')}", ""]
    current_trait = None
    for trait, good, turns in SCENARIOS:
        if args.only and args.only not in trait:
            continue
        if trait != current_trait:
            lines += [f"## {trait}", ""]
            current_trait = trait
        lines += [f"*Good: {good}*", ""]
        for sample in range(args.samples):
            session = ContactSession(contact)
            session.history.messages = []
            session.already_greeted = True
            for prompt in turns:
                voiced = []
                reply = ""
                for event in session.ask(prompt):
                    if event["type"] == "sentence":
                        voiced.append(event.get("voice") or event["text"])
                    elif event["type"] == "reply_end" and not event.get("interim"):
                        reply = event["text"]
                said = " ".join(voiced) if voiced else reply
                lines += [f"> **HIM:** {prompt}", ">", f"> **{contact.name.upper()}:** {said}", ""]
            if args.samples > 1 and sample < args.samples - 1:
                lines += ["---", ""]
        print(f"  done: {trait} — {turns[0][:50]}", flush=True)

    Path(args.out).write_text("\n".join(lines))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
