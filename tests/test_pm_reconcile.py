"""Сверката: активните ставки (0x32) и дневната таксация (0x41).

Тези две команди носят целия контрол при затваряне на смяна и НИТО
ЕДНАТА не печата и не нулира. Дотук в драйвера ги нямаше: четенето на
ставки удряше 0x53 (командата за ПИСАНЕ, която законно иска нулиран
ден), а 0x41 беше обявена в commands.py без метод.

Отговорите долу следват Datecs Programmer's Manual за
FMP-350X / FMP-55X / FP-700X / WP-500X / WP-50X / DP-25X / DP-150X.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import commands, errors
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.pm_v2_11_4 import PmDevice


class _Resp:
    def __init__(self, data):
        self.data = data
        self.status = b"\x80" * 8


def _device(reply, expect_cmd=None):
    """Апарат-имитация, който връща един готов отговор."""
    d = PmDevice.__new__(PmDevice)
    d._seq = 0x20
    d.sent = []

    def exchange(cmd, data=b"", timeout=None):
        d.sent.append((cmd, data))
        if expect_cmd is not None:
            assert cmd == expect_cmd, f"очаквана 0x{expect_cmd:02X}, дошла 0x{cmd:02X}"
        return _Resp(reply)

    d._exchange = exchange
    return d


# ─── 0x32 — активните ставки ─────────────────────────────────────────

# {ErrorCode}{nZreport}{TaxA}..{TaxH}{EntDate}
_RATES = b"0\t42\t0.00\t20.00\t20.00\t9.00\t100.00\t100.00\t100.00\t100.00\t01-07-25\t"


def test_active_rates_are_read_with_command_50_not_83():
    """0x53 е ПИСАНЕ и отказва с -104000, докато денят не е нулиран."""
    d = _device(_RATES, expect_cmd=commands.CMD_READ_VAT_RATES)
    d.read_active_vat_rates()
    assert d.sent[0][0] == 0x32
    assert d.sent[0][1] == b"", "командата няма параметри"


def test_enabled_slots_come_back_as_rates():
    r = _device(_RATES).read_active_vat_rates()["rates"]
    assert r["A"] == 0.0      # нулева ставка — слотът СЪЩЕСТВУВА
    assert r["B"] == 20.0
    assert r["C"] == 20.0     # втори двайсетпроцентов слот, обичайно за БГ
    assert r["D"] == 9.0


def test_hundred_means_disabled_not_a_rate():
    """100.00 е признак „изключен слот". Прочетено като ставка, то би
    произвело данък 100% в сверката."""
    r = _device(_RATES).read_active_vat_rates()["rates"]
    for letter in ("E", "F", "G", "H"):
        assert r[letter] is None


def test_zero_rate_is_not_confused_with_disabled():
    """Разликата носи смисъл: слот с 0.00 може да натрупва оборот по
    нулева ставка; изключеният не може."""
    r = _device(_RATES).read_active_vat_rates()["rates"]
    assert r["A"] == 0.0 and r["A"] is not None
    assert r["E"] is None


def test_rates_carry_their_validity():
    """`nZreport` и `EntDate` казват ОТКОГА важат — без тях сверка на
    минал период стъпва на днешните ставки."""
    out = _device(_RATES).read_active_vat_rates()
    assert out["z_number"] == 42
    assert out["entry_date"] == "01-07-25"


def test_truncated_answer_does_not_raise():
    out = _device(b"0\t7\t0.00\t20.00\t").read_active_vat_rates()
    assert out["rates"]["B"] == 20.0
    assert out["rates"]["D"] is None
    assert out["z_number"] == 7


# ─── 0x41 — дневната таксация ────────────────────────────────────────

# {ErrorCode}{nRep}{SumA}..{SumH}
_DAILY = b"0\t43\t0.00\t812.40\t0.00\t145.00\t0.00\t0.00\t0.00\t0.00\t"


def test_daily_taxation_asks_for_the_requested_cut():
    d = _device(_DAILY, expect_cmd=commands.CMD_DAILY_TAXATION)
    d.read_daily_taxation(PmDevice.TAX_STORNO_TURNOVER)
    assert d.sent[0][1] == b"2\t"


def test_daily_taxation_defaults_to_turnover():
    d = _device(_DAILY)
    d.read_daily_taxation()
    assert d.sent[0][1] == b"0\t"


def test_daily_sums_land_on_their_letters():
    out = _device(_DAILY).read_daily_taxation()
    assert out["report_number"] == 43
    assert out["sums"]["B"] == 812.40
    assert out["sums"]["D"] == 145.00
    assert out["sums"]["A"] == 0.0


@pytest.mark.parametrize("kind", [-1, 4, 9, "0"])
def test_unknown_cut_is_refused(kind):
    with pytest.raises(ValueError):
        _device(_DAILY).read_daily_taxation(kind)


def test_all_four_cuts_are_read_without_printing():
    """Сверката не бива да минава през Z — той нулира апарата."""
    d = _device(_DAILY)
    out = d.read_daily_totals()
    assert set(out) == {"turnover", "tax", "storno_turnover", "storno_tax"}
    assert [data for _cmd, data in d.sent] == [b"0\t", b"1\t", b"2\t", b"3\t"]
    assert all(cmd == commands.CMD_DAILY_TAXATION for cmd, _ in d.sent)
    assert commands.CMD_REPORTS not in [cmd for cmd, _ in d.sent]


def test_device_error_is_raised_not_swallowed():
    d = _device(b"-104000\t")

    def boom(cmd, data=b"", timeout=None):
        return _Resp(b"-104000\t")

    d._exchange = boom
    with pytest.raises(errors.FiscalError) as exc:
        d.read_daily_taxation()
    assert exc.value.code == -104000


# ─── X/Z отчетът вече връща и сторната ───────────────────────────────

# {ErrorCode}{nRep}{TotA..TotH}{StorA..StorH}
_ZREP = (b"0\t44\t0.00\t812.40\t0.00\t145.00\t0.00\t0.00\t0.00\t0.00"
         b"\t0.00\t12.00\t0.00\t5.00\t0.00\t0.00\t0.00\t0.00\t")


def test_report_returns_storno_totals_too():
    """Дотук се четяха само продажбените тотали — ден с връщания не
    можеше да се сведе, защото едната страна липсваше."""
    d = _device(_ZREP)
    n_rep, sales = d._exchange_report("X")
    assert n_rep == 44
    assert sales["B"] == 812.40
    assert d.last_report_storno["B"] == 12.00
    assert d.last_report_storno["D"] == 5.00


def test_report_type_is_validated():
    with pytest.raises(ValueError):
        _device(_ZREP)._exchange_report("D")


# ─── двете азбуки ────────────────────────────────────────────────────


def test_answers_are_keyed_in_latin_not_cyrillic():
    """Капан, платен веднъж: „A" и „А" изглеждат еднакво в кода.

    Отговорите на апарата носят латински имена (TotA, SumA, TaxA) —
    така са и в ръководството. Смяната им с кирилица счупи заварен
    договор, без нищо да изглежда различно на екрана.
    """
    out = _device(_DAILY).read_daily_taxation()
    assert "B" in out["sums"]           # латинско
    assert "\u0411" not in out["sums"]  # кирилско Б
    assert set(out["sums"]) == set("ABCDEFGH")


def test_bridge_to_the_odoo_side_is_explicit():
    """Кирилицата се иска от `l10n_bg_fiscal_tax_group` в Odoo."""
    assert PmDevice.slot_to_cyrillic("A") == "\u0410"
    assert PmDevice.slot_to_cyrillic("B") == "\u0411"
    assert PmDevice.slot_to_cyrillic("D") == "\u0413"
    assert PmDevice.slot_to_cyrillic("H") == "\u0417"


def test_bridge_refuses_a_cyrillic_input():
    """Подадена кирилица значи, че някой вече е объркал двете азбуки."""
    with pytest.raises(ValueError):
        PmDevice.slot_to_cyrillic("\u0411")
