"""Trip adapters.

v0.1 supports the "legacy" text helpers used by the original installation:

* km per day:      ``2026-10-08=49.6;2026-10-12=49.6;``
* away hours:      ``2026-10-08=8-17;2026-10-15=8-23``  (several entries per day allowed;
                   an end of 0 means midnight, e.g. ``18-0``)
* away overnight:  ``2026-10-10,2026-10-11``  (car away the night after these dates)

A calendar + routing adapter is planned for v0.2 (docs/DESIGN.md §8).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from ..planner.models import DayInfo


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_km(text: str | None) -> dict[date, float]:
    out: dict[date, float] = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        d_s, km_s = part.split("=", 1)
        d = _parse_date(d_s)
        try:
            km = float(km_s.strip().replace(",", "."))
        except ValueError:
            continue
        if d is not None:
            out[d] = out.get(d, 0.0) + km
    return out


def parse_away_hours(text: str | None) -> dict[date, list[tuple[int, int]]]:
    out: dict[date, list[tuple[int, int]]] = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        d_s, rng = part.split("=", 1)
        d = _parse_date(d_s)
        if d is None or "-" not in rng:
            continue
        s_s, e_s = rng.split("-", 1)
        try:
            s, e = int(s_s), int(e_s)
        except ValueError:
            continue
        if e == 0:
            e = 24  # until midnight
        if 0 <= s < e <= 24:
            out.setdefault(d, []).append((s, e))
    return out


def parse_dates(text: str | None) -> set[date]:
    return {d for d in (_parse_date(p) for p in (text or "").split(",") if p.strip()) if d is not None}


def build_days(
    km_text: str | None,
    away_text: str | None,
    overnight_text: str | None,
    holidays: Iterable[date] = (),
) -> dict[date, DayInfo]:
    """Combine the legacy helpers into planner ``DayInfo`` objects."""
    km = parse_km(km_text)
    away = parse_away_hours(away_text)
    overnight = parse_dates(overnight_text)
    hol = set(holidays)
    out: dict[date, DayInfo] = {}
    for d in set(km) | set(away) | overnight | hol:
        out[d] = DayInfo(
            day=d,
            km=km.get(d, 0.0),
            away_hours=sorted(away.get(d, [])),
            away_overnight=d in overnight,
            holiday=d in hol,
        )
    return out
