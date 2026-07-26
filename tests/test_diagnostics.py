"""Unit tests for the battery-probe diagnostics helpers.

Scratch branch only. These cover the pure helpers in `protocol.py` that back
the probe in `light.py`; the probe itself is BLE-side and is exercised by
running it against a real lamp, not here. Delete this file together with the
probe if the answer comes back "the lamp does not tell us".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Same standalone load as test_protocol.py: protocol.py must stay importable
# without Home Assistant.
_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "fermob" / "protocol.py"
)
_SPEC = importlib.util.spec_from_file_location("fermob_protocol_diag", _PATH)
protocol = importlib.util.module_from_spec(_SPEC)
sys.modules["fermob_protocol_diag"] = protocol
_SPEC.loader.exec_module(protocol)


# ---------------------------------------------------------------------------
# iter_tlv
# ---------------------------------------------------------------------------


def test_iter_tlv_reads_length_type_value_entries():
    # [len=3, type=0xb1, 0x12, 0x34], [len=2, type=0xb8, 0x02]
    payload = bytes([3, 0xB1, 0x12, 0x34, 2, 0xB8, 0x02])
    assert protocol.iter_tlv(payload) == [
        (0xB1, b"\x12\x34"),
        (0xB8, b"\x02"),
    ]


def test_iter_tlv_stops_at_zero_length_terminator():
    payload = bytes([2, 0xB8, 0x02, 0, 3, 0xB1, 0x12, 0x34])
    assert protocol.iter_tlv(payload) == [(0xB8, b"\x02")]


def test_iter_tlv_stops_on_truncated_tail():
    # Trailing length byte with no type byte behind it.
    payload = bytes([2, 0xB8, 0x02, 4])
    assert protocol.iter_tlv(payload) == [(0xB8, b"\x02")]


def test_iter_tlv_clamps_a_value_that_overruns_the_payload():
    # Declares 4 value bytes, only 2 are present: slice, do not raise.
    payload = bytes([5, 0x42, 0xAA, 0xBB])
    assert protocol.iter_tlv(payload) == [(0x42, b"\xaa\xbb")]


def test_iter_tlv_empty_payload():
    assert protocol.iter_tlv(b"") == []


def test_iter_tlv_agrees_with_parse_module_info_on_a_real_shaped_payload():
    """The two walks are separate code; they must not disagree on live data."""
    payload = bytes([3, protocol.LMP_PARAM_SHORT_ADDRESS, 0x12, 0x34])
    payload += bytes([2, protocol.LMP_PARAM_API_VERSION, 0x02])
    addr_b2, addr_b3, api_ver = protocol.parse_module_info(payload)
    entries = dict(protocol.iter_tlv(payload))
    assert entries[protocol.LMP_PARAM_SHORT_ADDRESS] == bytes([addr_b2, addr_b3])
    assert entries[protocol.LMP_PARAM_API_VERSION][0] == api_ver


# ---------------------------------------------------------------------------
# unknown_module_info_tlvs
# ---------------------------------------------------------------------------


def test_unknown_module_info_tlvs_filters_the_two_known_types():
    payload = bytes([3, protocol.LMP_PARAM_SHORT_ADDRESS, 0x12, 0x34])
    payload += bytes([2, protocol.LMP_PARAM_API_VERSION, 0x02])
    payload += bytes([2, 0x5A, 0x63])  # unknown type carrying 99
    assert protocol.unknown_module_info_tlvs(payload) == [(0x5A, b"\x63")]


def test_unknown_module_info_tlvs_empty_when_everything_is_known():
    payload = bytes([3, protocol.LMP_PARAM_SHORT_ADDRESS, 0x12, 0x34])
    payload += bytes([2, protocol.LMP_PARAM_API_VERSION, 0x02])
    assert protocol.unknown_module_info_tlvs(payload) == []


def test_known_module_info_params_matches_what_parse_module_info_consumes():
    assert {
        protocol.LMP_PARAM_SHORT_ADDRESS,
        protocol.LMP_PARAM_API_VERSION,
    } == protocol.KNOWN_MODULE_INFO_PARAMS


# ---------------------------------------------------------------------------
# byte_table
# ---------------------------------------------------------------------------


def test_byte_table_renders_index_and_decimal_value():
    assert protocol.byte_table(bytes([0, 50, 255])) == "00=000 01=050 02=255"


def test_byte_table_is_fixed_width_so_two_dumps_line_up():
    a = protocol.byte_table(bytes([1] * 15))
    b = protocol.byte_table(bytes([100] * 15))
    assert len(a) == len(b)


def test_byte_table_empty_payload():
    assert protocol.byte_table(b"") == ""
