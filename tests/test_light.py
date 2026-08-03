"""Tests for the parts of `light.py` that are not pure protocol.

Until now this file did not exist: `FermobBLEConnection` and `FermobLight` were
verified only by running them against a lamp. These tests cover the logic that
does *not* need a radio -- family resolution, what a reported module_type does to
the config entry, the entity's fixed capabilities, and the failure path that
marks the lamp unavailable.

Deliberately still uncovered, because faking it well is a bigger job than the
value it returns: the pairing handshake's 10-step sequence, long-frame
reassembly, and the real timing of the idle disconnect. `docs/tech/TESTING.md`
says so out loud rather than implying the suite is complete.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.light import ColorMode
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fermob.light import (
    DEFAULT_KELVIN,
    FermobBLEConnection,
    FermobLight,
    resolve_light_type,
)
from custom_components.fermob.protocol import (
    LIGHT_TYPE_DW,
    LIGHT_TYPE_TW,
    MAX_KELVIN,
    MIN_KELVIN,
    MODULE_TYPE_DW,
    MODULE_TYPE_TW,
    ModuleInfo,
)

ADDRESS = "D6:86:76:E8:7E:75"


def _entry(**data) -> SimpleNamespace:
    """A stand-in for ConfigEntry: `resolve_light_type` only reads two dicts."""
    options = data.pop("options", {})
    return SimpleNamespace(data={CONF_ADDRESS: ADDRESS, **data}, options=options)


# ---------------------------------------------------------------------------
# Family resolution: override > reported module_type > name heuristic
# ---------------------------------------------------------------------------


def test_explicit_option_beats_everything():
    """A user override must win even when the lamp contradicts it.

    This is the escape hatch for a lamp that reports something wrong, so
    module_type must not be allowed to override it.
    """
    entry = _entry(
        name="Hoopik", module_type=MODULE_TYPE_TW, options={"light_type": LIGHT_TYPE_DW}
    )
    assert resolve_light_type(entry) == LIGHT_TYPE_DW


def test_entry_data_override_also_wins():
    entry = _entry(light_type=LIGHT_TYPE_DW, module_type=MODULE_TYPE_TW)
    assert resolve_light_type(entry) == LIGHT_TYPE_DW


def test_reported_module_type_beats_the_name_heuristic():
    """The whole point: a renamed lamp is no longer misidentified.

    "hoopik-lookalike" would trip the name heuristic into dimmable white; the
    reported module_type says tunable, and that must win.
    """
    entry = _entry(name="hoopik-lookalike", module_type=MODULE_TYPE_TW)
    assert resolve_light_type(entry) == LIGHT_TYPE_TW


def test_reported_module_type_can_also_select_dimmable_white():
    entry = _entry(name="Balcony Mooon", module_type=MODULE_TYPE_DW)
    assert resolve_light_type(entry) == LIGHT_TYPE_DW


@pytest.mark.parametrize("bogus", [None, 0, 402, 37889, "404"])
def test_unrecognised_module_type_falls_through_to_the_name(bogus):
    """An unusable module_type must not shadow the heuristic."""
    assert resolve_light_type(_entry(name="Hoopik L1200", module_type=bogus)) == (
        LIGHT_TYPE_DW
    )
    assert resolve_light_type(_entry(name="Moon7E75", module_type=bogus)) == (
        LIGHT_TYPE_TW
    )


def test_first_run_with_no_module_type_uses_the_name():
    """Before the first connection there is nothing but the name."""
    assert resolve_light_type(_entry(name="Hoopik")) == LIGHT_TYPE_DW
    assert resolve_light_type(_entry(name="Moon7E75")) == LIGHT_TYPE_TW
    # No name at all: default to tunable white, which covers every model but one.
    assert resolve_light_type(_entry()) == LIGHT_TYPE_TW


# ---------------------------------------------------------------------------
# Storing what the lamp reported
# ---------------------------------------------------------------------------


def _conn(hass: HomeAssistant) -> FermobBLEConnection:
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    return FermobBLEConnection(hass, ADDRESS, store, light_type=LIGHT_TYPE_TW)


async def test_store_module_info_records_and_announces(hass: HomeAssistant):
    conn = _conn(hass)
    seen: list[tuple] = []
    conn.on_module_info = lambda mt, model: seen.append((mt, model))

    conn._store_module_info(ModuleInfo(0x75, 0x7E, 2, MODULE_TYPE_TW, "MOOON - H134"))

    assert conn.module_type == MODULE_TYPE_TW
    assert conn.model == "MOOON - H134"
    assert seen == [(MODULE_TYPE_TW, "MOOON - H134")]


async def test_store_module_info_is_quiet_when_nothing_changed(hass: HomeAssistant):
    """Repeat reports must not churn the config entry on every connect."""
    conn = _conn(hass)
    conn._store_module_info(ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134"))

    seen: list[tuple] = []
    conn.on_module_info = lambda mt, model: seen.append((mt, model))
    conn._store_module_info(ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134"))

    assert seen == []


async def test_store_module_info_keeps_known_values_when_absent(hass: HomeAssistant):
    """A response missing the fields must not wipe what we already knew."""
    conn = _conn(hass)
    conn._store_module_info(ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134"))
    conn._store_module_info(ModuleInfo(0, 0, 2, None, None))

    assert conn.module_type == MODULE_TYPE_TW
    assert conn.model == "MOOON - H134"


async def test_fetch_module_info_once_skips_when_already_known(hass: HomeAssistant):
    """The extra round trip is one per install, not one per connect."""
    conn = _conn(hass)
    conn.module_type = MODULE_TYPE_TW
    conn._send = AsyncMock()

    await conn._fetch_module_info_once()

    conn._send.assert_not_called()


async def test_fetch_module_info_once_reads_and_persists(hass: HomeAssistant):
    conn = _conn(hass)
    conn._have_keys = True
    payload = bytes([3, 0xB4, 0x94, 0x01, 17, 0xB3]) + b"MOOON - H134".ljust(
        16, b"\x00"
    )
    conn._send = AsyncMock(return_value=(payload, 0))

    await conn._fetch_module_info_once()

    assert conn.module_type == MODULE_TYPE_TW
    assert conn.model == "MOOON - H134"
    conn._store.async_save.assert_awaited_once()


async def test_fetch_module_info_once_swallows_transport_errors(hass: HomeAssistant):
    """A diagnostic read must never be able to break the light."""
    conn = _conn(hass)
    conn._send = AsyncMock(side_effect=RuntimeError("ACK timeout"))

    await conn._fetch_module_info_once()  # must not raise

    assert conn.module_type is None


async def test_fetch_module_info_once_tolerates_an_unanswered_command(
    hass: HomeAssistant,
):
    conn = _conn(hass)
    conn._send = AsyncMock(return_value=(None, 0))

    await conn._fetch_module_info_once()

    assert conn.module_type is None


# ---------------------------------------------------------------------------
# The entity
# ---------------------------------------------------------------------------


def _light(hass: HomeAssistant, light_type: str, **data) -> FermobLight:
    entry = MockConfigEntry(
        domain="fermob", data={CONF_ADDRESS: ADDRESS, "name": "Balcony Mooon", **data}
    )
    conn = _conn(hass)
    return FermobLight(hass, entry, conn, light_type)


async def test_tunable_white_exposes_only_color_temp(hass: HomeAssistant):
    """COLOR_TEMP already implies brightness in HA; advertising both is wrong."""
    light = _light(hass, LIGHT_TYPE_TW)

    assert light.supported_color_modes == {ColorMode.COLOR_TEMP}
    assert light.min_color_temp_kelvin == MIN_KELVIN == 3000
    assert light.max_color_temp_kelvin == MAX_KELVIN == 6000
    assert light.color_temp_kelvin == DEFAULT_KELVIN


async def test_dimmable_white_exposes_only_brightness(hass: HomeAssistant):
    light = _light(hass, LIGHT_TYPE_DW)

    assert light.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light.color_mode == ColorMode.BRIGHTNESS


async def test_device_info_prefers_the_reported_model(hass: HomeAssistant):
    light = _light(hass, LIGHT_TYPE_TW, model="MOOON - H134")

    assert light.device_info["model"] == "MOOON - H134"
    assert light.device_info["manufacturer"] == "Fermob"
    assert light.device_info["identifiers"] == {("fermob", ADDRESS)}


async def test_device_info_falls_back_to_the_family_label(hass: HomeAssistant):
    """Before the lamp has told us, the family guess is all we can show."""
    assert _light(hass, LIGHT_TYPE_TW).device_info["model"] == "MOOON (tunable white)"
    assert (
        _light(hass, LIGHT_TYPE_DW).device_info["model"]
        == "Hoopik GL1200 (dimmable white)"
    )


async def test_unique_id_is_derived_from_the_address(hass: HomeAssistant):
    assert _light(hass, LIGHT_TYPE_TW).unique_id == "fermob_d6_86_76_e8_7e_75"


# ---------------------------------------------------------------------------
# The single command path
# ---------------------------------------------------------------------------


async def test_failed_command_marks_the_lamp_unavailable(hass: HomeAssistant):
    """A failure must not leave HA asserting a state the lamp does not have."""
    light = _light(hass, LIGHT_TYPE_TW)
    light._attr_available = True
    light.async_write_ha_state = MagicMock()
    light._conn.ensure_connected = AsyncMock(side_effect=RuntimeError("out of range"))
    light._conn.disconnect = AsyncMock()

    ok = await light._async_send_led("turn_on", True, 50, 0.5)

    assert ok is False
    assert light.available is False
    light._conn.disconnect.assert_awaited_once()
    light.async_write_ha_state.assert_called_once()


async def test_failed_turn_on_does_not_record_the_requested_state(
    hass: HomeAssistant,
):
    """Attributes are updated only after a confirmed send."""
    light = _light(hass, LIGHT_TYPE_TW)
    light._attr_is_on = False
    light._attr_brightness = 10
    light.async_write_ha_state = MagicMock()
    light._conn.ensure_connected = AsyncMock(side_effect=RuntimeError("no route"))
    light._conn.disconnect = AsyncMock()

    await light.async_turn_on(brightness=255, color_temp_kelvin=6000)

    assert light.is_on is False
    assert light.brightness == 10
    assert light.color_temp_kelvin == DEFAULT_KELVIN


async def test_successful_command_restores_availability(hass: HomeAssistant):
    light = _light(hass, LIGHT_TYPE_TW)
    light._attr_available = False
    light.async_write_ha_state = MagicMock()
    light._conn.ensure_connected = AsyncMock()
    light._conn.send_led = AsyncMock()

    ok = await light._async_send_led("turn_on", True, 50, 0.5)

    assert ok is True
    assert light.available is True


async def test_turn_on_maps_ha_brightness_to_percent(hass: HomeAssistant):
    """HA's 0-255 becomes the protocol's 0-100, and 6000 K is all-cold."""
    light = _light(hass, LIGHT_TYPE_TW)
    light.async_write_ha_state = MagicMock()
    light._conn.ensure_connected = AsyncMock()
    light._conn.send_led = AsyncMock()

    await light.async_turn_on(brightness=255, color_temp_kelvin=MAX_KELVIN)

    on, pct, warm_ratio = light._conn.send_led.await_args.args
    assert on is True
    assert pct == 100
    assert warm_ratio == 0.0
    assert light.is_on is True


async def test_turn_on_never_sends_zero_percent(hass: HomeAssistant):
    """Brightness 1/255 must stay on, not become an implicit off."""
    light = _light(hass, LIGHT_TYPE_TW)
    light.async_write_ha_state = MagicMock()
    light._conn.ensure_connected = AsyncMock()
    light._conn.send_led = AsyncMock()

    await light.async_turn_on(brightness=1)

    assert light._conn.send_led.await_args.args[1] >= 1


async def test_turn_off_preserves_color_temperature(hass: HomeAssistant):
    """So the lamp keeps its warm/cold balance when switched on at the button."""
    light = _light(hass, LIGHT_TYPE_TW)
    light.async_write_ha_state = MagicMock()
    light._attr_color_temp_kelvin = MIN_KELVIN
    light._conn.ensure_connected = AsyncMock()
    light._conn.send_led = AsyncMock()

    await light.async_turn_off()

    on, _pct, warm_ratio = light._conn.send_led.await_args.args
    assert on is False
    assert warm_ratio == 1.0  # 3000 K == all warm
    assert light.color_temp_kelvin == MIN_KELVIN


async def test_commands_are_serialised_by_the_connection_lock(hass: HomeAssistant):
    """Two overlapping commands must not interleave on one BLE link."""
    light = _light(hass, LIGHT_TYPE_TW)
    light.async_write_ha_state = MagicMock()
    order: list[str] = []

    async def slow_connect() -> None:
        order.append("enter")
        await asyncio.sleep(0)
        order.append("leave")

    light._conn.ensure_connected = slow_connect
    light._conn.send_led = AsyncMock()

    await asyncio.gather(
        light._async_send_led("a", True, 10, 0.5),
        light._async_send_led("b", True, 20, 0.5),
    )

    assert order == ["enter", "leave", "enter", "leave"]


# ---------------------------------------------------------------------------
# Inbound state
# ---------------------------------------------------------------------------


async def test_event_sets_state_and_restores_availability(hass: HomeAssistant):
    """An inbound EVENT is the other way HA learns the lamp is alive."""
    light = _light(hass, LIGHT_TYPE_TW)
    # EVENTs arrive on the bleak notification callback, so this path uses the
    # thread-safe scheduler rather than async_write_ha_state.
    light.schedule_update_ha_state = MagicMock()
    light._attr_available = False

    light.on_lamp_state_change(True, 33, 67)

    assert light.is_on is True
    assert light.available is True
    # cold=33 warm=67 is 100 % output at the warm end of the middle.
    assert light.brightness == pytest.approx(255, abs=3)
    assert MIN_KELVIN <= light.color_temp_kelvin <= MAX_KELVIN


async def test_event_reporting_off_clears_is_on(hass: HomeAssistant):
    light = _light(hass, LIGHT_TYPE_TW)
    light.schedule_update_ha_state = MagicMock()
    light._attr_is_on = True

    light.on_lamp_state_change(False, 0, 0)

    assert light.is_on is False
