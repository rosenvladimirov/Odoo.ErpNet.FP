# Odoo.ErpNet.FP

**Python drop-in replacement for the C# ErpNet.FP fiscal-printer server,
extended into a one-stop POS-side device hub for the Bulgarian retail
and SMB market.**

> Version `0.17.0` · Production · Docker: `vladimirovrosen/odoo-erpnet-fp:0.17.0` (also `:latest`)
> Access-control image (offline ZEN): build with `--build-arg EXTRAS=zen`, tag `:0.17.0-zen`.

## Quick links

- 🇬🇧 **[README.en.md](README.en.md)** — full English documentation
- 🇧🇬 **[README.bg.md](README.bg.md)** — пълна документация на български
- 📖 [CHANGELOG.md](CHANGELOG.md) — release notes
- 🗺️ [ROADMAP.md](ROADMAP.md) — what's next
- 🚀 [INSTALL_PINPAD.md](INSTALL_PINPAD.md) — Datecs pinpad setup guide

## TL;DR

```bash
docker run -d --name odoo-erpnet-fp \
  -p 8001:8001 \
  -v ./config:/app/config \
  -v odoo-erpnet-fp-data:/app/data \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  vladimirovrosen/odoo-erpnet-fp:latest

curl http://localhost:8001/printers/dp150/status
```

Same HTTP protocol as the original C# ErpNet.FP — existing Odoo
`l10n_bg_erp_net_fp` clients work unchanged.

## Hardware coverage

| Class | Production drivers |
|---|---|
| **Fiscal printers** | Datecs PM (FP-700MX, BC-50MX), Datecs ISL (DP-25 / DP-150 / DP-150X / FP-700X / FP-2000 / FP-800 / FMP-350X / FMP-55X) |
| **Customer displays** | Datecs DPD-201; ESC/POS-compatible (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000) |
| **Scales** | CAS PR-II/PD-II, Elicom EVL CASH47, Datecs CAS-compat (≈75 % of BG retail), Toledo 8217, generic ASCII, OHAUS Ranger SICS over TCP |
| **Barcode readers** | USB HID + serial CDC + BLE — via [`hid2serial`](https://github.com/rosenvladimirov/hid2serial) sister daemon |
| **Pinpads** | Datecs Pay (BluePad-50, BluePad-55, BlueCash-50; NDA-locked) |
| **Cameras** | RTSP / ONVIF — multi-camera ingest + snapshot API |
| **Access control** | Polimex iCON115 push events + door control |
| **MQTT** | Bidirectional ingest + publish for sensor / actuator integration |

## What this thing actually does

1. **Replaces** the C# ErpNet.FP server — 1:1 HTTP protocol, no client changes
2. **Adds** Datecs PM v2.11.4 driver (FP-700MX series) — not covered by either upstream
3. **Bridges** Odoo POS to a real BG fiscal device, with full PLU programming, multi-operator support, X/Z reports, programmable VAT rates
4. **Hosts** non-fiscal POS peripherals (displays, scales, pinpads, barcode readers) on the same proxy
5. **Bridges** the new **BlueCash PLU Android client** (Phase 1 contracts: `/devices/<serial>/shift_close` + `/devices/<serial>/events/*`) to Odoo `pos.session` lifecycle

Detailed feature list, endpoint reference, architecture diagrams and
configuration guide live in the bilingual READMEs above.

## License

LGPL-3.0-or-later · Author: Rosen Vladimirov ([@rosenvladimirov](https://github.com/rosenvladimirov))

⚠️ **NDA caveat:** Datecs pinpad (DatecsPay) and Datecs BluePad-55 BLE drivers
contain protocol logic protected by NDA. The `.so` libraries are not
redistributed; the Python wrappers are thin shims around them. Source code
for the protocol layer is **not** part of this repository.
