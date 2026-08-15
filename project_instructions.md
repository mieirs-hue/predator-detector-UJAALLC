# UJAALLC Project Instructions

This document is the current project specification for the live workspace. It is intentionally grounded in the actual code and runtime state in this repository and is the source of truth for Phase 6 development.

## 1. Source-of-truth rule

Treat the repository code as authoritative:

- Live dashboard: jetson_engine/dashboard_server.py
- Hub + serial/telemetry bridging: jetson_engine/ujaallc_hub.py
- Zone definitions and node metadata: jetson_engine/config.py
- Project overview: README.md
- Tests: tests/

Do not rebuild the system from memory. Extend the existing four-node pipeline.

## 2. Workspace state

Current layout:

- Root
  - main.py
  - README.md
  - requirements-dashboard.txt
  - scripts/
  - tests/
  - Research/
  - lighthouses/
  - jetson_engine/

Key runtime pieces:

- Dashboard web app: jetson_engine/dashboard_server.py
- Telemetry hub: jetson_engine/ujaallc_hub.py
- Dashboard mock UI: jetson_engine/dashboard_mock.py
- Node topology: jetson_engine/config.py

Observed active nodes in configuration:

- FSS-N01
- FSS-N02
- FSS-N03
- FSS-N04

## 3. Existing architecture

The project already follows a split architecture:

- ESP32 = sensing and immediate I/O
- Jetson = fusion, inference, state, logging, orchestration
- Browser = visualization and operator interface

This is the intended architectural boundary and should remain unchanged.

## 4. Current live runtime

The local workspace is already configured to run the dashboard and hub within the repo environment.

- Dashboard URL: http://127.0.0.1:8000
- Hub WebSocket: ws://127.0.0.1:8765

The dashboard server is a FastAPI app that exposes:

- / for the main dashboard
- /ws/dashboard for dashboard control and status traffic
- /ws/telemetry for telemetry stream clients

The hub starts serial handlers for the configured nodes and forwards JSON telemetry and audio commands to the visualizer/dashboard layer.

## 5. Current project rules and behavior

### 5.1 RF-first, EMA-first approach

Phase 6 should be implemented in this order:

1. RF estimated radius from calibrated RSSI
2. EMA smoothing
3. LiDAR corroboration
4. Confidence score
5. Target / zone state machine
6. Autonomous alert gating

Kalman filtering is intentionally deferred. The project should not add a state estimator before proving that a simpler model is informative.

Guiding equation:

R_t = alpha * R_raw,t + (1 - alpha) * R_t-1

Recommended starting point for experimentation:

- alpha around 0.25 to 0.35
- expose alpha as a dashboard slider
- evaluate raw radius and smoothed radius side-by-side

### 5.2 Radius terminology

The path-loss-derived value should be treated as an RF estimated radius or RF confidence radius, not as a definitive physical distance measurement.

This is important because RSSI-to-distance relationships are environment-dependent and can be distorted by:

- walls
- furniture
- multipath
- body occlusion
- antenna orientation
- local RF noise

### 5.3 Autonomous siren gating

The autonomous siren must be gated by validated fusion state, not LiDAR distance alone.

Required logic:

- RF activity
- fusion confidence check
- target confirmation
- LiDAR corroboration
- classification rule
- autonomous alert if threshold is satisfied

Unsafe logic to avoid:

- siren on only because distance < 150 cm
- siren on because RSSI crosses threshold without fusion validation
- siren on because a single noisy sensor indicates a target

The correct state path is:

RF activity -> spatial fusion -> target confirmed -> LiDAR corroboration -> threat classification -> autonomous siren

### 5.4 Privacy-safe microphone usage

The INMP441 is a sensing device but not a recording device.

Required design:

- MIC ENABLE
  - audio level/event processing only
  - no persistent audio recording
  - no long-term storage
- MIC MUTE
  - INMP441 acquisition disabled

This must remain explicit in the system architecture and dashboard labeling.

## 6. Phase 6 starting point

The first implementation sequence is:

1. Verify four-node live telemetry
2. Verify TF-Luna measurements
3. Verify INMP441 acquisition + hardware mute
4. Implement RF + LiDAR fusion state
5. Implement target / zone state machine
6. Add the neon-yellow 3D bounding volume
7. Add individual node status indicators
8. Add operator siren control
9. Add carefully gated autonomous alerting
10. Replay the complete event from JSONL
11. Only then polish the Three.js presentation

## 7. Development principles

- Extend existing code instead of replacing it.
- Do not introduce a Kalman filter before EMA proves useful.
- Keep per-node state independent; do not use a single global smoothing filter.
- Preserve the four-node spatial model and zoning logic.
- Keep human-safety logic behind validated fusion confirmation.
- Favor explicit, testable state transitions.

## 8. Immediate next step

The next implementation move is to validate the current live telemetry and measurement pipeline before adding more logic. We should first inspect the actual runtime readings from the live hub/dashboard and then implement the EMA-first fusion state in the current architecture.

## 9. Current code references

- Dashboard entry: jetson_engine/dashboard_server.py
- Hub entry: jetson_engine/ujaallc_hub.py
- Node config: jetson_engine/config.py
- Tests: tests/test_dashboard_server.py
- Live telemetry + controls: dashboard websocket logic in jetson_engine/dashboard_server.py

## 10. Working assumption for this phase

The objective for Phase 6 is not to invent a new architecture. The objective is to improve the current four-node telemetry pipeline by adding clear, observable RF confidence estimation, EMA smoothing, and validated fusion gating before any Kalman step or higher-order state estimator.
