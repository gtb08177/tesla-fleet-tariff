"""The UI-ready BAU script must load in a real HA as a script config."""

import pathlib

import yaml

from homeassistant.setup import async_setup_component

SCRIPT = pathlib.Path(__file__).parents[1] / "examples/normal_rate_plan_script.yaml"


async def test_bau_script_is_valid_ha_script(hass):
    body = yaml.safe_load(SCRIPT.read_text())
    assert "sequence" in body and "powerwall_set_bau_rate_plan" not in body
    assert await async_setup_component(
        hass, "script", {"script": {"powerwall_set_bau_rate_plan": body}}
    )
    await hass.async_block_till_done()
    state = hass.states.get("script.powerwall_set_bau_rate_plan")
    assert state is not None
    assert state.attributes["friendly_name"] == "Powerwall - Set normal rate plan"
