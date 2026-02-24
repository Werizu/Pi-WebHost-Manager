import shutil
import subprocess
import sys

import click
from rich.console import Console
from rich.table import Table

from .config import (
    CONFIG_DIR,
    UserExit,
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
    remove_pi,
    rename_pi,
    add_service_to_pi,
    remove_service_from_pi,
    prompt_with_exit,
    numbered_select,
)
from .ssh import SSHError

console = Console()


def ensure_config() -> dict:
    """Load config or run first-time setup."""
    config = load_config()
    if not config:
        try:
            config = first_run_setup()
        except UserExit:
            console.print("\n[yellow]Setup cancelled.[/yellow]")
            sys.exit(0)
    return config


# ---------------------------------------------------------------------------
# Shared --pi option
# ---------------------------------------------------------------------------


def pi_option(f):
    """Click decorator: adds --pi option to a command."""
    return click.option("--pi", "pi_name", default=None, help="Target Pi name")(f)


# ---------------------------------------------------------------------------
# Helper: list remote directories on a Pi
# ---------------------------------------------------------------------------


def _list_remote_dirs(pi_cfg: dict, base_path: str = "/var/www") -> list[str]:
    """List directories on a Pi via SSH. Returns list of paths."""
    from .ssh import run_remote

    stdout, _, code = run_remote(
        pi_cfg,
        f'find {base_path} -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort',
    )
    if code == 0 and stdout.strip():
        return [d.strip() for d in stdout.strip().split("\n") if d.strip()]
    return []


def _choose_remote_path(pi_cfg: dict) -> str:
    """Let user pick a remote path from Pi directories or enter manually.

    Raises UserExit if the user cancels.
    """
    dirs = _list_remote_dirs(pi_cfg)

    if not dirs:
        return prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")

    console.print("\n[cyan]Directories on Pi:[/cyan]")
    for i, d in enumerate(dirs, 1):
        console.print(f"  {i}) {d}")
    console.print(f"  {len(dirs) + 1}) Enter manually")
    console.print(f"  0) Cancel")

    while True:
        choice = prompt_with_exit("Choose", default=str(len(dirs) + 1))
        try:
            idx = int(choice)
        except ValueError:
            console.print("[yellow]Please enter a number.[/yellow]")
            continue

        if idx == 0:
            raise UserExit()
        if 1 <= idx <= len(dirs):
            path = dirs[idx - 1]
            if not path.endswith("/"):
                path += "/"
            return path
        if idx == len(dirs) + 1:
            return prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")
        console.print("[yellow]Invalid choice.[/yellow]")


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
    elif ctx.invoked_subcommand == "update":
        # update works without full setup
        ctx.obj["config"] = load_config() or {}
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
            try:
                show_status(pi_cfg)
            except SSHError as e:
                console.print(f"[red]Offline — {e}[/red]")


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
            try:
                show_services(pi_cfg)
            except SSHError as e:
                console.print(f"[red]Offline — {e}[/red]")


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
@click.argument("service")
@pi_option
@click.pass_context
def stop(ctx, service, pi_name):
    """Stop a service on a Pi."""
    from .services import stop_service

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    stop_service(pi_cfg, service)


@cli.command()
@click.argument("service")
@pi_option
@click.pass_context
def start(ctx, service, pi_name):
    """Start a service on a Pi."""
    from .services import start_service

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)
    start_service(pi_cfg, service)


@cli.command()
@pi_option
@click.pass_context
def ping(ctx, pi_name):
    """Check if a Pi is reachable via SSH and show response time."""
    from .ssh import ping_pi

    config = ctx.obj["config"]
    if pi_name:
        pi_cfg = get_pi_config(config, pi_name)
        console.print(f"\n[bold cyan]--- {pi_name} ---[/bold cyan]")
        ping_pi(pi_cfg)
    else:
        for name in get_pi_names(config):
            pi_cfg = get_pi_config(config, name)
            console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
            ping_pi(pi_cfg)


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
    sys.exit(0)


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
# Pi management: list-pis, add-pi, remove-pi
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
        is_default = "\u2605" if name == default else ""
        svcs = ", ".join(info.get("services", [])) or "-"
        table.add_row(name, info.get("host", ""), info.get("user", "pi"), svcs, is_default)

    console.print(table)


@cli.command("add-pi")
@click.pass_context
def add_pi_cmd(ctx):
    """Add a new Pi interactively."""
    from .config import _setup_single_pi, test_connection

    config = ctx.obj["config"]

    try:
        pi_name, pi_dict = _setup_single_pi()
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

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
    try:
        if config.get("cloudflare_api_token"):
            if not click.confirm(
                f"\nUse the global Cloudflare token for {pi_name}?", default=True
            ):
                token = prompt_with_exit(f"Cloudflare API token for {pi_name}", default="")
                if token:
                    config["pis"][pi_name]["cloudflare_api_token"] = token
                    save_config(config)
        else:
            token = prompt_with_exit(
                f"\nCloudflare API token for {pi_name} (leave empty to skip)", default=""
            )
            if token:
                config["pis"][pi_name]["cloudflare_api_token"] = token
                save_config(config)
    except UserExit:
        # Pi was already added, just skip Cloudflare config
        pass

    console.print(f"[green]Pi '{pi_name}' added.[/green]")

    # Test connection
    pi_cfg = get_pi_config(config, pi_name)
    console.print("Testing SSH connection...")
    if test_connection(pi_cfg):
        console.print(click.style("Connected successfully!", fg="green"))
    else:
        console.print(click.style("Could not connect.", fg="yellow"))
        console.print("Check that your Pi is powered on and the IP/key are correct.")


@cli.command("remove-pi")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def remove_pi_cmd(ctx, name):
    """Remove a Pi by name."""
    name = " ".join(name)
    config = ctx.obj["config"]

    if remove_pi(config, name):
        console.print(f"[green]Pi '{name}' removed.[/green]")
    else:
        console.print(f"[red]Pi '{name}' not found.[/red]")
        pis = config.get("pis", {})
        if pis:
            console.print(f"Available: {', '.join(pis.keys())}")


@cli.command("rename-pi")
@click.argument("old_name")
@click.argument("new_name")
@click.pass_context
def rename_pi_cmd(ctx, old_name, new_name):
    """Rename a Pi, updating all references (including projects)."""
    config = ctx.obj["config"]

    if old_name not in config.get("pis", {}):
        console.print(f"[red]Pi '{old_name}' not found.[/red]")
        pis = config.get("pis", {})
        if pis:
            console.print(f"Available: {', '.join(pis.keys())}")
        return

    if new_name in config.get("pis", {}):
        console.print(f"[red]Pi '{new_name}' already exists.[/red]")
        return

    rename_pi(config, old_name, new_name)
    console.print(f"[green]Pi '{old_name}' renamed to '{new_name}'.[/green]")


@cli.command("edit-pi")
@click.pass_context
def edit_pi_cmd(ctx):
    """Edit an existing Pi's host, user, or SSH key path interactively."""
    config = ctx.obj["config"]
    pi_names = get_pi_names(config)

    if not pi_names:
        console.print("[yellow]No Pis configured. Run `pi add-pi` first.[/yellow]")
        return

    try:
        pis = config.get("pis", {})
        items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
        selected = numbered_select(items, "Select a Pi to edit", allow_cancel=True)
        if not selected:
            return

        pi = pis[selected]
        console.print(f"\nEditing [bold]{selected}[/bold] (leave empty to keep current value)\n")

        new_host = prompt_with_exit(f"Host [{pi['host']}]", default="")
        new_user = prompt_with_exit(f"User [{pi.get('user', 'pi')}]", default="")
        new_key = prompt_with_exit(
            f"SSH key path [{pi.get('ssh_key_path', '~/.pi-manager/keys/id_rsa')}]",
            default="",
        )

        changed = False
        if new_host:
            pi["host"] = new_host
            changed = True
        if new_user:
            pi["user"] = new_user
            changed = True
        if new_key:
            pi["ssh_key_path"] = new_key
            changed = True

        if changed:
            save_config(config)
            console.print(f"[green]Pi '{selected}' updated.[/green]")
        else:
            console.print("[dim]No changes made.[/dim]")

    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")


@cli.command("add-service")
@click.argument("name")
@pi_option
@click.pass_context
def add_service_cmd(ctx, name, pi_name):
    """Add a service to a Pi's monitored services list."""
    config = ctx.obj["config"]

    if not pi_name:
        pi_names = get_pi_names(config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        if len(pi_names) == 1:
            pi_name = pi_names[0]
        else:
            try:
                pis = config.get("pis", {})
                items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
                pi_name = numbered_select(items, "Select a Pi", allow_cancel=True)
                if not pi_name:
                    return
            except UserExit:
                console.print("\n[yellow]Cancelled.[/yellow]")
                return

    if add_service_to_pi(config, pi_name, name):
        console.print(f"[green]Service '{name}' added to {pi_name}.[/green]")
    else:
        pis = config.get("pis", {})
        if pi_name not in pis:
            console.print(f"[red]Pi '{pi_name}' not found.[/red]")
        else:
            console.print(f"[yellow]Service '{name}' already monitored on {pi_name}.[/yellow]")


@cli.command("remove-service")
@click.argument("name", required=False)
@pi_option
@click.pass_context
def remove_service_cmd(ctx, name, pi_name):
    """Remove a service from a Pi's monitored services list."""
    config = ctx.obj["config"]

    if not pi_name:
        pi_names = get_pi_names(config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        if len(pi_names) == 1:
            pi_name = pi_names[0]
        else:
            try:
                pis = config.get("pis", {})
                items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
                pi_name = numbered_select(items, "Select a Pi", allow_cancel=True)
                if not pi_name:
                    return
            except UserExit:
                console.print("\n[yellow]Cancelled.[/yellow]")
                return

    # If no service name given, show numbered selection
    if not name:
        services = config.get("pis", {}).get(pi_name, {}).get("services", [])
        if not services:
            console.print(f"[yellow]No services configured for {pi_name}.[/yellow]")
            return
        try:
            items = [(s, s) for s in services]
            name = numbered_select(items, f"Remove service from {pi_name}", allow_cancel=True)
            if not name:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    if remove_service_from_pi(config, pi_name, name):
        console.print(f"[green]Service '{name}' removed from {pi_name}.[/green]")
    else:
        pis = config.get("pis", {})
        if pi_name not in pis:
            console.print(f"[red]Pi '{pi_name}' not found.[/red]")
        else:
            console.print(f"[red]Service '{name}' not found on {pi_name}.[/red]")
            services = pis[pi_name].get("services", [])
            if services:
                console.print(f"Current services: {', '.join(services)}")


@cli.command("open")
@click.argument("project", required=False)
@click.pass_context
def open_cmd(ctx, project):
    """Open a project's website URL in the default browser."""
    import subprocess as sp

    config = ctx.obj["config"]
    projects = config.get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured.[/yellow]")
        return

    if not project:
        try:
            items = [
                (name, f"{name} (zone: {info.get('cloudflare_zone_id', '-')})")
                for name, info in projects.items()
            ]
            project = numbered_select(items, "Select a project to open", allow_cancel=True)
            if not project:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    proj = projects.get(project)
    if not proj:
        console.print(f"[red]Unknown project: {project}[/red]")
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")
        return

    url = proj.get("url")
    if not url:
        console.print(
            f"[yellow]No URL configured for '{project}'.[/yellow]\n"
            f"[dim]Add a 'url' field to the project in ~/.pi-manager/config.json[/dim]"
        )
        return

    console.print(f"[cyan]Opening {url}...[/cyan]")
    sp.run(["open", url])


@cli.command("cache-clear")
@click.argument("project", required=False)
@click.pass_context
def cache_clear_cmd(ctx, project):
    """Clear Cloudflare cache for a project without deploying."""
    from .deploy import purge_cloudflare_cache

    config = ctx.obj["config"]
    projects = config.get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured.[/yellow]")
        return

    if not project:
        try:
            items = [
                (name, f"{name} (zone: {info.get('cloudflare_zone_id', '-')})")
                for name, info in projects.items()
            ]
            project = numbered_select(items, "Select a project", allow_cancel=True)
            if not project:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    proj = projects.get(project)
    if not proj:
        console.print(f"[red]Unknown project: {project}[/red]")
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")
        return

    # Build a config dict with the right Cloudflare token
    pi_name = proj.get("pi")
    if pi_name and pi_name in config.get("pis", {}):
        pi = config["pis"][pi_name]
        token = pi.get("cloudflare_api_token") or config.get("cloudflare_api_token", "")
    else:
        token = config.get("cloudflare_api_token", "")

    cf_config = {"cloudflare_api_token": token}

    console.print(f"[cyan]Purging Cloudflare cache for {project}...[/cyan]")
    if purge_cloudflare_cache(cf_config, proj):
        console.print(f"[green]Cache cleared for {project}.[/green]")


# ---------------------------------------------------------------------------
# Setup / Config
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
def setup(ctx):
    """Re-run the setup wizard to reconfigure PiManager."""
    try:
        config = first_run_setup()
        ctx.obj["config"] = config
    except UserExit:
        console.print("\n[yellow]Setup cancelled.[/yellow]")


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
            is_default = "\u2605" if name == default else ""
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


# ---------------------------------------------------------------------------
# Project management: add-project, list-projects, remove-project
# ---------------------------------------------------------------------------


@cli.command("add-project")
@click.pass_context
def add_project_cmd(ctx):
    """Add a new deploy project interactively."""
    config = ctx.obj["config"]

    try:
        name = prompt_with_exit("Project name")
        if name in config.get("projects", {}):
            console.print(f"[yellow]Project '{name}' already exists. Use a different name.[/yellow]")
            return

        local_path = prompt_with_exit("Local path (folder to sync)")

        # Target Pi
        pi_names = get_pi_names(config)
        if pi_names:
            if len(pi_names) == 1:
                target_pi = pi_names[0]
                console.print(f"Target Pi: [bold]{target_pi}[/bold]")
            else:
                pis = config.get("pis", {})
                items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
                target_pi = numbered_select(items, "Select target Pi", allow_cancel=False)
        else:
            target_pi = ""

        # Remote path: browse directories on Pi or enter manually
        if target_pi and target_pi in config.get("pis", {}):
            pi_cfg = get_pi_config(config, target_pi)
            remote_path = _choose_remote_path(pi_cfg)
        else:
            remote_path = prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")

        cf_zone = prompt_with_exit("Cloudflare zone ID (leave empty to skip)", default="")

        add_project(config, name, local_path, remote_path, pi_name=target_pi, cloudflare_zone_id=cf_zone)
        console.print(f"[green]Project '{name}' added.[/green]")
        console.print(f"Deploy with: [bold]pi deploy {name}[/bold]")

    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")


@cli.command("list-projects")
@click.pass_context
def list_projects_cmd(ctx):
    """List all configured projects."""
    projects = ctx.obj["config"].get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured. Run `pi add-project` to add one.[/yellow]")
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


@cli.command("remove-project")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def remove_project_cmd(ctx, name):
    """Remove a project by name."""
    name = " ".join(name)
    config = ctx.obj["config"]

    if remove_project(config, name):
        console.print(f"[green]Project '{name}' removed.[/green]")
    else:
        console.print(f"[red]Project '{name}' not found.[/red]")
        projects = config.get("projects", {})
        if projects:
            console.print(f"Available: {', '.join(projects.keys())}")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def do_update(config: dict) -> bool:
    """Update PiManager from git repo. Shared between CLI and REPL.

    Returns True if an update was installed, False otherwise.
    """
    import re
    from pathlib import Path

    repo_path = config.get("install_path", "")
    if not repo_path or not Path(repo_path).is_dir():
        console.print("[cyan]PiManager needs to know where the git repo is cloned.[/cyan]")
        try:
            repo_path = prompt_with_exit("Path to PiManager git repo")
        except UserExit:
            console.print("[yellow]Cancelled.[/yellow]")
            return False
        config["install_path"] = str(Path(repo_path).expanduser().resolve())
        save_config(config)
        repo_path = config["install_path"]

    repo = Path(repo_path)
    if not (repo / ".git").is_dir():
        console.print(f"[red]Not a git repository: {repo_path}[/red]")
        return False

    # Read version from pyproject.toml
    def _read_version():
        try:
            text = (repo / "pyproject.toml").read_text()
            m = re.search(r'version\s*=\s*"([^"]+)"', text)
            return m.group(1) if m else "unknown"
        except Exception:
            return "unknown"

    old_version = _read_version()
    console.print(f"Current version: [bold]{old_version}[/bold]")

    # Save HEAD before pull
    old_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()

    # Git pull
    console.print("[cyan]Pulling latest changes...[/cyan]")
    result = subprocess.run(
        ["git", "pull"], cwd=str(repo),
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]git pull failed: {result.stderr.strip()}[/red]")
        return False

    if "Already up to date" in result.stdout:
        console.print("[green]Already up to date.[/green]")
        return False

    # Changelog
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()

    log_result = subprocess.run(
        ["git", "log", "--oneline", f"{old_head}..{new_head}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    if log_result.stdout.strip():
        console.print("\n[bold]Changelog:[/bold]")
        for line in log_result.stdout.strip().split("\n"):
            console.print(f"  {line}")

    # Reinstall via pipx
    console.print("\n[cyan]Reinstalling via pipx...[/cyan]")
    install_result = subprocess.run(
        ["pipx", "install", ".", "--force"],
        cwd=str(repo), capture_output=True, text=True,
    )

    if install_result.returncode != 0:
        console.print(f"[red]Installation failed: {install_result.stderr.strip()}[/red]")
        return False

    new_version = _read_version()
    console.print(f"\n[bold green]Updated: {old_version} → {new_version}[/bold green]")
    return True


@cli.command()
@click.pass_context
def update(ctx):
    """Update PiManager to the latest version."""
    config = ctx.obj["config"]
    do_update(config)


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


@cli.command()
def uninstall():
    """Uninstall PiManager (remove config + pipx package)."""
    if not click.confirm("This will delete your config and uninstall PiManager. Continue?"):
        sys.exit(0)

    # Remove config directory
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]Config removed (~/.pi-manager)[/green]")

    # Uninstall via pipx
    console.print("[cyan]Uninstalling via pipx...[/cyan]")
    subprocess.run(["pipx", "uninstall", "pi-manager"])
    sys.exit(0)
