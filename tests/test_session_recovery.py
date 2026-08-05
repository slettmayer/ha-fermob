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
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.fermob as fermob
from custom_components.fermob import _key_store
from custom_components.fermob.light import (
    FermobBLEConnection,
    FermobLight,
    LampNotAnswering,
)
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


def _deaf_until_repaired(hass: HomeAssistant) -> FermobBLEConnection:
    """Deaf on the first pass, answering once it has been re-paired.

    The realistic factory-reset shape: two unanswered requests, the probe, the
    handshake, and then a lamp that talks again.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(side_effect=[False, False, True])
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
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE, True))

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
    conn._send_frames = AsyncMock()
    conn.request_battery = AsyncMock(return_value=True)

    await conn.ensure_connected()

    conn._send_frames.assert_not_called()


# ---------------------------------------------------------------------------
# A lamp that was factory-reset behind our back
# ---------------------------------------------------------------------------


async def test_a_factory_reset_lamp_is_repaired(hass: HomeAssistant):
    """Stored keys plus a reset lamp is a silent, permanent dead end.

    Nothing else detects it: the reconnect path skips the handshake, every frame
    goes out PRIVATE-encrypted to a lamp back in NONE mode, and the only recovery
    was deleting `.storage/fermob_*` by hand.
    """
    conn = _deaf_until_repaired(hass)
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_NONE, True))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()
    # In memory only: a stale key left behind would encrypt the next frame.
    assert conn._pub == bytes(16)
    assert conn._priv == bytes(16)
    assert conn._nonce == bytes(16)
    # One link for the failed pass, then the pairing pass's two.
    assert conn._open_link.await_count == 3


async def test_the_repair_does_not_delete_the_stored_keys_first(
    hass: HomeAssistant,
):
    """The handshake's own `_save_keys()` replaces the record, so deleting it
    up front buys nothing -- and costs everything if the probe misread the lamp
    or the handshake fails halfway. Then the keys are gone, the lamp is still
    registered to us, and nothing retries: `async_check_in` short-circuits on
    `_load_keys()`. That is the unrecoverable state.
    """
    conn = _deaf_until_repaired(hass)
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_NONE, True))

    await conn.ensure_connected()

    conn._store.async_remove.assert_not_called()
    # And a failed re-pair must re-read what is still on disk, not assume none.
    assert conn._keys_loaded is False


async def test_one_dropped_ack_is_not_a_diagnosis(hass: HomeAssistant):
    """A marginal link drops one ACK. That must not cost a pairing frame.

    Everything past this point is expensive or destructive -- REGISTER(0), maybe
    a re-pair, otherwise a failed connect that takes the light unavailable.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(side_effect=[False, True])
    conn._send_frames = AsyncMock()

    await conn.ensure_connected()

    assert conn.request_battery.await_count == 2
    conn._send_frames.assert_not_called()  # never reached the probe
    assert conn._connected is True


async def test_a_half_paired_lamp_is_also_repaired(hass: HomeAssistant):
    """PUBLIC is not PRIVATE: our stored private key is no use there either."""
    conn = _deaf_until_repaired(hass)
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_PUBLIC, True))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()


async def test_the_repair_pass_never_loops(hass: HomeAssistant):
    """A lamp that is deaf for some other reason must not pair over and over."""
    conn = _deaf(hass)  # never answers, even after re-pairing
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_NONE, True))

    with pytest.raises(LampNotAnswering):
        await conn.ensure_connected()

    # Exactly one repair: the second pass pairs, and then fails rather than
    # looping back round to pair again.
    assert conn._pairing_handshake.await_count == 1


async def test_a_lamp_still_in_private_is_left_alone(hass: HomeAssistant):
    """It is deaf, but it is still ours -- re-pairing would not fix that."""
    conn = _deaf(hass)
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE, True))

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


async def test_a_refused_probe_is_read_as_reset_not_as_silence(
    hass: HomeAssistant,
):
    """A lamp that *refuses* the probe has still told us which mode it is in.

    `_send` drops the `answered` flag, so reading the payload here would call a
    refusal "no answer" and classify a reset lamp as still ours. The caller then
    raises `LampNotAnswering` on every connect from then on, and nothing ever
    re-pairs it -- a permanent dead end reached by the code meant to avoid one.
    """
    conn = _deaf_until_repaired(hass)
    # Refused: no payload, but answered, and answered in NONE.
    conn._send_frames = AsyncMock(return_value=(None, ENCRYPT_NONE, True))

    await conn.ensure_connected()

    conn._pairing_handshake.assert_awaited_once()


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
    conn._send_frames = AsyncMock(return_value=(b"\x01", ENCRYPT_PRIVATE, True))

    with pytest.raises(RuntimeError, match="not answering"):
        await conn.ensure_connected()

    conn.disconnect.assert_awaited()
    assert conn._connected is False


async def test_a_fresh_pairing_is_not_trusted_without_an_answer(
    hass: HomeAssistant,
):
    """ "We just paired" is not evidence the session works.

    The handshake's ten ACKs all landed on the pre-REGISTER_END link -- the very
    link the lamp stops honouring, which is why pairing reconnects. Nothing on
    the new link has been acknowledged by anything, so exempting this path would
    report the reproduced post-pairing failure as success.
    """
    conn = _conn(hass)  # no keys
    conn.request_battery = AsyncMock(return_value=False)

    with pytest.raises(LampNotAnswering, match="paired, but"):
        await conn.ensure_connected()

    # And it does not loop round to pair a second time.
    conn._pairing_handshake.assert_awaited_once()


async def test_the_post_pairing_failure_does_not_read_as_pairing_failed(
    hass: HomeAssistant,
):
    """The handshake completed -- keys saved, REGISTER_END sent, lamp owned.

    A message reading like "pairing failed" would send the user to a 10-second
    factory reset they do not need; retrying the command takes the reconnect
    path and works if the link does.
    """
    conn = _conn(hass)
    conn.request_battery = AsyncMock(return_value=False)

    with pytest.raises(LampNotAnswering) as err:
        await conn.ensure_connected()

    assert "registered to Home Assistant" in str(err.value)
    assert "retry the command" in str(err.value)


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
    conn._send_frames = AsyncMock()

    await conn.ensure_connected()

    conn._send_frames.assert_not_called()


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


async def test_check_in_never_pairs(hass: HomeAssistant):
    """A 3 a.m. timer must not take ownership of a lamp.

    The old guard only checked that *we* have keys, which a factory-reset lamp
    leaves untouched on disk. So a user who reset their lamp to hand it back to
    the Fermob app would find it silently re-registered to Home Assistant
    overnight, flashing through the handshake unattended.
    """
    conn = _deaf(hass)
    conn._send_frames = AsyncMock(
        return_value=(b"\x01", ENCRYPT_NONE, True)
    )  # says "reset"

    await conn.async_check_in()  # must not raise

    conn._pairing_handshake.assert_not_called()
    conn._store.async_remove.assert_not_called()


async def test_check_in_reports_a_lamp_that_never_answered(hass: HomeAssistant):
    """Swallowing the failure is not the same as pretending it did not happen.

    Availability is otherwise written only on the command path, so a lamp that
    has gone deaf reads as available and *on* until somebody presses the switch.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.ensure_connected = AsyncMock(side_effect=LampNotAnswering("deaf"))
    seen: list[bool] = []
    conn.add_availability_listener(seen.append)

    await conn.async_check_in()

    assert seen == [False]


async def test_an_out_of_range_lamp_is_not_reported_unavailable(
    hass: HomeAssistant,
):
    """A balcony lamp is out of range for whole seasons. That is not a fault.

    In on-demand mode the next check-in is six hours away, so greying the entity
    out over one missed advertisement would lock the user out of a lamp that
    would answer a command perfectly well -- and `async_check_in`'s own contract
    says the reading is "as of last contact".
    """
    conn = _conn(hass, keys=_KEYS)
    conn.ensure_connected = AsyncMock(
        side_effect=RuntimeError("Fermob BLE device not found")
    )
    seen: list[bool] = []
    conn.add_availability_listener(seen.append)

    await conn.async_check_in()

    assert seen == []


async def test_check_in_reports_a_lamp_that_did_answer(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    conn.ensure_connected = AsyncMock()
    seen: list[bool] = []
    conn.add_availability_listener(seen.append)

    await conn.async_check_in()

    assert seen == [True]


async def test_the_light_follows_the_check_in_verdict(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    entry = SimpleNamespace(data={CONF_ADDRESS: ADDRESS}, options={}, entry_id="abc123")
    light = FermobLight(hass, entry, conn, LIGHT_TYPE_TW)
    light.hass = hass
    light.schedule_update_ha_state = MagicMock()
    light._attr_available = True

    light.on_check_in_result(False)
    assert light.available is False

    light.on_check_in_result(True)
    assert light.available is True

    # Unchanged verdicts must not churn the state machine.
    light.schedule_update_ha_state.reset_mock()
    light.on_check_in_result(True)
    light.schedule_update_ha_state.assert_not_called()


async def test_a_lost_dead_session_is_still_reported(hass: HomeAssistant):
    """ "Cannot reach it" only excuses the entity while the lamp was last known good.

    Here the check-in already proved the session was dead -- unanswered over an
    open link -- tore it down, and then could not get the lamp back. Staying
    quiet would leave the entity reading available and *on* for another whole
    interval, which is the failure this release exists to fix.
    """
    conn = _conn(hass, keys=_KEYS)
    conn._connected = True
    conn._client = MagicMock(is_connected=True)
    conn.request_battery = AsyncMock(return_value=False)
    conn.ensure_connected = AsyncMock(
        side_effect=[None, RuntimeError("Fermob BLE device not found")]
    )
    seen: list[bool] = []
    conn.add_availability_listener(seen.append)

    await conn.async_check_in()  # must not raise

    assert seen == [False]


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
    conn._send_frames = AsyncMock(return_value=(b"\x00\x00\x00", ENCRYPT_PRIVATE, True))

    assert await conn.request_battery() is True


async def test_request_battery_reports_a_timeout(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    del conn.request_battery
    conn._send_frames = AsyncMock(return_value=(None, 0, False))

    assert await conn.request_battery() is False


async def test_a_refused_battery_request_still_counts_as_an_answer(
    hass: HomeAssistant,
):
    """A NAK is proof the lamp is listening, which is the only thing asked here.

    `_send_frames` returns `payload=None` for a rejection as well as a timeout,
    so reading the payload would call a lamp that answered "no" dead -- and this
    codebase already documents commands the lamp refuses outright in GATEWAY
    mode. The consequence would be a working lamp taken unavailable, and sent a
    pairing frame, for declining one diagnostic read.
    """
    conn = _conn(hass, keys=_KEYS)
    del conn.request_battery
    conn._send_frames = AsyncMock(return_value=(None, ENCRYPT_PRIVATE, True))

    assert await conn.request_battery() is True


async def test_open_link_releases_a_stale_client_first(hass: HomeAssistant):
    """A session that died without `disconnect()` leaves a client behind.

    Overwriting it strands the old BleakClient: never closed, never collected,
    and still holding one of an ESPHome proxy's three connection slots.
    """
    conn = _conn(hass, keys=_KEYS)
    del conn._open_link
    conn._client = MagicMock(is_connected=False)  # stale

    with (
        patch(
            "custom_components.fermob.light.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fermob.light.establish_connection",
            AsyncMock(return_value=MagicMock(start_notify=AsyncMock())),
        ),
    ):
        await conn._open_link()

    conn.disconnect.assert_awaited_once()


async def test_a_push_during_an_ack_wait_does_not_spin(hass: HomeAssistant):
    """Re-queue-and-continue re-reads the same frame immediately.

    With `_ready` False -- which every connect pass now sets -- an unsolicited
    push arriving before its ACK would spin the event loop until the 3 s
    deadline. It must be set aside and the ACK still matched.
    """
    conn = _conn(hass, keys=_KEYS)
    conn._ready = False
    conn._client = MagicMock(write_gatt_char=AsyncMock())

    push = bytes([(4 << 5) | 0, 0x00, 0, 0]) + bytes(16)  # mt=4, a state push
    ack = bytes([(2 << 5) | 0, 0x01, 0, 0]) + bytes(16)  # mt=2, cmd == our seq
    conn._ack_queue.put_nowait(push)
    conn._ack_queue.put_nowait(ack)

    with patch(
        "custom_components.fermob.light.decode_fragment", return_value=bytes(15)
    ):
        pl, _enc, answered = await conn._send_frames([ack])

    assert answered is True
    assert pl is not None
    # Set aside, not dropped: it goes back for the notification handler's turn.
    assert conn._ack_queue.qsize() == 1


async def test_deferred_pushes_survive_an_undecodable_ack(hass: HomeAssistant):
    """`_drain()` has to run even when the loop body raises.

    `decode_fragment` can throw on a malformed ACK. Anything still held aside
    would be dropped rather than handed back -- during pairing that can swallow
    the post-REGISTER_END EVENT `_wait_for_event` is waiting for.
    """
    conn = _conn(hass, keys=_KEYS)
    conn._ready = False
    conn._client = MagicMock(write_gatt_char=AsyncMock())

    push = bytes([(4 << 5) | 0, 0x00, 0, 0]) + bytes(16)
    ack = bytes([(2 << 5) | 0, 0x01, 0, 0]) + bytes(16)
    conn._ack_queue.put_nowait(push)
    conn._ack_queue.put_nowait(ack)

    with (
        patch(
            "custom_components.fermob.light.decode_fragment",
            side_effect=ValueError("undecodable"),
        ),
        pytest.raises(ValueError),
    ):
        await conn._send_frames([ack])

    assert conn._ack_queue.qsize() == 1  # the push was handed back


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


async def test_unpair_does_not_broadcast_when_the_session_is_dead(
    hass: HomeAssistant,
):
    """Sending it anyway makes the caller's error message a coin toss.

    UNREGISTER is destructive and unacknowledged. Fire it into a session we
    just failed to verify and the lamp may well receive it and drop to NONE --
    while `async_unpair` truthfully reports "nothing has been removed" and keeps
    the keys. Lamp unregistered, HA still holding keys and an entry, and the next
    connect silently re-pairs it: the user can never hand the lamp back to the
    Fermob app.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(return_value=False)
    conn._client = MagicMock()
    conn._client.write_gatt_char = AsyncMock()

    assert await conn.unpair() is False
    # Retried, like every other place that acts on an unanswered request.
    assert conn.request_battery.await_count == 2
    conn._client.write_gatt_char.assert_not_called()


async def test_unpair_survives_one_dropped_ack(hass: HomeAssistant):
    """One missed reply must not abort the service and blame the user."""
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(side_effect=[False, True])
    conn._client = MagicMock(write_gatt_char=AsyncMock())

    assert await conn.unpair() is True
    conn._client.write_gatt_char.assert_awaited_once()


async def test_unpair_never_pairs(hass: HomeAssistant):
    """The service releases the lamp; it must not claim one on the way there.

    On a lamp reset behind HA's back, the default `allow_pairing=True` would run
    the re-pair branch -- flashing it, re-registering it -- and only then
    broadcast UNREGISTER.
    """
    conn = _conn(hass, keys=_KEYS)
    conn.unpair = AsyncMock(return_value=True)
    conn.ensure_connected = AsyncMock()
    light = _light(hass, conn)

    with patch.object(hass.config_entries, "async_remove", AsyncMock()):
        await light.async_unpair()

    assert conn.ensure_connected.await_args.kwargs == {"allow_pairing": False}


async def test_unpair_broadcasts_when_the_session_answers(hass: HomeAssistant):
    conn = _conn(hass, keys=_KEYS)
    conn.request_battery = AsyncMock(return_value=True)
    conn._client = MagicMock()
    conn._client.write_gatt_char = AsyncMock()

    assert await conn.unpair() is True
    conn._client.write_gatt_char.assert_awaited_once()


# ---------------------------------------------------------------------------
# Removing the entry takes the keys with it
# ---------------------------------------------------------------------------


async def test_removing_the_entry_deletes_the_stored_keys(hass: HomeAssistant):
    """`fermob.unpair` refuses on an unreachable lamp, so this is the cleanup
    path for a lamp that is gone -- the keys must not outlive it as an orphan.

    Accepted cost: this also makes "delete it and add it again" a one-way door,
    because the lamp stays registered while its keys go. `async_remove_entry`
    documents that, and the pairing error tells the user to factory-reset.
    """
    entry = MockConfigEntry(domain="fermob", data={CONF_ADDRESS: ADDRESS})

    with patch(
        "custom_components.fermob.Store.async_remove", AsyncMock()
    ) as async_remove:
        await fermob.async_remove_entry(hass, entry)

    async_remove.assert_awaited_once()


def test_the_key_store_is_named_after_the_lamp():
    """`fermob.unpair` and setup must agree on which file holds the keys."""
    hass = MagicMock()
    assert _key_store(hass, ADDRESS).key == "fermob_d6_86_76_e8_7e_75"
