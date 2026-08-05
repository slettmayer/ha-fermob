"""What happens when the lamp stops honouring a link that still looks up.

Every one of these covers the same failure shape, which 0.8.0 made permanent by
holding the link open: the BLE link is up, `is_connected` is True, and the lamp
is discarding everything we send. Nothing in the command path can see it --
`send_led` is a write-without-response and cannot fail, so the entity stays
available and the UI reports success while the lamp sits dark.

Before the link was held open, the 30 s idle disconnect repaired this within
half a minute of every command, invisibly and by accident. These tests pin the
mechanisms that replace it:

* pairing hands back a *fresh* link, because the lamp stops honouring the one it
  was paired on once REGISTER_END puts it in GATEWAY mode;
* the check-in treats an unacknowledged battery request as a dead session and
  reconnects, rather than swallowing the timeout;
* a reconnect asks the lamp what encryption mode it is in, so a lamp that was
  factory-reset behind our back is re-paired instead of being sent frames it can
  no longer decrypt, forever;
* an unpair that was not acknowledged keeps the keys, because deleting them
  while the lamp stays registered is the one state nothing recovers from.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.fermob as fermob
from custom_components.fermob import _key_store
from custom_components.fermob.light import FermobBLEConnection, FermobLight
from custom_components.fermob.protocol import (
    ENCRYPT_NONE,
    ENCRYPT_PRIVATE,
    ENCRYPT_PUBLIC,
    LIGHT_TYPE_TW,
)

ADDRESS = "D6:86:76:E8:7E:75"

_KEYS = {
    "pub": "00" * 16,
    "priv": "11" * 16,
    "nonce": "22" * 16,
    "addr_b2": 0x75,
    "addr_b3": 0x7E,
}


def _conn(hass: HomeAssistant, keys: dict | None = None) -> FermobBLEConnection:
    """A connection whose radio is entirely stubbed out.

    `_open_link` stands in for the whole BLE connect -- device lookup,
    `establish_connection`, `start_notify` -- so these tests can count links
    without a radio.
    """
    store = MagicMock()
    store.async_save = AsyncMock()
    store.async_remove = AsyncMock()
    store.async_load = AsyncMock(return_value=keys)
    conn = FermobBLEConnection(
        hass, ADDRESS, store, light_type=LIGHT_TYPE_TW, idle_disconnect_delay=None
    )
    conn._open_link = AsyncMock(
        side_effect=lambda: setattr(conn, "_client", MagicMock())
    )
    conn.disconnect = AsyncMock(side_effect=lambda: setattr(conn, "_connected", False))
    conn._pairing_handshake = AsyncMock()
    conn._fetch_module_info_once = AsyncMock()
    conn.set_module_time = AsyncMock()
    conn.request_battery = AsyncMock(return_value=True)
    return conn


def _deaf(hass: HomeAssistant) -> FermobBLEConnection:
    """A paired connection whose lamp never acknowledges anything.

    The failure that gates the pairing probe: the link comes up, the battery
    request goes unanswered, and only then is the lamp asked whether it still
    holds our keys.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Pairing hands back a fresh link
# ---------------------------------------------------------------------------


async def test_pairing_ends_on_a_fresh_link(hass: HomeAssistant):
    """The link a lamp was paired on is not the link it will answer on.

    Reproduced on an H134: after pairing, every command was accepted by Home
    Assistant and ignored by the lamp until the integration was reloaded. A
    reload is a fresh connect, so pairing does the reconnect itself.
    """
    conn = _conn(hass)  # no stored keys -> pairing

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()
    assert conn._open_link.await_count == 2
    conn.disconnect.assert_awaited_once()
    assert conn._connected is True
    assert conn._ready is True


async def test_a_plain_reconnect_opens_one_link(hass: HomeAssistant):
    """The extra connect belongs to pairing only -- it is not a per-connect cost."""
    conn = _conn(hass, keys=_KEYS)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_not_called()
    assert conn._open_link.await_count == 1
    conn._fetch_module_info_once.assert_awaited_once()


async def test_a_healthy_reconnect_sends_no_pairing_frame(hass: HomeAssistant):
    """`REGISTER(0)` is the first frame of the pairing sequence.

    What it does to a lamp that is already registered is unknown beyond "it
    answers" -- the protocol is reverse-engineered and the vendor app has never
    been seen sending it to a lamp it owns. So it stays behind a failure: a lamp
    that answers its battery request is never asked anything else.
    """
    conn = _conn(hass, keys=_KEYS)
    conn._send = AsyncMock()
    conn.request_battery = AsyncMock(return_value=True)

    await conn.ensure_connected()

    conn._send.assert_not_called()


# ---------------------------------------------------------------------------
# A lamp that was factory-reset behind our back
# ---------------------------------------------------------------------------


async def test_a_factory_reset_lamp_is_repaired(hass: HomeAssistant):
    """Stored keys plus a reset lamp is a silent, permanent dead end.

    Nothing else detects it: the reconnect path skips the handshake, every frame
    goes out PRIVATE-encrypted to a lamp back in NONE mode, and the only recovery
    was deleting `.storage/fermob_*` by hand.
    """
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_NONE))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()
    conn._store.async_remove.assert_awaited_once()
    # In memory too: a stale key left behind would encrypt the next frame.
    assert conn._pub == bytes(16)
    assert conn._priv == bytes(16)
    assert conn._nonce == bytes(16)
    # One link for the failed pass, then the pairing pass's two.
    assert conn._open_link.await_count == 3


async def test_a_half_paired_lamp_is_also_repaired(hass: HomeAssistant):
    """PUBLIC is not PRIVATE: our stored private key is no use there either."""
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_PUBLIC))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()


async def test_the_repair_pass_never_loops(hass: HomeAssistant):
    """A lamp that is deaf for some other reason must not pair over and over."""
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_NONE))

    await conn.ensure_connected()

    # Exactly one repair: the second pass pairs and stops, answered or not.
    assert conn._pairing_handshake.await_count == 1


async def test_a_lamp_still_in_private_is_left_alone(hass: HomeAssistant):
    """It is deaf, but it is still ours -- re-pairing would not fix that."""
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE))

    with pytest.raises(RuntimeError):
        await conn.ensure_connected()

    conn._pairing_handshake.assert_not_called()
    conn._store.async_remove.assert_not_called()


async def test_an_unanswered_probe_never_repairs(hass: HomeAssistant):
    """Pairing makes the lamp flash, so silence must never be read as consent.

    A probe that times out says nothing about pairing -- the lamp may simply be
    at the edge of range. Re-pairing on that would flash the lamp at an arbitrary
    hour and, worse, would throw away keys that were still good.
    """
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(None, 0))

    with pytest.raises(RuntimeError):
        await conn.ensure_connected()

    conn._pairing_handshake.assert_not_called()
    conn._store.async_remove.assert_not_called()


async def test_a_probe_that_raises_never_repairs(hass: HomeAssistant):
    conn = _deaf(hass)
    conn._send = AsyncMock(side_effect=RuntimeError("link dropped"))

    with pytest.raises(RuntimeError):
        await conn.ensure_connected()

    conn._pairing_handshake.assert_not_called()


# ---------------------------------------------------------------------------
# A connect that cannot prove itself fails, rather than looking healthy
# ---------------------------------------------------------------------------


async def test_a_deaf_lamp_that_is_still_ours_fails_the_connect(hass: HomeAssistant):
    """Returning normally here is the whole bug in miniature.

    `send_led` is a write-without-response, so a caller handed this link would
    write into the void and mark the entity *available*. Nothing downstream can
    tell the difference -- so `ensure_connected` has to be the one that does.
    """
    conn = _deaf(hass)
    conn._send = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE))

    with pytest.raises(RuntimeError, match="not answering"):
        await conn.ensure_connected()

    conn.disconnect.assert_awaited()
    assert conn._connected is False


async def test_a_fresh_pairing_is_trusted_without_a_battery_answer(
    hass: HomeAssistant,
):
    """The handshake ACKs every step, so the session is proven already.

    Failing here on a silent battery request would refuse a lamp that has just
    demonstrably completed a ten-command exchange.
    """
    conn = _conn(hass)  # no keys
    conn.request_battery = AsyncMock(return_value=False)

    await conn.ensure_connected()  # must not raise

    assert conn._connected is True


async def test_start_notify_failure_releases_the_client(hass: HomeAssistant):
    """A link with no notifications is useless, and keeping it leaks it.

    `async_check_in` swallows what this raises, so a client left on the instance
    would be `establish_connection`-ed on top of at the next connect.
    """
    conn = _conn(hass, keys=_KEYS)
    del conn._open_link  # exercise the real one
    client = MagicMock()
    client.start_notify = AsyncMock(side_effect=RuntimeError("no such characteristic"))

    with (
        patch(
            "custom_components.fermob.light.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fermob.light.establish_connection",
            AsyncMock(return_value=client),
        ),
        pytest.raises(RuntimeError, match="no such characteristic"),
    ):
        await conn._open_link()

    conn.disconnect.assert_awaited_once()


async def test_an_unpaired_lamp_is_not_probed(hass: HomeAssistant):
    """There is nothing to verify without keys -- the handshake probes anyway."""
    conn = _conn(hass)
    conn._send = AsyncMock()

    await conn.ensure_connected()

    conn._send.assert_not_called()


# ---------------------------------------------------------------------------
# The check-in as a liveness probe
# ---------------------------------------------------------------------------


async def test_check_in_reconnects_when_the_lamp_stops_answering(
    hass: HomeAssistant,
):
    """The battery request is the only frame we send that is ever acknowledged.

    So it is the only thing that can tell a live session from a dead one, and a
    timeout has to drive a reconnect rather than be logged and dropped.
    """
    conn = _conn(hass, keys=_KEYS)
    conn._connected = True
    conn._client = MagicMock(is_connected=True)
    conn.ensure_connected = AsyncMock()
    conn.request_battery = AsyncMock(return_value=False)

    await conn.async_check_in()

    conn.disconnect.assert_awaited_once()
    assert conn.ensure_connected.await_count == 2


async def test_check_in_stays_put_when_the_lamp_answers(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    conn._connected = True
    conn._client = MagicMock(is_connected=True)
    conn.ensure_connected = AsyncMock()
    conn.request_battery = AsyncMock(return_value=True)

    await conn.async_check_in()

    conn.disconnect.assert_not_called()
    conn.ensure_connected.assert_awaited_once()


async def test_a_failed_recovery_is_not_an_error(hass: HomeAssistant):
    """A lamp taken indoors for the winter must not log an error every 30 min."""
    conn = _conn(hass, keys=_KEYS)
    conn._connected = True
    conn._client = MagicMock(is_connected=True)
    conn.request_battery = AsyncMock(return_value=False)
    conn.ensure_connected = AsyncMock(
        side_effect=[None, RuntimeError("device not found")]
    )

    await conn.async_check_in()  # must not raise


# ---------------------------------------------------------------------------
# `request_battery` reports whether the lamp answered
# ---------------------------------------------------------------------------


async def test_request_battery_reports_an_acknowledgement(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    del conn.request_battery  # use the real one
    conn._send_frames = AsyncMock(return_value=(b"\x00\x00\x00", ENCRYPT_PRIVATE))

    assert await conn.request_battery() is True


async def test_request_battery_reports_a_timeout(hass: HomeAssistant):
    """`_send_frames` returns None for both a timeout and a rejection."""
    conn = _conn(hass, keys=_KEYS)
    del conn.request_battery
    conn._send_frames = AsyncMock(return_value=(None, 0))

    assert await conn.request_battery() is False


async def test_request_battery_still_never_raises(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    del conn.request_battery
    conn._send_frames = AsyncMock(side_effect=RuntimeError("link dropped"))

    assert await conn.request_battery() is False


# ---------------------------------------------------------------------------
# Unpair keeps the keys unless the lamp was reachable
# ---------------------------------------------------------------------------


def _light(hass: HomeAssistant, conn: FermobBLEConnection) -> FermobLight:
    entry = SimpleNamespace(data={CONF_ADDRESS: ADDRESS}, options={}, entry_id="abc123")
    light = FermobLight(hass, entry, conn, LIGHT_TYPE_TW)
    light.hass = hass
    return light


async def test_unpair_keeps_the_keys_when_the_lamp_did_not_answer(
    hass: HomeAssistant,
):
    """UNREGISTER is a fire-and-forget broadcast and can never be acknowledged.

    Deleting the keys anyway is what leaves a lamp registered to a controller
    that has forgotten it -- the "PRIVATE mode but no stored keys" dead end,
    which only a factory reset clears. The entry has to survive too, or the keys
    go with it.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.unpair = AsyncMock(return_value=False)
    light = _light(hass, conn)
    conn.ensure_connected = AsyncMock()

    with (
        patch.object(hass.config_entries, "async_remove", AsyncMock()) as remove,
        pytest.raises(HomeAssistantError),
    ):
        await light.async_unpair()

    remove.assert_not_called()
    conn._store.async_remove.assert_not_called()


async def test_unpair_removes_entry_and_keys_when_the_lamp_answered(
    hass: HomeAssistant,
):
    """Here the keys really are dead -- the lamp has been told, and is in NONE.

    This is the only place that deletes them: entry removal deliberately does
    not, so if this stopped doing it nothing would.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.unpair = AsyncMock(return_value=True)
    light = _light(hass, conn)
    conn.ensure_connected = AsyncMock()

    with patch.object(hass.config_entries, "async_remove", AsyncMock()) as remove:
        await light.async_unpair()

    remove.assert_awaited_once_with("abc123")
    conn._store.async_remove.assert_awaited_once()
    assert conn._have_keys is False


async def test_unpair_checks_the_session_before_sending(hass: HomeAssistant):
    """The broadcast cannot be acknowledged, so something else has to be."""
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(return_value=False)
    conn._client = MagicMock()
    conn._client.write_gatt_char = AsyncMock()

    assert await conn.unpair() is False
    conn.request_battery.assert_awaited_once()
    # Still sent: a broadcast costs nothing and may yet land.
    conn._client.write_gatt_char.assert_awaited_once()


# ---------------------------------------------------------------------------
# Removing the entry deliberately keeps the keys
# ---------------------------------------------------------------------------


def test_removing_the_entry_does_not_delete_the_keys():
    """A regression guard, because deleting them looks like obvious hygiene.

    It is the opposite. Removing an entry tells the lamp nothing, so it stays
    registered to us in PRIVATE mode; throw the keys away at the same moment and
    "delete it and add it again" -- the first thing anyone tries -- becomes
    unrecoverable without a 10-second factory reset. Keeping them makes that
    re-add just work, and `_lamp_still_paired()` covers the case the deletion
    was meant to: a lamp factory-reset while the entry existed.
    """
    assert not hasattr(fermob, "async_remove_entry")


def test_the_key_store_is_named_after_the_lamp():
    """`fermob.unpair` and setup must agree on which file holds the keys."""
    hass = MagicMock()
    assert _key_store(hass, ADDRESS).key == "fermob_d6_86_76_e8_7e_75"
