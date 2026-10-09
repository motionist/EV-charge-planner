# EV Charge Planner

Calculation of cheapest charging based on planned consumption (estimated based on calendar).

> **Status: v0.1 – design + skeleton, not yet ready for general use.**
> The planner core is implemented and unit tested; the Home Assistant
> integration around it is a first skeleton. See [docs/DESIGN.md](docs/DESIGN.md).

A Home Assistant custom integration (installable via HACS) that looks up to
7 days ahead at electricity prices and your planned trips, and publishes the
battery level your EV should have after the next charging opportunity –
enough for the trips plus a safety buffer, bought at the cheapest hours.

## What it does

* Considers every **night** (00–08) and every **day window** when the car is
  home (08–17 outside your away hours) as a charging opportunity.
* Each opportunity charges just enough to reach the next **cheaper**
  opportunity: a floor at every departure (weekday/weekend), the trips in
  between and a **buffer** (default 100 km) left after them – capped at a
  max level (default 90 %).
* Simulates the week, so a capped charge that cannot cover a long trip later
  is topped up at a later opportunity.
* Optional periodic **battery calibration** charge on a cheap night.
* Uses known prices first, then a price forecast (e.g. Open Spot Forecast),
  then a fallback forecast (e.g. Energi Data Service).
* Weighs a **known price higher than a forecast** (design, not yet in the
  code): when a forecast day's accuracy (from the forecast's own error
  statistics) is below 90 %, its prices are divided by the accuracy before
  they are compared – so a known 0.74 today beats a 0.65 forecast for
  tomorrow at 84 % accuracy (0.77). See [DESIGN.md §5.0](docs/DESIGN.md).

## Entities

| Entity | Description |
|---|---|
| `sensor.<name>_target_soc` | Target SoC for the next charge – feed it to EV Smart Charging, your charger integration or an automation. |
| `sensor.<name>_next_charge_start` / `_end` | The next charging window. |
| `sensor.<name>_plan` | The full plan in the `rows` attribute (one row per date) for your own dashboard – see [docs/example-card.md](docs/example-card.md). |
| `number.<name>_*` | Parameters: floors, max level, buffer (km), battery capacity, calibration settings. |

## Inputs (v0.1)

Selected in the config flow:

* Battery level (SoC) sensor, optional plugged-in/home binary sensor.
* Price sensor: **Energi Data Service** (known prices + its forecast),
  **Nord Pool** (custom integration) or a generic list attribute.
* Optional price forecast sensor (**Open Spot Forecast**).
* Optional efficiency sensor (kWh/km), otherwise a fixed value.
* Trips via text helpers (calendar adapter planned for v0.2). How the km per
  day can be calculated from Google Calendar with HERE routing is described in
  [docs/here-routing.md](docs/here-routing.md):
  * km per day: `2026-10-08=49.6;2026-10-12=49.6;`
  * away hours: `2026-10-08=8-17;2026-10-15=8-23`
  * away nights: `2026-10-10,2026-10-11` (car away the night after these dates)
* Optional public-holiday calendar, optional last-calibration datetime.

## Installation (development)

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/motionist/EV-charge-planner` as *Integration*.
2. Install **EV Charge Planner**, restart Home Assistant.
3. Settings → Devices & services → Add integration → EV Charge Planner.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The planner (`custom_components/ev_charge_planner/planner/`) and adapters
are pure Python and do not need Home Assistant to run the tests.

## License

MIT
