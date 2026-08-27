"""Parametric FSSS spatial topology and lightweight fusion primitives."""

from dataclasses import dataclass
from itertools import combinations
from math import cos, hypot, radians, sin
from typing import Iterable

CORE_POSITIONS = ((-10.0, 5.0, -10.0), (10.0, 5.0, -10.0), (-10.0, 5.0, 10.0), (10.0, 5.0, 10.0))


@dataclass(frozen=True)
class NodeDefinition:
  node_id: str
  position: tuple[float, float, float]
  role: str
  ring: int
  azimuth_deg: float
  elevation_deg: float


@dataclass(frozen=True)
class NodeNeighborhood:
  node_id: str
  neighbors: tuple[str, ...]


@dataclass(frozen=True)
class TriangleDefinition:
  node_ids: tuple[str, str, str]


def generate_spiral_nodes(node_count: int = 24, center: tuple[float, float] = (0.0, 0.0), start_radius: float = 24.0, radial_step: float = 1.2, angular_step_deg: float = 35.0) -> tuple[NodeDefinition, ...]:
  """Generate the fixed reference core and a deterministic perimeter spiral."""
  if node_count < 4:
    raise ValueError("FSSS topology requires at least four core nodes")
  nodes = [NodeDefinition(f"FSS-N{index + 1:02d}", position, "REFERENCE", 0, index * 90.0, position[1]) for index, position in enumerate(CORE_POSITIONS)]
  for index in range(4, node_count):
    perimeter_index = index - 4
    angle_deg = -35.0 + perimeter_index * angular_step_deg
    radius = start_radius + perimeter_index * radial_step
    angle = radians(angle_deg)
    position = (center[0] + radius * cos(angle), 2.0 + (perimeter_index % 3) * 0.5, center[1] + radius * sin(angle))
    ring = 1 if index < 12 else 2
    nodes.append(NodeDefinition(f"FSS-N{index + 1:02d}", position, "PERIMETER", ring, angle_deg % 360.0, position[1]))
  return tuple(nodes)


def build_neighborhoods(nodes: Iterable[NodeDefinition], max_neighbors: int = 5) -> tuple[NodeNeighborhood, ...]:
  node_list = tuple(nodes)
  return tuple(NodeNeighborhood(node.node_id, tuple(other.node_id for other in sorted((candidate for candidate in node_list if candidate.node_id != node.node_id), key=lambda candidate: distance(node.position, candidate.position))[:max_neighbors])) for node in node_list)


def build_candidate_triangles(neighborhoods: Iterable[NodeNeighborhood]) -> tuple[TriangleDefinition, ...]:
  unique = {tuple(sorted((neighborhood.node_id, first, second))) for neighborhood in neighborhoods for first, second in combinations(neighborhood.neighbors, 2)}
  return tuple(TriangleDefinition(node_ids) for node_ids in sorted(unique))


def distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
  return hypot(hypot(first[0] - second[0], first[1] - second[1]), first[2] - second[2])


def fusion_weight(confidence: float, health: float = 1.0, geometry: float = 1.0) -> float:
  return max(0.0, confidence) * max(0.0, health) * max(0.0, geometry)


NODE_DEFINITIONS = generate_spiral_nodes()
NODE_NEIGHBORHOODS = build_neighborhoods(NODE_DEFINITIONS)
NODE_TRIANGLES = build_candidate_triangles(NODE_NEIGHBORHOODS)
NODE_POSITIONS = {node.node_id: node.position for node in NODE_DEFINITIONS}