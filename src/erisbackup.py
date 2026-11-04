"""ERIS backups: settings and databases of this server in one archive, nightly or by hand.

An archive (<host>-backup-YYYYmmdd-HHMM.tar) holds
  files.tar.gz   the folders below with full paths (the ones that exist on this server)
  databases/     consistent copies of the live SQLite databases ("__" stands for "/")
  RESTORE.txt    what is inside and how to put it back
  manifest.json  host, time, ERIS version, paths and databases
"""
import datetime
import hashlib
import io
import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, "/usr/local/lib/eris")
import erislib as E  # noqa: E402

STATE = os.path.join(E.STATE_DIR, "backup-state.json")

# settings and data worth keeping; paths that do not exist here are skipped
PATHS = [
    "/etc/eris", "/var/lib/eris", "/usr/local/bin/eris", "/usr/local/lib/eris", "/usr/local/sbin/eris-uninstall",
    "/var/lib/marzban", "/opt/marzban", "/opt/marzban-nodes", "/var/lib/marzban-nodes",
    "/etc/x-ui", "/etc/xray-site", "/var/lib/xray-site", "/usr/local/bin/xray-site", "/usr/local/share/xray-site",
    "/etc/claude-f-web", "/var/lib/claude-f-web",
    "/root/.acme.sh", "/appdata", "/config/jellyfin",
    "/etc/openmediavault", "/etc/haproxy", "/etc/nginx", "/etc/smartd.conf", "/etc/sysctl.d", "/etc/modules-load.d",
    "/etc/systemd/system", "/etc/fstab", "/etc/docker/daemon.json", "/root/.ssh/authorized_keys",
    "/usr/local/sbin/eris-smart-enable",
]
# live SQLite databases: copied with SQLite's own backup, so they are consistent
DATABASES = ["/var/lib/marzban/db.sqlite3", "/etc/x-ui/x-ui.db", "/config/jellyfin/data/data/jellyfin.db"]
# caches, logs and the like
EXCLUDE = re.compile(r"(^|/)(__pycache__|cache|log|transcodes)(/|$)|\.(pyc|tmp)$")
# services that write a database, stopped while it is restored: (database, stop, start)
OWNERS = {
    "/var/lib/marzban/db.sqlite3": (["docker", "compose", "-f", "/opt/marzban/docker-compose.yml", "stop"],
                                    ["docker", "compose", "-f", "/opt/marzban/docker-compose.yml", "start"]),
    "/etc/x-ui/x-ui.db": (["systemctl", "stop", "x-ui"], ["systemctl", "start", "x-ui"]),
    "/config/jellyfin/data/data/jellyfin.db": (["docker", "stop", "jellyfin"], ["docker", "start", "jellyfin"]),
}
NAME_RX = re.compile(r"-backup-(\d{8})-(\d{4})(?:-[a-z-]+)?\.tar$")


def db_name(path):
    return path.strip("/").replace("/", "__")


def prefix():
    return re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname()) or "eris"


def save_state(**kw):
    st = load_state()
    st.update(kw)
    os.makedirs(E.STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def skip(info):
    rel = info.name
    if rel.startswith(tuple(db.strip("/") for db in DATABASES)):
        return None                       # the database copies come from SQLite itself
    if EXCLUDE.search(rel):
        return None
    return info


def archives(dest):
    """Backups in dest, newest first: [(path, epoch)]."""
    out = []
    try:
        names = os.listdir(dest)
    except OSError:
        return out
    for n in names:
        m = NAME_RX.search(n)
        if m and os.path.isfile(os.path.join(dest, n)):
            t = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M"))
            out.append((os.path.join(dest, n), t))
    return sorted(out, key=lambda x: (x[1], x[0]), reverse=True)


def make(dest, keep=7, owner="", label="", log=print):
    """One backup into dest; keeps the newest `keep` archives. Returns the archive path."""
    if not dest:
        raise ValueError("no backup folder: eris set BACKUP_DIR /path/to/folder")
    if not os.path.isdir(dest):
        raise ValueError("%s is not a folder (is the disk mounted?)" % dest)
    stamp = time.strftime("%Y%m%d-%H%M")
    name = "%s-backup-%s%s.tar" % (prefix(), stamp, "-" + label if label else "")
    final = os.path.join(dest, name)
    work = tempfile.mkdtemp(prefix="eris-backup-", dir="/var/tmp")
    try:
        dbs = []
        os.makedirs(os.path.join(work, "databases"))
        for src in DATABASES:
            if not os.path.exists(src):
                continue
            dst = os.path.join(work, "databases", db_name(src))
            with sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=60) as a, sqlite3.connect(dst) as b:
                a.backup(b)
            ok = sqlite3.connect(dst).execute("pragma integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError("the copy of %s fails its integrity check: %s" % (src, ok))
            dbs.append(src)
            log("  database %s (%d KB)" % (src, os.path.getsize(dst) // 1024))
        have = [p for p in PATHS if os.path.lexists(p)]
        with tarfile.open(os.path.join(work, "files.tar.gz"), "w:gz", compresslevel=6) as t:
            for p in have:
                t.add(p, arcname=p.lstrip("/"), filter=skip)
        manifest = {"host": socket.gethostname(), "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                    "eris": E.VERSION, "paths": have, "databases": dbs}
        with open(os.path.join(work, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)
        with open(os.path.join(work, "RESTORE.txt"), "w") as f:
            f.write(restore_text(manifest))
        tmp = final + ".part"
        with tarfile.open(tmp, "w") as t:
            for n in ("files.tar.gz", "databases", "manifest.json", "RESTORE.txt"):
                t.add(os.path.join(work, n), arcname=n)
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    digest = sha256(final)
    with open(final + ".sha256", "w") as f:
        f.write("%s  %s\n" % (digest, name))
    if owner:
        u = pwd.getpwnam(owner)
        for p in (final, final + ".sha256"):
            os.chown(p, u.pw_uid, os.stat(dest).st_gid)
    for old, _ in archives(dest)[max(1, keep):]:
        for p in (old, old + ".sha256"):
            if os.path.exists(p):
                os.remove(p)
                log("  removed the old %s" % os.path.basename(p))
    return final


def restore_text(m):
    lines = ["ERIS backup of %s, %s (ERIS Console %s)" % (m["host"], m["time"], m["eris"]), "",
             "Put it back with:   eris restore <this file>      (eris restore --help)",
             "or by hand:         tar -C / -xzf files.tar.gz [path ...]", "",
             "databases/ holds consistent copies of the live SQLite databases; stop the program that",
             "writes one before copying it back (\"__\" in the name stands for \"/\"):"]
    for d in m["databases"]:
        lines.append("  %-45s -> %s" % (db_name(d), d))
    lines += ["", "Inside files.tar.gz:"] + ["  " + p for p in m["paths"]]
    lines += ["", "It holds secrets (panel passwords, Xray keys, TLS private keys): keep it private.",
              "Not inside: media files, Docker images, program files that an installer brings back."]
    return "\n".join(lines) + "\n"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path):
    """(ok, message): the checksum file matches and the archive reads back."""
    sf = path + ".sha256"
    if os.path.exists(sf):
        want = open(sf).read().split()[0]
        if want != sha256(path):
            return False, "checksum mismatch (%s)" % os.path.basename(sf)
    try:
        with tarfile.open(path) as t:
            names = t.getnames()
            if "files.tar.gz" not in names:
                return False, "files.tar.gz is missing"
            inner = tarfile.open(fileobj=io.BytesIO(t.extractfile("files.tar.gz").read()))
            n = len(inner.getnames())
    except (tarfile.TarError, OSError, EOFError) as e:
        return False, "unreadable: %s" % e
    return True, "%d files, %d databases" % (n, sum(1 for x in names if x.startswith("databases/") and x != "databases"))


def manifest(path):
    with tarfile.open(path) as t:
        try:
            return json.load(t.extractfile("manifest.json"))
        except KeyError:
            return {"host": "?", "time": "?", "eris": "?", "paths": [], "databases": []}


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def restore(path, to="/", only=(), log=print):
    """Put a backup back. to="/" stops the programs whose databases come back and starts them again."""
    ok, msg = verify(path)
    if not ok:
        raise ValueError("%s: %s" % (os.path.basename(path), msg))
    only = [o.strip("/") for o in only if o.strip("/")]

    def wanted(name):
        return not only or any(name == o or name.startswith(o + "/") for o in only)

    live = os.path.realpath(to) == "/"
    with tarfile.open(path) as outer:
        inner = tarfile.open(fileobj=io.BytesIO(outer.extractfile("files.tar.gz").read()))
        members = [m for m in inner.getmembers() if wanted(m.name)]
        dbs = [m for m in outer.getmembers() if m.isfile() and m.name.startswith("databases/")]
        db_paths = {}
        for m in dbs:
            real = "/" + os.path.basename(m.name).replace("__", "/")
            if wanted(real.lstrip("/")):
                db_paths[m.name] = real
        stopped = []
        if live:
            for real in db_paths.values():
                cmds = OWNERS.get(real)
                if cmds and run(cmds[0]):
                    stopped.append(cmds[1])
                    log("  stopped: %s" % " ".join(cmds[0]))
        try:
            # our own archive, checked against its checksum above; the "tar" filter would refuse
            # the absolute symlinks it holds (e.g. Marzban's certificate links)
            kw = {"filter": "fully_trusted"} if sys.version_info >= (3, 12) else {}
            inner.extractall(to, members=members, numeric_owner=True, **kw)
            log("  %d files put back under %s" % (len(members), to))
            for name, real in db_paths.items():
                dst = os.path.join(to, real.lstrip("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                for side in ("-wal", "-shm"):
                    if live and os.path.exists(dst + side):
                        os.remove(dst + side)
                st = os.stat(dst) if os.path.exists(dst) else None
                with outer.extractfile(name) as src, open(dst + ".restore", "wb") as out:
                    shutil.copyfileobj(src, out)
                if st:
                    os.chown(dst + ".restore", st.st_uid, st.st_gid)
                    os.chmod(dst + ".restore", st.st_mode & 0o7777)
                os.replace(dst + ".restore", dst)
                log("  database %s" % dst)
        finally:
            if live:
                run(["systemctl", "daemon-reload"])
                for cmd in stopped:
                    run(cmd)
                    log("  started: %s" % " ".join(cmd))
    return len(members), len(db_paths)


def age_text(sec):
    if sec < 3600:
        return "%d min" % (sec // 60)
    if sec < 86400 * 2:
        return "%d h" % (sec // 3600)
    return "%d days" % (sec // 86400)


def timer_text(at):
    return ("[Unit]\nDescription=Nightly ERIS backup (eris backup)\n\n[Timer]\nOnCalendar=*-*-* %s:00\n"
            "Persistent=true\nRandomizedDelaySec=5min\n\n[Install]\nWantedBy=timers.target\n" % at)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
