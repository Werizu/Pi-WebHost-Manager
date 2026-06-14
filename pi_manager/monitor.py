from rich.console import Console
from rich.table import Table

from .ssh import run_remote

console = Console()

DEFAULT_SERVICES = ["apache2", "mariadb", "cloudflared"]


def show_status(config: dict) -> None:
    """Display Pi system status as a rich table."""
    commands = {
        "CPU": "top -bn1 | head -3 | tail -1",
        "RAM": "free -h | grep Mem",
        "Disk": "df -h / | tail -1",
        "Temp": "vcgencmd measure_temp 2>/dev/null || echo 'N/A'",
        "Uptime": "uptime -p",
    }

    table = Table(title="Pi Status", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    for label, cmd in commands.items():
        stdout, stderr, code = run_remote(config, cmd)
        value = stdout if code == 0 else f"[red]{stderr or 'error'}[/red]"
        # Clean up output
        if label == "RAM" and value:
            parts = value.split()
            if len(parts) >= 4:
                value = f"Used: {parts[2]} / Total: {parts[1]}"
        if label == "Disk" and value:
            parts = value.split()
            if len(parts) >= 5:
                value = f"Used: {parts[2]} / Total: {parts[1]} ({parts[4]})"
        if label == "Temp":
            value = value.replace("temp=", "")
        table.add_row(label, value)

    console.print(table)


def show_services(config: dict, services: list = None) -> None:
    """Display service statuses. Pass an explicit `services` list (e.g. from
    auto-detection); otherwise fall back to the Pi's configured/default list."""
    svc_list = services if services is not None else config.get("services", DEFAULT_SERVICES)

    if not svc_list:
        console.print("[dim]Keine Anwendungs-Services erkannt.[/dim]")
        return

    table = Table(title="Services", show_header=True, header_style="bold cyan")
    table.add_column("Service", style="bold")
    table.add_column("Status")

    for svc in svc_list:
        stdout, _, code = run_remote(config, f"systemctl is-active {svc}")
        status = stdout.strip()
        if status == "active":
            styled = "[green]active[/green]"
        elif status == "inactive":
            styled = "[yellow]inactive[/yellow]"
        else:
            styled = f"[red]{status}[/red]"
        table.add_row(svc, styled)

    console.print(table)
