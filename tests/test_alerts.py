"""Alerts: detecting issues and deciding which notifications to send (DESIGN §5.6)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ev_charge_planner.alerts import (
    ALL_ALERTS,
    CALENDAR,
    CHARGE_SPEED,
    PLUG_IN,
    PRICES_AHEAD,
    PRICES_NOW,
    PRICES_TOMORROW,
    ROUTING,
    SOC,
    UNMET,
    AlertTracker,
    Snapshot,
    detect,
)

TZ = ZoneInfo("Europe/Copenhagen")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def ok(now: datetime, **kw) -> Snapshot:
    """Everything fine: prices known through tomorrow and forecasts after."""
    base = dict(
        now=now,
        known_until=now.date() + timedelta(days=1),
        price_dates={now.date() + timedelta(days=i) for i in range(5)},
    )
    base.update(kw)
    return Snapshot(**base)


def test_nothing_when_all_is_well():
    assert detect(ok(at(14, 16))) == {}


def test_tomorrows_prices_only_after_15():
    s = ok(at(14, 14), known_until=date(2026, 10, 14))
    assert PRICES_TOMORROW not in detect(s)
    s = ok(at(14, 15), known_until=date(2026, 10, 14))
    assert PRICES_TOMORROW in detect(s)


def test_prices_ahead_and_now():
    s = ok(at(14, 10), price_dates={date(2026, 10, 14), date(2026, 10, 15)}, price_now=False)
    issues = detect(s)
    assert PRICES_NOW in issues
    assert "2026-10-16" in issues[PRICES_AHEAD][1] and "2026-10-17" in issues[PRICES_AHEAD][1]


def test_other_issues():
    s = ok(
        at(14, 10),
        soc_ok=False,
        calendar_error=True,
        missing_legs=["home → Aarhus"],
        unmet=12.4,
        unmet_day=date(2026, 10, 15),
        charge_speed_fallback=True,
    )
    issues = detect(s, "da")
    assert {SOC, CALENDAR, ROUTING, UNMET, CHARGE_SPEED} <= set(issues)
    assert issues[UNMET][1] == "Bilen kan ikke nå det nødvendige niveau til afgangen 2026-10-15 (12 % mangler)."
    assert issues[ROUTING][0] == "Ture kan ikke ruteberegnes"


def test_plug_in_reminder():
    first = at(14, 23)
    assert PLUG_IN in detect(ok(at(14, 21), first_charge=first, plugged=False, car_home=True))
    assert PLUG_IN not in detect(ok(at(14, 20), first_charge=first, plugged=False, car_home=True))  # more than 2 h ahead
    assert PLUG_IN not in detect(ok(at(14, 21), first_charge=first, plugged=True, car_home=True))
    assert PLUG_IN not in detect(ok(at(14, 21), first_charge=first, plugged=False, car_home=False))  # still out
    assert PLUG_IN not in detect(ok(at(14, 21), first_charge=first, plugged=None))  # no plugged-in sensor


def test_grace_reminder_and_resolved():
    tr = AlertTracker()
    enabled = set(ALL_ALERTS)
    issue = {CALENDAR: ("Calendar could not be read", "…")}
    assert tr.update(at(14, 10), issue, enabled) == []  # 1 h grace
    assert tr.update(at(14, 10, 59), issue, enabled) == []
    sent = tr.update(at(14, 11), issue, enabled)
    assert [(n.key, n.kind) for n in sent] == [(CALENDAR, "new")]
    assert tr.update(at(14, 12), issue, enabled) == []  # not again
    sent = tr.update(at(15, 11), issue, enabled)
    assert [(n.key, n.kind) for n in sent] == [(CALENDAR, "reminder")]
    sent = tr.update(at(15, 12), {}, enabled)
    assert [(n.key, n.kind) for n in sent] == [(CALENDAR, "resolved")]
    assert sent[0].title == "✅ OK again: Calendar could not be read"


def test_short_gap_in_forecasts_is_not_notified():
    """Forecasts are optional: a gap is only notified after 24 h."""
    tr = AlertTracker()
    issue = {PRICES_AHEAD: ("No prices", "…")}
    assert tr.update(at(14, 10), issue, set(ALL_ALERTS)) == []
    assert tr.update(at(15, 9), issue, set(ALL_ALERTS)) == []
    assert tr.update(at(15, 10, 1), {}, set(ALL_ALERTS)) == []  # gone before 24 h: no message at all
    assert tr.active == {}


def test_disabled_alerts_are_tracked_but_not_sent():
    tr = AlertTracker()
    issue = {PLUG_IN: ("Plug in the car", "…")}
    assert tr.update(at(14, 21), issue, {SOC}) == []
    assert PLUG_IN in tr.current()
    assert tr.update(at(14, 22), {}, {SOC}) == []  # never sent → no resolved message
