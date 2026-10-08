"""Make this repo's custom_components visible to the HA test harness."""

import pathlib

import custom_components

_ours = str(pathlib.Path(__file__).parents[1] / "custom_components")
if _ours not in custom_components.__path__:
    custom_components.__path__.insert(0, _ours)
