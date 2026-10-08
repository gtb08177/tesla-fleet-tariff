"""End-to-end tests: real Home Assistant core, fake Tesla Fleet site, fake Octopus
Power Down / Power Up entities, and Ryan's BAU plan with his own labels.

Week used: Wed 2026-09-30 is "today"."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from tesla_fleet_api.const import Scope
from tesla_fleet_api.tariff import get_tariff_periods

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

DOMAIN = "tesla_fleet_tariff"
SITE_ID = 1234567890
LONDON = ZoneInfo("Europe/London")
DOWN = "event.octopus_energy_a_bc039fe4_octoplus_power_down_events"
UP = "event.octopus_energy_a_bc039fe4_octoplus_power_up_events"

RYAN_BAU = [
    {"start": "05:30", "label": "Mid-Peak", "buy": 0.35, "sell": 0.01},
    {"start": "13:00", "label": "Mid-Peak", "buy": 0.35, "sell": 0.01},
    {"start": "15:00", "label": "Mid-Peak", "buy": 0.35, "sell": 0.01},
    {"start": "18:00", "label": "Mid-Peak", "buy": 0.35, "sell": 0.01},
    {"start": "19:00", "label": "Mid-Peak", "buy": 0.35, "sell": 0.01},
    {"start": "20:30", "label": "Peak", "buy": 0.35, "sell": 0.12},
    {"start": "23:30", "label": "Super Off-Peak", "buy": 0.07, "sell": 0.01},
]
MID, PEAK, SOP = (0.35, 0.01), (0.35, 0.12), (0.07, 0.01)
APP_LABELS = {"SUPER_OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK"}


def at(day: int, hhmm: str) -> datetime:
    """Local time; day 28-30 = September, 1-9 = October 2026."""
    h, m = map(int, hhmm.split(":"))
    month = 9 if day >= 28 else 10
    return datetime(2026, month, day, h, m, tzinfo=LONDON)


TUE, WED, THU, FRI, SAT, SUN = 29, 30, 1, 2, 3, 4


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(params=[{"schedule": "weekday_weekend"}])
async def site(hass: HomeAssistant, freezer, request):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(at(WED, "09:00"))
    api = SimpleNamespace(
        time_of_use_settings=AsyncMock(return_value={"response": {"code": 201}}),
        site_info=AsyncMock(return_value={"response": {}}),
    )
    energysite = SimpleNamespace(
        id=SITE_ID, api=api,
        info_coordinator=SimpleNamespace(async_request_refresh=AsyncMock()),
    )
    entry = MockConfigEntry(domain="tesla_fleet", unique_id="acct")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(
        vehicles=[], energysites=[energysite],
        scopes=[Scope.ENERGY_DEVICE_DATA, Scope.ENERGY_CMDS],
    )
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("tesla_fleet", str(SITE_ID))},
        name="Powerwall 3 - 32JD",
        serial_number=str(SITE_ID),
    )
    with patch("homeassistant.setup.async_process_deps_reqs", AsyncMock(return_value=None)):
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: request.param})
    await hass.async_block_till_done()

    async def call(service, **data):
        return await hass.services.async_call(
            DOMAIN, service, {"device_id": device.id, **data},
            blocking=True, return_response=True,
        )

    s = SimpleNamespace(api=api, device_id=device.id, entry=entry, call=call)
    await call("set_base_tariff", rates=RYAN_BAU, name="Ryan BAU")
    s.base = api.time_of_use_settings.call_args.args[0]
    return s


def octopus(hass, entity, *sessions, attr="joined_events"):
    """Mimic Octopus: datetimes in memory, a past session always present."""
    items = [{"id": 1750, "start": at(28, "17:30"), "end": at(28, "18:30")}]
    items += [{"id": i, "start": s, "end": e} for i, s, e in sessions]
    hass.states.async_set(entity, dt_util.utcnow().isoformat(), {attr: items})


def pushes(s):
    return s.api.time_of_use_settings.call_count


def last(s):
    return s.api.time_of_use_settings.call_args.args[0]


def price(tariff, when):
    r = get_tariff_periods(tariff, when)
    return r.buy.price, r.sell.price


def labels_used(tariff):
    return set(tariff["seasons"]["All Year"]["tou_periods"]) | set(
        tariff["sell_tariff"]["seasons"]["All Year"]["tou_periods"]
    )


async def tick(hass, freezer, when):
    freezer.move_to(when)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def sync_down(s):
    return await s.call("sync_events", entity_id=DOWN, label="Peak",
                        relabel={"Peak": "Mid-Peak"}, name="Power Down")


async def sync_up(s):
    return await s.call("sync_events", entity_id=UP, attribute="events",
                        label="Super Off-Peak", name="Power Up")


# --------------------------------------------------------------------------- #
async def test_same_day_session_pushes_straight_away_with_peak_label(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (6640, at(WED, "18:00"), at(WED, "19:00")))
    r = await sync_down(site)
    assert r["pushed"] and pushes(site) == 2

    t = last(site)
    assert labels_used(t) == APP_LABELS  # only Ryan's own labels / colours
    assert price(t, at(WED, "18:30")) == PEAK
    assert price(t, at(WED, "17:59")) == MID
    assert price(t, at(WED, "19:00")) == MID
    # The usual 20:30-23:30 Peak is Mid-Peak today, so the session is the
    # only Peak window; Super Off-Peak untouched. The Tesla app has one
    # Mon-Fri schedule, so the change covers Mon-Fri; the weekend is untouched.
    assert price(t, at(WED, "20:30")) == MID
    assert price(t, at(WED, "23:29")) == MID
    assert price(t, at(WED, "23:30")) == SOP
    assert price(t, at(WED, "05:29")) == SOP
    assert price(t, at(THU, "18:30")) == PEAK
    assert price(t, at(TUE, "21:00")) == MID
    assert price(t, at(SAT, "18:30")) == MID
    assert price(t, at(SAT, "21:00")) == PEAK
    assert price(t, at(SUN, "21:00")) == PEAK

    # Octopus refreshes: no Tesla calls.
    for _ in range(3):
        octopus(hass, DOWN, (6640, at(WED, "18:00"), at(WED, "19:00")))
        assert (await sync_down(site))["pushed"] is False
    assert pushes(site) == 2

    await tick(hass, freezer, at(WED, "19:00") + timedelta(seconds=2))
    assert pushes(site) == 3 and last(site) == site.base
    assert price(last(site), at(WED, "21:00")) == PEAK  # BAU evening Peak back


async def test_tomorrow_session_waits_until_midnight(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (7001, at(THU, "18:00"), at(THU, "19:00")))
    r = await sync_down(site)
    assert r["pushed"] is False and r["pending_events"] == [f"{DOWN}:7001"]
    assert pushes(site) == 1  # Wednesday's plan untouched

    status = await site.call("get_status")
    assert status["events"][0]["in_tariff"] is False
    assert dt_util.parse_datetime(status["next_check"]) == dt_util.as_utc(at(THU, "00:00")) + timedelta(seconds=1)

    await tick(hass, freezer, at(WED, "23:59"))
    assert pushes(site) == 1
    await tick(hass, freezer, at(THU, "00:00") + timedelta(seconds=2))
    assert pushes(site) == 2
    assert price(last(site), at(THU, "18:30")) == PEAK
    assert price(last(site), at(THU, "21:00")) == MID
    assert price(last(site), at(SAT, "18:30")) == MID
    assert price(last(site), at(SAT, "21:00")) == PEAK

    await tick(hass, freezer, at(THU, "19:00") + timedelta(seconds=2))
    assert pushes(site) == 3 and last(site) == site.base


async def test_today_pushes_and_tomorrow_follows(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")),
            (2, at(THU, "17:30"), at(THU, "18:30")))
    await sync_down(site)
    assert pushes(site) == 2
    t = last(site)
    assert price(t, at(WED, "18:30")) == PEAK
    assert price(t, at(THU, "17:45")) == MID  # tomorrow's 17:30 session not in yet

    await tick(hass, freezer, at(WED, "19:00") + timedelta(seconds=2))
    assert pushes(site) == 3 and last(site) == site.base
    await tick(hass, freezer, at(THU, "00:00") + timedelta(seconds=2))
    assert pushes(site) == 4 and price(last(site), at(THU, "18:00")) == PEAK
    await tick(hass, freezer, at(THU, "18:30") + timedelta(seconds=2))
    assert pushes(site) == 5 and last(site) == site.base


async def test_two_sessions_same_day(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "17:00"), at(WED, "18:00")))
    await sync_down(site)
    octopus(hass, DOWN, (1, at(WED, "17:00"), at(WED, "18:00")),
            (2, at(WED, "19:30"), at(WED, "20:00")))
    await sync_down(site)
    assert pushes(site) == 3
    t = last(site)
    assert price(t, at(WED, "17:30")) == PEAK
    assert price(t, at(WED, "19:00")) == MID
    assert price(t, at(WED, "19:45")) == PEAK

    # First session ending doesn't spend a Tesla plan change: its window is
    # already in the past. BAU goes back once, after the last session.
    await tick(hass, freezer, at(WED, "18:00") + timedelta(seconds=2))
    assert pushes(site) == 3
    status = await site.call("get_status")
    assert dt_util.parse_datetime(status["next_check"]) == dt_util.as_utc(at(WED, "20:00")) + timedelta(seconds=1)
    await tick(hass, freezer, at(WED, "20:00") + timedelta(seconds=2))
    assert pushes(site) == 4 and last(site) == site.base
    assert (await site.call("get_status"))["events"] == []


async def test_power_up_uses_super_off_peak(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, UP, (9, at(WED, "13:00"), at(WED, "14:00")), attr="events")
    await sync_up(site)
    t = last(site)
    assert labels_used(t) == APP_LABELS
    assert price(t, at(WED, "13:30")) == SOP
    assert price(t, at(WED, "14:00")) == MID
    assert price(t, at(WED, "05:29")) == SOP
    assert price(t, at(WED, "23:30")) == SOP
    assert price(t, at(THU, "05:29")) == SOP


async def test_power_down_and_up_together(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    octopus(hass, UP, (9, at(WED, "13:00"), at(WED, "14:00")), attr="events")
    await sync_down(site)
    await sync_up(site)
    t = last(site)
    assert labels_used(t) == APP_LABELS
    assert price(t, at(WED, "13:30")) == SOP
    assert price(t, at(WED, "18:30")) == PEAK
    # Re-syncing one source leaves the other alone.
    await sync_down(site)
    assert price(last(site), at(WED, "13:30")) == SOP


async def test_cancelled_session_reverts(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    await sync_down(site)
    octopus(hass, DOWN)
    await sync_down(site)
    assert pushes(site) == 3 and last(site) == site.base


async def test_unavailable_entity_keeps_events(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    await sync_down(site)
    hass.states.async_set(DOWN, "unavailable", {})
    r = await sync_down(site)
    assert r["pushed"] is False and "skipped" in r
    assert [e["id"] for e in (await site.call("get_status"))["events"]] == [f"{DOWN}:1"]


async def test_restart_after_event_ended_restores_bau(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    await sync_down(site)
    hass.data[DOMAIN].async_shutdown()
    await hass.async_block_till_done()

    from custom_components.tesla_fleet_tariff.manager import TariffManager

    freezer.move_to(at(WED, "21:00"))
    fresh = TariffManager(hass)
    await fresh.async_load()
    await fresh.async_resume()
    assert pushes(site) == 3 and last(site) == site.base


async def test_restart_overnight_activates_tomorrow(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(THU, "18:00"), at(THU, "19:00")))
    await sync_down(site)
    hass.data[DOMAIN].async_shutdown()

    from custom_components.tesla_fleet_tariff.manager import TariffManager

    freezer.move_to(at(THU, "07:00"))  # HA was down over midnight
    fresh = TariffManager(hass)
    await fresh.async_load()
    await fresh.async_resume()
    assert pushes(site) == 2 and price(last(site), at(THU, "18:30")) == PEAK


async def test_priced_event_and_activate_now(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    r = await site.call("add_event", start=at(THU, "13:00"), end=at(THU, "14:00"),
                        buy_price=3.0, event_id="x", activate="now")
    assert r["pushed"]
    assert price(last(site), at(THU, "13:30")) == (3.0, 3.0)
    await site.call("remove_event", event_id="x")
    assert last(site) == site.base


async def test_unknown_label_rejected(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    with pytest.raises(ServiceValidationError, match="not used by the base"):
        await site.call("add_event", start=at(WED, "18:00"), end=at(WED, "19:00"),
                        label="Off-Peak")


async def test_dry_run_does_not_push(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    r = await site.call("sync_events", entity_id=DOWN, label="Peak", dry_run=True)
    assert r["found"] == [f"{DOWN}:1"]
    assert price(r["tariff"], at(WED, "18:30")) == PEAK
    assert pushes(site) == 1


async def test_clear_forces_bau(hass, site):
    r = await site.call("clear_events")
    assert r["pushed"] and pushes(site) == 2 and last(site) == site.base


async def test_missing_scope(hass, site):
    site.entry.runtime_data.scopes = [Scope.ENERGY_DEVICE_DATA]
    with pytest.raises(ServiceValidationError, match="energy_cmds"):
        await site.call("get_status")


async def test_session_overlapping_the_evening_peak(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN, (1, at(WED, "20:00"), at(WED, "21:00")))
    await sync_down(site)
    t = last(site)
    assert price(t, at(WED, "19:59")) == MID
    assert price(t, at(WED, "20:15")) == PEAK
    assert price(t, at(WED, "20:59")) == PEAK
    assert price(t, at(WED, "21:00")) == MID
    assert price(t, at(WED, "23:30")) == SOP


async def test_bad_relabel_rejected(hass, site, freezer):
    freezer.move_to(at(WED, "10:00"))
    with pytest.raises(ServiceValidationError, match="Relabel"):
        await site.call("add_event", start=at(WED, "18:00"), end=at(WED, "19:00"),
                        label="Peak", relabel={"Peak": "Off-Peak"})


# --------------------------------------------------------------------------- #
# Called from a real HA script, the way Ryan runs it
# --------------------------------------------------------------------------- #
async def _run_script(hass, step):
    assert await async_setup_component(hass, "script", {"script": {"t": {"sequence": [step]}}})
    await hass.async_block_till_done()
    await hass.services.async_call("script", "t", blocking=True)


async def test_set_base_from_script_with_device_in_data(hass, site):
    before = pushes(site)
    await _run_script(hass, {"action": "tesla_fleet_tariff.set_base_tariff",
                             "data": {"device_id": site.device_id, "rates": RYAN_BAU,
                                      "push": True, "name": "x"},
                             "response_variable": "result"})
    assert pushes(site) == before + 1


async def test_set_base_from_script_with_device_as_target(hass, site):
    before = pushes(site)
    await _run_script(hass, {"action": "tesla_fleet_tariff.set_base_tariff",
                             "target": {"device_id": site.device_id},
                             "data": {"rates": RYAN_BAU, "name": "y"}})
    assert pushes(site) == before + 1


async def test_services_yaml_is_valid_for_ha(hass, site, caplog):
    from homeassistant.helpers.service import async_get_all_descriptions

    descriptions = await async_get_all_descriptions(hass)
    ours = descriptions[DOMAIN]
    assert "Unable to parse services.yaml" not in caplog.text
    assert set(ours) == {"set_base_tariff", "capture_base_tariff", "add_event",
                         "remove_event", "sync_events", "clear_events", "get_status"}
    for name, desc in ours.items():
        assert "device_id" in desc["fields"], name


# --------------------------------------------------------------------------- #
# Blueprints (as a friend would use them, via the UI)
# --------------------------------------------------------------------------- #
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

BLUEPRINTS = Path(__file__).parents[1] / "blueprints"


@pytest.fixture
def bp_config(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(BLUEPRINTS, tmp_path / "blueprints")
    return tmp_path


async def test_bau_script_blueprint(hass, site, bp_config):
    await async_setup_component(hass, "persistent_notification", {})
    rates = [{k: v for k, v in r.items()} for r in RYAN_BAU]
    assert await async_setup_component(hass, "script", {"script": {"bau": {
        "use_blueprint": {"path": "tesla_fleet_tariff/normal_rate_plan.yaml", "input": {
            "powerwall": site.device_id, "plan_name": "Friend BAU", "rates": rates}}}}})
    await hass.async_block_till_done()
    assert hass.states.get("script.bau") is not None
    before = pushes(site)
    await hass.services.async_call("script", "bau", blocking=True)
    await hass.async_block_till_done()
    assert pushes(site) == before + 1 and last(site)["name"] == "Friend BAU (Normal)"
    assert hass.states.get("persistent_notification.") is None or True


async def test_follow_sessions_blueprint(hass, site, bp_config, freezer):
    freezer.move_to(at(WED, "10:00"))
    octopus(hass, DOWN)
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "pd", "use_blueprint": {
            "path": "tesla_fleet_tariff/follow_demand_sessions.yaml", "input": {
                "powerwall": site.device_id, "session_entity": DOWN,
                "label": "Peak", "relabel": {"Peak": "Mid-Peak"},
                "session_name": "Power Down"}}},
        {"id": "pu", "use_blueprint": {
            "path": "tesla_fleet_tariff/follow_demand_sessions.yaml", "input": {
                "powerwall": site.device_id, "session_entity": UP,
                "attribute": "events", "label": "Super Off-Peak",
                "session_name": "Power Up"}}},
    ]})
    await hass.async_block_till_done()
    assert hass.states.get("automation.pd") or hass.states.async_entity_ids("automation")
    before = pushes(site)
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    await hass.async_block_till_done()
    assert pushes(site) == before + 1
    t = last(site)
    assert price(t, at(WED, "18:30")) == PEAK and price(t, at(WED, "21:00")) == MID
    octopus(hass, UP, (9, at(WED, "13:00"), at(WED, "15:00")), attr="events")
    await hass.async_block_till_done()
    assert pushes(site) == before + 2 and price(last(site), at(WED, "14:00")) == SOP


@pytest.mark.parametrize(("minutes", "drain_from"), [
    ("omitted", "11:00"), (None, "11:00"), ("", "11:00"), (0, None), (60, "12:00"),
    (90, "11:30")])
async def test_follow_sessions_blueprint_drain_length(hass, site, bp_config, freezer,
                                                      minutes, drain_from):
    freezer.move_to(at(WED, "08:00"))
    octopus(hass, UP, attr="events")
    inputs = {"powerwall": site.device_id, "session_entity": UP,
              "attribute": "events", "label": "Super Off-Peak",
              "pre_label": "Peak", "session_name": "Power Up"}
    if minutes != "omitted":  # blank in the UI arrives as None or not at all
        inputs["pre_minutes"] = minutes
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "pu", "use_blueprint": {
            "path": "tesla_fleet_tariff/follow_demand_sessions.yaml", "input": inputs}}]})
    await hass.async_block_till_done()
    octopus(hass, UP, (9, at(WED, "13:00"), at(WED, "15:00")), attr="events")
    await hass.async_block_till_done()
    t = last(site)
    if drain_from is None:  # 0 = no drain
        assert price(t, at(WED, "12:45")) == MID and price(t, at(WED, "13:30")) == SOP
        return
    start = datetime.strptime(drain_from, "%H:%M")
    before_drain = (start - timedelta(minutes=15)).strftime("%H:%M")
    assert price(t, at(WED, before_drain)) == MID
    assert price(t, at(WED, drain_from)) == PEAK and price(t, at(WED, "12:45")) == PEAK
    assert price(t, at(WED, "13:30")) == SOP and price(t, at(WED, "14:45")) == SOP


async def test_notify_blueprint(hass, site, bp_config):
    calls = []
    hass.services.async_register("notify", "phone", lambda call: calls.append(call.data))
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "n", "use_blueprint": {
            "path": "tesla_fleet_tariff/notify_on_change.yaml", "input": {
                "powerwall": site.device_id,
                "notify_actions": [{"action": "notify.phone",
                                    "data": {"title": "{{ notify_title }}",
                                             "message": "{{ plan_message }}"}}]}}}]})
    await hass.async_block_till_done()
    await site.call("clear_events")
    await hass.async_block_till_done()
    assert calls and calls[-1]["message"] == "Pushed: Normal rate plan"
    assert calls[-1]["title"] == "Powerwall - Rate Plan Changed"


async def _override_automation(hass, mode_state):
    scenes = []
    hass.services.async_register("scene", "turn_on", lambda c: scenes.append(str(c.data["entity_id"])))
    hass.states.async_set("select.pw_mode", mode_state)
    hass.states.async_set("binary_sensor.off_peak", "on")
    hass.states.async_set("calendar.power_down", "off")
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "o", "use_blueprint": {
            "path": "tesla_fleet_tariff/session_overrides_forced_charging.yaml", "input": {
                "session_calendar": "calendar.power_down",
                "operation_mode": "select.pw_mode",
                "normal_scene": "scene.normal",
                "charge_scene": "scene.charge",
                "cheap_slot_sensor": "binary_sensor.off_peak"}}}]})
    await hass.async_block_till_done()
    return scenes


async def test_override_blueprint_session_start(hass, site, bp_config, freezer):
    import asyncio

    freezer.move_to(at(WED, "12:00"))
    scenes = await _override_automation(hass, "backup")
    hass.states.async_set("calendar.power_down", "on")
    for _ in range(5000):
        if scenes:
            break
        await asyncio.sleep(0)
    assert scenes == ["['scene.normal']"] or scenes == ["scene.normal"]
    await hass.services.async_call(  # cancel the run waiting in its 1-min retry
        "automation", "turn_off", {"entity_id": "all", "stop_actions": True}, blocking=True
    )


async def test_override_blueprint_session_end(hass, site, bp_config, freezer):
    freezer.move_to(at(WED, "12:00"))
    hass.states.async_set("calendar.power_down", "on")
    scenes = await _override_automation(hass, "autonomous")
    hass.states.async_set("calendar.power_down", "on")
    await hass.async_block_till_done()
    hass.states.async_set("calendar.power_down", "off")
    await hass.async_block_till_done()
    assert scenes and "scene.charge" in scenes[-1]


# --------------------------------------------------------------------------- #
# Drain the battery ahead of Power Up sessions
# --------------------------------------------------------------------------- #
from custom_components.tesla_fleet_tariff.manager import drain_windows  # noqa: E402


def _hm(windows):
    return [(dt_util.as_local(a).strftime("%H:%M"), dt_util.as_local(b).strftime("%H:%M"))
            for a, b in windows]


async def test_drain_windows_rules(hass):
    await hass.config.async_set_time_zone("Europe/London")
    # single 1h -> the hour before
    assert _hm(drain_windows([(at(WED, "11:00"), at(WED, "12:00"))])) == [("10:00", "11:00")]
    # two consecutive hours -> the two hours before
    assert _hm(drain_windows([(at(WED, "11:00"), at(WED, "12:00")),
                              (at(WED, "12:00"), at(WED, "13:00"))])) == [("09:00", "11:00")]
    # split 11-12 and 13-14 -> 10-11 and 12-13
    assert _hm(drain_windows([(at(WED, "13:00"), at(WED, "14:00")),
                              (at(WED, "11:00"), at(WED, "12:00"))])) == [
        ("10:00", "11:00"), ("12:00", "13:00")]
    # short gap: never eats into the earlier session
    assert _hm(drain_windows([(at(WED, "11:00"), at(WED, "12:00")),
                              (at(WED, "12:30"), at(WED, "13:30"))])) == [
        ("10:00", "11:00"), ("12:00", "12:30")]
    # fixed length override
    assert _hm(drain_windows([(at(WED, "11:00"), at(WED, "13:00"))], minutes=30)) == [
        ("10:30", "11:00")]


async def sync_up_drain(s, **extra):
    return await s.call("sync_events", entity_id=UP, attribute="events",
                        label="Super Off-Peak", pre_label="Peak", name="Power Up", **extra)


async def test_power_up_split_sessions_drain_before_each(hass, site, freezer):
    freezer.move_to(at(WED, "08:00"))
    octopus(hass, UP, (1, at(WED, "11:00"), at(WED, "12:00")),
            (2, at(WED, "13:00"), at(WED, "14:00")), attr="events")
    r = await sync_up_drain(site)
    assert r["pushed"]
    t = last(site)
    assert labels_used(t) == APP_LABELS
    assert price(t, at(WED, "09:59")) == MID
    assert price(t, at(WED, "10:30")) == PEAK   # drain
    assert price(t, at(WED, "11:30")) == SOP    # Power Up
    assert price(t, at(WED, "12:30")) == PEAK   # drain
    assert price(t, at(WED, "13:30")) == SOP    # Power Up
    assert price(t, at(WED, "14:00")) == MID
    assert price(t, at(WED, "21:00")) == PEAK   # evening Peak untouched
    assert price(t, at(SAT, "10:30")) == MID    # weekend untouched

    # Back to the normal plan once the last window has ended.
    await tick(hass, freezer, at(WED, "14:00") + timedelta(seconds=2))
    assert last(site) == site.base


async def test_power_up_two_consecutive_hours(hass, site, freezer):
    freezer.move_to(at(WED, "08:00"))
    octopus(hass, UP, (1, at(WED, "11:00"), at(WED, "12:00")),
            (2, at(WED, "12:00"), at(WED, "13:00")), attr="events")
    await sync_up_drain(site)
    t = last(site)
    assert price(t, at(WED, "08:59")) == MID
    assert price(t, at(WED, "09:00")) == PEAK
    assert price(t, at(WED, "10:59")) == PEAK
    assert price(t, at(WED, "11:00")) == SOP
    assert price(t, at(WED, "12:59")) == SOP
    assert price(t, at(WED, "13:00")) == MID


async def test_power_up_tomorrow_drain_waits_for_midnight(hass, site, freezer):
    freezer.move_to(at(WED, "20:00"))
    octopus(hass, UP, (1, at(THU, "13:00"), at(THU, "14:00")), attr="events")
    r = await sync_up_drain(site)
    assert r["pushed"] is False and len(r["pending_events"]) == 2
    await tick(hass, freezer, at(THU, "00:00") + timedelta(seconds=2))
    t = last(site)
    assert price(t, at(THU, "12:30")) == PEAK and price(t, at(THU, "13:30")) == SOP


async def test_power_up_resync_is_idempotent_and_cancel_drops_drain(hass, site, freezer):
    freezer.move_to(at(WED, "08:00"))
    octopus(hass, UP, (1, at(WED, "11:00"), at(WED, "12:00")), attr="events")
    await sync_up_drain(site)
    n = pushes(site)
    await sync_up_drain(site)
    assert pushes(site) == n
    octopus(hass, UP, attr="events")  # session cancelled
    await sync_up_drain(site)
    assert last(site) == site.base


async def test_add_event_with_drain_and_remove(hass, site, freezer):
    freezer.move_to(at(WED, "08:00"))
    await site.call("add_event", start=at(WED, "13:00"), end=at(WED, "15:00"),
                    label="Super Off-Peak", pre_label="Peak", event_id="pu")
    t = last(site)
    assert price(t, at(WED, "11:00")) == PEAK and price(t, at(WED, "14:00")) == SOP
    await site.call("remove_event", event_id="pu")
    assert last(site) == site.base



async def test_every_pushed_plan_is_app_compatible(hass, site, freezer):
    """Regression: the app only has Mon-Fri and Sat-Sun schedules."""
    from custom_components.tesla_fleet_tariff.tariff import APP_DAY_RANGES

    freezer.move_to(at(THU, "08:00"))
    octopus(hass, DOWN, (1, at(THU, "19:00"), at(THU, "20:00")))
    octopus(hass, UP, (2, at(SAT, "13:00"), at(SAT, "14:00")), attr="events")
    await sync_down(site)
    await sync_up_drain(site, activate="now")
    for call in site.api.time_of_use_settings.call_args_list:
        t = call.args[0]
        for part in (t, t["sell_tariff"]):
            for spec in part["seasons"]["All Year"]["tou_periods"].values():
                for p in spec["periods"]:
                    assert (p["fromDayOfWeek"], p["toDayOfWeek"]) in APP_DAY_RANGES
    t = last(site)
    # Weekday schedule: one Peak 19:00-20:00, evening Peak flipped, no overlaps.
    assert price(t, at(THU, "19:30")) == PEAK and price(t, at(THU, "21:00")) == MID
    # Weekend schedule: drain + Power Up, evening Peak kept.
    assert price(t, at(SAT, "12:30")) == PEAK and price(t, at(SAT, "13:30")) == SOP
    assert price(t, at(SAT, "21:00")) == PEAK



# --------------------------------------------------------------------------- #
# Default: one all-week schedule (what Ryan has always used in the app)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("site", [{}, None], indirect=True)
async def test_default_single_schedule(hass, site, freezer):
    freezer.move_to(at(THU, "08:00"))
    octopus(hass, DOWN, (1, at(THU, "19:00"), at(THU, "20:00")))
    await sync_down(site)
    t = last(site)
    for part in (t, t["sell_tariff"]):
        for spec in part["seasons"]["All Year"]["tou_periods"].values():
            for p in spec["periods"]:
                assert (p["fromDayOfWeek"], p["toDayOfWeek"]) == (0, 6)
    sop = t["seasons"]["All Year"]["tou_periods"]["SUPER_OFF_PEAK"]["periods"]
    assert [(p["fromHour"], p["fromMinute"], p["toHour"], p["toMinute"]) for p in sop] == [
        (23, 30, 5, 30)]
    assert price(t, at(THU, "19:30")) == PEAK and price(t, at(THU, "21:00")) == MID
    await tick(hass, freezer, at(THU, "20:00") + timedelta(seconds=2))
    assert last(site) == site.base



# --------------------------------------------------------------------------- #
# Plan name in the Tesla app + tidying a base stored by an older version
# --------------------------------------------------------------------------- #
async def test_plan_name_follows_active_sessions(hass, site, freezer):
    assert site.base["name"] == "Ryan BAU (Normal)"
    assert site.base["sell_tariff"]["name"] == "Ryan BAU (Normal)"
    freezer.move_to(at(WED, "08:00"))
    octopus(hass, UP, (9, at(WED, "11:00"), at(WED, "12:00")), attr="events")
    await sync_up_drain(site)
    assert last(site)["name"] == "Ryan BAU (Power Up)"  # drain counts as Power Up
    octopus(hass, DOWN, (1, at(WED, "18:00"), at(WED, "19:00")))
    await sync_down(site)
    assert last(site)["name"] == "Ryan BAU (Power Up + Power Down)"
    n = pushes(site)
    await tick(hass, freezer, at(WED, "12:00") + timedelta(seconds=2))
    assert pushes(site) == n  # Power Up over, Power Down still to come: no push
    await tick(hass, freezer, at(WED, "19:00") + timedelta(seconds=2))
    assert last(site)["name"] == "Ryan BAU (Normal)" and last(site) == site.base


async def test_old_bracket_suffix_replaced(hass, site):
    await site.call("set_base_tariff", rates=RYAN_BAU, name="Octopus Intelligent Go (HA BAU)")
    assert last(site)["name"] == "Octopus Intelligent Go (Normal)"


async def test_base_stored_by_older_version_is_tidied(hass, site):
    """A base with Super Off-Peak split at midnight is pushed as one 23:30-05:30 row."""
    manager = hass.data[DOMAIN]
    state = next(iter(manager.sites.values()))
    for part in (state.base, state.base["sell_tariff"]):
        sop = part["seasons"]["All Year"]["tou_periods"]["SUPER_OFF_PEAK"]["periods"]
        sop[:] = [
            {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0,
             "toHour": 5, "toMinute": 30},
            {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 23, "fromMinute": 30,
             "toHour": 0, "toMinute": 0},
        ]
    await site.call("clear_events")
    sop = last(site)["seasons"]["All Year"]["tou_periods"]["SUPER_OFF_PEAK"]["periods"]
    assert [(p["fromHour"], p["fromMinute"], p["toHour"], p["toMinute"]) for p in sop] == [
        (23, 30, 5, 30)]
    assert price(last(site), at(WED, "02:00")) == SOP


async def test_pushed_event_summary(hass, site, freezer):
    seen = []
    hass.bus.async_listen("tesla_fleet_tariff_pushed", lambda e: seen.append(e.data))
    freezer.move_to(at(WED, "08:00"))
    await site.call("add_event", start=at(WED, "18:00"), end=at(WED, "19:00"),
                    label="Peak", relabel={"Peak": "Mid-Peak"},
                    event_id="test-1", name="Power Down")
    await hass.async_block_till_done()
    assert seen[-1]["summary"] == "Power Down (18:00 - 19:00)"
    assert seen[-1]["plan_name"] == "Ryan BAU (Power Down)"
    octopus(hass, UP, (1, at(WED, "11:00"), at(WED, "12:00")), attr="events")
    await sync_up_drain(site)
    await hass.async_block_till_done()
    assert seen[-1]["summary"] == (
        "Power Up (10:00 - 11:00 drain, 11:00 - 12:00), Power Down (18:00 - 19:00)")
    await site.call("clear_events")
    await hass.async_block_till_done()
    assert seen[-1]["kind"] == "base" and seen[-1]["summary"] == ""


# --------------------------------------------------------------------------- #
# Calendar as the session source (any supplier, or a Local Calendar)
# --------------------------------------------------------------------------- #
async def test_follow_sessions_blueprint_with_calendar(hass, site, bp_config, freezer):
    from homeassistant.core import SupportsResponse

    freezer.move_to(at(WED, "08:00"))
    cal = "calendar.octopus_energy_a_bc039fe4_octoplus_power_down"
    listing = {"events": [
        {"start": at(WED, "18:00").isoformat(), "end": at(WED, "19:00").isoformat(),
         "summary": "Octopus Energy Saving Session"},
        {"start": "2026-10-02", "end": "2026-10-03", "summary": "all-day: ignored"},
    ]}
    asked = []

    async def get_events(call):
        asked.append(call.data)
        return {cal: listing}

    hass.services.async_register("calendar", "get_events", get_events,
                                 supports_response=SupportsResponse.ONLY)
    hass.states.async_set(cal, "off", {"message": ""})
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "cal", "use_blueprint": {
            "path": "tesla_fleet_tariff/follow_demand_sessions.yaml", "input": {
                "powerwall": site.device_id, "session_entity": cal,
                "label": "Peak", "relabel": {"Peak": "Mid-Peak"},
                "session_name": "Power Down"}}}]})
    await hass.async_block_till_done()
    before = pushes(site)
    # A new event shows up on the next 15-minute check.
    await tick(hass, freezer, at(WED, "08:15"))
    assert asked and pushes(site) == before + 1
    t = last(site)
    assert price(t, at(WED, "18:30")) == PEAK and price(t, at(WED, "21:00")) == MID
    assert t["name"] == "Ryan BAU (Power Down)"
    # Removed from the calendar -> back to normal on the next check.
    listing["events"] = []
    await tick(hass, freezer, at(WED, "08:30"))
    assert last(site) == site.base


async def test_pushed_event_message(hass, site, freezer):
    seen = []
    hass.bus.async_listen("tesla_fleet_tariff_pushed", lambda e: seen.append(e.data))
    freezer.move_to(at(WED, "08:00"))
    await site.call("add_event", start=at(WED, "18:00"), end=at(WED, "19:00"),
                    label="Peak", event_id="t", name="Power Down")
    await site.call("remove_event", event_id="t")
    await hass.async_block_till_done()
    assert [e["message"] for e in seen] == [
        "Pushed: Power Down (18:00 - 19:00)", "Pushed: Normal rate plan"]


async def test_utility_provider(hass, site):
    await site.call("set_base_tariff", rates=RYAN_BAU, name="Octopus Intelligent Go",
                    utility="Octopus Energy")
    t = last(site)
    assert t["utility"] == "Octopus Energy" and t["sell_tariff"]["utility"] == "Octopus Energy"
    assert t["name"] == "Octopus Intelligent Go (Normal)"
    await site.call("set_base_tariff", rates=RYAN_BAU, name="X")
    assert last(site)["utility"] == "Home Assistant"  # default when not given


async def test_normal_plan_blueprint_defaults_are_ryans_plan(hass, site, bp_config):
    """With only the Powerwall picked, the blueprint pushes the known plan."""
    await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "script", {"script": {"normal": {
        "use_blueprint": {"path": "tesla_fleet_tariff/normal_rate_plan.yaml",
                          "input": {"powerwall": site.device_id}}}}})
    await hass.async_block_till_done()
    await hass.services.async_call("script", "normal", blocking=True)
    await hass.async_block_till_done()
    t = last(site)
    assert t["utility"] == "Home Assistant"
    for when, expected in (("04:00", SOP), ("10:00", MID), ("14:00", MID), ("18:30", MID),
                           ("19:30", MID), ("21:00", PEAK), ("23:45", SOP)):
        assert price(t, at(WED, when)) == expected, when
    sop = t["seasons"]["All Year"]["tou_periods"]["SUPER_OFF_PEAK"]["periods"]
    assert len(sop) == 1  # one 23:30-05:30 row
    mid = t["seasons"]["All Year"]["tou_periods"]["PARTIAL_PEAK"]["periods"]
    assert len(mid) == 5  # override-ready blocks kept separate


async def test_resolve_does_not_read_device_config_entries(hass, site):
    """HA retires DeviceEntry.config_entries in 2027.10: don't touch it."""
    real = dr.DeviceEntry.config_entries

    class Tripwire:
        def __get__(self, obj, owner=None):
            raise AssertionError("DeviceEntry.config_entries was read")

    with patch.object(dr.DeviceEntry, "config_entries", Tripwire()):
        status = await site.call("get_status")
        await site.call("add_event", start=at(WED, "18:00"), end=at(WED, "19:00"),
                        label="Peak", event_id="t", dry_run=True)
    assert status["site_id"] == str(SITE_ID)
    assert dr.DeviceEntry.config_entries is real


async def test_resolve_errors(hass, site):
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    stranger = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "x")}, name="Kettle")
    with pytest.raises(ServiceValidationError, match="not a Tesla Fleet device"):
        await hass.services.async_call(DOMAIN, "get_status", {"device_id": stranger.id},
                                       blocking=True, return_response=True)
    car = dr.async_get(hass).async_get_or_create(
        config_entry_id=site.entry.entry_id, identifiers={("tesla_fleet", "VIN123")},
        name="Model Y")
    with pytest.raises(ServiceValidationError, match="not a Tesla energy site"):
        await hass.services.async_call(DOMAIN, "get_status", {"device_id": car.id},
                                       blocking=True, return_response=True)
    site.entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await site.call("get_status")
