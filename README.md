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
    - Clawd, a pixel mascot that walks around and reacts to load.
  - The dashboard uses the top part of tty1 (scroll region), so the shell below keeps working.
  - Full-screen programs (vim, htop, less, man, …) get the whole screen while they run.
  - Extra width or height spreads the text and grows the charts.

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

Options: `--no-selftest`, `--uninstall`.

## Use

```
eris                      settings menu
eris on | off | toggle    start / stop the dashboard now
eris enable | disable     dashboard at boot (and start / stop it now)
eris status               what is running and how the console is set up
eris set KEY VALUE        change a setting (eris config lists them)
eris font [list | set NAME|default | current]
eris apply                re-apply the console look
eris render [ROWSxCOLS] [utf8|safe|ascii]   print one dashboard frame here
eris selftest             quick checks of the installed files
```

Settings are in `/etc/eris/eris.conf`:

| Setting | What it controls |
| --- | --- |
| `DASH_HEIGHT` | dashboard height, in % of the rows |
| `REFRESH` | refresh interval |
| `WEATHER`, `WEATHER_CITY`, `WEATHER_UNITS` | weather panel on/off, the city (empty = detect by IP), C or F |
| `PALETTE` | `neon` or `default` |
| `CONSOLE_FONT` | the console font |
| `KERNEL_MSGS` | whether kernel messages are hidden from the console |
| `PROMPT_SCOPE` | `tty1` or `all` |
| `ERIS_GLYPHS` | the extra font glyphs (mascot, weather icons, sparklines) |

### Claude / Codex usage panel
- **Claude:** read-only, from Claude Code's own login (`/root/.claude/.credentials.json`). The token is never refreshed or changed.
- **Codex:** from Codex's session logs (`~/.codex/sessions`).
- **Proxy:** requests go through `/etc/eris/ai-proxy` when that file contains a proxy URL.
- **Not logged in:** the panel shows a hint instead.

## Remove

```bash
sudo eris-uninstall
```

This removes the files, services and shell blocks, and restores the original console settings.

## Build from source

`src/` holds the sources. `src/build.sh` assembles them into the single `install-eris.sh`:

```bash
bash src/build.sh ./install-eris.sh
```
