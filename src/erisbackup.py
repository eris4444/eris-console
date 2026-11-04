"""ERIS backups: settings and databases of this server, nightly or by hand.

Two kinds, each in a folder of its own under BACKUP_DIR:
  full/      the whole server in one archive (<host>-backup-YYYYmmdd-HHMM.tar); it stays on this disk
  <part>/    one part of the server (<host>-<part>-backup-YYYYmmdd-HHMM.tar), see PARTS

An archive holds
  files.tar.gz   the folders below with full paths (the ones that exist on this server)
  databases/     consistent copies of the live SQLite databases ("__" stands for "/")
  RESTORE.txt    what is inside and how to put it back
  manifest.json  host, time, ERIS version, part, paths and databases
"""
import contextlib
import datetime
import fcntl
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

sys.path.insert(0, os.environ.get("ERIS_LIB", "/usr/local/lib/eris"))
import erislib as E  # noqa: E402

STATE = os.path.join(E.STATE_DIR, "backup-state.json")
LOCK = os.path.join(E.RUN_DIR, "backup.lock")
FULL = "full"

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

# The parts backed up one by one. A part is on this server when one of its "detect" paths exists.
# To add a part: one entry here and its name in erislib.PART_NAMES.
PARTS = {
    "marzban": {
        "title": "Marzban panel and its nodes",
        "detect": ["/opt/marzban", "/var/lib/marzban"],
        "paths": ["/var/lib/marzban", "/opt/marzban", "/opt/marzban-nodes", "/var/lib/marzban-nodes"],
        "databases": ["/var/lib/marzban/db.sqlite3"],
    },
    "x-ui": {
        "title": "3x-ui panel",
        "detect": ["/etc/x-ui"],
        "paths": ["/etc/x-ui", "/etc/systemd/system/x-ui.service"],
        "databases": ["/etc/x-ui/x-ui.db"],
    },
    "xray-site": {
        "title": "xray-site, certificates (acme.sh), haproxy and nginx",
        "detect": ["/etc/xray-site", "/usr/local/bin/xray-site"],
        "paths": ["/etc/xray-site", "/var/lib/xray-site", "/usr/local/bin/xray-site", "/usr/local/share/xray-site",
                  "/root/.acme.sh", "/etc/haproxy", "/etc/nginx"],
        "databases": [],
    },
    "jellyfin": {
        "title": "Jellyfin settings and library database (no cache, logs or transcodes)",
        "detect": ["/config/jellyfin", "/appdata/JellyFin"],
        "paths": ["/config/jellyfin", "/appdata/JellyFin"],
        "databases": ["/config/jellyfin/data/data/jellyfin.db"],
    },
    "eris": {
        "title": "ERIS Console settings and state",
        "detect": ["/etc/eris"],
        "paths": ["/etc/eris", "/var/lib/eris"],
        "databases": [],
    },
    "claude-f-web": {
        "title": "Claude F Web",
        "detect": ["/etc/claude-f-web", "/var/lib/claude-f-web"],
        "paths": ["/etc/claude-f-web", "/var/lib/claude-f-web"],
        "databases": [],
    },
    "system": {
        "title": "System settings (openmediavault, systemd units, fstab, sysctl, SSH keys, Docker)",
        "detect": ["/etc/fstab", "/etc/systemd/system"],
        "paths": ["/etc/openmediavault", "/etc/systemd/system", "/etc/fstab", "/etc/sysctl.d", "/etc/modules-load.d",
                  "/root/.ssh/authorized_keys", "/etc/docker/daemon.json"],
        "databases": [],
    },
}
assert tuple(PARTS) == E.PART_NAMES, "erislib.PART_NAMES and erisbackup.PARTS differ"

# caches, logs and the like
EXCLUDE = re.compile(r"(^|/)(__pycache__|cache|log|transcodes)(/|$)|\.(pyc|tmp)$")
# services that write a database, stopped while it is restored: (database, stop, start)
OWNERS = {
    "/var/lib/marzban/db.sqlite3": (["docker", "compose", "-f", "/opt/marzban/docker-compose.yml", "stop"],
                                    ["docker", "compose", "-f", "/opt/marzban/docker-compose.yml", "start"]),
    "/etc/x-ui/x-ui.db": (["systemctl", "stop", "x-ui"], ["systemctl", "start", "x-ui"]),
    "/config/jellyfin/data/data/jellyfin.db": (["docker", "stop", "jellyfin"], ["docker", "start", "jellyfin"]),
}
NAME_RX = re.compile(r"-backup-(\d{8})-(\d{4})(\d{2})?(?:-([a-z-]+))?\.tar$")
CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000"]
DECRYPT_HINT = "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in FILE.enc -out FILE.tar"


def db_name(path):
    return path.strip("/").replace("/", "__")


def prefix():
    return re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname()) or "eris"


# ---------------------------------------------------------------- state (backup-state.json)
# The keys at the top level describe the full backup (as in ERIS 2.4); "parts" holds one entry per part.

@contextlib.contextmanager
def _state_lock():
    os.makedirs(E.STATE_DIR, exist_ok=True)
    with open(STATE + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _write_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def save_state(**kw):
    """Update the full-backup keys."""
    with _state_lock():
        st = load_state()
        st.update(kw)
        _write_state(st)


def save_part_state(part, **kw):
    with _state_lock():
        st = load_state()
        parts = st.get("parts") if isinstance(st.get("parts"), dict) else {}
        cur = parts.get(part) if isinstance(parts.get(part), dict) else {}
        cur.update(kw)
        parts[part] = cur
        st["parts"] = parts
        _write_state(st)


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def part_state(part):
    st = load_state()
    return (st.get("parts") or {}).get(part) or {} if part != FULL else {k: v for k, v in st.items() if k != "parts"}


@contextlib.contextmanager
def lock(wait=True):
    """One backup or restore at a time (the nightly timer, the menu, eris-bot)."""
    os.makedirs(E.RUN_DIR, exist_ok=True)
    with open(LOCK, "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError("another backup or restore is running")
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


# ---------------------------------------------------------------- folders and parts

def present(part):
    """True when the part is on this server."""
    return any(os.path.lexists(E.R(p)) for p in PARTS[part]["detect"])


def folder(base, part=None, owner="", create=True):
    """BACKUP_DIR/full or BACKUP_DIR/<part>; made (and given to owner) on first use."""
    if not base:
        raise ValueError("no backup folder: eris set BACKUP_DIR /path/to/folder")
    if not os.path.isdir(base):
        raise ValueError("%s is not a folder (is the disk mounted?)" % base)
    d = os.path.join(base, part or FULL)
    if create and not os.path.isdir(d):
        os.makedirs(d, mode=0o750)
        if owner:
            os.chown(d, pwd.getpwnam(owner).pw_uid, os.stat(base).st_gid)
    return d


def migrate(base, owner="", log=print):
    """Backups of ERIS 2.4 lie in BACKUP_DIR itself: move them (and their .sha256) into BACKUP_DIR/full once."""
    if not base or not os.path.isdir(base):
        return 0
    moved = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    old = [n for n in names if NAME_RX.search(n) and os.path.isfile(os.path.join(base, n))]
    if not old:
        return 0
    dest = folder(base, None, owner)
    for n in old:
        for x in (n, n + ".sha256"):
            src = os.path.join(base, x)
            if os.path.isfile(src):
                os.replace(src, os.path.join(dest, x))
        moved += 1
    log("  moved %d older backup%s into %s" % (moved, "s"[:moved != 1], dest))
    return moved


def skipper(databases):
    dbs = tuple(db.strip("/") for db in databases)

    def skip(info):
        rel = info.name
        if dbs and rel.startswith(dbs):
            return None                       # the database copies come from SQLite itself
        if EXCLUDE.search(rel):
            return None
        return info
    return skip


def skip(info):  # ERIS 2.4 name, the full backup's filter
    return skipper(DATABASES)(info)


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
            t = time.mktime(time.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S"))
            out.append((os.path.join(dest, n), t))
    return sorted(out, key=lambda x: (x[1], x[0]), reverse=True)


def make(dest, keep=7, owner="", label="", log=print, part=None):
    """One backup into dest (the full one, or one part); keeps the newest `keep` archives there.
    Returns the archive path."""
    if not dest:
        raise ValueError("no backup folder: eris set BACKUP_DIR /path/to/folder")
    if not os.path.isdir(dest):
        raise ValueError("%s is not a folder (is the disk mounted?)" % dest)
    if part:
        if part not in PARTS:
            raise ValueError("unknown part %s (eris backup parts)" % part)
        paths, databases = PARTS[part]["paths"], PARTS[part]["databases"]
    else:
        paths, databases = PATHS, DATABASES
    have = [p for p in paths if os.path.lexists(E.R(p))]
    if part and not have:
        raise ValueError("nothing of %s is on this server" % part)
    base = "%s%s-backup-" % (prefix(), "-" + part if part else "")
    tail = ("-" + label if label else "") + ".tar"
    name = base + time.strftime("%Y%m%d-%H%M") + tail
    while os.path.exists(os.path.join(dest, name)):       # another backup in the same minute (or second)
        name = base + time.strftime("%Y%m%d-%H%M%S") + tail
        if os.path.exists(os.path.join(dest, name)):
            time.sleep(1)
    final = os.path.join(dest, name)
    tmpdir = E.TMP_DIR if os.path.isdir(E.TMP_DIR) else None
    work = tempfile.mkdtemp(prefix="eris-backup-", dir=tmpdir)
    try:
        dbs = []
        os.makedirs(os.path.join(work, "databases"))
        for src in databases:
            real = E.R(src)
            if not os.path.exists(real):
                continue
            dst = os.path.join(work, "databases", db_name(src))
            with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % real, uri=True, timeout=60)) as a, \
                    contextlib.closing(sqlite3.connect(dst)) as b:
                a.backup(b)
            with contextlib.closing(sqlite3.connect(dst)) as chk:
                ok = chk.execute("pragma integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError("the copy of %s fails its integrity check: %s" % (src, ok))
            dbs.append(src)
            log("  database %s (%d KB)" % (src, os.path.getsize(dst) // 1024))
        with tarfile.open(os.path.join(work, "files.tar.gz"), "w:gz", compresslevel=6) as t:
            for p in have:
                t.add(E.R(p), arcname=p.lstrip("/"), filter=skipper(databases))
        manifest = {"host": socket.gethostname(), "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                    "eris": E.VERSION, "part": part or FULL, "paths": have, "databases": dbs}
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
        if os.path.exists(final + ".part"):
            os.remove(final + ".part")
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
    what = "" if m.get("part", FULL) == FULL else ", part %s" % m["part"]
    lines = ["ERIS backup of %s%s, %s (ERIS Console %s)" % (m["host"], what, m["time"], m["eris"]), "",
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


def stored_sha256(path):
    """The checksum written next to the archive, or None."""
    try:
        with open(path + ".sha256") as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return None


def check(path):
    """{"ok", "message", "files", "databases"}: the checksum file matches and the archive reads back."""
    want = stored_sha256(path)
    if want is not None and want != sha256(path):
        return {"ok": False, "message": "checksum mismatch (%s.sha256)" % os.path.basename(path), "files": 0, "databases": 0}
    try:
        with tarfile.open(path) as t:
            names = t.getnames()
            if "files.tar.gz" not in names:
                return {"ok": False, "message": "files.tar.gz is missing", "files": 0, "databases": 0}
            n = 0
            with tarfile.open(fileobj=t.extractfile("files.tar.gz"), mode="r|gz") as inner:
                for _ in inner:
                    n += 1
    except (tarfile.TarError, OSError, EOFError, ValueError) as e:
        return {"ok": False, "message": "unreadable: %s" % e, "files": 0, "databases": 0}
    dbs = sum(1 for x in names if x.startswith("databases/") and x != "databases")
    return {"ok": True, "message": "%d files, %d databases" % (n, dbs), "files": n, "databases": dbs}


def verify(path):
    """(ok, message): the checksum file matches and the archive reads back."""
    r = check(path)
    return r["ok"], r["message"]


def manifest(path):
    with tarfile.open(path) as t:
        try:
            return json.load(t.extractfile("manifest.json"))
        except KeyError:
            return {"host": "?", "time": "?", "eris": "?", "paths": [], "databases": []}


def describe(path, epoch=None):
    """What eris backup list --json shows for one archive (no unpacking)."""
    m = NAME_RX.search(os.path.basename(path))
    if epoch is None and m:
        epoch = time.mktime(time.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S"))
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    return {"path": path, "name": os.path.basename(path), "epoch": int(epoch or 0),
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch or 0)),
            "size": size, "sha256": stored_sha256(path), "label": (m.group(4) if m else None) or ""}


def run(cmd):
    if E.ROOT:                      # a test tree: never touch the real services
        return True
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

    live = os.path.realpath(to) == os.path.realpath(E.R("/") or "/")
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


# ---------------------------------------------------------------- encryption (for copies that leave this server)

def _openssl(args, src, out, password_file):
    if not os.path.isfile(password_file) or os.path.getsize(password_file) == 0:
        raise ValueError("no password in %s" % password_file)
    if not os.path.isfile(src):
        raise ValueError("%s does not exist" % src)
    tmp = out + ".tmp"
    try:
        r = subprocess.run(["openssl", "enc"] + args + CIPHER + ["-in", src, "-out", tmp, "-pass", "file:" + password_file],
                           capture_output=True, text=True, timeout=3600, preexec_fn=lambda: os.umask(0o077))
    except FileNotFoundError:
        raise RuntimeError("openssl is not installed")
    if r.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        err = r.stderr.strip().splitlines()
        if "-d" in args:
            raise ValueError("cannot decrypt %s: wrong password or a damaged file (%s)" % (
                os.path.basename(src), err[0] if err else r.returncode))
        raise RuntimeError("openssl failed: %s" % (err[0] if err else r.returncode))
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return out


def encrypt(path, password_file, out=None):
    """AES-256-CBC with PBKDF2 (200000 rounds) and a random salt; the password is read from a file
    (openssl -pass file:), so it never shows up in ps. Returns the .enc path."""
    return _openssl(["-salt"], path, out or path + ".enc", password_file)


def decrypt(path, password_file, out=None):
    """The reverse of encrypt(); FILE.enc -> FILE (by hand: DECRYPT_HINT)."""
    if out is None:
        out = path[:-4] if path.endswith(".enc") else path + ".dec"
    return _openssl(["-d"], path, out, password_file)


# ---------------------------------------------------------------- small helpers

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
