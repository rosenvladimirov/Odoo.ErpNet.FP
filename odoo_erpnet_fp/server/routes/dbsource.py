"""
Запитване към външна база по заявка — за етикета на бъндъла.

Не част от ErpNet.FP. Префикс `/dbsource`. Само JSON.

Потокът на dbsource драйвера е ИЗДЪРПВАЩ: поллерът чете неизядените
редове и ги бута към Odoo. Тук е другото — Odoo пита за КОНКРЕТНО
устройство в мига, в който операторът поиска кита, и получава `LabelData`:
вложения JSON, в който пише кои устройства влизат в него.

Разделено е нарочно. Етикетът е голям и е нужен рядко; да пътува с всеки
ред в потока значи да се тегли на едро нещо, което се ползва на дребно.

Endpoints:
  GET  /dbsource                      — конфигурирани източници
  GET  /dbsource/{name}/device/{sn}   — редът за този сериен номер + етикет
  POST /dbsource/{name}/mark_stored   — вдига флага за произведените редове

🚨 Четенето е свободно, писането — не. `mark_stored` пипа ЧУЖДА база и
затова иска админския токен, докато търсенето по сериен номер само чете.
Съответно Odoo не го вика направо: слага команда на опашката на проксито
(`erpnet.fp.proxy.command`, kind `mark_stored`), защото проксито е зад NAT,
а токенът му и без това е у него.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from .admin import _check_token

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dbsource", tags=["dbsource"])


def _registry(request: Request):
    return getattr(request.app.state, "dbsource_registry", None)


@router.get("")
@router.get("/", include_in_schema=False)
async def list_sources(request: Request):
    reg = _registry(request)
    if reg is None:
        return {"sources": []}
    return reg.snapshot()


@router.get("/{name}/device/{serial}")
async def device_by_serial(name: str, serial: str, request: Request):
    """Редът за този сериен номер, заедно с етикета.

    Търси се и по съдържанието на етикета, не само по серийния на реда:
    сканираното може да е на вложен участник, а китът не се намира по
    серийния на своя елемент.
    """
    reg = _registry(request)
    if reg is None or name not in reg.commands:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Source {name!r} is not configured",
        )
    result = await reg.commands[name].lookup_device(serial)
    if not result.get("ok"):
        # Провалът се връща като отговор, не като 500: Odoo трябва да
        # различава „няма такова устройство" от „базата е паднала", а
        # едно и също 500 не му го казва.
        return {"ok": False, "source": name, "serial": serial,
                "error": result.get("error", "")}
    return dict(result, source=name)


@router.post("/{name}/mark_stored")
async def mark_stored(
    name: str,
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """Вдига `StoredInOdoo` за подадените редове.

    Другият край на уговорката: източникът гаси флага СЛЕД реалното
    производство в Odoo, не при четенето. Дотогава той праща същите редове
    наново — безвредно за Odoo (`row_key` е уникален), но опашката му не се
    източва.

    Тяло: `{"row_keys": ["4711", "4712"]}`. Отговор: `CommandResult` —
    `{"ok": true, "affected": 2, "error": ""}`, плюс `source` и колко са
    поискани.

    Провалът на самата заявка се връща с `ok: false` и HTTP 200, както при
    търсенето горе: „базата отказа" и „маршрутът го няма" трябва да са
    различими отсреща, а едно 500 ги слива. Извикващият в Odoo проверява и
    двете нива.
    """
    _check_token(x_admin_token)
    reg = _registry(request)
    if reg is None or name not in reg.commands:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Source {name!r} is not configured",
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    keys = body.get("row_keys") if isinstance(body, dict) else None
    if keys is None:
        keys = []
    if not isinstance(keys, list):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "row_keys must be a list",
        )

    result = await reg.commands[name].mark_stored(keys)
    _logger.info("dbsource[%s]: mark_stored за %d ключ(а) → ok=%s affected=%s",
                 name, len(keys), result.ok, result.affected)
    return dict(result.as_dict(), source=name, requested=len(keys))
