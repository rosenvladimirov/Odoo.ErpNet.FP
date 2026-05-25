# Odoo.ErpNet.FP — Roadmap

> **Last updated:** 2026-05-25 · **Current version:** `0.13.6` · **Target v1.0:** Q3 2026
>
> 🇧🇬 На български: **[ROADMAP.bg.md](ROADMAP.bg.md)**

---

## Where we are (May 2026)

After 13 versions and 12 months of compounding work, the proxy is
**production-ready** for the core BG retail use case:

| Capability | Status |
|---|:-:|
| Datecs PM + ISL fiscal drivers (FP-700MX, BC-50MX, DP-150, DP-150X, FP-700X, FMP-350X, ...) | ✅ |
| 6-slot payment mapping empirically verified on real hardware | ✅ |
| PLU programming, X/Z reports, VAT programming | ✅ |
| Customer displays, scales, barcode readers (HID/BLE), pinpads | ✅ |
| Cameras + Polimex iCON access control + MQTT bridge | ✅ |
| Odoo IoT Box compatibility (v18 + v19, single instance) | ✅ |
| Fleet remote management (HMAC-signed heartbeat) | ✅ |
| Prometheus metrics + Grafana dashboards | ✅ |
| **BlueCash PLU Android client bridges** (shift_close + shift_signal) | ✅ NEW |
| **Bilingual EN/BG documentation** | ✅ NEW |

What's left for **v1.0**:
- Last 5–10 % of hardware coverage (pinpads, label-printing scales,
  Tremol/Eltrade/Incotex drivers)
- BlueCashPos backend integration (vendor POS as thin-client to Odoo)
- Hardening: signed images, public release, Helm chart

For a per-version timeline of what shipped already, scroll to
[Shipped in the past](#shipped-in-the-past).

## Near-term: 2026-Q2 (June)

### Active — BlueCash PLU integration (Phase 1)

**Status:** code-complete on both proxy + Odoo (`19.0.15.8.0`),
**deployed to live dev** on `www.odoo-shell.space`, awaiting Android-
side completion.

Remaining work:
- [ ] **Android `BlueCash.PluClient` repo** — `WS` subscriber listener
      (`/devices/<serial>/events/ws`); sync runner that uploads
      `pendingSync()` shifts via `POST /shift_close` (~150 LOC Kotlin).
- [ ] **Public-reachable proxy URL** for cloud Odoo → proxy push
      direction (currently `erpnet.lan.mcpworks.net` is LAN-only DNS).
      Options: Cloudflare tunnel on the proxy, or per-tenant
      `iot.box.erpnet_fp_url` override.

### Active — BlueCashPos backend (Path A)

Datecs ships a vendor POS app `BlueCashPos` on the BlueCash-50 PLU
device. Its fiscal API points at `127.0.0.1:8086/api/v3/*`.

- [ ] **Listener on `:8086`** for the 32 vendor-API endpoints.
      Minimum-viable scope: intercept `fiscalreceipt`,
      `fiscalreceipt-invoice`, `returndocument` → push to Odoo;
      forward the other 29 untouched.
- [ ] **Vendor app patch-free deployment** — `/etc/hosts` redirect
      on the device so `BlueCashPos.apk` continues to think it's
      talking to Datecs cloud, but actually hits our proxy.

### Active — Pinpad drivers

- [ ] **myPOS** (Free Partner Program submitted 2026-05-09;
      sandbox access pending). BluePad-50/55 hardware = Datecs;
      expect code reuse with the existing DatecsPay shim.
- [ ] **Borica direct TID protocol** — one driver covers ALL
      bank-issued terminals in BG (DSK, UBB, KBC, FiBank, Postbank,
      Allianz). Integrator registration with Borica needed.

## Near-term: 2026-Q3 (July–September)

### BlueCash storno Phase 2 — line-level refund

- [ ] Anchor [`anchor-bluecash-storno-phase2-contract`] —
      `GET /pos.order/<id>/fiscal_receipt` on proxy for cross-device
      storno lookup. Phase 2 contract carries per-line refund detail
      (current Phase 1 is header-level only).

### Remaining fiscal drivers

- [ ] **Tremol** full PM driver (stub exists, needs real-hardware test)
- [ ] **Eltrade** full PM driver (stub exists)
- [ ] **Incotex** full PM driver (stub exists)

### Remaining peripherals

- [ ] **SumUp / Stripe Terminal** — EU mobile readers for SMB segment
- [ ] **Posiflex PD-2600 / PD-2800** customer displays
- [ ] **Datecs ETS / Elicom EPS** label-printing scales — required
      for butcher / deli clients (with PLU sync from Odoo)

### Access control — Phase C (biometric)

`drivers/biometric/` — thin client to the external Node face-auth
microservice (separate Rosen-owned project). Liveness + anti-spoof
enforced **server-side in Odoo**, not on the kiosk. Configuration-
gated; pure-fiscal deployments unaffected.

## Mid-term: 2026-Q4 (October–December)

### v1.0 release readiness

- [ ] **Signed Docker images** (cosign), reproducible builds.
- [ ] **Stable tags** `:1.0`, `:stable`, `:latest`.
- [ ] **GitHub release** with changelog, signed checksums.
- [ ] **Helm chart** for Kubernetes deployments.
- [ ] **Authenticode-signed** Windows installer.
- [ ] **Public announcement** — Odoo forum, LinkedIn,
      `awesome-odoo` PR.

### Quality + tooling

- [ ] pytest coverage for every driver (mock pyserial via
      `pyserial.tools.testing`).
- [ ] Hardware-in-loop CI — GitHub Actions + Raspberry Pi runner
      with real DP-150 + scale on the bench.
- [ ] Integration tests: virtual fiscal printer + virtual scale +
      real Odoo POS in CI.

### Docs

- [ ] EN technical guide + driver-extension tutorial.
- [ ] BG admin docs — per-vendor cheat-sheets, common errors,
      `config.yaml` reference.

## Longer-term: post-v1.0

### MES / factory integration

> Real customer use case — ErpNet.FP proxy as bridge between MES
> devices (PLCs, sensors, scales) and Odoo via an **external** broker
> (RabbitMQ + `rabbitmq_mqtt`, Mosquitto, HiveMQ — customer's choice).
> Proxy and Odoo are clients only.

- [ ] **MQTT client bridge** (`server/mqtt_bridge.py`, `aiomqtt`) —
      already partly in place via the `/mqtt` route group; needs
      production hardening.
- [ ] **AMQP client + Odoo consumer** (`l10n_bg_erp_net_fp_amqp`).
- [ ] **CFX wire format** — IPC-CFX (Apache-2.0, industry standard
      for SMT/electronics manufacturing); Rosen has a dormant
      `ipc-cfx` Python lib that can be activated as the canonical
      envelope, giving zero-config compat with Yamaha, ASM/SIPLACE,
      Panasonic, Mycronic, etc.

### BlueCashPos backend (Path B — synthetic catalog)

Far harder than Path A: hijack the vendor's `dbgen.datecs.bg`
catalog endpoint and serve a synthetic AES-encrypted ZIP straight
from Odoo. Requires reverse-engineering the AES key from the
obfuscated Android class `d0/t0.java`. NDA-sensitive work, may
need vendor blessing first.

### Optional / nice-to-have

- **GUI config tool** (Tauri or Electron) — for non-technical
  operators on Linux / Windows.
- **Cluster mode** — 2+ ErpNet.FP instances behind a load balancer
  with shared device state.
- **Cloud-managed mode** — central registry + remote config push
  (SaaS-style deployments).
- **Plugin SDK** — third-party driver development without forking
  the proxy.

## Shipped in the past

A compressed log of what landed already.

| Version | Date | Highlight |
|---|---|---|
| **0.13.6** | 2026-05-25 | BlueCash shift_close + shift_signal contracts; bilingual README |
| 0.13.0 | 2026-05-22 | BluePad-55 BLE bridge (NDA-safe GATT↔PTY) |
| 0.12.0 | 2026-05-18 | DP-150 ISL payment-letter mapping verified empirically |
| 0.11.0 | 2026-05-17 | Camera + access-control add-on (Phases A+B): Polimex iCON / Hikvision / Dahua actuators |
| 0.10.0 | 2026-05-15 | MQTT generic bridge route group |
| 0.9.0  | 2026-05-13 | Multi-device proxy (multiple printers per config) |
| 0.8.0  | 2026-05-10 | InfoPay payment-bridge integration |
| 0.7.0  | 2026-05-08 | Native Odoo IoT Box compat (v18 `/hw_drivers` + v19 `/iot_drivers`) |
| 0.6.0  | 2026-05-07 | Camera Phase A — go2rtc media hub + ALPR + ONVIF edge-ANPR |
| 0.5.0  | 2026-05-07 | OHAUS Ranger SICS scale; `read_device_info`; `vat-rates` GET/POST; `/admin/logs` + bootstrap-token |
| 0.4.0  | 2026-05-07 | `l10n_bg_erp_net_fp_fleet` split (CE-installable, EE/OCA bridges separate) |
| 0.3.0  | 2026-05-07 | Fleet remote management (HMAC heartbeat + pairing + Fernet admin token) |
| 0.2.5  | 2026-05-06 | Prometheus `/metrics` + Grafana stack + iframe embed in Odoo |
| 0.2.0  | 2026-05-06 | HID barcode scanner via `hid2serial` sister daemon (Linux .deb + Windows installer) |
| 0.1.0  | 2026-04-x  | Initial fork — Datecs PM/ISL drivers + customer displays + scales + Datecs Pay pinpad |

## Schedule (revised 2026-05-25)

| Milestone | ETA | Cumulative |
|---|---|---|
| BlueCash Phase 1 deployment + Android wiring | ~3 weeks | mid-June 2026 |
| BlueCashPos backend Path A | ~3 weeks | early July 2026 |
| myPOS + Borica pinpad drivers | ~4 weeks | early August 2026 |
| Storno Phase 2 + remaining fiscal vendors | ~3 weeks | late August 2026 |
| v1.0 stabilization + release | ~3 weeks | **mid-September 2026** |

**Revised v1.0 target:** **mid-September 2026** (slipped from earlier
"early July" — BlueCash split-flow took ~6 weeks of architecture +
implementation work which wasn't in the original plan).

## Open decisions

1. **Auth for Android → proxy in production** — keep current
   HMAC-only, or layer per-device long-lived bearer tokens issued
   via a separate `/auth/device/issue` endpoint?
2. **BlueCashPos Path B** — is the AES RE work worth it, or is
   Path A (vendor app untouched, fiscal intercept only) sufficient?
3. **Hardware-in-loop CI for v1.0** — yes (slower but trustworthy)
   or no (relies on manual smoke tests)?
4. **License review** — keep LGPL-3, or split into LGPL-3 core +
   OPL-1 premium add-ons (cluster, cloud-managed)?
5. **`anchor-bluecash-shift-signal` topology** — current dev
   deployment shows that cloud Odoo → LAN proxy push needs a
   Cloudflare tunnel on the proxy. Is that the recommended pattern,
   or should we add a poll-based fallback for the Android client
   that doesn't require ingress on the proxy?

---

*Authored by Rosen Vladimirov · Assisted by Claude Code*
