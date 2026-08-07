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

from custom_components.fermob.config_flow import CONF_CHECK_FIRMWARE
from custom_components.fermob.firmware import FirmwareRelease
from custom_components.fermob.light import FermobBLEConnection
from custom_components.fermob.protocol import LIGHT_TYPE_TW
from custom_components.fermob.update import FermobFirmwareUpdate, async_setup_entry

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


def _entity(
    hass: HomeAssistant, conn, entry, checking: bool = True
) -> FermobFirmwareUpdate:
    entity = FermobFirmwareUpdate(hass, entry, conn, checking)
    entity.async_write_ha_state = MagicMock()
    entity.async_on_remove = MagicMock()
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

    **A background task specifically.** `async_schedule_update_ha_state` and
    `update_before_add` both land in `hass._tasks`, which bootstrap drains via
    `async_block_till_done()` -- so on a box that cannot reach the vendor server
    the whole request budget would be added to HA's startup time.
    """
    conn = _conn(hass, model="MOOON - H134", sw_version="2.3.21.0")
    entity = _entity(hass, conn, _entry())
    created: list = []
    hass.async_create_background_task = MagicMock(
        side_effect=lambda coro, name=None: created.append((coro, name))
    )

    await entity.async_added_to_hass()

    assert len(created) == 1
    coro, name = created[0]
    assert "firmware" in name
    with patch(
        "custom_components.fermob.update.async_get_latest_release",
        AsyncMock(return_value=FirmwareRelease("3.0.27.0", "abc", None)),
    ):
        await coro

    assert entity.latest_version == "3.0.27.0"


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


async def test_the_option_being_off_still_adds_the_entity_but_never_polls(
    hass: HomeAssistant,
):
    """The option governs the request, not whether the entity exists.

    Three alternatives were tried and each broke something a user owns: not
    adding it strands a stale `unavailable` state until the next restart,
    deleting the registry row destroys renames, areas and history, and disabling
    it makes core schedule a second config-entry reload that drops the BLE link.
    So the entity stays, unpolled, reading *unknown* -- which is exactly true.
    """
    entry = _entry()
    entry.entry_id = "abc"
    entry.options = {CONF_CHECK_FIRMWARE: False}
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass, sw_version="3.0.27.0")
    added: list = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
    entity = added[0]
    assert entity.should_poll is False
    assert entity.installed_version == "3.0.27.0"
    assert entity.latest_version is None


async def test_the_option_being_off_schedules_no_startup_check(hass: HomeAssistant):
    """`should_poll` alone is not enough -- the one-shot must be skipped too."""
    entity = _entity(hass, _conn(hass, model="MOOON - H134"), _entry(), checking=False)
    hass.async_create_background_task = MagicMock()

    await entity.async_added_to_hass()

    hass.async_create_background_task.assert_not_called()


async def test_the_option_defaults_to_on(hass: HomeAssistant):
    entry = _entry()
    entry.entry_id = "abc"
    hass.data.setdefault("fermob", {})["abc"] = _conn(hass)
    added: list = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
    assert isinstance(added[0], FermobFirmwareUpdate)
    assert added[0].should_poll is True
