"""
The American year, as Gotham lives it: the holidays, the long weekends, the
days everyone's counting down to — so a Monday that's Columbus Day isn't a
school day, Christmas Eve isn't a Tuesday like any other, and Halloween in
Gotham is three weeks of people talking about Halloween.

Worked out for any year, movable feasts included; nothing to keep up to date.
"""
import datetime


def _nth(year, month, weekday, n):
    """The nth `weekday` (Mon=0) of a month; n=-1 for the last."""
    if n > 0:
        first = datetime.date(year, month, 1)
        return first + datetime.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    """Western Easter Sunday (the anonymous Gregorian computus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    return datetime.date(year, month, (h + m - 7 * n + 114) % 31 + 1)


def days(year):
    """{date: (name, off)} — `off` when most people have the day off work or school."""
    thanksgiving = _nth(year, 11, 3, 4)
    out = {
        datetime.date(year, 1, 1): ("New Year's Day", True),
        _nth(year, 1, 0, 3): ("Martin Luther King Jr. Day", True),
        datetime.date(year, 2, 14): ("Valentine's Day", False),
        _nth(year, 2, 0, 3): ("Presidents' Day", True),
        datetime.date(year, 3, 17): ("St. Patrick's Day", False),
        _easter(year): ("Easter Sunday", False),
        _nth(year, 5, 6, 2): ("Mother's Day", False),
        _nth(year, 5, 0, -1): ("Memorial Day", True),
        _nth(year, 6, 6, 3): ("Father's Day", False),
        datetime.date(year, 6, 19): ("Juneteenth", True),
        datetime.date(year, 7, 4): ("the Fourth of July", True),
        _nth(year, 9, 0, 1): ("Labor Day", True),
        _nth(year, 10, 0, 2): ("Columbus Day", True),
        datetime.date(year, 10, 31): ("Halloween", False),
        datetime.date(year, 11, 11): ("Veterans Day", False),
        thanksgiving: ("Thanksgiving", True),
        thanksgiving + datetime.timedelta(days=1): ("Black Friday", False),
        datetime.date(year, 12, 24): ("Christmas Eve", False),
        datetime.date(year, 12, 25): ("Christmas Day", True),
        datetime.date(year, 12, 31): ("New Year's Eve", False),
    }
    if year % 2 == 0:
        out[_nth(year, 11, 0, 1) + datetime.timedelta(days=1)] = ("Election Day", False)   # the Tuesday after the first Monday
    return out


# Far enough ahead that people are talking about it.
_LOOKED_FORWARD_TO = {"Halloween": 21, "Thanksgiving": 14, "Christmas Day": 24, "the Fourth of July": 7,
                      "New Year's Eve": 6, "Valentine's Day": 5}


def note(when=None):
    """
    A few words on where the day sits in the year — "Columbus Day — no school,
    banks shut", "a long weekend (Columbus Day on Monday)", "Halloween in three
    weeks" — or "" for an ordinary day.
    """
    today = (when if isinstance(when, datetime.date) else
             datetime.date.fromtimestamp(when) if when else datetime.date.today())
    calendar = {**days(today.year - 1), **days(today.year), **days(today.year + 1)}
    if today in calendar:
        name, off = calendar[today]
        return f"it's {name}" + (" — no school, most people off work" if off and today.weekday() < 5 else "")
    # A long weekend: a Monday or Friday off, seen from the weekend around it.
    for ahead in range(1, 4):
        day = today + datetime.timedelta(days=ahead)
        if day in calendar and calendar[day][1] and day.weekday() in (0, 4) and today.weekday() >= 4:
            return f"a long weekend ({calendar[day][0]} on {day.strftime('%A')})"
    for back in range(1, 3):
        day = today - datetime.timedelta(days=back)
        if day in calendar and calendar[day][1] and day.weekday() == 4 and today.weekday() >= 5:
            return f"a long weekend ({calendar[day][0]} was Friday)"
    soonest = None
    for day, (name, _) in calendar.items():
        reach = _LOOKED_FORWARD_TO.get(name)
        if reach and 0 < (day - today).days <= reach and (soonest is None or day < soonest[0]):
            soonest = (day, name)
    if soonest:
        gap = (soonest[0] - today).days
        when_ = ("tomorrow" if gap == 1 else f"on {soonest[0].strftime('%A')}" if gap < 7
                 else ("in a week" if gap < 14 else f"in {gap // 7} weeks") if gap % 7 < 4 else f"in {gap} days")
        return f"{soonest[1][0].upper() + soonest[1][1:]} {when_}"
    return ""


def today(when=None):
    """Just the holiday itself, if today is one — "Columbus Day" — or ""."""
    day = (when if isinstance(when, datetime.date) else
           datetime.date.fromtimestamp(when) if when else datetime.date.today())
    calendar = days(day.year)
    return calendar[day][0] if day in calendar else ""
