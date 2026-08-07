"""The firmware entity.

Two things are worth pinning here, and neither is about HTTP -- `test_firmware`
covers the server. The first is that this entity **cannot install anything**: it
exists to report, and a stray `UpdateEntityFeature.INSTALL` would put a button in
the UI that nothing implements. The second is the up-to-date case, which an
update entity expresses by reporting the *installed* version as the latest one --
get that wrong and every lamp on current firmware claims an update forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.fermob.config_flow import CONF_CHECK_FIRMWARE
from custom_components.fermob.firmware import FirmwareRelease
from custom_components.fermob.light import FermobBLEConnection
from custom_components.fermob.protocol import LIGHT_TYPE_TW
from custom_components.fermob.update import (
    FermobFirmwareUpdate,
    async_setup_entry,
    firmware_unique_id,
)

ADDRESS = "D6:86:76:E8:7E:75"


def _conn(hass: HomeAssistant, **reported) -> FermobBLEConnection:
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_load = AsyncMock(return_value=None)
    conn = FermobBLEConnection(hass, ADDRESS, store, light_type=LIGHT_TYPE_TW)
    for field, value in reported.items():
        setattr(conn, field, value)
    return conn


def _entry(**data) -> SimpleNamespace:
    return SimpleNamespace(
        data={CONF_ADDRESS: ADDRESS, "name": "Balcony Mooon", **data},
        options={},
    )


def _entity(hass: HomeAssistant, conn, entry) -> FermobFirmwareUpdate:
    entity = FermobFirmwareUpdate(hass, entry, conn)
    entity.async_write_ha_state = MagicMock()
    return entity


async def _update(entity, release: FirmwareRelease | None) -> None:
    with patch(
        "custom_components.fermob.update.async_get_latest_release",
        AsyncMock(return_value=release),
    ):
        await entity.async_update()


def test_it_is_named_after_its_device_class_not_the_device(hass: HomeAssistant):
    """`_attr_name` must stay *absent*, not be set to None.

    HA returns `self._attr_name` whenever the attribute merely exists, and None
    declares the entity to be the device's main feature: it then takes the lamp's
    own name and registers as `update.<lamp>`, indistinguishable from the light in
    pickers -- while README, ENTITIES-AND-SERVICES and the changelog all promise
    `update.<lamp>_firmware`. Left unset, UpdateEntity names it from its device
    class instead.
    """
    entity = _entity(hass, _conn(hass), _entry())

    # The attribute must be absent on the *instance*: HA's `_attr_name` is a
    # class-level property that raises until something assigns it, which is what
    # `hasattr` is really testing inside `_name_internal`.
    assert not hasattr(entity, "_attr_name")
    # And the mechanism itself: "Firmware" here, None (i.e. "use the device
    # name") if `_attr_name` were set. Verified against HA 2026.8 both ways.
    assert (
        entity._name_internal(device_class_name="Firmware", platform_translations={})
        == "Firmware"
    )
    # What makes that fallback produce a name at all rather than nothing.
    assert entity.device_class is UpdateDeviceClass.FIRMWARE
    assert entity._default_to_device_class_name() is True


def test_it_never_offers_to_install(hass: HomeAssistant):
    """The whole design decision, in one assertion.

    Nothing here can write a signed Nordic Secure DFU image, so advertising
    INSTALL would be a button that lies. See docs/domain/FIRMWARE-UPDATE.md.
    """
    entity = _entity(hass, _conn(hass), _entry())
    assert entity.supported_features == UpdateEntityFeature(0)
    assert UpdateEntityFeature.INSTALL not in entity.supported_features


async def test_reports_a_newer_published_build(hass: HomeAssistant):
    conn = _conn(hass, model="MOOON - H134", sw_version="2.3.21.0")
    entity = _entity(hass, conn, _entry())

    await _update(entity, FirmwareRelease("3.0.27.0", "abc", None))

    assert entity.installed_version == "2.3.21.0"
    assert entity.latest_version == "3.0.27.0"


async def test_a_lamp_on_current_firmware_reads_as_up_to_date(hass: HomeAssistant):
    """Same build must report latest == installed, not the raw server string.

    The server serves three components for some models where the lamp reports
    four, so a straight passthrough would show `3.0.24` against `3.0.24.0` as an
    available update on every poll, forever.
    """
    conn = _conn(hass, model="MOOON - D15", sw_version="3.0.24.0")
    entity = _entity(hass, conn, _entry())

    await _update(entity, FirmwareRelease("3.0.24", "abc", None))

    assert entity.latest_version == entity.installed_version == "3.0.24.0"


async def test_an_older_published_build_is_not_an_update(hass: HomeAssistant):
    conn = _conn(hass, model="MOOON - H134", sw_version="3.0.27.0")
    entity = _entity(hass, conn, _entry())

    await _update(entity, FirmwareRelease("2.3.21.0", "abc", None))

    assert entity.latest_version == "3.0.27.0"


async def test_the_installed_version_survives_a_restart(hass: HomeAssistant):
    """Persisted in entry.data, so there is an answer before the first connect."""
    entity = _entity(hass, _conn(hass), _entry(sw_version="2.3.21.0"))
    assert entity.installed_version == "2.3.21.0"


async def test_a_fresh_reading_wins_over_the_persisted_one(hass: HomeAssistant):
    """The entry is only written on a reload, so the connection is the newer one."""
    conn = _conn(hass, sw_version="3.0.27.0")
    entity = _entity(hass, conn, _entry(sw_version="2.3.21.0"))
    assert entity.installed_version == "3.0.27.0"


async def test_an_unanswered_check_reads_as_unknown_not_up_to_date(hass: HomeAssistant):
    """The important one: never claim currency that was never checked.

    `async_get_latest_release` returns None both for "the server does not carry
    this model" -- true of every Hoopik slug, permanently -- and for "no host
    answered". Falling back to the installed version there makes HA render
    "Up-to-date", a positive claim about firmware nobody ever asked about.
    """
    conn = _conn(hass, model="HOOPIK - GL1200", sw_version="2.3.21.0")
    entity = _entity(hass, conn, _entry())

    await _update(entity, None)

    assert entity.installed_version == "2.3.21.0"
    assert entity.latest_version is None
    assert entity.state is None


async def test_the_first_check_runs_at_startup_not_a_day_later(hass: HomeAssistant):
    """HA starts the poll timer and does no initial update of its own.

    With SCAN_INTERVAL at one day, without this the entity sits at unknown for
    24 h after every restart -- and on a box that reloads more often than that,
    forever. It also repairs the reload that drops `_latest`.
    """
    entity = _entity(hass, _conn(hass), _entry())
    entity.async_schedule_update_ha_state = MagicMock()

    await entity.async_added_to_hass()

    entity.async_schedule_update_ha_state.assert_called_once_with(force_refresh=True)


async def test_no_check_before_the_lamp_has_reported_a_model(hass: HomeAssistant):
    """The server path is keyed on the model, and guessing it would 400."""
    entity = _entity(hass, _conn(hass), _entry())
    checked = AsyncMock(return_value=None)

    with patch("custom_components.fermob.update.async_get_latest_release", checked):
        await entity.async_update()

    checked.assert_not_called()
    assert entity.latest_version is None


async def test_a_failed_check_keeps_the_last_known_answer(hass: HomeAssistant):
    conn = _conn(hass, model="MOOON - H134", sw_version="2.3.21.0")
    entity = _entity(hass, conn, _entry())

    await _update(entity, FirmwareRelease("3.0.27.0", "abc", None))
    await _update(entity, None)

    assert entity.latest_version == "3.0.27.0"


async def test_the_lamp_reported_names_are_what_gets_asked_about(hass: HomeAssistant):
    conn = _conn(hass, model="MOOON - H134", manufacturer="Fermob")
    entity = _entity(hass, conn, _entry())
    checked = AsyncMock(return_value=None)

    with patch("custom_components.fermob.update.async_get_latest_release", checked):
        await entity.async_update()

    assert checked.await_args.args[1:] == ("Fermob", "MOOON - H134")


async def test_the_option_being_off_adds_no_entity(hass: HomeAssistant):
    """Off must remove the entity, not leave one that never learns anything."""
    entry = _entry()
    entry.entry_id = "abc"
    entry.options = {CONF_CHECK_FIRMWARE: False}
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)
    added: list = []

    await async_setup_entry(hass, entry, added.extend)

    assert added == []


async def test_the_option_being_off_disables_rather_than_deletes(hass: HomeAssistant):
    """Off must not destroy what the user put on the entity.

    Not providing an entity leaves HA rendering its row as unavailable, which
    reads as broken. Deleting the row instead -- which 0.10.0 briefly did --
    throws away the rename, area, icon and hidden flag, orphans the recorder
    history and silently breaks any dashboard card or automation pointing at the
    entity_id. Disabling keeps all of it and says what is true.
    """
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "update", "fermob", firmware_unique_id(ADDRESS)
    )
    entry = _entry()
    entry.entry_id = "abc"
    entry.options = {CONF_CHECK_FIRMWARE: False}
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)

    await async_setup_entry(hass, entry, [].extend)

    row = registry.async_get(existing.entity_id)
    assert row is not None
    assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_switching_the_option_back_on_re_enables_the_entity(hass: HomeAssistant):
    """Our own disable must reverse cleanly, or off is a one-way door."""
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "update",
        "fermob",
        firmware_unique_id(ADDRESS),
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    entry = _entry()
    entry.entry_id = "abc"
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)
    added: list = []

    await async_setup_entry(hass, entry, added.extend)

    assert registry.async_get(existing.entity_id).disabled_by is None
    assert len(added) == 1


async def test_a_user_disabled_entity_is_left_disabled(hass: HomeAssistant):
    """Only `INTEGRATION` is ours to clear -- a user's choice outranks the option."""
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "update",
        "fermob",
        firmware_unique_id(ADDRESS),
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    entry = _entry()
    entry.entry_id = "abc"
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)

    await async_setup_entry(hass, entry, [].extend)

    assert (
        registry.async_get(existing.entity_id).disabled_by
        is er.RegistryEntryDisabler.USER
    )


async def test_the_option_being_off_is_fine_with_nothing_to_remove(
    hass: HomeAssistant,
):
    """The normal case: it was never on, so there is no registry entry."""
    entry = _entry()
    entry.entry_id = "abc"
    entry.options = {CONF_CHECK_FIRMWARE: False}
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)

    await async_setup_entry(hass, entry, [].extend)  # must not raise


async def test_the_option_defaults_to_on(hass: HomeAssistant):
    entry = _entry()
    entry.entry_id = "abc"
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)
    added: list = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
    assert isinstance(added[0], FermobFirmwareUpdate)
