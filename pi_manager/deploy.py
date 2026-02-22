import subprocess
from pathlib import Path

import click
import requests
from rich.console import Console

from .ssh import run_remote

console = Console()


def rsync_project(config: dict, name: str) -> bool:
    """Rsync a project to the Pi. Returns True on success."""
    project = config["projects"].get(name)
    if not project:
        console.print(f"[red]Unknown project: {name}[/red]")
        console.print(f"Available: {', '.join(config['projects'].keys())}")
        return False

    local_path = project["local_path"]
    if not Path(local_path).exists():
        console.print(f"[red]Local path does not exist: {local_path}[/red]")
        return False

    # Ensure trailing slash for rsync
    if not local_path.endswith("/"):
        local_path += "/"

    key_path = Path(config["ssh_key_path"]).expanduser()
    remote = f"{config['pi_user']}@{config['pi_host']}:{project['remote_path']}"

    cmd = [
        "rsync",
        "-avz",
        "--delete",
        "--exclude", ".git",
        "--exclude", ".DS_Store",
        "--exclude", "node_modules",
        "-e", f"ssh -i {key_path}",
        local_path,
        remote,
    ]

    console.print(f"[cyan]Syncing {name}...[/cyan]")
    result = subprocess.run(cmd)
    return result.returncode == 0


def purge_cloudflare_cache(config: dict, project: dict) -> bool:
    """Purge the Cloudflare cache for a project. Returns True on success."""
    token = config.get("cloudflare_api_token")
    zone_id = project.get("cloudflare_zone_id", "")

    if not token or not zone_id:
        console.print("[yellow]Cloudflare not configured for this project, skipping cache purge.[/yellow]")
        return True

    resp = requests.post(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"purge_everything": True},
        timeout=15,
    )

    if resp.ok and resp.json().get("success"):
        return True

    console.print(f"[red]Cloudflare purge failed: {resp.text}[/red]")
    return False


def deploy(config: dict, name: str) -> None:
    """Deploy a project: rsync + Cloudflare cache purge."""
    project = config.get("projects", {}).get(name)
    if not project:
        console.print(f"[red]Unknown project: {name}[/red]")
        projects = config.get("projects", {})
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")
        return

    with console.status(f"[bold green]Deploying {name}..."):
        if not rsync_project(config, name):
            console.print("[red]Deploy failed at rsync step.[/red]")
            return

    console.print("[green]Rsync complete.[/green]")

    # Restart Apache after deploy
    console.print("[cyan]Restarting Apache...[/cyan]")
    stdout, stderr, code = run_remote(config, "sudo systemctl restart apache2")
    if code != 0:
        console.print(f"[red]Apache restart failed: {stderr}[/red]")
    else:
        console.print("[green]Apache restarted.[/green]")

    # Purge Cloudflare cache
    console.print("[cyan]Purging Cloudflare cache...[/cyan]")
    if purge_cloudflare_cache(config, project):
        console.print("[green]Cache purged.[/green]")

    console.print(f"\n[bold green]Deploy of {name} complete![/bold green]")
