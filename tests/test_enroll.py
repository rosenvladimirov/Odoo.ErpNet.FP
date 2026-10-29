"""Записването на касата пипа само своето (ADR erpnet-kasi-fleet/0003)."""

import json
from pathlib import Path

import pytest

from odoo_erpnet_fp import enroll

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExampl rosen@laptop"
DATA = {"v": 1, "c": "demo", "k": "kasa9", "h": "kasa9.demo.erpnet.mcpworks.net",
        "t": "00000000-0000-4000-8000-000000000000", "tok": "eyJhIjoi" + "x" * 230,
        "d": {"bluecash": "192.0.2.12"}, "ssh": [KEY]}
FOREIGN_HOSTS = "# Copyright (c) 1993-2009 Microsoft Corp.\r\n127.0.0.1\tlocalhost\r\n10.0.0.5\tchuzhd.local # клиентът\r\n"


def _paths(tmp_path: Path) -> dict:
    return {"state": tmp_path / "enroll.json", "token": tmp_path / "tunnel" / "token",
            "names": tmp_path / "tunnel" / "names",
            "hosts": tmp_path / "hosts", "keys": tmp_path / "ssh" / "administrators_authorized_keys"}


def test_code_roundtrip_and_fits_nsis():
    code = enroll.encode(DATA)
    assert code.startswith("EFP1.") and len(code) <= enroll.MAX_CODE_LEN
    # пренесен на редове при копиране — пак се чете
    assert enroll.decode(code[:50] + "\n  " + code[50:]) == DATA


@pytest.mark.parametrize("bad", ["", "EFP2.abc", "EFP1.!!!", "hello"])
def test_bad_code_is_rejected(bad):
    with pytest.raises(enroll.EnrollError):
        enroll.decode(bad)


def test_apply_touches_only_its_block_and_is_idempotent(tmp_path):
    p = _paths(tmp_path)
    p["hosts"].write_bytes(FOREIGN_HOSTS.encode())
    p["keys"].parent.mkdir()
    p["keys"].write_text("ssh-rsa AAAAB3Nza chuzhd@admin\n")

    st = enroll.apply(enroll.encode(DATA), p)
    assert p["hosts"].read_bytes().decode() == FOREIGN_HOSTS      # hosts не се пипа
    names = p["names"].read_text()
    assert "self kasa9.demo.erpnet.mcpworks.net\n" in names       # касата = свой LAN IP
    assert "192.0.2.12 bluecash.kasa9.demo.erpnet.mcpworks.net\n" in names
    assert p["token"].read_text() == DATA["tok"]
    assert oct(p["token"].stat().st_mode & 0o777) == "0o600"
    keys = p["keys"].read_text()
    assert keys.startswith("ssh-rsa AAAAB3Nza chuzhd@admin\n")
    assert keys.count("erpnet-fp:demo") == 1
    assert st["hosts_changed"] and st["keys_changed"]
    assert "tok" not in p["state"].read_text() and DATA["tok"] not in p["state"].read_text()

    # второ записване: нищо не се дублира
    st2 = enroll.apply(enroll.encode(DATA), p)
    assert not st2["hosts_changed"] and not st2["keys_changed"]
    assert p["names"].read_text().count("kasa9.demo") == 2


def test_reenroll_with_other_devices_replaces_block(tmp_path):
    p = _paths(tmp_path)
    enroll.apply(enroll.encode(DATA), p)
    enroll.apply(enroll.encode({**DATA, "d": {"scale": "192.0.2.50"}}), p)
    names = p["names"].read_text()
    assert "bluecash" not in names and "scale.kasa9" in names


def test_remove_restores_foreign_content_exactly(tmp_path):
    p = _paths(tmp_path)
    p["hosts"].write_bytes(FOREIGN_HOSTS.encode())
    p["keys"].parent.mkdir()
    p["keys"].write_text("ssh-rsa AAAAB3Nza chuzhd@admin\n")
    enroll.apply(enroll.encode(DATA), p)

    done = enroll.remove(p)
    assert p["hosts"].read_bytes().decode() == FOREIGN_HOSTS
    assert p["keys"].read_text() == "ssh-rsa AAAAB3Nza chuzhd@admin\n"
    assert not p["token"].exists() and not p["state"].exists() and not p["names"].exists()
    assert len(done) == 4
    assert enroll.remove(p) == []                       # второ махане — нищо


def test_bad_device_and_key_are_rejected():
    with pytest.raises(enroll.EnrollError):
        enroll.host_entries({**DATA, "d": {"bad name": "1.2.3.4"}})
    with pytest.raises(enroll.EnrollError):
        enroll.host_entries({**DATA, "d": {"x": "not-an-ip"}})
    with pytest.raises(enroll.EnrollError):
        enroll._key_line("hello world", "x")


def test_legacy_hosts_block_is_cleaned_on_apply(tmp_path):
    """Блокът от по-ранна версия в hosts се маха, чуждото остава."""
    p = _paths(tmp_path)
    p["hosts"].write_bytes(FOREIGN_HOSTS.encode())
    enroll.set_hosts_block(p["hosts"], [("old.example", "127.0.0.1")])
    assert "BEGIN erpnet-fp" in p["hosts"].read_text()
    enroll.apply(enroll.encode(DATA), p)
    assert p["hosts"].read_bytes().decode() == FOREIGN_HOSTS


def test_edge_block_in_hosts_survives_reenroll(tmp_path):
    """„# BEGIN erpnet-fp-edge“ (Traefik пред касата) не е стар блок на enroll — остава."""
    p = _paths(tmp_path)
    edge = ("# BEGIN erpnet-fp-edge (managed by Odoo.ErpNet.FP - do not edit inside)\n"
            "127.0.0.1 kasa.demo.erpnet.mcpworks.net\n# END erpnet-fp-edge\n")
    p["hosts"].write_bytes((FOREIGN_HOSTS + edge).encode())
    enroll.apply(enroll.encode(DATA), p)
    assert edge in p["hosts"].read_bytes().decode()


def test_code_can_carry_the_certificate_secret_for_the_edge(tmp_path):
    p = _paths(tmp_path)
    p["edge"] = tmp_path / "edge"
    data = {**DATA, "crt": "s3cret-for-cert-issuer", "cu": "https://certs.example",
            "e": {"n": "kasa.demo.erpnet.mcpworks.net", "p": ""}}
    st = enroll.apply(enroll.encode(data), p)
    assert (p["edge"] / "certpull.secret").read_text() == "s3cret-for-cert-issuer"
    assert oct((p["edge"] / "certpull.secret").stat().st_mode & 0o777) == "0o600"
    edge = json.loads((p["edge"] / "edge.json").read_text())
    assert edge == {"name": "kasa.demo.erpnet.mcpworks.net", "pinpad": "", "cert_url": "https://certs.example"}
    assert "s3cret" not in (p["edge"] / "edge.json").read_text() and "s3cret" not in p["state"].read_text()
    assert st["edge"]["name"] == "kasa.demo.erpnet.mcpworks.net"
