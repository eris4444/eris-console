#!/bin/bash
# Real install of install-eris.sh in a Debian trixie container that runs systemd.
#   bash tests/ci-install.sh fresh      a new server
#   bash tests/ci-install.sh upgrade    2.4.0 first (from its release), backups in the old place, then this version
set -euo pipefail
mode=${1:-fresh}
cd "$(dirname "$0")/.."
work=$(mktemp -d)
bash src/build.sh "$work/install-eris.sh"
if [ "$mode" = upgrade ]; then
  curl -fsSL -o "$work/install-eris-2.4.0.sh" \
    https://github.com/eris4444/eris-console/releases/download/v2.4.0/install-eris.sh
fi
docker build -q -t eris-ci-systemd -f tests/Dockerfile.systemd tests > /dev/null
cid=$(docker run -d --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
        --tmpfs /run --tmpfs /run/lock -v "$PWD:/src:ro" -v "$work:/ci" eris-ci-systemd)
trap 'docker rm -f "$cid" > /dev/null' EXIT
for _ in $(seq 60); do
  st=$(docker exec "$cid" systemctl is-system-running 2> /dev/null || true)
  case $st in running | degraded) break ;; esac
  sleep 1
done
echo "container: systemd $st"
old=""
[ "$mode" = upgrade ] && old=/ci/install-eris-2.4.0.sh
docker exec "$cid" bash /src/tests/install-test.sh /ci/install-eris.sh $old
