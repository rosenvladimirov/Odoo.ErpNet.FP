"""TCP shift bridge — proxy ↔ Android JSON-RPC client.

Long-lived TCP connection to Android `ShiftBridgeService` (default port
9103). One bridge per device, lazily created when a route first calls
`get_shift_registry().get(serial)`.

Wire protocol (NDJSON, UTF-8, `\n` separator):

    Request (proxy → Android):
        {"id": <int>, "method": "<method>", "params": {<dict>}}

    Response (Android → proxy):
        {"id": <int>, "result": {<dict>}}                              # ok
        {"id": <int>, "error": {"code": <int>, "message": "<str>"}}   # fail

    Notification (Android → proxy, no response expected):
        {"id": null, "method": "<event>", "params": {<dict>}}

Calls are correlation-ID based so multiple in-flight requests are safe,
but the `_call_lock` serialises them by default since the Android handler
is single-client (mirror на PinpadBridge mutex semantics).

Connection lifecycle:
  * On first `call()`: lazy connect with `connect_timeout`.
  * On any read/write error: cancel pending futures, reconnect with
    exponential backoff (`backoff_initial` → `backoff_max`).
  * Async notifications are pushed into `_notifications` queue; consumers
    iterate via `notifications()`.

Test hook: `ShiftBridge` accepts an injected `reader`/`writer` pair (see
`from_streams()` classmethod) so unit tests can drive the bridge against
a `asyncio.StreamReader.feed_data` buffer без real socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

from ....config.loader import ShiftConfig

_logger = logging.getLogger(__name__)


class ShiftBridgeError(Exception):
    """Raised when a call fails (Android error response, timeout, or
    transport error). `.code` is the JSON-RPC-style numeric code (или -1
    when transport-level)."""

    def __init__(self, message: str, code: int = -1) -> None:
        super().__init__(message)
        self.code = code


class ShiftBridge:
    """Persistent NDJSON client to a single Android ShiftBridgeService.

    Re-entrant safe: `call()` uses an `asyncio.Lock` to serialise
    request/response over the wire. Notifications are demuxed by the
    reader task and pushed into a queue.
    """

    def __init__(self, cfg: ShiftConfig) -> None:
        self.cfg = cfg
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._reader_task: Optional[asyncio.Task] = None
        # `_call_lock` serialises overlapping calls so we don't interleave
        # writes mid-frame. Android side is single-client — concurrent
        # in-flight requests would just sit in a Mutex anyway.
        self._call_lock = asyncio.Lock()
        # `_conn_lock` gates lazy connect attempts so two parallel
        # callers don't race to open two sockets.
        self._conn_lock = asyncio.Lock()
        # Backoff state survives disconnect/reconnect cycles within
        # a burst of failures; resets to `backoff_initial` after a
        # successful connect.
        self._backoff = cfg.backoff_initial
        self._closed = False

    # ─── Construction helpers ────────────────────────────────────────

    @classmethod
    def from_streams(
        cls,
        cfg: ShiftConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> "ShiftBridge":
        """Test entry point — bypasses real `open_connection`."""
        bridge = cls(cfg)
        bridge._reader = reader
        bridge._writer = writer
        bridge._reader_task = asyncio.create_task(
            bridge._read_loop(), name=f"shift-rx-{cfg.id}")
        return bridge

    # ─── Public API ──────────────────────────────────────────────────

    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a JSON-RPC-style request; await matching response.

        Raises `ShiftBridgeError` на timeout, transport error, или Android
        error response.
        """
        if self._closed:
            raise ShiftBridgeError("Bridge closed", code=-2)
        timeout = timeout if timeout is not None else self.cfg.call_timeout
        async with self._call_lock:
            await self._ensure_connected()
            assert self._writer is not None  # for type checker
            req_id = self._next_id
            self._next_id += 1
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = fut
            frame = {
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            try:
                self._writer.write(
                    (json.dumps(frame, ensure_ascii=False) + "\n")
                    .encode(self.cfg.encoding))
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                self._pending.pop(req_id, None)
                await self._reset_connection(reason=f"write failed: {exc}")
                raise ShiftBridgeError(
                    f"transport write: {exc}", code=-1) from exc
            try:
                resp = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                self._pending.pop(req_id, None)
                # Timeout не значи broken socket задължително — Android
                # може просто да е бавен. Не closing-ваме. Caller може да
                # retry.
                raise ShiftBridgeError(
                    f"timeout waiting for {method}",
                    code=-3) from exc
            if "error" in resp:
                err = resp["error"] or {}
                raise ShiftBridgeError(
                    str(err.get("message", "unknown error")),
                    code=int(err.get("code", -1)),
                )
            return resp.get("result") or {}

    def notifications(self) -> AsyncIterator[dict]:
        """Async iterator over notifications (id=null frames от Android).

        Each yielded dict has `method` + `params` (no `id`). Iterator is
        long-lived: it never raises StopIteration — caller breaks on
        `_closed`.
        """
        return _NotificationIterator(self)

    async def close(self) -> None:
        """Shut down the bridge — cancel reader task, close writer,
        wake pending callers with ShiftBridgeError."""
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    ShiftBridgeError("Bridge closed", code=-2))
        self._pending.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None

    @property
    def is_connected(self) -> bool:
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    # ─── Internal: connect / reconnect ───────────────────────────────

    async def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        async with self._conn_lock:
            if self.is_connected:
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.cfg.tcp_host, self.cfg.tcp_port),
                    timeout=self.cfg.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                # Don't escalate backoff exponentially when we haven't
                # even tried to reconnect yet — let the caller decide.
                raise ShiftBridgeError(
                    f"connect {self.cfg.tcp_host}:{self.cfg.tcp_port}: "
                    f"{exc.__class__.__name__}: {exc}",
                    code=-1,
                ) from exc
            self._reader = reader
            self._writer = writer
            self._backoff = self.cfg.backoff_initial
            self._reader_task = asyncio.create_task(
                self._read_loop(), name=f"shift-rx-{self.cfg.id}")
            _logger.info(
                "shift bridge %s connected → %s:%s",
                self.cfg.id, self.cfg.tcp_host, self.cfg.tcp_port)

    async def _reset_connection(self, reason: str = "") -> None:
        """Tear down current connection so next `call()` reconnects."""
        _logger.warning(
            "shift bridge %s reset: %s", self.cfg.id, reason or "manual")
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    ShiftBridgeError(
                        f"connection reset: {reason}", code=-1))
        self._pending.clear()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
        # Don't await reader task here — it may be the one calling us.
        # Sleep с exponential backoff before next reconnect attempt.
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, self.cfg.backoff_max)

    # ─── Internal: read loop ─────────────────────────────────────────

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while not self._closed:
                line = await self._reader.readline()
                if not line:
                    # EOF — peer closed.
                    await self._reset_connection(reason="EOF from peer")
                    return
                try:
                    frame = json.loads(line.decode(self.cfg.encoding))
                except (ValueError, UnicodeDecodeError) as exc:
                    _logger.warning(
                        "shift bridge %s: bad frame %r: %s",
                        self.cfg.id, line[:200], exc)
                    continue
                await self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.exception(
                "shift bridge %s read loop crashed", self.cfg.id)
            await self._reset_connection(reason="read loop exception")

    async def _dispatch_frame(self, frame: dict) -> None:
        # Notification (id is null или missing)
        if frame.get("id") is None:
            try:
                self._notifications.put_nowait(frame)
            except asyncio.QueueFull:
                _logger.warning(
                    "shift bridge %s: notification queue full — "
                    "dropping %s",
                    self.cfg.id, frame.get("method"))
            return
        # Response — match by id.
        try:
            req_id = int(frame["id"])
        except (KeyError, ValueError, TypeError):
            _logger.warning(
                "shift bridge %s: response with bad id: %r",
                self.cfg.id, frame)
            return
        fut = self._pending.pop(req_id, None)
        if fut is None:
            _logger.warning(
                "shift bridge %s: response for unknown id %s",
                self.cfg.id, req_id)
            return
        if not fut.done():
            fut.set_result(frame)


class _NotificationIterator:
    """Async iterator that drains the bridge's notification queue."""

    def __init__(self, bridge: ShiftBridge) -> None:
        self._bridge = bridge

    def __aiter__(self) -> "_NotificationIterator":
        return self

    async def __anext__(self) -> dict:
        if self._bridge._closed:
            raise StopAsyncIteration
        # `get()` blocks until next notification. If bridge closes,
        # the put-нати None sentinels (from `close()`) са пропуснати,
        # затова периодично проверяваме `_closed` чрез wait_for.
        while not self._bridge._closed:
            try:
                return await asyncio.wait_for(
                    self._bridge._notifications.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
        raise StopAsyncIteration


# ─── Process-wide registry ───────────────────────────────────────────


class ShiftBridgeRegistry:
    """Singleton lookup: `device_serial → ShiftBridge`."""

    def __init__(self) -> None:
        self._bridges: dict[str, ShiftBridge] = {}
        self._configs: dict[str, ShiftConfig] = {}

    def add_config(self, cfg: ShiftConfig) -> None:
        """Register a config without eagerly connecting. Call once per
        startup из `from_config()`."""
        self._configs[cfg.device_serial] = cfg

    def get(self, device_serial: str) -> Optional[ShiftBridge]:
        """Look up (or lazily create) a bridge for `device_serial`.
        Returns None if no config is registered for that serial.
        """
        bridge = self._bridges.get(device_serial)
        if bridge is not None:
            return bridge
        cfg = self._configs.get(device_serial)
        if cfg is None:
            return None
        bridge = ShiftBridge(cfg)
        self._bridges[device_serial] = bridge
        return bridge

    def all_serials(self) -> list[str]:
        return list(self._configs.keys())

    @classmethod
    def from_config(cls, app_config) -> "ShiftBridgeRegistry":
        reg = cls()
        for cfg in app_config.shifts or []:
            reg.add_config(cfg)
        return reg

    async def close_all(self) -> None:
        for bridge in list(self._bridges.values()):
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "error closing shift bridge %s", bridge.cfg.id)
        self._bridges.clear()


_REGISTRY_SINGLETON: Optional[ShiftBridgeRegistry] = None


def get_shift_registry() -> ShiftBridgeRegistry:
    """Process-wide singleton getter.

    Tests can monkey-patch this module's `_REGISTRY_SINGLETON` to inject
    a stub registry.
    """
    global _REGISTRY_SINGLETON
    if _REGISTRY_SINGLETON is None:
        _REGISTRY_SINGLETON = ShiftBridgeRegistry()
    return _REGISTRY_SINGLETON


def set_shift_registry(reg: ShiftBridgeRegistry) -> None:
    """Replace the process-wide singleton — for app startup."""
    global _REGISTRY_SINGLETON
    _REGISTRY_SINGLETON = reg


__all__ = [
    "ShiftBridge",
    "ShiftBridgeError",
    "ShiftBridgeRegistry",
    "get_shift_registry",
    "set_shift_registry",
]
