import shlex
import shutil
import subprocess

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import CONFIG_DIR, load_config, save_config, first_run_setup, add_project, remove_project

console = Console()

COMMANDS = [
    "status", "services", "logs", "restart", "ssh", "deploy",
    "config", "project", "setup", "shutdown", "reboot",
    "uninstall", "help", "exit", "quit",
]

HELP_TABLE = [
    ("status", "Show Pi system status (CPU, RAM, disk, temp, uptime)"),
    ("services", "Show status of monitored services"),
    ("logs", "Show Apache error logs"),
    ("logs --live", "Stream logs in real-time (Ctrl+C to stop)"),
    ("restart <service>", "Restart a service on the Pi"),
    ("restart all", "Restart all monitored services"),
    ("ssh", "Open SSH session in a new Terminal window"),
    ("deploy <name>", "Deploy a project (rsync + cache purge)"),
    ("config", "Show current configuration"),
    ("project add", "Add a new deploy project"),
    ("project list", "List configured projects"),
    ("project remove <name>", "Remove a project"),
    ("setup", "Re-run the setup wizard"),
    ("shutdown", "Shut down the Pi"),
    ("reboot", "Reboot the Pi"),
    ("uninstall", "Uninstall PiManager"),
    ("help", "Show this help"),
    ("exit / quit", "Exit PiManager"),
]


def _print_header(config: dict) -> None:
    host = config.get("pi_host", "not configured")
    user = config.get("pi_user", "?")
    header = f"[bold white]PiManager[/bold white]  [dim]v0.1.0[/dim]\n"
    header += f"[dim]Connected to[/dim] [cyan]{user}@{host}[/cyan]"
    console.print(Panel(header, border_style="blue", expand=False))
    console.print("[dim]Type [bold]help[/bold] for commands, [bold]exit[/bold] to quit.[/dim]\n")


def _print_help() -> None:
    table = Table(show_header=True, header_style="bold cyan", show_edge=False, pad_edge=False)
    table.add_column("Command", style="bold white", min_width=24)
    table.add_column("Description")
    for cmd, desc in HELP_TABLE:
        table.add_row(cmd, desc)
    console.print(table)


def _dispatch(args: list[str], config: dict) -> dict:
    """Dispatch a parsed command. Returns potentially updated config."""
    from .ssh import open_ssh_session, SSHError

    cmd = args[0]
    rest = args[1:]

    try:
        if cmd == "help":
            _print_help()

        elif cmd == "status":
            from .monitor import show_status
            show_status(config)

        elif cmd == "services":
            from .monitor import show_services
            show_services(config)

        elif cmd == "logs":
            from .monitor import show_logs
            live = "--live" in rest
            lines = 30
            for i, a in enumerate(rest):
                if a in ("-n", "--lines") and i + 1 < len(rest):
                    try:
                        lines = int(rest[i + 1])
                    except ValueError:
                        pass
            show_logs(config, live=live, lines=lines)

        elif cmd == "restart":
            from .services import restart_service, restart_all
            if not rest:
                console.print("[yellow]Usage: restart <service|all>[/yellow]")
            elif rest[0] == "all":
                restart_all(config)
            else:
                restart_service(config, rest[0])

        elif cmd == "ssh":
            open_ssh_session(config)

        elif cmd == "deploy":
            from .deploy import deploy
            if not rest:
                console.print("[yellow]Usage: deploy <project-name>[/yellow]")
            else:
                deploy(config, rest[0])

        elif cmd == "config":
            _show_config(config)

        elif cmd == "project":
            config = _handle_project(rest, config)

        elif cmd == "setup":
            config = first_run_setup()

        elif cmd == "shutdown":
            from .services import shutdown_pi
            shutdown_pi(config)

        elif cmd == "reboot":
            from .services import reboot_pi
            reboot_pi(config)

        elif cmd == "uninstall":
            _handle_uninstall()

        else:
            console.print(f"[red]Unknown command:[/red] {cmd}")
            console.print("[dim]Type [bold]help[/bold] to see available commands.[/dim]")

    except SSHError as e:
        console.print(f"[red]{e}[/red]")

    return config


def _show_config(config: dict) -> None:
    table = Table(title="PiManager Config", show_header=True, header_style="bold cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row("Pi host", config.get("pi_host", ""))
    table.add_row("Pi user", config.get("pi_user", ""))
    table.add_row("SSH key", config.get("ssh_key_path", ""))

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


def _handle_project(rest: list[str], config: dict) -> dict:
    import click

    if not rest:
        console.print("[yellow]Usage: project add|list|remove <name>[/yellow]")
        return config

    sub = rest[0]

    if sub == "list":
        projects = config.get("projects", {})
        if not projects:
            console.print("[yellow]No projects configured. Use 'project add' to add one.[/yellow]")
            return config
        table = Table(title="Projects", show_header=True, header_style="bold cyan")
        table.add_column("Name", style="bold")
        table.add_column("Local path")
        table.add_column("Remote path")
        table.add_column("CF zone")
        for name, info in projects.items():
            zone = info.get("cloudflare_zone_id", "")
            table.add_row(name, info.get("local_path", ""), info.get("remote_path", ""), zone or "-")
        console.print(table)

    elif sub == "add":
        name = click.prompt("Project name")
        if name in config.get("projects", {}):
            console.print(f"[yellow]Project '{name}' already exists.[/yellow]")
            return config
        local_path = click.prompt("Local path (folder to sync)")
        remote_path = click.prompt("Remote path on Pi (e.g. /var/www/my-site/)")
        cf_zone = click.prompt("Cloudflare zone ID (leave empty to skip)", default="")
        add_project(config, name, local_path, remote_path, cloudflare_zone_id=cf_zone)
        console.print(f"[green]Project '{name}' added.[/green]")

    elif sub == "remove":
        if len(rest) < 2:
            console.print("[yellow]Usage: project remove <name>[/yellow]")
            return config
        name = rest[1]
        if remove_project(config, name):
            console.print(f"[green]Project '{name}' removed.[/green]")
        else:
            console.print(f"[red]Project '{name}' not found.[/red]")
            projects = config.get("projects", {})
            if projects:
                console.print(f"Available: {', '.join(projects.keys())}")
    else:
        console.print(f"[yellow]Unknown subcommand: project {sub}[/yellow]")
        console.print("[dim]Usage: project add|list|remove <name>[/dim]")

    return config


def _handle_uninstall() -> None:
    import click

    if not click.confirm("This will delete your config and uninstall PiManager. Continue?"):
        return

    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]Config removed (~/.pi-manager)[/green]")

    console.print("[cyan]Uninstalling via pipx...[/cyan]")
    subprocess.run(["pipx", "uninstall", "pi-manager"])


def start_repl() -> None:
    """Start the interactive PiManager REPL."""
    config = load_config()
    if not config:
        config = first_run_setup()

    _print_header(config)

    history_file = CONFIG_DIR / "history"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    completer = WordCompleter(COMMANDS, ignore_case=True)
    session: PromptSession = PromptSession(
        history=FileHistory(str(history_file)),
        completer=completer,
    )

    while True:
        try:
            text = session.prompt(HTML("<b><skyblue>pi</skyblue></b> <b>&gt;</b> ")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not text:
            continue

        try:
            args = shlex.split(text)
        except ValueError as e:
            console.print(f"[red]Parse error: {e}[/red]")
            continue

        if args[0] in ("exit", "quit"):
            console.print("[dim]Goodbye![/dim]")
            break

        config = _dispatch(args, config)
        console.print()
