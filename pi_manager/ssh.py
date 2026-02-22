import subprocess
import socket
from pathlib import Path

import paramiko
from rich.console import Console

console = Console()


class SSHError(Exception):
    """Raised when SSH connection fails with a user-friendly message."""


def _connect(config: dict) -> paramiko.SSHClient:
    """Create and return a connected SSH client."""
    key_path = Path(config["ssh_key_path"]).expanduser()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=config["pi_host"],
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


def open_ssh_session(config: dict) -> None:
    """Open an interactive SSH session in a new Terminal.app window."""
    key_path = Path(config["ssh_key_path"]).expanduser()
    ssh_cmd = f"ssh -i {key_path} {config['pi_user']}@{config['pi_host']}"

    applescript = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "{ssh_cmd}"\n'
        "end tell"
    )

    console.print("[cyan]Opening SSH in new terminal...[/cyan]")
    subprocess.run(["osascript", "-e", applescript])
