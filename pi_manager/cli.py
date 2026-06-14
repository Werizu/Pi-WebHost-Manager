import re
import shlex
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
    get_pi_config,
    get_pi_names,
    get_default_pi,
    resolve_pi,
    add_pi,
    remove_pi,
    rename_pi,
    set_tailscale_ip,
    remove_tailscale_ip,
    prompt_with_exit,
    numbered_select,
)
from .ssh import SSHError, print_connection_label

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


def _hostname_label(name: str) -> str:
    """Turn a friendly Pi name into a valid hostname label (letters/digits/hyphen)."""
    label = re.sub(r"[^A-Za-z0-9-]", "-", name.strip())
    label = re.sub(r"-+", "-", label).strip("-")
    return label or "raspberrypi"


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
    elif ctx.invoked_subcommand == "update":
        # update works without full setup
        ctx.obj["config"] = load_config() or {}
    else:
        ctx.obj["config"] = ensure_config()


# ---------------------------------------------------------------------------
# Status & check
# ---------------------------------------------------------------------------


@cli.command()
@pi_option
@click.pass_context
def status(ctx, pi_name):
    """Show system status and auto-detected services for all Pis (or --pi <name>)."""
    from .monitor import show_status, show_services
    from .services import detect_services

    config = ctx.obj["config"]
    names = [pi_name] if pi_name else get_pi_names(config)
    changed = False
    for name in names:
        pi_cfg = get_pi_config(config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)
            show_status(pi_cfg)
            svcs = detect_services(pi_cfg)
            config["pis"][name]["services"] = svcs
            changed = True
            show_services(pi_cfg, services=svcs)
        except SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")
    if changed:
        save_config(config)


@cli.command()
@click.pass_context
def check(ctx):
    """Full sweep of all Pis: health, services, Tailscale/LAN IP, hostname, OS updates."""
    from .ssh import run_remote
    from .monitor import show_status
    from .services import detect_services, detect_lan_ip, check_updates

    config = ctx.obj["config"]
    pi_names = get_pi_names(config)
    if not pi_names:
        console.print("[yellow]No Pis configured.[/yellow]")
        return

    renames = []
    needs_upgrade, needs_reboot, offline = [], [], []
    for name in list(pi_names):
        pi_cfg = get_pi_config(config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)

            show_status(pi_cfg)

            svcs = detect_services(pi_cfg)
            config["pis"][name]["services"] = svcs
            console.print(f"[green]Services:[/green] {', '.join(svcs) or '-'}")

            ts_out, _, _ = run_remote(pi_cfg, "tailscale ip -4 2>/dev/null")
            ts_ip = ts_out.strip().splitlines()[0].strip() if ts_out.strip() else ""
            if ts_ip and ts_ip != config["pis"][name].get("tailscale_host"):
                config["pis"][name]["tailscale_host"] = ts_ip
                console.print(f"[cyan]Tailscale IP → {ts_ip} (updated)[/cyan]")
            elif ts_ip:
                console.print(f"[green]Tailscale IP:[/green] {ts_ip}")

            lan_ip = detect_lan_ip(pi_cfg)
            if lan_ip and lan_ip != config["pis"][name].get("host"):
                console.print(f"[cyan]LAN IP → {lan_ip} (was {config['pis'][name].get('host')}, updated)[/cyan]")
                config["pis"][name]["host"] = lan_ip

            upd = check_updates(pi_cfg)
            if upd["total"]:
                sec = f", davon {upd['security']} Sicherheits-Updates" if upd["security"] else ""
                console.print(f"[yellow]Updates verfügbar: {upd['total']} Pakete{sec}[/yellow]")
                needs_upgrade.append(name)
            else:
                console.print("[green]System aktuell — keine Updates.[/green]")
            if upd["reboot_required"]:
                console.print("[yellow]Neustart erforderlich (reboot-required).[/yellow]")
                needs_reboot.append(name)

            out, _, code = run_remote(pi_cfg, "hostname")
            real = out.strip()
            if real and real != name:
                if real in config["pis"]:
                    console.print(f"[yellow]Hostname '{real}' already used as a name — keeping '{name}'.[/yellow]")
                else:
                    renames.append((name, real))
                    console.print(f"[cyan]Hostname '{real}' — adopting as tool name.[/cyan]")
        except SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")
            offline.append(name)

    for old, new in renames:
        rename_pi(config, old, new)
    save_config(config)

    console.print("\n[bold green]Check abgeschlossen.[/bold green]")
    if offline:
        console.print(f"[red]Nicht erreichbar:[/red] {', '.join(offline)}")
    if needs_upgrade:
        console.print(f"[yellow]Updates ausstehend auf:[/yellow] {', '.join(needs_upgrade)} "
                      f"[dim]→ mit 'upgrade' einspielen[/dim]")
    if needs_reboot:
        console.print(f"[yellow]Neustart nötig auf:[/yellow] {', '.join(needs_reboot)} "
                      f"[dim]→ mit 'reboot'[/dim]")
    if not (offline or needs_upgrade or needs_reboot):
        console.print("[green]Alles aktuell und sauber.[/green]")


# ---------------------------------------------------------------------------
# Service control: restart / stop / start
# ---------------------------------------------------------------------------


def _services_for(config: dict, pi_cfg: dict) -> list:
    """Known services for a Pi, falling back to live auto-detection."""
    from .services import detect_services
    return pi_cfg.get("services") or detect_services(pi_cfg)


@cli.command()
@click.argument("service", required=False, default=None)
@pi_option
@click.pass_context
def restart(ctx, service, pi_name):
    """Restart a service (or 'all'). Without args, pick from a numbered list."""
    from .services import restart_service, restart_all

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)

    if not service:
        services = _services_for(config, pi_cfg)
        if not services:
            console.print(f"[yellow]No services found on {pi_name}.[/yellow]")
            return
        try:
            items = [(s, s) for s in services]
            items.append(("all", "all (restart all services)"))
            service = numbered_select(items, f"Restart service on {pi_name}", allow_cancel=True)
            if not service:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    print_connection_label(pi_cfg)
    if service == "all":
        restart_all(pi_cfg)
    else:
        restart_service(pi_cfg, service)


@cli.command()
@click.argument("service", required=False, default=None)
@pi_option
@click.pass_context
def stop(ctx, service, pi_name):
    """Stop a service on a Pi. Without args, pick from a numbered list."""
    from .services import stop_service

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)

    if not service:
        services = _services_for(config, pi_cfg)
        if not services:
            console.print(f"[yellow]No services found on {pi_name}.[/yellow]")
            return
        try:
            items = [(s, s) for s in services]
            service = numbered_select(items, f"Stop service on {pi_name}", allow_cancel=True)
            if not service:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    print_connection_label(pi_cfg)
    stop_service(pi_cfg, service)


@cli.command()
@click.argument("service", required=False, default=None)
@pi_option
@click.pass_context
def start(ctx, service, pi_name):
    """Start a service on a Pi. Without args, pick from a numbered list."""
    from .services import start_service

    config = ctx.obj["config"]
    pi_name = resolve_pi(config, pi_name)
    pi_cfg = get_pi_config(config, pi_name)

    if not service:
        services = _services_for(config, pi_cfg)
        if not services:
            console.print(f"[yellow]No services found on {pi_name}.[/yellow]")
            return
        try:
            items = [(s, s) for s in services]
            service = numbered_select(items, f"Start service on {pi_name}", allow_cancel=True)
            if not service:
                return
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return

    print_connection_label(pi_cfg)
    start_service(pi_cfg, service)


# ---------------------------------------------------------------------------
# SSH / power / upgrade
# ---------------------------------------------------------------------------


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
    print_connection_label(pi_cfg)
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
    print_connection_label(pi_cfg)
    reboot_pi(pi_cfg)


@cli.command()
@pi_option
@click.pass_context
def upgrade(ctx, pi_name):
    """Install OS/package updates on a Pi, then restart its services."""
    from .services import upgrade_pi, detect_services, restart_service

    config = ctx.obj["config"]
    names = [pi_name] if pi_name else get_pi_names(config)
    for name in names:
        pi_cfg = get_pi_config(config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)
            if upgrade_pi(pi_cfg):
                svcs = detect_services(pi_cfg)
                config["pis"][name]["services"] = svcs
                save_config(config)
                if svcs:
                    console.print(f"[cyan]Restarting services: {', '.join(svcs)}[/cyan]")
                    for s in svcs:
                        restart_service(pi_cfg, s)
                console.print(f"[bold green]{name} fully updated.[/bold green]")
        except SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")


# ---------------------------------------------------------------------------
# Pi management: list / add / remove / rename / edit / use
# ---------------------------------------------------------------------------


@cli.command("list")
@click.pass_context
def list_cmd(ctx):
    """List all configured Pis."""
    config = ctx.obj["config"]
    pis = config.get("pis", {})
    default = get_default_pi(config)

    if not pis:
        console.print("[yellow]No Pis configured. Run `pi setup` or `pi add`.[/yellow]")
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


@cli.command("add")
@click.pass_context
def add_cmd(ctx):
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
        tailscale_host=pi_dict.get("tailscale_host", ""),
    )
    console.print(f"[green]Pi '{pi_name}' added.[/green]")

    pi_cfg = get_pi_config(config, pi_name)
    console.print("Testing SSH connection...")
    if test_connection(pi_cfg):
        console.print(click.style("Connected successfully!", fg="green"))
    else:
        console.print(click.style("Could not connect.", fg="yellow"))
        console.print("Check that your Pi is powered on and the IP/key are correct.")


@cli.command("remove")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def remove_cmd(ctx, name):
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


@cli.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@click.pass_context
def rename_cmd(ctx, old_name, new_name):
    """Rename a Pi: changes the real hostname and adopts the name in the tool."""
    from .ssh import run_remote

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

    host_label = _hostname_label(new_name)
    pi_cfg = get_pi_config(config, old_name)
    try:
        print_connection_label(pi_cfg)
        console.print(f"[cyan]Setting hostname to '{host_label}'...[/cyan]")
        _, stderr, code = run_remote(pi_cfg, f"sudo hostnamectl set-hostname {shlex.quote(host_label)}")
        if code != 0:
            console.print(f"[red]Failed to set hostname: {stderr}[/red]")
            console.print("[yellow]Renaming in the tool only.[/yellow]")
        else:
            run_remote(pi_cfg, f"sudo sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{host_label}/' /etc/hosts")
            console.print(f"[green]Hostname set to '{host_label}'.[/green]")
    except SSHError as e:
        console.print(f"[red]Offline — {e}. Renaming in the tool only.[/red]")

    rename_pi(config, old_name, new_name)
    console.print(f"[green]'{old_name}' → '{new_name}'.[/green]")


@cli.command("edit")
@click.pass_context
def edit_cmd(ctx):
    """Edit an existing Pi's host, user, SSH key path, or Tailscale IP."""
    config = ctx.obj["config"]
    pi_names = get_pi_names(config)

    if not pi_names:
        console.print("[yellow]No Pis configured. Run `pi add` first.[/yellow]")
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
        current_ts = pi.get("tailscale_host", "")
        ts_label = current_ts or "none"
        new_ts = prompt_with_exit(
            f"Tailscale IP [{ts_label}] (type 'none' to remove)",
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
        if new_ts:
            if new_ts.lower() == "none":
                pi.pop("tailscale_host", None)
            else:
                pi["tailscale_host"] = new_ts
            changed = True

        if changed:
            save_config(config)
            console.print(f"[green]Pi '{selected}' updated.[/green]")
        else:
            console.print("[dim]No changes made.[/dim]")

    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")


@cli.command("use")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def use_cmd(ctx, name):
    """Set the default Pi (persists)."""
    target = " ".join(name)
    config = ctx.obj["config"]
    pi_names = get_pi_names(config)

    # Allow numeric selection
    try:
        idx = int(target)
        if 1 <= idx <= len(pi_names):
            target = pi_names[idx - 1]
        else:
            console.print(f"[red]Invalid number: {idx}[/red]")
            return
    except ValueError:
        pass

    if target not in pi_names:
        console.print(f"[red]Unknown Pi: '{target}'[/red]")
        for i, n in enumerate(pi_names, 1):
            console.print(f"  {i}) {n} ({config['pis'][n]['host']})")
        return

    config["default_pi"] = target
    save_config(config)
    info = config["pis"][target]
    console.print(f"[green]Default Pi set to [bold]{target}[/bold] ({info.get('user', 'pi')}@{info['host']})[/green]")


# ---------------------------------------------------------------------------
# Setup
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


# ---------------------------------------------------------------------------
# Tailscale management
# ---------------------------------------------------------------------------


@cli.group()
def tailscale():
    """Manage Tailscale VPN IPs for Pis."""


def _resolve_pi_name_or_number(config: dict, raw: str) -> str | None:
    """Resolve a Pi name or numeric index. Returns name or None."""
    pi_names = get_pi_names(config)
    try:
        idx = int(raw)
        if 1 <= idx <= len(pi_names):
            return pi_names[idx - 1]
        console.print(f"[red]Invalid number: {idx}[/red]")
        for i, n in enumerate(pi_names, 1):
            console.print(f"  {i}) {n} ({config['pis'][n]['host']})")
        return None
    except ValueError:
        if raw in config.get("pis", {}):
            return raw
        console.print(f"[red]Pi '{raw}' not found.[/red]")
        for i, n in enumerate(pi_names, 1):
            console.print(f"  {i}) {n} ({config['pis'][n]['host']})")
        return None


@tailscale.command("set")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def tailscale_set(ctx, name):
    """Set Tailscale IP for a Pi: tailscale set <pi-name|number> <ip>"""
    parts = list(name)
    if len(parts) < 2:
        console.print("[yellow]Usage: pi tailscale set <pi-name|number> <ip>[/yellow]")
        config = ctx.obj["config"]
        for i, n in enumerate(get_pi_names(config), 1):
            console.print(f"  {i}) {n} ({config['pis'][n]['host']})")
        return

    ip = parts[-1]
    raw_name = " ".join(parts[:-1])
    config = ctx.obj["config"]

    pi_name = _resolve_pi_name_or_number(config, raw_name)
    if not pi_name:
        return

    set_tailscale_ip(config, pi_name, ip)
    console.print(f"[green]Tailscale IP for '{pi_name}' set to {ip}.[/green]")


@tailscale.command("remove")
@click.argument("name", nargs=-1, required=True)
@click.pass_context
def tailscale_remove(ctx, name):
    """Remove Tailscale IP from a Pi."""
    raw_name = " ".join(name)
    config = ctx.obj["config"]

    pi_name = _resolve_pi_name_or_number(config, raw_name)
    if not pi_name:
        return

    if remove_tailscale_ip(config, pi_name):
        console.print(f"[green]Tailscale IP removed from '{pi_name}'.[/green]")
    else:
        console.print(f"[yellow]No Tailscale IP configured for '{pi_name}'.[/yellow]")


@tailscale.command("list")
@click.pass_context
def tailscale_list(ctx):
    """Show Tailscale IPs for all Pis."""
    from .ssh import is_on_home_network

    config = ctx.obj["config"]
    pis = config.get("pis", {})

    if not pis:
        console.print("[yellow]No Pis configured.[/yellow]")
        return

    at_home = is_on_home_network()

    table = Table(title="Tailscale Configuration", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("LAN IP")
    table.add_column("Tailscale IP")
    table.add_column("Connection")

    for name, info in pis.items():
        lan_ip = info.get("host", "")
        ts_ip = info.get("tailscale_host", "")
        if at_home:
            mode = "[green]LAN[/green]"
        elif ts_ip:
            mode = "[blue]Tailscale[/blue]"
        else:
            mode = "[red]Unavailable[/red]"
        table.add_row(name, lan_ip, ts_ip or "-", mode)

    console.print(table)


# ---------------------------------------------------------------------------
# Update / uninstall
# ---------------------------------------------------------------------------


def do_update(config: dict) -> bool:
    """Update PiManager from git repo. Shared between CLI and REPL.

    Returns True if an update was installed, False otherwise.
    """
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

    def _read_version():
        try:
            text = (repo / "pyproject.toml").read_text()
            m = re.search(r'version\s*=\s*"([^"]+)"', text)
            return m.group(1) if m else "unknown"
        except Exception:
            return "unknown"

    old_version = _read_version()
    console.print(f"Current version: [bold]{old_version}[/bold]")

    old_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()

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


@cli.command()
def uninstall():
    """Uninstall PiManager (remove config + pipx package)."""
    if not click.confirm("This will delete your config and uninstall PiManager. Continue?"):
        sys.exit(0)

    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]Config removed (~/.pi-manager)[/green]")

    console.print("[cyan]Uninstalling via pipx...[/cyan]")
    subprocess.run(["pipx", "uninstall", "pi-manager"])
    sys.exit(0)
