"""Unit tests for the pure Fermob/Linkio protocol layer.

Scope and honesty note: these tests pin the frame/payload layout as this
integration builds it, and they check the invariants the tunable-white mixing is
supposed to hold. They are a regression guard and a specification of intent --
they are *not* independent verification against the official app, which nobody
here can run. The one exception is `test_dw_payload_matches_upstream_literal`,
which re-expresses the original Hoopik payload independently of the
implementation and so does substantiate the "dimmable white is unchanged" claim.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load protocol.py directly rather than as custom_components.fermob.protocol:
# the package __init__ imports Home Assistant, and the point of this module is
# that it needs none of it. Keeps CI to `pip install pytest cryptography`.
_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "fermob" / "protocol.py"
_SPEC = importlib.util.spec_from_file_location("fermob_protocol", _PATH)
protocol = importlib.util.module_from_spec(_SPEC)
sys.modules["fermob_protocol"] = protocol
_SPEC.loader.exec_module(protocol)

from fermob_protocol import (  # noqa: E402 — must follow the loader above
    CMD_DEVICE_DATA_SET,
    ENCRYPT_NONE,
    ENCRYPT_PRIVATE,
    ENCRYPT_PUBLIC,
    FADE,
    LED_MODE_COLOR,
    LIGHT_TYPE_DW,
    LIGHT_TYPE_TW,
    MAX_KELVIN,
    MIN_KELVIN,
    MSG_CMD,
    MSG_FIRE,
    MSG_MESH_CMD,
    build_led_payload,
    build_long,
    build_short,
    crc,
    crypt,
    decode_fragment,
    kelvin_to_warm_ratio,
    pad15,
    parse_device_state,
    parse_module_info,
    warm_ratio_to_kelvin,
)

KEY_PUB   = bytes(range(16))
KEY_PRIV  = bytes(range(16, 32))
NONCE     = bytes(range(32, 48))


# ---------------------------------------------------------------------------
# Light payloads
# ---------------------------------------------------------------------------

def test_dw_payload_matches_upstream_literal():
    """The Hoopik (dimmable-white) body must stay byte-identical to 0.1.0.

    The expected value is written out longhand rather than derived from the
    implementation, so this fails if the DW frame ever drifts.
    """
    for level in (0, 1, 50, 99, 100):
        for on in (True, False):
            expected = [6, 0x41, 0x00, (0x11 if on else 0x10), level, 50, 0]
            assert build_led_payload(LIGHT_TYPE_DW, on, level) == expected


def test_tw_payload_layout():
    """Tunable white adds a channel: [7, .., cold, warm, ..] with a 7-byte body."""
    # 4000 K at 100 % -> warm_ratio 2/3 -> warm 67, cold 33
    payload = build_led_payload(LIGHT_TYPE_TW, True, 100,
                                kelvin_to_warm_ratio(4000))
    assert payload == [7, CMD_DEVICE_DATA_SET, 0x00, 0x11, 33, 67, 50, 0]


def test_tw_extremes_are_single_channel():
    warm_only = build_led_payload(LIGHT_TYPE_TW, True, 80,
                                  kelvin_to_warm_ratio(MIN_KELVIN))
    cold_only = build_led_payload(LIGHT_TYPE_TW, True, 80,
                                  kelvin_to_warm_ratio(MAX_KELVIN))
    assert warm_only[4:6] == [0, 80]   # cold=0,  warm=80
    assert cold_only[4:6] == [80, 0]   # cold=80, warm=0


@pytest.mark.parametrize("level", range(0, 101))
@pytest.mark.parametrize("kelvin", (3000, 3500, 4000, 4500, 5000, 5500, 6000))
def test_tw_channels_sum_to_brightness(level, kelvin):
    """warm + cold == level for every brightness/temperature combination."""
    _, _, _, _, cold, warm, _, _ = build_led_payload(
        LIGHT_TYPE_TW, True, level, kelvin_to_warm_ratio(kelvin))
    assert cold + warm == level
    assert 0 <= cold <= 100
    assert 0 <= warm <= 100


def test_on_byte_is_shared_by_both_families():
    """led_mode is LEDS_MODE_COLOR for both families -> 0x11 on, 0x10 off."""
    assert LED_MODE_COLOR == 1
    for light_type in (LIGHT_TYPE_DW, LIGHT_TYPE_TW):
        assert build_led_payload(light_type, True, 50)[3] == 0x11
        assert build_led_payload(light_type, False, 0)[3] == 0x10


def test_level_is_clamped():
    assert build_led_payload(LIGHT_TYPE_DW, True, 500)[4] == 100
    assert build_led_payload(LIGHT_TYPE_DW, True, -5)[4] == 0
    assert build_led_payload(LIGHT_TYPE_TW, True, 500, 1.0)[5] == 100


def test_fade_is_little_endian():
    assert build_led_payload(LIGHT_TYPE_DW, True, 50)[-2:] == [FADE & 0xFF, FADE >> 8]


# ---------------------------------------------------------------------------
# Colour temperature mapping
# ---------------------------------------------------------------------------

def test_kelvin_ratio_endpoints():
    assert kelvin_to_warm_ratio(MIN_KELVIN) == 1.0   # 3000 K = all warm
    assert kelvin_to_warm_ratio(MAX_KELVIN) == 0.0   # 6000 K = all cold
    assert kelvin_to_warm_ratio(4500) == 0.5


@pytest.mark.parametrize("kelvin", range(MIN_KELVIN, MAX_KELVIN + 1, 50))
def test_kelvin_round_trip(kelvin):
    assert warm_ratio_to_kelvin(kelvin_to_warm_ratio(kelvin)) == kelvin


def test_kelvin_clamped_outside_envelope():
    assert kelvin_to_warm_ratio(2200) == 1.0
    assert kelvin_to_warm_ratio(9000) == 0.0
    assert warm_ratio_to_kelvin(-1.0) == MAX_KELVIN
    assert warm_ratio_to_kelvin(2.0) == MIN_KELVIN


# ---------------------------------------------------------------------------
# Crypto / framing
# ---------------------------------------------------------------------------

def test_crypt_none_is_passthrough():
    data = bytes(range(16))
    assert crypt(data, ENCRYPT_NONE, KEY_PUB, KEY_PRIV, NONCE) == data


@pytest.mark.parametrize("mode", (ENCRYPT_PUBLIC, ENCRYPT_PRIVATE))
def test_crypt_is_symmetric(mode):
    data = bytes(range(100, 116))
    once  = crypt(data, mode, KEY_PUB, KEY_PRIV, NONCE)
    twice = crypt(once, mode, KEY_PUB, KEY_PRIV, NONCE)
    assert once != data
    assert twice == data


def test_crypt_public_and_private_use_different_keys():
    data = bytes(16)
    assert (crypt(data, ENCRYPT_PUBLIC, KEY_PUB, KEY_PRIV, NONCE)
            != crypt(data, ENCRYPT_PRIVATE, KEY_PUB, KEY_PRIV, NONCE))


def test_crc_is_xor():
    assert crc(b"\x01\x02\x03") == 0x00
    assert crc(b"\x0f\xf0") == 0xFF


def test_pad15_terminator_then_filler():
    assert pad15([1, 2, 3]) == [1, 2, 3, 0x00] + [0xFF] * 11
    assert len(pad15([])) == 15
    assert pad15(list(range(20))) == list(range(15))  # truncated, no terminator


def test_build_short_frame_shape():
    frame = build_short(MSG_FIRE, ENCRYPT_PRIVATE, [1, 2], 0x2A,
                        KEY_PUB, KEY_PRIV, NONCE, b2=0xAB, b3=0xCD)
    assert len(frame) == 20
    assert frame[0] == (MSG_FIRE << 5) | (ENCRYPT_PRIVATE << 3) | 2  # ft=2
    assert frame[1] == 0x2A
    assert frame[2:4] == b"\xab\xcd"


def test_frame_type_depends_on_message_type():
    def ft(msg_type):
        return build_short(msg_type, ENCRYPT_NONE, [1], 0,
                           KEY_PUB, KEY_PRIV, NONCE)[0] & 7
    assert ft(MSG_FIRE) == 2       # lmp_short_frame
    assert ft(MSG_MESH_CMD) == 2   # lmp_short_frame, short address
    assert ft(MSG_CMD) == 0        # local_short_frame


@pytest.mark.parametrize("enc", (ENCRYPT_NONE, ENCRYPT_PUBLIC, ENCRYPT_PRIVATE))
def test_decode_fragment_recovers_payload(enc):
    payload = [6, CMD_DEVICE_DATA_SET, 0x00, 0x11, 42, 50, 0]
    frame = build_short(MSG_FIRE, enc, payload, 1, KEY_PUB, KEY_PRIV, NONCE)
    assert list(decode_fragment(frame, enc, KEY_PUB, KEY_PRIV, NONCE)) == pad15(payload)


def test_build_long_fragments():
    payload = list(range(32))  # 15 + 15 + 2 -> 3 fragments
    frames = build_long(ENCRYPT_PRIVATE, payload, 0x07, KEY_PUB, KEY_PRIV, NONCE)
    assert len(frames) == 3
    assert all(len(f) == 20 for f in frames)
    assert [f[2] for f in frames] == [0, 1, 2]        # fragment index
    assert all(f[3] == 3 for f in frames)             # total count
    assert frames[0][0] & 7 == 3                      # first fragment
    assert all(f[0] & 7 == 6 for f in frames[1:])     # continuation


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------

def _state_payload(is_on: bool, ch1: int, ch2: int) -> bytes:
    pl = bytearray(15)
    pl[7] = 0                       # status OK
    pl[8] = 0x01 if is_on else 0x00
    pl[9] = ch1
    pl[10] = ch2
    return bytes(pl)


def test_parse_device_state_tw():
    assert parse_device_state(_state_payload(True, 33, 67)) == (True, 33, 67)
    assert parse_device_state(_state_payload(False, 0, 0)) == (False, 0, 0)


def test_parse_device_state_uses_low_nibble_for_on():
    pl = bytearray(_state_payload(False, 10, 20))
    pl[8] = 0x10          # led_mode bits set, on bits clear
    assert parse_device_state(bytes(pl))[0] is False
    pl[8] = 0x11
    assert parse_device_state(bytes(pl))[0] is True


def test_parse_device_state_rejects_bad_payloads():
    assert parse_device_state(b"\x00" * 9) is None        # too short
    bad_status = bytearray(_state_payload(True, 1, 2))
    bad_status[7] = 5
    assert parse_device_state(bytes(bad_status)) is None  # non-zero status


def test_parse_device_state_tolerates_missing_second_channel():
    """A 10-byte DW response has no warm byte; ch2 must default to 0."""
    assert parse_device_state(_state_payload(True, 55, 0)[:10]) == (True, 55, 0)


def test_parse_module_info_reads_short_address():
    # TLV: len=3, type=0xb1 (short address), b2, b3 | len=2, type=0xb8, api=4
    payload = bytes([3, 0xB1, 0x12, 0x34, 2, 0xB8, 4, 0])
    assert parse_module_info(payload) == (0x12, 0x34, 4)


def test_parse_module_info_defaults_when_absent():
    assert parse_module_info(b"\x00") == (0, 0, 2)
