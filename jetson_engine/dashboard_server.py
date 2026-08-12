import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Set

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .config import ZONE_TOPOLOGY

app = FastAPI(title="FSS Fleet Dashboard", version="0.1.0")
HUB_WS_URL = os.getenv("UJAALLC_HUB_URL", "ws://127.0.0.1:8765")
NODE_ORDER = ["FSS-N01", "FSS-N02", "FSS-N03", "FSS-N04"]
LIGHTHOUSE_TO_NODE = {
  "FSS-N01": "FSS-N01",
  "FSS-N02": "FSS-N02",
  "FSS-N03": "FSS-N03",
  "FSS-N04": "FSS-N04",
}


def build_node_state(node_id: str) -> dict:
  meta = ZONE_TOPOLOGY[node_id]
  return {
    "node_id": node_id,
    "label": meta.get("label", node_id),
    "zone_id": meta.get("zone_id", node_id),
    "color": meta.get("color", "#39ff14"),
    "baseline_rssi": meta.get("baseline_rssi", -70),
    "motion": False,
    "motion_label": "CLEAR",
    "motion_intensity": "idle",
    "rssi": None,
    "strength": 0,
    "last_seen": None,
    "source": "Awaiting telemetry",
    "siren_on": False,
    "intercom_on": False,
    "last_packet": None,
  }


def build_initial_state() -> dict:
  return {
    "type": "dashboard_state",
    "updated_at": None,
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

  rssi = state.get("rssi")
  if rssi is None:
    rssi = raw.get("rssi")
  try:
    rssi_value = float(rssi) if rssi is not None else None
  except (TypeError, ValueError):
    rssi_value = None

  motion_active = state.get("state") == "HOLD"
  if not motion_active and rssi_value is not None:
    motion_active = rssi_value >= float(node["baseline_rssi"]) + 5.0

  node["motion"] = motion_active
  node["motion_label"] = "MOTION" if motion_active else "CLEAR"
  node["motion_intensity"] = "high" if motion_active else "idle"
  node["rssi"] = rssi_value
  node["strength"] = state.get("strength", 0)
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
  if node is None or feature not in {"siren", "intercom"}:
    return None

  key = f"{feature}_on"
  next_value = not bool(node[key]) if enabled is None else bool(enabled)
  node[key] = next_value
  node["last_seen"] = datetime.now().isoformat()
  dashboard_state["updated_at"] = datetime.now().isoformat()
  return node


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
    html { font-size: 18px; }
    body { margin:0; min-height:100vh; font-family:"Consolas","Courier New",monospace; color:var(--text); background: radial-gradient(circle at top, rgba(57,255,20,.10), transparent 32%), radial-gradient(circle at bottom right, rgba(68,215,255,.08), transparent 28%), linear-gradient(180deg,#04070b 0%,#07110d 100%); }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; background-image: linear-gradient(rgba(57,255,20,.05) 1px, transparent 1px), linear-gradient(90deg, rgba(57,255,20,.05) 1px, transparent 1px); background-size: 100% 42px, 42px 100%; opacity:.32; }
    .shell { position:relative; max-width:1600px; margin:0 auto; padding:28px; }
    .masthead { display:flex; flex-wrap:wrap; gap:12px; justify-content:space-between; align-items:baseline; margin-bottom:20px; }
    h1 { margin:0; font-size:clamp(2.2rem,5vw,4rem); letter-spacing:.12em; text-transform:uppercase; text-shadow:0 0 12px rgba(57,255,20,.6); }
    .subtitle { color:var(--muted); font-size:1rem; letter-spacing:.18em; text-transform:uppercase; }
    .dashboard-grid { display:grid; grid-template-columns:minmax(0,2.1fr) minmax(340px,.9fr); gap:18px; align-items:start; }
    .stack { display:grid; gap:18px; }
    .scene, .card, .status-card, .node-control { border:1px solid var(--panel-border); border-radius:18px; box-shadow:var(--shadow); background:linear-gradient(180deg, rgba(10,18,14,.96), rgba(6,10,8,.96)); backdrop-filter:blur(10px); }
    .scene { position:relative; min-height:520px; overflow:hidden; background: radial-gradient(circle at top, rgba(57,255,20,.10), transparent 34%), linear-gradient(180deg, rgba(8,14,11,.98), rgba(3,6,5,.98)); }
    .scene-header { display:flex; justify-content:space-between; gap:12px; padding:20px 20px 12px; }
    .scene-header h2, .card h3, .status-card h3, .node-control h4 { margin:0; text-transform:uppercase; letter-spacing:.12em; }
    .scene-header h2 { font-size:1.2rem; color:var(--cyan); }
    .scene-header .hint, .status-card p, .node-meta, .control-button small { color:var(--muted); }
    #canvas-container { width:100%; height:440px; }
    .status { display:inline-flex; align-items:center; gap:.6rem; color:var(--neon); font-size:1.05rem; letter-spacing:.08em; text-transform:uppercase; margin-bottom:20px; }
    .status::before { content:"●"; color:var(--cyan); text-shadow:0 0 10px var(--cyan); }
    .control-panel { position:sticky; top:18px; display:grid; gap:18px; }
    .speaker-control-panel { border:1px solid rgba(68,215,255,.30); border-radius:14px; background:rgba(6,18,26,.88); padding:14px; }
    .speaker-control-title { font-weight:bold; margin-bottom:8px; color:var(--cyan); letter-spacing:.12em; text-transform:uppercase; font-size:.9rem; }
    .speaker-test-button { width:100%; border:1px solid rgba(68,215,255,.55); background:linear-gradient(180deg, rgba(68,215,255,.92), rgba(30,180,220,.92)); color:#031014; border-radius:10px; padding:10px 12px; font-family:inherit; font-weight:bold; cursor:pointer; letter-spacing:.08em; text-transform:uppercase; }
    .speaker-test-button:active { transform:translateY(1px); }
    .speaker-status { margin-top:8px; font-size:.78rem; color:var(--muted); text-align:center; min-height:1.2em; }
    .status-card, .card, .node-control { padding:16px; }
    .status-card h3, .card h3 { color:var(--cyan); font-size:1rem; }
    .status-card p { margin:10px 0 0; line-height:1.45; font-size:.95rem; }
    .control-readout { display:grid; gap:8px; margin-bottom:14px; padding:14px; border-radius:14px; border:1px solid rgba(68,215,255,.18); background:rgba(8,14,11,.74); }
    .control-readout strong { color:var(--amber); letter-spacing:.12em; text-transform:uppercase; font-size:.9rem; }
    .control-grid { display:grid; gap:12px; }
    .node-control { background:rgba(8,14,11,.80); }
    .node-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:.88rem; margin:8px 0 12px; }
    .node-pill { padding:4px 8px; border-radius:999px; border:1px solid rgba(68,215,255,.24); color:var(--cyan); text-transform:uppercase; letter-spacing:.12em; font-size:.74rem; }
    .control-button { appearance:none; width:100%; border:1px solid rgba(57,255,20,.28); background:linear-gradient(180deg, rgba(18,31,22,.96), rgba(8,12,10,.98)); color:var(--text); border-radius:14px; padding:14px 16px; font-family:inherit; font-size:1rem; letter-spacing:.10em; text-transform:uppercase; text-align:left; cursor:pointer; }
    .control-button[data-active="true"] { border-color:rgba(57,255,20,.85); background:linear-gradient(180deg, rgba(57,255,20,.18), rgba(8,12,10,.98)); box-shadow:0 0 0 1px rgba(57,255,20,.20), 0 0 22px rgba(57,255,20,.16); }
    .card { margin-bottom:0; }
    table { width:100%; border-collapse:collapse; font-size:1.02rem; }
    th, td { text-align:left; padding:12px 10px; border-bottom:1px solid rgba(57,255,20,.14); }
    th { color:var(--amber); font-size:.9rem; letter-spacing:.14em; text-transform:uppercase; }
    tbody tr:hover { background:rgba(57,255,20,.06); }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:1rem; line-height:1.55; color:#d8ffe0; }
    #nodeCardBar { display:flex; gap:12px; justify-content:center; padding:10px 18px 18px; flex-wrap:wrap; }
    .node-card { background:rgba(18,26,38,.88); border:1px solid #1e2d42; border-top:3px solid #00f0ff; border-radius:6px; padding:10px 14px; min-width:140px; backdrop-filter:blur(8px); }
    .card-id { font-weight:bold; font-size:1rem; color:#fff; letter-spacing:.08em; }
    .card-zone { font-size:.75rem; text-transform:uppercase; color:#00f0ff; letter-spacing:.5px; margin-bottom:4px; }
    .card-dist { font-size:1.1rem; color:var(--neon); font-variant-numeric:tabular-nums; margin-bottom:4px; }
    .card-status { font-size:.74rem; color:#4cd964; display:flex; align-items:center; gap:5px; }
    .dot { width:7px; height:7px; background:#4cd964; border-radius:50%; box-shadow:0 0 6px #4cd964; }
    @media (max-width: 1100px) { .dashboard-grid { grid-template-columns:1fr; } .control-panel { position:static; } }
    @media (max-width: 720px) { html { font-size: 16px; } .shell { padding:16px; } #canvas-container { height:300px; } }
  </style>
</head>
<body>
  <div class=\"shell\">
    <div class=\"masthead\"><h1>FSS Fleet Dashboard</h1><div class=\"subtitle\">Neon matrix control view</div></div>
    <div class=\"dashboard-grid\">
      <div class=\"stack\">
        <div class=\"status\" id=\"status\">Connecting to edge hub…</div>
        <div class=\"scene\">
          <div class=\"scene-header\"><h2>40×40 ft Engineering Floorplan</h2><div class=\"hint\">1 unit = 1 foot · 4 rooms × 20ft² · 5ft sensor height · drag to orbit</div></div>
          <div id=\"canvas-container\"></div>          <div id="nodeCardBar"></div>        </div>
        <div class=\"card\"><h3>Node Status</h3><table><thead><tr><th>Node</th><th>Motion</th><th>Signal</th><th>State</th><th>Last Update</th></tr></thead><tbody id=\"rows\"></tbody></table></div>
        <div class=\"card\"><h3>Latest packet</h3><pre id=\"latest\">Waiting for telemetry…</pre></div>
      </div>
      <div class=\"control-panel\">
        <div class=\"status-card\"><h3>System Controls</h3><p>Speakers start silenced. Each FSS node has independent intercom and siren toggles, and the dashboard will stay lightweight by using live telemetry from the ESP32 side instead of doing heavy processing on the Jetson.</p></div>
        <div class=\"speaker-control-panel\">
          <div class=\"speaker-control-title\">Acoustic Subsystem</div>
          <button id=\"btn-siren-test\" class=\"speaker-test-button\" type=\"button\">Cycle Speaker Check (0.5s)</button>
          <div id=\"speaker-status\" class=\"speaker-status\">Ready to test (click to cycle nodes)</div>
        </div>
        <div class=\"card\"><h3>Command State</h3><div class=\"control-readout\"><strong>Dashboard</strong><span id=\"controlState\">Idle</span></div><div class=\"control-readout\"><strong>Broadcast</strong><span id=\"broadcastState\">No commands sent yet</span></div></div>
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
      "FSS-N01": { id: "FSS-N01", name: "OFFICE",      center: [-10, 0, -10], color: 0x00f0ff },
      "FSS-N02": { id: "FSS-N02", name: "GARAGE",      center: [10, 0, -10],  color: 0xff9900 },
      "FSS-N03": { id: "FSS-N03", name: "BABY'S ROOM", center: [-10, 0, 10],  color: 0xff0055 },
      "FSS-N04": { id: "FSS-N04", name: "ENTRYWAY",    center: [10, 0, 10],   color: 0x7b00ff }
    };
    
    let scene, camera, renderer, orbitControls;
    const animatedSpheres = [];
    const nodeSpheres = {};
    const speakerNodes = [
      { id: 'FSS-N01', zone: 'Office' },
      { id: 'FSS-N02', zone: 'Garage' },
      { id: 'FSS-N03', zone: "Baby\'s Room" },
      { id: 'FSS-N04', zone: 'Entryway' },
    ];
    let currentSpeakerIndex = 0;
    let audioContext = null;

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

    function highlightNodeSphere(nodeId) {
      const sphere = nodeSpheres[nodeId];
      if (!sphere) return;

      const originalScale = sphere.userData.baseScale || 1;
      sphere.scale.set(originalScale * 1.35, originalScale * 1.35, originalScale * 1.35);
      setTimeout(() => {
        sphere.scale.set(originalScale, originalScale, originalScale);
      }, 500);
    }

    async function cycleSpeakerTest() {
      const targetNode = speakerNodes[currentSpeakerIndex];
      const statusElem = document.getElementById('speaker-status');

      await playWebSirenTone(500);
      highlightNodeSphere(targetNode.id);

      if (statusElem) {
        statusElem.innerHTML = `TESTING: <b style="color:#44d7ff">${targetNode.id}</b> (${targetNode.zone})`;
      }

      socket.send(JSON.stringify({
        type: 'speaker_test',
        event: 'speaker_test',
        nodeId: targetNode.id,
        zone: targetNode.zone,
        durationMs: 500,
        sweepStartHz: 400,
        sweepEndHz: 1400,
      }));

      currentSpeakerIndex = (currentSpeakerIndex + 1) % speakerNodes.length;
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
        const s = 1 + Math.sin(animTime + item.offset) * 0.07;
        item.mesh.scale.set(s, s, s);
        item.mesh.rotation.y += 0.003;
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
    
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${location.host}/ws/dashboard`);
    const statusEl = document.getElementById('status');
    const rowsEl = document.getElementById('rows');
    const latestEl = document.getElementById('latest');
    const controlPanelEl = document.getElementById('controlPanel');
    const controlStateEl = document.getElementById('controlState');
    const broadcastStateEl = document.getElementById('broadcastState');
    const sirenTestButton = document.getElementById('btn-siren-test');

    if (sirenTestButton) {
      sirenTestButton.addEventListener('click', () => {
        cycleSpeakerTest().catch(() => {
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
      socket.send(JSON.stringify({ type: 'control_command', nodeId, feature, enabled }));
      controlStateEl.textContent = `${nodeId} ${feature} command sent`;
    }

    function renderDashboard(state) {
      const nodes = state.nodes || [];
      const activeNodes = nodes.filter((node) => node.motion).map((node) => node.label);
      statusEl.textContent = state.hub?.connected ? (activeNodes.length ? `Connected · ${activeNodes.join(', ')} active` : 'Connected · all rooms clear') : 'Waiting for telemetry…';
      controlStateEl.textContent = state.message || 'Idle';
      broadcastStateEl.textContent = state.hub?.status || 'No commands sent yet';

      controlPanelEl.innerHTML = '';
      rowsEl.innerHTML = '';

      for (const node of nodes) {
        const nodeControl = document.createElement('div');
        nodeControl.className = 'node-control';
        nodeControl.innerHTML = `<h4>${node.node_id} · ${node.label}</h4><div class=\"node-meta\"><span class=\"node-pill\">${node.motion_label}</span><span>${node.source}</span></div>`;

        const controls = document.createElement('div');
        controls.className = 'control-grid';

        const sirenButton = document.createElement('button');
        sirenButton.className = 'control-button';
        sirenButton.dataset.active = String(Boolean(node.siren_on));
        sirenButton.innerHTML = `${node.siren_on ? 'Siren On' : 'Siren Off'}<small>Audible alarm for ${node.label}. Starts silenced.</small>`;
        sirenButton.addEventListener('click', () => sendControl(node.node_id, 'siren', !node.siren_on));

        const intercomButton = document.createElement('button');
        intercomButton.className = 'control-button';
        intercomButton.dataset.active = String(Boolean(node.intercom_on));
        intercomButton.innerHTML = `${node.intercom_on ? 'Intercom On' : 'Intercom Off'}<small>Two-way talkback for ${node.label}.</small>`;
        intercomButton.addEventListener('click', () => sendControl(node.node_id, 'intercom', !node.intercom_on));

        controls.appendChild(sirenButton);
        controls.appendChild(intercomButton);
        nodeControl.appendChild(controls);
        controlPanelEl.appendChild(nodeControl);

        const row = document.createElement('tr');
        row.innerHTML = `<td>${node.node_id}</td><td>${node.motion_label}</td><td>${node.rssi ?? 'n/a'}</td><td>${node.motion ? 'MOTION' : 'CLEAR'}</td><td>${formatAgo(node.last_seen)}</td>`;
        rowsEl.appendChild(row);
      }

      latestEl.textContent = JSON.stringify(state.latest_packet || {}, null, 2);
      // update node cards with live distance
      if (state.latest_packet && state.latest_packet.node_id) {
        updateNodeCard(state.latest_packet.node_id, state.latest_packet);
      }
    }

    socket.onopen = () => { 
      controlStateEl.textContent = 'Connected to dashboard websocket';
      buildNodeCards();
      initThreeJS();
    };
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'dashboard_state') {
          renderDashboard(message);
        } else if (message.type === 'control_ack') {
          controlStateEl.textContent = message.message;
        }
      } catch (error) {
        latestEl.textContent = event.data;
      }
    };
    socket.onerror = () => { statusEl.textContent = 'Dashboard socket error'; };
    socket.onclose = () => { statusEl.textContent = 'Dashboard socket closed'; };
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

            if command_type != "control_command":
              continue

            node_id = message.get("nodeId")
            feature = message.get("feature")
            enabled = message.get("enabled")
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


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(connect_to_hub())
