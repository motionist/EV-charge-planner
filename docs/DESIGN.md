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

> *In which hours should the car charge, and how much, to have enough for
> every planned trip at the lowest price?*

It does that by looking up to 7 days ahead, **hour by hour**, at (a)
electricity prices and (b) planned driving. There are no fixed night or day
windows: prices vary from hour to hour, so every hour the car is at home is a
candidate.

Non-goals:

* Controlling the charger directly. The planner publishes a target SoC and a
  "charge until" time; an existing integration (EV Smart Charging, the
  charger's own integration, an automation) picks the exact quarters/hours
  and does the switching.
* Dashboards. The full plan is published as entity attributes; users build
  their own cards. An example Markdown card is provided in `docs/`.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Hour slot** | One hour from now over the horizon, with its price (all-in incl. tariffs/VAT, whatever the price sensor reports) and its **capacity**: `charge_kw ÷ battery_kwh × 100` % if the car is home and the hour has a price, else 0. The current hour only counts its remaining minutes. |
| **Away hours** | Per date: hour intervals when the car is away from home (derived from the calendar incl. travel time). |
| **Away overnight** | The car is away the night *after* date *d* (e.g. hotel): from its departure until `default_departure_hour` the next day (or that day's away hours). The departure before it requires `max_level`. |
| **Departure** | The first time the car leaves on date *d*: its first away hour, else `default_departure_hour` (08). The date's consumption is drawn here. After an away night the car does not depart from home, so that date has no requirement. |
| **Requirement** | SoC needed at a departure: `max(floor, consumption + buffer% if consumption > 0, max_level if away the following night)`, capped at `max(max_level, floor)`. |
| **Floor** | Minimum SoC at departure: `floor_weekday` or `floor_weekend` (weekend = Sat/Sun or a public holiday). |
| **Buffer** | Range that must remain *after* the planned trips: `buffer_km` (default 100 km) converted to %. |
| **Max level** | Upper cap for normal charging (default 90 %). Long trips may therefore not be able to keep the full buffer – this is accepted. |
| **Consumption** | `km × efficiency_kwh_per_km ÷ battery_kwh × 100` (% of battery). |
| **Forecast accuracy** | Per date that only has forecast prices: `1 − MAE ÷ mean known price`, where MAE is the forecast's mean absolute error for that lead day (see §5.0). Known prices have accuracy 1. |
| **Adjusted price** | The price the planner compares hours by: the known price, or a forecast price divided by its accuracy when the accuracy is below `forecast_min_accuracy`. A known price therefore weighs more than a forecast of the same size. |

## 3. Inputs

| Input | Source (configurable) | Notes |
|---|---|---|
| Current SoC | sensor entity | % |
| Car at home / plugged in | binary_sensor (optional) | planner still produces a plan when unplugged |
| Hourly prices | price adapter | layered: **known prices** (today/tomorrow) override **forecast** (e.g. Open Spot Forecast, next ~4 days) which override a **fallback forecast** (e.g. Energi Data Service forecast). Missing hours have no price: the car cannot be planned to charge in them. |
| Forecast error per lead day | sensor(s), optional | MAE in currency/kWh for lead day 1–4, e.g. Open Spot Forecast's `forecast_mae_day_1…4` sensors. Lead days beyond the last sensor use the last value × `forecast_far_factor`. Missing ⇒ forecasts are trusted (no adjustment). |
| km per day | trip adapter | v0.1: legacy text format `YYYY-MM-DD=km;…`, e.g. produced from Google Calendar with HERE routing – see [here-routing.md](here-routing.md). v0.2: calendar + routing built in. |
| Away hours | trip adapter | v0.1: legacy text `YYYY-MM-DD=8-17;…`. Later: calendar events with location + travel time. |
| Away overnight | trip adapter | v0.1: legacy text `YYYY-MM-DD,YYYY-MM-DD`. |
| Public holidays | `workday` binary sensor or holiday calendar | optional |
| Efficiency | sensor (kWh/km) or fixed number | |
| Battery capacity | number (kWh) | |
| Parameters | `number` entities owned by the integration | see §6 |

## 4. Hourly slots

The planner builds one slot per hour from the current hour over the horizon
(`horizon_days × 24` hours, continuous in UTC so DST changes are handled):

```
now ─ h0 ─ h1 ─ … ─ 08 depart(d1) ─ … ─ 08 depart(d2) ─ … ─ horizon
      price, capacity (0 when away / no price), departure: requirement + consumption
```

* The current hour's capacity is scaled by its remaining minutes. A
  departure in the current hour has already happened (its consumption is in
  the current SoC) unless it is exactly on the hour.
* Earlier versions used fixed night (00–08) and day (08–17) windows. That
  hid cheap hours outside the windows and made the planner compare a whole
  window by its cheapest hour; the hourly model has neither problem and no
  "night mode" special case.

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
4. Every hourly price of *d*:
   `adjusted = price ÷ factor` if `price > 0`, else `price × factor` (so an
   uncertain negative price is not made *more* attractive).
5. Known prices are never adjusted.

All comparisons in §5.1–5.3 (which hour is cheaper, calibration price
thresholds) use the adjusted price; the plan shows both the raw and the
adjusted price.

Example: tonight's known price is 0.74, tomorrow night's forecast is 0.65
with day-1 accuracy 84 % ⇒ adjusted 0.65 ÷ 0.84 = 0.77 > 0.74, so tonight's
hour is preferred and charges for tomorrow as well. The same applies to a
known 0.74 at noon against a forecast 0.66 tonight (adjusted 0.79), and to a
known 1.30 against a forecast 1.30.

### 5.1 Covering the requirements (greedy, cheapest hour first)

Requirements are covered in time order. For departure *j*:

1. Simulate the SoC at the start of every slot up to *j* from the current
   SoC, the charging planned so far and the consumption at departures.
2. `deficit = requirement(j) − SoC(j)`; done if ≤ 0.
3. Among the slots *i* before *j*, pick the one with the lowest adjusted
   price (the **latest** of equally cheap hours – it postpones the decision
   until more prices are known) that still has `room > 0`:
   `room = min(capacity(i) − planned(i), max_level − highest SoC between i and j)`.
4. Plan `min(deficit, room)` in that slot and repeat. If no slot has room,
   the rest of the deficit is **unmet** (reported, e.g. a long trip right
   after a short stop at home).

Consequences – exactly the behaviour asked for:

* An hour only charges what is needed until a **cheaper** hour can take
  over (the cheaper hour is chosen first for the later requirement).
* If nothing cheaper comes in time, the cheap hour **tops up** – but never
  beyond `max_level` before the departure that needs it.
* A capped charge that cannot cover a long trip later in the week is topped
  up in the cheapest hour before that trip (the cap check only looks at the
  SoC *before* the departure that needs the energy).

### 5.2 Calibration (optional)

Lithium batteries' SoC estimate drifts; periodically charging high
recalibrates it. Decided before §5.1, for the hand-over period (§5.3):

* `days_since` = days since the last calibration.
* `cheapest` = cheapest adjusted price among the home hours before the
  hand-over end.
* **Early grab**: `days_since ≥ early_days` and `cheapest ≤ threshold` and no
  later home hour in the horizon is cheaper.
* **Due**: `days_since ≥ interval_days` and `cheapest ≤ threshold × (1 + esc%)^weeks_over`.
* If either holds, charge to `calibration_soc` in the cheapest hours before
  the hand-over end (above `max_level` – that is the point); §5.1 then plans
  around it.
* `interval_days = 0` disables calibration.

### 5.3 Hand-over to the charger integration

Only part of the plan is acted on now: the slots before the **hand-over
end** = the next departure, the first hour without a known price, or
`handover_max_hours` (24), whichever comes first. Then:

* **Target SoC** = current SoC + the charging planned in those slots
  (+ calibration).
* **Charge until** = the hand-over end. Give both to e.g. EV Smart Charging
  (target + completion time, no start time); it picks the cheapest quarters
  in that period, which are the hours the plan chose.

Everything after the hand-over end is the *expected* plan. It is recomputed
on every update (every 5 minutes, new prices, new trips, SoC changes), so
forecast hours are only ever acted on once their prices are known.

### 5.4 Outputs

* **Target SoC** and **charge until** (§5.3), plus `charge_now` (the
  percentage handed over), `unmet` and a calibration flag.
* **Next charge** = the first planned hour (known or forecast).
* **Hours** = every planned hour: start, % charged, raw and adjusted price,
  known/forecast, calibration.
* **Rows** = one per date: type (weekday/weekend/holiday), km, floor,
  departure hour, requirement, planned % and hours, expected SoC at
  departure, forecast factor, calibration, away overnight and status
  (`charge`, `covered` = an earlier charge covers this date, `not_needed`).

## 6. Parameters (number entities)

| Entity | Default | Unit |
|---|---|---|
| Floor weekday | 50 | % |
| Floor weekend/holiday | 40 | % |
| Max level | 90 | % |
| Buffer after trips | 100 | km |
| Battery capacity | 75 | kWh |
| Charging power (`charge_kw`) | 11 | kW |
| Calibration SoC | 100 | % |
| Calibration interval | 0 (off) | days |
| Calibration early grab | 0 | days |
| Calibration price threshold | 0 | currency/kWh |
| Calibration escalation | 10 | %/week |
| Forecast min. accuracy (`forecast_min_accuracy`) | 90 | % |
| Forecast far factor (`forecast_far_factor`, lead > 4 days) | 1.25 | × |

The default departure hour (08) and the horizon are options (config flow)
rather than entities; `handover_max_hours` (24) is fixed.

## 7. Integration architecture

```
custom_components/ev_charge_planner/
  planner/        pure Python, no Home Assistant imports – unit tested
    models.py     dataclasses (HourPrice, DayInfo, Settings, ChargeHour, PlanRow, Plan)
    core.py       hourly slots, greedy allocation, calibration, hand-over
  adapters/       translate HA states → planner inputs
    prices.py     Energi Data Service, Nordpool, Open Spot Forecast, generic
    trips.py      legacy text helpers (v0.1); calendar + routing (later)
  coordinator.py  DataUpdateCoordinator: collects inputs, runs planner
  config_flow.py  entity selection + options
  sensor.py       target SoC, next charge, charge until, plan (attributes)
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
   the known noon hour vs forecast night cases. The planner is hourly
   (§4–5) – the night/day window model of the first draft was dropped.
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

* Charging power is a constant; a charge curve (slower above ~80 %) would
  make the capacity of high-SoC hours more realistic.
* The greedy allocation is optimal for a single requirement and very close
  for several; a small LP would be exact if it ever matters.
* Calibration "last done" – detect automatically (SoC ≥ calibration SoC) or
  require a helper? v0.1: optional datetime entity; v0.2: auto-detect.
