"""Config flow for Fermob integration — BLE discovery."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

_LOGGER = logging.getLogger(__name__)

DOMAIN = "fermob"

# UUID present in BLE advertisements (incomplete 128-bit UUID, type 0x06).
# Distinct from the GATT service UUID (41c15000) used after connection.
FERMOB_ADV_UUID = "41c13060-6def-11e5-bcde-0002a5d5c51b"


def _is_fermob_device(info: BluetoothServiceInfoBleak) -> bool:
    """Return True if the BLE advertisement is from a Fermob lamp."""
    return FERMOB_ADV_UUID in [uuid.lower() for uuid in info.service_uuids]


class FermobConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fermob BLE lamps.

    Supports both passive discovery (HA detects the lamp automatically via
    bluetooth: manifest entry) and manual addition via the UI.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Passive discovery — HA calls this when a matching advertisement is seen
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Fermob lamp discovered via BLE advertisement."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered lamp."""
        info = self._discovered_info
        assert info is not None

        if user_input is not None:
            return self._create_entry(info)

        self._set_confirm_only()
        placeholders = {
            "name": info.name or info.address,
            "address": info.address,
        }
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------
    # Manual addition — user opens Integrations > Add > Fermob
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan for nearby Fermob lamps when user adds the integration manually."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            info = self._discovered_devices.get(address)
            if info:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self._create_entry(info)

        # Collect all Fermob devices seen by the HA BLE scanner (passive cache).
        # Both connectable=True and connectable=False cover all advertisement modes.
        current_addresses = self._async_current_ids()
        seen: dict[str, BluetoothServiceInfoBleak] = {}
        for connectable in (True, False):
            for info in async_discovered_service_info(self.hass, connectable=connectable):
                if _is_fermob_device(info) and info.address not in current_addresses:
                    seen[info.address] = info
        self._discovered_devices = seen

        if not self._discovered_devices:
            # No lamp in the BLE cache yet.
            # Ask the user to power-cycle the lamp to make it advertise,
            # then try again.
            return self.async_abort(reason="no_devices_found")

        # Single lamp → confirm directly
        if len(self._discovered_devices) == 1:
            info = next(iter(self._discovered_devices.values()))
            await self.async_set_unique_id(info.address)
            self._abort_if_unique_id_configured()
            self._discovered_info = info
            return await self.async_step_bluetooth_confirm()

        # Multiple lamps → let the user pick
        import voluptuous as vol
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): vol.In({
                    addr: f"{info.name or addr} ({addr})"
                    for addr, info in self._discovered_devices.items()
                })
            }),
        )

    # ------------------------------------------------------------------

    def _create_entry(self, info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Create the config entry for a confirmed lamp."""
        name = info.name or info.address
        return self.async_create_entry(
            title=name,
            data={
                CONF_ADDRESS: info.address,
                "name": name,
            },
        )