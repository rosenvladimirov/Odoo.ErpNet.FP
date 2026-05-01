"""
CP-1251 codec + DATA field assembly helpers for Datecs PM v2.11.4.

Bulgarian text in DATA payloads is CP-1251 (Windows-1251) per industry
convention for BG fiscal devices. PROTOCOL_REFERENCE.md notes this is
documented implicitly only; Phase 1 verification with real device may
adjust this assumption (see ARCHITECTURE.md Risk #2).

Field separator inside DATA is TAB (0x09). Each field — including the
final one — is followed by a trailing TAB. Empty optional fields keep
their TAB separator.
"""

ENCODING = "cp1251"
TAB = b"\t"


def encode_str(s: str) -> bytes:
    """str → CP-1251 bytes."""
    return s.encode(ENCODING)


def decode_str(b: bytes) -> str:
    """CP-1251 bytes → str."""
    return b.decode(ENCODING)


def encode_data(*fields) -> bytes:
    """Assemble a DATA payload from a sequence of fields.

    Each field is converted to CP-1251 bytes and followed by a TAB.
    Accepts str, bytes, int, float, bool, or None (= empty optional).

    Example:
        encode_data("1", "1", 24, "I") → b'1\\t1\\t24\\tI\\t'
    """
    parts: list[bytes] = []
    for f in fields:
        if f is None:
            parts.append(b"")
        elif isinstance(f, bytes):
            parts.append(f)
        elif isinstance(f, bool):
            # bool before int — bool is subclass of int
            parts.append(b"1" if f else b"0")
        elif isinstance(f, str):
            parts.append(encode_str(f))
        elif isinstance(f, (int, float)):
            parts.append(str(f).encode(ENCODING))
        else:
            raise TypeError(f"Unsupported field type {type(f).__name__}")
        parts.append(TAB)
    return b"".join(parts)


def split_data(data: bytes) -> list[bytes]:
    """Split a DATA payload by TAB. Trailing empty entry from final TAB
    is dropped.
    """
    parts = data.split(TAB)
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return parts


def decode_data(data: bytes) -> list[str]:
    """Split + decode each field as CP-1251 string."""
    return [decode_str(p) for p in split_data(data)]
