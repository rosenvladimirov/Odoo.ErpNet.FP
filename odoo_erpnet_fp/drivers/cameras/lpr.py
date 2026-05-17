"""
Pluggable ALPR (Automatic License-Plate Recognition) engines.

The proxy itself stays free of heavy ML / native dependencies. The
default engine talks to a **fast-alpr sibling μservice** over HTTP
(same architectural pattern as the Phase C face-auth driver — the
ONNX / OpenCV stack lives in its own container, not in the fiscal
Python process). A reference sidecar is shipped under
`tools/alpr_sidecar/`.

Sibling contract (stable):

    POST {base_url}/v1/recognize
        multipart/form-data:
            image          = <jpeg bytes>          (required)
            min_confidence  = <float>               (optional)
            region          = <"bg" | "eu" | ...>   (optional hint)

    200 → {"results": [
              {"plate": "CA1234AB",
               "confidence": 0.93,
               "box": {"x": 120, "y": 88, "w": 210, "h": 70}}
           ]}

`engine="cloud"` is left as an explicit plug-in seam (Plate Recognizer
or any other API that adopts the same response shape) — wire it by
pointing `url` at a translating adapter, no driver change needed.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

_logger = logging.getLogger(__name__)


@dataclass
class PlateCandidate:
    plate: str
    confidence: float
    bbox: Optional[tuple[int, int, int, int]] = None


class LprEngine(ABC):
    """Synchronous recognise() — called from the camera sampling thread."""

    name: str = "base"

    @abstractmethod
    def recognize(self, jpeg: bytes) -> list[PlateCandidate]:
        """Return zero or more plate candidates for one JPEG frame.

        Must never raise — transient engine failures return ``[]`` so
        the sampling loop keeps running.
        """

    def close(self) -> None:  # noqa: D401 — optional cleanup hook
        """Release any held resources (HTTP client, model, ...)."""


class NullLprEngine(LprEngine):
    """No-op engine — used when `lpr.enabled` is false. Camera still
    streams + serves snapshots; it just never emits plate events."""

    name = "none"

    def recognize(self, jpeg: bytes) -> list[PlateCandidate]:
        return []


class FastAlprSiblingEngine(LprEngine):
    """Thin HTTP client to the fast-alpr sidecar μservice.

    Heavy lifting (YOLO plate detector + fast-plate-ocr) runs in the
    sibling; this class only POSTs a JPEG and parses the JSON. A bad
    response / timeout degrades to ``[]`` (fail-open for *recognition*;
    the access *decision* is taken in Odoo and is fail-secure there).
    """

    name = "fast_alpr"

    def __init__(
        self,
        url: str,
        min_confidence: float = 0.5,
        region: str = "bg",
        timeout: float = 4.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.min_confidence = min_confidence
        self.region = region
        self._client = httpx.Client(timeout=timeout)

    def recognize(self, jpeg: bytes) -> list[PlateCandidate]:
        try:
            resp = self._client.post(
                f"{self.url}/v1/recognize",
                files={"image": ("frame.jpg", jpeg, "image/jpeg")},
                data={
                    "min_confidence": str(self.min_confidence),
                    "region": self.region,
                },
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ALPR sidecar %s call failed: %s", self.url, exc)
            return []

        out: list[PlateCandidate] = []
        for r in (body or {}).get("results", []) or []:
            plate = str(r.get("plate") or "").strip().upper()
            conf = float(r.get("confidence") or 0.0)
            if not plate or conf < self.min_confidence:
                continue
            box = r.get("box") or {}
            bbox = None
            if all(k in box for k in ("x", "y", "w", "h")):
                bbox = (int(box["x"]), int(box["y"]),
                        int(box["w"]), int(box["h"]))
            out.append(PlateCandidate(plate=plate, confidence=conf, bbox=bbox))
        return out

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def make_lpr_engine(
    enabled: bool,
    engine: str,
    url: str,
    min_confidence: float,
    region: str,
) -> LprEngine:
    """Factory — config → concrete engine. Unknown engine → Null + warn."""
    if not enabled or engine in ("none", "", None):
        return NullLprEngine()
    if engine in ("fast_alpr", "fast-alpr", "cloud"):
        # `cloud` ползва същия wire-contract през адаптер на `url`
        return FastAlprSiblingEngine(
            url=url, min_confidence=min_confidence, region=region,
        )
    _logger.warning(
        "Unknown LPR engine %r — falling back to NullLprEngine", engine
    )
    return NullLprEngine()
