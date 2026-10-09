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
| **Forecast accuracy** | Per date that only has forecast prices: `1 − MAE ÷ mean known price`, where MAE is the forecast's mean absolute error for that lead day (see §5.0). Known prices have accuracy 1. |
| **Adjusted price** | The price the planner compares opportunities by: the known price, or a forecast price divided by its accuracy when the accuracy is below `forecast_min_accuracy`. A known price therefore weighs more than a forecast of the same size. |

## 3. Inputs

| Input | Source (configurable) | Notes |
|---|---|---|
| Current SoC | sensor entity | % |
| Car at home / plugged in | binary_sensor (optional) | planner still produces a plan when unplugged |
| Hourly prices | price adapter | layered: **known prices** (today/tomorrow) override **forecast** (e.g. Open Spot Forecast, next ~4 days) which override a **fallback forecast** (e.g. Energi Data Service forecast). Missing hours ⇒ opportunity has no price and is never treated as "cheaper". |
| Forecast error per lead day | sensor(s), optional | MAE in currency/kWh for lead day 1–4, e.g. Open Spot Forecast's `forecast_mae_day_1…4` sensors. Lead days beyond the last sensor use the last value × `forecast_far_factor`. Missing ⇒ forecasts are trusted (no adjustment). |
| km per day | trip adapter | v0.1: legacy text format `YYYY-MM-DD=km;…`, e.g. produced from Google Calendar with HERE routing – see [here-routing.md](here-routing.md). v0.2: calendar + routing built in. |
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

### 5.0 Forecast price adjustment

A known (day-ahead) price is certain; a forecast is not. Comparing them
at face value makes the planner wait for a forecast night that is only
slightly cheaper and often turns out more expensive. Therefore every
forecast price is made "worse" in proportion to how unreliable the
forecast is before any comparison:

1. `last_known` = the last date covered by known prices.
2. For each date *d* after `last_known`: `lead = d − last_known` (days),
   `mae = mae_day[min(lead, 4)]` (× `forecast_far_factor` when lead > 4),
   `accuracy = 1 − mae ÷ mean(known prices)`.
3. `factor = 1` if `accuracy ≥ forecast_min_accuracy`, else
   `max(accuracy, 0.3)` (the floor prevents absurd prices when the
   forecast is very poor).
4. Every hourly price of *d* (night and day window alike):
   `adjusted = price ÷ factor` if `price > 0`, else `price × factor` (so an
   uncertain negative price is not made *more* attractive).
5. Known prices are never adjusted.

All comparisons in §5.1–5.3 (which opportunity is cheaper, calibration
price thresholds) use the adjusted price; displays and the plan rows
show both the raw and the adjusted price.

Example: tonight's known price is 0.74, tomorrow night's forecast is 0.65
with day-1 accuracy 84 % ⇒ adjusted 0.65 ÷ 0.84 = 0.77 > 0.74, so tonight
is the cheaper opportunity and charges for tomorrow as well. The same
applies to today's day window: a known 0.74 at noon beats a forecast 0.66
tonight when that forecast is only 84 % accurate (adjusted 0.79).

### 5.1 Target for one opportunity

For opportunity *k* with (adjusted, §5.0) price `p_k`:

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
  (`charge`, `covered`, `not_needed`), and the forecast factor plus
  adjusted night/day prices when the date only has forecast prices.

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
| Forecast min. accuracy (`forecast_min_accuracy`) | 90 | % |
| Forecast far factor (`forecast_far_factor`, lead > 4 days) | 1.25 | × |

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
   Includes the forecast price adjustment (§5.0): `known_until` from the
   known price layer, MAE sensors in the config flow, the two parameters
   as number entities, and tests for the 0.74 known vs 0.65 forecast and
   the known day window vs forecast night cases.
2. **v0.2** – calendar trip adapter (events with location → km via a
   routing provider; travel time → away hours; multi-day events → away
   overnight), Nordpool/Tibber adapters, workday holidays.
3. **v0.3** – optional direct control helpers (blueprint for EV Smart
   Charging start/completion time), example dashboard card, HACS default
   listing (brands, docs, translations).

## 9. Open questions

* Forecast accuracy: the reference is the mean of the currently known
  prices (today/tomorrow). A longer reference (e.g. 30-day mean price)
  would make the factor steadier across cheap and expensive days. The
  MAE could also come from the planner's own log of forecast vs actual
  (as the private installation does) instead of the forecast provider.

* Should the night window extend before midnight (e.g. 22:00–08:00)? The
  original used 00–08 because tariffs are lowest then.
* Day-window charging requires the charger to be allowed to start during
  the day; document per charger integration.
* Calibration "last done" – detect automatically (SoC ≥ calibration SoC) or
  require a helper? v0.1: optional datetime entity; v0.2: auto-detect.
