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
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

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
