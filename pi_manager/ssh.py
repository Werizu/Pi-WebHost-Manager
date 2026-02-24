import ipaddress
import subprocess
import socket
from pathlib import Path

import paramiko
from rich.console import Console

console = Console()


class SSHError(Exception):
    """Raised when SSH connection fails with a user-friendly message."""


def is_on_home_network() -> bool:
    """Check if this machine has a 192.168.178.x IP (Fritz!Box home network)."""
    home_net = ipaddress.IPv4Network("192.168.178.0/24")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = ipaddress.IPv4Address(info[4][0])
            if addr in home_net:
                return True
    except (socket.gaierror, ValueError):
        pass
    return False


def resolve_host(pi_config: dict) -> tuple[str, str]:
    """Return (host, label) — LAN or Tailscale based on network."""
    if is_on_home_network():
        return pi_config["pi_host"], f"LAN ({pi_config['pi_host']})"
    ts = pi_config.get("tailscale_host")
    if ts:
        return ts, f"Tailscale ({ts})"
    raise SSHError(
        f"Not on home network and no Tailscale IP configured for this Pi. "
        f"Set one with: pi tailscale set <name> <ip>"
    )


def _connect(config: dict) -> paramiko.SSHClient:
    """Create and return a connected SSH client."""
    host, label = resolve_host(config)
    console.print(f"[dim]→ {label}[/dim]")

    key_path = Path(config["ssh_key_path"]).expanduser()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=config["pi_user"],
            key_filename=str(key_path),
            timeout=10,
        )
        return client
    except paramiko.AuthenticationException:
        raise SSHError(
            "SSH key rejected — run `pi setup` to reconfigure."
        )
    except (ConnectionRefusedError, socket.error) as e:
        if "refused" in str(e).lower():
            raise SSHError(
                "Can't reach the Pi — is it powered on and SSH enabled?"
            )
        raise SSHError(f"Connection error: {e}")
    except socket.timeout:
        raise SSHError(
            "Connection timed out — check your network and Pi IP address."
        )
    except FileNotFoundError:
        raise SSHError(
            f"SSH key not found at {key_path} — run `pi setup` to reconfigure."
        )
    except Exception as e:
        raise SSHError(f"SSH connection failed: {e}")


def run_remote(config: dict, cmd: str) -> tuple[str, str, int]:
    """Execute a command on the Pi via SSH. Returns (stdout, stderr, exit_code)."""
    client = _connect(config)
    try:
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        return stdout.read().decode().strip(), stderr.read().decode().strip(), exit_code
    finally:
        client.close()


def get_ssh_client(config: dict) -> paramiko.SSHClient:
    """Return a connected paramiko SSHClient (caller must close)."""
    return _connect(config)


def ping_pi(config: dict) -> None:
    """Check if a Pi is reachable via SSH and show response time."""
    import time as _time

    try:
        host, label = resolve_host(config)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")
        return
    console.print(f"[cyan]Pinging {host} via SSH...[/cyan] [dim]({label})[/dim]")
    start = _time.monotonic()
    try:
        client = _connect(config)
        elapsed_ms = (_time.monotonic() - start) * 1000
        client.close()
        console.print(f"[green]Reachable — {elapsed_ms:.0f} ms[/green]")
    except SSHError as e:
        console.print(f"[red]Unreachable — {e}[/red]")


def open_ssh_session(config: dict) -> None:
    """Open an interactive SSH session in a new Terminal.app window."""
    host, label = resolve_host(config)
    console.print(f"[dim]→ {label}[/dim]")

    key_path = Path(config["ssh_key_path"]).expanduser()
    ssh_cmd = f'ssh -i \\"{key_path}\\" {config["pi_user"]}@{host}'

    applescript = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "{ssh_cmd}"\n'
        "end tell"
    )

    console.print("[cyan]Opening SSH in new terminal...[/cyan]")
    subprocess.run(["osascript", "-e", applescript], capture_output=True)
