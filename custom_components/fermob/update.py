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
    """Add the firmware entity unless the user switched the check off.

    Switching it off removes the entity rather than freezing it, because the
    entity exists to answer "is there a newer build" and without the network
    there is no answer -- only the installed version, which is on the device
    page either way.

    **Not adding it is not enough to make it go away.** Home Assistant keeps a
    registry entry for an entity a platform stops providing and shows it as
    unavailable, so switching the option off would otherwise leave a permanently
    broken-looking row that only a manual delete clears. Removing the registry
    entry here is what makes "off" mean what the option says.
    """
    if not entry.options.get(CONF_CHECK_FIRMWARE, DEFAULT_CHECK_FIRMWARE):
        registry = er.async_get(hass)
        unique_id = firmware_unique_id(entry.data[CONF_ADDRESS])
        if entity_id := registry.async_get_entity_id("update", DOMAIN, unique_id):
            _LOGGER.debug("Fermob: firmware check off, removing %s", entity_id)
            registry.async_remove(entity_id)
        return
    conn = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FermobFirmwareUpdate(hass, entry, conn)])


class FermobFirmwareUpdate(UpdateEntity):
    """The lamp's firmware version, and whether the vendor has a newer one."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    # No INSTALL, deliberately -- see the module docstring. Spelled out rather
    # than left to the base class's default so that removing it is a decision.
    _attr_supported_features = UpdateEntityFeature(0)
    _attr_has_entity_name = True
    _attr_name = None  # the device class names it "Firmware"
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
        """The newest published build, or the installed one when there is none.

        Reporting the installed version is how an update entity says "up to
        date"; it is also what keeps a *lower* published build from reading as an
        update, which matters because the comparison is only meaningful over
        three components -- the server serves `3.0.24` for one model where the
        lamp reports `3.0.24.0`. See `protocol.compare_versions`.
        """
        installed = self.installed_version
        if self._latest is None or installed is None:
            return self._latest or installed
        if compare_versions(self._latest, installed) > 0:
            return self._latest
        return installed

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
