"""Малък DNS отговарящ за имената на касата (ADR erpnet-kasi-fleet/0002).

Защо (измерено 05.10): при маршрут по име Gateway праща DNS заявката през
тунела, а cloudflared я препраща като ИСТИНСКА DNS заявка към резолвера на
машината — `hosts` на касата и `extra_hosts` на контейнера не участват, и
Gateway връща REFUSED. cloudflared се пуска с
`--dns-resolver-addrs 127.0.0.1:<порт>` и пита този отговарящ, който знае
само нашите имена. Резолверът на машината и `hosts` не се пипат.

Файлът с имената е като hosts: `<IP> <име>` на ред, `#` за коментар.
Вместо IP може `self` — текущият основен IPv4 адрес на машината (DHCP не
чупи нищо). НЕ 127.0.0.1: Gateway връща loopback на клиента такъв, какъвто
е (измерено 05.10), и лаптопът се свързва сам със себе си; само за
нелокален адрес Gateway дава начален IP от 172.64.128.0/20 и трафикът
минава през тунела.
Отговаря на A за известни имена; NOERROR без запис за други типове на
известно име; NXDOMAIN за непознато.

    python -m odoo_erpnet_fp.tunnel_dns --names <файл> [--port 53535]
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path

DEFAULT_PORT = 53535
log = logging.getLogger("erpnet.tunnel_dns")


SELF = "self"
_self_cache: tuple[float, str] = (0.0, "")


def self_ipv4() -> str:
    """Основният IPv4 адрес — този, през който излиза трафикът навън.
    UDP connect не праща пакет; кеш 30 s."""
    global _self_cache
    now = time.monotonic()
    if now - _self_cache[0] < 30 and _self_cache[1]:
        return _self_cache[1]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("1.1.1.1", 53))
        ip = s.getsockname()[0]
    _self_cache = (now, ip)
    return ip


def load_names(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not path.exists():
        return names
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        ip, *hosts = line.split()
        if ip != SELF:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                continue
        for h in hosts:
            names[h.lower().rstrip(".")] = ip
    return names


def _parse_qname(msg: bytes, off: int) -> tuple[str, int]:
    labels = []
    while True:
        ln = msg[off]
        if ln == 0:
            return ".".join(labels), off + 1
        if ln & 0xC0:
            raise ValueError("compressed qname in question")
        labels.append(msg[off + 1:off + 1 + ln].decode("ascii", errors="replace"))
        off += 1 + ln


def answer(query: bytes, names: dict[str, str]) -> bytes | None:
    """Отговор на една DNS заявка; None за счупен пакет."""
    if len(query) < 12:
        return None
    qid, flags, qd, _an, _ns, _ar = struct.unpack("!6H", query[:12])
    if qd != 1 or flags & 0x8000:
        return None
    try:
        qname, off = _parse_qname(query, 12)
        qtype, qclass = struct.unpack("!2H", query[off:off + 4])
    except (ValueError, IndexError, struct.error):
        return None
    question = query[12:off + 4]
    name = qname.lower().rstrip(".")
    rd = flags & 0x0100
    ip = names.get(name)
    if ip == SELF:
        try:
            ip = self_ipv4()
        except OSError:
            return struct.pack("!6H", qid, 0x8000 | rd | 2, 1, 0, 0, 0) + query[12:off + 4]  # SERVFAIL
    if ip is None:
        rflags, ans = 0x8400 | rd | 3, b""          # AA + NXDOMAIN
    elif qtype == 1 and qclass == 1:
        rflags = 0x8400 | rd
        ans = struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ip)
    else:
        rflags, ans = 0x8400 | rd, b""              # известно име, друг тип
    header = struct.pack("!6H", qid, rflags, 1, 1 if ans else 0, 0, 0)
    return header + question + ans


class _Names:
    """Чете файла наново при промяна — нови устройства без рестарт."""

    def __init__(self, path: Path):
        self.path, self._mtime, self._names, self._lock = path, None, {}, threading.Lock()

    def get(self) -> dict[str, str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if mtime != self._mtime:
                self._names, self._mtime = load_names(self.path), mtime
                log.info("names loaded: %d from %s", len(self._names), self.path)
            return self._names


def serve(names_file: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          ready: threading.Event | None = None) -> tuple[socketserver.BaseServer, socketserver.BaseServer]:
    store = _Names(names_file)

    class Udp(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            resp = answer(data, store.get())
            if resp:
                sock.sendto(resp, self.client_address)

    class Tcp(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                hdr = self.rfile.read(2)
                if len(hdr) < 2:
                    return
                data = self.rfile.read(struct.unpack("!H", hdr)[0])
                resp = answer(data, store.get())
                if not resp:
                    return
                self.wfile.write(struct.pack("!H", len(resp)) + resp)

    socketserver.ThreadingUDPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    udp = socketserver.ThreadingUDPServer((host, port), Udp)
    tcp = socketserver.ThreadingTCPServer((host, port), Tcp)
    for srv in (udp, tcp):
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if ready:
        ready.set()
    return udp, tcp


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m odoo_erpnet_fp.tunnel_dns")
    ap.add_argument("--names", required=True, type=Path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=DEFAULT_PORT, type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(args.names, args.host, args.port)
    log.info("listening on %s:%d (udp+tcp)", args.host, args.port)
    threading.Event().wait()


if __name__ == "__main__":
    main()
