"""PKCS#11 КЕП клиент — mutual-TLS + PKCS7 подпис през хардуерен токен.

Реализацията ползва `curl` + OpenSSL pkcs11 engine за mutual-TLS (доказано
работещо срещу ebenefits.nssi.bg; Python requests/gnutls се спъват в TLS
renegotiation, което НОИ изисква), и `openssl cms` за detached PKCS7 подпис.

Сигурност: PIN-ът не се логва; токенът никога не напуска машината; само
localhost достъп (контролира се от router-а на проксито).
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import os
from html import unescape

_logger = logging.getLogger(__name__)

# НОИ ЕРБЛ endpoint-и (Pril3 + WS_ePCDataForEGN).
NSSI_PULL_URL = "https://ebenefits.nssi.bg/ePCDataForEGN/ePCData.asmx"
NSSI_SICK_UPLOAD_URL = "https://ebenefits.nssi.bg/ePCWSUploadDataOnline"
NSSI_LKK_UPLOAD_URL = "https://ebenefits.nssi.bg/ePCLKKWSUploadDataOnline"
NSSI_NS = "https://eBenefits.nssi.bg/ePCDataForEGN"


class KepError(Exception):
    pass


class KepClient:
    def __init__(self, *, pkcs11_module=None, token, cert_id="%00%01", pin,
                 engine="pkcs11", timeout=60):
        """`token` = PKCS#11 token label (напр. 'b-trust'); `cert_id` = obj id
        (URI-encoded, напр. '%00%01'); `pin` = PIN на authentication slot-а."""
        self.pkcs11_module = pkcs11_module
        self.token = token
        self.cert_id = cert_id
        self.pin = pin
        self.engine = engine
        self.timeout = timeout

    # --------------------------------------------------------------- helpers
    def _uri(self, typ, with_pin=False):
        uri = "pkcs11:token=%s;id=%s;type=%s" % (self.token, self.cert_id, typ)
        if with_pin:
            uri += ";pin-value=%s" % self.pin
        return uri

    def _curl_mtls(self, url, headers=None, data=None):
        """mutual-TLS заявка с КЕП. Връща тялото на отговора (str).

        ⚠️ curl 8 + OpenSSL 3 махнаха ENGINE API → `--engine pkcs11` дава
        „SSL crypto engine not found". На OpenSSL 3 трябва pkcs11 PROVIDER
        (pkcs11-provider) + openssl.cnf с provider секция, и curl
        `--cert/--key pkcs11:...`. TODO: финализиране на provider конфига —
        `openssl cms` подписът (sign_cms) работи с engine, но curl mutual-TLS
        иска provider. (`openssl cms` CLI още поддържа engine; curl 8 — не.)
        """
        cmd = [
            "curl", "-sS", "--fail-with-body",
            "--engine", self.engine, "--cert-type", "ENG", "--key-type", "ENG",
            "--cert", self._uri("cert"),
            "--key", self._uri("private", with_pin=True),
        ]
        for h in (headers or []):
            cmd += ["-H", h]
        if data is not None:
            cmd += ["--data", "@-"]
        cmd.append(url)
        try:
            r = subprocess.run(
                cmd, input=(data or None), capture_output=True, text=True,
                timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise KepError("NSSI request timed out") from exc
        if r.returncode != 0:
            # Не логваме целия cmd (съдържа PIN) — само кода.
            _logger.error("ЕРБЛ curl rc=%s: %s", r.returncode, r.stderr[:300])
            raise KepError("mutual-TLS request failed (rc=%s)" % r.returncode)
        return r.stdout

    @staticmethod
    def _soap_envelope(body_inner):
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>%s</soap:Body></soap:Envelope>' % body_inner)

    @staticmethod
    def _extract_result(soap_resp, tag):
        """Вади <tag> от SOAP отговора и unescape-ва вътрешния XML низ."""
        from lxml import etree  # noqa: PLC0415
        root = etree.fromstring(soap_resp.encode("utf-8"))
        for el in root.iter():
            if etree.QName(el).localname == tag:
                return unescape(el.text or "")
        return ""

    # ------------------------------------------------------------------ pull
    def get_data_for_egn(self, egn, flag_egn, date1, date2):
        inner = (
            '<GetDataForEGN xmlns="%s">'
            '<egn>%s</egn><flagegn>%s</flagegn>'
            '<PeriodDate1>%s</PeriodDate1><PeriodDate2>%s</PeriodDate2>'
            '</GetDataForEGN>' % (NSSI_NS, egn, flag_egn, date1, date2))
        resp = self._curl_mtls(
            NSSI_PULL_URL,
            headers=["Content-Type: text/xml; charset=utf-8",
                     'SOAPAction: "%s/GetDataForEGN"' % NSSI_NS],
            data=self._soap_envelope(inner))
        return self._extract_result(resp, "GetDataForEGNResult")

    # ---------------------------------------------------------------- upload
    def upload_sick(self, in_xml, test=False):
        method = "ePCTestXMLData" if test else "ePCLoadSaveXML"
        return self._upload(NSSI_SICK_UPLOAD_URL, method, in_xml)

    def upload_lkk(self, in_xml, test=False):
        method = "ePCTestLKKXMLData" if test else "ePCLKKLoadSaveXML"
        return self._upload(NSSI_LKK_UPLOAD_URL, method, in_xml)

    def _upload(self, url, method, in_xml):
        # in_xml се escape-ва като текст в SOAP параметъра inXML.
        from xml.sax.saxutils import escape  # noqa: PLC0415
        inner = ('<%s xmlns="%s"><inXML>%s</inXML></%s>'
                 % (method, NSSI_NS, escape(in_xml), method))
        resp = self._curl_mtls(
            url,
            headers=["Content-Type: text/xml; charset=utf-8",
                     'SOAPAction: "%s/%s"' % (NSSI_NS, method)],
            data=self._soap_envelope(inner))
        return self._extract_result(resp, "%sResult" % method)

    # ------------------------------------------------------------------ sign
    def _export_signer_cert(self):
        """Извлича КЕП серта (PUBLIC, без PIN) от токена като PEM bytes.

        openssl cms `-signer` иска серта като ФАЙЛ (engine дава само ключа).
        cert_id е URI-encoded (%00%01) → за pkcs11-tool --id става hex (0001).
        """
        module = (self.pkcs11_module
                  or "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so")
        hex_id = self.cert_id.replace("%", "")
        r = subprocess.run(
            ["pkcs11-tool", "--module", module, "-r", "--type", "cert",
             "--id", hex_id],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            raise KepError("cannot read certificate from token")
        r2 = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-outform", "PEM"],
            input=r.stdout, capture_output=True, timeout=15)
        if r2.returncode != 0:
            raise KepError("cannot convert certificate DER->PEM")
        return r2.stdout

    def sign_cms(self, content_bytes):
        """Detached PKCS7/CMS подпис (.p7s) през токена — заменя StampIT.

        Серта (public) се чете от токена във временен PEM; частният ключ
        остава на токена (engine). НАП иска detached DER PKCS7.
        """
        cert_pem = self._export_signer_cert()
        paths = []
        try:
            with tempfile.NamedTemporaryFile(delete=False) as fin:
                fin.write(content_bytes)
                in_path = fin.name
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as fc:
                fc.write(cert_pem)
                cert_path = fc.name
            out_path = in_path + ".p7s"
            paths = [in_path, cert_path, out_path]
            # cms -sign БЕЗ -nodetach → DETACHED (контентът е external, .txt +
            # отделен .txt.p7s); -binary спира MIME каноникализация; DER outform.
            cmd = [
                "openssl", "cms", "-sign", "-binary", "-outform", "DER",
                "-engine", self.engine, "-keyform", "ENG",
                "-signer", cert_path,
                "-inkey", self._uri("private", with_pin=True),
                "-in", in_path, "-out", out_path,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
            if r.returncode != 0:
                _logger.error("CMS sign rc=%s: %s", r.returncode,
                              r.stderr.decode("utf-8", "replace")[:300])
                raise KepError("CMS sign failed (rc=%s)" % r.returncode)
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
