# PiManager

A command-line tool for managing a Raspberry Pi server from macOS. Deploy websites, monitor system health, restart services, view logs, and SSH into your Pi — all from a single `pi` command with an interactive REPL.

## Features

- **Interactive REPL** — type `pi` and stay in a persistent shell with tab-completion and command history
- **One-shot CLI** — run `pi status` or `pi deploy my-site` directly from your terminal for scripting
- **System monitoring** — CPU, RAM, disk, temperature, uptime at a glance
- **Service management** — check status, restart individual services or all at once
- **Log viewing** — tail Apache error logs, with optional real-time streaming
- **One-command deploys** — rsync to Pi, restart Apache, purge Cloudflare cache
- **SSH** — opens in a new Terminal.app window so the REPL stays running
- **Setup wizard** — generates SSH keys, copies them to the Pi, tests the connection

## Requirements

- macOS (uses Terminal.app for SSH windows)
- Python 3.10+
- [pipx](https://pypa.github.io/pipx/) (recommended) or pip
- A Raspberry Pi with SSH enabled

## Installation

```bash
# Install pipx if you don't have it
brew install pipx

# Clone the repo (use SSH if you have it set up)
git clone https://github.com/Werizu/Pi_Manager.git
# or: git clone git@github.com:Werizu/pi-manager.git
cd Pi_Manager

# Install
pipx install .
```

The `pi` command is now available globally in your terminal.

## Quick start

```bash
pi
```

On first run a setup wizard walks you through:

1. Pi IP address / hostname and username
2. SSH key generation (stored safely in `~/.pi-manager/keys/`, not in the project directory)
3. Copying the public key to your Pi
4. Which services to monitor (defaults: `apache2`, `mariadb`, `cloudflared`)
5. Cloudflare API token (optional, for cache purging on deploy)
6. Deploy projects (local path, remote path, Cloudflare zone)

The wizard tests the SSH connection and tells you if something's wrong. Re-run anytime with `setup` (REPL) or `pi setup` (CLI).

## Usage

### Interactive mode (REPL)

Run `pi` with no arguments to enter the interactive shell:

```
$ pi
╭───────────────────────────────────────╮
│ PiManager  v0.1.0                     │
│ Connected to pi@192.168.1.100         │
╰───────────────────────────────────────╯
Type help for commands, exit to quit.

pi > status
pi > deploy my-site
pi > ssh
pi > exit
```

Features:
- **Tab completion** for all commands
- **Command history** persisted in `~/.pi-manager/history` (arrow keys to navigate)
- **Styled prompt** powered by prompt\_toolkit

### One-shot mode (CLI)

Pass a command directly for scripting or quick use:

```bash
pi status
pi deploy my-site
pi services
```

### Commands

| Command | Description |
|---|---|
| `status` | Show Pi system status (CPU, RAM, disk, temp, uptime) |
| `services` | Show status of all monitored services |
| `logs` | Show last 30 lines of Apache error log |
| `logs -n 100` | Show more lines |
| `logs --live` | Stream logs in real-time (Ctrl+C to stop) |
| `restart <service>` | Restart a single service |
| `restart all` | Restart all monitored services |
| `ssh` | Open SSH in a new Terminal.app window |
| `deploy <name>` | Rsync project to Pi + restart Apache + purge Cloudflare cache |
| `config` | Show current configuration |
| `project add` | Add a new deploy project interactively |
| `project list` | List all configured projects |
| `project remove <name>` | Remove a project |
| `setup` | Re-run the setup wizard |
| `shutdown` | Shut down the Pi (asks for confirmation) |
| `reboot` | Reboot the Pi (asks for confirmation) |
| `uninstall` | Remove config and uninstall PiManager |
| `help` | Show all available commands |
| `exit` / `quit` | Exit the REPL |

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
  "pi_host": "192.168.1.100",
  "pi_user": "pi",
  "ssh_key_path": "~/.pi-manager/keys/id_rsa",
  "cloudflare_api_token": "",
  "services": ["apache2", "mariadb", "cloudflared"],
  "projects": {
    "my-site": {
      "local_path": "/Users/you/Sites/my-site/",
      "remote_path": "/var/www/my-site/",
      "cloudflare_zone_id": "abc123..."
    }
  }
}
```

You can edit the file directly or use `pi setup` / `pi project add`.

## Deploy workflow

When you run `deploy <name>`, PiManager:

1. **Rsyncs** the local project folder to the Pi (excludes `.git`, `.DS_Store`, `node_modules`)
2. **Restarts Apache** on the Pi
3. **Purges Cloudflare cache** (if a zone ID and API token are configured)

## Project structure

```
pi-manager/
├── pi_manager/
│   ├── __init__.py
│   ├── cli.py          # Click entry point, routes to REPL or one-shot
│   ├── repl.py         # Interactive REPL (prompt_toolkit + rich)
│   ├── config.py       # Config loading, saving, setup wizard
│   ├── ssh.py          # SSH connection, remote commands, Terminal.app integration
│   ├── monitor.py      # System status, services, log viewing
│   ├── services.py     # Service restart, shutdown, reboot
│   └── deploy.py       # Rsync deploy + Cloudflare cache purge
├── pyproject.toml
└── README.md
```

## Dependencies

| Package | Purpose |
|---|---|
| [click](https://click.palletsprojects.com/) | CLI framework and one-shot command parsing |
| [rich](https://rich.readthedocs.io/) | Tables, panels, styled terminal output |
| [paramiko](https://www.paramiko.org/) | SSH connections and remote command execution |
| [prompt\_toolkit](https://python-prompt-toolkit.readthedocs.io/) | Interactive REPL with history and tab-completion |
| [requests](https://requests.readthedocs.io/) | Cloudflare API calls |

## FAQ

**Where is the config stored?**
`~/.pi-manager/config.json`. Edit it directly or use `pi setup` / `pi project add`.

**Where are the SSH keys stored?**
In `~/.pi-manager/keys/`. This keeps them out of any project directory so they can't accidentally be committed to git.

**How do I change my SSH key path?**
Run `pi setup` to reconfigure, or edit `ssh_key_path` in the config file.

**How do I get a Cloudflare API token?**
Go to the [Cloudflare dashboard](https://dash.cloudflare.com/profile/api-tokens) and create a token with **Zone > Cache Purge > Purge** permission. Add it during setup or edit the config file.

**Can I deploy multiple websites?**
Yes. Run `project add` for each site, then `deploy <name>` to deploy individually.

**How do I monitor custom services?**
Enter your services as a comma-separated list during setup, or edit the `services` array in the config file.

**SSH opens in a new window — can I use the current terminal instead?**
In one-shot mode (`pi ssh`) SSH also opens in a new Terminal.app window. This is by design so the REPL stays usable. For an inline session, run `ssh -i ~/.pi-manager/keys/id_rsa pi@your-pi-ip` directly.

**Connection issues?**
- *"Can't reach the Pi"* — check that the Pi is powered on and the IP is correct
- *"SSH key rejected"* — run `pi setup` to regenerate and recopy the key
- *"Connection timed out"* — verify your network connection

## Updating

```bash
cd Pi_Manager
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
