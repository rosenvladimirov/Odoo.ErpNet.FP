"""
Bit-level frame encode/decode tests.

The two anchor cases are taken verbatim from PROTOCOL_REFERENCE §12 —
the PDF's worked-out example for `Open fiscal receipt` (cmd 48).
If these fail, every other layer is broken.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec, frame

# PDF §12 — request bytes
PDF_REQUEST_HEX = (
    "01 30 30 33 33 2C 30 30 33 30 31 09 31 09 32 34 09 49 09 05 30 32 3E 3F 03"
)
PDF_RESPONSE_HEX = (
    "01 30 30 33 39 2C 30 30 33 30 30 09 34 37 32 09 04 80 80 88 80 86 9A 80 80 "
    "05 30 36 3C 3B 03"
)


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.replace(" ", ""))


# ---- nibble encoding -------------------------------------------------


def test_encode_hex4_zero():
    assert frame.encode_hex4(0x0000) == b"\x30\x30\x30\x30"


def test_encode_hex4_max():
    # nibble 0xF + 0x30 = 0x3F (= '?')
    assert frame.encode_hex4(0xFFFF) == b"\x3F\x3F\x3F\x3F"


def test_encode_hex4_pdf_len_value():
    # PDF §12 LEN = 0x33 (51 decimal) → "0033" → bytes 30 30 33 33
    assert frame.encode_hex4(0x0033) == b"\x30\x30\x33\x33"


def test_decode_hex4_roundtrip():
    for v in [0, 1, 0x12, 0x33, 0xAB, 0xFF, 0x1234, 0xFFFF]:
        assert frame.decode_hex4(frame.encode_hex4(v)) == v


def test_decode_hex4_rejects_out_of_range_byte():
    # 0x40 ('A') is one past the legal high (0x3F = '?')
    with pytest.raises(frame.FormatError):
        frame.decode_hex4(b"\x30\x30\x30\x40")


# ---- request encode/decode -------------------------------------------


def test_decode_pdf_request():
    raw = _b(PDF_REQUEST_HEX)
    req = frame.decode_request(raw)
    assert req.seq == 0x2C
    assert req.cmd == 0x30  # cmd 48 — open fiscal receipt
    assert req.data == b"1\t1\t24\tI\t"


def test_pdf_request_data_decodes_to_expected_fields():
    raw = _b(PDF_REQUEST_HEX)
    req = frame.decode_request(raw)
    assert codec.decode_data(req.data) == ["1", "1", "24", "I"]


def test_encode_request_byte_for_byte_matches_pdf():
    raw = _b(PDF_REQUEST_HEX)
    req = frame.decode_request(raw)
    re = frame.encode_request(req.seq, req.cmd, req.data)
    assert re == raw


def test_request_minimal_with_empty_data():
    out = frame.encode_request(seq=0x20, cmd=0x4A, data=b"")
    # Round-trip
    req = frame.decode_request(out)
    assert req.seq == 0x20 and req.cmd == 0x4A and req.data == b""


def test_request_seq_out_of_range():
    with pytest.raises(ValueError):
        frame.encode_request(seq=0x1F, cmd=0x4A)
    with pytest.raises(ValueError):
        frame.encode_request(seq=0x100, cmd=0x4A)


def test_request_data_overflow():
    with pytest.raises(ValueError):
        frame.encode_request(seq=0x20, cmd=0x4A, data=b"x" * (frame.MAX_REQUEST_DATA + 1))


def test_request_bad_pre():
    raw = bytearray(_b(PDF_REQUEST_HEX))
    raw[0] = 0x02
    with pytest.raises(frame.FormatError):
        frame.decode_request(bytes(raw))


def test_request_bad_eot():
    raw = bytearray(_b(PDF_REQUEST_HEX))
    raw[-1] = 0x04
    with pytest.raises(frame.FormatError):
        frame.decode_request(bytes(raw))


def test_request_bcc_mismatch():
    raw = bytearray(_b(PDF_REQUEST_HEX))
    raw[-2] ^= 0x01  # flip a BCC nibble
    with pytest.raises(frame.ChecksumError):
        frame.decode_request(bytes(raw))


# ---- response encode/decode ------------------------------------------


def test_decode_pdf_response():
    raw = _b(PDF_RESPONSE_HEX)
    resp = frame.decode_response(raw)
    assert resp.seq == 0x2C
    assert resp.cmd == 0x30
    assert codec.decode_data(resp.data) == ["0", "472"]
    assert resp.status == bytes.fromhex("80808880869a8080")


def test_encode_response_byte_for_byte_matches_pdf():
    raw = _b(PDF_RESPONSE_HEX)
    resp = frame.decode_response(raw)
    re = frame.encode_response(resp.seq, resp.cmd, resp.data, resp.status)
    assert re == raw


def test_response_status_must_have_bit7_set():
    # All-zero status would mean control-byte ambiguity per PDF.
    with pytest.raises(ValueError):
        frame.encode_response(seq=0x20, cmd=0x4A, data=b"", status=b"\x00" * 8)


def test_response_status_length_enforced():
    with pytest.raises(ValueError):
        frame.encode_response(seq=0x20, cmd=0x4A, data=b"", status=b"\x80" * 7)


# ---- BCC algorithm sanity --------------------------------------------


def test_bcc_sum_pdf_example():
    """The BCC value in PROTOCOL_REFERENCE.md §12 is decoded as 0x023F.
    That is a typo in the distill — the actual bytes 30 32 3E 3F decode
    as nibbles 0,2,E,F = 0x02EF = 751 decimal. We verify that our
    algorithm produces 0x02EF for the request payload, matching the
    actual PDF wire bytes (which the test above also rounds-trips).
    """
    raw = _b(PDF_REQUEST_HEX)
    inner = raw[1:-1]
    pst_idx = len(inner) - 5  # PST + BCC = last 5
    bcc_payload = inner[: pst_idx + 1]
    assert frame.bcc_sum(bcc_payload) == 0x02EF
