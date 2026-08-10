import asyncio
import json
import logging
from typing import Any, Dict, Set

import websockets

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

connected_clients: Set[websockets.WebSocketServerProtocol] = set()

system_state = {
    "mode_state": "STOP",
    "datasets": ["session_001_folsom", "session_002_perimeter"],
    "active_dataset": None,
    "frame_index": 0,
    "is_ready": True,
}


class ReplayInjector:
    """Handles injection of historical tracking telemetry frames during replay states."""

    def __init__(self, dataset_name: str):
        self.dataset_name = dataset_name
        self.frames = [
            {"frame": i, "timestamp": i * 0.1, "status": "REPLAY_ACTIVE"}
            for i in range(100)
        ]
        self.cursor = 0

    def get_next_frame(self) -> Dict[str, Any]:
        if self.cursor >= len(self.frames):
            self.cursor = 0
        frame = self.frames[self.cursor]
        self.cursor += 1
        return frame

    def seek(self, target_index: int) -> None:
        self.cursor = max(0, min(target_index, len(self.frames) - 1))


active_injector: ReplayInjector | None = None


def reset_tracking_state() -> None:
    """Performs safety resets to clear residual artifacts and corrupt tracking states."""
    global active_injector
    logging.warning("[SAFETY RESET] Clearing tracking state and resetting accumulators.")
    active_injector = None
    system_state["frame_index"] = 0


async def handle_system_mode(command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Manages operational mode state transitions: REPLAY, PAUSE, RESUME, STOP, SEEK."""
    global active_injector

    cmd = command.upper()
    success = True
    message = f"Successfully transitioned to {cmd}"

    if cmd == "STOP":
        reset_tracking_state()
        system_state["mode_state"] = "STOP"
    elif cmd == "REPLAY":
        dataset = payload.get("dataset")
        if not dataset or dataset not in system_state["datasets"]:
            success = False
            message = f"Invalid or missing dataset specified for replay: {dataset}"
        else:
            reset_tracking_state()
            active_injector = ReplayInjector(dataset)
            system_state["active_dataset"] = dataset
            system_state["mode_state"] = "REPLAY"
    elif cmd == "PAUSE":
        if system_state["mode_state"] in ["REPLAY", "RESUME"]:
            system_state["mode_state"] = "PAUSE"
        else:
            success = False
            message = "Cannot pause when system is not actively running or replaying."
    elif cmd == "RESUME":
        if system_state["mode_state"] == "PAUSE" and system_state["active_dataset"]:
            system_state["mode_state"] = "RESUME"
        else:
            success = False
            message = "No active session paused to resume."
    elif cmd == "SEEK":
        target_index = int(payload.get("frame_index", 0))
        reset_tracking_state()
        if active_injector:
            active_injector.seek(target_index)
        system_state["frame_index"] = target_index
        message = f"Successfully sought to frame index {target_index}"
    else:
        success = False
        message = f"Unknown system command mode: {cmd}"

    return {
        "type": "system_mode_ack",
        "success": success,
        "message": message,
        "mode_state": system_state["mode_state"],
        "datasets": system_state["datasets"],
        "active_dataset": system_state["active_dataset"],
        "frame_index": system_state["frame_index"],
    }


async def broadcast_acknowledgment(ack_payload: dict) -> None:
    """Broadcasts system_mode_ack to all connected control panel clients."""
    if not connected_clients:
        return
    message = json.dumps(ack_payload)
    await asyncio.gather(*[client.send(message) for client in connected_clients], return_exceptions=True)


async def websocket_server_handler(websocket) -> None:
    """Handles incoming WebSocket control connections and commands."""
    connected_clients.add(websocket)
    logging.info("Client connected: %s", websocket.remote_address)
    try:
        init_packet = {
            "type": "system_mode_ack",
            "success": True,
            "message": "Connected to Jetson FSSS Core",
            "mode_state": system_state["mode_state"],
            "datasets": system_state["datasets"],
            "active_dataset": system_state["active_dataset"],
            "frame_index": system_state["frame_index"],
        }
        await websocket.send(json.dumps(init_packet))

        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
                command = data.get("command")
                payload = data.get("payload", {})

                if command:
                    logging.info("Received command: %s with payload: %s", command, payload)
                    ack_response = await handle_system_mode(command, payload)
                    await broadcast_acknowledgment(ack_response)
                else:
                    error_ack = {
                        "type": "system_mode_ack",
                        "success": False,
                        "message": "Missing 'command' field in message payload.",
                        "mode_state": system_state["mode_state"],
                        "datasets": system_state["datasets"],
                    }
                    await websocket.send(json.dumps(error_ack))
            except json.JSONDecodeError:
                logging.warning("Malformed JSON received: %s", raw_message)
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        logging.info("Client disconnected: %s", websocket.remote_address)


async def main() -> None:
    server_host = "0.0.0.0"
    server_port = 8765
    logging.info("Launching FSSS Tracking Core on ws://%s:%s", server_host, server_port)
    async with websockets.serve(websocket_server_handler, server_host, server_port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shut down cleanly.")
