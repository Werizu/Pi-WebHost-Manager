import json
import subprocess
from pathlib import Path

import click
import paramiko

CONFIG_DIR = Path.home() / ".pi-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYS_DIR = CONFIG_DIR / "keys"

DEFAULT_CONFIG = {
    "pi_host": "",
    "pi_user": "pi",
    "ssh_key_path": "~/.pi-manager/keys/id_rsa",
    "cloudflare_api_token": "",
    "services": ["apache2", "mariadb", "cloudflared"],
    "projects": {},
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def test_connection(config: dict) -> bool:
    """Test SSH connection to the Pi. Returns True on success."""
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
        client.close()
        return True
    except Exception:
        return False


def first_run_setup() -> dict:
    click.echo("Welcome to PiManager! Let's set things up.\n")

    pi_host = click.prompt("Pi IP address or hostname")
    pi_user = click.prompt("Pi username", default="pi")

    ssh_key_path = Path(
        click.prompt("SSH key path", default=DEFAULT_CONFIG["ssh_key_path"])
    ).expanduser()

    # Generate SSH key if it doesn't exist
    if not ssh_key_path.exists():
        click.echo(f"\nGenerating SSH key at {ssh_key_path}...")
        ssh_key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", str(ssh_key_path), "-N", ""],
            check=True,
        )
        click.echo("SSH key generated.")

        # Copy key to Pi
        if click.confirm(f"\nCopy SSH key to {pi_user}@{pi_host}?", default=True):
            subprocess.run(
                ["ssh-copy-id", "-i", str(ssh_key_path), f"{pi_user}@{pi_host}"],
            )

    # Services
    default_services = ", ".join(DEFAULT_CONFIG["services"])
    services_input = click.prompt(
        "\nServices to monitor (comma-separated)",
        default=default_services,
    )
    services = [s.strip() for s in services_input.split(",") if s.strip()]

    # Cloudflare (optional)
    cf_token = click.prompt("\nCloudflare API token (leave empty to skip)", default="")

    # Projects
    projects = {}
    click.echo("\n--- Projects ---")
    click.echo("Add projects you want to deploy to the Pi.")
    while click.confirm("Add a project?", default=not projects):
        name = click.prompt("  Project name")
        local_path = click.prompt("  Local path (folder to sync)")
        remote_path = click.prompt("  Remote path on Pi (e.g. /var/www/my-site/)")
        cf_zone = ""
        if cf_token:
            cf_zone = click.prompt("  Cloudflare zone ID (leave empty to skip)", default="")
        project = {"local_path": local_path, "remote_path": remote_path}
        if cf_zone:
            project["cloudflare_zone_id"] = cf_zone
        projects[name] = project
        click.echo(f"  Added '{name}'.\n")

    config = {
        "pi_host": pi_host,
        "pi_user": pi_user,
        "ssh_key_path": str(ssh_key_path),
        "cloudflare_api_token": cf_token,
        "services": services,
        "projects": projects,
    }

    save_config(config)
    click.echo("\nConfig saved to ~/.pi-manager/config.json")

    # Test connection
    click.echo("\nTesting SSH connection...")
    if test_connection(config):
        click.echo(click.style("Connected successfully!", fg="green"))
    else:
        click.echo(click.style("Could not connect.", fg="yellow"))
        click.echo("Check that your Pi is powered on and the IP/key are correct.")
        click.echo("You can re-run setup anytime with: pi setup")

    return config


def add_project(
    config: dict, name: str, local_path: str, remote_path: str, cloudflare_zone_id: str = ""
) -> None:
    """Add a project to the config."""
    project = {
        "local_path": local_path,
        "remote_path": remote_path,
    }
    if cloudflare_zone_id:
        project["cloudflare_zone_id"] = cloudflare_zone_id
    config.setdefault("projects", {})[name] = project
    save_config(config)


def remove_project(config: dict, name: str) -> bool:
    """Remove a project from the config. Returns True if it existed."""
    if name in config.get("projects", {}):
        del config["projects"][name]
        save_config(config)
        return True
    return False
