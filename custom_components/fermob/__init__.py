"""Fermob BLE light integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

DOMAIN = "fermob"
PLATFORMS = [Platform.LIGHT, Platform.SENSOR, Platform.BINARY_SENSOR]

_STORAGE_VERSION = 1


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The BLE connection is created here rather than inside the light platform
    # because three platforms now share one lamp, and platform setups run
    # concurrently -- whichever ran first would otherwise own the connection and
    # the others would race it.
    from .light import FermobBLEConnection, resolve_light_type

    address = entry.data[CONF_ADDRESS]
    store = Store(hass, _STORAGE_VERSION, f"fermob_{address.replace(':', '_').lower()}")
    conn = FermobBLEConnection(
        hass, address, store, light_type=resolve_light_type(entry)
    )

    def _remember_module_info(module_type: int | None, model: str | None) -> None:
        """Persist what the lamp reported into the config entry.

        Runs while the connection lock is held, so it must not await a reload:
        async_update_entry only *schedules* the update listener, and the reload
        that listener triggers then waits on the lock we are inside. Writing
        entry.data is also what makes this self-limiting -- once stored,
        resolve_light_type agrees with the lamp and nothing changes again.
        """
        updates = {
            k: v
            for k, v in (("module_type", module_type), ("model", model))
            if v is not None and entry.data.get(k) != v
        }
        if not updates:
            return
        hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})

    conn.on_module_info = _remember_module_info
    entry.async_on_unload(conn.async_shutdown)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = conn

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload the entry when the user changes the lamp-type option.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
