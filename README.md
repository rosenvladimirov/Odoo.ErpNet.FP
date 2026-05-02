# Odoo.ErpNet.FP

**Python drop-in replacement for [ErpNet.FP](https://github.com/rosenvladimirov/ErpNet.FP) HTTP fiscal-printer server, oriented for the Bulgarian POS market.**

The Net.FP HTTP protocol is implemented 1:1 — existing clients (e.g. the
`l10n_bg_erp_net_fp` Odoo addon) work without changes. Underneath, the
device drivers are pure-Python ports of the Odoo IoT box drivers plus a
new driver for the Datecs PM v2.11.4 protocol (FP-700 MX series), which
neither ErpNet.FP nor IoT box covers yet.

## Status

| Phase | Scope | Status |
|---|---|---|
| **1 (MVP)** | Server skeleton + Datecs PM v2.11.4 + ErpNet.FP API | **Alpha** |
| 2 | Datecs ISL port (DP-25, FP-700X, FP-2000, FP-800) | TBD |
| 3 | Daisy / Tremol / Eltrade / Incotex fiscal printers | TBD |
| 4 | DatecsPay pinpad terminals (BluePad-50, BlueCash-50) | TBD |
| 5 | Scales (Adam / Mettler / Ohaus) + barcode readers | TBD |

## Why not just use ErpNet.FP?

- ErpNet.FP is C# / .NET — no native Linux ARM build for IoT boxes
- Datecs PM v2.11.4 (FP-700 **MX** series) is **not implemented** in ErpNet.FP
- Pure-Python lets us reuse the Odoo IoT box driver code (which is also
  Python) instead of porting it to C#
- One service hosts every BG vendor + extension peripherals (pinpads,
  scales, readers) instead of a per-vendor fragmented stack

## Quick start

### Option A — Docker stack (Traefik + proxy, drop-in replacement for ErpNet.FP)

The bundled `docker-compose.yml` mirrors the original ErpNet.FP setup
1:1 — Traefik routing the same hostnames (`erpnet-fp.odoo-shell.space`
/ `erpnet.local` / `localhost`), HTTPS only, CORS middleware. Existing
Odoo addons (`l10n_bg_erp_net_fp`) work without changing the configured
host URL — only the container behind it changes.

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

# Run — starts Traefik (:443) + proxy (:8001 internal)
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

### Migrating from the C# ErpNet.FP container

If you already run `~/Проекти/ErpNet.FP/docker-compose.yml` (Traefik +
the C# `erpnet-fp` container), the migration is a 1-step swap:

```bash
# 1. Stop the old C# stack
cd ~/Проекти/ErpNet.FP && docker compose down

# 2. Bring up our Python stack — same Traefik routes, same hostnames,
#    same TLS, same CORS — Odoo clients keep working unchanged.
cd ~/Проекти/odoo/iot/Odoo.ErpNet.FP
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
- **Step-CA** — point at the IoT box's Step-CA (see
  [`odoo-iot-docker`](https://github.com/odoo-iot-docker) reference
  setup) and use `step certificate` to issue
- **Self-signed (testing only)** — `openssl req -x509 -newkey rsa:2048 -nodes -keyout origin-key.pem -out origin-cert.pem -days 365 -subj "/CN=localhost"`

## Configuration

Two formats are accepted, picked by file extension:

| File | Format | TLS support | Use case |
|---|---|---|---|
| `config.yaml` | Native YAML | ✓ | New deployments |
| `configuration.json` | ErpNet.FP-compatible | ✗ (use reverse proxy) | Migration from ErpNet.FP |

See `config-examples/` for fully-commented examples.

## API compatibility

100% protocol parity with ErpNet.FP. The following endpoints are
implemented per [PROTOCOL.md](https://github.com/rosenvladimirov/ErpNet.FP/blob/master/PROTOCOL.md):

```
GET  /printers                         — list
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
GET  /healthz                          — liveness probe (Odoo.ErpNet.FP-specific)
```

## Development

```bash
# Editable install with dev deps
pip install -e ".[dev]"

# Run tests (all 80 must pass)
pytest tests/ -v

# Lint
ruff check .

# Smoke-test the API without a real device (uses MockDevice)
pytest tests/test_server.py -v   # TBD
```

## License

LGPL-3.0-or-later. The drivers folder ports are derived from Odoo
Enterprise IoT box source (LGPL-3) — same license.

## Credits

- The C# **ErpNet.FP** by [Erp.net](https://erp.net) — protocol
  reference and motivation
- Odoo Enterprise **IoT box drivers** — protocol logic for ISL, Daisy,
  Tremol, Eltrade and friends (ported here)
- **Datecs Ltd.** — published the PM v2.11.4 protocol PDF, see
  `docs/PROTOCOL_REFERENCE.md`
