"""НОИ ЕРБЛ API — дърпане/качване на болнични и решения на ЛКК през КЕП токена.

Проксито прави mutual-TLS към НОИ с хардуерния КЕП (токенът = устройство).
Odoo (l10n_bg_api_nssi_erbl) вика тези endpoints вместо да прави SOAP-а сам,
защото токенът е на потребителската машина, не на Odoo сървъра.

Endpoints:
  POST /nssi/get_data_for_egn  — справка по ЕГН/период → <ePCReport>
  POST /nssi/upload_sick       — подаване/тест на болнични → <result>
  POST /nssi/upload_lkk        — подаване/тест на решения ЛКК → <result>
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ..drivers.kep import KepClient, KepError

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/nssi", tags=["nssi"])


def _client(request: Request) -> KepClient:
    cfg = getattr(request.app.state, "config", None)
    kep = getattr(cfg, "kep", None) if cfg else None
    if not kep or not getattr(kep, "enabled", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KEP not configured on this proxy (config.yaml: kep).")
    return KepClient(
        pkcs11_module=kep.pkcs11_module, token=kep.token,
        cert_id=kep.cert_id, pin=kep.pin, engine=kep.engine)


class PullReq(BaseModel):
    egn: str
    flag_egn: str = "0"
    date_from: str  # yyyy-mm-dd
    date_to: str


class UploadReq(BaseModel):
    xml: str
    test: bool = False


@router.post("/get_data_for_egn")
def get_data_for_egn(req: PullReq, request: Request):
    try:
        report = _client(request).get_data_for_egn(
            req.egn, req.flag_egn, req.date_from, req.date_to)
    except KepError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"report": report}


@router.post("/upload_sick")
def upload_sick(req: UploadReq, request: Request):
    try:
        result = _client(request).upload_sick(req.xml, test=req.test)
    except KepError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"result": result}


@router.post("/upload_lkk")
def upload_lkk(req: UploadReq, request: Request):
    try:
        result = _client(request).upload_lkk(req.xml, test=req.test)
    except KepError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"result": result}
