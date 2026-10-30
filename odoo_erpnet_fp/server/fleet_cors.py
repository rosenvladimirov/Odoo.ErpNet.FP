"""CORS от флота, приложен в процеса (ADR erpnet-kasi-fleet/0006).

Флотът в Odoo връща `cors_origins` с всеки heartbeat. Зад Traefik (Docker)
списъкът отива в `dynamic.yml` (`cors_writer`); на Windows CORS го прави
самото прокси, затова тук списъкът се държи в паметта и middleware-ът го
пита при всяка заявка — без рестарт.

- статичният списък/регулярен израз от config.yaml важи винаги; флотът
  само добавя;
- новият списък от флота ЗАМЕНЯ стария; празен = „няма какво да каже“ —
  последният остава;
- пази се на диска (`fleet_cors.json` до config.yaml), за да оцелее
  рестарт без връзка към Odoo;
- приема само `http(s)://хост[:порт]` — списъкът решава кой браузър
  стига до апарата.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from starlette.middleware.cors import CORSMiddleware

_logger = logging.getLogger(__name__)

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")

_origins: frozenset[str] = frozenset()
_path: Path | None = None


def _clean(origins) -> list[str]:
    out = []
    for raw in origins or []:
        s = str(raw).strip().rstrip("/")
        if _ORIGIN_RE.match(s):
            out.append(s)
        elif s:
            _logger.warning("Fleet CORS: отхвърлен адрес %r", s)
    return sorted(set(out))


def current() -> frozenset[str]:
    return _origins


def is_allowed(origin: str) -> bool:
    return origin in _origins


def load(path: Path) -> None:
    """Зарежда пазения списък; липсващ или счупен файл = празен списък."""
    global _origins, _path
    _path = path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _origins = frozenset(_clean(data.get("origins")))
    except FileNotFoundError:
        _origins = frozenset()
    except (OSError, ValueError, AttributeError) as exc:
        _logger.warning("Fleet CORS: не чета %s: %s", path, exc)
        _origins = frozenset()
    if _origins:
        _logger.info("Fleet CORS: %d адрес(а) от %s", len(_origins), path)


def apply(origins) -> bool:
    """Списъкът от heartbeat-а. True, ако нещо се е сменило."""
    global _origins
    if not origins:
        return False
    new = frozenset(_clean(origins))
    if not new or new == _origins:
        return False
    _origins = new
    _logger.info("Fleet CORS: %d адрес(а) от флота", len(new))
    if _path is not None:
        try:
            _path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=_path.parent, prefix=".fleet_cors-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"origins": sorted(new)}, f, indent=1)
            os.replace(tmp, _path)
        except OSError as exc:
            _logger.warning("Fleet CORS: не пиша %s: %s", _path, exc)
    return True


class FleetCORSMiddleware(CORSMiddleware):
    """CORSMiddleware, който пита и списъка от флота.

    Starlette проверява адреса на страницата само през `is_allowed_origin`
    — и при preflight, и при самата заявка — затова е достатъчно тук.
    """

    def is_allowed_origin(self, origin: str) -> bool:
        return super().is_allowed_origin(origin) or is_allowed(origin)
