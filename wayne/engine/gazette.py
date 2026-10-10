"""
Gotham's own news — what the city's talking about this morning. The Gazette's
front page, GBS at six, the Globe's gossip: the mayoral race, the Knights in
October, who got out of Arkham and who got sent back, last night's worst on the
scanner, what Bruce Wayne was seen doing.

Written once a day by the model from what's actually happened in the city (the
scanner's worst, the rogues' comings and goings), the storylines that run for
weeks — each a step further along every day — and the season; kept, so
everyone read the same paper. The family bring it up the way people do:
"you see Evelyn Adams' piece this morning?" — or not at all.

The people in it are Gotham's own — the mayor, the reporters, the money —
and anyone named in a conversation comes with a line on who they are.
"""
import datetime
import hashlib
import json
import re
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text

_lock = threading.Lock()

# Who writes it, and where.
STAFF = [
    ("Gotham Gazette", "Vicki Vale", "society, City Hall and the Waynes"),
    ("Gotham Gazette", "Evelyn Adams", "the crime desk — the scanner's worst, by morning"),
    ("Gotham Gazette", "Alexander Knox", "investigations — money, contracts, who's paying whom"),
    ("GBS", "Jack Ryder", "a nightly opinion show — loud, contrarian, hates the vigilantes and loves the ratings"),
    ("GCN", "Summer Gleeson", "the six o'clock anchor — weather, traffic, the human story"),
    ("Gotham Globe", "the Globe", "the tabloid — gossip, sightings, a headline in capitals"),
]

# Gotham's public figures: who everyone in the city knows of.
FIGURES = {
    "Christopher Nakano": "the mayor — a law-and-order man who wants the vigilantes gone, up for re-election in November",
    "Marion Grange": "the city councilwoman running against Nakano — Narrows housing, police reform, a former mayor's niece",
    "James Gordon": "GCPD commissioner — the one honest man at the top, everyone says, which is the problem",
    "Janet Van Dorn": "the district attorney — tough, ambitious, and Nakano's to lose",
    "Simon Saint": "Saint Industries — drones, security contracts, and Nakano's biggest donor",
    "Roland Daggett": "Daggett Industries — developer behind half the Narrows rebuilding contracts, and the audit into them",
    "Oswald Cobblepot": "owner of the Iceberg Lounge — 'a legitimate businessman', he'll tell you",
    "Roman Sionis": "CEO of Janus Cosmetics — society pages by day; the rumours do the rest",
    "Joan Leland": "Arkham's chief psychiatrist — the one who gives the interviews when someone gets out",
    "Vicki Vale": "Gotham Gazette — society, City Hall and Bruce Wayne's love life, whether he likes it or not",
    "Evelyn Adams": "Gotham Gazette's crime reporter — first at every scene, owes nobody",
    "Alexander Knox": "Gotham Gazette's investigations man — follows the money, sees bats in every shadow",
    "Jack Ryder": "GBS's nightly loudmouth — ratings over truth, every time",
    "Summer Gleeson": "GCN's six o'clock anchor — the face half the city has dinner with",
}
# Surnames said alone that mean the figure — not "Saint", which is a bar on Park Row too.
_SURNAMES = {name.split()[-1].lower(): name for name in FIGURES if name.split()[-1] not in ("Saint", "Adams")}
_TITLES = {"the mayor": "Christopher Nakano", "mayor": "Christopher Nakano", "the commissioner": "James Gordon",
           "the da": "Janet Van Dorn", "district attorney": "Janet Van Dorn"}


def _draw(*parts):
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def storylines(day=None):
    """
    The stories that run for weeks, where each stands today: the race, the
    Knights, the audit, the strike talk. Drawn from the date, so every day
    moves them on and everyone reads the same.
    """
    from . import holidays
    day = day or datetime.date.today()
    out = []
    election = next((d for d, (name, _) in sorted({**holidays.days(day.year), **holidays.days(day.year + 1)}.items())
                     if name == "Election Day" and d >= day), None)
    if election and (election - day).days <= 70:
        days_left = (election - day).days
        drift = sum(_draw("poll", (day - datetime.timedelta(days=k)).isoformat()) - 0.5 for k in range(10))
        nakano = round(44 + drift * 1.6)
        grange = round(41 - drift * 1.4)
        out.append(f"the mayoral election, {'today' if days_left == 0 else f'in {days_left} days'} — Mayor Nakano "
                   f"{nakano}%, Councilwoman Grange {grange}% in the latest Globe poll"
                   + ("; their debate is this week" if days_left in range(15, 22) or days_left in range(4, 9) else ""))
    if day.month == 10:
        wins = sum(_draw("knights", day.year, k) < 0.55 for k in range(day.day // 3))
        out.append(f"the Gotham Knights in the postseason — {wins} wins this October, the city half-mad about it")
    elif day.month in (4, 5, 6, 7, 8, 9):
        out.append("the Gotham Knights' season — the Knightsdome full on a good night")
    if _draw("audit", day.isocalendar()[1]) < 0.7:
        out.append("the audit into the Narrows rebuilding money — Daggett Industries' contracts under the "
                   "microscope, Knox at the Gazette on it")
    if _draw("strike", day.isocalendar()[1]) < 0.4:
        out.append("the transit workers' union threatening a subway strike over pay")
    return out


def _path():
    return paths.DATA_DIR / "_gazette.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def today(day=None):
    """This morning's news, or yesterday's if this morning's isn't written yet: [{"outlet", "by", "headline", "dek"}]."""
    day = day or datetime.date.today()
    data = _load()
    return data.get(day.isoformat()) or data.get((day - datetime.timedelta(days=1)).isoformat()) or []


def written(day=None):
    return (day or datetime.date.today()).isoformat() in _load()


def _happenings(now):
    """What actually happened in the city since yesterday: the scanner's worst, the rogues' comings and goings."""
    from . import codex, incidents
    out = []
    seen = {}
    for hours in range(0, 24, 2):
        for r in incidents.at(now - hours * 3600):
            if r["severity"] >= 3:
                seen[r["id"]] = r
    for r in sorted(seen.values(), key=lambda r: (-r["severity"], -r["toll"]["dead"]))[:5]:
        toll = incidents.toll_text(r["toll"])
        out.append(f"{r['kind'].lower()} at {r['place']} ({r['area']})" + (f", {toll}" if toll else "")
                   + (f" — {r['suspect']} suspected" if r.get("suspect") else "")
                   + (f" — {r['gang']}" if r.get("gang") else ""))
    for rogue in incidents.rogues():
        state = codex.where(rogue, now)
        if state.get("how") and now - (state.get("since") or 0) < 36 * 3600:
            out.append(f"{rogue['name']} {state['how']}")
    out.extend(_how_they_ended(now))
    return out


# How the city would hear a case ended: never who, only what — and masked figures, as rumour.
_HEARD = {"caught": "suspects in custody — witnesses describe masked figures", "saved": "the hostages walked out alive",
          "solved": "an arrest that police won't explain", "contained": "brought under control before it spread",
          "recovered": "the stolen goods turned up by morning; the thief didn't",
          "got away": "the suspects escaped despite reports of masked figures giving chase",
          "too late": "the suspects were long gone before anyone arrived", "cold": "no arrests, no leads",
          "lost": "a victim didn't survive", "worse": "more people were hurt before it ended",
          "killed": "a suspect was found dead at the scene — police are tight-lipped"}


def _how_they_ended(now):
    """The night's cases the family worked, as the papers would have them: what happened, never who."""
    from . import cases
    out = []
    done = [c for c in cases.everything() if c["status"] == "closed" and now - c.get("closed_at", 0) < 30 * 3600
            and (c["severity"] >= 3 or c.get("suspect"))]
    for c in sorted(done, key=lambda c: -c["severity"])[:4]:
        how = (c.get("result") or {}).get("how", "")
        heard = _HEARD.get(how)
        if heard:
            out.append(f"{c['kind'].lower()} at {c['place']} ({c['area']})"
                       + (f", {c['suspect']} behind it" if c.get("suspect") else "") + f": {heard}")
    return out


def write(model, options, now=None):
    """
    This morning's paper, written by the model from the day's facts. Blocking;
    run once a day while the model's loaded and idle. [] if it can't be had.
    """
    from . import holidays
    from . import model as llm
    now = now or time.time()
    day = datetime.date.fromtimestamp(now)
    happened = _happenings(now)
    staff = "; ".join(f"{who} ({outlet}: {beat})" for outlet, who, beat in STAFF)
    figures = "; ".join(f"{name} — {about}" for name, about in FIGURES.items() if name not in dict((s[1], 1) for s in STAFF))
    season = holidays.note(now)
    instruction = (
        f"It's the morning of {day.strftime('%A %-d %B %Y')} in Gotham City" + (f" — {season}" if season else "")
        + ". Write the city's news this morning: seven items across its papers and channels, the way a "
        "front page and the evening news would actually read — specific, local, a little lurid where Gotham "
        "earns it. Mix it: the running stories moved on a day, last night's worst, city politics, money, sport, "
        "a society item, one human story. Headlines as each outlet writes them: the Globe shouts in capitals, "
        "the Gazette and the channels don't. Never name Batman or any vigilante as a fact: the papers only ever "
        "say 'the vigilantes' or 'the Bat' as rumour.\n"
        f"Who writes it: {staff}.\nWho's who: {figures}.\n"
        f"The running stories: {' | '.join(storylines(day)) or 'none'}.\n"
        f"What happened in the last day: {' | '.join(happened) or 'a quiet night, by Gotham standards'}.\n"
        "Return JSON only: "
        '{"items": [{"outlet": "Gotham Gazette|GBS|GCN|Gotham Globe", "by": "reporter", "headline": "...", '
        '"dek": "one sentence of what it says"}]}')
    items = []
    for _attempt in range(2):           # a thin front page — one story — is written again
        try:
            reply = llm.ask(model, [{"role": "user", "content": instruction}],
                            {**options, "temperature": 0.9, "num_predict": 1100}, fmt="json", purpose="the morning news")
            got = json.loads(reply).get("items") or []
        except Exception:
            continue
        got = [{"outlet": str(i.get("outlet") or "")[:30], "by": str(i.get("by") or "")[:40],
                "headline": str(i.get("headline") or "").strip()[:140], "dek": str(i.get("dek") or "").strip()[:240]}
               for i in got if isinstance(i, dict) and i.get("headline")][:8]
        if len(got) > len(items):
            items = got
        if len(items) >= 4:
            break
    if items:
        with _lock:
            data = _load()
            data[day.isoformat()] = items
            atomic_write(_path(), json.dumps(dict(sorted(data.items())[-7:]), ensure_ascii=False))
    return items


def _words(text):
    return {w for w in re.findall(r"[a-z']{4,}", (text or "").lower())}


def note(contact, prompt="", now=None):
    """
    A few of this morning's items for their prompt — what's on what they were
    asked about first, then whatever they'd have seen — or ''.
    """
    items = today(datetime.date.fromtimestamp(now) if now else None)
    if not items:
        return ""
    asked = _words(prompt)
    ranked = sorted(range(len(items)), key=lambda k: (-len(asked & _words(items[k]["headline"] + " " + items[k]["dek"])),
                                                      _draw(contact.id, datetime.date.today().isoformat(), k)))
    shown = [items[k] for k in ranked[:3]]
    lines = "; ".join(f"{i['outlet']}{', ' + i['by'] if i['by'] and i['by'] != i['outlet'] else ''}: "
                      f"“{i['headline']}” — {i['dek']}" for i in shown)
    return ("Gotham this morning, as the papers and GBS have it — yours to bring up if you would, the way "
            f"people do, or not at all: {lines}")


def who(prompt):
    """Gotham's public figures named in what he said, with who they are — or ''."""
    text = (prompt or "").lower()
    named = {name for name in FIGURES if name.lower() in text}
    named |= {full for surname, full in _SURNAMES.items() if re.search(rf"\b{re.escape(surname)}\b", text)}
    named |= {full for title, full in _TITLES.items() if re.search(rf"\b{re.escape(title)}\b", text)}
    if not named:
        return ""
    return "Who's who in Gotham — as everyone in the city knows of them: " + "; ".join(
        f"{name}, {FIGURES[name]}" for name in sorted(named))
