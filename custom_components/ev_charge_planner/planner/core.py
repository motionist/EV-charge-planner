"""Planner core – hourly price plan, calibration and hand-over.

See docs/DESIGN.md §4–5. Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .models import ChargeHour, DayInfo, HourPrice, Plan, PlanRow, Settings

HourMap = dict[tuple[date, int], float]

EPS = 0.05  # percentage points treated as "nothing"
CHEAP_FILL_WINDOW = 48  # hours compared by the cheap fill (DESIGN §5.1a)
ROAD_FACTOR = 1.3  # road distance ≈ 1.3 × straight-line distance


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


def _local(hp: HourPrice, tz) -> datetime:
    return hp.start.astimezone(tz) if (tz is not None and hp.start.tzinfo) else hp.start


def fallback_offsets(fallback: Iterable[HourPrice], forecast: Iterable[HourPrice], tz=None) -> dict[int, float] | None:
    """Per hour of day, how much the forecast lies above the fallback forecast (DESIGN §5.0).

    The mean difference (forecast − fallback) of each hour of day in the
    hours both cover; an hour of day without overlap gets the overall mean
    difference. ``None`` when they do not overlap at all.
    """
    covered = {(t.date(), t.hour): hp.price for hp in forecast for t in [_local(hp, tz)]}
    diffs: dict[int, list[float]] = defaultdict(list)
    for hp in fallback:
        t = _local(hp, tz)
        if (t.date(), t.hour) in covered:
            diffs[t.hour].append(covered[(t.date(), t.hour)] - hp.price)
    if not diffs:
        return None
    every = [d for v in diffs.values() for d in v]
    overall = sum(every) / len(every)
    return {h: (sum(diffs[h]) / len(diffs[h]) if h in diffs else overall) for h in range(24)}


def calibrate_fallback(
    fallback: Iterable[HourPrice],
    forecast: Iterable[HourPrice],
    tz=None,
    offsets: Mapping[int, float] | None = None,
) -> list[HourPrice]:
    """Fallback forecast hours the forecast does not cover, at the forecast's level (DESIGN §5.0).

    A fallback forecast (e.g. Energi Data Service's) can be on another level
    than the forecast – e.g. without the evening peak tariff. Every fallback
    hour the forecast does not cover gets the ``offsets`` of its hour of
    day (default: ``fallback_offsets`` of the two). Without offsets the
    fallback hours are returned unchanged. Hours the forecast covers are
    left out (the forecast wins there anyway).
    """
    fallback, forecast = list(fallback), list(forecast)
    if offsets is None:
        offsets = fallback_offsets(fallback, forecast, tz)
    covered = {(t.date(), t.hour) for t in (_local(hp, tz) for hp in forecast)}
    out = []
    for hp in fallback:
        t = _local(hp, tz)
        if (t.date(), t.hour) in covered:
            continue
        out.append(HourPrice(hp.start, hp.price + offsets.get(t.hour, 0.0), hp.source) if offsets else hp)
    return out


MIN_FORECAST_FACTOR = 0.3


def _known_mean(prices: HourMap, known_until: date) -> float:
    known = [p for (d, _h), p in prices.items() if d <= known_until]
    return sum(known) / len(known) if known else 0.0


def forecast_factors(prices: HourMap, known_until: date | None, s: Settings) -> dict[date, float]:
    """Accuracy factor per forecast date whose accuracy is too low (DESIGN §5.0).

    Dates up to ``known_until`` have known prices and are never adjusted.
    accuracy = 1 − MAE(lead day) ÷ mean known price; dates with accuracy below
    ``forecast_min_accuracy`` get factor max(accuracy, MIN_FORECAST_FACTOR).
    """
    if known_until is None or not s.forecast_mae:
        return {}
    mean = _known_mean(prices, known_until)
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


def fallback_factors(
    prices: HourMap,
    fallback_hours: Collection[tuple[date, int]],
    known_until: date | None,
    s: Settings,
) -> dict[tuple[date, int], float]:
    """Accuracy factor of every fallback forecast hour (DESIGN §5.0).

    The fallback forecast is used after the forecast ends, so it is taken to
    be less accurate than the forecast's last lead day, and more so the
    further out: ``mae = mae_day[last] × forecast_far_factor ** max(lead −
    last + 1, 1)``, ``accuracy = 1 − mae ÷ mean known price``. Unlike the
    forecast, it is always applied (whatever ``forecast_min_accuracy``),
    with the same floor.
    """
    if known_until is None or not s.forecast_mae or not fallback_hours:
        return {}
    mean = _known_mean(prices, known_until)
    if mean <= 0:
        return {}
    n = len(s.forecast_mae)
    out: dict[tuple[date, int], float] = {}
    for d, h in fallback_hours:
        if d <= known_until or (d, h) not in prices:
            continue
        lead = (d - known_until).days
        mae = s.forecast_mae[-1] * s.forecast_far_factor ** max(lead - n + 1, 1)
        out[(d, h)] = max(1 - mae / mean, MIN_FORECAST_FACTOR)
    return out


def adjust_prices(
    prices: HourMap,
    factors: Mapping[date, float],
    hour_factors: Mapping[tuple[date, int], float] | None = None,
) -> HourMap:
    """Divide forecast prices by their factor (negative prices are multiplied).

    ``hour_factors`` (fallback forecast hours) take precedence over the
    date's factor.
    """
    out: HourMap = {}
    for (d, h), p in prices.items():
        f = (hour_factors or {}).get((d, h), factors.get(d))
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


def _end(start: int, end: int) -> int:
    """End hour of an away interval; an end at or before the start means midnight."""
    return end if end > start else 24


def departure_hour(info: DayInfo | None, s: Settings, after: int | None = None, prev: DayInfo | None = None) -> int:
    """First hour the car leaves on a date: its first away hour, else the default.

    With ``after`` (today's current hour) only trips that start later count,
    so a later trip is still a departure once the first one has started.
    With ``prev`` (the day before), away hours from midnight that continue a
    trip of the evening before (e.g. driving home after midnight) are not a
    departure.
    """
    if info is not None and info.away_hours:
        continues = prev is not None and any(end >= 24 for _start, end in prev.away_hours)
        starts = [start for start, _end in info.away_hours if not (continues and start == 0)]
        if starts:
            later = [h for h in starts if after is None or h > after]
            return min(later or starts)
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
        if any(start <= hour < _end(start, end) for start, end in info.away_hours):
            return False
        if info.away_overnight and hour >= departure_hour(info, s):
            return False
    prev = days.get(day - timedelta(days=1))
    return not (prev is not None and prev.away_overnight and hour < s.default_departure_hour)


def remaining_km(now: datetime, days: Mapping[date, DayInfo], car_home: bool | None, distance_km: float | None) -> float:
    """km still to drive today before the car is home again (DESIGN §5.4).

    Today's consumption is drawn at its departure, so once the car has left it
    is already in the SoC – except the way back. ``car_home`` is ``None`` when
    unknown. The estimate is the larger of the road distance home
    (``ROAD_FACTOR`` × straight line) and, during a calendar away interval,
    half of the day's km (the return leg of a round trip).
    """
    if car_home:
        return 0.0
    by_distance = distance_km * ROAD_FACTOR if car_home is False and distance_km else 0.0
    info = days.get(now.date())
    away_now = info is not None and any(start <= now.hour < _end(start, end) for start, end in info.away_hours)
    by_calendar = info.km / 2 if away_now and info is not None else 0.0
    return max(by_distance, by_calendar)


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
        dep = departure_hour(info, s, now.hour if d == now.date() else None, days.get(d - timedelta(days=1)))
        # A departure in the current hour has already happened unless it is exactly now.
        if h == dep and d not in departed and (i > 0 or now == t0):
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
    in time, in which case it tops up (to at most the max level).

    High cap before long trips: in the home stay right before a departure of
    at least ``long_trip_km`` an hour may charge up to ``max_level_long_trip``
    when it is at least ``long_trip_advantage_pct`` cheaper than the best hour
    within the max level (or no such hour exists). The battery must be back
    within the max level after that trip. ``q`` is updated in place; returns
    the percentage that could not be planned.
    """
    unmet = 0.0
    high = max(s.max_level_long_trip, s.max_level)
    long_use = s.long_trip_km * s.pct_per_km()
    advantage = s.long_trip_advantage_pct / 100
    for j, slot in enumerate(slots):
        if slot.requirement is None:
            continue
        for _ in range(len(slots) + 1):
            levels = _levels(soc, slots, q, j)
            deficit = slot.requirement - levels[j]
            if deficit <= EPS:
                break
            # suffix[i] = highest SoC between charging in slot i and departure j;
            # high_room[i] = room under the high cap: only when the next
            # departure after slot i is a long trip, back within the max level
            # after it.
            suffix = [0.0] * (j + 1)
            high_room = [0.0] * (j + 1)
            highest = segment = after = float("-inf")
            nxt = j
            for k in range(j, -1, -1):
                suffix[k] = highest
                if slots[nxt].use > 0 and slots[nxt].use >= long_use:
                    high_room[k] = min(high - segment, s.max_level - after)
                else:
                    high_room[k] = s.max_level - highest
                if slots[k].departs:
                    after, segment, nxt = highest, levels[k], k
                else:
                    segment = max(segment, levels[k])
                highest = max(highest, levels[k])
            best: int | None = None
            best_room = 0.0
            best_high: int | None = None
            best_high_room = 0.0
            for i in range(j):
                price = slots[i].price
                if price is None:
                    continue
                free = slots[i].capacity - q[i]
                room = min(free, s.max_level - suffix[i])
                # On equal prices take the later hour: it postpones the decision.
                if room > EPS and (best is None or price <= slots[best].price):  # type: ignore[operator]
                    best, best_room = i, room
                room = min(free, high_room[i])
                if room > EPS and (best_high is None or price <= slots[best_high].price):  # type: ignore[operator]
                    best_high, best_high_room = i, room
            if best_high is not None and best_high != best:
                p_high = slots[best_high].price
                p_best = slots[best].price if best is not None else None
                if p_best is None or p_high <= p_best - abs(p_best) * advantage:  # type: ignore[operator]
                    best, best_room = best_high, best_high_room
            if best is None:
                unmet += deficit
                break
            q[best] += min(deficit, best_room)
    return unmet


def cheap_fill(soc: float, slots: list[_Slot], q: list[float], s: Settings) -> set[int]:
    """Fill up in hours much cheaper than the following days (DESIGN §5.1a).

    A home hour qualifies when its price is at least
    ``cheap_fill_advantage_pct`` below every later home hour within
    ``CHEAP_FILL_WINDOW`` hours, and that window lies inside the horizon with
    prices. Later qualifying hours that are not that much cheaper are left
    out of the comparison, so a cheap night qualifies as a whole while a much
    cheaper night later wins. Qualifying hours fill cheapest first without
    the battery exceeding the max level after them; ``q`` is updated in place
    and the fill hours are returned. ``cheap_fill_advantage_pct = 0``
    disables it.
    """
    advantage = s.cheap_fill_advantage_pct / 100
    filled: set[int] = set()
    if advantage <= 0:
        return filled
    n = len(slots)
    qualifies: set[int] = set()
    # Backwards: whether an hour qualifies only depends on the later hours.
    for i in range(n - CHEAP_FILL_WINDOW - 1, -1, -1):
        price = slots[i].price
        if slots[i].capacity <= 0 or price is None or slots[i + CHEAP_FILL_WINDOW].price is None:
            continue
        later = [
            sl.price
            for j, sl in enumerate(slots[i + 1 : i + CHEAP_FILL_WINDOW + 1], start=i + 1)
            if sl.capacity > 0
            and sl.price is not None
            and not (j in qualifies and sl.price > price - abs(price) * advantage)
        ]
        if later:
            m = min(later)  # type: ignore[type-var]
            if price > m - abs(m) * advantage:
                continue
        qualifies.add(i)
    for i in sorted(qualifies, key=lambda i: (slots[i].price, i)):  # type: ignore[arg-type,return-value]
        levels = _levels(soc, slots, q, n - 1)
        end = levels[-1] + q[-1] - slots[-1].use
        add = min(slots[i].capacity - q[i], s.max_level - max([*levels[i + 1 :], end]))
        if add > EPS:
            q[i] += add
            filled.add(i)
    return filled


def trim(soc: float, slots: list[_Slot], q: list[float], fill: Collection[int], keep: Collection[int] = ()) -> None:
    """Remove charging made redundant by the cheap fill.

    Planned hours dearer than every fill hour (except ``fill`` and ``keep``)
    are reduced, the most expensive first, by at most the smallest surplus
    over the requirement at every later departure. ``q`` is updated in place.
    """
    n = len(slots)
    dearest_fill = max(slots[i].price for i in fill)  # type: ignore[type-var]
    order = sorted(
        (
            k
            for k in range(n)
            if q[k] > EPS
            and k not in fill
            and k not in keep
            and slots[k].price is not None
            and slots[k].price > dearest_fill  # type: ignore[operator]
        ),
        key=lambda k: (-slots[k].price, k),  # type: ignore[operator]
    )
    for k in order:
        levels = _levels(soc, slots, q, n - 1)
        surplus = [levels[j] - sl.requirement for j, sl in enumerate(slots) if j > k and sl.requirement is not None]
        if not surplus:
            # Nothing after this hour needs the energy.
            continue
        cut = min(q[k], min(surplus))
        if cut > EPS:
            q[k] -= cut


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
    car_home: bool | None = None,
    distance_km: float | None = None,
    fallback_hours: Collection[tuple[date, int]] = (),
) -> Plan:
    """Plan charging hour by hour over the horizon (DESIGN §5).

    ``known_until`` is the last date with known prices; later dates are
    forecasts, adjusted for their accuracy before any comparison (§5.0), and
    are never handed over to the charger integration. ``None`` treats every
    price as known.

    ``car_home`` and ``distance_km`` (straight line from home) describe where
    the car is now; away from home, the rest of today's driving is taken from
    the SoC before planning (§5.4).

    ``fallback_hours`` are the (date, hour) keys whose price comes from the
    fallback forecast; they get their own, larger uncertainty (§5.0).
    """
    rest_km = remaining_km(now, days, car_home, distance_km)
    soc = soc - rest_km * s.pct_per_km()
    factors = forecast_factors(prices, known_until, s)
    hour_factors = fallback_factors(prices, fallback_hours, known_until, s)
    slots = build_slots(now, days, prices, adjust_prices(prices, factors, hour_factors), known_until, s)
    q = [0.0] * len(slots)

    until = handover_end(slots, s)
    cal_hours = set(calibrate(now, soc, slots, q, until, s))
    unmet = allocate(soc, slots, q, s)
    fill_hours = cheap_fill(soc, slots, q, s)
    if fill_hours:
        trim(soc, slots, q, fill_hours, cal_hours)

    charge_now = sum(q[:until])
    t0 = now.replace(minute=0, second=0, microsecond=0)
    charge_until = slots[until].start if until < len(slots) else (t0.astimezone(UTC) + timedelta(hours=until)).astimezone(now.tzinfo)

    rows: dict[date, PlanRow] = {}
    hours: list[ChargeHour] = []
    charged = 0.0
    level = float(soc)
    kwh_per_pct = s.battery_kwh / 100
    totals = {True: [0.0, 0.0], False: [0.0, 0.0]}  # known → [kWh, cost]
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
                charge_kwh=0.0,
                cost=None,
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
            kwh = q[i] * kwh_per_pct
            ch = ChargeHour(
                start=slot.start,
                pct=round(q[i], 2),
                kwh=round(kwh, 2),
                price=slot.price,
                raw_price=slot.raw_price,
                known=slot.known,
                calibration=i in cal_hours,
                cheap_fill=i in fill_hours,
            )
            hours.append(ch)
            row.charge_hours.append(ch)
            row.charge_pct = round(row.charge_pct + q[i], 2)
            row.charge_kwh = round(row.charge_kwh + kwh, 2)
            if slot.raw_price is not None:
                row.cost = round((row.cost or 0.0) + kwh * slot.raw_price, 2)
                totals[slot.known][0] += kwh
                totals[slot.known][1] += kwh * slot.raw_price
            row.calibration = row.calibration or ch.calibration
            row.status = "charge"
            charged += q[i]
        level += q[i] - slot.use

    # Today's trips are over and the car is home: today's km no longer matter.
    today = rows.get(now.date())
    if today is not None and today.soc_departure is None and rest_km == 0:
        today.km = 0.0

    return Plan(
        target_soc=int(min(round(soc + charge_now), 100)),
        charge_until=charge_until,
        charge_now=round(charge_now, 2),
        hours=hours,
        rows=[rows[d] for d in sorted(rows)],
        unmet=round(unmet, 1),
        calibration=bool(cal_hours),
        remaining_km=round(rest_km, 1),
        soc_home=round(soc, 1),
        kwh_known=round(totals[True][0], 2),
        cost_known=round(totals[True][1], 2),
        kwh_forecast=round(totals[False][0], 2),
        cost_forecast=round(totals[False][1], 2),
    )
