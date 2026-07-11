"""Fermob BLE light entity — mirrors JS meshConnectionService / BLEProtocolService.

MOOON support (tunable white) added on top of the original Hoopik (dimmable white)
integration by edouardrosset. See the PR description for the reverse-engineering notes.

Two lamp families share the identical Linkio handshake/crypto and the identical
DEVICE_DATA_SET command *header* (led_mode = LEDS_MODE_COLOR = 1):

  * Dimmable White (DW)  — Hoopik L1200 (model_id 3, module_type 401)
        payload: [6, 0x41, dev, on_byte, level,             fade_lo, fade_hi]
  * Tunable  White (TW)  — every MOOON / table lamp (module_type 404)
        payload: [7, 0x41, dev, on_byte, cold_white, warm_white, fade_lo, fade_hi]

on_byte = (1 if on else 0) | (led_mode << 4)      # led_mode = 1 -> 0x11 / 0x10

The TW lamp mixes two intensity channels:
    warm_white = round(brightness% * warm_ratio)
    cold_white = brightness% - warm_white
    (warm_white + cold_white) == brightness%      # total output
Colour temperature 3000 K (all warm) .. 6000 K (all cold).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

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
CMD_DEVICE_INFO_GET      = 50
CMD_DEVICE_DATA_SET      = 65
CMD_DEVICE_DATA_GET      = 66

LMP_PARAM_SHORT_ADDRESS = 177  # 0xb1
LMP_PARAM_API_VERSION   = 184  # 0xb8

# JS lmp_led_mode.LEDS_MODE_COLOR — used for BOTH DW and TW commands
LED_MODE_COLOR = 1
# JS fade_timing_10.color_transition (ms)
FADE = 50

# Lamp families
LIGHT_TYPE_DW = "dw"   # dimmable white  (Hoopik, module_type 401)
LIGHT_TYPE_TW = "tw"   # tunable  white  (MOOON,  module_type 404)

# JS device-class table (manufacturer_id 7): only Hoopik L1200 is DW.
MODULE_TYPE_DW = 401
MODULE_TYPE_TW = 404

# Tunable-white colour-temperature envelope (Fermob spec: 3000 K .. 6000 K)
MIN_KELVIN = 3000
MAX_KELVIN = 6000

DEFAULT_BRIGHTNESS_PCT = 50
DEFAULT_KELVIN         = 4000

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


def _parse_device_state(payload: bytes) -> tuple[bool, int, int] | None:
    """Parse DEVICE_DATA_GET response or EVENT_DEVICE_DATA notification.

    Returns (is_on, ch1, ch2) where the meaning of the two channel bytes
    depends on the lamp family:
      * DW  -> (is_on, level,      0)          payload[9] = level
      * TW  -> (is_on, cold_white, warm_white) payload[9] = cold, payload[10] = warm

    (JS: `is_on = e[8] & 0x0F`, `cold = e[9]`, `warm = e[10]`.)
    The caller interprets ch1/ch2 according to its configured light_type.
    """
    if len(payload) < 10:
        return None
    if payload[7] != 0:
        return None
    is_on = bool(payload[8] & 0x0F)
    ch1 = payload[9]
    ch2 = payload[10] if len(payload) >= 11 else 0
    return is_on, ch1, ch2


# ---------------------------------------------------------------------------
# Connection — mirrors JS meshConnectionService + BLEProtocolService
# (Handshake / crypto / reconnect logic unchanged from the original integration.)
# ---------------------------------------------------------------------------

class FermobBLEConnection:
    """Manages a persistent BLE connection to one Fermob lamp."""

    def __init__(self, hass: HomeAssistant, address: str, store: Store,
                 light_type: str = LIGHT_TYPE_TW) -> None:
        self.hass     = hass
        self._address = address
        self._store   = store
        self.light_type = light_type      # "dw" | "tw"
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
        self.on_state_change: Any = None  # (is_on, ch1, ch2) -> None

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
                            is_on, ch1, ch2 = state
                            _LOGGER.debug("Fermob %s: EVENT is_on=%s ch1=%d ch2=%d",
                                          self._address, is_on, ch1, ch2)
                            self.on_state_change(is_on, ch1, ch2)
                except Exception:
                    pass
            else:
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

            if mt == 4:
                if self._ready and self.on_state_change is not None:
                    try:
                        pl = _decode_fragment(frame, resp_enc,
                                               self._pub, self._priv, self._nonce)
                        if len(pl) >= 10 and pl[1] == 146:
                            state = _parse_device_state(pl)
                            if state is not None:
                                is_on, ch1, ch2 = state
                                _LOGGER.debug(
                                    "Fermob %s: EVENT (in-flight) is_on=%s ch1=%d ch2=%d",
                                    self._address, is_on, ch1, ch2)
                                self.on_state_change(is_on, ch1, ch2)
                    except Exception:
                        pass
                else:
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
        while True:
            try:
                await asyncio.wait_for(self._ack_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break

    async def _wait_for_event(self, timeout: float = 0.5) -> tuple[bool, int, int] | None:
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
                continue
            resp_enc = (h0 >> 3) & 3
            try:
                pl = _decode_fragment(frame, resp_enc,
                                       self._pub, self._priv, self._nonce)
                if len(pl) >= 10 and pl[1] == 146:
                    state = _parse_device_state(pl)
                    if state is not None:
                        _LOGGER.debug(
                            "Fermob %s: EVENT after REGISTER_END %s",
                            self._address, state)
                        return state
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Initial pairing handshake (first-time only, mirrors JS startPairing)
    # ------------------------------------------------------------------

    async def _pairing_handshake(self) -> tuple[bool, int, int] | None:
        _LOGGER.warning("Fermob %s: fresh pairing", self._address)

        probe_pl, probe_enc = await self._send(ENCRYPT_NONE, [2, CMD_REGISTER, 0])
        if probe_enc == ENCRYPT_PRIVATE:
            raise RuntimeError(
                "Lamp is in PRIVATE mode but no stored keys found. "
                "Factory-reset the lamp (hold button 10 s) and delete "
                ".storage/fermob_* before retrying."
            )

        pl, _ = await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_AUTHKEY_GET, 0])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GET failed")
        self._pub = bytes(pl[3:19])

        pl, _ = await self._send(ENCRYPT_NONE, [1, CMD_CRYPT_NONCE_GENERATE])
        if not pl or len(pl) < 19:
            raise RuntimeError("NONCE_GENERATE failed")
        self._nonce = bytes(pl[3:19])

        await self._send(ENCRYPT_NONE, [2, CMD_CRYPT_SET, ENCRYPT_PUBLIC])

        pl, _ = await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_AUTHKEY_GEN, 1])
        if not pl or len(pl) < 19:
            raise RuntimeError("AUTHKEY_GEN failed")
        self._priv = bytes(pl[3:19])

        await self._send(ENCRYPT_PUBLIC, [2, CMD_CRYPT_SET, ENCRYPT_PRIVATE])

        pl, _ = await self._send(ENCRYPT_PRIVATE, [1, CMD_MODULE_INFO_GET])
        if pl:
            self._addr_b2, self._addr_b3, _ = _parse_module_info(pl)

        await self._send(ENCRYPT_PRIVATE, [1, CMD_DEVICE_INFO_GET])

        await self._save_keys()
        self._have_keys = True

        await self._send(ENCRYPT_PRIVATE, [2, CMD_REGISTER, 1])

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

    async def ensure_connected(self) -> tuple[bool, int, int] | None:
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection
        from homeassistant.components.bluetooth import async_ble_device_from_address

        have_keys = await self._load_keys()

        if self._connected and self._client and self._client.is_connected:
            self._schedule_idle_disconnect()
            return None

        device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            raise RuntimeError(f"Fermob BLE device not found: {self._address}")

        _LOGGER.warning("Fermob %s: connecting…", self._address)
        self._client = await establish_connection(BleakClient, device, self._address)

        while not self._ack_queue.empty():
            self._ack_queue.get_nowait()

        await self._client.start_notify(CHAR_UUID, self._notif_handler)

        lamp_state: tuple[bool, int, int] | None = None

        if not have_keys:
            lamp_state = await self._pairing_handshake()
        else:
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

    async def get_state(self) -> tuple[bool, int, int] | None:
        """Query current lamp state via DEVICE_DATA_GET (MESH CMD, SHORT addr)."""
        payload = [14, CMD_DEVICE_DATA_GET, 0] + [0xFF] * 12
        sid = self._next_seq()
        frame = _build_short(MSG_MESH_CMD, ENCRYPT_PRIVATE, payload, sid,
                             self._pub, self._priv, self._nonce,
                             b2=self._addr_b2, b3=self._addr_b3)
        pl, _ = await self._send_frames([frame])
        if pl:
            return _parse_device_state(pl)
        return None

    async def send_led(self, on: bool,
                       brightness_pct: int | None = None,
                       warm_ratio: float = 0.5) -> None:
        """Send DEVICE_DATA_SET (FIRE / no-ACK, PRIVATE, lmp_short).

        DW (Hoopik):  [6, 0x41, dev, on_byte, level,             fade_lo, fade_hi]
        TW (MOOON):   [7, 0x41, dev, on_byte, cold_white, warm_white, fade_lo, fade_hi]

        on_byte = (1|0) | (LED_MODE_COLOR << 4)     # 0x11 on / 0x10 off
        warm_ratio in [0,1]: 1.0 = 3000 K (all warm), 0.0 = 6000 K (all cold).
        """
        if brightness_pct is None:
            brightness_pct = DEFAULT_BRIGHTNESS_PCT
        level   = max(0, min(100, brightness_pct))
        on_byte = (0x01 if on else 0x00) | (LED_MODE_COLOR << 4)

        if self.light_type == LIGHT_TYPE_TW:
            warm = max(0, min(100, round(level * warm_ratio)))
            cold = max(0, min(100, level - warm))
            payload = [7, CMD_DEVICE_DATA_SET, 0x00, on_byte,
                       cold, warm, FADE & 0xFF, FADE >> 8]
        else:  # DW
            payload = [6, CMD_DEVICE_DATA_SET, 0x00, on_byte,
                       level, FADE & 0xFF, FADE >> 8]

        sid = self._next_seq()
        pkt = _build_short(
            MSG_FIRE, ENCRYPT_PRIVATE, payload,
            sid, self._pub, self._priv, self._nonce,
            b2=self._addr_b2, b3=self._addr_b3,
        )
        _LOGGER.debug("Fermob %s →FIRE (%s) %s", self._address, self.light_type, pkt.hex())
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
# Lamp-type resolution
# ---------------------------------------------------------------------------

def _resolve_light_type(entry: ConfigEntry) -> str:
    """Decide DW vs TW for this lamp.

    Priority:
      1. Explicit override in entry.options / entry.data ("light_type").
      2. module_type captured at discovery (401 = DW, 404 = TW).
      3. Name heuristic: only the Hoopik string light is DW; everything else
         (MOOON / table lamps) is tunable white.
    """
    override = entry.options.get("light_type") or entry.data.get("light_type")
    if override in (LIGHT_TYPE_DW, LIGHT_TYPE_TW):
        return override

    module_type = entry.data.get("module_type")
    if module_type == MODULE_TYPE_DW:
        return LIGHT_TYPE_DW
    if module_type == MODULE_TYPE_TW:
        return LIGHT_TYPE_TW

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
    address    = entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
    light_type = _resolve_light_type(entry)
    store      = Store(hass, _STORAGE_VERSION, f"fermob_{address.replace(':', '_').lower()}")
    conn       = FermobBLEConnection(hass, address, store, light_type=light_type)
    entity     = FermobLight(hass, entry, conn, light_type)
    conn.on_state_change = entity.on_lamp_state_change
    async_add_entities([entity])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("unpair", {}, "async_unpair")


class FermobLight(LightEntity):
    """Representation of a Fermob BLE lamp (dimmable-white or tunable-white)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 conn: FermobBLEConnection, light_type: str) -> None:
        self.hass          = hass
        self._entry        = entry
        self._conn         = conn
        self._light_type   = light_type
        self._attr_is_on   = False
        self._attr_brightness = 128

        if light_type == LIGHT_TYPE_TW:
            self._attr_color_mode            = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_min_color_temp_kelvin = MIN_KELVIN
            self._attr_max_color_temp_kelvin = MAX_KELVIN
            self._attr_color_temp_kelvin     = DEFAULT_KELVIN
        else:
            self._attr_color_mode            = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

        addr               = entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
        self._attr_name    = entry.data.get("name", addr)
        self._attr_unique_id = f"fermob_{addr.replace(':', '_').lower()}"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        addr = self._entry.data.get(CONF_ADDRESS, "ED:BC:18:EA:CA:99")
        model = "MOOON (tunable white)" if self._light_type == LIGHT_TYPE_TW \
            else "Hoopik GL1200 (dimmable white)"
        return DeviceInfo(
            identifiers={("fermob", addr)},
            name=self._attr_name,
            manufacturer="Fermob",
            model=model,
        )

    # ------------------------------------------------------------------
    # State sync from the lamp (unsolicited EVENT or DEVICE_DATA_GET)
    # ------------------------------------------------------------------

    def on_lamp_state_change(self, is_on: bool, ch1: int, ch2: int) -> None:
        """ch1/ch2 = (level, 0) for DW, (cold_white, warm_white) for TW."""
        self._attr_is_on = is_on
        if self._light_type == LIGHT_TYPE_TW:
            cold, warm = ch1, ch2
            total = cold + warm
            if total > 0:
                self._attr_brightness = round(total / 100 * 255)
                warm_ratio = warm / total
                self._attr_color_temp_kelvin = round(
                    MAX_KELVIN - warm_ratio * (MAX_KELVIN - MIN_KELVIN)
                )
            # total == 0 (lamp off): keep last brightness/temp for the UI
        else:
            level = ch1
            if level > 0:
                self._attr_brightness = round(level / 100 * 255)

        _LOGGER.debug("Fermob %s: state is_on=%s ch1=%d ch2=%d",
                      self._entry.data.get(CONF_ADDRESS), is_on, ch1, ch2)
        self.schedule_update_ha_state()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_ha  = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 128)
        brightness_pct = max(1, round(brightness_ha / 255 * 100))

        warm_ratio = 0.5
        kelvin = self._attr_color_temp_kelvin or DEFAULT_KELVIN
        if self._light_type == LIGHT_TYPE_TW:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN, kelvin)
            kelvin = max(MIN_KELVIN, min(MAX_KELVIN, kelvin))
            warm_ratio = (MAX_KELVIN - kelvin) / (MAX_KELVIN - MIN_KELVIN)

        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.send_led(True, brightness_pct, warm_ratio)
                self._attr_is_on      = True
                self._attr_brightness = brightness_ha
                if self._light_type == LIGHT_TYPE_TW:
                    self._attr_color_temp_kelvin = kelvin
            except Exception as exc:
                _LOGGER.error("Fermob %s turn_on error: %s",
                              self._entry.data.get(CONF_ADDRESS), exc, exc_info=True)
                await self._conn.disconnect()
                return

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Preserve the current colour temperature on the off command so the lamp
        # keeps its warm/cold balance when toggled back on from the button.
        warm_ratio = 0.5
        if self._light_type == LIGHT_TYPE_TW:
            kelvin = self._attr_color_temp_kelvin or DEFAULT_KELVIN
            warm_ratio = (MAX_KELVIN - kelvin) / (MAX_KELVIN - MIN_KELVIN)

        async with self._conn.lock:
            try:
                await self._conn.ensure_connected()
                await self._conn.send_led(False, 0, warm_ratio)
                self._attr_is_on = False
            except Exception as exc:
                _LOGGER.error("Fermob %s turn_off error: %s",
                              self._entry.data.get(CONF_ADDRESS), exc, exc_info=True)
                await self._conn.disconnect()
                return

        self.async_write_ha_state()

    async def async_unpair(self) -> None:
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
