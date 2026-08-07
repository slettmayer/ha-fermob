"""Firmware-version entity for a Fermob lamp.

Reports what the lamp is running and whether the vendor has published something
newer. It deliberately does **not** offer `UpdateEntityFeature.INSTALL`: the
lamps take a signed Nordic Secure DFU image and nothing here can write one, so
the only honest thing to do is say an update exists and point at the app that
can install it. The reasoning, the server, and what installing would cost are in
[docs/domain/FIRMWARE-UPDATE.md](../../docs/domain/FIRMWARE-UPDATE.md).

The installed version is local -- a TLV from the same `MODULE_INFO_GET` reply
that gives us the model. Only the *available* version needs the network, which
is why this platform is the one thing a user can switch off.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .config_flow import CONF_CHECK_FIRMWARE, DEFAULT_CHECK_FIRMWARE
from .firmware import async_get_latest_release
from .light import FermobBLEConnection
from .protocol import compare_versions

_LOGGER = logging.getLogger(__name__)

# Firmware for these lamps is published rarely -- the newest build on the server
# for any model is from 2023 -- so once a day is already generous, and the check
# is one small GET per lamp.
SCAN_INTERVAL = timedelta(days=1)

# What the lamp reports as its manufacturer, used to build the server path. Only
# a fallback: we prefer the string the lamp itself sent.
DEFAULT_MANUFACTURER = "Fermob"

RELEASE_SUMMARY = (
    "Home Assistant cannot install firmware on this lamp. Update it with the "
    "Fermob app, which needs the lamp released from Home Assistant first "
    "(`fermob.unpair`)."
)


def firmware_unique_id(address: str) -> str:
    """The entity's registry key. Shared, so removal cannot drift from setup."""
    return f"fermob_{address.replace(':', '_').lower()}_firmware"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the firmware entity, or disable it if the user switched the check off.

    **Off disables the registry entry rather than deleting it, and neither is the
    obvious "just don't add it".** Simply not providing an entity leaves Home
    Assistant rendering its registry row as unavailable, which reads as broken
    rather than switched off. Deleting the row instead -- which 0.10.0 briefly did
    -- throws away everything the *user* put there: a rename, an area, an icon,
    the hidden flag, and the recorder history keyed to that entity_id, silently
    breaking any dashboard card or automation that referenced it. Disabling says
    exactly what is true, keeps all of it, and reverses cleanly.

    A user-disabled entity is left alone: only `INTEGRATION` is ours to clear.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "update", DOMAIN, firmware_unique_id(entry.data[CONF_ADDRESS])
    )
    if not entry.options.get(CONF_CHECK_FIRMWARE, DEFAULT_CHECK_FIRMWARE):
        if entity_id and registry.entities[entity_id].disabled_by is None:
            _LOGGER.debug("Fermob: firmware check off, disabling %s", entity_id)
            registry.async_update_entity(
                entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        return

    # Back on after being off: undo our own disable, but never the user's.
    if entity_id and (
        registry.entities[entity_id].disabled_by is er.RegistryEntryDisabler.INTEGRATION
    ):
        registry.async_update_entity(entity_id, disabled_by=None)

    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FermobFirmwareUpdate(hass, entry, conn)])


class FermobFirmwareUpdate(UpdateEntity):
    """The lamp's firmware version, and whether the vendor has a newer one."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # No INSTALL, deliberately -- see the module docstring. Spelled out rather
    # than left to the base class's default so that removing it is a decision.
    _attr_supported_features = UpdateEntityFeature(0)
    _attr_has_entity_name = True
    # **No `_attr_name` here, deliberately, and it must stay absent.** HA reads the
    # name as `self._attr_name` whenever the attribute merely *exists*, and setting
    # it to None declares the entity to be the device's main feature -- which named
    # it after the lamp and gave it `update.<lamp>`, not the `update.<lamp>_firmware`
    # named "Firmware" the docs promise. Left unset, `UpdateEntity` falls through to
    # its device-class name. `test_it_is_named_after_its_device_class` pins that.
    _attr_should_poll = True
    _attr_release_summary = RELEASE_SUMMARY

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, conn: FermobBLEConnection
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._conn = conn
        self._latest: str | None = None
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = firmware_unique_id(address)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={("bluetooth", address)},
        )

    @property
    def installed_version(self) -> str | None:
        """What the lamp last said it was running.

        Read from the connection first so a fresh reading shows up without
        waiting for the entry reload that persists it, and from entry.data
        second so a restart has an answer before the first connect.
        """
        return self._conn.sw_version or self._entry.data.get("sw_version")

    @property
    def latest_version(self) -> str | None:
        """The newest published build, the installed one, or None for unknown.

        **`None` until a check has actually succeeded, and that is the important
        case.** Reporting the installed version is how an update entity says "up
        to date", so falling back to it when we have never had an answer would
        state as fact something never checked -- permanently, for a model the
        server does not carry (every Hoopik slug), and for any install behind a
        blocked DNS or a dead server. Unknown is the honest state, and Home
        Assistant renders it as one.

        Once there *is* an answer, reporting the installed version is right: it is
        what stops a lower published build reading as an update, and the
        comparison is only meaningful over three components -- the server serves
        `3.0.24` for one model where the lamp reports `3.0.24.0`. See
        `protocol.compare_versions`.
        """
        if self._latest is None:
            return None
        installed = self.installed_version
        if installed is None:
            return self._latest
        if compare_versions(self._latest, installed) > 0:
            return self._latest
        return installed

    async def async_added_to_hass(self) -> None:
        """Ask once now, rather than waiting a day for the first poll.

        HA starts the poll timer at `SCAN_INTERVAL` and does no initial update, so
        with a daily interval the entity would sit at *unknown* for 24 h after
        every restart -- and on a box that reloads or restarts more often than
        that, would never check at all. This also repairs the one real cost of
        keeping `_latest` in memory: a reload drops it, and the refresh puts it
        back within a second instead of a day.

        Scheduled rather than awaited so platform setup is not held up by an
        HTTP request to a third party.
        """
        await super().async_added_to_hass()
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update(self) -> None:
        """Ask the vendor server what the newest build for this model is.

        Needs the model the lamp reported, so it does nothing until the lamp has
        been connected to once. Failures are logged and dropped by
        `async_get_latest_release`, leaving the previous answer in place.
        """
        model = self._conn.model or self._entry.data.get("model")
        if not model:
            _LOGGER.debug(
                "Fermob %s: no reported model yet, skipping firmware check",
                self._entry.data[CONF_ADDRESS],
            )
            return
        manufacturer = (
            self._conn.manufacturer
            or self._entry.data.get("manufacturer")
            or DEFAULT_MANUFACTURER
        )
        release = await async_get_latest_release(
            async_get_clientsession(self.hass), manufacturer, model
        )
        if release is not None:
            self._latest = release.version
