"""
CP-1251 codec + DATA field assembly tests.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec


def test_cp1251_roundtrip_bg():
    s = "Хляб ръжен 500г"
    assert codec.decode_str(codec.encode_str(s)) == s


def test_cp1251_specific_bytes_for_known_chars():
    # Cyrillic 'А' (capital A, ASCII-similar) is 0xC0 in CP-1251
    assert codec.encode_str("А") == b"\xC0"
    assert codec.encode_str("Я") == b"\xDF"
    assert codec.encode_str("а") == b"\xE0"
    assert codec.encode_str("я") == b"\xFF"


def test_encode_data_pdf_example():
    """PDF §7 example: 1\\t1\\t24\\tI\\t"""
    out = codec.encode_data("1", "1", 24, "I")
    assert out == b"1\t1\t24\tI\t"


def test_encode_data_handles_none_as_empty_optional():
    out = codec.encode_data("a", None, "b")
    assert out == b"a\t\tb\t"


def test_encode_data_bool_before_int_subclass():
    # bool is subclass of int — must serialise as 0/1, not True/False
    out = codec.encode_data(True, False)
    assert out == b"1\t0\t"


def test_encode_data_float_default_repr():
    out = codec.encode_data(3.14)
    assert out == b"3.14\t"


def test_encode_data_unsupported_type_raises():
    with pytest.raises(TypeError):
        codec.encode_data(["unsupported"])


def test_split_data_drops_trailing_empty():
    parts = codec.split_data(b"a\tb\t")
    assert parts == [b"a", b"b"]


def test_split_data_keeps_internal_empties():
    parts = codec.split_data(b"a\t\tb\t")
    assert parts == [b"a", b"", b"b"]


def test_decode_data_uses_cp1251():
    # 'А' = 0xC0 in CP-1251
    assert codec.decode_data(b"\xC0\t\xC1\t") == ["А", "Б"]
