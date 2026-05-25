
## [0.14.0] — 2026-05-26

### Added — BlueCash PLU client integration (4 anchor contracts shipped)

- **shift_close endpoint** — Android → proxy → Odoo bridge for closed-shift
  upload. `POST /devices/<serial>/shift_close` with HMAC-SHA256 auth and
  SQLite dedupe cache; pos.order materialisation, refund link-up, cash
  movement posting, session close. Companion controllers in
  l10n_bg_erp_net_fp v18 + v19 (model l10n.bg.erp.net.fp.shift.sync).
- **shift_signal endpoint** — Odoo → proxy → Android push channel.
  In-memory pub/sub: `POST /devices/<serial>/events/push` (HMAC),
  `WS /devices/<serial>/events/ws`, `GET …/events/sse` (with `?since=`
  replay), `GET …/events/last`, `GET …/events/stats`. pos.session open
  / closing hooks emit shift.open + shift.close.request to all linked
  fiscal-device proxies.
- **storno Phase 2 endpoints** — `GET /pos.order/<id>/fiscal_receipt`
  (lookup by Odoo ID), `GET /pos.order/by_uns/<uns>/fiscal_receipt`
  (cross-device lookup), `POST /pos.order/<id>/refund_printed` (Android
  notifies after print → proxy creates linked refund pos.order). Service
  model l10n.bg.erp.net.fp.pos.order.storno with PLU reverse lookup,
  VAT-letter best-effort, payment-slot reverse mapping.
- **TCP barcode reader** transport — new `tcp` option on ReaderConfig +
  `TcpBarcodeReader` driver (push-only, newline-delimited bytes from any
  TCP source). Wires the BlueCash-55 built-in scanner (port 9102) into
  the standard `/readers/<id>` event bus.
- **BlueCash-55 socat pinpad bridge** — `tools/bluecash55_bridges.sh` with
  `--watchdog` and `--watchdog-daemon` modes. socat baked into the
  Docker image (no apt-install at runtime). PTY symlink target uses
  serial naming (`/dev/datecs_pinpad/<serial>`).

### Changed

- **Device id naming** — adopted serial-number convention for new entries
  (DA054852, DT737851, …); model aliases like `bluecash01` retired for
  newly-registered devices (legacy aliases preserved). Documented in
  feedback_proxy_device_serial_id_convention memory note.
- **HMAC canonical-JSON rule** — `sort_keys=True, separators=(',', ':'),
  ensure_ascii=False` enforced on both sides of every cross-trust hop
  (Android → proxy, proxy → Odoo). odoo_forwarder helper module extracted
  from registry.py inline pattern.
- **Cloudflare User-Agent** — `User-Agent: Odoo.ErpNet.FP/1.0 (proxy
  bridge)` on all outbound HTTP to Odoo (avoids CF Browser Integrity
  error 1010 on default urllib UA).
- **README + ROADMAP rewritten** — bilingual EN/BG (README.en.md,
  README.bg.md, ROADMAP.bg.md), reflect v0.14.0 reality, full BlueCash
  PLU integration documented.

### Fixed

- `shift_sync` route reads `request.app.state.config.server` (was the
  non-existent `app.state.cfg` — caused 500 on every request post-restart).


## [0.13.0] — 2026-05-23
### Added
- DatecsPay pinpad: пълен event-loop `datecs_run_transaction` в C (NDA библиотеката); проксито вика само тази функция.
- `POST /pinpads/{id}/cancel` — прекъсване на текущата транзакция без да чака lock-а; thread-safe `cancel_requested` флаг в C, polled от run_transaction (≤2s latency); опит за TRANSACTION END abort на терминала.
- `/pinpads/{id}/purchase` (реално работещ), `void`, `end_of_day`, `test_connection`.
- USB autodetect: пропускане на pinpad VID-ове като ридъри.

### Changed
- `PinpadEntry.active_pinpad` — публичен handle към активния facade за безлок-овото cancel route.

## [0.13.2] — 2026-05-23
### Added
- `udev/99-erpnet-fp.rules` — stable `/dev/datecs_pinpad` + `/dev/honeywell_scanner` symlinks (по vendor:product ID, оцеляват power-cycle/re-enumeration).
- `scripts/install-udev.sh` — host install helper (sudo).

### Changed
- `reader_autodetect._build_reader_config` предпочита udev DEVLINKS симлинк (custom-named → `/dev/serial/by-id/*` → raw devnode). UI-ът и логовете сега показват `/dev/honeywell_scanner` вместо разменчивия `/dev/ttyACM0`.

## [0.13.3] — 2026-05-23
### Added
- Multi-device udev support: всяко правило създава И `<name>_<serial>` symlink (винаги уникален), не само генеричния. При 2+ устройства от един тип use serial-suffix-а в `config.yaml`; генеричният сочи към последно enumerated устройство (недетерминистичен).
- `_prefer_stable_symlink` сега предпочита по-дългия (specific) symlink → UI винаги показва кое физическо устройство е свързано.

### Changed
- `config.yaml` примерният bluepad55 ползва `/dev/datecs_pinpad_3526900033` (per-serial) — оцелява добавяне на 2-ро устройство.

## [0.13.4] — 2026-05-23
### Security / NDA boundary
- **DockerHub repo превърнат в private** — distribution става само под NDA agreement.
- **Header `datecs_pinpad_driver.h` премахнат от distribution**: pyproject `package-data` сега bundle-ва само `lib/*.so`. Headerът беше „Rosetta stone" за протокола (всички cmd/subcmd/TLV кодове в plain text); .so-то изисква reverse engineering, което е значително по-висока бариера.
- **`.so` strip-нат с `--strip-unneeded`**: запазва exported symbols за ctypes, но премахва debug info → -12% size + по-голяма RE трудност.

## [0.13.5] — 2026-05-24
### Added
- Multi-device udev support за устройства БЕЗ USB serial (Datecs Serial клас, FP-700МК): USB topology suffix (`datecs_fp_port<bus_path>`) — стабилно per физически порт; при смяна на порт името се променя (което е feature за traceability).
- Distinguish-ване между Datecs PinPad (`ATTRS{product}="PinPad"`) и Datecs Serial (`ATTRS{product}="Datecs Serial"`) за същия VID:PID (`fff0:0100`).
- FTDI symlink (`/dev/ftdi_serial` + `_port<path>` за clones без serial).
- `scripts/discover-devices.sh` — scan-ва /dev/* + извежда готов config.yaml snippet за откритите устройства.
- `config.yaml` пример с `fp700mk` (driver `datecs.pm`).

### Notes
- ФП-700МК е Datecs PM (НЕ ISL) — `datecs.pm` driver връща `PM (v2.11.4)` + fiscalized status. `datecs.isl`/`datecs.islx` връщат SYN+NAK (грешен framing).

## [0.13.6] — 2026-05-24
### Added
- **PLU-based sale support** (cmd 0x3A `sale_programmed`) — за фискални устройства в PLU-only режим (ФП-700МК, които отказват free-text sales с `ERR_R_PLU_VAT_DISABLE`). `SaleItem.pluNumber` (optional Integer) → ако е попълнен, receipt route ползва PLU lookup вместо `register_sale`.
- `PmDevice.sale_programmed(plu_number, quantity, price=None, discount_percent=None)` метод за cmd 0x3A.

### Fixed
- `pm_v2_11_4.read_device_info`: fallback на `CMD_DIAGNOSTIC (0x5A)` когато `0x7B` връща минимален отговор (1 поле). FP-700MX (FW 3.00 Jul25) сега коректно се parse-ва: model `FP-700MX`, serial `DA052093`, FW `3.00 22Jul25 0923`.
