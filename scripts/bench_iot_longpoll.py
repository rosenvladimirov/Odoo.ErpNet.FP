#!/usr/bin/env python3
"""
IoT longpoll benchmark — soak-test for the proxy.

Spins up N async clients that POST `/hw_drivers/event` long-polls in
a tight cycle, while a producer task injects fake barcode scans into
a configured reader. Measures latency, success rate, and timeout
distribution; emits a one-line summary every 60s; on shutdown writes
a final summary file.

Defaults match a single-shop benchmark scenario (50 simultaneous POS
clients, 1 scanner). Tune via env vars or CLI flags.

Run from the project root (so `docker stats` can sample the proxy):

    python3 scripts/bench_iot_longpoll.py \\
        --proxy http://127.0.0.1:8001 \\
        --reader-id auto-honeywell-25070b52e4 \\
        --consumers 50 --duration-seconds 28800

Outputs `bench_<timestamp>.log` with per-tick stats and final summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx


# ─── Stats accumulators (shared between consumers) ────────────────


class Stats:
    def __init__(self) -> None:
        self.latencies_ms: list[float] = []
        self.poll_count = 0
        self.event_count = 0
        self.timeout_count = 0
        self.error_count = 0
        self.last_summary_at = time.monotonic()

    def record_poll(self, latency_ms: float, got_event: bool) -> None:
        self.latencies_ms.append(latency_ms)
        self.poll_count += 1
        if got_event:
            self.event_count += 1
        else:
            self.timeout_count += 1

    def record_error(self) -> None:
        self.error_count += 1

    def summary(self, label: str) -> str:
        L = sorted(self.latencies_ms[-1000:]) if self.latencies_ms else [0.0]
        if len(L) < 2:
            p50 = p95 = p99 = L[0] if L else 0.0
        else:
            p50 = L[len(L) // 2]
            p95 = L[int(len(L) * 0.95)]
            p99 = L[int(len(L) * 0.99)]
        return (
            f"[{label}] polls={self.poll_count} events={self.event_count} "
            f"timeouts={self.timeout_count} errors={self.error_count} | "
            f"latency_ms p50={p50:.0f} p95={p95:.0f} p99={p99:.0f}"
        )


# ─── Workers ──────────────────────────────────────────────────────


async def consumer(client: httpx.AsyncClient,
                   proxy: str, reader_id: str,
                   stats: Stats, stop_at: float) -> None:
    """Run /hw_drivers/event long-polls until `stop_at`."""
    session_id = str(uuid.uuid4())
    while time.monotonic() < stop_at:
        body = {
            "params": {
                "listener": {
                    "session_id": session_id,
                    "devices": {f"reader.{reader_id}": session_id},
                    "last_event": 0,
                }
            }
        }
        t0 = time.monotonic()
        try:
            r = await client.post(
                f"{proxy}/hw_drivers/event",
                json=body, timeout=60.0,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            try:
                data = r.json()
            except ValueError:
                data = {}
            got = bool(data.get("result"))
            stats.record_poll(elapsed_ms, got)
        except (httpx.TimeoutException, httpx.HTTPError):
            stats.record_error()
            await asyncio.sleep(0.5)


async def producer(client: httpx.AsyncClient,
                   proxy: str, reader_id: str,
                   stop_at: float) -> None:
    """Inject fake scans into the proxy via /inject (external readers
    only). Falls back gracefully if the configured reader doesn't
    accept inject — in that case the bench still measures consumer-side
    behavior under genuine reader traffic from a hand scanner.
    """
    n = 0
    while time.monotonic() < stop_at:
        await asyncio.sleep(random.uniform(0.5, 2.0))
        n += 1
        barcode = f"BENCH{n:08d}"
        try:
            await client.post(
                f"{proxy}/readers/{reader_id}/inject",
                json={"barcode": barcode},
                timeout=5.0,
            )
        except Exception:
            pass  # not fatal — many readers are not external


async def reporter(stats: Stats, stop_at: float, log_path: str,
                   proxy_container: str) -> None:
    """Print + log a one-line summary every 60s. Also samples
    `docker stats --no-stream` for memory/CPU growth."""
    interval = 60.0
    started = time.monotonic()
    while time.monotonic() < stop_at:
        await asyncio.sleep(interval)
        elapsed = int(time.monotonic() - started)
        # Best-effort container metrics
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "stats", "--no-stream", "--format",
                "{{.CPUPerc}}|{{.MemUsage}}", proxy_container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            sample = out.decode().strip()
        except Exception:
            sample = "?"
        line = stats.summary(f"t+{elapsed}s") + f" | container={sample}"
        print(line, flush=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")


# ─── Main ─────────────────────────────────────────────────────────


async def main_async(args) -> None:
    stop_at = time.monotonic() + args.duration_seconds
    stats = Stats()
    log_path = f"bench_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    print(f"writing log to {log_path}")
    with open(log_path, "w") as f:
        f.write(
            f"# bench started at {datetime.now(timezone.utc).isoformat()}\n"
            f"# proxy={args.proxy} reader={args.reader_id} "
            f"consumers={args.consumers} duration={args.duration_seconds}s\n"
        )
    timeout = httpx.Timeout(connect=10.0, read=70.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        tasks = [
            asyncio.create_task(consumer(client, args.proxy,
                                         args.reader_id, stats, stop_at),
                                name=f"consumer-{i}")
            for i in range(args.consumers)
        ]
        if args.inject:
            tasks.append(asyncio.create_task(
                producer(client, args.proxy, args.reader_id, stop_at),
                name="producer"))
        tasks.append(asyncio.create_task(
            reporter(stats, stop_at, log_path, args.container),
            name="reporter"))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            for t in tasks:
                t.cancel()
    final = stats.summary("FINAL")
    print(final)
    with open(log_path, "a") as f:
        f.write(final + "\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", default="http://127.0.0.1:8001")
    p.add_argument("--reader-id", default="auto-honeywell-25070b52e4")
    p.add_argument("--consumers", type=int, default=50)
    p.add_argument("--duration-seconds", type=int, default=28800)  # 8h
    p.add_argument("--inject", action="store_true",
                   help="Also inject scans (works only on external readers)")
    p.add_argument("--container", default="odoo-erpnet-fp",
                   help="Docker container name for `docker stats` sampling")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
