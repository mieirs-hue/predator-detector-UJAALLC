__all__ = ['ZONE_TOPOLOGY']

ZONE_TOPOLOGY = {
    "FSS-N01": {
        "zone_id": "ZONE_NORTH_OFFICE",
        "category": "Interior",
        "label": "Office",
        "color": "#00FF88",  # Green - Primary Active
        "baseline_rssi": -65,
        "rf_confidence_floor": 0.20,
        "rf_confirm_threshold": 0.60,
        "motion_confirm_delta_cm": 20,
        "motion_predator_delta_cm": 45,
        "motion_baseline_update_delta_cm": 12,
        "motion_rssi_margin": 5.0,
        "mount_height_ft": 5,
        "vertical_reference": {
            "above": "ZONE_UPSTAIRS",
            "below": "ZONE_OFFICE"
        }
    },
    "FSS-N02": {
        "zone_id": "ZONE_EAST_GARAGE",
        "category": "Vehicle",
        "label": "Garage",
        "color": "#3399FF",  # Blue - Equipment
        "baseline_rssi": -70,
        "rf_confidence_floor": 0.20,
        "rf_confirm_threshold": 0.60,
        "motion_confirm_delta_cm": 24,
        "motion_predator_delta_cm": 52,
        "motion_baseline_update_delta_cm": 12,
        "motion_rssi_margin": 5.0,
        "mount_height_ft": 5,
        "vertical_reference": {
            "above": "NONE",
            "below": "ZONE_GARAGE"
        }
    },
    "FSS-N03": {
        "zone_id": "ZONE_SOUTH_BASELINE",
        "category": "Reference",
        "label": "Baby's Room",
        "color": "#A0A0A0",  # Gray - Baseline/Control
        "baseline_rssi": -85,
        "rf_confirm_threshold": 0.80,
        "motion_confirm_delta_cm": 14,
        "motion_predator_delta_cm": 30,
        "motion_baseline_update_delta_cm": 10,
        "motion_rssi_margin": 4.0,
        "mount_height_ft": 5,
        "vertical_reference": {
            "above": "NONE",
            "below": "NONE"
        }
    },
    "FSS-N04": {
        "zone_id": "ZONE_WEST_ENTRY",
        "category": "Entry",
        "label": "Entryway",
        "color": "#FFD700",  # Gold - Entry Monitor
        "baseline_rssi": -60,
        "rf_confirm_threshold": 0.80,
        "motion_confirm_delta_cm": 30,
        "motion_predator_delta_cm": 70,
        "motion_baseline_update_delta_cm": 15,
        "motion_rssi_margin": 7.0,
        "mount_height_ft": 5,
        "vertical_reference": {
            "above": "NONE",
            "below": "ZONE_ENTRY"
        }
    },
    # --- Expanded Zones ---
    "FSS-N05": {
        "zone_id": "ZONE_PATIO",
        "category": "Outdoor",
        "label": "Patio / Deck",
        "color": "#32CD32",  # Lime
        "baseline_rssi": -75
    },
    "4B:BD:CE:1B:BF:06": {
        "zone_id": "ZONE_DRIVEWAY",
        "category": "Vehicle",
        "label": "Driveway",
        "color": "#00FFFF",  # Cyan
        "baseline_rssi": -80
    },
    "4B:BD:CE:1B:BF:07": {
        "zone_id": "ZONE_BACKYARD_W",
        "category": "Outdoor",
        "label": "Backyard West",
        "color": "#8B4513",  # Brown
        "baseline_rssi": -80
    },
    "4B:BD:CE:1B:BF:08": {
        "zone_id": "ZONE_BACKYARD_E",
        "category": "Outdoor",
        "label": "Backyard East",
        "color": "#008080",  # Teal
        "baseline_rssi": -80
    },
    "4B:BD:CE:1B:BF:09": {
        "zone_id": "ZONE_UPSTAIRS",
        "category": "Interior",
        "label": "Upstairs Landing",
        "color": "#800080",  # Purple
        "baseline_rssi": -65
    }
}
