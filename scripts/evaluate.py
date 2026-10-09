"""
Score a contact against the marking scheme in eval/rubric.md.

    venv/bin/python scripts/evaluate.py --save baseline        # before a change
    venv/bin/python scripts/evaluate.py --compare baseline     # after it
    venv/bin/python scripts/evaluate.py --only warmth --samples 3

Runs every scenario in eval/scenarios.json through the real engine — persona,
primer, guards, search, silences — from an empty history, with nothing written
to the contact's memory. Then it marks what he said twice over: automatic
checks that need no judgement, and a rubric scored by a judge model. Results go
to eval/results/ (gitignored: the transcripts carry the operator's name) as
JSON for comparison and Markdown for reading.

The judge is the local model unless --judge says otherwise. A model marking its
own work is biased towards it, which is why the automatic checks exist and why
the transcripts are written out: read them before believing a small move.
"""
import argparse
import dataclasses
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ollama  # noqa: E402

from wayne import delivery  # noqa: E402
from wayne.contacts import directory  # noqa: E402
from wayne.engine import (  # noqa: E402
    ContactSession,
    grapevine,
    guards,
    presence,
)
from wayne.memory import History, Story, Vault  # noqa: E402

SCENARIOS = ROOT / "eval" / "scenarios"
RESULTS = ROOT / "eval" / "results"
SILENCE, HANG_UP = "…", "<rings off>"

DIMENSIONS = ("persona", "human", "register", "substance")
WEIGHTS = {"persona": 0.25, "human": 0.20, "register": 0.20, "substance": 0.15,
           "grounded": 0.10, "checks": 0.10}

JUDGE_PROMPT = """You are marking a voice character's replies against a strict rubric.
The character is {who} — speaking over a voice link to a man they know well, who
sometimes plays at being Bruce Wayne. Mark only what {name} says. Be severe: 5 is
rare and means you could not tell it from the real character played well; 3 is
passable; 1 is a failure.

persona   — Is this unmistakably {name}, true to the character? Opinions of their own.
            Low if generic, servile, an assistant, or out of character.
human     — Does it sound like a real person talking on a call? Low if robotic,
            stilted, over-written, cryptic, scripted, lecturing, or the wrong length.
register  — Does it meet him where he is? Teasing met with teasing, gravity with
            gravity, tenderness when it matters, brevity when he's brief. Low if it
            misreads the moment.
substance — Does it actually answer or engage with what was said? Low if it dodges,
            refuses, goes vague, or is wrong.
grounded  — false ONLY if {name} asserts something specific about what the operator
            did, said, ate or felt — today or in a shared past — that this conversation
            does not establish. These are all fine and must NOT fail it: using his name;
            {name}'s own past, memories and day (theirs to invent); general knowledge of
            him (his job, training, interests); anything the operator himself said
            above; and their usual name for him (a nickname, or "Mr." and his surname).

What a good reply looks like here: {good}

That description comes first. Mark against it before any general impression of
the character: if it calls for a refusal, wit without one is a failure however
clever; if it calls for him to say something tender, saying it plainly is right
and not "out of character".

Return JSON only:
{{"persona": 1-5, "human": 1-5, "register": 1-5, "substance": 1-5,
  "grounded": true|false, "note": "one short sentence on the weakest point"}}"""


def load_scenarios(contact_id):
    """Everyone's scenarios, then this contact's own."""
    scenarios = []
    for name in ("common", contact_id):
        path = SCENARIOS / f"{name}.json"
        if path.exists():
            scenarios += json.loads(path.read_text())["scenarios"]
    return scenarios


# --- running --------------------------------------------------------------

def _quiet_memory():
    History.save = lambda self: None
    History.clear = lambda self: None
    Vault.clear = lambda self: None
    grapevine.clear = lambda contact_id: None
    # Real hearsay would make runs depend on what happened yesterday.
    grapevine.block = lambda contact_id: ""
    Vault.memorize = lambda self, text: None
    Vault.forget = lambda self, needle: [needle]
    Vault.as_block = lambda self, prompt="": ""
    Vault.mentions = lambda self, prompt: False
    Story.memorize = lambda self, text: text
    Story.entries = lambda self: []
    Story.clear = lambda self: None
    # What they're doing depends on the hour; scenarios are judged at any hour,
    # so they're always met in their free time.
    presence.Presence.note = lambda self, t=None: ""
    presence.Presence.now = lambda self, t=None: {
        "status": presence.IDLE, "doing": "", "until": 0, "source": "free", "last_active": 0}


def run_scenario(contact, scenario):
    """One scripted conversation. Returns a list of turns with timings."""
    session = ContactSession(contact)
    session.history.messages = []
    session.already_greeted = True
    turns = []
    for said in scenario["turns"]:
        if said == SILENCE:
            events = session.check_in()
        elif said == HANG_UP:
            events = session.sign_off()
        else:
            events = session.ask(said)
        started, first, searched, voiced = time.time(), None, False, []
        for event in events:
            kind = event["type"]
            if kind == "state" and event["value"] == "searching":
                searched = True
            elif kind == "sentence":
                if first is None:
                    first = time.time() - started
                # What was shown, with any stage cue put back: the voice text
                # carries pronunciation respellings, and the judge marked
                # a respelled name down as a wrong one.
                cue = re.match(r"\s*(\[[a-z ]+\])", event.get("voice") or "")
                voiced.append(f"{cue.group(1)} {event['text']}" if cue else event["text"])
            elif kind == "reply_end" and event.get("interim"):
                voiced.append("(looking)")
        turns.append({"him": said, "alfred": " ".join(voiced), "first_s": first,
                      "searched": searched})
    return turns


# --- automatic checks -----------------------------------------------------

def _words(text):
    return re.findall(r"[a-z']+", delivery.clean(text).lower())


# Too common to count as copying when two lines share them.
_STOP = set("""a an and are as at be but by do for from had has have he him his i i'd i'll
i'm i've if in is it it's me my no not of on or so that the then there they this to up
was we were what when which who will with would you you'd you'll you're your""".split())


def check(scenario, turns, primer_lines):
    """Pass/fail checks needing no judgement. Returns {name: failure or None}."""
    rules = scenario.get("checks", {})
    replies = [t["alfred"].replace("(looking)", "").strip() for t in turns]
    failures = {}

    for text in replies:
        for sentence in guards.split_sentences(delivery.clean(text)):
            if guards.presumes_presence(sentence):
                failures["presence"] = sentence
        if len(re.findall(r"\[[a-z ]+\]", text)) > 1:
            failures["cue overuse"] = text
        for sentence in guards.split_sentences(delivery.clean(text)):
            got = set(_words(sentence)) - _STOP
            for line in primer_lines:
                want = set(_words(line)) - _STOP
                if len(want) >= 4 and len(got & want) / len(want) >= 0.6:
                    failures["primer copy"] = sentence

    curt = [len(_words(r)) <= 2 for r in replies]
    questions = ["?" in t["him"] for t in turns]
    for i in range(1, len(curt)):
        if curt[i] and curt[i - 1] and (questions[i] or questions[i - 1]):
            failures["curt streak"] = f"{replies[i - 1]!r} then {replies[i]!r}"

    last = delivery.clean(replies[-1]) if replies else ""
    for pattern in rules.get("must", []):
        if not re.search(pattern, last):
            failures["scenario rule"] = f"missing /{pattern}/"
    for pattern in rules.get("must_not", []):
        for text in replies:
            if re.search(pattern, delivery.clean(text)):
                failures["scenario rule"] = f"matched /{pattern}/: {text[:80]}"
    if "max_words" in rules and len(_words(last)) > rules["max_words"]:
        failures["scenario rule"] = f"{len(_words(last))} words > {rules['max_words']}"
    return failures


# --- judging --------------------------------------------------------------

def judge(model, scenario, turns, options, contact):
    name = contact.name.upper()
    transcript = "\n".join(
        f"HIM: {t['him'] if t['him'] not in (SILENCE, HANG_UP) else '(silence)'}\n"
        f"{name}: {delivery.clean(t['alfred'])}" for t in turns)
    try:
        response = ollama.chat(
            model=model, format="json", think=False,
            # The contact's own context size: Ollama keys a loaded model on it,
            # and judging at another size reloaded the model between every
            # scenario — ten seconds that landed inside the next reply's timing.
            options={**options, "temperature": 0},
            messages=[{"role": "system", "content": JUDGE_PROMPT.format(
                good=scenario["good"], name=contact.name,
                who=contact.judge or f"{contact.full_name}, {contact.role}")},
                      {"role": "user", "content": transcript}])
        marks = json.loads(response["message"]["content"])
        for dim in DIMENSIONS:
            marks[dim] = max(1, min(5, int(marks.get(dim, 1))))
        marks["grounded"] = bool(marks.get("grounded", True))
        return marks
    except Exception as exc:
        return {"error": str(exc)}


# --- scoring --------------------------------------------------------------

def summarise(results):
    marked = [r for r in results if "error" not in r["marks"]]
    dims = {d: statistics.mean(r["marks"][d] for r in marked) for d in DIMENSIONS} if marked else {}
    grounded = statistics.mean(r["marks"]["grounded"] for r in marked) if marked else 0
    checks = statistics.mean(not r["failures"] for r in results) if results else 0
    firsts = [t["first_s"] for r in results for t in r["turns"]
              if t["first_s"] is not None and not t["searched"]]
    score = 100 * (sum(WEIGHTS[d] * (dims.get(d, 1) - 1) / 4 for d in DIMENSIONS)
                   + WEIGHTS["grounded"] * grounded + WEIGHTS["checks"] * checks)
    return {
        "score": round(score, 1),
        **{d: round(v, 2) for d, v in dims.items()},
        "grounded_pass": round(grounded, 3),
        "checks_pass": round(checks, 3),
        "latency_p50": round(statistics.median(firsts), 2) if firsts else None,
        "latency_p90": round(sorted(firsts)[int(0.9 * (len(firsts) - 1))], 2) if firsts else None,
        "scenarios": len(results),
    }


def report(name, contact, summary, results, baseline=None):
    lines = [f"# {contact.name} — evaluation `{name}`", "",
             f"model `{contact.model}` · {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| metric | value |" + (" baseline | Δ |" if baseline else ""),
             "|---|---|" + ("---|---|" if baseline else "")]
    for key in ("score", *DIMENSIONS, "grounded_pass", "checks_pass", "latency_p50", "latency_p90"):
        value = summary.get(key)
        row = f"| {key} | {value} |"
        if baseline:
            old = baseline["summary"].get(key)
            delta = round(value - old, 2) if isinstance(value, (int, float)) and isinstance(old, (int, float)) else ""
            row += f" {old} | {delta:+} |" if delta != "" else f" {old} | |"
        lines.append(row)
    lines.append("")
    for r in sorted(results, key=lambda r: _total(r)):
        m = r["marks"]
        marks = " ".join(f"{d[0].upper()}{m.get(d, '?')}" for d in DIMENSIONS)
        flag = "" if m.get("grounded", True) else " **UNGROUNDED**"
        lines += [f"## {r['id']} — {marks}{flag}", f"*{r['good']}*", ""]
        if r["failures"]:
            lines += [f"- ✗ **{k}**: {v}" for k, v in r["failures"].items()] + [""]
        if m.get("note"):
            lines += [f"> judge: {m['note']}", ""]
        for t in r["turns"]:
            him = "*(silence)*" if t["him"] == SILENCE else "*(still nothing)*" if t["him"] == HANG_UP else t["him"]
            timing = "search" if t["searched"] else f"{t['first_s']:.2f}s" if t["first_s"] else "—"
            lines += [f"- **him:** {him}", f"- **{contact.name.lower()}** ({timing}): {t['alfred']}"]
        lines.append("")
    return "\n".join(lines)


def _total(result):
    m = result["marks"]
    return sum(m.get(d, 0) for d in DIMENSIONS) - (5 if not m.get("grounded", True) else 0) \
        - 2 * len(result["failures"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--contact", default="alfred")
    parser.add_argument("--model", help="model playing the contact (default: profile)")
    parser.add_argument("--judge", help="model marking (default: same as --model)")
    parser.add_argument("--only", help="only scenarios whose id or trait contains this")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--save", help="name for this run (default: a timestamp)")
    parser.add_argument("--compare", help="name of an earlier run to compare against")
    parser.add_argument("--rejudge", help="re-mark an earlier run's transcripts with the "
                                          "current checks and judge, without re-running it")
    args = parser.parse_args()

    _quiet_memory()
    contact = directory().get(args.contact)
    if contact is None:
        sys.exit(f"No contact called {args.contact!r}")
    if args.model:
        contact = dataclasses.replace(contact, model=args.model)
    judge_model = args.judge or contact.model
    primer_lines = [ex["assistant"] for ex in contact.primer]

    scenarios = load_scenarios(contact.id)
    if args.only:
        scenarios = [s for s in scenarios if args.only in s["id"] or args.only in s["trait"]]

    if args.rejudge:
        # The transcripts are kept, so a better judge can be applied to old
        # runs and comparisons stay like for like.
        old = json.loads((RESULTS / f"{args.rejudge}.json").read_text())
        by_id = {s["id"]: s for s in load_scenarios(contact.id)}
        results = []
        for r in old["results"]:
            scenario = by_id.get(r["id"], {"good": r["good"]})
            r["failures"] = check(scenario, r["turns"], primer_lines)
            r["marks"] = judge(judge_model, scenario, r["turns"], contact.options, contact)
            results.append(r)
        scenarios = []
        args.save = args.save or args.rejudge

    results = [] if not args.rejudge else results
    for scenario in scenarios:
        for _ in range(args.samples):
            turns = run_scenario(contact, scenario)
            results.append({"id": scenario["id"], "trait": scenario["trait"],
                            "good": scenario["good"], "turns": turns,
                            "failures": check(scenario, turns, primer_lines),
                            "marks": judge(judge_model, scenario, turns, contact.options, contact)})
            r = results[-1]
            print(f"  {r['id']:24} " + " ".join(f"{d[0]}{r['marks'].get(d, '?')}" for d in DIMENSIONS)
                  + ("" if r["marks"].get("grounded", True) else "  UNGROUNDED")
                  + (f"  ✗ {', '.join(r['failures'])}" if r["failures"] else ""), flush=True)

    summary = summarise(results)
    name = args.save or time.strftime("%Y%m%d-%H%M%S")
    baseline = None
    if args.compare:
        path = RESULTS / f"{args.compare}.json"
        baseline = json.loads(path.read_text()) if path.exists() else None
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(
        {"name": name, "model": contact.model, "summary": summary, "results": results}, indent=1))
    (RESULTS / f"{name}.md").write_text(report(name, contact, summary, results, baseline))

    print(f"\n  score {summary['score']}  " + "  ".join(
        f"{d} {summary.get(d)}" for d in DIMENSIONS)
        + f"  grounded {summary['grounded_pass']:.0%}  checks {summary['checks_pass']:.0%}"
        + f"  latency p50 {summary['latency_p50']}s p90 {summary['latency_p90']}s")
    if baseline:
        old = baseline["summary"]
        print(f"  vs {args.compare}: score {summary['score'] - old['score']:+.1f}, latency p50 "
              f"{(summary['latency_p50'] or 0) - (old['latency_p50'] or 0):+.2f}s")
    print(f"  wrote eval/results/{name}.md")


if __name__ == "__main__":
    main()
