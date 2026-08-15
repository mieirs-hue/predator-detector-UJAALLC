import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any, Dict, List, Set

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .config import ZONE_TOPOLOGY

HUB_WS_URL = os.getenv("UJAALLC_HUB_URL", "ws://127.0.0.1:8765")
NODE_ORDER = ["FSS-N01", "FSS-N02", "FSS-N03", "FSS-N04"]
LIGHTHOUSE_TO_NODE = {
  "FSS-N01": "FSS-N01",
  "FSS-N02": "FSS-N02",
  "FSS-N03": "FSS-N03",
  "FSS-N04": "FSS-N04",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
  # Keep hub relay alive while the dashboard API is running.
  relay_task = asyncio.create_task(connect_to_hub())
  try:
    yield
  finally:
    relay_task.cancel()
    with suppress(asyncio.CancelledError):
      await relay_task


app = FastAPI(title="FSS Fleet Dashboard", version="0.1.0", lifespan=lifespan)


def build_node_state(node_id: str) -> dict:
  meta = ZONE_TOPOLOGY[node_id]
  return {
    "node_id": node_id,
    "label": meta.get("label", node_id),
    "zone_id": meta.get("zone_id", node_id),
    "color": meta.get("color", "#39ff14"),
    "baseline_rssi": meta.get("baseline_rssi", -70),
    "motion_confirm_delta_cm": meta.get("motion_confirm_delta_cm", 20),
    "motion_predator_delta_cm": meta.get("motion_predator_delta_cm", 45),
    "motion_baseline_update_delta_cm": meta.get("motion_baseline_update_delta_cm", 12),
    "motion_rssi_margin": meta.get("motion_rssi_margin", 5.0),
    "motion": False,
    "motion_label": "CLEAR",
    "motion_intensity": "idle",
    "rssi": None,
    "strength": 0,
    "distance_cm": None,
    "distance_baseline_cm": None,
    "distance_baseline_samples": 0,
    "mic_rms": None,
    "sensor_status": "UNKNOWN",
    "sensor_ok": False,
    "audio_ready": False,
    "last_audio_cmd": "NONE",
    "last_audio_cmd_ms": 0,
    "last_seen": None,
    "source": "Awaiting telemetry",
    "mic_enabled": False,
    "mic_state": "MUTED",
    "siren_on": False,
    "intercom_on": False,
    "siren_manual_off_latch": False,
    "intercom_manual_off_latch": False,
    "auto_alert_engaged": False,
    "last_packet": None,
  }


def build_initial_state() -> dict:
  return {
    "type": "dashboard_state",
    "updated_at": None,
    "operation_mode": "FULLY_INTERACTIVE",
    "hub": {
      "connected": False,
      "last_seen": None,
      "status": "Waiting for telemetry",
    },
    "latest_packet": None,
    "nodes": [build_node_state(node_id) for node_id in NODE_ORDER],
  }


dashboard_clients: Set[WebSocket] = set()
dashboard_state: dict = build_initial_state()


def set_operation_mode(mode: str) -> dict | None:
  normalized = str(mode).upper()
  if normalized not in {"FULLY_INTERACTIVE", "FULLY_AUTOMATIC"}:
    return None

  dashboard_state["operation_mode"] = normalized
  dashboard_state["updated_at"] = datetime.now().isoformat()
  return dashboard_state


def get_node_state(node_id: str) -> dict | None:
  for node in dashboard_state["nodes"]:
    if node["node_id"] == node_id:
      return node
  return None


def snapshot_state(message: str | None = None) -> dict:
  payload = {
    "type": "dashboard_state",
    "updated_at": datetime.now().isoformat(),
    "hub": dict(dashboard_state["hub"]),
    "latest_packet": dashboard_state["latest_packet"],
    "nodes": [dict(node) for node in dashboard_state["nodes"]],
  }
  if message is not None:
    payload["message"] = message
  return payload


async def broadcast_state(message: str | None = None) -> None:
  if not dashboard_clients:
    return
  payload = json.dumps(snapshot_state(message))
  dead_connections: List[WebSocket] = []
  for connection in dashboard_clients:
    try:
      await connection.send_text(payload)
    except Exception:
      dead_connections.append(connection)

  for connection in dead_connections:
    dashboard_clients.discard(connection)


def update_motion_state(node: dict, packet: dict) -> None:
  zone_data = packet.get("zone_data", {}) if isinstance(packet, dict) else {}
  state = zone_data.get("state", {}) if isinstance(zone_data, dict) else {}
  raw = zone_data.get("raw_telemetry", {}) if isinstance(zone_data, dict) else {}

  distance_cm = raw.get("distance_cm")
  try:
    distance_value = int(distance_cm) if distance_cm is not None else None
  except (TypeError, ValueError):
    distance_value = None

  sensor_status = str(raw.get("status", "UNKNOWN")).upper()
  has_valid_distance = distance_value is not None and distance_value >= 0
  sensor_ok = sensor_status != "SENSOR_ERR" and has_valid_distance

  rssi = state.get("rssi")
  if rssi is None:
    rssi = raw.get("rssi")
  try:
    rssi_value = float(rssi) if rssi is not None else None
  except (TypeError, ValueError):
    rssi_value = None

  mic_rms = raw.get("mic_rms")
  try:
    mic_rms_value = float(mic_rms) if mic_rms is not None else None
  except (TypeError, ValueError):
    mic_rms_value = None

  motion_active = state.get("state") == "HOLD"
  motion_label = "CLEAR"

  confirm_delta_cm = float(node.get("motion_confirm_delta_cm", 20))
  predator_delta_cm = float(node.get("motion_predator_delta_cm", 45))
  baseline_update_delta_cm = float(node.get("motion_baseline_update_delta_cm", 12))
  rssi_margin = float(node.get("motion_rssi_margin", 5.0))

  baseline_distance = node.get("distance_baseline_cm")
  baseline_samples = int(node.get("distance_baseline_samples") or 0)
  distance_delta = None

  if has_valid_distance:
    if baseline_distance is None:
      baseline_distance = float(distance_value)
      baseline_samples = 1
    else:
      distance_delta = abs(float(distance_value) - float(baseline_distance))
      if distance_delta <= baseline_update_delta_cm:
        baseline_distance = (float(baseline_distance) * 0.85) + (float(distance_value) * 0.15)
        baseline_samples = min(baseline_samples + 1, 1000)
    node["distance_baseline_cm"] = round(float(baseline_distance), 1)
    node["distance_baseline_samples"] = baseline_samples
  else:
    node["distance_baseline_samples"] = baseline_samples

  # Primary confirmation path from TF-Luna distance.
  # Treat a steady return as background. Only distance changes away from the
  # learned baseline should enlarge the sphere, otherwise nearby furniture/walls
  # can look like a permanent predator.
  if has_valid_distance and baseline_distance is not None and baseline_samples >= 5:
    distance_delta = abs(float(distance_value) - float(baseline_distance))
    if distance_delta >= predator_delta_cm:
      motion_label = "PREDATOR_DETECTED"
      motion_active = True
    elif distance_delta >= confirm_delta_cm:
      motion_label = "CONFIRMING_TARGET"
      motion_active = True

  # Respect firmware-provided labels when present.
  if sensor_status in {"PREDATOR_DETECTED", "CONFIRMING_TARGET"}:
    motion_label = sensor_status
    motion_active = True
  elif sensor_status == "SENSOR_ERR" and not motion_active:
    motion_label = "SENSOR_ERR"

  if not motion_active and rssi_value is not None:
    motion_active = rssi_value >= float(node["baseline_rssi"]) + rssi_margin
    if motion_active and motion_label == "CLEAR":
      motion_label = "CONFIRMING_TARGET"

  # Voice-activity fallback path for firmware that publishes microphone RMS.
  if not motion_active and mic_rms_value is not None and mic_rms_value >= 0.12:
    motion_active = True
    motion_label = "CONFIRMING_TARGET"

  if not motion_active and motion_label not in {"SENSOR_ERR", "UNKNOWN"}:
    motion_label = "CLEAR"

  if motion_label != "PREDATOR_DETECTED":
    node["siren_manual_off_latch"] = False
    node["intercom_manual_off_latch"] = False

  operation_mode = str(dashboard_state.get("operation_mode", "FULLY_INTERACTIVE")).upper()
  if operation_mode == "FULLY_AUTOMATIC" and motion_label == "PREDATOR_DETECTED":
    auto_raised = False
    if not node.get("siren_on") and not node.get("siren_manual_off_latch"):
      node["siren_on"] = True
      auto_raised = True
    if not node.get("intercom_on") and not node.get("intercom_manual_off_latch"):
      node["intercom_on"] = True
      auto_raised = True
    node["auto_alert_engaged"] = bool(node.get("auto_alert_engaged")) or auto_raised
  elif node.get("auto_alert_engaged") and motion_label != "PREDATOR_DETECTED":
    node["siren_on"] = False
    node["intercom_on"] = False
    node["auto_alert_engaged"] = False

  node["motion"] = motion_active
  node["motion_label"] = motion_label
  node["motion_intensity"] = (
    "high" if motion_label == "PREDATOR_DETECTED"
    else "medium" if motion_label == "CONFIRMING_TARGET"
    else "error" if motion_label == "SENSOR_ERR"
    else "idle"
  )
  node["rssi"] = rssi_value
  node["strength"] = state.get("strength", 0)
  node["distance_cm"] = distance_value
  node["mic_rms"] = mic_rms_value
  node["sensor_status"] = sensor_status
  node["sensor_ok"] = sensor_ok
  node["audio_ready"] = bool(raw.get("audio_ready", False))
  node["last_audio_cmd"] = str(raw.get("last_audio_cmd", "NONE"))
  try:
    node["last_audio_cmd_ms"] = int(raw.get("last_audio_cmd_ms", 0) or 0)
  except (TypeError, ValueError):
    node["last_audio_cmd_ms"] = 0
  node["last_seen"] = datetime.now().isoformat()
  node["source"] = zone_data.get("lighthouse") or raw.get("node_name") or raw.get("node_id") or "unknown"
  node["last_packet"] = packet


def apply_telemetry_packet(packet: dict) -> str | None:
  zone_data = packet.get("zone_data", {}) if isinstance(packet, dict) else {}
  lighthouse = zone_data.get("lighthouse") if isinstance(zone_data, dict) else None
  node_id = LIGHTHOUSE_TO_NODE.get(lighthouse or "")
  if node_id is None:
    raw = zone_data.get("raw_telemetry", {}) if isinstance(zone_data, dict) else {}
    candidate = raw.get("node_name") or raw.get("node_id")
    if candidate in NODE_ORDER:
      node_id = candidate
  if node_id is None:
    return None

  node = get_node_state(node_id)
  if node is None:
    return None

  update_motion_state(node, packet)
  dashboard_state["hub"] = {
    "connected": True,
    "last_seen": datetime.now().isoformat(),
    "status": "Live telemetry stream",
  }
  dashboard_state["latest_packet"] = packet
  dashboard_state["updated_at"] = datetime.now().isoformat()
  return node_id


def toggle_node_feature(node_id: str, feature: str, enabled: bool | None = None) -> dict | None:
  node = get_node_state(node_id)
  if node is None:
    return None

  if feature == "mic":
    next_value = not bool(node.get("mic_enabled")) if enabled is None else bool(enabled)
    node["mic_enabled"] = next_value
    node["mic_state"] = "UNMUTED" if next_value else "MUTED"
    dashboard_state["updated_at"] = datetime.now().isoformat()
    return node

  if feature not in {"siren", "intercom"}:
    return None

  key = f"{feature}_on"
  next_value = not bool(node[key]) if enabled is None else bool(enabled)
  node[key] = next_value
  latch_key = f"{feature}_manual_off_latch"
  node[latch_key] = not next_value
  if next_value:
    node["auto_alert_engaged"] = False
  dashboard_state["updated_at"] = datetime.now().isoformat()
  return node


async def clear_node_feature_after(node_id: str, feature: str, delay_seconds: float) -> None:
  await asyncio.sleep(delay_seconds)
  node = get_node_state(node_id)
  if node is None:
    return
  key = f"{feature}_on"
  if not node.get(key):
    return
  node[key] = False
  dashboard_state["updated_at"] = datetime.now().isoformat()
  await broadcast_state(f"{node_id} {feature} reset")


def node_is_online(node: dict, max_age_seconds: int = 10) -> bool:
  last_seen = node.get("last_seen")
  if not last_seen:
    return False
  try:
    age = datetime.now() - datetime.fromisoformat(last_seen)
  except Exception:
    return False
  return age.total_seconds() <= max_age_seconds


def stage_label_for_motion(motion_label: str) -> str:
  if motion_label == "PREDATOR_DETECTED":
    return "PREDATOR DETECTED"
  if motion_label == "CONFIRMING_TARGET":
    return "MONITORING CONFIRMING TARGET"
  if motion_label == "SENSOR_ERR":
    return "SENSOR ERROR"
  return "MONITORING CLEAR"


async def send_hub_command(payload: dict) -> bool:
  try:
    async with websockets.connect(HUB_WS_URL) as websocket:
      await websocket.send(json.dumps(payload))
    return True
  except Exception as exc:
    logging.debug("[dashboard] failed to send hub command: %s", exc)
    return False


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        dead_connections: List[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except Exception:
                dead_connections.append(connection)

        for connection in dead_connections:
            self.disconnect(connection)


manager = ConnectionManager()

HTML_PAGE = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>FSS Fleet Dashboard | Neon Matrix</title>
  <script src=\"https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js\"></script>
  <script src=\"https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js\"></script>
  <style>
    :root { color-scheme: dark; --text:#e8ffe1; --muted:#89a38d; --neon:#39ff14; --cyan:#44d7ff; --amber:#ffd166; --shadow:0 0 0 1px rgba(57,255,20,.08),0 0 24px rgba(57,255,20,.12); --panel-border:rgba(57,255,20,.22); }
    html { font-size: 16px; }
    body { margin:0; min-height:100vh; overflow:hidden; font-family:"Consolas","Courier New",monospace; color:var(--text); background: radial-gradient(circle at top, rgba(57,255,20,.10), transparent 32%), radial-gradient(circle at bottom right, rgba(68,215,255,.08), transparent 28%), linear-gradient(180deg,#04070b 0%,#07110d 100%); }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; background-image: linear-gradient(rgba(57,255,20,.05) 1px, transparent 1px), linear-gradient(90deg, rgba(57,255,20,.05) 1px, transparent 1px); background-size: 100% 42px, 42px 100%; opacity:.32; }
    .shell { position:relative; max-width:1600px; height:100vh; margin:0 auto; padding:10px 14px 12px; box-sizing:border-box; display:flex; flex-direction:column; }
    .masthead { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; align-items:baseline; margin-bottom:8px; }
    h1 { margin:0; font-size:clamp(1.2rem,2.4vw,2rem); letter-spacing:.10em; text-transform:uppercase; text-shadow:0 0 10px rgba(57,255,20,.45); }
    .subtitle { color:var(--muted); font-size:.78rem; letter-spacing:.14em; text-transform:uppercase; }
    .dashboard-grid { display:grid; flex:1; min-height:0; grid-template-columns:minmax(0,70%) minmax(280px,30%); gap:10px; align-items:stretch; }
    .stack { display:grid; min-height:0; grid-template-rows:auto minmax(0,70%) minmax(0,30%); gap:10px; }
    .scene, .card, .status-card, .node-control { border:1px solid var(--panel-border); border-radius:18px; box-shadow:var(--shadow); background:linear-gradient(180deg, rgba(10,18,14,.96), rgba(6,10,8,.96)); backdrop-filter:blur(10px); }
    .scene { position:relative; min-height:0; overflow:hidden; display:flex; flex-direction:column; background: radial-gradient(circle at top, rgba(57,255,20,.10), transparent 34%), linear-gradient(180deg, rgba(8,14,11,.98), rgba(3,6,5,.98)); }
    .scene-header { display:flex; justify-content:space-between; gap:8px; padding:10px 12px 8px; }
    .scene-header h2, .card h3, .status-card h3, .node-control h4 { margin:0; text-transform:uppercase; letter-spacing:.12em; }
    .scene-header h2 { font-size:.9rem; color:var(--cyan); }
    .scene-header .hint, .status-card p, .node-meta, .control-button small { color:var(--muted); }
    #canvas-container { width:100%; flex:1; min-height:220px; }
    .status { display:inline-flex; align-items:center; gap:.5rem; color:var(--neon); font-size:.85rem; letter-spacing:.06em; text-transform:uppercase; margin-bottom:0; }
    .status::before { content:"●"; color:var(--cyan); text-shadow:0 0 10px var(--cyan); }
    .control-panel { display:grid; align-content:start; gap:8px; min-height:0; overflow:auto; padding-right:2px; }
    .speaker-control-panel { border:1px solid rgba(68,215,255,.30); border-radius:10px; background:rgba(6,18,26,.88); padding:8px; }
    .speaker-control-title { font-weight:bold; margin-bottom:6px; color:var(--cyan); letter-spacing:.10em; text-transform:uppercase; font-size:.74rem; }
    .speaker-test-button { width:100%; border:1px solid rgba(68,215,255,.55); background:linear-gradient(180deg, rgba(68,215,255,.92), rgba(30,180,220,.92)); color:#031014; border-radius:8px; padding:7px 8px; font-family:inherit; font-weight:bold; cursor:pointer; letter-spacing:.06em; text-transform:uppercase; font-size:.72rem; }
    .speaker-test-button:active { transform:translateY(1px); }
    .speaker-status { margin-top:5px; font-size:.68rem; color:var(--muted); text-align:center; min-height:1em; }
    .status-card, .card, .node-control { padding:8px; }
    .status-card h3, .card h3 { color:var(--cyan); font-size:.74rem; }
    .status-card p { margin:3px 0 0; line-height:1.25; font-size:.66rem; }
    .mode-switcher { display:grid; gap:6px; border:1px solid rgba(68,215,255,.28); border-radius:10px; background:rgba(8,18,14,.82); padding:8px; }
    .mode-switcher-title { margin:0; color:var(--amber); letter-spacing:.10em; text-transform:uppercase; font-size:.72rem; }
    .mode-switcher-actions { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
    .mode-button { appearance:none; border:1px solid rgba(57,255,20,.24); background:linear-gradient(180deg, rgba(18,31,22,.96), rgba(8,12,10,.98)); color:var(--text); border-radius:10px; padding:8px 9px; font-family:inherit; font-size:.68rem; letter-spacing:.08em; text-transform:uppercase; text-align:left; cursor:pointer; }
    .mode-button[data-active="true"] { border-color:rgba(68,215,255,.88); background:linear-gradient(180deg, rgba(68,215,255,.16), rgba(8,12,10,.98)); box-shadow:0 0 0 1px rgba(68,215,255,.16), 0 0 18px rgba(68,215,255,.14); }
    .mode-readout { color:var(--muted); font-size:.68rem; line-height:1.25; }
    .control-readout { display:grid; gap:3px; margin-bottom:6px; padding:7px; border-radius:9px; border:1px solid rgba(68,215,255,.18); background:rgba(8,14,11,.74); }
    .control-readout strong { color:var(--amber); letter-spacing:.08em; text-transform:uppercase; font-size:.66rem; }
    .control-grid { display:grid; gap:6px; }
    .node-control { background:rgba(8,14,11,.80); }
    .node-meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:.70rem; margin:6px 0 6px; }
    .node-pill { padding:2px 6px; border-radius:999px; border:1px solid rgba(68,215,255,.24); color:var(--cyan); text-transform:uppercase; letter-spacing:.08em; font-size:.62rem; }
    .control-button { appearance:none; width:100%; border:1px solid rgba(57,255,20,.28); background:linear-gradient(180deg, rgba(18,31,22,.96), rgba(8,12,10,.98)); color:var(--text); border-radius:10px; padding:8px 9px; font-family:inherit; font-size:.73rem; letter-spacing:.06em; text-transform:uppercase; text-align:left; cursor:pointer; }
    .control-button[data-active="true"] { border-color:rgba(57,255,20,.85); background:linear-gradient(180deg, rgba(57,255,20,.18), rgba(8,12,10,.98)); box-shadow:0 0 0 1px rgba(57,255,20,.20), 0 0 22px rgba(57,255,20,.16); }
    .guard-siren-button { appearance:none; width:100%; min-height:72px; border:1px solid rgba(255,193,7,.80); background:linear-gradient(180deg, rgba(255,213,79,.98), rgba(255,170,0,.92)); color:#231500; border-radius:14px; padding:12px 12px; font-family:inherit; font-size:.90rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase; text-align:left; cursor:pointer; box-shadow:0 0 0 1px rgba(255,193,7,.28), 0 0 24px rgba(255,193,7,.18); }
    .guard-siren-button[data-active="true"] { border-color:rgba(57,255,20,.82); background:linear-gradient(180deg, rgba(57,255,20,.92), rgba(23,164,71,.92)); color:#071006; box-shadow:0 0 0 1px rgba(57,255,20,.24), 0 0 30px rgba(57,255,20,.20); }
    .guard-siren-button small { display:block; margin-top:4px; font-size:.62rem; font-weight:700; letter-spacing:.05em; text-transform:none; opacity:.92; }
    .card { margin-bottom:0; }
    table { width:100%; border-collapse:collapse; font-size:.78rem; }
    th, td { text-align:left; padding:6px 6px; border-bottom:1px solid rgba(57,255,20,.14); }
    th { color:var(--amber); font-size:.64rem; letter-spacing:.10em; text-transform:uppercase; }
    tbody tr:hover { background:rgba(57,255,20,.06); }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:.70rem; line-height:1.28; color:#d8ffe0; max-height:110px; overflow:auto; }
    #nodeCardBar { display:flex; gap:6px; justify-content:center; align-items:stretch; flex-wrap:wrap; padding:6px 6px 10px; max-height:84px; overflow:auto; }
    .node-card { background:rgba(18,26,38,.88); border:1px solid #1e2d42; border-top:2px solid #00f0ff; border-radius:6px; padding:5px 7px; min-width:98px; backdrop-filter:blur(8px); }
    .card-id { font-weight:bold; font-size:.68rem; color:#fff; letter-spacing:.05em; }
    .card-zone { font-size:.57rem; text-transform:uppercase; color:#00f0ff; letter-spacing:.3px; margin-bottom:2px; }
    .card-dist { font-size:.78rem; color:var(--neon); font-variant-numeric:tabular-nums; margin-bottom:2px; }
    .card-status { font-size:.56rem; color:#4cd964; display:flex; align-items:center; gap:5px; }
    .dot { width:7px; height:7px; background:#4cd964; border-radius:50%; box-shadow:0 0 6px #4cd964; }
    .telemetry-grid { min-height:0; display:grid; grid-template-columns:minmax(0,1.4fr) minmax(0,.9fr); gap:8px; }
    .telemetry-grid .card { min-height:0; overflow:auto; }
    @media (max-width: 1100px) {
      body { overflow:auto; }
      .shell { height:auto; min-height:100vh; }
      .dashboard-grid { grid-template-columns:1fr; }
      .stack { grid-template-rows:auto minmax(420px,70vh) auto; }
      .control-panel { overflow:visible; }
      .telemetry-grid { grid-template-columns:1fr; }
      #nodeCardBar { max-height:none; }
    }
    @media (max-width: 720px) { html { font-size: 15px; } .shell { padding:10px; } }
  </style>
</head>
<body>
  <div class=\"shell\">
    <div class=\"masthead\"><h1>FSS Fleet Dashboard</h1><div class=\"subtitle\">Visual Priority Mode</div></div>
    <div class=\"dashboard-grid\">
      <div class=\"stack\">
        <div class=\"status\" id=\"status\">Connecting to edge hub…</div>
        <div class=\"scene\">
          <div class=\"scene-header\"><h2>40×40 ft Engineering Floorplan</h2><div class=\"hint\">1 unit = 1 foot · 20ft confirm radius · active zone auto-expands</div></div>
          <div id=\"canvas-container\"></div>          <div id="nodeCardBar"></div>        </div>
        <div class=\"telemetry-grid\">
          <div class=\"card\"><h3>Node Status</h3><table><thead><tr><th>Node</th><th>Motion</th><th>Signal</th><th>State</th><th>Last</th></tr></thead><tbody id=\"rows\"></tbody></table></div>
        </div>
      </div>
      <div class=\"control-panel\">
        <div class=\"status-card\"><h3>System Controls</h3><p>Confirming-target uses TF-Luna distance first and RSSI fallback when sensors are degraded.</p></div>
        <div class="mode-switcher">
          <div class="mode-switcher-title">Operating Mode</div>
          <div class="mode-switcher-actions">
            <button id="btn-mode-interactive" class="mode-button" type="button">Blue: Acoustic Subsystem</button>
            <button id="btn-mode-automatic" class="mode-button" type="button">Green: Actions Automatic</button>
          </div>
          <div id="dashboardMode" class="mode-readout">Mode: Blue acoustic standby</div>
        </div>
        <div class=\"speaker-control-panel\">
          <div class=\"speaker-control-title\">Acoustic Subsystem</div>
          <button id="btn-siren-test" class="speaker-test-button" type="button">Blue Button: Cycle Beep</button>
          <div id="speaker-status" class="speaker-status">Blue button cycles a beep</div>
        </div>
        <div class=\"control-grid\" id=\"controlPanel\"></div>
      </div>
    </div>
  </div>
  <script>
    const FLOOR_WIDTH = 40;
    const FLOOR_DEPTH = 40;
    const ROOM_WIDTH = 20;
    const ROOM_DEPTH = 20;
    const WALL_HEIGHT = 9;
    const WALL_THICKNESS = 0.25;
    const SENSOR_HEIGHT = 5; // 5-foot tripod mount
    
    const ROOMS = {
      "FSS-N01": { id: "FSS-N01", name: "OFFICE",      center: [-10, 0, -10], color: 0x00ff88 },
      "FSS-N02": { id: "FSS-N02", name: "GARAGE",      center: [10, 0, -10],  color: 0x3399ff },
      "FSS-N03": { id: "FSS-N03", name: "BABY'S ROOM", center: [-10, 0, 10],  color: 0xa0a0a0 },
      "FSS-N04": { id: "FSS-N04", name: "ENTRYWAY",    center: [10, 0, 10],   color: 0xffd700 }
    };
    
    let scene, camera, renderer, orbitControls;
    let threeInitialized = false;
    const animatedSpheres = [];
    const nodeSpheres = {};
    const lastNodeVisualState = {};
    const lastDashboardPingAt = {};
    const speakerNodes = [
      { id: 'FSS-N01', zone: 'Office' },
      { id: 'FSS-N02', zone: 'Garage' },
      { id: 'FSS-N03', zone: "Baby's Room" },
      { id: 'FSS-N04', zone: 'Entryway' },
    ];
    let latestDashboardState = null;
    let audioContext = null;
    let speakerSequenceRunning = false;

    function getAudioContext() {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return null;
      if (!audioContext) audioContext = new AudioCtx();
      return audioContext;
    }

    async function playWebSirenTone(durationMs = 500) {
      const ctx = getAudioContext();
      if (!ctx) return;
      if (ctx.state === 'suspended') {
        try {
          await ctx.resume();
        } catch (error) {
          return;
        }
      }

      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const now = ctx.currentTime;
      const duration = durationMs / 1000;

      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(400, now);
      osc.frequency.exponentialRampToValueAtTime(1400, now + duration * 0.5);
      osc.frequency.exponentialRampToValueAtTime(400, now + duration);

      gain.gain.setValueAtTime(0.001, now);
      gain.gain.linearRampToValueAtTime(0.25, now + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.001, now + duration);

      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now);
      osc.stop(now + duration);
    }

    async function playDashboardQuietPing() {
      const ctx = getAudioContext();
      if (!ctx) return;
      if (ctx.state === 'suspended') {
        try {
          await ctx.resume();
        } catch (error) {
          return;
        }
      }

      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.linearRampToValueAtTime(0.08, now + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.34);
      gain.connect(ctx.destination);

      const toneA = ctx.createOscillator();
      toneA.type = 'sine';
      toneA.frequency.setValueAtTime(1020, now);
      toneA.connect(gain);
      toneA.start(now);
      toneA.stop(now + 0.11);

      const toneB = ctx.createOscillator();
      toneB.type = 'sine';
      toneB.frequency.setValueAtTime(860, now + 0.15);
      toneB.connect(gain);
      toneB.start(now + 0.15);
      toneB.stop(now + 0.30);
    }

    function triggerPhaseTracer(nodeId, mode = 'CONFIRMING_TARGET') {
      const room = ROOMS[nodeId];
      if (!room || !room.phaseBox) return;

      const box = room.phaseBox;
      const isPredator = mode === 'PREDATOR_DETECTED';
      box.userData.mode = mode;
      box.userData.alertUntil = Date.now() + 4200;

      if (isPredator) {
        box.scale.set(1.45, 1.9, 1.45);
      } else {
        box.scale.set(1.05, 1.05, 1.05);
      }
    }

    function highlightNodeSphere(nodeId) {
      const sphere = nodeSpheres[nodeId];
      if (!sphere) return;
      sphere.userData.highlightUntil = Date.now() + 520;
    }

    function pulseControlVisual(nodeId, feature = 'ping', enabled = true) {
      const room = ROOMS[nodeId];
      if (!room) return;

      const now = Date.now();
      if (room.phaseBox) {
        room.phaseBox.userData.mode = feature === 'siren' && enabled ? 'PREDATOR_DETECTED' : 'CONTROL_ACTIVE';
        room.phaseBox.userData.alertUntil = now + (feature === 'ping' ? 900 : 2200);
        room.phaseBox.userData.controlColor = room.color;
        room.phaseBox.scale.set(
          feature === 'siren' && enabled ? 1.45 : 1.16,
          feature === 'siren' && enabled ? 1.9 : 1.2,
          feature === 'siren' && enabled ? 1.45 : 1.16,
        );
      }

      const sphere = nodeSpheres[nodeId];
      if (sphere) {
        sphere.userData.controlUntil = now + (feature === 'ping' ? 900 : 2200);
        sphere.userData.controlFeature = feature;
        sphere.userData.controlEnabled = enabled;
        sphere.userData.highlightUntil = now + (feature === 'ping' ? 900 : 1400);
      }
    }

    function isSocketOpen() {
      return socket && socket.readyState === WebSocket.OPEN;
    }

    function waitMs(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function runSpeakerSequence() {
      if (speakerSequenceRunning) return;
      speakerSequenceRunning = true;

      const statusElem = document.getElementById('speaker-status');
      const button = document.getElementById('btn-siren-test');
      if (button) {
        button.disabled = true;
        button.textContent = 'Running Speaker Sequence...';
      }

      try {
        // Run the full fleet cycle in the fixed order requested by the operator.
        // Do not narrow this to only the currently live subset, or the test can stop
        // after N02 even when the remaining nodes are valid members of the system.
        const sequenceTargets = speakerNodes;

        for (const targetNode of sequenceTargets) {
          await playWebSirenTone(500);
          highlightNodeSphere(targetNode.id);

          if (statusElem) {
            statusElem.innerHTML = `TESTING: <b style="color:#44d7ff">${targetNode.id}</b> (${targetNode.zone})`;
          }

          if (isSocketOpen()) {
            socket.send(JSON.stringify({
              type: 'speaker_test',
              event: 'speaker_test',
              nodeId: targetNode.id,
              zone: targetNode.zone,
              durationMs: 500,
              sweepStartHz: 400,
              sweepEndHz: 1400,
            }));
          }

          await waitMs(600);
        }

        if (statusElem) {
          statusElem.textContent = 'Ready to test (click to cycle nodes)';
        }
      } finally {
        speakerSequenceRunning = false;
        if (button) {
          button.disabled = false;
          button.textContent = 'Cycle Speaker Check (0.5s)';
        }
      }
    }

    function initializeThreeSceneOnce() {
      if (threeInitialized) return;
      buildNodeCards();
      initThreeJS();
      threeInitialized = true;
    }
    
    function initThreeJS() {
      const container = document.getElementById('canvas-container');
      const width = container.clientWidth;
      const height = container.clientHeight;
      
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x030605);
      scene.fog = new THREE.FogExp2(0x0b0e14, 0.008);
      
      camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
      camera.position.set(0, 38, 42);
      camera.lookAt(0, 0, 0);
      
      renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setSize(width, height);
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.shadowMap.enabled = true;
      container.appendChild(renderer.domElement);
      
      // Orbit controls — drag to rotate, scroll to zoom
      orbitControls = new THREE.OrbitControls(camera, renderer.domElement);
      orbitControls.enableDamping = true;
      orbitControls.dampingFactor = 0.05;
      orbitControls.maxPolarAngle = Math.PI / 2 - 0.05;
      
      const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
      scene.add(ambientLight);
      const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
      directionalLight.position.set(20, 30, 20);
      directionalLight.castShadow = true;
      scene.add(directionalLight);
      
      // FLOOR
      const floorGeometry = new THREE.BoxGeometry(FLOOR_WIDTH, 0.15, FLOOR_DEPTH);
      const floorMaterial = new THREE.MeshStandardMaterial({ color: 0x0f1520, roughness: 0.85, metalness: 0.05 });
      const floor = new THREE.Mesh(floorGeometry, floorMaterial);
      floor.position.set(0, -0.075, 0);
      floor.receiveShadow = true;
      scene.add(floor);
      
      // ENGINEERING GRID (1-foot spacing)
      const engineeringGrid = new THREE.GridHelper(40, 40, 0x1e2d42, 0x121a26);
      engineeringGrid.position.y = 0.01;
      scene.add(engineeringGrid);
      
      // WALLS (wireframe style matching neon aesthetic)
      const wallMaterial = new THREE.MeshBasicMaterial({ color: 0x1e3a5f, wireframe: true });
      
      function createWall(w, h, d, x, y, z) {
        const geo = new THREE.BoxGeometry(w, h, d);
        const wall = new THREE.Mesh(geo, wallMaterial);
        wall.position.set(x, y, z);
        scene.add(wall);
      }
      
      createWall(FLOOR_WIDTH, WALL_HEIGHT, WALL_THICKNESS, 0, WALL_HEIGHT/2, -20); // NORTH
      createWall(FLOOR_WIDTH, WALL_HEIGHT, WALL_THICKNESS, 0, WALL_HEIGHT/2,  20); // SOUTH
      createWall(WALL_THICKNESS, WALL_HEIGHT, FLOOR_DEPTH, -20, WALL_HEIGHT/2, 0); // WEST
      createWall(WALL_THICKNESS, WALL_HEIGHT, FLOOR_DEPTH,  20, WALL_HEIGHT/2, 0); // EAST
      createWall(WALL_THICKNESS, WALL_HEIGHT, FLOOR_DEPTH,   0, WALL_HEIGHT/2, 0); // N/S divider
      createWall(FLOOR_WIDTH, WALL_HEIGHT, WALL_THICKNESS,   0, WALL_HEIGHT/2, 0); // E/W divider
      
      // SENSOR NODES — board + pulsing monitoring sphere
      Object.values(ROOMS).forEach(room => {
        const nodeGroup = new THREE.Group();
        nodeGroup.position.set(room.center[0], SENSOR_HEIGHT, room.center[2]);
        
        // PCB board
        const boardGeo = new THREE.BoxGeometry(1.2, 0.2, 1.8);
        const boardMat = new THREE.MeshStandardMaterial({ color: 0x004d25, metalness: 0.5 });
        nodeGroup.add(new THREE.Mesh(boardGeo, boardMat));
        
        // Chip
        const chipGeo = new THREE.BoxGeometry(0.6, 0.1, 0.6);
        const chipMat = new THREE.MeshStandardMaterial({ color: 0xaaaaaa, metalness: 0.9 });
        const chip = new THREE.Mesh(chipGeo, chipMat);
        chip.position.y = 0.15;
        nodeGroup.add(chip);
        
        // Monitoring sphere (pulsing)
        const sphereGeo = new THREE.SphereGeometry(6, 32, 32);
        const sphereMat = new THREE.MeshBasicMaterial({ color: room.color, transparent: true, opacity: 0.12, wireframe: true });
        const sphere = new THREE.Mesh(sphereGeo, sphereMat);
        sphere.userData.baseScale = 1;
        nodeGroup.add(sphere);
        animatedSpheres.push({ mesh: sphere, offset: Math.random() * Math.PI * 2 });
        nodeSpheres[room.id] = sphere;
        
        // Point light
        const pointLight = new THREE.PointLight(room.color, 1.5, 18);
        nodeGroup.add(pointLight);
        
        scene.add(nodeGroup);
        room.sensorGroup = nodeGroup;
        room.sphereMesh = sphere;
        room.pointLight = pointLight;

        // Phase-10 alert tracer box (pet/human-sized visual envelope).
        const phaseBoxGeo = new THREE.BoxGeometry(2.4, 2.4, 2.4);
        const phaseBoxMat = new THREE.MeshBasicMaterial({ color: 0xffb347, wireframe: true, transparent: true, opacity: 0.0 });
        const phaseBox = new THREE.Mesh(phaseBoxGeo, phaseBoxMat);
        phaseBox.position.set(0, 1.2, 0);
        phaseBox.userData.alertUntil = 0;
        phaseBox.userData.mode = 'CLEAR';
        nodeGroup.add(phaseBox);
        room.phaseBox = phaseBox;
      });
      
      // DIMENSION LABEL
      const labelCanvas = document.createElement('canvas');
      labelCanvas.width = 256; labelCanvas.height = 64;
      const ctx = labelCanvas.getContext('2d');
      ctx.fillStyle = '#44d7ff';
      ctx.font = 'bold 36px monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('← 40 ft →', 128, 32);
      const labelTex = new THREE.CanvasTexture(labelCanvas);
      const labelSprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: labelTex }));
      labelSprite.scale.set(12, 3, 1);
      labelSprite.position.set(0, 12, -22);
      scene.add(labelSprite);
      
      window.addEventListener('resize', onWindowResize);
      animate();
    }
    
    function onWindowResize() {
      const container = document.getElementById('canvas-container');
      camera.aspect = container.clientWidth / container.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(container.clientWidth, container.clientHeight);
    }
    
    let animTime = 0;
    function animate() {
      requestAnimationFrame(animate);
      animTime += 0.03;
      animatedSpheres.forEach((item) => {
        const dynamicScale = item.mesh.userData.dynamicScale || 1;
        const boost = item.mesh.userData.highlightUntil && Date.now() < item.mesh.userData.highlightUntil ? 1.2 : 1;
        const s = dynamicScale * boost * (1 + Math.sin(animTime + item.offset) * 0.05);
        item.mesh.scale.set(s, s, s);
        item.mesh.rotation.y += 0.003;
      });

      const nowMs = Date.now();
      Object.values(ROOMS).forEach((room) => {
        if (!room.phaseBox) return;
        const box = room.phaseBox;
        const alertUntil = box.userData.alertUntil || 0;
        const active = nowMs < alertUntil;
        if (!active) {
          box.material.opacity *= 0.85;
          return;
        }

        const t = (alertUntil - nowMs) / 1000;
        const blink = 0.35 + Math.abs(Math.sin(animTime * 4.3)) * 0.55;
        box.material.opacity = Math.min(0.85, blink * (0.55 + Math.max(0, t) * 0.1));
        if (box.userData.mode === 'PREDATOR_DETECTED') {
          box.material.color.setHex(0xff3b30);
        } else if (box.userData.mode === 'CONTROL_ACTIVE') {
          box.material.color.setHex(box.userData.controlColor || 0xffb347);
        } else {
          box.material.color.setHex(0xffb347);
        }
      });

      orbitControls.update();
      renderer.render(scene, camera);
    }
    
    // ── Node status cards (bottom bar driven by live telemetry) ──────────────
    function buildNodeCards() {
      const bar = document.getElementById('nodeCardBar');
      bar.innerHTML = '';
      Object.values(ROOMS).forEach(room => {
        const card = document.createElement('div');
        card.className = 'node-card';
        card.id = `card-${room.id}`;
        card.innerHTML = `
          <div class="card-id">${room.id}</div>
          <div class="card-zone">${room.name}</div>
          <div class="card-dist" id="dist-${room.id}">— cm</div>
          <div class="card-status" id="dot-${room.id}"><span class="dot"></span> AWAITING</div>`;
        bar.appendChild(card);
      });
    }
    
    function updateNodeCard(nodeId, packet) {
      const distEl = document.getElementById(`dist-${nodeId}`);
      const dotEl  = document.getElementById(`dot-${nodeId}`);
      if (distEl) distEl.textContent = packet.distance_cm >= 0 ? `${packet.distance_cm} cm` : 'ERR';
      if (dotEl)  dotEl.innerHTML = `<span class="dot"></span> ${packet.status || 'LIVE'}`;
    }

    function updateRoomVisual(node) {
      const room = ROOMS[node.node_id];
      if (!room || !room.sphereMesh) return;

      const sphere = room.sphereMesh;
      const pointLight = room.pointLight;
      const baseColor = room.color;
      const hasFreshData = node.last_seen && (Date.now() - new Date(node.last_seen).getTime()) < 5000;
      const isActive = Boolean(node.motion);
      const isConfirming = node.motion_label === 'CONFIRMING_TARGET';
      const isPredator = node.motion_label === 'PREDATOR_DETECTED';
      const controlActive = sphere.userData.controlUntil && Date.now() < sphere.userData.controlUntil;
      const sirenActive = Boolean(node.siren_on);
      const intercomActive = Boolean(node.intercom_on);

      sphere.material.color.setHex(baseColor);
      if (controlActive && sirenActive) {
        sphere.material.opacity = 0.48;
        sphere.userData.dynamicScale = 1.56;
      } else if (controlActive && intercomActive) {
        sphere.material.opacity = 0.34;
        sphere.userData.dynamicScale = 1.32;
      } else if (controlActive) {
        sphere.material.opacity = 0.28;
        sphere.userData.dynamicScale = 1.22;
      } else if (isPredator) {
        sphere.material.opacity = 0.42;
        sphere.userData.dynamicScale = 1.48;
      } else if (isConfirming) {
        sphere.material.opacity = 0.30;
        sphere.userData.dynamicScale = 1.28;
      } else if (node.sensor_ok) {
        sphere.material.opacity = isActive ? 0.24 : 0.12;
        sphere.userData.dynamicScale = 1.04;
      } else if (hasFreshData) {
        sphere.material.opacity = 0.08;
        sphere.userData.dynamicScale = 0.98;
      } else {
        sphere.material.opacity = 0.04;
        sphere.userData.dynamicScale = 0.92;
      }

      if (pointLight) {
        pointLight.intensity = controlActive ? (sirenActive ? 3.2 : 2.0) : isPredator ? 2.9 : isConfirming ? 2.1 : (hasFreshData ? 0.9 : 0.35);
      }
    }
    
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${location.host}/ws/dashboard`);
    const statusEl = document.getElementById('status');
    const rowsEl = document.getElementById('rows');
    const controlPanelEl = document.getElementById('controlPanel');
    const dashboardModeEl = document.getElementById('dashboardMode');
    const modeInteractiveButton = document.getElementById('btn-mode-interactive');
    const modeAutomaticButton = document.getElementById('btn-mode-automatic');
    const sirenTestButton = document.getElementById('btn-siren-test');
    const nodeControlBindings = {};
    const nodeRowBindings = {};

    function normalizeModeLabel(mode) {
      return String(mode || 'FULLY_INTERACTIVE').replace(/_/g, ' ');
    }

    function formatMotionLabel(label) {
      if (label === 'PREDATOR_DETECTED') return 'PREDATOR DETECTED';
      if (label === 'CONFIRMING_TARGET') return 'MONITORING CONFIRMING TARGET';
      if (label === 'SENSOR_ERR') return 'SENSOR ERROR';
      return 'MONITORING CLEAR';
    }

    function sendDashboardMode(mode) {
      if (!isSocketOpen()) {
        if (dashboardModeEl) {
          dashboardModeEl.textContent = 'Dashboard socket not connected';
        }
        return;
      }
      socket.send(JSON.stringify({ type: 'dashboard_mode', mode }));
      if (dashboardModeEl) {
        dashboardModeEl.textContent = `Requesting ${normalizeModeLabel(mode)}`;
      }
    }

    if (modeInteractiveButton) {
      modeInteractiveButton.addEventListener('click', () => sendDashboardMode('FULLY_INTERACTIVE'));
    }
    if (modeAutomaticButton) {
      modeAutomaticButton.addEventListener('click', () => sendDashboardMode('FULLY_AUTOMATIC'));
    }

    if (sirenTestButton) {
      sirenTestButton.addEventListener('click', () => {
        runSpeakerSequence().catch(() => {
          const statusElem = document.getElementById('speaker-status');
          if (statusElem) {
            statusElem.textContent = 'Audio context unavailable or command failed';
          }
        });
      });
    }

    function formatAgo(timestamp) {
      if (!timestamp) return 'no signal';
      const deltaMs = Date.now() - new Date(timestamp).getTime();
      if (Number.isNaN(deltaMs) || deltaMs < 1000) return 'live';
      if (deltaMs < 60000) return `${Math.round(deltaMs / 1000)}s ago`;
      return `${Math.round(deltaMs / 60000)}m ago`;
    }

    function sendControl(nodeId, feature, enabled) {
      if (!isSocketOpen()) {
        if (dashboardModeEl) {
          dashboardModeEl.textContent = 'Dashboard socket not connected';
        }
        return;
      }
      pulseControlVisual(nodeId, feature, Boolean(enabled));
      socket.send(JSON.stringify({ type: 'control_command', nodeId, feature, enabled }));
    }

    function ensureNodeControls(nodes) {
      for (const node of nodes) {
        if (!nodeControlBindings[node.node_id]) {
          const nodeControl = document.createElement('div');
          nodeControl.className = 'node-control';

          const title = document.createElement('h4');
          title.textContent = `${node.node_id} · ${node.label}`;

          const meta = document.createElement('div');
          meta.className = 'node-meta';
          const pill = document.createElement('span');
          pill.className = 'node-pill';
          const source = document.createElement('span');
          meta.appendChild(pill);
          meta.appendChild(source);

          const controls = document.createElement('div');
          controls.className = 'control-grid';

          const micButton = document.createElement('button');
          micButton.className = 'control-button';
          micButton.addEventListener('click', () => {
            const current = nodeControlBindings[node.node_id]?.state;
            const enabled = !(current && current.mic_enabled);
            sendControl(node.node_id, 'mic', enabled);
          });

          const sirenButton = document.createElement('button');
          sirenButton.className = 'guard-siren-button';
          sirenButton.addEventListener('click', () => {
            // Siren is an operator-triggered one-shot command.
            sendControl(node.node_id, 'siren', true);
          });

          const intercomButton = document.createElement('button');
          intercomButton.className = 'control-button';
          intercomButton.addEventListener('click', () => {
            const current = nodeControlBindings[node.node_id]?.state;
            sendControl(node.node_id, 'intercom', !Boolean(current?.intercom_on));
          });

          const pingButton = document.createElement('button');
          pingButton.className = 'control-button';
          pingButton.addEventListener('click', () => {
            sendControl(node.node_id, 'ping', true);
          });

          controls.appendChild(micButton);
          controls.appendChild(sirenButton);
          controls.appendChild(intercomButton);
          controls.appendChild(pingButton);
          nodeControl.appendChild(title);
          nodeControl.appendChild(meta);
          nodeControl.appendChild(controls);
          controlPanelEl.appendChild(nodeControl);

          const row = document.createElement('tr');
          const colNode = document.createElement('td');
          const colMotion = document.createElement('td');
          const colSignal = document.createElement('td');
          const colState = document.createElement('td');
          const colLast = document.createElement('td');
          row.appendChild(colNode);
          row.appendChild(colMotion);
          row.appendChild(colSignal);
          row.appendChild(colState);
          row.appendChild(colLast);
          rowsEl.appendChild(row);

          nodeControlBindings[node.node_id] = { nodeControl, pill, source, micButton, sirenButton, intercomButton, pingButton, state: null };
          nodeRowBindings[node.node_id] = { colNode, colMotion, colSignal, colState, colLast };
        }
      }
    }

    function updateNodeControl(node) {
      const ui = nodeControlBindings[node.node_id];
      if (!ui) return;

      ui.state = node;
      ui.pill.textContent = formatMotionLabel(node.motion_label);
      const audioStamp = node.last_audio_cmd && node.last_audio_cmd !== 'NONE'
        ? `audio:${node.last_audio_cmd}`
        : 'audio:none';
      ui.source.textContent = `${node.source} · ${audioStamp}`;

      const lastSeenMs = node.last_seen ? new Date(node.last_seen).getTime() : 0;
      const online = Boolean(lastSeenMs) && (Date.now() - lastSeenMs) <= 10000;

      const micEnabled = online ? Boolean(node.mic_enabled) : false;
      ui.micButton.disabled = !online;
      ui.micButton.dataset.active = String(micEnabled);
      ui.micButton.innerHTML = `${micEnabled ? 'Mic Unmute' : 'Mic Mute'}<small>${online ? `Mic sensor state for ${node.label}.` : 'Offline: no recent telemetry'}</small>`;

      ui.sirenButton.disabled = !online;
      ui.intercomButton.disabled = !online;
      ui.pingButton.disabled = !online;

      const sirenState = online ? Boolean(node.siren_on) : false;
      ui.sirenButton.dataset.active = String(sirenState);
      ui.sirenButton.innerHTML = `${sirenState ? 'Actions Automatic' : 'Sirens Disabled'}<small>${online ? (sirenState ? `Automatic response active for ${node.label}. Security guard can click to return to silence.` : `Ready for ${node.label}. Predator alerts will light the button and beep.`) : 'Offline: no recent telemetry'}</small>`;

      const intercomState = online ? Boolean(node.intercom_on) : false;
      ui.intercomButton.dataset.active = String(intercomState);
      ui.intercomButton.innerHTML = `${intercomState ? 'Intercom On' : 'Intercom Off'}<small>${online ? `Two-way talkback for ${node.label}.` : 'Offline: no recent telemetry'}</small>`;

      ui.pingButton.dataset.active = 'false';
      ui.pingButton.innerHTML = `Quiet Ping<small>${online ? `Detection notification ping for ${node.label}.` : 'Offline: no recent telemetry'}</small>`;
    }

    function updateNodeRow(node) {
      const row = nodeRowBindings[node.node_id];
      if (!row) return;

      row.colNode.textContent = node.node_id;
      row.colMotion.textContent = formatMotionLabel(node.motion_label);
      row.colSignal.textContent = node.distance_cm != null && node.distance_cm >= 0
        ? `${node.distance_cm} cm`
        : (node.rssi ?? 'n/a');
      const cmd = node.last_audio_cmd && node.last_audio_cmd !== 'NONE' ? node.last_audio_cmd : '-';
      const audio = node.audio_ready ? 'AUDIO_OK' : 'AUDIO_OFF';
      row.colState.textContent = `${node.sensor_status || 'CLEAR'} · ${audio} · ${cmd}`;
      row.colLast.textContent = formatAgo(node.last_seen);
    }

    function renderDashboard(state) {
      latestDashboardState = state;
      const nodes = state.nodes || [];
      const activeNodes = nodes.filter((node) => node.motion).map((node) => node.label);
      const operationMode = String(state.operation_mode || 'FULLY_INTERACTIVE').toUpperCase();
      const modeLabel = normalizeModeLabel(operationMode);
      statusEl.textContent = state.hub?.connected ? (activeNodes.length ? `Connected · ${modeLabel} · ${activeNodes.join(', ')} active` : `Connected · ${modeLabel} · all rooms clear`) : 'Waiting for telemetry…';
      if (dashboardModeEl) {
        dashboardModeEl.textContent = operationMode === 'FULLY_AUTOMATIC' ? 'Mode: Green actions automatic' : 'Mode: Blue acoustic standby';
      }
      if (modeInteractiveButton) {
        modeInteractiveButton.dataset.active = String(operationMode === 'FULLY_INTERACTIVE');
      }
      if (modeAutomaticButton) {
        modeAutomaticButton.dataset.active = String(operationMode === 'FULLY_AUTOMATIC');
      }
      ensureNodeControls(nodes);
      for (const node of nodes) {
        const previousLabel = lastNodeVisualState[node.node_id] || 'CLEAR';
        const enteringAlert = (
          node.motion_label === 'CONFIRMING_TARGET' || node.motion_label === 'PREDATOR_DETECTED'
        ) && !['CONFIRMING_TARGET', 'PREDATOR_DETECTED'].includes(previousLabel);

        if (enteringAlert) {
          triggerPhaseTracer(node.node_id, node.motion_label);

          // Attention nudge at the dashboard display (Roku/desk), not on node speaker.
          const now = Date.now();
          const lastPing = lastDashboardPingAt[node.node_id] || 0;
          if (now - lastPing > 4000) {
            lastDashboardPingAt[node.node_id] = now;
            playDashboardQuietPing();
          }
        }

        lastNodeVisualState[node.node_id] = node.motion_label;
        updateNodeControl(node);
        updateNodeRow(node);
        updateRoomVisual(node);
      }

      // update node cards with live distance
      const latestRaw = state.latest_packet?.zone_data?.raw_telemetry;
      if (latestRaw && latestRaw.node_id) {
        updateNodeCard(latestRaw.node_id, latestRaw);
      }
    }

    socket.onopen = () => { 
      if (dashboardModeEl) {
        dashboardModeEl.textContent = 'Blue acoustic standby';
      }
      initializeThreeSceneOnce();
    };
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'dashboard_state') {
          renderDashboard(message);
        } else if (message.type === 'control_ack') {
          if (dashboardModeEl) {
            dashboardModeEl.textContent = message.message;
          }
        }
      } catch (error) {
        if (dashboardModeEl) {
          dashboardModeEl.textContent = 'Dashboard message parse error';
        }
      }
    };
    socket.onerror = () => { statusEl.textContent = 'Dashboard socket error'; };
    socket.onclose = () => { statusEl.textContent = 'Dashboard socket closed'; };

    // Render 3D scene even if websocket connect is delayed.
    initializeThreeSceneOnce();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(HTML_PAGE)


@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    dashboard_clients.add(websocket)
    await websocket.send_text(json.dumps(snapshot_state("connected")))
    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            command_type = message.get("type")
            if command_type == "speaker_test":
              command_payload = {
                "event": "speaker_test",
                "node_id": message.get("nodeId"),
                "zone": message.get("zone"),
                "duration_ms": int(message.get("durationMs", 500)),
                "sweep_start_hz": int(message.get("sweepStartHz", 400)),
                "sweep_end_hz": int(message.get("sweepEndHz", 1400)),
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(command_payload)
              if ok:
                await websocket.send_text(json.dumps({
                  "type": "control_ack",
                  "message": f"Speaker test dispatched to {command_payload['node_id']}",
                }))
                await broadcast_state(f"Speaker test on {command_payload['node_id']}")
              else:
                await websocket.send_text(json.dumps({
                  "type": "control_ack",
                  "message": "Speaker test failed to reach hub",
                }))
              continue

            if command_type == "dashboard_mode":
              requested_mode = message.get("mode")
              updated_state = set_operation_mode(requested_mode)
              if updated_state is None:
                await websocket.send_text(json.dumps({
                  "type": "control_ack",
                  "message": f"Ignored dashboard mode {requested_mode}",
                }))
                continue

              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"Dashboard mode set to {updated_state['operation_mode']}",
              }))
              await broadcast_state(f"dashboard mode {updated_state['operation_mode']}")
              continue

            if command_type != "control_command":
              continue

            node_id = message.get("nodeId")
            feature = message.get("feature")
            enabled = message.get("enabled")

            existing = get_node_state(node_id)
            if existing is None:
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"Ignored {feature} command for {node_id}",
              }))
              continue

            if feature in {"siren", "intercom", "ping", "mic"} and not node_is_online(existing):
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} is offline (no recent telemetry). Command blocked.",
              }))
              await broadcast_state(f"{node_id} offline: {feature} not sent")
              continue

            if feature == "mic":
              updated_node = toggle_node_feature(node_id, "mic", bool(enabled))
              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": "mic",
                "enabled": bool(updated_node["mic_enabled"]),
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(audio_payload)
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} mic {'enabled' if updated_node['mic_enabled'] else 'muted'} {'sent' if ok else 'failed'}",
              }))
              if ok:
                await broadcast_state(f"{node_id} mic {updated_node['mic_state']}")
              continue

            if feature == "ping":
              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": "ping",
                "enabled": True,
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(audio_payload)
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} quiet ping {'sent' if ok else 'failed'}",
              }))
              if ok:
                await broadcast_state(f"{node_id} ping sent")
              continue

            if feature == "siren":
              node = get_node_state(node_id)
              if node is None:
                await websocket.send_text(json.dumps({
                  "type": "control_ack",
                  "message": f"Ignored siren command for {node_id}",
                }))
                continue

              node["siren_on"] = True
              node["siren_manual_off_latch"] = False
              node["auto_alert_engaged"] = False
              dashboard_state["updated_at"] = datetime.now().isoformat()

              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": "siren",
                "enabled": True,
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(audio_payload)
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} siren {'sent' if ok else 'failed'}",
              }))
              if ok:
                asyncio.create_task(clear_node_feature_after(node_id, "siren", 2.8))
                await broadcast_state(f"{node_id} siren sent")
              else:
                node["siren_on"] = False
                dashboard_state["updated_at"] = datetime.now().isoformat()
                await broadcast_state(f"{node_id} siren failed")
              continue

            updated_node = toggle_node_feature(node_id, feature, enabled)
            if updated_node is None:
                await websocket.send_text(json.dumps({
                    "type": "control_ack",
                    "message": f"Ignored {feature} command for {node_id}",
                }))
                continue

            await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} {feature} set to {'on' if updated_node[f'{feature}_on'] else 'off'}",
            }))

            if feature == "intercom":
              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": feature,
                "enabled": bool(updated_node[f"{feature}_on"]),
                "issued_at": datetime.now().isoformat(),
              }
              await send_hub_command(audio_payload)
              if bool(updated_node[f"{feature}_on"]):
                asyncio.create_task(clear_node_feature_after(node_id, feature, 1.2))

            await broadcast_state(f"{node_id} {feature} updated")
    except WebSocketDisconnect:
        dashboard_clients.discard(websocket)
    except Exception:
        dashboard_clients.discard(websocket)


async def connect_to_hub() -> None:
    while True:
        try:
            async with websockets.connect(HUB_WS_URL) as websocket:
                async for raw_message in websocket:
                    try:
                        payload = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue
                    # Pass full hub envelope — apply_telemetry_packet reads zone_data internally
                    applied_node = apply_telemetry_packet(payload)
                    if applied_node is not None:
                        await broadcast_state(f"{applied_node} updated")
        except Exception as exc:
            logging.debug("[dashboard] hub not reachable: %s", exc)
        await asyncio.sleep(10)


