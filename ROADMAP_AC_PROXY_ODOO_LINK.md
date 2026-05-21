# ROADMAP — Proxy ↔ Odoo Link (the seam)

> 2026-05-17. Planning artifact (NOT committed). The integration
> contract & milestones between `Odoo.ErpNet.FP` (proxy) and the
> `hr_attendance_access_control` / `access_control_guest` Odoo
> modules. Companion: `ROADMAP_AC_ECOSYSTEM.md`. Authoritative
> contract memory: `reference_erpnet_access_addon_contracts`.

## 0. Two independent consumers of ONE proxy

The proxy multiplexes two unrelated Odoo workloads over the same box;
the link design must keep them isolated:

```
                         ┌─────────────── Odoo.ErpNet.FP (one proxy) ───────────────┐
 fiscal printer ─────────┤ printers/scales/displays/readers/pinpads  (POS / fiscal) │
 tablet+reader→hid2serial┤   reader.<id>  native-IoT  ───────────────► Odoo POS      │
                         │                                                          │
 camera (RTSP/ONVIF) ────┤ cameras/  → camera.<id>  ─┐                               │
 Polimex webstack ───────┤ /polimex/event → reader bus┤  hac.card/<id> ─┐            │
 access controller ──────┤ access/  (relay/door)      │                 │           │
                         └────────────────────────────┼─────────────────┼───────────┘
                                                       ▼                 ▼
                              hr_attendance_access_control (Channel-2 LPR + Channel-1 card)
                                          decision (Ch1⊕Ch2, fail-secure)
                                                       │
                                                       ▼  sync, zero-latency
                                          POST {proxy}/access/{id}/open
```

- **POS path is sacrosanct:** `reader.<id>` native-IoT (and the whole
  fiscal surface) is what the POS tablet consumes. The КА link must
  **never** poll/steal `reader.<id>` — it uses the *parallel*
  `hac.card/<id>` identifier (separate waiter queue → guaranteed
  non-blocking; the native-IoT registry is per-identifier
  single-delivery).
- The КА link is **HTTP-coupled** (no Odoo `depends`/import) →
  copyleft-clean (proxy LGPL-3, modules OPL-1).

## 1. Contract surface (proxy endpoints the modules use)

| Concern | Proxy endpoint / channel | Direction | Odoo side |
|---|---|---|---|
| **Discovery** | proxy `POST {odoo}/iot/setup` → `iot.box`+`iot.device`; `camera.<id>`→type=camera, `access.<id>`→type=device | proxy→Odoo | iot/iot_oca bridge (optional; HTTP-coupled, no hard dep) |
| **Camera stream (#15)** | `GET {proxy}/cameras/{id}` → `CameraInfoResp.streamUrls` (public go2rtc, **0 creds**) | Odoo→proxy (pull) | `lpr.camera.config._proxy_stream_urls()` ✅ |
| **Plate IN — webhook** | camera bus webhook → POST `PlateEvent.to_json()` (camelCase `cameraId,plate,confidence,timestamp,source,bbox,imageB64`) | proxy→Odoo | `/lpr_gateway` (needs **#17** to recognise this shape) |
| **Plate IN — native-IoT** | `camera.<id>` long-poll event (`/iot_drivers/event` v19 / `/hw_drivers/event` v18) | Odoo polls | **#17** native-IoT consume |
| **Card IN — native-IoT** | `hac.card/<reader_id>` long-poll (parallel to `reader.<id>`; payload `{result,value,card,reader_id,timestamp}`) | Odoo polls | **#17** native-IoT consume |
| **Polimex card events** | webstack → `POST {proxy}/polimex/event` → reader bus → `hac.card/<id>` | device→proxy→Odoo | covered by #17 (same channel) |
| **Access execute (#16)** | `POST {proxy}/access/{id}/open` `{"seconds":N}` / `/deny` / `GET /status` — synchronous, zero queue latency, fail-secure | Odoo→proxy | `lpr.access.controller` ✅ |
| **Fleet (secondary)** | `kind=="access_open"` Fleet command-queue | Odoo→Fleet→proxy | **NOT** the decision path (slow remote-mgmt only) |
| **Snapshot/evidence** | `GET {proxy}/cameras/{id}/snapshot` (JPEG) | Odoo→proxy | optional, for `lpr.event` evidence frame |

⚠ The `reference_erpnet_access_addon_contracts` memory was written at
proxy `8dc397b`; current `main` = `7f6adac` adds `/polimex/event`,
`hac.card/<id>`, tolerant `/cameras/{id}/plate`, polimex/hik/dahua
access drivers. The richer surface above is current. (Update that
memory when the link stabilises.)

## 2. Link milestones

| # | Milestone | Proxy side | Odoo side | Status |
|---|---|---|---|---|
| L1 | Camera stream (public go2rtc, no creds) | A ✅ | #15 ✅ | ✅ DONE |
| L2 | Sync access execute (fail-secure) | B ✅ | #16 ✅ | ✅ DONE |
| **L3** | **Plate/card ingestion alignment** — `/lpr_gateway` accepts proxy `PlateEvent` (Step 1); self-contained `proxy.event.service` long-poll consumer of `camera.<id>`+`hac.card/<id>` (Step 2, NO iot/iot_oca depend — HTTP-coupled); never `reader.<id>`; `/lpr_gateway` direct-from-camera kept as fallback | `/cameras/{id}/plate`, `camera.<id>`, `hac.card/<id>`, `/polimex/event` ✅ | **#17 ✅ CODE-COMPLETE** (lockstep v18==v19; dev-19 `-u` ⏳) | 🟡 code-done, verify pending |
| L4 | Discovery auto-wire | `iot/setup` ✅ + Fleet companion (pushed, not `-u`) | optional iot.device auto-config of `lpr.camera.config`/`lpr.access.controller` (no hard dep) | ⏳ pending |
| L5 | Biometric (Phase C) | proxy `drivers/biometric/` + `biometric.<id>` + dedicated `hac.biometric/<terminal>` | m1 Channel-1 face/WebAuthn transports | ⏳ not started (both sides) |
| L6 | Deploy | proxy image + Fleet `-u` on iot.mcpworks.net (gated) | modules dev-19 verified; dev-18 gated by Rosen workstream | ⏳ gated |

## 3. L3 — ✅ DONE 2026-05-18 (code-complete, lockstep v18==v19; dev-19 `-u` verify ⏳ batched). Plan as implemented:

1. **`/lpr_gateway` proxy-PlateEvent shape:** extend
   `_extract_plate_info` to recognise camelCase `cameraId,plate,
   confidence,timestamp,source,bbox` (additive to the existing
   Dahua/Hik/recursive parsers) → existing `lpr.event._resolve_access`.
   Optional shared-secret/header to distinguish proxy vs
   direct-from-camera.
2. **Native-IoT consumer:** a polling client (cron / queue job /
   `bus`-driven) that long-polls `{proxy}/iot_drivers/event` (v19) /
   `/hw_drivers/event` (v18) for identifiers `camera.<id>` and
   `hac.card/<reader_id>` (from `lpr.camera.config` /a reader-map),
   feeding the same `_resolve_access` / Channel-1 credential path.
   **MUST NOT** subscribe to `reader.<id>` (POS).
3. **Channel mapping:** `lpr.camera.config` already has
   `proxy_base_url`/`proxy_camera_id` → derive `camera.<proxy_camera_id>`;
   add a thin reader→`hac.card/<id>` map for Channel-1 cards.
4. **Fail-secure + idempotency:** dedupe by event id/timestamp;
   unknown plate/card → guest hook; never auto-open (decision stays
   Ch1⊕Ch2 in Odoo → `lpr.access.controller.open()`).
5. Verify on **dev-19** (`mcpworks_dev`); dev-18 gated — code
   byte-identical lockstep gives high confidence.

## 4. Invariants the link must never break

1. POS `reader.<id>` + the whole fiscal surface — untouched, never
   polled by КА.
2. Decision in Odoo, fail-secure; proxy only executes.
3. HTTP-coupled only (no depends/import) — copyleft-clean.
4. Config-gated: КА off ⇒ proxy byte-identical for pure-fiscal/POS.
5. Lockstep v18+v19; dev-19 is the verification target.
