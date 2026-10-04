"""
Daisy ICP — разликите с Datecs, хванати на жив eXpert SX-01
(ФП 36774375, сериен DY585202, 04.10.2026).

Байтовете и отговорите в тестовете са реалните от апарата.
"""

from odoo_erpnet_fp.drivers.fiscal.datecs_icp import DaisyIcpDevice, DatecsIcpDevice
from odoo_erpnet_fp.drivers.fiscal.datecs_icp import commands as cmd
from odoo_erpnet_fp.drivers.fiscal.datecs_icp.protocol import IcpDeviceInfo

OK_STATUS = bytes.fromhex("88 80 c0 80 80 b8")


class _Recorder:
    """Подменя _icp_request: пази командите и връща зададен отговор."""

    def __init__(self, device, text="", status_bytes=OK_STATUS):
        self.calls = []
        self.text = text
        self.status_bytes = status_bytes
        device._icp_request = self

    def __call__(self, command, data="", timeout=5.0):
        self.calls.append((command, data))
        return self.text, self.device._parse_status(self.status_bytes), self.status_bytes


def _make(cls):
    device = cls.__new__(cls)
    device.operator_id = "1"
    device.operator_password = "1"
    device.info = IcpDeviceInfo()
    return device


def _recorder(device, **kw):
    rec = _Recorder(device, **kw)
    rec.device = device
    return rec


def test_open_receipt_has_three_fields():
    # 4-то поле (касата) дава грешка 21 на Daisy и оставя отворен празен бон
    device = _make(DaisyIcpDevice)
    rec = _recorder(device)
    device.open_receipt("DY585202-0001-0005001")
    assert rec.calls == [(cmd.CMD_OPEN_FISCAL_RECEIPT, "1,1,DY585202-0001-0005001")]


def test_datecs_open_receipt_unchanged():
    device = _make(DatecsIcpDevice)
    rec = _recorder(device)
    device.open_receipt("DT737851-0001-0000001")
    assert rec.calls[0][1].count(",") == 3


def test_abort_uses_daisy_command():
    device = _make(DaisyIcpDevice)
    rec = _recorder(device)
    device.abort_receipt()
    assert rec.calls == [(0x82, "")]


def test_status_byte3_is_error_code():
    # отговорът на отварянето с 4 полета: байт 3 = 0x95 → код 21
    status = _make(DaisyIcpDevice)._parse_status(bytes.fromhex("a8 80 88 95 80 b8"))
    assert ("E999", "Daisy error code 21 (see the Daisy manual)") in [(e.code, e.text) for e in status.errors]


def test_status_ok_without_error_code():
    status = _make(DaisyIcpDevice)._parse_status(OK_STATUS)
    assert status.ok


def test_status_no_paper_kept():
    status = _make(DaisyIcpDevice)._parse_status(bytes.fromhex("a8 80 c1 80 80 b8"))
    assert "E301" in [e.code for e in status.errors]


def test_detect_daisy_info():
    device = _make(DaisyIcpDevice)
    _recorder(device, text="eXpert ONL01AZ_EUR-2.0BG 15-07-2019 11:26,1843,00,6,DY585202,36774375")
    info = device.detect()
    assert info.manufacturer == "Daisy"
    assert info.model == "eXpert"
    assert info.firmware_version == "ONL01AZ_EUR-2.0BG 15-07-2019 11:26"
    assert info.serial_number == "DY585202"
    assert info.fiscal_memory_serial_number == "36774375"
    assert info.protocol == "daisy.icp"


def test_daily_report_waits_longer_on_daisy():
    # PerfectS печата Z отчета ~120 с; 120 с лимит гърми на ръба
    calls = []
    device = _make(DaisyIcpDevice)
    device._icp_request = lambda command, data="", timeout=5.0: calls.append(timeout) or ("", device._parse_status(OK_STATUS), OK_STATUS)
    device.print_z_report()
    device.print_x_report()
    assert calls == [300.0, 300.0]
    assert DatecsIcpDevice.REPORT_TIMEOUT == 120.0
