"""Calendar trip adapter: calendar events with a location → km and away hours per date.

Pure Python. The coordinator reads the events (``calendar.get_events``) and
looks up the legs between home and the locations with a routing provider;
this module decides which events are car trips, which legs are needed and
turns events + legs into planner ``DayInfo`` objects (docs/DESIGN.md §3.1).

Rules:

* A **car trip** is an event with a location, unless the location, title or
  description contains an ignore word (online meetings, ``#tog`` …) or the
  location contains a home word or lies within ``home_radius_km`` of home.
* Timed events of a date are driven in order. Between two events the car
  goes home when that leaves at least ``min_home_minutes`` at home,
  otherwise it drives directly to the next location.
* The car is away from (first start − travel there) until (last end +
  travel home) of every block of events driven without going home.
* An all-day event with a location: away ``all_day_hours`` (06–20) on every
  date it spans, with a round trip on each.
* A timed event over midnight (e.g. a hotel stay) is away overnight: the
  nights it spans are away nights.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ..planner.models import DayInfo

HOME = "__home__"

DEFAULT_IGNORE_WORDS: tuple[str, ...] = (
    "teams",
    "zoom",
    "meet.google",
    "webex",
    "online",
    "http://",
    "https://",
    "#ingenbil",
    "#tog",
)
DEFAULT_TRAVEL_MINUTES = 30.0  # when a leg could not be routed


@dataclass(frozen=True)
class CalendarEvent:
    """A calendar event (times are timezone-aware, local)."""

    start: datetime
    end: datetime
    location: str
    summary: str = ""
    description: str = ""
    all_day: bool = False


@dataclass(frozen=True)
class Leg:
    """A driven leg: road distance and driving time."""

    km: float
    minutes: float


@dataclass(frozen=True)
class CalendarRules:
    home_words: tuple[str, ...] = ()
    ignore_words: tuple[str, ...] = DEFAULT_IGNORE_WORDS
    min_home_minutes: float = 60.0
    all_day_hours: tuple[int, int] = (6, 20)
    home_radius_km: float = 1.0


@dataclass
class DayTrips:
    """Car trips of one date."""

    km: float = 0.0
    away: list[tuple[int, int]] = field(default_factory=list)
    away_overnight: bool = False
    stops: list[dict] = field(default_factory=list)


def split_words(text: str | None) -> tuple[str, ...]:
    """Comma separated words → lower-case tuple (empty entries dropped)."""
    return tuple(w.strip().lower() for w in (text or "").split(",") if w.strip())


def _parse_time(value: object, tz) -> tuple[datetime | None, bool]:
    """A calendar start/end → (local datetime, all-day)."""
    if isinstance(value, datetime):
        return (value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)), False
    if isinstance(value, date):
        return datetime.combine(value, time(), tz), True
    text = str(value or "").strip()
    if not text:
        return None, False
    if "T" not in text and len(text) == 10:
        try:
            return datetime.combine(date.fromisoformat(text), time(), tz), True
        except ValueError:
            return None, False
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    return (dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)), False


def parse_events(raw: Iterable[Mapping], tz) -> list[CalendarEvent]:
    """``calendar.get_events`` response events → ``CalendarEvent`` (invalid ones skipped)."""
    out: list[CalendarEvent] = []
    for ev in raw:
        start, all_day = _parse_time(ev.get("start"), tz)
        end, _ = _parse_time(ev.get("end"), tz)
        if start is None or end is None or end < start:
            continue
        out.append(
            CalendarEvent(
                start=start,
                end=end,
                location=str(ev.get("location") or "").strip(),
                summary=str(ev.get("summary") or ""),
                description=str(ev.get("description") or ""),
                all_day=all_day,
            )
        )
    return sorted(out, key=lambda e: e.start)


def car_events(events: Iterable[CalendarEvent], rules: CalendarRules) -> list[CalendarEvent]:
    """Events that are car trips by their texts (the home radius is checked in ``plan_trips``)."""
    out = []
    for ev in events:
        if not ev.location:
            continue
        loc = ev.location.lower()
        texts = f"{loc} {ev.summary.lower()} {ev.description.lower()}"
        if any(w in texts for w in rules.ignore_words):
            continue
        if any(w in loc for w in rules.home_words):
            continue
        out.append(ev)
    return out


def _timed_by_date(events: Iterable[CalendarEvent]) -> dict[date, list[CalendarEvent]]:
    out: dict[date, list[CalendarEvent]] = {}
    for ev in events:
        if not ev.all_day:
            out.setdefault(ev.start.date(), []).append(ev)
    for evs in out.values():
        evs.sort(key=lambda e: e.start)
    return out


def legs_needed(events: Iterable[CalendarEvent]) -> set[tuple[str, str]]:
    """Every leg ``plan_trips`` may look up: home ↔ each location, and consecutive locations of a date."""
    evs = list(events)
    out: set[tuple[str, str]] = set()
    for ev in evs:
        out.add((HOME, ev.location))
        out.add((ev.location, HOME))
    for day in _timed_by_date(evs).values():
        for a, b in zip(day, day[1:], strict=False):
            if a.location != b.location:
                out.add((a.location, b.location))
    return out


def _ceil_hour(dt: datetime) -> tuple[date, int]:
    """(date, hour) of the end of the hour containing ``dt`` (24 = midnight)."""
    if dt.minute or dt.second or dt.microsecond:
        return dt.date(), dt.hour + 1
    return (dt.date(), dt.hour) if dt.hour else (dt.date() - timedelta(days=1), 24)


def _add_interval(trips: dict[date, DayTrips], leave: datetime, back: datetime) -> None:
    """Mark the hours from ``leave`` until ``back`` as away, split at midnight."""
    end_day, end_hour = _ceil_hour(back)
    d = leave.date()
    start_hour = leave.hour
    while d <= end_day:
        last = end_hour if d == end_day else 24
        if last > start_hour:
            trips.setdefault(d, DayTrips()).away.append((start_hour, last))
        d += timedelta(days=1)
        start_hour = 0


def plan_trips(
    events: Iterable[CalendarEvent],
    legs: Mapping[tuple[str, str], Leg],
    rules: CalendarRules,
) -> tuple[dict[date, DayTrips], set[tuple[str, str]]]:
    """Car events + routed legs → trips per date, and the legs that were missing.

    A missing leg counts ``DEFAULT_TRAVEL_MINUTES`` and 0 km.
    """
    missing: set[tuple[str, str]] = set()

    def leg(a: str, b: str) -> Leg:
        if a == b:
            return Leg(0.0, 0.0)
        found = legs.get((a, b))
        if found is None:
            missing.add((a, b))
            return Leg(0.0, DEFAULT_TRAVEL_MINUTES)
        return found

    def at_home(ev: CalendarEvent) -> bool:
        there = legs.get((HOME, ev.location))
        return there is not None and there.km < rules.home_radius_km

    evs = [ev for ev in car_events(events, rules) if not at_home(ev)]
    trips: dict[date, DayTrips] = {}

    for d, day in sorted(_timed_by_date(evs).items()):
        blocks: list[list[CalendarEvent]] = [[day[0]]]
        for ev in day[1:]:
            prev = blocks[-1][-1]
            home_minutes = (ev.start - prev.end).total_seconds() / 60 - leg(prev.location, HOME).minutes - leg(HOME, ev.location).minutes
            if home_minutes >= rules.min_home_minutes:
                blocks.append([ev])
            else:
                blocks[-1].append(ev)
        info = trips.setdefault(d, DayTrips())
        for block in blocks:
            first, last = block[0], block[-1]
            there, back = leg(HOME, first.location), leg(last.location, HOME)
            km = there.km + back.km + sum(leg(a.location, b.location).km for a, b in zip(block, block[1:], strict=False))
            info.km += km
            leave = first.start - timedelta(minutes=there.minutes)
            home = max(e.end for e in block) + timedelta(minutes=back.minutes)
            _add_interval(trips, leave, home)
            # A stay over midnight: the car is away the nights in between.
            n = leave.date()
            while n < home.date():
                trips.setdefault(n, DayTrips()).away_overnight = True
                n += timedelta(days=1)
            for e in block:
                info.stops.append({"start": e.start.isoformat(), "end": e.end.isoformat(), "location": e.location, "summary": e.summary})

    start_h, end_h = rules.all_day_hours
    for ev in evs:
        if not ev.all_day:
            continue
        round_trip = leg(HOME, ev.location).km + leg(ev.location, HOME).km
        d = ev.start.date()
        last = ev.end.date() - timedelta(days=1) if ev.end.date() > d else d
        while d <= last:
            info = trips.setdefault(d, DayTrips())
            info.km += round_trip
            info.away.append((start_h, end_h))
            info.stops.append({"start": d.isoformat(), "end": d.isoformat(), "location": ev.location, "summary": ev.summary})
            d += timedelta(days=1)

    for info in trips.values():
        info.away = _merge(info.away)
        info.km = round(info.km, 1)
    return trips, missing


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def build_days(
    trips: Mapping[date, DayTrips],
    away_nights: Iterable[date] = (),
    holidays: Iterable[date] = (),
) -> dict[date, DayInfo]:
    """Trips per date (+ away nights and holidays from other sources) → planner ``DayInfo``."""
    nights, hol = set(away_nights), set(holidays)
    out: dict[date, DayInfo] = {}
    for d in set(trips) | nights | hol:
        t = trips.get(d, DayTrips())
        out[d] = DayInfo(
            day=d,
            km=t.km,
            away_hours=list(t.away),
            away_overnight=t.away_overnight or d in nights,
            holiday=d in hol,
        )
    return out
