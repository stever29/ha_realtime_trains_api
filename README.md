# realtime_trains_api
Realtime Trains (data.rtt.io) Home Assistant integration

It provides detailed live train departures and journey stats:

```yaml
station_code: WAT
calling_at: WAL
next_trains:
  - origin_name: London Waterloo
    destination_name: Basingstoke
    service_uid: Q46478
    unique_identity: gb-nr:Q46478:2022-01-21
    scheduled: 21-01-2022 20:12
    estimated: 21-01-2022 20:12
    minutes: 3
    platform: '10'
    operator_name: South Western Railway
    stops_of_interest: []
    scheduled_arrival: 21-01-2022 20:37
    estimate_arrival: 21-01-2022 20:36
    journey_time_mins: 24
    stops: 2
  - origin_name: London Waterloo
    destination_name: Woking
    service_uid: Q46174
    scheduled: 21-01-2022 20:20
    estimated: 21-01-2022 20:20
    minutes: 11
    platform: '4'
    operator_name: South Western Railway
    stops_of_interest:
      - stop: VXH
        name: Vauxhall
        scheduled_stop: 21-01-2022 20:23
        estimate_stop: 21-01-2022 20:23
        journey_time_mins: 3
        stops: 0
    scheduled_arrival: 21-01-2022 20:54
    estimate_arrival: 21-01-2022 20:53
    journey_time_mins: 33
    stops: 7
unit_of_measurement: min
icon: mdi:train
friendly_name: Next Waterloo train data
```

This Home Assistant integration is only made possible by the brilliant Realtime Trains API (https://data.rtt.io, sign up at https://api-portal.rtt.io; also see https://www.realtimetrains.co.uk) which is maintained by Tom Cairns under swlines Ltd (https://twitter.com/swlines).

Alternatively, you can use the built-in `uk_transport` integration (see https://www.home-assistant.io/integrations/uk_transport/).  NOTE: Unlike this `realtime_trains_api` integration, `uk_tr# Guide

## Version 2.0: new Realtime Trains API

The original API at api.rtt.io was switched off at the end of September 2026. Version 2.0 uses the new API at https://data.rtt.io:

- Authentication is a bearer token from https://api-portal.rtt.io instead of a username and password. The portal gives you either a **refresh token** (the default here; the integration exchanges it for short-lived access tokens itself) or a long-life **access token** (set `token_type: access`).
- The new API is rate limited per account (for example 10/minute, 100/hour, 1,000/day, 10,000/week). Check yours in the `X-RateLimit-*` response headers. Use `poll_windows` so the API is only polled often when you need it.
- Sensor names, entity IDs and the `next_trains` attributes are unchanged, so existing templates and dashboards keep working. New attributes: `unique_identity` on each train, plus `last_polled` and `last_error` on the sensor.

## Installation & Usage

1. Sign up at https://api-portal.rtt.io and copy your token into `secrets.yaml` as `rtt_token`.
2. Copy `custom_components/realtime_trains_api` into your Home Assistant `config/custom_components/` folder (or add this repository to HACS as a custom repository).
3. Add to `configuration.yaml`:
```yaml
sensor:
  - platform: realtime_trains_api
    token: !secret rtt_token
    # token_type: access          # only if the portal gave you a long-life access token
    scan_interval: 60             # how often HA checks; the API is only called when a query is due (see below)
    auto_adjust_scans: true       # if a query finds no trains, wait idle_scan_interval before asking again
    queries:
      - origin: WIC
        destination: LST
        sensor_name: Next Train To Liv Street
        journey_data_for_next_X_trains: 2   # arrival times for the next 2 trains (one extra API call each, cached for 5 minutes)
        return_empty_train_for_no_departures: 3
        considered_delay_mins: 4            # later than this counts as LATE
        time_window_mins: 120               # how far ahead to list trains (falls back to 60 if your token doesn't allow it)
        active_scan_interval:
          minutes: 2                        # poll every 2 minutes inside poll_windows...
        idle_scan_interval:
          minutes: 30                       # ...and every 30 minutes outside them (default)
        poll_windows:
          - start: "06:30"
            end: "09:30"
            days: [mon, tue, wed, thu, fri] # default: every day
        stops_of_interest:
          - SNF
      - origin: LST
        destination: WIC
        sensor_name: Next Train Home
        time_offset:
          minutes: 10                       # only show trains leaving at least 10 minutes from now
        active_scan_interval:
          minutes: 2
        poll_windows:
          - start: "16:30"
            end: "22:30"
            days: [mon, tue, wed, thu, fri]
```
4. Restart Home Assistant.
5. Each query creates a sensor named like `sensor.next_train_from_wic_to_lst` (or from `sensor_name`).

Queries with the same origin, destination and `time_window_mins` share one API call, so several sensors on one route (for example with different `time_offset`s) cost no more than one. Without `poll_windows`, a query polls every `active_scan_interval` (default: `scan_interval`) all day.

If the API returns `429 Too Many Requests`, all sensors stop calling it until the `Retry-After` time has passed, and keep showing their last data in the meantime.
