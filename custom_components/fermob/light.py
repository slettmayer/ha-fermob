"""Fermob BLE light entity — mirrors JS meshConnectionService / BLEProtocolService.

MOOON support (tunable white) added on top of the original Hoopik (dimmable white)
integration by edouardrosset.

The frame/payload construction lives in `protocol.py` (no Home Assistant imports,
unit-tested); this module owns the BLE connection lifecycle and the HA entity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

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
    DEVICE_DATA_MARKERS,
    ENCRYPT_NONE,
    ENCRYPT_PRIVATE,
    ENCRYPT_PUBLIC,
    LIGHT_TYPE_DW,
    LIGHT_TYPE_TW,
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
    build_led_payload,
    build_long,
    build_short,
    decode_fragment,
    error_name,
    kelvin_to_warm_ratio,
    module_type_to_light_type,
    parse_battery,
    parse_device_state,
    parse_module_info,
    warm_ratio_to_kelvin,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BRIGHTNESS_PCT = 50
DEFAULT_KELVIN = 4000

_STORAGE_VERSION = 1

# JS keeps BLE connected indefinitely; we disconnect after 30 s idle
_IDLE_DISCONNECT_DELAY = 30.0


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
    ) -> None:
        self.hass = hass
        self._address = address
        self._store = store
        self.light_type = light_type  # "dw" | "tw"
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

        # What the lamp says it is (MODULE_INFO_GET). None until read once.
        self.module_type: int | None = None
        self.model: str | None = None
        self.on_module_info: Any = None  # (module_type, model) -> None

        # Runtime state
        self._connected = False  # BLE link is up
        self._ready = False  # post-connect setup complete, commands allowed
        self._idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

        # Last battery reading, None until the lamp reports one. A reported 0 is
        # a real 0; "never reported" must stay distinguishable from "empty".
        self.battery: Battery | None = None

        # Queues / callbacks
        self._ack_queue: asyncio.Queue = asyncio.Queue()
        self.on_state_change: Any = None  # (is_on, ch1, ch2) -> None
        self.on_battery: Any = None  # (Battery) -> None

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
            if self.on_battery is not None:
                self.on_battery(battery)
            return

        if len(pl) < 10 or pl[1] not in DEVICE_DATA_MARKERS:
            return
        state = parse_device_state(pl)
        if state is None:
            return
        is_on, ch1, ch2 = state
        _LOGGER.debug(
            "Fermob %s: EVENT is_on=%s ch1=%d ch2=%d", self._address, is_on, ch1, ch2
        )
        if self.on_state_change is not None:
            self.on_state_change(is_on, ch1, ch2)

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

    async def _send_frames(self, frames: list[bytes]) -> tuple[bytes | None, int]:
        """Write BLE frames and wait for the matching CMD_ACK (mt=2, cmd==seq)."""
        my_seq = frames[0][1]
        for frame in frames:
            await self._client.write_gatt_char(CHAR_UUID, frame, response=False)
            if len(frames) > 1:
                await asyncio.sleep(0.05)

        deadline = asyncio.get_event_loop().time() + 3.0
        fragments: dict[int, bytes] = {}
        seq_total: int | None = None
        first_enc = 0
        LONG_START = {3, 4, 5}

        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                _LOGGER.warning(
                    "Fermob %s: ACK timeout seq=%02x", self._address, my_seq
                )
                return None, 0
            try:
                frame = await asyncio.wait_for(self._ack_queue.get(), timeout=rem)
            except TimeoutError:
                _LOGGER.warning(
                    "Fermob %s: ACK timeout seq=%02x", self._address, my_seq
                )
                return None, 0

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
                    self._ack_queue.put_nowait(frame)
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
            return None, first_enc
        pl = b"".join(fragments[i] for i in sorted(fragments))

        # A rejected command still arrives as a correctly-sequenced CMD_ACK, so
        # without this check a NAK reads as success and whatever the caller
        # parses out of the body is garbage -- silently storing bad keys, in the
        # handshake's case.
        err = ack_error(pl)
        if err is not None:
            _LOGGER.warning(
                "Fermob %s: command seq=%02x rejected: %s",
                self._address,
                my_seq,
                error_name(err),
            )
            return None, first_enc

        return pl, first_enc

    async def _send(self, enc: int, payload: list[int]) -> tuple[bytes | None, int]:
        sid = self._next_seq()
        if len(payload) <= 15:
            frames = [
                build_short(
                    MSG_CMD, enc, payload, sid, self._pub, self._priv, self._nonce
                )
            ]
        else:
            frames = build_long(enc, payload, sid, self._pub, self._priv, self._nonce)
        return await self._send_frames(frames)

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
                state = parse_device_state(pl)
                if state is not None:
                    _LOGGER.debug(
                        "Fermob %s: EVENT after REGISTER_END %s", self._address, state
                    )
                    return state

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
            self._addr_b2, self._addr_b3 = info.addr_b2, info.addr_b3
            self._store_module_info(info)

        # Step 8: optional device info
        await self._send(ENCRYPT_PRIVATE, [1, CMD_DEVICE_INFO_GET])

        # Persist keys before REGISTER_END so they survive a missing EVENT
        await self._save_keys()
        self._have_keys = True

        # Step 9: REGISTER_END → lamp enters GATEWAY mode
        await self._send(ENCRYPT_PRIVATE, [2, CMD_REGISTER, 1])

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
        (e.g. after changing the lamp-type option) closes the connection instead
        of leaking an open BleakClient and a pending idle task.
        """
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        async with self.lock:  # wait for any in-flight command to finish
            await self.disconnect()

    def _schedule_idle_disconnect(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()

        async def _idle() -> None:
            await asyncio.sleep(_IDLE_DISCONNECT_DELAY)
            async with self.lock:
                _LOGGER.debug("Fermob %s: idle timeout → disconnect", self._address)
                await self.disconnect()

        self._idle_task = asyncio.ensure_future(_idle())

    async def ensure_connected(self) -> None:
        """Ensure an authenticated BLE connection is up.

        Runs the full pairing handshake on first use and a plain BLE reconnect
        afterwards; the lamp keeps its GATEWAY+PRIVATE state across disconnects.
        """
        have_keys = await self._load_keys()

        if self._connected and self._client and self._client.is_connected:
            self._schedule_idle_disconnect()
            return

        device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            raise RuntimeError(f"Fermob BLE device not found: {self._address}")

        _LOGGER.warning("Fermob %s: connecting…", self._address)
        self._client = await establish_connection(BleakClient, device, self._address)

        # Flush any stale frames
        while not self._ack_queue.empty():
            self._ack_queue.get_nowait()

        await self._client.start_notify(CHAR_UUID, self._notif_handler)

        if not have_keys:
            # First pairing: full handshake (sets _have_keys and saves keys)
            await self._pairing_handshake()
        else:
            # Reconnect: the lamp retains GATEWAY+PRIVATE state across BLE
            # disconnects, so no crypto handshake is needed. It also emits no
            # spontaneous EVENT here, and neither state-read command returns
            # anything usable, so there is no state to read back.
            _LOGGER.debug(
                "Fermob %s: reconnected (lamp keeps GATEWAY state)", self._address
            )
            await self._fetch_module_info_once()

        self._ready = True
        self._connected = True
        _LOGGER.warning("Fermob %s: ready", self._address)
        await self.request_battery()
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
        if self.module_type is not None:
            return
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
        self._store_module_info(info)
        if self._have_keys:
            await self._save_keys()

    # ------------------------------------------------------------------
    # Lamp commands
    # ------------------------------------------------------------------

    async def request_battery(self) -> None:
        """Ask the lamp for its state of charge.

        The ACK carries nothing but a success code -- the value follows as a
        separate STATUS push, which `_dispatch_event` picks up. So this returns
        as soon as the request is acknowledged and does *not* wait for the
        reading; in practice the push arrives in the same millisecond.

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
            await self._send_frames([frame])
        except Exception:
            _LOGGER.debug(
                "Fermob %s: battery request failed", self._address, exc_info=True
            )

    # There is deliberately no state-read method here. Both candidate commands
    # were tried on an H134 and neither yields usable state -- see
    # `docs/domain/LINKIO-PROTOCOL.md` for the traces. In short:
    #
    #   * `DEVICE_DATA_GET` (66) is answered with `INVALID_SIZE`, even when sent
    #     with the app's byte-exact body. The app only ever sends it to modules
    #     whose role is not LEAF, and this lamp is a leaf.
    #   * `DEVICES_DATA_LIST_GET` (74), which is what the app actually uses, *is*
    #     accepted and does push a `DEVICE_DATA` reply -- but the record it
    #     returns is frozen. Eight reads across on/off cycles came back
    #     byte-identical, reporting the lamp off while it was lit.
    #
    # Wiring that reply to `on_state_change` is therefore worse than not reading
    # at all: during the probe it drove the HA entity to "off" while the lamp was
    # on. `parse_device_state` stays, because unsolicited EVENT pushes during
    # pairing use the same layout.

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

    async def unpair(self) -> None:
        """Send LMP_COMMAND_UNREGISTER broadcast (JS "Forget")."""
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
    entity = FermobLight(hass, entry, conn, conn.light_type)
    conn.on_state_change = entity.on_lamp_state_change
    async_add_entities([entity])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("unpair", {}, "async_unpair")


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
    # State sync from the lamp (unsolicited EVENT during pairing)
    # ------------------------------------------------------------------

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
                await self._conn.ensure_connected()
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

    async def async_unpair(self) -> None:
        """Unpair the lamp and remove this config entry."""
        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.unpair()
            except Exception as exc:  # reported to the user via the log
                _LOGGER.error(
                    "Fermob %s unpair error: %s",
                    self._entry.data[CONF_ADDRESS],
                    exc,
                    exc_info=True,
                )
            finally:
                await self._conn.disconnect()
                await self._conn._store.async_remove()
                self._conn._keys_loaded = False
                self._conn._have_keys = False

        await self.hass.config_entries.async_remove(self._entry.entry_id)
