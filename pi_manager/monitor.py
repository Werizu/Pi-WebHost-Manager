import sys
import time

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from .ssh import run_remote, get_ssh_client

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


def show_services(config: dict) -> None:
    """Display service statuses."""
    table = Table(title="Services", show_header=True, header_style="bold cyan")
    table.add_column("Service", style="bold")
    table.add_column("Status")

    for svc in config.get("services", DEFAULT_SERVICES):
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


def show_logs(config: dict, live: bool = False, lines: int = 30) -> None:
    """Show Apache error logs, optionally streaming live."""
    if not live:
        stdout, stderr, code = run_remote(
            config, f"sudo tail -{lines} /var/log/apache2/error.log"
        )
        if code == 0:
            console.print(Panel(stdout or "[dim]No recent errors[/dim]", title="Apache Error Log"))
        else:
            console.print(f"[red]Failed to read logs: {stderr}[/red]")
        return

    # Live tail via paramiko channel
    console.print("[cyan]Streaming logs (Ctrl+C to stop)...[/cyan]\n")
    client = get_ssh_client(config)
    try:
        channel = client.get_transport().open_session()
        channel.exec_command("sudo tail -f /var/log/apache2/error.log")
        channel.settimeout(1.0)
        while True:
            try:
                data = channel.recv(4096)
                if not data:
                    break
                sys.stdout.write(data.decode())
                sys.stdout.flush()
            except TimeoutError:
                continue
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        client.close()
