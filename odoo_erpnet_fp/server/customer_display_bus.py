"""
Customer display bus — POS → proxy → screens and pole displays.

The Point of Sale posts the current order (the same data Odoo's own
customer display uses) to `POST /customer-display/{channel}`. The proxy
keeps the last state per channel and pushes it to every screen that
listens on `/customer-display/{channel}/events` (SSE) — the page at
`/customer-display/{channel}` is served by the proxy itself, so a
monitor needs only a browser pointed at the proxy, not Odoo.

The same update also drives a pole display (VFD 2×20, e.g. Datecs
DPD-201) when the POS names one: `pole_lines()` turns the order into
two short lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

_logger = logging.getLogger(__name__)

DEFAULT_LABELS = {
    "welcome": "Добре дошли",
    "total": "ОБЩО",
    "change": "РЕСТО",
    "paid": "ПЛАТЕНО",
    "thanks": "Благодарим ви!",
}


def _clean(text) -> str:
    """Formatted amounts use non-breaking spaces; the VFD knows plain ones."""
    return " ".join(str(text or "").replace("\xa0", " ").replace(" ", " ").split())


def _fit(left: str, right: str, width: int) -> str:
    """`left` and `right` on one line; the right part wins when it does not fit."""
    left, right = _clean(left), _clean(right)
    if len(right) >= width:
        return right[-width:]
    room = width - len(right) - 1
    if room <= 0:
        return right.rjust(width)
    return (left[:room]).ljust(room) + " " + right


def pole_lines(payload: dict, width: int = 20, encoding: str = "") -> tuple[str, str]:
    """The two lines of a pole display for the current state of the order.

    Empty order → welcome / shop; paid with change → change; payment lines
    → total; otherwise the selected (or last) line: name / qty × unit + price.
    `encoding`: the code page of the display — the euro sign is spelled out
    BEFORE the layout, otherwise the padded line gets longer than the display.
    """
    if encoding:
        payload = _spell_out(payload, encoding)
    labels = {**DEFAULT_LABELS, **(payload.get("labels") or {})}
    order = payload.get("order") or {}
    lines = order.get("lines") or []
    shop = _clean(payload.get("shop"))
    if order.get("finalized") and order.get("change"):
        return _clean(labels["change"])[:width], _clean(order["change"]).rjust(width)[-width:]
    if order.get("finalized"):
        return _clean(labels["thanks"]).center(width)[:width], shop.center(width)[:width]
    if not lines:
        return _clean(labels["welcome"]).center(width)[:width], shop.center(width)[:width]
    if order.get("paymentLines"):
        return _clean(labels["total"])[:width], _clean(order.get("amount")).rjust(width)[-width:]
    line = next((l for l in lines if l.get("isSelected")), lines[-1])
    top = _clean(line.get("productName"))[:width]
    qty = _clean(line.get("qty"))
    unit = _clean(line.get("unitPrice"))
    left = f"{qty} x {unit}" if qty and qty not in ("1", "1,00", "1.00") else ""
    return top, _fit(left, _clean(line.get("price")), width)


def encodable(text: str, encoding: str) -> str:
    """The euro sign is missing in cp437/cp850 — spell it out there."""
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.replace("€", "EUR")


def _spell_out(value, encoding: str):
    """`encodable()` върху всички низове в данните (копие)."""
    if isinstance(value, str):
        return encodable(value, encoding)
    if isinstance(value, dict):
        return {k: _spell_out(v, encoding) for k, v in value.items()}
    if isinstance(value, list):
        return [_spell_out(v, encoding) for v in value]
    return value


class CustomerDisplayChannel:
    """The last state of one channel and the queues of its listening screens."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.payload: dict = {}
        self.version = 0
        self.updated_at = 0.0
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def snapshot(self) -> dict:
        return {"channel": self.name, "version": self.version, "updatedAt": self.updated_at, **self.payload}

    def publish(self, payload: dict) -> dict:
        self.payload = payload
        self.version += 1
        self.updated_at = time.time()
        snap = self.snapshot()
        message = json.dumps(snap, ensure_ascii=False)
        for queue in list(self._subscribers):
            if queue.full():
                # бавен екран: старото състояние не е нужно — само последното
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)
        return snap


class CustomerDisplayBus:
    """All channels. A channel appears on its first update or listener."""

    def __init__(self) -> None:
        self._channels: dict[str, CustomerDisplayChannel] = {}

    def channel(self, name: str) -> CustomerDisplayChannel:
        if name not in self._channels:
            self._channels[name] = CustomerDisplayChannel(name)
        return self._channels[name]

    def channels(self) -> dict[str, CustomerDisplayChannel]:
        return dict(self._channels)
