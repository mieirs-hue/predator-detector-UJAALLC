"""Discrete occupancy-grid fusion: stacks per-node, multi-modal confidence onto a
shared (X, Z) floor grid and reports the weighted centroid of active cells.

This is additive to the existing RF fusion (compute_rf_fusion / SpatialTriangulationEngine)
in dashboard_server.py, not a replacement. It shares the same NODE_POSITIONS so the two
models never disagree about where a node physically is. Plain Python lists are used
instead of numpy: the grid is small (\u22481600 cells at 1ft resolution) and this keeps the
dashboard's dependency list minimal.
"""

from __future__ import annotations


class SpatialOccupancyGrid:
  def __init__(self, node_positions: dict[str, tuple], width: float = 40.0, height: float = 40.0, resolution: float = 1.0):
    self.node_positions = node_positions
    self.width = width
    self.height = height
    self.resolution = resolution

    self.grid_x = max(1, int(self.width / self.resolution))
    self.grid_y = max(1, int(self.height / self.resolution))
    self.grid = [[0.0] * self.grid_y for _ in range(self.grid_x)]

    # Confidence at or above this cell weight is a verified multi-modal target lock.
    self.target_threshold = 0.85

  def _cell_index(self, node_id: str) -> tuple[int, int] | None:
    pos = self.node_positions.get(node_id)
    if pos is None:
      return None
    # NODE_POSITIONS is (x, height_ft, z) in Three.js convention; use the floor plane.
    x_idx = int(pos[0] + (self.width / 2))
    y_idx = int(pos[2] + (self.height / 2))
    if not (0 <= x_idx < self.grid_x and 0 <= y_idx < self.grid_y):
      return None
    return x_idx, y_idx

  def reset(self) -> None:
    for row in self.grid:
      for i in range(len(row)):
        row[i] = 0.0

  def update_cell_confidence(self, node_id: str, tf_confidence: float = 0.0, rf_confidence: float = 0.0, mic_confidence: float = 0.0) -> None:
    """Stack the Z-axis (probability) confidence for one node's cell this cycle.

    Callers pass already-normalized 0..1 per-modality confidences; weighting
    reflects how directly each modality implies physical presence.
    """
    idx = self._cell_index(node_id)
    if idx is None:
      return

    weight = (
      0.4 * max(0.0, min(1.0, tf_confidence))
      + 0.3 * max(0.0, min(1.0, rf_confidence))
      + 0.2 * max(0.0, min(1.0, mic_confidence))
    )
    x_idx, y_idx = idx
    self.grid[x_idx][y_idx] = min(1.0, self.grid[x_idx][y_idx] + weight)

  def calculate_global_centroid(self, nodes: list[dict]) -> tuple[float, float, float] | None:
    """Confidence-weighted centroid across all active cells for this cycle's nodes.

    Returns (x_ft, z_ft, confidence) or None if no cell holds any confidence.
    """
    self.reset()
    for node in nodes:
      node_id = node.get("node_id")
      if not node_id:
        continue
      distance_cm = node.get("distance_cm")
      baseline_cm = node.get("distance_baseline_cm")
      tf_confidence = 0.0
      if distance_cm is not None and baseline_cm is not None:
        predator_delta = float(node.get("motion_predator_delta_cm", 45))
        tf_confidence = max(0.0, min(1.0, abs(float(distance_cm) - float(baseline_cm)) / max(predator_delta, 1e-6)))
      rf_confidence = float(node.get("rf_confidence_smooth", 0.0) or 0.0)
      mic_confidence = 1.0 if node.get("mic_alarm_active") else 0.0
      self.update_cell_confidence(node_id, tf_confidence, rf_confidence, mic_confidence)

    total_weight = 0.0
    wx = wz = 0.0
    max_confidence = 0.0
    for x in range(self.grid_x):
      for y in range(self.grid_y):
        c = self.grid[x][y]
        if c <= 0.0:
          continue
        phys_x = x - (self.width / 2)
        phys_z = y - (self.height / 2)
        wx += c * phys_x
        wz += c * phys_z
        total_weight += c
        max_confidence = max(max_confidence, c)

    if total_weight <= 0.0:
      return None

    return (wx / total_weight, wz / total_weight, max_confidence)

  def check_for_target_lock(self) -> list[dict]:
    """Scan the grid for cells that breached target_threshold this cycle."""
    locked_targets = []
    for x in range(self.grid_x):
      for y in range(self.grid_y):
        if self.grid[x][y] >= self.target_threshold:
          phys_x = x - (self.width / 2)
          phys_z = y - (self.height / 2)
          locked_targets.append({"x": phys_x, "z": phys_z, "confidence": float(self.grid[x][y])})
    return locked_targets
