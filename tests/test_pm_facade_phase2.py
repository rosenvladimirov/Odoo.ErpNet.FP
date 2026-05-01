"""
Phase 2 facade tests with MockDevice.

Anchors on PDF examples:
  * §4.55.1 syntax #2 — `program_plu` for "Български ябълки"
  * §4.55.2 — `read_plu_info` returns (capacity, programmed, name_len)
  * §4.55.4 — `delete_plu` on a PLU range
  * §4.70   — `upload_logo` chunked workflow (START → chunks → STOPP → RESTART)
  * §4.73   — `read_parameter` / `write_parameter` for AutoPowerOff
"""

import base64

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec, commands, errors, frame
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.pm_v2_11_4 import PmDevice

from mock_device import MockDevice


# ---- program_plu ----------------------------------------------------


def test_program_plu_pdf_syntax2_example():
    """PDF §4.55.1 syntax #2 example, human-oriented log:
       P\\t10\\tB\\t2\\t1\\t2\\t1.09\\tA\\t1000\\t1000111\\t2000111\\t
       3000111\\t4000111\\tБългарски ябълки\\t1\\t
    """
    mock = MockDevice()
    mock.expect_static(0x6B, data=codec.encode_data(0), status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()

    pm.program_plu(
        plu_number=10,
        name="Български ябълки",
        price=1.09,
        vat_group="B",
        department=2,
        group=1,
        price_type=2,  # max-price
        quantity=1000.0,
        barcodes=("1000111", "2000111", "3000111", "4000111"),
        measurement_unit=1,  # кг
    )

    sent = mock.history[-1]
    assert sent.cmd == 0x6B
    fields = codec.decode_data(sent.data)
    assert fields[0] == "P"
    assert fields[1] == "10"
    assert fields[2] == "B"
    assert fields[3] == "2"
    assert fields[4] == "1"
    assert fields[5] == "2"
    assert fields[6] == "1.09"
    assert fields[7] == "A"
    assert fields[8] == "1000.000"
    assert fields[9:13] == ["1000111", "2000111", "3000111", "4000111"]
    assert fields[13] == "Български ябълки"
    assert fields[14] == "1"


def test_program_plu_minimal_no_quantity_no_barcodes():
    """When quantity is None we omit AddQty + Quantity fields (empty)."""
    mock = MockDevice()
    mock.expect_static(0x6B, data=codec.encode_data(0), status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()
    pm.program_plu(
        plu_number=1,
        name="Хляб",
        price=1.50,
    )

    fields = codec.decode_data(mock.history[-1].data)
    # Expected: P, 1, А, 0, 1, 0, 1.50, '', '', '', '', '', '', Хляб, 0
    assert fields[0] == "P"
    assert fields[6] == "1.50"
    assert fields[7] == ""  # AddQty empty
    assert fields[8] == ""  # Quantity empty
    assert fields[9:13] == ["", "", "", ""]
    assert fields[13] == "Хляб"
    assert fields[14] == "0"  # default measurement unit (бр.)


def test_program_plu_rejects_long_name():
    pm = PmDevice(MockDevice())
    pm.open()
    with pytest.raises(ValueError, match="72 chars"):
        pm.program_plu(plu_number=1, name="x" * 73, price=1.0)


def test_program_plu_rejects_out_of_range():
    pm = PmDevice(MockDevice())
    pm.open()
    with pytest.raises(ValueError, match="PLU number"):
        pm.program_plu(plu_number=0, name="x", price=1.0)
    with pytest.raises(ValueError, match="PLU number"):
        pm.program_plu(plu_number=100001, name="x", price=1.0)


def test_program_plu_propagates_device_error():
    mock = MockDevice()
    mock.expect_static(0x6B, data=codec.encode_data(-103001), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    with pytest.raises(errors.FiscalError) as exc:
        pm.program_plu(plu_number=1, name="x", price=1.0)
    assert exc.value.code == -103001


# ---- delete_plu ------------------------------------------------------


def test_delete_plu_range_pdf_example():
    """PDF §4.55.4 example: D\\t30\\t40\\t — delete PLUs 30..40."""
    mock = MockDevice()
    mock.expect_static(0x6B, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.delete_plu(first_plu=30, last_plu=40)

    fields = codec.decode_data(mock.history[-1].data)
    assert fields == ["D", "30", "40"]


def test_delete_plu_single():
    mock = MockDevice()
    mock.expect_static(0x6B, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.delete_plu(first_plu=42)
    # last_plu omitted → empty optional field
    fields = codec.decode_data(mock.history[-1].data)
    assert fields[0] == "D"
    assert fields[1] == "42"
    # codec.decode_data drops the trailing empty from the final TAB —
    # but the empty internal tab is preserved as empty string.
    # When we send `("D", 42, None)`, encode_data emits "D\t42\t\t",
    # split gives ["D", "42", ""] (trailing dropped).
    assert len(fields) == 3
    assert fields[2] == ""


# ---- read_plu_info --------------------------------------------------


def test_read_plu_info_pdf_example():
    """PDF §4.55.2 answer human log: 0\\t100000\\t5\\t72\\t"""
    mock = MockDevice()
    mock.expect_static(
        0x6B,
        data=codec.encode_data(0, 100000, 5, 72),
        status=b"\x80" * 8,
    )
    pm = PmDevice(mock)
    pm.open()
    capacity, programmed, name_len = pm.read_plu_info()
    assert capacity == 100000
    assert programmed == 5
    assert name_len == 72

    sent = mock.history[-1]
    assert codec.decode_data(sent.data) == ["I"]


# ---- upload_logo / upload_stamp -------------------------------------


def test_upload_logo_chunked_workflow():
    """Verify START → chunks → STOPP → RESTART sequence with cmd 0xCA."""
    mock = MockDevice()
    captured: list[str] = []

    def handler(req):
        # Capture the parameter string, then return a normal OK response.
        # STOPP returns CheckSum in DATA.
        param = codec.decode_data(req.data)[0]
        captured.append(param)
        if param == "STOPP":
            return (codec.encode_data(0, "00403F70"), b"\x80" * 8)
        return (codec.encode_data(0), b"\x80" * 8)

    mock.expect(0xCA, handler)

    pm = PmDevice(mock)
    pm.open()

    # 200 bytes of binary → 268 base64 chars → 4 chunks of 72 = ceil(268/72)
    image = bytes(range(256)) + b"\x00\x01\x02\x03"  # 260 bytes
    expected_b64 = base64.b64encode(image).decode("ascii")
    expected_chunks = [
        expected_b64[i : i + 72] for i in range(0, len(expected_b64), 72)
    ]

    checksum = pm.upload_logo(image)

    assert captured[0] == "START"
    assert captured[1 : 1 + len(expected_chunks)] == expected_chunks
    assert captured[1 + len(expected_chunks)] == "STOPP"
    assert captured[-1] == "RESTART"
    assert checksum == 0x00403F70  # from PDF §4.71 example STOPP response


def test_upload_stamp_no_restart():
    """Stamp upload (0xCB) must NOT send RESTART per PDF §4.71."""
    mock = MockDevice()
    captured: list[str] = []

    def handler(req):
        param = codec.decode_data(req.data)[0]
        captured.append(param)
        if param == "STOPP":
            return (codec.encode_data(0, "00FF"), b"\x80" * 8)
        return (codec.encode_data(0), b"\x80" * 8)

    mock.expect(0xCB, handler)

    pm = PmDevice(mock)
    pm.open()
    pm.upload_stamp(b"\xFF" * 50)  # tiny stamp

    assert "RESTART" not in captured
    assert captured[0] == "START"
    assert captured[-1] == "STOPP"


def test_upload_logo_rejects_empty():
    pm = PmDevice(MockDevice())
    pm.open()
    with pytest.raises(ValueError):
        pm.upload_logo(b"")


# ---- read_parameter / write_parameter -------------------------------


def test_read_parameter_pdf_autopower_example():
    """PDF §4.73.1: read AutoPowerOff with blank value → returns "1"."""
    mock = MockDevice()
    mock.expect_static(
        0xFF, data=codec.encode_data(0, 1), status=b"\x80" * 8
    )
    pm = PmDevice(mock)
    pm.open()
    val = pm.read_parameter("AutoPowerOff")
    assert val == "1"

    sent = mock.history[-1]
    fields = codec.decode_data(sent.data)
    assert fields[0] == "AutoPowerOff"
    assert fields[1] == "0"  # default index
    assert fields[2] == ""  # blank value triggers read


def test_write_parameter_pdf_autopower_example():
    """PDF §4.73.2: write AutoPowerOff = 2."""
    mock = MockDevice()
    mock.expect_static(0xFF, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.write_parameter("AutoPowerOff", 2)

    fields = codec.decode_data(mock.history[-1].data)
    assert fields[0] == "AutoPowerOff"
    assert fields[1] == "0"  # default index
    assert fields[2] == "2"
