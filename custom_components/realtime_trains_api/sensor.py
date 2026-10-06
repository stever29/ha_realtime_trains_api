"""Support for UK train data provided by data.rtt.io."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import (
    UnitOfTime,
    CONF_SCAN_INTERVAL,
    WEEKDAYS,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import homeassistant.util.dt as dt_util

from .api import (
    PollSchedule,
    RttAuthError,
    RttClient,
    RttError,
    RttRateLimited,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
)
from .trains import add_journey_data, build_departure, empty_train

_LOGGER = logging.getLogger(__name__)

DOMAIN = "realtime_trains_api"

DEFAULT_TIMEOFFSET = timedelta(minutes=0)
DEFAULT_DELAY = 4
DEFAULT_TIME_WINDOW = 120
DEFAULT_IDLE_INTERVAL = timedelta(minutes=30)
JOURNEY_CACHE_TTL = timedelta(minutes=5)

ATTR_JOURNEY_START = "journey_start"
ATTR_JOURNEY_END = "journey_end"
ATTR_NEXT_TRAINS = "next_trains"
ATTR_LAST_ERROR = "last_error"
ATTR_LAST_POLLED = "last_polled"

CONF_API_TOKEN = "token"
CONF_TOKEN_TYPE = "token_type"
CONF_API_USERNAME = "username"
CONF_API_PASSWORD = "password"
CONF_QUERIES = "queries"
CONF_AUTOADJUSTSCANS = "auto_adjust_scans"

CONF_START = "origin"
CONF_END = "destination"
CONF_JOURNEYDATA = "journey_data_for_next_X_trains"
CONF_SENSORNAME = "sensor_name"
CONF_TIMEOFFSET = "time_offset"
CONF_STOPS_OF_INTEREST = "stops_of_interest"
CONF_CONSIDERED_DELAY = "considered_delay_mins"
CONF_RETURN_EMPTY = "return_empty_train_for_no_departures"
CONF_TIME_WINDOW = "time_window_mins"
CONF_POLL_WINDOWS = "poll_windows"
CONF_ACTIVE_INTERVAL = "active_scan_interval"
CONF_IDLE_INTERVAL = "idle_scan_interval"
CONF_WINDOW_START = "start"
CONF_WINDOW_END = "end"
CONF_WINDOW_DAYS = "days"

_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WINDOW_START): cv.time,
        vol.Required(CONF_WINDOW_END): cv.time,
        vol.Optional(CONF_WINDOW_DAYS, default=list(WEEKDAYS)): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
    }
)

_QUERY_SCHEME = vol.Schema(
    {
        vol.Optional(CONF_SENSORNAME): cv.string,
        vol.Required(CONF_START): cv.string,
        vol.Required(CONF_END): cv.string,
        vol.Optional(CONF_JOURNEYDATA, default=0): cv.positive_int,
        vol.Optional(CONF_TIMEOFFSET, default=DEFAULT_TIMEOFFSET):
            vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_STOPS_OF_INTEREST): [cv.string],
        vol.Optional(CONF_CONSIDERED_DELAY, default=DEFAULT_DELAY): cv.positive_int,
        vol.Optional(CONF_RETURN_EMPTY, default=0): cv.positive_int,
        vol.Optional(CONF_TIME_WINDOW, default=DEFAULT_TIME_WINDOW): vol.All(vol.Coerce(int), vol.Range(min=1, max=1439)),
        vol.Optional(CONF_POLL_WINDOWS, default=[]): [_WINDOW_SCHEMA],
        vol.Optional(CONF_ACTIVE_INTERVAL): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_IDLE_INTERVAL, default=DEFAULT_IDLE_INTERVAL):
            vol.All(cv.time_period, cv.positive_timedelta),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_AUTOADJUSTSCANS, default=False): cv.boolean,
        vol.Required(CONF_API_TOKEN): cv.string,
        vol.Optional(CONF_TOKEN_TYPE, default=TOKEN_TYPE_REFRESH): vol.In([TOKEN_TYPE_REFRESH, TOKEN_TYPE_ACCESS]),
        # No longer used by data.rtt.io; accepted so old configs give a clear warning rather than an error.
        vol.Optional(CONF_API_USERNAME): cv.string,
        vol.Optional(CONF_API_PASSWORD): cv.string,
        vol.Required(CONF_QUERIES): [_QUERY_SCHEME],
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the realtime_train sensors."""
    if CONF_API_USERNAME in config or CONF_API_PASSWORD in config:
        _LOGGER.warning(
            "realtime_trains_api: 'username' and 'password' are no longer used by data.rtt.io and can be removed"
        )

    token = config[CONF_API_TOKEN]
    clients = hass.data.setdefault(DOMAIN, {})
    client = clients.get(token)
    if client is None:
        client = RttClient(async_get_clientsession(hass), token, config[CONF_TOKEN_TYPE])
        clients[token] = client

    tick = config[CONF_SCAN_INTERVAL]
    autoadjustscans = config[CONF_AUTOADJUSTSCANS]

    sensors = {}
    for query in config[CONF_QUERIES]:
        schedule = PollSchedule(
            query[CONF_POLL_WINDOWS],
            query.get(CONF_ACTIVE_INTERVAL, tick),
            query[CONF_IDLE_INTERVAL],
        )
        sensor = RealtimeTrainLiveTrainTimeSensor(
            query.get(CONF_SENSORNAME),
            query[CONF_START],
            query[CONF_END],
            query[CONF_JOURNEYDATA],
            query[CONF_TIMEOFFSET],
            autoadjustscans,
            query.get(CONF_STOPS_OF_INTEREST, []),
            query[CONF_CONSIDERED_DELAY],
            query[CONF_RETURN_EMPTY],
            query[CONF_TIME_WINDOW],
            schedule,
            client,
        )
        sensors[sensor.name] = sensor

    async_add_entities(sensors.values(), True)


class RealtimeTrainLiveTrainTimeSensor(SensorEntity):
    """Next departures between two stations, from data.rtt.io.

    Home Assistant calls async_update every scan_interval. The API is only
    queried when the poll schedule says it is due; in between, the minutes
    countdown is recalculated from the last response.
    """

    _attr_icon = "mdi:train"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, sensor_name, journey_start, journey_end, journey_data_for_next_X_trains,
                 timeoffset, autoadjustscans, stops_of_interest, considered_delay_mins,
                 fill_empty, time_window, schedule: PollSchedule, client: RttClient):
        """Construct a live train time sensor."""
        default_sensor_name = (
            f"Next train from {journey_start} to {journey_end} ({timeoffset})" if (timeoffset.total_seconds() > 0)
            else f"Next train from {journey_start} to {journey_end}")

        self._journey_start = journey_start.upper()
        self._journey_end = journey_end.upper()
        self._journey_data_for_next_X_trains = journey_data_for_next_X_trains
        self._timeoffset = timeoffset
        self._autoadjustscans = autoadjustscans
        self._stops_of_interest = [stop.upper() for stop in stops_of_interest]
        self._considered_delay_mins = considered_delay_mins
        self._fill_empty = fill_empty
        self._time_window = time_window
        self._schedule = schedule
        self._client = client

        self._name = default_sensor_name if sensor_name is None else sensor_name
        self._state = None
        self._next_trains: list[dict] = []
        self._services: list[dict] = []
        self._journeys: dict[str, dict] = {}
        self._last_poll: datetime | None = None
        self._last_error: str | None = None
        self._had_departures = True

    async def async_update(self):
        """Refresh from the API when due, then recalculate the attributes."""
        now = dt_util.utcnow()
        if self._is_due(now):
            await self._poll(now)
        self._rebuild(dt_util.utcnow())

    def _is_due(self, now: datetime) -> bool:
        if self._last_poll is None:
            return True
        if self._autoadjustscans and not self._had_departures:
            return now - self._last_poll >= self._schedule.idle_interval
        return self._schedule.is_due(now, self._last_poll)

    async def _poll(self, now: datetime) -> None:
        self._last_poll = now
        ttl = self._schedule.interval(now) - timedelta(seconds=10)
        try:
            self._services = await self._client.departures(
                self._journey_start, self._journey_end, self._time_window, ttl
            )
            self._last_error = None
        except RttAuthError as err:
            self._last_error = "Credentials invalid"
            _LOGGER.error("%s: %s", self._name, err)
            return
        except RttRateLimited as err:
            self._last_error = "Rate limited"
            _LOGGER.warning("%s: %s", self._name, err)
            return
        except RttError as err:
            self._last_error = str(err)
            _LOGGER.warning("%s: %s", self._name, err)
            return

        if self._journey_data_for_next_X_trains:
            await self._refresh_journeys(now)

    async def _refresh_journeys(self, now: datetime) -> None:
        wanted = []
        for service in self._services:
            train = build_departure(service, now, self._timeoffset, self._considered_delay_mins)
            if train and train.get("unique_identity"):
                wanted.append(train["unique_identity"])
            if len(wanted) >= self._journey_data_for_next_X_trains:
                break

        journeys = {}
        for index, unique_identity in enumerate(wanted):
            try:
                service = await self._client.service(unique_identity, JOURNEY_CACHE_TTL)
            except RttRateLimited as err:
                # Stop asking; keep whatever journey data we already had for the rest.
                _LOGGER.warning("%s: could not get journey data: %s", self._name, err)
                for remaining in wanted[index:]:
                    if remaining in self._journeys:
                        journeys[remaining] = self._journeys[remaining]
                break
            except RttError as err:
                # The departures are still useful without arrival times.
                _LOGGER.warning("%s: could not get journey data: %s", self._name, err)
                service = self._journeys.get(unique_identity)
            if service is not None:
                journeys[unique_identity] = service
        self._journeys = journeys

    def _rebuild(self, now: datetime) -> None:
        trains = []
        for service in self._services:
            train = build_departure(service, now, self._timeoffset, self._considered_delay_mins)
            if train is None:
                continue
            journey = self._journeys.get(train.get("unique_identity"))
            if journey is not None and not add_journey_data(
                train, journey, self._journey_start, self._journey_end, self._stops_of_interest
            ):
                _LOGGER.debug("Could not find %s in stops for service %s", self._journey_end, train["service_uid"])
            trains.append(train)

        self._had_departures = bool(trains)
        next_departure = min((t["_estimated_dt"] for t in trains), default=None)
        for train in trains:
            del train["_estimated_dt"]

        for _ in range(len(trains), self._fill_empty):
            trains.append(empty_train(self._journey_start, self._journey_end))

        self._next_trains = trains
        self._state = None if next_departure is None else int((next_departure - now).total_seconds() // 60)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return other details about the sensor state."""
        attrs = {
            ATTR_JOURNEY_START: self._journey_start,
            ATTR_JOURNEY_END: self._journey_end,
        }
        if self._next_trains:
            attrs[ATTR_NEXT_TRAINS] = self._next_trains
        if self._last_error:
            attrs[ATTR_LAST_ERROR] = self._last_error
        if self._last_poll:
            attrs[ATTR_LAST_POLLED] = self._last_poll.isoformat()
        return attrs
