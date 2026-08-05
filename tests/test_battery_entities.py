"""The two battery entities, and how they stay subscribed to the lamp.

Neither had any test coverage before, which was the real gap: the light is the
thing anyone would look at, so a diagnostic entity that quietly stopped updating
would not be noticed from the outside.

The properties that matter here are about lifetime, not about values. Both
entities want every battery push, they are added and removed independently of
each other and of the connection object, and their subscription must die exactly
when the entity does. The old single-slot callback could express none of that.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from custom_components.fermob.binary_sensor import FermobChargingSensor
from custom_components.fermob.light import FermobBLEConnection
from custom_components.fermob.protocol import LIGHT_TYPE_TW, Battery
from custom_components.fermob.sensor import FermobBatterySensor

ADDRESS = "D6:86:76:E8:7E:75"


def _conn(hass: HomeAssistant) -> FermobBLEConnection:
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    return FermobBLEConnection(hass, ADDRESS, store, light_type=LIGHT_TYPE_TW)


def _entry() -> SimpleNamespace:
    """The entities read only `data`, for the address and the unique_id."""
    return SimpleNamespace(data={CONF_ADDRESS: ADDRESS, "name": "Balcony Mooon"})


def _added(entity) -> list:
    """Stub out HA's plumbing and return the removers the entity registered."""
    removers: list = []
    entity.async_on_remove = removers.append
    entity.async_write_ha_state = MagicMock()
    return removers


def _battery_push(conn: FermobBLEConnection, percent: int, charging: bool) -> None:
    raw = (percent & 0x7F) | (0x80 if charging else 0)
    payload = bytes([2, 0xC0, raw]) + bytes(12)
    with patch("custom_components.fermob.light.decode_fragment", return_value=payload):
        conn._dispatch_event(bytes(20), 2)


async def test_the_subscription_is_tied_to_entity_removal(hass: HomeAssistant):
    """Registered via async_on_remove, so it cannot outlive the entity.

    This is the fix in one assertion: the connection holds the subscription only
    for as long as HA holds the entity.
    """
    conn = _conn(hass)
    entity = FermobBatterySensor(_entry(), conn)
    removers = _added(entity)

    await entity.async_added_to_hass()
    assert len(conn._battery_listeners) == 1
    assert len(removers) == 1

    removers[0]()
    assert conn._battery_listeners == []


async def test_both_entities_update_from_one_push(hass: HomeAssistant):
    """The level and the charging flag travel in the same byte, and one push.

    Asserts both entities wrote, not merely that the push was parsed -- a single
    shared callback slot could feed one and silently miss the other.
    """
    conn = _conn(hass)
    entry = _entry()
    sensor = FermobBatterySensor(entry, conn)
    charging = FermobChargingSensor(entry, conn)
    _added(sensor)
    _added(charging)
    await sensor.async_added_to_hass()
    await charging.async_added_to_hass()

    _battery_push(conn, 84, charging=False)

    sensor.async_write_ha_state.assert_called_once()
    charging.async_write_ha_state.assert_called_once()
    assert sensor.native_value == 84
    assert charging.is_on is False


async def test_removing_one_entity_leaves_the_other_subscribed(hass: HomeAssistant):
    """A reload removes entities one at a time; the survivor must keep updating."""
    conn = _conn(hass)
    entry = _entry()
    sensor = FermobBatterySensor(entry, conn)
    charging = FermobChargingSensor(entry, conn)
    sensor_removers = _added(sensor)
    _added(charging)
    await sensor.async_added_to_hass()
    await charging.async_added_to_hass()

    sensor_removers[0]()
    _battery_push(conn, 84, charging=True)

    sensor.async_write_ha_state.assert_not_called()
    charging.async_write_ha_state.assert_called_once()


async def test_charging_flag_reaches_the_binary_sensor(hass: HomeAssistant):
    """Bit 7 of the same byte -- the push the lamp sends when docked."""
    conn = _conn(hass)
    charging = FermobChargingSensor(_entry(), conn)
    _added(charging)
    await charging.async_added_to_hass()

    _battery_push(conn, 98, charging=True)

    assert charging.is_on is True
    assert conn.battery == Battery(percent=98, charging=True)


async def test_both_read_unavailable_until_the_lamp_reports(hass: HomeAssistant):
    """A lamp that never answers must not read as a flat battery."""
    conn = _conn(hass)
    entry = _entry()
    sensor = FermobBatterySensor(entry, conn)
    charging = FermobChargingSensor(entry, conn)

    assert sensor.available is False
    assert charging.available is False
    assert sensor.native_value is None
    assert charging.is_on is None

    _battery_push(conn, 0, charging=False)

    # A reported 0 is a real 0, and must not be confused with "never reported".
    assert sensor.available is True
    assert sensor.native_value == 0
