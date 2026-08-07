"""Config flow for Fermob — BLE discovery, lamp type, connection, firmware check."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "fermob"

# UUID present in BLE advertisements, carried in an incomplete list of 128-bit
# service UUIDs (AD type 0x06). Distinct from the GATT service UUID (41c15000)
# used after connection.
FERMOB_ADV_UUID = "41c13060-6def-11e5-bcde-0002a5d5c51b"

# Lamp-type override values (must match protocol.LIGHT_TYPE_* / "auto").
CONF_LIGHT_TYPE = "light_type"
LIGHT_TYPE_AUTO = "auto"
LIGHT_TYPE_DW = "dw"
LIGHT_TYPE_TW = "tw"

# How the BLE link is managed. `__init__.py` turns this into an idle timeout and
# a check-in interval; the two are set together on purpose, because they
# interact -- a check-in re-arms the idle timer, so a check-in shorter than the
# timeout holds the link open no matter what the timeout says.
CONF_CONNECTION_MODE = "connection_mode"
CONNECTION_MODE_ALWAYS = "always"
CONNECTION_MODE_ON_DEMAND = "on_demand"

# Whether to ask the vendor's release server for the newest firmware build. On
# by default: a lamp running old firmware is worth knowing about, and the check
# is one small GET per lamp per day. It is the integration's only non-local
# traffic, which is why it is switchable at all -- off removes the entity.
CONF_CHECK_FIRMWARE = "check_firmware_updates"
DEFAULT_CHECK_FIRMWARE = True


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FermobOptionsFlow:
        """Return the options flow (lamp type, connection, firmware check)."""
        return FermobOptionsFlow(config_entry)

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
            for info in async_discovered_service_info(
                self.hass, connectable=connectable
            ):
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
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{info.name or addr} ({addr})"
                            for addr, info in self._discovered_devices.items()
                        }
                    )
                }
            ),
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


class FermobOptionsFlow(OptionsFlow):
    """Lamp type, and how the BLE link is managed.

    **Lamp type.** The Linkio advertisement is a rotating/encrypted payload, so
    the model cannot be read from it. light.py auto-detects by name (only the
    Hoopik string light is dimmable-white; everything else is tunable-white).
    This is the manual escape hatch for any lamp the heuristic gets wrong.

    **Connection mode.** The lamp reports a physical button press only while the
    link is held open, so holding it is what makes the light's state truthful --
    at the price of one connection slot on whichever adapter or BLE proxy the
    lamp reaches. Proxies are typically limited to three. On-demand hands that
    slot back between commands, and gives up press detection to do it.

    Deliberately a mode rather than two numbers: the idle timeout and the
    check-in interval are not independent (a check-in re-arms the idle timer),
    so exposing both invites settings whose combination does nothing.

    **Firmware check.** The one thing here that is not about the lamp in the
    room: whether to ask the vendor's release server, once a day, if a newer
    firmware build exists. It is the integration's only non-local traffic, which
    is the whole reason it is switchable -- off removes the entity along with the
    request. See docs/domain/FIRMWARE-UPDATE.md.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LIGHT_TYPE,
                        default=options.get(CONF_LIGHT_TYPE, LIGHT_TYPE_AUTO),
                    ): vol.In(
                        {
                            LIGHT_TYPE_AUTO: "Auto-detect (by name)",
                            LIGHT_TYPE_TW: "Tunable white (MOOON / table lamps)",
                            LIGHT_TYPE_DW: "Dimmable white (Hoopik L1200)",
                        }
                    ),
                    vol.Required(
                        CONF_CONNECTION_MODE,
                        default=options.get(
                            CONF_CONNECTION_MODE, CONNECTION_MODE_ALWAYS
                        ),
                    ): vol.In(
                        {
                            CONNECTION_MODE_ALWAYS: (
                                "Always connected — button presses show up"
                            ),
                            CONNECTION_MODE_ON_DEMAND: (
                                "On demand — frees a Bluetooth connection slot"
                            ),
                        }
                    ),
                    vol.Required(
                        CONF_CHECK_FIRMWARE,
                        default=options.get(
                            CONF_CHECK_FIRMWARE, DEFAULT_CHECK_FIRMWARE
                        ),
                    ): bool,
                }
            ),
        )
