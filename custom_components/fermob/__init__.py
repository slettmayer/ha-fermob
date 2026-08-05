"""Fermob BLE light integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

DOMAIN = "fermob"
PLATFORMS = [Platform.LIGHT, Platform.SENSOR, Platform.BINARY_SENSOR]

_STORAGE_VERSION = 1

# How often to reach the lamp to reconnect if needed and read its battery.
#
# This started life as a six-hourly battery poll, which was the right number
# while the link was dropped after 30 s idle and the connection was expected to
# be down. Now that the link is held open the job has changed: nothing else
# notices an unexpected disconnect, so this is the only thing that brings the
# link back, and at six hours the entity could show confidently stale state for
# most of a day after a BLE proxy rebooted. Thirty minutes bounds that.
#
# Cheap by the manufacturer's own standard: over a live link this is one battery
# request, and the vendor app polls the same command roughly every 40 s whenever
# its screen is open.
CHECK_IN_INTERVAL = timedelta(minutes=30)

# Also check in once shortly after startup, for two reasons. The interval timer
# restarts from zero on every reload, so on a box that is restarted often the
# tick could otherwise be missed repeatedly; and both battery entities read as
# unavailable until the lamp has reported once, which would otherwise last until
# something turns the light on. The delay lets the Bluetooth stack come up first.
CHECK_IN_STARTUP_DELAY = timedelta(minutes=2)


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

    async def _check_in(_now: datetime) -> None:
        """Scheduled reconnect + battery read. Swallows its failures by contract."""
        await conn.async_check_in()

    # Both cancels are registered on the entry, so a reload or unload leaves no
    # timer firing against a connection that has already been shut down.
    entry.async_on_unload(async_track_time_interval(hass, _check_in, CHECK_IN_INTERVAL))
    entry.async_on_unload(async_call_later(hass, CHECK_IN_STARTUP_DELAY, _check_in))

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
