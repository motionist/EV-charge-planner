"""Planner core – timeline, targets, calibration and simulation.

See docs/DESIGN.md §4–5. Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import DayInfo, HourPrice, Opportunity, Plan, PlanRow, Settings

HourMap = dict[tuple[date, int], float]


def merge_prices(*layers: Iterable[HourPrice], tz=None) -> HourMap:
    """Merge price layers; later layers override earlier ones.

    Pass layers from least to most trusted, e.g. ``merge_prices(fallback,
    forecast, known)``. Keys are (local date, local hour).
    """
    out: HourMap = {}
    for layer in layers:
        for hp in layer:
            start = hp.start.astimezone(tz) if (tz is not None and hp.start.tzinfo) else hp.start
            out[(start.date(), start.hour)] = hp.price
    return out


def day_type(day: date, info: DayInfo | None) -> str:
    """weekday | weekend | holiday."""
    if info is not None and info.holiday:
        return "holiday"
    return "weekend" if day.weekday() >= 5 else "weekday"


def floor_for(day: date, info: DayInfo | None, s: Settings) -> float:
    """Minimum SoC at departure on ``day``."""
    return s.floor_weekday if day_type(day, info) == "weekday" else s.floor_weekend


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


@dataclass
class _Event:
    kind: str  # opp | depart | drive
    day: date
    opp: Opportunity | None = None
    floor: float = 0.0
    pct: float = 0.0


def _clip(intervals: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    out = []
    for s, e in intervals:
        s2, e2 = max(s, lo), min(e, hi)
        if e2 > s2:
            out.append((s2, e2))
    return out


def _hours(intervals: list[tuple[int, int]]) -> int:
    return sum(e - s for s, e in intervals)


def _is_away(hour: int, intervals: list[tuple[int, int]]) -> bool:
    return any(s <= hour < e for s, e in intervals)


def _night(day: date, prices: HourMap, s: Settings, from_hour: int, available: bool) -> Opportunity:
    priced = [(h, prices[(day, h)]) for h in range(from_hour, s.night_end_hour) if (day, h) in prices]
    if priced:
        best_h, best_p = min(priced, key=lambda x: x[1])
    else:
        best_h, best_p = None, None
    return Opportunity("night", day, best_p, best_h, from_hour, s.night_end_hour, available)


def _day_window(day: date, info: DayInfo | None, prices: HourMap, s: Settings, from_hour: int) -> Opportunity | None:
    away = info.away_hours if info else []
    home = [h for h in range(max(from_hour, s.day_start_hour), s.day_end_hour) if not _is_away(h, away)]
    priced = [(h, prices[(day, h)]) for h in home if (day, h) in prices]
    if not priced:
        return None
    best_h, best_p = min(priced, key=lambda x: x[1])
    start = end = best_h
    while start - 1 in home:
        start -= 1
    while end + 1 in home:
        end += 1
    return Opportunity("day", day, best_p, best_h, start, end + 1, True)


def _drive_split(info: DayInfo | None, window: Opportunity | None, from_hour: int, s: Settings, today: bool) -> tuple[float, float]:
    """Consumption (%) before and after the day window."""
    if info is None or info.km <= 0:
        return 0.0, 0.0
    total_pct = info.km * s.pct_per_km()
    away = info.away_hours
    if today:
        remaining = _clip(away, from_hour, 24)
        if _hours(away) == 0:
            return 0.0, 0.0
        total_pct *= _hours(remaining) / _hours(away)
        away = remaining
    if _hours(away) == 0:
        return total_pct, 0.0
    if window is None:
        return total_pct, 0.0
    before = _hours(_clip(away, 0, window.start_hour)) / _hours(away)
    return total_pct * before, total_pct * (1 - before)


def build_timeline(now: datetime, days: Mapping[date, DayInfo], prices: HourMap, s: Settings) -> tuple[list[_Event], date, bool]:
    """Chronological events from now. Returns (events, first_date, night_mode)."""
    today = now.date()
    night_mode = now.hour < s.night_end_hour
    events: list[_Event] = []

    if not night_mode:
        info = days.get(today)
        win = _day_window(today, info, prices, s, now.hour) if now.hour < s.day_end_hour else None
        before, after = _drive_split(info, win, now.hour, s, today=True)
        if before:
            events.append(_Event("drive", today, pct=before))
        if win:
            events.append(_Event("opp", today, opp=win))
        if after:
            events.append(_Event("drive", today, pct=after))
        first = today + timedelta(days=1)
    else:
        first = today

    for i in range(s.horizon_days):
        d = first + timedelta(days=i)
        info = days.get(d)
        prev = days.get(d - timedelta(days=1))
        is_today = d == today
        night_from = now.hour if is_today else 0
        events.append(_Event("opp", d, opp=_night(d, prices, s, night_from, available=not (prev and prev.away_overnight))))
        events.append(_Event("depart", d, floor=floor_for(d, info, s)))
        win = _day_window(d, info, prices, s, now.hour if is_today else 0)
        before, after = _drive_split(info, win, now.hour if is_today else 0, s, today=is_today and not night_mode)
        if before:
            events.append(_Event("drive", d, pct=before))
        if win:
            events.append(_Event("opp", d, opp=win))
        if after:
            events.append(_Event("drive", d, pct=after))
    return events, first, night_mode


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #


def target_for(events: list[_Event], idx: int, days: Mapping[date, DayInfo], s: Settings) -> int:
    """Target SoC for the opportunity at ``events[idx]`` (DESIGN §5.1)."""
    opp = events[idx].opp
    assert opp is not None
    if opp.kind == "night":
        info = days.get(opp.day)
        if info is not None and info.away_overnight:
            return round(s.max_level)
    ref = opp.price
    consumed = 0.0
    need = 0.0
    first_floor: float | None = None
    for ev in events[idx + 1 :]:
        if ev.kind == "opp":
            o = ev.opp
            if ref is not None and o is not None and o.available and o.price is not None and o.price < ref:
                break
        elif ev.kind == "depart":
            if first_floor is None:
                first_floor = ev.floor
            need = max(need, ev.floor + consumed)
        elif ev.kind == "drive":
            consumed += ev.pct
    extra = consumed + s.buffer_pct() if consumed > 0 else 0.0
    cap = max(s.max_level, first_floor or 0.0)
    return round(min(max(need, extra), cap))


def _cheaper_later(events: list[_Event], idx: int, ref: float) -> bool:
    for ev in events[idx + 1 :]:
        o = ev.opp
        if ev.kind == "opp" and o is not None and o.available and o.price is not None and o.price < ref:
            return True
    return False


def calibration_due(events: list[_Event], idx: int, s: Settings, last_cal: datetime | None, tz) -> bool:
    """Whether the night at ``events[idx]`` should be a calibration night (DESIGN §5.2)."""
    opp = events[idx].opp
    if s.calibration_interval_days <= 0 or opp is None or opp.kind != "night" or opp.price is None:
        return False
    start = datetime.combine(opp.day, time(0), tzinfo=tz)
    days_since = 999.0 if last_cal is None else (start - last_cal).total_seconds() / 86400
    early = (
        s.calibration_early_days > 0
        and days_since >= s.calibration_early_days
        and opp.price <= s.calibration_price_threshold
        and not _cheaper_later(events, idx, opp.price)
    )
    weeks_over = max((days_since - s.calibration_interval_days) / 7, 0)
    allowed = s.calibration_price_threshold * (1 + s.calibration_escalation_pct_per_week / 100) ** weeks_over
    due = days_since >= s.calibration_interval_days and opp.price <= allowed
    return early or due


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def plan(now: datetime, soc: float, days: Mapping[date, DayInfo], prices: HourMap, s: Settings) -> Plan:
    """Simulate the coming days and return the plan (DESIGN §5.3–5.4)."""
    events, first, night_mode = build_timeline(now, days, prices, s)
    tz = now.tzinfo

    rows: dict[date, PlanRow] = {}

    def row(d: date) -> PlanRow:
        if d not in rows:
            info = days.get(d)
            rows[d] = PlanRow(
                day=d,
                day_type=day_type(d, info),
                km=info.km if info else 0.0,
                floor=floor_for(d, info, s),
                night=None,
                night_target=None,
                day_window=None,
                day_target=None,
                soc_departure=soc,
                away_overnight=bool(info and info.away_overnight),
            )
        return rows[d]

    level = float(soc)
    last_cal = s.last_calibration
    charged_before = False
    first_opp: Opportunity | None = None
    first_target: int | None = None
    next_charge: Opportunity | None = None
    next_charge_target: int | None = None

    for i, ev in enumerate(events):
        if ev.kind == "drive":
            level = max(level - ev.pct, 0.0)
            continue
        r = row(ev.day)
        if ev.kind == "depart":
            r.soc_departure = round(level, 1)
            continue
        opp = ev.opp
        assert opp is not None
        if opp.kind == "night":
            r.night = opp
        else:
            r.day_window = opp
        if not opp.available:
            continue
        tgt = target_for(events, i, days, s)
        if calibration_due(events, i, s, last_cal, tz):
            tgt = max(tgt, round(s.calibration_soc))
            r.calibration = True
            last_cal = datetime.combine(opp.day, time(0), tzinfo=tz)
        if first_opp is None:
            first_opp, first_target = opp, tgt
        if level < tgt:
            level = float(tgt)
            if opp.kind == "night":
                r.night_target = tgt
            else:
                r.day_target = tgt
            r.status = "charge"
            charged_before = True
            if next_charge is None:
                next_charge, next_charge_target = opp, tgt
        elif r.status != "charge":
            r.status = "covered" if charged_before else "not_needed"

    # Publish the target of the next opportunity that actually charges (its
    # window is in next_charge); fall back to the next opportunity's target.
    target = next_charge_target if next_charge_target is not None else (first_target or 0)
    ordered = [rows[d] for d in sorted(rows)]
    return Plan(target_soc=int(target), next_opportunity=first_opp, next_charge=next_charge, rows=ordered)
