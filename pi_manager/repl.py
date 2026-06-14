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
    remove_pi,
    rename_pi,
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
    "status", "check",
    "restart", "stop", "start", "upgrade", "ssh", "shutdown", "reboot",
    "list", "add", "remove", "rename", "edit", "use",
    "tailscale",
    "setup", "update", "uninstall",
    "help", "clear", "exit", "quit",
]

HELP_TABLE = [
    ("status", "System- & Service-Status aller Pis"),
    ("check", "Alle Pis prüfen: Services & Hostnamen automatisch übernehmen"),
    ("", ""),
    ("restart", "Service neu starten (Pi wählen → Service wählen)"),
    ("restart all", "Alle Services eines Pis neu starten (Pi wählen)"),
    ("stop", "Service stoppen (Pi wählen → Service wählen)"),
    ("start", "Service starten (Pi wählen → Service wählen)"),
    ("upgrade", "Pi wählen → System- & Service-Updates prüfen und einspielen"),
    ("ssh", "SSH-Sitzung öffnen (Pi wählen)"),
    ("shutdown", "Pi herunterfahren (Pi wählen)"),
    ("reboot", "Pi neu starten (Pi wählen)"),
    ("", ""),
    ("list", "Alle konfigurierten Pis anzeigen"),
    ("add", "Neuen Pi hinzufügen"),
    ("remove", "Pi entfernen (Pi wählen)"),
    ("rename", "Pi umbenennen (Pi wählen → neuer Name, ändert echten Hostnamen)"),
    ("edit", "Pi bearbeiten: Host, User, SSH-Key (Pi wählen)"),
    ("use", "Aktiven Pi setzen (Pi wählen)"),
    ("", ""),
    ("tailscale list", "Tailscale-IPs & Verbindungsmodus anzeigen"),
    ("tailscale set", "Tailscale-IP für einen Pi setzen (Pi wählen)"),
    ("tailscale remove", "Tailscale-IP eines Pis entfernen (Pi wählen)"),
    ("", ""),
    ("setup", "Setup-Assistent erneut ausführen"),
    ("update", "PiManager aktualisieren"),
    ("uninstall", "PiManager deinstallieren"),
    ("clear", "Ausgabe leeren"),
    ("help", "Diese Hilfe anzeigen"),
    ("exit / quit", "PiManager beenden"),
]

# Commands that need direct terminal access (interactive prompts).
# All action commands follow the same principle: enter command → pick Pi → extra input if needed.
INTERACTIVE_COMMANDS = {
    "setup", "update", "uninstall",
    "add", "edit", "use", "remove", "rename",
    "restart", "stop", "start", "reboot", "shutdown", "upgrade", "ssh", "check",
}

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
    import pi_manager.ssh as _ssh

    modules = [_mon, _svc, _ssh]
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
            f'  <b>PiManager</b> <style fg="ansibrightblack">v{__version__}</style>\n'
            '  <style fg="ansibrightblack">No Pis configured — run </style><b>setup</b>'
            '<style fg="ansibrightblack"> or </style><b>add</b>\n'
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
                from .monitor import show_status, show_services
                from .services import detect_services
                from .ssh import SSHError, print_connection_label
                names = [pi_name] if pi_name else get_pi_names(_config)
                changed = False
                for name in names:
                    pi_cfg = get_pi_config(_config, name)
                    cap.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
                    try:
                        print_connection_label(pi_cfg, cap)
                        show_status(pi_cfg)
                        # Auto-detect installed services and remember them (no add/remove needed)
                        svcs = detect_services(pi_cfg)
                        _config["pis"][name]["services"] = svcs
                        changed = True
                        show_services(pi_cfg, services=svcs)
                    except SSHError as e:
                        cap.print(f"[red]Offline — {e}[/red]")
                if changed:
                    save_config(_config)

            elif cmd == "list":
                _list_pis(cap)

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


def _list_pis(cap: Console) -> None:
    pis = _config.get("pis", {})
    default = get_default_pi(_config)

    if not pis:
        cap.print("[yellow]No Pis configured. Use 'add' or 'setup' to add one.[/yellow]")
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


# ---------------------------------------------------------------------------
# Interactive command handlers (run in real terminal, not captured)
# ---------------------------------------------------------------------------


def _select_pi(prompt_text: str = "Select a Pi") -> str | None:
    """Always show a numbered Pi selection (auto-picks if only one). Returns name or None.

    This is the shared 'pick a Pi first' step every action command uses.
    """
    console = Console()
    pi_names = get_pi_names(_config)
    if not pi_names:
        console.print("[yellow]No Pis configured. Run 'add' or 'setup'.[/yellow]")
        return None
    if len(pi_names) == 1:
        return pi_names[0]
    items = [(n, f"{n} ({_config['pis'][n]['host']})") for n in pi_names]
    try:
        return numbered_select(items, prompt_text, allow_cancel=True)
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return None


def _hostname_label(name: str) -> str:
    """Turn a friendly Pi name into a valid hostname label (letters/digits/hyphen)."""
    label = re.sub(r"[^A-Za-z0-9-]", "-", name.strip())
    label = re.sub(r"-+", "-", label).strip("-")
    return label or "raspberrypi"


def _run_setup() -> None:
    global _config
    try:
        _config = first_run_setup()
    except UserExit:
        Console().print("\n[yellow]Setup cancelled.[/yellow]")


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
        console.print("[yellow]No Pis configured. Run 'add' or 'setup'.[/yellow]")
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
        console.print("[yellow]No Pis configured. Run 'add' or 'setup'.[/yellow]")
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


def _run_remove_select() -> None:
    """Select a Pi → confirm → remove it from PiManager."""
    global _config, _active_pi
    import click
    console = Console()
    selected = _select_pi("Select a Pi to remove")
    if not selected:
        return
    if not click.confirm(f"Remove '{selected}' from PiManager?"):
        console.print("[yellow]Cancelled.[/yellow]")
        return
    if remove_pi(_config, selected):
        if _active_pi == selected:
            _active_pi = None
        console.print(f"[green]Pi '{selected}' removed.[/green]")
    else:
        console.print(f"[red]Could not remove '{selected}'.[/red]")


def _run_rename_select() -> None:
    """Select a Pi → enter new name → change the REAL hostname + adopt it in the tool."""
    global _config, _active_pi
    console = Console()
    from .ssh import run_remote, SSHError, print_connection_label

    selected = _select_pi("Select a Pi to rename")
    if not selected:
        return
    try:
        new_name = prompt_with_exit("New name")
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    new_name = (new_name or "").strip()
    if not new_name:
        console.print("[yellow]No name entered.[/yellow]")
        return
    if new_name == selected:
        console.print("[dim]No change.[/dim]")
        return
    if new_name in _config.get("pis", {}):
        console.print(f"[red]A Pi named '{new_name}' already exists.[/red]")
        return

    # Change the real hostname on the Pi, then adopt the name in the tool.
    host_label = _hostname_label(new_name)
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        console.print(f"[cyan]Setting hostname to '{host_label}'...[/cyan]")
        _, stderr, code = run_remote(pi_cfg, f"sudo hostnamectl set-hostname {shlex.quote(host_label)}")
        if code != 0:
            console.print(f"[red]Failed to set hostname: {stderr}[/red]")
            console.print("[yellow]Renaming in the tool only.[/yellow]")
        else:
            # Keep /etc/hosts in sync so the new name resolves locally on the Pi.
            run_remote(pi_cfg, f"sudo sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{host_label}/' /etc/hosts")
            console.print(f"[green]Hostname set to '{host_label}'.[/green]")
    except SSHError as e:
        console.print(f"[red]Offline — {e}. Renaming in the tool only.[/red]")

    rename_pi(_config, selected, new_name)
    if _active_pi == selected:
        _active_pi = new_name
    console.print(f"[green]'{selected}' → '{new_name}'.[/green]")


def _run_reboot_select() -> None:
    """Select a Pi → reboot (with confirmation)."""
    console = Console()
    from .services import reboot_pi
    from .ssh import print_connection_label, SSHError
    selected = _select_pi("Select a Pi to reboot")
    if not selected:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        reboot_pi(pi_cfg)
    except SSHError as e:
        console.print(f"[red]Offline — {e}[/red]")


def _run_shutdown_select() -> None:
    """Select a Pi → shut down (with confirmation)."""
    console = Console()
    from .services import shutdown_pi
    from .ssh import print_connection_label, SSHError
    selected = _select_pi("Select a Pi to shut down")
    if not selected:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        shutdown_pi(pi_cfg)
    except SSHError as e:
        console.print(f"[red]Offline — {e}[/red]")


def _run_ssh_select() -> None:
    """Select a Pi → open an SSH session in a new terminal."""
    from .ssh import open_ssh_session
    selected = _select_pi("Select a Pi for SSH")
    if not selected:
        return
    pi_cfg = get_pi_config(_config, selected)
    open_ssh_session(pi_cfg)


def _run_upgrade_select() -> None:
    """Select a Pi → install OS/package updates, then restart its services."""
    global _config
    console = Console()
    from .services import upgrade_pi, detect_services, restart_service
    from .ssh import print_connection_label, SSHError
    selected = _select_pi("Select a Pi to upgrade")
    if not selected:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        if upgrade_pi(pi_cfg):
            # Refresh detected services and restart them so updates take effect.
            svcs = detect_services(pi_cfg)
            _config["pis"][selected]["services"] = svcs
            save_config(_config)
            if svcs:
                console.print(f"[cyan]Restarting services: {', '.join(svcs)}[/cyan]")
                for s in svcs:
                    restart_service(pi_cfg, s)
            console.print(f"[bold green]{selected} fully updated.[/bold green]")
    except SSHError as e:
        console.print(f"[red]Offline — {e}[/red]")


def _run_check() -> None:
    """Sweep ALL Pis: auto-detect services (save) and adopt real hostnames as tool names."""
    global _config, _active_pi
    console = Console()
    from .ssh import run_remote, SSHError, print_connection_label
    from .services import detect_services

    pi_names = get_pi_names(_config)
    if not pi_names:
        console.print("[yellow]No Pis configured.[/yellow]")
        return

    renames: list[tuple[str, str]] = []
    for name in list(pi_names):
        pi_cfg = get_pi_config(_config, name)
        console.print(f"\n[bold cyan]--- {name} ({pi_cfg['pi_host']}) ---[/bold cyan]")
        try:
            print_connection_label(pi_cfg)
            svcs = detect_services(pi_cfg)
            _config["pis"][name]["services"] = svcs
            console.print(f"[green]Services:[/green] {', '.join(svcs) or '-'}")

            # Refresh the Tailscale IP straight from the Pi (authoritative).
            ts_out, _, _ = run_remote(pi_cfg, "tailscale ip -4 2>/dev/null")
            ts_ip = ts_out.strip().splitlines()[0].strip() if ts_out.strip() else ""
            if ts_ip and ts_ip != _config["pis"][name].get("tailscale_host"):
                _config["pis"][name]["tailscale_host"] = ts_ip
                console.print(f"[cyan]Tailscale IP → {ts_ip} (updated)[/cyan]")
            elif ts_ip:
                console.print(f"[green]Tailscale IP:[/green] {ts_ip}")

            out, _, code = run_remote(pi_cfg, "hostname")
            real = out.strip()
            if real and real != name:
                if real in _config["pis"]:
                    console.print(f"[yellow]Hostname '{real}' already used as a name — keeping '{name}'.[/yellow]")
                else:
                    renames.append((name, real))
                    console.print(f"[cyan]Hostname '{real}' — adopting as tool name.[/cyan]")
        except SSHError as e:
            console.print(f"[red]Offline — {e}[/red]")

    # Apply renames after the sweep (avoid mutating the dict while iterating).
    for old, new in renames:
        rename_pi(_config, old, new)
        if _active_pi == old:
            _active_pi = new
    save_config(_config)
    console.print("\n[bold green]Check abgeschlossen.[/bold green]")


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


def _pick_service(pi_name: str, prompt_text: str, *, include_all: bool = False) -> str | None:
    """Pick a service on a Pi. Uses detected/known services, falls back to live detection."""
    console = Console()
    from .services import detect_services
    pi_cfg = get_pi_config(_config, pi_name)
    services = pi_cfg.get("services") or detect_services(pi_cfg)
    if not services:
        console.print(f"[yellow]No services found on {pi_name}.[/yellow]")
        return None
    items = [(s, s) for s in services]
    if include_all:
        items.append(("all", "all (restart all services)"))
    try:
        return numbered_select(items, prompt_text, allow_cancel=True)
    except UserExit:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return None


def _run_restart_select() -> None:
    """Pick a Pi → pick a service (or all) → restart."""
    console = Console()
    from .ssh import SSHError, print_connection_label
    from .services import restart_service, restart_all
    selected = _select_pi("Select a Pi")
    if not selected:
        return
    svc = _pick_service(selected, f"Restart service on {selected}", include_all=True)
    if not svc:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        if svc == "all":
            restart_all(pi_cfg)
        else:
            restart_service(pi_cfg, svc)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")


def _run_stop_select() -> None:
    """Pick a Pi → pick a service → stop."""
    console = Console()
    from .ssh import SSHError, print_connection_label
    from .services import stop_service
    selected = _select_pi("Select a Pi")
    if not selected:
        return
    svc = _pick_service(selected, f"Stop service on {selected}")
    if not svc:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        stop_service(pi_cfg, svc)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")


def _run_start_select() -> None:
    """Pick a Pi → pick a service → start."""
    console = Console()
    from .ssh import SSHError, print_connection_label
    from .services import start_service
    selected = _select_pi("Select a Pi")
    if not selected:
        return
    svc = _pick_service(selected, f"Start service on {selected}")
    if not svc:
        return
    pi_cfg = get_pi_config(_config, selected)
    try:
        print_connection_label(pi_cfg)
        start_service(pi_cfg, svc)
    except SSHError as e:
        console.print(f"[red]{e}[/red]")


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
        # Every action command follows the same principle: pick a Pi (and any
        # further input) interactively. Args after the command are ignored.
        static_handlers = {
            "setup": _run_setup,
            "update": _run_update,
            "uninstall": _run_uninstall,
            "add": _run_add_pi,
            "edit": _run_edit_pi,
            "use": _run_use_select,
            "remove": _run_remove_select,
            "rename": _run_rename_select,
            "restart": _run_restart_select,
            "stop": _run_stop_select,
            "start": _run_start_select,
            "reboot": _run_reboot_select,
            "shutdown": _run_shutdown_select,
            "upgrade": _run_upgrade_select,
            "ssh": _run_ssh_select,
            "check": _run_check,
        }
        interactive_handler = static_handlers.get(cmd)

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
