import time
from rich.live import Live
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from config import ZONE_TOPOLOGY

console = Console()

def generate_dashboard(state):
    # Group zones by category
    categories = {}
    for mac, zone in ZONE_TOPOLOGY.items():
        cat = zone.get("category", "Uncategorized")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append((zone, state.get(mac, {"status": "CLEAR"})))

    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3)
    )

    layout["header"].update(Panel("[bold white]PROPERTY SECURITY GRID[/bold white]", style="bold blue"))

    tables = []
    for cat, zones in categories.items():
        table = Table(title=cat, expand=True, show_edge=False)
        table.add_column("Zone", justify="left", style="cyan")
        table.add_column("Status", justify="center")
        
        for zone, z_state in zones:
            status_text = f"[bold green]ACTIVE[/bold green]" if z_state["status"] == "ACTIVE" else "[dim]CLEAR[/dim]"
            if zone['label'] == 'No-Movement Zone':
                 status_text = "[dim]BASELINE[/dim]"
            
            table.add_row(f"[{zone['color']}]{zone['label']}[/{zone['color']}]", status_text)
        tables.append(Panel(table))

    # Just displaying tables linearly for immediate prototype
    layout["main"].update(Panel("\n".join([str(t) for t in categories.keys()]), title="Zones"))
    
    table_grid = Table.grid(expand=True)
    table_grid.add_column()
    for p in tables:
         table_grid.add_row(p)
         
    layout["main"].update(table_grid)
    layout["footer"].update(Panel("Telemetry: Healthy | Jetson: Online | Nodes: 9/9"))

    return layout

if __name__ == "__main__":
    
    # Mock State
    mock_state = {
        "FSS-N01": {"status": "ACTIVE"},  # Office
        "FSS-N02": {"status": "CLEAR"},
        "FSS-N03": {"status": "BASELINE"},
    }

    with Live(generate_dashboard(mock_state), refresh_per_second=4) as live:
        for i in range(10):
            time.sleep(1)
            # Toggle front door
            if i % 2 == 0:
                mock_state["FSS-N04"] = {"status": "ACTIVE"}
            else:
                 mock_state["FSS-N04"] = {"status": "CLEAR"}
            live.update(generate_dashboard(mock_state))