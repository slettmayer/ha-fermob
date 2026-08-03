"""Shared base for the lamp's non-light entities.

The battery sensors are push-only: the lamp reports a level in reply to a
request we send on connect, and there is nothing to poll in between. Both
entities therefore subscribe to the same connection callback and write their
state when it fires.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN
from .light import FermobBLEConnection
from .protocol import Battery


class FermobBatteryEntityBase(Entity):
    """Common wiring for entities fed by the lamp's battery pushes."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, conn: FermobBLEConnection) -> None:
        self._entry = entry
        self._conn = conn
        address = entry.data[CONF_ADDRESS]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={("bluetooth", address)},
        )

    async def async_added_to_hass(self) -> None:
        """Chain onto the connection's battery callback.

        Chained rather than assigned: the sensor and the binary sensor share one
        connection, so overwriting `on_battery` would silently disconnect
        whichever registered first.
        """
        previous = self._conn.on_battery

        def _forward(battery: Battery) -> None:
            if previous is not None:
                previous(battery)
            self._handle_battery(battery)

        self._conn.on_battery = _forward

    def _handle_battery(self, battery: Battery) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable until the lamp has actually reported a level.

        A lamp that never answers must read as unknown, not as 0 % -- the app
        makes the same distinction, defaulting to -1 and rendering `--%`.
        """
        return self._conn.battery is not None
