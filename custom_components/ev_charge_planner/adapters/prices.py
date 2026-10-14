"""Price adapters.

Each adapter takes the attributes of a price sensor and returns a list of
``HourPrice`` (one per hour, all-in price as reported by the sensor).
Sub-hourly data (e.g. 15-minute slots) is averaged per hour.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from ..planner.models import HourPrice

ADAPTER_ENERGI_DATA_SERVICE = "energi_data_service"
ADAPTER_NORDPOOL = "nordpool"
ADAPTER_OPEN_SPOT_FORECAST = "open_spot_forecast"
ADAPTER_GENERIC = "generic"

TIME_KEYS = ("hour", "start", "dt", "time", "datetime")
PRICE_KEYS = ("price", "value", "total", "t")


def _to_dt(value: Any, tz) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz) if tz is not None else dt


def _hourly(points: Iterable[tuple[datetime, float]], source: str) -> list[HourPrice]:
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for dt, price in points:
        buckets[dt.replace(minute=0, second=0, microsecond=0)].append(price)
    return [HourPrice(h, sum(v) / len(v), source) for h, v in sorted(buckets.items())]


def from_list(items: Iterable[Mapping[str, Any]] | None, tz, source: str) -> list[HourPrice]:
    """Generic list of dicts with a time key and a price key."""
    points = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        t = next((item[k] for k in TIME_KEYS if k in item), None)
        p = next((item[k] for k in PRICE_KEYS if k in item), None)
        dt = _to_dt(t, tz)
        if dt is None or p is None:
            continue
        try:
            points.append((dt, float(p)))
        except (TypeError, ValueError):
            continue
    return _hourly(points, source)


def energi_data_service(attrs: Mapping[str, Any], tz) -> tuple[list[HourPrice], list[HourPrice]]:
    """Energi Data Service: (known prices, built-in forecast)."""
    known = from_list(list(attrs.get("raw_today") or []) + list(attrs.get("raw_tomorrow") or []), tz, "known")
    forecast = from_list(attrs.get("forecast") or [], tz, "fallback")
    return known, forecast


def nordpool(attrs: Mapping[str, Any], tz) -> list[HourPrice]:
    """Nordpool (custom integration): raw_today/raw_tomorrow with start/value."""
    return from_list(list(attrs.get("raw_today") or []) + list(attrs.get("raw_tomorrow") or []), tz, "known")


def open_spot_forecast(attrs: Mapping[str, Any], tz) -> list[HourPrice]:
    """Open Spot Forecast: compact {'s': [epoch…], 't': [price…]} or a list of dicts."""
    pred = attrs.get("predictions")
    if isinstance(pred, Mapping) and "s" in pred and "t" in pred:
        points = []
        for s, t in zip(pred["s"], pred["t"]):
            dt = _to_dt(s, tz)
            if dt is not None and t is not None:
                points.append((dt, float(t)))
        return _hourly(points, "forecast")
    if isinstance(pred, list):
        return from_list(pred, tz, "forecast")
    return []
