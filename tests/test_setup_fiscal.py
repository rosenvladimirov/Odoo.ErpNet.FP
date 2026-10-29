"""setup_fiscal: апаратът и CORS в config.yaml от кода за записване."""

from pathlib import Path
from types import SimpleNamespace

import yaml

from odoo_erpnet_fp import setup_fiscal as sf

WIN_EXAMPLE = Path(__file__).resolve().parents[1] / "config-examples" / "config-windows.example.yaml"
EDGE = {"fiscal": {"id": "daisy", "driver": "daisy.icp", "usb": "0483:5740", "op": "1", "pw": "1"},
        "cors": ["https://shop1.odoo.com"]}
PORTS = [SimpleNamespace(device="COM3", vid=0x10C4, pid=0xEA60),     # чуждо (CP210x)
         SimpleNamespace(device="COM7", vid=0x0483, pid=0x5740)]     # Daisy (STM VCP)


def _cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(WIN_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def test_daisy_and_cors_are_written_and_comments_kept(tmp_path):
    p = _cfg(tmp_path)
    out = sf.configure(p, EDGE, PORTS)
    assert "fiscal device daisy (daisy.icp) on COM7" in out
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["printers"] == [{"id": "daisy", "driver": "daisy.icp", "transport": "serial", "port": "COM7",
                                 "baudrate": 115200, "operator": "1", "operator_password": "1", "till_number": 1}]
    assert data["server"]["cors_origins"] == ["https://shop1.odoo.com"]
    assert data["server"]["port"] == 8001                                  # останалото — непокътнато
    assert "Find the COM number in Device Manager" in p.read_text(encoding="utf-8")  # коментарите — също
    assert list(tmp_path.glob("config.yaml.bak-*"))                          # и старият файл


def test_second_run_changes_nothing(tmp_path):
    p = _cfg(tmp_path)
    sf.configure(p, EDGE, PORTS)
    before = p.read_text(encoding="utf-8")
    assert sf.configure(p, EDGE, PORTS)[-1] == "config.yaml unchanged"
    assert p.read_text(encoding="utf-8") == before


def test_device_not_plugged_in_writes_no_printer(tmp_path):
    p = _cfg(tmp_path)
    out = sf.configure(p, EDGE, PORTS[:1])
    assert any("not found" in x for x in out)
    assert yaml.safe_load(p.read_text(encoding="utf-8")).get("printers") is None


def test_existing_cors_list_is_extended_not_replaced(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("server:\n  port: 8001\n  cors_origins:\n    - \"https://old.example\"\nprinters: []\n", encoding="utf-8")
    sf.configure(p, EDGE, PORTS)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["server"]["cors_origins"] == ["https://shop1.odoo.com", "https://old.example"]
    assert data["printers"][0]["port"] == "COM7"
