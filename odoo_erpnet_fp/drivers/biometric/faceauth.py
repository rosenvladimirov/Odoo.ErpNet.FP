"""
FaceAuthVerifier — тънък HTTP клиент към външния face-auth μservice.

face-auth (Node/Express, автор Довид Р. Милев) остава самостоятелна
услуга зад прокси-то — **НЕ се reimplement-ва тук** (HTTP-coupled,
copyleft-clean; IP-то на Довид остава негово). Този клас само превежда
прокси-вата `BiometricVerifier` ABC към реалния face-auth REST:

  POST   /api/verify    {descriptor:[128]} → {ranked,top:{name,dist},verdict}
  POST   /api/enroll    {name,descriptor[128]} → {ok,name,count}
  DELETE /api/enrolled/<name> → {ok:true}
  GET    /api/enrolled   → {<name>: <count>}

`name` == непрозрачния `subject_uuid` (= `hr.employee.x_bio_subject_uuid`)
— нито прокси-то, нито face-auth виждат PII. verdict прагове идват от
face-auth (`<0.55 MATCH`, `<0.65 WEAK`). Fail-secure: timeout/non-200/
грешка → `ok=False` (Odoo DENY-ва; решението НЕ е тук).
"""

from __future__ import annotations

import logging
from typing import List

import httpx

from .common import BiometricResult, BiometricVerifier

_log = logging.getLogger(__name__)


class FaceAuthVerifier(BiometricVerifier):
    def __init__(self, terminal_id: str, base_url: str,
                 timeout: float = 8.0, fail_secure: bool = True) -> None:
        super().__init__(terminal_id, fail_secure=fail_secure)
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.Client | None = None

    # ── lifecycle ───────────────────────────────────────────────────
    def connect(self) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def _c(self) -> httpx.Client:
        if self._client is None:
            self.connect()
        return self._client  # type: ignore[return-value]

    # ── operations ──────────────────────────────────────────────────
    def verify(self, descriptor: List[float]) -> BiometricResult:
        if not isinstance(descriptor, (list, tuple)) or len(descriptor) != 128:
            return BiometricResult(
                terminal_id=self.terminal_id, action="verify",
                ok=False, detail="descriptor[128] required")
        try:
            r = self._c().post(f"{self.base_url}/api/verify",
                                json={"descriptor": list(descriptor)})
            if r.status_code != 200:
                return BiometricResult(
                    terminal_id=self.terminal_id, action="verify",
                    ok=False, detail=f"HTTP {r.status_code}")
            data = r.json()
            top = data.get("top") or {}
            return BiometricResult(
                terminal_id=self.terminal_id, action="verify",
                ok=bool(top), subject_uuid=top.get("name", ""),
                verdict=data.get("verdict", ""),
                distance=top.get("dist"))
        except Exception as e:  # noqa: BLE001 — fail-secure
            _log.warning("faceauth verify unreachable: %s", e)
            return BiometricResult(
                terminal_id=self.terminal_id, action="verify",
                ok=False, detail=str(e))

    def enroll(self, subject_uuid: str,
               descriptor: List[float]) -> BiometricResult:
        if not subject_uuid or len(descriptor or []) != 128:
            return BiometricResult(
                terminal_id=self.terminal_id, action="enroll",
                ok=False, subject_uuid=subject_uuid or "",
                detail="subject_uuid + descriptor[128] required")
        try:
            r = self._c().post(
                f"{self.base_url}/api/enroll",
                json={"name": subject_uuid,
                      "descriptor": list(descriptor)})
            ok = r.status_code == 200 and bool(r.json().get("ok"))
            return BiometricResult(
                terminal_id=self.terminal_id, action="enroll",
                ok=ok, subject_uuid=subject_uuid,
                detail="" if ok else f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 — fail-secure
            _log.warning("faceauth enroll unreachable: %s", e)
            return BiometricResult(
                terminal_id=self.terminal_id, action="enroll",
                ok=False, subject_uuid=subject_uuid, detail=str(e))

    def erase(self, subject_uuid: str) -> BiometricResult:
        """GDPR/ЗЗЛД right-to-erasure. Best-effort: дори при грешка
        Odoo ротира UUID-а → дескрипторите стават недостижими."""
        if not subject_uuid:
            return BiometricResult(
                terminal_id=self.terminal_id, action="erase",
                ok=False, detail="subject_uuid required")
        try:
            r = self._c().delete(
                f"{self.base_url}/api/enrolled/{subject_uuid}")
            ok = r.status_code == 200
            return BiometricResult(
                terminal_id=self.terminal_id, action="erase",
                ok=ok, subject_uuid=subject_uuid,
                detail="" if ok else f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 — fail-secure
            _log.warning("faceauth erase unreachable: %s", e)
            return BiometricResult(
                terminal_id=self.terminal_id, action="erase",
                ok=False, subject_uuid=subject_uuid, detail=str(e))

    def list_subjects(self) -> BiometricResult:
        try:
            r = self._c().get(f"{self.base_url}/api/enrolled")
            ok = r.status_code == 200
            return BiometricResult(
                terminal_id=self.terminal_id, action="list",
                ok=ok,
                detail=(str(len(r.json())) + " subjects") if ok
                else f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 — fail-secure
            _log.warning("faceauth list unreachable: %s", e)
            return BiometricResult(
                terminal_id=self.terminal_id, action="list",
                ok=False, detail=str(e))
