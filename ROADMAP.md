# ErpNet.FP — Roadmap to v1.0

> Last updated: 2026-05-06 (evening) · Current version: **0.2.3**

A pure-Python drop-in replacement for the C# ErpNet.FP fiscal-printer
HTTP server, oriented at the Bulgarian POS / retail market. Adds
native Odoo IoT Box compatibility, customer-display drivers, scale
drivers, packaging-weight QC, and a sister daemon (`hid2serial`) that
turns USB / BLE HID barcode scanners into virtual serial ports for
clean integration.

---

## Where we are (v0.2.3)

### Hardware drivers
- ✅ **Datecs PM** (FP-700 MX series)
- ✅ **Datecs ISL** (DP-25, DP-150, DP-150X, FP-700X, FP-2000, etc.)
- ✅ **Customer displays**: Datecs DPD-201 + ESC/POS-compatible clones (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000)
- ✅ **Scales**: CAS PR-II / PD-II + Elicom EVL CASH47 + Datecs CAS-compat (~75% of BG retail), Toledo 8217 industrial, generic ASCII continuous (ACS / JCS / no-name OEM)
- ✅ **HID barcode scanners**: USB HID + serial CDC + BLE — through `hid2serial` v0.1.6 sister daemon (production-ready Linux .deb, 23 MB Windows installer in v0.2.0-dev)
- ✅ **Pinpad**: Datecs Pay (NDA-locked)

### Integration
- ✅ Native Odoo IoT Box compatibility — `/hw_drivers/{action,event}` (Odoo 18) + `/iot_drivers/{action,event}` (Odoo 19+); single instance serves both
- ✅ `l10n_bg_erp_net_fp` v18 module → `iot.box` + `iot.device` + Phase 3 packaging weight QC; LIVE on dev-18 (18.0.10.0.4)
- ✅ Dashboard with version badge + 5 tabbed device sections + reader running indicator + auto-refresh
- ✅ **End-to-end POS test passed** (Teemi TMSL-55 BLE → hid2serial → ErpNet.FP → dev-18 Odoo POS, 2026-05-06)

### What's still missing for v1.0

- Hardware coverage gaps: **Borica** / **myPOS** pinpads, **Posiflex PD-2600/2800** displays, **Datecs ETS** / **Elicom EPS** label-printing scales
- Fiscal printers: **Tremol** / **Eltrade** / **Incotex** — stubs exist, need full driver
- Production: no auth beyond Bearer, no `/metrics` endpoint, no persistent device state, no audit log rotation
- Quality: minimal test coverage, no CI, no hardware-in-loop testing
- Distribution: no signed Docker images, no Helm chart, no public release announcement
- Windows: code-complete (`hid2serial` v0.2.0-dev) but **awaiting real-hardware test** with com0com + scanner
- l10n_bg_erp_net_fp v19 deploy — pending `l10n_bg_*` schema repair on dev-19

---

## Roadmap

### ~~v0.3 — Hardware completeness~~ → split

The original v0.3 was scoped as one milestone. Today's session shipped
**half of it** (HID scanner integration + auto-detect + hot-reload on
disconnect). The remaining half stays as v0.3 below.

### v0.3 — Remaining hardware (~2 weeks, awaiting client hardware)

**Goal:** cover the last 5% of the BG market.

- **Pinpad drivers**: Borica + myPOS terminal (no NDA blocker; spec is public)
- **Customer-display driver**: Posiflex PD-2600 / PD-2800 native protocol
- **Scale drivers**: Datecs ETS / Elicom EPS label-printing (with PLU sync from Odoo product list) — required for butcher / deli clients
- **Fiscal printer drivers**: finish Tremol / Eltrade / Incotex
- l10n_bg_erp_net_fp 19.0.10.0.x — repair `l10n_bg_*` schema gaps on dev-19, deploy
- Discovery wizard: browser-side fetch fallback for LAN-only setups (currently does server-side request, fails when proxy hostname is local-only)

### v0.4 — Production hardening (~2 weeks)

**Goal:** safe to deploy at any client without babysitting.

- HTTPS auth: Bearer / mTLS / IP allow-list, picked per server in config.yaml
- Per-device health monitoring — probe every 30 s, alert on webhook if a device stays down longer than 5 min
- `/metrics` Prometheus endpoint — request count, latency p50/p95/p99, per-device error rate, queue depth
- Persistent device state — JSON cache for last weight, last scan, last receipt; survives restart
- Webhook delivery with retry queue + dead-letter queue
- Audit log for all outgoing fiscal commands — JSONL, rotate daily, optional remote shipment
- Windows hardware test with `hid2serial` 0.2.0 → release as 0.2.0 stable

### v0.5 — Odoo integration depth (~2 weeks)

**Goal:** zero-config experience for Odoo POS clients.

- Auto-discovery push to Odoo — proxy POSTs to Odoo `/web/dataset/call_kw/iot.device/auto_create` so admins don't even click the discovery wizard
- PLU sync wizards for label-printing scales — one-click "Sync products → scale memory" with the 28-prefix weight-barcode format (`28XXXXXWWWWWC`)
- Multi-tenant ErpNet.FP — one instance, many clients with separate config namespaces and isolated audit logs
- Browser-side discovery — wizard option to issue fetches from the user's browser (already in iot.box.connection_mode = `proxy` design — wire it to the wizard)

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
- Branded icons (placeholder green-square in v0.2.x) + Authenticode-signed Windows installer

---

## hid2serial sister project

| Version | Date | Highlights |
|---|---|---|
| 0.1.0 | 2026-05-06 | Linux daemon (evdev + pty), CLI |
| 0.1.3 | 2026-05-06 | Tray (GTK3 + AyatanaAppIndicator, Wayland-OK), .deb packaging, polkit rule, generic any-external match |
| 0.1.5 | 2026-05-06 | Reader watchdog — exits non-zero when readers die, systemd respawns to recover from BLE sleep |
| 0.1.6 | 2026-05-06 | Pty slave-fd race fix — consumer no longer hits "device reports readiness but no data" |
| **0.2.0-dev** | 2026-05-06 | Windows backend: RawInput + WH_KEYBOARD_LL hook + com0com bridge + pywin32 service + pystray tray; Docker-based NSIS installer (23 MB self-contained setup.exe). Code-complete, awaiting hardware test. |

---

## Optional / post-1.0

- **GUI config tool** (Tauri or Electron) — for non-technical operators on Linux/Windows
- **Cluster mode** — 2+ ErpNet.FP instances behind a load balancer with shared device state
- **Cloud-managed mode** — central registry + remote config push (SaaS-style deployments)
- **Plugin SDK** — third-party driver development without forking the proxy

---

## Schedule (revised)

| Milestone | Estimate | Cumulative |
|---|---|---|
| ~~v0.2.3~~ | shipped today | — |
| v0.3 | 2 weeks (waits on hardware) | week 2 |
| v0.4 | 2 weeks | week 4 |
| v0.5 | 2 weeks | week 6 |
| v0.6 | 1-2 weeks | week 8 |
| v1.0 release | 1 week | **week 9** |

**Revised target:** **~early July 2026**, faster than original (we picked up time today with HID scanner work and end-to-end test).

---

## Open decisions (still relevant)

1. **v0.3 hardware coverage scope** — full list above, or tight subset (Borica/myPOS + label-print scales only)?
2. **Auth flavour for v0.4** — Bearer only (fast), mTLS (serious), or both?
3. **Hardware-in-loop CI for v1.0** — yes (slower but trustworthy) or no (relies on manual smoke tests)?
4. **Docs language priority** — BG first (clients in our pipeline) or EN first (community visibility)?
5. **License review** — keep LGPL-3, or split into LGPL-3 core + OPL-1 premium add-ons (cluster, cloud-managed)?

---

## Today's session deliverables (2026-05-06)

- **9 commits** on `Odoo.ErpNet.FP` (dashboard tabs, version badge, IoT compat, displays, scale drivers, last-scan endpoints, etc.)
- **8 tags** on `hid2serial` (v0.1.0 → v0.1.6, plus v0.2.0-dev)
- **4 module bumps** on `l10n_bg_erp_net_fp` (18.0.9.0.0 → 18.0.10.0.4, mirror in v19)
- **deb package** built + tested (`hid2serial_0.1.6_all.deb`)
- **Windows installer** built via Docker (`hid2serial-0.2.0-dev-setup.exe`)
- **dev-18 deployment** — module installed + upgraded with all dependencies
- **End-to-end POS test PASSED** with real Teemi BLE scanner

---

*Authored by Rosen Vladimirov · Assisted by Claude Code*
