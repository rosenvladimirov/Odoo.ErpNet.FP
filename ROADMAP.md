# ErpNet.FP — Roadmap to v1.0

> Last updated: 2026-05-06 · Current version: **0.2.2**

A pure-Python drop-in replacement for the C# ErpNet.FP fiscal-printer
HTTP server, oriented at the Bulgarian POS / retail market. Adds
native Odoo IoT Box compatibility, customer-display drivers, scale
drivers, and a packaging-weight QC integration on top of the legacy
fiscal-printer surface.

---

## Where we are (v0.2.2)

- ✅ Datecs PM driver (FP-700 MX series)
- ✅ Datecs ISL driver (DP-25, DP-150, DP-150X, FP-700X, FP-2000, etc.)
- ✅ Customer-display driver: Datecs DPD-201 + ESC/POS-compatible clones (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000)
- ✅ Scale drivers: CAS PR-II / PD-II + Elicom EVL CASH47 + Datecs CAS-compat (75% of BG retail), Toledo 8217 industrial, generic ASCII continuous (ACS / JCS / no-name OEM)
- ✅ Reader drivers: USB HID + serial CDC + BLE external (host-side listener pattern)
- ✅ Pinpad driver: Datecs Pay (NDA-locked)
- ✅ Native Odoo IoT Box compatibility — `/hw_drivers/{action,event}` (Odoo 18) + `/iot_drivers/{action,event}` (Odoo 19+); single instance serves both
- ✅ Dashboard with version badge + 5 tabbed device sections

### What's missing for v1.0

- Hardware coverage gaps: Borica/myPOS pinpads, Posiflex displays, Datecs ETS / Elicom EPS label-printing scales
- Tremol / Eltrade / Incotex fiscal printers — stubs only
- Production: no auth beyond Bearer, no metrics endpoint, no persistent device state, no audit log rotation
- Quality: minimal test coverage, no CI, no hardware-in-loop testing
- Distribution: no signed images, no Helm chart, no public release announcement

---

## Roadmap

### v0.3 — Hardware completeness (~2-3 weeks)

**Goal:** cover 95% of the BG market without per-client custom code.

- HID scanner integration spanning all common vendor families (Symbol/Zebra, Honeywell, Datalogic, Newland, Mertech) — see `memory/project_hid2serial.md` for the integration plan
- Pinpad drivers: **Borica** + **myPOS** terminal (no NDA blocker; spec is public)
- Customer-display driver: **Posiflex PD-2600 / PD-2800** native protocol
- Scale drivers: **Datecs ETS** / **Elicom EPS** native (label-printing scales with PLU sync from Odoo product list) — required for butcher / deli clients
- Fiscal printer drivers: finish **Tremol** / **Eltrade** / **Incotex** (stubs already exist)
- **Auto-detect on startup** — probe `/dev/ttyUSB*` + `/dev/input/event*` + USB IDs → suggest config.yaml block for each detected device
- **Hot-reload of config** — SIGHUP → re-scan without restart; new devices come online without dropping existing connections

### v0.4 — Production hardening (~2 weeks)

**Goal:** safe to deploy at any client without babysitting.

- HTTPS auth: Bearer / mTLS / IP allow-list, picked per server in config.yaml
- Per-device health monitoring — probe every 30s, alert on webhook if a device stays down longer than 5 min
- `/metrics` Prometheus endpoint — request count, latency p50/p95/p99, per-device error rate, queue depth
- Persistent device state — JSON cache for last weight, last scan, last receipt; survives restart
- Webhook delivery with retry queue + dead-letter queue
- Audit log for all outgoing fiscal commands — JSONL, rotate daily, optional remote shipment

### v0.5 — Odoo integration depth (~2 weeks)

**Goal:** zero-config experience for Odoo POS clients.

- Auto-discovery push to Odoo — proxy POSTs to Odoo `/web/dataset/call_kw/iot.device/auto_create` so admins don't even click the discovery wizard
- PLU sync wizards for label-printing scales — one-click "Sync products → scale memory" with the 28-prefix weight-barcode format (`28XXXXXWWWWWC`)
- POS integration testing — Odoo POS sells a weighed product → ErpNet.FP scale → receipt → fiscal — end-to-end
- Multi-tenant ErpNet.FP — one instance, many clients with separate config namespaces and isolated audit logs

### v0.6 — Quality + tooling (~1-2 weeks)

**Goal:** confidence to release v1.0.

- pytest test suite for every driver, mocking pyserial via `pyserial.tools.testing`
- Integration tests: virtual scale + virtual fiscal printer + real Odoo POS in CI
- Hardware-in-loop CI: GitHub Actions + Raspberry Pi runner with a real DP-150 + scale on the bench
- BG admin docs — installation / config.yaml reference / common errors / per-vendor cheat-sheets
- EN technical docs — developer guide + driver-extension tutorial

### v1.0 — Stable release (~1 week)

- Signed Docker images (cosign), reproducible builds
- Stable tags: `:stable` / `:1.0` / `:latest`
- GitHub release with changelog + signed checksums
- Helm chart for Kubernetes deployments
- Public announcement: Odoo forum, LinkedIn, [`awesome-odoo`](https://github.com/odoo/awesome-odoo) PR
- Final license review (currently LGPL-3.0-or-later)

---

## Optional / post-1.0

- **GUI config tool** (Tauri or Electron) — for non-technical operators
- **Cluster mode** — 2+ ErpNet.FP instances behind a load balancer with shared device state
- **Cloud-managed mode** — central registry + remote config push (SaaS-style deployments)
- **Plugin SDK** — third-party driver development without forking the proxy

---

## Schedule

| Milestone | Estimate | Cumulative |
|---|---|---|
| v0.3 | 2-3 weeks | week 3 |
| v0.4 | 2 weeks | week 5 |
| v0.5 | 2 weeks | week 7 |
| v0.6 | 1-2 weeks | week 9 |
| v1.0 release | 1 week | **week 10** |

**Target release date:** ~7-8 July 2026 (assuming we keep current pace).

---

## Open decisions

1. **v0.3 hardware coverage scope** — full list above, or tight subset (Borica/myPOS + label-print scales only)?
2. **Auth flavour for v0.4** — Bearer only (fast), mTLS (serious), or both?
3. **Hardware-in-loop CI for v1.0** — yes (slower but trustworthy) or no (relies on manual smoke tests)?
4. **Docs language priority** — BG first (clients in our pipeline) or EN first (community visibility)?
5. **License review** — keep LGPL-3, or split into LGPL-3 core + OPL-1 premium add-ons (cluster, cloud-managed)?

---

*Authored by Rosen Vladimirov · Assisted by Claude Code*
