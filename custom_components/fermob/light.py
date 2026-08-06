"""Fermob BLE light entity — mirrors JS meshConnectionService / BLEProtocolService.

MOOON support (tunable white) added on top of the original Hoopik (dimmable white)
integration by edouardrosset.

The frame/payload construction lives in `protocol.py` (no Home Assistant imports,
unit-tested); this module owns the BLE connection lifecycle and the HA entity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any, NamedTuple

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import DOMAIN
from .protocol import (
    CHAR_UUID,
    CMD_CRYPT_AUTHKEY_GEN,
    CMD_CRYPT_AUTHKEY_GET,
    CMD_CRYPT_NONCE_GENERATE,
    CMD_CRYPT_SET,
    CMD_DEVICE_INFO_GET,
    CMD_MODULE_INFO_GET,
    CMD_REGISTER,
    CMD_UNREGISTER,
    CRYPTO_REJECTION_ERRORS,
    DEVICE_DATA_MARKERS,
    ENCRYPT_NONE,
    ENCRYPT_PRIVATE,
    ENCRYPT_PUBLIC,
    LIGHT_TYPE_DW,
    LIGHT_TYPE_TW,
    LMP_STATUS_DEVICE_DATA,
    MAX_KELVIN,
    MIN_KELVIN,
    MSG_CMD,
    MSG_CMD_ACK,
    MSG_FIRE,
    STATE_PUSH_TYPES,
    Battery,
    ModuleInfo,
    ack_error,
    build_battery_request,
    build_datetime_set_payload,
    build_led_payload,
    build_long,
    build_short,
    decode_fragment,
    error_name,
    kelvin_to_warm_ratio,
    local_time_seconds,
    module_type_to_light_type,
    parse_battery,
    parse_device_record,
    parse_module_info,
    warm_ratio_to_kelvin,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BRIGHTNESS_PCT = 50
DEFAULT_KELVIN = 4000

_STORAGE_VERSION = 1

# How many times `establish_connection` may try before giving up, and the one
# thing about the connect budget we can actually set.
#
# `bleak_retry_connector` hardcodes its per-attempt timeout at the
# `client.connect()` call site -- `BLEAK_TIMEOUT`, 20 s -- and it is not
# reachable through `**kwargs`, which go to the client constructor. So
# `max_attempts` is the only lever, and the library's own default is 4.
#
# **Do not read that as "attempts x 20 s".** Two things break the arithmetic, and
# both were found by reading the pinned library rather than assuming:
#
#   - each attempt also sits under `BLEAK_SAFETY_TIMEOUT`, 60 s, which is what
#     bounds an attempt whose own timeout does not fire; and
#   - `_raise_if_needed` counts only `timeouts + connect_errors` against
#     `max_attempts`. Errors in the `TRANSIENT_ERRORS` set -- and a device that
#     goes missing -- get a *separate* budget of `MAX_TRANSIENT_ERRORS` (9),
#     plus backoff sleeps of up to 4 s each.
#
# So this bounds the common failures (a connect timeout, and hard errors such as
# `ESP_GATT_ERROR`, which counts as a connect error) and does not bound the
# transient ones at all. Roughly: halving it halves a typical out-of-range
# failure, and promises nothing about the worst case.
#
# The right number differs by who is waiting, which is why there are two. A lamp
# in range connects in one to two seconds, so the retries only ever cost time
# when it is genuinely absent: out of range, asleep, or off. On a background
# check-in that costs nothing and the full budget is worth having, because the
# alternative is a missed heartbeat and up to another interval of stale state.
# On a command a human is watching the UI, and a minute-plus of nothing reads as
# a hang. Cutting the interactive path to two does not cost the proxy flakes it
# might look like it does: those are transient errors, and they retry on their
# own budget regardless of what this says.
CONNECT_ATTEMPTS_BACKGROUND = 4
CONNECT_ATTEMPTS_INTERACTIVE = 2

# How long the BLE link is held open after the last command, in seconds --
# or None to hold it open indefinitely, which is now the default.
#
# This used to be 30 s, and that single number was the reason a lamp switched on
# at its own button never showed up in Home Assistant. A vendor-app packet
# capture (2026-08-04) settled what the lamp actually does: it pushes an
# unsolicited EVENT_DEVICE_DATA on every physical button press and a battery
# push on every charger change -- but only while the link is up, and it pushes
# nothing on reconnect. The app reads no state at all, ever. It simply holds the
# link and listens, which is what we now do too.
#
# The cost was measured rather than guessed, over 7.6 h on an H134 held
# connected: about 0.1 %/h, some 2 %/day, against 5 h 20 min of link uptime with
# no disconnects. The connection slot on the BLE proxy is the real cost, which
# is what the on-demand option in the config flow is for.
_IDLE_DISCONNECT_DELAY: float | None = None

# How many times MODULE_INFO_GET may be re-read while it keeps answering without
# a short address. Bounded because a lamp whose short address genuinely is
# 0x0000 must not re-read, and re-write the key store, on every reconnect --
# see `_fetch_module_info_once`.
_MODULE_INFO_MAX_READS = 3


class Ack(NamedTuple):
    """What came back from one command.

    `payload` is None both when the lamp said nothing and when it refused, so
    callers that care about liveness must branch on `answered`, and callers that
    care about *why* it refused must branch on `error`.
    """

    payload: bytes | None
    enc: int
    answered: bool
    error: int | None


class BatteryVerdict(StrEnum):
    """What one battery request established about the session.

    Three outcomes, not two, and the third is the one hardware taught us.
    A NAK normally proves the lamp is listening -- but a `CRYPT_MSG` NAK proves
    the opposite, because it is the lamp saying it cannot decrypt us.
    """

    ANSWERED = "answered"  # a live session
    SILENT = "silent"  # no reply -- could be anything
    KEYS_REJECTED = "keys_rejected"  # the lamp does not hold our keys


class LampNotAnswering(RuntimeError):
    """The BLE link came up and the lamp is not usable over it.

    Distinct from every other connect failure on purpose. "Could not reach the
    lamp at all" -- out of range, taken indoors, adapter busy, no advertisement
    yet -- is the normal condition of a balcony lamp and must leave the entity
    alone. This one means we *did* reach it and it will not do as it is told.

    It carries the `BatteryVerdict` that produced it, because the two kinds need
    opposite handling and the message alone cannot be branched on:

    * `SILENT` -- the lamp is not talking on this link. Nothing the user does
      fixes it *directly*, and only one of the three raise sites offers any
      advice at all ("retry the command rather than resetting it"). It is not
      necessarily terminal -- a marginal link recovers on its own -- but what it
      never is, is grounds to re-pair.
    * `KEYS_REJECTED` -- the lamp was factory-reset. Turning the light on
      re-pairs it, so this one is recoverable, by exactly one gesture.

    Callers that treat the second as the first take the entity unavailable, and
    an unavailable entity cannot be told to turn on -- see `async_check_in`.

    The verdict is **required**. Defaulting it would let a forgotten argument
    read as `SILENT`, which is the wrong answer in both places that branch on it:
    `fermob.unpair` would blame range, and the check-in would grey out a lamp it
    should have left alone -- silently, with nothing pointing at the omission.
    """

    def __init__(self, message: str, verdict: BatteryVerdict) -> None:
        super().__init__(message)
        self.verdict = verdict

    def __reduce__(self) -> tuple:
        """Keep `copy` and `pickle` able to rebuild this.

        They reconstruct from `args`, which holds the message alone -- so with
        `verdict` required they would call the one-argument form and raise
        `TypeError`. Making the argument required is what introduced that, and
        it is worth keeping.

        `args` deliberately stays one long rather than carrying the verdict:
        `BaseException.__str__` returns `repr(args)` once there is more than
        one, which would turn every `"%s" % err` log line -- and the
        `HomeAssistantError` text `async_unpair` builds from it -- into a tuple.
        """
        return (self.__class__, (str(self), self.verdict))


# ---------------------------------------------------------------------------
# Connection — mirrors JS meshConnectionService + BLEProtocolService
#
# JS CONNECTION MODEL (from APK reverse engineering):
#   Initial pairing  : BLE connect → full handshake (REGISTER(0) + key exchange
#                      + REGISTER(1)) → lamp enters GATEWAY+PRIVATE state permanently.
#   Reconnection     : BLE connect + startNotification ONLY.
#                      NO CMD_REGISTER, NO key exchange. The lamp retains its
#                      GATEWAY+PRIVATE state across BLE disconnects.
# ---------------------------------------------------------------------------


class FermobBLEConnection:
    """Manages a persistent BLE connection to one Fermob lamp."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        store: Store,
        light_type: str = LIGHT_TYPE_TW,
        idle_disconnect_delay: float | None = _IDLE_DISCONNECT_DELAY,
    ) -> None:
        self.hass = hass
        self._address = address
        self._store = store
        self.light_type = light_type  # "dw" | "tw"
        self._idle_disconnect_delay = idle_disconnect_delay
        self._client = None
        self._seq = 0

        # Crypto keys (persisted)
        self._pub = bytes(16)
        self._priv = bytes(16)
        self._nonce = bytes(16)
        self._addr_b2 = 0
        self._addr_b3 = 0
        self._keys_loaded = False
        self._have_keys = False  # True after successful pairing
        # Whether MODULE_INFO_GET has told us everything it can -- see
        # `_fetch_module_info_once`, which bounds the retries with the counter.
        self._module_info_read = False
        self._module_info_attempts = 0

        # What the lamp says it is (MODULE_INFO_GET). None until read once.
        self.module_type: int | None = None
        self.model: str | None = None
        self.on_module_info: Any = None  # (module_type, model) -> None

        # Runtime state
        self._connected = False  # BLE link is up
        self._ready = False  # post-connect setup complete, commands allowed
        self._idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

        # What the battery request established on the current link, or None when
        # nothing has been asked on it. `unpair()` reuses it rather than paying
        # for a second round trip -- see there.
        self._connect_verdict: BatteryVerdict | None = None

        # Last battery reading, None until the lamp reports one. A reported 0 is
        # a real 0; "never reported" must stay distinguishable from "empty".
        self.battery: Battery | None = None

        self._ack_queue: asyncio.Queue = asyncio.Queue()

        # Push subscribers. Lists with explicit removal, not single assignable
        # slots, because more than one entity wants each push and they are added
        # and removed independently of this object's lifetime -- see
        # `add_battery_listener`.
        self._battery_listeners: list[Callable[[Battery], None]] = []
        self._state_listeners: list[Callable[[bool, int, int], None]] = []
        self._availability_listeners: list[Callable[[bool], None]] = []

    # ------------------------------------------------------------------
    # Push subscriptions
    #
    # These were single assignable slots (`on_battery`, `on_state_change`).
    # Two entities want the battery push, so whichever registered second had to
    # *chain* onto whatever it found in the slot -- and nothing ever unchained,
    # because the entities had no removal hook. Knowing who was subscribed then
    # meant knowing the platform setup order, which is a poor thing to have to
    # reason about.
    #
    # No user-visible failure was ever demonstrated from that shape; this is a
    # defensive change to the HA idiom, not a fix. See
    # `docs/tech/ARCHITECTURE.md`.
    # ------------------------------------------------------------------

    def add_battery_listener(
        self, listener: Callable[[Battery], None]
    ) -> Callable[[], None]:
        """Subscribe to battery pushes. Returns a callable that unsubscribes.

        Hand the returned callable to `Entity.async_on_remove` so the
        subscription dies with the entity rather than outliving it.
        """
        self._battery_listeners.append(listener)

        def _remove() -> None:
            if listener in self._battery_listeners:
                self._battery_listeners.remove(listener)

        return _remove

    def add_availability_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Subscribe to check-in outcomes. Returns a callable that unsubscribes.

        The scheduled check-in is the only thing that talks to the lamp when the
        user is not; without this its verdict goes nowhere, because availability
        is written only on the command path. A lamp that has gone deaf would
        then keep reading *available* and *on* in the UI, indefinitely, while it
        sits dark -- which is precisely the failure this release is about.
        """
        self._availability_listeners.append(listener)

        def _remove() -> None:
            if listener in self._availability_listeners:
                self._availability_listeners.remove(listener)

        return _remove

    def add_state_listener(
        self, listener: Callable[[bool, int, int], None]
    ) -> Callable[[], None]:
        """Subscribe to light-state pushes. Returns a callable that unsubscribes."""
        self._state_listeners.append(listener)

        def _remove() -> None:
            if listener in self._state_listeners:
                self._state_listeners.remove(listener)

        return _remove

    def _notify(self, listeners: list, what: str, *args: Any) -> None:
        """Fan a push out to every subscriber, isolating their failures.

        Iterates a copy, because a listener may unsubscribe itself while being
        called. Each call is guarded so one broken subscriber cannot stop the
        others -- these run in the BLE notification callback, where an escaping
        exception would take the whole push with it. Logged at error level
        rather than swallowed, because a push path that goes quiet is invisible
        from the outside: the value stays plausible, just stale.
        """
        for listener in list(listeners):
            try:
                listener(*args)
            except Exception:
                _LOGGER.error(
                    "Fermob %s: %s listener failed",
                    self._address,
                    what,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Key persistence
    # ------------------------------------------------------------------

    async def _load_keys(self) -> bool:
        if self._keys_loaded:
            return self._have_keys
        self._keys_loaded = True
        data = await self._store.async_load()
        if data and all(k in data for k in ("pub", "priv", "nonce")):
            self._pub = bytes.fromhex(data["pub"])
            self._priv = bytes.fromhex(data["priv"])
            self._nonce = bytes.fromhex(data["nonce"])
            self._addr_b2 = data.get("addr_b2", 0)
            self._addr_b3 = data.get("addr_b3", 0)
            self.module_type = data.get("module_type")
            self.model = data.get("model")
            self._have_keys = True
            _LOGGER.debug("Fermob %s: keys loaded", self._address)
            return True
        return False

    async def is_paired(self) -> bool:
        """Whether a stored pairing exists, reading the store if it has to.

        For callers outside the connect path -- `async_unpair`, which has to tell
        "no keys, nothing to release" apart from "keys, but the lamp is out of
        range". Take `lock` first, like every other caller of `_load_keys()`.
        """
        return await self._load_keys()

    async def _save_keys(self) -> None:
        await self._store.async_save(
            {
                "pub": self._pub.hex(),
                "priv": self._priv.hex(),
                "nonce": self._nonce.hex(),
                "addr_b2": self._addr_b2,
                "addr_b3": self._addr_b3,
                "module_type": self.module_type,
                "model": self.model,
            }
        )
        _LOGGER.debug("Fermob %s: keys saved", self._address)

    # ------------------------------------------------------------------
    # BLE notification handler
    # ------------------------------------------------------------------

    def _dispatch_event(self, frame: bytes, resp_enc: int) -> None:
        """Decode a state push and hand it to whichever entity wants it."""
        try:
            pl = decode_fragment(frame, resp_enc, self._pub, self._priv, self._nonce)
        except Exception:  # malformed or undecodable frame
            _LOGGER.debug(
                "Fermob %s: undecodable EVENT frame %s",
                self._address,
                frame.hex(),
                exc_info=True,
            )
            return

        # Battery arrives as its own short push, not as part of DEVICE_DATA, so
        # it has to be handled before the device-data marker check below --
        # which would otherwise drop it as "not a state frame".
        battery = parse_battery(pl)
        if battery is not None:
            self.battery = battery
            _LOGGER.debug(
                "Fermob %s: battery %d%% charging=%s",
                self._address,
                battery.percent,
                battery.charging,
            )
            self._notify(self._battery_listeners, "battery", battery)
            return

        if len(pl) < 10 or pl[1] not in DEVICE_DATA_MARKERS:
            return
        record = parse_device_record(pl)
        if record is None:
            return

        # The marker separates live state from stale state, and it is the only
        # thing that does -- the two bodies are byte-identical. 146 is the lamp
        # volunteering a change as it happens; 147 is a stored record, and on an
        # H134 that record reported the lamp off while it was lit. Nothing sends
        # the query that produces a 147 any more, so this is a guard against
        # bringing one back, not a live code path.
        #
        # `stamped` is logged, never branched on: it is the only outside
        # evidence that DATETIME_SET reached the lamp, since a lamp whose clock
        # never started stamps every record it writes with the same low number.
        solicited = pl[1] == LMP_STATUS_DEVICE_DATA
        _LOGGER.debug(
            "Fermob %s: DEVICE_DATA %s is_on=%s ch1=%d ch2=%d stamped=%d",
            self._address,
            "record (stale, ignored)" if solicited else "push",
            record.is_on,
            record.ch1,
            record.ch2,
            record.timestamp,
        )
        if solicited:
            return

        self._notify(
            self._state_listeners, "state", record.is_on, record.ch1, record.ch2
        )

    def _notif_handler(self, sender, data: bytearray) -> None:
        frame = bytes(data)
        if len(frame) < 20:
            return
        h0 = frame[0]
        mt = (h0 >> 5) & 7

        if mt in STATE_PUSH_TYPES:
            # Not gated on on_state_change: a battery push is a state push too,
            # and it must still be dispatched when only the sensor cares.
            if self._ready:
                self._dispatch_event(frame, (h0 >> 3) & 3)
            else:
                # During handshake: stash so _wait_for_event() can see it
                self._ack_queue.put_nowait(frame)
            return

        self._ack_queue.put_nowait(frame)

    # ------------------------------------------------------------------
    # Frame send/receive
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) % 256
        return self._seq

    async def _send_frames(self, frames: list[bytes]) -> Ack:
        """Write BLE frames and wait for the matching CMD_ACK (mt=2, cmd==seq).

        `Ack.answered` separates the two ways `Ack.payload` can be None, which
        callers must not conflate: the lamp said nothing at all
        (`answered=False`), or it answered and refused (`answered=True`, and
        `Ack.error` carries the code). A refusal is proof the lamp is listening,
        and `request_battery` reads it as exactly that -- see the liveness check
        in `ensure_connected`. Reading a NAK as silence would make a lamp that
        declines one diagnostic command look dead.

        The one exception is `Ack.error` in `CRYPTO_REJECTION_ERRORS`, where the
        refusal is the lamp saying it cannot decrypt us at all -- see
        `_request_battery_verdict`.
        """
        my_seq = frames[0][1]
        for frame in frames:
            await self._client.write_gatt_char(CHAR_UUID, frame, response=False)
            if len(frames) > 1:
                await asyncio.sleep(0.05)

        deadline = asyncio.get_event_loop().time() + 3.0
        fragments: dict[int, bytes] = {}
        first_enc = 0
        LONG_START = {3, 4, 5}

        # State pushes that arrived while we were waiting, to be handled after
        # the loop. Putting them straight back on the queue and `continue`ing
        # re-reads the same frame immediately, which spins the event loop for
        # the full 3 s deadline whenever a push arrives before its ACK.
        deferred: list[bytes] = []

        def _drain() -> None:
            for pending in deferred:
                self._ack_queue.put_nowait(pending)
            deferred.clear()

        try:
            return await self._await_ack(
                my_seq, deadline, fragments, deferred, first_enc, LONG_START
            )
        finally:
            # In a finally, not on each return path: `decode_fragment` below can
            # raise on a malformed ACK, and anything still held here would be
            # dropped rather than handed back -- during pairing that can swallow
            # the post-REGISTER_END EVENT that `_wait_for_event` is waiting for.
            _drain()

    async def _await_ack(
        self,
        my_seq: int,
        deadline: float,
        fragments: dict[int, bytes],
        deferred: list[bytes],
        first_enc: int,
        LONG_START: set[int],
    ) -> Ack:
        """The ACK-matching loop of `_send_frames`. Split out so its caller can
        re-queue deferred pushes in a `finally`."""
        seq_total: int | None = None
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                _LOGGER.warning(
                    "Fermob %s: ACK timeout seq=%02x", self._address, my_seq
                )
                return Ack(None, 0, False, None)
            try:
                frame = await asyncio.wait_for(self._ack_queue.get(), timeout=rem)
            except TimeoutError:
                _LOGGER.warning(
                    "Fermob %s: ACK timeout seq=%02x", self._address, my_seq
                )
                return Ack(None, 0, False, None)

            if len(frame) < 20:
                continue
            h0 = frame[0]
            mt = (h0 >> 5) & 7
            ft = h0 & 7
            cmd = frame[1]
            resp_enc = (h0 >> 3) & 3

            _LOGGER.debug(
                "Fermob %s ← mt=%d ft=%d enc=%d cmd=%02x seq=%02x raw=%s",
                self._address,
                mt,
                ft,
                resp_enc,
                cmd,
                my_seq,
                frame.hex(),
            )

            # A state push arrived while we were waiting for an ACK: re-route it
            if mt in STATE_PUSH_TYPES:
                if self._ready:
                    self._dispatch_event(frame, resp_enc)
                else:
                    deferred.append(frame)
                continue

            if mt != MSG_CMD_ACK or cmd != my_seq:
                _LOGGER.debug(
                    "Fermob %s: ignored frame (mt=%d cmd=%02x expected=%02x)",
                    self._address,
                    mt,
                    cmd,
                    my_seq,
                )
                continue

            if not fragments:
                first_enc = resp_enc
            frag = decode_fragment(frame, resp_enc, self._pub, self._priv, self._nonce)
            if ft in LONG_START:
                seq_total = frame[3]
                fragments[frame[2]] = frag
            elif ft == 6:
                fragments[frame[2]] = frag
            else:
                fragments[0] = frag
                seq_total = 1
            if seq_total is not None and len(fragments) >= seq_total:
                break

        if not fragments:
            return Ack(None, first_enc, False, None)
        pl = b"".join(fragments[i] for i in sorted(fragments))

        # A rejected command still arrives as a correctly-sequenced CMD_ACK, so
        # without this check a NAK reads as success and whatever the caller
        # parses out of the body is garbage -- silently storing bad keys, in the
        # handshake's case. It is still an answer, hence `answered=True`.
        err = ack_error(pl)
        if err is not None:
            _LOGGER.warning(
                "Fermob %s: command seq=%02x rejected: %s",
                self._address,
                my_seq,
                error_name(err),
            )
            return Ack(None, first_enc, True, err)

        return Ack(pl, first_enc, True, None)

    async def _send(self, enc: int, payload: list[int]) -> tuple[bytes | None, int]:
        """`_send_frames` without the liveness flag, for callers that want a body."""
        sid = self._next_seq()
        if len(payload) <= 15:
            frames = [
                build_short(
                    MSG_CMD, enc, payload, sid, self._pub, self._priv, self._nonce
                )
            ]
        else:
            frames = build_long(enc, payload, sid, self._pub, self._priv, self._nonce)
        ack = await self._send_frames(frames)
        return ack.payload, ack.enc

    async def _wait_for_event(
        self, timeout: float = 0.5
    ) -> tuple[bool, int, int] | None:
        """Wait for the lamp's post-REGISTER_END state EVENT (mt=4).

        After REGISTER(1) the lamp emits its current state as an EVENT ~200-300 ms
        later. Capturing it confirms the lamp entered GATEWAY mode, and it is the
        only state the lamp ever volunteers -- no query returns usable state.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                return None
            try:
                frame = await asyncio.wait_for(self._ack_queue.get(), timeout=rem)
            except TimeoutError:
                return None
            if len(frame) < 20:
                continue
            h0 = frame[0]
            if (h0 >> 5) & 7 not in STATE_PUSH_TYPES:
                continue  # discard stray ACKs
            try:
                pl = decode_fragment(
                    frame, (h0 >> 3) & 3, self._pub, self._priv, self._nonce
                )
            except Exception:
                _LOGGER.debug(
                    "Fermob %s: undecodable EVENT frame %s",
                    self._address,
                    frame.hex(),
                    exc_info=True,
                )
                continue
            if len(pl) >= 10 and pl[1] in DEVICE_DATA_MARKERS:
                record = parse_device_record(pl)
                if record is not None:
                    _LOGGER.debug(
                        "Fermob %s: EVENT after REGISTER_END %s", self._address, record
                    )
                    return record.is_on, record.ch1, record.ch2

    # ------------------------------------------------------------------
    # Initial pairing handshake (first-time only, mirrors JS startPairing)
    # ------------------------------------------------------------------

    async def _pairing_handshake(self) -> None:
        """Full pairing sequence — run ONCE, when we have no stored keys.

        JS flow (startPairing → setPublicKey → getNonce → setPublicEncryptionMode
                 → setPrivateKey → setPrivateEncryptionMode → sendRegisterEnd).
        """
        _LOGGER.warning("Fermob %s: fresh pairing", self._address)

        # Step 1: probe — confirm lamp is in NONE (fresh / factory-reset)
        _probe_pl, probe_enc = await self._send(ENCRYPT_NONE, [2, CMD_REGISTER, 0])
        if probe_enc == ENCRYPT_PRIVATE:
            raise RuntimeError(
                "Lamp is in PRIVATE mode but no stored keys found. "
                "Factory-reset the lamp (hold button 10 s) and delete "
                ".storage/fermob_* before retrying."
            )

        # Step 2: get the lamp's public key
        pl, _ = await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_AUTHKEY_GET, 0])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GET failed")
        self._pub = bytes(pl[3:19])

        # Step 3: generate the nonce
        pl, _ = await self._send(ENCRYPT_NONE, [1, CMD_CRYPT_NONCE_GENERATE])
        if not pl or len(pl) < 19:
            raise RuntimeError("NONCE_GENERATE failed")
        self._nonce = bytes(pl[3:19])

        # Step 4: switch to PUBLIC encryption
        await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_SET, ENCRYPT_PUBLIC])

        # Step 5: generate the private key
        pl, _ = await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_AUTHKEY_GEN, 1])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GEN failed")
        self._priv = bytes(pl[3:19])

        # Step 6: switch to PRIVATE encryption
        await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_SET, ENCRYPT_PRIVATE])

        # Step 7: get the short address (and, for free, what the lamp says it is)
        pl, _ = await self._send(ENCRYPT_PRIVATE, [1, CMD_MODULE_INFO_GET])
        if pl:
            info = parse_module_info(pl)
            # Only when the reply actually carried one. `_save_keys()` two steps
            # below persists whatever is in memory, so a reply without the 0xb1
            # TLV would write addr 0x0000 over a good stored record -- and every
            # addressed frame after that, the battery request that is our only
            # liveness signal included, would go to the wrong place.
            if info.addr_b2 or info.addr_b3:
                self._addr_b2, self._addr_b3 = info.addr_b2, info.addr_b3
            self._store_module_info(info)

        # Step 8: optional device info
        await self._send(ENCRYPT_PRIVATE, [1, CMD_DEVICE_INFO_GET])

        # Persist keys before REGISTER_END so they survive a missing EVENT
        await self._save_keys()
        self._have_keys = True
        # In sync with `_have_keys`, or the next `_load_keys()` re-reads the
        # store and overwrites everything the handshake just put in memory --
        # harmless only for as long as all of it happens to be persisted.
        self._keys_loaded = True

        # Step 9: REGISTER_END → lamp enters GATEWAY mode
        await self._send(ENCRYPT_PRIVATE, [2, CMD_REGISTER, 1])

        # Step 9b: start the lamp's clock, exactly where the app does it -- in
        # the success handler of REGISTER_END. A lamp paired without this stamps
        # every record it writes with a clock that never started.
        await self.set_module_time()

        # Step 10: wait for the state EVENT the lamp emits on entering GATEWAY
        # mode. This is a confirmation + timing gate (it mirrors the app's 100 ms
        # settle after setMeshConnection); the state it carries is the lamp's
        # *pre-command* state and is about to be overwritten by the command that
        # triggered this connection, so it is only logged.
        state = await self._wait_for_event(timeout=0.5)
        if state is None:
            _LOGGER.debug("Fermob %s: no EVENT after REGISTER_END", self._address)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        self._connected = False
        self._ready = False
        # Whatever the battery request proved, it proved it about a link that no
        # longer exists.
        self._connect_verdict = None
        if self._client:
            try:
                await self._client.stop_notify(CHAR_UUID)
            except Exception:  # the link may already be gone
                _LOGGER.debug(
                    "Fermob %s: stop_notify failed", self._address, exc_info=True
                )
            try:
                await self._client.disconnect()
            except Exception:  # the link may already be gone
                _LOGGER.debug(
                    "Fermob %s: disconnect failed", self._address, exc_info=True
                )
            self._client = None
        _LOGGER.debug("Fermob %s: disconnected", self._address)

    async def async_shutdown(self) -> None:
        """Release the BLE link and cancel the idle timer.

        Registered via entry.async_on_unload, so an entry unload *or reload*
        (e.g. after changing an option) closes the connection instead of leaking
        an open BleakClient and a pending idle task.
        """
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        async with self.lock:  # wait for any in-flight command to finish
            await self.disconnect()

    def _schedule_idle_disconnect(self) -> None:
        """(Re)arm the idle timer, or leave the link up if there is no timeout."""
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

        delay = self._idle_disconnect_delay
        if delay is None:
            return

        async def _idle() -> None:
            await asyncio.sleep(delay)
            async with self.lock:
                _LOGGER.debug("Fermob %s: idle timeout → disconnect", self._address)
                await self.disconnect()

        self._idle_task = asyncio.ensure_future(_idle())

    async def _open_link(self, max_attempts: int = CONNECT_ATTEMPTS_BACKGROUND) -> None:
        """Open a raw BLE link and start notifications. No crypto, no state.

        Split out of `ensure_connected` because pairing now needs to do it
        twice -- see the reconnect there for why.
        """
        # A session that died without going through disconnect() -- a BLE proxy
        # reboot, an adapter reset -- leaves a client here with is_connected
        # False. Overwriting it below would strand it: never closed, never
        # collected, and still holding one of the proxy's three slots.
        if self._client is not None:
            await self.disconnect()

        device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            raise RuntimeError(f"Fermob BLE device not found: {self._address}")

        _LOGGER.debug("Fermob %s: connecting…", self._address)
        self._client = await establish_connection(
            BleakClient, device, self._address, max_attempts=max_attempts
        )

        try:
            # Flush any stale frames
            while not self._ack_queue.empty():
                self._ack_queue.get_nowait()

            await self._client.start_notify(CHAR_UUID, self._notif_handler)
        except Exception:
            # A link with no notifications is useless, and leaving the client on
            # the instance leaks it: `async_check_in` swallows what we raise, so
            # the next connect would `establish_connection` on top of this one.
            await self.disconnect()
            raise

    async def _lamp_still_paired(self) -> bool:
        """Ask the lamp which encryption mode it is in, and believe only a yes.

        A lamp that has been factory-reset is back in NONE mode and can no
        longer decrypt anything we send, but it still advertises, still accepts
        a BLE link, and still says nothing about it -- so with keys in
        `.storage` the reconnect path would hand back a link that looks perfect
        and discards every frame. Permanently: nothing else in the integration
        ever re-examines stored keys, and the only recovery was deleting them by
        hand. This is the inverse of the handshake's step-1 probe, which covers
        only the other direction (lamp PRIVATE, us with no keys).

        The probe is the same unencrypted REGISTER query, and the answer we care
        about is the mode the lamp replies *in*, not its body.

        **Only ever sent to a lamp that has already failed to answer**, never on
        a healthy connect -- see `ensure_connected`. `REGISTER(0)` is the first
        frame of the pairing sequence, and what it does to a lamp that is already
        registered is not known beyond "it answers": the protocol is
        reverse-engineered, Fermob document none of it, and the vendor app has
        never been observed sending it to a lamp it owns. Putting it on the happy
        path would mean sending a pairing frame to a working lamp every time the
        link comes up, on a guess. It stays behind a failure, where the lamp is
        useless anyway and a surprise is worth the diagnosis.

        Silence is read as "still paired", deliberately. A probe can time out
        because the lamp is at the edge of range, and re-pairing on that
        evidence would both flash the lamp unattended and throw away keys that
        were still good. Only a lamp that positively answers in some mode other
        than PRIVATE is treated as reset.
        """
        # `_send_frames` rather than `_send`, for the `answered` flag: a lamp
        # that *refuses* the probe has still told us which mode it is in, and
        # `_send` drops that distinction. Reading a refusal as silence would
        # classify a reset lamp as still-ours, and since the caller then raises
        # `LampNotAnswering` on every subsequent connect, it would never be
        # re-paired -- a permanent dead end reached by the code meant to avoid
        # one.
        payload = [2, CMD_REGISTER, 0]
        frame = build_short(
            MSG_CMD,
            ENCRYPT_NONE,
            payload,
            self._next_seq(),
            self._pub,
            self._priv,
            self._nonce,
        )
        try:
            ack = await self._send_frames([frame])
        except Exception:
            # Any transport failure here says nothing about pairing.
            _LOGGER.debug(
                "Fermob %s: pairing probe did not complete",
                self._address,
                exc_info=True,
            )
            return True
        if not ack.answered:
            _LOGGER.debug("Fermob %s: pairing probe unanswered", self._address)
            return True
        return ack.enc == ENCRYPT_PRIVATE

    async def discard_keys(self) -> None:
        """Forget the stored pairing, in memory and on disk.

        Irreversible, so `fermob.unpair` is the only caller: there the lamp has
        been told and the keys are genuinely dead. The re-pair path inside
        `ensure_connected` deliberately uses `_forget_keys_in_memory()` instead
        -- see there.
        """
        await self._store.async_remove()
        self._forget_keys_in_memory()
        self._keys_loaded = True  # known state: no keys, do not re-read the store
        _LOGGER.warning("Fermob %s: stored keys discarded", self._address)

    def _forget_keys_in_memory(self) -> None:
        """Drop the in-memory keys, leaving the stored ones alone.

        What the re-pair path needs, and all it needs: `_pairing_handshake()`
        ends in `_save_keys()`, which overwrites the record anyway. Deleting it
        first would buy nothing and cost everything -- if the probe misread the
        lamp, or the handshake fails halfway, the old keys are gone and the lamp
        is still registered to us. That is the unrecoverable state, and nothing
        would retry: `async_check_in` short-circuits on `_load_keys()`.

        `_keys_loaded` is cleared too, so a failed re-pair re-reads the record
        that is still on disk rather than assuming there is none.

        The short address is deliberately *not* cleared. It is derived from the
        lamp's MAC, a factory reset does not change it, and it is not key
        material -- but the handshake only learns it if step 7's MODULE_INFO_GET
        is answered, while `_save_keys()` runs either way. Zeroing it here meant
        one dropped reply during a re-pair persisted 0x0000 over a good record,
        and the light never came back.

        `_module_info_read` *is* cleared, with its retry budget: the lamp is
        about to be paired again, and the latch would otherwise stop
        `_fetch_module_info_once()` from ever re-reading what the new handshake
        failed to get.
        """
        self._pub = bytes(16)
        self._priv = bytes(16)
        self._nonce = bytes(16)
        self._have_keys = False
        self._keys_loaded = False
        self._module_info_read = False
        self._module_info_attempts = 0

    async def ensure_connected(
        self,
        allow_pairing: bool = True,
        max_attempts: int = CONNECT_ATTEMPTS_BACKGROUND,
    ) -> None:
        """Ensure an authenticated BLE connection is up.

        Runs the full pairing handshake on first use and a plain BLE reconnect
        afterwards; the lamp keeps its GATEWAY+PRIVATE state across disconnects.

        At most two passes, and the second one always pairs. A reconnect ends
        with a battery request, which is the only frame the lamp ever
        acknowledges -- if that goes unanswered (twice; one dropped ACK is not
        evidence of anything) the stored keys are suspect, so the lamp is asked
        whether it still holds them, and a lamp that says no is re-paired.
        Everything else about a reconnect is unchanged and costs nothing extra:
        the probe runs only after a failure.

        `allow_pairing=False` forbids the handshake outright and raises instead.
        Pairing makes the lamp flash and takes ownership of it, so it belongs to
        something the user just did -- `async_check_in` passes False, because a
        3 a.m. timer must not re-register a lamp its owner deliberately reset to
        hand back to the Fermob app.

        `max_attempts` is the connect budget, and defaults to the background one
        because that is the safe direction to be wrong in: a caller who forgets
        it waits longer, rather than giving up on a lamp that was there. Callers
        a user is waiting on pass `CONNECT_ATTEMPTS_INTERACTIVE`.
        """
        have_keys = await self._load_keys()

        if self._connected and self._client and self._client.is_connected:
            # Nothing was asked on this call, so there is no fresh verdict for
            # `unpair()` to reuse. Leaving a stale one here would let it skip the
            # one check it has.
            self._connect_verdict = None
            self._schedule_idle_disconnect()
            return

        if not have_keys and not allow_pairing:
            raise RuntimeError(
                f"Fermob {self._address}: not paired, and pairing is not allowed here"
            )

        for _ in range(2):
            # Reset before opening: a link that dropped without going through
            # disconnect() would otherwise leave `_ready` set from the previous
            # session, and the handshake below needs its state pushes queued
            # rather than dispatched to entities that cannot decode them.
            self._ready = False
            self._connected = False
            await self._open_link(max_attempts)

            if have_keys:
                # The lamp retains GATEWAY+PRIVATE state across BLE disconnects,
                # so no crypto handshake is needed. It also emits no spontaneous
                # EVENT here, and neither state-read command returns anything
                # usable, so there is no state to read back.
                _LOGGER.debug(
                    "Fermob %s: reconnected (lamp keeps GATEWAY state)", self._address
                )
                await self._fetch_module_info_once()
            else:
                await self._pairing_handshake()

                # ...and then start again on a fresh link. REGISTER_END puts the
                # lamp into GATEWAY mode, and it stops honouring the link it was
                # paired on: reproduced on an H134, where every command after
                # pairing was accepted by Home Assistant and ignored by the lamp
                # until the integration was reloaded. A reload is a fresh
                # connect, so pairing does that itself rather than asking the
                # user to.
                _LOGGER.debug(
                    "Fermob %s: reconnecting after pairing (lamp is now a gateway)",
                    self._address,
                )
                await self.disconnect()
                await self._open_link(max_attempts)

            self._ready = True
            self._connected = True
            _LOGGER.debug("Fermob %s: ready", self._address)
            await self.set_module_time()
            verdict = await self._request_battery_verdict()

            if verdict is BatteryVerdict.SILENT:
                # One dropped ACK is a marginal link, not a diagnosis.
                # Everything below this point is expensive or destructive -- a
                # pairing frame, possibly a re-pair, otherwise a failed connect
                # that takes the light entity unavailable -- so none of it
                # happens on a single miss.
                #
                # The retry has to yield a *verdict*, not a bool. It used to call
                # `request_battery()`, which flattens KEYS_REJECTED into False:
                # a lamp that answered "I cannot decrypt you" on the second try
                # then fell through to the REGISTER(0) probe, which is exactly
                # the path the KEYS_REJECTED branch below exists to keep it off.
                _LOGGER.debug(
                    "Fermob %s: battery request unanswered, retrying once",
                    self._address,
                )
                verdict = await self._request_battery_verdict()

            self._connect_verdict = verdict

            if verdict is BatteryVerdict.ANSWERED:
                break

            if verdict is BatteryVerdict.KEYS_REJECTED:
                # The lamp said so itself, so there is nothing to infer and no
                # probe to send: `REGISTER(0)` could only reach the same
                # conclusion less certainly, and it is a pairing frame. This is
                # the factory-reset case arriving faster, and with better
                # evidence, than the probe path below ever gave it.
                if not have_keys:
                    await self.disconnect()
                    raise LampNotAnswering(
                        f"Fermob {self._address}: paired, and the lamp then "
                        "rejected the keys it had just been given",
                        BatteryVerdict.KEYS_REJECTED,
                    )
                if not allow_pairing:
                    await self.disconnect()
                    raise LampNotAnswering(
                        f"Fermob {self._address}: lamp no longer holds our keys "
                        "-- turn the light on in Home Assistant to re-pair it",
                        BatteryVerdict.KEYS_REJECTED,
                    )
                _LOGGER.warning(
                    "Fermob %s: lamp is no longer paired with us (factory "
                    "reset?) -- re-pairing",
                    self._address,
                )
                self._forget_keys_in_memory()
                have_keys = False
                await self.disconnect()
                continue

            # Silent, twice.
            #
            # A pass that just paired gets no probe and no second pass: the keys
            # are seconds old, so nothing is wrong with them, and re-pairing a
            # lamp we have just paired would be a loop.
            #
            # It does still have to fail here. The handshake's ten ACKs happened
            # on the *pre-REGISTER_END* link, which is exactly the link the lamp
            # stops honouring -- that is why this pass reconnected. Nothing on
            # the new link has been acknowledged by anything, so "we just paired"
            # is not evidence the session works, and accepting it as evidence
            # would report the reproduced post-pairing failure as success.
            if not have_keys:
                # Say what actually happened. The handshake completed -- keys
                # saved, REGISTER_END sent -- so the lamp *is* registered to us,
                # and a message reading like "pairing failed" would send the user
                # to a 10-second factory reset they do not need. Retrying the
                # command takes the reconnect path and will work if the link does.
                await self.disconnect()
                raise LampNotAnswering(
                    f"Fermob {self._address}: paired, but the lamp did not "
                    "acknowledge on the new link -- it is registered to Home "
                    "Assistant, so retry the command rather than resetting it",
                    BatteryVerdict.SILENT,
                )

            if not allow_pairing:
                await self.disconnect()
                raise LampNotAnswering(
                    f"Fermob {self._address}: silent on a link that just came "
                    "up, and this caller may not send the pairing probe that "
                    "would say why",
                    BatteryVerdict.SILENT,
                )

            _LOGGER.warning(
                "Fermob %s: no answer on a link that just came up -- asking the "
                "lamp whether it still holds our keys",
                self._address,
            )
            if await self._lamp_still_paired():
                # It is ours, and it is not talking. Re-pairing would not fix
                # that, and there is nothing else left to try -- so fail rather
                # than hand back a link the caller will report as healthy. That
                # is the whole bug: `send_led` cannot fail, so a session nobody
                # rejected here is one the entity will call available while the
                # lamp sits dark. The check-in retries on its own schedule.
                await self.disconnect()
                raise LampNotAnswering(
                    f"Fermob {self._address}: still holds our keys and is still "
                    "not answering -- connected, but nothing gets through",
                    BatteryVerdict.SILENT,
                )

            _LOGGER.warning(
                "Fermob %s: lamp is no longer paired with us (factory reset?) "
                "-- re-pairing",
                self._address,
            )
            # In memory only. The handshake's own `_save_keys()` replaces the
            # stored record, and if it never gets that far the old one is still
            # there to try again with -- see `_forget_keys_in_memory`.
            self._forget_keys_in_memory()
            have_keys = False
            await self.disconnect()

        self._schedule_idle_disconnect()

    # ------------------------------------------------------------------
    # What the lamp says it is
    # ------------------------------------------------------------------

    def _store_module_info(self, info: ModuleInfo) -> None:
        """Record a reported module_type / model and announce any change."""
        changed = (
            info.module_type is not None and info.module_type != self.module_type
        ) or (info.model is not None and info.model != self.model)
        if info.module_type is not None:
            self.module_type = info.module_type
        if info.model is not None:
            self.model = info.model
        if not changed:
            return

        _LOGGER.info(
            "Fermob %s: reports module_type=%s model=%s",
            self._address,
            self.module_type,
            self.model,
        )
        if self.on_module_info:
            self.on_module_info(self.module_type, self.model)

    async def _fetch_module_info_once(self) -> None:
        """Read MODULE_INFO_GET on reconnect, but only until it has answered.

        Lamps paired before this existed never ran the handshake step that reads
        it, so without this their family stays a name guess forever. The lamp
        does answer this command in GATEWAY mode (unlike DEVICE_DATA_GET), and
        the result is persisted, so this costs one extra round trip per install
        rather than one per connect.

        Diagnostic only -- a failure must never stop the light from working.
        """
        # "Once" means once the answer has left nothing to come back for, or once
        # the stored record already carries both things it returns. Latching on
        # the address being non-zero alone would never be satisfied by a lamp
        # whose short address genuinely is 0x0000: it would re-read, and re-write
        # the key store, on every single reconnect.
        if self._module_info_read:
            return
        if self.module_type is not None and (self._addr_b2 or self._addr_b3):
            return
        self._module_info_attempts += 1
        try:
            pl, _ = await self._send(ENCRYPT_PRIVATE, [1, CMD_MODULE_INFO_GET])
        except Exception as err:
            # Broad on purpose: any transport failure here is non-fatal.
            _LOGGER.debug("Fermob %s: MODULE_INFO_GET failed: %s", self._address, err)
            return
        if not pl:
            _LOGGER.debug("Fermob %s: MODULE_INFO_GET not answered", self._address)
            return
        info = parse_module_info(pl)
        # The short address too, not just the family. Only the handshake's step 7
        # ever set it, so a pairing whose MODULE_INFO_GET went unanswered left it
        # at 0 for good -- and every addressed frame after that, the battery
        # request included, goes to the wrong place. That used to cost an
        # unavailable battery sensor; now that the battery ACK is the liveness
        # signal it would cost the whole light, permanently.
        if info.addr_b2 or info.addr_b3:
            self._addr_b2, self._addr_b3 = info.addr_b2, info.addr_b3
        self._store_module_info(info)
        # Latch only once there is an address, because latching on any answer at
        # all disarmed the retry above: a reply carrying the module type but no
        # 0xb1 short-address TLV left the address at 0 *and* stopped anything from
        # asking again. Bounded, so a lamp whose address genuinely is 0x0000 costs
        # a few extra round trips rather than one per reconnect forever.
        if (
            self._addr_b2
            or self._addr_b3
            or self._module_info_attempts >= _MODULE_INFO_MAX_READS
        ):
            self._module_info_read = True
        if self._have_keys:
            await self._save_keys()

    async def async_check_in(self) -> None:
        """Reconnect if the link dropped, and refresh the battery reading.

        Two jobs, and with the link now held open the first is the important
        one. Nothing else notices an unexpected disconnect -- a BLE proxy
        rebooting for a firmware update, say -- so without this the entity would
        keep showing confidently stale state until someone next touched the
        light. Reconnecting is the whole reason this runs as often as it does.

        The second is the battery, which is why this existed in the first place:
        the lamp reports its level only when asked.

        No light command is involved, and connecting cannot change what the lamp
        is doing, so a check-in is invisible -- the vendor app polls the same
        command on a timer with every lamp dark, and its connect routine sends
        nothing else either.

        Takes the command lock, because `ensure_connected` is only ever safe
        under it -- a check-in landing mid-transition waits for the in-flight
        command rather than interleaving frames with it. The key load is inside
        the lock too: the re-pair path clears `_keys_loaded` and then spends
        seconds in the handshake, so an unlocked `_load_keys()` firing in that
        window would re-read the dead pre-reset record straight over the keys
        being negotiated, and the lamp would end up registered to a key Home
        Assistant cannot reproduce.

        When the link is already up it asks explicitly instead of relying on the
        connect path, which would not run: a lamp held connected for days would
        otherwise keep the reading it happened to take at connect time.

        Never raises. A lamp out of range or switched off at the socket is the
        normal case for a balcony lamp, not an error -- it must leave the last
        known level in place rather than clearing it or marking the entity
        unavailable, since the reading is explicitly "as of last contact".

        It does, however, *report* the outcome: swallowing the exception is not
        the same as pretending it did not happen. Subscribers are told whether
        the lamp answered, which is how the light entity's availability changes
        without anyone pressing a switch -- see `add_availability_listener`.

        And it never pairs (`allow_pairing=False`). Pairing flashes the lamp and
        takes ownership of it, which is not something a 3 a.m. timer may decide:
        the lamp may have been factory-reset on purpose, to hand it back to the
        Fermob app.
        """
        async with self.lock:
            if not await self._load_keys():
                _LOGGER.debug("Fermob %s: check-in skipped, not paired", self._address)
                return

            connected = bool(
                self._connected and self._client and self._client.is_connected
            )
            # Set once we have *proved* the session was dead. From then on any
            # failure means unavailable, even a routine-looking one: "could not
            # reach it" only excuses the entity while the last thing we knew was
            # that the lamp was fine.
            proven_dead = False
            try:
                await self.ensure_connected(allow_pairing=False)
                if connected:
                    # ensure_connected already asked on a fresh connect; this is
                    # the branch where it returned early.
                    await self.set_module_time()
                    if not await self.request_battery():
                        # The link is up by every measure available to us and
                        # the lamp is not answering on it, which is the failure
                        # holding the link open made permanent: `is_connected`
                        # stays True, `send_led` is a write-without-response and
                        # cannot fail, so nothing else would ever notice. Before
                        # 0.8.0 the 30 s idle disconnect repaired this by
                        # accident after every command; this is what replaces it.
                        _LOGGER.warning(
                            "Fermob %s: no answer over a link that looks up "
                            "-- reconnecting",
                            self._address,
                        )
                        proven_dead = True
                        await self.disconnect()
                        await self.ensure_connected(allow_pairing=False)
            except LampNotAnswering as err:
                # Reached it, and it is not usable. This is the one outcome worth
                # reporting.
                #
                # Log the exception, not a summary of it. Two of
                # `ensure_connected`'s five messages reach this path -- the
                # `not allow_pairing` pair, the rest needing `have_keys` false or
                # pairing allowed -- and one of the two is the only place the
                # user is ever told how to recover ("turn the light on in Home
                # Assistant to re-pair it"). Flattening both to "reachable but
                # not answering" threw that away, and described a lamp that had
                # answered `CRYPT_MSG`, clearly and usefully, as not answering.
                # No address prefix: every `LampNotAnswering` message already
                # carries one.
                _LOGGER.warning("%s", err)

                # And a factory-reset lamp must be reported *available*, which
                # looks backwards and is not. Unavailable means "commands will
                # not work". Here exactly one command will, and it is the
                # documented recovery: turning the light on re-pairs the lamp.
                #
                # Reported, not merely left alone. Suppressing the `False` is not
                # enough, because the entity may already be unavailable -- the
                # command path writes that on any failure, and so does the
                # `proven_dead` branch above. The realistic order is exactly
                # that: the lamp goes quiet, the entity greys out, the owner
                # reacts by factory-resetting it (which the README suggests for
                # several symptoms), and from then on every check-in returns
                # KEYS_REJECTED against an entity nothing can lift. Same dead
                # end, reached the long way round.
                #
                # Reporting it unavailable does not merely overstate the problem,
                # it removes the cure. Home Assistant silently drops every
                # entity-service call to an unavailable entity
                # (`helpers/service.py`: `if not entity.available: continue`), so
                # neither `light.turn_on` nor `fermob.unpair` ever arrives -- and
                # the check-in may not pair. The lamp is then stuck until someone
                # reloads the integration, which nothing tells them to do.
                # Observed on an H134, 2026-08-06.
                #
                # Every other verdict is genuinely unusable, and still greys out.
                reachable = err.verdict is BatteryVerdict.KEYS_REJECTED
                self._notify(self._availability_listeners, "availability", reachable)
                return
            except Exception:
                # Broad on purpose: out of range, adapter busy, lamp asleep --
                # all of it is routine here and none of it is worth a warning.
                #
                # And explicitly NOT an availability change. A balcony lamp is
                # out of range for whole seasons, and in on-demand mode the next
                # check-in is six hours away -- so reporting unavailable here
                # would grey the entity out for the rest of the day over one
                # missed advertisement, for a lamp that would answer a command
                # perfectly well. Leaving it alone is what the "as of last
                # contact" contract means.
                #
                # Unless we already knew: if the recovery reconnect is what
                # failed, the session it was replacing had already gone unanswered
                # on an open link. Staying quiet there would leave the entity
                # reading available and on for up to another interval, which is
                # the exact failure this release is about.
                if proven_dead:
                    _LOGGER.warning(
                        "Fermob %s: lost a dead session and could not get it back",
                        self._address,
                    )
                    self._notify(self._availability_listeners, "availability", False)
                    return
                _LOGGER.debug(
                    "Fermob %s: check-in did not reach the lamp",
                    self._address,
                    exc_info=True,
                )
                return

        self._notify(self._availability_listeners, "availability", True)

    # ------------------------------------------------------------------
    # Lamp commands
    # ------------------------------------------------------------------

    async def request_battery(self) -> bool:
        """Ask the lamp for its charge. True only if the session is usable.

        The simple form of `_request_battery_verdict`, for callers that only
        need "can I talk to this lamp" -- the check-in and `unpair`.
        """
        return await self._request_battery_verdict() is BatteryVerdict.ANSWERED

    async def _request_battery_verdict(self) -> BatteryVerdict:
        """Ask the lamp for its state of charge, and classify what came back.

        The ACK carries nothing but a success code -- the value follows as a
        separate STATUS push, which `_dispatch_event` picks up. So this returns
        as soon as the request is acknowledged and does *not* wait for the
        reading; in practice the push arrives in the same millisecond.

        That acknowledgement is the only one this integration ever receives on a
        live link -- every other frame we send is fire-and-forget -- which makes
        it the only evidence available about the session at all.

        Three outcomes, and the third was learned the hard way. A refusal
        normally proves the lamp is listening... unless the refusal is
        `CRYPT_MSG`, which is the lamp saying it cannot decrypt us. A lamp
        factory-reset behind our back answers exactly that, rather than going
        silent (observed on an H134, 2026-08-06) -- so reading "any answer" as a
        healthy session made the reset undetectable, and the light went on
        reporting success into a lamp that could not read a word.

        Never raises: a lamp that will not answer must not break the connect
        path, it must just leave the sensor unknown.
        """
        payload = build_battery_request(self._addr_b2, self._addr_b3)
        sid = self._next_seq()
        frame = build_short(
            MSG_CMD,
            ENCRYPT_PRIVATE,
            payload,
            sid,
            self._pub,
            self._priv,
            self._nonce,
            b2=self._addr_b2,
            b3=self._addr_b3,
            addressed=True,
        )
        try:
            ack = await self._send_frames([frame])
        except Exception:
            _LOGGER.debug(
                "Fermob %s: battery request failed", self._address, exc_info=True
            )
            return BatteryVerdict.SILENT
        if not ack.answered:
            return BatteryVerdict.SILENT
        if ack.error in CRYPTO_REJECTION_ERRORS:
            _LOGGER.warning(
                "Fermob %s: lamp rejected our keys (%s)",
                self._address,
                error_name(ack.error),
            )
            return BatteryVerdict.KEYS_REJECTED
        # Any other refusal is still proof it is listening.
        return BatteryVerdict.ANSWERED

    async def set_module_time(self) -> None:
        """Start (or re-sync) the lamp's own clock — JS `setModuleTime`.

        The app sends this at the end of pairing and in the success handler of
        each of its state reads, so its every read re-dates the lamp. This
        integration never sent it at all, and an H134 paired by it stamps every
        record it stores `37` -- a clock that never started. Nothing here reads
        those records back, but the lamp keeps them for the vendor app, and
        matching the app costs one unacknowledged frame per connection.

        FIRE, like the app: there is no reply, and none is waited for.

        Never raises. The clock is a nicety; the light has to work without it.
        """
        payload = build_datetime_set_payload(local_time_seconds(dt_util.now()))
        sid = self._next_seq()
        pkt = build_short(
            MSG_FIRE,
            ENCRYPT_PRIVATE,
            payload,
            sid,
            self._pub,
            self._priv,
            self._nonce,
            b2=self._addr_b2,
            b3=self._addr_b3,
            addressed=True,
        )
        _LOGGER.debug("Fermob %s →DATETIME_SET %s", self._address, pkt.hex())
        try:
            await self._client.write_gatt_char(CHAR_UUID, pkt, response=False)
        except Exception:
            _LOGGER.debug(
                "Fermob %s: DATETIME_SET failed", self._address, exc_info=True
            )

    # There is deliberately no state-read method here, and this is settled
    # rather than open. Both candidate commands were tried on an H134 and
    # neither yields usable state -- see `docs/domain/LINKIO-PROTOCOL.md` for
    # the traces. In short:
    #
    #   * `DEVICE_DATA_GET` (66) is refused with error 18, even when sent with
    #     the app's byte-exact body. Why is unexplained; the module-role and
    #     payload-length theories are both ruled out in the doc.
    #   * `DEVICES_DATA_LIST_GET` (74) *is* accepted and does push a
    #     `DEVICE_DATA` reply -- but the record it returns is frozen. Eight
    #     reads across on/off cycles came back byte-identical, reporting the
    #     lamp off while it was lit. Setting the lamp's clock first does not
    #     unfreeze it; that was tested.
    #
    # The 2026-08-04 vendor-app capture closed the question: the app builds that
    # command and never sends it. It reads nothing, holds the link open, and
    # relies entirely on the unsolicited pushes the lamp emits while connected.
    # So do we -- see `_IDLE_DISCONNECT_DELAY`.

    async def send_led(
        self, on: bool, brightness_pct: int | None = None, warm_ratio: float = 0.5
    ) -> None:
        """Send DEVICE_DATA_SET (CMD_WITH_NO_ACK / FIRE, PRIVATE, lmp_short)."""
        if brightness_pct is None:
            brightness_pct = DEFAULT_BRIGHTNESS_PCT

        payload = build_led_payload(self.light_type, on, brightness_pct, warm_ratio)
        sid = self._next_seq()
        pkt = build_short(
            MSG_FIRE,
            ENCRYPT_PRIVATE,
            payload,
            sid,
            self._pub,
            self._priv,
            self._nonce,
            b2=self._addr_b2,
            b3=self._addr_b3,
            addressed=True,
        )
        _LOGGER.debug(
            "Fermob %s →FIRE (%s) %s", self._address, self.light_type, pkt.hex()
        )
        await self._client.write_gatt_char(CHAR_UUID, pkt, response=False)

    async def unpair(self) -> BatteryVerdict:
        """Send LMP_COMMAND_UNREGISTER broadcast (JS "Forget").

        Returns what the session proved, not a bool, because the three outcomes
        need three different answers from the caller. A live session was told; a
        silent lamp was not reached and may still be ours; a lamp that rejected
        our keys is already free and cannot be told anything, and reporting that
        one as "did not answer, bring it in range" sent the user to retry a
        service that could never succeed.

        The broadcast itself is fire-and-forget, exactly as the app sends it, so
        it can never be acknowledged -- but the session carrying it can be
        checked, with a battery request one command earlier. That matters
        because of what the caller does next: deleting our keys while the lamp
        stays registered leaves it owned by a controller that has forgotten it,
        and nothing recovers that except a factory reset with a paperclip.
        """
        verdict = self._connect_verdict
        if verdict is None:
            # `ensure_connected()` returned early on an already-open link, so
            # nothing has been asked on it. Retried once, for the same reason
            # `ensure_connected` retries: one dropped ACK is a marginal link,
            # not a verdict. Without it a single missed reply aborts the service
            # and tells the user to "bring the lamp in range" when the lamp was
            # in range all along.
            verdict = await self._request_battery_verdict()
            if verdict is BatteryVerdict.SILENT:
                verdict = await self._request_battery_verdict()

        if verdict is not BatteryVerdict.ANSWERED:
            # Do NOT send it anyway. UNREGISTER is destructive and unacknowledged,
            # so a broadcast fired here is a coin toss the caller then has to
            # guess the result of: the lamp may well receive it and drop to NONE
            # while `async_unpair` truthfully reports "nothing has been removed"
            # and keeps the keys. That combination -- lamp unregistered, HA still
            # holding keys and an entry -- is worse than not trying, and the next
            # connect would silently re-pair it, so a user trying to hand the lamp
            # back to the Fermob app could never succeed.
            _LOGGER.warning(
                "Fermob %s: session not usable (%s); UNREGISTER not sent",
                self._address,
                verdict,
            )
            return verdict

        sid = self._next_seq()
        pkt = build_short(
            MSG_FIRE,
            ENCRYPT_PRIVATE,
            [1, CMD_UNREGISTER],
            sid,
            self._pub,
            self._priv,
            self._nonce,
            b2=0xFF,
            b3=0xFF,
            addressed=True,
        )
        _LOGGER.warning("Fermob %s: sending UNREGISTER broadcast", self._address)
        await self._client.write_gatt_char(CHAR_UUID, pkt, response=False)
        await asyncio.sleep(0.5)
        return verdict


# ---------------------------------------------------------------------------
# Lamp-type resolution
# ---------------------------------------------------------------------------


def resolve_light_type(entry: ConfigEntry) -> str:
    """Decide DW vs TW for this lamp.

    The app's device-class table (manufacturer_id 7) keys this off module_type
    (401 = DW, 404 = TW). The Linkio advertisement is a rotating encrypted
    payload, so that is not readable *before* pairing -- but the lamp does report
    it in MODULE_INFO_GET once connected, and we persist what it said into
    entry.data. Hence:

      1. Explicit override in entry.options / entry.data ("light_type").
      2. module_type as reported by the lamp itself -- exact, but only available
         from the second setup onwards, since it takes a connection to learn.
      3. Name heuristic: only the Hoopik string light is DW; everything else
         (MOOON! / table lamps) is tunable white. This is the first-run guess
         and the fallback for a lamp that reports a module_type we don't know.
    """
    override = entry.options.get("light_type") or entry.data.get("light_type")
    if override in (LIGHT_TYPE_DW, LIGHT_TYPE_TW):
        return override

    reported = module_type_to_light_type(entry.data.get("module_type"))
    if reported is not None:
        return reported

    name = (entry.data.get("name") or entry.data.get(CONF_ADDRESS) or "").lower()
    if "hoop" in name:
        return LIGHT_TYPE_DW
    return LIGHT_TYPE_TW


# ---------------------------------------------------------------------------
# HA platform
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # The connection is owned by __init__.py, not by this platform: the sensor
    # platforms share it, and they are set up concurrently with this one, so
    # creating it here would be a race.
    conn = hass.data[DOMAIN][entry.entry_id]
    # The state subscription is taken in the entity's async_added_to_hass, not
    # here: registering it against an entity that has not been added yet is what
    # allows a subscription to outlive the entity holding it.
    async_add_entities([FermobLight(hass, entry, conn, conn.light_type)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("unpair", {}, "async_unpair")
    platform.async_register_entity_service("check_in", {}, "async_check_in")


class FermobLight(LightEntity):
    """Representation of a Fermob BLE lamp (dimmable-white or tunable-white)."""

    # State is pushed: by our own commands, and by EVENT notifications while the
    # BLE link is up. There is nothing to poll -- neither state-read command
    # returns usable state on this lamp (see FermobBLEConnection.send_led).
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        conn: FermobBLEConnection,
        light_type: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._conn = conn
        self._light_type = light_type
        self._attr_is_on = False
        self._attr_brightness = 128

        if light_type == LIGHT_TYPE_TW:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_min_color_temp_kelvin = MIN_KELVIN
            self._attr_max_color_temp_kelvin = MAX_KELVIN
            self._attr_color_temp_kelvin = DEFAULT_KELVIN
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

        addr = entry.data[CONF_ADDRESS]
        self._attr_name = entry.data.get("name", addr)
        self._attr_unique_id = f"fermob_{addr.replace(':', '_').lower()}"

    @property
    def device_info(self) -> DeviceInfo:
        addr = self._entry.data[CONF_ADDRESS]
        # Prefer the model string the lamp reported over our family guess.
        model = self._entry.data.get("model") or (
            "MOOON (tunable white)"
            if self._light_type == LIGHT_TYPE_TW
            else "Hoopik GL1200 (dimmable white)"
        )
        return DeviceInfo(
            identifiers={("fermob", addr)},
            name=self._attr_name,
            manufacturer="Fermob",
            model=model,
        )

    # ------------------------------------------------------------------
    # State sync from the lamp (unsolicited EVENT pushes)
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscribe to the lamp's state pushes for as long as this entity lives.

        Tied to the entity via async_on_remove, so the subscription cannot
        outlive it -- the failure this replaced was exactly that.
        """
        self.async_on_remove(self._conn.add_state_listener(self.on_lamp_state_change))
        self.async_on_remove(
            self._conn.add_availability_listener(self.on_check_in_result)
        )

    def on_check_in_result(self, reached: bool) -> None:
        """Track availability from the scheduled check-in, not just commands.

        Without this the only writer of `_attr_available` is `_async_send_led`,
        so a lamp that stops answering keeps reading *available* until somebody
        happens to press the switch.
        """
        if self._attr_available == reached:
            return
        self._attr_available = reached
        self.schedule_update_ha_state()

    def on_lamp_state_change(self, is_on: bool, ch1: int, ch2: int) -> None:
        """ch1/ch2 = (level, 0) for DW, (cold_white, warm_white) for TW."""
        self._attr_is_on = is_on
        self._attr_available = True  # we just heard from the lamp
        if self._light_type == LIGHT_TYPE_TW:
            cold, warm = ch1, ch2
            total = cold + warm
            if total > 0:
                self._attr_brightness = round(total / 100 * 255)
                self._attr_color_temp_kelvin = warm_ratio_to_kelvin(warm / total)
            # total == 0 (lamp off): keep last brightness/temp for the UI
        else:
            level = ch1
            if level > 0:
                self._attr_brightness = round(level / 100 * 255)

        _LOGGER.debug(
            "Fermob %s: state is_on=%s ch1=%d ch2=%d",
            self._entry.data[CONF_ADDRESS],
            is_on,
            ch1,
            ch2,
        )
        self.schedule_update_ha_state()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _async_send_led(
        self, action: str, on: bool, brightness_pct: int, warm_ratio: float
    ) -> bool:
        """Connect if needed and send one LED command.

        Returns True on success. On failure the BLE link is dropped and the
        entity is marked unavailable, so the UI stops implying the last known
        state is still true (the lamp is typically off, asleep or out of range).
        """
        async with self._conn.lock:
            try:
                # A user is watching this one, so it gives up sooner -- see
                # CONNECT_ATTEMPTS_INTERACTIVE.
                await self._conn.ensure_connected(
                    max_attempts=CONNECT_ATTEMPTS_INTERACTIVE
                )
                await self._conn.send_led(on, brightness_pct, warm_ratio)
            except Exception as exc:  # reported to the user via the log
                _LOGGER.error(
                    "Fermob %s %s error: %s",
                    self._entry.data[CONF_ADDRESS],
                    action,
                    exc,
                    exc_info=True,
                )
                await self._conn.disconnect()
                self._attr_available = False
                self.async_write_ha_state()
                return False

        self._attr_available = True
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_ha = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 128)
        brightness_pct = max(1, round(brightness_ha / 255 * 100))

        warm_ratio = 0.5
        kelvin = self._attr_color_temp_kelvin or DEFAULT_KELVIN
        if self._light_type == LIGHT_TYPE_TW:
            kelvin = max(
                MIN_KELVIN, min(MAX_KELVIN, kwargs.get(ATTR_COLOR_TEMP_KELVIN, kelvin))
            )
            warm_ratio = kelvin_to_warm_ratio(kelvin)

        if not await self._async_send_led("turn_on", True, brightness_pct, warm_ratio):
            return

        self._attr_is_on = True
        self._attr_brightness = brightness_ha
        if self._light_type == LIGHT_TYPE_TW:
            self._attr_color_temp_kelvin = kelvin
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Preserve the current colour temperature on the off command so the lamp
        # keeps its warm/cold balance when toggled back on from the button.
        warm_ratio = 0.5
        if self._light_type == LIGHT_TYPE_TW:
            warm_ratio = kelvin_to_warm_ratio(
                self._attr_color_temp_kelvin or DEFAULT_KELVIN
            )

        if not await self._async_send_led("turn_off", False, 0, warm_ratio):
            return

        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_check_in(self) -> None:
        """Reconnect and refresh the battery now, rather than on the timer.

        The entity-service face of `FermobBLEConnection.async_check_in`, which
        already takes the lock and swallows its own failures -- so this is a
        plain delegation, and calling it on an out-of-range lamp is a no-op
        rather than an error.

        **Being an entity service, it cannot be called on an unavailable
        entity** -- Home Assistant filters the target out before this runs, and
        the call still reports success. That is a real limitation and it is
        accepted rather than worked around: the *scheduled* check-in calls the
        connection directly, so a lamp whose entity has gone unavailable is
        recovered without anyone asking, within one check-in interval. See
        `docs/domain/ENTITIES-AND-SERVICES.md`.
        """
        await self._conn.async_check_in()

    async def async_unpair(self) -> None:
        """Unpair the lamp and remove this config entry.

        Both halves or neither, and the order matters. Removing the entry deletes
        the keys with it (`async_remove_entry`), and that half cannot be walked
        back: the lamp stays registered until it hears UNREGISTER, and a lamp
        registered to a controller with no key is recoverable only by a 10-second
        factory reset. That state reads as "PRIVATE mode but no stored keys" on
        the next attempt.

        So an unreachable lamp raises and removes nothing. `unpair()` does not
        even send the broadcast in that case -- see there. The user sees why, and
        can retry once the lamp is in range; if it is gone for good, deleting the
        integration is the cleanup path, and it takes the keys with it.

        Three outcomes, not two, and both of the new ones are cases where the old
        "could not reach it, try again" was simply false. A lamp that *rejected*
        our keys is already free, so there is nothing to release and retrying can
        never succeed. And an entry with no stored keys never had a pairing at
        all, so it is removed without touching the radio.

        What "reachable" can and cannot establish is worth being precise about:
        UNREGISTER is a broadcast with no acknowledgement, so nothing here proves
        the lamp acted on it. What is proved is that the session was alive one
        command earlier, which rules out the case this guards against -- a
        broadcast fired into a link the lamp had already stopped honouring.
        """
        address = self._entry.data[CONF_ADDRESS]
        async with self._conn.lock:
            if not await self._conn.is_paired():
                # No keys, so there is nothing registered to release: either a
                # first pairing failed after the entry was created but before
                # `_save_keys()` ran, or something removed the record. Going down
                # the connect path here raised "not paired, and pairing is not
                # allowed" wrapped in "could not reach the lamp" -- blaming the
                # radio for a state that has nothing to do with range, and
                # leaving an entry the service could never clean up.
                _LOGGER.warning(
                    "Fermob %s: no stored pairing, nothing to release -- "
                    "removing the entry",
                    address,
                )
            else:
                try:
                    # Never pairing: this service exists to *release* the lamp.
                    # On a lamp the user reset behind our back the default would
                    # run the re-pair branch -- flashing it, re-registering it to
                    # Home Assistant -- and only then broadcast UNREGISTER, which
                    # is the exact opposite of what was asked for.
                    await self._conn.ensure_connected(
                        allow_pairing=False,
                        max_attempts=CONNECT_ATTEMPTS_INTERACTIVE,
                    )
                    verdict = await self._conn.unpair()
                except LampNotAnswering as err:
                    # `ensure_connected` refuses a link it could not get an
                    # answer over, so on a factory-reset lamp it raises *before*
                    # `unpair()` runs -- which means the verdict has to be read
                    # off the exception here as well as out of `unpair()`.
                    # Without this the generic handler below produced "Could not
                    # reach the Fermob lamp at ... to unpair it: ... turn the
                    # light on in Home Assistant to re-pair it": the wrong
                    # diagnosis for a lamp that had answered, followed by the
                    # exact opposite of what was asked for. Seen on an H134,
                    # 2026-08-06; the unit tests missed it because they mock
                    # `ensure_connected`.
                    #
                    # Logged, because the `HomeAssistantError` built below is
                    # generic by verdict and drops the specific message -- "silent
                    # on a link that just came up" and "still holds our keys and
                    # is still not answering" are different problems that both
                    # surface to the user as "did not answer".
                    _LOGGER.warning("%s", err)
                    verdict = err.verdict
                except Exception as exc:
                    _LOGGER.error(
                        "Fermob %s unpair error: %s", address, exc, exc_info=True
                    )
                    raise HomeAssistantError(
                        f"Could not reach the Fermob lamp at {address} to unpair "
                        f"it: {exc}"
                    ) from exc
                finally:
                    await self._conn.disconnect()

                if verdict is BatteryVerdict.KEYS_REJECTED:
                    # The lamp answered, and what it said was that it cannot
                    # decrypt us -- so it is already free and UNREGISTER could
                    # not reach it even if we sent it. Telling the user to bring
                    # it in range was doubly wrong: it is in range, and retrying
                    # can never work. Still not removed here, because this is the
                    # one-way door -- if the rejection were somehow ours rather
                    # than the lamp's, deleting the keys would strand a lamp that
                    # is still registered to us.
                    raise HomeAssistantError(
                        f"The Fermob lamp at {address} rejected our keys, so it "
                        "is no longer paired with Home Assistant -- it was most "
                        "likely factory-reset. There is nothing left to release. "
                        "Delete the integration to clean up; that removes the "
                        "stored keys too."
                    )

                if verdict is not BatteryVerdict.ANSWERED:
                    raise HomeAssistantError(
                        f"The Fermob lamp at {address} did not answer, so it was "
                        "probably not unpaired. Nothing has been removed -- bring "
                        "the lamp in range and try again. Removing it now would "
                        "leave the lamp registered to Home Assistant with no way "
                        "back except a factory reset."
                    )

                # Only now: the lamp has been told, so the keys are genuinely
                # dead. `async_remove_entry` deletes the stored record when the
                # entry goes; this clears the copy this object is still holding,
                # so nothing can use it in the window before the platforms
                # unload.
                await self._conn.discard_keys()

        await self.hass.config_entries.async_remove(self._entry.entry_id)
