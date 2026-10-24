"""Печат на етикети (`/labels`) — glabels, lp и lpstat са подменени."""

import base64
import gzip
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, LabelsConfig, ServerConfig
from odoo_erpnet_fp.server import label_printing as lpmod
from odoo_erpnet_fp.server.main import create_app

TEMPLATE = """<?xml version="1.0"?>
<Glabels-document version="4.0">
  <Objects id="0" rotate="false">
    <Object-text x="0pt" y="0pt" w="100pt" h="20pt"><p>${product}</p></Object-text>
  </Objects>
  <Merge type="None" src=""/>
  <Data/>
</Glabels-document>
"""

GLABELS = "/opt/test/glabels-batch-qt"
LP = "/opt/test/lp"
LPSTAT = "/opt/test/lpstat"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeOS:
    """Подмяната на `subprocess.run`: записва повикванията и какво е видял."""

    def __init__(self):
        self.calls = []
        self.printers = ["Zebra_ZD421", "Office Laser"]
        self.default = "Zebra_ZD421"
        self.glabels_rc = 0
        self.glabels_writes_pdf = True
        self.glabels_raises = None
        self.lp_rc = 0
        self.seen_csv = None
        self.seen_template = None
        self.job_dir = None
        self.lp_pdf_bytes = None

    def __call__(self, args, **kw):
        assert kw.get("shell") in (None, False)
        assert kw.get("timeout")
        self.calls.append(list(args))
        exe = args[0]
        if exe == LPSTAT:
            if args[1] == "-e":
                return subprocess.CompletedProcess(
                    args, 0, "\n".join(self.printers) + "\n", "")
            return subprocess.CompletedProcess(
                args, 0, "system default destination: %s\n" % self.default, "")
        if exe == GLABELS:
            if self.glabels_raises:
                raise self.glabels_raises
            if args[1] == "--version":
                return subprocess.CompletedProcess(
                    args, 0, "glabels-batch-qt 3.99-test (abc)\n", "")
            i = args[args.index("-i") + 1]
            o = args[args.index("-o") + 1]
            self.job_dir = Path(i).parent
            self.seen_csv = Path(i).read_bytes()
            self.seen_template = Path(args[-1]).read_text(encoding="utf-8")
            if self.glabels_writes_pdf and self.glabels_rc == 0:
                Path(o).write_bytes(
                    b"%PDF-1.4\n1 0 obj << /Type /Pages >>\n"
                    b"2 0 obj << /Type /Page >>\n3 0 obj << /Type /Page >>\n")
            return subprocess.CompletedProcess(
                args, self.glabels_rc, "",
                "Batch mode.\nPrinting 2 items on 2 pages.\n"
                if self.glabels_rc == 0 else "boom: bad template\n")
        if exe == LP:
            self.lp_pdf_bytes = Path(args[-1]).read_bytes()
            if self.lp_rc:
                return subprocess.CompletedProcess(
                    args, self.lp_rc, "", "lp: The printer is not accepting jobs.")
            return subprocess.CompletedProcess(
                args, 0, "request id is %s-42 (1 file(s))\n" % args[2], "")
        raise FileNotFoundError(exe)


@pytest.fixture
def fake(monkeypatch):
    f = FakeOS()
    monkeypatch.setattr(lpmod.subprocess, "run", f)
    monkeypatch.setattr(lpmod, "_is_windows", lambda: False)
    return f


@pytest.fixture
def work_dir(tmp_path):
    return tmp_path / "labels-work"


@pytest.fixture
def client(fake, work_dir):
    cfg = AppConfig(server=ServerConfig(), labels=LabelsConfig(
        glabels=GLABELS, lp=LP, lpstat=LPSTAT, print_via="lp",
        work_dir=str(work_dir), timeout_seconds=7, cache_seconds=0))
    return TestClient(create_app(cfg))


def _print(client, **over):
    body = {"printer": "Zebra_ZD421", "template": _b64(TEMPLATE),
            "records": [{"product": "Фолио ПЕ", "weight_kg": 12.5},
                        {"product": 'Торба "30л", бяла', "weight_kg": 7.25}],
            "copies": 1}
    body.update(over)
    return client.post("/labels/print", json=body)


# ─── /labels/printers ────────────────────────────────────────────────


def test_printers_lists_the_os_printers_and_glabels(client):
    r = client.get("/labels/printers")
    assert r.status_code == 200
    assert r.json() == {
        "printers": [{"name": "Zebra_ZD421", "default": True},
                     {"name": "Office Laser", "default": False}],
        "glabels": "3.99-test (abc)",
    }


def test_printers_glabels_null_when_not_installed(client, fake):
    fake.glabels_raises = FileNotFoundError(GLABELS)
    assert client.get("/labels/printers").json()["glabels"] is None


def test_lpstat_runs_in_the_c_locale(monkeypatch):
    seen = {}

    def run(args, **kw):
        seen.setdefault("env", kw["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lpmod.subprocess, "run", run)
    monkeypatch.setattr(lpmod, "_is_windows", lambda: False)
    lpmod.LabelPrinter(LabelsConfig(lpstat=LPSTAT)).printers()
    assert seen["env"]["LC_ALL"] == "C"


def test_windows_powershell_fallback(monkeypatch):
    def run(args, **kw):
        assert args[0] == "ps.exe" and "Get-CimInstance Win32_Printer" in args[-1]
        return subprocess.CompletedProcess(
            args, 0, '[{"Name":"ZDesigner ZD421","Default":true},'
                     '{"Name":"Microsoft Print to PDF","Default":false}]', "")

    monkeypatch.setattr(lpmod.subprocess, "run", run)
    monkeypatch.setattr(lpmod, "_is_windows", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "win32print", None)
    got = lpmod.LabelPrinter(LabelsConfig(powershell="ps.exe")).printers()
    assert got == [{"name": "ZDesigner ZD421", "default": True},
                   {"name": "Microsoft Print to PDF", "default": False}]


def test_healthz_lists_label_printers_and_keeps_the_old_keys(client):
    body = client.get("/healthz").json()
    assert body["labels"] == ["Zebra_ZD421", "Office Laser"]
    for key in ("ok", "version", "printers", "scales", "readers",
                "europlacer", "shifts", "features"):
        assert key in body


# ─── /labels/print ───────────────────────────────────────────────────


def test_print_csv_is_utf8_with_the_fields_first(client, fake):
    r = _print(client)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "job_id": "Zebra_ZD421-42", "pages": 2}
    csv = fake.seen_csv
    assert not csv.startswith(b"\xef\xbb\xbf")  # без BOM
    assert b"\r" not in csv
    text = csv.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == '"product","weight_kg"'
    assert lines[1] == '"Фолио ПЕ","12.5"'
    assert lines[2] == '"Торба ""30л"", бяла","7.25"'


def test_print_merge_points_at_the_csv(client, fake):
    _print(client)
    assert '<Merge type="Text/Comma/Line1Keys" src="records.csv"/>' \
        in fake.seen_template
    assert 'type="None"' not in fake.seen_template


def test_print_template_without_merge_gets_one():
    out = lpmod.prepare_template(
        '<Glabels-document version="4.0"><Data/></Glabels-document>')
    assert out.count("<Merge ") == 1
    assert out.index("<Merge ") < out.index("</Glabels-document>")


def test_print_pdf_reaches_lp_with_the_right_printer(client, fake):
    _print(client, printer="Office Laser", copies=3)
    gl = next(c for c in fake.calls if c[0] == GLABELS and "-o" in c)
    assert gl[gl.index("-c") + 1] == "3"
    lp = next(c for c in fake.calls if c[0] == LP)
    assert lp[1:3] == ["-d", "Office Laser"]
    assert lp[-1].endswith("labels.pdf")
    assert fake.lp_pdf_bytes.startswith(b"%PDF")


def test_print_glabels_error_is_ok_false(client, fake):
    fake.glabels_rc = 1
    r = _print(client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "boom: bad template" in body["error"]
    assert not any(c[0] == LP for c in fake.calls)


def test_print_glabels_exit_zero_without_pdf_is_ok_false(client, fake):
    # glabels-batch-qt връща 0 и при нечетим шаблон — мерим PDF-а.
    fake.glabels_writes_pdf = False
    body = _print(client).json()
    assert body["ok"] is False and "no PDF" in body["error"]


def test_print_glabels_not_installed_is_ok_false(client, fake):
    fake.glabels_raises = FileNotFoundError(GLABELS)
    body = _print(client).json()
    assert body["ok"] is False and "not installed" in body["error"]


def test_print_lp_error_is_ok_false(client, fake):
    fake.lp_rc = 1
    r = _print(client)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "not accepting jobs" in r.json()["error"]


def test_print_timeout(client, fake):
    fake.glabels_raises = subprocess.TimeoutExpired(GLABELS, 7)
    body = _print(client).json()
    assert body == {"ok": False, "error": "glabels-qt timed out after 7 s"}


def test_print_passes_the_configured_timeout(client, fake, monkeypatch):
    seen = []
    orig = lpmod.subprocess.run

    def run(args, **kw):
        seen.append((args[0], kw["timeout"]))
        return orig(args, **kw)

    monkeypatch.setattr(lpmod.subprocess, "run", run)
    _print(client)
    assert (GLABELS, 7) in seen and (LP, 7) in seen


def test_unknown_printer_is_4xx(client, fake):
    r = _print(client, printer="Nope")
    assert r.status_code == 404
    assert not any(c[0] == GLABELS and "-o" in c for c in fake.calls)


@pytest.mark.parametrize("over, code", [
    ({"template": ""}, 400),
    ({"template": "@@not base64@@"}, 400),
    ({"template": _b64("<html/>")}, 400),
    ({"records": []}, 400),
    ({"records": [{"product": {"nested": 1}}]}, 400),
    ({"copies": 0}, 400),
    ({"printer": ""}, 400),
])
def test_invalid_requests_are_4xx(client, over, code):
    assert _print(client, **over).status_code == code


def test_missing_fields_are_422(client):
    assert client.post("/labels/print", json={"printer": "x"}).status_code == 422


def test_gzip_template_is_accepted(client, fake):
    gz = base64.b64encode(gzip.compress(TEMPLATE.encode("utf-8"))).decode()
    assert _print(client, template=gz).json()["ok"] is True
    assert "${product}" in fake.seen_template


@pytest.mark.parametrize("break_it", [None, "glabels", "lp"])
def test_temporary_files_are_removed(client, fake, work_dir, break_it):
    if break_it == "glabels":
        fake.glabels_rc = 1
    elif break_it == "lp":
        fake.lp_rc = 1
    _print(client)
    assert fake.job_dir is not None and fake.job_dir.parent == work_dir
    assert not fake.job_dir.exists()
    assert list(work_dir.iterdir()) == []


def test_disabled_labels_is_404(fake, work_dir):
    cfg = AppConfig(labels=LabelsConfig(enabled=False, lpstat=LPSTAT))
    c = TestClient(create_app(cfg))
    assert c.get("/labels/printers").status_code == 404
    assert c.get("/healthz").json()["labels"] == []


def test_csv_escapes_backslash_newline_and_none():
    text = lpmod.records_to_csv([{"a": "x\\y", "b": "1\n2"}, {"a": None, "c": True}])
    assert text.splitlines() == [
        '"a","b","c"',
        '"x\\\\y","1\\n2",""',
        '"","","true"',
    ]


def test_windows_prints_through_glabels(monkeypatch, tmp_path):
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_bytes(b"%PDF /Type /Page ")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lpmod.subprocess, "run", run)
    monkeypatch.setattr(lpmod, "_is_windows", lambda: True)
    lp = lpmod.LabelPrinter(LabelsConfig(glabels="gl.exe",
                                         work_dir=str(tmp_path)))
    monkeypatch.setattr(lp, "printer_names", lambda: ["ZDesigner"])
    out = lp.print_labels("ZDesigner", _b64(TEMPLATE), [{"product": "А"}], 2)
    assert out["ok"] is True and out["pages"] == 1
    direct = calls[-1]
    assert direct[:3] == ["gl.exe", "-p", "ZDesigner"]
    assert direct[direct.index("-c") + 1] == "2"
