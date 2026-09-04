"""URI схемата на фискалните драйвери — вендорът е серийният префикс.

Семейството се казваше **ISL** — име на ФИРМА („ISL Integrated Systems
Laboratory", София), лепнато от оригиналния ErpNet.FP върху протокола на
Datecs. Преименувано на **ICP** (решение на Росен, 04.09.2026); пълната
бележка, включително цената на решението, е в
`drivers/fiscal/datecs_icp/vendors.py`.

Цената накратко: ICP беше името на протокола на самата ISL, който е
ДРУГА рамка (STX/ETX, без sequence) от нашата (0x01/0x05/0x04/0x03 със
sequence). След преименуването `bg.dt.c.icp` и `bg.is.icp` носят едно
име за две рамки — разделя ги само вендорският код. Затова тестовете тук
пазят вендорските кодове по-строго от преди.
"""

import pytest

from odoo_erpnet_fp.config.loader import (
    _LEGACY_DRIVER_ALIASES,
    _PLANNED_DRIVERS,
    _URI_DRIVER_MAP,
    PrinterConfig,
    _parse_erpnet_uri,
)
from odoo_erpnet_fp.server.routes.printers import _DRIVER_TO_URI
from odoo_erpnet_fp.server.service import SUPPORTED_DRIVERS


def test_uri_tables_are_mutual_inverses():
    """Каквото издаваме, трябва да можем и да го прочетем обратно."""
    for driver, prefix in _DRIVER_TO_URI.items():
        assert _URI_DRIVER_MAP.get(prefix) == driver, (
            f"{driver!r} издава {prefix!r}, а входящата карта го чете като "
            f"{_URI_DRIVER_MAP.get(prefix)!r}"
        )


@pytest.mark.parametrize("prefix,driver", sorted(_URI_DRIVER_MAP.items()))
def test_inbound_map_points_at_a_real_driver(prefix, driver):
    """Мъртва стойност се зарежда тихо и гърми чак при отваряне на ФУ-то."""
    if driver in _PLANNED_DRIVERS:
        pytest.skip(f"{driver} е обявен, но класът още не е в дървото")
    assert driver in SUPPORTED_DRIVERS, (
        f"{prefix!r} сочи към {driver!r}, което го няма в SUPPORTED_DRIVERS"
    )


@pytest.mark.parametrize("uri,expected", [
    ("bg.dt.c.icp.com://COM5", "datecs.icp"),
    ("bg.dt.p.icp.tcp://192.168.1.77:9100", "datecs.icp"),
    ("bg.dt.x.icp.com://COM21", "datecs.icpx"),
    ("bg.dy.icp.com://COM5", "daisy.icp"),
    ("bg.ed.icp.com://COM20", "eltrade.icp"),
    ("bg.in.icp.com://COM3", "incotex.icp"),
])
def test_erpnet_uris_resolve(uri, expected):
    """Формите следват `Config.md`/`PROTOCOL.md` на оригинала, с новото
    име на протокола. Съкратените ключове („bg.dy", „bg.el") никога не
    съвпадаха — търсенето е по ТОЧЕН ключ, не по префикс."""
    driver, _transport, _addr = _parse_erpnet_uri(uri)
    assert driver == expected


# ─── вендорските кодове ──────────────────────────────────────────────

# Префиксът на серийния номер, по драйвера в оригиналния ErpNet.FP.
VENDOR_CODES = {
    "dt": "Datecs", "dy": "Daisy", "ed": "Eltrade",
    "in": "Incotex", "is": "ISL Bulgaria", "zk": "Tremol",
}


@pytest.mark.parametrize("prefix", sorted(_URI_DRIVER_MAP))
def test_vendor_code_is_a_known_serial_prefix(prefix):
    """„el" вместо „ed", „tr" вместо „zk" — така се раждат чуждите URI-та."""
    vendor = prefix.split(".")[1]
    assert vendor in VENDOR_CODES, (
        f"{prefix!r} ползва вендорски код {vendor!r}, който не е префикс на\n"
        f"нечий сериен номер. Известни: {sorted(VENDOR_CODES)}"
    )


def test_isl_bulgaria_prefix_belongs_only_to_isl():
    """`bg.is.*` е на ISL Bulgaria — ДРУГА рамка, не този клас.

    Досега го носеше Incotex. Правилото не е „никой да не ползва
    bg.is.", а „само ISL го ползва".
    """
    for prefix, driver in _URI_DRIVER_MAP.items():
        if prefix.startswith("bg.is."):
            assert driver.startswith("isl."), (
                f"{prefix!r} е закачено за {driver!r} — bg.is. е на ISL Bulgaria"
            )
    for driver, prefix in _DRIVER_TO_URI.items():
        if prefix.startswith("bg.is."):
            assert driver.startswith("isl."), (
                f"{driver!r} взима {prefix!r} — това е URI на ISL Bulgaria"
            )


def test_no_driver_of_this_family_claims_isl_framing():
    """Нашето семейство НЕ е рамката на ISL, колкото и да делят името."""
    for driver in SUPPORTED_DRIVERS:
        assert not driver.startswith("isl."), (
            f"{driver!r} е в SUPPORTED_DRIVERS, а рамката на ISL "
            f"(STX/ETX, без sequence) не е имплементирана"
        )


# ─── псевдонимите на старите имена ───────────────────────────────────


# 🔑 Списъкът е ЗАКОВАН, не изведен от `_LEGACY_DRIVER_ALIASES`.
# Изведен, той не може да падне: изтрит псевдоним просто изчезва и от
# параметризацията, а точно това е дефектът, който трябва да хване —
# конфигурация по обект, която спира да се зарежда след обновяване.
# Тези шест имена са в config.yaml по обектите към 04.09.2026.
LEGACY_NAMES_IN_THE_FIELD = {
    "datecs.isl": "datecs.icp",
    "datecs.islx": "datecs.icpx",
    "daisy.isl": "daisy.icp",
    "eltrade.isl": "eltrade.icp",
    "incotex.isl": "incotex.icp",
    "tremol.isl": "tremol.zfp",
}


@pytest.mark.parametrize(
    "legacy,modern", sorted(LEGACY_NAMES_IN_THE_FIELD.items())
)
def test_legacy_driver_name_still_loads(legacy, modern):
    """Конфигурация по обект със старото име НЕ бива да пада при обновяване."""
    assert legacy in _LEGACY_DRIVER_ALIASES, (
        f"{legacy!r} е по обектите, а псевдонимът му е махнат"
    )
    assert PrinterConfig(id="fp1", driver=legacy).driver == modern


@pytest.mark.parametrize("legacy", sorted(LEGACY_NAMES_IN_THE_FIELD))
def test_legacy_name_is_gone_from_the_live_set(legacy):
    """Псевдонимът е вход, не съществуващо име — иначе не се маха никога."""
    assert legacy not in SUPPORTED_DRIVERS
