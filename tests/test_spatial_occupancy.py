import unittest

from jetson_engine.spatial_occupancy import SpatialOccupancyGrid


class SpatialOccupancyGridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.positions = {
            "FSS-N01": (10.0, 5.0, 10.0),
            "FSS-N02": (-10.0, 5.0, 10.0),
            "FSS-N03": (10.0, 5.0, -10.0),
            "FSS-N04": (-10.0, 5.0, -10.0),
        }
        self.grid = SpatialOccupancyGrid(self.positions)

    def test_no_confidence_returns_no_centroid(self) -> None:
        nodes = [{"node_id": "FSS-N01", "distance_cm": None, "rf_confidence_smooth": 0.0}]
        self.assertIsNone(self.grid.calculate_global_centroid(nodes))

    def test_single_node_disturbance_centroids_on_that_node(self) -> None:
        nodes = [{
            "node_id": "FSS-N01",
            "distance_cm": 100,
            "distance_baseline_cm": 200,
            "motion_predator_delta_cm": 45,
            "rf_confidence_smooth": 0.0,
        }]
        centroid = self.grid.calculate_global_centroid(nodes)
        self.assertIsNotNone(centroid)
        x, z, confidence = centroid
        self.assertAlmostEqual(x, 10.0, places=0)
        self.assertAlmostEqual(z, 10.0, places=0)
        self.assertGreater(confidence, 0.0)

    def test_unknown_node_id_is_ignored(self) -> None:
        nodes = [{"node_id": "FSS-N99", "distance_cm": 10, "distance_baseline_cm": 200}]
        self.assertIsNone(self.grid.calculate_global_centroid(nodes))

    def test_target_lock_requires_threshold_breach(self) -> None:
        nodes = [{
            "node_id": "FSS-N01",
            "distance_cm": 100,
            "distance_baseline_cm": 200,
            "motion_predator_delta_cm": 45,
            "rf_confidence_smooth": 1.0,
            "mic_alarm_active": True,
        }]
        self.grid.calculate_global_centroid(nodes)
        locks = self.grid.check_for_target_lock()
        self.assertEqual(len(locks), 1)
        self.assertGreaterEqual(locks[0]["confidence"], self.grid.target_threshold)

    def test_confidence_is_capped_at_one(self) -> None:
        self.grid.update_cell_confidence("FSS-N01", tf_confidence=5.0, rf_confidence=5.0, mic_confidence=5.0)
        self.grid.update_cell_confidence("FSS-N01", tf_confidence=5.0, rf_confidence=5.0, mic_confidence=5.0)
        x_idx, y_idx = self.grid._cell_index("FSS-N01")
        self.assertEqual(self.grid.grid[x_idx][y_idx], 1.0)


if __name__ == "__main__":
    unittest.main()
