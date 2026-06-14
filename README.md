# PiManager

A command-line tool for managing one or more Raspberry Pis from macOS. Check system health, control services, run OS upgrades, and SSH into your Pis — all from a single `pi` command with an interactive REPL.

Every action follows the same principle: **type a command → pick the Pi from a numbered list → answer any follow-up.** Nothing to memorize, no IDs to type.

## Features

- **Multi-Pi support** — manage several Raspberry Pis from one tool
- **Consistent selection** — every command shows a numbered Pi list (and service list where relevant); no name-typing required
- **Auto-detected services** — `status` and `check` discover the apps running on each Pi automatically; no manual service list to maintain
- **`check` sweep** — one command walks every Pi, refreshes its services, and adopts each Pi's real hostname as its name
- **Real renames** — `rename` changes the actual hostname on the Pi (`hostnamectl` + `/etc/hosts`), not just the label in the tool
- **Full OS upgrades** — `upgrade` runs `apt full-upgrade` + cleanup and restarts the Pi's services
- **Tailscale support** — connects via LAN at home or Tailscale VPN when away
- **Interactive REPL** — type `pi` for a persistent shell with tab-completion and history
- **One-shot CLI** — run `pi status`, `pi restart`, etc. directly for scripting
- **Self-updating** — `pi update` pulls the latest version and reinstalls
- **SSH** — opens in a new Terminal.app window so the REPL keeps running
- **Setup wizard** — generates SSH keys, copies them to the Pi, tests the connection

## Requirements

- macOS (uses Terminal.app for SSH windows)
- Python 3.10+
- [pipx](https://pypa.github.io/pipx/) (recommended) or pip
- One or more Raspberry Pis with SSH enabled

## Installation

```bash
# Install pipx if you don't have it
brew install pipx

# Clone the repo
git clone https://github.com/Werizu/Pi-WebHost-Manager.git
cd Pi-WebHost-Manager

# Install
pipx install .
```

The `pi` command is now available globally in your terminal.

## Quick start

```bash
pi
```

On first run a short setup wizard walks you through:

1. Pi name (e.g. `homepi`, `mediaserver`)
2. IP address / hostname and username
3. SSH key generation (stored in `~/.pi-manager/keys/`, not in the project directory)
4. Copying the public key to your Pi
5. Tailscale IP (optional — for remote access via VPN)
6. Option to add more Pis

You don't enter a service list anymore — services are detected automatically the first time you run `status` or `check`. Re-run the wizard anytime with `setup` (REPL) or `pi setup` (CLI).

## Usage

### Interactive mode (REPL)

Run `pi` with no arguments to enter the interactive shell:

```
$ pi
  PiManager  v0.4.1
  2 Pis: homepi(192.168.1.100) · mediaserver(192.168.1.101)

  Type help for commands, exit to quit
─────────────────────────────────────

pi > status              # all Pis: health + auto-detected services
pi > check               # sweep all Pis: refresh services + adopt hostnames
pi > restart             #   1) homepi (192.168.1.100)
                         #   2) mediaserver (192.168.1.101)
                         # > 1   → then pick a service (or "all")
pi > rename              # pick a Pi → enter new name → real hostname changes
pi > upgrade             # pick a Pi → apt full-upgrade + restart services
pi > use                 # pick the default Pi (persists)
pi > update              # self-update from git
pi > exit
```

Features:
- **Tab completion** for all commands
- **Command history** persisted in `~/.pi-manager/history` (arrow keys to navigate)
- **Styled prompt** powered by prompt\_toolkit

### One-shot mode (CLI)

Pass a command directly for scripting or quick use:

```bash
pi status                     # all Pis (health + services)
pi status --pi homepi         # one specific Pi
pi check                      # sweep all Pis
pi restart                    # numbered Pi → service selection
pi restart apache2 --pi homepi   # restart a specific service directly
pi restart all --pi homepi    # restart all of a Pi's services
pi stop nginx --pi homepi
pi start nginx --pi homepi
pi upgrade                    # full OS upgrade on all Pis + restart services
pi upgrade --pi homepi        # one Pi only
pi ssh --pi homepi            # open SSH in a new Terminal window
pi shutdown --pi homepi
pi reboot --pi homepi
pi list                       # list configured Pis
pi add                        # add a new Pi interactively
pi remove homepi              # remove a Pi from the tool
pi rename homepi mainpi       # change the real hostname + the tool name
pi edit                       # edit host/user/key/Tailscale interactively
pi use homepi                 # set the default Pi
pi tailscale list             # show LAN/Tailscale IPs and connection mode
pi tailscale set homepi 100.64.0.1
pi tailscale remove homepi
pi update                     # self-update from git
```

In the REPL the action commands (`restart`, `stop`, `start`, `upgrade`, `ssh`, `shutdown`, `reboot`, `remove`, `rename`, `edit`, `use`) always prompt for the Pi via a numbered list. In one-shot CLI mode they take `--pi <name>` (and an optional service argument for `restart`/`stop`/`start`).

### Commands

| Command | Description |
|---|---|
| `status` | System health (CPU, RAM, disk, temp, uptime) + auto-detected services for all Pis (or `--pi <name>`) |
| `check` | Sweep **all** Pis: refresh detected services and adopt each Pi's real hostname as its name |
| `restart` | Pick a Pi → pick a service (or `all`) → restart |
| `restart all` | Restart all of a Pi's services |
| `stop` | Pick a Pi → pick a service → stop |
| `start` | Pick a Pi → pick a service → start |
| `upgrade` | Pick a Pi → `apt full-upgrade` + autoremove, then restart its services |
| `ssh` | Pick a Pi → open SSH in a new Terminal.app window |
| `shutdown` | Pick a Pi → shut down (asks for confirmation) |
| `reboot` | Pick a Pi → reboot (asks for confirmation) |
| `list` | List all configured Pis |
| `add` | Add a new Pi interactively |
| `remove` | Pick a Pi → remove it from the tool (asks for confirmation) |
| `rename` | Pick a Pi → new name → changes the **real hostname** (`hostnamectl` + `/etc/hosts`) and the tool name |
| `edit` | Pick a Pi → edit host, user, SSH key path, or Tailscale IP |
| `use` | Pick a Pi → set as default (persists to config) |
| `tailscale list` | Show LAN/Tailscale IPs and current connection mode for all Pis |
| `tailscale set` | Set Tailscale IP for a Pi |
| `tailscale remove` | Remove Tailscale IP from a Pi |
| `setup` | Re-run the setup wizard |
| `update` | Update PiManager to the latest version from git |
| `uninstall` | Remove config and uninstall PiManager |
| `help` | Show all available commands |
| `clear` | Clear the output (REPL) |
| `exit` / `quit` | Exit the REPL |

`status` and `check` cover all Pis at once (or use `--pi <name>` to scope `status` to one). All other actions target a single Pi — chosen from the numbered list in the REPL, or via `--pi <name>` in the CLI.

## How service detection works

There is no manual service list to maintain. When you run `status` or `check`, PiManager queries the Pi for its **enabled** systemd services and filters out base-OS/systemd noise (a denylist in `services.py`), leaving the apps you actually run (e.g. `docker`, `apache2`, `mariadb`, `market-intel`). The result is saved back to the config so `restart`/`stop`/`start` can offer it. Install a new app and it shows up automatically on the next `status`/`check`.

## Configuration

All config and keys are stored in `~/.pi-manager/`:

```
~/.pi-manager/
├── config.json     # Main configuration
├── keys/           # SSH keys (generated during setup)
│   ├── id_rsa
│   └── id_rsa.pub
└── history         # REPL command history
```

Example `config.json`:

```json
{
  "pis": {
    "homepi": {
      "host": "192.168.1.100",
      "user": "pi",
      "ssh_key_path": "~/.pi-manager/keys/id_rsa",
      "services": ["docker", "apache2"],
      "tailscale_host": "100.64.0.1"
    },
    "mediaserver": {
      "host": "192.168.1.101",
      "user": "pi",
      "ssh_key_path": "~/.pi-manager/keys/id_rsa",
      "services": ["plex", "samba"],
      "tailscale_host": "100.64.0.2"
    }
  },
  "default_pi": "homepi"
}
```

- **`host`** — LAN IP / hostname used at home
- **`user`** / **`ssh_key_path`** — SSH credentials
- **`services`** — filled automatically by `status` / `check`; you normally don't edit this
- **`tailscale_host`** (optional) — Tailscale IP for remote access when off the home network

You can edit the file directly or use `pi setup` / `pi add` / `pi edit`.

## Project structure

```
pi-manager/
├── pi_manager/
│   ├── __init__.py
│   ├── cli.py          # Click entry point, routes to REPL or one-shot commands
│   ├── repl.py         # Interactive REPL (prompt_toolkit + rich)
│   ├── config.py       # Config loading, saving, setup wizard, multi-Pi helpers
│   ├── ssh.py          # SSH connection, remote commands, Terminal.app integration
│   ├── monitor.py      # System status + service status display
│   └── services.py     # Service control, auto-detection, OS upgrade, power
├── pyproject.toml
└── README.md
```

## Dependencies

| Package | Purpose |
|---|---|
| [click](https://click.palletsprojects.com/) | CLI framework and one-shot command parsing |
| [rich](https://rich.readthedocs.io/) | Tables and styled terminal output |
| [paramiko](https://www.paramiko.org/) | SSH connections and remote command execution |
| [prompt\_toolkit](https://python-prompt-toolkit.readthedocs.io/) | Interactive REPL with history and tab-completion |

## FAQ

**Where is the config stored?**
`~/.pi-manager/config.json`. Edit it directly or use `pi setup` / `pi add` / `pi edit`.

**Where are the SSH keys stored?**
In `~/.pi-manager/keys/`, kept out of any project directory so they can't accidentally be committed to git.

**Do I need to tell it which services to monitor?**
No. `status` and `check` detect the enabled application services on each Pi automatically and remember them.

**What's the difference between `status` and `check`?**
`status` shows health + services for the Pis you look at. `check` is the maintenance sweep: it goes through every Pi, refreshes the detected services, and adopts each Pi's real hostname as its name in the tool (handy after you've renamed Pis or set them up fresh).

**Does `rename` change the actual hostname?**
Yes. It runs `sudo hostnamectl set-hostname` and updates `/etc/hosts` on the Pi, then adopts the new name in the tool. If the Pi is unreachable it renames in the tool only and tells you.

**Does `upgrade` update the Pi's OS?**
Yes — it runs `apt-get update`, `apt-get full-upgrade -y` (so packages needing new dependencies, like kernel/firmware bumps, are installed rather than held back), and `apt-get autoremove -y`, then restarts the Pi's detected services. It does **not** run `rpi-update` (bleeding-edge firmware) and does **not** reboot automatically — use `reboot` after a kernel update.

**Can I manage multiple Pis?**
Yes. Run `pi add` anytime, or add more during `pi setup`. `pi list` shows them all. `use` sets the default Pi and persists it.

**How does Pi resolution work in one-shot CLI mode?**
Explicit `--pi <name>` wins; otherwise the `default_pi` from the config is used. In the REPL, action commands always show a numbered Pi list instead.

**How does Tailscale support work?**
PiManager detects whether you're on your home network. At home it uses the Pi's LAN IP; away, it uses the Tailscale IP if configured. Each operation shows the connection method once (e.g. `-> LAN (192.168.178.201)` or `-> Tailscale (100.64.0.1)`). Set Tailscale IPs during setup, via `pi edit`, or with `pi tailscale set <pi> <ip>`.

**Connection issues?**
- *"Can't reach the Pi"* — check it's powered on and the IP is correct
- *"SSH key rejected"* — run `pi setup` to regenerate and recopy the key
- *"Connection timed out"* — verify your network connection

## Updating

The easiest way to update:

```bash
pi update
```

This pulls the latest changes from git, reinstalls via pipx, and shows a changelog. Your config and SSH keys in `~/.pi-manager/` are never touched. On first run, `update` asks for the path to your local git clone and remembers it.

Manual update (same thing, by hand):

```bash
cd Pi-WebHost-Manager
git pull
pipx install . --force
```

## Uninstalling

From the REPL:
```
pi > uninstall
```

Or manually:
```bash
pipx uninstall pi-manager
rm -rf ~/.pi-manager
```

## License

MIT
