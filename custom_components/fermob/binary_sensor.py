"""Charging state for a Fermob lamp."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .entity import FermobBatteryEntityBase


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FermobChargingSensor(entry, conn)])


class FermobChargingSensor(FermobBatteryEntityBase, BinarySensorEntity):
    """Whether the lamp is on its charger.

    Bit 7 of the battery byte, confirmed on an H134 on 2026-08-03: clear while
    discharging, and observed setting the moment the lamp went on its charger.

    Note for anyone reading the level alongside this: the reported percentage
    jumps up as soon as charging starts (24 % to 33 % in the confirming test).
    That is faster than real capacity can accumulate, so the lamp is very likely
    deriving charge from terminal voltage, which a charger raises immediately.
    The level is optimistic whenever this sensor is on; the trustworthy figure is
    a resting one taken after the charger comes off.
    """

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "charging"

    def __init__(self, entry, conn) -> None:
        super().__init__(entry, conn)
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"fermob_{address.replace(':', '_').lower()}_charging"

    @property
    def is_on(self) -> bool | None:
        battery = self._conn.battery
        return None if battery is None else battery.charging
