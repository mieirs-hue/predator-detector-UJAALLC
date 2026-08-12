import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, Set

import serial
import websockets

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

HUB_SERIAL_BAUD_RATE = 921600

# Serial port → canonical node name (update port paths to match your Jetson)
ZONE_MAP = {
    "/dev/ttyACM0": "FSS-N01",
    "/dev/ttyACM1": "FSS-N02",
    "/dev/ttyACM2": "FSS-N03",
    "/dev/ttyACM3": "FSS-N04",
}

HYSTERESIS_CONFIG = {
    "TRIGGER_THRESHOLD": -58,
    "RELEASE_THRESHOLD": -75,
    "CONSECUTIVE_LOCKS": 10,
}

active_locks: Dict[str, dict] = {}
visualizer_clients: Set[object] = set()


async def handle_websocket_message(websocket, message: str) -> None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    if data.get("event") != "speaker_test":
        return

    node_id = data.get("node_id", "unknown")
    zone = data.get("zone", "unknown")
    duration_ms = data.get("duration_ms", 500)
    start_hz = data.get("sweep_start_hz", 400)
    end_hz = data.get("sweep_end_hz", 1400)
    logging.info(
        "[AUDIO TEST] %s (%s) %sms sweep %sHz->%sHz",
        node_id,
        zone,
        duration_ms,
        start_hz,
        end_hz,
    )

    # Placeholder for downstream physical trigger routing, for example serial write to target node.
    # await route_speaker_test_to_node(node_id, duration_ms, start_hz, end_hz)


def process_zone_hysteresis(zone_id: str, mac_address: str, rssi_sample: float) -> dict:
    if mac_address not in active_locks:
        active_locks[mac_address] = {
            "current_zone": "CLEAR",
            "rssi": rssi_sample,
            "strength_count": 0,
            "state": "IDLE",
        }

    target_state = active_locks[mac_address]
    target_state["rssi"] = rssi_sample

    if target_state["state"] == "IDLE":
        if rssi_sample >= HYSTERESIS_CONFIG["TRIGGER_THRESHOLD"]:
            target_state["strength_count"] += 1
            if target_state["strength_count"] >= HYSTERESIS_CONFIG["CONSECUTIVE_LOCKS"]:
                target_state["current_zone"] = zone_id
                target_state["state"] = "HOLD"
                logging.info("[ZONE LOCK] %s locked to %s", mac_address, zone_id)
        else:
            target_state["strength_count"] = 0
    elif target_state["state"] == "HOLD" and target_state["current_zone"] == zone_id:
        if rssi_sample <= HYSTERESIS_CONFIG["RELEASE_THRESHOLD"]:
            target_state["current_zone"] = "CLEAR"
            target_state["state"] = "IDLE"
            target_state["strength_count"] = 0
            logging.info("[ZONE RELEASE] %s released from %s", mac_address, zone_id)

    return {
        "mac": mac_address,
        "zone": target_state["current_zone"],
        "rssi": target_state["rssi"],
        "state": target_state["state"],
        "strength": target_state["strength_count"],
    }


async def serial_endpoint_handler(port_path: str, node_name: str) -> None:
    try:
        ser = serial.Serial(port_path, HUB_SERIAL_BAUD_RATE, timeout=1)
        logging.info("[BOOT] %s initialized on %s", node_name, port_path)
        loop = asyncio.get_running_loop()
        while True:
            # readline() blocks; run in executor so asyncio stays responsive
            raw_line = await loop.run_in_executor(
                None, lambda: ser.readline().decode("utf-8", errors="ignore").strip()
            )
            if not raw_line:
                continue
            try:
                packet = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            resolved_name = packet.get("node_id") or node_name
            rssi = float(packet.get("rssi", -99))
            filtered = process_zone_hysteresis(resolved_name, resolved_name, rssi)

            payload = json.dumps({
                "type": "PERIMETER_TELEMETRY",
                "zone_data": {
                    "lighthouse": resolved_name,
                    "raw_telemetry": packet,
                    "state": filtered,
                },
                "server_time": datetime.now().isoformat(),
            })

            if visualizer_clients:
                dead = set()
                for c in list(visualizer_clients):
                    try:
                        await c.send(payload)
                    except Exception:
                        dead.add(c)
                for c in dead:
                    visualizer_clients.discard(c)
    except serial.SerialException as exc:
        logging.error("[ERROR] Serial %s (%s) disconnected: %s", port_path, node_name, exc)


async def visualizer_endpoint_handler(websocket) -> None:
    visualizer_clients.add(websocket)
    logging.info("[SYSTEM] Monitoring visualizer connected. Total: %d", len(visualizer_clients))
    try:
        initial_payload = {
            "type": "GLOBAL_STATE_INIT",
            "active_locks": active_locks,
            "server_time": datetime.now().isoformat(),
        }
        await websocket.send(json.dumps(initial_payload))
        while True:
            try:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await handle_websocket_message(websocket, raw_message)
    except websockets.ConnectionClosed:
        pass
    finally:
        visualizer_clients.discard(websocket)
        logging.info("[SYSTEM] Visualizer disconnected. Total: %d", len(visualizer_clients))


async def main() -> None:
    logging.info("[SYSTEM] Starting UJAALLC hub — serial@%d baud, ws://0.0.0.0:8765", HUB_SERIAL_BAUD_RATE)
    tasks = [serial_endpoint_handler(port, name) for port, name in ZONE_MAP.items()]
    tasks.append(websockets.serve(visualizer_endpoint_handler, "0.0.0.0", 8765))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("[SYSTEM] Hub shut down cleanly")
