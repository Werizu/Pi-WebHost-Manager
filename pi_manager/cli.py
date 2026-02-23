import shutil
import subprocess
import sys

import click
from rich.console import Console
from rich.table import Table

from .config import (
    CONFIG_DIR,
    load_config,
    save_config,
    first_run_setup,
    add_project,
    remove_project,
    get_pi_config,
    get_pi_names,
    get_default_pi,
    resolve_pi,
    add_pi,
)

console = Console()


def ensure_config() -> dict:
    """Load config or run first-time setup."""
    config = load_config()
    if not config:
        config = first_run_setup()
    return config


# ---------------------------------------------------------------------------
# Shared --pi option
# ---------------------------------------------------------------------------


def pi_option(f):
    """Click decorator: adds --pi option to a command."""
    return click.option("--pi", "pi_name", default=None, help="Target Pi name")(f)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """PiManager — manage your Raspberry Pis from the command line."""
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand is None:
        # No subcommand → launch interactive REPL
        from .repl import start_repl
        start_repl()
    else:
        ctx.obj["config"] = ensure_config()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("name")
@pi_option
@click.pass_context
def deploy(ctx, name, pi_name):
    """Deploy a project to a Pi (rsync + cache purge)."""
    from .deploy import deploy as do_deploy

    config = ctx.obj["config"]
    project = config.get("projects", {}).get(name)
    if not project:
        console.print(f"[red]Unknown project: {name}[/red]")
        projects = config.get("projects", {})
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")
        return

    # Resolve Pi: explicit --pi > project config > default
    if not pi_name:
        pi_name = project.get("pi")
    pi_name = resolve_pi(config, pi_name)

    pi_cfg = get_pi_config(config, pi_name)
    pi_cfg["projects"] = config.get("projects", {})
    do_deploy(pi_cfg, name, pi_name=pi_name)


@cli.command()
@pi_option
@click.pass_context
def status(ctx, pi_name):
    """Show Pi system status (CPU, RAM, disk, temp, uptime)."""
    from .monitor import show_status

    config = ctx.obj["config"]
    if pi_name:
        pi_cfg = get_pi_config(config, pi_name)
        console.print(f"\n[bold cyan]--- {pi_name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        show_status(pi_cfg)
    else:
        # All Pis
        for name in get_pi_names(config):
            pi_cfg = get_pi_config(config, name)
            console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
            show_status(pi_cfg)


@cli.command()
@pi_option
@click.pass_context
def services(ctx, pi_name):
    """Show status of monitored services."""
    from .monitor import show_services

    config = ctx.obj["config"]
    if pi_name:
        pi_cfg = get_pi_config(config, pi_name)
        console.print(f"\n[bold cyan]--- {pi_name} ---[/bold cyan]")
        show_services(pi_cfg)
    else:
        for name in get_pi_names(config):
            pi_cfg = get_pi_config(config, name)
            console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
            show_services(pi_cfg)


@cli.command()
@click.option("--live", is_flag=True, help="Stream logs in real-time.")
@click.option("--lines", "-n", default=30, help="Number of lines to show.")
@pi_option
@click.pass_context
def logs(ctx, live, lines, pi_name):
    """Show Apache error logs."""
    from .monitor import show_logs

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    show_logs(pi_cfg, live=live, lines=lines)


@cli.command()
@click.argument("service")
@pi_option
@click.pass_context
def restart(ctx, service, pi_name):
    """Restart a service (or 'all' for all monitored services)."""
    from .services import restart_service, restart_all

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)

    if service == "all":
        restart_all(pi_cfg)
    else:
        restart_service(pi_cfg, service)


@cli.command()
@pi_option
@click.pass_context
def ssh(ctx, pi_name):
    """Open an interactive SSH session to a Pi."""
    from .ssh import open_ssh_session

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    open_ssh_session(pi_cfg)


@cli.command()
@pi_option
@click.pass_context
def shutdown(ctx, pi_name):
    """Shut down a Pi."""
    from .services import shutdown_pi

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    shutdown_pi(pi_cfg)


@cli.command()
@pi_option
@click.pass_context
def reboot(ctx, pi_name):
    """Reboot a Pi."""
    from .services import reboot_pi

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    reboot_pi(pi_cfg)


# ---------------------------------------------------------------------------
# New commands: list-pis, add-pi
# ---------------------------------------------------------------------------


@cli.command("list-pis")
@click.pass_context
def list_pis(ctx):
    """List all configured Pis."""
    config = ctx.obj["config"]
    pis = config.get("pis", {})
    default = get_default_pi(config)

    if not pis:
        console.print("[yellow]No Pis configured. Run `pi setup` or `pi add-pi`.[/yellow]")
        return

    table = Table(title="Raspberry Pis", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Host")
    table.add_column("User")
    table.add_column("Services")
    table.add_column("Default")

    for name, info in pis.items():
        is_default = "★" if name == default else ""
        svcs = ", ".join(info.get("services", [])) or "-"
        table.add_row(name, info.get("host", ""), info.get("user", "pi"), svcs, is_default)

    console.print(table)


@cli.command("add-pi")
@click.pass_context
def add_pi_cmd(ctx):
    """Add a new Pi interactively."""
    from .config import _setup_single_pi, test_connection

    config = ctx.obj["config"]

    pi_name, pi_dict = _setup_single_pi()

    if pi_name in config.get("pis", {}):
        console.print(f"[yellow]Pi '{pi_name}' already exists. Use a different name.[/yellow]")
        return

    add_pi(
        config,
        pi_name,
        host=pi_dict["host"],
        user=pi_dict["user"],
        ssh_key_path=pi_dict["ssh_key_path"],
        services=pi_dict.get("services", []),
    )

    # Ask for Cloudflare token if this Pi uses a different account
    if config.get("cloudflare_api_token"):
        if not click.confirm(
            f"\nUse the global Cloudflare token for {pi_name}?", default=True
        ):
            token = click.prompt(f"Cloudflare API token for {pi_name}", default="")
            if token:
                config["pis"][pi_name]["cloudflare_api_token"] = token
                save_config(config)
    else:
        token = click.prompt(
            f"\nCloudflare API token for {pi_name} (leave empty to skip)", default=""
        )
        if token:
            config["pis"][pi_name]["cloudflare_api_token"] = token
            save_config(config)

    console.print(f"[green]Pi '{pi_name}' added.[/green]")

    # Test connection
    pi_cfg = get_pi_config(config, pi_name)
    console.print("Testing SSH connection...")
    if test_connection(pi_cfg):
        console.print(click.style("Connected successfully!", fg="green"))
    else:
        console.print(click.style("Could not connect.", fg="yellow"))
        console.print("Check that your Pi is powered on and the IP/key are correct.")


# ---------------------------------------------------------------------------
# Setup / Config / Project commands
# ---------------------------------------------------------------------------


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

    # Global settings
    table = Table(title="PiManager Config", show_header=True, header_style="bold cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row("Default Pi", config.get("default_pi", "(not set)"))

    cf_token = config.get("cloudflare_api_token", "")
    if cf_token:
        masked = cf_token[:4] + "..." + cf_token[-4:] if len(cf_token) > 8 else "****"
    else:
        masked = "(not set)"
    table.add_row("Cloudflare token", masked)

    projects = config.get("projects", {})
    table.add_row("Projects", ", ".join(projects.keys()) if projects else "(none)")

    console.print(table)

    # Per-Pi table
    pis = config.get("pis", {})
    default = get_default_pi(config)
    if pis:
        pi_table = Table(title="Pis", show_header=True, header_style="bold cyan")
        pi_table.add_column("Name", style="bold")
        pi_table.add_column("Host")
        pi_table.add_column("User")
        pi_table.add_column("SSH key")
        pi_table.add_column("Services")
        pi_table.add_column("Default")

        for name, info in pis.items():
            is_default = "★" if name == default else ""
            svcs = ", ".join(info.get("services", [])) or "-"
            pi_table.add_row(
                name,
                info.get("host", ""),
                info.get("user", "pi"),
                info.get("ssh_key_path", ""),
                svcs,
                is_default,
            )
        console.print(pi_table)


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

    # Target Pi
    pi_names = get_pi_names(config)
    if len(pi_names) == 1:
        target_pi = pi_names[0]
        console.print(f"Target Pi: {target_pi}")
    elif pi_names:
        target_pi = click.prompt(
            f"Target Pi ({', '.join(pi_names)})", default=get_default_pi(config)
        )
    else:
        target_pi = ""

    cf_zone = click.prompt("Cloudflare zone ID (leave empty to skip)", default="")

    add_project(config, name, local_path, remote_path, pi_name=target_pi, cloudflare_zone_id=cf_zone)
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
    table.add_column("Pi")
    table.add_column("CF zone")

    for name, info in projects.items():
        zone = info.get("cloudflare_zone_id", "")
        table.add_row(
            name,
            info.get("local_path", ""),
            info.get("remote_path", ""),
            info.get("pi", "-"),
            zone or "-",
        )

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
