# RuView Deployment — State (single-board complete)

**Last updated:** 2026-05-22
**Approved plan file:** `C:\Users\Draunzer\.claude\plans\witty-herding-crab.md`
**Goal:** 2× ESP32-S3 DevKits → WiFi-CSI sensing → Home Assistant via custom MQTT bridge + 12 automations.

**Status TL;DR (2026-05-22 evening):** Both ESP32 boards flashed and provisioned with custom firmware (MGMT+DATA filter + 10 Hz probe injection scheduler). HA shows live presence, breathing rate, heart rate, motion energy, person count, activity. 12 automations deployed and enabled. UDP relay persisted via Startup-folder VBS launcher.

Board inventory:
- **Board 1**: node_id=1 (bedroom), MAC `44:1B:F6:89:8E:F8`, IP `192.168.1.7` (DHCP), Waveshare ESP32-S3-Touch-AMOLED-1.8 with CH340 UART (VID 1A86). Currently unplugged.
- **Board 2**: node_id=2 (living room), MAC `9C:13:9E:BA:15:C4`, IP `192.168.1.26` (DHCP), same hardware variant. Currently plugged into PC via COM3 for verification.

**Per-node split (2026-05-22):** Bridge updated to drive each room from its own entry in `/api/v1/nodes`. Presence, motion, person_count, activity, status all sourced per-node. Availability topic is dynamic — entities go `unavailable` in HA when the corresponding board is offline or stale. Verified: bedroom entities marked `unavailable` while board 1 unplugged; living room entities show real data from board 2.

Caveat that remains: `breathing_rate_bpm` and `heart_rate_bpm` are still fused across all nodes (RuView 0.6.5 exposes only one combined value). If both rooms are occupied, both will show the same number. The bridge logs this with a `TODO` comment.

**Both boards now in their rooms** (board 1 in bedroom, board 2 in living room, both on USB wall chargers, both showing `status: active` with `last_seen_ms ≈ 30` and producing real CSI data).

**Verification layer (2026-05-22):** New package `D:\dev\ha\packages\ruview_test_notifications.yaml` deployed. 10 test automations + 1 toggle (`input_boolean.ruview_test_mode`):
- `4h Digest` (6/day): both-rooms snapshot via the new `sensor.ruview_cross_room_snapshot` template sensor
- `Morning Summary` (1/day at 08:00): last sleep window + both rooms now
- `Bedroom Presence Change`, `Living Room Presence Change` (~5–15/day): 60s debounce, includes cross-room snapshot
- `Bedroom Activity Change`, `Living Room Activity Change` (~5–15/day): 300s (5-min) debounce, includes cross-room snapshot
- `Sleep State Change` (~2/day): on every flip of `binary_sensor.ruview_bedroom_sleeping`
- `Wakeup Confirmation` (~1/day): on `binary_sensor.ruview_bedroom_wakeup` → on
- `Board Availability Change`: when a board goes offline 60s+ or comes back
- `Fall Detection (informational)`: won't fire on RuView 0.6.5 — kept as a marker for when upstream wires it
- Every notification body embeds `sensor.ruview_cross_room_snapshot` so each ping shows BOTH rooms' state, not just the triggering one

Expected daily volume: ~18–35 Telegram messages. Disable via `input_boolean.ruview_test_mode` off, or delete the package file and reload.

---

## 📋 Backlog (for future sessions)

### Hot — verify in the next 24–48h
- [ ] Watch Telegram for **board availability flips** — the MGMT+DATA filter trade-off means the chip *could* crash under heavy WiFi load. If either room repeatedly goes "unavailable" without you unplugging it, plan a rollback to stock `release_bins/*.bin` (see "Rollback path" below).
- [ ] Confirm **sleep detection** actually fires tonight: bedroom presence + stable breathing (6–22 BPM) + low motion for 15+ samples after 22:00 should flip `binary_sensor.ruview_bedroom_sleeping` to `on`. Expect a Telegram from "RuView Test: Sleep State Change".
- [ ] Confirm **wakeup → morning briefing TTS** chain in the morning. Wake-induced motion should flip `binary_sensor.ruview_bedroom_wakeup` to `on` between 06:00–10:00, which triggers `script.morning_briefing_tts` via the main automation `RuView: Morning Briefing on Wakeup`.
- [ ] Sanity-check **breathing rate**: should settle to 12–20 BPM when you're sitting still. Higher during walking is expected. 25+ at rest = give it 30+ seconds to accumulate samples after a new occupancy event.

### Warm — once the system is trusted (~1 week)
- [ ] Turn off test notifications: `input_boolean.turn_off ruview_test_mode` (or delete `packages/ruview_test_notifications.yaml` and reload).
- [ ] Per-room **breathing/heart split**: currently fused (both rooms show the same number when both are occupied). Either monkey-patch the RuView server to expose `/api/v1/nodes/{id}/vitals`, or rewrite `RoomState.update()` to derive vitals from each node's `amplitude[]` array in `/api/v1/sensing/latest`.
- [ ] **Fall detection**: RuView 0.6.5 has no fall endpoint (`/api/v1/falls/*` returns 404). Two paths: (a) wait for upstream to ship it, or (b) implement a heuristic in the MQTT bridge (sudden motion spike → motion absent for 60s in the same room).
- [ ] Tune **presence threshold (`pres_thresh`)** if you see false positives near fans or the microwave. Re-provision the affected board with `--pres-thresh <number>` (default 50, lower = more sensitive).
- [ ] **Heart rate calibration**: the initial values were ~100+ BPM, settling around 75–85. If you see persistent unrealistic numbers after a week, the algorithm may need retuning — RuView server uses an FFT on the breathing band; heart rate detection is less reliable than breathing.

### Cold — nice-to-have
- [ ] Build an **HA dashboard card** with both rooms' vitals + presence map (LiveChart for breathing/heart, occupancy timeline).
- [ ] Add a **second-room movie mode** (currently only one room).
- [ ] **Spare router as WiFi illuminator** — if MGMT+DATA filter ever proves unstable, an active WiFi illuminator removes the filter requirement entirely.
- [ ] **OTA upload** infrastructure: the firmware has `/ota` and `/wasm/upload` endpoints exposed at port 8032 on each board, but they're fail-closed until provisioned with `wasm_pubkey` / `security` NVS namespaces. Wire this up so we can push firmware updates without re-flashing over USB.
- [ ] Submit our firmware patch upstream: `csi_collector.c` MGMT+DATA filter + 10 Hz probe-injection scheduler is genuinely useful work. The probe-inject scheduler matches the existing ADR-029 design — could be a clean PR. The filter expansion would need stability testing on more hardware before being mergeable.

### Files of record

| File | Where | Status |
|---|---|---|
| `D:\dev\ruview\DEPLOYMENT_STATE.md` | this file | canonical state |
| `D:\dev\ruview\firmware\esp32-csi-node\main\{csi_collector.c,csi_collector.h,main.c}` | ruview clone | local mods, not pushed (no remote we own) |
| `D:\dev\ha\docs\ruview\firmware-patch.diff` | HA repo | exported patch — re-apply via `git apply` if ruview clone lost |
| `D:\dev\ha\docs\ruview\README.md` | HA repo | breadcrumb to here |
| `D:\dev\ruview\ruview-bridge\*` | ruview clone | MQTT bridge code, image tag `ruview-mqtt-bridge:latest` |
| `D:\dev\ruview\docker-compose.ruview.yml` | ruview clone | compose stack |
| `D:\dev\ha\packages\ruview_automations.yaml` | HA repo, deployed to Pi `/config/packages/` | 12 real automations + 3 helpers |
| `D:\dev\ha\packages\ruview_test_notifications.yaml` | HA repo, deployed to Pi `/config/packages/` | 10 test automations + 1 toggle |
| `D:\dev\ha\.env` | HA repo (tracked despite being secrets — user pattern) | RUVIEW_MQTT_USER, RUVIEW_MQTT_PASS appended |
| `C:\Users\Draunzer\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\ruview-relay-startup.vbs` | Windows user Startup folder | runs `udp-relay.py` at logon |

### Rollback path (if MGMT+DATA filter destabilizes the chip)

```powershell
cd D:\dev\ruview\firmware\esp32-csi-node
python -m esptool --chip esp32s3 --port COM3 --baud 460800 write-flash --flash-mode dio --flash-size detect `
  0x0     release_bins\bootloader.bin `
  0x8000  release_bins\partition-table.bin `
  0xf000  release_bins\ota_data_initial.bin `
  0x20000 release_bins\esp32-csi-node.bin
python provision.py --port COM3 --chip esp32s3 --ssid Bumblebee --password "<WIFI_PASSWORD>" `
  --target-ip 192.168.1.99 --target-port 5005 --node-id <1 or 2> --edge-tier 2 --reset
```

This reverts to MGMT-only filter — chip will be stable but CSI starved (back to ~0 useful captures, presence/breathing won't work). It's a known-stable fallback if our custom firmware ever causes problems.

---

## ✅ Completed phases

### Phase A — Foundation
- [x] **Cloned RuView** to `D:\dev\ruview` (commit `d72e06fc`, v0.6.5, May-2026 build). Pre-built firmware bins, `scripts/udp-relay.py`, and `firmware/esp32-csi-node/provision.py` all verified present.
- [x] **Added MQTT credentials** to `D:\dev\ha\.env`:
  - `RUVIEW_MQTT_USER=ruview`
  - `RUVIEW_MQTT_PASS=<see D:\dev\ha\.env>`
- [x] **Created `ruview` MQTT user** in HA (Settings → People → Users, non-admin). Verified by successful CONNACK from bridge container.

### Phase B — Server validation (simulated)
- [x] Pulled `ruvnet/wifi-densepose:latest` (~224 MB).
- [x] Brought up simulated-mode container; `/health`, `/api/v1/vital-signs`, `/api/v1/sensing/latest` all respond correctly.
- [x] **Port mapping change:** uptime-kuma owns host port `3001`, so RuView WebSocket is remapped to host `3011` in `docker-compose.ruview.yml`. WS-using clients must target `:3011`, not `:3001`.

### Phase C — MQTT bridge
- [x] Built `ruview-mqtt-bridge:latest` from `D:\dev\ruview\ruview-bridge\` (mqtt_bridge.py, requirements.txt, Dockerfile).
- [x] **Bridge code rewritten** — source guide had wrong field names. Validated against real 0.6.5 API:
  - `vitals.vital_signs.breathing_rate_bpm` (not `breathing_bpm`)
  - `vitals.vital_signs.heart_rate_bpm` (not `heart_bpm`)
  - `sensing.classification.presence` (not top-level `presence`)
  - `sensing.classification.motion_level` (string: `present_still`, `present_moving`, etc.)
  - `sensing.estimated_persons` (not `n_persons`)
  - motion-energy normalized from `features.motion_band_power` via `tanh(x/50)`.
- [x] **Fail-fast CONNACK handler** added — bridge now exits with a clear error message if the broker rejects auth (rc=4 or rc=5).
- [x] `docker-compose.ruview.yml` created at `D:\dev\ruview\docker-compose.ruview.yml` — env interpolation from `.env`, no healthchecks, no CPU limits (per repo conventions).
- [x] **End-to-end validated** against simulated server: all 20 HA entities appeared via MQTT auto-discovery, state messages flowing at 2-second intervals with sensible values.

### Phase F — automations source file staged
- [x] `D:\dev\ha\packages\ruview_automations.yaml` written (12 automations + 3 helpers — `input_datetime.ruview_sleep_start`, `input_datetime.ruview_sleep_end`, `input_number.ruview_sleep_quality`).
- [x] **NOT YET DEPLOYED** to the Pi. Held back deliberately until ESP32 entities exist; otherwise HA would warn about unavailable entities every reload.

---

## ⏳ Pending phases (resume here)

### Phase D — Flash + provision ESP32 boards
**Status: COMPLETED for board 1**. Board flashed with custom-built firmware including the 10 Hz probe-injection patch (see Phase D-fix below). NVS provisioned with SSID=`Bumblebee`, target_ip=`192.168.1.99`, node_id=1, edge_tier=2. Board connects on each boot, RSSI -42 to -47 dBm.

**Hardware confirmed:** Waveshare ESP32-S3-Touch-AMOLED-1.8 (not a plain WROOM DevKit — has SH8601 AMOLED display attached); CH340 UART (VID `1A86`), shows up as COM3 on Windows.

### Phase D-fix — Firmware patch (RESOLVED 2026-05-22)
**Status: CSI sensing fully working on a single board, no external illuminator needed.**

**Final fix:** Expand the promiscuous filter from `MGMT-only` to `MGMT | DATA` in csi_collector.c. Probe injection scheduler also added but is no longer strictly needed (kept in place as belt-and-suspenders). The existing 50 Hz software early-drop gate (CSI_MIN_PROCESS_INTERVAL_US) is sufficient to prevent the wDev_ProcessFiq crash the original firmware author was avoiding.

**Live results (post-fix, board on desk, person sitting nearby):**
- CSI callbacks: ~7-10 Hz sustained (was ~0.03 Hz)
- adaptive_ctrl yield: 4-12 pps (was 0 pps)
- Server: source=`esp32` online, last_seen_ms=44 (was 20000+), status=active
- Server: motion_level=`present_moving`, person_count=1, breathing_rate=13.3 BPM, heart_rate=~107 BPM
- HA entities: presence=on, activity=sitting, motion_energy=10.7%, person_count=1

**Stability caveat (carry-over from upstream comment):** the original filter was MGMT-only because the ESP-IDF WiFi blob can crash under sustained DATA-frame interrupt load. The 50 Hz software throttle drops excess callbacks, but the underlying HW interrupts still fire. At normal home WiFi load this is fine. Under sustained heavy load (large file transfers, 4K video streaming directly through the board's vicinity) the chip may still crash — uncertain without more testing.

### Phase D-fix — historical record (the path we took)

Changes shipped (all in `D:\dev\ruview\firmware\esp32-csi-node\main\`):
- `csi_collector.h`: added `csi_collector_start_probe_injection_timer(uint32_t period_ms)` declaration
- `csi_collector.c`: added `s_probe_inject_timer`, `probe_injection_cb`, `csi_collector_start_probe_injection_timer()`; rewrote `csi_inject_ndp_frame()` to emit a real 802.11 probe request (MGMT type 0x00, subtype 0x04) instead of the v0.6.5 placeholder Null DATA frame
- `main.c`: invoked `csi_collector_start_probe_injection_timer(100)` after `csi_collector_init()`

Rebuild via `docker run --rm -v "D:/dev/ruview/firmware/esp32-csi-node:/project" -w /project espressif/idf:v5.4 bash -lc "idf.py build"` (needs `MSYS_NO_PATHCONV=1` on Git-Bash). ESP-IDF v5.2 is too old — need v5.3+ for `esp_driver_uart` component. Build artifacts: `D:\dev\ruview\firmware\esp32-csi-node\build\{bootloader,partition_table,ota_data_initial,esp32-csi-node}.bin`.

**What works:** The 10 Hz scheduler is rock-solid — boot log shows `Probe inject heartbeat: 100/200/300/400 ok, 0 fail` every 10s. Probe-request frames build cleanly and `esp_wifi_80211_tx()` returns OK every call.

**What still doesn't work:** Despite 400+ probe requests sent over 45 s, the CSI callback fires only **once** (`CSI cb #1` at t=25.6 s). `adaptive_ctrl` shows `yield=0pps` continuously. Server side: `breathing_samples: 0`, `amp_len: 0`, classification frozen at `motion: absent, confidence: 0.0`.

**Diagnosis floor reached:** This is at the ESP-IDF WiFi-blob level — either APs aren't responding to broadcast probe requests from this STA, or `esp_wifi_80211_tx()` returns OK without actually transmitting, or CSI callbacks aren't being generated for the responses. We cannot dig deeper without external WiFi monitor-mode hardware to packet-capture what's actually on the air. Three paths forward:
1. **Wait for board 2 + use mutual illumination** (canonical RuView approach — each board's traffic is the other's CSI source) — recommended.
2. **Disable MGMT-only filter and accept DATA frames** with the 50 Hz software throttle — risky, the firmware comments warn this crashes Core 0 via `wDev_ProcessFiq`. Untested.
3. **Get a USB WiFi adapter with monitor mode** (Alfa AWUS036ACH or similar) + run on Linux to packet-capture and verify our injected probe requests are actually being transmitted and whether APs are responding.

**Pre-built bins rollback path:** `D:\dev\ruview\firmware\esp32-csi-node\release_bins\*.bin` (v0.6.5 stock) is untouched. To revert to stock firmware:
```
python -m esptool --chip esp32s3 --port COM3 --baud 460800 write-flash --flash-mode dio --flash-size detect 0x0 release_bins/bootloader.bin 0x8000 release_bins/partition-table.bin 0xf000 release_bins/ota_data_initial.bin 0x20000 release_bins/esp32-csi-node.bin
```

**WiFi credentials (for `provision.py` at flash time):**
- SSID: `Bumblebee` (2.4 GHz internal IoT network — correct band; ESP32-S3 only does 2.4 GHz)
- Password: `<see D:\dev\ha\.env RUVIEW_WIFI_PASS, or your password manager>`
- Note: re-provisioning to a different SSID later requires re-running `provision.py` with new args — no re-flashing needed.

**Once the board is detected (a new COMn port appears):**

```powershell
# Flash node_id=1 (bedroom). Replace COM_X with the actual port.
cd D:\dev\ruview\firmware\esp32-csi-node
python -m esptool --chip esp32s3 --port COM_X --baud 460800 write_flash --flash_mode dio --flash_size 8MB `
  0x0      release_bins\bootloader.bin `
  0x8000   release_bins\partition-table.bin `
  0xf000   release_bins\ota_data_initial.bin `
  0x20000  release_bins\esp32-csi-node.bin

# Provision over the same port (WiFi creds + node identity + target IP).
python provision.py --port COM_X --ssid "Bumblebee" --password "<WIFI_PASSWORD>" `
  --target-ip 192.168.1.99 --target-port 5005 --node-id 1 --edge-tier 2

# Verify boot — expect "CSI streaming active -> 192.168.1.99:5005"
python -m serial.tools.miniterm COM_X 115200
```

### Phase E — Live integration (one node initially)
1. Start UDP relay on host (NOT in Docker), keep in its own minimized PowerShell window:
   ```powershell
   python D:\dev\ruview\scripts\udp-relay.py --listen-port 5005 --forward-port 5006 --verbose
   ```
2. Bring up the ESP32-mode stack:
   ```powershell
   docker compose -f D:\dev\ruview\docker-compose.ruview.yml --env-file D:\dev\ha\.env up -d
   ```
3. Verify:
   - `curl http://localhost:3000/health` → `source: esp32`
   - Relay logs: `forwarded N pkts from 1 sources` (will become `2 sources` after board 2 is added)
   - `docker logs ruview-bridge --tail 20` — no errors, steady publishes
   - HA Developer Tools → States, filter `ruview` → 20 entities (living-room ones will mirror bedroom data until board 2 arrives — a known v1 limitation since the API fuses across nodes)

### Phase F — Deploy automations package
1. Either mount Z: via `D:\dev\ha\bin\mount.ps1` and copy `D:\dev\ha\packages\ruview_automations.yaml` to `Z:\packages\`, OR scp via:
   ```bash
   scp -i ~/.ssh/id_ed25519_personal_git D:/dev/ha/packages/ruview_automations.yaml root@192.168.1.3:/config/packages/
   ```
2. Reload HA: `D:\dev\ha\bin\ha.ps1 reload`. Helpers (`input_datetime`, `input_number`) are new — if `reload_all` doesn't pick them up, do a full `ha core restart` from SSH.
3. Verify: Settings → Automations, filter "RuView" → 12 automations enabled.

### Phase D2 — Second board
- When the second ESP32-S3 arrives, repeat Phase D with `--node-id 2` for the living room.
- Place each board in its respective room, plugged into a USB wall charger.

### Phase G — Persistence
- Register the UDP relay as a Windows scheduled task that runs at startup:
   ```powershell
   $action = New-ScheduledTaskAction -Execute "python" -Argument "D:\dev\ruview\scripts\udp-relay.py --listen-port 5005 --forward-port 5006"
   $trigger = New-ScheduledTaskTrigger -AtStartup
   $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
   Register-ScheduledTask -TaskName "RuView UDP Relay" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
   ```
- Docker containers already have `restart: unless-stopped`.

---

## 🐞 Known caveats / gotchas (don't repeat these mistakes)

| # | Issue | Mitigation |
|---|---|---|
| 1 | Source guide's bridge code had **wrong field names** — would publish zeros forever. | Field mapping fixed and documented in `mqtt_bridge.py` docstring. |
| 2 | `paho.connect()` is sync TCP only — auth failure shows up only via CONNACK callback. **Bridge would silently never publish state** on bad creds. | Added on_connect handler that fail-fasts within 5s with a clear error. |
| 3 | uptime-kuma owns host port **3001**. | RuView WS remapped to host `3011`. Update any WebSocket consumer accordingly. |
| 4 | RuView 0.6.5 has **no fall-detection REST endpoint** (`/api/v1/falls/*` returns 404). | Bridge defaults `fall_confirmed=false`. HA automation #4 (Fall Emergency) will not fire on this RuView version. Re-check on RuView upgrade. |
| 5 | `/api/v1/vital-signs` is **fused across all nodes** (not per-node). | Bridge publishes the same payload to both rooms. When `/api/v1/nodes/{id}/vitals` becomes available, split per node_id in the poll loop's TODO. |
| 6 | HA Mosquitto add-on uses HA users as MQTT logins. | The `ruview` HA user must exist before bridge starts. Already created. |
| 7 | Docker Desktop on Windows collapses multi-source UDP. | `udp-relay.py` runs on the host (NOT in Docker) and forwards :5005 → :5006 from a single source IP. The compose maps host :5006 → container :5005/udp. |
| 8 | `notify.home_assistant_deepak` is **Telegram, not iOS push**. | The `critical: 1` payload in fall/security automations is silently ignored by Telegram. Normal Telegram delivery still works. |
| 9 | esptool 5.2.0 uses `python -m esptool` invocation; the older `esptool.py` form may not be on PATH. | Use `python -m esptool ...` everywhere. |

---

## 📁 Files created/modified by this deployment

| Path | Purpose |
|---|---|
| `D:\dev\ruview\` (whole tree) | RuView clone |
| `D:\dev\ruview\docker-compose.ruview.yml` | Two-service stack (sensing-server + mqtt-bridge) |
| `D:\dev\ruview\ruview-bridge\mqtt_bridge.py` | API-correct MQTT bridge with fail-fast CONNACK |
| `D:\dev\ruview\ruview-bridge\requirements.txt` | `paho-mqtt>=1.6,<2.0`, `requests>=2.28` |
| `D:\dev\ruview\ruview-bridge\Dockerfile` | python:3.11-slim, runs the bridge |
| `D:\dev\ha\.env` (appended) | `RUVIEW_MQTT_USER`, `RUVIEW_MQTT_PASS` |
| `D:\dev\ha\packages\ruview_automations.yaml` | 12 automations + 3 helpers (staged, **not deployed** yet) |
| `D:\dev\ruview\DEPLOYMENT_STATE.md` | This document |

No changes to existing `D:\dev\ha\packages\*.yaml`. Nothing on the Pi has been modified — `/config/packages/ruview_automations.yaml` does **not** exist yet.

---

## 🛠️ Resume protocol for a fresh chat session

A future Claude can resume by:
1. Reading this file (`D:\dev\ruview\DEPLOYMENT_STATE.md`) for state.
2. Reading the original plan (`C:\Users\Draunzer\.claude\plans\witty-herding-crab.md`) for design rationale.
3. Picking up at **Phase D — Flash + provision** once the user confirms the ESP32 is now detected by Windows (a new `COMn` port appears in `python -m serial.tools.list_ports`).
4. Following the commands in the Phase D section above verbatim (substituting the actual COM port).
