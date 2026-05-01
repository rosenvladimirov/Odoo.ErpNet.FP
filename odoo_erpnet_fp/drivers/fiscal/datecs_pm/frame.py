"""
Datecs PM v2.11.4 frame envelope — encode/decode.

Wire format (per docs/PROTOCOL_REFERENCE.md sections 2-4):

  Request:  <PRE> <LEN:4> <SEQ:1> <CMD:4> <DATA:0..496> <PST> <BCC:4> <EOT>
  Response: <PRE> <LEN:4> <SEQ:1> <CMD:4> <DATA:0..480> <SEP> <STAT:8> <PST> <BCC:4> <EOT>

LEN, CMD, BCC are encoded as 4 nibble bytes, each nibble + 0x30 (range
0x30..0x3F). This is NOT standard ASCII-hex: nibble 0xA encodes to ':' (0x3A),
not 'A' (0x41).

LEN value = (count of bytes from after-PRE through PST inclusive) + 0x20.

BCC = sum of all bytes from after-PRE through PST inclusive, masked to 16 bits.

Slave may return single non-wrapped control bytes instead of a frame:
  NAK (0x15) — checksum/format error, host retries with same SEQ
  SYN (0x16) — slave still processing, sent every 60 ms
"""

from dataclasses import dataclass

PRE = 0x01
PST = 0x05
SEP = 0x04
EOT = 0x03
NAK = 0x15
SYN = 0x16

LEN_OFFSET = 0x20
NIBBLE_BIAS = 0x30

MAX_REQUEST_DATA = 496
MAX_RESPONSE_DATA = 480
STATUS_LEN = 8

# Header sizes for size accounting:
#   LEN(4) + SEQ(1) + CMD(4) = 9 bytes
#   trailing PST(1) = 1 byte
# Plus DATA in between.
_REQ_OVERHEAD_NO_DATA = 9 + 1  # LEN+SEQ+CMD + PST
# For response add SEP(1) + STAT(8) before PST.
_RESP_EXTRA = 1 + STATUS_LEN


class FrameError(Exception):
    """Base class for frame-level wire format errors."""


class ChecksumError(FrameError):
    """BCC mismatch."""


class FormatError(FrameError):
    """Field out of range, missing terminator, or similar."""


def _nibble_to_byte(nib: int) -> int:
    if not 0 <= nib <= 0xF:
        raise ValueError(f"Nibble out of range 0..F: {nib}")
    return nib + NIBBLE_BIAS


def _byte_to_nibble(b: int) -> int:
    nib = b - NIBBLE_BIAS
    if not 0 <= nib <= 0xF:
        raise FormatError(
            f"Byte 0x{b:02X} not in nibble range 0x30..0x3F"
        )
    return nib


def encode_hex4(value: int) -> bytes:
    """Encode a 16-bit value as 4 nibble bytes, MSB first."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"Value {value} out of 16-bit range")
    return bytes(
        [
            _nibble_to_byte((value >> 12) & 0xF),
            _nibble_to_byte((value >> 8) & 0xF),
            _nibble_to_byte((value >> 4) & 0xF),
            _nibble_to_byte(value & 0xF),
        ]
    )


def decode_hex4(b: bytes) -> int:
    if len(b) != 4:
        raise FormatError(f"Expected 4 bytes, got {len(b)}")
    n0 = _byte_to_nibble(b[0])
    n1 = _byte_to_nibble(b[1])
    n2 = _byte_to_nibble(b[2])
    n3 = _byte_to_nibble(b[3])
    return (n0 << 12) | (n1 << 8) | (n2 << 4) | n3


def bcc_sum(payload: bytes) -> int:
    """Compute 16-bit BCC over payload (after-PRE through PST inclusive)."""
    return sum(payload) & 0xFFFF


@dataclass(frozen=True)
class Request:
    seq: int
    cmd: int
    data: bytes


@dataclass(frozen=True)
class Response:
    seq: int
    cmd: int
    data: bytes
    status: bytes  # 8 bytes


def _validate_seq(seq: int) -> None:
    if not 0x20 <= seq <= 0xFF:
        raise ValueError(f"SEQ {seq} out of range 0x20..0xFF")


def encode_request(seq: int, cmd: int, data: bytes = b"") -> bytes:
    """Encode a host→device request frame.

    Raises ValueError for out-of-range fields.
    """
    _validate_seq(seq)
    if len(data) > MAX_REQUEST_DATA:
        raise ValueError(f"DATA exceeds {MAX_REQUEST_DATA} bytes: {len(data)}")

    seq_b = bytes([seq])
    cmd_b = encode_hex4(cmd)
    pst = bytes([PST])

    body_len = _REQ_OVERHEAD_NO_DATA + len(data)
    len_value = body_len + LEN_OFFSET
    len_b = encode_hex4(len_value)

    bcc_payload = len_b + seq_b + cmd_b + data + pst
    bcc_b = encode_hex4(bcc_sum(bcc_payload))

    return bytes([PRE]) + bcc_payload + bcc_b + bytes([EOT])


def encode_response(
    seq: int, cmd: int, data: bytes, status: bytes
) -> bytes:
    """Encode a device→host response frame.

    Useful for MockDevice in tests.
    """
    _validate_seq(seq)
    if len(data) > MAX_RESPONSE_DATA:
        raise ValueError(f"DATA exceeds {MAX_RESPONSE_DATA} bytes: {len(data)}")
    if len(status) != STATUS_LEN:
        raise ValueError(f"STATUS must be {STATUS_LEN} bytes, got {len(status)}")
    for i, b in enumerate(status):
        if not (b & 0x80):
            raise ValueError(
                f"STATUS byte {i} bit 7 must be 1 (got 0x{b:02X})"
            )

    seq_b = bytes([seq])
    cmd_b = encode_hex4(cmd)
    pst = bytes([PST])
    sep = bytes([SEP])

    body_len = _REQ_OVERHEAD_NO_DATA + _RESP_EXTRA + len(data)
    len_value = body_len + LEN_OFFSET
    len_b = encode_hex4(len_value)

    bcc_payload = len_b + seq_b + cmd_b + data + sep + status + pst
    bcc_b = encode_hex4(bcc_sum(bcc_payload))

    return bytes([PRE]) + bcc_payload + bcc_b + bytes([EOT])


def _decode_envelope(raw: bytes) -> tuple[int, int, bytes]:
    """Strip PRE/EOT, verify LEN+BCC, return (seq, cmd, body_after_cmd_before_pst).

    body_after_cmd_before_pst is DATA for requests, DATA+SEP+STAT for responses.
    """
    if len(raw) < 1 + 4 + 1 + 4 + 1 + 4 + 1:
        raise FormatError(f"Frame too short: {len(raw)}")
    if raw[0] != PRE:
        raise FormatError(f"Expected PRE 0x01, got 0x{raw[0]:02X}")
    if raw[-1] != EOT:
        raise FormatError(f"Expected EOT 0x03, got 0x{raw[-1]:02X}")

    inner = raw[1:-1]  # everything between PRE and EOT
    # inner = LEN(4) SEQ(1) CMD(4) <middle> PST(1) BCC(4)
    if len(inner) < 4 + 1 + 4 + 1 + 4:
        raise FormatError(f"Inner frame too short: {len(inner)}")

    len_value = decode_hex4(inner[0:4])
    seq = inner[4]
    cmd = decode_hex4(inner[5:9])

    # Last 5 bytes = PST + BCC
    pst_idx = len(inner) - 5
    if inner[pst_idx] != PST:
        raise FormatError(
            f"Expected PST 0x05 at idx {pst_idx}, got 0x{inner[pst_idx]:02X}"
        )

    middle = inner[9:pst_idx]
    bcc_payload = inner[: pst_idx + 1]  # LEN..PST inclusive
    bcc_received = decode_hex4(inner[pst_idx + 1 :])
    bcc_computed = bcc_sum(bcc_payload)
    if bcc_received != bcc_computed:
        raise ChecksumError(
            f"BCC mismatch: received 0x{bcc_received:04X}, "
            f"computed 0x{bcc_computed:04X}"
        )

    expected_len = len(bcc_payload) + LEN_OFFSET
    if expected_len != len_value:
        raise FormatError(
            f"LEN mismatch: claimed 0x{len_value:04X}, "
            f"computed 0x{expected_len:04X}"
        )

    return seq, cmd, middle


def decode_request(raw: bytes) -> Request:
    seq, cmd, middle = _decode_envelope(raw)
    return Request(seq=seq, cmd=cmd, data=middle)


def decode_response(raw: bytes) -> Response:
    seq, cmd, middle = _decode_envelope(raw)
    # middle = DATA + SEP + STAT(8)
    if len(middle) < 1 + STATUS_LEN:
        raise FormatError(f"Response middle too short: {len(middle)}")
    sep_idx = len(middle) - 1 - STATUS_LEN
    if middle[sep_idx] != SEP:
        raise FormatError(
            f"Expected SEP 0x04 at idx {sep_idx}, got 0x{middle[sep_idx]:02X}"
        )
    data = middle[:sep_idx]
    status = middle[sep_idx + 1 :]
    return Response(seq=seq, cmd=cmd, data=data, status=status)


def is_control_byte(b: int) -> bool:
    """True if `b` is a non-wrapped control byte (NAK or SYN)."""
    return b in (NAK, SYN)
