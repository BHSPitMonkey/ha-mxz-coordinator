"""Opt-in: a setpoint / on-off change made at a head is ADOPTED by the room.

Default (option off) the coordinator overwrites it. With ``accept_head_changes``
the room's target + enable take the head's value; the head's heat/cool choice
is never adopted (the plan keeps the shared mode).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    ADOPT_DELAY,
    CONF_ACCEPT_HEAD_CHANGES,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_target,
    _set_temp,
    _setup_mock_heads,
)


async def _setup(hass: HomeAssistant, *, accept: bool = True):
    """Primary hot (75 vs 70): head A cooling to 70, B satisfied."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 75)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_ACCEPT_HEAD_CHANGES: accept,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 70)
    await _set_target(hass, _eid(hass, entry, "_secondary_target"), 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    return head_a, entry


async def _wall(hass: HomeAssistant, head: str, service: str, **data) -> None:
    """A person at the head: calls the head directly, bypassing the coordinator."""
    await hass.services.async_call(
        "climate", service, {"entity_id": head, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def _elapse(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _target(hass, entry) -> float:
    return float(hass.states.get(_eid(hass, entry, "_primary_target")).state)


async def test_option_off_overwrites(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass, accept=False)
    await _wall(hass, head_a, "set_temperature", target_temp_low=64, target_temp_high=66)
    await _elapse(hass, ADOPT_DELAY + 1)
    await _recompute(hass, entry)
    assert _target(hass, entry) == 70
    assert hass.states.get(head_a).attributes["target_temp_high"] == 70


async def test_setpoint_adopted(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass)
    await _wall(hass, head_a, "set_temperature", target_temp_low=64, target_temp_high=66)
    await _elapse(hass, ADOPT_DELAY + 1)
    assert _target(hass, entry) == 66
    await _recompute(hass, entry)
    a = hass.states.get(head_a)
    assert a.state == "cool"
    assert (a.attributes["target_temp_low"], a.attributes["target_temp_high"]) == (64, 66)


async def test_own_write_echo_is_not_adopted(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass)
    await _set_target(hass, _eid(hass, entry, "_primary_target"), 72)
    await _recompute(hass, entry)
    await _elapse(hass, ADOPT_DELAY + 1)
    assert _target(hass, entry) == 72
    assert hass.states.get(head_a).attributes["target_temp_high"] == 72


async def test_pending_change_is_not_overwritten_before_adoption(
    hass: HomeAssistant,
) -> None:
    head_a, entry = await _setup(hass)
    await _wall(hass, head_a, "set_temperature", target_temp_low=64, target_temp_high=66)
    await _recompute(hass, entry)  # a refresh lands inside the debounce window
    assert hass.states.get(head_a).attributes["target_temp_high"] == 66
    await _elapse(hass, ADOPT_DELAY + 1)
    assert _target(hass, entry) == 66


async def test_heat_cool_choice_is_not_adopted(hass: HomeAssistant) -> None:
    """Head flipped to heat with a new low edge: target follows, mode does not."""
    head_a, entry = await _setup(hass)
    await _wall(
        hass, head_a, "set_temperature",
        hvac_mode="heat", target_temp_low=72, target_temp_high=74,
    )
    await _elapse(hass, ADOPT_DELAY + 1)
    await _recompute(hass, entry)
    assert _target(hass, entry) == 72  # heat reads the low edge
    assert hass.states.get(head_a).state == "cool"  # room is 75 > 72: plan says cool


async def test_head_off_disables_room(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass)
    await _wall(hass, head_a, "set_hvac_mode", hvac_mode="off")
    await _elapse(hass, ADOPT_DELAY + 1)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "off"
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"


async def test_head_on_enables_room(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": _eid(hass, entry, "_primary_enable")}, blocking=True,
    )
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "off"
    await _wall(
        hass, head_a, "set_temperature",
        hvac_mode="cool", target_temp_low=62, target_temp_high=64,
    )
    await _elapse(hass, ADOPT_DELAY + 1)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "on"
    assert _target(hass, entry) == 64


async def test_plan_parked_off_is_not_a_request(hass: HomeAssistant) -> None:
    """Satisfied room parked by the plan (fan_only) is never read as a wall change."""
    head_a, entry = await _setup(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "fan_only"
    await _elapse(hass, ADOPT_DELAY + 1)
    assert hass.states.get(_eid(hass, entry, "_primary_enable")).state == "on"
    assert _target(hass, entry) == 70


async def test_eco_hold_is_not_adopted(hass: HomeAssistant) -> None:
    head_a, entry = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_eco_idle")}, blocking=True
    )
    await _recompute(hass, entry)
    await _wall(hass, head_a, "set_temperature", hvac_mode="cool",
                target_temp_low=60, target_temp_high=62)
    await _elapse(hass, ADOPT_DELAY + 1)
    assert _target(hass, entry) == 70
