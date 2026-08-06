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
targeting behaviour that keeps existing `target:`-style automations working.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fermob import (
    DOMAIN,
    SERVICE_CHECK_IN,
    _async_register_check_in_service,
    _targeted_connections,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.async_check_in = AsyncMock()
    return conn


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
