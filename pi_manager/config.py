import json
import subprocess
from pathlib import Path

import click
import paramiko

CONFIG_DIR = Path.home() / ".pi-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYS_DIR = CONFIG_DIR / "keys"

DEFAULT_CONFIG = {
    "pis": {},
    "default_pi": "",
    "cloudflare_api_token": "",
    "projects": {},
}


# ---------------------------------------------------------------------------
# Exit-aware prompts
# ---------------------------------------------------------------------------


class UserExit(Exception):
    """Raised when user chooses to exit an interactive flow."""


def prompt_with_exit(text, **kwargs):
    """Like click.prompt but treats 'exit'/'quit' as cancellation."""
    result = click.prompt(text, **kwargs)
    if isinstance(result, str) and result.strip().lower() in ("exit", "quit", "q"):
        raise UserExit()
    return result


def confirm_with_exit(text, **kwargs):
    """Like click.confirm — user can type 'exit' to abort (counted as decline)."""
    return click.confirm(text, **kwargs)


def numbered_select(
    items: list[tuple[str, str]],
    prompt_text: str = "Select",
    allow_cancel: bool = True,
) -> str | None:
    """Show a numbered list and return the selected value.

    items: list of (value, display_label) tuples.
    Returns the value of the selected item, or None if cancelled.
    Raises UserExit if user types exit/quit.
    """
    if not items:
        return None

    if len(items) == 1 and not allow_cancel:
        return items[0][0]

    click.echo(f"\n{prompt_text}:")
    for i, (_value, label) in enumerate(items, 1):
        click.echo(f"  {i}) {label}")
    if allow_cancel:
        click.echo("  0) Cancel")

    while True:
        choice = prompt_with_exit(">")
        try:
            idx = int(choice.strip())
        except ValueError:
            click.echo("Please enter a number.")
            continue
        if allow_cancel and idx == 0:
            return None
        if 1 <= idx <= len(items):
            return items[idx - 1][0]
        click.echo("Invalid choice.")


# ---------------------------------------------------------------------------
# Multi-Pi helpers
# ---------------------------------------------------------------------------


def get_pi_config(config: dict, pi_name: str) -> dict:
    """Build a legacy-compatible config dict for a specific Pi.

    Returns a dict with pi_host, pi_user, ssh_key_path, services,
    cloudflare_api_token, and projects — ready for ssh.py, monitor.py, etc.
    """
    pis = config.get("pis", {})
    if pi_name not in pis:
        raise click.ClickException(
            f"Unknown Pi: '{pi_name}'. Available: {', '.join(pis.keys()) or '(none)'}"
        )

    pi = pis[pi_name]
    # Per-Pi token overrides global token
    token = pi.get("cloudflare_api_token") or config.get("cloudflare_api_token", "")
    result = {
        "pi_host": pi["host"],
        "pi_user": pi.get("user", "pi"),
        "ssh_key_path": pi.get("ssh_key_path", "~/.pi-manager/keys/id_rsa"),
        "services": pi.get("services", []),
        "cloudflare_api_token": token,
        "projects": config.get("projects", {}),
    }
    if "tailscale_host" in pi:
        result["tailscale_host"] = pi["tailscale_host"]
    return result


def get_pi_names(config: dict) -> list[str]:
    """Return all Pi names."""
    return list(config.get("pis", {}).keys())


def get_default_pi(config: dict) -> str:
    """Return the default Pi name (may be empty)."""
    return config.get("default_pi", "")


def resolve_pi(config: dict, pi_name: str | None) -> str:
    """Resolve a Pi name: validate if given, fall back to default_pi.

    Raises ClickException if no Pi can be determined.
    """
    if pi_name:
        pis = config.get("pis", {})
        if pi_name not in pis:
            raise click.ClickException(
                f"Unknown Pi: '{pi_name}'. Available: {', '.join(pis.keys()) or '(none)'}"
            )
        return pi_name

    default = get_default_pi(config)
    if not default:
        raise click.ClickException(
            "No Pi specified and no default_pi configured. Use --pi <name> or set a default."
        )
    return default


def migrate_config(config: dict) -> dict:
    """Migrate old single-Pi config to new multi-Pi format."""
    if "pi_host" in config and "pis" not in config:
        pi_name = "pi"
        config["pis"] = {
            pi_name: {
                "host": config.pop("pi_host"),
                "user": config.pop("pi_user"),
                "ssh_key_path": config.pop("ssh_key_path"),
                "services": config.pop("services", []),
            }
        }
        config["default_pi"] = pi_name
        # Add pi field to existing projects
        for proj in config.get("projects", {}).values():
            proj["pi"] = pi_name
        save_config(config)
    return config


def add_pi(
    config: dict,
    name: str,
    host: str,
    user: str,
    ssh_key_path: str,
    services: list[str] | None = None,
    cloudflare_api_token: str = "",
    tailscale_host: str = "",
) -> None:
    """Add a Pi to the config."""
    pi_entry: dict = {
        "host": host,
        "user": user,
        "ssh_key_path": ssh_key_path,
        "services": services or [],
    }
    if cloudflare_api_token:
        pi_entry["cloudflare_api_token"] = cloudflare_api_token
    if tailscale_host:
        pi_entry["tailscale_host"] = tailscale_host

    config.setdefault("pis", {})[name] = pi_entry

    # Set as default if it's the first Pi
    if not config.get("default_pi"):
        config["default_pi"] = name

    save_config(config)


def remove_pi(config: dict, name: str) -> bool:
    """Remove a Pi from the config. Returns True if it existed."""
    pis = config.get("pis", {})
    if name not in pis:
        return False

    del pis[name]

    # Update default_pi if we removed the default
    if config.get("default_pi") == name:
        config["default_pi"] = next(iter(pis), "")

    save_config(config)
    return True


def rename_pi(config: dict, old_name: str, new_name: str) -> bool:
    """Rename a Pi in config, updating all references. Returns True on success."""
    pis = config.get("pis", {})
    if old_name not in pis:
        return False
    if new_name in pis:
        return False

    # Move the Pi entry
    pis[new_name] = pis.pop(old_name)

    # Update default_pi
    if config.get("default_pi") == old_name:
        config["default_pi"] = new_name

    # Update project references
    for proj in config.get("projects", {}).values():
        if proj.get("pi") == old_name:
            proj["pi"] = new_name

    save_config(config)
    return True


def add_service_to_pi(config: dict, pi_name: str, service: str) -> bool:
    """Add a service to a Pi's monitored services list. Returns True on success."""
    pis = config.get("pis", {})
    if pi_name not in pis:
        return False
    services = pis[pi_name].setdefault("services", [])
    if service in services:
        return False
    services.append(service)
    save_config(config)
    return True


def remove_service_from_pi(config: dict, pi_name: str, service: str) -> bool:
    """Remove a service from a Pi's monitored services list. Returns True on success."""
    pis = config.get("pis", {})
    if pi_name not in pis:
        return False
    services = pis[pi_name].get("services", [])
    if service not in services:
        return False
    services.remove(service)
    save_config(config)
    return True


def set_tailscale_ip(config: dict, pi_name: str, ip: str) -> bool:
    """Set the Tailscale IP for a Pi. Returns True on success."""
    pis = config.get("pis", {})
    if pi_name not in pis:
        return False
    pis[pi_name]["tailscale_host"] = ip
    save_config(config)
    return True


def remove_tailscale_ip(config: dict, pi_name: str) -> bool:
    """Remove the Tailscale IP from a Pi. Returns True if it existed."""
    pis = config.get("pis", {})
    if pi_name not in pis:
        return False
    if "tailscale_host" not in pis[pi_name]:
        return False
    del pis[pi_name]["tailscale_host"]
    save_config(config)
    return True


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    return migrate_config(config)


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def test_connection(config: dict) -> bool:
    """Test SSH connection to the Pi. Returns True on success.

    Accepts a legacy-style config (pi_host, pi_user, ssh_key_path)
    as produced by get_pi_config().
    """
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


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _setup_single_pi(default_ssh_key: str = "~/.pi-manager/keys/id_rsa") -> tuple[str, dict]:
    """Interactively configure a single Pi. Returns (name, pi_dict).

    Raises UserExit if the user types 'exit' at any prompt.
    """
    click.echo("  (Type 'exit' at any prompt to cancel)\n")

    pi_name = prompt_with_exit("Pi name (e.g. homepi, mediaserver)")
    pi_host = prompt_with_exit("Pi IP address or hostname")
    pi_user = prompt_with_exit("Pi username", default="pi")

    ssh_key_input = prompt_with_exit("SSH key path", default=default_ssh_key)
    ssh_key_path = Path(ssh_key_input).expanduser()

    # Generate SSH key if it doesn't exist
    if not ssh_key_path.exists():
        click.echo(f"\nGenerating SSH key at {ssh_key_path}...")
        ssh_key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", str(ssh_key_path), "-N", ""],
            check=True,
        )
        click.echo("SSH key generated.")

    # Copy key to Pi (always offer, not just for newly generated keys)
    pub_key = ssh_key_path.with_suffix(".pub")
    if pub_key.exists():
        if click.confirm(f"\nCopy SSH key to {pi_user}@{pi_host}?", default=True):
            subprocess.run(
                ["ssh-copy-id", "-i", str(ssh_key_path), f"{pi_user}@{pi_host}"],
            )

    # Services
    services_input = prompt_with_exit(
        "\nServices to monitor (comma-separated)",
        default="apache2, mariadb, cloudflared",
    )
    services = [s.strip() for s in services_input.split(",") if s.strip()]

    # Tailscale
    tailscale_ip = prompt_with_exit(
        "\nTailscale IP (leave empty to skip)",
        default="",
    )

    pi_dict: dict = {
        "host": pi_host,
        "user": pi_user,
        "ssh_key_path": str(ssh_key_path),
        "services": services,
    }
    if tailscale_ip:
        pi_dict["tailscale_host"] = tailscale_ip

    return pi_name, pi_dict


def first_run_setup() -> dict:
    """Run the interactive setup wizard. Returns config dict.

    Raises UserExit if the user cancels during setup.
    """
    click.echo("Welcome to PiManager! Let's set things up.\n")
    click.echo("  (Type 'exit' at any prompt to cancel)\n")

    # --- Pis ---
    pis = {}
    default_pi = ""

    click.echo("--- Raspberry Pis ---")
    while True:
        pi_name, pi_dict = _setup_single_pi()
        pis[pi_name] = pi_dict
        if not default_pi:
            default_pi = pi_name
        click.echo(f"\n  Added Pi '{pi_name}'.")
        if not click.confirm("\nAdd another Pi?", default=False):
            break

    # --- Cloudflare ---
    cf_token = ""
    click.echo("\n--- Cloudflare ---")
    if click.confirm("Do you use Cloudflare for cache purging?", default=False):
        if len(pis) > 1 and not click.confirm(
            "Same Cloudflare account for all Pis?", default=True
        ):
            click.echo("Enter a Cloudflare API token per Pi (leave empty to skip):")
            for pi_name_key in pis:
                token = prompt_with_exit(f"  Token for {pi_name_key}", default="")
                if token:
                    pis[pi_name_key]["cloudflare_api_token"] = token
        else:
            cf_token = prompt_with_exit("Cloudflare API token", default="")

    # --- Projects ---
    has_cf = bool(cf_token) or any(
        "cloudflare_api_token" in pi for pi in pis.values()
    )

    projects: dict = {}
    click.echo("\n--- Projects ---")
    click.echo("Add projects you want to deploy to a Pi.")
    pi_names = list(pis.keys())
    while click.confirm("Add a project?", default=not projects):
        name = prompt_with_exit("  Project name")
        local_path = prompt_with_exit("  Local path (folder to sync)")
        remote_path = prompt_with_exit("  Remote path on Pi (e.g. /var/www/my-site/)")

        # Which Pi?
        if pi_names:
            if len(pi_names) == 1:
                target_pi = pi_names[0]
                click.echo(f"  Target Pi: {target_pi}")
            else:
                items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
                target_pi = numbered_select(items, "  Select target Pi", allow_cancel=False)
        else:
            target_pi = ""

        project: dict = {
            "local_path": local_path,
            "remote_path": remote_path,
            "pi": target_pi,
        }
        if has_cf:
            cf_zone = prompt_with_exit("  Cloudflare zone ID (leave empty to skip)", default="")
            if cf_zone:
                project["cloudflare_zone_id"] = cf_zone
        projects[name] = project
        click.echo(f"  Added '{name}'.\n")

    config = {
        "pis": pis,
        "default_pi": default_pi,
        "cloudflare_api_token": cf_token,
        "projects": projects,
    }

    save_config(config)
    click.echo("\nConfig saved to ~/.pi-manager/config.json")

    # Test connections
    for pi_name in pis:
        pi_cfg = get_pi_config(config, pi_name)
        click.echo(f"\nTesting SSH connection to {pi_name}...")
        if test_connection(pi_cfg):
            click.echo(click.style(f"  {pi_name}: Connected successfully!", fg="green"))
        else:
            click.echo(click.style(f"  {pi_name}: Could not connect.", fg="yellow"))
            click.echo("  Check that your Pi is powered on and the IP/key are correct.")
            click.echo("  You can re-run setup anytime with: pi setup")

    return config


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------


def add_project(
    config: dict,
    name: str,
    local_path: str,
    remote_path: str,
    pi_name: str = "",
    cloudflare_zone_id: str = "",
) -> None:
    """Add a project to the config."""
    project: dict = {
        "local_path": local_path,
        "remote_path": remote_path,
    }
    if pi_name:
        project["pi"] = pi_name
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
