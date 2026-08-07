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

from homeassistant.components.update import UpdateEntityFeature
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


async def test_the_option_being_off_deletes_the_registry_entry(hass: HomeAssistant):
    """Not adding an entity does not remove it -- HA shows it as unavailable.

    Without this, switching the option off would leave a permanently broken
    looking row behind that only a manual delete clears, which is not what the
    option says it does.
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

    assert registry.async_get(existing.entity_id) is None


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
