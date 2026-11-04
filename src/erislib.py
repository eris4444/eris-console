"""ERIS Console shared library: settings, Linux VT helpers, fonts and palette.

Used by /usr/local/bin/eris (CLI) and /usr/local/lib/eris/erisd.py (dashboard).
Python 3 standard library only.
"""
import ctypes
import fcntl
import glob
import gzip
import os
import re
import struct
import subprocess
import termios
import time

VERSION = "2.4.1"
REPO = os.environ.get("ERIS_REPO", "eris4444/eris-console")
REPO_URL = "https://github.com/" + REPO
RELEASES_API = "https://api.github.com/repos/%s/releases/latest" % REPO
DOWNLOAD_URL = REPO_URL + "/releases/download/%s/install-eris.sh"
CONF = "/etc/eris/eris.conf"
STATE_DIR = "/var/lib/eris"
RUN_DIR = "/run/eris"
FONT_DIR = "/usr/share/consolefonts"
CONSOLE_SETUP = "/etc/default/console-setup"
SYSCTL_FILE = "/etc/sysctl.d/90-eris-console.conf"
BACKUP_ROOT = "/var/backups/eris"
LOG = "/var/log/eris-build.log"

FULLSCREEN_DEFAULT = ("vim vi nvim view vimdiff vim.basic vim.tiny nano pico emacs editor sensible-editor "
                      "htop top btop atop less more most pager sensible-pager man mc mcedit ncdu tmux screen "
                      "watch tig ranger nmtui iftop nload iotop bmon glances alsamixer whiptail dialog lynx w3m "
                      "elinks mutt cfdisk nmon s-tui")

# key: (default, validator description, validator)
SETTINGS = {
    "DASH_HEIGHT": ("30", "15-70 (% of the console)", lambda v: v.isdigit() and 15 <= int(v) <= 70),
    "DASH_MODE": ("split", "split|full", lambda v: v in ("split", "full")),
    "REFRESH": ("2", "1-30 seconds", lambda v: v.isdigit() and 1 <= int(v) <= 30),
    "WEATHER": ("on", "on|off", lambda v: v in ("on", "off")),
    "WEATHER_CITY": ("", "city name, empty = detect by IP",
                     lambda v: len(v) <= 48 and not re.search(r'["$`\\\n;|&<>]', v)),
    "WEATHER_UNITS": ("C", "C|F", lambda v: v in ("C", "F")),
    "PALETTE": ("neon", "neon|default", lambda v: v in ("neon", "default")),
    "CHARSET": ("auto", "auto|utf8|safe|ascii", lambda v: v in ("auto", "utf8", "safe", "ascii")),
    "PROMPT_SCOPE": ("tty1", "tty1|all", lambda v: v in ("tty1", "all")),
    "DASH_AT_BOOT": ("no", "yes|no", lambda v: v in ("yes", "no")),
    "CONSOLE_FONT": ("", "font name from /usr/share/consolefonts or empty",
                     lambda v: v == "" or (re.fullmatch(r"[A-Za-z0-9._+-]+", v) is not None
                                           and font_path(v) is not None)),
    "BLANK_MINUTES": ("0", "0-60 (0 = never)", lambda v: v.isdigit() and 0 <= int(v) <= 60),
    "KERNEL_MSGS": ("hidden", "hidden|shown", lambda v: v in ("hidden", "shown")),
    "CONSOLE_TTY": ("/dev/tty1", "/dev/ttyN", lambda v: re.fullmatch(r"/dev/tty([1-9]|[1-5][0-9]|6[0-3])", v) is not None),
    "FULLSCREEN_APPS": ("", "extra program names separated by spaces",
                        lambda v: re.fullmatch(r"[A-Za-z0-9._+ -]*", v) is not None),
    "ERIS_GLYPHS": ("on", "on|off", lambda v: v in ("on", "off")),
    "MARZBAN": ("auto", "auto|off", lambda v: v in ("auto", "off")),
    "ALERTS": ("on", "on|off", lambda v: v in ("on", "off")),
    "ALERT_CPU_TEMP": ("80", "50-105 (C)", lambda v: v.isdigit() and 50 <= int(v) <= 105),
    "ALERT_DISK_TEMP": ("55", "35-80 (C)", lambda v: v.isdigit() and 35 <= int(v) <= 80),
    "BACKUP_DIR": ("", "absolute folder path, empty = nightly backup off",
                   lambda v: v == "" or (re.fullmatch(r"/[A-Za-z0-9._/+@-]*", v) is not None
                                         and ".." not in v.split("/"))),
    "BACKUP_KEEP": ("7", "1-90 backups", lambda v: v.isdigit() and 1 <= int(v) <= 90),
    "BACKUP_AT": ("03:30", "HH:MM", lambda v: re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v) is not None),
    "BACKUP_OWNER": ("", "user that owns the backups (e.g. to read them over SMB), empty = root",
                     lambda v: v == "" or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", v) is not None),
}

COMMENTS = {
    "DASH_HEIGHT": "dashboard height in % of the console rows",
    "DASH_MODE": "split = dashboard on top, shell below; full = the whole screen but 2 shell rows (eris full, Alt+F)",
    "REFRESH": "dashboard refresh interval in seconds",
    "WEATHER": "weather panel on|off (Open-Meteo, no API key)",
    "WEATHER_CITY": "empty = detect the city by IP",
    "WEATHER_UNITS": "C or F",
    "PALETTE": "neon = ERIS 16-colour console palette, default = kernel colours",
    "CHARSET": "auto picks utf8/safe/ascii from the glyphs in the console font",
    "PROMPT_SCOPE": "tty1 = physical console only, all = every interactive bash",
    "DASH_AT_BOOT": "start the dashboard at boot (eris enable / eris disable)",
    "CONSOLE_FONT": "e.g. Uni3-Terminus24x12; empty = the console-setup font",
    "BLANK_MINUTES": "console blanking/powerdown after N idle minutes, 0 = never",
    "KERNEL_MSGS": "hidden = kernel messages only in dmesg/journal, not on the console",
    "CONSOLE_TTY": "the physical console used by ERIS",
    "FULLSCREEN_APPS": "extra full-screen programs; built in: vim nano htop top less man mc ncdu tmux watch ...",
    "ERIS_GLYPHS": "on = the console font gets the ERIS glyphs (Claude mascot, weather icons, sparklines, ➜)",
    "MARZBAN": "Marzban panel (users, today's traffic, nodes) when /opt/marzban runs here; off = hidden",
    "ALERTS": "red alert bar on the dashboard (heat, SMART, full disks, failed services, nodes, backups)",
    "ALERT_CPU_TEMP": "alert when a CPU core reaches this temperature (C)",
    "ALERT_DISK_TEMP": "alert when a disk reaches this temperature (C)",
    "BACKUP_DIR": "nightly backup folder (eris backup), empty = off",
    "BACKUP_KEEP": "how many backups to keep; older ones are deleted",
    "BACKUP_AT": "time of the nightly backup",
    "BACKUP_OWNER": "owner of the backup files (they stay readable only by this user), empty = root",
}

# ANSI order 0..15: black red green amber blue purple cyan white, then the bright set
NEON = ["000000", "ff2a55", "22e584", "d97757", "2f7dff", "a855f7", "00d9ff", "d6d6e0",   # 3 = Claude orange
        "5c5f77", "ff5c7a", "5cff9d", "ffd54a", "5ea2ff", "ff4fd8", "5cf2ff", "ffffff"]

# glyph sets; "need" lists what the console font must contain for the set to be usable
GLYPH_SETS = {
    "utf8": {"need": "┌┐└┘─│├┤┬┴█░▒■↑↓°·●…✘"},
    "safe": {"need": "┌┐└┘─│├┤┬┴█░↑↓°"},
    "ascii": {"need": ""},
}


# ---------------------------------------------------------------- settings

def load_conf(path=CONF):
    conf = {k: v[0] for k, v in SETTINGS.items()}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^\s*([A-Z_]+)=(?:"([^"]*)"|(\S*))', line)
                if m and m.group(1) in SETTINGS:
                    val = m.group(2) if m.group(2) is not None else m.group(3)
                    if SETTINGS[m.group(1)][2](val):
                        conf[m.group(1)] = val
    except FileNotFoundError:
        pass
    return conf


def validate(key, value):
    if key not in SETTINGS:
        raise ValueError("unknown setting %s" % key)
    if not SETTINGS[key][2](value):
        raise ValueError("invalid value for %s (expected %s)" % (key, SETTINGS[key][1]))


def save_conf(updates, path=CONF):
    """Update KEY="value" lines in place, keep comments and unknown lines."""
    for k, v in updates.items():
        validate(k, v)
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = ["# ERIS Console settings. Change them with: eris  (menu)  or  eris set KEY VALUE"]
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r'^\s*([A-Z_]+)=', line)
        if m and m.group(1) in updates:
            lines[i] = '%s="%s"' % (m.group(1), updates[m.group(1)])
            done.add(m.group(1))
    for k, v in updates.items():
        if k not in done:
            lines.append("# %s" % COMMENTS.get(k, k))
            lines.append('%s="%s"' % (k, v))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def default_conf_text():
    out = ["# ERIS Console settings. Change them with: eris  (menu)  or  eris set KEY VALUE", ""]
    for k, (d, _, _) in SETTINGS.items():
        out.append("# %s" % COMMENTS[k])
        out.append('%s="%s"' % (k, d))
    return "\n".join(out) + "\n"


def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def log(msg, tag="eris"):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg))
    except OSError:
        pass


# ---------------------------------------------------------------- VT helpers

def tty_index(tty):
    m = re.fullmatch(r"/dev/tty(\d+)", tty)
    return int(m.group(1)) if m else None


def tty_rdev(tty):
    st = os.stat(tty)
    return os.major(st.st_rdev), os.minor(st.st_rdev)


def open_tty(tty, write=True):
    return os.open(tty, (os.O_WRONLY if write else os.O_RDONLY) | os.O_NOCTTY)


def write_all(fd, data):
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def tty_write(tty, data):
    fd = open_tty(tty)
    try:
        write_all(fd, data)
    finally:
        os.close(fd)


VT_GETCONSIZECSRPOS = 0x80085610   # _IOR('V', 0x10, struct vt_consizecsrpos), Linux >= 6.4


def console_geometry(fd, tty=None):
    """(rows, cols, cursor_row, cursor_col) of a VT, 0-based cursor."""
    try:
        r = fcntl.ioctl(fd, VT_GETCONSIZECSRPOS, bytes(8))
        rows, cols, crow, ccol = struct.unpack("4H", r)
        return rows, cols, crow, ccol
    except OSError:
        pass
    ws = fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8))
    rows, cols = struct.unpack("4H", ws)[:2]
    n = tty_index(tty) if tty else None
    crow = ccol = 0
    if n:
        with open("/dev/vcsa%d" % n, "rb") as f:
            h = f.read(4)
        ccol, crow = h[2], h[3]
    return rows, cols, crow, ccol


KDFONTOP = 0x4B72


def font_info(tty):
    """(width, height, glyph count) of the font on a VT."""
    fd = open_tty(tty, write=False)
    try:
        buf = ctypes.create_string_buffer(512 * 32 * 8)
        op = struct.pack("IIIIIP", 1, 0, 64, 32, 512, ctypes.addressof(buf))
        r = fcntl.ioctl(fd, KDFONTOP, op)
        return struct.unpack("IIIIIP", r)[2:5]
    finally:
        os.close(fd)


class _UniPair(ctypes.Structure):
    _fields_ = [("unicode", ctypes.c_ushort), ("fontpos", ctypes.c_ushort)]


class _UniDesc(ctypes.Structure):
    _fields_ = [("entry_ct", ctypes.c_ushort), ("entries", ctypes.POINTER(_UniPair))]


def tty_unicode_set(tty):
    """Unicode code points the VT can display (from its unicode map, KDGETUNIMAP)."""
    arr = (_UniPair * 16384)()
    desc = _UniDesc(16384, arr)
    fd = open_tty(tty, write=False)
    try:
        fcntl.ioctl(fd, 0x4B66, desc)
    finally:
        os.close(fd)
    return {arr[i].unicode for i in range(desc.entry_ct)}


def choose_charset(conf, tty):
    """Resolve CHARSET=auto to utf8/safe/ascii from the glyphs in the live console font."""
    cs = conf.get("CHARSET", "auto")
    if cs != "auto":
        return cs
    try:
        have = tty_unicode_set(tty)
    except OSError:
        return "ascii"
    for name in ("utf8", "safe"):
        if all(ord(c) in have for c in GLYPH_SETS[name]["need"]):
            return name
    return "ascii"


def psf_header(path):
    """(width, height, glyph count, has unicode table) of a .psf/.psf.gz file."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        head = f.read(32)
    if head[:2] == b"\x36\x04":
        mode, size = head[2], head[3]
        return 8, size, 512 if mode & 1 else 256, bool(mode & 2)
    if head[:4] == b"\x72\xb5\x4a\x86":
        _, _, _, flags, n, _, h, w = struct.unpack("<8I", head)
        return w, h, n, bool(flags & 1)
    raise ValueError("not a PSF font: %s" % path)


def font_path(name):
    for ext in (".psf.gz", ".psfu.gz", ".psf", ".psfu"):
        p = os.path.join(FONT_DIR, name + ext)
        if os.path.isfile(p):
            return p
    return None


def system_font():
    """The font console-setup loads at boot (from its generated setup script), or None."""
    script = rd("/etc/console-setup/cached_setup_font.sh", "") or ""
    m = re.search(r"setfont\b[^\n]*?'?(/[^'\s]+\.psfu?(?:\.gz)?)'?", script)
    if m and os.path.isfile(m.group(1)):
        return m.group(1)
    cached = sorted(glob.glob("/etc/console-setup/cached_*.psf*"))
    if cached:
        return cached[0]
    cs = read_console_setup()
    if cs.get("FONT"):
        p = cs["FONT"].split()[0]
        return p if p.startswith("/") else font_path(re.sub(r"\.psfu?(\.gz)?$", "", p))
    return None


def read_console_setup():
    out = {}
    try:
        for line in open(CONSOLE_SETUP, encoding="utf-8"):
            m = re.match(r'^\s*([A-Z_]+)=["\']?([^"\'\n]*)', line)
            if m:
                out[m.group(1)] = m.group(2).strip()
    except FileNotFoundError:
        pass
    return out


TERMINUS_RE = re.compile(r"^(Uni3|Uni2|Lat15)-(Terminus|TerminusBold|TerminusBoldVGA)(\d+)(?:x(\d+))?\.psf\.gz$")


def terminus_fonts():
    """Best installed Terminus font per (face, size): Uni3 > Uni2 > Lat15.

    Returns a list of dicts sorted by glyph size: name, path, face, w, h, charset.
    """
    rank = {"Uni3": 0, "Uni2": 1, "Lat15": 2}
    best = {}
    try:
        names = os.listdir(FONT_DIR)
    except FileNotFoundError:
        return []
    for fn in names:
        m = TERMINUS_RE.match(fn)
        if not m:
            continue
        cs, face, a, b = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        h, w = (a, int(b)) if b else (a, 8)
        key = (face, w, h)
        if key not in best or rank[cs] < rank[best[key]["charset"]]:
            best[key] = {"name": fn[:-len(".psf.gz")], "path": os.path.join(FONT_DIR, fn),
                         "face": face, "w": w, "h": h, "charset": cs}
    return sorted(best.values(), key=lambda f: (f["h"], f["w"], f["face"]))


def fb_resolution():
    try:
        w, h = open("/sys/class/graphics/fb0/virtual_size").read().strip().split(",")
        return int(w), int(h)
    except (OSError, ValueError):
        return None


def set_font(tty, path):
    r = subprocess.run(["setfont", "-C", tty, path], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError("setfont failed: %s" % (r.stderr.strip() or r.returncode))


def fbcon_ready():
    """True once a framebuffer console (fbcon) is bound, i.e. the KMS driver took over."""
    for p in glob.glob("/sys/class/vtconsole/vtcon*"):
        try:
            name = open(p + "/name").read()
            bound = open(p + "/bind").read().strip() == "1"
        except OSError:
            continue
        if bound and "frame buffer" in name:
            return True
    return False


# ---------------------------------------------------------------- palette / console look

def palette_seq(name):
    if name != "neon":
        return "\033]R"
    return "".join("\033]P%X%s" % (i, hx) for i, hx in enumerate(NEON))


VT_PARAM = "/sys/module/vt/parameters/default_%s"


def read_vt_default():
    return {c: open(VT_PARAM % c).read().strip() for c in ("red", "grn", "blu")}


def write_vt_default(values):
    for c, v in values.items():
        with open(VT_PARAM % c, "w") as f:
            f.write(v)


def neon_vt_default():
    cols = [(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)) for h in NEON]
    return {"red": ",".join(str(c[0]) for c in cols),
            "grn": ",".join(str(c[1]) for c in cols),
            "blu": ",".join(str(c[2]) for c in cols)}


def blank_seq(minutes):
    m = int(minutes)
    return "\033[9;%d]\033[14;%d]" % (m, m)


def prompt_glyphs(tty):
    try:
        have = tty_unicode_set(tty)
    except OSError:
        have = set()
    arrow = "➜" if ord("➜") in have else ">"
    err = "✘" if ord("✘") in have else "x"
    return arrow, err


def fullscreen_apps(conf):
    return sorted(set(FULLSCREEN_DEFAULT.split()) | set(conf.get("FULLSCREEN_APPS", "").split()))


def write_prompt_env(tty, conf=None):
    """Glyphs the console font can draw + full-screen program names, read by /etc/eris/prompt.bash."""
    arrow, err = prompt_glyphs(tty)
    apps = " ".join(fullscreen_apps(conf or load_conf()))
    os.makedirs(RUN_DIR, exist_ok=True)
    tmp = os.path.join(RUN_DIR, "prompt.env.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("ERIS_ARROW='%s'\nERIS_ERRMARK='%s'\nERIS_FULLSCREEN='%s'\n" % (arrow, err, apps))
    os.replace(tmp, os.path.join(RUN_DIR, "prompt.env"))
    return arrow, err


def printk():
    return open("/proc/sys/kernel/printk").read().split()


def set_kernel_msgs(mode):
    """hidden: console loglevel 1 (journal/dmesg keep everything); shown: original value."""
    orig = None
    try:
        orig = open(os.path.join(STATE_DIR, "printk.orig")).read().split()
    except OSError:
        pass
    if mode == "hidden":
        with open(SYSCTL_FILE + ".tmp", "w") as f:
            f.write("# ERIS Console: keep kernel messages off the physical console (they stay in dmesg/journal).\n"
                    "# Remove this file (or: eris set KERNEL_MSGS shown) to get them back.\n"
                    "kernel.printk = 1 4 1 7\n")
        os.replace(SYSCTL_FILE + ".tmp", SYSCTL_FILE)
        value = "1 4 1 7"
    else:
        try:
            os.unlink(SYSCTL_FILE)
        except FileNotFoundError:
            pass
        value = " ".join(orig) if orig else "4 4 1 7"
    with open("/proc/sys/kernel/printk", "w") as f:
        f.write(value)
    return value


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    import socket
    if path.startswith("@"):
        path = "\0" + path[1:]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(path)
        s.sendall(msg.encode())
    except OSError:
        pass
    finally:
        s.close()


def human(n, suffix=""):
    if n is None:
        return "N/A"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or unit == "P":
            if unit == "B":
                return "%d%s%s" % (n, unit, suffix)
            return ("%.1f%s%s" if abs(n) < 100 else "%.0f%s%s") % (n, unit, suffix)
        n /= 1024.0
    return "N/A"
