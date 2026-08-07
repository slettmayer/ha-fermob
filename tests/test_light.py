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
from unittest.mock import AsyncMock, MagicMock, patch

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
    LMP_EVENT_DEVICE_DATA,
    LMP_STATUS_DEVICE_DATA,
    MAX_KELVIN,
    MIN_KELVIN,
    MODULE_TYPE_DW,
    MODULE_TYPE_TW,
    Battery,
    ModuleInfo,
)

ADDRESS = "D6:86:76:E8:7E:75"

# A stored key set, so `_load_keys` reports the lamp as paired. Only the three
# hex fields are load-bearing -- the check-in refuses to run without them.
_KEYS = {
    "pub": "00" * 16,
    "priv": "11" * 16,
    "nonce": "22" * 16,
    "addr_b2": 0x75,
    "addr_b3": 0x7E,
}


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


def _conn(
    hass: HomeAssistant, idle_disconnect_delay: float | None = None
) -> FermobBLEConnection:
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    return FermobBLEConnection(
        hass,
        ADDRESS,
        store,
        light_type=LIGHT_TYPE_TW,
        idle_disconnect_delay=idle_disconnect_delay,
    )


async def test_store_module_info_records_and_announces(hass: HomeAssistant):
    conn = _conn(hass)
    seen: list[dict] = []
    conn.on_module_info = seen.append

    conn._store_module_info(
        ModuleInfo(
            0x75,
            0x7E,
            2,
            MODULE_TYPE_TW,
            "MOOON - H134",
            "Fermob",
            "2.3.21.0",
            "1.0.0",
        )
    )

    assert conn.module_type == MODULE_TYPE_TW
    assert conn.model == "MOOON - H134"
    assert conn.manufacturer == "Fermob"
    assert conn.sw_version == "2.3.21.0"
    assert conn.hw_version == "1.0.0"
    assert seen == [
        {
            "module_type": MODULE_TYPE_TW,
            "model": "MOOON - H134",
            "manufacturer": "Fermob",
            "sw_version": "2.3.21.0",
            "hw_version": "1.0.0",
        }
    ]


async def test_store_module_info_announces_only_what_changed(hass: HomeAssistant):
    """The entry update is a reload, so it must carry no field that is unchanged."""
    conn = _conn(hass)
    conn._store_module_info(ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134"))

    seen: list[dict] = []
    conn.on_module_info = seen.append
    conn._store_module_info(
        ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134", sw_version="2.3.21.0")
    )

    assert seen == [{"sw_version": "2.3.21.0"}]


async def test_store_module_info_is_quiet_when_nothing_changed(hass: HomeAssistant):
    """Repeat reports must not churn the config entry on every connect."""
    conn = _conn(hass)
    conn._store_module_info(ModuleInfo(0, 0, 2, MODULE_TYPE_TW, "MOOON - H134"))

    seen: list[dict] = []
    conn.on_module_info = seen.append
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
    conn.sw_version = "2.3.21.0"
    conn._addr_b2, conn._addr_b3 = 0x75, 0x7E
    conn._send = AsyncMock()

    await conn._fetch_module_info_once()

    conn._send.assert_not_called()


async def test_fetch_module_info_once_rereads_for_a_missing_firmware_version(
    hass: HomeAssistant,
):
    """An install paired before we read 0xb5 must ask once more.

    Its stored record has the module type and the address, so the old guard
    returned early and the firmware version would never have been learned.
    """
    conn = _conn(hass)
    conn.module_type = MODULE_TYPE_TW
    conn._addr_b2, conn._addr_b3 = 0x75, 0x7E
    conn._send = AsyncMock(return_value=(b"", 2))

    await conn._fetch_module_info_once()

    conn._send.assert_called_once()


async def test_fetch_module_info_once_retries_while_the_address_is_zero(
    hass: HomeAssistant,
):
    """ "Once" means once it has given us both things it carries.

    Only the handshake's step 7 ever set the short address, so a pairing whose
    MODULE_INFO_GET went unanswered left it at 0 permanently -- and every
    addressed frame after that, the battery request included, goes to the wrong
    place. That used to cost an unavailable battery sensor; now that the battery
    ACK is the liveness signal it would cost the whole light.
    """
    conn = _conn(hass)
    conn.module_type = MODULE_TYPE_TW  # family known, address never learned
    # [len, type, *value]; 0xb1 is the short address, 0xb4 the module type.
    payload = bytes([3, 0xB1, 0x75, 0x7E, 3, 0xB4, 0x94, 0x01])
    conn._send = AsyncMock(return_value=(payload, 0))

    await conn._fetch_module_info_once()

    conn._send.assert_awaited_once()
    assert (conn._addr_b2, conn._addr_b3) == (0x75, 0x7E)


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


async def test_device_info_carries_both_reported_versions(hass: HomeAssistant):
    """Firmware and hardware version ride the same reply as the model."""
    light = _light(hass, LIGHT_TYPE_TW, sw_version="2.3.21.0", hw_version="1.0.0")

    assert light.device_info["sw_version"] == "2.3.21.0"
    assert light.device_info["hw_version"] == "1.0.0"


async def test_device_info_omits_versions_before_the_lamp_reports_them(
    hass: HomeAssistant,
):
    """Absent must stay absent -- the registry renders None as nothing at all."""
    light = _light(hass, LIGHT_TYPE_TW)

    assert light.device_info["sw_version"] is None
    assert light.device_info["hw_version"] is None


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

    async def slow_connect(**_kwargs: object) -> None:
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


# ---------------------------------------------------------------------------
# Push subscriptions
#
# These replaced single assignable callback slots, which required each new
# subscriber to chain onto whatever it found and offered no way to unchain. So
# these tests are less about the happy path than about the two properties the
# slots could not offer -- several independent subscribers, and a removal that
# actually removes.
# ---------------------------------------------------------------------------


def _battery_push(conn: FermobBLEConnection, percent: int = 42) -> None:
    """Drive one battery push through `_dispatch_event`, skipping the crypto."""
    payload = bytes([2, 0xC0, percent]) + bytes(12)
    with patch("custom_components.fermob.light.decode_fragment", return_value=payload):
        conn._dispatch_event(bytes(20), 2)


async def test_every_battery_subscriber_gets_the_push(hass: HomeAssistant):
    """The sensor and the binary sensor both want it; neither may shadow the other.

    With a single slot this only worked if the second registrant remembered to
    chain onto whatever it found there.
    """
    conn = _conn(hass)
    seen: list[str] = []
    conn.add_battery_listener(lambda b: seen.append(f"sensor:{b.percent}"))
    conn.add_battery_listener(lambda b: seen.append(f"binary:{b.percent}"))

    _battery_push(conn, 42)

    assert seen == ["sensor:42", "binary:42"]


async def test_removing_one_battery_subscriber_leaves_the_other(hass: HomeAssistant):
    """An entity being removed must not take its neighbour's updates with it."""
    conn = _conn(hass)
    seen: list[str] = []
    remove_first = conn.add_battery_listener(lambda b: seen.append("first"))
    conn.add_battery_listener(lambda b: seen.append("second"))

    remove_first()
    _battery_push(conn)

    assert seen == ["second"]


async def test_a_removed_subscriber_is_never_called_again(hass: HomeAssistant):
    """The property the old code had no way to express: a real unsubscribe.

    Without it, a subscription outlived the entity holding it -- which is how
    pushes ended up being delivered to entities that no longer existed while
    the live ones got nothing.
    """
    conn = _conn(hass)
    seen: list[Battery] = []
    remove = conn.add_battery_listener(seen.append)

    _battery_push(conn, 42)
    remove()
    _battery_push(conn, 7)

    assert [b.percent for b in seen] == [42]


async def test_removing_twice_is_harmless(hass: HomeAssistant):
    """HA can call a remover more than once; it must not raise."""
    conn = _conn(hass)
    remove = conn.add_battery_listener(lambda b: None)
    remove()
    remove()  # must not raise


async def test_one_broken_subscriber_does_not_silence_the_others(hass: HomeAssistant):
    """These run in the BLE notification callback, so an escape takes the push.

    A raising sensor must not cost the binary sensor its update.
    """
    conn = _conn(hass)
    seen: list[str] = []

    def _explode(battery: Battery) -> None:
        raise RuntimeError("entity not added yet")

    conn.add_battery_listener(_explode)
    conn.add_battery_listener(lambda b: seen.append("survived"))

    _battery_push(conn)

    assert seen == ["survived"]


async def test_state_subscribers_behave_the_same_way(hass: HomeAssistant):
    """The light uses the same mechanism, so it gets the same guarantees."""
    conn = _conn(hass)
    seen: list[tuple] = []
    remove = conn.add_state_listener(lambda *args: seen.append(args))
    pushed = _device_data(1_785_882_856, is_on=True, marker=LMP_EVENT_DEVICE_DATA)

    with patch("custom_components.fermob.light.decode_fragment", return_value=pushed):
        conn._dispatch_event(bytes(20), 2)
    remove()
    with patch("custom_components.fermob.light.decode_fragment", return_value=pushed):
        conn._dispatch_event(bytes(20), 2)

    assert seen == [(True, 40, 60)]


async def test_a_battery_push_with_no_subscribers_is_not_an_error(hass: HomeAssistant):
    """Pushes can arrive before the platforms have finished setting up."""
    conn = _conn(hass)
    _battery_push(conn)  # must not raise
    assert conn.battery == Battery(percent=42, charging=False)


# ---------------------------------------------------------------------------
# Believing (or not) what the lamp pushes
#
# The two DEVICE_DATA markers carry byte-identical bodies, so nothing but the
# marker separates a live push from a stored record -- and on an H134 the stored
# record reported the lamp off while it was lit.
# ---------------------------------------------------------------------------


def _device_data(
    timestamp: int,
    *,
    is_on: bool,
    ch1: int = 40,
    ch2: int = 60,
    marker: int = LMP_EVENT_DEVICE_DATA,
) -> bytes:
    pl = bytearray(15)
    pl[0] = 10
    pl[1] = marker
    pl[3:7] = timestamp.to_bytes(4, "little")
    pl[7] = 0  # status OK
    pl[8] = 0x11 if is_on else 0x10
    pl[9] = ch1
    pl[10] = ch2
    return bytes(pl)


def _dispatch(conn: FermobBLEConnection, payload: bytes) -> list[tuple]:
    """Push one decoded payload through `_dispatch_event`, skipping the crypto."""
    seen: list[tuple] = []
    conn.add_state_listener(lambda *args: seen.append(args))
    with patch("custom_components.fermob.light.decode_fragment", return_value=payload):
        conn._dispatch_event(bytes(20), 2)
    return seen


async def test_an_unsolicited_push_reaches_the_entity(hass: HomeAssistant):
    """Marker 146 is live state -- this is what a button press produces."""
    conn = _conn(hass)
    pushed = _device_data(1_785_882_856, is_on=True, marker=LMP_EVENT_DEVICE_DATA)
    assert _dispatch(conn, pushed) == [(True, 40, 60)]


async def test_a_solicited_record_is_dropped(hass: HomeAssistant):
    """Marker 147 is a stored record, and applying it drove the entity wrong.

    Nothing sends the query that produces one any more, so this guards against
    reintroducing it rather than against a live code path.
    """
    conn = _conn(hass)
    stale = _device_data(37, is_on=False, marker=LMP_STATUS_DEVICE_DATA)
    assert _dispatch(conn, stale) == []


async def test_the_captured_button_press_decodes(hass: HomeAssistant):
    """Byte-for-byte from a vendor-app capture: 2026-08-04 22:34:16, lamp on.

    Decrypted from the phone's own BLE traffic, so unlike most of this suite it
    is real evidence rather than a restatement of our own encoder.
    """
    conn = _conn(hass)
    real = bytes.fromhex("0a9200e868726a00110032")
    assert _dispatch(conn, real) == [(True, 0, 50)]  # on, cold 0, warm 50


# ---------------------------------------------------------------------------
# Holding the link open
# ---------------------------------------------------------------------------


async def test_no_idle_timer_is_armed_when_the_link_is_held(hass: HomeAssistant):
    """The default: the lamp only pushes while connected, so we stay connected."""
    conn = _conn(hass)
    assert conn._idle_disconnect_delay is None

    conn._schedule_idle_disconnect()

    assert conn._idle_task is None


async def test_an_idle_timeout_arms_a_timer_and_defers_it(hass: HomeAssistant):
    """On-demand mode still drops the link, and each command defers the drop."""
    conn = _conn(hass, idle_disconnect_delay=30.0)

    conn._schedule_idle_disconnect()
    first = conn._idle_task
    assert first is not None

    conn._schedule_idle_disconnect()

    assert first is not conn._idle_task
    assert first.cancelled() or first.done() or first.cancelling()
    conn._idle_task.cancel()


# ---------------------------------------------------------------------------
# Scheduled check-in
#
# It reconnects a dropped link and refreshes the battery *without* touching the
# light, so the tests assert what it does not do as much as what it does.
# ---------------------------------------------------------------------------


async def test_check_in_connects_and_lets_the_connect_path_read_battery(
    hass: HomeAssistant,
):
    """When disconnected, connecting is enough -- ensure_connected asks."""
    conn = _conn(hass)
    conn._store.async_load = AsyncMock(return_value=_KEYS)
    conn.ensure_connected = AsyncMock()
    conn.request_battery = AsyncMock()

    await conn.async_check_in()

    conn.ensure_connected.assert_awaited_once()
    # Not sent twice: ensure_connected already requests it on a fresh connect.
    conn.request_battery.assert_not_called()


async def test_check_in_asks_explicitly_when_already_connected(hass: HomeAssistant):
    """A lamp held connected all evening would otherwise never refresh.

    `ensure_connected` returns early when the link is up, so it never reaches
    its battery request -- the check-in has to send one itself.
    """
    conn = _conn(hass)
    conn._store.async_load = AsyncMock(return_value=_KEYS)
    conn._connected = True
    conn._client = MagicMock(is_connected=True)
    conn.ensure_connected = AsyncMock()
    conn.request_battery = AsyncMock()

    await conn.async_check_in()

    conn.request_battery.assert_awaited_once()


async def test_check_in_never_sends_a_light_command(hass: HomeAssistant):
    """The whole feature is worthless if it disturbs the lamp."""
    conn = _conn(hass)
    conn._store.async_load = AsyncMock(return_value=_KEYS)
    conn.ensure_connected = AsyncMock()
    conn.request_battery = AsyncMock()
    conn.send_led = AsyncMock()

    await conn.async_check_in()

    conn.send_led.assert_not_called()


async def test_check_in_survives_an_unreachable_lamp(hass: HomeAssistant):
    """Out of range is the normal case for a balcony lamp, not an error."""
    conn = _conn(hass)
    conn._store.async_load = AsyncMock(return_value=_KEYS)
    conn.battery = Battery(percent=42, charging=False)
    conn.ensure_connected = AsyncMock(side_effect=RuntimeError("device not found"))

    await conn.async_check_in()  # must not raise

    # The last known level survives -- the reading is "as of last contact".
    assert conn.battery == Battery(percent=42, charging=False)


async def test_check_in_does_not_pair_an_unpaired_lamp(hass: HomeAssistant):
    """Pairing makes the lamp flash; a 3 a.m. timer must never trigger it."""
    conn = _conn(hass)  # store.async_load returns None -> no keys
    conn.ensure_connected = AsyncMock()

    await conn.async_check_in()

    conn.ensure_connected.assert_not_called()


async def test_check_in_holds_the_command_lock(hass: HomeAssistant):
    """It must queue behind an in-flight command, not interleave frames."""
    conn = _conn(hass)
    conn._store.async_load = AsyncMock(return_value=_KEYS)
    conn.ensure_connected = AsyncMock()

    async with conn.lock:
        task = asyncio.ensure_future(conn.async_check_in())
        await asyncio.sleep(0)
        conn.ensure_connected.assert_not_called()  # blocked on the lock

    await task
    conn.ensure_connected.assert_awaited_once()
