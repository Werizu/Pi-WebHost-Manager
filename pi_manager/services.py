import click
from rich.console import Console

from .ssh import run_remote

console = Console()

DEFAULT_SERVICES = ["apache2", "mariadb", "cloudflared"]

# Base-OS / systemd-internal units that aren't "applications" the user cares about.
_SYSTEM_SERVICE_PREFIXES = (
    "systemd-", "getty@", "serial-getty@", "autovt@", "user@", "console-",
    "dpkg-", "apt-", "e2scrub", "blk-availability", "phpsessionclean",
    "ssh@", "ifup@", "modprobe@",
)
_SYSTEM_SERVICES = {
    "ssh", "sshd", "cron", "dbus", "rsyslog", "polkit", "networking",
    "NetworkManager", "systemd-timesyncd", "wpa_supplicant", "dhcpcd",
    "avahi-daemon", "bluetooth", "ModemManager", "udisks2", "accounts-daemon",
    "rpcbind", "nfs-common", "packagekit", "unattended-upgrades",
    "raspi-config", "triggerhappy", "hciuart", "rng-tools-debian", "rng-tools",
    "alsa-restore", "alsa-state", "keyboard-setup", "console-setup",
    "plymouth", "x11-common", "lightdm", "getty", "emergency", "rescue",
    "networkd-dispatcher", "systemd-resolved", "fail2ban", "smartmontools",
    "wpa_supplicant@", "rsync", "watchdog", "ntp", "chrony",
}


def _is_system_service(svc: str) -> bool:
    """True if a service is base-OS noise rather than a user application."""
    if svc in _SYSTEM_SERVICES:
        return True
    return any(svc.startswith(p) for p in _SYSTEM_SERVICE_PREFIXES)


def detect_services(config: dict) -> list:
    """Auto-detect the 'interesting' (enabled, app-level) services on a Pi.

    Lists enabled service unit-files and filters out base-OS/systemd units, so
    newly installed apps show up automatically — no add/remove-service needed.
    Returns a sorted list of service names (without the .service suffix).
    """
    cmd = ("systemctl list-unit-files --type=service --state=enabled "
           "--no-legend --no-pager --plain 2>/dev/null | awk '{print $1}'")
    stdout, _, code = run_remote(config, cmd)
    if code != 0 or not stdout.strip():
        return []
    seen = set()
    out = []
    for line in stdout.splitlines():
        unit = line.strip()
        if not unit.endswith(".service"):
            continue
        svc = unit[: -len(".service")]
        if not svc or svc in seen or _is_system_service(svc):
            continue
        seen.add(svc)
        out.append(svc)
    return sorted(out)


def stop_service(config: dict, service: str) -> None:
    """Stop a single service on the Pi."""
    console.print(f"[cyan]Stopping {service}...[/cyan]")
    stdout, stderr, code = run_remote(config, f"sudo systemctl stop {service}")
    if code == 0:
        console.print(f"[green]{service} stopped.[/green]")
    else:
        console.print(f"[red]Failed to stop {service}: {stderr}[/red]")


def start_service(config: dict, service: str) -> None:
    """Start a single service on the Pi."""
    console.print(f"[cyan]Starting {service}...[/cyan]")
    stdout, stderr, code = run_remote(config, f"sudo systemctl start {service}")
    if code == 0:
        console.print(f"[green]{service} started.[/green]")
    else:
        console.print(f"[red]Failed to start {service}: {stderr}[/red]")


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


def upgrade_pi(config: dict) -> bool:
    """Fully update the Pi's OS: apt update → full-upgrade → autoremove.

    Uses `full-upgrade` (not plain `upgrade`) so packages needing new
    dependencies — typical for Raspberry Pi OS kernel/firmware bumps — are
    installed instead of held back. Returns True on success.
    """
    console.print("[cyan]Updating package lists...[/cyan]")
    _, stderr, code = run_remote(config, "sudo apt-get update -q")
    if code != 0:
        console.print(f"[red]apt-get update failed: {stderr}[/red]")
        return False
    console.print("[green]Package lists updated.[/green]")

    console.print("[cyan]Upgrading the OS (full-upgrade, this may take a few minutes)...[/cyan]")
    stdout, stderr, code = run_remote(
        config,
        "sudo DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -q",
    )
    if code != 0:
        console.print(f"[red]apt-get full-upgrade failed: {stderr}[/red]")
        return False

    for line in stdout.splitlines():
        if "upgraded" in line or "newly installed" in line:
            console.print(f"[dim]{line.strip()}[/dim]")
            break
    console.print("[green]OS packages upgraded.[/green]")

    # Clean up packages no longer needed after the upgrade.
    console.print("[cyan]Removing unused packages...[/cyan]")
    run_remote(config, "sudo DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -q")
    console.print("[green]Cleanup done.[/green]")
    return True
