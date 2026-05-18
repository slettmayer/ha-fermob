"""Fermob BLE light entity — mirrors JS meshConnectionService / BLEProtocolService."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# BLE characteristic (JS LINKIO_TXRX_CHARACTERISTIC)
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"

# Encryption modes (JS lms_header_encryption)
ENCRYPT_NONE    = 0
ENCRYPT_PUBLIC  = 1
ENCRYPT_PRIVATE = 2

# Frame message types
MSG_FIRE     = 0   # CMD_WITH_NO_ACK — lmp_short_frame (ft=2)
MSG_CMD      = 1   # CMD_WITH_ACK    — local_short_frame (ft=0)
MSG_MESH_CMD = 2   # CMD_WITH_ACK    — lmp_short_frame  (ft=2, SHORT addr)

# LMP command IDs (JS CODES)
CMD_REGISTER             = 16
CMD_UNREGISTER           = 17
CMD_CRYPT_NONCE_GENERATE = 19
CMD_CRYPT_NONCE_SET      = 21
CMD_CRYPT_AUTHKEY_GEN    = 22
CMD_CRYPT_AUTHKEY_GET    = 23
CMD_CRYPT_AUTHKEY_SET    = 24
CMD_CRYPT_SET            = 25
CMD_MODULE_INFO_GET      = 48
CMD_DEVICE_DATA_SET      = 65
CMD_DEVICE_DATA_GET      = 66

LMP_PARAM_SHORT_ADDRESS = 177  # 0xb1
LMP_PARAM_API_VERSION   = 184  # 0xb8

# Hoopik GL1200 = Dimmable White (model_id=3)
FADE        = 50
LED_MODE_DW = 1

DEFAULT_BRIGHTNESS_PCT = 50

_STORAGE_VERSION = 1

# JS keeps BLE connected indefinitely; we disconnect after 30 s idle
_IDLE_DISCONNECT_DELAY = 30.0

# ---------------------------------------------------------------------------
# Crypto / frame helpers
# ---------------------------------------------------------------------------

def _crypt(data16: bytes, mode: int, pub: bytes, priv: bytes, nonce: bytes) -> bytes:
    if mode == ENCRYPT_NONE:
        return data16
    from Crypto.Cipher import AES
    key = pub if mode == ENCRYPT_PUBLIC else priv
    ks = AES.new(bytes(key), AES.MODE_ECB).encrypt(bytes(nonce))
    return bytes(a ^ b for a, b in zip(ks, data16))


def _crc(data: bytes) -> int:
    r = 0
    for b in data:
        r ^= b
    return r


def _pad15(payload: list) -> list:
    p = list(payload)
    if len(p) < 15:
        p.append(0x00)
    while len(p) < 15:
        p.append(0xFF)
    return p[:15]


def _build_short(msg_type: int, enc: int, payload: list,
                 cmd_id: int, pub: bytes, priv: bytes, nonce: bytes,
                 b2: int = 0, b3: int = 0) -> bytes:
    ft = 2 if msg_type in (MSG_FIRE, MSG_MESH_CMD) else 0
    hdr = ((msg_type & 7) << 5) | ((enc & 3) << 3) | ft
    p = _pad15(payload)
    enc_data = _crypt(bytes([_crc(bytes(p))] + p), enc, pub, priv, nonce)
    return bytes([hdr, cmd_id, b2, b3]) + enc_data


def _build_long(enc: int, payload: list,
                cmd_id: int, pub: bytes, priv: bytes, nonce: bytes) -> list[bytes]:
    chunks = [_pad15(payload[i:i + 15]) for i in range(0, len(payload), 15)]
    frames = []
    for idx, chunk in enumerate(chunks):
        ft = 3 if idx == 0 else 6
        h0 = ((MSG_CMD & 7) << 5) | ((enc & 3) << 3) | ft
        enc_data = _crypt(bytes([_crc(bytes(chunk))] + chunk), enc, pub, priv, nonce)
        frames.append(bytes([h0, cmd_id, idx, len(chunks)]) + enc_data)
    return frames


def _decode_fragment(frame: bytes, enc: int,
                     pub: bytes, priv: bytes, nonce: bytes) -> bytes:
    plain = _crypt(bytes(frame[4:20]), enc, pub, priv, nonce)
    return plain[1:16]


def _parse_module_info(payload: bytes) -> tuple[int, int, int]:
    addr_b2 = addr_b3 = 0
    api_ver = 2
    i = 0
    while i < len(payload):
        t_len = payload[i]
        if t_len == 0:
            break
        if i + 1 >= len(payload):
            break
        t_type = payload[i + 1]
        if t_type == LMP_PARAM_SHORT_ADDRESS and t_len >= 2:
            addr_b2 = payload[i + 2]
            addr_b3 = payload[i + 3]
        elif t_type == LMP_PARAM_API_VERSION and t_len >= 1:
            api_ver = payload[i + 2]
        i += t_len + 1
    return addr_b2, addr_b3, api_ver


def _parse_device_state(payload: bytes) -> tuple[bool, int] | None:
    """Parse DEVICE_DATA_GET response or EVENT_DEVICE_DATA notification."""
    if len(payload) < 10:
        return None
    if payload[7] != 0:
        return None
    is_on = bool(payload[8] & 0x0F)
    level = payload[9]
    return is_on, level


# ---------------------------------------------------------------------------
# Connection — mirrors JS meshConnectionService + BLEProtocolService
#
# JS CONNECTION MODEL (from APK reverse engineering):
#   Initial pairing  : BLE connect → full handshake (REGISTER(0) + key exchange
#                      + REGISTER(1)) → lamp enters GATEWAY+PRIVATE state permanently.
#   Reconnection     : BLE connect + startNotification ONLY.
#                      NO CMD_REGISTER, NO key exchange. The lamp retains its
#                      GATEWAY+PRIVATE state across BLE disconnects.
#
# Our model mirrors this exactly:
#   - If we have stored keys AND the lamp advertises in PRIVATE: just reconnect BLE.
#   - Only do the full handshake on first-ever pairing (no stored keys).
# ---------------------------------------------------------------------------

class FermobBLEConnection:
    """
    Manages a persistent BLE connection to one Fermob lamp.

    Connection lifecycle (mirrors JS):
    - After initial pairing the lamp stays in GATEWAY+PRIVATE state indefinitely.
    - On reconnect we only re-establish the BLE link; no crypto handshake.
    - Unsolicited EVENT notifications (physical button) are dispatched to
      on_state_change so the HA entity can update its state without polling.
    - After reconnect we send one DEVICE_DATA_GET to sync the real lamp state.
    """

    def __init__(self, hass: HomeAssistant, address: str, store: Store) -> None:
        self.hass     = hass
        self._address = address
        self._store   = store
        self._client  = None
        self._seq     = 0

        # Crypto keys (persisted)
        self._pub     = bytes(16)
        self._priv    = bytes(16)
        self._nonce   = bytes(16)
        self._addr_b2 = 0
        self._addr_b3 = 0
        self._keys_loaded = False
        self._have_keys   = False  # True after successful pairing

        # Runtime state
        self._connected = False   # BLE link is up
        self._ready     = False   # post-connect setup complete, commands allowed
        self._idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

        # Queues / callbacks
        self._ack_queue: asyncio.Queue = asyncio.Queue()
        self.on_state_change: Any = None  # (is_on: bool, level: int) -> None

    # ------------------------------------------------------------------
    # Key persistence
    # ------------------------------------------------------------------

    async def _load_keys(self) -> bool:
        if self._keys_loaded:
            return self._have_keys
        self._keys_loaded = True
        data = await self._store.async_load()
        if data and all(k in data for k in ("pub", "priv", "nonce")):
            self._pub     = bytes.fromhex(data["pub"])
            self._priv    = bytes.fromhex(data["priv"])
            self._nonce   = bytes.fromhex(data["nonce"])
            self._addr_b2 = data.get("addr_b2", 0)
            self._addr_b3 = data.get("addr_b3", 0)
            self._have_keys = True
            _LOGGER.debug("Fermob %s: keys loaded", self._address)
            return True
        return False

    async def _save_keys(self) -> None:
        await self._store.async_save({
            "pub":     self._pub.hex(),
            "priv":    self._priv.hex(),
            "nonce":   self._nonce.hex(),
            "addr_b2": self._addr_b2,
            "addr_b3": self._addr_b3,
        })
        _LOGGER.debug("Fermob %s: keys saved", self._address)

    # ------------------------------------------------------------------
    # BLE notification handler
    # ------------------------------------------------------------------

    def _notif_handler(self, sender, data: bytearray) -> None:
        """Route incoming BLE frames.

        mt=4 (EVENT) frames are unsolicited state notifications from the lamp
        (physical button press, or post-REGISTER_END status).
        All other frames (ACKs, mt=2) go to _ack_queue for _send_frames().
        """
        frame = bytes(data)
        if len(frame) < 20:
            return
        h0 = frame[0]
        mt = (h0 >> 5) & 7

        if mt == 4:
            if self._ready and self.on_state_change is not None:
                resp_enc = (h0 >> 3) & 3
                try:
                    pl = _decode_fragment(frame, resp_enc,
                                          self._pub, self._priv, self._nonce)
                    if len(pl) >= 10 and pl[1] == 146:  # LMP_EVENT_DEVICE_DATA
                        state = _parse_device_state(pl)
                        if state is not None:
                            is_on, level = state
                            _LOGGER.debug("Fermob %s: EVENT is_on=%s level=%d",
                                          self._address, is_on, level)
                            self.on_state_change(is_on, level)
                except Exception:
                    pass
            else:
                # During handshake: stash so _wait_for_event() can see it
                self._ack_queue.put_nowait(frame)
            return

        # All non-EVENT frames → ACK queue
        self._ack_queue.put_nowait(frame)

    # ------------------------------------------------------------------
    # Frame send/receive
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) % 256
        return self._seq

    async def _send_frames(self, frames: list[bytes]) -> tuple[bytes | None, int]:
        """Write BLE frames and wait for the matching ACK (mt=2, cmd==seq)."""
        my_seq = frames[0][1]
        for frame in frames:
            await self._client.write_gatt_char(CHAR_UUID, frame, response=False)
            if len(frames) > 1:
                await asyncio.sleep(0.05)

        deadline   = asyncio.get_event_loop().time() + 3.0
        fragments: dict[int, bytes] = {}
        seq_total: int | None = None
        first_enc = 0
        LONG_START = {3, 4, 5}

        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                _LOGGER.warning("Fermob %s: ACK timeout seq=%02x", self._address, my_seq)
                return None, 0
            try:
                frame = await asyncio.wait_for(self._ack_queue.get(), timeout=rem)
            except asyncio.TimeoutError:
                _LOGGER.warning("Fermob %s: ACK timeout seq=%02x", self._address, my_seq)
                return None, 0

            if len(frame) < 20:
                continue
            h0       = frame[0]
            mt       = (h0 >> 5) & 7
            ft       = h0 & 7
            cmd      = frame[1]
            resp_enc = (h0 >> 3) & 3

            _LOGGER.debug("Fermob %s ← mt=%d ft=%d enc=%d cmd=%02x seq=%02x raw=%s",
                          self._address, mt, ft, resp_enc, cmd, my_seq, frame.hex())

            # mt=4 arrived while we were waiting for an ACK: re-route appropriately
            if mt == 4:
                if self._ready and self.on_state_change is not None:
                    try:
                        pl = _decode_fragment(frame, resp_enc,
                                               self._pub, self._priv, self._nonce)
                        if len(pl) >= 10 and pl[1] == 146:
                            state = _parse_device_state(pl)
                            if state is not None:
                                is_on, level = state
                                _LOGGER.debug(
                                    "Fermob %s: EVENT (in-flight) is_on=%s level=%d",
                                    self._address, is_on, level)
                                self.on_state_change(is_on, level)
                    except Exception:
                        pass
                else:
                    # During handshake: re-queue so _wait_for_event() can see it
                    self._ack_queue.put_nowait(frame)
                continue

            if mt != 2 or cmd != my_seq:
                _LOGGER.debug("Fermob %s: ignored frame (mt=%d cmd=%02x expected=%02x)",
                              self._address, mt, cmd, my_seq)
                continue

            if not fragments:
                first_enc = resp_enc
            frag = _decode_fragment(frame, resp_enc, self._pub, self._priv, self._nonce)
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
        return pl, first_enc

    async def _send(self, enc: int, payload: list) -> tuple[bytes | None, int]:
        sid = self._next_seq()
        if len(payload) <= 15:
            frames = [_build_short(MSG_CMD, enc, payload, sid,
                                   self._pub, self._priv, self._nonce)]
        else:
            frames = _build_long(enc, payload, sid,
                                 self._pub, self._priv, self._nonce)
        return await self._send_frames(frames)

    async def _drain(self, timeout: float = 0.15) -> None:
        """Drain any stale frames from the ACK queue."""
        while True:
            try:
                await asyncio.wait_for(self._ack_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break

    async def _wait_for_event(self, timeout: float = 0.5) -> tuple[bool, int] | None:
        """
        Wait for the lamp's post-REGISTER_END state EVENT (mt=4).

        After REGISTER(1) the lamp emits its current state as an EVENT ~200-300 ms
        later. We capture it here to:
          1. Confirm the lamp has entered GATEWAY mode (mirrors JS setTimeout(100ms)).
          2. Get the real lamp state without a separate DEVICE_DATA_GET round-trip.

        Returns (is_on, level) or None if the EVENT doesn't arrive in time.
        Used during initial pairing (0.5 s timeout) and on reconnect (0.2 s timeout).
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                return None
            try:
                frame = await asyncio.wait_for(self._ack_queue.get(), timeout=rem)
            except asyncio.TimeoutError:
                return None
            if len(frame) < 20:
                continue
            h0 = frame[0]
            mt = (h0 >> 5) & 7
            if mt != 4:
                continue  # discard stray ACKs
            resp_enc = (h0 >> 3) & 3
            try:
                pl = _decode_fragment(frame, resp_enc,
                                       self._pub, self._priv, self._nonce)
                if len(pl) >= 10 and pl[1] == 146:
                    state = _parse_device_state(pl)
                    if state is not None:
                        is_on, level = state
                        _LOGGER.debug(
                            "Fermob %s: EVENT after REGISTER_END is_on=%s level=%d",
                            self._address, is_on, level)
                        return state
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Initial pairing handshake (first-time only, mirrors JS startPairing)
    # ------------------------------------------------------------------

    async def _pairing_handshake(self) -> tuple[bool, int] | None:
        """
        Full pairing sequence — run ONCE, when we have no stored keys.

        JS flow (startPairing → setPublicKey → getNonce → setPublicEncryptionMode
                 → setPrivateKey → setPrivateEncryptionMode → sendRegisterEnd):
          1. REGISTER(0)          NONE   → probe (gets public key in response)
          2. AUTHKEY_GET          NONE   → get lamp's public key
          3. NONCE_GENERATE       NONE   → generate nonce, lamp returns it
          4. CRYPT_SET(PUBLIC)    NONE   → switch to public encryption
          5. AUTHKEY_GEN(1)       PUBLIC → generate private key
          6. CRYPT_SET(PRIVATE)   PUBLIC → switch to private encryption
          7. MODULE_INFO_GET      PRIVATE→ get short address
          8. DEVICE_INFO_GET(50)  PRIVATE→ optional info
          9. REGISTER(1)          PRIVATE→ REGISTER_END → lamp → GATEWAY mode
          10. Wait for state EVENT (mt=4) → confirms GATEWAY mode + real state

        Returns (is_on, level) from the post-REGISTER_END EVENT, or None.
        """
        _LOGGER.warning("Fermob %s: fresh pairing", self._address)

        # Step 1: probe — confirm lamp is in NONE (fresh / factory-reset)
        probe_pl, probe_enc = await self._send(ENCRYPT_NONE, [2, CMD_REGISTER, 0])
        if probe_enc == ENCRYPT_PRIVATE:
            raise RuntimeError(
                "Lamp is in PRIVATE mode but no stored keys found. "
                "Factory-reset the lamp (hold button 10 s) and delete "
                ".storage/fermob_* before retrying."
            )

        # Step 2: get public key
        pl, _ = await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_AUTHKEY_GET, 0])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GET failed")
        self._pub = bytes(pl[3:19])

        # Step 3: generate nonce
        pl, _ = await self._send(ENCRYPT_NONE, [1, CMD_CRYPT_NONCE_GENERATE])
        if not pl or len(pl) < 19:
            raise RuntimeError("NONCE_GENERATE failed")
        self._nonce = bytes(pl[3:19])

        # Step 4: switch to PUBLIC encryption
        await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_SET, ENCRYPT_PUBLIC])

        # Step 5: generate private key
        pl, _ = await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_AUTHKEY_GEN, 1])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GEN failed")
        self._priv = bytes(pl[3:19])

        # Step 6: switch to PRIVATE encryption
        await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_SET, ENCRYPT_PRIVATE])

        # Step 7: get short address
        pl, _ = await self._send(ENCRYPT_PRIVATE, [1, CMD_MODULE_INFO_GET])
        if pl:
            self._addr_b2, self._addr_b3, _ = _parse_module_info(pl)

        # Step 8: optional DEVICE_INFO_GET
        await self._send(ENCRYPT_PRIVATE, [1, 50])

        # Persist keys before REGISTER_END (safety: so keys survive even if EVENT
        # doesn't arrive)
        await self._save_keys()
        self._have_keys = True

        # Step 9: REGISTER_END → lamp enters GATEWAY mode
        await self._send(ENCRYPT_PRIVATE, [2, CMD_REGISTER, 1])

        # Step 10: wait for the state EVENT the lamp emits after entering GATEWAY mode
        # (mirrors JS setTimeout(100ms) after setMeshConnection)
        lamp_state = await self._wait_for_event(timeout=0.5)

        return lamp_state

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        self._connected = False
        self._ready     = False
        if self._client:
            try:
                await self._client.stop_notify(CHAR_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        _LOGGER.debug("Fermob %s: disconnected", self._address)

    def _schedule_idle_disconnect(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()

        async def _idle() -> None:
            await asyncio.sleep(_IDLE_DISCONNECT_DELAY)
            async with self.lock:
                _LOGGER.debug("Fermob %s: idle timeout → disconnect", self._address)
                await self.disconnect()

        self._idle_task = asyncio.ensure_future(_idle())

    async def ensure_connected(self) -> tuple[bool, int] | None:
        """
        Ensure an authenticated BLE connection is up.

        JS reconnection model:
          - If the lamp is already paired (we have keys), just re-establish the BLE
            link and re-subscribe to notifications. NO CMD_REGISTER, NO key exchange.
            The lamp stays in GATEWAY+PRIVATE state across BLE disconnects.
          - If we have no keys, run the full pairing handshake.

        Returns the real lamp state (is_on, level) from:
          - The post-REGISTER_END EVENT on first pairing, or
          - A DEVICE_DATA_GET query on reconnect (so callers can sync HA state).
        Returns None if state could not be determined.
        """
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection
        from homeassistant.components.bluetooth import async_ble_device_from_address

        have_keys = await self._load_keys()

        if self._connected and self._client and self._client.is_connected:
            self._schedule_idle_disconnect()
            return None  # already connected, no state update needed

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

        lamp_state: tuple[bool, int] | None = None

        if not have_keys:
            # First pairing: full handshake
            # _pairing_handshake sets _have_keys and saves keys
            lamp_state = await self._pairing_handshake()
        else:
            # Reconnect: JS just reconnects BLE, no crypto handshake.
            # The lamp retains GATEWAY+PRIVATE state across disconnects.
            #
            # We do NOT query state here. Reasons:
            #   - The lamp does not emit a spontaneous EVENT after a plain BLE reconnect.
            #   - DEVICE_DATA_GET (MESH CMD) is not ACKed by this lamp in this state.
            #   - HA already tracks the last-known state from the previous session.
            # The caller (async_turn_on/off) uses its own _attr_is_on / _attr_brightness,
            # which are accurate because every successful command updates them immediately.
            _LOGGER.debug("Fermob %s: reconnected (lamp keeps GATEWAY state)", self._address)
            lamp_state = None

        self._ready   = True
        self._connected = True
        _LOGGER.warning("Fermob %s: ready", self._address)
        self._schedule_idle_disconnect()
        return lamp_state

    # ------------------------------------------------------------------
    # Lamp commands
    # ------------------------------------------------------------------

    async def get_state(self) -> tuple[bool, int] | None:
        """
        Query current lamp state via DEVICE_DATA_GET.

        JS: requestModuleLightState → MESH CMD, SHORT addr, PRIVATE encryption.
        """
        payload = [14, CMD_DEVICE_DATA_GET, 0] + [0xFF] * 12
        sid = self._next_seq()
        frame = _build_short(MSG_MESH_CMD, ENCRYPT_PRIVATE, payload, sid,
                             self._pub, self._priv, self._nonce,
                             b2=self._addr_b2, b3=self._addr_b3)
        pl, _ = await self._send_frames([frame])
        if pl:
            return _parse_device_state(pl)
        return None

    async def send_led(self, on: bool, brightness_pct: int | None = None) -> None:
        """
        Send DEVICE_DATA_SET (CMD_WITH_NO_ACK / FIRE, PRIVATE, lmp_short).

        Hoopik GL1200 = Dimmable White (DW):
          payload: [6, 0x41, dev_index=0, on_byte, level, fade_lo, fade_hi]
          on_byte = (1|0) | (LED_MODE_DW << 4)
        """
        if brightness_pct is None:
            brightness_pct = DEFAULT_BRIGHTNESS_PCT
        level   = max(0, min(100, brightness_pct))
        on_byte = (0x01 if on else 0x00) | (LED_MODE_DW << 4)
        sid = self._next_seq()
        pkt = _build_short(
            MSG_FIRE, ENCRYPT_PRIVATE,
            [6, CMD_DEVICE_DATA_SET, 0x00, on_byte, level, FADE & 0xFF, FADE >> 8],
            sid, self._pub, self._priv, self._nonce,
            b2=self._addr_b2, b3=self._addr_b3,
        )
        _LOGGER.debug("Fermob %s →FIRE %s", self._address, pkt.hex())
        await self._client.write_gatt_char(CHAR_UUID, pkt, response=False)

    async def unpair(self) -> None:
        """Send LMP_COMMAND_UNREGISTER broadcast (JS "Forget")."""
        sid = self._next_seq()
        pkt = _build_short(
            MSG_FIRE, ENCRYPT_PRIVATE,
            [1, CMD_UNREGISTER],
            sid, self._pub, self._priv, self._nonce,
            b2=0xFF, b3=0xFF,
        )
        _LOGGER.warning("Fermob %s: sending UNREGISTER broadcast", self._address)
        await self._client.write_gatt_char(CHAR_UUID, pkt, response=False)
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# HA platform
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    address = entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
    store   = Store(hass, _STORAGE_VERSION, f"fermob_{address.replace(':', '_').lower()}")
    conn    = FermobBLEConnection(hass, address, store)
    entity  = FermobLight(hass, entry, conn)
    conn.on_state_change = entity.on_lamp_state_change
    async_add_entities([entity])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("unpair", {}, "async_unpair")


class FermobLight(LightEntity):
    """Representation of a Fermob BLE dimmable lamp."""

    _attr_color_mode            = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 conn: FermobBLEConnection) -> None:
        self.hass          = hass
        self._entry        = entry
        self._conn         = conn
        self._attr_is_on   = False
        self._attr_brightness = 128
        addr               = entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
        self._attr_name    = entry.data.get("name", addr)
        self._attr_unique_id = f"fermob_{addr.replace(':', '_').lower()}"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        addr = self._entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
        return DeviceInfo(
            identifiers={("fermob", addr)},
            name=self._attr_name,
            manufacturer="Fermob",
            model="Hoopik GL1200",
        )

    def on_lamp_state_change(self, is_on: bool, level: int) -> None:
        """Called when the lamp sends an unsolicited state EVENT."""
        self._attr_is_on      = is_on
        self._attr_brightness = round(level / 100 * 255)
        _LOGGER.debug("Fermob %s: state update is_on=%s level=%d",
                      self._entry.data.get(CONF_ADDRESS), is_on, level)
        self.schedule_update_ha_state()


    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_ha  = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 128)
        brightness_pct = round(brightness_ha / 255 * 100)

        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.send_led(True, brightness_pct)
                self._attr_is_on      = True
                self._attr_brightness = brightness_ha
            except Exception as exc:
                _LOGGER.error("Fermob %s turn_on error: %s",
                              self._entry.data.get(CONF_ADDRESS), exc, exc_info=True)
                await self._conn.disconnect()
                return

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.send_led(False, None)
                self._attr_is_on = False
            except Exception as exc:
                _LOGGER.error("Fermob %s turn_off error: %s",
                              self._entry.data.get(CONF_ADDRESS), exc, exc_info=True)
                await self._conn.disconnect()
                return

        self.async_write_ha_state()

    async def async_unpair(self) -> None:
        """Unpair the lamp and remove this config entry."""
        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.unpair()
            except Exception as exc:
                _LOGGER.error("Fermob %s unpair error: %s",
                              self._entry.data.get(CONF_ADDRESS), exc, exc_info=True)
            finally:
                await self._conn.disconnect()
                await self._conn._store.async_remove()
                self._conn._keys_loaded = False
                self._conn._have_keys   = False

        await self.hass.config_entries.async_remove(self._entry.entry_id)