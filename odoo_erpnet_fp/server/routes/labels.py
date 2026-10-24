# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""
Extension API for label printing through glabels-qt.

Not part of ErpNet.FP. URL prefix is `/labels`. JSON-only.

Endpoints:
  GET  /labels/printers   — the OS printers, the default one, glabels version
  POST /labels/print      — .glabels template (base64) + records → OS printer

Auth: като `/scales` и `/printers` — викат се от браузъра на таблета
(през тунела на компютъра), затова маршрутът няма собствен подпис;
HMAC с тайната на проксито е само за маршрутите Odoo-сървър → прокси.

Отговорите са по договора: HTTP 200 и при грешка на glabels/печата
(`{"ok": false, "error": …}`), 4xx само при невалидна заявка.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..label_printing import LabelPrinter, LabelRequestError

router = APIRouter(prefix="/labels", tags=["labels"])


class LabelPrinterInfo(BaseModel):
    name: str
    default: bool = False


class PrintersResp(BaseModel):
    printers: list[LabelPrinterInfo]
    glabels: Optional[str] = None


class PrintReq(BaseModel):
    model_config = ConfigDict(extra="ignore")

    printer: str
    template: str
    records: list[dict[str, Any]]
    copies: int = 1


def _label_printer(request: Request) -> LabelPrinter:
    lp = getattr(request.app.state, "label_printer", None)
    if lp is None or not lp.cfg.enabled:
        raise HTTPException(status_code=404,
                            detail="Label printing is disabled")
    return lp


@router.get("/printers", response_model=PrintersResp)
def list_printers(request: Request) -> PrintersResp:
    lp = _label_printer(request)
    # Изричното питане е свежо — кешът е за `/healthz`.
    return PrintersResp(
        printers=[LabelPrinterInfo(**p) for p in lp.printers(refresh=True)],
        glabels=lp.glabels_version(refresh=True),
    )


@router.post("/print")
def print_labels(request: Request, body: PrintReq) -> dict:
    lp = _label_printer(request)
    try:
        return lp.print_labels(body.printer, body.template, body.records,
                               body.copies)
    except LabelRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
