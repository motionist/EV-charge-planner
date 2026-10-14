"""Calendar trip adapter (DESIGN §3.1)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_charge_planner.adapters.calendar import (
    DEFAULT_TRAVEL_MINUTES,
    HOME,
    CalendarEvent,
    CalendarRules,
    Leg,
    build_days,
    car_events,
    clean_location,
    legs_needed,
    parse_events,
    plan_trips,
    split_words,
)

TZ = ZoneInfo("Europe/Copenhagen")
RULES = CalendarRules(home_words=("værløse",))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def ev(day: int, start: tuple[int, int], end: tuple[int, int], location: str, summary: str = "", description: str = "") -> CalendarEvent:
    return CalendarEvent(at(day, *start), at(day, *end), location, summary, description)


def legs(*items: tuple[str, str, float, float]) -> dict[tuple[str, str], Leg]:
    """Symmetric legs: (a, b, km, minutes)."""
    out = {}
    for a, b, km, minutes in items:
        out[(a, b)] = out[(b, a)] = Leg(km, minutes)
    return out


def test_parse_events_timed_and_all_day():
    raw = [
        {"start": "2026-10-15T10:00:00+02:00", "end": "2026-10-15T12:00:00+02:00", "location": " Ballerup ", "summary": "Møde"},
        {"start": "2026-10-17", "end": "2026-10-19", "location": "Aarhus", "summary": "Weekend"},
        {"start": "2026-10-16T10:00:00+02:00", "end": "2026-10-16T09:00:00+02:00", "location": "x"},  # end before start
        {"start": "", "end": "", "location": "y"},
    ]
    evs = parse_events(raw, TZ)
    assert [e.location for e in evs] == ["Ballerup", "Aarhus"]
    assert evs[0].start == at(15, 10) and not evs[0].all_day
    assert evs[1].all_day and evs[1].start.date() == date(2026, 10, 17)


def test_car_events_filters():
    evs = [
        ev(15, (9, 0), (10, 0), ""),  # no location
        ev(15, (10, 0), (11, 0), "Microsoft Teams-møde"),
        ev(15, (11, 0), (12, 0), "Ballerup", description="Jeg tager #tog"),
        ev(15, (12, 0), (13, 0), "Værløse Bymidte"),  # home word
        ev(15, (14, 0), (15, 0), "Ballerup"),
    ]
    assert [e.start.hour for e in car_events(evs, RULES)] == [14]
    assert split_words(" Værløse, Søndersø ,,") == ("værløse", "søndersø")


def test_single_meeting():
    trips, missing = plan_trips([ev(15, (10, 0), (12, 0), "Ballerup")], legs((HOME, "Ballerup", 21, 25)), RULES)
    t = trips[date(2026, 10, 15)]
    assert t.km == 42 and not missing
    # leave 09:35, home 12:25 → away 9–13
    assert t.away == [(9, 13)]
    assert t.stops[0]["location"] == "Ballerup"


def test_goes_home_between_meetings_when_there_is_time():
    day = [ev(15, (8, 0), (10, 0), "A"), ev(15, (14, 0), (15, 0), "B")]
    lg = legs((HOME, "A", 20, 30), (HOME, "B", 30, 30), ("A", "B", 15, 20))
    trips, _ = plan_trips(day, lg, RULES)
    t = trips[date(2026, 10, 15)]
    assert t.km == 20 + 20 + 30 + 30
    assert t.away == [(7, 11), (13, 16)]


def test_drives_directly_when_the_gap_is_short():
    day = [ev(15, (8, 0), (10, 0), "A"), ev(15, (11, 0), (12, 0), "B")]
    lg = legs((HOME, "A", 20, 30), (HOME, "B", 30, 30), ("A", "B", 15, 20))
    trips, _ = plan_trips(day, lg, RULES)
    t = trips[date(2026, 10, 15)]
    # 60 min gap − 30 − 30 travel = 0 min at home < 60 → direct
    assert t.km == 20 + 15 + 30
    assert t.away == [(7, 13)]


def test_location_close_to_home_is_home():
    trips, _ = plan_trips([ev(15, (10, 0), (11, 0), "Nabo")], legs((HOME, "Nabo", 0.4, 2)), RULES)
    assert trips == {}


def test_all_day_event_each_date_with_round_trip():
    all_day = CalendarEvent(at(17, 0), at(19, 0), "Kursus", all_day=True)
    trips, _ = plan_trips([all_day], legs((HOME, "Kursus", 50, 45)), RULES)
    assert set(trips) == {date(2026, 10, 17), date(2026, 10, 18)}
    assert all(t.km == 100 and t.away == [(6, 20)] and not t.away_overnight for t in trips.values())


def test_evening_trip_until_after_midnight():
    trips, _ = plan_trips([ev(17, (20, 0), (23, 30), "Fest")], legs((HOME, "Fest", 40, 45)), RULES)
    assert trips[date(2026, 10, 17)].away == [(19, 24)]
    assert trips[date(2026, 10, 18)].away == [(0, 1)]  # home 00:15
    assert trips[date(2026, 10, 17)].km == 80
    # Driving home after midnight is not a night away.
    assert not trips[date(2026, 10, 17)].away_overnight


def test_hotel_stay_is_away_overnight():
    stay = CalendarEvent(at(15, 10), at(16, 16), "Hotel Aarhus", "Konference")
    trips, _ = plan_trips([stay], legs((HOME, "Hotel Aarhus", 190, 150)), RULES)
    assert trips[date(2026, 10, 15)].away == [(7, 24)] and trips[date(2026, 10, 15)].away_overnight
    assert trips[date(2026, 10, 16)].away == [(0, 19)] and not trips[date(2026, 10, 16)].away_overnight
    assert trips[date(2026, 10, 15)].km == 380


def test_missing_leg_uses_default_travel_time():
    trips, missing = plan_trips([ev(15, (10, 0), (11, 0), "Ukendt")], {}, RULES)
    assert missing == {(HOME, "Ukendt"), ("Ukendt", HOME)}
    t = trips[date(2026, 10, 15)]
    assert t.km == 0 and t.away == [(9, 12)]  # ±30 min
    assert DEFAULT_TRAVEL_MINUTES == 30


def test_legs_needed():
    day = [ev(15, (8, 0), (9, 0), "A"), ev(15, (10, 0), (11, 0), "B"), ev(15, (12, 0), (13, 0), "B")]
    assert legs_needed(day) == {(HOME, "A"), ("A", HOME), (HOME, "B"), ("B", HOME), ("A", "B")}


def test_build_days():
    trips, _ = plan_trips([ev(15, (10, 0), (12, 0), "Ballerup")], legs((HOME, "Ballerup", 21, 25)), RULES)
    days = build_days(trips, away_nights=[date(2026, 10, 17)], holidays=[date(2026, 12, 25)])
    assert days[date(2026, 10, 15)].km == 42 and days[date(2026, 10, 15)].away_hours == [(9, 13)]
    assert days[date(2026, 10, 17)].away_overnight
    assert days[date(2026, 12, 25)].holiday


@pytest.mark.parametrize("gap_minutes, expected_km", [(119, 20), (120, 40)])
def test_min_home_minutes_boundary(gap_minutes, expected_km):
    """30 min there + 30 min back + 60 min at home = 120 min gap needed to drive home in between."""
    first = ev(15, (8, 0), (9, 0), "A")
    second = CalendarEvent(at(15, 9) + timedelta(minutes=gap_minutes), at(15, 18), "A")
    trips, _ = plan_trips([first, second], legs((HOME, "A", 10, 30)), RULES)
    assert trips[date(2026, 10, 15)].km == expected_km


def test_clean_location():
    assert clean_location("1 Havreholmvej\nHornbÃ¦k 3100\nDK") == "1 Havreholmvej, Hornbæk 3100, DK"
    assert clean_location("  Ballerup  Boulevard \r\n 2750 ") == "Ballerup Boulevard, 2750"
    assert clean_location("Søborg") == "Søborg"  # already correct text is left alone
    raw = [{"start": "2026-10-17T18:30:00+02:00", "end": "2026-10-17T23:30:00+02:00", "location": "Vej 1\nHornbÃ¦k"}]
    assert parse_events(raw, TZ)[0].location == "Vej 1, Hornbæk"


def test_event_until_midnight_is_not_a_night_away():
    trips, _ = plan_trips([ev(17, (20, 0), (22, 0), "Fest")], legs((HOME, "Fest", 40, 120)), RULES)
    # home at 00:00 sharp
    assert trips[date(2026, 10, 17)].away == [(18, 24)] and not trips[date(2026, 10, 17)].away_overnight
