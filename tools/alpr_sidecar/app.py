"""
fast-alpr sidecar μservice — reference implementation.

Wraps the MIT-licensed `fast-alpr` (ONNX: YOLO plate detector +
fast-plate-ocr) behind the stable contract the proxy's
`FastAlprSiblingEngine` expects:

    POST /v1/recognize   (multipart: image, [min_confidence], [region])
    →    {"results": [{"plate","confidence","box":{x,y,w,h}}]}

This stays a SEPARATE process/container on purpose — the heavy ONNX /
OpenCV stack never enters the fiscal Python process (same principle as
the Phase C face-auth μservice). The proxy is a thin httpx client.

`region` is advisory only — the global/EU OCR model already covers
Bulgarian plates; the field is accepted for forward-compat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, Form, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("alpr-sidecar")

_DETECTOR = os.environ.get(
    "ALPR_DETECTOR", "yolo-v9-t-384-license-plate-end2end"
)
_OCR = os.environ.get("ALPR_OCR", "global-plates-mobile-vit-v2-model")

app = FastAPI(title="erpnet-alpr-sidecar", version="1.0.0")
_alpr = None  # lazy — moделите се теглят/зареждат при първия request


def _engine():
    global _alpr
    if _alpr is None:
        from fast_alpr import ALPR  # heavy import — само тук
        _logger.info("Loading fast-alpr (detector=%s ocr=%s)", _DETECTOR, _OCR)
        _alpr = ALPR(detector_model=_DETECTOR, ocr_model=_OCR)
    return _alpr


@app.get("/healthz")
def healthz():
    return {"ok": True, "detector": _DETECTOR, "ocr": _OCR,
            "loaded": _alpr is not None}


@app.post("/v1/recognize")
async def recognize(
    image: UploadFile,
    min_confidence: float = Form(0.0),
    region: str = Form(""),  # advisory; виж docstring
):
    raw = await image.read()
    if not raw:
        raise HTTPException(400, "Empty image")
    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Undecodable image (expected JPEG/PNG)")

    return {"results": _predict_bgr(frame, float(min_confidence))}


def _predict_bgr(frame, min_confidence: float) -> list[dict]:
    """Run fast-alpr on a BGR ndarray → contract result list."""
    results: list[dict] = []
    for r in _engine().predict(frame):
        ocr = getattr(r, "ocr", None)
        text = (getattr(ocr, "text", "") or "").strip().upper()
        conf = float(getattr(ocr, "confidence", 0.0) or 0.0)
        if not text or conf < min_confidence:
            continue
        box = None
        det = getattr(r, "detection", None)
        bb = getattr(det, "bounding_box", None)
        if bb is not None:
            # fast-alpr връща x1,y1,x2,y2 — превръщаме в x,y,w,h
            x1 = int(getattr(bb, "x1", 0))
            y1 = int(getattr(bb, "y1", 0))
            x2 = int(getattr(bb, "x2", 0))
            y2 = int(getattr(bb, "y2", 0))
            box = {"x": x1, "y": y1, "w": max(0, x2 - x1),
                   "h": max(0, y2 - y1)}
        results.append({"plate": text, "confidence": round(conf, 4),
                        "box": box})
    return results


# ─── Mode B — stream-watch (zero proxy frame work) ───────────────
#
# Enable by setting env ALPR_WATCH to a JSON list. The sidecar then
# pulls each go2rtc snapshot itself and POSTs ONLY recognised plates
# to the proxy's /cameras/{id}/inject — the fiscal proxy does no
# frame work at all (camera configured as `driver: external`).
#
#   ALPR_WATCH='[{"camera_id":"gateB",
#     "snapshot_url":"http://go2rtc:1984/api/frame.jpeg?src=gateB",
#     "inject_url":"http://odoo-erpnet-fp:8001/cameras/gateB/inject",
#     "interval":1.0,"min_confidence":0.55,"cooldown":8.0}]'


async def _watch_stream(spec: dict) -> None:
    cam = spec["camera_id"]
    snap_url = spec["snapshot_url"]
    inject_url = spec["inject_url"]
    interval = float(spec.get("interval", 1.0))
    min_conf = float(spec.get("min_confidence", 0.5))
    cooldown = float(spec.get("cooldown", 8.0))
    seen: dict[str, float] = {}
    _logger.info("Mode B watcher started for %s every %.1fs", cam, interval)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            t0 = time.monotonic()
            try:
                r = await client.get(snap_url)
                r.raise_for_status()
                arr = np.frombuffer(r.content, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    # ALPR е CPU/sync → в нишка, за да не блокира loop-а
                    plates = await asyncio.to_thread(
                        _predict_bgr, frame, min_conf
                    )
                    now = time.monotonic()
                    for p in plates:
                        plate = p["plate"]
                        if now - seen.get(plate, 0.0) < cooldown:
                            continue
                        seen[plate] = now
                        try:
                            await client.post(inject_url, json={
                                "plate": plate,
                                "confidence": p["confidence"],
                            })
                        except Exception as exc:  # noqa: BLE001
                            _logger.warning(
                                "%s inject failed: %s", cam, exc
                            )
            except Exception:  # noqa: BLE001
                _logger.debug("%s watch tick failed", cam, exc_info=True)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))


@app.on_event("startup")
async def _start_watchers() -> None:
    raw = os.environ.get("ALPR_WATCH", "").strip()
    if not raw:
        return
    try:
        specs = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _logger.error("ALPR_WATCH is not valid JSON: %s", exc)
        return
    for spec in specs or []:
        asyncio.create_task(_watch_stream(spec))
