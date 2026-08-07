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

import asyncio
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, address_slug
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
    """This entity's registry key, from the one shared address slug."""
    return f"{address_slug(address)}_firmware"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the firmware entity, checking the server only if the option is on.

    **The option governs the request, not the entity's existence, and three
    attempts at the alternative are why.** Not providing the entity leaves Home
    Assistant rendering its registry row as *unavailable* -- and, worse, leaves a
    stale `unavailable` state in the machine that only a restart clears, because
    the cleanup filter fires on removal and rename, not on anything else.
    Deleting the row throws away the user's rename, area, icon and hidden flag,
    orphans its history, and silently breaks references to the entity_id. And
    disabling the row makes core's own `EntityRegistryDisabledHandler` schedule a
    *second* config-entry reload 30 s later when the flag is cleared -- which
    tears down the BLE link for an unrelated options change.

    So the entity always exists, and with the check off it simply never asks:
    `should_poll` is False and nothing is scheduled, so no request leaves the
    network. `latest_version` then stays None and the entity reads *unknown*,
    which is exactly true -- we do not know, and were told not to find out. A
    user who wants it out of sight can disable it in the UI, which Home Assistant
    already honours by never adding it, and which is the one writer of that fact.
    """
    conn = hass.data[DOMAIN][entry.entry_id]
    checking = entry.options.get(CONF_CHECK_FIRMWARE, DEFAULT_CHECK_FIRMWARE)
    async_add_entities([FermobFirmwareUpdate(hass, entry, conn, checking)])


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
    _attr_release_summary = RELEASE_SUMMARY

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        conn: FermobBLEConnection,
        checking: bool,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._conn = conn
        self._checking = checking
        self._latest: str | None = None
        self._first_check: asyncio.Task | None = None
        # Per instance, not per class: with the check off the entity still exists
        # and still reports the installed version, but nothing may reach the
        # network on its behalf -- so it must not be polled at all.
        self._attr_should_poll = checking
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

        **A background task, not `async_schedule_update_ha_state` and not
        `update_before_add`.** Both of those are awaited before Home Assistant
        finishes starting -- the first because `async_create_task` registers in
        `hass._tasks`, which bootstrap's `async_block_till_done()` drains -- so a
        box that cannot reach the vendor server would have its startup held up by
        the whole request budget, and be told a firmware check was "taking over
        10 seconds". A background task is excluded from that drain and cancelled
        at shutdown, which is exactly the lifetime this deserves: nothing waits
        on the answer.
        """
        await super().async_added_to_hass()
        if not self._checking:
            return
        self._first_check = self.hass.async_create_background_task(
            self._async_first_check(),
            name=f"fermob firmware check {self._entry.data[CONF_ADDRESS]}",
        )
        self.async_on_remove(self._async_cancel_first_check)

    def _async_cancel_first_check(self) -> None:
        """Drop the startup check if the entity goes away mid-request."""
        if self._first_check is not None and not self._first_check.done():
            self._first_check.cancel()

    async def _async_first_check(self) -> None:
        """The one-shot startup check, written to never take the entity down."""
        try:
            await self.async_update()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - async_update swallows its own
            _LOGGER.debug("Fermob: startup firmware check failed", exc_info=True)
            return
        self.async_write_ha_state()

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
