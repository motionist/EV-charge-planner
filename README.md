# EV Charge Planner

Calculation of cheapest charging based on planned consumption (estimated based on calendar).

> **Status: v0.1 – design + skeleton, not yet ready for general use.**
> The planner core is implemented and unit tested; the Home Assistant
> integration around it is a first skeleton. See [docs/DESIGN.md](docs/DESIGN.md).

A Home Assistant custom integration (installable via HACS) that looks up to
7 days ahead, **hour by hour**, at electricity prices and your planned trips,
and works out in which hours the car should charge and how much – enough for
the trips plus a safety buffer, bought at the cheapest hours.

## What it does

* Every hour the car is **home** is a candidate – no fixed night or day
  windows. The current hour counts with its remaining minutes.
* Every **departure** (first away hour from the calendar, else 08:00) needs
  a floor (weekday/weekend), or the day's trips plus a **buffer** (default
  100 km), or the max level before a night away.
* Each need is charged in the **cheapest earlier hour** with room. So an
  hour only charges enough to reach a **cheaper** hour later; if nothing
  cheaper comes in time it **tops up** – never above the max level (default
  90 %). A long trip later in the week is topped up in the cheapest hour
  before it.
* **High cap before long trips**: right before a long trip (default ≥ 100
  km) it may charge up to 100 % when that is at least 30 % cheaper than
  charging after the trip – never days in advance.
* **Car away now**: with a location tracker, the way home is taken from the
  SoC while the car is out, so tomorrow's plan does not count on energy that
  will be used on the way back.
* Optional periodic **battery calibration** charge in a cheap hour.
* Uses known prices first, then a price forecast (e.g. Open Spot Forecast)
  as far as it reaches, then a fallback forecast (e.g. Energi Data Service)
  – calibrated per hour of day to the forecast's level and weighed by its
  larger uncertainty.
* Weighs a **known price higher than a forecast**: when a forecast day's
  accuracy (from the forecast's own error statistics) is below 90 %, its
  prices are divided by the accuracy before they are compared – so a known 0.74 today beats a 0.65 forecast for
  tomorrow at 84 % accuracy (0.77). See [DESIGN.md §5.0](docs/DESIGN.md).
* **Hands over** only the certain part: the hours with known prices before
  the next departure (max 24 h) become a target SoC and a "charge until"
  time for EV Smart Charging or your charger integration. The rest is the
  expected plan, recomputed as prices arrive.

## Entities

| Entity | Description |
|---|---|
| `sensor.<name>_target_soc` | Target SoC to hand over – charge to this level before *charge until*. Feed it to EV Smart Charging, your charger integration or an automation. |
| `sensor.<name>_charge_until` | End of the hand-over (next departure or last known price hour) – e.g. EV Smart Charging's completion time. |
| `sensor.<name>_next_charging_hour` | The first planned charging hour (known or forecast). |
| `sensor.<name>_plan` | The full plan: `hours` (every planned hour with %, kWh, price and known/forecast), `rows` (one per date) and `totals` (kWh and cost for known and forecast prices) for your own dashboard – see [docs/example-card.md](docs/example-card.md). |
| `number.<name>_*` | Parameters: floors, max level, high cap before long trips (level, km, price advantage), buffer (km), battery capacity, charging power, calibration settings, forecast min. accuracy and far factor. |

## Inputs (v0.1)

Selected in the config flow:

* Battery level (SoC) sensor, optional plugged-in/home binary sensor,
  optional car location (device tracker).
* Charging speed: set the *Charging power* number manually, or pick an
  entity in the config/options flow – e.g. EV Smart Charging's charging
  speed (%/h) or a charger power sensor (kW/W). The number is the fallback
  while the entity is unavailable.
* Price sensor: **Energi Data Service** (known prices + its forecast),
  **Nord Pool** (custom integration) or a generic list attribute.
* Optional price forecast sensor (**Open Spot Forecast**) and its error
  sensors (`forecast_mae_day_1…4`, in that order) for the forecast price
  adjustment.
* Optional efficiency sensor (kWh/km), otherwise a fixed value.
* **Trips from your calendars**: pick one or more calendar entities (Google,
  iCloud/CalDAV, Local Calendar, ICS …). Events with a location are car
  trips; km and travel time come from the **Waze Travel Time** integration
  (no API key). Online meetings and events marked `#tog`/`#ingenbil` are
  skipped, home words (e.g. your town) count as home, and the car goes home
  between two events when there is at least an hour at home. Details in
  [DESIGN.md §3.1](docs/DESIGN.md).
* Or trips via text helpers, e.g. calculated with HERE routing as described
  in [docs/here-routing.md](docs/here-routing.md):
  * km per day: `2026-10-08=49.6;2026-10-12=49.6;`
  * away hours: `2026-10-08=8-17;2026-10-15=8-23`
  * away nights: `2026-10-10,2026-10-11` (car away the night after these dates)
* Optional public-holiday calendar, optional last-calibration datetime.

## Alerts

A **Problem** sensor shows when information is missing or you should act –
e.g. *plug in the car* (charging planned within 2 h but not plugged in), *not
enough time to charge*, tomorrow's prices missing after 15:00, no prices for
the coming days for 24 h, the calendar or routing failing, the battery level
missing. In the options you can send these alerts to Home Assistant
notifications and/or your phone (companion app) and pick which ones. See
[DESIGN.md §5.6](docs/DESIGN.md).

Entity IDs follow the language Home Assistant had when the integration was
added (Danish: `_mal_soc`, `_naeste_ladetime`, `_lad_faerdig_senest`,
`_ladeplan`).

## Installation (beta)

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/motionist/EV-charge-planner` as *Integration*.
2. Install **EV Charge Planner**, restart Home Assistant.
3. Settings → Devices & services → Add integration → EV Charge Planner.

Tested on Home Assistant 2026.10. This is a beta: run it in **shadow mode**
first (let it plan while your current setup controls the charger), then
hand the plan over – see [docs/ev-smart-charging.md](docs/ev-smart-charging.md).

## Testing and feedback

Report anything odd as a [GitHub issue](https://github.com/motionist/EV-charge-planner/issues/new/choose)
with the plan sensor's `rows` and `hours` attributes. The `trips`
attribute contains your calendar locations – remove them before posting.

Known limitations (v0.1):

* One car per config entry (add the integration once per car).
* Charging losses are not included in kWh and cost.
* Open Spot Forecast only covers Denmark (DK1/DK2).
* Travel times come from Waze and can differ from your own experience –
  check the `trips` attribute.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The planner (`custom_components/ev_charge_planner/planner/`) and adapters
are pure Python and do not need Home Assistant to run the tests.

## License

MIT
