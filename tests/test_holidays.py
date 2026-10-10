"""The year as Gotham lives it."""
import datetime

from wayne.engine import holidays


def test_the_movable_ones_land_where_they_should():
    year = holidays.days(2026)
    assert year[datetime.date(2026, 11, 26)][0] == "Thanksgiving"       # fourth Thursday
    assert year[datetime.date(2026, 10, 12)][0] == "Columbus Day"       # second Monday
    assert year[datetime.date(2026, 4, 5)][0] == "Easter Sunday"
    assert year[datetime.date(2026, 5, 25)][0] == "Memorial Day"        # last Monday
    assert year[datetime.date(2026, 11, 3)][0] == "Election Day"


def test_a_day_knows_its_place_in_the_year():
    assert holidays.note(datetime.date(2026, 10, 10)) == "a long weekend (Columbus Day on Monday)"
    assert holidays.note(datetime.date(2026, 10, 12)).startswith("it's Columbus Day — no school")
    assert holidays.note(datetime.date(2026, 10, 24)) == "Halloween in a week"
    assert holidays.note(datetime.date(2026, 3, 4)) == ""
