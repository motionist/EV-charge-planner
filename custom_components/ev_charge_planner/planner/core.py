"""Planner core – hourly price plan, calibration and hand-over.

See docs/DESIGN.md §4–5. Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .models import ChargeHour, DayInfo, HourPrice, Plan, PlanRow, Settings

HourMap = dict[tuple[date, int], float]

EPS = 0.05  # percentage points treated as "nothing"


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


MIN_FORECAST_FACTOR = 0.3


def forecast_factors(prices: HourMap, known_until: date | None, s: Settings) -> dict[date, float]:
    """Accuracy factor per forecast date whose accuracy is too low (DESIGN §5.0).

    Dates up to ``known_until`` have known prices and are never adjusted.
    accuracy = 1 − MAE(lead day) ÷ mean known price; dates with accuracy below
    ``forecast_min_accuracy`` get factor max(accuracy, MIN_FORECAST_FACTOR).
    """
    if known_until is None or not s.forecast_mae:
        return {}
    known = [p for (d, _h), p in prices.items() if d <= known_until]
    mean = sum(known) / len(known) if known else 0.0
    if mean <= 0:
        return {}
    n = len(s.forecast_mae)
    out: dict[date, float] = {}
    for d in sorted({d for (d, _h) in prices if d > known_until}):
        lead = (d - known_until).days
        mae = s.forecast_mae[min(lead, n) - 1] * (s.forecast_far_factor if lead > n else 1.0)
        accuracy = 1 - mae / mean
        if accuracy < s.forecast_min_accuracy / 100:
            out[d] = max(accuracy, MIN_FORECAST_FACTOR)
    return out


def adjust_prices(prices: HourMap, factors: Mapping[date, float]) -> HourMap:
    """Divide forecast prices by their date's factor (negative prices are multiplied)."""
    out: HourMap = {}
    for (d, h), p in prices.items():
        f = factors.get(d)
        out[(d, h)] = p if f is None else (p / f if p > 0 else p * f)
    return out


def day_type(day: date, info: DayInfo | None) -> str:
    """weekday | weekend | holiday."""
    if info is not None and info.holiday:
        return "holiday"
    return "weekend" if day.weekday() >= 5 else "weekday"


def floor_for(day: date, info: DayInfo | None, s: Settings) -> float:
    """Minimum SoC at departure on ``day``."""
    return s.floor_weekday if day_type(day, info) == "weekday" else s.floor_weekend


def departure_hour(info: DayInfo | None, s: Settings) -> int:
    """First hour the car leaves on a date: its first away hour, else the default."""
    if info is not None and info.away_hours:
        return min(start for start, _end in info.away_hours)
    return s.default_departure_hour


def consumption(info: DayInfo | None, s: Settings) -> float:
    """Battery percentage used by a date's trips."""
    return info.km * s.pct_per_km() if info is not None else 0.0


def requirement(day: date, info: DayInfo | None, s: Settings) -> float:
    """SoC needed at departure on ``day`` (DESIGN §5.1).

    The floor, or the date's trips plus the buffer, or the max level before an
    away night – capped at the max level (or the floor, if that is higher).
    """
    floor = floor_for(day, info, s)
    cons = consumption(info, s)
    need = max(
        floor,
        cons + s.buffer_pct() if cons > 0 else 0.0,
        s.max_level if info is not None and info.away_overnight else 0.0,
    )
    return min(need, max(s.max_level, floor))


def is_home(day: date, hour: int, days: Mapping[date, DayInfo], s: Settings) -> bool:
    """Whether the car is at home (and can charge) in the given hour."""
    info = days.get(day)
    if info is not None:
        if any(start <= hour < end for start, end in info.away_hours):
            return False
        if info.away_overnight and hour >= departure_hour(info, s):
            return False
    prev = days.get(day - timedelta(days=1))
    return not (prev is not None and prev.away_overnight and hour < s.default_departure_hour)


# --------------------------------------------------------------------------- #
# Hourly slots
# --------------------------------------------------------------------------- #


@dataclass
class _Slot:
    start: datetime
    price: float | None  # adjusted
    raw_price: float | None
    known: bool
    capacity: float  # percentage that can be charged in this hour
    use: float = 0.0  # consumption when departing in this hour
    requirement: float | None = None  # SoC needed when departing in this hour
    departs: bool = False


def build_slots(
    now: datetime,
    days: Mapping[date, DayInfo],
    prices: HourMap,
    adjusted: HourMap,
    known_until: date | None,
    s: Settings,
) -> list[_Slot]:
    """One slot per hour from the current hour over the horizon."""
    tz = now.tzinfo
    t0 = now.replace(minute=0, second=0, microsecond=0)
    t0_utc = t0.astimezone(UTC)
    remaining = 1 - (now - t0).total_seconds() / 3600
    rate = s.charge_pct_per_hour()
    departed: set[date] = set()
    slots: list[_Slot] = []
    for i in range(s.horizon_days * 24):
        start = (t0_utc + timedelta(hours=i)).astimezone(tz)
        d, h = start.date(), start.hour
        raw = prices.get((d, h))
        home = is_home(d, h, days, s)
        cap = 0.0 if raw is None or not home else rate * (remaining if i == 0 else 1.0)
        slot = _Slot(start, adjusted.get((d, h)), raw, known_until is None or d <= known_until, cap)
        info = days.get(d)
        # A departure in the current hour has already happened unless it is exactly now.
        if h == departure_hour(info, s) and d not in departed and (i > 0 or now == t0):
            departed.add(d)
            prev = days.get(d - timedelta(days=1))
            slot.departs = True
            slot.use = consumption(info, s)
            # After an away night the car does not depart from home.
            if not (prev is not None and prev.away_overnight):
                slot.requirement = requirement(d, info, s)
        slots.append(slot)
    return slots


def _levels(soc: float, slots: list[_Slot], q: list[float], upto: int) -> list[float]:
    """SoC at the start of every slot up to and including ``upto``."""
    out = []
    level = soc
    for k in range(upto + 1):
        out.append(level)
        level += q[k] - slots[k].use
    return out


def allocate(soc: float, slots: list[_Slot], q: list[float], s: Settings) -> float:
    """Cover every departure's requirement in time order (DESIGN §5.1).

    Each deficit is charged in the cheapest earlier hour with spare capacity
    (the latest of equally cheap hours),
    without the battery exceeding the max level before the departure. Charging
    therefore only reaches the next cheaper hour unless nothing cheaper comes
    in time, in which case it tops up (to at most the max level). ``q`` is
    updated in place; returns the percentage that could not be planned.
    """
    unmet = 0.0
    for j, slot in enumerate(slots):
        if slot.requirement is None:
            continue
        for _ in range(len(slots) + 1):
            levels = _levels(soc, slots, q, j)
            deficit = slot.requirement - levels[j]
            if deficit <= EPS:
                break
            # suffix[i] = highest SoC between charging in slot i and departure j
            suffix = [0.0] * (j + 1)
            highest = float("-inf")
            for k in range(j, -1, -1):
                suffix[k] = highest
                highest = max(highest, levels[k])
            best: int | None = None
            best_room = 0.0
            for i in range(j):
                price = slots[i].price
                if price is None:
                    continue
                room = min(slots[i].capacity - q[i], s.max_level - suffix[i])
                # On equal prices take the later hour: it postpones the decision.
                if room > EPS and (best is None or price <= slots[best].price):  # type: ignore[operator]
                    best, best_room = i, room
            if best is None:
                unmet += deficit
                break
            q[best] += min(deficit, best_room)
    return unmet


def _days_since_calibration(now: datetime, s: Settings) -> float:
    if s.last_calibration is None:
        return 999.0
    return (now - s.last_calibration).total_seconds() / 86400


def calibrate(now: datetime, soc: float, slots: list[_Slot], q: list[float], until: int, s: Settings) -> list[int]:
    """Plan a calibration charge before slot ``until`` when due (DESIGN §5.2).

    The cheapest home hour before ``until`` must be ≤ the price threshold and
    no later hour cheaper (early grab), or ≤ the escalated threshold once the
    interval has passed. Charges to the calibration SoC in the cheapest of
    those hours and returns their indexes (empty when not due).
    """
    if s.calibration_interval_days <= 0 or until <= 0:
        return []
    before = [i for i in range(until) if slots[i].capacity > 0 and slots[i].price is not None]
    if not before:
        return []
    cheapest = min(slots[i].price for i in before)  # type: ignore[type-var]
    later = [sl.price for sl in slots[until:] if sl.capacity > 0 and sl.price is not None]
    days_since = _days_since_calibration(now, s)
    early = (
        s.calibration_early_days > 0
        and days_since >= s.calibration_early_days
        and cheapest <= s.calibration_price_threshold
        and not (later and min(later) < cheapest)
    )
    weeks_over = max((days_since - s.calibration_interval_days) / 7, 0)
    allowed = s.calibration_price_threshold * (1 + s.calibration_escalation_pct_per_week / 100) ** weeks_over
    due = days_since >= s.calibration_interval_days and cheapest <= allowed
    if not (early or due):
        return []
    need = s.calibration_soc - soc - sum(q[:until])
    used = []
    for i in sorted(before, key=lambda i: slots[i].price):  # type: ignore[arg-type,return-value]
        if need <= EPS:
            break
        amount = min(need, slots[i].capacity - q[i])
        if amount > 0:
            q[i] += amount
            need -= amount
            used.append(i)
    return used


def handover_end(slots: list[_Slot], s: Settings) -> int:
    """Index of the first slot not handed over to the charger integration.

    The hand-over covers the hours before the next departure, only hours with
    known prices and at most ``handover_max_hours``.
    """
    end = min(len(slots), s.handover_max_hours)
    for i in range(1, end):
        if slots[i].requirement is not None or not slots[i].known:
            return i
    return end


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


def plan(
    now: datetime,
    soc: float,
    days: Mapping[date, DayInfo],
    prices: HourMap,
    s: Settings,
    known_until: date | None = None,
) -> Plan:
    """Plan charging hour by hour over the horizon (DESIGN §5).

    ``known_until`` is the last date with known prices; later dates are
    forecasts, adjusted for their accuracy before any comparison (§5.0), and
    are never handed over to the charger integration. ``None`` treats every
    price as known.
    """
    factors = forecast_factors(prices, known_until, s)
    slots = build_slots(now, days, prices, adjust_prices(prices, factors), known_until, s)
    q = [0.0] * len(slots)

    until = handover_end(slots, s)
    cal_hours = set(calibrate(now, soc, slots, q, until, s))
    unmet = allocate(soc, slots, q, s)

    charge_now = sum(q[:until])
    t0 = now.replace(minute=0, second=0, microsecond=0)
    charge_until = slots[until].start if until < len(slots) else (t0.astimezone(UTC) + timedelta(hours=until)).astimezone(now.tzinfo)

    rows: dict[date, PlanRow] = {}
    hours: list[ChargeHour] = []
    charged = 0.0
    level = float(soc)
    for i, slot in enumerate(slots):
        d = slot.start.date()
        if d not in rows:
            info = days.get(d)
            rows[d] = PlanRow(
                day=d,
                day_type=day_type(d, info),
                km=info.km if info else 0.0,
                floor=floor_for(d, info, s),
                departure_hour=None,
                requirement=None,
                charge_pct=0.0,
                charge_hours=[],
                soc_departure=None,
                forecast_factor=factors.get(d, 1.0),
                away_overnight=bool(info and info.away_overnight),
            )
        row = rows[d]
        if slot.departs:
            row.soc_departure = round(level, 1)
            if slot.requirement is not None:
                row.departure_hour = slot.start.hour
                row.requirement = round(slot.requirement, 1)
                if charged > EPS and row.status != "charge":
                    row.status = "covered"
        if q[i] > EPS:
            ch = ChargeHour(slot.start, round(q[i], 2), slot.price, slot.raw_price, slot.known, i in cal_hours)
            hours.append(ch)
            row.charge_hours.append(ch)
            row.charge_pct = round(row.charge_pct + q[i], 2)
            row.calibration = row.calibration or ch.calibration
            row.status = "charge"
            charged += q[i]
        level += q[i] - slot.use

    return Plan(
        target_soc=int(min(round(soc + charge_now), 100)),
        charge_until=charge_until,
        charge_now=round(charge_now, 2),
        hours=hours,
        rows=[rows[d] for d in sorted(rows)],
        unmet=round(unmet, 1),
        calibration=bool(cal_hours),
    )
