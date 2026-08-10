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
  "North Entry": "FSS-N01",
  "South Patio": "FSS-N02",
  "East Gate": "FSS-N03",
  "West Driveway": "FSS-N04",
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
          <div class=\"scene-header\"><h2>40×40 ft Engineering Floorplan</h2><div class=\"hint\">1 unit = 1 foot · 4 rooms × 20ft² · 9ft sensor height</div></div>
          <div id=\"canvas-container\"></div>
        </div>
        <div class=\"card\"><h3>Node Status</h3><table><thead><tr><th>Node</th><th>Motion</th><th>Signal</th><th>State</th><th>Last Update</th></tr></thead><tbody id=\"rows\"></tbody></table></div>
        <div class=\"card\"><h3>Latest packet</h3><pre id=\"latest\">Waiting for telemetry…</pre></div>
      </div>
      <div class=\"control-panel\">
        <div class=\"status-card\"><h3>System Controls</h3><p>Speakers start silenced. Each FSS node has independent intercom and siren toggles, and the dashboard will stay lightweight by using live telemetry from the ESP32 side instead of doing heavy processing on the Jetson.</p></div>
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
    const SENSOR_HEIGHT = 9;
    
    const ROOMS = {
      "FSS-N01": { id: "FSS-N01", name: "OFFICE", center: [-10, 0, -10], bounds: { minX: -20, maxX: 0, minZ: -20, maxZ: 0 } },
      "FSS-N02": { id: "FSS-N02", name: "GARAGE", center: [10, 0, -10], bounds: { minX: 0, maxX: 20, minZ: -20, maxZ: 0 } },
      "FSS-N03": { id: "FSS-N03", name: "ROOM 3", center: [-10, 0, 10], bounds: { minX: -20, maxX: 0, minZ: 0, maxZ: 20 } },
      "FSS-N04": { id: "FSS-N04", name: "FRONT ENTRY", center: [10, 0, 10], bounds: { minX: 0, maxX: 20, minZ: 0, maxZ: 20 } }
    };
    
    let scene, camera, renderer, orbitControls;
    
    function initThreeJS() {
      const container = document.getElementById('canvas-container');
      const width = container.clientWidth;
      const height = container.clientHeight;
      
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x030605);
      
      camera = new THREE.PerspectiveCamera(75, width / height, 0.1, 1000);
      camera.position.set(0, 38, 42);
      camera.lookAt(0, 0, 0);
      
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setSize(width, height);
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.shadowMap.enabled = true;
      container.appendChild(renderer.domElement);
      
      const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
      scene.add(ambientLight);
      
      const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
      directionalLight.position.set(20, 30, 20);
      directionalLight.castShadow = true;
      scene.add(directionalLight);
      
      // FLOOR
      const floorGeometry = new THREE.BoxGeometry(FLOOR_WIDTH, 0.15, FLOOR_DEPTH);
      const floorMaterial = new THREE.MeshStandardMaterial({ color: 0x111827, roughness: 0.85, metalness: 0.05 });
      const floor = new THREE.Mesh(floorGeometry, floorMaterial);
      floor.position.set(0, -0.075, 0);
      floor.castShadow = true;
      floor.receiveShadow = true;
      scene.add(floor);
      
      // ENGINEERING GRID
      const engineeringGrid = new THREE.GridHelper(40, 40, 0x475569, 0x1e293b);
      engineeringGrid.position.y = 0.01;
      scene.add(engineeringGrid);
      
      // WALLS
      const wallMaterial = new THREE.MeshStandardMaterial({ color: 0x64748b, transparent: true, opacity: 0.35, roughness: 0.8 });
      
      function createWall(width, depth, x, y, z) {
        const geometry = new THREE.BoxGeometry(
          width,
          WALL_HEIGHT,
          width === WALL_THICKNESS ? FLOOR_DEPTH : WALL_THICKNESS
        );
        const wall = new THREE.Mesh(geometry, wallMaterial);
        wall.position.set(x, y, z);
        wall.castShadow = true;
        wall.receiveShadow = true;
        scene.add(wall);
      }
      
      // NORTH/SOUTH walls
      createWall(FLOOR_WIDTH, WALL_HEIGHT, 0, WALL_HEIGHT / 2, -20);
      createWall(FLOOR_WIDTH, WALL_HEIGHT, 0, WALL_HEIGHT / 2, 20);
      
      // EAST/WEST walls
      createWall(WALL_THICKNESS, WALL_HEIGHT, -20, WALL_HEIGHT / 2, 0);
      createWall(WALL_THICKNESS, WALL_HEIGHT, 20, WALL_HEIGHT / 2, 0);
      
      // INTERNAL walls
      createWall(WALL_THICKNESS, WALL_HEIGHT, 0, WALL_HEIGHT / 2, 0);  // North/South divider
      const horizontalWallGeometry = new THREE.BoxGeometry(FLOOR_WIDTH, WALL_HEIGHT, WALL_THICKNESS);
      const horizontalWall = new THREE.Mesh(horizontalWallGeometry, wallMaterial);
      horizontalWall.position.set(0, WALL_HEIGHT / 2, 0);
      horizontalWall.castShadow = true;
      horizontalWall.receiveShadow = true;
      scene.add(horizontalWall);
      
      // SENSOR POSITIONS
      Object.values(ROOMS).forEach(room => {
        const markerGeometry = new THREE.SphereGeometry(0.65, 24, 24);
        const markerMaterial = new THREE.MeshStandardMaterial({ color: 0x38bdf8, emissive: 0x123b59 });
        const marker = new THREE.Mesh(markerGeometry, markerMaterial);
        marker.position.set(room.center[0], SENSOR_HEIGHT, room.center[2]);
        marker.userData.nodeId = room.id;
        marker.castShadow = true;
        scene.add(marker);
        room.sensorMesh = marker;
      });
      
      // ROOM FLOOR CENTERS
      Object.values(ROOMS).forEach(room => {
        const geometry = new THREE.PlaneGeometry(ROOM_WIDTH - 0.5, ROOM_DEPTH - 0.5);
        const material = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.025, side: THREE.DoubleSide });
        const zone = new THREE.Mesh(geometry, material);
        zone.rotation.x = -Math.PI / 2;
        zone.position.set(room.center[0], 0.02, room.center[2]);
        zone.userData.room = room.id;
        scene.add(zone);
        room.zoneMesh = zone;
      });
      
      // DIMENSION ANNOTATIONS (using text sprites)
      const canvas = document.createElement('canvas');
      canvas.width = 256;
      canvas.height = 256;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#44d7ff';
      ctx.font = 'bold 48px monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('40 ft', 128, 128);
      
      const texture = new THREE.CanvasTexture(canvas);
      const spriteMaterial = new THREE.SpriteMaterial({ map: texture, sizeAttenuation: true });
      const sprite = new THREE.Sprite(spriteMaterial);
      sprite.scale.set(8, 8, 1);
      sprite.position.set(0, 22, -25);
      scene.add(sprite);
      
      window.addEventListener('resize', onWindowResize);
      animate();
    }
    
    function onWindowResize() {
      const container = document.getElementById('canvas-container');
      const width = container.clientWidth;
      const height = container.clientHeight;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
    }
    
    function animate() {
      requestAnimationFrame(animate);
      renderer.render(scene, camera);
    }
    
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${location.host}/ws/dashboard`);
    const statusEl = document.getElementById('status');
    const rowsEl = document.getElementById('rows');
    const latestEl = document.getElementById('latest');
    const controlPanelEl = document.getElementById('controlPanel');
    const controlStateEl = document.getElementById('controlState');
    const broadcastStateEl = document.getElementById('broadcastState');

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
    }

    socket.onopen = () => { 
      controlStateEl.textContent = 'Connected to dashboard websocket';
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

            if message.get("type") != "control_command":
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
                        payload = {"raw": raw_message}
                    await manager.broadcast(payload)
                if isinstance(payload, dict):
                  applied_node = apply_telemetry_packet(payload)
                  if applied_node is not None:
                        await broadcast_state(f"{applied_node} telemetry updated")
        except Exception as exc:
            logging.debug("[dashboard] hub not reachable: %s", exc)
        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(connect_to_hub())
