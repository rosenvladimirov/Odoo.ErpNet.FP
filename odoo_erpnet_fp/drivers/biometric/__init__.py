"""
Biometric verifiers (Phase C / #6) — face identity, Channel-1 transport.

Request/response identity devices. The proxy is a THIN CLIENT in front
of the external face-auth Node μservice (author: Довид Р. Милев) — it
never reimplements face matching (HTTP-coupled, copyleft-clean). The
attendance/access DECISION stays in Odoo (fail-secure); Odoo enforces
`x_bio_consent` (GDPR/ЗЗЛД special category). Identity is keyed only by
the opaque `subject_uuid` — proxy/face-auth never see PII.

Drivers:
  faceauth — @vladmandic/face-api Node μsvc via /api/verify|enroll +
             DELETE /api/enrolled/<uuid> (GDPR erasure)
"""

from .common import BiometricResult, BiometricVerifier
from .faceauth import FaceAuthVerifier

__all__ = [
    "BiometricResult",
    "BiometricVerifier",
    "FaceAuthVerifier",
]
