"""Alerts: missing information and things the user should act on (DESIGN §5.6).

Pure Python. The coordinator builds a ``Snapshot`` on every update; ``detect``
turns it into the current issues, and ``AlertTracker`` decides which
notifications to send: an issue is notified once it has lasted its grace
period, repeated every ``REMIND_HOURS`` while it lasts, and a short "resolved"
message follows once it is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

PRICES_TOMORROW = "prices_tomorrow"
PRICES_NOW = "prices_now"
PRICES_AHEAD = "prices_ahead"
CALENDAR = "calendar"
ROUTING = "routing"
SOC = "soc"
UNMET = "unmet"
PLUG_IN = "plug_in"
CHARGE_SPEED = "charge_speed"

ALL_ALERTS: tuple[str, ...] = (PLUG_IN, UNMET, SOC, PRICES_NOW, PRICES_TOMORROW, PRICES_AHEAD, CALENDAR, ROUTING, CHARGE_SPEED)

# How long an issue must last before it is notified.
GRACE = {
    PRICES_TOMORROW: timedelta(0),  # only raised after TOMORROW_PRICES_HOUR
    PRICES_NOW: timedelta(hours=1),
    PRICES_AHEAD: timedelta(hours=24),  # forecasts are optional: only a lasting gap matters
    CALENDAR: timedelta(hours=1),
    ROUTING: timedelta(hours=1),
    SOC: timedelta(hours=1),
    UNMET: timedelta(minutes=15),
    PLUG_IN: timedelta(0),
    CHARGE_SPEED: timedelta(hours=1),
}
REMIND_HOURS = 24
TOMORROW_PRICES_HOUR = 15  # day-ahead prices are normally published around 13:00
AHEAD_DAYS = 3  # dates checked for any price (known or forecast)
PLUG_IN_HOURS = 2  # remind this long before the first planned charging hour


@dataclass
class Snapshot:
    """What the coordinator knows after an update."""

    now: datetime
    soc_ok: bool = True
    known_until: date | None = None
    price_dates: set[date] = field(default_factory=set)  # dates with any price
    price_now: bool = True  # a known price for the current hour
    calendar_error: bool = False
    missing_legs: list[str] = field(default_factory=list)
    unmet: float = 0.0
    unmet_day: date | None = None
    first_charge: datetime | None = None
    car_home: bool | None = None
    plugged: bool | None = None  # None: no plugged-in sensor
    charge_speed_fallback: bool = False


TEXTS = {
    "en": {
        PRICES_TOMORROW: ("Tomorrow's electricity prices are missing", "No known prices for {d} yet. Charging is planned with forecasts until they arrive."),
        PRICES_NOW: ("Electricity prices are missing", "There is no known price for the current hour. Check the price integration."),
        PRICES_AHEAD: ("No prices for the coming days", "No known or forecast prices for {d}. The plan cannot charge on those dates."),
        CALENDAR: ("Calendar could not be read", "The calendar has not been read for over an hour. The plan uses the last trips it knows."),
        ROUTING: ("Trips could not be routed", "km and travel time are missing for: {legs}. They count 30 min and 0 km until Waze answers."),
        SOC: ("Battery level is missing", "The car's battery level has been unavailable for over an hour. The plan is not updated."),
        UNMET: ("Not enough time to charge", "The car cannot reach the needed level by the departure on {d} ({pct} % short)."),
        PLUG_IN: ("Plug in the car", "Charging is planned from {t}, but the car is not plugged in."),
        CHARGE_SPEED: ("Charging speed is missing", "The charging speed entity has no usable value; the Charging power number is used."),
        "resolved": "✅ OK again: {title}",
    },
    "da": {
        PRICES_TOMORROW: ("Morgendagens elpriser mangler", "Der er endnu ingen kendte priser for {d}. Ladningen planlægges med prognoser, til de kommer."),
        PRICES_NOW: ("Elpriserne mangler", "Der er ingen kendt pris for den aktuelle time. Tjek pris-integrationen."),
        PRICES_AHEAD: ("Ingen priser for de kommende dage", "Ingen kendte priser eller prognoser for {d}. Planen kan ikke lade på de dage."),
        CALENDAR: ("Kalenderen kan ikke læses", "Kalenderen er ikke læst i over en time. Planen bruger de sidste ture, den kender."),
        ROUTING: ("Ture kan ikke ruteberegnes", "Km og køretid mangler for: {legs}. De tæller 30 min og 0 km, til Waze svarer."),
        SOC: ("Batteriniveauet mangler", "Bilens batteriniveau har været utilgængeligt i over en time. Planen opdateres ikke."),
        UNMET: ("Ikke tid nok til at lade", "Bilen kan ikke nå det nødvendige niveau til afgangen {d} ({pct} % mangler)."),
        PLUG_IN: ("Sæt bilen til", "Der er planlagt ladning fra kl. {t}, men bilen er ikke sat til."),
        CHARGE_SPEED: ("Ladehastigheden mangler", "Enheden for ladehastighed har ingen brugbar værdi; tallet Ladeeffekt bruges."),
        "resolved": "✅ I orden igen: {title}",
    },
}


def _texts(language: str | None) -> dict:
    return TEXTS["da"] if (language or "").lower().startswith("da") else TEXTS["en"]


def detect(s: Snapshot, language: str | None = "en") -> dict[str, tuple[str, str]]:
    """Current issues: key → (title, message)."""
    t = _texts(language)
    out: dict[str, tuple[str, str]] = {}
    today = s.now.date()
    tomorrow = today + timedelta(days=1)

    def add(key: str, **fmt) -> None:
        title, message = t[key]
        out[key] = (title, message.format(**fmt))

    if not s.soc_ok:
        add(SOC)
    if not s.price_now:
        add(PRICES_NOW)
    if s.now.hour >= TOMORROW_PRICES_HOUR and (s.known_until is None or s.known_until < tomorrow):
        add(PRICES_TOMORROW, d=tomorrow.isoformat())
    gaps = [today + timedelta(days=i) for i in range(1, AHEAD_DAYS + 1) if today + timedelta(days=i) not in s.price_dates]
    if gaps:
        add(PRICES_AHEAD, d=", ".join(d.isoformat() for d in gaps))
    if s.calendar_error:
        add(CALENDAR)
    if s.missing_legs:
        add(ROUTING, legs="; ".join(s.missing_legs[:5]) + (" …" if len(s.missing_legs) > 5 else ""))
    if s.unmet > 0.5:
        add(UNMET, d=s.unmet_day.isoformat() if s.unmet_day else "?", pct=round(s.unmet))
    if (
        s.plugged is False
        and s.car_home is not False
        and s.first_charge is not None
        and s.now <= s.first_charge <= s.now + timedelta(hours=PLUG_IN_HOURS)
    ):
        add(PLUG_IN, t=s.first_charge.strftime("%H:%M"))
    if s.charge_speed_fallback:
        add(CHARGE_SPEED)
    return out


@dataclass
class Notification:
    key: str
    kind: str  # new | reminder | resolved
    title: str
    message: str


@dataclass
class _Active:
    since: datetime
    title: str
    message: str
    notified: datetime | None = None


class AlertTracker:
    """Turns the current issues of every update into notifications to send.

    All issues are tracked (for the problem sensor); only the ``enabled`` ones
    are notified.
    """

    def __init__(self) -> None:
        self.active: dict[str, _Active] = {}

    def update(
        self,
        now: datetime,
        issues: dict[str, tuple[str, str]],
        enabled: set[str] | frozenset[str],
        language: str | None = "en",
    ) -> list[Notification]:
        out: list[Notification] = []
        for key in list(self.active):
            if key not in issues:
                gone = self.active.pop(key)
                if gone.notified is not None:
                    title = _texts(language)["resolved"].format(title=gone.title)
                    out.append(Notification(key, "resolved", title, ""))
        for key, (title, message) in issues.items():
            a = self.active.get(key)
            if a is None:
                a = self.active[key] = _Active(now, title, message)
            a.title, a.message = title, message
            if key not in enabled:
                continue  # tracked (shown on the problem sensor) but not notified
            if a.notified is None:
                if now - a.since >= GRACE.get(key, timedelta(0)):
                    a.notified = now
                    out.append(Notification(key, "new", title, message))
            elif now - a.notified >= timedelta(hours=REMIND_HOURS):
                a.notified = now
                out.append(Notification(key, "reminder", title, message))
        return out

    def current(self) -> dict[str, dict]:
        """Active issues for a sensor attribute."""
        return {k: {"title": a.title, "message": a.message, "since": a.since.isoformat(), "notified": a.notified is not None} for k, a in self.active.items()}
