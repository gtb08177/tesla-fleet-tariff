"""Tesla Fleet Tariff: drive a Powerwall's utility rate plan from Home Assistant.

Reuses the authenticated API client of the core ``tesla_fleet`` integration,
so there is no separate login. Add ``tesla_fleet_tariff:`` to
configuration.yaml to enable it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CoreState,
    Event as HaEvent,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER
from .manager import Event, TariffManager, drain_windows
from .tariff import DailyRate, TariffError, build_daily_tariff, parse_time

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            lambda value: value or {},  # bare `tesla_fleet_tariff:` is fine
            vol.Schema(
                {
                    # single: one all-week schedule in the Tesla app (default).
                    # weekday_weekend: session changes go into the Mon-Fri or
                    # Sat-Sun schedule only.
                    vol.Optional("schedule", default="single"): vol.In(
                        ["single", "weekday_weekend"]
                    ),
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PRICE = vol.All(vol.Coerce(float), vol.Range(min=0))
def _one_device(value: Any) -> str:
    """Accept device_id as a string (action data) or a one-item list (target)."""
    ids = cv.ensure_list(value)
    if len(ids) != 1 or not isinstance(ids[0], str):
        raise vol.Invalid("Pick exactly one Tesla Fleet energy site device")
    return ids[0]


DEVICE = {vol.Required("device_id"): _one_device}
DRY_RUN = {vol.Optional("dry_run", default=False): cv.boolean}
# An event either reuses a label from the normal plan (its prices and its colour
# in the Tesla app) or sets its own prices.
PRICES = {
    vol.Exclusive("label", "price"): cv.string,
    vol.Exclusive("buy_price", "price"): PRICE,
    vol.Optional("sell_price"): PRICE,
    vol.Optional("activate", default="event_day"): vol.In(["event_day", "now"]),
    # e.g. {"Peak": "Mid-Peak"}: on the event's day, normal Peak becomes Mid-Peak
    vol.Optional("relabel", default={}): {cv.string: cv.string},
    # Drain ahead of sessions: e.g. pre_label "Peak" exports for the same
    # length of time immediately before each block of sessions.
    vol.Optional("pre_label"): cv.string,
    vol.Optional("pre_minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=720)),
}
NEEDS_PRICE = cv.has_at_least_one_key("label", "buy_price")

RATE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,
        vol.Required("buy"): PRICE,
        vol.Optional("sell", default=0.0): PRICE,
        vol.Optional("label"): cv.string,
    }
)

SET_BASE_SCHEMA = vol.All(
    vol.Schema(
        {
            **DEVICE,
            vol.Exclusive("rates", "source"): vol.All(cv.ensure_list, [RATE_SCHEMA]),
            vol.Exclusive("tariff", "source"): dict,
            vol.Optional("currency", default="GBP"): vol.In(["GBP", "EUR", "USD"]),
            vol.Optional("name", default="Home Assistant tariff"): cv.string,
            # "Utility provider" in the Tesla app, e.g. Octopus Energy.
            vol.Optional("utility"): cv.string,
            vol.Optional("daily_charge", default=0.0): PRICE,
            vol.Optional("push", default=True): cv.boolean,
            **DRY_RUN,
        }
    ),
    cv.has_at_least_one_key("rates", "tariff"),
)

ADD_EVENT_SCHEMA = vol.All(vol.Schema(
    {
        **DEVICE,
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        **PRICES,
        vol.Optional("event_id"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("source", default="manual"): cv.string,
        **DRY_RUN,
    }
), NEEDS_PRICE)

LIST_ITEM = vol.Schema(
    {
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("id"): vol.Any(cv.string, int, None),
    },
    extra=vol.ALLOW_EXTRA,
)

SYNC_SCHEMA = vol.All(
    vol.Schema(
        {
            **DEVICE,
            vol.Exclusive("entity_id", "src"): cv.entity_id,
            vol.Exclusive("events", "src"): vol.All(cv.ensure_list, [LIST_ITEM]),
            vol.Optional("attribute"): cv.string,
            vol.Optional("source"): cv.string,
            vol.Optional("name"): cv.string,
            **PRICES,
            **DRY_RUN,
        }
    ),
    cv.has_at_least_one_key("entity_id", "events"),
    NEEDS_PRICE,
)

CLEAR_SCHEMA = vol.Schema(
    {
        **DEVICE,
        vol.Optional("source"): cv.string,
        vol.Optional("force", default=True): cv.boolean,
    }
)


def _aware(value: datetime) -> datetime:
    """Naive datetimes (strings without an offset) are HA local time."""
    return value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        raise ServiceValidationError(f"Could not parse datetime '{value}'")
    return _aware(parsed)


def _event_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    common = {"activate": data["activate"], "relabel": dict(data["relabel"])}
    if "label" in data:
        return {"label": data["label"], **common}
    buy = data["buy_price"]
    # Default export = import: makes exporting as attractive as avoiding
    # import (saving sessions), and 0/0 for free electricity.
    return {"buy": buy, "sell": data.get("sell_price", buy), **common}


def _drain_events(
    manager: TariffManager,
    data: dict[str, Any],
    source: str,
    sessions: list[tuple[datetime, datetime]],
    name: str | None,
    single_id: str | None = None,
) -> list[Event]:
    """Pre-session drain windows as events (label only, no relabel)."""
    if not data.get("pre_label"):
        return []
    out = []
    for start, end in drain_windows(sessions, data.get("pre_minutes")):
        if end <= dt_util.utcnow():
            continue
        out.append(
            manager.make_event(
                event_id=f"{single_id}:pre" if single_id else f"{source}:pre:{dt_util.as_utc(end).isoformat()}",
                source=source,
                start=start,
                end=end,
                label=data["pre_label"],
                name=f"{name or 'Session'} (drain)",
                activate=data["activate"],
            )
        )
    return out


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML."""
    if DOMAIN not in config:
        return True

    manager = TariffManager(hass, config[DOMAIN]["schedule"])
    await manager.async_load()
    hass.data[DOMAIN] = manager

    async def set_base(call: ServiceCall) -> ServiceResponse:
        data = call.data
        if "tariff" in data:
            tariff = dict(data["tariff"])
            tariff = tariff.get("tariff_content_v2", tariff)
            if data.get("utility"):
                for part in (tariff, tariff.get("sell_tariff") or {}):
                    if part:
                        part["utility"] = data["utility"]
        else:
            try:
                tariff = build_daily_tariff(
                    [
                        DailyRate(parse_time(r["start"]), r["buy"], r["sell"], r.get("label"))
                        for r in data["rates"]
                    ],
                    currency=data["currency"],
                    name=data["name"],
                    utility=data.get("utility") or "Home Assistant",
                    daily_charge=data["daily_charge"],
                )
            except TariffError as err:
                raise ServiceValidationError(str(err)) from err
        if data["dry_run"]:
            return {"tariff": tariff}
        result = await manager.async_set_base(data["device_id"], tariff, data["push"])
        return {**result, "tariff": tariff}

    async def capture_base(call: ServiceCall) -> ServiceResponse:
        tariff = await manager.async_fetch_site_tariff(call.data["device_id"])
        if not tariff:
            raise ServiceValidationError(
                "site_info did not include tariff_content_v2 for this site. "
                "Use set_base_tariff with explicit rates instead."
            )
        if not call.data["dry_run"]:
            await manager.async_set_base(call.data["device_id"], tariff, push=False)
        return {"tariff": tariff}

    async def add_event(call: ServiceCall) -> ServiceResponse:
        data = call.data
        event = manager.make_event(
            event_id=data.get("event_id"),
            source=data["source"],
            start=_aware(data["start"]),
            end=_aware(data["end"]),
            name=data.get("name"),
            **_event_kwargs(data),
        )
        drain = _drain_events(
            manager, data, data["source"], [(event.start_dt, event.end_dt)],
            data.get("name"), single_id=event.id,
        )
        if data["dry_run"]:
            return manager.preview(data["device_id"], extra=[*drain, event])
        return {
            "event_id": event.id,
            **await manager.async_add_event(data["device_id"], event, extra=drain),
        }

    async def remove_event(call: ServiceCall) -> ServiceResponse:
        return await manager.async_remove_event(call.data["device_id"], call.data["event_id"])

    async def sync_events(call: ServiceCall) -> ServiceResponse:
        data = call.data
        kwargs = _event_kwargs(data)
        if "entity_id" in data:
            entity_id = data["entity_id"]
            state = hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                # Never wipe known events because the source briefly vanished.
                LOGGER.warning("sync_events: %s unavailable, keeping events", entity_id)
                return {"pushed": False, "skipped": f"{entity_id} unavailable"}
            attribute = data.get("attribute") or (
                "joined_events" if "joined_events" in state.attributes else "events"
            )
            items = state.attributes.get(attribute)
            if not isinstance(items, list):
                raise ServiceValidationError(
                    f"{entity_id} has no list attribute '{attribute}'"
                )
            source = data.get("source", entity_id)
        else:
            items = data["events"]
            source = data.get("source", "list")
        events: list[Event] = []
        sessions: list[tuple[datetime, datetime]] = []
        for item in items:
            if not isinstance(item, dict) or "start" not in item or "end" not in item:
                continue
            start, end = _as_dt(item["start"]), _as_dt(item["end"])
            sessions.append((start, end))
            if end <= dt_util.utcnow():
                continue  # Octopus keeps past sessions in the list
            item_id = item.get("id")
            events.append(
                manager.make_event(
                    event_id=f"{source}:{item_id}" if item_id is not None else None,
                    source=source,
                    start=start,
                    end=end,
                    name=data.get("name"),
                    **kwargs,
                )
            )
        events += _drain_events(manager, data, source, sessions, data.get("name"))
        if data["dry_run"]:
            return {**manager.preview(data["device_id"], extra=events),
                    "found": [e.id for e in events]}
        return await manager.async_sync(data["device_id"], source, events)

    async def clear_events(call: ServiceCall) -> ServiceResponse:
        return await manager.async_clear(
            call.data["device_id"], call.data.get("source"), call.data["force"]
        )

    async def get_status(call: ServiceCall) -> ServiceResponse:
        return manager.status(call.data["device_id"])

    opt, only = SupportsResponse.OPTIONAL, SupportsResponse.ONLY
    for name, handler, schema, resp in (
        ("set_base_tariff", set_base, SET_BASE_SCHEMA, opt),
        ("capture_base_tariff", capture_base, vol.Schema({**DEVICE, **DRY_RUN}), opt),
        ("add_event", add_event, ADD_EVENT_SCHEMA, opt),
        ("remove_event", remove_event,
         vol.Schema({**DEVICE, vol.Required("event_id"): cv.string}), opt),
        ("sync_events", sync_events, SYNC_SCHEMA, opt),
        ("clear_events", clear_events, CLEAR_SCHEMA, opt),
        ("get_status", get_status, vol.Schema(DEVICE), only),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema, supports_response=resp)

    # Catch up after a restart (e.g. an event ended while HA was down). Wait
    # for startup so Tesla Fleet has loaded its energy sites.
    async def _on_started(_event: HaEvent | None = None) -> None:
        await manager.async_resume()

    if hass.state is CoreState.running:
        await _on_started()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _e: manager.async_shutdown())
    return True
