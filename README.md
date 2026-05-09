# Odoo.ErpNet.FP

**Python drop-in replacement for the [C# ErpNet.FP](https://github.com/rosenvladimirov/ErpNet.FP) HTTP fiscal-printer server, oriented for the Bulgarian POS / retail / SMB market.**

The Net.FP HTTP protocol is implemented 1:1 — existing clients (e.g. the
`l10n_bg_erp_net_fp` Odoo addon) work without changes. Underneath, the
device drivers are pure-Python ports of the Odoo IoT box drivers plus a
new driver for the Datecs PM v2.11.4 protocol (FP-700 MX series), which
neither the original ErpNet.FP nor the Odoo IoT box covers, plus
extended drivers for customer displays, scales, barcode readers and
pinpads — turning a single ErpNet.FP instance into a one-stop POS-side
device hub.

> **Current version: 0.5.4** · Production/Beta · Docker image
> `vladimirovrosen/odoo-erpnet-fp:0.5.4` (also tagged `:latest`).

## Hardware coverage

| Class | Drivers | Status |
|---|---|---|
| **Fiscal printers** | Datecs PM (FP-700 MX), Datecs ISL (DP-25, DP-150, DP-150X, FP-700X, FP-2000, FP-800, FMP-350X, FMP-55X) | ✅ Production |
| **Customer displays** | Datecs DPD-201; ESC/POS-compatible (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000) | ✅ Production |
| **Scales** | CAS PR-II / PD-II + Elicom EVL CASH47 + Datecs CAS-compat (~75% of БГ retail), Toledo 8217, generic ASCII continuous (ACS / JCS / no-name OEM), OHAUS Ranger SICS over TCP | ✅ Production |
| **Barcode readers** | USB HID + serial CDC + BLE — through the [`hid2serial`](https://github.com/rosenvladimirov/hid2serial) sister daemon (Linux .deb 0.1.7 production-ready; Windows 1.0.0 driver via [`hid2vsp`](https://github.com/rosenvladimirov/hid2serial/tree/main/driver/hid2vsp)) | ✅ Production |
| **Pinpads** | Datecs Pay (BluePad-50, BlueCash-50; NDA-locked) | ✅ Production |
| **Pinpads pending** | myPOS, Borica | 🚧 v0.6 backlog |
| **Fiscal pending** | Tremol, Eltrade, Incotex (stubs exist) | 🚧 v0.6 backlog |

## Why not just use the C# ErpNet.FP?

- ErpNet.FP is C# / .NET — no native Linux ARM build for IoT boxes; deployment outside Windows is a fight
- Datecs PM v2.11.4 (FP-700 MX series) is **not implemented** in ErpNet.FP at all
- ISL PLU programming (CMD 0x4B) — ErpNet.FP delegates to manual operator input; we ship an HTTP endpoint
- Pure-Python lets us reuse the Odoo IoT box driver code (also Python) instead of porting it to C#
- One service hosts every БГ vendor + extension peripherals (pinpads, scales, displays, readers) instead of a per-vendor fragmented stack
- Native Odoo IoT Box compatibility — single instance serves Odoo 18 (`/hw_drivers/*`) and Odoo 19+ (`/iot_drivers/*`) clients side-by-side

## Quick start

### Option A — Docker stack (Traefik + proxy, drop-in replacement)

The bundled `docker-compose.yml` mirrors the original ErpNet.FP setup
1:1 — Traefik routing the same hostnames (`erpnet-fp.odoo-shell.space`
/ `erpnet.local` / `localhost`), HTTPS only, CORS + Private Network
Access middleware. Existing Odoo addons (`l10n_bg_erp_net_fp`) work
without changing the configured host URL — only the container behind
it changes.

```bash
git clone https://github.com/rosenvladimirov/Odoo.ErpNet.FP
cd Odoo.ErpNet.FP

# Configure
mkdir -p config certs
cp config-examples/config.yaml config/config.yaml
$EDITOR config/config.yaml

# Drop your TLS cert/key into ./certs (Cloudflare Origin / Let's Encrypt /
# self-signed). Same files as the original ErpNet.FP stack.
cp /path/to/cert.pem certs/cert.pem
cp /path/to/key.pem  certs/key.pem

# Run — starts Traefik (:443) + proxy (:8001 internal) +
# Prometheus + Grafana
docker compose up -d

# Test (Traefik at :443 routes to the proxy)
curl --insecure https://localhost/printers
curl https://erpnet-fp.odoo-shell.space/printers   # if DNS points here
```

### Option B — Native install (no Docker)

```bash
pip install .
odoo-erpnet-fp --config /path/to/config.yaml
```

### Option C — Pre-built Docker image

```bash
docker pull vladimirovrosen/odoo-erpnet-fp:latest
docker run -d --name erpnet-fp \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/certs:/app/certs \
  -p 8001:8001 \
  vladimirovrosen/odoo-erpnet-fp:latest
```

### Migrating from the C# ErpNet.FP container

Same Traefik routes, same hostnames, same TLS, same CORS — Odoo
clients keep working unchanged:

```bash
cd ~/Проекти/ErpNet.FP && docker compose down       # stop the C# stack
cd ~/Проекти/odoo/iot/Odoo.ErpNet.FP                # bring up the Python one
docker compose up -d
```

The only thing that changes server-side is the container handling
incoming requests on :8001; client-side nothing changes.

## HTTPS / TLS

The server accepts any PEM-encoded cert/key pair via the `tls` block in
`config.yaml`:

```yaml
server:
  port: 8443
  tls:
    enabled: true
    certfile: /app/certs/origin-cert.pem
    keyfile:  /app/certs/origin-key.pem
```

### Cloudflare Origin Certificate (recommended for cloud deployments)

1. Cloudflare dashboard → SSL/TLS → **Origin Server**
2. Click **Create Certificate**, accept defaults (RSA 2048, 15 years)
3. Save the **certificate** as `certs/origin-cert.pem` and the
   **private key** as `certs/origin-key.pem`
4. In `config.yaml` set `tls.enabled: true` and the paths above
5. (Optional) For mTLS — set `ca_certs` to Cloudflare's Origin CA bundle
   and `require_client_cert: true` so only Cloudflare can talk to the
   service

The 15-year validity makes this the lowest-maintenance option for shop
deployments. **No browser warnings** when the request originates from
Cloudflare's proxy network — the `Cf-Connecting-IP` header carries the
real client.

### Let's Encrypt wildcard via Cloudflare DNS-01 (browser-trusted, auto-renew)

Best for in-shop installs that sit behind NAT — no inbound port needed,
no manual cert acceptance per device. The bundled Traefik stack
auto-issues + auto-renews `*.lan.mcpworks.net` from Let's Encrypt.

See [`docs/TLS_CLOUDFLARE.md`](docs/TLS_CLOUDFLARE.md) for the full
setup (token scope, `.env` layout, `acme.json` storage, troubleshooting,
on-prem vs centralized cert distribution models).

### Other PEM sources

- **Let's Encrypt (HTTP-01 / external)** — provision externally with
  `certbot`/`Caddy` and copy the `fullchain.pem` + `privkey.pem` into
  `certs/`
- **Step-CA** — point at the IoT box's Step-CA and use
  `step certificate` to issue
- **Self-signed (testing only)** — `openssl req -x509 -newkey rsa:2048 -nodes -keyout origin-key.pem -out origin-cert.pem -days 365 -subj "/CN=localhost"`

## Configuration

Two formats are accepted, picked by file extension:

| File | Format | TLS support | Use case |
|---|---|---|---|
| `config.yaml` | Native YAML | ✓ | New deployments |
| `configuration.json` | ErpNet.FP-compatible | ✗ (use reverse proxy) | Migration from ErpNet.FP |

See `config-examples/` for fully-commented examples.

## API surface

100% protocol parity with the original C# ErpNet.FP, plus a sizable
set of new endpoints we use that the C# server does not implement.
Everything is JSON in / JSON out unless noted.

### Fiscal printers — full ErpNet.FP compatibility

```
GET  /printers                         — list devices
GET  /printers/{id}                    — info
GET  /printers/{id}/status             — fiscal status
GET  /printers/{id}/cash               — cash in safe
POST /printers/{id}/receipt            — print fiscal receipt
POST /printers/{id}/reversalreceipt    — print storno
POST /printers/{id}/withdraw           — cash out (служебно изведено)
POST /printers/{id}/deposit            — cash in (служебно въведено)
POST /printers/{id}/datetime           — set device clock
POST /printers/{id}/zreport            — Z report (daily, with reset)
POST /printers/{id}/xreport            — X report (interim)
POST /printers/{id}/duplicate          — print duplicate of last receipt
POST /printers/{id}/reset              — abort stuck open receipt
POST /printers/{id}/rawrequest         — raw command (advanced)
```

### Fiscal printers — Odoo.ErpNet.FP extensions

```
POST /printers/{id}/zreport-totals     — Z + parsed totals_by_group (PM)
                                          or DeviceStatus (ISL); used by
                                          POS close-shift reconcile
POST /printers/{id}/plu/sync           — bulk PLU programming
                                          (PM + ISL CMD 0x4B);
                                          payload {items:[{plu, name,
                                          price, vat_group, ...}]}
GET  /printers/{id}/vat-rates          — read programmed VAT rates
POST /printers/{id}/vat-rates          — program VAT rate per group
                                          (Datecs PM cmd 0x53)
POST /printers/{id}/operators          — program operator credentials
POST /printers/{id}/template           — header / footer text (PM only
                                          today; ISL on the v0.6 backlog)
POST /printers/{id}/logo               — upload customer logo bitmap
```

### Customer displays · scales · barcode readers

```
GET  /displays                         — list
POST /displays/{id}/{action}           — 13 actions (text, clock, idle,
                                          totalize, item-line, …) —
                                          covers DPD-201 + ESC/POS clones

GET  /scales                           — list
GET  /scales/{id}/weight               — last reading
GET  /scales/{id}/stream               — Server-Sent Events live stream
POST /scales/{id}/zero                 — zero/tare ops where supported

GET  /readers                          — list
GET  /readers/{id}/last                — last barcode (with timestamp)
GET  /readers/{id}/stream              — SSE live stream of scans
POST /readers/{id}/inject              — inject a synthetic scan
                                          (development / fallback)
POST /readers/{id}/reset               — re-enumerate (BLE recovery)
```

### Pinpads (DatecsPay)

```
GET  /pinpads                          — list
POST /pinpads/{id}/{action}            — payment / void / refund / status
```

### Native Odoo IoT Box compatibility

```
POST /hw_drivers/event                 — long-poll bus for Odoo 18
POST /hw_drivers/action                — execute device action (Odoo 18)
POST /iot_drivers/event                — same for Odoo 19+
POST /iot_drivers/action               — same for Odoo 19+
POST /iot/setup                        — outbound proxy registration
                                          (proxy → Odoo)
```

A single ErpNet.FP instance serves both Odoo 18 and Odoo 19+ clients
simultaneously. Device identifier convention: `printer.<id>`,
`scale.<id>`, `display.<id>`, `reader.<id>`, `pinpad.<id>`.

### Admin · observability · auto-update

```
GET  /healthz                          — liveness probe
GET  /metrics                          — Prometheus exposition
                                          (request count, per-path latency
                                          histogram, per-device errors,
                                          scale reads, last-weight gauge,
                                          reader / display counters)

GET  /admin/logs                       — paginated in-memory ring buffer
                                          (5000 lines), level + contains
                                          filters
GET  /admin/logs/stream                — Server-Sent Events live tail
POST /admin/self-update                — pull docker:25-cli sidecar +
                                          `compose --force-recreate`;
                                          drives the dashboard UI modal
GET  /admin/bootstrap-info             — RFC1918-restricted, one-time
                                          claim of the auto-bootstrap
                                          admin token (returns 410 Gone
                                          after first use)
```

### Fleet management — heartbeat to Odoo

When `server.registry` is configured in `config.yaml`, the proxy
publishes once via `POST /erp_net_fp/registry/pair` (one-time pairing
token from the Odoo `l10n_bg_erp_net_fp_fleet` module) and then
heartbeats every 60 s with HMAC-SHA256-signed body containing host,
version, admin token, device list. See `ROADMAP.md` "v0.3 — Fleet
remote management" for the protocol.

## Observability

The bundled `docker-compose.yml` ships Prometheus + Grafana out of the
box, scraping the proxy every 5 seconds, 7-day retention, ~220 MB RAM
combined. The pre-loaded dashboard `erpnet-fp-overview` (provisioned
via YAML) is reachable at `http://localhost:3001` (or
`https://grafana.lan.mcpworks.net` over Traefik) with anonymous
Viewer access. The Odoo addon `l10n_bg_erp_net_fp` carries an
embedded view of this same dashboard inside Odoo.

The bundled stack is fully optional — `/metrics` is exposed regardless
and any external Prometheus / observability stack can scrape it.

## Development

```bash
# Editable install with dev deps
pip install -e ".[dev]"

# Run tests (currently 173 unit tests; passes on Python 3.12)
pytest tests/ -v

# Lint
ruff check .

# Smoke test against the live API without a real device (uses MockDevice
# fixtures — see tests/conftest.py)
pytest tests/test_server.py -v
```

## License

LGPL-3.0-or-later. The drivers folder ports are derived from Odoo
Enterprise IoT box source (LGPL-3) — same license. The Datecs PM
driver is a clean-room port from the published Datecs PM v2.11.4
PDF, see `docs/PROTOCOL_REFERENCE.md`.

## Related projects

- [`hid2serial`](https://github.com/rosenvladimirov/hid2serial) —
  sister daemon that turns USB / BLE HID barcode scanners into virtual
  serial ports. Linux .deb production, Windows via the
  [`hid2vsp`](https://github.com/rosenvladimirov/hid2serial/tree/main/driver/hid2vsp)
  UMDF v2 paired-COM driver (1.0.0).
- [`l10n-bulgaria`](https://github.com/rosenvladimirov/l10n-bulgaria) —
  Odoo 18 / 19 addons that drive ErpNet.FP from the POS UI; full
  external POS mode orchestrator (PLU sync, VAT push, operators,
  logo, headers, X-report, Z-report close + reconcile).

## Credits

- The C# **ErpNet.FP** by [Erp.net](https://erp.net) — protocol
  reference and motivation
- Odoo Enterprise **IoT box drivers** — protocol logic for ISL, Daisy,
  Tremol, Eltrade and friends (ported here)
- **Datecs Ltd.** — published the PM v2.11.4 protocol PDF, see
  `docs/PROTOCOL_REFERENCE.md`
