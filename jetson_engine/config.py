__all__ = ['ZONE_TOPOLOGY']

ZONE_TOPOLOGY = {
    # Replace with actual ESP32 MAC addresses
    "4B:BD:CE:1B:BF:01": {
        "zone_id": "ZONE_NORTH",
        "category": "Interior",
        "label": "Uncle Jesse's Office",
        "color": "#00FF88",  # Green - Primary Active
        "baseline_rssi": -65 
    },
    "4B:BD:CE:1B:BF:02": {
        "zone_id": "ZONE_EAST",
        "category": "Vehicle",
        "label": "Garage",
        "color": "#3399FF",  # Blue - Equipment
        "baseline_rssi": -70
    },
    "4B:BD:CE:1B:BF:03": {
        "zone_id": "ZONE_SOUTH",
        "category": "Reference",
        "label": "No-Movement Zone",
        "color": "#A0A0A0",  # Gray - Baseline/Control
        "baseline_rssi": -85
    },
    "4B:BD:CE:1B:BF:04": {
        "zone_id": "ZONE_WEST",
        "category": "Entry",
        "label": "Front Door",
        "color": "#FFD700",  # Gold - Entry Monitor
        "baseline_rssi": -60
    },
    # --- Expanded Zones ---
    "4B:BD:CE:1B:BF:05": {
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
