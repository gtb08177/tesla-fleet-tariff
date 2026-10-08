"""Constants for Tesla Fleet Tariff."""

from __future__ import annotations

import logging

DOMAIN = "tesla_fleet_tariff"
TESLA_FLEET_DOMAIN = "tesla_fleet"
LOGGER = logging.getLogger(__package__)

STORAGE_KEY = f"{DOMAIN}.sites"
STORAGE_VERSION = 1

# Fired on the HA bus every time a tariff is actually sent to Tesla.
EVENT_TARIFF_PUSHED = f"{DOMAIN}_pushed"
