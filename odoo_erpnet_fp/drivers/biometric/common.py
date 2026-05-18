"""
Common types for biometric verifiers (Phase C / #6 — access add-on).

A `BiometricVerifier` is a request/response identity device — the same
shape as an `AccessActuator` is for barriers: the host issues a
discrete operation (verify / enroll / erase) and gets one synchronous
result. There is **no continuous readback**.

**The access/attendance DECISION is taken in Odoo** (Channel-1 ⊕
Channel-2, fail-secure). This proxy NEVER reimplements face matching —
it is a thin client in front of the external face-auth Node μservice
(author: Довид Р. Милев; HTTP-coupled, copyleft-clean, no vendoring).
The proxy only relays and reports; Odoo enforces `x_bio_consent`
(GDPR/ЗЗЛД special category) and drives the attendance toggle.

Identity is keyed ONLY by the opaque `subject_uuid`
(`hr.employee.x_bio_subject_uuid`) — the proxy/face-auth never see
PII. Native-IoT push uses identifier `biometric.<terminal>` (parity
with `camera.<id>` / `hac.card/<id>`); the standard fiscal/pinpad/
reader/scale/display paths are byte-identical and untouched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class BiometricResult:
    terminal_id: str
    action: str                       # verify | enroll | erase | list
    ok: bool = True
    subject_uuid: str = ""            # opaque — никога PII
    verdict: str = ""                 # MATCH | WEAK | NO_MATCH | EMPTY
    distance: Optional[float] = None  # Euclidean (по-малко = по-близо)
    detail: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def to_json(self) -> dict:
        return {
            "terminalId": self.terminal_id,
            "action": self.action,
            "ok": self.ok,
            "subjectUuid": self.subject_uuid,
            "verdict": self.verdict,
            "distance": self.distance,
            "detail": self.detail,
            "timestamp": self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        }


class BiometricVerifier(ABC):
    """ABC for biometric subject verification/enrolment.

    Lifecycle:
        v = FaceAuthVerifier(terminal_id="t1", base_url="http://...")
        v.connect()
        v.verify(descriptor)              # 1:N → BiometricResult
        v.enroll(subject_uuid, descriptor)
        v.erase(subject_uuid)             # GDPR right-to-erasure
        v.disconnect()

    `fail_secure` е семантичен флаг за Odoo/оператора — прокси-то и без
    друго никога не „пуска" самò: то само докладва верификацията,
    решението е в Odoo.
    """

    def __init__(self, terminal_id: str, fail_secure: bool = True) -> None:
        self.terminal_id = terminal_id
        self.fail_secure = fail_secure

    @abstractmethod
    def connect(self) -> None:
        """Acquire the transport (idempotent; may connect lazily)."""

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def verify(self, descriptor: List[float]) -> BiometricResult:
        """1:N match на 128-d дескриптор срещу in-memory базата."""

    @abstractmethod
    def enroll(self, subject_uuid: str,
               descriptor: List[float]) -> BiometricResult:
        """Регистрира дескриптор под непрозрачния subject_uuid."""

    @abstractmethod
    def erase(self, subject_uuid: str) -> BiometricResult:
        """GDPR/ЗЗЛД заличаване — purge на subject_uuid от базата."""

    def list_subjects(self) -> BiometricResult:
        """Best-effort: брой дескриптори по subject (без PII)."""
        return BiometricResult(
            terminal_id=self.terminal_id, action="list",
            ok=True, detail="not supported by this verifier",
        )
