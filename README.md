# ERIS Console

A neon look for the physical console (tty1) of a headless Debian server, plus an optional live dashboard that runs on the top part of the screen.

- **Layer A: the look (always on)**
  - Black console with the ERIS 16-colour neon palette.
  - A one-line prompt, for example `ERIS root@host /path git:main ➜`.
  - Kernel messages are kept off the console; they stay in `dmesg` and the journal.
  - A font fixed for HDMI/KMS consoles.
- **Layer B: the dashboard (`eris-dashboard.service`, OFF by default)**
  - Panels:
    - CPU, RAM, disks (SMART), network and temperatures, with history charts;
    - weather for the coming hours and days (Open-Meteo, no API key);
    - Claude and Codex plan usage: 5-hour and weekly windows with reset times;
    - temperatures of the CPU and every disk: now, 24-hour min/max and a 24-hour chart (one column per minute, kept across restarts);
    - Marzban, when it runs on the server: the users connected right now (traffic in the last 2 minutes) with their names, users online in 24 h, today's traffic, live speed, Xray version and every node;
    - Clawd, a pixel mascot that walks around and reacts to load.
  - An alert bar under the header: a hot CPU or disk, SMART failed or unreadable, a filesystem over 90%, a failed service, a Marzban node that is not connected, a backup that failed or is too old.
  - The dashboard uses the top part of tty1 (scroll region), so the shell below keeps working.
  - Full-screen programs (vim, htop, less, man, …) get the whole screen while they run.
  - Extra width or height spreads the text and grows the charts.
  - Full screen: `eris full` (or **Alt+F** at the console prompt) gives the dashboard the whole screen but the last 2 rows, where the shell stays; again for the split view.
- **Firewall (`eris fw`, optional, OFF unless you set it up)**
  - One readable file, `/etc/eris/firewall.conf`: LAN networks, open ports with connection limits, allowed and blocked addresses.
  - Every change is checked with `nft -c`, backed up, and rolled back after 60 seconds unless you confirm it.
  - Localhost and SSH from the LAN always keep working. Only its own nftables table is touched, never Docker's.
- **Backups (`eris backup`, nightly once a folder is set)**
  - Settings and data of the server in one archive, with consistent copies of the live SQLite databases (Marzban, x-ui, Jellyfin).
  - The newest `BACKUP_KEEP` archives are kept; `eris restore` puts one back (all of it, or only some paths).

Everything is plain Python 3 and bash with no pip packages. The installer is one self-contained file.

## Install

```bash
curl -fsSLO https://github.com/eris4444/eris-console/releases/latest/download/install-eris.sh
sudo bash install-eris.sh
```

The installer:
- needs Debian, run as root;
- installs only missing packages (`python3 kbd console-setup smartmontools iproute2 procps util-linux pciutils`) and never upgrades anything;
- backs up every file it touches to `/var/backups/eris/<time>/`.

Options:
- `--firewall`: also set up the firewall (`eris fw setup`; asks what to open when run in a terminal);
- `--no-selftest`, `--uninstall`.

An upgrade never reloads or restarts an existing firewall. An older hand-written `/etc/eris/guard.nft` is taken over with `eris fw import`, but only if the result gives exactly the same rules.

## Use

```
eris                      settings menu
eris on | off | toggle    start / stop the dashboard now
eris full [on|off|toggle] full screen dashboard (Alt+F at the console prompt)
eris enable | disable     dashboard at boot (and start / stop it now)
eris status               what is running and how the console is set up
eris set KEY VALUE        change a setting (eris config lists them)
eris font [list | set NAME|default | current]
eris apply                re-apply the console look
eris render [ROWSxCOLS] [utf8|safe|ascii]   print one dashboard frame here
eris fw ...               the firewall (see below; eris fw help)
eris backup [now|list|status|verify N]   a backup now, the list, the last result
eris restore N|FILE [--to DIR] [--only PATH ...] [--yes]   put a backup back
eris selftest             quick checks of the installed files
eris update [--check]     install the latest release from this repository (settings stay)
```

Settings are in `/etc/eris/eris.conf`:

| Setting | What it controls |
| --- | --- |
| `DASH_HEIGHT` | dashboard height, in % of the rows |
| `DASH_MODE` | `split` (dashboard on top, shell below) or `full` (all rows but 2) |
| `REFRESH` | refresh interval |
| `WEATHER`, `WEATHER_CITY`, `WEATHER_UNITS` | weather panel on/off, the city (empty = detect by IP), C or F |
| `PALETTE` | `neon` or `default` |
| `CONSOLE_FONT` | the console font |
| `KERNEL_MSGS` | whether kernel messages are hidden from the console |
| `PROMPT_SCOPE` | `tty1` or `all` |
| `ERIS_GLYPHS` | the extra font glyphs (mascot, weather icons, sparklines) |
| `MARZBAN` | `auto` = Marzban panel when `/opt/marzban/.env` exists, `off` = hidden |
| `ALERTS`, `ALERT_CPU_TEMP`, `ALERT_DISK_TEMP` | the alert bar on/off and its temperature limits (80 °C / 55 °C) |
| `BACKUP_DIR`, `BACKUP_AT`, `BACKUP_KEEP`, `BACKUP_OWNER` | nightly backup folder (empty = off), time, how many to keep, owner of the files |

### Backups
`eris set BACKUP_DIR /srv/disk2/backups` turns the nightly backup on (`eris-backup.timer`, at `BACKUP_AT`).
- **An archive** (`<host>-backup-YYYYmmdd-HHMM.tar`, mode 600) holds:
  - `files.tar.gz`: the settings and data folders that exist on the server, such as `/etc/eris`, `/var/lib/marzban`, `/opt/marzban*`, `/etc/x-ui`, `/etc/xray-site`, `/root/.acme.sh`, `/appdata`, `/config/jellyfin`, `/etc/openmediavault`, `/etc/systemd/system`; caches and logs are left out;
  - `databases/`: copies made with SQLite's own backup, checked with `integrity_check`;
  - `manifest.json` and `RESTORE.txt`.
- **Checks:** a `.sha256` file is written next to each archive, and the archive is read back after writing.
- **Restore:** `eris restore 1` puts the newest backup back:
  - first it backs up the present state (`…-before-restore.tar`);
  - it stops Marzban, x-ui and Jellyfin while their databases come back, then starts them again;
  - `--to DIR` only unpacks into a folder, and `--only /etc/x-ui` restores just that path.
- **Secrets:** an archive holds passwords and private keys. `BACKUP_OWNER` gives the files to one user, for example to read them over SMB.

### Marzban panel
It logs in to `https://127.0.0.1:<UVICORN_PORT>` as the `.env` admin (`SUDO_USERNAME` / `SUDO_PASSWORD`) every 30 s and reads `/api/system`, `/api/nodes`, `/api/core` and today's `/api/nodes/usage`. Nothing is changed in Marzban.

### Claude / Codex usage panel
- **Claude:** read-only, from Claude Code's own login (`/root/.claude/.credentials.json`). The token is never refreshed or changed.
- **Codex:** from Codex's session logs (`~/.codex/sessions`).
- **Proxy:** requests go through `/etc/eris/ai-proxy` when that file contains a proxy URL.
- **Not logged in:** the panel shows a hint instead.

## Firewall (optional)

`eris fw setup` (or `install-eris.sh --firewall`) writes `/etc/eris/firewall.conf` from the server's own networks. From then on, `eris-guard.service` loads the rules at boot. The rules live in an nftables table of their own, `inet eris_guard`.

```
lan 192.168.1.0/24            # full access: LAN and Docker networks (IPv4 or IPv6)
lan 172.16.0.0/12
open 443/tcp limit 300        # reachable from the Internet, at most 300 parallel connections
open 51820/udp
allow 203.0.113.7             # full access for one Internet address
deny 198.51.100.0/24          # blocked (SSH from the LAN still works)
limit-per total               # total: a limit is shared by all addresses; address: one for each address
docker lan-only               # Docker-published ports only from the lan networks
ping on
```

| Command | What it does |
| --- | --- |
| `eris fw` | status: open ports, LAN networks and custom rules (numbered), drop counters, current connections |
| `eris fw open PORT[/udp] [limit N]` / `close PORT[/udp]` | open a port to the Internet / close it |
| `eris fw allow ADDR[/PREFIX]` / `deny ADDR[/PREFIX]` | full access / block an address or network |
| `eris fw remove N` | remove entry N of the status list |
| `eris fw limit PORT N` | parallel connection limit (0 = none) |
| `eris fw set limit-per\|docker\|ping VALUE` | the options |
| `eris fw reload` | load `firewall.conf` after editing it by hand |
| `eris fw confirm` | keep a change (Enter does the same when you run it in a terminal) |
| `eris fw off [MINUTES]` / `on` | switch it off (for a while, or until `on` / the next boot) / on again |
| `eris fw backup` / `restore [N]` | save the settings / bring a saved version back |

The `eris` menu has the same options under **Firewall**: you pick everything by number.

Safety:
- **Before loading:** every change is checked with `nft -c` and backed up to `/var/backups/eris/fw-*`.
- **Rollback:** a change that nobody confirms within 60 seconds is rolled back (Enter, `n` = undo at once). New rules exist only in `/run` until you confirm them, so a reboot also brings back the old ones.
- **SSH:** localhost and SSH from the LAN are always allowed, even from a blocked LAN address. `eris fw` refuses any change that would block the SSH session you are connected from, unless you explicitly agree.
- **Explicit consent:** opening SSH to the Internet needs `--open-ssh` (or typing `open ssh` in a terminal). Admin panels (x-ui, Cockpit, Webmin, Portainer, openmediavault), file sharing, databases and very large `allow` ranges need `--force` (or typing `yes`).
- **Warnings:** you get one for a port nothing listens on, a port that only listens on localhost, or a Docker port.
- **Outside changes:** changes made to the live table outside `eris fw` (for example `nft add element` by hand) are shown, and replacing them needs a confirmation.

## Remove

```bash
sudo eris-uninstall
```

This removes the files, services and shell blocks, and restores the original console settings. The firewall table `inet eris_guard` and `eris-guard.service` are removed too. A copy of `firewall.conf` stays in `/var/backups/eris/fw-uninstall-*`.

## Build from source

`src/` holds the sources. `src/build.sh` assembles them into the single `install-eris.sh`:

```bash
bash src/build.sh ./install-eris.sh
```
