# Example dashboard card

A Markdown card rendering the plan from `sensor.ev_charge_planner_plan`
(adjust the entity ids to your config entry name). Each table row must be
rendered on a single line, hence the one-line `{% set line = … %}`.
⚡ = known prices (handed over to the charger), 🔮 = forecast prices.

```yaml
type: markdown
content: >-
  {% set rows = state_attr('sensor.ev_charge_planner_plan', 'rows') or [] %}
  {% set wd = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}
  ### 🔋 Charging plan

  **Target:** {{ states('sensor.ev_charge_planner_target_soc') }} % before
  {{ as_timestamp(states('sensor.ev_charge_planner_charge_until')) | timestamp_custom('%a %H:%M') }}


  | Day | km | Charge | Hours | SoC at departure |

  |---|---|---|---|---|

  {% for r in rows %}{% set d = as_datetime(r.date) %}{% set hrs = (('⚡ ' if r.charge_known else '🔮 ') ~ (r.charge_hours | map('string') | join(', '))) if r.charge_hours else r.status %}{% set line = '| ' ~ wd[d.weekday()] ~ ' ' ~ d.strftime('%d/%m') ~ (' 🔧' if r.calibration else '') ~ ' | ' ~ ((r.km | round(0) | int) if r.km else '') ~ ' | ' ~ (('+' ~ (r.charge_pct | round(0) | int) ~ ' %') if r.charge_pct else '–') ~ ' | ' ~ hrs ~ ' | ' ~ (((r.soc_departure | round(0) | int) ~ ' %') if r.soc_departure is not none else '–') ~ ' |' %}{{ line }}

  {% endfor %}
```
