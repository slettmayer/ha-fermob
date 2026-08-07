"""Battery level sensor for a Fermob lamp."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, address_slug
from .entity import FermobBatteryEntityBase


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FermobBatterySensor(entry, conn)])


class FermobBatterySensor(FermobBatteryEntityBase, SensorEntity):
    """State of charge, as reported by the lamp itself."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "battery"

    def __init__(self, entry, conn) -> None:
        super().__init__(entry, conn)
        self._attr_unique_id = f"{address_slug(entry.data[CONF_ADDRESS])}_battery_level"

    @property
    def native_value(self) -> int | None:
        battery = self._conn.battery
        return None if battery is None else battery.percent
