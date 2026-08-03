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

    Bit 7 of the battery byte. Confirmed clear on a discharging lamp; that it
    sets while charging follows from the app's decode but has not been seen
    flip on hardware.
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
