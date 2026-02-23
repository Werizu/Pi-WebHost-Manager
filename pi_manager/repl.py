import asyncio
import os
import shlex
import shutil
import subprocess
from io import StringIO

from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML, ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import TextArea
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMANDS = [
    "status", "services", "logs", "restart", "ssh", "deploy",
    "config", "project", "setup", "shutdown", "reboot",
    "uninstall", "help", "clear", "exit", "quit",
    "list-pis", "add-pi", "use",
]

HELP_TABLE = [
    ("status", "Show system status (all Pis, or --pi <name>)"),
    ("services", "Show service status (all Pis, or --pi <name>)"),
    ("logs", "Show Apache error logs"),
    ("logs --live", "Stream logs in real-time (CLI only)"),
    ("restart <service>", "Restart a service (--pi <name>)"),
    ("restart all", "Restart all monitored services"),
    ("ssh", "Open SSH in a new Terminal window"),
    ("deploy <name>", "Deploy a project (rsync + cache purge)"),
    ("", ""),
    ("list-pis", "List all configured Pis"),
    ("add-pi", "Add a new Pi interactively"),
    ("use <pi-name>", "Set active Pi for this session"),
    ("", ""),
    ("config", "Show current configuration"),
    ("project add", "Add a new deploy project"),
    ("project list", "List configured projects"),
    ("project remove <name>", "Remove a project"),
    ("setup", "Re-run the setup wizard"),
    ("shutdown", "Shut down the active Pi"),
    ("reboot", "Reboot the active Pi"),
    ("uninstall", "Uninstall PiManager"),
    ("clear", "Clear the output"),
    ("help", "Show this help"),
    ("exit / quit", "Exit PiManager"),
]

# Commands that need direct terminal access (interactive prompts)
INTERACTIVE_COMMANDS = {"setup", "shutdown", "reboot", "uninstall", "add-pi"}

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_app: Application | None = None
_config: dict = {}
_output_text: str = ""
_busy: bool = False
_active_pi: str | None = None  # Session-level active Pi override

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _make_console() -> Console:
    """Create a Console that captures output to a StringIO buffer."""
    return Console(
        file=StringIO(),
        force_terminal=True,
        width=max(_term_width() - 4, 40),
    )


def _patch_consoles(cap: Console):
    """Replace console objects in command modules. Returns a restore function."""
    import pi_manager.monitor as _mon
    import pi_manager.services as _svc
    import pi_manager.deploy as _dep
    import pi_manager.ssh as _ssh

    modules = [_mon, _svc, _dep, _ssh]
    saved = {}
    for m in modules:
        if hasattr(m, "console"):
            saved[m] = m.console
            m.console = cap

    def restore():
        for m, c in saved.items():
            m.console = c

    return restore


def _parse_pi_option(args: list[str]) -> tuple[list[str], str | None]:
    """Extract --pi <name> from args. Returns (remaining_args, pi_name)."""
    pi_name = None
    remaining = []
    i = 0
    while i < len(args):
        if args[i] == "--pi" and i + 1 < len(args):
            pi_name = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return remaining, pi_name


def _resolve_effective_pi(pi_name: str | None) -> str:
    """Resolve which Pi to use: explicit --pi > _active_pi > default_pi."""
    if pi_name:
        return resolve_pi(_config, pi_name)
    if _active_pi:
        return resolve_pi(_config, _active_pi)
    return resolve_pi(_config, None)


# ---------------------------------------------------------------------------
# UI rendering
# ---------------------------------------------------------------------------


def _get_header() -> HTML:
    pis = _config.get("pis", {})
    pi_count = len(pis)

    if pi_count == 0:
        return HTML(
            "\n"
            '  <b>PiManager</b> <style fg="ansibrightblack">v0.1.1</style>\n'
            '  <style fg="ansibrightblack">No Pis configured — run </style><b>setup</b>'
            '<style fg="ansibrightblack"> or </style><b>add-pi</b>\n'
        )

    # Build Pi list for header
    default = get_default_pi(_config)
    active = _active_pi or default

    if pi_count == 1:
        name = next(iter(pis))
        info = pis[name]
        return HTML(
            "\n"
            '  <b>PiManager</b> <style fg="ansibrightblack">v0.1.1</style>\n'
            f'  <ansicyan>{name}</ansicyan>'
            f' <style fg="ansibrightblack">({info.get("user", "pi")}@{info["host"]})</style>\n'
        )

    # Multiple Pis: show all, mark active
    lines = [
        "\n",
        '  <b>PiManager</b> <style fg="ansibrightblack">v0.1.1</style>\n',
        f'  <style fg="ansibrightblack">{pi_count} Pis:</style> ',
    ]
    parts = []
    for name, info in pis.items():
        host = info["host"]
        if name == active:
            parts.append(f'<b><ansicyan>{name}</ansicyan></b><style fg="ansibrightblack">({host})</style>')
        else:
            parts.append(f'<style fg="ansibrightblack">{name}({host})</style>')
    lines.append(" · ".join(parts))
    lines.append("\n")

    return HTML("".join(lines))


def _get_hint() -> HTML:
    return HTML(
        '  <style fg="ansibrightblack">Type </style>'
        "<b>help</b>"
        '<style fg="ansibrightblack"> for commands, </style>'
        "<b>exit</b>"
        '<style fg="ansibrightblack"> to quit</style>'
    )


def _get_output():
    if not _output_text:
        return HTML('  <style fg="ansibrightblack">Type a command to get started...</style>')
    try:
        return ANSI(_output_text)
    except Exception:
        return FormattedText([("", _output_text)])


# ---------------------------------------------------------------------------
# Command dispatch (captured — output goes to the TUI output area)
# ---------------------------------------------------------------------------


def _dispatch_captured(args: list[str]) -> str:
    """Dispatch a command, capture all rich output, return as ANSI string."""
    global _config, _active_pi

    # Parse --pi from args
    args, pi_name = _parse_pi_option(args)

    cap = _make_console()
    restore = _patch_consoles(cap)

    try:
        cmd = args[0]
        rest = args[1:]

        from .ssh import SSHError

        try:
            if cmd == "help":
                table = Table(show_header=True, header_style="bold cyan", show_edge=False, pad_edge=False)
                table.add_column("Command", style="bold white", min_width=28)
                table.add_column("Description")
                for c, desc in HELP_TABLE:
                    table.add_row(c, desc)
                cap.print(table)

            elif cmd == "status":
                from .monitor import show_status
                if pi_name:
                    pi_cfg = get_pi_config(_config, pi_name)
                    cap.print(f"\n[bold cyan]--- {pi_name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    show_status(pi_cfg)
                elif _active_pi:
                    pi_cfg = get_pi_config(_config, _active_pi)
                    cap.print(f"\n[bold cyan]--- {_active_pi} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    show_status(pi_cfg)
                else:
                    # All Pis
                    for name in get_pi_names(_config):
                        pi_cfg = get_pi_config(_config, name)
                        cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                        show_status(pi_cfg)

            elif cmd == "services":
                from .monitor import show_services
                if pi_name:
                    pi_cfg = get_pi_config(_config, pi_name)
                    cap.print(f"\n[bold cyan]--- {pi_name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    show_services(pi_cfg)
                elif _active_pi:
                    pi_cfg = get_pi_config(_config, _active_pi)
                    cap.print(f"\n[bold cyan]--- {_active_pi} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    show_services(pi_cfg)
                else:
                    for name in get_pi_names(_config):
                        pi_cfg = get_pi_config(_config, name)
                        cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                        show_services(pi_cfg)

            elif cmd == "logs":
                from .monitor import show_logs
                live = "--live" in rest
                if live:
                    cap.print("[yellow]Live streaming is not supported in the REPL.[/yellow]")
                    cap.print("[dim]Use [bold]pi logs --live[/bold] from your terminal instead.[/dim]")
                else:
                    lines = 30
                    for i, a in enumerate(rest):
                        if a in ("-n", "--lines") and i + 1 < len(rest):
                            try:
                                lines = int(rest[i + 1])
                            except ValueError:
                                pass
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    show_logs(pi_cfg, live=False, lines=lines)

            elif cmd == "restart":
                from .services import restart_service, restart_all
                if not rest:
                    cap.print("[yellow]Usage: restart <service|all>[/yellow]")
                else:
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    if rest[0] == "all":
                        restart_all(pi_cfg)
                    else:
                        restart_service(pi_cfg, rest[0])

            elif cmd == "ssh":
                from .ssh import open_ssh_session
                effective_pi = _resolve_effective_pi(pi_name)
                pi_cfg = get_pi_config(_config, effective_pi)
                open_ssh_session(pi_cfg)

            elif cmd == "deploy":
                from .deploy import deploy
                if not rest:
                    cap.print("[yellow]Usage: deploy <project-name>[/yellow]")
                else:
                    project_name = rest[0]
                    project = _config.get("projects", {}).get(project_name)
                    if not project:
                        cap.print(f"[red]Unknown project: {project_name}[/red]")
                        projects = _config.get("projects", {})
                        if projects:
                            cap.print(f"Available: {', '.join(projects.keys())}")
                    else:
                        # Resolve Pi: explicit --pi > project config > active pi > default
                        effective_name = pi_name or project.get("pi")
                        if not effective_name and _active_pi:
                            effective_name = _active_pi
                        effective_name = resolve_pi(_config, effective_name)
                        pi_cfg = get_pi_config(_config, effective_name)
                        pi_cfg["projects"] = _config.get("projects", {})
                        deploy(pi_cfg, project_name, pi_name=effective_name)

            elif cmd == "config":
                _show_config(cap)

            elif cmd == "project":
                if not rest:
                    cap.print("[yellow]Usage: project add|list|remove <name>[/yellow]")
                elif rest[0] == "list":
                    _project_list(cap)
                elif rest[0] == "remove":
                    if len(rest) < 2:
                        cap.print("[yellow]Usage: project remove <name>[/yellow]")
                    else:
                        _project_remove(rest[1], cap)
                elif rest[0] == "add":
                    # Interactive — handled separately, should not reach here
                    cap.print("[yellow]Opening interactive prompt...[/yellow]")
                else:
                    cap.print(f"[yellow]Unknown subcommand: project {rest[0]}[/yellow]")

            elif cmd == "list-pis":
                _list_pis(cap)

            elif cmd == "use":
                if not rest:
                    cap.print("[yellow]Usage: use <pi-name>[/yellow]")
                    cap.print(f"[dim]Available: {', '.join(get_pi_names(_config))}[/dim]")
                else:
                    target = rest[0]
                    pi_names = get_pi_names(_config)
                    if target not in pi_names:
                        cap.print(f"[red]Unknown Pi: '{target}'[/red]")
                        cap.print(f"[dim]Available: {', '.join(pi_names)}[/dim]")
                    else:
                        _active_pi = target
                        pi_info = _config["pis"][target]
                        cap.print(
                            f"[green]Active Pi set to [bold]{target}[/bold] "
                            f"({pi_info.get('user', 'pi')}@{pi_info['host']})[/green]"
                        )

            else:
                cap.print(f"[red]Unknown command:[/red] {cmd}")
                cap.print("[dim]Type [bold]help[/bold] to see available commands.[/dim]")

        except SSHError as e:
            cap.print(f"[red]{e}[/red]")
        except Exception as e:
            cap.print(f"[red]Error: {e}[/red]")
    finally:
        restore()

    cap.file.seek(0)
    return cap.file.read()


def _show_config(cap: Console) -> None:
    # Global settings
    table = Table(title="PiManager Config", show_header=True, header_style="bold cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row("Default Pi", _config.get("default_pi", "(not set)"))
    if _active_pi:
        table.add_row("Active Pi (session)", _active_pi)

    cf_token = _config.get("cloudflare_api_token", "")
    if cf_token:
        masked = cf_token[:4] + "..." + cf_token[-4:] if len(cf_token) > 8 else "****"
    else:
        masked = "(not set)"
    table.add_row("Cloudflare token", masked)

    projects = _config.get("projects", {})
    table.add_row("Projects", ", ".join(projects.keys()) if projects else "(none)")
    cap.print(table)

    # Per-Pi table
    pis = _config.get("pis", {})
    default = get_default_pi(_config)
    if pis:
        pi_table = Table(title="Pis", show_header=True, header_style="bold cyan")
        pi_table.add_column("Name", style="bold")
        pi_table.add_column("Host")
        pi_table.add_column("User")
        pi_table.add_column("Services")
        pi_table.add_column("Default")

        for name, info in pis.items():
            is_default = "★" if name == default else ""
            svcs = ", ".join(info.get("services", [])) or "-"
            pi_table.add_row(name, info.get("host", ""), info.get("user", "pi"), svcs, is_default)
        cap.print(pi_table)


def _list_pis(cap: Console) -> None:
    pis = _config.get("pis", {})
    default = get_default_pi(_config)

    if not pis:
        cap.print("[yellow]No Pis configured. Use 'add-pi' or 'setup' to add one.[/yellow]")
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

    cap.print(table)


def _project_list(cap: Console) -> None:
    projects = _config.get("projects", {})
    if not projects:
        cap.print("[yellow]No projects configured. Use 'project add' to add one.[/yellow]")
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
    cap.print(table)


def _project_remove(name: str, cap: Console) -> None:
    if remove_project(_config, name):
        cap.print(f"[green]Project '{name}' removed.[/green]")
    else:
        cap.print(f"[red]Project '{name}' not found.[/red]")
        projects = _config.get("projects", {})
        if projects:
            cap.print(f"Available: {', '.join(projects.keys())}")


# ---------------------------------------------------------------------------
# Interactive command handlers (run in real terminal, not captured)
# ---------------------------------------------------------------------------


def _run_setup() -> None:
    global _config
    _config = first_run_setup()


def _run_project_add() -> None:
    import click
    console = Console()
    name = click.prompt("Project name")
    if name in _config.get("projects", {}):
        console.print(f"[yellow]Project '{name}' already exists.[/yellow]")
        return
    local_path = click.prompt("Local path (folder to sync)")
    remote_path = click.prompt("Remote path on Pi (e.g. /var/www/my-site/)")

    # Target Pi
    pi_names = get_pi_names(_config)
    if len(pi_names) == 1:
        target_pi = pi_names[0]
        console.print(f"Target Pi: {target_pi}")
    elif pi_names:
        target_pi = click.prompt(
            f"Target Pi ({', '.join(pi_names)})",
            default=_active_pi or get_default_pi(_config),
        )
    else:
        target_pi = ""

    cf_zone = click.prompt("Cloudflare zone ID (leave empty to skip)", default="")
    add_project(_config, name, local_path, remote_path, pi_name=target_pi, cloudflare_zone_id=cf_zone)
    console.print(f"[green]Project '{name}' added.[/green]")


def _run_add_pi() -> None:
    """Interactive wizard for adding a Pi in the REPL."""
    import click
    from .config import _setup_single_pi, test_connection, save_config

    global _config
    console = Console()

    pi_name, pi_dict = _setup_single_pi()

    if pi_name in _config.get("pis", {}):
        console.print(f"[yellow]Pi '{pi_name}' already exists. Use a different name.[/yellow]")
        return

    add_pi(
        _config,
        pi_name,
        host=pi_dict["host"],
        user=pi_dict["user"],
        ssh_key_path=pi_dict["ssh_key_path"],
        services=pi_dict.get("services", []),
    )

    # Ask for Cloudflare token if this Pi uses a different account
    if _config.get("cloudflare_api_token"):
        if not click.confirm(
            f"\nUse the global Cloudflare token for {pi_name}?", default=True
        ):
            token = click.prompt(f"Cloudflare API token for {pi_name}", default="")
            if token:
                _config["pis"][pi_name]["cloudflare_api_token"] = token
                save_config(_config)
    else:
        token = click.prompt(
            f"\nCloudflare API token for {pi_name} (leave empty to skip)", default=""
        )
        if token:
            _config["pis"][pi_name]["cloudflare_api_token"] = token
            save_config(_config)

    console.print(f"[green]Pi '{pi_name}' added.[/green]")

    # Test connection
    pi_cfg = get_pi_config(_config, pi_name)
    console.print("Testing SSH connection...")
    if test_connection(pi_cfg):
        console.print("[green]Connected successfully![/green]")
    else:
        console.print("[yellow]Could not connect. Check IP/key.[/yellow]")


def _run_uninstall() -> None:
    import click
    console = Console()
    if not click.confirm("This will delete your config and uninstall PiManager. Continue?"):
        return
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]Config removed (~/.pi-manager)[/green]")
    console.print("[cyan]Uninstalling via pipx...[/cyan]")
    subprocess.run(["pipx", "uninstall", "pi-manager"])


# ---------------------------------------------------------------------------
# Input handler
# ---------------------------------------------------------------------------


def _on_accept(buff) -> None:
    """Called when the user presses Enter in the input area."""
    global _output_text, _busy, _active_pi

    text = buff.text.strip()
    if not text:
        return

    try:
        args = shlex.split(text)
    except ValueError as e:
        _output_text = f"\033[31mParse error: {e}\033[0m\n"
        _app.invalidate()
        return

    cmd = args[0]

    # Exit
    if cmd in ("exit", "quit"):
        _app.exit()
        return

    # Clear
    if cmd == "clear":
        _output_text = ""
        _app.invalidate()
        return

    # Don't allow concurrent commands
    if _busy:
        _output_text = "\033[33mCommand still running, please wait...\033[0m\n"
        _app.invalidate()
        return

    # Interactive commands need real terminal access
    is_interactive = cmd in INTERACTIVE_COMMANDS or (
        cmd == "project" and len(args) > 1 and args[1] == "add"
    )

    if is_interactive:
        _busy = True

        async def run_interactive():
            global _busy, _output_text

            handler = {
                "setup": _run_setup,
                "shutdown": lambda: __import__("pi_manager.services", fromlist=["shutdown_pi"]).shutdown_pi(
                    get_pi_config(_config, _resolve_effective_pi(None))
                ),
                "reboot": lambda: __import__("pi_manager.services", fromlist=["reboot_pi"]).reboot_pi(
                    get_pi_config(_config, _resolve_effective_pi(None))
                ),
                "uninstall": _run_uninstall,
                "add-pi": _run_add_pi,
            }

            func = handler.get(cmd, _run_project_add)

            try:
                await run_in_terminal(func)
            except Exception as e:
                _output_text = f"\033[31mError: {e}\033[0m\n"
            finally:
                _busy = False
                _app.invalidate()

        _app.create_background_task(run_interactive())
        return

    # Non-interactive: capture output in background thread
    _busy = True
    _output_text = "\033[33mRunning...\033[0m\n"
    _app.invalidate()

    async def run_captured():
        global _output_text, _busy
        loop = asyncio.get_event_loop()
        try:
            output = await loop.run_in_executor(None, lambda: _dispatch_captured(args))
            _output_text = output
        except Exception as e:
            _output_text = f"\033[31mError: {e}\033[0m\n"
        finally:
            _busy = False
            _app.invalidate()

    _app.create_background_task(run_captured())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def start_repl() -> None:
    """Start the interactive PiManager REPL."""
    global _app, _config

    _config = load_config()
    if not _config:
        _config = first_run_setup()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # --- Layout ---

    header_window = Window(
        content=FormattedTextControl(_get_header),
        height=4,
        dont_extend_height=True,
    )

    hint_window = Window(
        content=FormattedTextControl(_get_hint),
        height=1,
        dont_extend_height=True,
    )

    separator = Window(height=1, char="\u2500", style="class:separator")

    output_window = Window(
        content=FormattedTextControl(_get_output, focusable=False),
        wrap_lines=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )

    completer = WordCompleter(COMMANDS, ignore_case=True)
    history_file = CONFIG_DIR / "history"

    input_area = TextArea(
        height=1,
        prompt=HTML("<b><skyblue>pi</skyblue></b> <b>&gt;</b> "),
        multiline=False,
        completer=completer,
        history=FileHistory(str(history_file)),
        accept_handler=_on_accept,
    )

    body = HSplit([
        header_window,
        hint_window,
        separator,
        output_window,
        separator,
        input_area,
    ])

    layout = Layout(body, focused_element=input_area)

    # --- Key bindings ---

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-d")
    def _exit(event):
        event.app.exit()

    # --- Application ---

    _app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=False,
    )

    _app.run()
