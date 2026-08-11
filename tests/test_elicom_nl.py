"""
Elicom NL continuous-protocol parser and stability tests.

Frames captured from a live EEP60-2-1-NL-X (indicator ELICOM NL,
SN 020644): ``t+000.000\\r`` at zero, ``p+000.900\\r`` under load,
~80 frames/s. These tests run offline against synthetic byte streams.
"""

import pytest

from odoo_erpnet_fp.drivers.scales.elicom_nl import (
    STABLE_RUN,
    ElicomNlScale,
)


def parse(frame: bytes):
    return ElicomNlScale._parse_frame(frame)


# ─── frame parsing ─────────────────────────────────────────────


def test_parse_zero_frame():
    assert parse(b"t+000.000") == (0.0, "t")


def test_parse_load_frame():
    assert parse(b"p+000.900") == (pytest.approx(0.9), "p")


def test_parse_negative_weight():
    # Снета тара — индикаторът показва отрицателно нето.
    assert parse(b"p-002.240") == (pytest.approx(-2.24), "p")


def test_parse_unknown_flag_still_parses():
    value, flag = parse(b"u+001.500")
    assert value == pytest.approx(1.5)
    assert flag == "u"


def test_parse_no_flag():
    assert parse(b"+012.345") == (pytest.approx(12.345), "")


def test_parse_rejects_garbage():
    assert parse(b"\xe6\xcc\x98.000") is None
    assert parse(b"") is None
    assert parse(b"PCon") is None
    assert parse(b"t 000.000") is None


def test_parse_rejects_missing_sign():
    assert parse(b"t000.000") is None


# ─── stability from the value stream ──────────────────────────


class FakeConn:
    """Minimal serial.Serial stand-in fed from a byte script."""

    def __init__(self, payload: bytes, chunk: int = 32):
        self._data = payload
        self._chunk = chunk
        self.is_open = True

    def read(self, size: int = 1) -> bytes:
        take = min(self._chunk, size, len(self._data))
        out, self._data = self._data[:take], self._data[take:]
        return out

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def make_scale(payload: bytes) -> ElicomNlScale:
    s = ElicomNlScale("loop://", read_timeout=0.3)
    s._conn = FakeConn(payload)
    return s


def test_stable_stream_returns_weight():
    payload = b"p+000.900\r" * (STABLE_RUN + 2)
    reading = make_scale(payload).read_weight()
    assert reading.ok
    assert reading.weight_kg == pytest.approx(0.9)
    assert reading.status == []


def test_zero_stream_is_a_valid_stable_zero():
    payload = b"t+000.000\r" * (STABLE_RUN + 2)
    reading = make_scale(payload).read_weight()
    assert reading.ok
    assert reading.weight_kg == 0.0


def test_settling_stream_returns_the_settled_value():
    # Стойността се движи, после застива — печели застиналата.
    moving = b"p+000.300\rp+000.700\rp+000.850\rp+000.898\r"
    settled = b"p+000.900\r" * STABLE_RUN
    reading = make_scale(moving + settled).read_weight()
    assert reading.ok
    assert reading.weight_kg == pytest.approx(0.9)


def test_never_settling_stream_is_unstable():
    frames = b"".join(
        b"p+000.%03d\r" % n for n in range(0, 400, 7)
    )
    reading = make_scale(frames).read_weight()
    assert not reading.ok
    assert reading.weight_kg is None
    assert "Scale unstable" in reading.status


def test_silent_port_reports_no_data():
    reading = make_scale(b"").read_weight()
    assert not reading.ok
    assert "No data from scale" in reading.status


def test_unknown_flag_is_surfaced_in_status():
    payload = b"u+001.500\r" * (STABLE_RUN + 1)
    reading = make_scale(payload).read_weight()
    assert reading.ok
    assert reading.weight_kg == pytest.approx(1.5)
    assert "flag:u" in reading.status


def test_garbage_between_frames_is_skipped():
    payload = b"\xe6\xcc\x98.000\r" + b"p+000.900\r" * (STABLE_RUN + 1)
    reading = make_scale(payload).read_weight()
    assert reading.ok
    assert reading.weight_kg == pytest.approx(0.9)


def test_crlf_frames_also_parse():
    payload = b"p+000.900\r\n" * (STABLE_RUN + 2)
    reading = make_scale(payload).read_weight()
    assert reading.ok
    assert reading.weight_kg == pytest.approx(0.9)
