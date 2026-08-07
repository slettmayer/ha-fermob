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
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

# Load protocol.py directly rather than as custom_components.fermob.protocol:
# the package __init__ imports Home Assistant, and the point of this module is
# that it needs none of it. Keeps CI to `pip install pytest cryptography`.
_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "fermob" / "protocol.py"
)
_SPEC = importlib.util.spec_from_file_location("fermob_protocol", _PATH)
protocol = importlib.util.module_from_spec(_SPEC)
sys.modules["fermob_protocol"] = protocol
_SPEC.loader.exec_module(protocol)

from fermob_protocol import (  # noqa: E402 — must follow the loader above
    CMD_DEVICE_DATA_GET,
    CMD_DEVICE_DATA_SET,
    DEVICE_DATA_MARKERS,
    ENCRYPT_NONE,
    ENCRYPT_PRIVATE,
    ENCRYPT_PUBLIC,
    FADE,
    LED_MODE_COLOR,
    LIGHT_TYPE_DW,
    LIGHT_TYPE_TW,
    LMP_EVENT_DEVICE_DATA,
    LMP_PARAM_SHORT_ADDRESS,
    LMP_STATUS_ACK,
    LMP_STATUS_DEVICE_DATA,
    MAX_KELVIN,
    MIN_KELVIN,
    MSG_CMD,
    MSG_CMD_ACK,
    MSG_EVENT,
    MSG_FIRE,
    MSG_STATUS,
    STATE_PUSH_TYPES,
    ack_error,
    build_battery_request,
    build_datetime_set_payload,
    build_led_payload,
    build_long,
    build_short,
    crc,
    crypt,
    decode_fragment,
    error_name,
    kelvin_to_warm_ratio,
    local_time_seconds,
    pad15,
    parse_battery,
    parse_device_record,
    parse_device_state,
    parse_module_info,
    warm_ratio_to_kelvin,
)

KEY_PUB = bytes(range(16))
KEY_PRIV = bytes(range(16, 32))
NONCE = bytes(range(32, 48))


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
    # 4000 K at 100 % -> warm_ratio 1/2 -> warm 50, cold 50 (mired midpoint)
    payload = build_led_payload(LIGHT_TYPE_TW, True, 100, kelvin_to_warm_ratio(4000))
    assert payload == [7, CMD_DEVICE_DATA_SET, 0x00, 0x11, 50, 50, 50, 0]


def test_tw_extremes_are_single_channel():
    warm_only = build_led_payload(
        LIGHT_TYPE_TW, True, 80, kelvin_to_warm_ratio(MIN_KELVIN)
    )
    cold_only = build_led_payload(
        LIGHT_TYPE_TW, True, 80, kelvin_to_warm_ratio(MAX_KELVIN)
    )
    assert warm_only[4:6] == [0, 80]  # cold=0,  warm=80
    assert cold_only[4:6] == [80, 0]  # cold=80, warm=0


@pytest.mark.parametrize("level", range(0, 101))
@pytest.mark.parametrize("kelvin", (3000, 3500, 4000, 4500, 5000, 5500, 6000))
def test_tw_channels_sum_to_brightness(level, kelvin):
    """warm + cold == level for every brightness/temperature combination."""
    _, _, _, _, cold, warm, _, _ = build_led_payload(
        LIGHT_TYPE_TW, True, level, kelvin_to_warm_ratio(kelvin)
    )
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
    assert kelvin_to_warm_ratio(MIN_KELVIN) == 1.0  # 3000 K = all warm
    assert kelvin_to_warm_ratio(MAX_KELVIN) == 0.0  # 6000 K = all cold
    # An even mix is 4000 K, not the arithmetic mean 4500 K -- two fixed-CCT
    # channels blend linearly in mired. 4000 K is not exactly representable as
    # a ratio, hence approx; see test_mix_is_linear_in_mired.
    assert kelvin_to_warm_ratio(4000) == pytest.approx(0.5)
    assert warm_ratio_to_kelvin(0.5) == 4000


def test_mix_is_linear_in_mired():
    """Guards against a revert to Kelvin-linear interpolation.

    Every ratio below is a round fraction in mired and a distinctly different
    Kelvin than Kelvin-linear interpolation would give (4500 K would map to
    0.5 rather than 1/3), so this fails loudly if the mapping regresses.
    """
    for kelvin, warm_ratio in (
        (3000, 1.0),
        (3750, 0.6),
        (4000, 0.5),
        (4500, 1 / 3),
        (5000, 0.2),
        (6000, 0.0),
    ):
        assert kelvin_to_warm_ratio(kelvin) == pytest.approx(warm_ratio)
        assert warm_ratio_to_kelvin(warm_ratio) == kelvin


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
    once = crypt(data, mode, KEY_PUB, KEY_PRIV, NONCE)
    twice = crypt(once, mode, KEY_PUB, KEY_PRIV, NONCE)
    assert once != data
    assert twice == data


def test_crypt_public_and_private_use_different_keys():
    data = bytes(16)
    assert crypt(data, ENCRYPT_PUBLIC, KEY_PUB, KEY_PRIV, NONCE) != crypt(
        data, ENCRYPT_PRIVATE, KEY_PUB, KEY_PRIV, NONCE
    )


def test_crc_is_xor():
    assert crc(b"\x01\x02\x03") == 0x00
    assert crc(b"\x0f\xf0") == 0xFF


def test_pad15_terminator_then_filler():
    assert pad15([1, 2, 3]) == [1, 2, 3, 0x00] + [0xFF] * 11
    assert len(pad15([])) == 15
    assert pad15(list(range(20))) == list(range(15))  # truncated, no terminator


def test_build_short_frame_shape():
    frame = build_short(
        MSG_FIRE,
        ENCRYPT_PRIVATE,
        [1, 2],
        0x2A,
        KEY_PUB,
        KEY_PRIV,
        NONCE,
        b2=0xAB,
        b3=0xCD,
        addressed=True,
    )
    assert len(frame) == 20
    assert frame[0] == (MSG_FIRE << 5) | (ENCRYPT_PRIVATE << 3) | 2  # ft=2
    assert frame[1] == 0x2A
    assert frame[2:4] == b"\xab\xcd"


def test_frame_type_depends_on_addressing_not_message_type():
    """The frame type comes from the addressing mode alone.

    Both message types appear with both frame types in the app, so the frame
    type cannot be inferred from the message type -- which is what this module
    used to do.
    """

    def ft(msg_type, addressed):
        return (
            build_short(
                msg_type,
                ENCRYPT_NONE,
                [1],
                0,
                KEY_PUB,
                KEY_PRIV,
                NONCE,
                addressed=addressed,
            )[0]
            & 7
        )

    for msg_type in (MSG_FIRE, MSG_CMD):
        assert ft(msg_type, True) == 2  # lmp_short_frame
        assert ft(msg_type, False) == 0  # local_short_frame


def test_acknowledged_mesh_command_header_is_0x32():
    """Regression: an ACK'd, SHORT-addressed command must not use message type 2.

    Message type 2 is CMD_ACK -- the lamp's own reply. Sending it made the lamp
    read our request as an acknowledgement, so it never answered. The app sends
    CMD_WITH_ACK (1) with a SHORT address instead.
    """
    frame = build_short(
        MSG_CMD,
        ENCRYPT_PRIVATE,
        [14, CMD_DEVICE_DATA_GET, 0],
        1,
        KEY_PUB,
        KEY_PRIV,
        NONCE,
        b2=0x12,
        b3=0x34,
        addressed=True,
    )
    assert frame[0] == 0x32
    assert (frame[0] >> 5) & 7 != MSG_CMD_ACK


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
    assert [f[2] for f in frames] == [0, 1, 2]  # fragment index
    assert all(f[3] == 3 for f in frames)  # total count
    assert frames[0][0] & 7 == 3  # first fragment
    assert all(f[0] & 7 == 6 for f in frames[1:])  # continuation


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------


def _state_payload(is_on: bool, ch1: int, ch2: int) -> bytes:
    pl = bytearray(15)
    pl[7] = 0  # status OK
    pl[8] = 0x01 if is_on else 0x00
    pl[9] = ch1
    pl[10] = ch2
    return bytes(pl)


def test_parse_device_state_tw():
    assert parse_device_state(_state_payload(True, 33, 67)) == (True, 33, 67)
    assert parse_device_state(_state_payload(False, 0, 0)) == (False, 0, 0)


def test_parse_device_state_uses_low_nibble_for_on():
    pl = bytearray(_state_payload(False, 10, 20))
    pl[8] = 0x10  # led_mode bits set, on bits clear
    assert parse_device_state(bytes(pl))[0] is False
    pl[8] = 0x11
    assert parse_device_state(bytes(pl))[0] is True


def test_parse_device_state_rejects_bad_payloads():
    assert parse_device_state(b"\x00" * 9) is None  # too short
    bad_status = bytearray(_state_payload(True, 1, 2))
    bad_status[7] = 5
    assert parse_device_state(bytes(bad_status)) is None  # non-zero status


def test_parse_device_state_tolerates_missing_second_channel():
    """A 10-byte DW response has no warm byte; ch2 must default to 0."""
    assert parse_device_state(_state_payload(True, 55, 0)[:10]) == (True, 55, 0)


def test_parse_device_record_reads_the_lamps_own_stamp():
    """Bytes 3..6, little-endian. Nothing branches on it -- it gets logged.

    It is the only outside evidence that DATETIME_SET took effect: an H134 that
    had never been sent one stamped every record it wrote `37`.
    """
    pl = bytearray(_state_payload(True, 33, 67))
    pl[3:7] = (1_785_882_856).to_bytes(4, "little")
    record = parse_device_record(bytes(pl))
    assert record.timestamp == 1_785_882_856
    assert (record.is_on, record.ch1, record.ch2) == (True, 33, 67)


def test_parse_device_state_is_the_undated_view_of_the_record():
    pl = _state_payload(True, 33, 67)
    record = parse_device_record(pl)
    assert parse_device_state(pl) == (record.is_on, record.ch1, record.ch2)


# ---------------------------------------------------------------------------
# The lamp's own clock
# ---------------------------------------------------------------------------


def test_datetime_set_body():
    """[5, 26, t0..t3] -- the 5 counts the command byte plus four time bytes."""
    assert build_datetime_set_payload(0x6A70B631) == [5, 26, 0x31, 0xB6, 0x70, 0x6A]


def test_local_time_seconds_labels_local_wall_clock_as_utc():
    """The app's own quirk, reproduced rather than corrected.

    JS adds the local UTC offset before dividing, so a lamp in Vienna is told
    12:00 when it is 12:00 there. Our records have to line up with the app's.
    """
    vienna = timezone(timedelta(hours=2))
    noon = datetime(2026, 8, 5, 12, 0, 0, tzinfo=vienna)
    assert local_time_seconds(noon) == int(
        datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC).timestamp()
    )


def test_local_time_seconds_refuses_a_naive_datetime():
    """A naive value has no offset to add, and would silently stamp UTC."""
    with pytest.raises(ValueError):
        local_time_seconds(datetime(2026, 8, 5, 12, 0, 0))


def test_battery_request_body():
    """[3, 44, addr_lo, addr_hi]; 0xFF,0xFF is the app's broadcast form."""
    assert build_battery_request(0x75, 0x7E) == [3, 44, 0x75, 0x7E]
    assert build_battery_request(0xFF, 0xFF) == [3, 44, 0xFF, 0xFF]


def test_parse_battery_matches_the_h134_capture():
    """Pinned to a real payload: `02c01b00...` observed on an H134.

    The lamp reported 27 %, discharging. Bit 7 is the charging flag and the low
    seven bits are the percentage, so 0x1b decodes as (27, False).
    """
    captured = bytes.fromhex("02c01b00ffffffffffffffffffffff")
    assert parse_battery(captured) == (27, False)
    assert parse_battery(captured).percent == 27
    assert parse_battery(captured).charging is False


@pytest.mark.parametrize("percent", range(0, 101))
def test_parse_battery_splits_flag_from_percentage(percent):
    for charging in (False, True):
        raw = percent | (0x80 if charging else 0x00)
        assert parse_battery(bytes([2, 0xC0, raw])) == (percent, charging)


def test_parse_battery_ignores_other_payloads():
    """Only a 0xC0 payload is a battery reading.

    Device-data pushes share the same message types, so a marker check is the
    only thing separating them.
    """
    assert parse_battery(_state_payload(True, 33, 67)) is None
    assert parse_battery(bytes([2, LMP_STATUS_ACK, 0])) is None
    assert parse_battery(b"") is None
    assert parse_battery(bytes([2, 0xC0])) is None  # truncated before the value


def test_parse_battery_reports_zero_as_zero():
    """0 is returned as a real value; "never reported" is the caller's None."""
    assert parse_battery(bytes([2, 0xC0, 0x00])) == (0, False)
    assert parse_battery(bytes([2, 0xC0, 0x80])) == (0, True)


def test_ack_error_flags_only_failed_acks():
    """A non-zero third byte of an ACK TLV means the command was rejected."""
    assert ack_error(bytes([3, LMP_STATUS_ACK, 0, 0])) is None  # SUCCESS
    assert ack_error(bytes([3, LMP_STATUS_ACK, 5, 0])) == 5  # UNREGISTERED
    assert ack_error(bytes([3, LMP_STATUS_ACK, 7])) == 7  # CRYPT_MSG


def test_ack_error_ignores_non_ack_payloads():
    """Only ACK TLVs carry an error byte; other replies must pass through.

    A MODULE_INFO_GET reply is a TLV list whose third byte is payload data, not
    a status -- misreading it as an error would reject every successful info
    read.
    """
    module_info = bytes([3, LMP_PARAM_SHORT_ADDRESS, 0x12, 0x34])
    assert ack_error(module_info) is None
    assert ack_error(_state_payload(True, 33, 67)) is None
    assert ack_error(b"") is None
    assert ack_error(b"\x03\x80") is None  # truncated before the error byte


def test_error_name_covers_codes_and_unknowns():
    assert error_name(0) == "SUCCESS"
    assert error_name(5) == "UNREGISTERED"
    assert error_name(99) == "UNKNOWN(99)"


def test_solicited_state_is_recognised_alongside_unsolicited():
    """Both DEVICE_DATA markers and both state message types must be accepted.

    The app parses LMP_EVENT_DEVICE_DATA (146) and LMP_STATUS_DEVICE_DATA (147)
    in one branch, wrapped in either the EVENT or the STATUS message type.
    Accepting only the unsolicited pair discarded every solicited state reply.
    """
    assert DEVICE_DATA_MARKERS == (LMP_EVENT_DEVICE_DATA, LMP_STATUS_DEVICE_DATA)
    assert DEVICE_DATA_MARKERS == (146, 147)
    assert STATE_PUSH_TYPES == (MSG_STATUS, MSG_EVENT)
    assert STATE_PUSH_TYPES == (3, 4)

    # The body is marker-independent: only byte 1 differs between the two.
    for marker in DEVICE_DATA_MARKERS:
        pl = bytearray(_state_payload(True, 33, 67))
        pl[1] = marker
        assert parse_device_state(bytes(pl)) == (True, 33, 67)


def test_parse_module_info_reads_short_address():
    # TLV: len=3, type=0xb1 (short address), b2, b3 | len=2, type=0xb8, api=4
    payload = bytes([3, 0xB1, 0x12, 0x34, 2, 0xB8, 4, 0])
    assert parse_module_info(payload) == (
        0x12,
        0x34,
        4,
        None,
        None,
        None,
        None,
        None,
    )


def test_parse_module_info_defaults_when_absent():
    assert parse_module_info(b"\x00") == (0, 0, 2, None, None, None, None, None)


# ---------------------------------------------------------------------------
# MODULE_INFO_GET — module_type and model
#
# Unlike everything above, the payload below is *observed*, not constructed: it
# is the verbatim MODULE_INFO_GET response of a real Fermob MOOON! H134, read
# over a BLE proxy on 2026-08-02 (lamp D6:86:76:E8:7E:75). It is the one place in
# this suite where the expected values come from hardware rather than from our
# reading of the app.
# ---------------------------------------------------------------------------

H134_MODULE_INFO = bytes.fromhex(
    "0280000eaf757ee87686d60700000094010403b4940105b50002031504b601000002"
    "b80202c10002b90007b0757ee87686d603b1757e11b24665726d6f62000000000000"
    "0000000011b34d4f4f4f4e202d20483133340000000009b74d6f6f6e3745373500ff"
    "ffffff"
)


def test_parse_module_info_real_h134_capture():
    """Every field we consume, read off one real lamp's response."""
    info = parse_module_info(H134_MODULE_INFO)
    assert info.addr_b2 == 0x75
    assert info.addr_b3 == 0x7E
    assert info.api_version == 2
    assert info.module_type == protocol.MODULE_TYPE_TW == 404
    assert info.model == "MOOON - H134"


def test_parse_module_info_real_h134_names_and_versions():
    """Manufacturer, firmware and hardware version off the same real response.

    The firmware bytes in that capture are `00 02 03 15`, which is the whole
    reason `format_sw_version` exists: read in order it would be 0.2.3.21, and
    the vendor's own release server publishes 3.x for this model -- consistent
    with the app's reordering and not with the naive reading.
    """
    info = parse_module_info(H134_MODULE_INFO)
    assert info.manufacturer == "Fermob"
    assert info.sw_version == "2.3.21.0"
    assert info.hw_version == "1.0.0"


def test_format_sw_version_moves_the_first_byte_last():
    assert protocol.format_sw_version(bytes([0x00, 0x02, 0x03, 0x15])) == "2.3.21.0"
    assert protocol.format_sw_version(bytes([0x01, 0x03, 0x00, 0x1B])) == "3.0.27.1"


def test_format_versions_return_none_when_too_short():
    """A truncated TLV must read as "not reported", never as a partial version."""
    assert protocol.format_sw_version(bytes([0, 2, 3])) is None
    assert protocol.format_hw_version(bytes([1, 0])) is None


def test_format_hw_version_reads_in_order():
    assert protocol.format_hw_version(bytes([0x01, 0x00, 0x00])) == "1.0.0"
    # A fourth byte is ignored rather than appended: the app reads three.
    assert protocol.format_hw_version(bytes([0x01, 0x02, 0x03, 0x04])) == "1.2.3"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("3.0.27.0", "2.3.21.0", 1),
        ("2.3.21.0", "3.0.27.0", -1),
        ("2.3.21.0", "2.3.21.0", 0),
        # Only the first three components count, which is what makes the
        # server's `3.0.24` and a lamp's `3.0.24.0` the same build.
        ("3.0.24", "3.0.24.0", 0),
        ("3.0.24.9", "3.0.24.0", 0),
        # Numeric, not lexicographic: "21" is above "9", not below it.
        ("2.3.21", "2.3.9", 1),
        # Junk must not raise; it compares as zero.
        ("3.0.x", "3.0.0", 0),
    ],
)
def test_compare_versions(left, right, expected):
    assert protocol.compare_versions(left, right) == expected


def test_real_capture_resolves_to_tunable_white():
    """The point of the whole change: TW by report, not by name guess."""
    info = parse_module_info(H134_MODULE_INFO)
    assert (
        protocol.module_type_to_light_type(info.module_type) == protocol.LIGHT_TYPE_TW
    )


def test_parse_module_info_module_type_is_little_endian():
    # 0x0194 == 404 must be read low byte first, not as 0x9401 (37889).
    assert parse_module_info(bytes([3, 0xB4, 0x94, 0x01, 0])).module_type == 404
    # And the dimmable-white value, 401 == 0x0191.
    assert parse_module_info(bytes([3, 0xB4, 0x91, 0x01, 0])).module_type == 401


def test_module_type_to_light_type_maps_both_families():
    assert protocol.module_type_to_light_type(401) == protocol.LIGHT_TYPE_DW
    assert protocol.module_type_to_light_type(404) == protocol.LIGHT_TYPE_TW


@pytest.mark.parametrize("unknown", [None, 0, 1, 402, 999, 37889])
def test_module_type_to_light_type_returns_none_when_unrecognised(unknown):
    """An unknown module_type must fall through to the name heuristic.

    Guessing a family here would silently send the wrong payload layout, which
    is exactly the tunable-white bug this integration exists to fix.
    """
    assert protocol.module_type_to_light_type(unknown) is None


def test_model_string_strips_nul_padding():
    value = b"MOOON - H134".ljust(16, b"\x00")
    payload = bytes([17, 0xB3]) + value + b"\x00"
    assert parse_module_info(payload).model == "MOOON - H134"


def test_model_string_absent_stays_none_not_empty():
    """All-NUL must read as "not reported", so a caller can tell it apart."""
    payload = bytes([17, 0xB3]) + bytes(16) + b"\x00"
    assert parse_module_info(payload).model is None


def test_iter_tlv_walks_every_entry_and_stops_at_terminator():
    payload = bytes([2, 0xB8, 0x02, 3, 0xB1, 0x12, 0x34, 0, 0xFF, 0xFF])
    assert protocol.iter_tlv(payload) == [(0xB8, b"\x02"), (0xB1, b"\x124")]


def test_iter_tlv_survives_a_truncated_tail():
    """A length that overruns the buffer must not raise; it yields short."""
    assert protocol.iter_tlv(bytes([9, 0xB3, 0x41])) == [(0xB3, b"A")]
    assert protocol.iter_tlv(bytes([3])) == []


def test_parse_module_info_ignores_unknown_tlvs():
    """Unknown types must be skipped by length, not misread as known ones."""
    payload = bytes([2, 0xC1, 0x00, 7, 0xB0, 1, 2, 3, 4, 5, 6, 3, 0xB1, 0xAB, 0xCD, 0])
    info = parse_module_info(payload)
    assert (info.addr_b2, info.addr_b3) == (0xAB, 0xCD)
