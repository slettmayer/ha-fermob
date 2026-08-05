"""Shared base for the lamp's non-light entities.

The battery sensors are push-only: the lamp reports a level in reply to a
request we send on connect, and unprompted whenever its charger changes, with
nothing to poll in between. Both entities subscribe to the connection's battery
pushes and write their state when one arrives.
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
        """Subscribe to the lamp's battery pushes.

        Registered through async_on_remove, so the subscription is released when
        the entity is. That is what makes it safe for the sensor and the binary
        sensor to both want every push: either can be added or removed without
        disturbing the other.

        This used to chain onto a single assignable `on_battery` slot, with no
        unsubscribe anywhere, which meant you had to know the platform setup
        order to know who was subscribed. No failure was ever demonstrated from
        it; the list is simply the HA idiom and easier to reason about.
        """
        self.async_on_remove(self._conn.add_battery_listener(self._handle_battery))

    def _handle_battery(self, battery: Battery) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable until the lamp has actually reported a level.

        A lamp that never answers must read as unknown, not as 0 % -- the app
        makes the same distinction, defaulting to -1 and rendering `--%`.
        """
        return self._conn.battery is not None
