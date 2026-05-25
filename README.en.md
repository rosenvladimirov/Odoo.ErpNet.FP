# Odoo.ErpNet.FP — English documentation

> Python drop-in replacement for the C# [ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP)
> HTTP fiscal-printer server, extended into a one-stop POS-side device
> hub for the Bulgarian retail and SMB market.
>
> **Version `0.13.6`** · Production · Docker
> `vladimirovrosen/odoo-erpnet-fp:0.13.6` (also `:latest`).

---

## Table of contents

1. [What it is and why](#what-it-is-and-why)
2. [Hardware coverage](#hardware-coverage)
3. [Architecture overview](#architecture-overview)
4. [Quick start](#quick-start)
5. [Configuration](#configuration)
6. [Endpoint reference](#endpoint-reference)
7. [Security model](#security-model)
8. [BlueCash PLU integration](#bluecash-plu-integration)
9. [Operations](#operations)
10. [Development](#development)
11. [License + NDA](#license--nda)

---

## What it is and why

The Bulgarian fiscal-device market is fragmented: every major
vendor (Datecs, Daisy, Tremol, Eltrade, Incotex) ships its own protocol
dialect, its own utility, and its own Windows-only driver. The original
**[ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP)** C# project gave
the market a uniform HTTP front-end across most ISL-family devices —
but the PM-family (Datecs FP-700MX, BC-50MX) was never covered there,
non-fiscal peripherals were out of scope, and Linux deployment was
clumsy.

**Odoo.ErpNet.FP** is a Python re-implementation that:

- preserves the original HTTP protocol byte-for-byte (existing
  `l10n_bg_erp_net_fp` Odoo addon clients continue to work);
- adds a **Datecs PM v2.11.4** driver (FP-700MX, BC-50MX) with full
  PLU programming, multi-operator support, programmable VAT rates,
  X/Z reports, and the empirically verified 6-slot payment layout;
- hosts **all** the POS-side peripherals (customer displays, scales,
  pinpads, barcode readers, cameras, access control, MQTT) on the
  same FastAPI process;
- runs natively on Linux (Docker preferred), Windows, and the Odoo IoT
  Box variants (Debian + Linux Mint);
- exposes a **secure bridge** to a remote Odoo instance via
  HMAC-SHA256-signed bodies, mirroring the proven pattern from the
  Fleet registry protocol;
- bridges the new **BlueCash PLU Android client** (`/devices/<serial>/
  shift_close`, `/devices/<serial>/events/*`) into Odoo's
  `pos.session` lifecycle.

## Hardware coverage

### Fiscal printers — production-verified

| Family | Devices | Driver | Notes |
|---|---|---|---|
| **Datecs PM** | FP-700MX, BC-50MX | `datecs_pm/pm_v2_11_4.py` | PLU-only mode supported; 6-slot payment table verified |
| **Datecs ISL — C variant** | DP-25, DP-150 | `datecs_isl/vendors.py:DatecsIslDevice` | Empirically verified payment-letter map on fw 3.00 |
| **Datecs ISL — X variant** | DP-150X, FP-700X, FMP-350X, FP-2000, FP-800, FMP-55X | `datecs_isl/vendors.py:DatecsIslXDevice` | Header is TAB-separated 6 fields; password `0000` |
| **Daisy** | (ISL-family) | `DaisyIslDevice` | Same letters as Datecs ISL |
| **Eltrade** | (ISL-family) | `EltradeIslDevice` | 8 VAT letters A–H + 11-letter payment alphabet |
| **Incotex** | (ISL-family) | `IncotexIslDevice` | Only 4 VAT slots (A–D); rejects E–H |
| **Tremol** | ISL profile only | `TremolIslDevice` | Master/slave framing on legacy devices NOT covered |

### Non-fiscal POS peripherals

| Class | Drivers / protocols |
|---|---|
| **Customer displays** | Datecs DPD-201; ESC/POS-compatible (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000) |
| **Scales** | CAS PR-II/PD-II, Elicom EVL CASH47, Datecs CAS-compat (≈75 % of BG retail), Toledo 8217, generic ASCII, OHAUS Ranger SICS over TCP |
| **Barcode readers** | USB HID + serial CDC + BLE through the [`hid2serial`](https://github.com/rosenvladimirov/hid2serial) sister daemon. Linux `.deb` 0.1.7 production-ready; Windows 1.0.0 driver via [`hid2vsp`](https://github.com/rosenvladimirov/hid2serial/tree/main/driver/hid2vsp). |
| **Pinpads** | Datecs Pay (BluePad-50, BluePad-55, BlueCash-50) — see NDA caveat at the bottom |
| **Cameras** | RTSP / ONVIF — multi-camera ingest, snapshot API, motion-event hook |
| **Access control** | Polimex iCON115 push-event receiver, door open/lock, card swipe forwarding |
| **MQTT** | Generic bridge: subscribe + publish for sensor / actuator integration |

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────┐
│  Odoo on the cloud (l10n_bg_erp_net_fp)                          │
│                                                                  │
│   pos.session    /erp_net_fp/shift_close   HMAC-SHA256           │
│   pos.order            ▲                                         │
│   pos.payment          │                                         │
│                                                                  │
│  ───────────────────── │ ──────────────────────────────────────  │
│                  HTTPS │ HTTPS                                   │
│  ─────────────── proxy ↓ Odoo ─────────────────────────────────  │
│                                                                  │
│  Odoo.ErpNet.FP (this project)                                   │
│   FastAPI on :8001 / :8443 / :443 (Cloudflare tunnel)            │
│                                                                  │
│   ┌─ routes/ ──────────────────────────────────────┐             │
│   │ printers   pinpads   scales   readers          │             │
│   │ displays   cameras   access   biometric        │             │
│   │ mqtt       polimex_events                      │             │
│   │ shift_sync (Android → Odoo bridge)             │             │
│   │ shift_signal (Odoo → Android bridge)           │             │
│   │ admin      rescue    iot_compat                │             │
│   └────────────────────────────────────────────────┘             │
│                                                                  │
│   USB · RS-232 · TCP · BLE · MQTT                                │
└──────┼─────────┼──────────┼─────────┼─────────────────────────────┘
       ▼         ▼          ▼         ▼
  Fiscal ECR  Scales    Pinpads   Cameras / BLE pinpad
  Receipts    Weight     Card    Snapshots / GATT
  Z reports   read       reads
```

### Trust boundaries

Three concentric trust zones, each with its own auth model:

1. **LAN-trusted local devices** (USB, RS-232, BLE) — no auth. The
   proxy is the only thing on the bus.
2. **Same-LAN HTTP clients** (Odoo browser POS, the Android BlueCash
   client when WS-subscribing) — no auth. The proxy's URL is
   itself the subscription key.
3. **Cross-trust HTTP** (Android → Odoo through proxy, Odoo →
   proxy push) — **HMAC-SHA256** over the canonical-JSON body.
   See [Security model](#security-model).

## Quick start

### Docker (recommended)

```bash
# 1. Create a working dir with a minimal config
mkdir -p odoo-erpnet-fp/config
cat > odoo-erpnet-fp/config/config.yaml <<'EOF'
server:
  host: 0.0.0.0
  port: 8001
  auto_detect: true            # probe USB/TTY on startup

  iot_setup:                   # optional — only if bridging to a
    enabled: false             # remote Odoo via shift_close
    odoo_url: ""
    token: ""

logging:
  level: INFO
EOF

# 2. Run
docker run -d --name odoo-erpnet-fp \
  -p 8001:8001 \
  -v $(pwd)/odoo-erpnet-fp/config:/app/config \
  -v odoo-erpnet-fp-data:/app/data \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  vladimirovrosen/odoo-erpnet-fp:latest

# 3. Verify
curl http://localhost:8001/healthz
curl http://localhost:8001/printers | jq
```

### Bare-metal (Linux)

```bash
pip install odoo-erpnet-fp
odoo-erpnet-fp --config /etc/odoo-erpnet-fp/config.yaml
```

A systemd unit lives at `packaging/systemd/odoo-erpnet-fp.service`.

### Windows

A pre-built installer is in `build/win-server/`. The Windows binary
ships with the same FastAPI server, Datecs PM/ISL drivers, and a
USBSerial-friendly udev replacement.

## Configuration

`config/config.yaml` is the single source of truth. Sections (most are
optional — defaults are sane for a LAN-only fiscal-printer-only setup):

```yaml
server:
  host: 0.0.0.0
  port: 8001
  auto_detect: true            # probe USB/TTY on startup

  iot_setup:                   # bridge to remote Odoo
    enabled: true
    odoo_url: https://www.example.com
    token: <shared-secret>     # HMAC secret + Bearer for /iot/setup

  registry:                    # Fleet enrolment (optional)
    enabled: false
    url: https://iot.example.com
    secret: ""

  watchdog:
    enabled: true              # restart unresponsive device drivers
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
    device: /dev/datecs_pinpad/DAxxxxxx     # udev-discovered

readers: []          # auto-discovered via hid2serial
cameras: []          # see config-examples/cameras.yaml
access: []           # see config-examples/access.yaml
mqtt: {}             # see config-examples/mqtt.yaml
```

`config-examples/` contains documented templates for every section.

## Endpoint reference

The HTTP surface is grouped by device class. Each class shares a small
set of conventions — `GET /<class>` lists known devices; `GET /<class>/
<id>` shows config; class-specific operations follow.

### Fiscal printers (`/printers`)

```
GET    /printers                       — list all
GET    /printers/{id}                  — device info + supportedPaymentTypes
GET    /printers/{id}/status           — status flags + open errors
POST   /printers/{id}/receipt          — print a fiscal receipt
POST   /printers/{id}/invoice          — print a fiscal invoice
POST   /printers/{id}/reversalreceipt  — refund / storno
POST   /printers/{id}/zreport          — daily Z close
POST   /printers/{id}/xreport          — non-fiscal X read
GET    /printers/{id}/vat-rates        — read programmed VAT
POST   /printers/{id}/vat-rates        — program VAT (PM only)
POST   /printers/{id}/plu/sync         — bulk PLU programming
POST   /printers/{id}/datetime         — set device clock (PM only)
POST   /printers/{id}/cash             — service deposit / withdraw
GET    /printers/{id}/journal          — read electronic journal
POST   /printers/{id}/duplicate        — re-print last receipt
POST   /printers/{id}/reset            — soft reset
... and more (operators, logo, header/footer template)
```

### Other peripherals

```
/pinpads/{id}/purchase                 — Datecs Pay card charge
/scales/{id}/read                      — current weight
/displays/{id}/text                    — write to customer display
/readers/{id}/ws  /sse  /next  /last   — barcode push channels
/cameras/{id}/snapshot                 — JPEG snapshot
/access/{id}/door/open                 — Polimex door control
/mqtt/{topic}                          — publish
```

### BlueCash bridges (the new contracts)

```
POST   /devices/<serial>/shift_close                  — Android uploads closed shift
DELETE /devices/<serial>/shift_close/<day>/<z>        — admin: forget dedup
POST   /devices/<serial>/events/push                  — Odoo emits shift event
GET    /devices/<serial>/events/last                  — last event poll
GET    /devices/<serial>/events/sse                   — SSE subscription
WS     /devices/<serial>/events/ws                    — WebSocket subscription
GET    /devices/<serial>/events/stats                 — admin / debug
```

See [BlueCash PLU integration](#bluecash-plu-integration) for the
full lifecycle.

### Admin + observability

```
GET    /healthz                        — liveness probe
GET    /metrics                        — Prometheus exposition
GET    /admin/bootstrap-info           — first-boot admin token
POST   /admin/<various>                — rescue + reload endpoints
```

### Odoo IoT Box compatibility shims

A single Odoo.ErpNet.FP instance answers BOTH the Odoo 18 IoT Box
URL prefixes (`/hw_drivers`, `/hw_proxy`) and the Odoo 19 prefix
(`/iot_drivers`). Same handlers; different paths.

## Security model

### Local LAN

No auth. The proxy listens on `0.0.0.0:8001` by default. If it's on a
WAN-exposed VM, **firewall it** to LAN-only and put a reverse proxy
in front for TLS. There's no built-in user/password — that's
intentional, because the consumers (Odoo POS browser, Android BlueCash
client) all live in the same trust zone.

### Cross-trust HTTP

For any path that crosses the LAN boundary — Android client → proxy
(through Cloudflare tunnel), proxy → cloud Odoo, Odoo → proxy push —
authentication is **HMAC-SHA256** over the canonical-JSON request
body, with the secret being **`server.iot_setup.token`** (in
`config.yaml`) on the proxy side and **`ir.config_parameter
('iot_token')`** on the Odoo side. The same value goes into the
Android client's `ConnectionPreferences.apiKey`.

```
canonical_raw = json.dumps(body,
                           separators=(",", ":"),
                           sort_keys=True,
                           ensure_ascii=False).encode("utf-8")
sig = HMAC-SHA256(secret, canonical_raw).hexdigest()
header = X-Registry-Signature: <sig>
```

Both sides recompute `canonical_raw` from the parsed body — this
guarantees the signature survives any intermediary re-serialisation.

### Cloudflare quirk

Default `Python-urllib/3.x` and `httpx` user agents trip Cloudflare's
**Browser Integrity Check (error 1010)**. The proxy and the Odoo
emitter both ship a stable identifier:

```
User-Agent: Odoo.ErpNet.FP/1.0 (proxy bridge)
```

If you front the proxy with your own Cloudflare tunnel, you'll need
the same UA on any custom client (or whitelist it in Cloudflare WAF).

## BlueCash PLU integration

[BlueCash PLU Client](https://github.com/rosenvladimirov/BlueCash.PluClient)
is the new Android client for BlueCash-50 / 5000 devices in PLU
mode. The integration is fully bidirectional:

### Odoo → Android (shift signal)

When `pos.session.action_pos_session_open()` fires in Odoo, our
extension posts a `shift.open` event to every linked fiscal device:

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

The proxy's in-memory hub fans out to every connected Android
subscriber (`WS /devices/<serial>/events/ws` or `GET …/sse`).
Subscribers receive the event verbatim and act on it
(`ShiftTracker.openShift(...)` on the client side).

On session close, the proxy emits `shift.close.request` similarly —
which prompts the cashier in the Android UI to run a Z-report.

### Android → Odoo (shift sync)

After the cashier presses Z, the Android client uploads the closed
shift to the proxy:

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

The proxy:
1. Verifies the HMAC.
2. Checks its **SQLite dedup cache** keyed by `(device_serial,
   fiscal_day_number, z_report_number)`. If hit → returns cached
   Odoo response (idempotent replay).
3. Re-canonicalises + re-signs the body and forwards to
   `<iot_setup.odoo_url>/erp_net_fp/shift_close`.
4. Caches the Odoo response (successes AND deterministic failures
   like 409 Conflict).

The Odoo controller then:
1. Re-verifies the HMAC.
2. Delegates to the `l10n.bg.erp.net.fp.shift.sync` service.
3. Service looks up `pos.session` by `odoo_session_id` and the
   `fiscal.printer.device` by serial.
4. For each receipt: lookup-or-create `pos.order` keyed by
   `l10n_bg_uns` (this is the Odoo-side dedup anchor).
5. For storno receipts (`is_storno: true`): create a refund
   `pos.order` and link via `original_uns`.
6. For cash movements: post `account.bank.statement.line` rows
   under the session's cash journal with ±2 s dedup window.
7. Close the `pos.session`.

The proxy returns the controller's response verbatim.

### Two-layer idempotency

| Layer | Key | Storage |
|---|---|---|
| Proxy | `(device_serial, fiscal_day_number, z_report_number)` | SQLite WAL @ `/app/data/shift_dedup.sqlite` |
| Odoo  | `pos.order.l10n_bg_uns` | Postgres unique index |

The proxy absorbs Android retry storms; Odoo catches the case where
the proxy SQLite was wiped (e.g. container rebuild without volume).

## Operations

### Logs

```bash
docker logs -f odoo-erpnet-fp
# Per-driver log levels in config.yaml:
# logging:
#   level: INFO
#   loggers:
#     odoo_erpnet_fp.drivers.datecs_pm: DEBUG
```

### Health & metrics

```bash
curl http://localhost:8001/healthz
curl http://localhost:8001/metrics    # Prometheus format
```

Standard metrics: request rate / latency / errors per route; per-device
status check counts; PM driver frame round-trip latency histogram.

### Hot-patch in dev

```bash
# Container ships the code in TWO places. Patch BOTH.
docker cp my.py odoo-erpnet-fp:/app/odoo_erpnet_fp/.../my.py
docker cp my.py odoo-erpnet-fp:/usr/local/lib/python3.12/site-packages/odoo_erpnet_fp/.../my.py
docker exec odoo-erpnet-fp find / -name __pycache__ -type d -exec rm -rf {} +
docker restart odoo-erpnet-fp
```

For permanent changes, rebuild the image (`docker compose build`).

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ERR_R_PLU_VAT_DISABLE` on Datecs PM | Device in PLU-only mode (FP-700MX); supply `pluNumber` in the receipt item, not `taxGroup` |
| `Forbidden VAT` on every group | Same as above; not a VAT-config problem |
| `End of paper` won't clear after reload | Power-cycle the device; some firmware locks until X-report |
| All payments print as `В БРОЙ` | Payment slot mapping needs verification on this firmware variant |
| `X-Registry-Signature mismatch` | `iot_setup.token` (proxy) ≠ `iot_token` ICP (Odoo); or canonicalisation drift (proxy log shows raw bytes for comparison) |
| HTTP 1010 from Cloudflare | Missing User-Agent; expected if calling the controller manually with `curl --user-agent ""` |
| Container can't see USB device | Pass `--device /dev/...` and check `usb-modeswitch` for `cdc-acm` devices |

## Development

```bash
git clone https://github.com/rosenvladimirov/Odoo.ErpNet.FP
cd Odoo.ErpNet.FP
pip install -e '.[dev]'
pytest                           # unit tests
ruff check .                     # lint
mypy odoo_erpnet_fp              # type check
```

Project layout:

```
odoo_erpnet_fp/
  server/                 — FastAPI app, config loader, registry
    routes/               — one HTTP module per device class
    adapters/             — payment_type / tax_group / messages
    iot_setup.py          — pair with Odoo, register devices
    registry.py           — Fleet enrolment (optional)
    shift_dedup.py        — SQLite idempotency cache
    odoo_forwarder.py     — HMAC-signed POST helper
  drivers/                — device-class drivers
    fiscal/datecs_pm/     — PM v2.11.4 protocol
    fiscal/datecs_isl/    — ISL family (Datecs + Daisy + Eltrade etc.)
    scales/               — CAS, Toledo, generic, OHAUS, etc.
    displays/             — Datecs, ESC/POS
    pinpad/               — Datecs Pay (NDA shims)
    barcodes/             — hid2serial integration
    cameras/              — RTSP / ONVIF
    access/               — Polimex iCON
  config/                 — runtime config + secrets (gitignored)
  packaging/              — debian, systemd, win installer
  tools/                  — operational scripts
  tests/                  — pytest suite
```

### Contributing

PRs welcome. Constraints:

- Code style: ruff + black (PEP 8 with 80-char target).
- Type hints encouraged but not enforced.
- New device drivers MUST include a `tests/` smoke test that uses
  the fixture transport (no real hardware required for CI).
- All user-facing strings stay in **English**; translation goes
  into `.po` files. Source comments may be Bulgarian.
- Don't push to OCA upstream — this repo is the canonical source.

## License + NDA

**LGPL-3.0-or-later.** Author: Rosen Vladimirov
([@rosenvladimirov](https://github.com/rosenvladimirov),
<vladimirov.rosen@gmail.com>).

### NDA caveats

The following modules contain protocol logic protected by NDA with the
respective vendors. The Python sources here are thin shims around
externally-distributed `.so` libraries — the actual protocol logic is
NOT in this repository, and the `.so` files are NOT redistributed:

- `drivers/pinpad/datecs_pay/` — Datecs Pay (BluePad-50, BlueCash-50)
- `drivers/pinpad/bluepad55/`  — BluePad-55 BLE bridge

If you need to develop against these, you must obtain the SDK
directly from Datecs and the corresponding NDA. The shims in this
repo will gracefully no-op if the `.so` is missing.

### Acknowledgements

- Original [ErpNet.FP](https://github.com/erpnetbg/ErpNet.FP) project
  by ERP.NET — for the HTTP protocol design.
- Odoo IoT Box drivers — for the ISL-family base implementations
  that this project's `datecs_isl/` package ports to Python.
- The Bulgarian fiscal-printer manufacturers (Datecs, Daisy, Tremol,
  Eltrade, Incotex) — for tolerating my reverse-engineering.
