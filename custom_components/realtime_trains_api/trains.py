"""Turn data.rtt.io responses into the sensor's train attributes.

The attribute names and formats match the old api.rtt.io version of this
component, so existing templates and dashboards keep working.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .api import LOCAL_TZ, best_time, has_code, is_public_call, parse_datetime, platform_of

STRFFORMAT = "%d-%m-%Y %H:%M"
STRFFORMAT_HHMM = "%H:%M"


def _fmt(value: datetime, fmt: str = STRFFORMAT) -> str:
    return value.astimezone(LOCAL_TZ).strftime(fmt)


def _mins(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)


def build_departure(service: dict, now: datetime, time_offset: timedelta, considered_delay_mins: int) -> dict | None:
    """Build one train dict from a location line-up entry, or None to skip it."""
    metadata = service.get("scheduleMetadata") or {}
    if metadata.get("inPassengerService") is False:
        return None

    temporal = service.get("temporalData") or {}
    departure = temporal.get("departure") or {}
    scheduled = parse_datetime(departure.get("scheduleAdvertised"))
    if scheduled is None:
        # Not advertised as departing here (e.g. passes, or sets down only).
        return None
    estimated = best_time(departure) or scheduled

    if (estimated - now).total_seconds() < time_offset.total_seconds():
        return None

    cancelled = bool(departure.get("isCancelled")) or temporal.get("displayAs") == "CANCELLED"
    delay = _mins(estimated, scheduled)
    if cancelled:
        status = "CANCELLED"
    elif delay > considered_delay_mins:
        status = "LATE"
    else:
        status = "ON TIME"

    origin = (service.get("origin") or [{}])[0].get("location") or {}
    destination = (service.get("destination") or [{}])[0].get("location") or {}

    return {
        "origin_name": origin.get("description"),
        "destination_name": destination.get("description"),
        "service_uid": metadata.get("identity"),
        "unique_identity": metadata.get("uniqueIdentity"),
        "scheduled": _fmt(scheduled),
        "scheduled_hhmm": _fmt(scheduled, STRFFORMAT_HHMM),
        "estimated": _fmt(estimated),
        "estimated_hhmm": _fmt(estimated, STRFFORMAT_HHMM),
        "estimated_arrival": None,  # filled in by add_journey_data
        "estimated_arrival_hhmm": "",
        "minutes": _mins(estimated, now),
        "delay": delay,
        "platform": platform_of(service.get("locationMetadata")),
        "operator_name": (metadata.get("operator") or {}).get("name"),
        "status": status,
        # Internal: used for the next-departure state and journey lookups.
        "_estimated_dt": estimated,
    }


def add_journey_data(
    train: dict, service: dict, journey_start: str, journey_end: str, stops_of_interest: list[str]
) -> bool:
    """Add arrival time, journey time and stops of interest from a full service. Returns True if found."""
    locations = service.get("locations") or []
    estimated_departure: datetime = train["_estimated_dt"]

    start_index = next(
        (i for i, loc in enumerate(locations) if has_code(loc.get("location"), journey_start)), None
    )
    if start_index is None:
        return False

    stops = 0
    found_stops = []
    for loc in locations[start_index + 1 :]:
        location = loc.get("location") or {}
        temporal = loc.get("temporalData") or {}
        arrival = temporal.get("arrival") or {}
        if has_code(location, journey_end):
            scheduled_arrival = parse_datetime(arrival.get("scheduleAdvertised"))
            estimated_arrival = best_time(arrival)
            if scheduled_arrival is None or estimated_arrival is None:
                return False
            train.update(
                {
                    "stops_of_interest": found_stops,
                    "scheduled_arrival": _fmt(scheduled_arrival),
                    "scheduled_arrival_hhmm": _fmt(scheduled_arrival, STRFFORMAT_HHMM),
                    "estimated_arrival": _fmt(estimated_arrival),
                    "estimated_arrival_hhmm": _fmt(estimated_arrival, STRFFORMAT_HHMM),
                    "journey_time_mins": _mins(estimated_arrival, estimated_departure),
                    "stops": stops,
                }
            )
            return True
        if not is_public_call(temporal):
            continue
        codes = set(location.get("shortCodes") or []) | set(location.get("longCodes") or [])
        if codes & set(stops_of_interest):
            scheduled_stop = parse_datetime(arrival.get("scheduleAdvertised"))
            estimated_stop = best_time(arrival)
            if scheduled_stop is not None and estimated_stop is not None:
                found_stops.append(
                    {
                        "stop": (location.get("shortCodes") or [None])[0],
                        "name": location.get("description"),
                        "scheduled_stop": _fmt(scheduled_stop),
                        "estimated_stop": _fmt(estimated_stop),
                        "journey_time_mins": _mins(estimated_stop, estimated_departure),
                        "stops": stops,
                    }
                )
        stops += 1
    return False


def empty_train(journey_start: str, journey_end: str) -> dict:
    """Placeholder train used to pad next_trains (return_empty_train_for_no_departures)."""
    return {
        "origin_name": journey_start,
        "destination_name": journey_end,
        "service_uid": None,
        "scheduled": None,
        "scheduled_hhmm": "--:--",
        "estimated": None,
        "estimated_hhmm": "--:--",
        "scheduled_arrival_hhmm": "--:--",
        "estimated_arrival_hhmm": "--:--",
        "minutes": "-",
        "platform": "-",
        "delay": 0,
        "status": "",
    }
