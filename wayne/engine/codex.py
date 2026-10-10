"""
The Codex: everyone and everywhere, in one place he can look things up — the
family and the people round them, the rogues he's crossed paths with, every
place on the map.

Only his contacts are in his phone. Everyone else in it is reached the way
anyone reaches someone they don't have a number for: through someone who
does — Barbara for her father, Cass for Steph, Alfred for Leslie (`reach`).

A place's description can be rewritten here, and the city remembers it: the
edit is what everyone who knows Gotham knows of it from then on (see
places.note). Rogues are marked once he's come across them — opened a report
with their work in it, or put someone on one.
"""
import json
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from .. import paths
from ..memory.store import atomic_write, read_text

_lock = threading.Lock()


@lru_cache(maxsize=1)
def _data():
    return json.loads(Path(__file__).with_name("codex.json").read_text())


def _encountered_path():
    return paths.DATA_DIR / "_encountered.json"


def encountered():
    try:
        return json.loads(read_text(_encountered_path()) or "{}")
    except ValueError:
        return {}


def encounter(name, how):
    """He's come across this rogue's work: when, and how."""
    if not name:
        return
    with _lock:
        seen = encountered()
        if name not in seen:
            seen[name] = {"at": time.time(), "how": how}
            atomic_write(_encountered_path(), json.dumps(seen, ensure_ascii=False))


def people(directory):
    """The people of the Codex, each with whether he can ring them, and through whom if not."""
    mine, framed = notes(), frames()
    out = []
    for person in _data()["people"]:
        entry = dict(person)
        contact = directory.get(person.get("contact") or "")
        entry["callable"] = contact is not None
        entry["reach"] = [{"id": cid, "name": directory.get(cid).name} for cid in person.get("reach", [])
                          if directory.get(cid)]
        entry["portrait"] = portrait_of(person["id"], person.get("contact"))
        entry["frame"] = framed.get(frame_key(person["id"], person.get("contact") if contact is not None else None), {})
        entry["note"] = mine.get(f"people:{person['name']}", "")
        entry["bio"] = mine.get(f"bio:people:{person['name']}") or person.get("bio", "")
        entry["edited"] = f"bio:people:{person['name']}" in mine
        if contact is not None:
            # The personnel file he keeps on them — the same words the directory shows.
            entry["dossier"] = (read_text(paths.bio_file(contact.id)) or contact.bio or "").strip()
        out.append(entry)
    return out


def rogues():
    """The rogues, as the scanner knows them, with what's on file — and whether he's crossed paths with them."""
    from . import incidents
    from . import places as gazetteer
    stats, seen, mine, framed = _data()["rogues"], encountered(), notes(), frames()
    out = []
    for rogue in incidents.rogues():
        now = where(rogue)
        held = gazetteer.resolve(now["held"]) if now["held"] else None
        haunt = gazetteer.resolve(rogue["haunts"][0]) if rogue.get("haunts") else None
        spot = held or haunt
        rid = re.sub(r"[^a-z0-9]+", "-", rogue["name"].lower()).strip("-")
        out.append({**rogue, **stats.get(rogue["name"], {}), "id": rid, "status": now["status"], "held": now["held"],
                    "status_how": now.get("how", ""), "status_since": now.get("since"),
                    "x": spot["x"] if spot else None, "y": spot["y"] if spot else None,
                    "encountered": rogue["name"] in seen,
                    "encountered_how": (seen.get(rogue["name"]) or {}).get("how", ""),
                    "portrait": portrait_of(rid), "frame": framed.get(frame_key(rid), {}),
                    "note": mine.get(f"rogues:{rogue['name']}", "")})
        bio = mine.get(f"bio:rogues:{rogue['name']}")
        if bio:
            out[-1].update(bio=bio, edited=True)
    return out


def places():
    """Every place on the map and every venue, with his own descriptions where he's written them."""
    from . import places as gazetteer
    edits, mine = gazetteer.edits(), notes()
    out = []
    sketches = gazetteer.gazetteer().get("areas", {})
    for p in gazetteer.gazetteer()["places"]:
        entry = {k: p.get(k) for k in ("name", "kind", "area", "icon", "bio", "note", "canon", "x", "y")}
        if not entry.get("bio") and p["name"] in sketches:
            entry["bio"] = sketches[p["name"]]             # a district: its sketch
        if p["name"] in edits:
            entry["bio"], entry["edited"] = edits[p["name"]], True
        if f"places:{p['name']}" in mine:
            entry["note"] = mine[f"places:{p['name']}"]
        out.append(entry)
    for v in gazetteer.venues():
        out.append({"name": v["name"], "kind": "venue", "icon": v["kind"], "area": v["area"], "x": v["x"], "y": v["y"],
                    "bio": edits.get(v["name"], v.get("bio", "")), "edited": v["name"] in edits, "venue": v["kind"],
                    "note": mine.get(f"places:{v['name']}", "")})
    return out


# --- where the rogues are: the city moves on ----------------------------------------
#
# rogues.json says where everyone stands to begin with. After that Gotham's revolving
# door turns on its own: someone the family catches is in GCPD custody, transferred a
# day later — to Arkham if they're the asylum's kind, Blackgate if they're not — and
# now and then someone breaks out, and GCPD now and then picks them up again. Rolled a
# day at a time from each rogue's own seed, so it's the same city however often it's asked.

TRANSFER = 24 * 3600
ESCAPES = {"Arkham Asylum": 0.025, "Blackgate Penitentiary": 0.012}
RECAPTURED = 0.06          # a day, for someone loose who isn't anyone's case
_cache = {"mtime": None, "data": {}}


def _status_path():
    return paths.DATA_DIR / "_rogue_status.json"


def _statuses():
    path = _status_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            _cache["data"] = json.loads(path.read_text() or "{}")
        except ValueError:
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def _save_statuses(data):
    atomic_write(_status_path(), json.dumps(data, ensure_ascii=False))
    _cache["mtime"] = None


def _roll(name, day, what):
    import hashlib
    return int(hashlib.sha1(f"{name}:{day}:{what}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def capture(name, how):
    """The family caught them: in GCPD custody now, to be transferred."""
    with _lock:
        data = dict(_statuses())
        data[name] = {"status": "in custody", "held": "GCPD Central", "since": time.time(), "how": how}
        _save_statuses(data)
    encounter(name, how)


def where(rogue, now=None):
    """
    {"status", "held", "since", "how"} for a rogue right now — custody, a cell,
    loose — moving them on as the days have passed since they were last looked at.
    """
    now = now or time.time()
    stats = _data()["rogues"].get(rogue["name"], {})
    kept = _statuses().get(rogue["name"])
    state = dict(kept) if kept else {"status": rogue["status"], "held": rogue.get("held", ""),
                                      "since": now - 30 * 86400, "how": ""}
    changed = False
    if state["status"] == "in custody" and now - state["since"] > TRANSFER:
        held = "Arkham Asylum" if stats.get("asylum") else "Blackgate Penitentiary"
        state = {"status": "locked up", "held": held, "since": state["since"] + TRANSFER, "how": f"transferred to {held}"}
        changed = True
    # A day at a time since: the odd breakout, the odd recapture.
    day0, day1 = int(state["since"] // 86400) + 1, int(now // 86400)
    for day in range(max(day0, day1 - 60), day1 + 1):
        if state["status"] == "locked up" and _roll(rogue["name"], day, "out") < ESCAPES.get(state["held"], 0.0):
            state = {"status": "at large", "held": "", "since": day * 86400.0, "how": f"broke out of {state['held']}"}
            changed = True
        elif state["status"] == "at large" and kept and _roll(rogue["name"], day, "in") < RECAPTURED:
            held = "Arkham Asylum" if stats.get("asylum") else "Blackgate Penitentiary"
            state = {"status": "locked up", "held": held, "since": day * 86400.0, "how": f"picked up by GCPD, sent to {held}"}
            changed = True
    if changed or (kept is None and state["status"] != rogue["status"]):
        with _lock:
            data = dict(_statuses())
            data[rogue["name"]] = state
            _save_statuses(data)
    return state


def rogue_status(name, now=None):
    """Just the status word for a rogue by name — "at large", "in custody", "locked up", "unknown"."""
    from . import incidents
    rogue = next((g for g in incidents.rogues() if g["name"] == name), None)
    return where(rogue, now)["status"] if rogue else "unknown"


# --- his own notes ------------------------------------------------------------------

def _notes_path():
    return paths.DATA_DIR / "_codex_notes.json"


def notes():
    try:
        return json.loads(read_text(_notes_path()) or "{}")
    except ValueError:
        return {}


def write_bio(kind, name, text):
    """He rewrites what's on file for someone — `kind` people or rogues. Empty puts back the file as it was."""
    write_note(f"bio:{kind}", name, text, limit=4000)


def write_note(kind, name, text, limit=2000):
    """His note on someone or somewhere — `kind` is people, rogues or places. Empty puts back the default."""
    with _lock:
        data = notes()
        key = f"{kind}:{name}"
        if (text or "").strip():
            data[key] = text.strip()[:limit]
        else:
            data.pop(key, None)
        atomic_write(_notes_path(), json.dumps(data, ensure_ascii=False))


# --- faces ------------------------------------------------------------------------

PORTRAITS = Path(__file__).resolve().parents[2] / "web" / "portraits"


def portrait_of(entry_id, contact_id=None):
    """
    The image for someone, if one's been added: a contact's own, else the
    Codex's — its address stamped with when it was saved, so a new picture is
    never shown as the browser's cached old one.
    """
    for folder, name in ((PORTRAITS, contact_id), (PORTRAITS / "codex", entry_id)):
        if not name:
            continue
        for ext in ("png", "jpg", "webp"):
            found = folder / f"{name}.{ext}"
            if found.exists():
                rel = "" if folder == PORTRAITS else "codex/"
                return f"/static/portraits/{rel}{name}.{ext}?v={int(found.stat().st_mtime)}"
    return ""


def portrait_version(contact_id):
    """When a contact's picture was last saved — for the page's cache-busting — or 0."""
    for ext in ("png", "jpg", "webp"):
        found = PORTRAITS / f"{contact_id}.{ext}"
        if found.exists():
            return int(found.stat().st_mtime)
    return 0


def _frames_path():
    return paths.DATA_DIR / "_portrait_frames.json"


def frames():
    """How each face is framed, as he's set it: {key: {"size": "180%", "position": "48% 16%"}}."""
    try:
        return json.loads(read_text(_frames_path()) or "{}")
    except ValueError:
        return {}


def frame_key(entry_id, contact_id=None):
    return contact_id or f"codex:{entry_id}"


def set_frame(key, size, position):
    """Keep his framing for a face; empty puts back the default."""
    with _lock:
        data = frames()
        if size and position:
            data[key] = {"size": size, "position": position}
        else:
            data.pop(key, None)
        atomic_write(_frames_path(), json.dumps(data, ensure_ascii=False))


def frame_for(contact):
    """A contact's framing as he set it in the Codex; none, and the picture fills the circle."""
    return frames().get(contact.id) or {}


def save_portrait(entry_id, data, ext, contact_id=None):
    """Keep an image for someone: a contact's goes where the directory looks, anyone else's in codex/."""
    folder, name = (PORTRAITS, contact_id) if contact_id else (PORTRAITS / "codex", entry_id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in ("png", "jpg", "webp"):
        (folder / f"{name}.{old}").unlink(missing_ok=True)
    (folder / f"{name}.{ext}").write_bytes(data)
    set_frame(frame_key(entry_id, contact_id), "", "")
    return portrait_of(entry_id, contact_id)
