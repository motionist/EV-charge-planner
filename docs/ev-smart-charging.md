# Hand-over to EV Smart Charging

EV Charge Planner **plans** – it does not switch the charger. It publishes
the part of the plan that is certain (known prices before the next
departure, max 24 h) as two sensors:

| Sensor (English / Danish Home Assistant) | Meaning |
|---|---|
| `sensor.<name>_target_soc` / `sensor.<name>_mal_soc` | Charge to this level … |
| `sensor.<name>_charge_until` / `sensor.<name>_lad_faerdig_senest` | … before this time (a full hour). |

Entity IDs follow the language Home Assistant had when the integration was
added – check them on the integration's device page.

[EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging)
(EVSC) then picks the cheapest quarter-hours within that window and
switches the charger.

## 0. Shadow mode first

Run the planner for a week or two **without** handing over: keep your
current charging control and compare `sensor.<name>_plan` (attribute
`rows`: one row per date with the planned hours and the SoC at departure)
with what you would have done. Report surprises as an issue.

## 1. Target SoC

In EVSC's options, set **EV Target SOC entity** to the planner's target
SoC sensor. If your EVSC version only accepts an `input_number`, create one
(0–100 %) and let the automation below copy the sensor into it.

Make sure nothing else writes the target (e.g. an older automation of your
own) – two writers will fight.

## 2. Completion time

EVSC's completion time is a select (`select.<evsc>_charge_completion_time`,
options `None`, `00:00` … `23:00`). Keep it in step with the planner and
clear EVSC's start time so EVSC uses the whole window:

```yaml
alias: EV Charge Planner → EV Smart Charging
description: Hands the certain part of the charge plan over to EVSC.
mode: queued
max: 2
triggers:
  - trigger: state
    entity_id:
      - sensor.ev_charge_planner_target_soc
      - sensor.ev_charge_planner_charge_until
  - trigger: homeassistant
    event: start
conditions:
  - condition: not
    conditions:
      - condition: state
        entity_id:
          - sensor.ev_charge_planner_target_soc
          - sensor.ev_charge_planner_charge_until
        state:
          - unavailable
          - unknown
actions:
  # Only when EVSC's target SoC entity is an input_number (see step 1):
  - action: input_number.set_value
    target:
      entity_id: input_number.ev_target_soc
    data:
      value: "{{ states('sensor.ev_charge_planner_target_soc') | int }}"
  - action: select.select_option
    target:
      entity_id: select.ev_smart_charging_charge_completion_time
    data:
      option: "{{ (states('sensor.ev_charge_planner_charge_until') | as_datetime | as_local).strftime('%H:00') }}"
  - action: select.select_option
    target:
      entity_id: select.ev_smart_charging_charge_start_time
    data:
      option: "None"
```

Replace the entity IDs with yours. Keep EVSC's *Smart charging activated*
switch on.

## Other chargers

Without EVSC, use the same two sensors in your own automation: charge
while the battery is below the target SoC and the current hour is one of
the planned hours (`sensor.<name>_plan`, attribute `hours`, those with
`known: true`), and stop at *charge until*.
