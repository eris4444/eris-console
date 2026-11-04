#!/bin/bash
# ERIS Console installer (self-contained).
#   Layer A: neon black console + one-line prompt on the physical console (tty1), always on.
#   Layer B: eris-dashboard.service, installed but OFF (start it with: eris on).
#   Firewall (optional, OFF unless asked for): eris fw, nftables table inet eris_guard.
#
#   bash install-eris.sh                 install or upgrade (an existing firewall is kept as it is)
#   bash install-eris.sh --firewall      also set up the firewall on this server (eris fw setup)
#   bash install-eris.sh --uninstall     remove everything (same as eris-uninstall)
#   bash install-eris.sh --no-selftest   skip the checks at the end
set -euo pipefail
VERSION=2.5.0
LOG=/var/log/eris-build.log
TS=$(date +%Y%m%d-%H%M%S)
BK=/var/backups/eris/$TS
say() { printf '\033[1;35m[ERIS]\033[0m %s\n' "$*"; printf '%s [install] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
die() { say "ERROR: $*"; exit 1; }

put_file() {  # DEST MODE < content
  local dest=$1 mode=$2
  install -d -m 0755 "$(dirname "$dest")"
  cat > "$dest.eris-new"
  chmod "$mode" "$dest.eris-new"
  mv -f "$dest.eris-new" "$dest"
}

#@@FILES@@

[ "$(id -u)" -eq 0 ] || die "run as root"
SELFTEST=1
FIREWALL=0
for a in "$@"; do
  case $a in
    --uninstall)
      if [ -x /usr/local/sbin/eris-uninstall ]; then exec /usr/local/sbin/eris-uninstall; fi
      tmp=$(mktemp)
      write_uninstaller "$tmp"
      bash "$tmp"; rc=$?; rm -f "$tmp"; exit $rc ;;
    --firewall) FIREWALL=1 ;;
    --no-selftest) SELFTEST=0 ;;
    -h | --help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown option $a" ;;
  esac
done

[ -r /etc/debian_version ] || die "this installer is for Debian"
say "ERIS Console $VERSION installer, log: $LOG"

# 1. packages: install only what is missing, never upgrade
need=()
for p in python3 kbd console-setup console-setup-linux smartmontools iproute2 procps util-linux pciutils; do
  dpkg-query -W -f='${Status}' "$p" 2> /dev/null | grep -q "install ok installed" || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
  say "installing missing packages: ${need[*]}"
  if ! DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1; then
    DEBIAN_FRONTEND=noninteractive timeout 600 apt-get update -q >> "$LOG" 2>&1 || true
    DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1 \
      || die "apt-get install ${need[*]} failed (see $LOG)"
  fi
else
  say "packages: all present (python3 kbd console-setup smartmontools iproute2 procps util-linux pciutils)"
fi

# 2. backup of everything this installer may touch
mkdir -p "$BK"
for p in /root/.bashrc /root/.profile /etc/default/console-setup /etc/eris /usr/local/bin/eris /usr/local/lib/eris \
         /usr/local/sbin/eris-uninstall /etc/systemd/system/eris-console.service \
         /etc/systemd/system/eris-dashboard.service /etc/systemd/system/eris-guard.service \
         /etc/systemd/system/eris-backup.service /etc/systemd/system/eris-backup.timer \
         /etc/sysctl.d/90-eris-console.conf /var/lib/eris/firewall.applied; do
  if [ -e "$p" ]; then
    mkdir -p "$BK$(dirname "$p")"
    cp -a "$p" "$BK$p"
  fi
done
say "backup: $BK"

# 3. an upgrade keeps the dashboard state; a fresh install leaves it OFF
was_on=0
if systemctl is-active --quiet eris-dashboard.service 2> /dev/null; then
  was_on=1
  systemctl stop eris-dashboard.service || true
fi

# 4. files
write_files
if [ ! -e /etc/eris/eris.conf ]; then
  python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris"); import erislib as E; open(E.CONF, "w").write(E.default_conf_text())'
  chmod 0644 /etc/eris/eris.conf
  say "settings: /etc/eris/eris.conf (defaults)"
else
  python3 - << 'PY'
import sys
sys.path.insert(0, "/usr/local/lib/eris")
import erislib as E
conf = E.load_conf()
have = open(E.CONF).read()
missing = {k: v for k, v in conf.items() if ("\n%s=" % k) not in "\n" + have}
if missing:
    E.save_conf(missing)
PY
  say "settings: kept /etc/eris/eris.conf"
fi
say "files: /usr/local/bin/eris, /usr/local/lib/eris/, /etc/eris/prompt.bash, eris-console.service, eris-dashboard.service, eris-guard.service, /usr/local/sbin/eris-uninstall"

# 5. managed blocks in root's shell files (only these lines are ever touched)
add_block() {  # FILE TEXT
  local f=$1
  [ -e "$f" ] || : > "$f"
  sed -i '/^# >>> ERIS CONSOLE/,/^# <<< ERIS CONSOLE/d' "$f"
  printf '%s\n' "$2" >> "$f"
}
add_block /root/.bashrc '# >>> ERIS CONSOLE (managed by install-eris.sh, removed by eris-uninstall) >>>
[ -r /etc/eris/prompt.bash ] && . /etc/eris/prompt.bash
# <<< ERIS CONSOLE <<<'
add_block /root/.profile '# >>> ERIS CONSOLE (managed by install-eris.sh, removed by eris-uninstall) >>>
# physical console only: re-apply the ERIS look (font, palette, blanking) at login
if [ -z "${SSH_CONNECTION-}" ] && [ -x /usr/local/bin/eris ]; then /usr/local/bin/eris apply --login 2> /dev/null; fi
# <<< ERIS CONSOLE <<<'
say "shell: ERIS blocks in /root/.bashrc and /root/.profile"

# 6. services: the look unit now and at boot; the dashboard stays as it was (OFF on a fresh install)
# nightly backup timer at BACKUP_AT (the backup itself does nothing while BACKUP_DIR is empty)
python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris"); import erislib as E, erisbackup as B
open("/etc/systemd/system/eris-backup.timer", "w").write(B.timer_text(E.load_conf()["BACKUP_AT"]))'
systemctl daemon-reload
systemctl enable --now eris-backup.timer > /dev/null 2>&1 || say "warning: eris-backup.timer did not start"
if grep -q '^BACKUP_DIR="/' /etc/eris/eris.conf; then
  say "backup: nightly at $(sed -n 's/^BACKUP_AT="\(.*\)"$/\1/p' /etc/eris/eris.conf) into $(sed -n 's/^BACKUP_DIR="\(.*\)"$/\1/p' /etc/eris/eris.conf) (full/ and one folder per part)"
  # 2.5: the archives of 2.4 move from BACKUP_DIR into BACKUP_DIR/full (once; nothing to do afterwards)
  python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris"); import erislib as E, erisbackup as B
c = E.load_conf(); B.migrate(c["BACKUP_DIR"], c["BACKUP_OWNER"], log=lambda m: print("[ERIS] backup:" + m))' \
    || say "warning: the older backups were not moved into full/ (eris backup list does it later)"
else
  say "backup: off until a folder is set (eris set BACKUP_DIR /path/to/folder)"
fi
systemctl enable eris-console.service > /dev/null 2>&1
systemctl restart eris-console.service || say "warning: eris-console.service failed (journalctl -u eris-console)"
if [ "$(sed -n 's/^DASH_AT_BOOT="\(.*\)"$/\1/p' /etc/eris/eris.conf)" = yes ]; then
  systemctl enable eris-dashboard.service > /dev/null 2>&1
else
  systemctl disable eris-dashboard.service > /dev/null 2>&1 || true
fi
[ "$was_on" = 1 ] && systemctl start eris-dashboard.service
say "look applied: $(tail -n 1 "$LOG" | sed 's/^.*\] //' | cut -c1-160)"

# 7. firewall: optional; an upgrade never reloads or restarts it (the live rules stay as they are)
if [ -e /etc/eris/firewall.conf ]; then
  say "firewall: kept /etc/eris/firewall.conf and the live rules (eris fw)"
elif [ -e /etc/eris/guard.nft ]; then
  /usr/local/bin/eris fw import || say "warning: eris fw import refused; the old /etc/eris/guard.nft stays in use"
elif [ "$FIREWALL" = 1 ]; then
  /usr/local/bin/eris fw setup || say "warning: the firewall is not set up (later: eris fw setup)"
else
  say "firewall: not set up (optional: eris fw setup, or install with --firewall)"
fi

# 8. self-test
rc=0
if [ "$SELFTEST" = 1 ]; then
  /usr/local/bin/eris selftest || rc=$?
fi
say "done. Commands: eris (menu) | eris on | eris off | eris toggle | eris enable | eris disable | eris status | eris fw | eris backup | eris update"
say "remove: eris-uninstall   (or: bash $0 --uninstall)"
exit $rc
