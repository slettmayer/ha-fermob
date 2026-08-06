"""`fermob.check_in` is a domain service, and why that is not a detail.

Home Assistant filters an entity service's targets by availability *before* the
handler runs, silently: `async_extract_entities` drops the entity from the match
set before testing `entity.available`, so the call is not even logged as missing
and reports success having done nothing. Registered on the light platform, as it
was through 0.9.1, `fermob.check_in` was therefore unreachable on exactly the
lamp a user would want to check in on -- one whose entity had gone unavailable
after a failed command. Found on hardware, 2026-08-06.

These tests pin the structural property that fixes it (the service is on the
domain, and reaching the connection does not depend on any entity), plus the
three things being a domain service costs, each of which was got wrong first
time: target expansion, concurrency, and a registration lifetime not tied to any
entry.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS, ENTITY_MATCH_ALL, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import custom_components.fermob as fermob
from custom_components.fermob import (
    DOMAIN,
    SERVICE_CHECK_IN,
    _async_register_check_in_service,
    _targeted_connections,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _conn(address: str = ADDRESS) -> MagicMock:
    conn = MagicMock()
    conn.address = address
    conn.async_check_in = AsyncMock()
    return conn


def _lamp(hass: HomeAssistant, unique: str, **kw) -> tuple[str, str]:
    """A registered Fermob lamp. Returns (entry_id, entity_id)."""
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS})
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "light", DOMAIN, unique, config_entry=entry, **kw
    )
    return entry.entry_id, entity.entity_id


def _register(hass: HomeAssistant, **connections) -> None:
    hass.data.setdefault(DOMAIN, {}).update(connections)
    _async_register_check_in_service(hass)


async def test_the_service_is_registered_on_the_domain(hass: HomeAssistant):
    """Not on the light platform -- that is the whole fix.

    A platform registration would be filtered by entity availability before ever
    reaching the handler.
    """
    _register(hass, entry_a=_conn())

    assert hass.services.has_service(DOMAIN, SERVICE_CHECK_IN)


async def test_it_runs_without_any_entity_being_available(hass: HomeAssistant):
    """The regression this exists for.

    Nothing in the call path consults an entity, so there is no availability to
    be filtered on. Calling with no target at all is the strongest form of that:
    an entity service could not even be dispatched here.
    """
    conn = _conn()
    _register(hass, entry_a=conn)

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_IN, {}, blocking=True)

    assert conn.async_check_in.await_count == 1


async def test_an_untargeted_call_reaches_every_lamp(hass: HomeAssistant):
    """ "Check in" with no target means all of them, not none of them."""
    first, second = _conn(), _conn()
    _register(hass, entry_a=first, entry_b=second)

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_IN, {}, blocking=True)

    assert first.async_check_in.await_count == 1
    assert second.async_check_in.await_count == 1


async def test_an_entity_target_reaches_only_that_lamp(hass: HomeAssistant):
    """Existing `target: {entity_id: ...}` automations must keep working.

    A domain service gets no target expansion, so the entity id is resolved back
    to its config entry by hand. Two lamps, one named: the other must not be
    woken.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS})
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "light", DOMAIN, "fermob_unique", config_entry=entry
    )

    targeted, other = _conn(), _conn()
    _register(hass, **{entry.entry_id: targeted, "other_entry": other})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"entity_id": entity.entity_id}, blocking=True
    )

    assert targeted.async_check_in.await_count == 1
    assert other.async_check_in.await_count == 0


async def test_an_unknown_entity_target_reaches_nothing(hass: HomeAssistant):
    """A target that resolves to no lamp must not silently mean "all of them".

    The untargeted default is deliberate; falling back to it on a typo would
    make a mistargeted call look like it worked.
    """
    conn = _conn()
    _register(hass, entry_a=conn)

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"entity_id": "light.not_a_fermob"}, blocking=True
    )

    assert conn.async_check_in.await_count == 0


async def test_a_device_target_resolves_through_its_entities(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, ADDRESS)}
    )
    er.async_get(hass).async_get_or_create(
        "light", DOMAIN, "fermob_unique", config_entry=entry, device_id=device.id
    )

    targeted, other = _conn(), _conn()
    _register(hass, **{entry.entry_id: targeted, "other_entry": other})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"device_id": device.id}, blocking=True
    )

    assert targeted.async_check_in.await_count == 1
    assert other.async_check_in.await_count == 0


async def test_registering_twice_is_harmless(hass: HomeAssistant):
    """Every entry setup calls it; the second lamp must not trip over the first."""
    _register(hass, entry_a=_conn())
    _register(hass, entry_b=_conn())

    assert hass.services.has_service(DOMAIN, SERVICE_CHECK_IN)


def test_targeting_ignores_a_target_for_an_entry_with_no_connection(
    hass: HomeAssistant,
):
    """An entry mid-unload has no connection left, and must not be conjured up."""
    hass.data[DOMAIN] = {}
    call = MagicMock(data={"entity_id": "light.anything"})

    assert _targeted_connections(hass, call) == []


# ---------------------------------------------------------------------------
# Target expansion: a domain service gets none, so all five forms must work
# ---------------------------------------------------------------------------
#
# The first version of this handler read only `entity_id` and `device_id`. Every
# other target form the UI picker offers fell through to the untargeted branch
# and checked in with *every* lamp -- and `entity_id: all` matched none. Both are
# silent, which is the failure mode this whole service exists to remove.


async def test_an_area_target_reaches_only_the_lamps_in_it(hass: HomeAssistant):
    """`services.yaml` declares a target block, so the picker offers areas."""
    area = ar.async_get(hass).async_create("Balcony")
    targeted_entry, entity_id = _lamp(hass, "in_area")
    er.async_get(hass).async_update_entity(entity_id, area_id=area.id)
    other_entry, _ = _lamp(hass, "elsewhere")

    targeted, other = _conn(), _conn()
    _register(hass, **{targeted_entry: targeted, other_entry: other})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"area_id": area.id}, blocking=True
    )

    assert targeted.async_check_in.await_count == 1
    assert other.async_check_in.await_count == 0


async def test_a_label_target_reaches_only_the_labelled_lamp(hass: HomeAssistant):
    targeted_entry, entity_id = _lamp(hass, "labelled")
    er.async_get(hass).async_update_entity(entity_id, labels={"outdoor"})
    other_entry, _ = _lamp(hass, "unlabelled")

    targeted, other = _conn(), _conn()
    _register(hass, **{targeted_entry: targeted, other_entry: other})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"label_id": "outdoor"}, blocking=True
    )

    assert targeted.async_check_in.await_count == 1
    assert other.async_check_in.await_count == 0


async def test_entity_id_all_reaches_every_lamp(hass: HomeAssistant):
    """HA special-cases `all` above target extraction, so this must too.

    Left to the extractor it is looked up as a literal entity id and matches
    nothing -- reintroducing silent success on a perfectly ordinary automation.
    """
    first_entry, _ = _lamp(hass, "one")
    second_entry, _ = _lamp(hass, "two")
    first, second = _conn(), _conn()
    _register(hass, **{first_entry: first, second_entry: second})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"entity_id": ENTITY_MATCH_ALL}, blocking=True
    )

    assert first.async_check_in.await_count == 1
    assert second.async_check_in.await_count == 1


async def test_a_battery_entity_target_still_finds_its_lamp(hass: HomeAssistant):
    """The diagnostic entities are targetable, and share the lamp's entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS})
    entry.add_to_hass(hass)
    battery = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "fermob_battery",
        config_entry=entry,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    conn = _conn()
    _register(hass, **{entry.entry_id: conn})

    await hass.services.async_call(
        DOMAIN, SERVICE_CHECK_IN, {"entity_id": battery.entity_id}, blocking=True
    )

    assert conn.async_check_in.await_count == 1


# ---------------------------------------------------------------------------
# Concurrency and failure isolation
# ---------------------------------------------------------------------------


async def test_lamps_are_contacted_concurrently(hass: HomeAssistant):
    """The entity service gathered; a serial loop would cost N x the budget.

    With every lamp out of range that is minutes of a blocked caller, so this
    pins overlap rather than merely "all of them were called".
    """
    running = 0
    peak = 0

    async def _slow() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1

    conns = []
    for _ in range(3):
        conn = _conn()
        conn.async_check_in = AsyncMock(side_effect=_slow)
        conns.append(conn)
    _register(hass, **{f"entry_{i}": c for i, c in enumerate(conns)})

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_IN, {}, blocking=True)

    assert peak == 3


async def test_one_failing_lamp_does_not_skip_the_others(hass: HomeAssistant):
    """`async_check_in` swallows its own failures -- but not all of them.

    Its lock acquisition and `_load_keys()` sit outside the try, so a truncated
    key store raises straight out. Serially that lost every later lamp.
    """
    broken = _conn()
    broken.async_check_in = AsyncMock(side_effect=HomeAssistantError("bad store"))
    healthy = _conn()
    _register(hass, entry_broken=broken, entry_healthy=healthy)

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_IN, {}, blocking=True)

    assert healthy.async_check_in.await_count == 1


# ---------------------------------------------------------------------------
# The registration outlives entries
# ---------------------------------------------------------------------------


async def test_the_service_survives_unloading_the_last_entry(hass: HomeAssistant):
    """Every options change reloads the entry, and a single-lamp install then
    has no entries at all for a moment.

    De-registering there means a call in that window raises `ServiceNotFound`
    and aborts the whole automation -- strictly worse than the silent no-op this
    release set out to remove.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS})
    entry.add_to_hass(hass)
    _register(hass, **{entry.entry_id: _conn()})

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await fermob.async_unload_entry(hass, entry) is True

    assert hass.data[DOMAIN] == {}
    assert hass.services.has_service(DOMAIN, SERVICE_CHECK_IN)


async def test_async_setup_registers_it_before_any_entry_exists(
    hass: HomeAssistant,
):
    assert await fermob.async_setup(hass, {}) is True

    assert hass.services.has_service(DOMAIN, SERVICE_CHECK_IN)


async def test_calling_it_with_no_lamps_configured_is_harmless(hass: HomeAssistant):
    """It is registered before any entry, so this is reachable."""
    assert await fermob.async_setup(hass, {}) is True

    await hass.services.async_call(DOMAIN, SERVICE_CHECK_IN, {}, blocking=True)


# ---------------------------------------------------------------------------
# The startup check-in retries, which is what makes 30 s safe
# ---------------------------------------------------------------------------


async def test_the_startup_check_in_retries_until_the_lamp_reports(
    hass: HomeAssistant,
):
    """A single shot at 30 s was a gamble on the Bluetooth stack.

    The check-in swallows its failures, so a proxy that had not finished coming
    up consumed the one attempt in silence and left the next a full interval
    away -- half an hour, or six hours on demand.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS, "name": "Lamp"})
    entry.add_to_hass(hass)

    conn = _conn()
    conn.battery = None
    conn.async_shutdown = lambda: None
    calls: list[None] = []

    async def _check_in() -> None:
        calls.append(None)
        # The stack comes up somewhere between the second and third attempt.
        if len(calls) >= 3:
            conn.battery = MagicMock()

    conn.async_check_in = AsyncMock(side_effect=_check_in)

    with (
        patch("custom_components.fermob.light.FermobBLEConnection", return_value=conn),
        patch("custom_components.fermob.light.resolve_light_type", return_value="tw"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    ):
        assert await fermob.async_setup_entry(hass, entry) is True

        now = dt_util.utcnow()
        for minutes in (1, 3, 6, 10):
            async_fire_time_changed(hass, now + timedelta(minutes=minutes))
            await hass.async_block_till_done()

        # Through the real unload path, so the entry's timers are cancelled
        # rather than left lingering into the next test.
        entry.mock_state(hass, ConfigEntryState.LOADED)
        assert await hass.config_entries.async_unload(entry.entry_id)

    # Three attempts: two that found nothing, then the one that reported. A
    # fourth would mean the retry chain ignored its own success signal.
    assert len(calls) == 3
