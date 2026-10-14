# Example dashboard card

A Markdown card rendering the plan from `sensor.ev_charge_planner_plan`
(adjust the entity ids to your config entry name). Each table row must be
rendered on a single line, hence the one-line `{% set line = … %}`.
⚡ = known prices (handed over to the charger), 🔮 = forecast prices.

Per day it shows the planned energy and its cost (kWh into the battery,
charging losses not included). Below the table the week's total is split into
hours with a known price and hours with a forecast price – the forecast part
is an estimate until the prices are published. While the car is away, the
header also shows the SoC expected when it is home (DESIGN §5.4).

```yaml
type: markdown
content: >-
  {% set rows = state_attr('sensor.ev_charge_planner_plan', 'rows') or [] %}
  {% set t = state_attr('sensor.ev_charge_planner_plan', 'totals') or {} %}
  {% set away_km = state_attr('sensor.ev_charge_planner_target_soc', 'remaining_km') %}
  {% set home = state_attr('sensor.ev_charge_planner_target_soc', 'soc_home') %}
  {% set wd = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}
  ### 🔋 Charging plan

  **Target:** {{ states('sensor.ev_charge_planner_target_soc') }} % before
  {{ as_timestamp(states('sensor.ev_charge_planner_charge_until')) | timestamp_custom('%a %H:%M') }}{{ (' · ≈ ' ~ (home | round(0) | int) ~ ' % when home') if away_km else '' }}


  | Day | km | Charge | Hours | SoC at departure |

  |---|---|---|---|---|

  {% for r in rows %}{% set d = as_datetime(r.date) %}{% set hrs = (('⚡ ' if r.charge_known else '🔮 ') ~ (r.charge_hours | map('string') | join(', '))) if r.charge_hours else r.status %}{% set chg = ((r.charge_kwh | round(0) | int) ~ ' kWh' ~ ((' · ' ~ (r.cost | round(0) | int) ~ ' kr') if r.cost is not none else '')) if r.charge_kwh else '–' %}{% set line = '| ' ~ wd[d.weekday()] ~ ' ' ~ d.strftime('%d/%m') ~ (' 🔧' if r.calibration else '') ~ ' | ' ~ ((r.km | round(0) | int) if r.km else '') ~ ' | ' ~ chg ~ ' | ' ~ hrs ~ ' | ' ~ (((r.soc_departure | round(0) | int) ~ ' %') if r.soc_departure is not none else '–') ~ ' |' %}{{ line }}

  {% endfor %}

  **Week: {{ t.kwh | default(0) | round(0) | int }} kWh ≈ {{ t.cost | default(0) | round(0) | int }} kr{{ (' · avg ' ~ ('%.2f' | format(t.cost / t.kwh)) ~ ' kr/kWh') if t.kwh else '' }}**


  | Price basis | kWh | kr | avg kr/kWh |

  |---|---|---|---|

  | ⚡ Known | {{ t.kwh_known | default(0) | round(0) | int }} | {{ t.cost_known | default(0) | round(0) | int }} | {{ ('%.2f' | format(t.cost_known / t.kwh_known)) if t.kwh_known else '–' }} |

  | 🔮 Forecast | {{ t.kwh_forecast | default(0) | round(0) | int }} | {{ t.cost_forecast | default(0) | round(0) | int }} | {{ ('%.2f' | format(t.cost_forecast / t.kwh_forecast)) if t.kwh_forecast else '–' }} |
```
