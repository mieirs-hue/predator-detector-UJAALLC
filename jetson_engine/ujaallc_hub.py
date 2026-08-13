import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Set

import serial
import websockets

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

HUB_SERIAL_BAUD_RATE = 921600
SERIAL_RETRY_DELAY_SECONDS = 2.0

# Canonical node → candidate serial paths.
# Prefer /dev/serial/by-id for stable mapping across reboots and reordering.
ZONE_PORTS = {
    "FSS-N01": [
        "/dev/serial/by-id/usb-Arduino_NanoESP32_28848546D968-if01",
        "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:46:D9:68-if00",
        "/dev/ttyACM0",
    ],
    "FSS-N02": [
        "/dev/serial/by-id/usb-Arduino_NanoESP32_28848546D8CC-if01",
        "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_28:84:85:46:D8:CC-if00",
        "/dev/ttyACM1",
    ],
    "FSS-N03": [
        "/dev/serial/by-id/usb-Arduino_NanoESP32_E072A1CED858-if01",
        "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_E0:72:A1:CE:D8:58-if00",
        "/dev/ttyACM2",
    ],
    "FSS-N04": [
        "/dev/serial/by-id/usb-Arduino_NanoESP32_E072A1F0A348-if01",
        "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_E0:72:A1:F0:A3:48-if00",
        "/dev/ttyACM3",
    ],
}

HYSTERESIS_CONFIG = {
    "TRIGGER_THRESHOLD": -58,
    "RELEASE_THRESHOLD": -75,
    "CONSECUTIVE_LOCKS": 10,
}

active_locks: Dict[str, dict] = {}
visualizer_clients: Set[object] = set()
active_serial_ports: Dict[str, serial.Serial] = {}


def resolve_available_port(node_name: str) -> str | None:
    for candidate in ZONE_PORTS.get(node_name, []):
        if Path(candidate).exists():
            return candidate
    return None


async def handle_websocket_message(websocket, message: str) -> None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    event_type = data.get("event")

    if event_type == "speaker_test":
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
        await route_audio_command(node_id, "PING")
        return

    if event_type == "node_audio_command":
        node_id = data.get("node_id", "unknown")
        feature = str(data.get("feature", "")).lower()
        enabled = bool(data.get("enabled", False))
        if not enabled:
            return

        if feature == "siren":
            await route_audio_command(node_id, "SIREN")
            return
        if feature == "intercom":
            await route_audio_command(node_id, "BEEP")
            return
        if feature == "ping":
            await route_audio_command(node_id, "PING")
            return


async def route_audio_command(node_id: str, command: str) -> None:
    ser = active_serial_ports.get(node_id)
    if ser is None:
        logging.warning("[AUDIO COMMAND] %s requested for %s but node is offline", command, node_id)
        return

    payload = f"{command}\n".encode("utf-8")
    try:
        await asyncio.get_running_loop().run_in_executor(None, ser.write, payload)
        logging.info("[AUDIO COMMAND] Sent %s to %s", command, node_id)
    except Exception as exc:
        logging.error("[AUDIO COMMAND] Failed %s to %s: %s", command, node_id, exc)


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


async def serial_endpoint_handler(node_name: str) -> None:
    while True:
        ser: serial.Serial | None = None
        port_path = resolve_available_port(node_name)
        if port_path is None:
            logging.info("[WAIT] %s not present on any configured serial path", node_name)
            await asyncio.sleep(SERIAL_RETRY_DELAY_SECONDS)
            continue

        try:
            ser = serial.Serial(port_path, HUB_SERIAL_BAUD_RATE, timeout=1)
            active_serial_ports[node_name] = ser
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

                # Canonical zone identity comes from the mapped serial port.
                # This keeps room mapping stable even if a board was flashed with the wrong NODE_NAME.
                reported_node_id = packet.get("node_id")
                resolved_name = node_name
                rssi = float(packet.get("rssi", -99))
                filtered = process_zone_hysteresis(resolved_name, resolved_name, rssi)

                payload = json.dumps({
                    "type": "PERIMETER_TELEMETRY",
                    "zone_data": {
                        "lighthouse": resolved_name,
                        "reported_node_id": reported_node_id,
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
        finally:
            tracked = active_serial_ports.get(node_name)
            if ser is not None and tracked is ser:
                active_serial_ports.pop(node_name, None)
            try:
                if ser is not None and ser.is_open:
                    ser.close()
            except Exception:
                pass

        await asyncio.sleep(SERIAL_RETRY_DELAY_SECONDS)


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
    tasks = [serial_endpoint_handler(name) for name in ZONE_PORTS]
    tasks.append(websockets.serve(visualizer_endpoint_handler, "0.0.0.0", 8765))
    await asyncio.gather(*tasks)


def _get_listener_pid(port: int) -> int | None:
    """Best-effort lookup of the PID currently listening on a TCP port."""
    try:
        result = subprocess.run(
            ["ss", "-ltnp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    needle = f":{port}"
    pid_re = re.compile(r"pid=(\d+)")
    for line in result.stdout.splitlines():
        if needle not in line:
            continue
        match = pid_re.search(line)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except OSError as exc:
        if exc.errno == 98:
            pid = _get_listener_pid(8765)
            if pid is not None:
                logging.error("[SYSTEM] Hub already running on 8765 (pid %d). Stop it first or use the existing instance.", pid)
            else:
                logging.error("[SYSTEM] Port 8765 is already in use. Stop the existing hub or other listener and retry.")
        else:
            raise
    except KeyboardInterrupt:
        logging.info("[SYSTEM] Hub shut down cleanly")
