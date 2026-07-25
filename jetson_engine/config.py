__all__ = ['ZONE_TOPOLOGY']

ZONE_TOPOLOGY = {
    "FSS-N01": {
        "zone_id": "ZONE_NORTH_OFFICE",
        "category": "Interior",
        "label": "Uncle Jesse's Office",
        "color": "#00FF88",  # Green - Primary Active
        "baseline_rssi": -65,
        "mount_height_ft": 9,
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
        "mount_height_ft": 6,
        "vertical_reference": {
            "above": "NONE",
            "below": "ZONE_GARAGE"
        }
    },
    "FSS-N03": {
        "zone_id": "ZONE_SOUTH_BASELINE",
        "category": "Reference",
        "label": "No-Movement Zone",
        "color": "#A0A0A0",  # Gray - Baseline/Control
        "baseline_rssi": -85,
        "mount_height_ft": 4,
        "vertical_reference": {
            "above": "NONE",
            "below": "NONE"
        }
    },
    "FSS-N04": {
        "zone_id": "ZONE_WEST_ENTRY",
        "category": "Entry",
        "label": "Front Door",
        "color": "#FFD700",  # Gold - Entry Monitor
        "baseline_rssi": -60,
        "mount_height_ft": 7,
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
