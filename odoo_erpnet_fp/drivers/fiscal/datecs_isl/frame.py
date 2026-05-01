"""
Datecs ISL frame envelope encode/decode.

Wire format::

  Request:  PRE LEN SEQ CMD [DATA] PST <BCC:4> ETX
  Response: PRE LEN SEQ CMD [DATA] SEP <STATUS:6/8> PST <BCC:4> ETX

Where:
  PRE  = 0x01      preamble
  PST  = 0x05      postamble
  SEP  = 0x04      separator (response only — between DATA and STATUS)
  ETX  = 0x03      terminator
  NAK  = 0x15      single-byte negative ack (slave detected protocol error)
  SYN  = 0x16      single-byte synchronisation (slave still working)
  LEN  = 0x20 + (4 + len(DATA))                    1 byte
  SEQ  = 0x20 + n  where n ∈ [0, 0x5F]              1 byte
  CMD  = 0x00..0xFF                                  1 byte
  BCC  = 4 ASCII-hex digits with +0x30 offset of 16-bit sum from LEN to PST

DATA is CP-1251 encoded plain text (typically comma-separated parameters).
"""

from __future__ import annotations

# ─── Envelope markers ────────────────────────────────────────────
PRE = 0x01
PST = 0x05
SEP = 0x04
ETX = 0x03
NAK = 0x15
SYN = 0x16
SPACE = 0x20

# ─── Limits (from upstream IoT box driver) ───────────────────────
MAX_SEQUENCE_NUMBER = 0x7F - SPACE  # 0x5F = 95
MAX_WRITE_RETRIES = 6
MAX_READ_RETRIES = 200
DEFAULT_READ_BUF = 256


class FrameError(Exception):
    """Base for frame-level errors."""


class ChecksumError(FrameError):
    """BCC mismatch."""


class FormatError(FrameError):
    """Bad envelope (missing PRE / SEP / PST / ETX)."""


def _bcc_bytes(payload: bytes) -> bytes:
    """Compute 16-bit checksum and encode as 4 ASCII-hex chars with +0x30."""
    s = sum(payload) & 0xFFFF
    return bytes(
        [
            ((s >> 12) & 0x0F) + 0x30,
            ((s >> 8) & 0x0F) + 0x30,
            ((s >> 4) & 0x0F) + 0x30,
            (s & 0x0F) + 0x30,
        ]
    )


def encode_request(seq: int, cmd: int, data: bytes = b"") -> bytes:
    """Build a host→device ISL frame.

    `seq` must be in [0, MAX_SEQUENCE_NUMBER]; the caller manages
    increment + wrap.
    """
    if not 0 <= seq <= MAX_SEQUENCE_NUMBER:
        raise ValueError(f"SEQ {seq} out of [0..{MAX_SEQUENCE_NUMBER}]")
    if not 0 <= cmd <= 0xFF:
        raise ValueError(f"CMD {cmd:#x} out of byte range")

    length = SPACE + 4 + len(data)
    if length > 0xFF:
        raise ValueError(f"DATA too long: LEN={length:#x}")

    body = bytes([length, SPACE + seq, cmd]) + data + bytes([PST])
    return bytes([PRE]) + body + _bcc_bytes(body) + bytes([ETX])


def validate_checksum(raw: bytes) -> bool:
    """Verify the BCC of a complete framed response.

    BCC is encoded as 4 nibbles with +0x30 offset (same as the LEN
    field — see `_bcc_bytes`). Decoding is symmetric: each byte's low
    4 bits hold the nibble value. Note: upstream Odoo IoT box uses
    `int(b, 16)` here which only works when all nibbles are 0..9; we
    use the proper +0x30 inverse so high-nibble checksums round-trip.
    """
    if len(raw) < 10:
        return False
    if raw[-1] != ETX:
        return False
    bcc_bytes = raw[-5:-1]
    nibbles = [b - 0x30 for b in bcc_bytes]
    if any(n < 0 or n > 0xF for n in nibbles):
        return False
    bcc_received = (
        (nibbles[0] << 12) | (nibbles[1] << 8) | (nibbles[2] << 4) | nibbles[3]
    )
    bcc_computed = sum(raw[1:-5]) & 0xFFFF
    return bcc_received == bcc_computed


def parse_response(raw: bytes) -> tuple[bytes, bytes]:
    """Split a wrapped ISL response into (DATA, STATUS).

    Returns the raw DATA (CP-1251 bytes — caller decodes) and the raw
    status payload (6 or 8 bytes depending on firmware).
    """
    if len(raw) < 10:
        raise FormatError(f"Response too short: {len(raw)} bytes")
    if raw[0] != PRE:
        raise FormatError(f"Missing PRE 0x01, got {raw[0]:#04x}")
    if raw[-1] != ETX:
        raise FormatError(f"Missing ETX 0x03, got {raw[-1]:#04x}")
    if not validate_checksum(raw):
        raise ChecksumError("BCC mismatch in response")

    sep_pos = raw.find(SEP)
    pst_pos = raw.find(PST, sep_pos + 1) if sep_pos > 0 else -1
    if sep_pos < 0 or pst_pos < 0:
        raise FormatError("Missing SEP / PST markers")
    # First 4 bytes after PRE are LEN(1) + SEQ(1) + CMD(1) + … so DATA
    # starts at index 4.
    data = raw[4:sep_pos]
    status = raw[sep_pos + 1 : pst_pos]
    return data, status


def is_control_byte(b: int) -> bool:
    return b in (NAK, SYN)
