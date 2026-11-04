#!/bin/bash
# Assemble the self-contained /root/install-eris.sh from the sources in this directory.
set -euo pipefail
cd "$(dirname "$0")"
out=${1:-/root/install-eris.sh}
emit() {  # SRC DEST MODE
  local tag
  tag="ERIS_EOF_$(basename "$1" | tr -c 'A-Za-z0-9\n' '_')"
  if grep -qx "$tag" "$1"; then echo "delimiter $tag occurs in $1" >&2; exit 1; fi
  printf "  put_file %s %s << '%s'\n" "$2" "$3" "$tag"
  cat "$1"
  printf '%s\n' "$tag"
}
{
  sed -n '1,/^#@@FILES@@$/p' install-head.sh | sed '$d'
  echo "write_uninstaller() {"
  emit eris-uninstall '"$1"' 0755
  echo "}"
  echo "write_files() {"
  emit erislib.py /usr/local/lib/eris/erislib.py 0644
  emit erisd.py /usr/local/lib/eris/erisd.py 0755
  emit erisfont.py /usr/local/lib/eris/erisfont.py 0644
  emit erisfw.py /usr/local/lib/eris/erisfw.py 0644
  emit erisbackup.py /usr/local/lib/eris/erisbackup.py 0644
  emit fw-rollback /usr/local/lib/eris/fw-rollback 0755
  emit eris /usr/local/bin/eris 0755
  emit prompt.bash /etc/eris/prompt.bash 0644
  emit eris-console.service /etc/systemd/system/eris-console.service 0644
  emit eris-dashboard.service /etc/systemd/system/eris-dashboard.service 0644
  emit eris-guard.service /etc/systemd/system/eris-guard.service 0644
  emit eris-backup.service /etc/systemd/system/eris-backup.service 0644
  echo "  write_uninstaller /usr/local/sbin/eris-uninstall"
  echo "}"
  sed -n '/^#@@FILES@@$/,$p' install-head.sh | sed '1d'
} > "$out.tmp"
chmod 0755 "$out.tmp"
bash -n "$out.tmp"
mv -f "$out.tmp" "$out"
echo "built $out ($(wc -c < "$out") bytes, sha256 $(sha256sum "$out" | cut -c1-16))"
