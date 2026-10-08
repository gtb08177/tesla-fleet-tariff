"""Glue between Home Assistant, the Tesla Fleet integration and tariff.py.

Model: per energy site we keep a *base* (normal) tariff and a set of *events*.
Whenever anything changes (an event is added/removed/synced, one ends, the
base changes, HA starts) we recompute ``base + current events`` and push it
to Tesla **only if it differs from what we last pushed**. That makes it safe
to call ``sync_events`` on every Octopus refresh without burning Tesla's
energy-settings rate limit.
"""

from __future__ import annotations

import asyncio
import re
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from tesla_fleet_api.const import Scope
from tesla_fleet_api.exceptions import TeslaFleetError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    EVENT_TARIFF_PUSHED,
    LOGGER,
    STORAGE_KEY,
    STORAGE_VERSION,
    TESLA_FLEET_DOMAIN,
)
from .tariff import (
    EventWindow,
    TariffError,
    apply_events,
    normalise_label,
    validate_tariff,
)

# Tesla tariffs repeat weekly. An event must never be written into the tariff
# while the same weekday/time slot one week earlier is still ahead, or it
# would take effect a week early. By default events activate at local
# midnight on their own day, which always satisfies this.
HORIZON = timedelta(days=7)
ACTIVATE_EVENT_DAY = "event_day"
ACTIVATE_NOW = "now"
MAX_EVENT_LENGTH = timedelta(hours=24)
RETRY_DELAY = timedelta(minutes=5)


@dataclass
class Event:
    """A price override for an absolute time window."""

    id: str
    source: str
    start: str  # UTC ISO
    end: str  # UTC ISO
    buy: float | None = None
    sell: float | None = None
    name: str | None = None
    label: str | None = None  # reuse a base-tariff label instead of prices
    relabel: dict[str, str] = field(default_factory=dict)  # on the event's day
    activate_at: str | None = None  # UTC ISO; when it is written into the tariff

    @property
    def start_dt(self) -> datetime:
        return dt_util.parse_datetime(self.start)  # type: ignore[return-value]

    @property
    def end_dt(self) -> datetime:
        return dt_util.parse_datetime(self.end)  # type: ignore[return-value]

    @property
    def activate_dt(self) -> datetime:
        if self.activate_at:
            return dt_util.parse_datetime(self.activate_at)  # type: ignore[return-value]
        return self.end_dt - HORIZON

    def is_active(self, now: datetime) -> bool:
        return self.activate_dt <= now < self.end_dt


@dataclass
class SiteState:
    """Persisted state for one energy site."""

    device_id: str | None = None
    base: dict[str, Any] | None = None
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_hash: str | None = None
    last_push: str | None = None
    last_push_kind: str | None = None
    last_error: str | None = None

    def event_objs(self) -> list[Event]:
        return [Event(**e) for e in self.events.values()]


def event_windows(event: Event) -> list[EventWindow]:
    """Split an event into per-weekday windows in local (site) time."""
    start = dt_util.as_local(event.start_dt).replace(second=0, microsecond=0)
    end = dt_util.as_local(event.end_dt)
    if end.second or end.microsecond:
        end = end.replace(second=0, microsecond=0) + timedelta(minutes=1)
    windows: list[EventWindow] = []
    cur = start
    while cur < end:
        next_midnight = (cur + timedelta(days=1)).replace(hour=0, minute=0)
        seg_end = min(end, next_midnight)
        end_min = 1440 if seg_end == next_midnight else seg_end.hour * 60 + seg_end.minute
        windows.append(
            EventWindow(
                day=cur.weekday(),
                start=cur.hour * 60 + cur.minute,
                end=end_min,
                buy=event.buy,
                sell=event.sell,
                label=event.label,
                relabel=tuple(sorted(event.relabel.items())),
            )
        )
        cur = seg_end
    return windows


def drain_windows(
    sessions: list[tuple[datetime, datetime]], minutes: int | None = None
) -> list[tuple[datetime, datetime]]:
    """Windows to empty the battery ahead of each block of sessions.

    Sessions that touch or overlap are merged into blocks. Each block gets a
    window ending at the block's start, as long as the block itself (or
    ``minutes`` if given), clipped so it never overlaps an earlier block.
    """
    blocks: list[list[datetime]] = []
    for start, end in sorted(sessions):
        if blocks and start <= blocks[-1][1]:
            blocks[-1][1] = max(blocks[-1][1], end)
        else:
            blocks.append([start, end])
    windows: list[tuple[datetime, datetime]] = []
    prev_end: datetime | None = None
    for start, end in blocks:
        length = timedelta(minutes=minutes) if minutes else end - start
        pre_start = start - length
        if prev_end is not None and pre_start < prev_end:
            pre_start = prev_end
        if pre_start < start:
            windows.append((pre_start, start))
        prev_end = end
    return windows


def describe_plan(base_name: str | None, active: list[Event]) -> str:
    """Plan name shown in the Tesla app, e.g. 'Octopus Intelligent Go (Power Down)'.

    Any bracketed suffix on the normal plan's name is replaced; drain windows count as
    part of their session.
    """
    root = re.sub(r"\s*\([^)]*\)\s*$", "", base_name or "").strip() or "Home Assistant"
    kinds = []
    for event in active:
        kind = re.sub(r"\s*\(drain\)$", "", event.name or "Event")
        if kind not in kinds:
            kinds.append(kind)
    return f"{root} ({' + '.join(kinds) if kinds else 'Normal'})"


def describe_sessions(active: list[Event]) -> str:
    """Human summary, e.g. 'Power Down (18:00 - 19:00)'.

    Drain windows are listed with their session:
    'Power Up (10:00 - 11:00 drain, 11:00 - 12:00)'. Empty string for the normal plan.
    """
    groups: dict[str, list[list]] = {}
    for event in sorted(active, key=lambda e: e.start_dt):
        name = event.name or "Event"
        drain = name.endswith(" (drain)")
        if drain:
            name = name[: -len(" (drain)")]
        items = groups.setdefault(name, [])
        start, end = event.start_dt, event.end_dt
        # Back-to-back sessions read as one: 13:00 - 14:00 + 14:00 - 15:00 -> 13:00 - 15:00
        if items and items[-1][1] == start and items[-1][2] == drain:
            items[-1][1] = end
        else:
            items.append([start, end, drain])

    def fmt(item: list) -> str:
        start, end = dt_util.as_local(item[0]), dt_util.as_local(item[1])
        return f"{start:%H:%M} - {end:%H:%M}" + (" drain" if item[2] else "")

    return ", ".join(
        f"{name} ({', '.join(fmt(i) for i in items)})" for name, items in groups.items()
    )


def _hash(tariff: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(tariff, sort_keys=True).encode()).hexdigest()


class TariffManager:
    """Owns persisted base tariffs, events and the recompute timers."""

    def __init__(self, hass: HomeAssistant, schedule: str = "single") -> None:
        self.hass = hass
        self.schedule = schedule  # "single" or "weekday_weekend"
        self.store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.sites: dict[str, SiteState] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._next_check: dict[str, datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    async def async_load(self) -> None:
        data = await self.store.async_load() or {}
        for site_id, raw in data.items():
            known = {k: v for k, v in raw.items() if k in SiteState.__dataclass_fields__}
            self.sites[site_id] = SiteState(**known)

    async def _async_save(self) -> None:
        await self.store.async_save({k: asdict(v) for k, v in self.sites.items()})

    def site(self, site_id: str) -> SiteState:
        return self.sites.setdefault(site_id, SiteState())

    # ------------------------------------------------------------------ #
    # Resolving a device to a live Tesla Fleet energy site
    # ------------------------------------------------------------------ #
    def resolve(self, device_id: str) -> tuple[str, Any]:
        """Return (site_id, TeslaFleetEnergyData) for a Tesla Fleet device."""
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"Device {device_id} not found")
        site_id = next(
            (ident for dom, ident in device.identifiers if dom == TESLA_FLEET_DOMAIN),
            None,
        )
        if site_id is None:
            raise ServiceValidationError(f"'{device.name}' is not a Tesla Fleet device")
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != TESLA_FLEET_DOMAIN:
                continue
            if entry.state is not ConfigEntryState.LOADED:
                raise HomeAssistantError("The Tesla Fleet integration is not loaded")
            data = entry.runtime_data
            for energysite in data.energysites:
                if str(energysite.id) == site_id:
                    if Scope.ENERGY_CMDS not in data.scopes:
                        raise ServiceValidationError(
                            "The Tesla Fleet token is missing the 'energy_cmds' "
                            "scope. Re-authenticate Tesla Fleet and grant energy "
                            "commands."
                        )
                    self.site(site_id).device_id = device_id
                    return site_id, energysite
        raise ServiceValidationError(
            f"'{device.name}' is not a Tesla energy site (pick the Powerwall device)"
        )

    def _lock(self, site_id: str) -> asyncio.Lock:
        return self._locks.setdefault(site_id, asyncio.Lock())

    # ------------------------------------------------------------------ #
    # Composition
    # ------------------------------------------------------------------ #
    def compose(
        self, site_id: str, now: datetime | None = None, extra: list[Event] | None = None
    ) -> tuple[dict[str, Any], list[Event], list[Event]]:
        """Return (tariff, events in the tariff, events waiting for the horizon)."""
        state = self.site(site_id)
        if state.base is None:
            raise ServiceValidationError(
                "No base tariff stored for this site yet. Call "
                "tesla_fleet_tariff.set_base_tariff (or capture_base_tariff) first."
            )
        now = now or dt_util.utcnow()
        merged = {e.id: e for e in state.event_objs()}
        merged.update({e.id: e for e in extra or []})
        events = [e for e in merged.values() if e.end_dt > now]
        live = [e for e in events if e.is_active(now)]
        # Windows that already ended today stay in the plan while a later one
        # is still to come: they're in the past, so removing them would only
        # spend a Tesla plan change (and a notification). The normal plan is pushed once
        # the last window of the day ends.
        done: list[Event] = []
        if live:
            day_start = min(e.activate_dt for e in live)
            done = [
                e for e in merged.values()
                if e.end_dt <= now and e.end_dt > day_start and e.activate_dt <= now
            ]
        active = sorted(
            live + done,
            key=lambda e: (not e.id.endswith(":pre"), e.start_dt),
        )
        pending = [e for e in events if not e.is_active(now)]
        windows = [w for e in active for w in event_windows(e)]
        try:
            tariff, _ = apply_events(state.base, windows, self.schedule)
        except TariffError as err:
            raise ServiceValidationError(str(err)) from err
        plan_name = describe_plan(state.base.get("name"), active)
        for part in (tariff, tariff.get("sell_tariff") or {}):
            if part:
                part["name"] = plan_name
        if errors := validate_tariff(tariff):
            raise ServiceValidationError(
                "Tariff failed validation: " + "; ".join(errors[:5])
            )
        return tariff, active, pending

    # ------------------------------------------------------------------ #
    # Recompute + push
    # ------------------------------------------------------------------ #
    async def async_recompute(
        self, site_id: str, *, force: bool = False, reason: str = "update"
    ) -> dict[str, Any]:
        """Prune ended events, push base+events if changed, schedule next check."""
        async with self._lock(site_id):
            state = self.site(site_id)
            now = dt_util.utcnow()
            if not any(e.is_active(now) for e in state.event_objs()):
                # Nothing live: forget everything that has ended.
                state.events = {
                    k: v for k, v in state.events.items() if Event(**v).end_dt > now
                }
            if state.base is None:
                await self._async_save()
                return {"pushed": False, "reason": "no base tariff"}
            tariff, active, pending = self.compose(site_id, now)
            digest = _hash(tariff)
            pushed = False
            if force or digest != state.last_hash:
                summary = describe_sessions([e for e in active if e.end_dt > now])
                kind = (
                    "base"
                    if not active
                    else "events:" + ",".join(e.name or e.id for e in active)
                )
                try:
                    await self._async_push(site_id, tariff)
                except HomeAssistantError as err:
                    state.last_error = str(err)
                    await self._async_save()
                    self._schedule(site_id, now + RETRY_DELAY)
                    LOGGER.error("Tariff push failed (%s); retrying in 5 min", err)
                    raise
                state.last_hash = digest
                state.last_push = now.isoformat()
                state.last_push_kind = kind
                state.last_error = None
                pushed = True
                LOGGER.info("Pushed %s tariff to site %s (%s)", kind, site_id, reason)
                self.hass.bus.async_fire(
                    EVENT_TARIFF_PUSHED,
                    {
                        "device_id": state.device_id,
                        "site_id": site_id,
                        "kind": kind,
                        "reason": reason,
                        "events": [e.id for e in active],
                        "summary": summary,
                        "message": (
                            f"Pushed: {summary}" if summary
                            else "Pushed: Normal rate plan"
                        ),
                        "plan_name": tariff.get("name"),
                    },
                )
            await self._async_save()

            # Next time something changes on its own: an active event ends,
            # or a pending event's activation time arrives.
            candidates = [e.end_dt for e in active if e.end_dt > now] + [
                e.activate_dt for e in pending
            ]
            if candidates:
                self._schedule(site_id, min(candidates) + timedelta(seconds=1))
            else:
                self._cancel(site_id)
            return {
                "pushed": pushed,
                "active_events": [e.id for e in active],
                "pending_events": [e.id for e in pending],
            }

    async def _async_push(self, site_id: str, tariff: dict[str, Any]) -> None:
        device_id = self.site(site_id).device_id
        if device_id is None:
            raise HomeAssistantError(f"No device known for site {site_id}")
        _, energysite = self.resolve(device_id)
        try:
            resp = await energysite.api.time_of_use_settings(tariff)
        except TeslaFleetError as err:
            raise HomeAssistantError(
                f"Tesla rejected the tariff: {getattr(err, 'message', None) or err}"
            ) from err
        if isinstance(resp, dict) and resp.get("error"):
            raise HomeAssistantError(f"Tesla rejected the tariff: {resp['error']}")
        self.hass.async_create_task(energysite.info_coordinator.async_request_refresh())

    async def async_fetch_site_tariff(self, device_id: str) -> dict[str, Any] | None:
        """Read the tariff currently configured on the site, if exposed."""
        _, energysite = self.resolve(device_id)
        try:
            resp = await energysite.api.site_info()
        except TeslaFleetError as err:
            raise HomeAssistantError(
                f"Could not read site info: {getattr(err, 'message', None) or err}"
            ) from err
        info = resp.get("response", {}) if isinstance(resp, dict) else {}
        return info.get("tariff_content_v2")

    # ------------------------------------------------------------------ #
    # Public operations used by the services
    # ------------------------------------------------------------------ #
    async def async_set_base(
        self, device_id: str, tariff: dict[str, Any], push: bool
    ) -> dict[str, Any]:
        if errors := validate_tariff(tariff):
            raise ServiceValidationError(
                "Tariff failed validation: " + "; ".join(errors[:5])
            )
        site_id, _ = self.resolve(device_id)
        self.site(site_id).base = tariff
        await self._async_save()
        if not push:
            return {"pushed": False}
        return await self.async_recompute(site_id, reason="base tariff changed")

    @staticmethod
    def make_event(
        *,
        event_id: str | None,
        source: str,
        start: datetime,
        end: datetime,
        buy: float | None = None,
        sell: float | None = None,
        label: str | None = None,
        name: str | None = None,
        activate: str = ACTIVATE_EVENT_DAY,
        relabel: dict[str, str] | None = None,
    ) -> Event:
        start_u, end_u = dt_util.as_utc(start), dt_util.as_utc(end)
        if end_u <= start_u:
            raise ServiceValidationError("Event end must be after start")
        if end_u - start_u > MAX_EVENT_LENGTH:
            raise ServiceValidationError("Events must be 24 hours or shorter")
        if (label is None) == (buy is None):
            raise ServiceValidationError("Give either a label or a buy price")
        if buy is not None and sell is not None and sell > buy:
            raise ServiceValidationError(
                "Export price must not exceed import price (Tesla would raise "
                "the import price to match)"
            )
        if activate == ACTIVATE_NOW:
            activate_at = end_u - HORIZON
        else:
            # Midnight (local) at the start of the event's own day.
            activate_at = dt_util.as_utc(dt_util.start_of_local_day(dt_util.as_local(start_u)))
        return Event(
            id=str(event_id if event_id is not None else f"{source}:{start_u.isoformat()}"),
            source=source,
            start=start_u.isoformat(),
            end=end_u.isoformat(),
            buy=None if buy is None else float(buy),
            sell=None if buy is None else float(sell if sell is not None else buy),
            label=normalise_label(label) if label else None,
            name=name,
            activate_at=activate_at.isoformat(),
            relabel={
                normalise_label(k): normalise_label(v) for k, v in (relabel or {}).items()
            },
        )

    async def async_add_event(
        self, device_id: str, event: Event, extra: list[Event] | None = None
    ) -> dict[str, Any]:
        site_id, _ = self.resolve(device_id)
        if event.end_dt <= dt_util.utcnow():
            raise ServiceValidationError("That event has already finished")
        self.compose(site_id, extra=[*(extra or []), event])  # validate first
        for e in [*(extra or []), event]:
            self.site(site_id).events[e.id] = asdict(e)
        return await self.async_recompute(site_id, reason=f"event {event.id} added")

    async def async_remove_event(self, device_id: str, event_id: str) -> dict[str, Any]:
        site_id, _ = self.resolve(device_id)
        events = self.site(site_id).events
        if events.pop(str(event_id), None) is None:
            raise ServiceValidationError(f"No event with id '{event_id}'")
        events.pop(f"{event_id}:pre", None)  # its drain window, if any
        return await self.async_recompute(site_id, reason=f"event {event_id} removed")

    async def async_sync(
        self, device_id: str, source: str, events: list[Event]
    ) -> dict[str, Any]:
        """Replace all events from ``source`` (so cancellations drop out)."""
        site_id, _ = self.resolve(device_id)
        now = dt_util.utcnow()
        wanted = {e.id: asdict(e) for e in events if e.end_dt > now}
        state = self.site(site_id)
        kept = {k: v for k, v in state.events.items() if v["source"] != source}
        if state.base is not None:
            # Validate against the other sources only, so a changed/cancelled
            # event from this source can't block the sync.
            saved, state.events = state.events, kept
            try:
                self.compose(site_id, extra=[Event(**v) for v in wanted.values()])
            finally:
                state.events = saved
        state.events = {**kept, **wanted}
        result = await self.async_recompute(site_id, reason=f"sync {source}")
        result["synced"] = sorted(wanted)
        return result

    async def async_clear(
        self, device_id: str, source: str | None, force: bool
    ) -> dict[str, Any]:
        site_id, _ = self.resolve(device_id)
        state = self.site(site_id)
        state.events = {
            k: v
            for k, v in state.events.items()
            if source is not None and v["source"] != source
        }
        return await self.async_recompute(site_id, force=force, reason="events cleared")

    def preview(self, device_id: str, extra: list[Event] | None = None) -> dict[str, Any]:
        site_id, _ = self.resolve(device_id)
        tariff, active, pending = self.compose(site_id, extra=extra)
        return {
            "tariff": tariff,
            "active_events": [e.id for e in active],
            "pending_events": [e.id for e in pending],
        }

    def status(self, device_id: str) -> dict[str, Any]:
        site_id, _ = self.resolve(device_id)
        state = self.site(site_id)
        now = dt_util.utcnow()
        events = [
            {
                **asdict(e),
                "in_tariff": e.is_active(now),
                "running": e.start_dt <= now < e.end_dt,
            }
            for e in sorted(state.event_objs(), key=lambda e: e.start_dt)
        ]
        nxt = self._next_check.get(site_id)
        return {
            "site_id": site_id,
            "base_name": (state.base or {}).get("name"),
            "events": events,
            "last_push": state.last_push,
            "last_push_kind": state.last_push_kind,
            "last_error": state.last_error,
            "next_check": nxt.isoformat() if nxt else None,
        }

    # ------------------------------------------------------------------ #
    # Timers (state lives in the Store, so a restart just recomputes)
    # ------------------------------------------------------------------ #
    def _cancel(self, site_id: str) -> None:
        if unsub := self._timers.pop(site_id, None):
            unsub()
        self._next_check.pop(site_id, None)

    def _schedule(self, site_id: str, when: datetime) -> None:
        self._cancel(site_id)

        async def _fire(_now: datetime) -> None:
            self._timers.pop(site_id, None)
            self._next_check.pop(site_id, None)
            try:
                await self.async_recompute(site_id, reason="scheduled")
            except HomeAssistantError:
                pass  # logged; retry already scheduled

        self._timers[site_id] = async_track_point_in_utc_time(self.hass, _fire, when)
        self._next_check[site_id] = when

    async def async_resume(self) -> None:
        """After startup: catch up on anything that ended while HA was down."""
        for site_id, state in self.sites.items():
            if state.base is None or state.device_id is None:
                continue
            try:
                await self.async_recompute(site_id, reason="startup")
            except HomeAssistantError as err:
                LOGGER.warning("Startup recompute for site %s failed: %s", site_id, err)

    @callback
    def async_shutdown(self) -> None:
        for site_id in list(self._timers):
            self._cancel(site_id)
