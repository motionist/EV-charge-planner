# Example dashboard card

A Markdown card rendering the plan from `sensor.ev_charge_planner_plan`
(adjust the entity ids to your config entry name). Each table row must be
rendered on a single line, hence the one-line `{% set line = … %}`.

```yaml
type: markdown
content: >-
  {% set rows = state_attr('sensor.ev_charge_planner_plan', 'rows') or [] %}
  {% set wd = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}
  ### 🔋 Charging plan

  **Target:** {{ states('sensor.ev_charge_planner_target_soc') }} %


  | Day | km | Target | When (≈SoC) | Night | Day |

  |---|---|---|---|---|---|

  {% for r in rows %}{% set d = as_datetime(r.date) %}{% set tgt = r.night_target or r.day_target %}{% set when = ('🌙 %02d:00' | format(r.night_hour)) if r.night_target else (('☀️ %02d:00' | format(r.day_hour)) if r.day_target else r.status) %}{% set line = '| ' ~ wd[d.weekday()] ~ ' ' ~ d.strftime('%d/%m') ~ (' 🔧' if r.calibration else '') ~ ' | ' ~ ((r.km | round(0) | int) if r.km else '') ~ ' | ' ~ ((tgt ~ ' %') if tgt else '–') ~ ' | ' ~ when ~ ' (≈' ~ (r.soc_departure | round(0) | int) ~ ' %) | ' ~ ((r.night_price | round(2)) if r.night_price is not none else '–') ~ ' | ' ~ ((r.day_price | round(2)) if r.day_price is not none else '–') ~ ' |' %}{{ line }}

  {% endfor %}
```
