"""
The sound of where someone is, under their voice on a call.

A phone call carries the room: the engine and the road from someone driving,
the checkout beeping behind someone in the supermarket, wind on a rooftop, the
drip of the cave. Each kind of place is a seamless loop made once with
ElevenLabs' sound effects (eleven_text_to_sound_v2, looping, about twenty
seconds — some 800 credits a place, once) and kept on disk ever after; the page
plays it low under the call, and on a call with company only behind whoever is
talking, the way a phone's noise gate lets a room through only with a voice.

Which place is worked out from what they're doing and where (see `scene_for`)
— the same truth the map and their words come from. Nothing here runs without
a key, and nothing is made twice.
"""
import logging
import re
import threading
import time

import requests

from .. import config, paths

log = logging.getLogger("wayne")

LOOP_SECONDS = 20
# Every loop is told the same things: seamless, no music, nobody speaking words
# that could be mistaken for the call.
_TAIL = " Seamless loop. No music. Any voices distant and indistinct."
SCENES = {
    "car": "Inside a moving car: steady engine hum, tyre and road noise, an indicator ticking now and then.",
    "subway": "Inside a subway carriage: wheels rumbling on the rails, the carriage rattling, a muffled announcement.",
    "rooftop": "Wind gusting over a city rooftop at night, traffic far below, a police siren far away now and then.",
    "street_day": "A busy city street by day: traffic passing, footsteps, car horns in the distance.",
    "street_night": "A city street at night: sparse traffic hiss, a distant siren, a dog barking far off.",
    "grocery": "A supermarket: shopping trolleys rattling, checkout scanners beeping in the distance, a fridge humming.",
    "cafe": "A busy coffee shop: cups and saucers clinking, an espresso machine hissing, murmured conversation.",
    "bar": "A lively bar at night: glasses clinking, crowd chatter and laughter.",
    "office": "A quiet office: keyboards typing, air conditioning humming, a phone ringing in the distance.",
    "lab": "A high-tech workshop: servers humming, cooling fans, soft electronic beeps.",
    "cave": "A vast cave: water dripping and echoing, bats fluttering far off, the low hum of computers.",
    "manor": "A large quiet old house: a grandfather clock ticking, old wood creaking, wind outside.",
    "home": "A quiet apartment: a refrigerator humming, city traffic faint through a window.",
    "park": "A city park by day: birdsong, leaves rustling in a light breeze, children playing in the distance.",
    "garden": "A garden: birdsong, a light breeze through hedges, a distant lawnmower.",
    "hospital": "A hospital corridor: monitors beeping in the distance, quiet footsteps, a muffled intercom.",
    "gym": "A gym: weights clanking, a treadmill whirring, a heavy bag being hit.",
    "police": "A busy police station: phones ringing, radio chatter, a door buzzing.",
    "docks": "A harbour at night: water lapping at the pilings, a foghorn far off, gulls, rope creaking.",
    "campus": "A university campus: students chatting, footsteps on paths, a bell in the distance.",
    "scuffle": "A fight nearby, muffled: grunts, blows landing, a body hitting crates, glass breaking.",
    # What they're doing
    "kitchen": "Cooking in a home kitchen: a pan sizzling, a pot simmering, a knife on a chopping board.",
    "shower": "A shower running in a tiled bathroom, water drumming steadily.",
    "walking": "Footsteps walking along a city pavement, traffic passing, the odd voice going by.",
    "running": "Fast running footsteps on pavement and heavy steady breathing.",
    "gliding": "Wind rushing past at speed while gliding high above a city at night, fabric snapping.",
    "studio": "An empty dance studio: soft footsteps and turns on a sprung wooden floor, a creak of the barre.",
    # Whose home it is
    "cats_home": "A quiet, stylish apartment at night with cats: soft purring close by, paws padding across the floor.",
    "dog_home": "A small apartment with a dog: a dog's tags jingling, paws clicking on the floor, a contented huff.",
    "houseboat": "Inside a houseboat at a marina at night: water lapping at the hull, ropes creaking, a halyard tapping a mast.",
    "clocktower": "Inside a clock tower: huge gears ticking and turning slowly overhead, computer fans humming.",
    "safehouse": "A bare safehouse in a run-down building: a dripping pipe, distant traffic, the building creaking.",
    # The weather, out in it
    "rain_street": "Steady rain falling on a city street at night, water running in gutters, tyres hissing.",
    "rain_roof": "Heavy rain drumming on a rooftop at night, wind gusting, water pouring off ledges.",
    "storm": "A thunderstorm over a city at night: heavy rain, wind, thunder rolling and cracking.",
    "snow": "A snowy city street at night, muffled and hushed, wind sighing, footsteps crunching.",
}

# The things that happen in a place now and then, over its loop: made once each
# (a few seconds, not looped), heard at random — a siren passing, two shots
# between buildings, a punch landing in the fight they're in.
ONE_SHOTS = {
    # The street
    "siren": "A police siren passing at a distance on a city street at night, fading away.",
    "horn": "A car horn blaring in traffic and an angry shout.",
    "car_pass": "A car driving past quickly on a wet street, tyres hissing.",
    "motorbike": "A motorcycle revving and roaring past, fading down the street.",
    "bus": "A city bus pulling up, air brakes hissing, doors folding open.",
    "alarm": "A car alarm going off a few streets away, then cutting out.",
    "dog": "A dog barking a few streets away at night.",
    "scream": "A distant scream somewhere in the city at night, cut short.",
    "gunshots": "Two distant gunshots echoing between tall buildings at night.",
    "helicopter": "A police helicopter passing low overhead, rotor thump fading into the distance.",
    "train": "An elevated train rattling past on its tracks.",
    "thunder": "Distant thunder rolling over a city.",
    # Up high
    "wind_gust": "A strong gust of wind buffeting a rooftop, whistling through railings.",
    "grapple": "A grapnel gun firing, the cable zipping out and catching with a clank.",
    "cape": "Heavy fabric flapping hard in the wind.",
    # A fight
    "punch": "A heavy punch landing, a grunt, a body stumbling into crates — close by.",
    "kick": "A hard kick connecting and a man groaning as he hits the ground.",
    "glass": "A bottle smashing on concrete nearby.",
    # Inside
    "door": "A heavy metal door banging shut somewhere in a building.",
    "phone_buzz": "A phone vibrating on a wooden table.",
    "phone_ring": "An office desk phone ringing twice.",
    "keyboard": "A quick burst of fast typing on a mechanical keyboard.",
    "printer": "An office printer whirring and pushing out a page.",
    "elevator": "An elevator chime and doors sliding open.",
    "kettle": "A kettle coming to the boil and clicking off.",
    "clock": "A grandfather clock chiming the hour in a big quiet house.",
    "computer_beep": "A few soft electronic alert beeps from a computer console.",
    "police_radio": "A short burst of police radio chatter, crackling and indistinct.",
    "announcement": "A muffled public-address announcement echoing, indistinct.",
    # Out and about
    "trolley": "A shopping trolley rattling over a shop floor.",
    "checkout": "A supermarket checkout scanner beeping a few times.",
    "espresso": "An espresso machine hissing and steaming milk.",
    "till": "A cash register drawer ringing open.",
    "glasses": "Glasses clinking in a toast and a burst of laughter.",
    "cheer": "A crowd cheering at something on the bar's television.",
    "weights": "Gym weights clanking down onto a rack.",
    "bell": "A campus bell ringing the hour in the distance.",
    "birds": "A burst of birdsong in a park.",
    # The water and the cave
    "foghorn": "A ship's foghorn far off across the harbour.",
    "gulls": "Seagulls crying over a harbour.",
    "bats": "A flurry of bats fluttering through a cave, echoing.",
    "drip": "Water dripping into a pool in a vast echoing cave.",
    "subway_brakes": "A subway train braking into a station, doors chiming and sliding open.",
    # At home, theirs
    "meow": "A cat meowing close by, curious.",
    "purr": "A cat purring loudly right next to the phone.",
    "cat_jump": "A cat jumping down onto a wooden floor and trotting away.",
    "dog_bark": "A dog barking twice inside an apartment, excited.",
    "dog_tags": "A dog shaking itself, tags jingling.",
    "sizzle": "Food hitting a hot pan and sizzling loudly.",
    "chop": "A knife chopping quickly on a wooden board.",
    "plates": "Plates and cutlery clinking as a table is laid.",
    "tap": "A kitchen tap running briefly and turning off.",
    "shower_door": "A glass shower door sliding open, water still running.",
    "hull": "Water slapping against a boat's hull, the boat rocking and creaking.",
    "gears": "A big clockwork gear clunking into place.",
    "slide": "A pistol slide being racked, metallic and close.",
    "footsteps": "Footsteps on a stone floor, coming closer and passing.",
    "rustle": "A paper grocery bag rustling as it's set down.",
    "jet": "A jet thruster whining past overhead and fading.",
    "window_rain": "A gust of rain spattering against a window.",
}
# For each kind of place, what's heard over it and about how often (seconds between, on average).
SCENE_SHOTS = {
    "scuffle": [("punch", 7), ("kick", 14), ("glass", 45), ("gunshots", 300), ("scream", 400)],
    "rooftop": [("wind_gust", 35), ("siren", 70), ("cape", 90), ("helicopter", 180), ("grapple", 240),
                ("gunshots", 420), ("thunder", 600)],
    "street_night": [("siren", 75), ("car_pass", 60), ("dog", 120), ("alarm", 300), ("train", 240),
                     ("gunshots", 500), ("scream", 900)],
    "street_day": [("horn", 60), ("car_pass", 45), ("bus", 150), ("siren", 160), ("motorbike", 200), ("train", 220)],
    "car": [("horn", 90), ("motorbike", 160), ("siren", 180), ("car_pass", 70)],
    "subway": [("subway_brakes", 120), ("announcement", 160)],
    "grocery": [("trolley", 40), ("checkout", 50), ("announcement", 180)],
    "cafe": [("espresso", 45), ("till", 90), ("glasses", 150)],
    "bar": [("glasses", 40), ("cheer", 110), ("door", 150)],
    "office": [("keyboard", 40), ("phone_ring", 120), ("printer", 160), ("elevator", 200)],
    "lab": [("computer_beep", 45), ("keyboard", 60), ("door", 300)],
    "cave": [("drip", 25), ("bats", 90), ("computer_beep", 70)],
    "manor": [("clock", 600), ("door", 240), ("kettle", 500)],
    "home": [("phone_buzz", 200), ("kettle", 400), ("siren", 400)],
    "park": [("birds", 40), ("dog", 120)],
    "garden": [("birds", 35)],
    "hospital": [("announcement", 90), ("computer_beep", 60), ("door", 150)],
    "gym": [("weights", 30), ("punch", 50)],
    "police": [("police_radio", 45), ("phone_ring", 90), ("door", 120)],
    "docks": [("foghorn", 130), ("gulls", 70), ("door", 200)],
    "campus": [("bell", 300), ("birds", 90)],
    "kitchen": [("sizzle", 35), ("chop", 50), ("tap", 90), ("plates", 120)],
    "shower": [("shower_door", 240)],
    "walking": [("car_pass", 40), ("horn", 120), ("dog", 150)],
    "running": [("car_pass", 50), ("dog", 140)],
    "gliding": [("wind_gust", 25), ("cape", 30), ("grapple", 90), ("siren", 120), ("jet", 400)],
    "studio": [("footsteps", 60)],
    "cats_home": [("purr", 60), ("meow", 90), ("cat_jump", 150)],
    "dog_home": [("dog_tags", 70), ("dog_bark", 160)],
    "houseboat": [("hull", 50), ("gulls", 120), ("foghorn", 300)],
    "clocktower": [("gears", 40), ("computer_beep", 70), ("keyboard", 80)],
    "safehouse": [("drip", 30), ("slide", 300), ("siren", 150), ("door", 200)],
    "rain_street": [("car_pass", 45), ("thunder", 400), ("siren", 160)],
    "rain_roof": [("wind_gust", 40), ("thunder", 250), ("grapple", 200)],
    "storm": [("thunder", 35), ("window_rain", 60)],
    "snow": [("footsteps", 90), ("wind_gust", 120)],
}
# Out in the weather: what the place sounds like when it's raining, storming, snowing.
_WEATHERED = {"street_day": "rain_street", "street_night": "rain_street", "walking": "rain_street",
              "running": "rain_street", "rooftop": "rain_roof", "gliding": "rain_roof", "park": "rain_street",
              "docks": "rain_street"}

# What they're doing, said any of the ways it gets said, to a kind of place.
_DOING = [
    (r"shower|bath\b|bathing", "shower"),
    (r"cook\w*|making (dinner|lunch|breakfast)|kitchen|baking|chopping", "kitchen"),
    (r"grappl\w*|glid\w*|swinging|on the rooftops|rooftop run|parkour", "gliding"),
    (r"\b(run|running|jog\w*|sprint\w*)\b", "running"),
    (r"\b(walk\w*|stroll\w*|on foot|heading home)\b", "walking"),
    (r"ballet|dance class|barre|dancing alone", "studio"),
    (r"grocer|supermarket|shopping|groceries", "grocery"),
    (r"coffee|caf[eé]|espresso|brunch", "cafe"),
    (r"\b(bar|drinks?|club|pub|nightclub|party)\b", "bar"),
    (r"\b(gym|training|sparring|workout|boxing|heavy bag)\b", "gym"),
    (r"stake ?out|patrol|rooftop|swinging|on the beat", "rooftop"),
    (r"\bcave\b", "cave"),
    (r"\b(lab|workshop|tinker\w*|r&d|server|code|coding|hack\w*|clock ?tower|oracle)\b", "lab"),
    (r"hospital|clinic|surgery|ward\b|shift at", "hospital"),
    (r"\b(class|lecture|campus|seminar|studying|library)\b", "campus"),
    (r"\b(office|meeting|board|paperwork|emails?)\b", "office"),
    (r"precinct|gcpd|station house|bullpen", "police"),
    (r"garden\w*|roses|pruning", "garden"),
    (r"\bpark\b", "park"),
    (r"asleep|sleep\w*|in bed|napping", "home"),
]
# The map's icon for where they are, to a kind of place.
_ICON = {"manor": "manor", "police": "police", "hospital": "hospital", "lab": "lab", "clock": "lab",
         "university": "campus", "school": "campus", "diner": "cafe", "bar": "bar", "club": "bar",
         "nightlife": "bar", "dock": "docks", "marina": "docks", "park": "park", "garden": "garden",
         "gym": "gym", "tower": "office", "home": "home", "market": "street_day", "station": "street_day"}
_BY = {"driving": "car", "on the subway": "subway", "over the rooftops": "rooftop", "on the bike": "rooftop"}

_locks, _failed = {}, {}
_guard = threading.Lock()


def available():
    return bool(config.AMBIENCE and config.ELEVENLABS_API_KEY)


def scene_for(contact, t=None):
    """
    The kind of place they're in right now, from their day — and, out in the
    open, the weather over it: a rooftop in the rain is rain on a rooftop.
    """
    from ..engine import world
    scene = _scene(contact, t)
    sky = world.sky()
    if sky == "storm" and scene in _WEATHERED:
        return "storm"
    if sky == "snow" and scene in _WEATHERED:
        return "snow"
    if sky == "rain" and scene in _WEATHERED:
        return _WEATHERED[scene]
    return scene


def _scene(contact, t=None):
    """The kind of place they're in right now, from their day — or "" for nothing worth hearing."""
    from ..engine import cases, places, presence
    t = t or time.time()
    whereabouts = presence.of(contact)
    case = cases.active(contact.id) if contact.id in cases.FIELD else None
    if case:
        stage = cases.phase(case, t)[0]
        if stage in ("in it", "gone wrong"):
            return "scuffle"
        if stage in ("arriving", "wrapping up", "en route"):
            return "rooftop" if getattr(contact, "beat", None) else "street_night"
    trip = whereabouts.get("trip") or {}
    if trip.get("end", 0) > t and len(trip.get("pts") or []) > 1:
        by = trip.get("by") or ""
        if by in _BY:
            return _BY[by]
        return "street_night" if _night(t) else "street_day"
    doing = (whereabouts.now(t).get("doing") or "").lower()
    for pattern, scene in _DOING:
        if re.search(pattern, doing):
            return scene
    where, _ = whereabouts.whereabouts(t)
    home = getattr(contact, "home", "") or ""
    spot = places.resolve(where)
    data = places.gazetteer()
    icon = next((p.get("icon") for p in data["places"] if spot and p["name"] == spot["name"]), "") or ""
    if icon in _ICON:
        return _ICON[icon]
    if where and (where == home or "home" in where.lower()):
        return getattr(contact, "home_sound", "") or ("manor" if "manor" in where.lower() else "home")
    return ("street_night" if _night(t) else "street_day") if where else ""


def _night(t):
    hour = time.localtime(t).tm_hour
    return hour >= 20 or hour < 6


def shots_for(scene):
    """The one-off sounds over a scene: [{"name", "every"}] — seconds between, on average."""
    return [{"name": name, "every": every} for name, every in SCENE_SHOTS.get(scene, [])]


def path(scene):
    return paths.DATA_DIR / "ambience" / f"{scene}.mp3"


def clip(scene):
    """
    The loop for a scene — or a one-off sound (`ONE_SHOTS`) — made the first
    time it's wanted and kept: a path, or None — no key, switched off, an
    unknown name, or ElevenLabs saying no (asked again only after a while).
    Blocking: run off the event loop.
    """
    shot = scene in ONE_SHOTS
    if (scene not in SCENES and not shot) or not available():
        return None
    target = path(scene)
    if target.exists() and target.stat().st_size > 1000:
        return target
    with _guard:
        lock = _locks.setdefault(scene, threading.Lock())
    with lock:
        if target.exists() and target.stat().st_size > 1000:
            return target
        if time.time() - _failed.get(scene, 0) < 600:
            return None
        try:
            response = requests.post(
                "https://api.elevenlabs.io/v1/sound-generation",
                params={"output_format": "mp3_44100_128"},
                json=({"text": ONE_SHOTS[scene] + " No music.", "duration_seconds": 4, "loop": False,
                       "prompt_influence": 0.55, "model_id": "eleven_text_to_sound_v2"} if shot else
                      {"text": SCENES[scene] + _TAIL, "duration_seconds": LOOP_SECONDS, "loop": True,
                       "prompt_influence": 0.45, "model_id": "eleven_text_to_sound_v2"}),
                headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                timeout=60)
            if response.status_code != 200 or len(response.content) < 1000:
                raise RuntimeError(f"{response.status_code}: {response.text[:160]}")
        except Exception as exc:
            _failed[scene] = time.time()
            log.warning("ambience %s couldn't be made: %s", scene, str(exc)[:200])
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        log.info("ambience %s made (%d KB)", scene, len(response.content) // 1024)
        return target
