"""КЕП подпис API — detached PKCS7 (.p7s) през хардуерния токен.

Заменя StampIT LSManager: НАП портал (и др.) приемат файл + detached .p7s.
Браузърът/Odoo праща съдържанието, проксито го подписва с токена и връща
подписа — същата роля като StampIT, но наш код, един токен за всичко.

Endpoint:
  POST /kep/sign  — body {content_b64} → {p7s_b64}
"""
from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ..drivers.kep import KepClient, KepError

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kep", tags=["kep"])


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


class SignReq(BaseModel):
    content_b64: str


@router.post("/sign")
def sign(req: SignReq, request: Request):
    try:
        content = base64.b64decode(req.content_b64)
        p7s = _client(request).sign_cms(content)
    except KepError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # base64/въвеждане
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"p7s_b64": base64.b64encode(p7s).decode("ascii")}
