import asyncio
import json
import logging
import math
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

# Node 3-D positions (Three.js units; 1 unit = 1 foot) for RF fusion centroid
NODE_POSITIONS: dict = {
    "FSS-N01": (-10.0, 5.0, -10.0),
    "FSS-N02": (10.0, 5.0, -10.0),
    "FSS-N03": (-10.0, 5.0, 10.0),
    "FSS-N04": (10.0, 5.0, 10.0),
}
NODE_COMPASS: dict = {"FSS-N01": "NORTH", "FSS-N02": "EAST", "FSS-N03": "SOUTH", "FSS-N04": "WEST"}

# RF visualization parameters — initial values, not thesis-validated physical measurements
RF_ALPHA: float = 0.20
RF_R_MIN: float = 2.0               # minimum confidence sphere radius (ft)
RF_R_MAX: float = 20.0              # maximum confidence sphere radius (ft)
RF_DISTURBANCE_THRESHOLD: float = 8.0    # dB from baseline before confidence rises
RF_DISTURBANCE_SCALE: float = 15.0       # dB range mapping 0→1 confidence
RF_CONFIRM_THRESHOLD: float = 0.50       # smoothed confidence level for CONFIRMING_TARGET
RF_BUZZER_COOLDOWN_S: float = 3.0        # minimum seconds between per-node RF buzzer events
RF_MIN_FUSION_WEIGHT: float = 0.30       # minimum sum(C_i) to compute fusion centroid

# RF path-loss localization — log-distance model for indoor 2.4 GHz
RF_PATH_LOSS_EXP: float = 2.5       # indoor exponent (2.0 = free space, 3–4 = obstructed)
RF_RSSI_1M: float = -40.0           # reference RSSI at 1 metre (WiFi 2.4 GHz typical)
RF_RANGE_SIGMA_FT: float = 5.0      # Gaussian ring uncertainty per range estimate (ft)
RF_GRID_CELLS: int = 8              # cells per side on 40×40 ft floor (8×8 = 64 cells)
RF_CELL_FT: float = 40.0 / RF_GRID_CELLS   # = 5 ft per cell
RF_MULTILATERATION_ITERS: int = 12  # gradient-descent iterations for position refinement


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
    # RF disturbance model (confidence 0→1; not measured physical distance)
    "rf_baseline": None,
    "rf_confidence_raw": 0.0,
    "rf_confidence_smooth": 0.0,
    "rf_sphere_radius": RF_R_MIN,
    "rf_state": "NORMAL",
    "rf_last_buzzer_at": 0.0,
    # Directional inference — deferred; UNKNOWN until spatial evidence supports it
    "direction": "UNKNOWN",
    "direction_confidence": 0.0,
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
    "rf_fusion": {
        "state": "NO_FUSION",
        "confidence": 0.0,
        "position": None,
        "active_nodes": [],
        "dominant_node": None,
    },
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
    "rf_fusion": dict(dashboard_state.get("rf_fusion", {})),
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


def update_rf_state(node: dict, rssi_value: float | None) -> None:
  """Update per-node RF confidence, sphere radius, and rf_state using EMA disturbance model.

  C_raw = clamp((|rssi - baseline| - threshold) / scale, 0, 1)
  C_smooth = alpha * C_raw + (1 - alpha) * C_prev
  R = R_min + C_smooth * (R_max - R_min)
  """
  if rssi_value is None:
    return

  meta = ZONE_TOPOLOGY.get(node["node_id"], {})
  threshold = float(meta.get("rf_disturbance_threshold", RF_DISTURBANCE_THRESHOLD))
  scale = float(meta.get("rf_disturbance_scale", RF_DISTURBANCE_SCALE))

  if node["rf_baseline"] is None:
    node["rf_baseline"] = rssi_value
    return

  # Baseline adapts slowly only while NORMAL to avoid chasing an active disturbance
  if node["rf_state"] == "NORMAL":
    node["rf_baseline"] = node["rf_baseline"] * 0.98 + rssi_value * 0.02

  d = abs(rssi_value - node["rf_baseline"])
  c_raw = max(0.0, min(1.0, (d - threshold) / max(scale, 1e-6)))
  c_prev = float(node.get("rf_confidence_smooth", 0.0))
  c_smooth = RF_ALPHA * c_raw + (1.0 - RF_ALPHA) * c_prev

  node["rf_confidence_raw"] = round(c_raw, 4)
  node["rf_confidence_smooth"] = round(c_smooth, 4)
  node["rf_sphere_radius"] = round(RF_R_MIN + c_smooth * (RF_R_MAX - RF_R_MIN), 2)
  node["rf_state"] = "CONFIRMING_TARGET" if c_smooth >= RF_CONFIRM_THRESHOLD else "NORMAL"


def compute_rf_fusion(nodes: list) -> dict:
  """Weighted spatial centroid from per-node RF confidence.

  X = sum(C_i * X_i) / sum(C_i), same for Y and Z.
  Only computed when sum(C_i) >= RF_MIN_FUSION_WEIGHT.
  """
  total_weight = 0.0
  wx = wy = wz = 0.0
  active: list = []
  dominant_node = None
  dominant_conf = 0.0

  for node in nodes:
    nid = node["node_id"]
    pos = NODE_POSITIONS.get(nid)
    if pos is None:
      continue
    c = float(node.get("rf_confidence_smooth", 0.0))
    if c <= 0.0:
      continue
    wx += c * pos[0]
    wy += c * pos[1]
    wz += c * pos[2]
    total_weight += c
    active.append(nid)
    if c > dominant_conf:
      dominant_conf = c
      dominant_node = nid

  if total_weight < RF_MIN_FUSION_WEIGHT:
    return {
      "state": "NO_FUSION",
      "confidence": round(total_weight, 4),
      "position": None,
      "active_nodes": [],
      "dominant_node": None,
    }

  n = max(len(active), 1)
  return {
    "state": "ACTIVE",
    "confidence": round(total_weight / n, 4),
    "position": [round(wx / total_weight, 2), round(wy / total_weight, 2), round(wz / total_weight, 2)],
    "active_nodes": active,
    "dominant_node": dominant_node,
  }


def rssi_to_distance_ft(rssi: float) -> float:
  """Log-distance path loss: d = 10^((RSSI_1m - RSSI) / (10 * n)) metres → feet."""
  d_m = 10.0 ** ((RF_RSSI_1M - rssi) / (10.0 * RF_PATH_LOSS_EXP))
  return max(0.5, min(d_m * 3.28084, 30.0))  # clamp to [0.5, 30] ft


def compute_rf_position_estimate(nodes: list) -> dict:
  """Iterative weighted multilateration from per-node WiFi RSSI.

  Each node with real RSSI (not -99) contributes a distance ring.
  Gradient descent minimises sum(w_i * (d_actual - d_estimated)^2).
  Direction remains UNKNOWN — directional estimator is a future milestone.
  """
  anchors: list = []  # (x_ft, z_ft, d_est_ft, weight)

  for node in nodes:
    nid = node["node_id"]
    pos = NODE_POSITIONS.get(nid)
    if pos is None:
      continue
    rssi = node.get("rssi")
    if rssi is None or float(rssi) == -99.0:
      continue
    c = float(node.get("rf_confidence_smooth", 0.0))
    if c < 0.05:
      continue
    d_ft = rssi_to_distance_ft(float(rssi))
    anchors.append((pos[0], pos[2], d_ft, c))

  if len(anchors) < 2:
    return {
      "state": "INSUFFICIENT_NODES",
      "position_2d": None,
      "position_3d": None,
      "confidence": 0.0,
      "range_estimates": [],
      "direction": "UNKNOWN",
      "direction_confidence": 0.0,
    }

  # Initialise at confidence-weighted centroid
  tw = sum(a[3] for a in anchors)
  cx = sum(a[3] * a[0] for a in anchors) / tw
  cz = sum(a[3] * a[2] for a in anchors) / tw

  # Gradient descent: pull estimate toward each node's distance ring
  for _ in range(RF_MULTILATERATION_ITERS):
    gx = gz = gw = 0.0
    for ax, az, d_est, w in anchors:
      d_act = math.sqrt((cx - ax) ** 2 + (cz - az) ** 2)
      if d_act < 1e-3:
        continue
      err = d_act - d_est
      gx += w * err * (cx - ax) / d_act
      gz += w * err * (cz - az) / d_act
      gw += w
    if gw < 1e-6:
      break
    cx -= 0.5 * gx / gw
    cz -= 0.5 * gz / gw

  cx = max(-19.0, min(19.0, cx))
  cz = max(-19.0, min(19.0, cz))

  range_estimates = [
    {"node": nodes[i]["node_id"], "distance_ft": round(anchors[i][2], 2)}
    for i in range(len(anchors))
    if i < len(nodes)
  ]

  return {
    "state": "ACTIVE",
    "position_2d": [round(cx, 2), round(cz, 2)],
    "position_3d": [round(cx, 2), 0.0, round(cz, 2)],
    "confidence": round(tw / max(len(anchors), 1), 4),
    "range_estimates": range_estimates,
    "direction": "UNKNOWN",
    "direction_confidence": 0.0,
  }


def compute_rf_probability_field(nodes: list) -> list:
  """8×8 spatial probability field over 40×40 ft floor.

  For every cell (col, row) compute:
    P = product over active nodes of: exp(-0.5 * ((d_cell - d_est) / sigma)^2)
  where d_cell = Euclidean distance from cell centre to node position,
        d_est  = path-loss distance estimate from RSSI.

  Returns list of [col, row, normalised_probability] for cells with P >= 0.05.
  Each cell is RF_CELL_FT × RF_CELL_FT (5 ft × 5 ft).
  """
  active = [
    n for n in nodes
    if NODE_POSITIONS.get(n["node_id"]) is not None
    and n.get("rssi") is not None
    and float(n.get("rssi", -99)) != -99.0
    and float(n.get("rf_confidence_smooth", 0.0)) >= 0.05
  ]
  if not active:
    return []

  sigma = RF_RANGE_SIGMA_FT
  raw: list = []
  max_p = 0.0

  for col in range(RF_GRID_CELLS):
    for row in range(RF_GRID_CELLS):
      cx = -20.0 + (col + 0.5) * RF_CELL_FT
      cz = -20.0 + (row + 0.5) * RF_CELL_FT
      p = 1.0
      for node in active:
        pos = NODE_POSITIONS[node["node_id"]]
        d_est = rssi_to_distance_ft(float(node["rssi"]))
        d_cell = math.sqrt((cx - pos[0]) ** 2 + (cz - pos[2]) ** 2)
        p *= math.exp(-0.5 * ((d_cell - d_est) / sigma) ** 2)
      raw.append((col, row, p))
      if p > max_p:
        max_p = p

  if max_p < 1e-12:
    return []

  return [
    [c, r, round(p / max_p, 4)]
    for c, r, p in raw
    if (p / max_p) >= 0.05
  ]


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
  # Store WiFi and BLE RSSI separately for diagnostics and path-loss model
  node["wifi_rssi"] = raw.get("wifi_rssi", rssi_value)
  node["ble_rssi"] = raw.get("ble_rssi", -99)
  node["ble_count"] = int(raw.get("ble_count", 0) or 0)
  try:
    node["last_audio_cmd_ms"] = int(raw.get("last_audio_cmd_ms", 0) or 0)
  except (TypeError, ValueError):
    node["last_audio_cmd_ms"] = 0
  node["last_seen"] = datetime.now().isoformat()
  node["source"] = zone_data.get("lighthouse") or raw.get("node_name") or raw.get("node_id") or "unknown"
  node["last_packet"] = packet

  # Firmware sends no RSSI field; synthesize a proximity signal from TF-Luna deviation.
  # Remove once hardware RF sensor is added to firmware.
  if (rssi_value is None or rssi_value == -99.0) and has_valid_distance:
    dist_baseline = node.get("distance_baseline_cm")
    if dist_baseline is not None:
      dev = abs(float(distance_value) - float(dist_baseline))
      # 0 dev → -70 (quiet); 30+ cm dev → -40 (max disturbance)
      rssi_value = -70.0 + min(dev, 30.0)
    else:
      rssi_value = -70.0

  update_rf_state(node, rssi_value)


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
  dashboard_state["rf_fusion"] = compute_rf_fusion(dashboard_state["nodes"])
  dashboard_state["rf_fusion"]["position_estimate"] = compute_rf_position_estimate(dashboard_state["nodes"])
  dashboard_state["rf_fusion"]["probability_field"] = compute_rf_probability_field(dashboard_state["nodes"])
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
    /* Phase 6 — per-node RF card styles */
    .node-header { display:flex; align-items:baseline; justify-content:space-between; gap:8px; margin-bottom:5px; }
    .compass-badge { font-size:.60rem; color:var(--amber); letter-spacing:.14em; text-transform:uppercase; border:1px solid rgba(255,209,102,.35); border-radius:4px; padding:1px 5px; }
    .rf-data-row, .tf-data-row, .last-event-row { font-size:.68rem; display:flex; align-items:center; gap:5px; flex-wrap:wrap; margin-bottom:3px; padding:4px 6px; border-radius:6px; background:rgba(6,14,10,.72); border:1px solid rgba(57,255,20,.10); }
    .data-label { color:var(--muted); letter-spacing:.08em; text-transform:uppercase; font-size:.60rem; min-width:52px; }
    .data-sep { color:rgba(255,255,255,.18); margin:0 2px; }
    .rf-field-val { color:var(--neon); font-variant-numeric:tabular-nums; min-width:34px; font-size:.72rem; }
    .rf-sphere-val { color:var(--cyan); letter-spacing:.06em; text-transform:uppercase; font-size:.62rem; }
    .rf-sphere-val.confirming { color:var(--amber); text-shadow:0 0 8px rgba(255,209,102,.5); }
    .tf-luna-val { color:var(--neon); font-size:.70rem; font-variant-numeric:tabular-nums; }
    .last-event-row { border-color:rgba(68,215,255,.08); }
    .last-event-val { color:var(--cyan); font-size:.62rem; }
    .node-buttons { display:grid; gap:4px; margin-bottom:4px; }
    .intercom-future-btn { opacity:0.38; cursor:not-allowed !important; }
    .intercom-future-btn:hover { opacity:0.38; }
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
          <div class=\"card\"><h3>RF Diagnostics</h3><pre id=\"rfDiag\" style=\"font-size:.62rem;color:#d8ffe0;line-height:1.50;max-height:140px;overflow:auto\">\u2014</pre></div>
        </div>
      </div>
      <div class=\"control-panel\">
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
      "FSS-N01": { id: "FSS-N01", name: "OFFICE",      compass: "NORTH", center: [-10, 0, -10], color: 0x00ff88 },
      "FSS-N02": { id: "FSS-N02", name: "GARAGE",      compass: "EAST",  center: [10, 0, -10],  color: 0x3399ff },
      "FSS-N03": { id: "FSS-N03", name: "BABY'S ROOM", compass: "SOUTH", center: [-10, 0, 10],  color: 0xa0a0a0 },
      "FSS-N04": { id: "FSS-N04", name: "ENTRYWAY",    compass: "WEST",  center: [10, 0, 10],   color: 0xffd700 }
    };

    // 8×8 RF probability heatmap (5 ft per cell on 40×40 ft floor)
    const RF_GRID = 8;
    const RF_CELL = 40 / RF_GRID;
    const heatmapMeshes = {};  // key: "col_row"
    let rfPositionMarker = null;
    
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
    const lastRfState = {};        // nodeId -> previous rf_state for buzzer transition
    const rfBuzzerCooldownAt = {}; // nodeId -> ms timestamp of last RF buzzer
    const lastEvents = {};         // nodeId -> last event string for card display

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

    // RF guard buzzer — fires once per NORMAL→CONFIRMING_TARGET transition (3s cooldown)
    async function playRfBuzzer() {
      const ctx = getAudioContext();
      if (!ctx) return;
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch { return; } }
      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.linearRampToValueAtTime(0.15, now + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 1.0);
      gain.connect(ctx.destination);
      const osc = ctx.createOscillator();
      osc.type = 'square';
      osc.frequency.setValueAtTime(180, now);
      osc.frequency.setValueAtTime(360, now + 0.5);
      osc.connect(gain);
      osc.start(now);
      osc.stop(now + 1.0);
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

      const isPingFeature = feature === 'ping' || feature === 'quiet_ping';

      const now = Date.now();
      if (room.phaseBox) {
        room.phaseBox.userData.mode = feature === 'siren' && enabled ? 'PREDATOR_DETECTED' : 'CONTROL_ACTIVE';
        room.phaseBox.userData.alertUntil = now + (isPingFeature ? 900 : 2200);
        room.phaseBox.userData.controlColor = room.color;
        room.phaseBox.scale.set(
          feature === 'siren' && enabled ? 1.45 : 1.16,
          feature === 'siren' && enabled ? 1.9 : 1.2,
          feature === 'siren' && enabled ? 1.45 : 1.16,
        );
      }

      const sphere = nodeSpheres[nodeId];
      if (sphere) {
        sphere.userData.controlUntil = now + (isPingFeature ? 900 : 2200);
        sphere.userData.controlFeature = feature;
        sphere.userData.controlEnabled = enabled;
        sphere.userData.highlightUntil = now + (isPingFeature ? 900 : 1400);
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

    function renderSceneError(message) {
      const container = document.getElementById('canvas-container');
      if (!container) return;
      container.innerHTML = '';
      const panel = document.createElement('div');
      panel.style.margin = '14px';
      panel.style.padding = '12px';
      panel.style.border = '1px solid rgba(255,209,102,0.5)';
      panel.style.borderRadius = '10px';
      panel.style.background = 'rgba(27,18,0,0.55)';
      panel.style.color = '#ffd166';
      panel.style.fontSize = '0.78rem';
      panel.style.lineHeight = '1.45';
      panel.innerHTML = `<strong>3D scene unavailable.</strong><br>${message}`;
      container.appendChild(panel);
      const status = document.getElementById('status');
      if (status) {
        status.textContent = '3D unavailable - check Chromium GPU/WebGL flags';
      }
    }
    
    function initThreeJS() {
      const container = document.getElementById('canvas-container');
      const width = container.clientWidth;
      const height = container.clientHeight;

      if (!window.THREE) {
        renderSceneError('Three.js did not load. Verify network access to CDN or bundle vendor scripts locally.');
        return;
      }

      const webglProbe = document.createElement('canvas');
      const webglOk = !!(webglProbe.getContext('webgl') || webglProbe.getContext('experimental-webgl'));
      if (!webglOk) {
        renderSceneError('WebGL is disabled in Chromium. On Jetson try: chromium --ignore-gpu-blocklist --enable-gpu-rasterization --use-gl=egl');
        return;
      }
      
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x030605);
      scene.fog = new THREE.FogExp2(0x0b0e14, 0.008);
      
      camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
      camera.position.set(0, 38, 42);
      camera.lookAt(0, 0, 0);
      
      try {
        renderer = new THREE.WebGLRenderer({ antialias: true });
      } catch (error) {
        renderSceneError('Chromium could not initialize WebGLRenderer. Ensure GPU acceleration is enabled for Jetson display output.');
        return;
      }
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

      // RF probability heatmap — 8×8 grid of 5×5 ft cells lying on the floor
      for (let col = 0; col < RF_GRID; col++) {
        for (let row = 0; row < RF_GRID; row++) {
          const cx = -20 + (col + 0.5) * RF_CELL;
          const cz = -20 + (row + 0.5) * RF_CELL;
          const geo = new THREE.PlaneGeometry(RF_CELL - 0.3, RF_CELL - 0.3);
          const mat = new THREE.MeshBasicMaterial({ color: 0xff2200, transparent: true, opacity: 0.0, side: THREE.DoubleSide, depthWrite: false });
          const cell = new THREE.Mesh(geo, mat);
          cell.rotation.x = -Math.PI / 2;
          cell.position.set(cx, 0.04, cz);
          scene.add(cell);
          heatmapMeshes[`${col}_${row}`] = cell;
        }
      }

      // RF position marker — glowing sphere at multilateration estimate
      const pmGeo = new THREE.SphereGeometry(1.2, 16, 16);
      const pmMat = new THREE.MeshBasicMaterial({ color: 0xff4444, transparent: true, opacity: 0.0, wireframe: false });
      rfPositionMarker = new THREE.Mesh(pmGeo, pmMat);
      rfPositionMarker.position.set(0, 1.2, 0);
      const pmRing = new THREE.Mesh(new THREE.RingGeometry(1.8, 2.2, 32), new THREE.MeshBasicMaterial({ color: 0xff4444, transparent: true, opacity: 0.0, side: THREE.DoubleSide }));
      pmRing.rotation.x = -Math.PI / 2;
      rfPositionMarker.add(pmRing);
      scene.add(rfPositionMarker);
      
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

        // RF confidence wireframe box — direction UNKNOWN; expands with RF disturbance confidence
        const rfBoxGeo = new THREE.BoxGeometry(3, 3, 3);
        const rfBoxMat = new THREE.MeshBasicMaterial({ color: 0xffd700, wireframe: true, transparent: true, opacity: 0.0 });
        const rfBox = new THREE.Mesh(rfBoxGeo, rfBoxMat);
        rfBox.position.set(0, 1.5, 0);
        rfBox.userData.rfState = 'NORMAL';
        rfBox.userData.rfConfidence = 0.0;
        rfBox.userData.rfRadius = 2.0;
        rfBox.userData.direction = 'UNKNOWN';
        nodeGroup.add(rfBox);
        room.rfBox = rfBox;
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

      // RF confidence box animation — yellow wireframe, direction always UNKNOWN for now
      Object.values(ROOMS).forEach((room) => {
        const rfBox = room.rfBox;
        if (!rfBox) return;
        const rfState = rfBox.userData.rfState || 'NORMAL';
        const rfConf = rfBox.userData.rfConfidence || 0.0;
        const rfRadius = rfBox.userData.rfRadius || 2.0;
        if (rfState === 'CONFIRMING_TARGET') {
          rfBox.material.color.setHex(0xffe44a);
          rfBox.material.opacity = 0.38 + Math.abs(Math.sin(animTime * 3.0)) * 0.40;
        } else if (rfConf > 0.05) {
          rfBox.material.color.setHex(0xffd700);
          rfBox.material.opacity = 0.05 + rfConf * 0.22;
        } else {
          rfBox.material.opacity = Math.max(0.0, rfBox.material.opacity - 0.04);
        }
        const boxScale = Math.max(0.33, rfRadius / 6.0);
        rfBox.scale.set(boxScale, boxScale, boxScale);
        rfBox.rotation.y += 0.007;
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
      const hasFreshData = node.last_seen && (Date.now() - new Date(node.last_seen).getTime()) < 5000;
      const isPredator = node.motion_label === 'PREDATOR_DETECTED';
      const rfState = node.rf_state || 'NORMAL';
      const rfRadius = node.rf_sphere_radius != null ? node.rf_sphere_radius : 2.0;
      const rfConf = node.rf_confidence_smooth || 0.0;
      const controlActive = sphere.userData.controlUntil && Date.now() < sphere.userData.controlUntil;
      const sirenActive = Boolean(node.siren_on);

      // RF confidence drives sphere scale (base geometry radius = 6 ft)
      sphere.userData.dynamicScale = Math.max(0.33, rfRadius / 6.0);
      sphere.material.color.setHex(room.color);

      if (controlActive && sirenActive) {
        sphere.material.opacity = 0.48;
        sphere.userData.dynamicScale = Math.max(sphere.userData.dynamicScale, 1.56);
      } else if (isPredator) {
        sphere.material.opacity = 0.42;
        sphere.userData.dynamicScale = Math.max(sphere.userData.dynamicScale, 1.48);
      } else if (rfState === 'CONFIRMING_TARGET') {
        sphere.material.opacity = 0.26;
      } else if (hasFreshData) {
        sphere.material.opacity = rfConf > 0.05 ? 0.18 : 0.12;
      } else {
        sphere.material.opacity = 0.04;
        sphere.userData.dynamicScale = 0.33;
      }

      if (pointLight) {
        pointLight.intensity = sirenActive ? 3.2 : isPredator ? 2.9 : rfState === 'CONFIRMING_TARGET' ? 2.0 : hasFreshData ? 0.9 : 0.35;
      }

      // Push RF data into the rfBox for the animate loop
      if (room.rfBox) {
        room.rfBox.userData.rfState = rfState;
        room.rfBox.userData.rfConfidence = rfConf;
        room.rfBox.userData.rfRadius = rfRadius;
        room.rfBox.userData.direction = node.direction || 'UNKNOWN';
      }
    }

    // Update 8×8 probability heatmap and multilateration position marker
    function updateRfHeatmap(rfFusion) {
      if (!rfFusion) return;

      // Fade all cells
      for (const mesh of Object.values(heatmapMeshes)) {
        mesh.material.opacity = Math.max(0, mesh.material.opacity - 0.06);
      }

      const field = rfFusion.probability_field;
      if (Array.isArray(field) && field.length > 0) {
        for (const [col, row, prob] of field) {
          const mesh = heatmapMeshes[`${col}_${row}`];
          if (!mesh) continue;
          // Heat colour: dark red → orange → yellow (low → high probability)
          const r = 1.0;
          const g = prob > 0.5 ? (prob - 0.5) * 2.0 : 0.0;
          mesh.material.color.setRGB(r, g, 0.0);
          mesh.material.opacity = Math.max(mesh.material.opacity, prob * 0.60);
        }
      }

      // Position marker from multilateration
      const pe = rfFusion.position_estimate;
      if (rfPositionMarker && pe && pe.state === 'ACTIVE' && pe.position_3d) {
        const [px, py, pz] = pe.position_3d;
        rfPositionMarker.position.set(px, 1.2, pz);
        rfPositionMarker.material.opacity = Math.min(0.92, pe.confidence * 1.4);
        const ring = rfPositionMarker.children[0];
        if (ring) ring.material.opacity = rfPositionMarker.material.opacity * 0.55;
        rfPositionMarker.material.color.setHex(pe.confidence > 0.5 ? 0xff2222 : 0xff8844);
      } else if (rfPositionMarker) {
        rfPositionMarker.material.opacity = Math.max(0, rfPositionMarker.material.opacity - 0.04);
        if (rfPositionMarker.children[0]) rfPositionMarker.children[0].material.opacity = Math.max(0, rfPositionMarker.children[0].material.opacity - 0.04);
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
          const compass = ROOMS[node.node_id]?.compass || '';
          const nodeControl = document.createElement('div');
          nodeControl.className = 'node-control';

          // Header: node ID + compass direction
          nodeControl.innerHTML = `
            <div class=\"node-header\">
              <h4 style=\"margin:0;font-size:.78rem;letter-spacing:.08em\">${node.node_id}</h4>
              <span class=\"compass-badge\">${compass}</span>
            </div>
            <div class=\"rf-data-row\">
              <span class=\"data-label\">RF FIELD</span>
              <span class=\"rf-field-val\" id=\"rfv-${node.node_id}\">0.00</span>
              <span class=\"data-sep\">\u00b7</span>
              <span class=\"data-label\">SPHERE</span>
              <span class=\"rf-sphere-val\" id=\"rfs-${node.node_id}\">NORMAL</span>
            </div>
            <div class=\"tf-data-row\">
              <span class=\"data-label\">TF-LUNA</span>
              <span class=\"tf-luna-val\" id=\"tfl-${node.node_id}\">READY</span>
            </div>
            <div class=\"node-buttons\" id=\"btns-${node.node_id}\"></div>
            <div class=\"last-event-row\">Last Event: <span class=\"last-event-val\" id=\"lev-${node.node_id}\">\u2014</span></div>
          `;
          controlPanelEl.appendChild(nodeControl);

          const btnsContainer = nodeControl.querySelector(`#btns-${node.node_id}`);

          const micButton = document.createElement('button');
          micButton.className = 'control-button';
          micButton.addEventListener('click', () => {
            const current = nodeControlBindings[node.node_id]?.state;
            sendControl(node.node_id, 'mic', !(current && current.mic_enabled));
          });

          const sirenButton = document.createElement('button');
          sirenButton.className = 'guard-siren-button';
          sirenButton.addEventListener('click', () => {
            sendControl(node.node_id, 'siren', true);
          });

          // Intercom: FUTURE USE — no click handler, visually distinct
          const intercomButton = document.createElement('button');
          intercomButton.className = 'control-button intercom-future-btn';
          intercomButton.disabled = true;
          intercomButton.innerHTML = 'INTERCOM<small>Future use \u2014 not yet active</small>';

          btnsContainer.appendChild(micButton);
          btnsContainer.appendChild(sirenButton);
          btnsContainer.appendChild(intercomButton);

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

          nodeControlBindings[node.node_id] = { nodeControl, micButton, sirenButton, intercomButton, state: null };
          nodeRowBindings[node.node_id] = { colNode, colMotion, colSignal, colState, colLast };
        }
      }
    }

    function updateNodeControl(node) {
      const ui = nodeControlBindings[node.node_id];
      if (!ui) return;
      ui.state = node;

      const lastSeenMs = node.last_seen ? new Date(node.last_seen).getTime() : 0;
      const online = Boolean(lastSeenMs) && (Date.now() - lastSeenMs) <= 10000;

      // RF row
      const rfFieldEl = document.getElementById(`rfv-${node.node_id}`);
      const rfSphereEl = document.getElementById(`rfs-${node.node_id}`);
      if (rfFieldEl) rfFieldEl.textContent = (node.rf_confidence_smooth ?? 0).toFixed(2);
      if (rfSphereEl) {
        const rfState = node.rf_state || 'NORMAL';
        rfSphereEl.textContent = rfState;
        rfSphereEl.className = rfState === 'CONFIRMING_TARGET' ? 'rf-sphere-val confirming' : 'rf-sphere-val';
      }

      // TF-Luna row
      const tfEl = document.getElementById(`tfl-${node.node_id}`);
      if (tfEl) {
        if (!online) {
          tfEl.textContent = 'OFFLINE';
        } else if (node.distance_cm != null && node.distance_cm >= 0) {
          tfEl.textContent = `${node.distance_cm} cm`;
        } else {
          tfEl.textContent = 'READY';
        }
      }

      // Mic button
      const micEnabled = online ? Boolean(node.mic_enabled) : false;
      ui.micButton.disabled = !online;
      ui.micButton.dataset.active = String(micEnabled);
      ui.micButton.innerHTML = `\uD83C\uDFA4 MIC: ${micEnabled ? 'UNMUTED' : 'MUTED'}<small>${online ? '' : ' \u2014 offline'}</small>`;

      // Siren button
      const sirenState = online ? Boolean(node.siren_on) : false;
      ui.sirenButton.disabled = !online;
      ui.sirenButton.dataset.active = String(sirenState);
      ui.sirenButton.innerHTML = `\uD83D\uDD0A SIREN: ${sirenState ? 'ACTIVE' : 'OFF'}<small>${sirenState ? 'Click to silence' : (online ? 'Ready' : '\u2014 offline')}</small>`;

      // Last event
      const levEl = document.getElementById(`lev-${node.node_id}`);
      if (levEl && lastEvents[node.node_id]) {
        levEl.textContent = lastEvents[node.node_id];
      }
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

          // TF-Luna chime — fires on TF-Luna-based state transition
          const now = Date.now();
          const lastPing = lastDashboardPingAt[node.node_id] || 0;
          if (now - lastPing > 4000) {
            lastDashboardPingAt[node.node_id] = now;
            playDashboardQuietPing();
            const ts = new Date().toLocaleTimeString();
            lastEvents[node.node_id] = `TF-LUNA ${node.motion_label} \u00b7 ${ts}`;
          }
        }

        // RF guard buzzer — fires once on NORMAL → CONFIRMING_TARGET RF transition
        const prevRfState = lastRfState[node.node_id] || 'NORMAL';
        const currRfState = node.rf_state || 'NORMAL';
        if (prevRfState === 'NORMAL' && currRfState === 'CONFIRMING_TARGET') {
          const now = Date.now();
          const lastBuzz = rfBuzzerCooldownAt[node.node_id] || 0;
          if (now - lastBuzz >= 3000) {
            rfBuzzerCooldownAt[node.node_id] = now;
            playRfBuzzer();
            const ts = new Date().toLocaleTimeString();
            lastEvents[node.node_id] = `RF CONFIRMING_TARGET \u00b7 ${ts}`;
          }
        }
        lastRfState[node.node_id] = currRfState;

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

      // RF diagnostics developer panel
      const rfDiagEl = document.getElementById('rfDiag');
      if (rfDiagEl) {
        const lines = ['NODE     CONF   WIFI_RSSI  BLE_RSSI  STATE'];
        lines.push('\u2500'.repeat(50));
        for (const node of nodes) {
          const conf = (node.rf_confidence_smooth ?? 0).toFixed(3);
          const rfState = (node.rf_state || 'NORMAL').padEnd(18);
          const wRssi = (node.wifi_rssi ?? node.rssi ?? -99).toString().padStart(5);
          const bRssi = (node.ble_rssi ?? -99).toString().padStart(5);
          lines.push(`${node.node_id.padEnd(8)} ${conf}  ${wRssi}dBm  ${bRssi}dBm  ${rfState}`);
        }
        lines.push('');
        const fusion = state.rf_fusion;
        if (fusion) {
          lines.push(`FUSION : ${fusion.state}`);
          if (fusion.position) {
            lines.push(`  CENTROID  X ${fusion.position[0].toFixed(1).padStart(6)}  Z ${fusion.position[2].toFixed(1).padStart(6)}`);
          }
          lines.push(`  CONF     ${(fusion.confidence ?? 0).toFixed(3)}`);
          if (fusion.active_nodes?.length) {
            lines.push(`  ACTIVE   ${fusion.active_nodes.join(', ')}`);
            lines.push(`  DOMINANT ${fusion.dominant_node || '\u2014'}`);
          }
          const pe = fusion.position_estimate;
          if (pe && pe.state === 'ACTIVE') {
            lines.push('');
            lines.push(`MULTILATERATION`);
            lines.push(`  POSITION  X ${pe.position_2d[0].toFixed(1).padStart(6)} ft   Z ${pe.position_2d[1].toFixed(1).padStart(6)} ft`);
            lines.push(`  CONF     ${pe.confidence.toFixed(3)}  DIR: ${pe.direction}`);
            if (pe.range_estimates?.length) {
              for (const r of pe.range_estimates) {
                lines.push(`  ${r.node.padEnd(8)} ~${r.distance_ft.toFixed(1)} ft`);
              }
            }
          }
        }
        rfDiagEl.textContent = lines.join('\n');
      }

      updateRfHeatmap(state.rf_fusion);
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

            if feature in {"siren", "loud_siren_test", "intercom", "ping", "quiet_ping", "mic"} and not node_is_online(existing):
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} is offline (no recent telemetry). Command blocked.",
              }))
              await broadcast_state(f"{node_id} offline: {feature} not sent")
              continue

            if feature == "loud_siren_test":
              if node_id not in {"FSS-N03", "FSS-N04"}:
                await websocket.send_text(json.dumps({
                  "type": "control_ack",
                  "message": f"{node_id} loud siren test is restricted to N03/N04",
                }))
                continue

              node = get_node_state(node_id)
              if node is not None:
                node["siren_on"] = True
                node["siren_manual_off_latch"] = False
                node["auto_alert_engaged"] = False
                dashboard_state["updated_at"] = datetime.now().isoformat()

              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": "loud_siren_test",
                "enabled": True,
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(audio_payload)
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} loud siren test {'sent' if ok else 'failed'}",
              }))
              if ok:
                asyncio.create_task(clear_node_feature_after(node_id, "siren", 8.5))
                await broadcast_state(f"{node_id} loud siren test sent")
              else:
                if node is not None:
                  node["siren_on"] = False
                  dashboard_state["updated_at"] = datetime.now().isoformat()
                await broadcast_state(f"{node_id} loud siren test failed")
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

            if feature == "quiet_ping":
              audio_payload = {
                "event": "node_audio_command",
                "node_id": node_id,
                "feature": "quiet_ping",
                "enabled": True,
                "issued_at": datetime.now().isoformat(),
              }
              ok = await send_hub_command(audio_payload)
              await websocket.send_text(json.dumps({
                "type": "control_ack",
                "message": f"{node_id} quiet ping {'sent' if ok else 'failed'}",
              }))
              if ok:
                await broadcast_state(f"{node_id} quiet ping sent")
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


