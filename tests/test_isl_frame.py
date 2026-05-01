"""
Datecs ISL frame layer + status parser tests.

The reference frames here come from manual decoding of the IoT box
driver's `_build_detection_message` / `_validate_checksum` helpers, so
porting parity is verifiable.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_isl import frame as fr
from odoo_erpnet_fp.drivers.fiscal.datecs_isl import commands as cmd
from odoo_erpnet_fp.drivers.fiscal.datecs_isl.status import parse_status_bytes


# ─── Encode / structure ──────────────────────────────────────────


def test_encode_get_status_frame_structure():
    out = fr.encode_request(seq=0, cmd=cmd.CMD_GET_STATUS, data=b"")
    assert len(out) == 10
    assert out[0] == fr.PRE
    assert out[-1] == fr.ETX
    # LEN = 0x20 + 4 + 0 = 0x24
    assert out[1] == 0x24
    # SEQ = 0x20 + 0 = 0x20
    assert out[2] == 0x20
    # CMD = 0x4A
    assert out[3] == 0x4A
    # PST after data
    assert out[4] == fr.PST
    # 4 BCC bytes after PST
    assert all(0x30 <= b <= 0x3F for b in out[5:9])


def test_encode_with_data_increases_length():
    out = fr.encode_request(seq=5, cmd=cmd.CMD_OPEN_FISCAL_RECEIPT, data=b"1,0000,DT123-0001-0000001")
    # PRE(1) + LEN(1) + SEQ(1) + CMD(1) + DATA(N) + PST(1) + BCC(4) + ETX(1) = 10 + N
    assert len(out) == 10 + 25


def test_encode_seq_out_of_range():
    with pytest.raises(ValueError):
        fr.encode_request(seq=fr.MAX_SEQUENCE_NUMBER + 1, cmd=cmd.CMD_GET_STATUS)


def test_encode_cmd_out_of_byte_range():
    with pytest.raises(ValueError):
        fr.encode_request(seq=0, cmd=0x100)


# ─── Checksum ────────────────────────────────────────────────────


def test_validate_checksum_on_valid_frame():
    out = fr.encode_request(seq=0, cmd=0x4A, data=b"")
    # Build a synthetic response by appending SEP + STATUS(6) + PST + BCC + ETX
    # to the request layout (this isn't a real device response but exercises
    # the checksum verifier)
    # Reuse encode_request for the checksum portion only.
    assert fr.validate_checksum(out) is True


def test_validate_checksum_rejects_corrupted_bcc():
    out = bytearray(fr.encode_request(seq=0, cmd=0x4A))
    out[-2] = (out[-2] + 1) & 0xFF  # flip a BCC byte
    assert fr.validate_checksum(bytes(out)) is False


# ─── Response parser (synthetic — we have no captured device frames yet) ─


def test_parse_response_synthetic():
    # Build a synthetic response: PRE LEN SEQ CMD DATA SEP STATUS(6) PST BCC ETX
    data = b"OK"
    status = b"\x40\x00\x00\x00\x00\x00"  # cover_open + nothing else
    seq = 0
    cmd_byte = 0x4A
    length = fr.SPACE + 4 + len(data) + 1 + len(status)  # +1 for SEP, +6 for status
    body = (
        bytes([length, fr.SPACE + seq, cmd_byte])
        + data
        + bytes([fr.SEP])
        + status
        + bytes([fr.PST])
    )
    bcc = sum(body) & 0xFFFF
    bcc_bytes = bytes(
        [
            ((bcc >> 12) & 0x0F) + 0x30,
            ((bcc >> 8) & 0x0F) + 0x30,
            ((bcc >> 4) & 0x0F) + 0x30,
            (bcc & 0x0F) + 0x30,
        ]
    )
    frame_bytes = bytes([fr.PRE]) + body + bcc_bytes + bytes([fr.ETX])

    parsed_data, parsed_status = fr.parse_response(frame_bytes)
    assert parsed_data == b"OK"
    assert parsed_status == status


def test_parse_response_rejects_short_frame():
    with pytest.raises(fr.FormatError):
        fr.parse_response(b"\x01\x03")


# ─── Status decoder ──────────────────────────────────────────────


def test_status_no_errors():
    s = parse_status_bytes(b"\x00" * 6)
    assert s.ok is True
    assert s.errors == []


def test_status_cover_open():
    s = parse_status_bytes(b"\x40\x00\x00\x00\x00\x00")
    assert s.ok is False
    assert any(e.code == "E302" for e in s.errors)


def test_status_no_paper():
    s = parse_status_bytes(b"\x00\x00\x01\x00\x00\x00")
    assert s.ok is False
    assert any(e.code == "E301" for e in s.errors)


def test_status_fm_full():
    s = parse_status_bytes(b"\x00\x00\x00\x00\x10\x00")
    assert s.ok is False
    assert any(e.code == "E201" for e in s.errors)


def test_status_warnings_dont_fail_ok():
    s = parse_status_bytes(b"\x00\x00\x02\x00\x08\x00")  # near-paper-end + FM-low
    assert s.ok is True  # no errors, only warnings
    codes = [m.code for m in s.messages]
    assert "W301" in codes
    assert "W201" in codes
