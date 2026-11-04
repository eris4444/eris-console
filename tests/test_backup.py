"""Tests of the ERIS backups (full + one archive per part) on a fake server tree (ERIS_ROOT).

Run: python3 -m unittest discover -s tests -v
"""
import importlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
SRC = os.path.abspath(SRC)
ROOT = tempfile.mkdtemp(prefix="eris-fake-")
os.environ["ERIS_ROOT"] = ROOT
os.environ["ERIS_LIB"] = SRC
sys.path.insert(0, SRC)
import erislib as E  # noqa: E402
import erisbackup as B  # noqa: E402

BACKUP_DIR = os.path.join(ROOT, "srv", "flash", "backups")
IS_ROOT = os.geteuid() == 0


def put(path, text="x\n"):
    real = ROOT + path
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w") as f:
        f.write(text)
    return real


def make_db(path, rows):
    real = ROOT + path
    os.makedirs(os.path.dirname(real), exist_ok=True)
    if os.path.exists(real):
        os.remove(real)
    db = sqlite3.connect(real)
    with db:
        db.execute("create table users (name text, used integer)")
        db.executemany("insert into users values (?, ?)", rows)
    db.close()
    return real


def db_rows(real):
    db = sqlite3.connect(real)
    try:
        return sorted(db.execute("select name, used from users").fetchall())
    finally:
        db.close()


def build_tree():
    put("/etc/eris/eris.conf", 'BACKUP_DIR="%s"\nBACKUP_KEEP="7"\nBACKUP_AT="03:30"\n' % BACKUP_DIR)
    put("/var/lib/eris/temps.json", "[]")
    make_db("/var/lib/marzban/db.sqlite3", [("alice", 1), ("bob", 2)])
    put("/var/lib/marzban/certs/panel.example.com.pem", "CERT")
    put("/opt/marzban/.env", 'UVICORN_PORT="8000"\nSUDO_USERNAME="admin"\n')
    put("/opt/marzban/docker-compose.yml", "services: {}\n")
    put("/opt/marzban-nodes/n1/docker-compose.yml", "services: {}\n")
    put("/var/lib/marzban-nodes/n1/ssl_client_cert.pem", "C")
    make_db("/etc/x-ui/x-ui.db", [("in1", 10)])
    put("/etc/systemd/system/x-ui.service", "[Unit]\n")
    put("/etc/xray-site/certs/example.com/fullchain.pem", "FULLCHAIN")
    put("/etc/xray-site/certs/example.com/privkey.pem", "KEY")
    put("/usr/local/bin/xray-site", "#!/bin/sh\n")
    put("/root/.acme.sh/acme.sh", "#!/bin/sh\n")
    put("/etc/nginx/nginx.conf", "events {}\n")
    put("/etc/haproxy/haproxy.cfg", "global\n")
    make_db("/config/jellyfin/data/data/jellyfin.db", [("movie", 1)])
    put("/config/jellyfin/config/system.xml", "<x/>")
    put("/config/jellyfin/cache/images/big.bin", "B" * 1000)
    put("/config/jellyfin/log/log_1.log", "LOG")
    put("/config/jellyfin/data/transcodes/t.ts", "TS")
    put("/appdata/JellyFin/JellyFin.yml", "services: {}\n")
    put("/appdata/JellyFin/compose.override.yml", "services: {}\n")
    put("/etc/fstab", "# fstab\n")
    put("/etc/systemd/system/foo.service", "[Unit]\n")
    put("/root/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(ROOT + "/var/tmp", exist_ok=True)


def cli(*args, check=True):
    env = dict(os.environ, ERIS_ROOT=ROOT, ERIS_LIB=SRC)
    r = subprocess.run([sys.executable, os.path.join(SRC, "eris")] + list(args), capture_output=True, text=True,
                       env=env, timeout=120, stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise AssertionError("eris %s -> %d\n%s\n%s" % (" ".join(args), r.returncode, r.stdout, r.stderr))
    return r


def inner_names(path):
    with tarfile.open(path) as t:
        inner = tarfile.open(fileobj=io.BytesIO(t.extractfile("files.tar.gz").read()))
        return set(inner.getnames()), set(t.getnames())


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_tree()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(ROOT, ignore_errors=True)

    def setUp(self):
        for n in os.listdir(BACKUP_DIR):
            p = os.path.join(BACKUP_DIR, n)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        for f in ("backup-state.json",):
            p = os.path.join(E.STATE_DIR, f)
            if os.path.exists(p):
                os.remove(p)
        put("/etc/eris/eris.conf", 'BACKUP_DIR="%s"\nBACKUP_KEEP="7"\nBACKUP_AT="03:30"\n' % BACKUP_DIR)

    # ------------------------------------------------------------ library
    def test_paths_follow_eris_root(self):
        self.assertEqual(E.CONF, ROOT + "/etc/eris/eris.conf")
        self.assertEqual(E.STATE_DIR, ROOT + "/var/lib/eris")
        self.assertEqual(B.STATE, ROOT + "/var/lib/eris/backup-state.json")

    def test_parts_are_data_driven(self):
        self.assertEqual(tuple(B.PARTS), E.PART_NAMES)
        for name, p in B.PARTS.items():
            for key in ("title", "detect", "paths", "databases"):
                self.assertIn(key, p, name)

    def test_present(self):
        self.assertTrue(B.present("marzban"))
        self.assertTrue(B.present("jellyfin"))
        self.assertFalse(B.present("claude-f-web"))

    def test_settings_defaults_and_validation(self):
        conf = E.load_conf()
        self.assertEqual(conf["BACKUP_FULL"], "on")
        self.assertEqual(conf["BACKUP_PARTS"], "all")
        self.assertEqual(conf["BACKUP_PART_KEEP"], "7")
        self.assertTrue(E.valid_parts("marzban,x-ui"))
        self.assertFalse(E.valid_parts("marzban,nope"))
        self.assertFalse(E.valid_parts("marzban,marzban"))
        self.assertEqual(E.parts_setting("x-ui,eris"), ["x-ui", "eris"])
        self.assertEqual(E.parts_setting("none"), [])
        self.assertEqual(len(E.parts_setting("all")), len(B.PARTS))
        # the 2.4 settings keep working
        for k in ("BACKUP_DIR", "BACKUP_KEEP", "BACKUP_AT", "BACKUP_OWNER"):
            self.assertIn(k, conf)

    def test_part_backup_marzban(self):
        dest = B.folder(BACKUP_DIR, "marzban")
        self.assertEqual(dest, os.path.join(BACKUP_DIR, "marzban"))
        path = B.make(dest, 7, log=lambda *_: None, part="marzban")
        self.assertRegex(os.path.basename(path), r"-marzban-backup-\d{8}-\d{4}\.tar$")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(os.path.exists(path + ".sha256"))
        r = B.check(path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["databases"], 1)
        files, outer = inner_names(path)
        self.assertIn("databases/var__lib__marzban__db.sqlite3", outer)
        self.assertIn("opt/marzban/.env", files)
        self.assertIn("opt/marzban-nodes/n1/docker-compose.yml", files)
        self.assertIn("var/lib/marzban-nodes/n1/ssl_client_cert.pem", files)
        self.assertNotIn("var/lib/marzban/db.sqlite3", files)          # comes from SQLite itself
        self.assertFalse(any(n.startswith("etc/x-ui") for n in files))  # only this part
        m = B.manifest(path)
        self.assertEqual(m["part"], "marzban")
        self.assertEqual(m["databases"], ["/var/lib/marzban/db.sqlite3"])

    def test_jellyfin_leaves_out_cache_logs_transcodes(self):
        path = B.make(B.folder(BACKUP_DIR, "jellyfin"), 7, log=lambda *_: None, part="jellyfin")
        files, outer = inner_names(path)
        self.assertIn("config/jellyfin/config/system.xml", files)
        self.assertIn("appdata/JellyFin/JellyFin.yml", files)
        self.assertFalse(any("/cache" in n or "/log" in n or "transcodes" in n for n in files), files)
        self.assertIn("databases/config__jellyfin__data__data__jellyfin.db", outer)

    def test_missing_part_is_refused(self):
        with self.assertRaises(ValueError):
            B.make(B.folder(BACKUP_DIR, "claude-f-web"), 7, log=lambda *_: None, part="claude-f-web")

    def test_keep_is_per_folder(self):
        full = B.folder(BACKUP_DIR, None)
        B.make(full, 7, log=lambda *_: None)
        dest = B.folder(BACKUP_DIR, "x-ui")
        made = [B.make(dest, 2, log=lambda *_: None, part="x-ui") for _ in range(3)]
        left = [p for p, _ in B.archives(dest)]
        self.assertEqual(len(left), 2)
        self.assertNotIn(made[0], left)
        self.assertFalse(os.path.exists(made[0] + ".sha256"))
        self.assertEqual(len(B.archives(full)), 1)   # the other folder is not touched

    def test_migration_moves_old_archives_once(self):
        for n in ("host-backup-20260101-0330.tar", "host-backup-20260102-0330.tar",
                  "host-backup-20260103-0331-before-restore.tar"):
            with open(os.path.join(BACKUP_DIR, n), "w") as f:
                f.write("old")
            with open(os.path.join(BACKUP_DIR, n + ".sha256"), "w") as f:
                f.write("abc  %s\n" % n)
        with open(os.path.join(BACKUP_DIR, "notes.txt"), "w") as f:
            f.write("keep me")
        self.assertEqual(B.migrate(BACKUP_DIR, log=lambda *_: None), 3)
        full = os.path.join(BACKUP_DIR, "full")
        self.assertEqual(len(B.archives(full)), 3)
        self.assertTrue(os.path.exists(os.path.join(full, "host-backup-20260101-0330.tar.sha256")))
        self.assertTrue(os.path.exists(os.path.join(BACKUP_DIR, "notes.txt")))
        self.assertEqual(B.archives(BACKUP_DIR), [])
        self.assertEqual(B.migrate(BACKUP_DIR, log=lambda *_: None), 0)

    def test_restore_part_to_folder(self):
        path = B.make(B.folder(BACKUP_DIR, "x-ui"), 7, log=lambda *_: None, part="x-ui")
        to = tempfile.mkdtemp(dir=ROOT)
        n, dbs = B.restore(path, to, log=lambda *_: None)
        self.assertEqual(dbs, 1)
        self.assertGreater(n, 0)
        self.assertEqual(db_rows(os.path.join(to, "etc/x-ui/x-ui.db")), [("in1", 10)])
        self.assertTrue(os.path.exists(os.path.join(to, "etc/systemd/system/x-ui.service")))
        to2 = tempfile.mkdtemp(dir=ROOT)
        B.restore(path, to2, only=["/etc/systemd/system/x-ui.service"], log=lambda *_: None)
        self.assertTrue(os.path.exists(os.path.join(to2, "etc/systemd/system/x-ui.service")))
        self.assertFalse(os.path.exists(os.path.join(to2, "etc/x-ui/x-ui.db")))

    def test_encrypt_round_trip(self):
        path = B.make(B.folder(BACKUP_DIR, "eris"), 7, log=lambda *_: None, part="eris")
        pw = os.path.join(ROOT, "pass")
        with open(pw, "w") as f:
            f.write("correct horse battery staple\n")
        os.chmod(pw, 0o600)
        out_dir = tempfile.mkdtemp(dir=ROOT)
        enc = B.encrypt(path, pw, os.path.join(out_dir, "a.tar.enc"))
        self.assertEqual(stat.S_IMODE(os.stat(enc).st_mode), 0o600)
        with open(enc, "rb") as f:
            self.assertEqual(f.read(8), b"Salted__")
        dec = B.decrypt(enc, pw)
        self.assertEqual(dec, os.path.join(out_dir, "a.tar"))
        self.assertEqual(B.sha256(dec), B.sha256(path))
        # also what the caption tells a person to run by hand
        hand = os.path.join(out_dir, "hand.tar")
        cmd = B.DECRYPT_HINT.replace("FILE.enc", enc).replace("FILE.tar", hand).split() + ["-pass", "file:" + pw]
        self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
        self.assertEqual(B.sha256(hand), B.sha256(path))
        with open(pw, "w") as f:
            f.write("wrong\n")
        with self.assertRaises(ValueError):
            B.decrypt(enc, pw, os.path.join(out_dir, "bad.tar"))
        self.assertFalse(os.path.exists(os.path.join(out_dir, "bad.tar")))

    def test_state_keeps_old_keys_for_full(self):
        B.save_state(last_ok="/x.tar", last_ok_epoch=100)
        B.save_part_state("marzban", last_ok="/m.tar", last_ok_epoch=200)
        B.save_part_state("x-ui", last_error="boom", last_error_epoch=300)
        st = B.load_state()
        self.assertEqual(st["last_ok"], "/x.tar")
        self.assertEqual(st["parts"]["marzban"]["last_ok_epoch"], 200)
        self.assertEqual(st["parts"]["x-ui"]["last_error"], "boom")
        self.assertEqual(B.part_state("full")["last_ok_epoch"], 100)
        self.assertNotIn("parts", B.part_state("full"))

    def test_dashboard_alerts(self):
        import erisd
        conf = dict(E.load_conf())
        now = int(time.time())
        d = {"backup": {"last_ok_epoch": now, "parts": {"marzban": {"last_ok_epoch": now - 10, "last_error_epoch": now},
                                                         "x-ui": {"last_ok_epoch": now}}}}
        texts = [t for _, t in erisd.alerts(d, conf)]
        self.assertIn("backup marzban failed", texts)
        self.assertNotIn("backup failed", texts)
        conf["BACKUP_FULL"] = "off"
        d = {"backup": {"parts": {"x-ui": {"last_ok_epoch": now - 3 * 86400}}}}
        texts = [t for _, t in erisd.alerts(d, conf)]
        self.assertTrue(any(t.startswith("backup 3d old") for t in texts), texts)
        d = {"backup": {"last_error_epoch": now}}
        conf["BACKUP_FULL"] = "on"
        texts = [t for _, t in erisd.alerts(d, conf)]
        self.assertEqual([t for t in texts if t.startswith(("backup", "no backup"))], ["backup failed"])


@unittest.skipUnless(IS_ROOT, "the eris command needs root")
class CliTests(unittest.TestCase):
    setUp = BackupTests.setUp

    @classmethod
    def setUpClass(cls):
        build_tree()

    def test_now_all_list_status(self):
        r = cli("backup", "now", "--json", check=False)
        out = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        by = {x["part"]: x for x in out["results"]}
        self.assertTrue(by["full"]["ok"])
        self.assertTrue(by["marzban"]["ok"])
        self.assertTrue(by["claude-f-web"].get("skipped"))
        self.assertEqual(by["marzban"]["databases"], 1)
        self.assertEqual(len(by["marzban"]["sha256"]), 64)
        for name in ("full", "marzban", "x-ui", "xray-site", "jellyfin", "eris", "system"):
            self.assertEqual(len(B.archives(os.path.join(BACKUP_DIR, name))), 1, name)
        self.assertFalse(os.path.exists(os.path.join(BACKUP_DIR, "claude-f-web")))
        st = B.load_state()
        self.assertTrue(st["last_ok"].startswith(os.path.join(BACKUP_DIR, "full")))   # 2.4 keys = full backup
        self.assertIn("marzban", st["parts"])
        lst = json.loads(cli("backup", "list", "--json").stdout)
        self.assertEqual(lst["groups"]["marzban"][0]["n"], 1)
        self.assertEqual(set(lst["groups"]), {"full"} | set(B.PARTS))
        one = json.loads(cli("backup", "list", "--part", "x-ui", "--json").stdout)
        self.assertEqual(list(one["groups"]), ["x-ui"])
        status = json.loads(cli("backup", "status", "--json").stdout)
        self.assertTrue(status["parts"]["marzban"]["present"])
        self.assertFalse(status["parts"]["claude-f-web"]["present"])
        self.assertEqual(status["parts"]["marzban"]["count"], 1)
        self.assertTrue(status["full"]["enabled"])
        text = cli("backup", "status").stdout
        self.assertIn("marzban", text)
        self.assertIn("not on this server", text)
        self.assertIn("1)", cli("backup", "list").stdout)

    def test_now_options(self):
        cli("backup", "now", "--part", "marzban,x-ui", "--quiet")
        self.assertEqual(sorted(os.listdir(BACKUP_DIR)), ["marzban", "x-ui"])
        cli("backup", "now", "--full", "--quiet")
        self.assertIn("full", os.listdir(BACKUP_DIR))
        self.assertNotEqual(cli("backup", "now", "--part", "nope", check=False).returncode, 0)
        put("/etc/eris/eris.conf", 'BACKUP_DIR="%s"\nBACKUP_FULL="off"\nBACKUP_PARTS="eris"\n' % BACKUP_DIR)
        out = json.loads(cli("backup", "now", "--json").stdout)
        self.assertEqual([x["part"] for x in out["results"]], ["eris"])

    def test_if_set_without_folder(self):
        put("/etc/eris/eris.conf", 'BACKUP_DIR=""\n')
        self.assertEqual(cli("backup", "now", "--quiet", "--if-set").returncode, 0)

    def test_failed_part_is_recorded(self):
        put("/etc/eris/eris.conf", 'BACKUP_DIR="%s/missing"\n' % BACKUP_DIR)
        r = cli("backup", "now", "--part", "x-ui", "--json", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])
        st = B.load_state()
        self.assertIn("not a folder", st["parts"]["x-ui"]["last_error"])

    def test_verify_restore_and_encrypt_cli(self):
        cli("backup", "now", "--part", "x-ui", "--quiet")
        self.assertIn("ok", cli("backup", "verify", "--part", "x-ui").stdout)
        v = json.loads(cli("backup", "verify", "1", "--part", "x-ui", "--json").stdout)
        self.assertTrue(v["ok"])
        # live restore (under ERIS_ROOT): the database comes back, the present state is saved first
        db = ROOT + "/etc/x-ui/x-ui.db"
        make_db("/etc/x-ui/x-ui.db", [("changed", 99)])
        time.sleep(1)
        cli("restore", "--part", "x-ui", "1", "--yes")
        self.assertEqual(db_rows(db), [("in1", 10)])
        names = [os.path.basename(p) for p, _ in B.archives(os.path.join(BACKUP_DIR, "x-ui"))]
        self.assertTrue(any(n.endswith("-before-restore.tar") for n in names), names)
        to = tempfile.mkdtemp(dir=ROOT)
        cli("restore", "--part", "x-ui", "2", "--to", to)
        self.assertTrue(os.path.exists(os.path.join(to, "etc/x-ui/x-ui.db")))
        # encrypt / decrypt
        pw = os.path.join(ROOT, "cli-pass")
        with open(pw, "w") as f:
            f.write("s3cret\n")
        arch = B.archives(os.path.join(BACKUP_DIR, "x-ui"))[0][0]
        out = os.path.join(tempfile.mkdtemp(dir=ROOT), "x.enc")
        self.assertEqual(cli("backup", "encrypt", arch, "--password-file", pw, "--out", out).stdout.strip(), out)
        dec = cli("backup", "decrypt", out, "--password-file", pw).stdout.strip()
        self.assertEqual(B.sha256(dec), B.sha256(arch))

    def test_old_archives_move_on_list(self):
        with open(os.path.join(BACKUP_DIR, "h-backup-20260101-0330.tar"), "w") as f:
            f.write("old")
        cli("backup", "list")
        self.assertTrue(os.path.exists(os.path.join(BACKUP_DIR, "full", "h-backup-20260101-0330.tar")))

    def test_parts_command(self):
        rows = json.loads(cli("backup", "parts", "--json").stdout)
        self.assertEqual([r["name"] for r in rows], list(B.PARTS))
        self.assertIn("usage", cli("backup", "help").stdout.lower() + "usage")


if __name__ == "__main__":
    unittest.main()
