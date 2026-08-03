"""Pure Fermob/Linkio BLE protocol layer — mirrors the app's BLEProtocolService.

This module deliberately has **no Home Assistant imports** so the frame and
payload construction can be unit-tested standalone (see `tests/`).

Frame layout (20 bytes, written to LINKIO_TXRX_CHARACTERISTIC):

    [0]      header  = (msg_type << 5) | (encryption << 3) | frame_type
    [1]      cmd_id / sequence number
    [2..3]   short address (b2/b3) for mesh frames, else 0
    [4..19]  encrypted( [crc] + payload padded to 15 bytes )

`msg_type` and `frame_type` are independent: the message type says whether the
lamp must acknowledge, the frame type says how the frame is addressed. An
acknowledged, SHORT-addressed command is therefore MSG_CMD with frame type 2
(header 0x32 under PRIVATE encryption), not message type 2 -- message type 2
is CMD_ACK, which is what the *lamp* sends back.

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

from typing import NamedTuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# BLE characteristic (JS LINKIO_TXRX_CHARACTERISTIC)
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"

# Encryption modes (JS lms_header_encryption)
ENCRYPT_NONE = 0
ENCRYPT_PUBLIC = 1
ENCRYPT_PRIVATE = 2

# Frame message types (JS lmp_module_msg_type). This is the *message* type
# only -- the frame type in the low bits of the header is chosen by the
# addressing mode, not by this value, so the two are passed separately to
# `build_short`.
MSG_FIRE = 0  # CMD_WITH_NO_ACK — a command we do not expect a reply to
MSG_CMD = 1  # CMD_WITH_ACK    — a command the lamp must acknowledge
MSG_CMD_ACK = 2  # CMD_ACK         — the lamp's *reply*; we never send this
MSG_STATUS = 3  # STATUS          — solicited state push
MSG_EVENT = 4  # EVENT           — unsolicited state push

# The app routes STATUS and EVENT through one shared branch, so both carry lamp
# state and both must be dispatched to the entity.
STATE_PUSH_TYPES = (MSG_STATUS, MSG_EVENT)

# LMP command IDs (JS CODES)
CMD_REGISTER = 16
CMD_UNREGISTER = 17
CMD_CRYPT_NONCE_GENERATE = 19
CMD_CRYPT_NONCE_SET = 21
CMD_CRYPT_AUTHKEY_GEN = 22
CMD_CRYPT_AUTHKEY_GET = 23
CMD_CRYPT_AUTHKEY_SET = 24
CMD_CRYPT_SET = 25
CMD_MODULES_BATTERY_LEVEL_GET = 44  # 0x2C — battery level of one/all modules
CMD_MODULE_INFO_GET = 48
CMD_DEVICE_INFO_GET = 50
CMD_DEVICE_DATA_SET = 65
CMD_DEVICE_DATA_GET = 66

# Payload marker of an EVENT_DEVICE_DATA notification (payload[1])
LMP_STATUS_ACK = 128  # 0x80 — an acknowledgement TLV: [len, 0x80, err, ...]
LMP_EVENT_DEVICE_DATA = 146  # unsolicited state push
LMP_STATUS_DEVICE_DATA = 147  # state pushed in reply to a query

# Both markers carry an identical body, and the app parses them through one
# shared branch -- as it does for the STATUS and EVENT message types that wrap
# them. Accepting only 146/EVENT silently discarded solicited state.
DEVICE_DATA_MARKERS = (LMP_EVENT_DEVICE_DATA, LMP_STATUS_DEVICE_DATA)

# LMP error codes (JS lmp_error_codes_e), used in the third byte of an ACK.
LMP_ERRORS = {
    0: "SUCCESS",
    1: "NOT_SUPPORTED",
    2: "INVALID_COMMAND",
    3: "INVALID_PARAMETER",
    4: "INVALID_DEVICE",
    5: "UNREGISTERED",
    6: "CLEAR_MSG_UNAUTHORIZED",
    7: "CRYPT_MSG",
    8: "TIMEOUT",
    9: "CONNECT_ERROR",
    10: "MEMORY_FAIL",
    11: "MEMORY_FULL",
    18: "INVALID_SIZE",
    20: "ITEM_NOT_FOUND",
}

LMP_PARAM_BATTERY_LEVEL = 192  # 0xc0 — bit7 = charging, bits0-6 = percent
LMP_PARAM_SHORT_ADDRESS = 177  # 0xb1
LMP_PARAM_MODEL = 179  # 0xb3 — NUL-padded ASCII, e.g. "MOOON - H134"
LMP_PARAM_MODULE_TYPE = 180  # 0xb4 — little-endian uint16
LMP_PARAM_API_VERSION = 184  # 0xb8

# module_type values from the app's device-class table (manufacturer_id 7).
MODULE_TYPE_DW = 401
MODULE_TYPE_TW = 404

# JS lmp_led_mode.LEDS_MODE_COLOR — used for BOTH DW and TW commands
LED_MODE_COLOR = 1
# JS fade_timing_10.color_transition (ms)
FADE = 50

# Lamp families
LIGHT_TYPE_DW = "dw"  # dimmable white  (Hoopik, module_type 401)
LIGHT_TYPE_TW = "tw"  # tunable  white  (MOOON,  module_type 404)

_MODULE_TYPE_FAMILIES = {
    MODULE_TYPE_DW: LIGHT_TYPE_DW,
    MODULE_TYPE_TW: LIGHT_TYPE_TW,
}

# Tunable-white colour-temperature envelope (Fermob spec: 3000 K .. 6000 K)
MIN_KELVIN = 3000
MAX_KELVIN = 6000

# Mixing two fixed-CCT channels is linear in *mired* (10^6 / K), not in Kelvin,
# so the conversion runs through mired rather than interpolating Kelvin
# directly. Half warm and half cold therefore lands at 4000 K, not 4500 K.
MIRED_WARM = 1_000_000 / MIN_KELVIN  # 333.3 mired
MIRED_COLD = 1_000_000 / MAX_KELVIN  # 166.7 mired


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
    # strict: a short body would otherwise be silently truncated, producing a
    # mis-parsed payload instead of a visible failure.
    return bytes(a ^ b for a, b in zip(keystream, data16, strict=True))


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


def build_short(
    msg_type: int,
    enc: int,
    payload: list[int],
    cmd_id: int,
    pub: bytes,
    priv: bytes,
    nonce: bytes,
    b2: int = 0,
    b3: int = 0,
    addressed: bool = False,
) -> bytes:
    """Build a single 20-byte frame.

    `addressed` selects the frame type independently of `msg_type`, mirroring
    the app: a SHORT-addressed frame is `lmp_short_frame` (2), an unaddressed
    one is `local_short_frame` (0). The frame type is *not* a function of the
    message type -- CMD_WITH_ACK appears with both.
    """
    ft = 2 if addressed else 0
    hdr = ((msg_type & 7) << 5) | ((enc & 3) << 3) | ft
    p = pad15(payload)
    enc_data = crypt(bytes([crc(bytes(p)), *p]), enc, pub, priv, nonce)
    return bytes([hdr, cmd_id, b2, b3]) + enc_data


def build_long(
    enc: int, payload: list[int], cmd_id: int, pub: bytes, priv: bytes, nonce: bytes
) -> list[bytes]:
    """Build a multi-fragment frame sequence for payloads longer than 15 bytes."""
    chunks = [pad15(payload[i : i + 15]) for i in range(0, len(payload), 15)]
    frames = []
    for idx, chunk in enumerate(chunks):
        ft = 3 if idx == 0 else 6
        h0 = ((MSG_CMD & 7) << 5) | ((enc & 3) << 3) | ft
        enc_data = crypt(bytes([crc(bytes(chunk)), *chunk]), enc, pub, priv, nonce)
        frames.append(bytes([h0, cmd_id, idx, len(chunks)]) + enc_data)
    return frames


def decode_fragment(
    frame: bytes, enc: int, pub: bytes, priv: bytes, nonce: bytes
) -> bytes:
    """Decrypt one received frame and strip the CRC byte."""
    plain = crypt(bytes(frame[4:20]), enc, pub, priv, nonce)
    return plain[1:16]


def iter_tlv(payload: bytes) -> list[tuple[int, bytes]]:
    """Walk a `[length, type, *value]` TLV list and return every entry.

    `length` counts the type byte plus the value bytes, so the value is
    `payload[i + 2 : i + 1 + length]`. A zero length or a truncated tail ends
    the walk.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(payload):
        t_len = payload[i]
        if t_len == 0:
            break
        if i + 1 >= len(payload):
            break
        out.append((payload[i + 1], bytes(payload[i + 2 : i + 1 + t_len])))
        i += t_len + 1
    return out


class ModuleInfo(NamedTuple):
    """What we read out of a MODULE_INFO_GET response.

    `module_type` and `model` are None when the lamp did not include them, so a
    caller can tell "not reported" from "reported as something we don't know".
    """

    addr_b2: int
    addr_b3: int
    api_version: int
    module_type: int | None
    model: str | None


def parse_module_info(payload: bytes) -> ModuleInfo:
    """Read the short address, API version, module_type and model string."""
    addr_b2 = addr_b3 = 0
    api_ver = 2
    module_type: int | None = None
    model: str | None = None

    for t_type, value in iter_tlv(payload):
        if t_type == LMP_PARAM_SHORT_ADDRESS and len(value) >= 2:
            addr_b2 = value[0]
            addr_b3 = value[1]
        elif t_type == LMP_PARAM_API_VERSION and len(value) >= 1:
            api_ver = value[0]
        elif t_type == LMP_PARAM_MODULE_TYPE and len(value) >= 2:
            module_type = value[0] | (value[1] << 8)
        elif t_type == LMP_PARAM_MODEL and value:
            # NUL-padded to a fixed width; anything undecodable is dropped
            # rather than allowed to raise on a diagnostic string.
            text = value.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
            model = text or None

    return ModuleInfo(addr_b2, addr_b3, api_ver, module_type, model)


def module_type_to_light_type(module_type: int | None) -> str | None:
    """Map a reported module_type to a lamp family, or None if unrecognised."""
    if module_type is None:
        return None
    return _MODULE_TYPE_FAMILIES.get(module_type)


def ack_error(payload: bytes) -> int | None:
    """Return the LMP error code if `payload` is a *failed* acknowledgement.

    Returns None when the payload is a successful ACK or is not an ACK at all
    (a MODULE_INFO_GET reply, say, is a TLV list with no ACK entry). The app
    checks this byte and abandons the response when it is non-zero; we used to
    treat any correctly-sequenced reply as success, so a rejected command was
    indistinguishable from a completed one.
    """
    if len(payload) < 3 or payload[1] != LMP_STATUS_ACK:
        return None
    return payload[2] or None


def error_name(code: int) -> str:
    """Human-readable name for an LMP error code, for log messages."""
    return LMP_ERRORS.get(code, f"UNKNOWN({code})")


class Battery(NamedTuple):
    """State of charge as the lamp reports it."""

    percent: int
    charging: bool


def build_battery_request(addr_b2: int, addr_b3: int) -> list[int]:
    """Body of MODULES_BATTERY_LEVEL_GET for one module.

    Pass `0xFF, 0xFF` for the app's broadcast form (every module at once);
    both are accepted by the H134. Send it as MSG_CMD with `addressed=True`.
    """
    return [3, CMD_MODULES_BATTERY_LEVEL_GET, addr_b2, addr_b3]


def parse_battery(payload: bytes) -> Battery | None:
    """Read a battery push, or None if this payload is not one.

    The value does **not** come back in the acknowledgement -- that is a bare
    `[2, 0x80, 0x00]` success. It arrives separately as a STATUS frame whose
    payload is `[2, 0xC0, byte]`, confirmed on an H134 (`02c01b00...` = 27 %,
    not charging).

    A reported 0 is returned as-is. The caller decides what it means: the app
    treats "no value yet" as -1 and renders `--%`, so 0 should not be shown as
    an empty battery until the lamp has actually reported one.
    """
    if len(payload) < 3 or payload[1] != LMP_PARAM_BATTERY_LEVEL:
        return None
    raw = payload[2]
    return Battery(percent=raw & 0x7F, charging=bool(raw & 0x80))


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


def build_led_payload(
    light_type: str, on: bool, level: int, warm_ratio: float = 0.5
) -> list[int]:
    """Build the DEVICE_DATA_SET body for one lamp family.

    `level` is a brightness percentage (0..100). For tunable white it is split
    across the warm and cold channels so that warm + cold == level.
    """
    level = max(0, min(100, int(level)))
    on_byte = (0x01 if on else 0x00) | (LED_MODE_COLOR << 4)

    if light_type == LIGHT_TYPE_TW:
        warm = max(0, min(100, round(level * warm_ratio)))
        cold = max(0, min(100, level - warm))
        return [
            7,
            CMD_DEVICE_DATA_SET,
            0x00,
            on_byte,
            cold,
            warm,
            FADE & 0xFF,
            FADE >> 8,
        ]

    return [6, CMD_DEVICE_DATA_SET, 0x00, on_byte, level, FADE & 0xFF, FADE >> 8]


def kelvin_to_warm_ratio(kelvin: int) -> float:
    """Map a colour temperature to the warm-channel share (0.0 .. 1.0).

    Interpolates in mired, because that is how two fixed-CCT channels actually
    blend -- see `MIRED_WARM`. Interpolating Kelvin instead (which this did up
    to and including 0.5.0) overstated the temperature everywhere strictly
    between the endpoints: worst at a 4727 K slider, where the lamp in fact
    emitted about 4212 K, a 515 K error.
    """
    kelvin = max(MIN_KELVIN, min(MAX_KELVIN, kelvin))
    mired = 1_000_000 / kelvin
    return (mired - MIRED_COLD) / (MIRED_WARM - MIRED_COLD)


def warm_ratio_to_kelvin(warm_ratio: float) -> int:
    """Inverse of `kelvin_to_warm_ratio`."""
    warm_ratio = max(0.0, min(1.0, warm_ratio))
    mired = MIRED_COLD + warm_ratio * (MIRED_WARM - MIRED_COLD)
    return round(1_000_000 / mired)
