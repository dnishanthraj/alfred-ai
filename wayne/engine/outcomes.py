"""
How a case ends — caught or gone, the hostages safe or not, solved or cold —
decided the way Gotham would decide it: by who went, what they're good at, what
it was, and who was behind it.

Everyone who works a scene has their strengths: Cass is the best fighter alive
and no detective; Tim is the reverse; Barbara is the network; Dick is the one
you want in a hostage room; Jason gets results and bodies; Randy has the sky;
Batman is the best at all of it. A situation asks for its own mix — a hostage
room wants calm and stealth, a riddle wants a detective, Croc wants someone
who can take a hit — and the city's monsters are hard to catch in their own
right: the Joker nearly never, Calendar Man most nights. The more of the
family on it, the better the odds, with less for each one added.

The numbers stay here. What comes out is an ending: who was caught, who got
away, who got hurt — and the one on the case writes it in their own words.
Seeded by the case, so it's the same ending however often it's asked.
"""
import hashlib

SKILLS = ("fight", "stealth", "detect", "tech", "pursuit", "hostage", "brutes", "toxins", "crowd", "rescue")

# How good each of them is at what a scene asks — never shown, only felt.
RATINGS = {
    "bruce":     {"fight": 0.98, "stealth": 0.97, "detect": 0.99, "tech": 0.92, "pursuit": 0.93, "hostage": 0.95,
                  "brutes": 0.92, "toxins": 0.95, "crowd": 0.9, "rescue": 0.92},
    "nightwing": {"fight": 0.88, "stealth": 0.8, "detect": 0.72, "tech": 0.6, "pursuit": 0.95, "hostage": 0.86,
                  "brutes": 0.62, "toxins": 0.6, "crowd": 0.88, "rescue": 0.86},
    "robin":     {"fight": 0.68, "stealth": 0.84, "detect": 0.95, "tech": 0.9, "pursuit": 0.72, "hostage": 0.72,
                  "brutes": 0.4, "toxins": 0.76, "crowd": 0.55, "rescue": 0.7},
    "batgirl":   {"fight": 0.78, "stealth": 0.78, "detect": 0.87, "tech": 0.98, "pursuit": 0.8, "hostage": 0.78,
                  "brutes": 0.5, "toxins": 0.7, "crowd": 0.65, "rescue": 0.76},
    "orphan":    {"fight": 0.97, "stealth": 0.96, "detect": 0.45, "tech": 0.3, "pursuit": 0.9, "hostage": 0.88,
                  "brutes": 0.78, "toxins": 0.55, "crowd": 0.8, "rescue": 0.7},
    "redhood":   {"fight": 0.9, "stealth": 0.62, "detect": 0.62, "tech": 0.55, "pursuit": 0.76, "hostage": 0.55,
                  "brutes": 0.74, "toxins": 0.55, "crowd": 0.86, "rescue": 0.5},
    "batwing":   {"fight": 0.72, "stealth": 0.55, "detect": 0.52, "tech": 0.83, "pursuit": 0.96, "hostage": 0.6,
                  "brutes": 0.68, "toxins": 0.72, "crowd": 0.76, "rescue": 0.86},
}

# What each sort of call asks for.
_HOSTAGE = {"hostage": 0.45, "stealth": 0.3, "fight": 0.25}
_STREET = {"fight": 0.45, "pursuit": 0.35, "crowd": 0.2}
_HUNT = {"detect": 0.6, "tech": 0.25, "stealth": 0.15}
_DISASTER = {"rescue": 0.45, "crowd": 0.3, "toxins": 0.25}
NEEDS = {
    "Hostage situation": _HOSTAGE, "Kidnapping": {"detect": 0.4, "pursuit": 0.3, "hostage": 0.3},
    "Child abduction": {"detect": 0.45, "pursuit": 0.35, "hostage": 0.2},
    "Surgical abduction": {"detect": 0.4, "stealth": 0.3, "fight": 0.3},
    "Body found": _HUNT, "Homicide": _HUNT, "Serial killing": {"detect": 0.7, "stealth": 0.3},
    "Riddle left at a crime scene": {"detect": 0.75, "tech": 0.25},
    "Missing person": {"detect": 0.55, "tech": 0.3, "pursuit": 0.15},
    "Torture victim found": {"detect": 0.55, "fight": 0.25, "stealth": 0.2},
    "Cult gathering": {"stealth": 0.4, "fight": 0.35, "crowd": 0.25},
    "Explosion reported": _DISASTER, "Arson": _DISASTER, "Chemical spill": {"rescue": 0.5, "toxins": 0.5},
    "Toxin exposure": {"toxins": 0.55, "rescue": 0.25, "fight": 0.2},
    "Laughing-gas attack": {"toxins": 0.5, "crowd": 0.25, "fight": 0.25},
    "Mass overdose": {"rescue": 0.6, "toxins": 0.25, "detect": 0.15},
    "Freezing incident": {"tech": 0.4, "brutes": 0.3, "fight": 0.3},
    "Plant overgrowth attack": {"toxins": 0.4, "stealth": 0.3, "fight": 0.3},
    "Riot": {"crowd": 0.6, "fight": 0.4}, "Turf war": {"fight": 0.45, "crowd": 0.35, "pursuit": 0.2},
    "Mauling": {"brutes": 0.6, "fight": 0.4}, "Assassination": {"pursuit": 0.4, "fight": 0.35, "detect": 0.25},
    "Mob hit": {"detect": 0.45, "pursuit": 0.3, "fight": 0.25}, "Officer down": {"fight": 0.45, "rescue": 0.35, "pursuit": 0.2},
    "Mass shooting": {"fight": 0.4, "rescue": 0.35, "crowd": 0.25}, "Vehicle pursuit": {"pursuit": 0.8, "tech": 0.2},
    "Foot chase": {"pursuit": 0.7, "fight": 0.3}, "Hit-and-run": {"pursuit": 0.6, "tech": 0.25, "detect": 0.15},
    "Cat burglary": {"stealth": 0.35, "pursuit": 0.4, "detect": 0.25},
    "Carjacking": {"pursuit": 0.6, "fight": 0.4},
    "Breakout": {"pursuit": 0.4, "fight": 0.35, "detect": 0.25},
}

# How hard each of them is to bring in, on their own night (0–1).
DIFFICULTY = {
    "Ra's al Ghul": 0.96, "The Court of Owls": 0.94, "The Joker": 0.9, "Deathstroke": 0.9, "Catwoman": 0.88, "Bane": 0.86,
    "Hush": 0.8, "Carmine Falcone": 0.8, "Clayface": 0.76, "Scarecrow": 0.72, "Poison Ivy": 0.72,
    "Mr. Freeze": 0.72, "Killer Croc": 0.7, "Hugo Strange": 0.7, "Black Mask": 0.66, "The Riddler": 0.66,
    "Rupert Thorne": 0.64, "The Penguin": 0.62, "Two-Face": 0.62, "Man-Bat": 0.6, "Professor Pyg": 0.6,
    "Sal Maroni": 0.58, "Harley Quinn": 0.56, "Victor Zsasz": 0.55, "Firefly": 0.55, "Mad Hatter": 0.5,
    "The Ventriloquist": 0.45, "Calendar Man": 0.4, "Killer Moth": 0.3,
}

# What kind of ending a call has.
_FAMILY = {k: "hostage" for k in ("Hostage situation", "Kidnapping", "Child abduction", "Surgical abduction")}
_FAMILY.update({k: "hunt" for k in ("Body found", "Homicide", "Serial killing", "Riddle left at a crime scene",
                                     "Missing person", "Torture victim found", "Mob hit", "Assassination")})
_FAMILY.update({k: "disaster" for k in ("Explosion reported", "Arson", "Chemical spill", "Toxin exposure",
                                         "Laughing-gas attack", "Mass overdose", "Freezing incident",
                                         "Plant overgrowth attack", "Riot")})
_PETTY = ("Mugging", "Break-in", "Vandalism", "Drug deal", "Phone snatch", "Car break-in", "Shoplifting", "Bar fight",
          "Smash-and-grab", "Suspicious package")
_VIOLENT_WORDS = ("stab", "machete", "shoot", "drive-by", "turf", "gang", "assault", "attack", "robbery", "carjack",
                  "brawl", "mob hit", "mass shooting", "officer down", "domestic")
# How long there is to stop it, in minutes from the first call: a phone snatch is over
# before anyone arrives; a standoff can go on for hours; a body waits for whoever comes.
WINDOW = {
    "Phone snatch": 3, "Mugging": 6, "Knifepoint robbery": 6, "Carjacking": 6, "Hit-and-run": 5, "Drive-by shooting": 6,
    "Smash-and-grab": 8, "Car break-in": 8, "Cat burglary": 12, "Unprovoked attack": 8, "Assault": 10, "Mob hit": 10, "Officer down": 10,
    "Assassination": 10, "Shoplifting": 10, "Stabbing": 12, "Machete attack": 12, "Vandalism": 12, "Break-in": 15,
    "Drug deal": 15, "Bar fight": 15, "Brawl": 15, "Armed robbery": 15, "Shots fired": 15, "Sexual assault": 15,
    "Vehicle pursuit": 15, "Mass shooting": 15, "Domestic violence": 20, "Overdose": 20, "Mauling": 20,
    "Gang activity": 30, "Protection racket": 30, "Arson": 30, "Explosion reported": 30, "Turf war": 40,
    "Laughing-gas attack": 40, "Mass overdose": 45, "Freezing incident": 45, "Toxin exposure": 45, "Suspicious package": 60,
    "Chemical spill": 60, "Cult gathering": 60, "Plant overgrowth attack": 60, "Kidnapping": 90, "Riot": 90,
    "Child abduction": 120, "Surgical abduction": 120, "Breakout": 120, "Hostage situation": 180,
    "Riddle left at a crime scene": 180, "Missing person": 240,
}


# How long it takes once they're there, in minutes: a mugging is a chase round the block
# and a word with GCPD; a standoff, a hunt or a fire is a long night.
WORK = {"petty": (8, 16), "street": (12, 26), "hunt": (25, 50), "hostage": (30, 70), "disaster": (25, 55)}


def family(kind):
    """What sort of call it is: hostage, hunt, disaster, petty or street."""
    return _FAMILY.get(kind, "petty" if kind in _PETTY else "street")


def work(case):
    """Minutes on scene before it's over, one way or the other — the same for the same case."""
    lo, hi = WORK[family(case["kind"])]
    return lo + (hi - lo) * _roll(case, "work") + 4 * max(0, case.get("severity", 2) - 2)


def arrival(case):
    """When the first of them got there (or will)."""
    members = case.get("members") or {}
    if members:
        return min(m.get("joined", case.get("opened_at", 0)) + m.get("travel", 9) * 60 for m in members.values())
    return case.get("opened_at", 0) + case.get("travel", 9) * 60


def too_late(case):
    """Whether it was over before anyone got there — the robber long gone, the snatch done."""
    window = WINDOW.get(case["kind"])
    return window is not None and arrival(case) - case.get("began", case.get("opened_at", 0)) > window * 60


INJURIES = ("a cracked rib", "a knife cut along the forearm", "a concussion", "a dislocated shoulder",
            "a burn across the back of the hand", "a sprained ankle", "a split lip and two black eyes",
            "a graze from a bullet", "bruised ribs that'll show for a week")


# The ratings, in words — what they'd do on a scene, never a number.
_SKILL_WORDS = {"fight": "fighting", "stealth": "getting in unseen", "detect": "detective work",
                "tech": "tech and the cameras", "pursuit": "the chase", "hostage": "talking people down, keeping hostages safe",
                "brutes": "taking on the big ones", "toxins": "toxins and antidotes", "crowd": "crowds and holding a line",
                "rescue": "getting people out"}


def strengths(member, kind=None, top=2):
    """What they bring — to this kind of call, if one's given — as words: ['detective work', 'tech and the cameras']."""
    ratings = RATINGS.get(member) or {}
    want = needs(kind) if kind else {}
    ranked = sorted(ratings, key=lambda k: -(ratings[k] * (1 + 2 * want.get(k, 0))))
    return [_SKILL_WORDS[k] for k in ranked[:top]]


def weakest(member, kind=None):
    """Where they're least at home on this call: 'detective work' for Cass, 'taking on the big ones' for Tim."""
    ratings = RATINGS.get(member) or {}
    pool = {k: v for k, v in ratings.items() if not kind or k in needs(kind)} or ratings
    return _SKILL_WORDS[min(pool, key=pool.get)] if pool else ""


def roles(team, kind, names):
    """
    Who does what on this call: what it asks for most goes to whoever's best at
    it, the next to the best of the rest — 'Dick on talking people down, Cass on
    getting in unseen, Tim on the cameras' — and anyone over is on what they're best at.
    """
    crew = [m for m in team if m in RATINGS]
    asked = sorted(needs(kind), key=lambda k: -needs(kind)[k])
    extra = [k for k in SKILLS if k not in asked]
    jobs = {}
    for skill in asked + extra:
        free = [m for m in crew if m not in jobs]
        if not free:
            break
        best = max(free, key=lambda m: RATINGS[m].get(skill, 0))
        if skill in asked or RATINGS[best].get(skill, 0) >= 0.85:
            jobs[best] = skill
    for m in crew:
        jobs.setdefault(m, max(RATINGS[m], key=RATINGS[m].get))
    return "; ".join(f"{names.get(m, m)} on {_SKILL_WORDS[jobs[m]]}" for m in crew)


def _roll(case, what):
    return int(hashlib.sha1(f"{case['id']}:{what}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def needs(kind):
    """What a call of this kind asks for, as weights over the skills."""
    if kind in NEEDS:
        return NEEDS[kind]
    lowered = kind.lower()
    if any(w in lowered for w in _VIOLENT_WORDS):
        return _STREET
    if kind in _PETTY:
        return {"pursuit": 0.55, "fight": 0.45}
    return {"fight": 0.4, "pursuit": 0.3, "detect": 0.3}


def fit(member, kind):
    """How well one of them suits this call, 0–1."""
    ratings = RATINGS.get(member)
    if not ratings:
        return 0.4
    return sum(w * ratings.get(skill, 0.5) for skill, w in needs(kind).items())


def team_fit(members, kind):
    """The whole team's strength on this call: each one helps, the second less than the first."""
    left = 1.0
    for m in members:
        left *= 1 - fit(m, kind) * 0.75
    return 1 - left


def difficulty(case):
    """How hard this one is to close: whoever's behind it, or how bad the crime is."""
    suspect = case.get("suspect") or ""
    if suspect in DIFFICULTY:
        return DIFFICULTY[suspect]
    return 0.15 + 0.1 * case.get("severity", 2)


def outnumbered(case):
    """How badly they're outnumbered: 0 when it's even, toward 1 against Bane and the thirty men he brought."""
    from . import cases
    crew = case.get("crew") or 1
    team = max(1, len(cases.team(case)))
    return max(0.0, min(1.0, (crew / (team * 4.0) - 1) / 4))        # each of them is good for about four


def _holds_a_line(members):
    """How well a team stands up to numbers: the fighters and the ones who can take a hit."""
    rated = [RATINGS[m] for m in members if m in RATINGS]
    if not rated:
        return 0.5
    return max((r["crowd"] + r["brutes"] + r["fight"]) / 3 for r in rated)


def chance(case, gone_wrong=False, backup=False, late=False):
    """The odds of a good ending, 0.05–0.95."""
    from . import cases
    members = cases.team(case)
    p = 0.5 + (team_fit(members, case["kind"]) - difficulty(case)) * 1.3
    # Thirty of Bane's men are thirty men: Tim alone goes under, Cass and Dick hold the door.
    p -= 0.45 * outnumbered(case) * (1.15 - _holds_a_line(members))
    if gone_wrong:
        p -= 0.05 if backup else 0.18          # it went bad; help coming made the difference
    if late:
        p -= 0.1                               # by the time they got there it was half over
    return max(0.05, min(0.95, p))


def decide(case, now=None):
    """
    The ending: {"ok": bool, "how": one of caught / saved / solved / contained /
    got away / lost / cold / worse / killed, "caught": the rogue brought in or "",
    "hurt": {member: injury}, "line": what happened, for whoever writes it up}.
    """
    from . import cases, incidents
    members = cases.team(case)
    sort = family(case["kind"])
    if case["kind"] in incidents.MOVING:
        return _chase_ending(case, members)
    gone_wrong = cases.goes_wrong(case)
    joined_late = len(members) > 1
    late = too_late(case)
    suspect = case.get("suspect") or ""
    who = suspect or "whoever did it"
    if late and sort in ("street", "petty"):
        # Over before anyone got there: geography decides some of these.
        return {"ok": False, "how": "too late", "caught": "", "hurt": {}, "chance": 0.0,
                "line": f"by the time they got there it was over — {who} long gone"}
    p = chance(case, gone_wrong, backup=joined_late, late=late)
    if late:
        p -= 0.2                               # the standoff, the fire, the abduction: hours lost
        p = max(0.05, p)
    r = _roll(case, "end")
    good = r < p
    middling = not good and r < p + (1 - p) * 0.55
    if good and suspect == "Catwoman":
        # Selina: the diamonds back in their case by morning — and her, nowhere. Even "caught", she isn't, for long.
        held = _roll(case, "cat") < 0.12
        return {"ok": True, "how": "caught" if held else "recovered", "caught": "", "hurt": {}, "chance": round(p, 2),
                "line": ("they had her cuffed on the roof — and she was out of them before the squad car came" if held
                         else "what she took was back where it belonged by morning; she was long gone")}
    if good:
        how = {"hostage": "saved", "hunt": "solved", "disaster": "contained"}.get(sort, "caught")
        caught = bool(suspect) and (sort not in ("hostage", "disaster") or _roll(case, "caught") < 0.75)
        if sort in ("street", "petty") and not suspect:
            caught = True
        line = {"saved": "everyone got out alive" + (f", and {who} was taken down" if caught else f" — {who} got away"),
                "solved": f"it was worked out — {who} identified" + (" and brought in" if caught else ", still out there"),
                "contained": "it was contained before it got worse" + (f"; {who} was brought in" if caught else ""),
                "caught": f"{who} was caught and handed to GCPD"}[how]
        if "redhood" in members and sort == "street" and not suspect and _roll(case, "jason") < 0.3:
            how, line = "killed", "the man responsible didn't survive Jason — GCPD found what was left"
    elif middling:
        how, caught = ("got away", False) if sort != "hunt" else ("cold", False)
        line = {"got away": f"nobody else was hurt, but {who} got away",
                "cold": f"it went nowhere — no one for it yet, {who} still out there"}[how]
    else:
        caught = False
        how = {"hostage": "lost", "hunt": "cold", "disaster": "worse"}.get(sort, "worse" if sort == "street" else "got away")
        line = {"lost": "it went wrong — someone they were there to save didn't make it, and " + who + " got away",
                "cold": f"it went cold — no one for it, {who} gone",
                "worse": f"it went bad — more people hurt before it was over, and {who} got away",
                "got away": f"{who} got away"}[how]
    hurt = {}
    if not good and sort in ("street", "hostage", "disaster") and _roll(case, "hurt") < 0.4:
        crew = [m for m in members if m != "bruce"] or members
        if crew:
            m = crew[int(_roll(case, "who") * len(crew))]
            hurt[m] = INJURIES[int(_roll(case, "injury") * len(INJURIES))]
    return {"ok": good, "how": how, "caught": suspect if (good and caught and suspect) else "", "hurt": hurt,
            "line": line, "chance": round(p, 2)}


def catches(case, who=None):
    """Whether the one cutting off a getaway — or chasing it — stops it: decided once, when they get there."""
    from . import cases
    team = cases.team(case) or ([who] if who else [])
    p = 0.5 + (team_fit(team, case["kind"]) - difficulty(case)) * 1.3
    return _roll(case, f"catch:{','.join(sorted(team))}") < max(0.08, min(0.92, p))


def _chase_ending(case, members):
    """A getaway ends where it was stopped, or it doesn't: nobody cut it off, and it's gone."""
    who = case.get("suspect") or "whoever it was"
    caught = (case.get("chase") or {}).get("caught")
    if caught and case.get("suspect") == "Catwoman":
        return {"ok": True, "how": "recovered", "caught": "", "hurt": {}, "chance": 1.0,
                "line": "they ran her down across the roofs and got the bag back — she went off the edge laughing"}
    if caught:
        hurt = {}
        if case["kind"] != "Foot chase" and _roll(case, "crash") < 0.25:
            crew = [m for m in members if m != "bruce"]
            if crew:
                hurt[crew[int(_roll(case, "who") * len(crew))]] = INJURIES[int(_roll(case, "injury") * len(INJURIES))]
        return {"ok": True, "how": "caught", "caught": case.get("suspect") or "", "hurt": hurt, "chance": 1.0,
                "line": (f"they ran {who} down on foot" if case["kind"] == "Foot chase"
                         else f"they cut the car off and {who} was pulled out of it and handed to GCPD")}
    late = not case.get("chase")
    return {"ok": False, "how": "too late" if late else "got away", "caught": "", "hurt": {}, "chance": 0.0,
            "line": (f"by the time they were anywhere near it {who} was long gone" if late
                     else f"they were on it, and {who} still got away")}
