"""
Biometric API (Phase C / #6) — face identity, Channel-1 transport.

SYNCHRONOUS request→response — Odoo POSTs a 128-d descriptor and gets
a definitive verdict, the same zero-latency channel a barcode read or
an access command uses (NOT the Fleet command-queue).

The attendance/access DECISION is taken in Odoo (Channel-1 ⊕
Channel-2, fail-secure); Odoo enforces `x_bio_consent` (GDPR/ЗЗЛД
special category). This proxy is a THIN CLIENT in front of the
external face-auth Node μservice (author: Довид Р. Милев) — it never
reimplements matching. Identity is keyed only by the opaque
`subject_uuid` — proxy/face-auth never see PII.

Endpoints:
  GET    /biometric                       — list verifiers
  GET    /biometric/{id}                  — verifier info
  POST   /biometric/{id}/verify           — {descriptor:[128]} → verdict
  POST   /biometric/{id}/enroll           — {subjectUuid, descriptor}
  DELETE /biometric/{id}/enrolled/{uuid}  — GDPR erasure
  GET    /biometric/{id}/enrolled         — best-effort subject count
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/biometric", tags=["biometric"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BiometricInfoResp(_CamelModel):
    id: str
    driver: str
    fail_secure: bool = Field(True, alias="failSecure")


class VerifyReq(BaseModel):
    descriptor: List[float]


class EnrollReq(_CamelModel):
    subject_uuid: str = Field(alias="subjectUuid")
    descriptor: List[float]


def _registry(request: Request):
    return getattr(request.app.state, "biometric_registry", None)


def _require(request: Request, id: str):
    reg = _registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Biometric verifier {id!r} not found")
    return reg


@router.get("", response_model=dict[str, BiometricInfoResp])
@router.get("/", include_in_schema=False)
async def list_biometric(request: Request):
    reg = _registry(request)
    if reg is None:
        return {}
    return {
        bid: BiometricInfoResp(id=bid, driver=e.config.driver,
                               fail_secure=e.config.fail_secure)
        for bid, e in reg.biometric.items()
    }


@router.get("/{id}", response_model=BiometricInfoResp)
async def biometric_info(id: str, request: Request):
    reg = _require(request, id)
    e = reg.get(id)
    return BiometricInfoResp(id=id, driver=e.config.driver,
                             fail_secure=e.config.fail_secure)


@router.post("/{id}/verify")
async def biometric_verify(id: str, request: Request, req: VerifyReq):
    """1:N match. The DECISION stays in Odoo — this only reports."""
    reg = _require(request, id)
    try:
        async with reg.with_biometric(id) as v:
            res = await asyncio.to_thread(v.verify, req.descriptor)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Biometric {id!r} verify failed: {exc}",
        ) from exc
    return res.to_json()


@router.post("/{id}/enroll")
async def biometric_enroll(id: str, request: Request, req: EnrollReq):
    """Enroll a descriptor under the opaque subject UUID. Consent
    (`x_bio_consent`) is enforced in Odoo BEFORE this is called."""
    reg = _require(request, id)
    try:
        async with reg.with_biometric(id) as v:
            res = await asyncio.to_thread(
                v.enroll, req.subject_uuid, req.descriptor)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Biometric {id!r} enroll failed: {exc}",
        ) from exc
    return res.to_json()


@router.delete("/{id}/enrolled/{subject_uuid}")
async def biometric_erase(id: str, subject_uuid: str, request: Request):
    """GDPR/ЗЗЛД right-to-erasure — purge a subject from face-auth."""
    reg = _require(request, id)
    try:
        async with reg.with_biometric(id) as v:
            res = await asyncio.to_thread(v.erase, subject_uuid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Biometric {id!r} erase failed: {exc}",
        ) from exc
    return res.to_json()


@router.get("/{id}/enrolled")
async def biometric_enrolled(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_biometric(id) as v:
            res = await asyncio.to_thread(v.list_subjects)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Biometric {id!r} list failed: {exc}",
        ) from exc
    return res.to_json()
