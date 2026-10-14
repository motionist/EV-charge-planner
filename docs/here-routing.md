# Km per day from Google Calendar with HERE routing

This is how the original installation turns calendar appointments into the
`km per day` input of the planner, as a Home Assistant automation that writes
a text helper (`YYYY-MM-DD=km;…`). Since v0.2 the integration can read your
calendars itself and route with Waze Travel Time (see [DESIGN.md](DESIGN.md)
§3.1); this automation is the alternative with HERE routing.

## How it works

1. **Calendar.** The Google Calendar integration exposes your calendar as a
   `calendar.*` entity. Every *timed* event with a **location** is treated as
   a destination for that day (all-day events and events without a location
   are ignored for distance).
2. **Geocoding.** Each location string is turned into coordinates with the
   HERE Geocoding & Search API.
3. **Home detour rule.** Events of the same day are ordered by start time.
   Between two events the car drives **home** if the gap allows at least one
   hour at home – i.e. `gap ≥ drive_home + 60 min + drive_back` (drive times
   from HERE). Otherwise it drives directly from one event to the next.
4. **Routing.** One HERE Routing v8 request per day: `home → (via …) → home`,
   with a `via` for every event and for every home detour. The sum of the
   route sections' `length` (metres) is the day's distance.
5. **Output.** `input_text.<km per day>` = `2026-10-08=49.6;2026-10-15=179.3;`,
   recalculated daily (22:10), on start-up and on a button press.

Away hours (`YYYY-MM-DD=8-17`) are produced by a separate automation from the
same calendar: event start/end ± travel time (Waze Travel Time in the original;
HERE `duration` works equally well), merged when less than 30 minutes apart;
all-day events with an away location count as 06–20.

## Setup

### 1. HERE API key

Create a free account at <https://platform.here.com>, create an app and an
**API key** with access to *Geocoding & Search* and *Routing*. HERE's free
tier is ample for a few requests per day – check the current limits on their
site.

Keep the key out of `configuration.yaml`. A `rest_command` URL is a
template, and a `!secret` is substituted before the template is rendered, so
the whole templated URL (including the key) can live in `secrets.yaml`:

```yaml
# secrets.yaml
here_geocode_url: "https://geocode.search.hereapi.com/v1/geocode?q={{ q | urlencode }}&in=countryCode:DNK&limit=1&apiKey=YOUR_KEY"
here_route_url: "https://router.hereapi.com/v8/routes?transportMode=car&origin={{ origin }}&destination={{ destination }}{{ vias | default('') }}&return=summary&apiKey=YOUR_KEY"
```

(`in=countryCode:DNK` restricts geocoding to Denmark – change or remove it.)

### 2. REST commands

```yaml
# configuration.yaml
rest_command:
  here_geocode:
    url: !secret here_geocode_url
    method: GET
  here_route:
    url: !secret here_route_url
    method: GET
```

Call them with `rest_command.here_geocode` (`q`) and `rest_command.here_route`
(`origin`, `destination`, optional `vias` = `"&via=lat,lng&via=lat,lng"`), using
`response_variable` to read the JSON (`content.items[0].position`,
`content.routes[0].sections[*].summary.length` / `.duration`).

### 3. Automation (outline)

```yaml
alias: Calendar → km per day (HERE routing)
triggers:
  - trigger: homeassistant
    event: start
  - trigger: time
    at: "22:10:00"
variables:
  home: "55.0000,12.0000"   # your home coordinates "lat,lng"
actions:
  - action: calendar.get_events
    target: { entity_id: calendar.your_calendar }
    data:
      start_date_time: "{{ today_at('00:00').isoformat() }}"
      end_date_time: "{{ (today_at('00:00') + timedelta(days=8)).isoformat() }}"
    response_variable: cal
  # For each day with located, timed events:
  #   1. rest_command.here_geocode (q = location)          → lat/lng
  #   2. rest_command.here_route home → event              → duration (for the detour rule)
  #   3. build "&via=lat,lng" for each event, inserting "&via=<home>" where the
  #      gap allows ≥ 60 min at home
  #   4. rest_command.here_route origin=home destination=home vias=…
  #      → km = sum(routes[0].sections[*].summary.length) / 1000
  #   5. append "YYYY-MM-DD=km;" to input_text.ladeplan_km_pr_dag
```

The full automation from the original installation is the reference for the
v0.2 calendar adapter.

## Planned for v0.2 (in the integration)

* Config flow: calendar entity, home location (defaults to the HA home zone),
  routing provider (**HERE** first; OSRM/Waze later) and API key stored in the
  config entry (never in YAML).
* Geocode cache per location string, so a recurring address costs one request.
* Same home-detour rule and travel-time-based away hours, with the "≥ 60 min at
  home" threshold as an option.
* Keywords/locations that count as *home* (e.g. your own address) are ignored.
