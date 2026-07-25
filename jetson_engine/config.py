__all__ = ['ZONE_TOPOLOGY']

ZONE_TOPOLOGY = {
    # Replace with actual ESP32 MAC addresses
    "4B:BD:CE:1B:BF:01": {
        "zone_id": "ZONE_NORTH",
        "label": "Uncle Jesse's Office",
        "color": "#00FF88",  # Green - Primary Active
        "baseline_rssi": -65 
    },
    "4B:BD:CE:1B:BF:02": {
        "zone_id": "ZONE_EAST",
        "label": "Garage",
        "color": "#3399FF",  # Blue - Equipment
        "baseline_rssi": -70
    },
    "4B:BD:CE:1B:BF:03": {
        "zone_id": "ZONE_SOUTH",
        "label": "No-Movement Zone",
        "color": "#A0A0A0",  # Gray - Baseline/Control
        "baseline_rssi": -85
    },
    "4B:BD:CE:1B:BF:04": {
        "zone_id": "ZONE_WEST",
        "label": "Front Door",
        "color": "#FFD700",  # Gold - Entry Monitor
        "baseline_rssi": -60
    }
}
