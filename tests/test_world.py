"""Ambient feeds: what they say, that they never block, and when they save a search."""
import time

import pytest

from wayne.engine import prompting, world

SAMPLE = {
    "current": {"temperature_2m": 11.4, "apparent_temperature": 8.2,
                "weather_code": 61, "wind_speed_10m": 7.6},
    "daily": {"weather_code": [63, 3], "temperature_2m_min": [9.1, 8.0],
              "temperature_2m_max": [15.6, 14.2],
              "precipitation_probability_max": [90, 10]},
}


def _ready(feed, text):
    feed.text, feed.fetched = text, time.time()
    return feed


@pytest.fixture
def weather(monkeypatch):
    feed = _ready(world.Weather("London"), world.describe_weather(SAMPLE, "London"))
    monkeypatch.setattr(world, "FEEDS", [feed])
    return feed


def test_a_reading_is_described_in_plain_words():
    text = world.describe_weather(SAMPLE, "London")
    assert "light rain, 11°C" in text
    assert "tomorrow overcast" in text


def test_no_feeds_means_nothing_said_and_nothing_covered(monkeypatch):
    monkeypatch.setattr(world, "FEEDS", [])
    assert world.snapshot() == []
    assert not world.covered("What's the weather like?")


def test_the_reading_carries_its_time(weather):
    assert "(as of" in world.snapshot()[0]


def test_a_stale_feed_refreshes_in_the_background_without_waiting(monkeypatch):
    class Slow(world.Feed):
        label = "Slow"
        def fetch(self):
            time.sleep(2)
            return "fresh"
    feed = Slow()
    monkeypatch.setattr(world, "FEEDS", [feed])
    started = time.time()
    assert world.snapshot() == []          # nothing cached yet, and no wait
    assert time.time() - started < 0.2


def test_local_weather_questions_need_no_search(weather):
    assert world.covered("What's the weather like?")
    assert world.covered("Is it going to rain in London tomorrow?")
    assert world.covered("Alfred, do I need an umbrella today?", ("Alfred",))


def test_weather_somewhere_else_still_searches(weather):
    assert not world.covered("What's the weather in Paris?")


def test_feeds_reach_the_model_framed_as_feeds(weather):
    block = prompting.reference_block("", "Evening.")
    assert "Weather feed" in block
    assert "light rain" in block


def test_calendar_events_read_as_a_day():
    now = time.mktime((2026, 10, 7, 13, 30, 0, 0, 0, -1))

    def at(h, m, day=7):
        return time.mktime((2026, 10, day, h, m, 0, 0, 0, -1))

    text = world.describe_events([
        (at(15, 0), at(16, 0), "Dentist", False),
        (at(13, 0), at(14, 0), "Standup", False),
        (at(9, 30, 8), at(10, 0, 8), "Flight", False),
        (at(0, 0), at(0, 0, 8), "Mum's birthday", True),
    ], now)
    assert "now, until 14:00: Standup" in text
    assert "today 15:00: Dentist" in text
    assert "tomorrow 09:30: Flight" in text
    assert "today all day: Mum's birthday" in text


def test_an_empty_calendar_says_so():
    assert "nothing scheduled" in world.describe_events([], time.time())
