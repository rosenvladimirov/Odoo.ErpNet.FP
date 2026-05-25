"""
HMAC-signed POST to the paired Odoo instance.

Extracted from the inline pattern in `server/registry.py:380-401, 446-503`
so multiple routes (heartbeat, command-result, shift_close) can share it
without duplicating the sign + send + parse logic.

Both legs are HMAC-SHA256 over the canonical-JSON bytes:

    raw = json.dumps(body, separators=(",",":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, raw, sha256).hexdigest()

    POST <url> + headers {Content-Type: application/json,
                          X-Registry-Signature: <sig>}

The Odoo side (`l10n_bg_erp_net_fp_fleet/controllers/registry.py`) already
verifies this format, so the same controller pattern is used by the new
`/erp_net_fp/shift_close` endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)


def canonicalise(body: dict) -> bytes:
    """Канонизация на JSON тяло за HMAC.

    `sort_keys=True` и компактни сепаратори — Odoo прави същото при
    проверка на подписа.
    """
    return json.dumps(body, separators=(",", ":"),
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_body(raw: bytes, secret: str) -> str:
    """HMAC-SHA256 hex digest."""
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, secret: str, sig: str) -> bool:
    """Const-time compare. Връща False при празен sig/secret."""
    if not sig or not secret:
        return False
    expected = sign_body(raw, secret)
    return hmac.compare_digest(expected, sig)


async def post_signed(
    url: str,
    body: dict,
    *,
    secret: str,
    timeout: float = 30.0,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST `body` (canonicalised + signed) to `url`.

    Returns `(http_status, parsed_json_body)`. На non-JSON отговор
    парсва `{"raw": "<text>"}` за да остане shape-а постоянен.

    Не raise-ва при transport грешка — връща `(0, {"error": "..."})`.
    Caller трябва да реши retry policy.
    """
    raw = canonicalise(body)
    sig = sign_body(raw, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Registry-Signature": sig,
    }
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                url, content=raw, headers=headers, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            _logger.warning("post_signed transport error %s: %s", url, exc)
            return 0, {"error": f"transport: {exc.__class__.__name__}: {exc}"}
    try:
        parsed = r.json()
    except ValueError:
        parsed = {"raw": r.text[:500]}
    return r.status_code, parsed


__all__ = [
    "canonicalise",
    "sign_body",
    "verify_signature",
    "post_signed",
]
