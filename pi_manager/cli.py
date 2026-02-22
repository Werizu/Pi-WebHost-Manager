import shutil
import subprocess
import sys

import click
from rich.console import Console
from rich.table import Table

from .config import CONFIG_DIR, load_config, save_config, first_run_setup, add_project, remove_project

console = Console()


def ensure_config() -> dict:
    """Load config or run first-time setup."""
    config = load_config()
    if not config:
        config = first_run_setup()
    return config


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """PiManager — manage your Raspberry Pi from the command line."""
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand is None:
        # No subcommand → launch interactive REPL
        from .repl import start_repl
        start_repl()
    else:
        ctx.obj["config"] = ensure_config()


@cli.command()
@click.argument("name")
@click.pass_context
def deploy(ctx, name):
    """Deploy a project to the Pi (rsync + cache purge)."""
    from .deploy import deploy as do_deploy

    do_deploy(ctx.obj["config"], name)


@cli.command()
@click.pass_context
def status(ctx):
    """Show Pi system status (CPU, RAM, disk, temp, uptime)."""
    from .monitor import show_status

    show_status(ctx.obj["config"])


@cli.command()
@click.pass_context
def services(ctx):
    """Show status of monitored services."""
    from .monitor import show_services

    show_services(ctx.obj["config"])


@cli.command()
@click.option("--live", is_flag=True, help="Stream logs in real-time.")
@click.option("--lines", "-n", default=30, help="Number of lines to show.")
@click.pass_context
def logs(ctx, live, lines):
    """Show Apache error logs."""
    from .monitor import show_logs

    show_logs(ctx.obj["config"], live=live, lines=lines)


@cli.command()
@click.argument("service")
@click.pass_context
def restart(ctx, service):
    """Restart a service (or 'all' for all monitored services)."""
    from .services import restart_service, restart_all

    if service == "all":
        restart_all(ctx.obj["config"])
    else:
        restart_service(ctx.obj["config"], service)


@cli.command()
@click.pass_context
def ssh(ctx):
    """Open an interactive SSH session to the Pi."""
    from .ssh import open_ssh_session

    open_ssh_session(ctx.obj["config"])


@cli.command()
@click.pass_context
def shutdown(ctx):
    """Shut down the Pi."""
    from .services import shutdown_pi

    shutdown_pi(ctx.obj["config"])


@cli.command()
@click.pass_context
def reboot(ctx):
    """Reboot the Pi."""
    from .services import reboot_pi

    reboot_pi(ctx.obj["config"])


# --- New commands ---


@cli.command()
@click.pass_context
def setup(ctx):
    """Re-run the setup wizard to reconfigure PiManager."""
    config = first_run_setup()
    ctx.obj["config"] = config


@cli.command("config")
@click.pass_context
def show_config(ctx):
    """Show the current PiManager configuration."""
    config = ctx.obj["config"]

    table = Table(title="PiManager Config", show_header=True, header_style="bold cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row("Pi host", config.get("pi_host", ""))
    table.add_row("Pi user", config.get("pi_user", ""))
    table.add_row("SSH key", config.get("ssh_key_path", ""))

    # Mask the Cloudflare token
    cf_token = config.get("cloudflare_api_token", "")
    if cf_token:
        masked = cf_token[:4] + "..." + cf_token[-4:] if len(cf_token) > 8 else "****"
    else:
        masked = "(not set)"
    table.add_row("Cloudflare token", masked)

    services_list = config.get("services", [])
    table.add_row("Services", ", ".join(services_list) if services_list else "(none)")

    projects = config.get("projects", {})
    table.add_row("Projects", ", ".join(projects.keys()) if projects else "(none)")

    console.print(table)


@cli.group()
@click.pass_context
def project(ctx):
    """Manage deploy projects (add, list, remove)."""
    pass


@project.command("add")
@click.pass_context
def project_add(ctx):
    """Add a new project interactively."""
    config = ctx.obj["config"]

    name = click.prompt("Project name")
    if name in config.get("projects", {}):
        console.print(f"[yellow]Project '{name}' already exists. Use a different name.[/yellow]")
        return

    local_path = click.prompt("Local path (folder to sync)")
    remote_path = click.prompt("Remote path on Pi (e.g. /var/www/my-site/)")
    cf_zone = click.prompt("Cloudflare zone ID (leave empty to skip)", default="")

    add_project(config, name, local_path, remote_path, cloudflare_zone_id=cf_zone)
    console.print(f"[green]Project '{name}' added.[/green]")
    console.print(f"Deploy with: [bold]pi deploy {name}[/bold]")


@project.command("list")
@click.pass_context
def project_list(ctx):
    """List all configured projects."""
    projects = ctx.obj["config"].get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured. Run `pi project add` to add one.[/yellow]")
        return

    table = Table(title="Projects", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Local path")
    table.add_column("Remote path")
    table.add_column("CF zone")

    for name, info in projects.items():
        zone = info.get("cloudflare_zone_id", "")
        table.add_row(name, info.get("local_path", ""), info.get("remote_path", ""), zone or "-")

    console.print(table)


@project.command("remove")
@click.argument("name")
@click.pass_context
def project_remove(ctx, name):
    """Remove a project by name."""
    config = ctx.obj["config"]

    if remove_project(config, name):
        console.print(f"[green]Project '{name}' removed.[/green]")
    else:
        console.print(f"[red]Project '{name}' not found.[/red]")
        projects = config.get("projects", {})
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")


@cli.command()
def uninstall():
    """Uninstall PiManager (remove config + pipx package)."""
    if not click.confirm("This will delete your config and uninstall PiManager. Continue?"):
        return

    # Remove config directory
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]Config removed (~/.pi-manager)[/green]")

    # Uninstall via pipx
    console.print("[cyan]Uninstalling via pipx...[/cyan]")
    subprocess.run(["pipx", "uninstall", "pi-manager"])
