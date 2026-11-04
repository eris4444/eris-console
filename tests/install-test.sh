#!/bin/bash
# Runs inside the systemd container (tests/ci-install.sh): install, use, upgrade and remove ERIS.
#   install-test.sh NEW_INSTALLER [OLD_INSTALLER]
set -euo pipefail
new=$1
old=${2-}
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }
B=/srv/flash/backups

if [ -n "$old" ]; then
  bash "$old" --no-selftest > /tmp/old.log 2>&1 || { cat /tmp/old.log; fail "the 2.4.0 installer"; }
  [ "$(eris version)" = 2.4.0 ] || fail "2.4.0 is not installed"
  mkdir -p "$B"
  eris set BACKUP_DIR "$B" > /dev/null
  eris backup now --quiet
  ls "$B"/*-backup-*.tar > /dev/null || fail "no 2.4 backup in $B"
  ok "2.4.0 installed, one backup in the old place"
fi

# a few parts of a server: Marzban and x-ui with SQLite databases, xray-site files
mkdir -p /opt/marzban /var/lib/marzban /etc/x-ui /etc/xray-site/certs/example.com
printf 'UVICORN_PORT="8000"\n' > /opt/marzban/.env
python3 - << 'PY'
import sqlite3
for p in ("/var/lib/marzban/db.sqlite3", "/etc/x-ui/x-ui.db"):
    db = sqlite3.connect(p)
    db.execute("create table if not exists t (v text)")
    db.execute("insert into t values ('ci')")
    db.commit()
    db.close()
PY
echo cert > /etc/xray-site/certs/example.com/fullchain.pem

bash "$new" --no-selftest > /tmp/new.log 2>&1 || { cat /tmp/new.log; fail "installer"; }
cat /tmp/new.log
[ "$(eris version)" = 2.5.0 ] || fail "version $(eris version)"
for f in /usr/local/bin/eris /usr/local/lib/eris/erislib.py /usr/local/lib/eris/erisbackup.py \
         /usr/local/lib/eris/erisd.py /usr/local/lib/eris/erisfw.py /etc/eris/eris.conf \
         /etc/systemd/system/eris-backup.service /etc/systemd/system/eris-backup.timer /usr/local/sbin/eris-uninstall; do
  [ -e "$f" ] || fail "missing $f"
done
ok "files installed"
systemctl is-enabled --quiet eris-backup.timer || fail "eris-backup.timer not enabled"
systemctl is-active --quiet eris-backup.timer || fail "eris-backup.timer not active"
for k in BACKUP_FULL BACKUP_PARTS BACKUP_PART_KEEP; do
  grep -q "^$k=" /etc/eris/eris.conf || fail "$k missing in eris.conf"
done
ok "timer and new settings"

if [ -n "$old" ]; then
  [ "$(eris get BACKUP_DIR)" = "$B" ] || fail "BACKUP_DIR not kept"
  ls "$B"/full/*-backup-*.tar > /dev/null || fail "old backups not moved into full/"
  ! ls "$B"/*-backup-*.tar > /dev/null 2>&1 || fail "old backups still in $B"
  ok "upgrade: settings kept, old backups moved into full/"
else
  mkdir -p "$B"
  eris set BACKUP_DIR "$B" > /dev/null
fi

eris backup now --json > /tmp/now.json || { cat /tmp/now.json; fail "eris backup now"; }
python3 - << 'PY'
import json
r = json.load(open("/tmp/now.json"))
by = {x["part"]: x for x in r["results"]}
assert r["ok"], r
for p in ("full", "marzban", "x-ui", "xray-site", "eris", "system"):
    assert by[p]["ok"], (p, by[p])
assert by["jellyfin"].get("skipped") and by["claude-f-web"].get("skipped")
assert by["marzban"]["databases"] == 1 and by["x-ui"]["databases"] == 1
print("ok: eris backup now: " + ", ".join(sorted(k for k, v in by.items() if v["ok"])))
PY
eris backup status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["parts"]["x-ui"]["count"] == 1, s'
eris backup list --json | python3 -c 'import json,sys; g=json.load(sys.stdin)["groups"]; assert g["marzban"][0]["n"] == 1, g'
eris backup status
eris backup list
ok "status and list"

systemctl start eris-backup.service || fail "eris-backup.service"
[ "$(ls "$B"/x-ui/*.tar | wc -l)" -ge 2 ] || fail "the service made no new part backups"
ok "eris-backup.service (the nightly job)"

printf 'ci-password\n' > /root/pw && chmod 600 /root/pw
f=$(ls -t "$B"/x-ui/*.tar | head -n 1)
enc=$(eris backup encrypt "$f" --password-file /root/pw --out /var/tmp/x.tar.enc)
dec=$(eris backup decrypt "$enc" --password-file /root/pw)
cmp "$f" "$dec" || fail "decrypt gives another file"
ok "encrypt / decrypt"

python3 -c 'import sqlite3; db = sqlite3.connect("/etc/x-ui/x-ui.db"); db.execute("delete from t"); db.commit()'
eris restore --part x-ui 1 --yes
n=$(python3 -c 'import sqlite3; print(sqlite3.connect("/etc/x-ui/x-ui.db").execute("select count(*) from t").fetchone()[0])')
[ "$n" -ge 1 ] || fail "x-ui database not restored"
ls "$B"/x-ui/*before-restore.tar > /dev/null || fail "no before-restore backup"
ok "restore --part x-ui"

eris selftest || echo "note: some selftest checks need real hardware (console, disks)"

eris-uninstall > /tmp/uninstall.log 2>&1 || { cat /tmp/uninstall.log; fail "eris-uninstall"; }
[ ! -e /usr/local/bin/eris ] || fail "eris still installed"
ls "$B"/full/*.tar > /dev/null || fail "uninstall removed the backups"
ok "uninstall (backups stay)"
echo "ALL INSTALL CHECKS PASSED"
