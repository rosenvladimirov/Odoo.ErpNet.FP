# ErpNet.FP — Roadmap to v1.0

> Last updated: 2026-05-07 (evening) · Current version: **0.2.17**

A pure-Python drop-in replacement for the C# ErpNet.FP fiscal-printer
HTTP server, oriented at the Bulgarian POS / retail market. Adds
native Odoo IoT Box compatibility, customer-display drivers, scale
drivers, packaging-weight QC, and a sister daemon (`hid2serial`) that
turns USB / BLE HID barcode scanners into virtual serial ports for
clean integration.

---

## Where we are (v0.2.5)

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

### Observability (added 2026-05-06 late evening — half of v0.4 done)
- ✅ `/metrics` Prometheus endpoint — request count, per-path latency histogram, per-device error rate, scale reads, last weight gauge, reader/display counters
- ✅ Local monitoring stack — Prometheus + Grafana in `docker-compose.yml`, scrapes proxy every 5 s, 7-day retention, ~220 MB RAM, anonymous Viewer access
- ✅ Pre-loaded dashboard `erpnet-fp-overview` (provisioned via YAML)
- ✅ TLS via Traefik wildcard cert (`grafana.lan.mcpworks.net`, ACME DNS-01 through Cloudflare resolver `cf`)
- ✅ Embedded view inside Odoo — `l10n_bg_erp_net_fp` 18.0.10.1.0 + 19.0.10.1.0: new "Monitoring" menu under ErpNet.FP that opens the dashboard in a kiosk-mode iframe (URL configurable in Settings → POS → Packaging Weight QC → Embedded Grafana monitoring)

### Shipped 2026-05-07 (post-0.2.5)

- ✅ **OHAUS Ranger SICS scale driver** (TCP MT-SICS, Ethernet kit P/N 30037447) — 0.2.10
- ✅ **Datecs PM `read_device_info`** (CMD 0x7B) — populates serial / FM / firmware in `/printers` — 0.2.10
- ✅ **`/admin/self-update` endpoint + UI modal** (docker:25-cli sidecar with `compose --force-recreate`) — 0.2.11..0.2.14
- ✅ **`/printers/{id}/vat-rates`** GET (read) + POST (program) — Datecs PM cmd 0x53; eliminates need for service tech to fix "Forbidden VAT" rejections — 0.2.15
- ✅ **`/admin/logs` + `/admin/logs/stream` SSE** — in-memory ring buffer (5000 lines), level/contains filters, Server-Sent Events for live tail — 0.2.16
- ✅ **Auto-bootstrap admin token** + `/admin/bootstrap-info` (RFC1918-restricted, one-time-claim → 410 Gone) — operators no longer need `ERPNET_ADMIN_TOKEN` env var preconfigured — 0.2.17
- ✅ **l10n_bg_erp_net_fp split** — core (CE-installable) + EE bridge `l10n_bg_erp_net_fp_iot` + OCA bridge `l10n_bg_erp_net_fp_iot_oca`; **v19 deployed today** (was open in roadmap)
- ✅ **Per-cashier operator credentials** on `res.users` — sent to Datecs as operator/password per receipt
- ✅ **Cloudflare bot-rule UA workaround** — browser-like UA on backend HTTP calls (was 403'ing fp.stedy.bg behind CF)

### What's still missing for v1.0

- Hardware coverage gaps:
  - **Pinpads**: myPOS + Borica direct protocol (Tier 1 — БГ retail
    mainstream); SumUp + Stripe Terminal (Tier 2 — EU mobile);
    Worldline / Verifone / Ingenico SDK contracts (Tier 3 — enterprise,
    post-v1.0)
  - **Customer displays**: Posiflex PD-2600 / PD-2800
  - **Scales**: Datecs ETS / Elicom EPS label-printing
- Fiscal printers: **Tremol** / **Eltrade** / **Incotex** — stubs exist, need full driver
- ~~l10n_bg_erp_net_fp v19 deploy~~ — **DONE 2026-05-07**
- Persistent device state, webhook delivery retry/DLQ, audit log JSONL rotation
- Quality: minimal test coverage (173 unit tests today, no integration), no CI, no hardware-in-loop testing
- Distribution: no signed Docker images, no Helm chart, no public release announcement
- Windows: code-complete (`hid2serial` v0.2.0-dev) but **awaiting real-hardware test** with com0com + scanner

---

## Roadmap

### ~~v0.3 — Hardware completeness~~ → split

The original v0.3 was scoped as one milestone. Today's session shipped
**half of it** (HID scanner integration + auto-detect + hot-reload on
disconnect). The remaining half stays as v0.3 below.

### v0.3 — Remaining hardware (~2 weeks, awaiting client hardware)

**Goal:** cover the last 5% of the BG market.

- **Pinpad drivers**:
  - **myPOS** — BluePad-50 / BlueCash-50 (same hardware vendor as
    Datecs Pay — myPOS is a Datecs Holdings spin-off, terminals are
    literally Datecs hardware with myPOS firmware → expect code reuse
    with our existing DatecsPay driver). Free Partner Program,
    instant-approval sandbox at partners.mypos.com — application
    submitted 2026-05-09, awaiting access
  - **Borica direct protocol** — single integration covers ALL bank-
    issued terminals in BG (DSK, UBB, KBC, FiBank, Postbank, Allianz —
    Ingenico Lane / Verifone V200 / PAX A920 hardware) since Borica
    is the national card-scheme operator. Public TID protocol spec,
    integrator registration with Borica needed
  - **SumUp / Stripe Terminal** — EU mobile readers with public SDKs;
    nice-to-have for the SMB segment that uses pure-mobile checkout
- **Customer-display driver**: Posiflex PD-2600 / PD-2800 native protocol
- **Scale drivers**: Datecs ETS / Elicom EPS label-printing (with PLU sync from Odoo product list) — required for butcher / deli clients
- **Fiscal printer drivers**: finish Tremol / Eltrade / Incotex
- l10n_bg_erp_net_fp 19.0.10.0.x — repair `l10n_bg_*` schema gaps on dev-19, deploy
- Discovery wizard: browser-side fetch fallback for LAN-only setups (currently does server-side request, fails when proxy hostname is local-only)

### v0.4 — Production hardening (~2 weeks)

**Goal:** safe to deploy at any client without babysitting.

- HTTPS auth: Bearer / mTLS / IP allow-list, picked per server in config.yaml
- Per-device health monitoring — probe every 30 s, alert on webhook if a device stays down longer than 5 min
- ~~`/metrics` Prometheus endpoint~~ — **DONE in 0.2.5** (full Counter/Gauge/Histogram, bounded label cardinality)
- Persistent device state — JSON cache for last weight, last scan, last receipt; survives restart
- Webhook delivery with retry queue + dead-letter queue
- Audit log for all outgoing fiscal commands — JSONL, rotate daily, optional remote shipment
- Windows hardware test with `hid2serial` 0.2.0 → release as 0.2.0 stable

### v0.3 — Fleet remote management (SHIPPED 2026-05-07)

**Goal:** one Odoo backend (`iot.mcpworks.net`, v19 CE) controls
every deployed ErpNet.FP proxy without per-instance SSH.

**Delivered:**
- Proxy: `config.yaml` `server.registry: {enabled, url, pairing_token, secret, interval_seconds}` (default `url: https://iot.mcpworks.net`); `server/registry.py` async httpx loop — pairs once via `POST /erp_net_fp/registry/pair`, persists secret back to YAML, then heartbeats every 60 s with HMAC-SHA256 signed body containing host, version, admin_token, device list
- Odoo module **`l10n_bg_erp_net_fp_fleet`** (separate from core `l10n_bg_erp_net_fp` — minimal deps `base, mail`, deployable on dedicated CE 19 stack):
  - Model `erpnet.fp.proxy` (state machine draft → pairing → active → archived)
  - One-time pairing tokens (1-h TTL, single-use, swept by cron every 5 min)
  - Long-lived `registry_secret` (32-byte URL-safe, validates HMAC on every heartbeat)
  - `admin_token_encrypted` — Fernet AES-128-CBC ciphertext, key in `ir.config_parameter` `l10n_bg_erp_net_fp_fleet.fernet_key` (group_system only)
  - Computed `alive` (true if `last_seen` < 180 s); decoration in list view (success/danger/warning)
  - Form buttons: Generate Pairing Token / Self-update / View Logs / Program VAT (wizard) / Reset Secret / Archive
  - Two security groups: `Fleet Viewer` (read-only) + `Fleet Manager` (full)
  - Public-facing routes: `/erp_net_fp/registry/pair` + `/erp_net_fp/registry/heartbeat` (auth=public, gated by token / HMAC)
- Bumped to **0.3.0**.

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

## MES integration as broker client (AMQP + MQTT) — pre-v1.0

Real customer use case: ErpNet.FP proxy bridges MES (Manufacturing
Execution System) devices — PLCs, sensors, scales, machines on the
factory floor — to the Odoo backend through an **external** broker
(typically RabbitMQ with `rabbitmq_mqtt` plugin enabled, but any
AMQP-0-9-1 / MQTT-3.1.1 compatible broker works). ErpNet.FP and
Odoo are clients only — broker deployment, topology, users, ACLs,
TLS certs are the customer's IT operation, outside of our scope.

```
MES devices  ─MQTT─►  [external broker]  ─MQTT─►  ErpNet.FP  ─HTTP─►  Odoo
     ▲                                                ▲
     └──MQTT◄─ [external broker] ◄─MQTT─ ErpNet.FP ◄─HTTP─┘
```

**Phase 1 — MQTT client bridge** (mid-development, before v1.0):
- `odoo_erpnet_fp/server/mqtt_bridge.py` (`aiomqtt`) — connects to
  the configured broker as a regular client; subscribes to topic
  patterns from `config.yaml` `mqtt.subscriptions:[]`; per-message
  handler dispatches to `/iot/event` POST to Odoo or to internal
  proxy state
- `config.yaml` adds `mqtt:` section: `url`, `username`, `password`,
  `tls`, `subscriptions: []`, `qos`, `keepalive_seconds`
- Topic conventions (recommended; broker admin can rename):
  - MES events: `mes/<plant>/<line>/<machine>/<event>`
  - Fiscal events: `fp/<tenant>/<device-kind>/<id>/<event>`
  - Commands: `cmd/<tenant>/<target>/<command>`
- Auto-reconnect with backoff; clean session on reconnect
- Documentation: minimum broker requirements (RabbitMQ 3.13+ with
  `rabbitmq_mqtt`; HiveMQ; Mosquitto with bridge to AMQP if Odoo
  uses AMQP); zero opinion on how the customer runs it

**Phase 2 — AMQP client + Odoo consumer** (right before v1.0):
- `odoo_erpnet_fp/server/amqp.py` (`aio-pika`) — connects to the
  broker as an AMQP client; declares (idempotently) topic exchange
  bindings the customer's broker admin pre-created OR allows on-
  connect declare per `config.yaml` flag
- New Odoo module `l10n_bg_erp_net_fp_amqp` — cron-driven worker,
  manual ack, idempotency by event id, retry with backoff before
  redelivery (DLQ routing left to broker admin)
- JSON envelope: `{schema_version, ts, tenant, source_kind,
  source_id, event_type, payload}` — same format on AMQP and MQTT
  sides for handler reuse
- Sample `rabbitmqctl` / `rabbitmqadmin` snippets in docs to help
  customer admin pre-create the expected exchange + binding when
  on-connect declare is disabled

**Phase 3 — production hardening** (post-v1.0):
- TLS for both protocols (client cert paths in config)
- Circuit-breaker fallback to HTTP webhook if broker unreachable
  >5min — the existing webhook path stays as default; broker is
  opt-in
- Prometheus metrics: publish rate, retries, broker reconnects,
  ack latency

**Out of scope** (customer's IT operation, not ours):
- Broker installation, container orchestration, Helm
- User accounts, MQTT ACLs, AMQP virtual hosts
- Cluster topology, HA, mirrored queues, federation
- TLS cert issuance for the broker itself

### CFX wire format — IPC-CFX schemas

IPC-CFX (Connected Factory Exchange, Apache-2.0) is the industry
standard for SMT / electronics manufacturing line messaging — major
equipment vendors (ASM/SIPLACE, Yamaha, Panasonic, Mycronic, MIRTEC,
Koh Young) ship it natively. Adopting it as our wire format = out-of-
the-box compatibility with hundreds of factory machines, instead of
inventing a custom envelope.

**Existing Python library:** Rosen-authored `ipc-cfx` v0.1.0 alpha
already exists at `/home/rosen/Проекти/amqp/proton-amqp/`
(`github.com/rosenvladimirov/ipc_cfx`, branch
`python-3.19-ipc-cfx-1.7`, ~1 yr dormant, 100 Python files, full
domain coverage in `src/lib/cfx/`: production, information_system,
maintenance, resourceperformance, structures, materials; plus
runtime in `src/server/` with Apache Qpid Proton AMQP transport).
Plan is **activate + integrate**, not port from scratch.

**Reference:** the official C# library at `/home/rosen/Проекти/CFX/`
serves as the audit source — diff its namespaces against the Python
package, backfill any messages Rosen hasn't yet covered, on demand
from the first MES customer.

**Activation work** (planned for ErpNet.FP integration):
- Refresh `ipc-cfx` against Python 3.12 (was tested on 3.9); resolve
  one uncommitted change in `cfx_processor.py`
- Decide repo strategy: keep `ipc-cfx` independent + `pip install`
  into ErpNet.FP, OR monorepo / submodule
- Optionally rebrand the folder from `proton-amqp` (transport-leaning)
  to `ipc-cfx-py` (canonical) since CFX supports MQTT too
- Audit completeness against the C# reference; backfill missing
  message types per first MES client need
- Wire into ErpNet.FP `odoo_erpnet_fp/server/` as broker client —
  instantiate endpoint, register on startup, publish CFX-standard
  Heartbeat / EndpointConnected, subscribe to production topics,
  bridge events to Odoo via existing `/iot/event` HTTP path

Topic conventions follow the CFX spec: `CFX.{Endpoint}`,
`CFX.Production.Subscribers.<endpoint>`, etc. — the broker admin
configures bindings per the spec; ErpNet.FP just publishes/subscribes
on the standard names.

`docs/cfx-compliance.md` lists which CFX message types are supported
in each release with a link back to the C# reference for the rest.

---

## Module independence — Grafana embed vs ErpNet.FP monitoring stack

The Odoo Grafana embed and the ErpNet.FP-bundled monitoring stack are
**fully decoupled** — either one works without the other:

| Component | Lives in | Depends on |
|---|---|---|
| `l10n_bg_erp_net_fp` "Monitoring" menu (iframe + URL config) | Odoo addon | Just an iframe — works with **any** Grafana URL the user configures |
| Bundled Prometheus + Grafana stack | `Odoo.ErpNet.FP/docker-compose.yml` | Just scrapes `odoo-erpnet-fp:8001/metrics` — works with or without an Odoo backend |

Implications:

- The Odoo embed module can point at a remote / hosted / cloud Grafana
  (a customer's existing observability stack, Grafana Cloud, etc.).
  Set Settings → POS → Embedded Grafana monitoring → URL to whatever
  Grafana you already run.
- The ErpNet.FP local stack can be browsed directly via
  `http://localhost:3001` or `https://grafana.lan.mcpworks.net`
  without ever installing the Odoo addon. This is useful for
  technicians who want a quick health dashboard at the till without
  jumping through Odoo.
- If the URL field is blank in Odoo settings, the menu still loads
  but shows a friendly "Configure Grafana URL first" message —
  no traceback.
- The dashboard JSON (`monitoring/grafana/dashboards/erpnet-fp.json`)
  is provisioned in the bundled stack but is also a plain Grafana
  export, importable into any other Grafana instance via the UI.

So you can mix and match: bundled stack only, embed only (pointing
elsewhere), both, or neither. The pieces don't share state, secrets,
or wire formats beyond standard Prometheus scrape and HTTPS iframe.

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
- **5 module bumps** on `l10n_bg_erp_net_fp` (18.0.9.0.0 → **18.0.10.1.0**, mirror in v19)
- **deb package** built + tested (`hid2serial_0.1.6_all.deb`)
- **Windows installer** built via Docker (`hid2serial-0.2.0-dev-setup.exe`)
- **dev-18 deployment** — module installed + upgraded with all dependencies
- **End-to-end POS test PASSED** with real Teemi BLE scanner

### Late-evening additions (after the first POS test)

- **`/metrics` endpoint** in proxy (0.2.4) — prometheus_client wrapper with no-op fallback when the lib is missing
- **Local Prometheus + Grafana stack** in `docker-compose.yml` (0.2.4) — anonymous viewer, dark theme, 192 MB + 128 MB memory caps
- **Wildcard TLS** for `*.lan.mcpworks.net` via Traefik DNS-01 ACME (Cloudflare resolver) — same cert covers `erpnet.lan.mcpworks.net` and `grafana.lan.mcpworks.net`
- **Traefik routing precedence fix** — `priority: 100` on the grafana router so it wins over the proxy's `HostRegexp({sub:[a-z0-9-]+}.lan.mcpworks.net)` catch-all
- **Grafana iframe-embedding allowed** (0.2.5) — `GF_SECURITY_ALLOW_EMBEDDING=true` + `SameSite=none` + `Secure` cookies → drops `X-Frame-Options: DENY`
- **Odoo embed module** — `l10n_bg_erp_net_fp` 18.0.10.1.0 + 19.0.10.1.0: new OWL component reads `ir.config_parameter` for URL + dashboard UID, builds kiosk-mode URL with auto-refresh, renders inside Odoo backend; new menu "Monitoring" under ErpNet.FP, restricted to `point_of_sale.group_pos_manager`
- **Documented decoupling** — see "Module independence" section above

---

*Authored by Rosen Vladimirov · Assisted by Claude Code*
