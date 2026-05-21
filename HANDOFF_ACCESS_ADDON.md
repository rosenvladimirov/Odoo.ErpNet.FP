# HANDOFF — ErpNet.FP Access-Control Add-on (Phases A + B)

> State as of **2026-05-17**. Hand this file to another Claude/engineer
> to continue. Everything below is **committed + pushed but NOT
> deployed** to any live instance. Communication language with the
> user (Rosen): **Bulgarian**. Codebase convention: English
> docstrings, Bulgarian inline comments, English user-facing strings.

---

## 1. What this is

An **add-on to the `Odoo.ErpNet.FP` proxy** (a pure-Python ErpNet.FP
fiscal-printer HTTP server for the BG market). The add-on adds two
device families — **camera streaming + LPR** (Phase A) and **live
access control** (Phase B) — so Odoo can do ANPR/biometric/credential
access control at gates & doors.

It is an **add-on, not a fork**: config-gated, additive driver
families in the same repo, riding the existing reader/registry/
EventBus patterns. **Zero regression** to fiscal/pinpad/readers/
scales/displays — independent `app.state.*` registries, separate
`APIRouter` prefixes, additive `iot_compat` kind-branches. When no
`cameras:`/`access:` config and no `--profile cameras`, behaviour is
**byte-identical to a pure-fiscal deployment**.

Driving consumer = the OPL-1 Odoo module **`hr_attendance_access_
control`** (in repo `l10n-bulgaria-expert`, NOT in this repo) —
two-channel access control around standard `hr_attendance`. The
**access DECISION is taken in Odoo** (Channel-1 credential/biometric ⊕
Channel-2 camera/LPR), fail-secure. This proxy only **streams cameras
and executes open/deny** — it never decides and never auto-opens.

Licensing: proxy stays **LGPL-3**; the Odoo consumer is **OPL-1 and
HTTP-coupled** (no Python import, not in `depends`) → no copyleft
combination. Realises "Open decision #5".

---

## 2. Repos, paths, branches

| Repo | Path | Branch | Role |
|---|---|---|---|
| **Odoo.ErpNet.FP** | `~/Проекти/odoo/iot/Odoo.ErpNet.FP` | `main` | the proxy — all add-on code |
| **l10n-bulgaria** (Fleet) | `~/Проекти/odoo/odoo-19.0/l10n-bulgaria/l10n_bg_erp_net_fp_fleet` | `19.0` | Odoo Fleet control-plane; companion `kind` rows. v18 fleet is a partial port (no device model) → v19-only |
| l10n-bulgaria-expert | (not touched here) | — | the OPL-1 `hr_attendance_access_control` consumer module |

Run tests: `cd ~/Проекти/odoo/iot/Odoo.ErpNet.FP && python3 -m pytest -q`
→ **205 passed**, 2 pre-existing warnings (admin.py `regex=` deprec).
Test files: `tests/test_cameras.py`, `tests/test_access.py`.

Canonical roadmap: **`ROADMAP.md`** → section "Access-control add-on
— camera streaming + live access control (pre-v1.0)" + its
"Status — 2026-05-17" block.

---

## 3. Commits (chronological; HEADs are the last lines)

**Odoo.ErpNet.FP / `main`:**
```
8dc397b [ADD] access: hikvision (ISAPI) + dahua (CGI) door-open drivers   ← HEAD
1785993 [DOC] roadmap — access-control add-on A+B DONE 2026-05-17
6944047 [IMP] access/polimex: ONE driver covers whole Polimex range (door+relay)
9140a44 [ADD] access: Polimex iCON WebSDK driver + dashboard 🚪 Access tab
9be05cb [ADD] access-control actuators — barrier/relay/turnstile (Phase B)
81ccc87 [ADD] camera-stream driver family + ONVIF ANPR/control (Phase A)
efd829d [ADD] DatecsIslXDevice … (0.5.5)  ← pre-add-on baseline
```
pyproject `version` bumped 0.5.5 → **0.6.0**. All pushed to origin.

**l10n-bulgaria / `19.0`** (fleet module only):
```
ffb8253 l10n_bg_erp_net_fp_fleet 19.0.3.2.0: access device kind
2896ea7 l10n_bg_erp_net_fp_fleet 19.0.3.1.0: camera device kind
ab46f0a fleet: drop invalid `aggregator` on graph/pivot (19.0.3.0.1)  ← separate AM fix, already deployed to iot.mcpworks.net that morning
```
camera-kind/access-kind companions are **pushed but NOT `-u`-deployed**.

---

## 4. Architecture invariants — DO NOT BREAK

1. **Proxy never decodes video or runs ML.** go2rtc (sibling, MIT
   binary) is the universal media hub; heavy AV/ML stays in
   siblings.
2. **Video goes browser/Odoo ↔ go2rtc DIRECTLY.** `CameraConfig` has
   `go2rtc_url` (internal, proxy→go2rtc API + ~1 small JPEG/interval
   for LPR) vs `go2rtc_public_url` (Cloudflare/Traefik HTTPS).
   `stream_urls()` returns the **public** one → zero proxy load while
   streaming. The proxy MJPEG relay (`/cameras/{id}/stream.mjpeg`) is
   **opt-in `mjpeg_relay: true`, default OFF** (LAN-only fallback;
   loads the fiscal process).
3. **LPR offload, 2 modes:** A) proxy-pull — proxy grabs 1 JPEG/
   interval → POST to fast-alpr sidecar; B) zero-proxy — camera
   `driver: external` + the sidecar's `ALPR_WATCH` pulls the stream
   itself and POSTs only plates to `POST /cameras/{id}/inject`. Proxy
   does zero frame work in B.
4. **Access decision is in Odoo (fail-secure).** Proxy only executes
   an explicit authorised command; no call ⇒ barrier stays shut.
5. **Commands are SYNCHRONOUS, zero queue latency:**
   `POST /access/{id}/{open,deny,status}` + native IoT `/action`
   (request→response, like a barcode read). The Fleet command-queue
   (`registry._execute_command` `kind=="access_open"`) is a
   **secondary slow remote-management path only**, NOT the decision
   path (it carries ~heartbeat latency).
6. **Everything config-gated** → pure-fiscal deployments byte-
   identical. go2rtc+alpr are docker-compose `profiles: ["cameras"]`.
7. **Mirror the existing patterns** (readers/displays). New code is
   1:1 with `drivers/readers/` + `reader_bus.py` + `routes/readers.py`
   + DisplayRegistry.
8. Vendor protocol drivers are **clean-room from public specs**, not
   code copies (relevant: Polimex `polimex-rfid` is AGPL-3; we only
   reimplement the documented wire protocol in our LGPL-3 proxy).

---

## 5. What's implemented

### Phase A — camera stream + LPR  ✅
- `odoo_erpnet_fp/drivers/cameras/`: `common.py` (`CameraStream` ABC,
  `PlateEvent`, `normalize_plate()` = BG Cyrillic→Latin canon),
  `go2rtc.py` (`Go2RtcCameraStream` base + `GenericRtspCameraStream`),
  `onvif_cam.py` (`OnvifCameraStream` + `OnvifAnprCameraStream` —
  on-board ANPR via ONVIF PullPoint events), `onvif_native.py`
  (`OnvifNativeClient` — ONVIF Events + Device IO relay + PTZ, lazy
  `onvif-zeep`), `lpr.py` (`LprEngine` ABC, `FastAlprSiblingEngine`,
  `NullLprEngine`, `make_lpr_engine`).
- `server/camera_bus.py` (`CameraEventBus` ≈ `ReaderEventBus`).
- `server/routes/cameras.py`: `/cameras`, `/{id}`, `/last`, `/next`,
  `/events` (SSE), `/ws`, `/snapshot`, `/stream`, `/stream.mjpeg`
  (gated), `/inject`, `/reset`, `/plate` (tolerant vendor
  HTTP-listener: Hikvision ISAPI XML / Dahua JSON / multipart),
  `/control` (synchronous ONVIF relay/PTZ).
- `tools/alpr_sidecar/` — reference fast-alpr μservice (FastAPI +
  ONNX), `POST /v1/recognize` (Mode A) + `ALPR_WATCH` env stream
  watcher (Mode B). Dockerfile + README.
- Dashboard **📷 Cameras** tab in `server/static/index.html` (live =
  `<iframe>` to public go2rtc; snapshot idle; SSE listen; test inject).

### Phase B — live access control  ✅
- `odoo_erpnet_fp/drivers/access/`: `common.py` (`AccessActuator`
  ABC: `connect/disconnect/open/deny/status`, `AccessResult`),
  `relay_tcp.py` (generic TCP relay board), `onvif_relay.py` (reuses
  Phase A `OnvifNativeClient`), `gpio.py` (Pi, lazy `[gpio]`),
  **`polimex.py`** (Polimex iCON WebSDK), **`hikvision.py`** (ISAPI),
  **`dahua.py`** (CGI), `wiegand.py` (honest scaffold — raises),
  `miv.py` (MIV Electronics vendor slot — protocol pending, raises).
- `server/routes/access.py`: synchronous `/access`, `/{id}`,
  `/{id}/open` (`{"seconds":N}` pulse or latched), `/{id}/deny`,
  `/{id}/status`.
- Dashboard **🚪 Access** tab (Open/Deny/Status per controller).

### Wiring (shared files, both phases)
- `config/loader.py`: `CameraConfig`, `AccessConfig` + parsers +
  `AppConfig.cameras/.access`.
- `server/service.py`: `CameraRegistry` (long-lived, start/stop),
  `AccessRegistry` (DisplayRegistry-style, per-id lock, lazy
  connect, `_make` driver factory; HTTP-port footgun fixed —
  `AccessConfig.port` default 23/telnet coerced to 80 for
  polimex/hikvision/dahua).
- `server/main.py`: registries on `app.state`, routers included,
  lifespan start/stop, `/healthz`, metrics path normalisation.
- `server/routes/iot_compat.py`: `_do_action` branches
  `camera`/`access` + `_camera_action` (last/snapshot/relay/ptz) +
  `_access_action` (open/deny/status) — synchronous native IoT.
- `server/registry.py` (Fleet client): `_execute_command`
  `kind=="access_open"` (secondary) + `_device_summary` includes
  `cameras`/`access`.
- `server/iot_setup.py`: announces `camera.<id>` (`type=camera`) and
  `access.<id>` (`type=device`) as Odoo `iot.device`.
- `server/metrics.py`: `camera_plates_total`, `camera_subscribers`.
- `docker-compose.yml`: `go2rtc` (Traefik `go2rtc.lan.mcpworks.net`,
  priority 100 beats erpnet-lan HostRegexp) + `alpr` siblings under
  `profiles: ["cameras"]`.
- `pyproject.toml`: optional extras `[onvif]` (onvif-zeep), `[gpio]`
  (gpiozero). `config-examples/config.yaml` + `go2rtc.example.yaml`
  fully documented.
- **Fleet companions** (l10n-bulgaria v19 `erpnet_fp_proxy_device.py`):
  `_KIND_LABELS` += `("camera","Camera")`, `("access","Access
  controller")`; `_sync_from_heartbeat` plural→singular map +=
  `"cameras":"camera"`, `"access":"access"`. Heartbeat silently skips
  unknown keys → proxy side is safe before the companion `-u`.

---

## 6. Access driver protocol cheat-sheet

| driver | open call | auth | notes |
|---|---|---|---|
| `relay_tcp` | TCP connect → send `on_cmd` bytes (`hex:` or text), optional pulse→`off_cmd` | none | KMtronic/Numato/USR/ESP boards |
| `onvif` | `OnvifNativeClient.pulse_relay/set_relay` (ONVIF Device IO) | WS-UsernameToken | camera's own relay; lazy onvif-zeep |
| `gpio` | gpiozero pin on/off | — | Pi at gate; lazy `[gpio]` |
| **`polimex`** | `POST http://<webstack>/sdk/cmd.json` `{"cmd":{"id":bus_id,"c":"DB","d":<payload>}}` | HTTP Basic SDK/key | door-type `%02x%02d%02d`(out,state,time); relay-type (`relay_ctrl:true`,`mode`1/2/3) `1F<reader>`+4-byte bitmask. ONE driver covers iCON50/110/115/130/180/SmartVend + iCON-R/110R |
| **`hikvision`** | `PUT /ISAPI/AccessControl/RemoteControl/door/<output>` `<RemoteControlDoor><cmd>open\|close\|alwaysOpen</cmd></RemoteControlDoor>` | HTTP Digest | DS-K2600/K2700, DS-K1T/K1A, DS-KD. `deny()`=real `close` |
| **`dahua`** | `GET /cgi-bin/accessControl.cgi?action=openDoor&channel=<output>&Type=Remote[&UserID=]` body `OK` | HTTP Digest | ASC/ASI/VTO. Probes CGI → legacy SDK-only ASC (404) fails clearly; NetSDK NOT shipped |
| `wiegand` | — | — | scaffold, raises (needs MCU bit-banger) |
| `miv` | — | — | vendor slot, raises (MIV Electronics protocol pending) |

`output` config field = doorNo (hikvision) / channel (dahua) /
output# (polimex/relay). Caveat in hik/dahua docstrings: **200/OK =
command accepted, not relay proven** (device door-mode/lock config
can swallow it — device issue, not API failure).

---

## 7. NOT deployed — deploy is a separate gated step

Nothing runs on a live instance. To deploy (confirm with Rosen first
— production control-plane; see memory `project_iot_mcpworks_deploy_
ops`):
- **Fleet companions** (`2896ea7`,`ffb8253`): rsync the 2 fleet files
  to `iot.mcpworks.net` `/opt/odoo/odoo-19.0/rv/l10n-bulgaria/` then
  `docker compose run --rm --no-deps iot odoo -d iot -u
  l10n_bg_erp_net_fp_fleet --stop-after-init --no-http` then
  `docker compose start iot`. **Do NOT `git pull` on that box** — it
  carries intentional uncommitted WIP. rsync only the touched files.
  Safe to deploy independently (heartbeat skips unknown keys).
- **Proxy image** (`8dc397b`): wherever a proxy actually runs;
  camera/alpr siblings are profile-gated so pure-fiscal proxies are
  byte-identical without `--profile cameras`.

---

## 8. Open items / next options

- **Phase C — biometric face-auth** ⏳ NOT STARTED. Spec in
  `ROADMAP.md` Phase C + the user's concept (memory
  `project_lpr_access_control_concept`): thin `httpx` client to an
  external **Node** face-auth μservice (author Довид Росенов Милев,
  `odoo-face-auth/tuning`) — NOT reimplemented in Python; new
  native-IoT prefix `biometric.<id>`; **dedicated Odoo bus channel
  `hac.biometric/<terminal_id>`** — must NOT touch the InfoPay bus
  channel nor LPR/kiosk channels; liveness/anti-spoof enforced
  server-side in the Odoo bridge; biometric template wallet =
  separate Rosen-owned workstream, OUT OF SCOPE here; visitors = no
  biometrics.
- **WebSDK event-stream ingestion** (idea): Polimex WebSDK also
  POSTs reader/card/door events — could feed the reader/camera bus
  (card scans → existing reader pipeline). Not started.
- **MIV Electronics** driver still a stub — pending vendor protocol
  spec from Rosen. `polimex` supersedes it for the BG market in
  practice.
- **Fleet companion deploy** (see §7).
- Polimex relay-type only `_relay_payload`-unit-tested vs the
  reference algorithm (no hardware). Hik/Dahua unit-tested with
  mocked HTTP (no hardware). Real e2e on hardware pending.

---

## 9. Sourced research (3 background agents, 2026-05-17)

- **Cameras:** plain ONVIF S/T does NOT carry plate text — only
  ONVIF **Profile M** (Axis ALPV / Milesight PlateXpert / Hanwha).
  Hikvision(ISAPI)/Dahua/Uniview/Vivotek/Provision push plate via
  proprietary HTTP-listener → our tolerant `POST /cameras/{id}/plate`.
  Optics rule: EN 62676-4 ≥250 px/m, ~100 px plate, ≤30°, shutter
  ≥1/1000 s; a varifocal ANPR cam beats a generic 4K cam. Shortlist:
  Axis P1465-LE-3 / Milesight PlateXpert / Dahua ITC413-PW4D;
  server-side 4K = Dahua IPC-HFW5842E-ZE.
- **iCON130 = Polimex Holding (Sofia, BG)** original controller, NOT
  a rebrand. Open HTTP/JSON WebSDK (websdk.polimex.online/docs).
  Official AGPL-3 Odoo suite **github.com/polimex/polimex-rfid**
  (`hr_rfid` etc. on apps.odoo.com).
- **securitybulgaria.com audit:** it is Polimex's own store. ONE
  `polimex` driver covers the whole genuine range (iCON50/110/110R/
  115/130/180/SmartVend/iCON-R). ⚠ **Name-collision trap:** the
  shop's "iCON100/iCON100SR" = **IDTECK** (Korea, different vendor/
  protocol — NOT our driver); "iTDC" = **SOYAL** AR-721 (not
  covered); SBR-01CR/02CR only via a Polimex LAN converter.
- **Hik/Dahua access controllers:** both expose a synchronous
  single-HTTP door-open with no binary SDK → implemented as
  `hikvision`/`dahua` (see §6). Dahua legacy SDK-only ASC explicitly
  unsupported (thin-proxy boundary).

---

## 10. Operational gotchas for the next agent

- Bash tool cwd resets to the claude.ai project dir between some
  calls — always `git -C <repo>` or `cd <repo> &&` explicitly;
  verify the commit landed in the right repo.
- Commit/push convention: this ecosystem commits **directly to the
  working branches** (`main`, `19.0`) — no PR flow. Commit messages
  end with the `Co-Authored-By: Claude …` line. Commit only when the
  user asks; the user here drives "implement → commit → push".
- Persistent memory: `~/.claude/projects/-home-rosen---------odoo-
  odoo-18-0-claude-ai/memory/` — main file
  `project_erpnet_camera_phase_a_2026_05_17.md` (+ `MEMORY.md`
  index). Related: `project_lpr_access_control_concept`,
  `project_iot_mcpworks_deploy_ops`, `feedback_ready_before_announce`.
- "Ready before announce" rule: don't say LIVE/deployed before a
  real e2e test on fresh head; code-complete локално ≠ deployed.
- l10n-bulgaria `19.0` is a shared branch (other people's commits
  land there) — only touch `l10n_bg_erp_net_fp_fleet`, stage
  explicit paths.

---
*Generated 2026-05-17 by Claude Code (Opus 4.7) for handoff.*
