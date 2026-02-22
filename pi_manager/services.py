import click
from rich.console import Console

from .ssh import run_remote

console = Console()

DEFAULT_SERVICES = ["apache2", "mariadb", "cloudflared"]


def restart_service(config: dict, service: str) -> None:
    """Restart a single service on the Pi."""
    console.print(f"[cyan]Restarting {service}...[/cyan]")
    stdout, stderr, code = run_remote(config, f"sudo systemctl restart {service}")
    if code == 0:
        console.print(f"[green]{service} restarted.[/green]")
    else:
        console.print(f"[red]Failed to restart {service}: {stderr}[/red]")


def restart_all(config: dict) -> None:
    """Restart all core services sequentially."""
    for svc in config.get("services", DEFAULT_SERVICES):
        restart_service(config, svc)


def shutdown_pi(config: dict) -> None:
    """Shut down the Pi."""
    if not click.confirm("Are you sure you want to shut down the Pi?"):
        return
    console.print("[yellow]Shutting down...[/yellow]")
    run_remote(config, "sudo shutdown -h now")
    console.print("[green]Shutdown command sent.[/green]")


def reboot_pi(config: dict) -> None:
    """Reboot the Pi."""
    if not click.confirm("Are you sure you want to reboot the Pi?"):
        return
    console.print("[yellow]Rebooting...[/yellow]")
    run_remote(config, "sudo reboot")
    console.print("[green]Reboot command sent.[/green]")
