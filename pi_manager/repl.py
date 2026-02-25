import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
from io import StringIO

from prompt_toolkit import Application

from . import __version__
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML, ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl, BufferControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import TextArea
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
    remove_pi,
    rename_pi,
    add_service_to_pi,
    remove_service_from_pi,
    set_tailscale_ip,
    remove_tailscale_ip,
    get_pi_config,
    get_pi_names,
    get_default_pi,
    resolve_pi,
    add_pi,
    prompt_with_exit,
    numbered_select,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMANDS = [
    "status", "services", "logs", "restart", "stop", "start", "ping",
    "ssh", "deploy", "upgrade-pis",
    "config", "setup", "shutdown", "reboot", "shutdown-pis", "reboot-pis", "update",
    "uninstall", "help", "clear", "exit", "quit",
    "list-pis", "add-pi", "remove-pi", "rename-pi", "edit-pi", "use",
    "add-project", "list-projects", "remove-project",
    "add-service", "remove-service",
    "open", "cache-clear",
    "tailscale",
]

HELP_TABLE = [
    ("status", "Show system status (all Pis, or --pi <name>)"),
    ("services", "Show service status (all Pis, or --pi <name>)"),
    ("logs", "Show Apache error logs (numbered Pi selection)"),
    ("logs --live", "Stream logs in real-time (CLI only)"),
    ("restart", "Restart a service (numbered selection)"),
    ("restart <service>", "Restart a specific service (--pi <name>)"),
    ("restart all", "Restart all monitored services"),
    ("stop", "Stop a service (numbered selection)"),
    ("stop <service>", "Stop a specific service (--pi <name>)"),
    ("start", "Start a service (numbered selection)"),
    ("start <service>", "Start a specific service (--pi <name>)"),
    ("ping", "Check if Pis are reachable via SSH"),
    ("ssh", "Open SSH in a new Terminal window (numbered Pi selection)"),
    ("deploy", "Deploy a project (numbered selection)"),
    ("deploy <name>", "Deploy a specific project (--pi <name>)"),
    ("", ""),
    ("list-pis", "List all configured Pis"),
    ("add-pi", "Add a new Pi interactively"),
    ("remove-pi <name>", "Remove a Pi"),
    ("rename-pi <old> <new>", "Rename a Pi (updates all references)"),
    ("edit-pi", "Edit a Pi's host, user, SSH key, or Tailscale IP"),
    ("use", "Set active Pi (numbered selection, persists)"),
    ("use <pi-name>", "Set active Pi by name (persists)"),
    ("add-service <name>", "Add a service to a Pi's monitor list"),
    ("remove-service", "Remove a service (numbered selection)"),
    ("", ""),
    ("tailscale list", "Show Tailscale IPs and connection mode"),
    ("tailscale set", "Set Tailscale IP for a Pi"),
    ("tailscale remove", "Remove Tailscale IP from a Pi"),
    ("", ""),
    ("config", "Show current configuration"),
    ("add-project", "Add a new deploy project (numbered selection)"),
    ("list-projects", "List configured projects"),
    ("remove-project <name>", "Remove a project"),
    ("open", "Open project URL in browser (numbered selection)"),
    ("open <project>", "Open a specific project's URL"),
    ("cache-clear", "Clear Cloudflare cache (numbered selection)"),
    ("cache-clear <project>", "Clear cache for a specific project"),
    ("setup", "Re-run the setup wizard"),
    ("update", "Update PiManager to latest version"),
    ("upgrade-pis", "Upgrade packages & restart services on all Pis"),
    ("upgrade-pis --pi <name>", "Upgrade a specific Pi"),
    ("shutdown", "Shut down the active Pi"),
    ("reboot", "Reboot the active Pi"),
    ("shutdown-pis", "Shut down ALL Pis (with confirmation)"),
    ("reboot-pis", "Reboot ALL Pis (with confirmation)"),
    ("uninstall", "Uninstall PiManager"),
    ("clear", "Clear the output"),
    ("help", "Show this help"),
    ("exit / quit", "Exit PiManager"),
]

# Commands that need direct terminal access (interactive prompts)
INTERACTIVE_COMMANDS = {"setup", "shutdown", "reboot", "shutdown-pis", "reboot-pis", "uninstall", "add-pi", "add-project", "edit-pi", "update"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class _AnsiStyleLexer(Lexer):
    """Lexer that preserves ANSI colors from Rich output in a BufferControl."""

    def __init__(self):
        self._styled_lines: list[list[tuple[str, str]]] = []

    @staticmethod
    def _merge(tuples):
        """Merge consecutive same-style character tuples into strings."""
        if not tuples:
            return []
        merged = []
        cur_style, cur_text = tuples[0][0], tuples[0][1]
        for style, text, *_ in tuples[1:]:
            if style == cur_style:
                cur_text += text
            else:
                merged.append((cur_style, cur_text))
                cur_style, cur_text = style, text
        merged.append((cur_style, cur_text))
        return merged

    def set_ansi_text(self, ansi_text: str) -> None:
        if not ansi_text:
            self._styled_lines = []
            return
        formatted = to_formatted_text(ANSI(ansi_text))
        self._styled_lines = [self._merge(line) for line in split_lines(formatted)]

    def lex_document(self, document):
        lines = self._styled_lines

        def get_line(lineno: int):
            if lineno < len(lines):
                return lines[lineno]
            return []

        return get_line


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_app: Application | None = None
_config: dict = {}
_output_text: str = ""
_busy: bool = False
_active_pi: str | None = None  # Session-level active Pi override
_output_buffer: Buffer | None = None  # Scrollable output buffer
_output_lexer = _AnsiStyleLexer()  # Lexer that preserves ANSI colors


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


def _list_remote_dirs(pi_cfg: dict, base_path: str = "/var/www") -> list[str]:
    """List directories on a Pi via SSH."""
    from .ssh import run_remote

    stdout, _, code = run_remote(
        pi_cfg,
        f'find {base_path} -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort',
    )
    if code == 0 and stdout.strip():
        return [d.strip() for d in stdout.strip().split("\n") if d.strip()]
    return []


# ---------------------------------------------------------------------------
# UI rendering
# ---------------------------------------------------------------------------


def _get_header() -> HTML:
    pis = _config.get("pis", {})
    pi_count = len(pis)

    if pi_count == 0:
        return HTML(
            "\n"
            f'  <b>PiManager</b> <style fg="ansibrightblack">v{__version__}</style>\n'
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
            f'  <b>PiManager</b> <style fg="ansibrightblack">v{__version__}</style>\n'
            f'  <ansicyan>{name}</ansicyan>'
            f' <style fg="ansibrightblack">({info.get("user", "pi")}@{info["host"]})</style>\n'
        )

    # Multiple Pis: show all, mark active
    lines = [
        "\n",
        f'  <b>PiManager</b> <style fg="ansibrightblack">v{__version__}</style>\n',
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
        "<b>PgUp/PgDn</b>"
        '<style fg="ansibrightblack"> to scroll, </style>'
        "<b>exit</b>"
        '<style fg="ansibrightblack"> to quit</style>'
    )



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
                from .ssh import SSHError, print_connection_label
                if pi_name:
                    pi_cfg = get_pi_config(_config, pi_name)
                    cap.print(f"\n[bold cyan]--- {pi_name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    print_connection_label(pi_cfg, cap)
                    show_status(pi_cfg)
                else:
                    # Always show all Pis
                    for name in get_pi_names(_config):
                        pi_cfg = get_pi_config(_config, name)
                        cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                        try:
                            print_connection_label(pi_cfg, cap)
                            show_status(pi_cfg)
                        except SSHError as e:
                            cap.print(f"[red]Offline — {e}[/red]")

            elif cmd == "services":
                from .monitor import show_services
                from .ssh import SSHError, print_connection_label
                if pi_name:
                    pi_cfg = get_pi_config(_config, pi_name)
                    cap.print(f"\n[bold cyan]--- {pi_name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    print_connection_label(pi_cfg, cap)
                    show_services(pi_cfg)
                else:
                    # Always show all Pis
                    for name in get_pi_names(_config):
                        pi_cfg = get_pi_config(_config, name)
                        cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                        try:
                            print_connection_label(pi_cfg, cap)
                            show_services(pi_cfg)
                        except SSHError as e:
                            cap.print(f"[red]Offline — {e}[/red]")

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
                    from .ssh import print_connection_label
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    print_connection_label(pi_cfg, cap)
                    show_logs(pi_cfg, live=False, lines=lines)

            elif cmd == "restart":
                from .services import restart_service, restart_all
                if not rest:
                    cap.print("[yellow]Usage: restart <service|all>[/yellow]")
                else:
                    from .ssh import print_connection_label
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    print_connection_label(pi_cfg, cap)
                    if rest[0] == "all":
                        restart_all(pi_cfg)
                    else:
                        restart_service(pi_cfg, rest[0])

            elif cmd == "stop":
                from .services import stop_service
                if not rest:
                    cap.print("[yellow]Usage: stop <service>[/yellow]")
                else:
                    from .ssh import print_connection_label
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    print_connection_label(pi_cfg, cap)
                    stop_service(pi_cfg, rest[0])

            elif cmd == "start":
                from .services import start_service
                if not rest:
                    cap.print("[yellow]Usage: start <service>[/yellow]")
                else:
                    from .ssh import print_connection_label
                    effective_pi = _resolve_effective_pi(pi_name)
                    pi_cfg = get_pi_config(_config, effective_pi)
                    print_connection_label(pi_cfg, cap)
                    start_service(pi_cfg, rest[0])

            elif cmd == "ping":
                from .ssh import ping_pi
                if pi_name:
                    pi_cfg = get_pi_config(_config, pi_name)
                    cap.print(f"\n[bold cyan]--- {pi_name} ---[/bold cyan]")
                    ping_pi(pi_cfg)
                else:
                    for name in get_pi_names(_config):
                        pi_cfg = get_pi_config(_config, name)
                        cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                        ping_pi(pi_cfg)

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

            elif cmd == "list-pis":
                _list_pis(cap)

            elif cmd == "list-projects":
                _project_list(cap)

            elif cmd == "remove-project":
                if not rest:
                    cap.print("[yellow]Usage: remove-project <name>[/yellow]")
                else:
                    _project_remove(" ".join(rest), cap)

            elif cmd == "remove-pi":
                if not rest:
                    cap.print("[yellow]Usage: remove-pi <name>[/yellow]")
                else:
                    _remove_pi(" ".join(rest), cap)

            elif cmd == "use":
                if not rest:
                    # Without args is handled interactively; this is a fallback
                    cap.print("[yellow]Usage: use <pi-name>[/yellow]")
                    pi_names = get_pi_names(_config)
                    if pi_names:
                        for i, n in enumerate(pi_names, 1):
                            cap.print(f"  {i}) {n} ({_config['pis'][n]['host']})")
                else:
                    target = " ".join(rest)
                    pi_names = get_pi_names(_config)
                    # Allow numeric selection: use 1, use 2, etc.
                    try:
                        idx = int(target)
                        if 1 <= idx <= len(pi_names):
                            target = pi_names[idx - 1]
                        else:
                            cap.print(f"[red]Invalid number: {idx}[/red]")
                            for i, n in enumerate(pi_names, 1):
                                cap.print(f"  {i}) {n} ({_config['pis'][n]['host']})")
                            target = None
                    except ValueError:
                        pass  # Not a number, treat as name

                    if target and target not in pi_names:
                        cap.print(f"[red]Unknown Pi: '{target}'[/red]")
                        for i, n in enumerate(pi_names, 1):
                            cap.print(f"  {i}) {n} ({_config['pis'][n]['host']})")
                    elif target:
                        _active_pi = target
                        _config["default_pi"] = target
                        save_config(_config)
                        pi_info = _config["pis"][target]
                        cap.print(
                            f"[green]Active Pi set to [bold]{target}[/bold] "
                            f"({pi_info.get('user', 'pi')}@{pi_info['host']})[/green]"
                        )

            elif cmd == "rename-pi":
                if len(rest) < 2:
                    cap.print("[yellow]Usage: rename-pi <old-name> <new-name>[/yellow]")
                else:
                    old_name, new_name = rest[0], rest[1]
                    if old_name not in _config.get("pis", {}):
                        cap.print(f"[red]Pi '{old_name}' not found.[/red]")
                        pis = _config.get("pis", {})
                        if pis:
                            cap.print(f"Available: {', '.join(pis.keys())}")
                    elif new_name in _config.get("pis", {}):
                        cap.print(f"[red]Pi '{new_name}' already exists.[/red]")
                    else:
                        rename_pi(_config, old_name, new_name)
                        if _active_pi == old_name:
                            _active_pi = new_name
                        cap.print(f"[green]Pi '{old_name}' renamed to '{new_name}'.[/green]")

            elif cmd == "add-service":
                if not rest:
                    cap.print("[yellow]Usage: add-service <name> [--pi <pi-name>][/yellow]")
                else:
                    service_name = rest[0]
                    target_pi = pi_name
                    if not target_pi:
                        try:
                            target_pi = _resolve_effective_pi(None)
                        except Exception:
                            cap.print("[red]No Pi specified and no default Pi set.[/red]")
                            target_pi = None
                    if target_pi:
                        if add_service_to_pi(_config, target_pi, service_name):
                            cap.print(f"[green]Service '{service_name}' added to {target_pi}.[/green]")
                        else:
                            if target_pi not in _config.get("pis", {}):
                                cap.print(f"[red]Pi '{target_pi}' not found.[/red]")
                            else:
                                cap.print(f"[yellow]Service '{service_name}' already monitored on {target_pi}.[/yellow]")

            elif cmd == "remove-service":
                if rest:
                    service_name = rest[0]
                    target_pi = pi_name
                    if not target_pi:
                        try:
                            target_pi = _resolve_effective_pi(None)
                        except Exception:
                            cap.print("[red]No Pi specified and no default Pi set.[/red]")
                            target_pi = None
                    if target_pi:
                        if remove_service_from_pi(_config, target_pi, service_name):
                            cap.print(f"[green]Service '{service_name}' removed from {target_pi}.[/green]")
                        else:
                            if target_pi not in _config.get("pis", {}):
                                cap.print(f"[red]Pi '{target_pi}' not found.[/red]")
                            else:
                                cap.print(f"[red]Service '{service_name}' not found on {target_pi}.[/red]")
                                svcs = _config["pis"][target_pi].get("services", [])
                                if svcs:
                                    cap.print(f"Current services: {', '.join(svcs)}")
                else:
                    # Without args is handled interactively; this is a fallback
                    cap.print("[yellow]Usage: remove-service <name> [--pi <pi-name>][/yellow]")

            elif cmd == "open":
                if rest:
                    project_name = rest[0]
                    proj = _config.get("projects", {}).get(project_name)
                    if not proj:
                        cap.print(f"[red]Unknown project: {project_name}[/red]")
                        projects = _config.get("projects", {})
                        if projects:
                            cap.print(f"Available: {', '.join(projects.keys())}")
                    else:
                        url = proj.get("url")
                        if not url:
                            cap.print(
                                f"[yellow]No URL configured for '{project_name}'.[/yellow]\n"
                                f"[dim]Add a 'url' field to the project in ~/.pi-manager/config.json[/dim]"
                            )
                        else:
                            cap.print(f"[cyan]Opening {url}...[/cyan]")
                            import subprocess as sp
                            sp.run(["open", url])
                else:
                    # Without args is handled interactively; this is a fallback
                    cap.print("[yellow]Usage: open <project>[/yellow]")
                    projects = _config.get("projects", {})
                    if projects:
                        for i, (name, info) in enumerate(projects.items(), 1):
                            url = info.get("url", "(no URL)")
                            cap.print(f"  {i}) {name} — {url}")

            elif cmd == "cache-clear":
                if rest:
                    project_name = rest[0]
                    proj = _config.get("projects", {}).get(project_name)
                    if not proj:
                        cap.print(f"[red]Unknown project: {project_name}[/red]")
                        projects = _config.get("projects", {})
                        if projects:
                            cap.print(f"Available: {', '.join(projects.keys())}")
                    else:
                        from .deploy import purge_cloudflare_cache

                        proj_pi = proj.get("pi")
                        if proj_pi and proj_pi in _config.get("pis", {}):
                            pi = _config["pis"][proj_pi]
                            token = pi.get("cloudflare_api_token") or _config.get("cloudflare_api_token", "")
                        else:
                            token = _config.get("cloudflare_api_token", "")
                        cf_config = {"cloudflare_api_token": token}
                        cap.print(f"[cyan]Purging Cloudflare cache for {project_name}...[/cyan]")
                        if purge_cloudflare_cache(cf_config, proj):
                            cap.print(f"[green]Cache cleared for {project_name}.[/green]")
                else:
                    # Without args is handled interactively; this is a fallback
                    cap.print("[yellow]Usage: cache-clear <project>[/yellow]")
                    projects = _config.get("projects", {})
                    if projects:
                        for i, (name, info) in enumerate(projects.items(), 1):
                            zone = info.get("cloudflare_zone_id", "-")
                            cap.print(f"  {i}) {name} (zone: {zone})")

            elif cmd == "tailscale":
                from .ssh import is_on_home_network

                if not rest:
                    cap.print("[yellow]Usage: tailscale <set|remove|list>[/yellow]")
                elif rest[0] == "list":
                    pis = _config.get("pis", {})
                    if not pis:
                        cap.print("[yellow]No Pis configured.[/yellow]")
                    else:
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
                        cap.print(table)
                elif rest[0] in ("set", "remove"):
                    # Handled via interactive dispatch
                    cap.print("[yellow]This command requires interactive mode.[/yellow]")
                else:
                    cap.print(f"[red]Unknown tailscale subcommand: {rest[0]}[/red]")
                    cap.print("[dim]Available: set, remove, list[/dim]")

            elif cmd == "upgrade-pis":
                from .services import upgrade_pi, restart_all
                from .ssh import print_connection_label
                pi_names_to_upgrade = [pi_name] if pi_name else get_pi_names(_config)
                for name in pi_names_to_upgrade:
                    pi_cfg = get_pi_config(_config, name)
                    cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    try:
                        print_connection_label(pi_cfg, cap)
                        if upgrade_pi(pi_cfg):
                            cap.print("[cyan]Restarting services...[/cyan]")
                            restart_all(pi_cfg)
                            cap.print(f"[bold green]{name} fully updated.[/bold green]")
                    except SSHError as e:
                        cap.print(f"[red]Offline — {e}[/red]")

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
            is_default = "\u2605" if name == default else ""
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
        is_default = "\u2605" if name == default else ""
        svcs = ", ".join(info.get("services", [])) or "-"
        table.add_row(name, info.get("host", ""), info.get("user", "pi"), svcs, is_default)

    cap.print(table)


def _project_list(cap: Console) -> None:
    projects = _config.get("projects", {})
    if not projects:
        cap.print("[yellow]No projects configured. Use 'add-project' to add one.[/yellow]")
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


def _remove_pi(name: str, cap: Console) -> None:
    if remove_pi(_config, name):
        cap.print(f"[green]Pi '{name}' removed.[/green]")
    else:
        cap.print(f"[red]Pi '{name}' not found.[/red]")
        pis = _config.get("pis", {})
        if pis:
            cap.print(f"Available: {', '.join(pis.keys())}")


# ---------------------------------------------------------------------------
# Interactive command handlers (run in real terminal, not captured)
# ---------------------------------------------------------------------------


def _run_setup() -> None:
    global _config
    try:
        _config = first_run_setup()
    except UserExit:
        Console().print("\n[yellow]Setup cancelled.[/yellow]")


def _run_add_project() -> None:
    import click
    console = Console()

    try:
        name = prompt_with_exit("Project name")
        if name in _config.get("projects", {}):
            console.print(f"[yellow]Project '{name}' already exists.[/yellow]")
            return
        local_path = prompt_with_exit("Local path (folder to sync)")

        # Target Pi
        pi_names = get_pi_names(_config)
        if pi_names:
            if len(pi_names) == 1:
                target_pi = pi_names[0]
                console.print(f"Target Pi: [bold]{target_pi}[/bold]")
            else:
                pis = _config.get("pis", {})
                items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
                target_pi = numbered_select(items, "Select target Pi", allow_cancel=False)
        else:
            target_pi = ""

        # Remote path: browse directories on Pi or enter manually
        if target_pi and target_pi in _config.get("pis", {}):
            pi_cfg = get_pi_config(_config, target_pi)
            dirs = _list_remote_dirs(pi_cfg)

            if dirs:
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
                        console.print("\n[yellow]Cancelled.[/yellow]")
                        return
                    if 1 <= idx <= len(dirs):
                        remote_path = dirs[idx - 1]
                        if not remote_path.endswith("/"):
                            remote_path += "/"
                        break
                    if idx == len(dirs) + 1:
                        remote_path = prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")
                        break
                    console.print("[yellow]Invalid choice.[/yellow]")
            else:
                remote_path = prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")
        else:
            remote_path = prompt_with_exit("Remote path on Pi (e.g. /var/www/my-site/)")

        cf_zone = prompt_with_exit("Cloudflare zone ID (leave empty to skip)", default="")
        add_project(_config, name, local_path, remote_path, pi_name=target_pi, cloudflare_zone_id=cf_zone)
        console.print(f"[green]Project '{name}' added.[/green]")

    except UserExit:
        Console().print("\n[yellow]Cancelled.[/yellow]")


def _run_add_pi() -> None:
    """Interactive wizard for adding a Pi in the REPL."""
    import click
    from .config import _setup_single_pi, test_connection, save_config

    global _config
    console = Console()

    try:
        pi_name, pi_dict = _setup_single_pi()
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

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

    # Ask for Cloudflare token
    try:
        if _config.get("cloudflare_api_token"):
            if not click.confirm(
                f"\nUse the global Cloudflare token for {pi_name}?", default=True
            ):
                token = prompt_with_exit(f"Cloudflare API token for {pi_name}", default="")
                if token:
                    _config["pis"][pi_name]["cloudflare_api_token"] = token
                    save_config(_config)
        else:
            token = prompt_with_exit(
                f"\nCloudflare API token for {pi_name} (leave empty to skip)", default=""
            )
            if token:
                _config["pis"][pi_name]["cloudflare_api_token"] = token
                save_config(_config)
    except UserExit:
        pass  # Pi already added, just skip Cloudflare

    console.print(f"[green]Pi '{pi_name}' added.[/green]")

    # Test connection
    pi_cfg = get_pi_config(_config, pi_name)
    console.print("Testing SSH connection...")
    if test_connection(pi_cfg):
        console.print("[green]Connected successfully![/green]")
    else:
        console.print("[yellow]Could not connect. Check IP/key.[/yellow]")


def _run_use_select() -> None:
    """Interactive Pi selection for 'use' without arguments."""
    global _active_pi
    console = Console()
    pi_names = get_pi_names(_config)

    if not pi_names:
        console.print("[yellow]No Pis configured. Run 'add-pi' or 'setup'.[/yellow]")
        return

    if len(pi_names) == 1:
        _active_pi = pi_names[0]
        _config["default_pi"] = pi_names[0]
        save_config(_config)
        pi_info = _config["pis"][pi_names[0]]
        console.print(
            f"[green]Active Pi set to [bold]{pi_names[0]}[/bold] ({pi_info['host']})[/green]"
        )
        return

    items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
    try:
        selected = numbered_select(items, "Select a Pi")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if selected:
        _active_pi = selected
        _config["default_pi"] = selected
        save_config(_config)
        pi_info = _config["pis"][selected]
        console.print(
            f"[green]Active Pi set to [bold]{selected}[/bold] ({pi_info['host']})[/green]"
        )


def _run_edit_pi() -> None:
    """Interactive wizard for editing a Pi's settings."""
    global _config
    console = Console()
    pi_names = get_pi_names(_config)

    if not pi_names:
        console.print("[yellow]No Pis configured. Run 'add-pi' or 'setup'.[/yellow]")
        return

    pis = _config.get("pis", {})
    items = [(n, f"{n} ({pis[n]['host']})") for n in pi_names]
    try:
        selected = numbered_select(items, "Select a Pi to edit", allow_cancel=True)
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return

    pi = pis[selected]
    console.print(f"\nEditing [bold]{selected}[/bold] (leave empty to keep current value)\n")

    try:
        new_host = prompt_with_exit(f"Host [{pi['host']}]", default="")
        new_user = prompt_with_exit(f"User [{pi.get('user', 'pi')}]", default="")
        new_key = prompt_with_exit(
            f"SSH key path [{pi.get('ssh_key_path', '~/.pi-manager/keys/id_rsa')}]",
            default="",
        )
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return

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
        save_config(_config)
        console.print(f"[green]Pi '{selected}' updated.[/green]")
    else:
        console.print("[dim]No changes made.[/dim]")


def _run_remove_service_select() -> None:
    """Interactive service selection for 'remove-service' without arguments."""
    global _config
    console = Console()

    try:
        effective_pi = _resolve_effective_pi(None)
    except Exception:
        pi_names = get_pi_names(_config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
        try:
            effective_pi = numbered_select(items, "Select a Pi")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not effective_pi:
            return

    services = _config.get("pis", {}).get(effective_pi, {}).get("services", [])
    if not services:
        console.print(f"[yellow]No services configured for {effective_pi}.[/yellow]")
        return

    items = [(s, s) for s in services]
    try:
        selected = numbered_select(items, f"Remove service from {effective_pi}")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return

    if remove_service_from_pi(_config, effective_pi, selected):
        console.print(f"[green]Service '{selected}' removed from {effective_pi}.[/green]")


def _run_open_select() -> None:
    """Interactive project selection for 'open' without arguments."""
    import subprocess as sp
    console = Console()
    projects = _config.get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured.[/yellow]")
        return

    items = [
        (name, f"{name} — {info.get('url', '(no URL)')}")
        for name, info in projects.items()
    ]
    try:
        selected = numbered_select(items, "Select a project to open")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return

    proj = projects[selected]
    url = proj.get("url")
    if not url:
        console.print(
            f"[yellow]No URL configured for '{selected}'.[/yellow]\n"
            f"[dim]Add a 'url' field to the project in ~/.pi-manager/config.json[/dim]"
        )
        return

    console.print(f"[cyan]Opening {url}...[/cyan]")
    sp.run(["open", url])


def _run_cache_clear_select() -> None:
    """Interactive project selection for 'cache-clear' without arguments."""
    from .deploy import purge_cloudflare_cache
    console = Console()
    projects = _config.get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured.[/yellow]")
        return

    items = [
        (name, f"{name} (zone: {info.get('cloudflare_zone_id', '-')})")
        for name, info in projects.items()
    ]
    try:
        selected = numbered_select(items, "Select a project")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return

    proj = projects[selected]
    proj_pi = proj.get("pi")
    if proj_pi and proj_pi in _config.get("pis", {}):
        pi = _config["pis"][proj_pi]
        token = pi.get("cloudflare_api_token") or _config.get("cloudflare_api_token", "")
    else:
        token = _config.get("cloudflare_api_token", "")
    cf_config = {"cloudflare_api_token": token}

    console.print(f"[cyan]Purging Cloudflare cache for {selected}...[/cyan]")
    if purge_cloudflare_cache(cf_config, proj):
        console.print(f"[green]Cache cleared for {selected}.[/green]")


def _run_tailscale_set() -> None:
    """Interactive handler for 'tailscale set'."""
    console = Console()
    pi_names = get_pi_names(_config)
    if not pi_names:
        console.print("[yellow]No Pis configured.[/yellow]")
        return
    items = [
        (n, f"{n}  (current: {_config['pis'][n].get('tailscale_host', '—')})")
        for n in pi_names
    ]
    try:
        selected = numbered_select(items, "Select Pi", allow_cancel=True)
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return
    try:
        ts_ip = prompt_with_exit("Tailscale IP")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if ts_ip:
        if set_tailscale_ip(_config, selected, ts_ip):
            console.print(f"[green]Tailscale IP for '{selected}' set to {ts_ip}.[/green]")
        else:
            console.print(f"[red]Failed to set Tailscale IP.[/red]")


def _run_tailscale_remove() -> None:
    """Interactive handler for 'tailscale remove'."""
    console = Console()
    pi_names = get_pi_names(_config)
    ts_pis = [n for n in pi_names if _config['pis'][n].get('tailscale_host')]
    if not ts_pis:
        console.print("[yellow]No Pis with a Tailscale IP configured.[/yellow]")
        return
    items = [
        (n, f"{n}  ({_config['pis'][n].get('tailscale_host')})")
        for n in ts_pis
    ]
    try:
        selected = numbered_select(items, "Select Pi to remove Tailscale IP", allow_cancel=True)
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return
    if remove_tailscale_ip(_config, selected):
        console.print(f"[green]Tailscale IP removed from '{selected}'.[/green]")
    else:
        console.print(f"[yellow]No Tailscale IP configured for '{selected}'.[/yellow]")


def _run_deploy_select() -> None:
    """Interactive project selection for 'deploy' without arguments."""
    console = Console()
    projects = _config.get("projects", {})

    if not projects:
        console.print("[yellow]No projects configured. Use 'add-project' to add one.[/yellow]")
        return

    items = [
        (name, f"{name} → {info.get('remote_path', '?')}")
        for name, info in projects.items()
    ]
    try:
        selected = numbered_select(items, "Select a project to deploy")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if not selected:
        return

    from .deploy import deploy
    from .ssh import SSHError

    project = projects[selected]
    try:
        effective_name = project.get("pi") or _active_pi
        effective_name = resolve_pi(_config, effective_name)
        pi_cfg = get_pi_config(_config, effective_name)
        pi_cfg["projects"] = projects
        deploy(pi_cfg, selected, pi_name=effective_name)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _run_restart_select() -> None:
    """Interactive service selection for 'restart' without arguments."""
    console = Console()

    try:
        effective_pi = _resolve_effective_pi(None)
    except Exception:
        # No Pi resolved — let user pick one first
        pi_names = get_pi_names(_config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
        try:
            effective_pi = numbered_select(items, "Select a Pi")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not effective_pi:
            return

    from .ssh import SSHError

    try:
        pi_cfg = get_pi_config(_config, effective_pi)
        services = pi_cfg.get("services", [])

        if not services:
            console.print(f"[yellow]No services configured for {effective_pi}.[/yellow]")
            return

        items = [(s, s) for s in services]
        items.append(("all", "all (restart all services)"))
        try:
            selected = numbered_select(items, f"Restart service on {effective_pi}")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not selected:
            return

        from .services import restart_service, restart_all
        from .ssh import print_connection_label

        print_connection_label(pi_cfg)
        if selected == "all":
            restart_all(pi_cfg)
        else:
            restart_service(pi_cfg, selected)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _run_stop_select() -> None:
    """Interactive service selection for 'stop' without arguments."""
    console = Console()

    try:
        effective_pi = _resolve_effective_pi(None)
    except Exception:
        pi_names = get_pi_names(_config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
        try:
            effective_pi = numbered_select(items, "Select a Pi")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not effective_pi:
            return

    from .ssh import SSHError

    try:
        pi_cfg = get_pi_config(_config, effective_pi)
        services = pi_cfg.get("services", [])

        if not services:
            console.print(f"[yellow]No services configured for {effective_pi}.[/yellow]")
            return

        items = [(s, s) for s in services]
        try:
            selected = numbered_select(items, f"Stop service on {effective_pi}")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not selected:
            return

        from .services import stop_service
        from .ssh import print_connection_label
        print_connection_label(pi_cfg)
        stop_service(pi_cfg, selected)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _run_start_select() -> None:
    """Interactive service selection for 'start' without arguments."""
    console = Console()

    try:
        effective_pi = _resolve_effective_pi(None)
    except Exception:
        pi_names = get_pi_names(_config)
        if not pi_names:
            console.print("[yellow]No Pis configured.[/yellow]")
            return
        items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
        try:
            effective_pi = numbered_select(items, "Select a Pi")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not effective_pi:
            return

    from .ssh import SSHError

    try:
        pi_cfg = get_pi_config(_config, effective_pi)
        services = pi_cfg.get("services", [])

        if not services:
            console.print(f"[yellow]No services configured for {effective_pi}.[/yellow]")
            return

        items = [(s, s) for s in services]
        try:
            selected = numbered_select(items, f"Start service on {effective_pi}")
        except UserExit:
            console.print("\n[yellow]Cancelled.[/yellow]")
            return
        if not selected:
            return

        from .services import start_service
        from .ssh import print_connection_label
        print_connection_label(pi_cfg)
        start_service(pi_cfg, selected)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _run_update() -> None:
    """Update PiManager from git repo, then restart if successful."""
    global _config
    import os
    import time as _time
    from .cli import do_update

    updated = do_update(_config)
    if updated:
        console = Console()
        console.print("\n[cyan]Restarting PiManager...[/cyan]")
        _time.sleep(0.5)
        os.execvp(sys.argv[0], sys.argv)


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
    sys.exit(0)


def _run_shutdown_pis() -> None:
    import click
    from .ssh import run_remote, print_connection_label
    from .ssh import SSHError as _SSHError
    console = Console()
    pi_names = get_pi_names(_config)
    if not pi_names:
        console.print("[yellow]No Pis configured.[/yellow]")
        return
    if not click.confirm(f"Shut down all {len(pi_names)} Pi(s)?"):
        return
    for name in pi_names:
        pi_cfg = get_pi_config(_config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)
            console.print("[yellow]Shutting down...[/yellow]")
            run_remote(pi_cfg, "sudo shutdown -h now")
            console.print(f"[bold green]Shutdown command sent to {name}.[/bold green]")
        except _SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")


def _run_reboot_pis() -> None:
    import click
    from .ssh import run_remote, print_connection_label
    from .ssh import SSHError as _SSHError
    console = Console()
    pi_names = get_pi_names(_config)
    if not pi_names:
        console.print("[yellow]No Pis configured.[/yellow]")
        return
    if not click.confirm(f"Reboot all {len(pi_names)} Pi(s)?"):
        return
    for name in pi_names:
        pi_cfg = get_pi_config(_config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)
            console.print("[yellow]Rebooting...[/yellow]")
            run_remote(pi_cfg, "sudo reboot")
            console.print(f"[bold green]Reboot command sent to {name}.[/bold green]")
        except _SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")


# ---------------------------------------------------------------------------
# Input handler
# ---------------------------------------------------------------------------


def _set_output(text: str) -> None:
    """Update the output buffer with new text, preserving colors via lexer."""
    global _output_text
    _output_text = text
    _output_lexer.set_ansi_text(text)
    if _output_buffer is not None:
        plain = _strip_ansi(text)
        _output_buffer.set_document(Document(plain, cursor_position=0), bypass_readonly=True)


def _on_accept(buff) -> None:
    """Called when the user presses Enter in the input area."""
    global _output_text, _busy, _active_pi

    text = buff.text.strip()
    if not text:
        return

    try:
        args = shlex.split(text)
    except ValueError as e:
        _set_output(f"Parse error: {e}\n")
        _app.invalidate()
        return

    cmd = args[0]

    # Exit
    if cmd in ("exit", "quit"):
        _app.exit()
        return

    # Clear
    if cmd == "clear":
        _set_output("")
        _app.invalidate()
        return

    # Don't allow concurrent commands
    if _busy:
        _set_output("Command still running, please wait...\n")
        _app.invalidate()
        return

    # Determine if this invocation needs interactive terminal access
    interactive_handler = None

    if cmd in INTERACTIVE_COMMANDS:
        static_handlers = {
            "setup": _run_setup,
            "shutdown": lambda: __import__("pi_manager.services", fromlist=["shutdown_pi"]).shutdown_pi(
                get_pi_config(_config, _resolve_effective_pi(None))
            ),
            "reboot": lambda: __import__("pi_manager.services", fromlist=["reboot_pi"]).reboot_pi(
                get_pi_config(_config, _resolve_effective_pi(None))
            ),
            "shutdown-pis": _run_shutdown_pis,
            "reboot-pis": _run_reboot_pis,
            "uninstall": _run_uninstall,
            "add-pi": _run_add_pi,
            "add-project": _run_add_project,
            "edit-pi": _run_edit_pi,
            "update": _run_update,
        }
        interactive_handler = static_handlers.get(cmd)

    # Commands that become interactive when called without arguments
    elif cmd == "use" and len(args) == 1:
        interactive_handler = _run_use_select
    elif cmd == "deploy" and len(args) == 1:
        interactive_handler = _run_deploy_select
    elif cmd == "restart" and len(args) == 1:
        interactive_handler = _run_restart_select
    elif cmd == "stop" and len(args) == 1:
        interactive_handler = _run_stop_select
    elif cmd == "start" and len(args) == 1:
        interactive_handler = _run_start_select
    elif cmd == "remove-service" and len(args) == 1:
        interactive_handler = _run_remove_service_select
    elif cmd == "open" and len(args) == 1:
        interactive_handler = _run_open_select
    elif cmd == "cache-clear" and len(args) == 1:
        interactive_handler = _run_cache_clear_select
    elif cmd == "tailscale" and len(args) >= 2 and args[1] == "set":
        interactive_handler = _run_tailscale_set
    elif cmd == "tailscale" and len(args) >= 2 and args[1] == "remove":
        interactive_handler = _run_tailscale_remove

    if interactive_handler is not None:
        _busy = True
        func = interactive_handler

        async def run_interactive():
            global _busy

            try:
                await run_in_terminal(func)
            except Exception as e:
                _set_output(f"Error: {e}\n")
            finally:
                _busy = False
                _app.invalidate()

        _app.create_background_task(run_interactive())
        return

    # Non-interactive: capture output in background thread
    _busy = True
    _set_output("Running...\n")
    _app.invalidate()

    async def run_captured():
        global _busy
        loop = asyncio.get_event_loop()
        try:
            output = await loop.run_in_executor(None, lambda: _dispatch_captured(args))
            _set_output(output)
        except Exception as e:
            _set_output(f"Error: {e}\n")
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
        try:
            _config = first_run_setup()
        except UserExit:
            Console().print("\n[yellow]Setup cancelled.[/yellow]")
            sys.exit(0)

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

    _output_buffer = Buffer(name="output", read_only=True)
    # Make module-level reference available for _set_output()
    import pi_manager.repl as _self
    _self._output_buffer = _output_buffer

    output_window = Window(
        content=BufferControl(buffer=_output_buffer, lexer=_output_lexer, focusable=True),
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

    @kb.add("pageup")
    def _page_up(event):
        """Scroll output up regardless of focus."""
        for _ in range(16):
            _output_buffer.cursor_up()

    @kb.add("pagedown")
    def _page_down(event):
        """Scroll output down regardless of focus."""
        for _ in range(16):
            _output_buffer.cursor_down()

    # --- Application ---

    _app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
    )

    _app.run()
    sys.exit(0)
