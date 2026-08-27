"""Canonical measurement-only telemetry contract for FSSS nodes."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RfMeasurement:
  rssi_dbm: float | None = None
  noise_floor_dbm: float | None = None
  channel: int | None = None
  mode: str = "RSSI"


@dataclass(frozen=True)
class LidarMeasurement:
  distance_cm: int | None = None
  strength: int | None = None
  valid: bool = False


@dataclass(frozen=True)
class AudioMetadata:
  mic_state: str = "MUTED"
  level_db: float = 0.0
  event: str = "NONE"
  ready: bool = False
  last_command: str = "NONE"


@dataclass(frozen=True)
class HealthMeasurement:
  uptime_s: int | None = None
  temperature_c: float | None = None
  supply_v: float | None = None
  wifi_rssi_dbm: float | None = None
  heap_free: int | None = None


@dataclass(frozen=True)
class TelemetryPacket:
  node_id: str
  sequence: int
  esp_timestamp_ms: int
  firmware: str = "unknown"
  rf: RfMeasurement = field(default_factory=RfMeasurement)
  lidar: LidarMeasurement = field(default_factory=LidarMeasurement)
  audio: AudioMetadata = field(default_factory=AudioMetadata)
  health: HealthMeasurement = field(default_factory=HealthMeasurement)
  protocol: str = "fsss.telemetry"
  version: str = "1.0.0"

  def __post_init__(self) -> None:
    if not self.node_id.startswith("FSS-N"):
      raise ValueError("node_id must use the FSS-N## format")
    if self.sequence < 0 or self.esp_timestamp_ms < 0:
      raise ValueError("sequence and esp_timestamp_ms must be non-negative")

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def parse_telemetry_packet(payload: dict[str, Any]) -> TelemetryPacket:
  """Parse a canonical packet without accepting interpreted Jetson fields."""
  if payload.get("protocol") != "fsss.telemetry":
    raise ValueError("unsupported telemetry protocol")
  return TelemetryPacket(
    node_id=str(payload["node_id"]),
    sequence=int(payload["sequence"]),
    esp_timestamp_ms=int(payload["esp_timestamp_ms"]),
    firmware=str(payload.get("firmware", "unknown")),
    rf=RfMeasurement(**payload.get("rf", {})),
    lidar=LidarMeasurement(**payload.get("lidar", {})),
    audio=AudioMetadata(**payload.get("audio", {})),
    health=HealthMeasurement(**payload.get("health", {})),
  )


def canonical_to_legacy_packet(packet: TelemetryPacket) -> dict[str, Any]:
  """Adapt measurements to the current dashboard pipeline during migration."""
  raw = {
    "node_id": packet.node_id,
    "node_name": packet.node_id,
    "rssi": packet.rf.rssi_dbm,
    "wifi_rssi": packet.health.wifi_rssi_dbm,
    "distance_cm": packet.lidar.distance_cm,
    "strength": packet.lidar.strength or 0,
    "status": "OK" if packet.lidar.valid else "UNKNOWN",
    "mic_enabled": packet.audio.mic_state == "UNMUTED",
    "audio_ready": packet.audio.ready,
    "last_audio_cmd": packet.audio.last_command,
    "mic_rms": 10 ** (packet.audio.level_db / 20.0) if packet.audio.mic_state == "UNMUTED" else None,
    "firmware": packet.firmware,
    "sequence": packet.sequence,
    "esp_timestamp_ms": packet.esp_timestamp_ms,
  }
  return {"zone_data": {"lighthouse": packet.node_id, "raw_telemetry": raw, "state": {"state": "CLEAR"}}}