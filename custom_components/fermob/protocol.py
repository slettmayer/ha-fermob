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

from datetime import datetime
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
CMD_DATETIME_SET = 26  # 0x1A — set the module's own clock (JS setModuleTime)
CMD_DEVICE_DATA_SET = 65
CMD_DEVICE_DATA_GET = 66
# 0x4A — the state read the app's `requestLatestsModuleStatuses` builds but, as
# a 2026-08-04 packet capture showed, never actually sends. The H134 accepts it
# where it rejects DEVICE_DATA_GET, yet answers with a stored record that never
# changes, so nothing sends it here either. Kept as protocol documentation; the
# traces are in docs/domain/LINKIO-PROTOCOL.md.
CMD_DEVICES_DATA_LIST_GET = 74

# Payload marker of a DEVICE_DATA notification (payload[1])
LMP_STATUS_ACK = 128  # 0x80 — an acknowledgement TLV: [len, 0x80, err, ...]
LMP_EVENT_DEVICE_DATA = 146  # unsolicited state push — live, and trustworthy
LMP_STATUS_DEVICE_DATA = 147  # state pushed in reply to a query — stale

# The two markers carry an identical body, so the same parser reads both, but
# they do NOT mean the same thing and only 146 may be applied to an entity:
#
#   146  the lamp volunteering a change as it happens. A vendor-app packet
#        capture (2026-08-04) showed one for every physical button press, each
#        correctly reporting on/off and both channels.
#   147  the reply to DEVICES_DATA_LIST_GET (74), which is a *stored* record and
#        on an H134 was frozen: it reported the lamp off while it was lit. The
#        app never sends 74 at all.
#
# Nothing sends 74 any more, so 147 should never arrive; `_dispatch_event`
# still refuses it explicitly rather than by omission, because "accept both,
# they look the same" is the mistake this pair of constants exists to prevent.
DEVICE_DATA_MARKERS = (LMP_EVENT_DEVICE_DATA, LMP_STATUS_DEVICE_DATA)

# LMP error codes (JS lmp_error_codes_e), used in the third byte of an ACK.
#
# This is the app's table verbatim -- ids 0-11 and 20, nothing between. Do not
# add invented names: an earlier `18: "INVALID_SIZE"` was not the manufacturer's
# and it cost two wrong diagnoses, because the H134 answers DEVICE_DATA_GET with
# 18 and the made-up name read as the firmware complaining about the payload
# size. Unknown codes surface as `UNKNOWN(n)` via `error_name`, which is the
# honest rendering.
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
    20: "ITEM_NOT_FOUND",
}

LMP_ERROR_UNREGISTERED = 5
LMP_ERROR_CRYPT_MSG = 7

# The two refusals that mean "I do not hold your keys" rather than "I decline
# this command". Observed on hardware 2026-08-06: a lamp factory-reset behind
# Home Assistant's back answers an addressed PRIVATE frame with CRYPT_MSG (7)
# instead of going silent, so a caller that treats any answer as a working
# session is talking to a lamp that cannot read a word of it.
#
# This is *stronger* evidence than the REGISTER(0) probe: the lamp is stating
# the crypto relationship is broken, rather than us inferring it from which
# mode it replies in.
CRYPTO_REJECTION_ERRORS = frozenset({LMP_ERROR_UNREGISTERED, LMP_ERROR_CRYPT_MSG})

LMP_PARAM_BATTERY_LEVEL = 192  # 0xc0 — bit7 = charging, bits0-6 = percent
LMP_PARAM_SHORT_ADDRESS = 177  # 0xb1
LMP_PARAM_MANUFACTURER_NAME = 178  # 0xb2 — NUL-padded ASCII, e.g. "Fermob"
LMP_PARAM_MODEL = 179  # 0xb3 — NUL-padded ASCII, e.g. "MOOON - H134"
LMP_PARAM_MODULE_TYPE = 180  # 0xb4 — little-endian uint16
LMP_PARAM_MODULE_SW_VERSION = 181  # 0xb5 — four bytes, reordered (see below)
LMP_PARAM_MODULE_HW_VERSION = 182  # 0xb6 — three bytes, in order
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


def _ascii_field(value: bytes) -> str | None:
    """Decode a NUL-padded fixed-width ASCII TLV, or None if it says nothing.

    Anything undecodable is dropped rather than allowed to raise: every caller
    here is a diagnostic string, and none of them is worth failing a connect
    over.
    """
    text = value.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    return text or None


def format_sw_version(value: bytes) -> str | None:
    """Render the `0xb5` software version the way the vendor app reads it.

    The app takes the four value bytes **reordered** as `[v1, v2, v3, v0]` — the
    first byte is the *last* component, not the first. On the H134 `00 02 03 15`
    is therefore `2.3.21.0`, not `0.2.3.21`.

    *Derived from the app's JS* (`m_firmware_version=[e[o+3],e[o+4],e[o+5],e[o+2]]`),
    and consistent with the versions the vendor's own update server serves — see
    [docs/domain/FIRMWARE-UPDATE.md]. Never checked against a screen that
    displays a version, so the order is the app's claim, not a verified fact.
    """
    if len(value) < 4:
        return None
    return ".".join(str(b) for b in (value[1], value[2], value[3], value[0]))


def format_hw_version(value: bytes) -> str | None:
    """Render the `0xb6` hardware version, which the app reads *in* order."""
    if len(value) < 3:
        return None
    return ".".join(str(b) for b in value[:3])


def _version_parts(version: str) -> list[int]:
    """Split a dotted version into three ints, padding and tolerating junk."""
    parts: list[int] = []
    for part in version.split(".")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return parts + [0] * (3 - len(parts))


def compare_versions(left: str, right: str) -> int:
    """Compare two dotted versions over their first three components.

    Three components because that is what the app's own `compVersion` compares,
    and the fourth is not always present: the update server serves `3.0.24` for
    one model and `3.0.24.0` for the next, while a lamp always reports four.

    We differ from the app in padding a missing component with `0` rather than
    special-casing "absent"; the two agree on everything the server serves.
    """
    a, b = _version_parts(left), _version_parts(right)
    for x, y in zip(a, b, strict=True):
        if x != y:
            return 1 if x > y else -1
    return 0


class ModuleInfo(NamedTuple):
    """What we read out of a MODULE_INFO_GET response.

    Everything but the address is None when the lamp did not include it, so a
    caller can tell "not reported" from "reported as something we don't know".
    """

    addr_b2: int
    addr_b3: int
    api_version: int
    module_type: int | None
    model: str | None
    manufacturer: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None


def parse_module_info(payload: bytes) -> ModuleInfo:
    """Read the address, API version, module_type, names and versions."""
    addr_b2 = addr_b3 = 0
    api_ver = 2
    module_type: int | None = None
    model: str | None = None
    manufacturer: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None

    for t_type, value in iter_tlv(payload):
        if t_type == LMP_PARAM_SHORT_ADDRESS and len(value) >= 2:
            addr_b2 = value[0]
            addr_b3 = value[1]
        elif t_type == LMP_PARAM_API_VERSION and len(value) >= 1:
            api_ver = value[0]
        elif t_type == LMP_PARAM_MODULE_TYPE and len(value) >= 2:
            module_type = value[0] | (value[1] << 8)
        elif t_type == LMP_PARAM_MODEL and value:
            model = _ascii_field(value)
        elif t_type == LMP_PARAM_MANUFACTURER_NAME and value:
            manufacturer = _ascii_field(value)
        elif t_type == LMP_PARAM_MODULE_SW_VERSION and value:
            sw_version = format_sw_version(value)
        elif t_type == LMP_PARAM_MODULE_HW_VERSION and value:
            hw_version = format_hw_version(value)

    return ModuleInfo(
        addr_b2,
        addr_b3,
        api_ver,
        module_type,
        model,
        manufacturer,
        sw_version,
        hw_version,
    )


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


class DeviceRecord(NamedTuple):
    """One device-state record as the lamp stores it.

    `ch1`/`ch2` depend on the lamp family -- (level, 0) for DW, (cold, warm) for
    TW -- and the caller interprets them according to its configured type.

    `timestamp` is the lamp's own clock, not ours. Nothing branches on it; it is
    carried so the connection can log it, which is the only way to see from the
    outside whether `DATETIME_SET` took effect. An H134 that had never been sent
    one stamped every record `37` -- thirty-seven seconds -- forever.
    """

    is_on: bool
    ch1: int
    ch2: int
    timestamp: int


def parse_device_record(payload: bytes) -> DeviceRecord | None:
    """Parse a DEVICE_DATA push into a dated record, or None if it is not one.

    (JS: `is_on = e[8] & 0x0F`, `cold = e[9]`, `warm = e[10]`.) Bytes 3..6 hold
    the little-endian timestamp the lamp stamped the record with -- the app
    never reads it back, and neither does anything here beyond the log.
    """
    if len(payload) < 10:
        return None
    if payload[7] != 0:
        return None
    return DeviceRecord(
        is_on=bool(payload[8] & 0x0F),
        ch1=payload[9],
        ch2=payload[10] if len(payload) >= 11 else 0,
        timestamp=int.from_bytes(payload[3:7], "little"),
    )


def parse_device_state(payload: bytes) -> tuple[bool, int, int] | None:
    """Undated view of `parse_device_record`, for callers that ignore the clock.

    Returns (is_on, ch1, ch2); the meaning of the two channel bytes depends on
    the lamp family:
      * DW -> (is_on, level,      0)
      * TW -> (is_on, cold_white, warm_white)
    """
    record = parse_device_record(payload)
    if record is None:
        return None
    return record.is_on, record.ch1, record.ch2


def local_time_seconds(now: datetime) -> int:
    """The app's `getLocalTime()` — local wall clock, labelled as if it were UTC.

    JS does `Math.round((Date.now() + -getTimezoneOffset() * 60000) / 1000)`,
    i.e. it adds the local UTC offset *before* dividing, so a lamp in Vienna is
    told 12:00 when it is 12:00 there rather than 10:00Z. Reproduced rather than
    corrected: the lamp's records must be comparable with the ones the app
    writes.

    `now` must be timezone-aware -- a naive value has no offset to add and would
    silently stamp the lamp with UTC.
    """
    offset = now.utcoffset()
    if offset is None:
        raise ValueError("local_time_seconds needs a timezone-aware datetime")
    return round(now.timestamp() + offset.total_seconds())


def _le32(value: int) -> tuple[int, int, int, int]:
    """Split a 32-bit value into little-endian bytes, as the JS shifts do."""
    v = value & 0xFFFFFFFF
    return v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF


def build_datetime_set_payload(local_secs: int) -> list[int]:
    """Body of DATETIME_SET (JS `setModuleTime`).

    `[5, 26, t0, t1, t2, t3]` -- the leading 5 counts the command byte plus its
    four timestamp bytes. The app sends it as CMD_WITH_NO_ACK, PRIVATE,
    SHORT-addressed, and never waits for a reply.
    """
    return [5, CMD_DATETIME_SET, *_le32(local_secs)]


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
