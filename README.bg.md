# Odoo.ErpNet.FP — документация на български

> Python drop-in заместител на C# [ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP)
> HTTP fiscal-printer сървър, разширен в централен POS-side device
> hub за българския retail и SMB пазар.
>
> **Версия `0.13.6`** · Production · Docker
> `vladimirovrosen/odoo-erpnet-fp:0.13.6` (също `:latest`).

---

## Съдържание

1. [Какво е и защо](#какво-е-и-защо)
2. [Поддържан хардуер](#поддържан-хардуер)
3. [Архитектура](#архитектура)
4. [Бърз старт](#бърз-старт)
5. [Конфигурация](#конфигурация)
6. [Списък на endpoints](#списък-на-endpoints)
7. [Модел на сигурността](#модел-на-сигурността)
8. [BlueCash PLU интеграция](#bluecash-plu-интеграция)
9. [Операции](#операции)
10. [Разработка](#разработка)
11. [Лиценз + НДА](#лиценз--нда)

---

## Какво е и защо

Българският пазар на фискални устройства е фрагментиран — всеки голям
вендор (Datecs, Daisy, Tremol, Eltrade, Incotex) използва свой
протоколен диалект, своя утилита и своя Windows-only драйвер.
Оригиналният **[ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP)**
проект на C# даде на пазара унифициран HTTP front-end за повечето
устройства от ISL семейството — но PM-семейството (Datecs FP-700MX,
BC-50MX) така и не беше покрито там, периферията извън фискалното не
влизаше в обхвата, а Linux deployment-ът беше тромав.

**Odoo.ErpNet.FP** е Python re-implementation, който:

- запазва оригиналния HTTP протокол байт-към-байт (съществуващи
  `l10n_bg_erp_net_fp` Odoo addon клиенти продължават да работят);
- добавя **Datecs PM v2.11.4** драйвер (FP-700MX, BC-50MX) с пълно
  PLU програмиране, multi-operator support, програмируеми ДДС
  ставки, X/Z отчети и емпирично проверената 6-slot таблица за
  плащания;
- поддържа **цялата** POS-странична периферия (клиентски дисплеи,
  везни, pinpad-и, баркод четци, камери, контрол на достъп, MQTT)
  в един FastAPI процес;
- върви нативно на Linux (Docker препоръчителен), Windows и Odoo
  IoT Box версиите (Debian + Linux Mint);
- предлага **сигурен мост** към отдалечена Odoo инстанция чрез
  HMAC-SHA256-подписани тела, повтаряйки шаблона от Fleet registry
  протокола;
- свързва новия **BlueCash PLU Android клиент** (`/devices/<serial>/
  shift_close`, `/devices/<serial>/events/*`) с lifecycle-а на
  `pos.session` в Odoo.

## Поддържан хардуер

### Фискални принтери — production-verified

| Семейство | Устройства | Driver | Бележки |
|---|---|---|---|
| **Datecs PM** | FP-700MX, BC-50MX | `datecs_pm/pm_v2_11_4.py` | PLU-only режим поддържан; 6-слот таблица за плащания верифицирана |
| **Datecs ISL — C variant** | DP-25, DP-150 | `datecs_isl/vendors.py:DatecsIslDevice` | Емпирично проверена payment-letter карта на fw 3.00 |
| **Datecs ISL — X variant** | DP-150X, FP-700X, FMP-350X, FP-2000, FP-800, FMP-55X | `datecs_isl/vendors.py:DatecsIslXDevice` | Header е TAB-separated 6 полета; парола `0000` |
| **Daisy** | (ISL-семейство) | `DaisyIslDevice` | Същите букви като Datecs ISL |
| **Eltrade** | (ISL-семейство) | `EltradeIslDevice` | 8 ДДС букви A–H + 11-буквена payment азбука |
| **Incotex** | (ISL-семейство) | `IncotexIslDevice` | Само 4 ДДС слота (A–D); отхвърля E–H |
| **Tremol** | само ISL profile | `TremolIslDevice` | Master/slave framing на legacy устройствата НЕ е покрит |

### POS периферия извън фискалното

| Клас | Драйвери / протоколи |
|---|---|
| **Клиентски дисплеи** | Datecs DPD-201; ESC/POS-съвместими (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000) |
| **Везни** | CAS PR-II/PD-II, Elicom EVL CASH47, Datecs CAS-compat (≈75 % от БГ retail-а), Toledo 8217, generic ASCII, OHAUS Ranger SICS over TCP |
| **Баркод четци** | USB HID + serial CDC + BLE през [`hid2serial`](https://github.com/rosenvladimirov/hid2serial) sister daemon. Linux `.deb` 0.1.7 production-ready; Windows 1.0.0 driver през [`hid2vsp`](https://github.com/rosenvladimirov/hid2serial/tree/main/driver/hid2vsp). |
| **Pinpad-и** | Datecs Pay (BluePad-50, BluePad-55, BlueCash-50) — виж NDA бележката в края |
| **Камери** | RTSP / ONVIF — multi-camera ingest, snapshot API, motion-event hook |
| **Контрол на достъп** | Polimex iCON115 push-event приемник, отваряне/заключване на врати, forward на card swipes |
| **MQTT** | Generic мост: subscribe + publish за интеграция със сензори / актуатори |

## Архитектура

```
┌──────────────────────────────────────────────────────────────────┐
│  Odoo в облака (l10n_bg_erp_net_fp)                              │
│                                                                  │
│   pos.session    /erp_net_fp/shift_close   HMAC-SHA256           │
│   pos.order            ▲                                         │
│   pos.payment          │                                         │
│                                                                  │
│  ───────────────────── │ ──────────────────────────────────────  │
│                  HTTPS │ HTTPS                                   │
│  ─────────────── proxy ↓ Odoo ─────────────────────────────────  │
│                                                                  │
│  Odoo.ErpNet.FP (този проект)                                    │
│   FastAPI на :8001 / :8443 / :443 (Cloudflare tunnel)            │
│                                                                  │
│   ┌─ routes/ ──────────────────────────────────────┐             │
│   │ printers   pinpads   scales   readers          │             │
│   │ displays   cameras   access   biometric        │             │
│   │ mqtt       polimex_events                      │             │
│   │ shift_sync (Android → Odoo мост)               │             │
│   │ shift_signal (Odoo → Android мост)             │             │
│   │ admin      rescue    iot_compat                │             │
│   └────────────────────────────────────────────────┘             │
│                                                                  │
│   USB · RS-232 · TCP · BLE · MQTT                                │
└──────┼─────────┼──────────┼─────────┼─────────────────────────────┘
       ▼         ▼          ▼         ▼
  Фискален ЕЦР Везни   Pinpad-и    Камери / BLE pinpad
  Бонове      Тегло    Карта       Snapshots / GATT
  Z отчети    разчет   разчет
```

### Граници на доверие

Три концентрични зони, всяка със свой auth модел:

1. **LAN-trusted local devices** (USB, RS-232, BLE) — без auth.
   Проксито е единствено на шината.
2. **Same-LAN HTTP clients** (Odoo browser POS, Android BlueCash
   клиент при WS-subscribe) — без auth. URL-ът на проксито сам
   служи като subscription key.
3. **Cross-trust HTTP** (Android → Odoo през проксито, Odoo →
   proxy push) — **HMAC-SHA256** над canonical-JSON тяло. Виж
   [Модел на сигурността](#модел-на-сигурността).

## Бърз старт

### Docker (препоръчително)

```bash
# 1. Работна директория с минимална конфигурация
mkdir -p odoo-erpnet-fp/config
cat > odoo-erpnet-fp/config/config.yaml <<'EOF'
server:
  host: 0.0.0.0
  port: 8001
  auto_detect: true            # проба USB/TTY при старт

  iot_setup:                   # опционално — само ако bridge-ваш
    enabled: false             # с отдалечен Odoo през shift_close
    odoo_url: ""
    token: ""

logging:
  level: INFO
EOF

# 2. Стартиране
docker run -d --name odoo-erpnet-fp \
  -p 8001:8001 \
  -v $(pwd)/odoo-erpnet-fp/config:/app/config \
  -v odoo-erpnet-fp-data:/app/data \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  vladimirovrosen/odoo-erpnet-fp:latest

# 3. Проверка
curl http://localhost:8001/healthz
curl http://localhost:8001/printers | jq
```

### Bare-metal (Linux)

```bash
pip install odoo-erpnet-fp
odoo-erpnet-fp --config /etc/odoo-erpnet-fp/config.yaml
```

В `packaging/systemd/odoo-erpnet-fp.service` има готов systemd unit.

### Windows

Предварително-компилиран installer е в `build/win-server/`.
Windows бинарникът съдържа същия FastAPI сървър, Datecs PM/ISL
драйвери и USBSerial-friendly udev заместител.

## Конфигурация

`config/config.yaml` е единственият source of truth. Секции (повечето
са опционални — defaults-ите са разумни за LAN-only фискален-принтер
setup):

```yaml
server:
  host: 0.0.0.0
  port: 8001
  auto_detect: true            # проба USB/TTY при старт

  iot_setup:                   # мост към отдалечен Odoo
    enabled: true
    odoo_url: https://www.example.com
    token: <shared-secret>     # HMAC secret + Bearer за /iot/setup

  registry:                    # Fleet enrolment (опционално)
    enabled: false
    url: https://iot.example.com
    secret: ""

  watchdog:
    enabled: true              # рестартира неотговарящи драйвери
    interval_s: 30

printers:
  - id: dp150
    driver: datecs.isl
    transport: serial
    device: /dev/ttyACM0
    baudrate: 115200

  - id: fp700mk
    driver: datecs.pm
    transport: serial
    device: /dev/ttyUSB0
    baudrate: 115200

scales:
  - id: cas1
    driver: cas.pr2
    transport: serial
    device: /dev/ttyUSB1

displays:
  - id: dpd201
    driver: datecs.dpd201
    transport: serial
    device: /dev/ttyUSB2

pinpads:
  - id: datecs_pay_main
    driver: datecs.pay
    device: /dev/datecs_pinpad/DAxxxxxx     # udev-открит

readers: []          # auto-discovered през hid2serial
cameras: []          # виж config-examples/cameras.yaml
access: []           # виж config-examples/access.yaml
mqtt: {}             # виж config-examples/mqtt.yaml
```

`config-examples/` съдържа документирани шаблони за всяка секция.

## Списък на endpoints

HTTP повърхността е групирана по клас устройства. Всеки клас споделя
малък набор convention-и — `GET /<class>` връща списък известни
устройства; `GET /<class>/<id>` показва конфиг; class-specific
операции следват.

### Фискални принтери (`/printers`)

```
GET    /printers                       — списък всички
GET    /printers/{id}                  — info + supportedPaymentTypes
GET    /printers/{id}/status           — флагове за статус + грешки
POST   /printers/{id}/receipt          — печат на фискален бон
POST   /printers/{id}/invoice          — печат на фактура
POST   /printers/{id}/reversalreceipt  — възстановяване / сторно
POST   /printers/{id}/zreport          — дневен Z-отчет
POST   /printers/{id}/xreport          — нефискален X-отчет
GET    /printers/{id}/vat-rates        — четене на програмирани ДДС
POST   /printers/{id}/vat-rates        — програмиране ДДС (само PM)
POST   /printers/{id}/plu/sync         — bulk PLU programming
POST   /printers/{id}/datetime         — задаване часовник (само PM)
POST   /printers/{id}/cash             — служебно внасяне/изнасяне
GET    /printers/{id}/journal          — четене на electronic journal
POST   /printers/{id}/duplicate        — повтаряне на последен бон
POST   /printers/{id}/reset            — soft reset
... и още (operators, logo, header/footer template)
```

### Друга периферия

```
/pinpads/{id}/purchase                 — Datecs Pay card charge
/scales/{id}/read                      — текущо тегло
/displays/{id}/text                    — изписване на дисплей
/readers/{id}/ws  /sse  /next  /last   — баркод push channels
/cameras/{id}/snapshot                 — JPEG snapshot
/access/{id}/door/open                 — Polimex отваряне на врата
/mqtt/{topic}                          — publish
```

### BlueCash мостове (новите контракти)

```
POST   /devices/<serial>/shift_close                  — Android upload-ва closed shift
DELETE /devices/<serial>/shift_close/<day>/<z>        — админ: изчисти dedup
POST   /devices/<serial>/events/push                  — Odoo emit на shift event
GET    /devices/<serial>/events/last                  — последно event poll
GET    /devices/<serial>/events/sse                   — SSE subscription
WS     /devices/<serial>/events/ws                    — WebSocket subscription
GET    /devices/<serial>/events/stats                 — админ / debug
```

Виж [BlueCash PLU интеграция](#bluecash-plu-интеграция) за пълния
жизнен цикъл.

### Админ + observability

```
GET    /healthz                        — liveness probe
GET    /metrics                        — Prometheus exposition
GET    /admin/bootstrap-info           — first-boot админ токен
POST   /admin/<various>                — rescue + reload endpoints
```

### Съвместимост с Odoo IoT Box

Една Odoo.ErpNet.FP инстанция отговаря и на двата URL prefix-а на
Odoo IoT Box — Odoo 18 (`/hw_drivers`, `/hw_proxy`) и Odoo 19
(`/iot_drivers`). Същите handler-и, различни пътища.

## Модел на сигурността

### Локален LAN

Без auth. Проксито слуша на `0.0.0.0:8001` по default. Ако е на
WAN-exposed VM, **filewall-ни го** до LAN-only и сложи reverse proxy
отпред за TLS. Няма вграден потребител/парола — нарочно, защото
консуматорите (Odoo POS browser, Android BlueCash клиент) живеят
в същата trust зона.

### Cross-trust HTTP

За всеки път, който пресича LAN границата — Android клиент → прокси
(през Cloudflare tunnel), прокси → cloud Odoo, Odoo → proxy push —
автентикацията е **HMAC-SHA256** над canonical-JSON request тялото,
със shared secret **`server.iot_setup.token`** (в `config.yaml`)
от страна на проксито и **`ir.config_parameter('iot_token')`** от
Odoo. Същата стойност отива в `ConnectionPreferences.apiKey` на
Android клиента.

```
canonical_raw = json.dumps(body,
                           separators=(",", ":"),
                           sort_keys=True,
                           ensure_ascii=False).encode("utf-8")
sig = HMAC-SHA256(secret, canonical_raw).hexdigest()
header = X-Registry-Signature: <sig>
```

Двете страни преизчисляват `canonical_raw` от parsed body — това
гарантира, че подписът оцелява през intermediary re-serialisation.

### Cloudflare особеност

Default `Python-urllib/3.x` и `httpx` user agents trig-ват Cloudflare
**Browser Integrity Check (error 1010)**. Проксито и Odoo emitter-ът
изпращат стабилен идентификатор:

```
User-Agent: Odoo.ErpNet.FP/1.0 (proxy bridge)
```

Ако сложиш Cloudflare tunnel пред проксито, ще ти трябва същият UA
на всеки custom клиент (или whitelist в Cloudflare WAF).

## BlueCash PLU интеграция

[BlueCash PLU Client](https://github.com/rosenvladimirov/BlueCash.PluClient)
е новият Android клиент за устройства BlueCash-50 / 5000 в PLU
режим. Интеграцията е напълно двупосочна:

### Odoo → Android (shift signal)

Когато `pos.session.action_pos_session_open()` се активира в Odoo,
нашето разширение publish-ва `shift.open` event към всяко linkнато
фискално устройство:

```
POST {device.host}/devices/{serial}/events/push
X-Registry-Signature: <hmac>
Content-Type: application/json

{"event": {"type": "shift.open",
            "pos_session_id": 1234,
            "operator_code": "1",
            "fiscal_day_number": 47,
            "issued_at": "2026-05-25T07:31:12Z"}}
```

In-memory hub-ът на проксито fans out до всеки свързан Android
subscriber (`WS /devices/<serial>/events/ws` или `GET …/sse`).
Subscriber-ите получават event-а verbatim и действат
(`ShiftTracker.openShift(...)` на client side).

При close на сесията проксито emit-ва `shift.close.request` по
същия начин — което подсказва на касиера в Android UI-то да пусне
Z-отчет.

### Android → Odoo (shift sync)

След като касиерът натисне Z, Android клиентът качва closed shift
към проксито:

```
POST /devices/<serial>/shift_close
X-Registry-Signature: <hmac>
Content-Type: application/json

{"device_serial": "DT525112",
 "odoo_session_id": 1234,
 "fiscal_day_number": 47,
 "operator_code": "1",
 "opened_at": "...", "closed_at": "...",
 "z_report_number": "00047",
 "z_report_at": "...",
 "totals": {...},
 "receipts": [{"uns": "DT525112-0001-0000123", ...}],
 "cash_movements": [{"direction": "IN", "amount": 50.00, ...}]}
```

Проксито:
1. Верифицира HMAC.
2. Проверява **SQLite dedup кеша** keyed по `(device_serial,
   fiscal_day_number, z_report_number)`. Ако hit → връща cached
   Odoo response (idempotent replay).
3. Re-canonicalise + re-sign на тялото и forward към
   `<iot_setup.odoo_url>/erp_net_fp/shift_close`.
4. Cache-ва Odoo response-а (успехи И детерминистични failures
   като 409 Conflict).

Odoo controller-ът после:
1. Реверифицира HMAC.
2. Делегира към `l10n.bg.erp.net.fp.shift.sync` service-а.
3. Service-ът lookup-ва `pos.session` по `odoo_session_id` и
   `fiscal.printer.device` по серия.
4. За всеки receipt: lookup-or-create `pos.order` keyed по
   `l10n_bg_uns` (това е Odoo-side dedup anchor-а).
5. За storno receipts (`is_storno: true`): създава refund
   `pos.order` и линква през `original_uns`.
6. За cash movements: post-ва `account.bank.statement.line`
   rows под cash journal-а на сесията с ±2 s dedup window.
7. Затваря `pos.session`.

Проксито връща controller response-а verbatim.

### Двуслойна идемпотентност

| Слой | Ключ | Storage |
|---|---|---|
| Прокси | `(device_serial, fiscal_day_number, z_report_number)` | SQLite WAL @ `/app/data/shift_dedup.sqlite` |
| Odoo  | `pos.order.l10n_bg_uns` | Postgres unique индекс |

Проксито поглъща Android retry буря; Odoo хваща случая където
проксито SQLite-а е изтрит (напр. container rebuild без volume).

## Операции

### Логове

```bash
docker logs -f odoo-erpnet-fp
# Per-driver log нива в config.yaml:
# logging:
#   level: INFO
#   loggers:
#     odoo_erpnet_fp.drivers.datecs_pm: DEBUG
```

### Здраве + метрики

```bash
curl http://localhost:8001/healthz
curl http://localhost:8001/metrics    # Prometheus format
```

Стандартни метрики: request rate / latency / errors per route;
per-device status check counts; PM driver frame round-trip latency
histogram.

### Hot-patch в dev

```bash
# Контейнерът съдържа кода на ДВЕ места. Patch-ни И двете.
docker cp my.py odoo-erpnet-fp:/app/odoo_erpnet_fp/.../my.py
docker cp my.py odoo-erpnet-fp:/usr/local/lib/python3.12/site-packages/odoo_erpnet_fp/.../my.py
docker exec odoo-erpnet-fp find / -name __pycache__ -type d -exec rm -rf {} +
docker restart odoo-erpnet-fp
```

За постоянни промени — rebuild на image-а (`docker compose build`).

### Решаване на проблеми

| Симптом | Вероятна причина |
|---|---|
| `ERR_R_PLU_VAT_DISABLE` на Datecs PM | Устройството е в PLU-only режим (FP-700MX); подай `pluNumber` в receipt item-а, не `taxGroup` |
| `Forbidden VAT` за всяка група | Същото като горе; не е ДДС-конфиг проблем |
| `End of paper` не се изчиства след reload | Power-cycle устройството; някои firmware lock-ват докато не се пусне X-отчет |
| Всички плащания се печатат като `В БРОЙ` | Payment slot mapping иска проверка за тази firmware вариация |
| `X-Registry-Signature mismatch` | `iot_setup.token` (прокси) ≠ `iot_token` ICP (Odoo); или canonicalisation drift (proxy лог показва raw bytes за сравнение) |
| HTTP 1010 от Cloudflare | Липсва User-Agent; очаквано ако викаш controller-а ръчно с `curl --user-agent ""` |
| Контейнерът не вижда USB | Подай `--device /dev/...` и провери `usb-modeswitch` за `cdc-acm` устройства |

## Разработка

```bash
git clone https://github.com/rosenvladimirov/Odoo.ErpNet.FP
cd Odoo.ErpNet.FP
pip install -e '.[dev]'
pytest                           # unit тестове
ruff check .                     # lint
mypy odoo_erpnet_fp              # type check
```

Структура на проекта:

```
odoo_erpnet_fp/
  server/                 — FastAPI app, config loader, registry
    routes/               — един HTTP модул на клас устройства
    adapters/             — payment_type / tax_group / messages
    iot_setup.py          — pair с Odoo, регистрирай устройствата
    registry.py           — Fleet enrolment (опционално)
    shift_dedup.py        — SQLite idempotency cache
    odoo_forwarder.py     — HMAC-signed POST helper
  drivers/                — драйвери на клас устройства
    fiscal/datecs_pm/     — PM v2.11.4 протокол
    fiscal/datecs_isl/    — ISL семейство (Datecs + Daisy + Eltrade и др.)
    scales/               — CAS, Toledo, generic, OHAUS и т.н.
    displays/             — Datecs, ESC/POS
    pinpad/               — Datecs Pay (NDA shim-ове)
    barcodes/             — hid2serial интеграция
    cameras/              — RTSP / ONVIF
    access/               — Polimex iCON
  config/                 — runtime конфиг + secrets (gitignored)
  packaging/              — debian, systemd, win installer
  tools/                  — operational скриптове
  tests/                  — pytest suite
```

### Принос

PR-и са добре дошли. Ограничения:

- Code style: ruff + black (PEP 8 с 80-char цел).
- Type hints препоръчителни, но не задължителни.
- Нови device драйвери ТРЯБВА да включват `tests/` smoke тест,
  който ползва fixture transport (без реален хардуер за CI).
- Всички user-facing strings остават на **английски**; преводът
  отива в `.po` файлове. Source коментарите могат да са на
  български.
- НЕ push-вай към OCA upstream — този repo е canonical source.

## Лиценз + НДА

**LGPL-3.0-or-later.** Автор: Rosen Vladimirov
([@rosenvladimirov](https://github.com/rosenvladimirov),
<vladimirov.rosen@gmail.com>).

### НДА ограничения

Следните модули съдържат протоколна логика защитена с НДА от
съответните производители. Python source-ите тук са тънки shim-ове
около externally-distributed `.so` библиотеки — реалната протоколна
логика НЕ е в този repo, и `.so` файловете НЕ се разпространяват:

- `drivers/pinpad/datecs_pay/` — Datecs Pay (BluePad-50, BlueCash-50)
- `drivers/pinpad/bluepad55/`  — BluePad-55 BLE мост

Ако искаш да разработваш срещу тях, трябва да получиш SDK
директно от Datecs и съответното NDA. Shim-овете в този repo ще
no-op-ват gracefully ако `.so`-то липсва.

### Благодарности

- Оригиналният [ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP)
  проект на ERP.NET — за HTTP протоколния дизайн.
- Odoo IoT Box драйверите — за ISL-семейство имплементациите,
  които `datecs_isl/` пакетът на този проект пренася към Python.
- Българските производители на фискални устройства (Datecs,
  Daisy, Tremol, Eltrade, Incotex) — за това, че толерират
  моите reverse-engineering усилия.
