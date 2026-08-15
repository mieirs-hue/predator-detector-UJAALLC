# UJAALLC Startup Technical Audit (2026-08-15)

## Scope
This audit is for the repository at:
- /home/unclejesse/predator-detector-UJAALLC

This is not the UJAALLC-FSSS-THESIS repository.

## Executive Summary
- Dashboard service: running and accepting WebSocket clients.
- Hub service: running and stable (no serial disconnect storm).
- USB hardware detected: 3 NanoESP32 boards (expected 4).
- Missing logical node: FSS-N04 remains unavailable because no configured serial path appears for it at runtime.
- Startup quality: partially clean (software stable), but not fully clean due to missing fourth board enumeration.

## Evidence
### 1) Services running
- Hub process is active: python3 jetson_engine/ujaallc_hub.py
- Dashboard process is active: uvicorn jetson_engine.dashboard_server:app

### 2) USB enumeration
Detected boards from lsusb:
- Arduino SA NanoESP32 x3

Expected:
- Arduino SA NanoESP32 x4

### 3) Serial device map observed
From /dev/serial/by-id:
- usb-Arduino_NanoESP32_28848546D968-if01 -> /dev/ttyACM0
- usb-Arduino_NanoESP32_E072A1CED858-if01 -> /dev/ttyACM2
- usb-Arduino_NanoESP32_E072A1F0A348-if01 -> /dev/ttyACM1

Missing by-id entry:
- usb-Arduino_NanoESP32_28848546D8CC-if01 (configured for FSS-N02)

### 4) Hub runtime behavior
Hub startup shows:
- FSS-N01 booted on by-id path
- FSS-N02 booted via fallback /dev/ttyACM1
- FSS-N03 booted on by-id path
- FSS-N04 repeatedly logs: "not present on any configured serial path"

Interpretation:
- Only three devices are present.
- One configured identity is missing.
- Fallback on FSS-N02 to /dev/ttyACM1 can mask identity mismatches when a board is absent.

### 5) Interference risk
- ModemManager status: active
- This can intermittently probe serial devices and is a known risk for USB-serial stability on Linux.

## Why node signal appears unstable
There are two separate classes of symptoms:
1. True missing node condition:
- If one board does not enumerate, that node remains UNKNOWN/AUDIO_OFF/no signal.

2. Identity fallback risk:
- When by-id for a node is missing, fallback /dev/ttyACM* can bind a different physical board to that logical node name.
- That can make labels and expected locations look wrong even while serial reads are technically working.

## Current technical verdict per session/component
- Dashboard session: PASS (running, websocket open, serving page)
- Hub session: PASS with warning (running, no duplicate-port contention, but missing node)
- Serial mapping session: WARN (3/4 by-id identities present)
- Fleet readiness: FAIL for full 4-node operation (hardware enumeration incomplete)

## Bench Checklist (in order)
1. Confirm 4 boards enumerate in USB:
   - lsusb | grep -Ei "Arduino SA NanoESP32"
   - Must return 4 lines.

2. Confirm 4 persistent by-id entries:
   - ls -l /dev/serial/by-id
   - Must include all expected IDs from configuration.

3. Confirm tty devices:
   - ls -l /dev/ttyACM*
   - Should show at least 4 when all four boards are connected.

4. Verify each board identity matches expected node assignment:
   - Compare physical labels vs configured IDs in:
     - jetson_engine/ujaallc_hub.py (ZONE_PORTS block)

5. If still only 3 devices:
   - Swap the suspected board's USB cable with a known-good data cable.
   - Move to a different powered USB port/hub channel.
   - Check board power LED and USB connector strain.

6. If device appears/disappears intermittently:
   - Temporarily stop ModemManager and retest.
   - Re-run startup launcher and inspect logs.

## Files to inspect quickly
- .runtime-logs/startup.log
- .runtime-logs/hub.log
- .runtime-logs/dashboard.log
- jetson_engine/ujaallc_hub.py

## Note on project identity
All checks and commands in this audit were executed against:
- /home/unclejesse/predator-detector-UJAALLC
