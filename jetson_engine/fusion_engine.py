"""Confidence-ranked adjacent-node triangulation for FSSS-24."""

from dataclasses import dataclass
from math import log10
from typing import Iterable

from .geometry import NODE_NEIGHBORHOODS, NODE_POSITIONS, NODE_TRIANGLES


@dataclass(frozen=True)
class RankedNode:
  node_id: str
  position: tuple[float, float, float]
  score: float


class SpatialTriangulationEngine:
  """Select the strongest adjacent triangle and estimate its weighted centroid.

  This is a spatial cue for optical systems, not a calibrated trilateration
  truth. The existing RF multilateration remains the authoritative estimator.
  """

  def __init__(self, *, min_score: float = 0.10) -> None:
    self.weight_tfluna = 1.0
    self.weight_mic = 0.6
    self.weight_rf = 0.4
    self.min_score = min_score
    self._triangle_ids = {frozenset(triangle.node_ids) for triangle in NODE_TRIANGLES}

  def calculate_node_confidence(self, node_data: dict) -> float:
    distance_cm = node_data.get("distance_cm")
    lidar_score = 0.0
    if distance_cm is not None:
      try:
        distance = float(distance_cm)
        if 0 <= distance < 800:
          lidar_score = self.weight_tfluna * (1.0 - distance / 800.0)
      except (TypeError, ValueError):
        pass

    mic_score = 0.0
    mic_rms = node_data.get("mic_rms")
    if mic_rms is not None:
      try:
        mic_dbfs = 20.0 * log10(max(float(mic_rms), 1e-6))
        if mic_dbfs > -40.0:
          mic_score = self.weight_mic * min(1.0, max(0.0, (mic_dbfs + 60.0) / 60.0))
      except (TypeError, ValueError):
        pass

    rf_score = self.weight_rf * min(1.0, max(0.0, float(node_data.get("rf_confidence_smooth", node_data.get("rf_confidence", 0.0)) or 0.0)))
    return round(lidar_score + mic_score + rf_score, 6)

  def rank_nodes(self, network_state: Iterable[dict] | dict[str, dict]) -> list[RankedNode]:
    node_items = network_state.items() if isinstance(network_state, dict) else ((node.get("node_id"), node) for node in network_state)
    ranked = []
    for node_id, data in node_items:
      if node_id not in NODE_POSITIONS:
        continue
      score = self.calculate_node_confidence(data)
      if score > self.min_score:
        ranked.append(RankedNode(node_id, NODE_POSITIONS[node_id], score))
    return sorted(ranked, key=lambda node: node.score, reverse=True)

  def get_active_triangle(self, network_state: Iterable[dict] | dict[str, dict]) -> list[RankedNode] | None:
    ranked = self.rank_nodes(network_state)
    for first_index, first in enumerate(ranked):
      for second_index in range(first_index + 1, len(ranked)):
        second = ranked[second_index]
        for third in ranked[second_index + 1:]:
          ids = frozenset((first.node_id, second.node_id, third.node_id))
          if ids in self._triangle_ids:
            return [first, second, third]
    return None

  def calculate_target_centroid(self, triangle_nodes: list[RankedNode] | None) -> dict | None:
    if not triangle_nodes:
      return None
    total_weight = sum(node.score for node in triangle_nodes)
    if total_weight <= 0:
      return None
    position = [sum(node.position[axis] * node.score for node in triangle_nodes) / total_weight for axis in range(3)]
    return {
      "target_x": round(position[0], 2),
      "target_y": round(position[1], 2),
      "target_z": round(position[2], 2),
      "confidence": round(total_weight / len(triangle_nodes), 4),
      "anchors": [node.node_id for node in triangle_nodes],
      "state": "ACTIVE",
    }

  def estimate(self, network_state: Iterable[dict] | dict[str, dict]) -> dict:
    return self.calculate_target_centroid(self.get_active_triangle(network_state)) or {
      "state": "NO_TRIANGLE",
      "confidence": 0.0,
      "anchors": [],
      "target_x": None,
      "target_y": None,
      "target_z": None,
    }
