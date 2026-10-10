"""
What's new in the things each of them follows — kept current, not written in.

Every profile names a few topics its person keeps up with (Tim: game releases;
Jason: books and boxing; Alfred: the cricket). While the console is idle, one
contact at a time, those topics are searched with today's date and the model
boils the results down to what a fan of each would have seen lately: names,
titles, scores, release dates — facts, not opinions. The opinions are theirs,
made in the moment from who they are.

Nothing here is a list of films or games: run it in three months and they're
three months on. If the search comes back empty, they simply know what they
knew yesterday — a stale reading is kept, never replaced with nothing.

It reaches them where it matters and nowhere else: what's on their mind to
text about, the days they plan, and any conversation that turns to films,
games, music, books or sport — so nobody talks about the box office unprompted
on every call, and nobody invents a release that doesn't exist.
"""
import json
import random
import re
import threading
import time

import ollama

from .. import paths
from ..memory.store import atomic_write, read_text
from .search import google_search

# How long what they've seen stays fresh before it's looked for again.
STALE_AFTER = 20 * 3600
TOPICS_PER_REFRESH = 4
ITEMS_PER_TOPIC = 3

_lock = threading.Lock()

# A conversation that's turned to the things people follow.
# Words, not stems: "won't" and "lost someone" are not about the box office —
# and in Gotham "watching the docks", "the matches on the body", "fights",
# "released from Blackgate" and the console itself aren't either.
_ABOUT_CULTURE = re.compile(
    r"(?i)\b(films?|movies?|cinema|box office|trailer|series|episodes?|streaming|telly|binge\w*|"
    r"seen anything|video ?games?|gaming|ps5|xbox|nintendo|songs?|albums?|charts?|"
    r"concerts?|gig|playlist|podcasts?|novel|author|fixtures?|grand prix|"
    r"league|cricket|boxing|f1|formula one|ballet|exhibition|gallery|sotheby'?s|"
    r"jazz|theatre|theater|premiere|out now|new season|season finale)\b")
# Words too ordinary in their lives to mean they're on about a hobby.
_ORDINARY = {"there", "their", "about", "thing", "things", "where", "which", "while", "after", "night",
             "nights", "board", "crime", "crimes", "manor", "working", "watching", "gotham", "family",
             "people", "anything", "something", "around", "lately", "local", "latest", "news", "world",
             "blüdhaven", "bludhaven", "metropolis"}


def _words(text, least=5):
    return set(re.findall(rf"[^\W\d_]{{{least},}}", (text or "").lower()))     # "blüdhaven", not "dhaven"


def _their_words(contact):
    """
    Words from what they're into and follow — Jason's 'motorcycles', Dick's
    'trapeze', Alfred's 'cricket' — less the ones too ordinary in their lives
    to mean a hobby ("board", "night", "working").
    """
    interests = contact.interests or {}
    words = set()
    for key in ("follows", "pastimes", "games", "watching", "music", "reading"):
        for item in interests.get(key, []):
            words.update(_words(item) - _ORDINARY)
    return words


def _path():
    return paths.DATA_DIR / "_culture.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def seen(contact):
    """What they've seen lately in the things they follow: short factual lines, or []."""
    return list((_load().get(contact.id) or {}).get("items") or [])


def stale(contact, now=None):
    if not (contact.interests or {}).get("follows"):
        return False
    entry = _load().get(contact.id) or {}
    return (now or time.time()) - entry.get("at", 0) > STALE_AFTER


def mark_tried(contact):
    """Came back empty: keep what they had, and don't look again for a few hours."""
    with _lock:
        data = _load()
        entry = data.get(contact.id) or {"items": []}
        entry["at"] = time.time() - STALE_AFTER + 4 * 3600
        data[contact.id] = entry
        atomic_write(_path(), json.dumps(data, ensure_ascii=False, indent=1))


def about_culture(text, contact=None):
    if _ABOUT_CULTURE.search(text or ""):
        return True
    if contact is None:
        return False
    return bool(_words(text) & _their_words(contact))


def topic_for(contact, text):
    """The topic they follow that this question is about, for a better search — or ''."""
    low = set(re.findall(r"[a-z]{4,}", (text or "").lower()))
    for topic in (contact.interests or {}).get("follows", []):
        if low & set(re.findall(r"[a-z]{4,}", topic.lower())):
            return topic
    return ""


def note(contact, text):
    """
    For a turn that's about the things people follow: what they've seen
    lately, so they talk about what's real — with their own take, as a fan
    or a sceptic — or "" when it isn't that kind of turn.
    """
    if not about_culture(text, contact):
        return ""
    items = seen(contact)
    if not items:
        return ""
    # Only the few that bear on the question; a stack of headlines was recited.
    # Asked about films or games in general, the latest few; brought here only
    # by a word of theirs, only what that word is actually about.
    words = _words(text, 4) - _ORDINARY
    relevant = [i for i in items if words & _words(i, 4)]
    if not relevant and not _ABOUT_CULTURE.search(text or ""):
        return ""
    items = (relevant or items)[:4]
    return ("What you've seen lately in the things you follow (real, current — use it the way "
            "you'd actually talk about it, with your own take; don't recite it, and say you "
            "haven't seen something if you wouldn't have). Nothing beyond it is known: no results, "
            "scores or details that aren't here, and nothing that hasn't happened yet treated as "
            "if it had: " + " | ".join(items[:9]))


def pick(contact, rng=random):
    """One thing they've seen lately, for something to text about — or None."""
    items = seen(contact)
    return rng.choice(items) if items else None


def refresh(contact, when=None):
    """
    Search what they follow, as of today, and keep what a fan would know.
    Blocking (network and the model); called from a worker thread when idle.
    Returns the new items, or [] — in which case the old reading stands.
    """
    topics = list((contact.interests or {}).get("follows") or [])[:TOPICS_PER_REFRESH]
    if not topics:
        return []
    month = time.strftime("%B %Y", time.localtime(when or time.time()))
    found = []
    for topic in topics:
        try:
            results = google_search(f"{topic} latest news {month}", 5)
        except Exception:
            results = []
        snippets = [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in results if r.get("snippet")]
        if snippets:
            found.append((topic, snippets))
    if not found:
        return []
    items = []
    for topic, snippets in found:
        items += _condense(contact, topic, snippets, month)
    if items:
        with _lock:
            data = _load()
            data[contact.id] = {"at": time.time(), "items": items[:TOPICS_PER_REFRESH * ITEMS_PER_TOPIC]}
            atomic_write(_path(), json.dumps(data, ensure_ascii=False, indent=1))
    return items


def _condense(contact, topic, snippets, month):
    """Search snippets → the few current facts a follower of `topic` would know."""
    instruction = (
        f"It's {month}. These are search results about {topic}:\n" + "\n".join(snippets[:6])
        + f"\n\nList up to {ITEMS_PER_TOPIC} things someone who follows {topic} would know right now — "
        "each a short factual line that starts with what it is in brackets — [film], [show], [game], "
        "[book], [music], [sport], [event], [tech] — then the actual name or title, and whether it's "
        f"out now or when it's coming, against today ({month}): \"[film] X opened at number one this "
        "weekend\", \"[game] Y is out on 19 November\". Only what the results say; nothing older than a "
        "few months; no opinions; nothing about Batman, his family or any Gotham villain. "
        "JSON only: {\"items\": [\"...\"]}")
    try:
        reply = ollama.chat(model=contact.model, think=False, format="json",
                            options={**contact.options, "temperature": 0.2, "num_predict": 260},
                            messages=[{"role": "user", "content": instruction}])["message"]["content"]
        items = json.loads(reply).get("items") or []
    except Exception:
        return []
    return [re.sub(r"\s+", " ", str(i)).strip()[:160] for i in items if str(i).strip()][:ITEMS_PER_TOPIC]
