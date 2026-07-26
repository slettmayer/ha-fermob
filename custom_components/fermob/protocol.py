"""Pure Fermob/Linkio BLE protocol layer — mirrors the app's BLEProtocolService.

This module deliberately has **no Home Assistant imports** so the frame and
payload construction can be unit-tested standalone (see `tests/`).

Frame layout (20 bytes, written to LINKIO_TXRX_CHARACTERISTIC):

    [0]      header  = (msg_type << 5) | (encryption << 3) | frame_type
    [1]      cmd_id / sequence number
    [2..3]   short address (b2/b3) for mesh frames, else 0
    [4..19]  encrypted( [crc] + payload padded to 15 bytes )

Encryption is an AES-ECB keystream: the 16-byte nonce is encrypted with the
public or private key and XORed over the 16-byte body.

Two lamp families share an identical handshake, crypto, framing and
DEVICE_DATA_SET (0x41) *header* — including led_mode = LEDS_MODE_COLOR = 1, so
the on-byte is the same 0x11/0x10. They differ only in the command body:

  * Dimmable White (DW) — Hoopik L1200 (model_id 3, module_type 401)
        [6, 0x41, dev, on_byte, level,             fade_lo, fade_hi]
  * Tunable  White (TW) — every MOOON! / table lamp (module_type 404)
        [7, 0x41, dev, on_byte, cold_white, warm_white, fade_lo, fade_hi]

The tunable-white lamp mixes two intensity channels, and their sum is the
total output:

    warm_white = round(brightness% * warm_ratio)
    cold_white = brightness% - warm_white

warm_ratio 1.0 = 3000 K (all warm), 0.0 = 6000 K (all cold).
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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
MSG_EVENT    = 4   # unsolicited notification from the lamp

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

# Payload marker of an EVENT_DEVICE_DATA notification (payload[1])
LMP_EVENT_DEVICE_DATA = 146

LMP_PARAM_SHORT_ADDRESS = 177  # 0xb1
LMP_PARAM_API_VERSION   = 184  # 0xb8

# JS lmp_led_mode.LEDS_MODE_COLOR — used for BOTH DW and TW commands
LED_MODE_COLOR = 1
# JS fade_timing_10.color_transition (ms)
FADE = 50

# Lamp families
LIGHT_TYPE_DW = "dw"   # dimmable white  (Hoopik, module_type 401)
LIGHT_TYPE_TW = "tw"   # tunable  white  (MOOON,  module_type 404)

# Tunable-white colour-temperature envelope (Fermob spec: 3000 K .. 6000 K)
MIN_KELVIN = 3000
MAX_KELVIN = 6000


# ---------------------------------------------------------------------------
# Crypto / frame helpers
# ---------------------------------------------------------------------------

def crypt(data16: bytes, mode: int, pub: bytes, priv: bytes, nonce: bytes) -> bytes:
    """XOR `data16` with the AES-ECB keystream derived from the nonce.

    Symmetric: applying it twice returns the original bytes, so it is used for
    both encryption and decryption.
    """
    if mode == ENCRYPT_NONE:
        return data16
    key = pub if mode == ENCRYPT_PUBLIC else priv
    encryptor = Cipher(algorithms.AES(bytes(key)), modes.ECB()).encryptor()
    keystream = encryptor.update(bytes(nonce)) + encryptor.finalize()
    return bytes(a ^ b for a, b in zip(keystream, data16))


def crc(data: bytes) -> int:
    """XOR checksum over the padded payload."""
    r = 0
    for b in data:
        r ^= b
    return r


def pad15(payload: list[int]) -> list[int]:
    """Pad a payload to 15 bytes: one 0x00 terminator, then 0xFF filler."""
    p = list(payload)
    if len(p) < 15:
        p.append(0x00)
    while len(p) < 15:
        p.append(0xFF)
    return p[:15]


def build_short(msg_type: int, enc: int, payload: list[int],
                cmd_id: int, pub: bytes, priv: bytes, nonce: bytes,
                b2: int = 0, b3: int = 0) -> bytes:
    """Build a single 20-byte frame."""
    ft = 2 if msg_type in (MSG_FIRE, MSG_MESH_CMD) else 0
    hdr = ((msg_type & 7) << 5) | ((enc & 3) << 3) | ft
    p = pad15(payload)
    enc_data = crypt(bytes([crc(bytes(p))] + p), enc, pub, priv, nonce)
    return bytes([hdr, cmd_id, b2, b3]) + enc_data


def build_long(enc: int, payload: list[int],
               cmd_id: int, pub: bytes, priv: bytes, nonce: bytes) -> list[bytes]:
    """Build a multi-fragment frame sequence for payloads longer than 15 bytes."""
    chunks = [pad15(payload[i:i + 15]) for i in range(0, len(payload), 15)]
    frames = []
    for idx, chunk in enumerate(chunks):
        ft = 3 if idx == 0 else 6
        h0 = ((MSG_CMD & 7) << 5) | ((enc & 3) << 3) | ft
        enc_data = crypt(bytes([crc(bytes(chunk))] + chunk), enc, pub, priv, nonce)
        frames.append(bytes([h0, cmd_id, idx, len(chunks)]) + enc_data)
    return frames


def decode_fragment(frame: bytes, enc: int,
                    pub: bytes, priv: bytes, nonce: bytes) -> bytes:
    """Decrypt one received frame and strip the CRC byte."""
    plain = crypt(bytes(frame[4:20]), enc, pub, priv, nonce)
    return plain[1:16]


def parse_module_info(payload: bytes) -> tuple[int, int, int]:
    """Walk the MODULE_INFO_GET TLV list for the short address and API version."""
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


def parse_device_state(payload: bytes) -> tuple[bool, int, int] | None:
    """Parse a DEVICE_DATA_GET response or EVENT_DEVICE_DATA notification.

    Returns (is_on, ch1, ch2); the meaning of the two channel bytes depends on
    the lamp family:
      * DW -> (is_on, level,      0)
      * TW -> (is_on, cold_white, warm_white)

    (JS: `is_on = e[8] & 0x0F`, `cold = e[9]`, `warm = e[10]`.)
    The caller interprets ch1/ch2 according to its configured light type.
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
# Light payloads
# ---------------------------------------------------------------------------

def build_led_payload(light_type: str, on: bool, level: int,
                      warm_ratio: float = 0.5) -> list[int]:
    """Build the DEVICE_DATA_SET body for one lamp family.

    `level` is a brightness percentage (0..100). For tunable white it is split
    across the warm and cold channels so that warm + cold == level.
    """
    level   = max(0, min(100, int(level)))
    on_byte = (0x01 if on else 0x00) | (LED_MODE_COLOR << 4)

    if light_type == LIGHT_TYPE_TW:
        warm = max(0, min(100, round(level * warm_ratio)))
        cold = max(0, min(100, level - warm))
        return [7, CMD_DEVICE_DATA_SET, 0x00, on_byte,
                cold, warm, FADE & 0xFF, FADE >> 8]

    return [6, CMD_DEVICE_DATA_SET, 0x00, on_byte,
            level, FADE & 0xFF, FADE >> 8]


def kelvin_to_warm_ratio(kelvin: int) -> float:
    """Map a colour temperature to the warm-channel share (0.0 .. 1.0)."""
    kelvin = max(MIN_KELVIN, min(MAX_KELVIN, kelvin))
    return (MAX_KELVIN - kelvin) / (MAX_KELVIN - MIN_KELVIN)


def warm_ratio_to_kelvin(warm_ratio: float) -> int:
    """Inverse of `kelvin_to_warm_ratio`."""
    warm_ratio = max(0.0, min(1.0, warm_ratio))
    return round(MAX_KELVIN - warm_ratio * (MAX_KELVIN - MIN_KELVIN))
