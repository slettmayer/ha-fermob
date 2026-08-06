"""Fermob BLE light integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import NamedTuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, ENTITY_MATCH_ALL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)
from homeassistant.helpers.typing import ConfigType

from .config_flow import (
    CONF_CONNECTION_MODE,
    CONNECTION_MODE_ALWAYS,
    CONNECTION_MODE_ON_DEMAND,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "fermob"
PLATFORMS = [Platform.LIGHT, Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_CHECK_IN = "check_in"

# There is an `async_setup` below purely to register the domain service, and
# defining one is otherwise how an integration announces that it accepts YAML.
# This says it does not: config entries only.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_STORAGE_VERSION = 1


class ConnectionProfile(NamedTuple):
    """The two timings that follow from the user's connection mode.

    They are chosen together because they interact: a check-in re-arms the idle
    timer, so an interval shorter than the timeout holds the link open whatever
    the timeout says. Deriving both from one choice is what keeps that
    impossible to configure by accident.
    """

    idle_disconnect_delay: float | None
    check_in_interval: timedelta


# Always connected: the link is never dropped, so the lamp's unsolicited pushes
# arrive and a button press shows up in Home Assistant. The check-in is then the
# reconnect heartbeat -- nothing else notices a dropped link -- which is why it
# runs far more often than the battery alone would justify. Thirty minutes
# bounds how long the entity can show confidently stale state after, say, a BLE
# proxy reboots. Cheap by the manufacturer's own standard: over a live link it
# is one battery request, and the vendor app polls that same command roughly
# every 40 s whenever its screen is open.
#
# On demand: the pre-0.8.0 behaviour. The link is dropped 30 s after the last
# command, which hands the connection slot back to the adapter or proxy, and
# nothing is listening in between -- so a check-in cannot be a heartbeat, only a
# battery poll, and six hours gives a same-day figure with four chances to catch
# the lamp in range.
_CONNECTION_PROFILES = {
    CONNECTION_MODE_ALWAYS: ConnectionProfile(None, timedelta(minutes=30)),
    CONNECTION_MODE_ON_DEMAND: ConnectionProfile(30.0, timedelta(hours=6)),
}

# Also check in once shortly after startup, for two reasons. The interval timer
# restarts from zero on every reload, so on a box that is restarted often the
# tick could otherwise be missed repeatedly; and both battery entities read as
# unavailable until the lamp has reported once, which would otherwise last until
# something turns the light on. The delay lets the Bluetooth stack come up first.
#
# Thirty seconds, down from two minutes in 0.9.1. What the delay costs is not
# the ability to command the lamp -- `_async_send_led` calls `ensure_connected`
# itself and never waits on this timer -- but the time before the link is held
# open, during which a button press goes unseen and both battery entities read
# unavailable. Firing late costs a window of exactly the blindness the
# always-connected mode exists to remove.
#
# Firing *early* is only cheap because of the retries below. On its own the
# short delay would be a gamble on the Bluetooth stack: the check-in swallows
# its failures, so a proxy still coming up at 30 s would consume the single
# attempt in silence and leave the next one a full interval away.
CHECK_IN_STARTUP_DELAY = timedelta(seconds=30)

# When the startup check-in reaches nothing, try again -- at 30 s, then 1 min,
# then 2 min after that. Bounded on purpose: out of range is the *normal* state
# of a balcony lamp, not an error to retry at length, and the regular interval
# takes over either way. Only the gap before the stack is ready is worth
# covering, and it is short.
_STARTUP_RETRY_DELAYS = (
    CHECK_IN_STARTUP_DELAY,
    timedelta(minutes=1),
    timedelta(minutes=2),
)


def _key_store(hass: HomeAssistant, address: str) -> Store:
    """The store holding one lamp's pairing keys, keyed by its BLE address."""
    return Store(hass, _STORAGE_VERSION, f"fermob_{address.replace(':', '_').lower()}")


def resolve_connection_profile(entry: ConfigEntry) -> ConnectionProfile:
    """Map the connection-mode option onto its timings.

    Defaults to always-connected, including for an unrecognised stored value:
    it is the mode that makes the light report the truth, and the one cost --
    a connection slot -- is the thing the option exists to give back.
    """
    mode = entry.options.get(CONF_CONNECTION_MODE, CONNECTION_MODE_ALWAYS)
    return _CONNECTION_PROFILES.get(mode, _CONNECTION_PROFILES[CONNECTION_MODE_ALWAYS])


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain service once, before any entry is set up.

    Deliberately here rather than in `async_setup_entry`: the service must
    outlive individual entries, because every options change reloads one and a
    single-lamp install would otherwise lose `fermob.check_in` for the duration.
    """
    _async_register_check_in_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The BLE connection is created here rather than inside the light platform
    # because three platforms now share one lamp, and platform setups run
    # concurrently -- whichever ran first would otherwise own the connection and
    # the others would race it.
    from .light import FermobBLEConnection, resolve_light_type

    address = entry.data[CONF_ADDRESS]
    store = _key_store(hass, address)
    profile = resolve_connection_profile(entry)
    conn = FermobBLEConnection(
        hass,
        address,
        store,
        light_type=resolve_light_type(entry),
        idle_disconnect_delay=profile.idle_disconnect_delay,
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
    entry.async_on_unload(
        async_track_time_interval(hass, _check_in, profile.check_in_interval)
    )

    async def _startup_check_in(_now: datetime, attempt: int = 0) -> None:
        """The post-startup check-in, retried a few times if it finds nothing.

        The retry is what makes the short `CHECK_IN_STARTUP_DELAY` safe. As a
        single shot it was a gamble on the Bluetooth stack -- an adapter or an
        ESPHome proxy that had not finished coming up 30 s in meant the one
        attempt failed silently (the check-in swallows its failures) and the
        next was a whole interval away: half an hour of unseen button presses,
        or six hours of `unavailable` battery entities under *on demand*.

        `conn.battery` is the success signal, because it is the one thing a
        check-in is guaranteed to set when it reaches the lamp. Retries stop the
        moment it is set, and are bounded: a lamp that is simply out of range --
        the normal state of a balcony lamp -- must not be retried at length, and
        the regular interval takes over regardless.
        """
        await conn.async_check_in()
        if conn.battery is not None or attempt + 1 >= len(_STARTUP_RETRY_DELAYS):
            return
        delay = _STARTUP_RETRY_DELAYS[attempt + 1]
        entry.async_on_unload(
            async_call_later(
                hass,
                delay,
                partial(_startup_check_in, attempt=attempt + 1),
            )
        )

    entry.async_on_unload(
        async_call_later(hass, _STARTUP_RETRY_DELAYS[0], _startup_check_in)
    )

    # Also here, not only in `async_setup`: a custom integration reloaded from
    # the UI can reach entry setup without `async_setup` running again, and the
    # guard inside makes the second call free.
    _async_register_check_in_service(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload the entry when the user changes an option. The connection mode
    # takes effect that way too: the reload builds a fresh connection with the
    # new idle timeout and re-registers the check-in timer at the new interval.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _async_register_check_in_service(hass: HomeAssistant) -> None:
    """Register `fermob.check_in` on the domain, not on the light platform.

    **It has to be a domain service, and that is the whole point of it.** Home
    Assistant filters an entity service's targets by availability before the
    handler ever runs -- `async_extract_entities` in `homeassistant/helpers/
    service.py` drops the entity from the match set *before* testing
    `entity.available`, so it is not even logged as missing and the call returns
    success having done nothing. As an entity service, `fermob.check_in` was
    therefore unreachable exactly when someone would reach for it: on a lamp
    whose entity had gone unavailable after a failed command. Confirmed on
    hardware, 2026-08-06.

    On the domain that filter does not apply, so the service reaches the
    connection whatever the entity looks like.

    **Registered from `async_setup`, once, and never removed.** Tying it to entry
    setup looks tidier and is wrong: unloading the last entry -- which every
    options change does, via a reload -- would take the service with it, and a
    call landing in that window fails with `ServiceNotFound` and aborts the whole
    automation. That is strictly worse than the no-op it replaced. The service
    outlives entries and reports honestly when there are none.
    """
    if hass.services.has_service(DOMAIN, SERVICE_CHECK_IN):
        return

    async def _handle_check_in(call: ServiceCall) -> None:
        connections = _targeted_connections(hass, call)
        if not connections:
            _LOGGER.warning("fermob.check_in matched no configured lamp; nothing to do")
            return

        # Concurrently, and each failure isolated. The entity service this
        # replaced gathered its per-entity calls, so a serial loop would have
        # made an untargeted call on N unreachable lamps take N times the connect
        # budget -- minutes, with the caller blocked. `async_check_in` swallows
        # its own failures by contract, but that contract does not cover its lock
        # acquisition or `_load_keys()`, and one lamp with an unreadable key store
        # must not stop the others being checked in.
        results = await asyncio.gather(
            *(conn.async_check_in() for conn in connections),
            return_exceptions=True,
        )
        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "Fermob %s: check-in failed",
                    conn.address,
                    exc_info=result,
                )

    hass.services.async_register(DOMAIN, SERVICE_CHECK_IN, _handle_check_in)


def _targeted_connections(hass: HomeAssistant, call: ServiceCall) -> list:
    """The connections a service call is aimed at, or all of them if untargeted.

    **Resolved through Home Assistant's own target extraction, not by hand.** A
    domain service gets no expansion -- `call.data` is whatever the caller put
    under `target:`, verbatim -- and a first attempt at this read only
    `entity_id` and `device_id`. That silently mis-resolved every other target
    form the UI picker offers: an `area_id`/`floor_id`/`label_id` target matched
    neither key, fell through to the untargeted branch and checked in with
    *every* lamp. `TargetSelection` + `async_extract_referenced_entity_ids` are
    the same helpers the entity-service path uses, so all five forms and group
    expansion behave exactly as they did before 0.9.2.

    `entity_id: all` is special-cased because HA special-cases it too, one layer
    above the extraction (`async_extract_entities`); left to the extractor it
    would be looked up as a literal entity id and match nothing.
    """
    connections = hass.data.get(DOMAIN, {})
    if not connections:
        return []

    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        return list(connections.values())

    selection = TargetSelection(call.data)
    if not selection.has_any_target:
        return list(connections.values())

    selected = async_extract_referenced_entity_ids(hass, selection)
    selected.log_missing(selected.referenced, _LOGGER)

    registry = er.async_get(hass)
    entry_ids = {
        entity.config_entry_id
        for entity_id in selected.referenced | selected.indirectly_referenced
        if (entity := registry.async_get(entity_id)) and entity.config_entry_id
    }
    return [conn for entry_id, conn in connections.items() if entry_id in entry_ids]


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the lamp's stored pairing keys along with the entry.

    Removing the integration is how a lamp that can no longer be reached gets
    cleaned up -- `fermob.unpair` refuses on an unreachable lamp by design -- so
    the keys must not outlive it as an orphaned `.storage/fermob_<mac>`.

    **This makes "delete it and add it again" a one-way door**, and that is a
    known, accepted cost rather than an oversight. Removing an entry tells the
    lamp nothing: it stays registered to us in PRIVATE mode, and once the keys
    are gone the re-add hits the handshake's step-1 probe, finds a lamp it cannot
    decrypt, and stops. The only way back is holding the lamp's button for ten
    seconds. The pairing error says exactly that, and README/PAIRING.md warn
    about it up front.

    Note this is *not* the recovery path for a lamp that was factory-reset while
    the entry existed -- `_lamp_still_paired()` handles that one automatically,
    with no `.storage` surgery and no re-add.
    """
    await _key_store(hass, entry.data[CONF_ADDRESS]).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
