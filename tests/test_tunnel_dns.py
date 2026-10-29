"""DNS отговарящият за имената на касата (ADR erpnet-kasi-fleet/0002)."""

import socket
import struct

from odoo_erpnet_fp import tunnel_dns


def _query(name: str, qtype: int = 1) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    return struct.pack("!6H", 0x1234, 0x0100, 1, 0, 0, 0) + q + struct.pack("!2H", qtype, 1)


def _parse(resp: bytes) -> tuple[int, int, list[str]]:
    _id, flags, _qd, an, _ns, _ar = struct.unpack("!6H", resp[:12])
    ips = [socket.inet_ntoa(resp[-4:])] if an else []
    return flags & 0xF, an, ips


NAMES = {"kasa1.shop1.erpnet.mcpworks.net": "self",
         "bluecash.kasa1.shop1.erpnet.mcpworks.net": "192.0.2.12"}


def test_device_name_answers_its_lan_ip():
    assert _parse(tunnel_dns.answer(_query("bluecash.kasa1.shop1.erpnet.mcpworks.net"), NAMES)) == (0, 1, ["192.0.2.12"])


def test_self_is_the_machine_ip_not_loopback(monkeypatch):
    monkeypatch.setattr(tunnel_dns, "self_ipv4", lambda: "192.0.2.6")
    rc, an, ips = _parse(tunnel_dns.answer(_query("KASA1.shop1.erpnet.mcpworks.net."), NAMES))
    assert (rc, an, ips) == (0, 1, ["192.0.2.6"])


def test_unknown_name_is_nxdomain_and_other_type_is_empty():
    assert _parse(tunnel_dns.answer(_query("other.example"), NAMES))[:2] == (3, 0)
    assert _parse(tunnel_dns.answer(_query("bluecash.kasa1.shop1.erpnet.mcpworks.net", 28), NAMES))[:2] == (0, 0)


def test_garbage_is_ignored():
    assert tunnel_dns.answer(b"\x00\x01", NAMES) is None


def test_names_file_accepts_self_and_skips_junk(tmp_path):
    f = tmp_path / "names"
    f.write_text("# x\nself a.b\n192.0.2.5 c.d e.f\nnot-ip g.h\n")
    assert tunnel_dns.load_names(f) == {"a.b": "self", "c.d": "192.0.2.5", "e.f": "192.0.2.5"}
