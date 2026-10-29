"""certpull: тегли сертификата от cert-issuer и го слага за Traefik."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from odoo_erpnet_fp import certpull

CERT = {"names": ["*.erpnet.example"], "serial": "a1", "notAfter": 1900000000,
        "chain": "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n",
        "key": "-----BEGIN EC PRIVATE KEY-----\nBBB\n-----END EC PRIVATE KEY-----\n"}


@pytest.fixture
def server():
    seen = []
    state = {"status": 200, "body": dict(CERT)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            raw = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, state
    srv.shutdown()


def _env(tmp_path, url, **kw):
    (tmp_path / "secret").write_text("s3cret\n")
    (tmp_path / "cf").write_text("cfsecret\n")
    (tmp_path / "tls.yml").write_text("x")
    return {"CERTPULL_URL": url, "CERTPULL_SECRET_FILE": str(tmp_path / "secret"),
            "CERTPULL_CF_ID": "cfid.access", "CERTPULL_CF_SECRET_FILE": str(tmp_path / "cf"),
            "CERTPULL_DIR": str(tmp_path / "certs"), "CERTPULL_TRAEFIK_TLS": str(tmp_path / "tls.yml"), **kw}


def test_pulls_with_the_machine_secret_and_the_access_token(tmp_path, server):
    url, seen, _ = server
    assert certpull.once(_env(tmp_path, url)) == 0
    path, h = seen[0]
    assert path == "/v1/cert"
    assert h["authorization"] == "Bearer s3cret"
    assert h["cf-access-client-id"] == "cfid.access" and h["cf-access-client-secret"] == "cfsecret"


def test_installs_key_0600_and_touches_traefik(tmp_path, server):
    url, _, _ = server
    import os
    env = _env(tmp_path, url)
    os.utime(env["CERTPULL_TRAEFIK_TLS"], (1, 1))
    assert certpull.once(env) == 0
    d = tmp_path / "certs"
    assert (d / "wildcard.crt").read_text() == CERT["chain"]
    assert (d / "wildcard.key").read_text() == CERT["key"]
    assert oct((d / "wildcard.key").stat().st_mode)[-3:] == "600"
    assert os.stat(env["CERTPULL_TRAEFIK_TLS"]).st_mtime > 1
    assert "certFile: /certs/wildcard.crt" in (tmp_path / "tls.yml").read_text()


def test_same_serial_changes_nothing(tmp_path, server):
    url, _, _ = server
    import os
    env = _env(tmp_path, url)
    certpull.once(env)
    os.utime(env["CERTPULL_TRAEFIK_TLS"], (1, 1))
    assert certpull.once(env) == 0
    assert os.stat(env["CERTPULL_TRAEFIK_TLS"]).st_mtime == 1


def test_new_serial_replaces(tmp_path, server):
    url, _, state = server
    env = _env(tmp_path, url)
    certpull.once(env)
    state["body"] = {**CERT, "serial": "b2", "key": CERT["key"].replace("BBB", "CCC")}
    certpull.once(env)
    assert "CCC" in (tmp_path / "certs" / "wildcard.key").read_text()


def test_refusal_keeps_the_old_certificate(tmp_path, server):
    url, _, state = server
    env = _env(tmp_path, url)
    certpull.once(env)
    state["status"], state["body"] = 401, {"detail": "unknown machine"}
    assert certpull.once(env) == 1
    assert (tmp_path / "certs" / "wildcard.crt").read_text() == CERT["chain"]


def test_garbage_is_not_installed(tmp_path, server):
    url, _, state = server
    state["body"] = {**CERT, "chain": "<html>login</html>"}
    with pytest.raises(ValueError):
        certpull.once(_env(tmp_path, url))
    assert not (tmp_path / "certs" / "wildcard.crt").exists()


def test_env_file(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("# c\nCERTPULL_URL='https://x'\nCERTPULL_EVERY_S=60\n")
    env = certpull.load_env(str(f))
    assert env["CERTPULL_URL"] == "https://x" and env["CERTPULL_EVERY_S"] == "60"


def test_certificates_mode_for_a_foreign_traefik(tmp_path, server):
    url, _, _ = server
    env = _env(tmp_path, url, CERTPULL_TRAEFIK_MODE="certificates", CERTPULL_TRAEFIK_CERTS="/etc/traefik/certs/erpnet")
    certpull.once(env)
    t = (tmp_path / "tls.yml").read_text()
    assert "certificates:" in t and "defaultCertificate" not in t
    assert "certFile: /etc/traefik/certs/erpnet/wildcard.crt" in t
