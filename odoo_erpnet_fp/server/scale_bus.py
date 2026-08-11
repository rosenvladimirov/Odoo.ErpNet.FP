# Copyright 2026 Rosen Vladimirov
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Фонов четец на везни — липсващият спусък за живото тегло.

## Защо съществува

`/scales/{id}/weight` праща `scale.weighed` към шината на Odoo, но само
когато го попитат. Картата в Shop Floor обаче е ПАСИВЕН слушател: чака
`SCALE_READ` и не пита никого. Резултатът беше, че екранът никога не
показва тегло — не защото данните се губят, а защото не се раждат.

📊 Измерено на MEC (06.08.2026): нула `scale.weighed` на шината за цялото
съществуване на базата, при 1 929 други събития за десет минути. Три
ръчни извиквания на `/weight` веднага дадоха четири събития — тоест
веригата прокси → bus_inject → шина е здрава и липсва само питащият.

## Как

Задача на всяка везна с `poll_ms > 0` в `extras`. По подразбиране **0 =
изключено**, тъй че нито една заварена инсталация не сменя поведението си.

🚨 Четенето минава през `registry.with_scale()`, а НЕ през собствен
сокет. Ethernet китът на OHAUS пуска **само един** TCP клиент: ако
четецът държеше връзката отворена, `/weight` щеше да получава откази, и
обратно. `with_scale` държи asyncio lock и отваря/затваря на всяко
ползване, тъй че четецът и REST маршрутът се редуват вместо да се бият.

🔑 Излъчва се при ПРОМЯНА, не на всяко четене. Везна на 400 мс би
добавяла 2.5 събития в секунда завинаги — на MEC това е повече от целия
останал трафик на шината. Затова:

- промяна над `poll_epsilon_kg` (по подразбиране 0.5 г) → събитие
- смяна на стабилността или на `ok` → събитие
- иначе тишина, но не повече от `poll_heartbeat_s` (по подразбиране 30 с),
  за да не остане картата с показание, което вече не е вярно
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

_logger = logging.getLogger(__name__)

# Изключено по подразбиране — заварените инсталации не се пипат.
DEFAULT_POLL_MS = 0
# 0.5 грама: под тази разлика е шум от везната, не движение на товара.
DEFAULT_EPSILON_KG = 0.0005
# Пресвежаване, дори нищо да не се е променило.
DEFAULT_HEARTBEAT_S = 30.0
# Долна граница на интервала. Под 100 мс китът не смогва и всяко четене
# започва да чака предишното на локa.
MIN_POLL_MS = 100.0
# Пауза след провалено четене: недостъпна везна не бива да върти цикъла
# на пълни обороти.
ERROR_BACKOFF_S = 5.0


def _num(extras: dict[str, Any], key: str, default: float) -> float:
    """Число от `extras`, търпеливо към низове и към глупости."""
    try:
        val = float(extras.get(key, default))
    except (TypeError, ValueError):
        return default
    return val


def _changed(prev: Optional[dict], data: dict, epsilon: float) -> bool:
    """Заслужава ли това четене събитие?"""
    if prev is None:
        return True
    if bool(prev.get("ok")) != bool(data.get("ok")):
        return True
    if bool(prev.get("stable")) != bool(data.get("stable")):
        return True
    if prev.get("mode") != data.get("mode"):
        return True
    # Броячният режим е цяло число — там всяка разлика е промяна.
    if data.get("mode") == "count":
        return prev.get("count") != data.get("count")
    a, b = prev.get("weight"), data.get("weight")
    if a is None or b is None:
        return a is not b
    try:
        return abs(float(a) - float(b)) > epsilon
    except (TypeError, ValueError):
        return True


async def _poll_one(app, scale_id: str, cfg, interval_s: float,
                    epsilon: float, heartbeat_s: float) -> None:
    """Цикълът на една везна. Не свършва сам и не вдига навън."""
    from .routes.scales import _weight_event_data, emit_weight_for_app

    loop = asyncio.get_running_loop()
    prev: Optional[dict] = None
    last_emit = 0.0
    _logger.info(
        "scale %s: polling every %.0f ms (epsilon=%.4f kg, heartbeat=%.0f s)",
        scale_id, interval_s * 1000.0, epsilon, heartbeat_s)

    while True:
        try:
            reg = getattr(app.state, "scale_registry", None)
            if reg is None or not reg.has(scale_id):
                # Конфигурацията е презаредена и везната я няма вече.
                _logger.info("scale %s: no longer configured — poll stops",
                             scale_id)
                return
            async with reg.with_scale(scale_id) as sc:
                reading = await asyncio.to_thread(sc.read_weight)
            data = _weight_event_data(scale_id, cfg, reading)
            now = loop.time()
            if _changed(prev, data, epsilon) or (now - last_emit) >= heartbeat_s:
                # 🚨 Синхронен httpx в отделна нишка: извикан направо тук,
                # той би блокирал event loop-а на ЦЯЛОТО прокси — с него
                # достъпния контрол и CFX ingest-а — докато заявката към
                # Odoo минава през Cloudflare. Същата причина, поради
                # която маршрутът ползва `_schedule_emit`.
                await asyncio.to_thread(
                    emit_weight_for_app, app, scale_id, cfg, data)
                prev = data
                last_emit = now
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            _logger.info("scale %s: poll cancelled", scale_id)
            raise
        except Exception:  # noqa: BLE001
            # Недостъпна везна е нормално състояние на цех: кабел, изключен
            # уред, зает кит. Логваме и продължаваме — цикълът е това, което
            # ще я хване, когато се върне.
            _logger.warning("scale %s: poll iteration failed", scale_id,
                            exc_info=True)
            try:
                await asyncio.sleep(ERROR_BACKOFF_S)
            except asyncio.CancelledError:
                raise


async def scale_poll_loop(app) -> None:
    """Вдигни по една задача за всяка везна с включено четене.

    Сама по себе си задачата само чака — работата е в подзадачите. Така
    отмяната ѝ при спиране на сървъра слиза до всички.
    """
    reg = getattr(app.state, "scale_registry", None)
    if reg is None:
        return
    tasks: list[asyncio.Task] = []
    for scale_id, entry in sorted(reg.scales.items()):
        cfg = entry.config
        extras = getattr(cfg, "extras", None) or {}
        poll_ms = _num(extras, "poll_ms", DEFAULT_POLL_MS)
        if poll_ms <= 0:
            continue
        if poll_ms < MIN_POLL_MS:
            _logger.warning(
                "scale %s: poll_ms=%.0f is below the %.0f ms floor — using "
                "the floor", scale_id, poll_ms, MIN_POLL_MS)
            poll_ms = MIN_POLL_MS
        tasks.append(asyncio.create_task(
            _poll_one(
                app, scale_id, cfg,
                interval_s=poll_ms / 1000.0,
                epsilon=_num(extras, "poll_epsilon_kg", DEFAULT_EPSILON_KG),
                heartbeat_s=_num(extras, "poll_heartbeat_s",
                                 DEFAULT_HEARTBEAT_S),
            ),
            name=f"scale-poll-{scale_id}",
        ))
    if not tasks:
        _logger.info(
            "scale polling: no scale has poll_ms set — live weight on the "
            "Shop Floor card stays silent by design")
        return
    _logger.info("scale polling: %d scale(s) being polled", len(tasks))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        raise
