import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, Set

import websockets

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

# Ports match firmware constants in lighthouses/src/main.cpp
PORT_AUDIO_RX  = 5005  # Jetson → node speaker
PORT_AUDIO_TX  = 5006  # Node mic → Jetson
PORT_TELEMETRY = 5007  # Node distance data → Jetson

HYSTERESIS_CONFIG = {
    "TRIGGER_THRESHOLD": -58,
    "RELEASE_THRESHOLD": -75,
    "CONSECUTIVE_LOCKS": 10,
}

# Maps firmware NODE_ID integer to canonical FSS node name
NODE_ID_MAP: Dict[int, str] = {
    1: "FSS-N01",
    2: "FSS-N02",
    3: "FSS-N03",
    4: "FSS-N04",
}

active_locks: Dict[str, dict] = {}
visualizer_clients: Set[object] = set()


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


async def serial_endpoint_handler(port_path: str, zone_id: str) -> None:
    pass  # Replaced by UDP — kept for import compatibility during transition


class TelemetryProtocol(asyncio.DatagramProtocol):
    """Receives UDP telemetry datagrams from all four ESP32-S3 nodes."""

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            packet = json.loads(data.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return

        node_id_int = packet.get("node_id")
        node_name = NODE_ID_MAP.get(node_id_int, f"FSS-N{node_id_int:02d}")
        rssi = float(packet.get("rssi", -99))

        filtered_telemetry = process_zone_hysteresis(node_name, node_name, rssi)

        visualization_payload = json.dumps({
            "type": "PERIMETER_TELEMETRY",
            "zone_data": {
                "lighthouse": node_name,
                "raw_telemetry": packet,
                "state": filtered_telemetry,
            },
            "server_time": datetime.now().isoformat(),
        })

        if visualizer_clients:
            loop = asyncio.get_event_loop()
            loop.create_task(
                asyncio.gather(*[c.send(visualization_payload) for c in visualizer_clients])
            )

    def error_received(self, exc: Exception) -> None:
        logging.error("[UDP] Telemetry socket error: %s", exc)


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
            await asyncio.sleep(1)
    except websockets.ConnectionClosed:
        pass
    finally:
        visualizer_clients.discard(websocket)
        logging.info("[SYSTEM] Visualizer disconnected. Total: %d", len(visualizer_clients))


async def main() -> None:
    logging.info("[SYSTEM] Starting UJAALLC hub — UDP telemetry on 0.0.0.0:%d", PORT_TELEMETRY)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        TelemetryProtocol,
        local_addr=("0.0.0.0", PORT_TELEMETRY),
    )
    logging.info("[SYSTEM] WebSocket visualizer on ws://0.0.0.0:8765")
    try:
        await websockets.serve(visualizer_endpoint_handler, "0.0.0.0", 8765)
        await asyncio.Future()  # run forever
    finally:
        transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("[SYSTEM] Hub shut down cleanly")
