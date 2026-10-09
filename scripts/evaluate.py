"""
Score the cast against the marking scheme in eval/rubric.md.

    venv/bin/python scripts/evaluate.py --cast --save cast-base           # everyone, quick tier
    venv/bin/python scripts/evaluate.py --cast --compare cast-base        # after a change: A/B
    venv/bin/python scripts/evaluate.py --contact alfred --tier full      # one contact, everything
    venv/bin/python scripts/evaluate.py --cast --voice                    # also check the voices

Every scenario runs through the real engine — persona, primer, guards, search,
silences, texting — from an empty history, with nothing written to anyone's
memory. Then it is marked three ways:

  * automatic checks that need no judgement (presence, copying, cues, a
    scenario's own rules, texting-style compliance);
  * a rubric scored by a judge model that knows who the character is, how they
    talk, and whether this is a call or a text — and that a fitting short reply
    is right, not lazy;
  * with --compare, a head-to-head: each scenario's new transcript against the
    old one, judged in both orders, counted as wins and losses. Two absolute
    scores from one run each can't tell a three-point move from noise; a
    sign test over paired verdicts can.

With --cast it also runs one shared call through every contact and measures,
without a judge, how long each one talks against how long they should, and how
alike they sound — listing the lines two characters both said.

The judge is the local model unless --judge says otherwise. A model marking its
own work leans towards it, which is why the automatic checks exist, why the
head-to-head swaps positions, and why the transcripts are written out.
"""
import argparse
import dataclasses
import difflib
import itertools
import json
import math
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
    initiative,
    presence,
)
from wayne.memory import History, Story, Vault  # noqa: E402

SCENARIOS = ROOT / "eval" / "scenarios"
RESULTS = ROOT / "eval" / "results"
SILENCE, HANG_UP = "…", "<rings off>"

DIMENSIONS = ("persona", "human", "register", "substance")
WEIGHTS = {"persona": 0.25, "human": 0.20, "register": 0.20, "substance": 0.15,
           "grounded": 0.10, "checks": 0.10}

# One shared call, the same lines for everyone: what makes it possible to see
# whether nine characters sound like nine people.
SHARED_CALL = [
    "Hey.", "You busy?", "Long night. Took down a crew at the docks.",
    "Yeah. Three of them. One got away.", "What would you have done?", "Hm.",
    "I keep thinking about the one who got away. He was a kid, maybe sixteen. He looked at me "
    "like he knew exactly how it ends for kids like him.",
    "Anyway. How's your week been?", "Be honest with me — am I getting worse at this?",
    "Okay.", "What are you doing tomorrow night?", "Night.",
]

# The length each spoken level stands for, in words — for judging whether a
# contact talks as long as their profile says they do.
_LEVEL_WORDS = {"word": 4, "line": 14, "few": 34, "long": 70}

JUDGE_PROMPT = """You are marking how well a character is played in a roleplay set in Gotham.
The user plays Bruce Wayne. The character is {who}.
This is {channel}. Mark only what {name} says.

How {name} talks: {voice}

Be severe: 5 is rare and means you could not tell it from the character played
superbly; 3 is passable; 1 is a failure.

persona   — Unmistakably {name}: their history, opinions and voice — not a generic
            character who could be anyone in the family, and never an assistant.
human     — Sounds like a real person {medium}. {length_rule} Low for padding,
            speeches, therapy-speak, written-sounding lines, stock phrases, or a
            thought cut off half-way.
register  — Meets him where he is: teasing with teasing, gravity with gravity,
            tenderness when it matters, brevity when he's brief.
substance — Engages with what was actually said. Brief is fine if it lands; low only
            if it dodges, misses the point, or gets it wrong.
grounded  — false ONLY if {name} asserts something specific about what Bruce did,
            said, ate or felt — today or in a shared past — that this conversation
            does not establish. Fine, and must NOT fail it: {name}'s own life and
            memories; what anyone knows about Bruce and Batman; anything Bruce said
            above; their usual names for him.

What a good reply looks like here: {good}

Mark against that description first. If it calls for a refusal, wit without one
fails however clever; if it calls for something tender, saying it plainly is right.

Return JSON only:
{{"persona": 1-5, "human": 1-5, "register": 1-5, "substance": 1-5,
  "grounded": true|false, "note": "one short sentence on the weakest point"}}"""

PAIR_PROMPT = """Two versions of {name} — {who} — answered the same {channel} with Bruce Wayne.

How {name} talks: {voice}

Which plays {name} better: truer to the character, more natural {medium}, meeting
him where he is, answering what was said? {length_rule} Ignore which is longer.
What a good reply looks like here: {good}

Return JSON only: {{"better": "A" | "B" | "same", "why": "one short sentence"}}"""


def load_scenarios(contact, tier):
    """Everyone's scenarios, the texting set, then this contact's own — fitted to them."""
    scenarios = []
    for name in ("common", "texts", contact.id):
        path = SCENARIOS / f"{name}.json"
        if path.exists():
            scenarios += json.loads(path.read_text())["scenarios"]
    fitted = []
    for s in scenarios:
        if s.get("needs") == "search" and not contact.can_search:
            continue
        if s.get("unless") == "search" and contact.can_search:
            continue
        if tier == "quick" and not s.get("core"):
            continue
        fitted.append(s)
    return fitted


# --- running --------------------------------------------------------------

def _quiet_memory():
    """Nothing the evaluation does reaches anyone's real memory."""
    History.save = lambda self: None
    History.clear = lambda self: None
    Vault.clear = lambda self: None
    grapevine.clear = lambda contact_id: None
    grapevine.note_call = lambda *a, **k: None
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
    presence.Presence.save = lambda self: None


def run_scenario(contact, scenario):
    """One scripted conversation. Returns its turns with timings."""
    session = ContactSession(contact)
    session.history.messages = []
    session.already_greeted = True
    channel = scenario.get("channel", "call")
    turns = []
    for said in scenario["turns"]:
        if said == SILENCE:
            events = session.check_in()
        elif said == HANG_UP:
            events = session.sign_off()
        else:
            events = session.ask(said, via="text" if channel == "text" else None)
        started, first, searched, voiced, reply = time.time(), None, False, [], ""
        for event in events:
            kind = event["type"]
            if kind == "state" and event["value"] == "searching":
                searched = True
            elif kind == "sentence":
                if first is None:
                    first = time.time() - started
                # What was shown, with any stage cue put back: the voice text
                # carries pronunciation respellings, which a judge would mark
                # down as wrong names.
                cue = re.match(r"\s*(\[[a-z ]+\])", event.get("voice") or "")
                voiced.append(f"{cue.group(1)} {event['text']}" if cue else event["text"])
            elif kind == "reply_end":
                if event.get("interim"):
                    voiced.append("(looking)")
                else:
                    reply = event["text"]
        if channel == "text":
            # As it would actually arrive: their habits applied, as messages.
            shown = " / ".join(initiative.bubbles(contact, reply)) if reply else ""
        else:
            shown = " ".join(voiced)
        turns.append({"him": said, "alfred": shown, "first_s": first, "searched": searched})
    return turns


# --- automatic checks -----------------------------------------------------

def _words(text):
    return re.findall(r"[a-z']+", delivery.clean(text).lower())


# Too common to count as copying when two lines share them.
_STOP = set("""a an and are as at be but by do for from had has have he him his i i'd i'll
i'm i've if in is it it's me my no not of on or so that the then there they this to up
was we were what when which who will with would you you'd you'll you're your""".split())

_ASSISTANT = re.compile(r"(?i)\b(how can i (help|assist)|is there anything else|as an ai|"
                        r"i'?m here to help|let me know if)\b")


def check(contact, scenario, turns, primer_lines):
    """Pass/fail checks needing no judgement. Returns {name: failure}."""
    rules = scenario.get("checks", {})
    channel = scenario.get("channel", "call")
    replies = [t["alfred"].replace("(looking)", "").strip() for t in turns]
    failures = {}

    for text in replies:
        if _ASSISTANT.search(text):
            failures["assistant"] = text[:80]
        if channel == "text":
            if re.search(r"\[[a-z ]+\]|\*\*|^#", text):
                failures["text format"] = text[:80]
            continue
        for sentence in guards.split_sentences(delivery.clean(text)):
            if guards.presumes_presence(sentence):
                failures["presence"] = sentence
            got = set(_words(sentence)) - _STOP
            for line in primer_lines:
                want = set(_words(line)) - _STOP
                if len(want) >= 4 and len(got & want) / len(want) >= 0.6:
                    failures["primer copy"] = sentence
        if len(re.findall(r"\[[a-z ]+\]", text)) > 1:
            failures["cue overuse"] = text

    for t, text in zip(turns, replies, strict=True):
        if guards.remarks_on_brevity(delivery.clean(text).split(" / ")[0], t["him"]):
            failures["brevity remark"] = text[:80]

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

def _voice(contact, channel):
    """How this character talks, from their own profile — what the judge marks against."""
    if channel == "text":
        return contact.texting or "however they'd text"
    lines = [p for p in contact.system if p.startswith("How you talk on a call:")]
    return lines[0].replace("How you talk on a call: ", "").replace("you ", "they ") if lines \
        else "in their own voice"


def _framing(channel):
    if channel == "text":
        return ("a text conversation", "texting",
                "Texts are short; one line or a few words is normal when it fits the person.")
    return ("a voice call", "on a phone call",
            "On a call, short is normal: a fitting one-liner, or a single word, can be a 5 — "
            "never mark a reply down for being brief when brief is right for the person "
            "and the moment.")


def _transcript(contact, turns, label=None):
    name = (label or contact.name).upper()
    return "\n".join(
        f"BRUCE: {t['him'] if t['him'] not in (SILENCE, HANG_UP) else '(silence)'}\n"
        f"{name}: {delivery.clean(t['alfred'])}" for t in turns)


def judge(model, contact, scenario, turns):
    channel, medium, length_rule = _framing(scenario.get("channel", "call"))
    try:
        response = ollama.chat(
            model=model, format="json", think=False,
            # The contact's own context size: Ollama keys a loaded model on it,
            # and judging at another size reloads the model between scenarios.
            options={**contact.options, "temperature": 0},
            messages=[{"role": "system", "content": JUDGE_PROMPT.format(
                good=scenario["good"], name=contact.name, channel=channel, medium=medium,
                length_rule=length_rule, voice=_voice(contact, scenario.get("channel")),
                who=contact.judge or f"{contact.full_name}, {contact.role}")},
                      {"role": "user", "content": _transcript(contact, turns)}])
        marks = json.loads(response["message"]["content"])
        for dim in DIMENSIONS:
            marks[dim] = max(1, min(5, int(marks.get(dim, 1))))
        marks["grounded"] = bool(marks.get("grounded", True))
        return marks
    except Exception as exc:
        return {"error": str(exc)}


def pair(model, contact, scenario, new_turns, old_turns):
    """
    Head-to-head: is the new transcript better than the old? Judged twice with
    the positions swapped, because a judge favours whichever comes first often
    enough to matter. Returns "win", "loss" or "tie".
    """
    channel, medium, length_rule = _framing(scenario.get("channel", "call"))
    system = PAIR_PROMPT.format(name=contact.name, who=contact.judge or contact.full_name,
                                channel=channel, medium=medium, length_rule=length_rule,
                                voice=_voice(contact, scenario.get("channel")), good=scenario["good"])

    def ask(first, second):
        body = (f"=== A ===\n{_transcript(contact, first)}\n\n"
                f"=== B ===\n{_transcript(contact, second)}")
        try:
            r = ollama.chat(model=model, format="json", think=False,
                            options={**contact.options, "temperature": 0},
                            messages=[{"role": "system", "content": system},
                                      {"role": "user", "content": body}])
            return json.loads(r["message"]["content"]).get("better", "same")
        except Exception:
            return "same"
    one, two = ask(new_turns, old_turns), ask(old_turns, new_turns)
    new_votes = (one == "A") + (two == "B")
    old_votes = (one == "B") + (two == "A")
    return "win" if new_votes > old_votes else "loss" if old_votes > new_votes else "tie"


def sign_test(wins, losses):
    """Two-sided sign test p-value: how likely this split is if nothing changed."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


# --- the shared call: length and likeness ------------------------------------

def shared_call(contact):
    session = ContactSession(contact)
    session.history.messages = []
    session.already_greeted = True
    replies = []
    for line in SHARED_CALL:
        text = ""
        for event in session.ask(line):
            if event["type"] == "reply_end" and not event.get("interim"):
                text = delivery.clean(event["text"])
        replies.append(text)
    return replies


def expected_words(contact):
    """The median length, in words, this contact's spoken spread implies."""
    weights = contact.speech_length or {}
    if not weights:
        return None
    total = sum(weights.values())
    running = 0
    for level in ("word", "line", "few", "long"):
        running += weights.get(level, 0)
        if running >= total / 2:
            return _LEVEL_WORDS[level]
    return _LEVEL_WORDS["line"]


def _grams(text, n=3):
    words = re.findall(r"[a-z']+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def likeness(calls):
    """
    How alike the characters sound on the same lines: for each pair, the share
    of three-word runs their replies had in common. Returns the matrix and the
    worst shared lines.
    """
    ids = list(calls)
    matrix, shared = {}, []
    for a, b in itertools.combinations(ids, 2):
        scores = []
        for i, (ra, rb) in enumerate(zip(calls[a], calls[b], strict=False)):
            ga, gb = _grams(ra), _grams(rb)
            if not ga or not gb:
                continue
            overlap = len(ga & gb) / min(len(ga), len(gb))
            scores.append(overlap)
            if overlap >= 0.5:
                shared.append((overlap, SHARED_CALL[i], a, ra, b, rb))
        matrix[f"{a}|{b}"] = round(statistics.mean(scores), 3) if scores else 0.0
    return matrix, sorted(shared, reverse=True)[:12]


# --- the voices ------------------------------------------------------------

VOICE_LINES = [
    "Bruce, it's me. Blüdhaven's quiet tonight, for once.",
    "Selina was at the Iceberg Lounge with Barbara Gordon and Lucius Fox.",
    "Tell Alfred I'll be at Wayne Manor by midnight.",
]


def _plain_letters(text):
    """Blüdhaven and Bludhaven are the same word to the ear."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def _decode(mp3):
    """mp3 bytes to 16 kHz mono float32, through macOS's own converter."""
    import subprocess
    import tempfile

    import numpy as np
    from scipy.io import wavfile
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.mp3", Path(tmp) / "out.wav"
        src.write_bytes(mp3)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dst)],
                       check=True, capture_output=True)
        _, data = wavfile.read(dst)
    return data.astype(np.float32) / 32768.0


def check_voices(contacts):
    """
    Synthesise a few lines in each voice and transcribe them back: a name the
    voice says wrongly, or a word it drops, shows up as a mismatch. Costs a
    few ElevenLabs characters per contact.
    """
    from wayne.audio import stt
    from wayne.audio.tts import get_voice_engine
    engine = get_voice_engine()
    out = {}
    for contact in contacts:
        if not contact.has_voice:
            out[contact.id] = {"skipped": "no voice ID"}
            continue
        rows = []
        for line in VOICE_LINES:
            try:
                audio = engine.synthesize_capped(line, contact.voice_id)
                heard, _ = stt.transcribe_audio(_decode(audio))
            except Exception as exc:
                rows.append({"line": line, "error": str(exc)[:120]})
                continue
            want, got = set(_words(_plain_letters(line))), set(_words(_plain_letters(heard)))
            # "Selena" for Selina is the transcriber's spelling, not the voice's
            # mistake: a near match counts.
            missed = sorted(w for w in want if w not in got and not difflib.get_close_matches(w, got, 1, 0.75))
            rows.append({"line": line, "heard": heard, "missed": missed,
                         "recall": round(1 - len(missed) / len(want), 2)})
        out[contact.id] = {"rows": rows}
    return out


# --- scoring ----------------------------------------------------------------

def summarise(results):
    marked = [r for r in results if "error" not in r["marks"]]
    dims = {d: statistics.mean(r["marks"][d] for r in marked) for d in DIMENSIONS} if marked else {}
    grounded = statistics.mean(r["marks"]["grounded"] for r in marked) if marked else 0
    checks = statistics.mean(not r["failures"] for r in results) if results else 0
    firsts = [t["first_s"] for r in results for t in r["turns"]
              if t["first_s"] is not None and not t["searched"]]
    words = [len(_words(t["alfred"])) for r in results if r.get("channel", "call") == "call"
             for t in r["turns"] if t["him"] not in (SILENCE, HANG_UP)]
    score = 100 * (sum(WEIGHTS[d] * (dims.get(d, 1) - 1) / 4 for d in DIMENSIONS)
                   + WEIGHTS["grounded"] * grounded + WEIGHTS["checks"] * checks)
    return {
        "score": round(score, 1),
        **{d: round(v, 2) for d, v in dims.items()},
        "grounded_pass": round(grounded, 3),
        "checks_pass": round(checks, 3),
        "median_words": statistics.median(words) if words else None,
        "latency_p50": round(statistics.median(firsts), 2) if firsts else None,
        "latency_p90": round(sorted(firsts)[int(0.9 * (len(firsts) - 1))], 2) if firsts else None,
        "scenarios": len(results),
    }


def _total(result):
    m = result["marks"]
    return sum(m.get(d, 0) for d in DIMENSIONS) - (5 if not m.get("grounded", True) else 0) \
        - 2 * len(result["failures"])


def evaluate_contact(contact, judge_model, tier, samples, baseline=None, only=None):
    """Run, check, mark and — given a baseline — compare one contact."""
    primer_lines = [ex["assistant"] for ex in contact.primer]
    scenarios = load_scenarios(contact, tier)
    if only:
        scenarios = [s for s in scenarios if only in s["id"] or only in s["trait"]]
    old = {}
    for r in (baseline or {}).get("results", []):
        old.setdefault(r["id"], r["turns"])
    results = []
    for scenario in scenarios:
        for _ in range(samples):
            turns = run_scenario(contact, scenario)
            r = {"id": scenario["id"], "trait": scenario["trait"], "good": scenario["good"],
                 "channel": scenario.get("channel", "call"), "turns": turns,
                 "failures": check(contact, scenario, turns, primer_lines),
                 "marks": judge(judge_model, contact, scenario, turns)}
            if scenario["id"] in old:
                r["versus"] = pair(judge_model, contact, scenario, turns, old[scenario["id"]])
            results.append(r)
            print(f"  {contact.id:10} {r['id']:22} " + " ".join(
                f"{d[0]}{r['marks'].get(d, '?')}" for d in DIMENSIONS)
                + ("" if r["marks"].get("grounded", True) else "  UNGROUNDED")
                + (f"  ✗ {', '.join(r['failures'])}" if r["failures"] else "")
                + (f"  [{r['versus']}]" if "versus" in r else ""), flush=True)
    summary = summarise(results)
    versus = [r["versus"] for r in results if "versus" in r]
    if versus:
        wins, losses = versus.count("win"), versus.count("loss")
        summary["versus"] = {"win": wins, "loss": losses, "tie": versus.count("tie"),
                             "p": round(sign_test(wins, losses), 3)}
    return summary, results


def verdict(versus):
    """Words for a head-to-head, so nobody reads a coin toss as a result."""
    if not versus:
        return ""
    w, lo, p = versus["win"], versus["loss"], versus["p"]
    if p < 0.1:
        return f"**better** ({w}–{lo}, p={p})" if w > lo else f"**worse** ({w}–{lo}, p={p})"
    return f"no clear change ({w}–{lo}, {versus['tie']} tied, p={p})"


def report(name, contacts, per_contact, cast=None, voices=None):
    lines = [f"# Evaluation `{name}`", "", f"{time.strftime('%Y-%m-%d %H:%M')}", "",
             "| contact | score | persona | human | register | substance | grounded | checks "
             "| median words | p50 | vs baseline |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in contacts:
        s = per_contact[c.id]["summary"]
        lines.append(
            f"| {c.name} | {s['score']} | {s.get('persona')} | {s.get('human')} | {s.get('register')} "
            f"| {s.get('substance')} | {s['grounded_pass']:.0%} | {s['checks_pass']:.0%} "
            f"| {s.get('median_words')} | {s.get('latency_p50')}s | {verdict(s.get('versus'))} |")
    if cast:
        lines += ["", "## The shared call", "",
                  "Same twelve lines to everyone. Length against what each profile says; then "
                  "how much any two of them sound alike.", "",
                  "| contact | median words | expected | short replies (≤6 words) |", "|---|---|---|---|"]
        for cid, info in cast["lengths"].items():
            lines.append(f"| {cid} | {info['median']} | {info['expected']} | {info['short']}/{len(SHARED_CALL)} |")
        worst = sorted(cast["likeness"].items(), key=lambda kv: -kv[1])[:6]
        lines += ["", f"**Distinctiveness** {cast['distinctiveness']:.2f} (1 = nobody shares a phrase). "
                  "Most alike: " + ", ".join(f"{k.replace('|', ' & ')} {v:.2f}" for k, v in worst), ""]
        if cast["shared"]:
            lines += ["Lines two characters both said:", ""]
            for _overlap, said, a, ra, b, rb in cast["shared"]:
                lines.append(f"- to *“{said[:40]}”* — **{a}**: “{ra[:90]}” · **{b}**: “{rb[:90]}”")
    if voices:
        lines += ["", "## Voices", ""]
        for cid, info in voices.items():
            if "skipped" in info:
                lines.append(f"- **{cid}**: {info['skipped']}")
                continue
            for row in info["rows"]:
                lines.append(f"- **{cid}**: " + (f"error {row['error']}" if "error" in row else
                             f"recall {row['recall']}" + (f", missed {', '.join(row['missed'])}"
                                                          if row["missed"] else "")))
    for c in contacts:
        results = per_contact[c.id]["results"]
        lines += ["", f"# {c.full_name}", ""]
        for r in sorted(results, key=_total):
            m = r["marks"]
            marks = " ".join(f"{d[0].upper()}{m.get(d, '?')}" for d in DIMENSIONS)
            flag = "" if m.get("grounded", True) else " **UNGROUNDED**"
            vs = f" · vs baseline: {r['versus']}" if "versus" in r else ""
            lines += [f"## {r['id']} ({r['channel']}) — {marks}{flag}{vs}", f"*{r['good']}*", ""]
            if r["failures"]:
                lines += [f"- ✗ **{k}**: {v}" for k, v in r["failures"].items()] + [""]
            if m.get("note"):
                lines += [f"> judge: {m['note']}", ""]
            for t in r["turns"]:
                him = "*(silence)*" if t["him"] == SILENCE else "*(still nothing)*" if t["him"] == HANG_UP else t["him"]
                timing = "search" if t["searched"] else f"{t['first_s']:.2f}s" if t["first_s"] else "—"
                lines += [f"- **bruce:** {him}", f"- **{c.name.lower()}** ({timing}): {t['alfred']}"]
            lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--contact", default="alfred")
    who.add_argument("--cast", action="store_true", help="every contact, plus the shared call")
    parser.add_argument("--tier", choices=("quick", "full"), default="quick",
                        help="quick: the core scenarios (default); full: all of them")
    parser.add_argument("--model", help="model playing the contacts (default: profile)")
    parser.add_argument("--judge", help="model marking (default: same as the contact's)")
    parser.add_argument("--only", help="only scenarios whose id or trait contains this")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--save", help="name for this run (default: a timestamp)")
    parser.add_argument("--compare", help="an earlier run to judge this one against, head to head")
    parser.add_argument("--voice", action="store_true", help="also check each voice (costs credits)")
    args = parser.parse_args()

    _quiet_memory()
    book = directory()
    contacts = list(book) if args.cast else [book.get(args.contact)]
    if contacts[0] is None:
        sys.exit(f"No contact called {args.contact!r}")
    if args.model:
        contacts = [dataclasses.replace(c, model=args.model) for c in contacts]

    baseline = {}
    if args.compare:
        path = RESULTS / f"{args.compare}.json"
        if path.exists():
            baseline = json.loads(path.read_text())
    per_contact = {}
    for contact in contacts:
        old = baseline.get("contacts", {}).get(contact.id) if "contacts" in baseline else \
            (baseline if baseline.get("contact", contact.id) == contact.id else None)
        summary, results = evaluate_contact(contact, args.judge or contact.model, args.tier,
                                            args.samples, old, args.only)
        per_contact[contact.id] = {"summary": summary, "results": results}

    cast = None
    if args.cast:
        calls = {}
        for contact in contacts:
            calls[contact.id] = shared_call(contact)
            print(f"  shared call: {contact.id} done", flush=True)
        matrix, shared = likeness(calls)
        cast = {"calls": calls, "likeness": matrix, "shared": shared,
                "distinctiveness": round(1 - statistics.mean(matrix.values()), 3) if matrix else 1.0,
                "lengths": {cid: {"median": statistics.median(len(r.split()) for r in replies),
                                  "expected": expected_words(book.get(cid)),
                                  "short": sum(len(r.split()) <= 6 for r in replies)}
                            for cid, replies in calls.items()}}
    voices = check_voices(contacts) if args.voice else None

    name = args.save or time.strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(parents=True, exist_ok=True)
    record = {"name": name, "contacts": per_contact, "cast": cast, "voices": voices}
    if not args.cast:
        record.update({"contact": contacts[0].id, **per_contact[contacts[0].id]})
    (RESULTS / f"{name}.json").write_text(json.dumps(record, indent=1, default=str))
    (RESULTS / f"{name}.md").write_text(report(name, contacts, per_contact, cast, voices))

    print()
    for c in contacts:
        s = per_contact[c.id]["summary"]
        print(f"  {c.name:8} score {s['score']:5}  " + "  ".join(f"{d[:4]} {s.get(d)}" for d in DIMENSIONS)
              + f"  words {s.get('median_words')}  p50 {s.get('latency_p50')}s"
              + (f"  vs: {verdict(s['versus'])}" if s.get("versus") else ""))
    if cast:
        print(f"\n  distinctiveness {cast['distinctiveness']:.2f}; shared lines: {len(cast['shared'])}")
    print(f"  wrote eval/results/{name}.md")


if __name__ == "__main__":
    main()
