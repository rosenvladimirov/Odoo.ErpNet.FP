# Odoo.ErpNet.FP — План за развитие

> **Последна актуализация:** 2026-05-25 · **Текуща версия:** `0.13.6` · **Цел v1.0:** Q3 2026
>
> 🇬🇧 In English: **[ROADMAP.md](ROADMAP.md)**

---

## Къде сме (май 2026)

След 13 версии и 12 месеца натрупана работа, проксито е
**production-ready** за core БГ retail use case-а:

| Възможност | Статус |
|---|:-:|
| Datecs PM + ISL фискални драйвери (FP-700MX, BC-50MX, DP-150, DP-150X, FP-700X, FMP-350X, ...) | ✅ |
| 6-slot payment mapping емпирично проверен на реален хардуер | ✅ |
| PLU програмиране, X/Z отчети, ДДС програмиране | ✅ |
| Клиентски дисплеи, везни, баркод четци (HID/BLE), pinpad-и | ✅ |
| Камери + Polimex iCON контрол на достъп + MQTT мост | ✅ |
| Odoo IoT Box съвместимост (v18 + v19, единствена инстанция) | ✅ |
| Fleet remote management (HMAC-signed heartbeat) | ✅ |
| Prometheus метрики + Grafana dashboards | ✅ |
| **BlueCash PLU Android client мостове** (shift_close + shift_signal) | ✅ НОВО |
| **Двуезична EN/BG документация** | ✅ НОВО |

Какво остава за **v1.0**:
- Последните 5–10 % покритие на хардуер (pinpad-и, label-printing
  везни, Tremol/Eltrade/Incotex драйвери)
- BlueCashPos backend интеграция (vendor POS като thin-client към Odoo)
- Hardening: подписани images, public release, Helm chart

За детайлен timeline какво е shipped досега, виж секцията
[Доставено в миналото](#доставено-в-миналото).

## Близък хоризонт: 2026-Q2 (юни)

### Активно — BlueCash PLU интеграция (Фаза 1)

**Статус:** code-complete на двете страни (proxy + Odoo
`19.0.15.8.0`), **deploy-нато на live dev** на
`www.odoo-shell.space`, чака Android-страна да завърши.

Остава:
- [ ] **Android `BlueCash.PluClient` repo** — `WS` subscriber listener
      (`/devices/<serial>/events/ws`); sync runner който качва
      `pendingSync()` shifts през `POST /shift_close` (~150 LOC Kotlin).
- [ ] **Public-reachable proxy URL** за cloud Odoo → proxy push
      посоката (понастоящем `erpnet.lan.mcpworks.net` е LAN-only DNS).
      Варианти: Cloudflare tunnel на проксито, или per-tenant
      `iot.box.erpnet_fp_url` override.

### Активно — BlueCashPos backend (Path A)

Datecs ships vendor POS app `BlueCashPos` на BlueCash-50 PLU
устройството. Fiscal API-то му сочи към `127.0.0.1:8086/api/v3/*`.

- [ ] **Listener на `:8086`** за 32-та vendor-API endpoint-а.
      Минимален scope: intercept на `fiscalreceipt`,
      `fiscalreceipt-invoice`, `returndocument` → push към Odoo;
      forward другите 29 непокътнати.
- [ ] **Vendor-app patch-free deployment** — `/etc/hosts` redirect
      на устройството така че `BlueCashPos.apk` продължава да мисли,
      че говори с Datecs cloud, но реално бие нашия proxy.

### Активно — Pinpad драйвери

- [ ] **myPOS** (Free Partner Program подаден 2026-05-09;
      sandbox access чака). BluePad-50/55 хардуер = Datecs;
      очакваме code reuse със съществуващия DatecsPay shim.
- [ ] **Borica direct TID протокол** — един драйвер покрива ВСИЧКИ
      bank-issued терминали в БГ (DSK, UBB, KBC, FiBank, Postbank,
      Allianz). Нужна е integrator registration в Borica.

## Близък хоризонт: 2026-Q3 (юли–септември)

### BlueCash storno Фаза 2 — refund по редове

- [ ] Anchor [`anchor-bluecash-storno-phase2-contract`] —
      `GET /pos.order/<id>/fiscal_receipt` на proxy за cross-device
      storno lookup. Phase 2 contract носи per-line refund detail
      (текущата Phase 1 е header-level only).

### Останалите фискални драйвери

- [ ] **Tremol** пълен PM driver (stub съществува, трябва real-hardware test)
- [ ] **Eltrade** пълен PM driver (stub съществува)
- [ ] **Incotex** пълен PM driver (stub съществува)

### Останалата периферия

- [ ] **SumUp / Stripe Terminal** — EU мобилни readers за SMB сегмента
- [ ] **Posiflex PD-2600 / PD-2800** клиентски дисплеи
- [ ] **Datecs ETS / Elicom EPS** label-printing везни — нужно за
      месарници / деликатеси (с PLU sync от Odoo)

### Контрол на достъп — Фаза C (биометрия)

`drivers/biometric/` — тънък клиент към external Node face-auth
microservice (отделен Rosen-owned проект). Liveness + anti-spoof
enforced **server-side в Odoo**, не на kiosk-а. Configuration-gated;
pure-fiscal deployments незасегнати.

## Средносрочен хоризонт: 2026-Q4 (октомври–декември)

### Готовност за v1.0 release

- [ ] **Подписани Docker images** (cosign), reproducible builds.
- [ ] **Стабилни тагове** `:1.0`, `:stable`, `:latest`.
- [ ] **GitHub release** с changelog, подписани checksums.
- [ ] **Helm chart** за Kubernetes deployments.
- [ ] **Authenticode-signed** Windows installer.
- [ ] **Публично обявяване** — Odoo forum, LinkedIn,
      `awesome-odoo` PR.

### Качество + tooling

- [ ] pytest покритие за всеки драйвер (mock pyserial през
      `pyserial.tools.testing`).
- [ ] Hardware-in-loop CI — GitHub Actions + Raspberry Pi runner
      с реален DP-150 + везна на банката.
- [ ] Integration tests: virtual fiscal printer + virtual scale +
      реален Odoo POS в CI.

### Документация

- [ ] EN technical guide + driver-extension tutorial.
- [ ] BG админ docs — per-vendor cheat-sheets, чести грешки,
      `config.yaml` reference.

## По-далечен хоризонт: след v1.0

### MES / факторска интеграция

> Реален customer use case — ErpNet.FP proxy като мост между MES
> устройства (PLC-та, сензори, везни) и Odoo през **външен** broker
> (RabbitMQ + `rabbitmq_mqtt`, Mosquitto, HiveMQ — клиентски избор).
> Proxy и Odoo са clients only.

- [ ] **MQTT client bridge** (`server/mqtt_bridge.py`, `aiomqtt`) —
      вече частично присъства през `/mqtt` route group; нужно е
      production hardening.
- [ ] **AMQP client + Odoo consumer** (`l10n_bg_erp_net_fp_amqp`).
- [ ] **CFX wire format** — IPC-CFX (Apache-2.0, индустриален стандарт
      за SMT/electronics manufacturing); Rosen има dormant `ipc-cfx`
      Python lib която може да се активира като canonical envelope,
      даваща zero-config compat с Yamaha, ASM/SIPLACE, Panasonic,
      Mycronic и др.

### BlueCashPos backend (Path B — synthetic catalog)

Много по-трудно от Path A: hijack-ваме vendor-ския
`dbgen.datecs.bg` catalog endpoint и serve-ваме synthetic
AES-encrypted ZIP директно от Odoo. Изисква reverse-engineering на
AES key от obfuscated Android class `d0/t0.java`. NDA-sensitive
работа, може да трябва vendor blessing първо.

### Опционално / nice-to-have

- **GUI config tool** (Tauri или Electron) — за non-technical
  оператори на Linux / Windows.
- **Cluster mode** — 2+ ErpNet.FP инстанции зад load balancer
  със shared device state.
- **Cloud-managed mode** — централен registry + remote config push
  (SaaS-style deployments).
- **Plugin SDK** — third-party driver development без forking
  на проксито.

## Доставено в миналото

Сбит лог на shipped работа.

| Версия | Дата | Highlight |
|---|---|---|
| **0.13.6** | 2026-05-25 | BlueCash shift_close + shift_signal contracts; bilingual README |
| 0.13.0 | 2026-05-22 | BluePad-55 BLE bridge (NDA-safe GATT↔PTY) |
| 0.12.0 | 2026-05-18 | DP-150 ISL payment-letter mapping verified emпирично |
| 0.11.0 | 2026-05-17 | Camera + access-control add-on (Phases A+B): Polimex iCON / Hikvision / Dahua actuators |
| 0.10.0 | 2026-05-15 | MQTT generic bridge route group |
| 0.9.0  | 2026-05-13 | Multi-device proxy (multiple printers per config) |
| 0.8.0  | 2026-05-10 | InfoPay payment-bridge интеграция |
| 0.7.0  | 2026-05-08 | Native Odoo IoT Box compat (v18 `/hw_drivers` + v19 `/iot_drivers`) |
| 0.6.0  | 2026-05-07 | Camera Phase A — go2rtc media hub + ALPR + ONVIF edge-ANPR |
| 0.5.0  | 2026-05-07 | OHAUS Ranger SICS scale; `read_device_info`; `vat-rates` GET/POST; `/admin/logs` + bootstrap-token |
| 0.4.0  | 2026-05-07 | `l10n_bg_erp_net_fp_fleet` split (CE-installable, EE/OCA bridges отделни) |
| 0.3.0  | 2026-05-07 | Fleet remote management (HMAC heartbeat + pairing + Fernet admin token) |
| 0.2.5  | 2026-05-06 | Prometheus `/metrics` + Grafana stack + iframe embed в Odoo |
| 0.2.0  | 2026-05-06 | HID barcode scanner през `hid2serial` sister daemon (Linux .deb + Windows installer) |
| 0.1.0  | 2026-04-x  | Initial fork — Datecs PM/ISL драйвери + клиентски дисплеи + везни + Datecs Pay pinpad |

## График (преразгледан 2026-05-25)

| Milestone | ETA | Кумулативно |
|---|---|---|
| BlueCash Phase 1 deployment + Android wiring | ~3 седмици | средата на юни 2026 |
| BlueCashPos backend Path A | ~3 седмици | началото на юли 2026 |
| myPOS + Borica pinpad драйвери | ~4 седмици | началото на август 2026 |
| Storno Phase 2 + останалите фискални вендори | ~3 седмици | края на август 2026 |
| v1.0 стабилизация + release | ~3 седмици | **средата на септември 2026** |

**Преразгледана v1.0 цел:** **средата на септември 2026** (slipped
от по-ранното "началото на юли" — BlueCash split-flow отне ~6
седмици архитектурна + имплементационна работа, която не беше в
оригиналния план).

## Отворени решения

1. **Auth за Android → proxy в production** — да задържим текущото
   HMAC-only, или да наслоим per-device long-lived bearer токени
   издавани през отделен `/auth/device/issue` endpoint?
2. **BlueCashPos Path B** — заслужава ли си AES RE работата, или
   Path A (vendor app untouched, fiscal intercept only) е достатъчно?
3. **Hardware-in-loop CI за v1.0** — да (по-бавно но trustworthy)
   или не (разчита на ръчни smoke tests)?
4. **License review** — да задържим LGPL-3, или split на LGPL-3
   core + OPL-1 premium add-ons (cluster, cloud-managed)?
5. **`anchor-bluecash-shift-signal` топология** — текущият dev
   deployment показва, че cloud Odoo → LAN proxy push изисква
   Cloudflare tunnel на проксито. Това ли е препоръчителният модел,
   или да добавим poll-based fallback за Android клиента, който не
   изисква ingress на проксито?

---

*Автор: Rosen Vladimirov · Асистент: Claude Code*
