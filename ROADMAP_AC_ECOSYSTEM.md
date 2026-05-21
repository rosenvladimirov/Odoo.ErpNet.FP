# ROADMAP — Access-Control Ecosystem (Modules + Hardware Proxy)

> 2026-05-17. Planning artifact (NOT committed). Companion:
> `ROADMAP_AC_PROXY_ODOO_LINK.md` (the seam between them),
> `ROADMAP.md` (proxy product roadmap), `HANDOFF_ACCESS_ADDON.md`.
> Source memories: `project_erpnet_camera_phase_a_2026_05_17`,
> `project_lpr_access_control_concept`,
> `reference_erpnet_access_addon_contracts`,
> `project_iot_mcpworks_deploy_ops`.

## 0. The one rule that frames everything

**The hardware proxy (`Odoo.ErpNet.FP`) is a SINGLE proxy that serves
BOTH:**
1. **POS / fiscal** — printers, scales, displays, **readers**,
   pinpads (the original ErpNet.FP product; tablet/POS + directly
   attached reader → hid2serial → proxy → Odoo POS). **Untouched.**
2. **Access control (КА)** — cameras + access actuators (the add-on).

The КА add-on is **config-gated & additive**: no `cameras:`/`access:`
config and no `--profile cameras` ⇒ **byte-identical to a pure-fiscal
deployment**. The standard POS reader path (`reader.<id>` native-IoT)
is **never blocked/stolen** — КА card events ride a *parallel*
`hac.card/<id>` channel (separate waiter queue). Every milestone
below must preserve this invariant.

---

## 1. Hardware proxy — `Odoo.ErpNet.FP` (branch `main`)

Current HEAD `7f6adac`, pyproject **0.6.0**. Committed+pushed,
**NOT deployed** anywhere live.

| Phase | Scope | Status |
|---|---|---|
| **A — Camera stream + LPR** | go2rtc media hub (internal vs `go2rtc_public_url`), fast-alpr sidecar (proxy-pull / zero-proxy `ALPR_WATCH`), ONVIF edge-ANPR PullPoint, tolerant `POST /cameras/{id}/plate` (Hik ISAPI/Dahua), `normalize_plate` BG Cyrillic→Latin, 📷 dashboard, `camera.<id>` native-IoT, Fleet camera-kind | ✅ DONE |
| **B — Live access control** | `drivers/access/` synchronous `POST /access/{id}/{open,deny,status}`; transports `relay_tcp·onvif·gpio·`**`polimex`** (whole iCON range, door+relay) ·**`hikvision`** (ISAPI) ·**`dahua`** (CGI) ·`wiegand`(stub)·`miv`(slot); 🚪 dashboard, Fleet access-kind; decision in Odoo (fail-secure) | ✅ DONE |
| **Polimex WebSDK event-stream** | `POST /polimex/event` (+`/hr/rfid/event` alias) → reader bus via `extras.polimex`; **Channel-1 `hac.card/<id>`** parallel native-IoT (never blocks POS `reader.<id>`) | ✅ DONE |
| **C — Biometric face-auth client** | `drivers/biometric/` thin httpx client to external Node face-auth μsvc (Довид Милев); `biometric.<id>` + dedicated `hac.biometric/<terminal>` bus channel; liveness server-side in Odoo; wallet = Rosen-owned, out of scope | ⏳ NOT STARTED |
| **Deploy** | proxy image + Fleet `-u` on `iot.mcpworks.net` (Fleet control-plane). rsync touched files only, **never `git pull`** (server has intentional WIP), `docker compose run --rm` `-u` (see `project_iot_mcpworks_deploy_ops`) | ⏳ GATED |

`214 passed`, zero regressions. Fiscal/POS suite untouched by A/B.

Fleet companion: `l10n-bulgaria@19.0` `ffb8253`
(`l10n_bg_erp_net_fp_fleet` 19.0.3.2.0 — camera+access `kind`s),
pushed, **NOT `-u`-deployed** (heartbeat silently skips unknown keys
→ safe to ship proxy side first). v18 fleet = partial port (no
device model) → v19-only.

---

## 2. Odoo modules — `l10n-bulgaria-expert` (OPL-1, v18 **and** v19)

Two modules, lockstep v18+v19, **uncommitted WIP** in working trees
(`~/Проекти/odoo/odoo-{18.0,19.0}/l10n-bulgaria-expert/`).
**dev-19 (`mcpworks_dev`) = clean verification target; dev-18
(`odoo`) GATED** by a Rosen parallel `l10n_bg_config`/`l10n.bg.kid`
M2M collision (diagnose-don't-touch).

### m1 `hr_attendance_access_control` (18/19.0.5.0.0)
Two-channel access on top of standard `hr_attendance`. Channel-1 =
person identity via universal reader (RFID/NFC/PIN → credential;
later face/WebAuthn transports). Channel-2 = camera/LPR. Decision =
Channel-1 ⊕ Channel-2 resolved through **Fleet** (`fleet.vehicle` +
`assignation.log` + `hr_fleet`) — **no `rfid.*` domain models, no
`car.registry`** (deleted; Fleet is the single source of truth).

### m2 `access_control_guest`
Owns `parking.session` + partner/visitor branch; depends on m1; m1
exposes `_handle_guest_access`/`_handle_partner_access` hooks (pass);
m2 overrides + inherits `lpr.event`. Visitors = live guard, no
biometrics.

| Milestone | Scope | Status |
|---|---|---|
| **Phase 1** | Fleet-native refactor (drop `car.registry`; `fleet.vehicle` inherit `x_access_*`; `lpr.event._resolve_access`/`resolve_employee_by_plate`/`_check_and_process_access`/`_handle_guest_access`); 2-module split; v19-compat deltas (Registry import, `res.groups.privilege`, bare `<group>`) | ✅ DONE, dev-18+dev-19 verified |
| **2a** | bus-unify → single `hr_attendance_access_control` channel (ping/onvif/socket/mqtt kept; `hac.biometric` reserved; InfoPay untouched) | ✅ DONE |
| **2b-1** | self-contained KioskRoot Owl app + bundle restructure | ✅ DONE |
| **#15** | camera↔proxy: `lpr.camera.config.proxy_base_url/proxy_camera_id` + `_proxy_stream_urls()` → public go2rtc (0 creds to client) | ✅ DONE |
| **#16** | sync access-execute: `lpr.access.controller` model (thin client → `POST {proxy}/access/{id}/{open,deny}`) wired in `_check_and_process_access` granted branch, fail-secure | ✅ DONE |
| **#17** | **plate-ingestion alignment** — Step 1 `/lpr_gateway` accepts proxy `PlateEvent` (camera-by-`proxy_camera_id`) ✅ · Step 2 native-IoT consumer `proxy.event.service`+`ProxyEventPollThread` long-poll `camera.<id>`/`hac.card/<id>`→`lpr.event.create_from_proxy` (never `reader.<id>`) ✅ | ✅ **CODE-COMPLETE** (lockstep v18==v19, compile OK; dev-19 `-u` verify ⏳ pending) |
| **#11 / 2b** | Owl panels in KioskRoot: Lpr (`streamUrls.webrtc` from #15) / Barrier (`lpr.access.controller` from #16) / Attendance (Channel-1 credential) — all 3 mounted; `_handle_proxy_card` unified `attendance_result` bus | ✅ **CODE-COMPLETE** (lockstep v18==v19, compile/XML/node OK; dev-19 `-u` ⏳ batched) |
| **#14 / 2e** | kiosk_controller dedupe (two `/lpr_camera/config` routes, recent_events×3) + token-gate | ⏳ pending |
| **#13/#6** | face-auth Phase C (Channel-1 biometric) — depends on proxy Phase C + Node μsvc + Rosen wallet | ⏳ not started (both sides) |
| **#8** | OPL-1 finalisation: canonical LICENSE text + `adapter.js` license audit | ⏳ end |

---

## 3. Cross-cutting constraints

- **POS/fiscal never regresses.** Any module/proxy change keeps the
  pure-fiscal & POS-with-reader paths byte-identical.
- **Decision in Odoo, fail-secure.** Proxy never decides, never
  auto-opens. No call ⇒ barrier shut.
- **HTTP-coupled, copyleft-clean.** Odoo modules (OPL-1) talk to the
  proxy (LGPL-3) and face-auth (Довид) over HTTP only — no `depends`,
  no import.
- **Lockstep v18+v19.** Edit v18 → cp version-agnostic to v19; do
  NOT blind-cp divergent files (`security.xml`, search-views,
  `__manifest__.py`, Registry import).
- **Nothing committed in the module trees yet** (Rosen owns git
  strategy there); proxy IS committed/pushed (not deployed).
