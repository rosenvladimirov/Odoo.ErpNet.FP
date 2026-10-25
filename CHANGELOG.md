## [3.0.25] — 2026-10-25

### Added — customer display through the proxy (`/customer-display`)

- The till posts the current order to `POST /customer-display/{channel}`; the
  proxy keeps the last state and pushes it over SSE
  (`/customer-display/{channel}/events`) to every screen on the channel.
- The screen page is served by the proxy itself (`/customer-display/{channel}`):
  a monitor needs only a browser on the local network — no Odoo login, no
  internet. It falls back to polling `/state` while SSE is down.
- The same update drives a pole display (VFD 2×20, e.g. Datecs DPD-201) when
  the till names one (`pole`): the selected line, the total on payment, the
  change after it, a welcome text on an empty order. The euro sign is spelled
  out on code pages that have none (cp437) before the line is laid out. A
  failing pole never fails the update.

## [3.0.24] — 2026-10-25

### Added — `E_DEVICE_UNAVAILABLE` on a receipt the device never saw

- When the proxy cannot reach the device at all (TCP connect, serial port),
  the receipt answers `ok: false` with the code `E_DEVICE_UNAVAILABLE` instead
  of a bare text. Nothing was printed and the UNS stays free for a new
  attempt; the till keeps an approved card payment and prints when the device
  is back. A failure while paying or closing stays `E_RECEIPT_UNKNOWN`.
- Virtual device fault `offline`: the connection fails before any command.

## [3.0.23] — 2026-10-25

### Added — fallback unique sale number (`POST /printers/{id}/uns/fallback`)

- When the till has no batch of numbers from Odoo (server unreachable, no
  device serial in Odoo) but the device is reachable, the till asks the proxy
  for a fallback number: the device serial plus a sequence from the range
  9 000 000–9 999 999. Odoo's counter starts at 1 and never reaches it, and
  the proxy is the only path to the device, so the number never repeats.
- The counter lives in the receipt journal on disk. A journal kept in memory
  answers 503 instead of a number that could repeat after a restart; an
  unknown serial answers 409. After a wiped counter it starts above the
  highest fallback number still in the receipt records.
- `/healthz` features: `unsFallback`.

## [3.0.22] — 2026-10-24

### Added — virtual pinpad (`driver: virtual`)

- A pinpad without a device or bank, with the API of the DatecsPay facade:
  modes `approve` (RRN + auth code), `decline`, `immediate_decline` and
  `timeout` (the purchase hangs until `/cancel` or the timeout). VOID needs
  the same amount and an approved, not yet voided transaction.
- Every request (purchase, void, cancel, status, end of day) goes into a
  journal: `GET /pinpads/{id}/virtual`; `PUT /pinpads/{id}/virtual/mode`;
  `POST /pinpads/{id}/virtual/reset`. A real pinpad answers 404 there.

## [3.0.21] — 2026-10-24

### Added — virtual device: `no_status` fault

- `no_status`: the virtual Datecs PM refuses 0x4C (state of the receipt), so
  the reconcile cannot decide and an interrupted receipt stays `pending` —
  for testing the till's "outcome unknown" path end to end.

## [3.0.20] — 2026-10-24

### Fixed — Datecs PM: the device serial right after start

- `GET /printers/{id}` of a Datecs PM device returned an empty
  `serialNumber` until the first `/status`. Odoo needs the serial to hand the
  till a batch of unique sale numbers (UNS), so a till that sold before any
  status check could not print at all. On an empty cache the route now asks
  the device once (8 s cap, skipped while it is printing) and keeps the answer.
- The device info built from that cache kept an empty `uri` (also after the
  first `/status`); it now falls back to the address from the configuration.

## [3.0.19] — 2026-10-24

### Added — reconcile interrupted receipts with the device; busy status; cash drawer

- The receipt journal keeps the number the device gave when the receipt was
  opened. `POST /printers/{id}/receipt/reconcile` (and automatically at start)
  compares each `pending` receipt with the device (Datecs PM, 0x4C): last
  document ≥ that number and nothing open ⇒ `done`; smaller ⇒ `failed`; a
  receipt still open, an unreadable answer or an ICP device ⇒ stays `pending`
  for a manual decision — never a guess.
- 🚨 The 0x4C answer (IsOpen, Number, Items, Amount, Payed) and the drawer
  commands (PM 0x6A, ICP 0x2A) follow the protocol description and are NOT yet
  measured on a device — to be checked on BlueCash/FP-700 and Daisy.
- `GET /printers/{id}/status` answers `I_BUSY` at once while the device is
  printing, instead of waiting for the receipt or reporting a false E101.
- `POST /printers/{id}/drawer` and `/cashbox` (one route, two names: the Odoo
  backend calls /drawer, the till /cashbox — neither existed).
- `GET /printers/{id}/lastdocument`; the ICP receipt and invoice answers carry
  `uniqueSaleNumber` too.
- Journal rotation at start: `done`/`failed` older than 90 days
  (`ODOO_ERPNET_FP_RECEIPT_JOURNAL_DAYS`); `pending` is never deleted.
- `/healthz` features: `cashbox`.

## [3.0.18] — 2026-10-24

### Added — one receipt per UNS, even when interrupted (receipt journal)

- Every receipt and storno with a `uniqueSaleNumber` goes through a journal
  keyed by (printer, kind, UNS) in SQLite (`ODOO_ERPNET_FP_RECEIPT_JOURNAL`,
  default `/app/data/receipt_journal.sqlite`; in memory when the disk is not
  writable). A repeated POST for a printed receipt returns the stored result
  (`I_ALREADY_PRINTED`) and prints nothing; a POST while the same receipt is
  still printing waits for it.
- A failure while paying or closing leaves the receipt `pending` — it may be
  printed. Any further POST answers `E_RECEIPT_UNKNOWN` and does NOT print
  until the state is resolved: `GET /printers/{id}/receipt/{uns}`, then
  `POST /printers/{id}/receipt/{uns}/resolve?state=done|failed` (admin token).
  A failure before payment (no paper, open error) is `failed` and may be retried.
- A storno carries the original sale's UNS and one sale may have several
  partial returns: its key adds a fingerprint of the items and payments.
- `/healthz` announces `features: {idempotentReceipt, receiptByUns}`.

### Added — a virtual Datecs PM device (`transport: virtual`) for tests and sandboxes

- A printer with `driver: datecs.pm` and `transport: virtual` needs no hardware:
  the device is simulated at the frame level (`transport_virtual.py`), so the PM
  facade, the routes and the answer parsing run their real code. It keeps a
  journal of the "printed" documents (receipts, stornos, Z, cash in/out) per
  printer, optionally as JSON (`extras.virtual_journal`).
- Faults can be injected (`extras.virtual_faults` or
  `PUT /printers/{id}/virtual/faults`): `no_paper`, `error_on_open`,
  `error_on_close`, `timeout_on_close` — the last one records the receipt but
  never answers, the dangerous case an idempotent print has to survive.
- `GET /printers/{id}/virtual` (journal and state), `POST /printers/{id}/virtual/reset`.
  These routes answer only for virtual printers (404 for real devices).
- A repeated SEQ inside one connection gets the same answer without running the
  command again, as the device does — so the facade's retries never print twice.

## [3.0.17] — 2026-10-14

### Fixed — Datecs PM: the receipt answer carries the FM serial and the UNS

- `fiscalMemorySerialNumber` was always empty on Datecs PM: the route read
  `PrinterEntry.info`, which is never set for PM. It now comes from the device
  info cache; with an empty cache the device is asked once (the connection is
  already open) and the answer is kept. Without it Odoo could not storno.
- The answer now also returns `uniqueSaleNumber` (the UNS the receipt was
  printed with), so Odoo can store it for a later storno.

### Changed — a storno paid by card is refused before any document is opened

- `E_STORNO_CARD_NEEDS_PINPAD` when a storno pays with card/bank on Datecs PM:
  the device refuses it at payment time (-112201) and the half-printed
  document has to be cancelled (14.10.2026: documents 112, 113). Pay the
  storno in cash or void the card payment first.

## [3.0.16] — 2026-10-14

### Added — Datecs PM: storno receipt (`/printers/{id}/reversalreceipt`)

- `PmDevice.open_storno_receipt()` — cmd **0x2B** (protocol group C §4.6):
  OpCode, OpPwd, TillNmb, Storno (0 operator error / 1 refund / 2 tax base
  reduction), DocNum, DateTime (`DD-MM-YY hh:mm:ss`), FMNumber, NSale.
  Sale lines, payment and close are the same commands as for a receipt.
- The route no longer answers `E_NOT_IMPLEMENTED` on Datecs PM. Bad original
  data (date, document number, FM number) → `E_INVALID_STORNO` before any
  document is opened. Datecs ICP still answers `E_NOT_IMPLEMENTED`.
- BlueCash-50MX (DA056718, 14.10.2026): the storno of receipt 109 printed as
  document 114. ⚠️ The device refuses a storno paid with payment types 1
  (КРЕДИТ) and 2 (ДЕБ.КАРТА) without a pinpad transaction — `-112201 Bad value
  of parameter 1` at 0x35; the document is then cancelled (112, 113). Paid in
  cash it passes.
- Tests: `tests/test_pm_facade.py` (the protocol example, validation),
  `tests/test_server.py` (full path, reason codes, bad data sends nothing,
  device refusal).


## [3.0.15] — 2026-10-12

### Fixed — Datecs PM: a line discount was printed as a SURCHARGE

- `register_sale` (0x31) and `sale_programmed` (0x3A) sent `DiscountType`
  **`1`** for `discount-percent`. In the PM protocol `1` is *surcharge by
  percentage*; *discount by percentage* is **`2`** (the C# ErpNet.FP maps it
  the same way). A 25 % discount on 79.00 came out as "Надбавка 25% +19.75",
  total 98.74 (BlueCash-50, 12.10.2026; the receipt was voided).
- Tests: `tests/test_pm_facade.py` — the discount type for both commands, and
  no discount type when there is no discount.

## [3.0.14] — 2026-10-10

### Fixed — `/printers/{id}/invoice` no longer prints a comment receipt instead of an invoice

- New config setting `invoice.allow_comment_fallback` (YAML section
  `invoice:`), **default `false`**.
- ICP device without native invoice (Datecs ICP firmware < 3.00, or no
  detect cache yet): `ok=False`, code **`E412`** "Native fiscal invoice not
  supported by this device/firmware" — no receipt is opened, no comment
  lines are printed.
- Native invoice rejected with `E401`: the half-opened document is aborted
  (`abort_receipt`), then the same `E412` (device errors appended) — **no
  second (fallback) receipt**.
- `invoice.allow_comment_fallback: true` keeps the legacy behaviour (regular
  fiscal receipt with a "ФАКТУРА" comment header — NOT a Naredba H-18
  invoice) and logs a warning on every use.
- Datecs PM unchanged (`E501`).
- Tests: `tests/test_server.py` P1–P4 (+ native happy path, E401 legacy
  path, setting default).

## [0.21.0] — 2026-07-24

### Added — Europlacer live file-system dashboard tab

- Two read-only endpoints on the europlacer router: `GET /europlacer/{name}/files`
  (top-level listing of `order_dir` + `answer_dir`, newest first — the order XML
  the proxy writes and the `.ans` answers the machine returns, classified
  order/answer) and `GET /europlacer/{name}/file?filename=` (path-traversal-safe
  content preview, 256 KiB cap). Both scan off the event loop with an 8 s hard
  timeout so a hung CIFS mount can never wedge the proxy; top-level only (never
  recurse — `order_dir` is a machine OUT tree with huge `Outputs/` subfolders).
- New `📁 Europlacer` dashboard tab (static UI): live 2 s polling of the producer
  status + file listing, click-to-preview file content, paused while a preview is
  open. Shows what we push and what the machine returns in real time.

## [0.20.0] — 2026-07-22

### Added — Europlacer material-OUT producer driver (first producer)

- New `drivers/europlacer/` + route `server/routes/europlacer.py`: the proxy's
  FIRST *producer* driver. Odoo POSTs a ready material-order XML to
  `POST /europlacer/{name}/order`; the driver writes it atomically into the
  machine's shared folder (`order_dir`, e.g. the Europlacer `/STK/InputsOrders`
  CIFS folder on the master), then — on a worker thread — waits for the
  machine's `.ans` answer file, reads `<Error>N</Error>` and retries per the
  reverse-engineered code table (0/5 = success incl. idempotent "already
  existing"; 4/10/12/13/14 = transient → retry with backoff; others = fatal).
  The `.ans` filename convention matches the Odoo 11 `dxf` mapper
  (`europlacer_trac.py`: same stem, `.xml`→`.ans`); the wait + retry are new
  (Odoo 11 did a single immediate check and only warned on mismatch).
- Non-blocking: `POST /order` returns **202** immediately (the `.ans` wait can
  reach the machine's answer timeout, param 290, far beyond any HTTP timeout);
  the terminal outcome is delivered out-of-band — a live `bus_inject`
  `europlacer.order.done|failed` ping plus an optional signed `result_url`
  POST — and is pollable at `GET /europlacer/{name}/order/{order_id}`.
- Auth: `POST /order|start|stop` are HMAC-guarded (`X-Registry-Signature` over
  the raw body / URL path, keyed on `server.iot_setup.token`), like the other
  Odoo→proxy business routes.
- New `europlacer:` config section (`EuroplacerStationSpec`), registry
  (`EuroplacerRegistry`, granular hot-reload), and `europlacer` added to the
  `config.d` fragment + `push_config` allow-lists.
- Fully isolated from the CFX consumer path (ADR-ERPNET-0003 "proxy = thin
  relay"): with no `europlacer:` section the driver is never imported and the
  pure-fiscal deployment stays byte-identical. See ADR-ERPNET-0005.

## [0.19.1] — 2026-07-20

### Fixed — CFX envelope `TimeStamp` was never forwarded to Odoo

- `CfxEvent.audit_body()` did not carry the event time, so the Odoo side
  (`cfx_ingest.py`, `event.get("ts")`) always read `None` and fell back to a
  body-derived time. Most CFX messages — `UnitsInspected`, `UnitsArrived`,
  `UnitsDeparted` — carry no time in `MessageBody` at all, because CFX puts
  it on the **envelope**. Result: `cfx.machine.stat.event_time` stayed empty
  and those records never surfaced in time-sorted views.
- Measured on the MEC production stack before the fix: **97.6 % of PARMI
  records (1412/1447)** and **38 % of Europlacer (44922/118347)** had a null
  `event_time`, while the messages themselves were arriving normally.
- `extract_timestamp()` reads the envelope `TimeStamp` (with `cfx-timestamp`
  AMQP property as fallback) and `CfxEvent.cfx_timestamp` forwards it raw as
  `ts`. No reformatting: Odoo's `_parse_dt` hands it to
  `datetime.fromisoformat`, which on py3.11+ parses the .NET 7-digit
  fractional second (`…12.0702018+03:00`) directly. Verified on the proxy's
  own py3.12.13.
- Absent timestamp → the `ts` key is omitted entirely, preserving the
  existing Odoo-side fallback. `to_json()` also exposes `cfxTimestamp`, kept
  distinct from `timestamp` (proxy receive time).

## [0.18.0] — 2026-05-30

### Added — Polimex offline time-window enforcement (D3 time schedules)

- **`write_time_schedule(ts_number, week)` + `read_time_schedule` (F3)** on
  the Polimex driver — builds the 258-char D3 `ts_data` blob (2-hex slot
  number + 8 days × 4 intervals × HH:MM begin/end; day 7 = holiday), ported
  1:1 from the AGPL `hr_rfid` reference. A local card references the slot via
  its `ts_code`, so the controller enforces the schedule **standalone /
  offline**.
- **`POST /access/{id}/card`** is preceded by **`POST /access/{id}/time_
  schedule`**: the Odoo `polimex.ts.sync` Fleet command writes the onboard
  TS slot (D3) before the card.sync (D1) that references it.
- Tests: D3 `ts_data` encoding (length, day layout, HH:MM) + D3/F3 frames.

## [0.17.0] — 2026-05-29

### Added — Polimex card sync (D1) — local-memory card programming

- **`add_card` / `remove_card` (D1 Add/Delete Card)** on the Polimex driver
  build the D1 d-body (`card + pin + ts_code + rights_data + rights_mask`),
  ported 1:1 from the AGPL `hr_rfid` reference; `_send` generalised to
  `_send_frame(c, d)`. Relay-type controllers guarded (different D1 body).
- **`POST /access/{id}/card`** (add|remove) — 501 for drivers without card
  management, 502 on controller error.
- **`polimex.card.sync` handler** is no longer a stub: `hw_op=skip` for
  **cloud** credentials (server-validated, External DB + proxy ZEN), or
  add/remove for **local/both** credentials (D1 write); needs `access_id`
  (the controller's `proxy_access_id`).
- Tests: D1 encoding vs reference + add/remove frames + relay guard.

## [0.16.1] — 2026-05-29

### Fixed — zen-engine missing from the image (offline ZEN was dormant)

- The Docker image installed only the base package (`pip install .`), never
  the `[zen]` extra, so deployed proxies had **no `zen-engine`** → every
  proxy-side offline access decision silently fell to fail-secure deny.
- **`ARG EXTRAS`** + conditional `pip install ".[${EXTRAS}]"`: fiscal-only
  images stay slim; access-control images build with
  `--build-arg EXTRAS=zen` → tag `:0.16.1-zen` (now `:0.18.0-zen`). Verified
  live: `zen-engine 0.53.0` + `import zen` OK in the deployed pods.

## [0.16.0] — 2026-05-26

### Added

- **Unified shift bridge (TCP :9103)** — `drivers/shifts/` + `/shifts`
  routes; proxy connects as a persistent NDJSON client (pull + mark + status
  + signal on one channel). Heartbeat `_device_summary` now unions running +
  configured devices.

## [0.15.0] — 2026-05-26

### Added — proxy-side offline ZEN access decisions (Phase 3)

- **`server/access_decision/` package** — `ZenLocalRunner` (lazy
  `import zen`, **fail-secure deny** on any failure), `GraphStore` (persists
  ZEN graphs to `/app/data/zen_graphs/<code>.json` with version + sha256),
  and `derive_direction()` (sequence/timing preprocessing kept byte-identical
  with the Odoo-side `access.context.builder._derive_direction`).
- **`POST /access/evaluate`** — evaluate an access decision **offline** when
  Odoo is unreachable: build the 3-dimension context, run the cached ZEN
  graph locally, return `{ok, result, version, fail_secure}`.
- **Heartbeat graph sync** (`routes/zen_sync.py`) — proxy reports loaded
  graph versions; Odoo pushes `pending_graphs` on mismatch.

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
- ФП-700МК е Datecs PM (НЕ ICP) — `datecs.pm` driver връща `PM (v2.11.4)` + fiscalized status. `datecs.icp`/`datecs.icpx` връщат SYN+NAK (грешен framing).

## [0.13.6] — 2026-05-24
### Added
- **PLU-based sale support** (cmd 0x3A `sale_programmed`) — за фискални устройства в PLU-only режим (ФП-700МК, които отказват free-text sales с `ERR_R_PLU_VAT_DISABLE`). `SaleItem.pluNumber` (optional Integer) → ако е попълнен, receipt route ползва PLU lookup вместо `register_sale`.
- `PmDevice.sale_programmed(plu_number, quantity, price=None, discount_percent=None)` метод за cmd 0x3A.

### Fixed
- `pm_v2_11_4.read_device_info`: fallback на `CMD_DIAGNOSTIC (0x5A)` когато `0x7B` връща минимален отговор (1 поле). FP-700MX (FW 3.00 Jul25) сега коректно се parse-ва: model `FP-700MX`, serial `DA052093`, FW `3.00 22Jul25 0923`.
