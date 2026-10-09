"""
What is true outside the conversation right now.

A person on the other end of a line knows what the weather is doing where you
are and what is on the news, if they have looked. A model does not, and asked
to sound like it does it will supply some — "filthy out there tonight" on a
clear evening is the same invention as any other, only about the sky. So the
facts are fetched, and he is handed them with a time on them, or he has nothing
to say about them at all.

Two ways a fact reaches him, and the split is the whole design:

  Ambient feeds (this module) — a small, fixed set of things worth knowing
  before anyone asks: weather, headlines. Fetched on a schedule on background
  threads and cached; a turn reads whatever is cached and never waits. Each
  costs a line or two of prompt, so the list stays short on purpose.

  On-demand lookups (search.py) — everything else in the world. Fetched only
  when a question needs it, because no feed can anticipate "who is Waylon
  Jones?" and pre-fetching the world would bury every turn in text.

Adding a feed is a subclass with `fetch()` and an interval; calendars,
reminders and the like belong here once they exist. Feeds are opt-in through
.env and make no requests when unset.
"""
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

from .. import config

_TIMEOUT = 4


class Feed:
    """One ambient source: fetched in the background, read from cache."""

    name = ""
    label = ""
    interval = 15 * 60

    def __init__(self):
        self._lock = threading.Lock()
        self.text = ""
        self.fetched = 0.0
        self._refreshing = False

    def fetch(self):
        """Return the current reading as plain text, or raise."""
        raise NotImplementedError

    def covers(self, prompt, names=()):
        """True when this feed already answers the question, so no search."""
        return False

    def refresh(self):
        try:
            text = self.fetch()
            if text:
                with self._lock:
                    self.text, self.fetched = text, time.time()
        except Exception:
            pass  # keep the last reading; it carries its own time
        finally:
            self._refreshing = False

    def read(self):
        """The cached reading, kicking off a refresh if stale. Never blocks."""
        with self._lock:
            if time.time() - self.fetched > self.interval and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self.refresh, daemon=True).start()
            if not self.text:
                return ""
            stamp = time.strftime("%H:%M", time.localtime(self.fetched))
            return f"{self.label} (as of {stamp}): {self.text}"


# --- weather -----------------------------------------------------------------

# WMO weather interpretation codes, as Open-Meteo reports them, in the words a
# person would use.
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

_WEATHER_WORDS = re.compile(
    r"\b(weather|forecast|temperature|rain(ing|y)?|umbrella|cold|warm|hot|"
    r"snow(ing)?|sunny|windy|degrees)\b", re.I)
# Words that start with a capital without naming a place.
_NOT_PLACES = {"i", "im", "i'm", "is", "what", "whats", "what's", "how", "hows", "how's",
               "will", "should", "do", "does", "can", "today", "tomorrow", "tonight",
               "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def describe_weather(data, place):
    """Open-Meteo's response as plain words: now, and the next two days."""
    current, daily = data.get("current", {}), data.get("daily", {})
    now = (f"{_CONDITIONS.get(current.get('weather_code'), 'unsettled')}, "
           f"{round(current.get('temperature_2m', 0))}°C "
           f"(feels {round(current.get('apparent_temperature', 0))}°C), "
           f"wind {round(current.get('wind_speed_10m', 0))} mph")
    days = []
    for i, label in enumerate(("today", "tomorrow")):
        try:
            days.append(
                f"{label} {_CONDITIONS.get(daily['weather_code'][i], 'unsettled')}, "
                f"{round(daily['temperature_2m_min'][i])}–{round(daily['temperature_2m_max'][i])}°C, "
                f"{daily['precipitation_probability_max'][i]}% chance of rain")
        except (KeyError, IndexError, TypeError):
            break
    return f"{place} now: {now}." + (f" Forecast: {'; '.join(days)}." if days else "")


class Weather(Feed):
    """Open-Meteo, for WAYNE_LOCATION. Free and keyless."""

    name = "weather"
    # Framed as a reading from a feed, not a view out of a window: it says
    # nothing about whether the operator is indoors, out in it, or has noticed.
    label = ("Weather feed for where he is (says nothing about whether he is "
             "indoors or out)")

    def __init__(self, place):
        super().__init__()
        self.place = place
        self._coords = None

    def fetch(self):
        if self._coords is None:
            response = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": self.place, "count": 1}, timeout=_TIMEOUT)
            response.raise_for_status()
            found = (response.json().get("results") or [None])[0]
            if not found:
                return ""
            self._coords = (found["latitude"], found["longitude"], found.get("name", self.place))
        lat, lon, name = self._coords
        name = config.LOCATION_NAME or name
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "wind_speed_unit": "mph",
            },
            timeout=_TIMEOUT)
        response.raise_for_status()
        return describe_weather(response.json(), name)

    def covers(self, prompt, names=()):
        # Only for here: "weather in Paris" names somewhere else and still
        # goes to a search.
        if not (self.text and _WEATHER_WORDS.search(prompt or "")):
            return False
        here = {w.lower() for w in re.findall(r"[\w']+", self.place)}
        skip = _NOT_PLACES | here | {n.lower() for n in names}
        capitals = re.findall(r"\b[A-Z][\w']+", prompt)
        return not [c for c in capitals if c.lower() not in skip]


# --- headlines ---------------------------------------------------------------

class Headlines(Feed):
    """Top stories from any RSS feed (WAYNE_NEWS_FEED). Titles only."""

    name = "headlines"
    label = ("Top headlines on your terminal (for awareness; raise one only if "
             "relevant or he asks)")
    interval = 30 * 60
    count = 5

    def __init__(self, url):
        super().__init__()
        self.url = url

    def fetch(self):
        response = requests.get(self.url, timeout=_TIMEOUT,
                                headers={"User-Agent": "WayneTech-Console/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        titles = [(item.findtext("title") or "").strip()
                  for item in root.iter("item")][:self.count]
        return " | ".join(t for t in titles if t)


# --- calendar ----------------------------------------------------------------

def describe_events(events, now):
    """
    (start, end, title, all_day) tuples as one line, grouped by day. Times are
    local; anything already under way says so, because "you're meant to be in
    a meeting right now" and "you have a meeting at three" are different news.
    """
    today = time.localtime(now).tm_yday
    parts = []
    for start, end, title, all_day in sorted(events):
        day = "today" if time.localtime(start).tm_yday == today else "tomorrow"
        if all_day:
            parts.append(f"{day} all day: {title}")
        elif start <= now < end:
            parts.append(f"now, until {time.strftime('%H:%M', time.localtime(end))}: {title}")
        else:
            parts.append(f"{day} {time.strftime('%H:%M', time.localtime(start))}: {title}")
    return "; ".join(parts) if parts else "nothing scheduled today or tomorrow"


class Calendar(Feed):
    """
    macOS Calendar via EventKit — every account Calendar knows about (iCloud,
    Google, Exchange). WAYNE_CALENDAR=1 to enable. The first fetch asks for
    permission; the titles stay on this machine except for whatever he says
    aloud.
    """

    name = "calendar"
    label = ("His calendar, today and tomorrow (from the calendar, not from him — "
             "he may have moved things; ask rather than assume)")
    interval = 5 * 60

    def __init__(self):
        super().__init__()
        self._store = None

    def _authorised(self):
        import EventKit
        if self._store is None:
            self._store = EventKit.EKEventStore.alloc().init()
        status = EventKit.EKEventStore.authorizationStatusForEntityType_(
            EventKit.EKEntityTypeEvent)
        if status == 3:   # EKAuthorizationStatusFullAccess
            return True
        if status != 0:   # denied, restricted or write-only: asking again won't help
            return False
        answered = threading.Event()
        granted = []
        self._store.requestFullAccessToEventsWithCompletion_(
            lambda ok, _error: (granted.append(bool(ok)), answered.set()))
        answered.wait(120)
        return bool(granted and granted[0])

    def fetch(self):
        if not self._authorised():
            return ""
        import Foundation
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        until = midnight + 2 * 86400
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            Foundation.NSDate.dateWithTimeIntervalSince1970_(now),
            Foundation.NSDate.dateWithTimeIntervalSince1970_(until), None)
        events = [
            (e.startDate().timeIntervalSince1970(), e.endDate().timeIntervalSince1970(),
             str(e.title() or "untitled"), bool(e.isAllDay()))
            for e in self._store.eventsMatchingPredicate_(predicate)
        ]
        return describe_events(events, now)


# --- the set -----------------------------------------------------------------

def _build():
    feeds = []
    if config.LOCATION:
        feeds.append(Weather(config.LOCATION))
    if config.NEWS_FEED:
        feeds.append(Headlines(config.NEWS_FEED))
    if config.CALENDAR:
        feeds.append(Calendar())
    return feeds


FEEDS = _build()


def snapshot():
    """Every feed that has a reading, one line each. Never blocks."""
    return [line for line in (feed.read() for feed in FEEDS) if line]


def covered(prompt, names=()):
    """True when an ambient feed already answers this, so no search is needed."""
    return any(feed.covers(prompt, names) for feed in FEEDS)


def prime():
    """Fetch every feed now, in parallel — at start-up, so the first call has them."""
    threads = [threading.Thread(target=feed.refresh, daemon=True) for feed in FEEDS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_TIMEOUT * 2 + 1)
