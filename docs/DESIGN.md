# EV Charge Planner – design

Status: draft v0.1 (design + skeleton). The algorithm described here is a
port of a planner that has been running as Jinja templates in a private Home
Assistant installation (Polestar 2 + go-e charger + EV Smart Charging,
Danish spot prices). This document is the reference for the Python port.

## 1. Goal

Charge an EV as cheaply as possible while guaranteeing that the car has
enough energy for the trips that are planned in the calendar – plus a
safety buffer – without the user having to think about it.

The planner answers one question every time something changes:

> *What should the battery level be when the next charging opportunity
> ends?*

It does that by looking up to 7 days ahead at (a) electricity prices and
(b) planned driving, and by simulating the battery night by night.

Non-goals:

* Controlling the charger directly. The planner publishes a target SoC and a
  charging window; an existing integration (EV Smart Charging, the charger's
  own integration, an automation) does the actual switching.
* Dashboards. The full plan is published as entity attributes; users build
  their own cards. An example Markdown card is provided in `docs/`.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Night** of date *d* | Hours `00:00 – night_end` (default 08:00) on date *d*. The car is normally plugged in at home. |
| **Day window** of date *d* | Hours `day_start – day_end` (default 08–17) on date *d* during which the car is **home** (not inside an *away* interval). Only exists if at least one such hour exists. |
| **Opportunity** | A night or a day window, i.e. a period in which the car can charge. Its price is the cheapest hour inside it (all-in price incl. tariffs/VAT, whatever the price sensor reports). |
| **Away hours** | Per date: hour intervals when the car is away from home (derived from the calendar incl. travel time). |
| **Away overnight** | The car is away the night *after* date *d* (e.g. hotel). That night is not an opportunity, and the night before leaving is charged to `max_level`. |
| **Departure** | The first time the car leaves on date *d*. At every departure the SoC must be ≥ the **floor** of that date. |
| **Floor** | Minimum SoC at departure: `floor_weekday` or `floor_weekend` (weekend = Sat/Sun or a public holiday). |
| **Buffer** | Range that must remain *after* the planned trips: `buffer_km` (default 100 km) converted to %. |
| **Max level** | Upper cap for normal charging (default 90 %). Long trips may therefore not be able to keep the full buffer – this is accepted. |
| **Consumption** | `km × efficiency_kwh_per_km ÷ battery_kwh × 100` (% of battery). |

## 3. Inputs

| Input | Source (configurable) | Notes |
|---|---|---|
| Current SoC | sensor entity | % |
| Car at home / plugged in | binary_sensor (optional) | planner still produces a plan when unplugged |
| Hourly prices | price adapter | layered: **known prices** (today/tomorrow) override **forecast** (e.g. Open Spot Forecast, next ~4 days) which override a **fallback forecast** (e.g. Energi Data Service forecast). Missing hours ⇒ opportunity has no price and is never treated as "cheaper". |
| km per day | trip adapter | v0.1: legacy text format `YYYY-MM-DD=km;…`. Later: calendar + routing provider. |
| Away hours | trip adapter | v0.1: legacy text `YYYY-MM-DD=8-17;…`. Later: calendar events with location + travel time. |
| Away overnight | trip adapter | v0.1: legacy text `YYYY-MM-DD,YYYY-MM-DD`. |
| Public holidays | `workday` binary sensor or holiday calendar | optional |
| Efficiency | sensor (kWh/km) or fixed number | |
| Battery capacity | number (kWh) | |
| Parameters | `number` entities owned by the integration | see §6 |

## 4. Timeline

The planner builds a chronological timeline of events, starting *now*:

```
[today's remaining driving] [today's day window] [rest of today's driving]
night(d0) depart(d0) drive_before(d0) day(d0) drive_after(d0)
night(d1) depart(d1) ...                                   ... up to 7 dates
```

* **Night mode.** Between 00:00 and `night_end` the night in progress
  belongs to *today* (`d0 = today`). Otherwise `d0 = tomorrow`, and the
  rest of today (remaining day window + remaining driving) comes first. This
  avoids the "target jumps at midnight" bug of the original templates.
* A date's driving is split around its day window in proportion to the
  away hours that lie before/after the window start. Km without away hours
  is treated as happening before the window.
* For *today*, only away hours that have not yet passed are counted (the
  current SoC already reflects earlier driving).

## 5. Algorithm

### 5.1 Target for one opportunity

For opportunity *k* with price `p_k`:

1. Walk forward through the timeline from *k*.
2. Stop at the first later **available** opportunity *j* with `p_j < p_k`
   (a cheaper chance to charge again). If none exists, stop at the horizon.
3. While walking, keep `consumed` = sum of consumption so far and, at each
   departure *d*, `need = max(need, floor(d) + consumed)`.
4. `extra = consumed + buffer%` if `consumed > 0` else 0.
5. `target = min(max(need, extra), max(max_level, first_floor))`, rounded.
6. If the car is away the night after the date of *k* (and *k* is that
   date's night) ⇒ `target = max_level` (charge full before the trip).

### 5.2 Calibration (optional)

Lithium batteries' SoC estimate drifts; periodically charging high
recalibrates it. Only night opportunities qualify.

* `days_since` = days since last calibration at the start of the night.
* **Early grab**: `days_since ≥ early_days` and `p ≤ threshold` and no
  later opportunity in the horizon is cheaper.
* **Due**: `days_since ≥ interval_days` and `p ≤ threshold × (1 + esc%)^weeks_over`.
* If either holds: `target = max(target, calibration_soc)` and the
  simulation records this night as the new calibration time.
* `interval_days = 0` disables calibration.

### 5.3 Simulation (the plan)

Starting at the current SoC, walk the timeline:

* opportunity ⇒ compute its target (5.1 + 5.2); if `soc < target` charge to
  target at the opportunity's cheapest hour, otherwise mark it *covered* or
  *not needed*;
* drive ⇒ `soc -= consumption`.

Because every opportunity recomputes its own target (as the live planner
will do when that moment arrives), a capped charge (e.g. 90 %) that cannot
cover a long trip later in the week is automatically topped up by a later
opportunity – the planner never assumes coverage it cannot deliver.

### 5.4 Outputs

* **Target SoC** = target of the next opportunity in time that will
  charge; if none will charge, the target of the next opportunity.
* **Next charge window** = start/end hour of that opportunity (night:
  `00:00–night_end`, day: the contiguous home block around its cheapest
  hour). Useful for setting EV Smart Charging start/completion times.
* **Plan** = one row per date with: date, type (weekday/weekend/holiday),
  km, night price + cheapest hour, night target, day price + hour, day
  target, expected SoC at departure, calibration flag, away flags, status
  (`charge`, `covered`, `not_needed`).

## 6. Parameters (number entities)

| Entity | Default | Unit |
|---|---|---|
| Floor weekday | 50 | % |
| Floor weekend/holiday | 40 | % |
| Max level | 90 | % |
| Buffer after trips | 100 | km |
| Battery capacity | 75 | kWh |
| Calibration SoC | 100 | % |
| Calibration interval | 0 (off) | days |
| Calibration early grab | 0 | days |
| Calibration price threshold | 0 | currency/kWh |
| Calibration escalation | 10 | %/week |

Night end, day start/end and horizon are options (config flow) rather than
entities.

## 7. Integration architecture

```
custom_components/ev_charge_planner/
  planner/        pure Python, no Home Assistant imports – unit tested
    models.py     dataclasses (HourPrice, DayInfo, Settings, Opportunity, PlanRow, Plan)
    core.py       timeline, target, calibration, simulation
  adapters/       translate HA states → planner inputs
    prices.py     Energi Data Service, Nordpool, Open Spot Forecast, generic
    trips.py      legacy text helpers (v0.1); calendar + routing (later)
  coordinator.py  DataUpdateCoordinator: collects inputs, runs planner
  config_flow.py  entity selection + options
  sensor.py       target SoC, next window start/end, plan (attributes)
  number.py       parameters (RestoreNumber)
```

Update triggers: every 5 minutes, on state change of any input entity, on
parameter change.

## 8. Roadmap

1. **v0.1** – this skeleton: planner core with tests reproducing the
   private installation, legacy text adapters, EDS/OSF price adapter,
   sensors + numbers. Side-by-side run against the private installation.
2. **v0.2** – calendar trip adapter (events with location → km via a
   routing provider; travel time → away hours; multi-day events → away
   overnight), Nordpool/Tibber adapters, workday holidays.
3. **v0.3** – optional direct control helpers (blueprint for EV Smart
   Charging start/completion time), example dashboard card, HACS default
   listing (brands, docs, translations).

## 9. Open questions

* Should the night window extend before midnight (e.g. 22:00–08:00)? The
  original used 00–08 because tariffs are lowest then.
* Day-window charging requires the charger to be allowed to start during
  the day; document per charger integration.
* Calibration "last done" – detect automatically (SoC ≥ calibration SoC) or
  require a helper? v0.1: optional datetime entity; v0.2: auto-detect.
