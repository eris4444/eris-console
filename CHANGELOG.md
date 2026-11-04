# Changelog

## 2.5.1
- Includes everything of 2.5.0 (backups of each part) and of 2.4.1, which 2.5.0 had left out.
- Temperatures panel: clock times under each chart, the time of every 24-hour maximum and when the history starts.
- Horizontal guide lines with their value at the left of every chart: the temperatures of the CPU and of each disk (amber = warm, red = hot, grey every 10 degrees) and the CPU usage and RAM charts in percent (amber and red at 60 / 80 % for the CPU, 70 / 90 % for the RAM).
- The temperature scale makes every row 5 degrees (2.5 / 10 / 20 on very tall or short panels), so the lines fall exactly on row edges: a disk at 31 always shows above the 30 line. Bars are green, amber or red by the row they reach.
- A minute without a sample (dashboard stopped) stays an empty column, so the clock under the chart lines up.

## 2.5.0
- **Backups of each part.** Besides the full backup, every part of the server gets an archive of its own, in a folder of its own under `BACKUP_DIR`: `marzban`, `x-ui`, `xray-site`, `jellyfin`, `eris`, `claude-f-web`, `system`. A part that is not on the server is skipped. The list of parts is one dictionary (`PARTS` in `erisbackup.py`).
- The full backup now lives in `BACKUP_DIR/full/` and never leaves the server. Archives of 2.4 in `BACKUP_DIR` move there once (on install, `eris backup now` or `eris backup list`).
- New settings: `BACKUP_FULL` (on), `BACKUP_PARTS` (all, or a list), `BACKUP_PART_KEEP` (7). Old copies are removed per folder.
- `eris backup now [--full | --parts | --part a,b]`, `eris backup list [--full | --part X]`, `eris backup status` (each part), `eris backup parts`, `eris restore --part X N`.
- Machine-readable output for eris-bot: `eris backup now|list|status|verify|parts --json`.
- `eris backup encrypt FILE --password-file F` / `eris backup decrypt`: AES-256-CBC, PBKDF2 with 200000 rounds and a salt (openssl; the password is read from a file, so `ps` never shows it).
- `backup-state.json` has a `parts` entry for each part; the keys at the top keep meaning the full backup, so the dashboard and its alerts work as before. The alert bar also shows a failed part (`backup marzban failed`).
- Menu item 13 (backup) has the new options.
- The dashboard collectors no longer stop when a `/sys` folder is missing (containers).
- `ERIS_ROOT` moves every path under another folder, for tests. Unit tests and a real install in a systemd container run on GitHub Actions (debian:trixie).
