#!/bin/bash
# ERIS Console installer (self-contained).
#   Layer A: neon black console + one-line prompt on the physical console (tty1), always on.
#   Layer B: eris-dashboard.service, installed but OFF (start it with: eris on).
#
#   bash install-eris.sh                 install or upgrade
#   bash install-eris.sh --uninstall     remove everything (same as eris-uninstall)
#   bash install-eris.sh --no-selftest   skip the checks at the end
set -euo pipefail
VERSION=2.0.0
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

write_uninstaller() {
  put_file "$1" 0755 << 'ERIS_EOF_eris_uninstall'
#!/bin/bash
# eris-uninstall: remove ERIS Console completely and put back what it changed.
# Backups made by the installer stay in /var/backups/eris.
set -u
[ "$(id -u)" -eq 0 ] || { echo "eris-uninstall: run as root" >&2; exit 1; }
LOG=/var/log/eris-build.log
ST=/var/lib/eris
say() { printf '[ERIS] %s\n' "$*"; printf '%s [uninstall] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

TTY=/dev/tty1
if [ -r /etc/eris/eris.conf ]; then
  t=$(sed -n 's/^CONSOLE_TTY="\(.*\)"$/\1/p' /etc/eris/eris.conf | tail -n 1)
  [ -n "$t" ] && TTY=$t
fi

# 1. services first: the dashboard hands the screen back when it stops
for u in eris-dashboard.service eris-console.service; do
  if systemctl cat "$u" > /dev/null 2>&1; then
    timeout 30 systemctl disable --now "$u" > /dev/null 2>&1
    say "stopped and disabled $u"
  fi
done

# 2. kernel default palette, console blanking, scroll region / origin mode
if [ -r "$ST/vt-default.orig" ]; then
  while IFS='=' read -r k v; do
    [ -w "/sys/module/vt/parameters/default_$k" ] && printf '%s' "$v" > "/sys/module/vt/parameters/default_$k"
  done < "$ST/vt-default.orig"
  say "kernel default palette restored"
fi
blank=0
[ -r "$ST/consoleblank.orig" ] && blank=$(( $(cat "$ST/consoleblank.orig") / 60 ))
if [ -c "$TTY" ]; then
  printf '\033]R\0337\033[r\033[?6l\0338\033[0m\033[9;%d]\033[14;%d]' "$blank" "$blank" > "$TTY"
  say "$TTY reset: palette, scroll region (ESC[r), origin mode (ESC[?6l), blanking ${blank} min"
fi

# 3. console-setup and the console font
if [ -r "$ST/console-setup.orig" ] && ! cmp -s "$ST/console-setup.orig" /etc/default/console-setup; then
  cp "$ST/console-setup.orig" /etc/default/console-setup
  timeout 90 setupcon --save-only > /dev/null 2>&1
  say "/etc/default/console-setup restored (setupcon --save-only)"
fi
if [ -r "$ST/cached-created" ]; then
  while read -r f; do
    case $f in /etc/console-setup/cached_*) [ -e "$f" ] && rm -f "$f" && say "removed $f (made by ERIS)" ;; esac
  done < "$ST/cached-created"
fi
font=$(sed -n "s/^setfont '\{0,1\}\(\/[^' ]*\.psfu\{0,1\}\(\.gz\)\{0,1\}\).*/\1/p" /etc/console-setup/cached_setup_font.sh 2> /dev/null | head -n 1)
[ -n "$font" ] || font=$(cat "$ST/system-font.orig" 2> /dev/null)
if [ -n "$font" ] && [ -c "$TTY" ] && timeout 20 setfont -C "$TTY" "$font"; then
  say "console font: $font"
fi

# 4. kernel messages back on the console
if [ -e /etc/sysctl.d/90-eris-console.conf ]; then
  rm -f /etc/sysctl.d/90-eris-console.conf
  p="4 4 1 7"
  [ -r "$ST/printk.orig" ] && p=$(cat "$ST/printk.orig")
  printf '%s' "$p" > /proc/sys/kernel/printk
  say "removed /etc/sysctl.d/90-eris-console.conf, kernel.printk = $p"
fi

# 5. only the ERIS blocks in the shell files
for f in /root/.bashrc /root/.profile; do
  if [ -f "$f" ] && grep -q '^# >>> ERIS CONSOLE' "$f"; then
    sed -i '/^# >>> ERIS CONSOLE/,/^# <<< ERIS CONSOLE/d' "$f"
    say "removed the ERIS block from $f"
  fi
done

# 6. files
for f in /etc/systemd/system/eris-dashboard.service /etc/systemd/system/eris-console.service \
         /usr/local/bin/eris /usr/local/lib/eris /etc/eris /run/eris "$ST"; do
  if [ -e "$f" ]; then
    rm -rf "$f"
    say "removed $f"
  fi
done
systemctl daemon-reload
rm -f /usr/local/sbin/eris-uninstall
say "removed /usr/local/sbin/eris-uninstall"
say "ERIS Console is gone. Open a new shell (or: exec bash -l) to drop the prompt in this one."
ERIS_EOF_eris_uninstall
}
write_files() {
  put_file /usr/local/lib/eris/erislib.py 0644 << 'ERIS_EOF_erislib_py'
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

VERSION = "2.0.0"
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
}

COMMENTS = {
    "DASH_HEIGHT": "dashboard height in % of the console rows",
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
ERIS_EOF_erislib_py
  put_file /usr/local/lib/eris/erisd.py 0755 << 'ERIS_EOF_erisd_py'
#!/usr/bin/python3
"""ERIS dashboard: a live system panel in the top part of the physical console.

The console's own scroll region (DECSTBM) + origin mode (DECOM) keep the user's real
bash in the bottom part; this process only paints the top rows, one atomic write per
refresh (ESC 7 ... ESC 8). Python standard library only.

    erisd.py                      run as the eris-dashboard.service daemon
    erisd.py --render RxC [CS]    print one frame to stdout (testing)
"""
import errno
import fcntl
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import erislib as E  # noqa: E402

TICK = 0.2


# ================================================================ small helpers

def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def rint(path):
    v = rd(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, errors="replace")
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def valid_temp(milli):
    """milli-degree hwmon value -> degrees C, or None for nonsense (-273.2, 0xFFFF...)."""
    if milli is None:
        return None
    t = milli / 1000.0
    return t if -30.0 < t < 150.0 else None


def fmt_dur(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def rate(n):
    return E.human(n, "/s") if n is not None else "N/A"


# ================================================================ collectors

class Cpu:
    def __init__(self):
        self.model, self.cores, self.threads = "N/A", 0, 0
        self.prev = None
        self.max_seen = None
        self.core_of = {}
        self.throttle_prev = None
        self.throttle_state = "OK"
        self.hwmon = None
        for line in rd("/proc/cpuinfo", "").splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "model name":
                self.model = re.sub(r"\s+", " ", v.strip())
                break
        cores = set()
        for d in sorted(os.listdir("/sys/devices/system/cpu")):
            if re.fullmatch(r"cpu\d+", d) and os.path.exists("/sys/devices/system/cpu/%s/topology" % d):
                t = "/sys/devices/system/cpu/%s/topology/" % d
                self.core_of[int(d[3:])] = rint(t + "core_id")
                cores.add((rint(t + "physical_package_id"), rint(t + "core_id")))
        self.threads = len(self.core_of) or os.cpu_count() or 1
        self.cores = len(cores) or self.threads
        for h in sorted(os.listdir("/sys/class/hwmon")):
            if rd("/sys/class/hwmon/%s/name" % h) in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                self.hwmon = "/sys/class/hwmon/" + h
                break
        self.sample()

    def _stat(self):
        out = {}
        for line in rd("/proc/stat", "").splitlines():
            if line.startswith("cpu"):
                p = line.split()
                vals = [int(x) for x in p[1:9]]
                idle = vals[3] + vals[4]
                out[p[0]] = (sum(vals), idle)
        return out

    def _freqs(self):
        res = {}
        for cpu in self.core_of:
            f = rint("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % cpu)
            if f:
                res[cpu] = f / 1000.0
        if not res:
            cur = None
            for line in rd("/proc/cpuinfo", "").splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "processor":
                    cur = int(v)
                elif k.strip() == "cpu MHz" and cur is not None:
                    res[cur] = float(v)
        return res

    def _temps(self):
        cores, pkg, crit = {}, None, None
        if not self.hwmon:
            return cores, pkg, crit
        for f in os.listdir(self.hwmon):
            m = re.fullmatch(r"temp(\d+)_input", f)
            if not m:
                continue
            t = valid_temp(rint("%s/%s" % (self.hwmon, f)))
            label = rd("%s/temp%s_label" % (self.hwmon, m.group(1)), "")
            c = valid_temp(rint("%s/temp%s_crit" % (self.hwmon, m.group(1))))
            if c:
                crit = c
            if label.startswith("Core "):
                cores[int(label.split()[1])] = t
            elif label.startswith(("Package", "Tdie", "Tctl")) or (not label and pkg is None):
                pkg = t
        return cores, pkg, crit

    def _throttle(self):
        total = 0
        found = False
        for cpu in self.core_of:
            for f in ("core_throttle_count", "package_throttle_count"):
                v = rint("/sys/devices/system/cpu/cpu%d/thermal_throttle/%s" % (cpu, f))
                if v is not None:
                    total += v
                    found = True
        return total if found else None

    def sample(self):
        st = self._stat()
        usage = {}
        if self.prev:
            for k, (tot, idle) in st.items():
                pt, pi = self.prev.get(k, (tot, idle))
                dt = tot - pt
                usage[k] = max(0.0, min(100.0, 100.0 * (dt - (idle - pi)) / dt)) if dt > 0 else 0.0
        self.prev = st
        temps, pkg, crit = self._temps()
        freqs = self._freqs()
        per = []
        for cpu in sorted(self.core_of):
            core = self.core_of[cpu]
            per.append({"cpu": cpu, "usage": usage.get("cpu%d" % cpu), "mhz": freqs.get(cpu),
                        "temp": temps.get(core) if core is not None else None})
        hottest = max([t for t in list(temps.values()) + [pkg] if t is not None], default=None)
        if hottest is not None:
            self.max_seen = hottest if self.max_seen is None else max(self.max_seen, hottest)
        thr = self._throttle()
        if thr is None:
            self.throttle_state = "N/A"
        else:
            if self.throttle_prev is not None and thr > self.throttle_prev:
                self.throttle_state = "THROTTLING"
            elif self.throttle_state == "THROTTLING":
                self.throttle_state = "recovered"
            elif self.throttle_state != "recovered":
                self.throttle_state = "OK"
            self.throttle_prev = thr
        la = (rd("/proc/loadavg", "0 0 0").split() + ["0", "0", "0"])[:3]
        return {"model": self.model, "cores": self.cores, "threads": self.threads,
                "usage": usage.get("cpu"), "per": per, "load": [float(x) for x in la],
                "pkg": pkg, "hottest": hottest, "max_seen": self.max_seen, "crit": crit,
                "throttle": self.throttle_state, "throttle_count": thr,
                "cpufreq": os.path.exists("/sys/devices/system/cpu/cpu0/cpufreq")}


def mem_sample():
    mi = {}
    for line in rd("/proc/meminfo", "").splitlines():
        k, _, v = line.partition(":")
        p = v.split()
        if p:
            mi[k] = int(p[0]) * 1024
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    cache = mi.get("Buffers", 0) + mi.get("Cached", 0) + mi.get("SReclaimable", 0)
    st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    return {"total": total, "used": total - avail, "free": mi.get("MemFree", 0), "avail": avail,
            "cache": cache, "pct": 100.0 * (total - avail) / total if total else None,
            "swap_total": st, "swap_used": st - sf, "swap_pct": 100.0 * (st - sf) / st if st else None}


class Gpu:
    def __init__(self):
        self.devs = []
        base = "/sys/bus/pci/devices"
        for slot in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            cls = rd("%s/%s/class" % (base, slot), "")
            if not cls.startswith("0x03"):
                continue
            path = os.path.realpath("%s/%s" % (base, slot))
            drv = os.path.basename(os.path.realpath(path + "/driver")) if os.path.exists(path + "/driver") else "none"
            name = None
            out = run(["lspci", "-mm", "-s", slot], timeout=5)
            if out:
                parts = re.findall(r'"([^"]*)"', out)
                if len(parts) >= 3:
                    vendor = parts[1].replace(" Corporation", "").replace(" Technologies Inc.", "")
                    name = "%s %s" % (vendor.split()[0] if vendor else "", parts[2])
            hw = None
            for h in os.listdir("/sys/class/hwmon"):
                if os.path.realpath("/sys/class/hwmon/%s/device" % h) == path:
                    hw = "/sys/class/hwmon/" + h
            vram = None
            if drv == "amdgpu":
                vram = rint(path + "/mem_info_vram_total")
            elif drv == "nouveau":
                k = run(["dmesg"], timeout=5) or ""
                m = re.search(r"nouveau %s: drm: VRAM: (\d+) MiB" % re.escape(slot), k)
                vram = int(m.group(1)) * 1024 * 1024 if m else None
            self.devs.append({"slot": slot, "path": path, "driver": drv, "name": name or slot,
                              "hwmon": hw, "vram": vram})

    def sample(self):
        out = []
        for d in self.devs:
            hw = d["hwmon"]
            temp = valid_temp(rint(hw + "/temp1_input")) if hw else None
            fan = rint(hw + "/fan1_input") if hw else None
            pwm, pwm_max = (rint(hw + "/pwm1"), rint(hw + "/pwm1_max")) if hw else (None, None)
            mv = rint(hw + "/in0_input") if hw else None
            util = rint(d["path"] + "/gpu_busy_percent")
            vram_used = rint(d["path"] + "/mem_info_vram_used")
            dpms = None
            for c in os.listdir("/sys/class/drm"):
                if re.fullmatch(r"card\d+-.+", c) and rd("/sys/class/drm/%s/status" % c) == "connected" \
                        and os.path.realpath("/sys/class/drm/%s/device" % c.split("-")[0]) == d["path"]:
                    dpms = "%s %s" % (c.split("-", 1)[1], rd("/sys/class/drm/%s/dpms" % c, "?"))
            out.append(dict(d, temp=temp, fan=fan,
                            fan_pct=(100.0 * pwm / pwm_max) if (pwm is not None and pwm_max) else None,
                            mv=mv, util=util, vram_used=vram_used, dpms=dpms,
                            pm=rd(d["path"] + "/power/runtime_status")))
        return out


class Net:
    def __init__(self):
        self.prev = None

    @staticmethod
    def default_route():
        for line in (rd("/proc/net/route", "") or "").splitlines()[1:]:
            p = line.split()
            if len(p) > 7 and p[1] == "00000000" and int(p[3], 16) & 2:
                gw = socket.inet_ntoa(struct.pack("<I", int(p[2], 16)))
                return p[0], gw
        return None, None

    @staticmethod
    def ipv4(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack("256s", ifname.encode()[:15])
            ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])     # SIOCGIFADDR
            mask = fcntl.ioctl(s.fileno(), 0x891B, req)[20:24]                       # SIOCGIFNETMASK
            prefix = bin(struct.unpack(">I", mask)[0]).count("1")
            return "%s/%d" % (ip, prefix)
        except OSError:
            return None
        finally:
            s.close()

    def sample(self):
        ifname, gw = self.default_route()
        if not ifname:
            for n in sorted(os.listdir("/sys/class/net")):
                if n != "lo" and rd("/sys/class/net/%s/operstate" % n) == "up":
                    ifname = n
                    break
        if not ifname:
            return None
        b = "/sys/class/net/%s/" % ifname
        now = time.monotonic()
        cnt = {k: rint(b + "statistics/" + k) or 0 for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors")}
        rx = tx = None
        if self.prev and self.prev[0] == ifname and now > self.prev[1]:
            dt = now - self.prev[1]
            rx = max(0, cnt["rx_bytes"] - self.prev[2]["rx_bytes"]) / dt
            tx = max(0, cnt["tx_bytes"] - self.prev[2]["tx_bytes"]) / dt
        self.prev = (ifname, now, cnt)
        speed = rint(b + "speed")
        ip6 = None
        for line in (rd("/proc/net/if_inet6", "") or "").splitlines():
            p = line.split()
            if len(p) == 6 and p[5] == ifname and p[3] == "00":
                ip6 = ":".join(p[0][i:i + 4] for i in range(0, 32, 4))
                break
        drv = os.path.basename(os.path.realpath(b + "device/driver")) if os.path.exists(b + "device/driver") else None
        others = [n for n in sorted(os.listdir("/sys/class/net")) if n not in ("lo", ifname)
                  and rd("/sys/class/net/%s/operstate" % n) == "up"]
        return {"if": ifname, "gw": gw, "ip": self.ipv4(ifname), "ip6": ip6, "state": rd(b + "operstate", "?"),
                "carrier": rint(b + "carrier"), "speed": speed if speed and speed > 0 else None,
                "duplex": rd(b + "duplex"), "driver": drv, "rx_rate": rx, "tx_rate": tx, "others": others, **cnt}


class DiskIO:
    def __init__(self):
        self.prev = None

    def sample(self, disks):
        now = time.monotonic()
        cur = {}
        for line in (rd("/proc/diskstats", "") or "").splitlines():
            p = line.split()
            if len(p) > 10 and p[2] in disks:
                cur[p[2]] = (int(p[5]) * 512, int(p[9]) * 512)
        per = {}
        if self.prev and now > self.prev[0]:
            dt = now - self.prev[0]
            for k in cur:
                if k in self.prev[1]:
                    per[k] = (max(0, cur[k][0] - self.prev[1][k][0]) / dt, max(0, cur[k][1] - self.prev[1][k][1]) / dt)
        self.prev = (now, cur)
        if not per:
            return (None, None), per
        return (sum(v[0] for v in per.values()), sum(v[1] for v in per.values())), per


class Worker(threading.Thread):
    """Background collector: a failure only leaves the last good snapshot (or None)."""

    def __init__(self, name):
        threading.Thread.__init__(self, name=name, daemon=True)
        self.snap = None
        self.stop_evt = threading.Event()
        self.wake = threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            try:
                wait = self.step()
            except Exception as e:  # keep the dashboard alive whatever a sensor does
                sys.stderr.write("eris: %s collector error: %r\n" % (self.name, e))
                wait = 30
            self.wake.wait(wait)
            self.wake.clear()

    def stop(self):
        self.stop_evt.set()
        self.wake.set()


class Storage(Worker):
    LSBLK = ["lsblk", "-J", "-b", "-o",
             "NAME,KNAME,PATH,TYPE,SIZE,MODEL,VENDOR,TRAN,ROTA,RM,RO,FSTYPE,LABEL,MOUNTPOINTS,PKNAME,STATE"]

    def __init__(self):
        Worker.__init__(self, "storage")
        self.tree = []
        self.smart = {}
        self.next_lsblk = 0
        self.next_smart = 0

    def step(self):
        now = time.monotonic()
        if now >= self.next_lsblk:
            out = run(self.LSBLK, timeout=10)
            if out:
                self.tree = json.loads(out).get("blockdevices", [])
            self.next_lsblk = now + 30
        self.snap = self.build()
        if now >= self.next_smart:
            for d in self.tree:
                if d.get("type") == "disk" and not self.is_virtual(d):
                    self.smart[d["name"]] = self.read_smart(d)
            self.next_smart = now + 120
            self.snap = self.build()
        return 5

    @staticmethod
    def is_virtual(d):
        name = d.get("kname") or d.get("name", "")
        return name.startswith(("loop", "zram", "ram", "nbd", "sr")) or \
            os.path.realpath("/sys/class/block/%s" % name).startswith("/sys/devices/virtual/")

    @staticmethod
    def read_smart(d):
        """Read-only SMART query that never spins up a sleeping disk (-n standby)."""
        res = {"temp": None, "health": None, "note": None}
        hw = Storage.drivetemp(d["name"])
        if hw is not None:
            res["temp"] = hw
        out = run(["smartctl", "-j", "-n", "standby", "-H", "-A", "-i", d["path"]], timeout=25)
        if out:
            try:
                j = json.loads(out)
            except ValueError:
                j = {}
            msgs = " ".join(m.get("string", "") for m in j.get("smartctl", {}).get("messages", []))
            if re.search(r"STANDBY|SLEEP", msgs, re.I) and (d.get("tran") != "usb"):
                res["note"] = "standby"
            sup = j.get("smart_support", {})
            if j.get("smart_status") is not None:
                res["health"] = "PASSED" if j["smart_status"].get("passed") else "FAILED"
            elif sup.get("available") and not sup.get("enabled"):
                res["note"] = "SMART off"
            elif sup.get("enabled") and j.get("smartctl", {}).get("exit_status", 0) & 4:
                res["note"] = "SMART err"      # drive/bridge aborts SMART reads
            elif not res["note"]:
                res["note"] = "no SMART"
            t = j.get("temperature", {}).get("current")
            if res["temp"] is None and isinstance(t, (int, float)) and 0 < t < 120:
                res["temp"] = float(t)
        if res["temp"] is None and d["name"].startswith("nvme"):
            out = run(["nvme", "smart-log", "-o", "json", d["path"]], timeout=15)
            try:
                k = json.loads(out or "{}").get("temperature")
                if k:
                    res["temp"] = float(k) - 273.15 if k > 200 else float(k)
            except (ValueError, TypeError):
                pass
        return res

    @staticmethod
    def drivetemp(name):
        for h in os.listdir("/sys/class/hwmon"):
            if rd("/sys/class/hwmon/%s/name" % h) == "drivetemp":
                blk = "/sys/class/hwmon/%s/device/block/%s" % (h, name)
                if os.path.exists(blk):
                    return valid_temp(rint("/sys/class/hwmon/%s/temp1_input" % h))
        return None

    @staticmethod
    def link(d):
        name = d["name"]
        real = os.path.realpath("/sys/block/" + name)
        if d.get("tran") == "usb" or "/usb" in real:
            p = real
            while p and p != "/":
                if os.path.exists(p + "/speed") and os.path.exists(p + "/idVendor"):
                    sp = rd(p + "/speed")
                    return "USB %sM" % sp if sp else "USB"
                p = os.path.dirname(p)
            return "USB"
        m = re.search(r"/ata(\d+)/", real)
        if m:
            spd = rd("/sys/class/ata_link/link%s/sata_spd" % m.group(1))
            if spd and "unknown" not in spd:
                return "SATA %s" % spd.replace(" Gbps", "G")
            return "ATA"
        if name.startswith("nvme"):
            ctrl = re.sub(r"n\d+$", "", name)
            sp = rd("/sys/class/nvme/%s/device/current_link_speed" % ctrl)
            wd = rd("/sys/class/nvme/%s/device/current_link_width" % ctrl)
            return "PCIe %s x%s" % (sp.split()[0], wd) if sp and wd else "PCIe"
        return d.get("tran") or ""

    @staticmethod
    def kind(d):
        name = d["name"]
        if name.startswith("nvme") or d.get("tran") == "nvme":
            return "NVMe"
        if d.get("tran") == "usb":
            return "USB"
        if name.startswith("mmcblk"):
            return "MMC"
        return "HDD" if d.get("rota") else "SSD"

    def build(self):
        swaps = {}
        for line in (rd("/proc/swaps", "") or "").splitlines()[1:]:
            p = line.split()
            if len(p) >= 4:
                swaps[os.path.realpath(p[0])] = (int(p[2]) * 1024, int(p[3]) * 1024)
        phys, virt = [], []

        def part_entry(c, depth):
            mounts = [m for m in (c.get("mountpoints") or []) if m]
            e = {"name": c["name"], "fs": c.get("fstype") or "", "size": c.get("size"),
                 "mounts": mounts, "used": None, "free": None, "total": None, "pct": None,
                 "depth": depth, "type": c.get("type")}
            real_mounts = [m for m in mounts if m.startswith("/")]
            if real_mounts:
                try:
                    sv = os.statvfs(real_mounts[0])
                    total = sv.f_blocks * sv.f_frsize
                    used = (sv.f_blocks - sv.f_bfree) * sv.f_frsize
                    avail = sv.f_bavail * sv.f_frsize
                    e.update(total=total, used=used, free=avail,
                             pct=math.ceil(100.0 * used / (used + avail)) if used + avail else 0)
                except OSError:
                    pass
            elif "[SWAP]" in mounts:
                sw = swaps.get(os.path.realpath(c.get("path", "")))
                if sw:
                    e.update(total=sw[0], used=sw[1], free=sw[0] - sw[1],
                             pct=math.ceil(100.0 * sw[1] / sw[0]) if sw[0] else 0)
            elif c.get("type") == "part" and (c.get("size") or 0) <= 4096 and not c.get("fstype"):
                e["fs"] = "ext."
            return e

        def walk(node, depth, acc):
            for c in node.get("children") or []:
                acc.append(part_entry(c, depth))
                walk(c, depth + 1, acc)

        for d in self.tree:
            parts = []
            if d.get("fstype") or d.get("mountpoints") and any(d.get("mountpoints")):
                parts.append(part_entry(d, 1))
            walk(d, 1, parts)
            sm = self.smart.get(d["name"], {})
            vendor = (d.get("vendor") or "").strip()
            model = (d.get("model") or "").strip()
            if vendor.upper() in ("ATA", "NVME") or model.upper().startswith(vendor.upper()):
                vendor = ""
            entry = {"name": d["name"], "model": " ".join(x for x in (vendor, model) if x) or "N/A",
                     "size": d.get("size"), "kind": self.kind(d), "temp": sm.get("temp"),
                     "health": sm.get("health"), "note": sm.get("note"), "link": "", "parts": parts,
                     "ro": d.get("ro"), "state": d.get("state")}
            if self.is_virtual(d):
                virt.append(entry)
            else:
                entry["link"] = self.link(d)
                phys.append(entry)
        return {"phys": phys, "virt": virt, "time": time.time()}


SERVICE_CANDIDATES = [
    ("ssh", "ssh.service"), ("nginx", "nginx.service"), ("apache", "apache2.service"),
    ("docker", "docker.service"), ("marzban", "marzban.service"), ("xray", "xray.service"),
    ("x-ui", "x-ui.service"), ("3x-ui", "3x-ui.service"), ("sing-box", "sing-box.service"),
    ("smbd", "smbd.service"), ("nmbd", "nmbd.service"), ("nfs", "nfs-server.service"),
    ("omv", "openmediavault-engined.service"), ("php-fpm", "php*-fpm.service"),
    ("mariadb", "mariadb.service"), ("mysql", "mysql.service"), ("postgres", "postgresql.service"),
    ("redis", "redis-server.service"), ("cron", "cron.service"), ("chrony", "chrony.service"),
    ("monit", "monit.service"), ("collectd", "collectd.service"), ("fail2ban", "fail2ban.service"),
    ("wsdd", "wsdd-server.service"), ("avahi", "avahi-daemon.service"), ("tailscale", "tailscaled.service"),
    ("smartd", "smartmontools.service"),
]


class Services(Worker):
    def __init__(self):
        Worker.__init__(self, "services")
        self.units = []
        self.next_list = 0

    def step(self):
        now = time.monotonic()
        if now >= self.next_list:
            out = run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager", "--plain"], 10) or ""
            files = {}
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 2:
                    files[p[0]] = p[1]
            units = []
            for label, pat in SERVICE_CANDIDATES:
                rx = re.compile("^" + re.escape(pat).replace(r"\*", ".*") + "$")
                for u in sorted(files):
                    if rx.match(u) and files[u] not in ("masked",):
                        units.append((label if "*" not in pat else u.replace(".service", ""), u, files[u]))
            self.units = units
            self.next_list = now + 600
        if not self.units:
            self.snap = []
            return 10
        out = run(["systemctl", "is-active"] + [u for _, u, _ in self.units], 10) or ""
        states = out.split()
        snap = []
        for (label, unit, enabled), st in zip(self.units, states + ["unknown"] * len(self.units)):
            if st in ("active", "activating", "failed", "reloading") or enabled == "enabled":
                snap.append({"name": label, "unit": unit, "state": st})
        if os.path.exists("/usr/bin/docker"):
            dk = run(["docker", "ps", "--format", "{{.Names}}"], 5) or ""
            for n in dk.split():
                if "marzban" in n.lower():
                    snap.append({"name": n[:14], "unit": "docker", "state": "active"})
        self.snap = snap
        return 10


WMO = {0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
       81: "Rain showers", 82: "Violent showers", 85: "Snow showers", 86: "Snow showers",
       95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail"}


class Weather(Worker):
    UA = {"User-Agent": "eris-console/%s" % E.VERSION}

    def __init__(self, conf):
        Worker.__init__(self, "weather")
        self.conf = conf
        self.loc = None
        self.loc_key = None

    def fetch(self, url):
        req = urllib.request.Request(url, headers=self.UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8"))

    def locate(self):
        city = self.conf.get("WEATHER_CITY", "").strip()
        key = city or "@ip"
        if self.loc and self.loc_key == key:
            return self.loc
        cache_path = os.path.join(E.STATE_DIR, "weather-location.json")
        try:
            cache = json.load(open(cache_path))
            if cache.get("key") == key and time.time() - cache.get("time", 0) < (30 * 86400 if city else 6 * 3600):
                self.loc, self.loc_key = cache["loc"], key
                return self.loc
        except (OSError, ValueError, KeyError):
            pass
        if city:
            j = self.fetch("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
                           + urllib.parse.quote(city))
            r = (j.get("results") or [None])[0]
            if not r:
                raise LookupError("city not found: %s" % city)
            loc = {"name": r.get("name", city), "country": r.get("country", ""), "lat": r["latitude"],
                   "lon": r["longitude"], "src": "city"}
        else:
            j = self.fetch("http://ip-api.com/json/?fields=status,city,country,lat,lon")
            if j.get("status") != "success":
                raise LookupError("IP location failed")
            loc = {"name": j.get("city", "?"), "country": j.get("country", ""), "lat": j["lat"], "lon": j["lon"], "src": "IP"}
        self.loc, self.loc_key = loc, key
        try:
            os.makedirs(E.STATE_DIR, exist_ok=True)
            json.dump({"key": key, "time": time.time(), "loc": loc}, open(cache_path, "w"))
        except OSError:
            pass
        return loc

    def step(self):
        if self.conf.get("WEATHER") != "on":
            self.snap = None
            return 30
        units = self.conf.get("WEATHER_UNITS", "C")
        try:
            loc = self.locate()
            q = {"latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 7,
                 "forecast_hours": 25,
                 "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                            "wind_speed_10m,wind_direction_10m,is_day",
                 "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
                 "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                 "temperature_unit": "fahrenheit" if units == "F" else "celsius",
                 "wind_speed_unit": "mph" if units == "F" else "kmh"}
            j = self.fetch("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))
            cur = j["current"]
            hj, dj = j.get("hourly") or {}, j.get("daily") or {}

            def col(src, key, i):
                v = src.get(key) or []
                return v[i] if i < len(v) else None
            hourly = [{"hour": ts[11:13], "temp": col(hj, "temperature_2m", i), "code": col(hj, "weather_code", i),
                       "pop": col(hj, "precipitation_probability", i), "is_day": col(hj, "is_day", i)}
                      for i, ts in enumerate(hj.get("time") or [])]
            daily = []
            for i, ds in enumerate(dj.get("time") or []):
                try:
                    day = time.strftime("%a", time.strptime(ds, "%Y-%m-%d"))
                except ValueError:
                    day = ds[5:]
                daily.append({"day": day, "code": col(dj, "weather_code", i), "tmin": col(dj, "temperature_2m_min", i),
                              "tmax": col(dj, "temperature_2m_max", i), "pop": col(dj, "precipitation_probability_max", i)})
            sr, ss = col(dj, "sunrise", 0), col(dj, "sunset", 0)
            self.snap = {"ok": True, "loc": loc, "units": units, "temp": cur.get("temperature_2m"),
                         "feels": cur.get("apparent_temperature"), "hum": cur.get("relative_humidity_2m"),
                         "code": cur.get("weather_code"), "wind": cur.get("wind_speed_10m"),
                         "wdir": cur.get("wind_direction_10m"), "is_day": cur.get("is_day", 1),
                         "tmax": daily[0]["tmax"] if daily else None, "tmin": daily[0]["tmin"] if daily else None,
                         "sunrise": sr[11:16] if sr else None, "sunset": ss[11:16] if ss else None,
                         "hourly": hourly, "daily": daily, "time": time.time(), "key": (self.loc_key, units)}
            return 900
        except Exception as e:  # network/API trouble only marks the panel unavailable
            old = self.snap if (self.snap and self.snap.get("ok") and time.time() - self.snap["time"] < 3 * 3600) else None
            self.snap = dict(old, stale=True) if old else {"ok": False, "error": str(e)[:60]}
            return 180


def iso_epoch(s):
    """'2026-11-01T15:40:00.123+00:00' / '...Z' -> epoch seconds (None if unparsable)."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1000.0 if s > 1e11 else 1.0)
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Usage(Worker):
    """Claude and Codex plan limits: 5-hour and weekly windows with their reset times.

    Claude: Claude Code's OAuth token (read-only, never refreshed here so Claude Code's own login is
    untouched) against the endpoint Claude Code's /usage screen uses. Codex: the newest rate_limits
    event Codex writes into its session logs. Both go through the AI proxy (/etc/eris/ai-proxy).
    """

    CLAUDE_CREDS = ("/root/.claude/.credentials.json",)
    CODEX_HOME = "/root/.codex"

    def __init__(self):
        Worker.__init__(self, "usage")
        self.snap = {"claude": {"state": "none", "hint": "checking..."}, "codex": {"state": "none", "hint": "checking..."}, "time": None}
        self.claude_last = None
        self.next_claude = 0
        self.backoff = 0
        self.creds_mtime = None

    @staticmethod
    def opener():
        url = ""
        try:
            for line in open("/etc/eris/ai-proxy"):
                line = line.strip()
                if line and not line.startswith("#"):
                    url = line
                    break
        except OSError:
            pass
        if url and url != "none":
            hp = url.split("://", 1)[-1].split("/", 1)[0]
            host, _, port = hp.rpartition(":")
            try:
                socket.create_connection((host or "127.0.0.1", int(port)), timeout=2).close()
                return urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
            except (OSError, ValueError):
                pass
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def claude(self):
        creds = None
        for p in self.CLAUDE_CREDS:
            try:
                creds = json.load(open(p)).get("claudeAiOauth")
                break
            except (OSError, ValueError, AttributeError):
                continue
        if not creds or not creds.get("accessToken"):
            return {"state": "login", "hint": "claude > /login"}
        plan = (creds.get("subscriptionType") or "").replace("_", " ").title() or None
        tier = creds.get("rateLimitTier") or ""
        m = re.search(r"max_(\d+)x", tier)
        if m and plan and "Max" in plan:
            plan = "Max %sx" % m.group(1)
        exp = iso_epoch(creds.get("expiresAt"))
        if exp and exp - 300 <= time.time():
            # Claude Code refreshes its own token the next time it runs; refreshing it here would rotate
            # the refresh token under Claude Code and log it out, so just wait and keep the last numbers.
            last = dict(self.claude_last or {}, plan=plan)
            last.update(state="ok" if last.get("windows") else "error",
                        note="token expired: runs again after the next claude use", error="token expired")
            return last
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + creds["accessToken"], "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json", "User-Agent": "claude-code/2.1.112"})
        try:
            with self.opener().open(req, timeout=15) as r:
                j = json.loads(r.read().decode("utf-8"))
            self.backoff = 0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.backoff = min(1800, max(600, self.backoff * 2))    # 10, 20, 30 min
            last = dict(self.claude_last or {})
            asof = (" (as of %s)" % time.strftime("%H:%M", time.localtime(last["time"]))) if last.get("time") else ""
            last.update(state="ok" if last.get("windows") else "error", plan=plan,
                        error="usage API: HTTP %d" % e.code, note="usage API: HTTP %d%s" % (e.code, asof))
            return last
        windows = []
        for key, label in (("five_hour", "5h"), ("seven_day", "7d"), ("seven_day_opus", "opus"), ("seven_day_sonnet", "son")):
            w = j.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                windows.append((label.ljust(5), {"pct": float(w["utilization"]), "reset": iso_epoch(w.get("resets_at"))}))
        out = {"state": "ok", "plan": plan, "windows": windows, "time": time.time()}
        self.claude_last = out
        return out

    def codex(self):
        base = os.path.join(self.CODEX_HOME, "sessions")
        if not os.path.isdir(base):
            ok = os.path.exists(os.path.join(self.CODEX_HOME, "auth.json"))
            return {"state": "login" if not ok else "none",
                    "hint": "codex login --device-auth" if not ok else "no Codex session yet"}
        newest = None
        for root, dirs, files in os.walk(base):
            for f in files:
                if f.startswith("rollout-") and f.endswith(".jsonl"):
                    p = os.path.join(root, f)
                    m = os.path.getmtime(p)
                    if not newest or m > newest[0]:
                        newest = (m, p)
        plan = None
        try:
            import base64
            tok = json.load(open(os.path.join(self.CODEX_HOME, "auth.json"))).get("tokens", {}).get("id_token", "")
            pay = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(pay + "=" * (-len(pay) % 4)))
            plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
            plan = plan.title() if plan else None
        except (OSError, ValueError, IndexError, AttributeError):
            pass
        if not newest:
            return {"state": "none", "plan": plan, "hint": "no Codex session yet"}
        with open(newest[1], "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400000))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") or ev
            rl = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
            if not isinstance(rl, dict):
                continue
            when = iso_epoch(ev.get("timestamp")) or newest[0]
            windows = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                mins = w.get("window_minutes") or (300 if key == "primary" else 10080)
                label = "5h" if mins <= 360 else "7d" if mins >= 9000 else "%dh" % (mins // 60)
                reset = w.get("resets_at")
                reset = iso_epoch(reset) if reset is not None else (when + w["resets_in_seconds"] if w.get("resets_in_seconds") is not None else None)
                windows.append((label.ljust(5), {"pct": float(w["used_percent"]), "reset": reset}))
            if not plan and rl.get("plan_type"):
                plan = str(rl["plan_type"]).title()
            age = time.time() - when
            # like Codex's own /status: a snapshot older than 15 minutes is stale
            note = ("as of %s ago" % until(time.time() + age).replace("now", "0m")) if age > 900 else None
            return {"state": "ok", "plan": plan, "windows": windows, "note": note}
        return {"state": "none", "plan": plan, "hint": "no rate-limit data in the Codex logs yet"}

    @staticmethod
    def busy():
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                cmd = open("/proc/%s/cmdline" % p, "rb").read(300)
            except OSError:
                continue
            if b"claude-code" in cmd or cmd.startswith(b"/opt/codex/") or b"/codex-x86_64" in cmd:
                return True
        return False

    def step(self):
        now = time.monotonic()
        snap = dict(self.snap)
        try:
            mt = os.path.getmtime(self.CLAUDE_CREDS[0])
        except OSError:
            mt = None
        if mt != self.creds_mtime:          # a new login or a token Claude Code just refreshed: ask now
            self.creds_mtime = mt
            self.next_claude = 0
        if now >= self.next_claude:
            try:
                snap["claude"] = self.claude()
            except Exception as e:  # network trouble: keep the last numbers, say so
                last = dict(self.claude_last or {"state": "error"})
                last["error"] = last["note"] = "usage: %s" % type(e).__name__
                snap["claude"] = last
            self.next_claude = now + max(300, self.backoff)   # the endpoint rate-limits frequent polling
        try:
            snap["codex"] = self.codex()
        except Exception as e:
            snap["codex"] = {"state": "error", "error": "codex: %s" % type(e).__name__}
        snap["busy"] = self.busy()
        snap["time"] = time.time()
        self.snap = snap
        return 30


# ================================================================ rendering

R = "\x1b[0m"
STY = {
    "pink": "\x1b[0;1;35m", "purple": "\x1b[0;35m", "red": "\x1b[0;1;31m", "dred": "\x1b[0;31m",
    "blue": "\x1b[0;1;34m", "dblue": "\x1b[0;34m", "green": "\x1b[0;1;32m", "dgreen": "\x1b[0;32m",
    "cyan": "\x1b[0;1;36m", "dcyan": "\x1b[0;36m", "amber": "\x1b[0;33m", "yellow": "\x1b[0;1;33m",
    "white": "\x1b[0;1;37m", "text": "\x1b[0;37m", "grey": "\x1b[0;1;30m",
    "clawd": "\x1b[0;33m",          # palette slot 3 = Claude orange #d97757 in the ERIS palette
    "gap": "",
}
GLYPHS = {
    "utf8": dict(tl="┌", tr="┐", bl="└", br="┘", h="─", v="│", full="█", empty="░", up="↑", down="↓",
                 deg="°", dot="●", ell="…", sep="·", tee="├", end="└", x="✘", rst="↻", track="─", rng="█"),
    "safe": dict(tl="┌", tr="┐", bl="└", br="┘", h="─", v="│", full="█", empty="░", up="↑", down="↓",
                 deg="°", dot="■", ell="~", sep="·", tee="├", end="└", x="x", rst="@", track="─", rng="█"),
    "ascii": dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", full="#", empty=".", up="^", down="v",
                  deg="", dot="*", ell="~", sep="|", tee="|", end="`", x="x", rst="@", track="-", rng="="),
}
LOGO_BOX = ["┌─┐┬─┐┬┌─┐", "├┤ ├┬┘│└─┐", "└─┘┴└─┴└─┘"]       # E R = cols 0-5, I S = cols 6-9
LOGO_ASCII = [" __  __     __", "|_  |__) | (_ ", "|__ |  \\ | __)"]  # E R = cols 0-8, I S = cols 9-13
QUAD = {(0, 0, 0, 0): " ", (1, 0, 0, 0): "▘", (0, 1, 0, 0): "▝", (1, 1, 0, 0): "▀", (0, 0, 1, 0): "▖",
        (1, 0, 1, 0): "▌", (0, 1, 1, 0): "▞", (1, 1, 1, 0): "▛", (0, 0, 0, 1): "▗", (1, 0, 0, 1): "▚",
        (0, 1, 0, 1): "▐", (1, 1, 0, 1): "▜", (0, 0, 1, 1): "▄", (1, 0, 1, 1): "▙", (0, 1, 1, 1): "▟",
        (1, 1, 1, 1): "█"}
SPARK = "▁▂▃▄▅▆▇█"
ICON_CP = {"sun": 0xE000, "moon": 0xE002, "partly": 0xE004, "cloud": 0xE006, "fog": 0xE008,
           "drizzle": 0xE00A, "rain": 0xE00C, "snow": 0xE00E, "storm": 0xE010}
ICON_STY = {"sun": "yellow", "moon": "yellow", "partly": "white", "cloud": "text", "fog": "grey",
            "drizzle": "cyan", "rain": "blue", "snow": "white", "storm": "amber"}
ICON_TXT = {"sun": "Sun", "moon": "Clr", "partly": "Pcl", "cloud": "Cld", "fog": "Fog",
            "drizzle": "Drz", "rain": "Rai", "snow": "Snw", "storm": "Thu"}


class Theme:
    def __init__(self, charset, cps=None, extras=None):
        self.cs = charset
        self.g = GLYPHS[charset]
        self.cps = cps  # code points the console font can show (None = trust UTF-8)
        ok = charset != "ascii"

        def has(chars):
            return ok and (extras if extras is not None else cps is not None and all(ord(c) in cps for c in chars))
        self.quad = has("".join(QUAD.values()))                       # the Claude mascot
        self.spark = has(SPARK)                                       # sparklines
        self.icons = has("".join(chr(c) + chr(c + 1) for c in ICON_CP.values()))
        self.bar = "━" if has("━") else self.g["rng"]

    def clean(self, s):
        """Make external text safe for the console: one column per character, glyph in the font."""
        out = []
        for ch in str(s):
            o = ord(ch)
            if o < 32 or o == 127 or 0x80 <= o < 0xA0 or unicodedata.combining(ch):
                continue
            wide = unicodedata.east_asian_width(ch) in ("W", "F")
            shown = o < 127 or (self.cs != "ascii" and (self.cps is None or o in self.cps))
            if wide or not shown:
                base = unicodedata.normalize("NFKD", ch)[:1]
                ch = base if base and ord(base) < 127 else "?"
            out.append(ch)
        return "".join(out)


class Ln:
    """A styled line. gap() marks spaces that grow when the line is wider than its text, so
    larger consoles spread the text out instead of leaving one empty block on the right.
    brk marks the line as a section start: spare panel rows become blank lines before it."""
    __slots__ = ("segs", "n", "gaps", "brk")

    def __init__(self, text="", style="text"):
        self.segs = []
        self.n = 0
        self.gaps = 0
        self.brk = False
        if text:
            self.add(text, style)

    def add(self, text, style="text"):
        if text:
            if self.segs and self.segs[-1][0] == style and style != "gap":
                self.segs[-1] = (style, self.segs[-1][1] + text)      # fewer colour codes on the wire
            else:
                self.segs.append((style, text))
            self.n += len(text)
        return self

    def gap(self, n=2):
        self.segs.append(("gap", " " * n))
        self.n += n
        self.gaps += 1
        return self

    def section(self):
        self.brk = True
        return self

    def cat(self, other):
        for st, tx in other.segs:
            if st == "gap":
                self.gap(len(tx))
            else:
                self.add(tx, st)
        return self

    def fit(self, w, ell=None, spread=True):
        if self.n > w:
            out = Ln()
            out.brk = self.brk
            room = w
            for st, tx in self.segs:
                if room <= 0:
                    break
                out.add(tx[:room], st if st != "gap" else "text")
                room -= len(tx[:room])
            if ell and w > 0 and out.segs:
                st, tx = out.segs[-1]
                out.segs[-1] = (st, tx[:-1] + ell)
            return out
        if self.n < w:
            extra = w - self.n
            if spread and self.gaps:
                per, rem = divmod(extra, self.gaps)
                segs, k = [], 0
                for st, tx in self.segs:
                    if st == "gap":
                        tx += " " * (per + (1 if k < rem else 0))
                        k += 1
                    segs.append((st, tx))
                self.segs = segs
                self.n = w
            else:
                self.add(" " * extra)
        return self

    def render(self):
        return "".join(STY.get(st, st) + tx for st, tx in self.segs) + R


def lr(left, right, w, ell="…"):
    """left-aligned + right-aligned parts in exactly w columns (right part wins)."""
    if right.n >= w:
        return right.fit(w)
    room = w - right.n - (1 if right.n else 0)
    out = Ln().cat(left.fit(room, ell, spread=False) if left.n > room else left)
    out.add(" " * (w - out.n - right.n))
    return out.cat(right)


def bar(pct, width, th, thresholds=(60, 85)):
    width = max(1, width)
    if pct is None:
        return Ln(th.g["empty"] * width, "grey")
    filled = int(round(max(0.0, min(100.0, pct)) * width / 100.0))
    st = "green" if pct < thresholds[0] else "amber" if pct < thresholds[1] else "red"
    return Ln(th.g["full"] * filled, st).add(th.g["empty"] * (width - filled), "grey")


def tcol(t, warn, crit):
    if t is None:
        return "grey"
    return "green" if t < warn else "amber" if t < crit else "red"


def ftemp(t, th, unit="C"):
    if t is None:
        return "N/A"
    return "%.0f%s%s" % (t, th.g["deg"], unit)


def pct_s(p):
    return "N/A" if p is None else "%3.0f%%" % p


def until(ts):
    """'1h12m' until epoch ts (or 'now')."""
    if ts is None:
        return ""
    s = int(ts - time.time())
    if s <= 0:
        return "now"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return "%dd%02dh" % (d, h) if d else "%dh%02dm" % (h, m) if h else "%dm" % m


def at(ts):
    """Local reset time: '15:40' today, else 'Thu 09:00'."""
    if ts is None:
        return ""
    lt = time.localtime(ts)
    return time.strftime("%H:%M", lt) if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d") else time.strftime("%a %H:%M", lt)


# ---------------------------------------------------------------- panels (content only)

def p_cpu(d, wi, th, hi=None):
    c = d["cpu"]
    L = [Ln(th.clean(c["model"]), "white")]
    mhz = [p["mhz"] for p in c["per"] if p["mhz"]]
    freq = ("avg %.0f MHz" % (sum(mhz) / len(mhz))) if mhz else "freq N/A"
    L.append(Ln("%d cores / %d threads" % (c["cores"], c["threads"]), "text").gap().add(freq, "text"))
    bw = max(4, wi - 12)
    L.append(Ln("Usage ", "grey").cat(bar(c["usage"], bw, th)).add(" " + pct_s(c["usage"]), "white").section())
    L.append(Ln("Load", "grey").gap().add("%.2f" % c["load"][0], "white").gap().add("%.2f" % c["load"][1], "white")
             .gap().add("%.2f" % c["load"][2], "white").gap().add("1/5/15m", "grey"))
    for i, p in enumerate(c["per"]):
        lab = "C%d " % p["cpu"]
        tail = Ln(" " + pct_s(p["usage"]), "white")
        if wi >= 30 and p["mhz"]:
            tail.add(" %4.0fM" % p["mhz"], "text")
        tail.add(" %5s" % ftemp(p["temp"], th), tcol(p["temp"], 60, 80))
        ln = Ln(lab, "cyan").cat(bar(p["usage"], max(3, wi - len(lab) - tail.n), th)).cat(tail)
        L.append(ln.section() if i == 0 else ln)
    L.append(Ln("Pkg ", "grey").add(ftemp(c["pkg"], th), tcol(c["pkg"], 60, 80)).gap()
             .add("max ", "grey").add(ftemp(c["max_seen"], th), tcol(c["max_seen"], 60, 80)).gap()
             .add("crit ", "grey").add(ftemp(c["crit"], th), "text").section())
    st = c["throttle"]
    L.append(Ln("Throttle", "grey").gap().add(st, "red" if st == "THROTTLING" else "green" if st == "OK" else "amber")
             .add(" (%s events)" % ("N/A" if c["throttle_count"] is None else c["throttle_count"]), "grey"))
    return with_chart(L, d, "cpu", wi, hi, th, "CPU usage", 100.0, lambda v: "%.0f%%" % v)


def p_mem(d, wi, th, hi=None):
    m = d["mem"]
    bw = max(4, wi - 10)
    L = [
        Ln("RAM  ", "grey").cat(bar(m["pct"], bw, th)).add(" " + pct_s(m["pct"]), "white"),
        Ln("used ", "grey").add(E.human(m["used"]), "white").add(" / " + E.human(m["total"]), "text").gap()
        .add("avail ", "grey").add(E.human(m["avail"]), "green"),
        Ln("free ", "grey").add(E.human(m["free"]), "text").gap().add("cache ", "grey").add(E.human(m["cache"]), "text"),
        Ln("Swap ", "grey").cat(bar(m["swap_pct"], bw, th)).add(" " + pct_s(m["swap_pct"]), "white").section(),
        Ln("used ", "grey").add(E.human(m["swap_used"]), "white").gap().add("of " + E.human(m["swap_total"]), "text"),
    ]
    return with_chart(L, d, "mem", wi, hi, th, "RAM used", 100.0, lambda v: "%.0f%%" % v)


def chart(vals, w, h, th, vmax=None, style=None):
    """h rows x w columns area chart of the newest w values (eighth blocks when the font has them)."""
    vals = [v for v in vals if v is not None][-w:]
    top = vmax if vmax else max(vals + [1e-9])
    levels = [int(round(max(0.0, min(1.0, v / top)) * h * 8)) for v in vals]
    pad = w - len(levels)
    rows = []
    for r in range(h):
        base = (h - 1 - r) * 8
        st = style or ("red" if r < h / 3.0 else "amber" if r < 2 * h / 3.0 else "green")
        s = []
        for lv in levels:
            f = max(0, min(8, lv - base))
            if f == 8:
                s.append(th.g["full"])
            elif f == 0:
                s.append(" ")
            else:
                s.append(SPARK[f - 1] if th.spark else (th.g["full"] if f >= 4 else " "))
        rows.append(Ln(" " * pad).add("".join(s), st))
    return rows


def with_chart(L, d, key, wi, hi, th, title, vmax, fmt):
    """Spend the panel's spare rows (tall consoles) on a history chart of `key`."""
    hist = (d.get("hist") or {}).get(key) or []
    spare = (hi or 0) - len(L) - 2
    if spare < 3 or len(hist) < 2:
        return L
    every = (d.get("sys") or {}).get("every", "2")
    peak = max(v for v in hist if v is not None) if any(v is not None for v in hist) else 0
    L.append(Ln(title, "grey").gap().add("peak " + fmt(peak), "text").gap().add("%ss/col" % every, "grey").section())
    return L + chart(hist, wi, spare, th, vmax)


def usage_line(label, w, wi, th):
    """'5h  ███░░░  62%  ↻ 1h12m 15:40' for one rate-limit window."""
    pct = w.get("pct") if w else None
    tail = Ln(" " + pct_s(pct), "white")
    when = ""
    if w and w.get("reset"):
        when = "%s %s" % (until(w["reset"]), at(w["reset"]))
    right = Ln()
    if when:
        right.add(th.g["rst"] + " ", "grey").add(when, "text")
    bw = max(4, min(30, wi - len(label) - tail.n - (right.n + 2 if right.n else 0)))
    ln = Ln(label, "grey").cat(bar(pct, bw, th, (70, 90))).cat(tail)
    if right.n:
        ln.gap().cat(right)
    return ln


def p_usage(d, wi, th):
    u = d.get("usage")
    if not u:
        return [Ln("reading usage...", "grey")]
    L = []
    for key, name, style in (("claude", "Claude", "clawd"), ("codex", "Codex", "green")):
        s = u.get(key) or {}
        head = Ln(name, style)
        if s.get("plan"):
            head.gap().add(s["plan"], "grey")
        if L:
            head.section()
        L.append(head)
        state = s.get("state")
        if state == "login":
            L.append(Ln("not logged in", "amber").gap().add(s.get("hint", ""), "grey"))
            continue
        if state == "none":
            L.append(Ln(s.get("hint", "no data yet"), "grey"))
            continue
        if state == "error" and not s.get("windows"):
            L.append(Ln(th.clean(s.get("error", "unavailable"))[:wi], "amber"))
            continue
        for label, w in s.get("windows", []):
            L.append(usage_line(label, w, wi, th))
        if s.get("note"):
            L.append(Ln(th.clean(s["note"]), "amber"))
    t = u.get("time")
    L.append(Ln("updated", "grey").gap().add(time.strftime("%H:%M:%S", time.localtime(t)) if t else "-", "text").section())
    return L


def p_gpu(d, wi, th):
    L = []
    for g in d["gpu"]:
        L.append(Ln(th.clean(g["name"]), "white"))
        L.append(Ln(g["driver"], "green").gap().add("VRAM ", "grey")
                 .add(E.human(g["vram"]) if g["vram"] else "N/A", "text")
                 .add(" used ", "grey").add(E.human(g["vram_used"]) if g["vram_used"] is not None else "N/A", "text"))
        fan = "N/A" if g["fan"] is None else "%d rpm" % g["fan"]
        if g["fan_pct"] is not None:
            fan += " %.0f%%" % g["fan_pct"]
        L.append(Ln("Temp ", "grey").add(ftemp(g["temp"], th), tcol(g["temp"], 65, 80)).gap().add("Fan ", "grey").add(fan, "text"))
        L.append(Ln("Load ", "grey").add("N/A" if g["util"] is None else "%d%%" % g["util"], "text")
                 .gap().add("Core ", "grey").add("N/A" if g["mv"] is None else "%d mV" % g["mv"], "text"))
        L.append(Ln("Out ", "grey").add(g["dpms"] or "N/A", "text").gap().add("PM ", "grey").add(g["pm"] or "N/A", "text"))
    return L or [Ln("no GPU found", "grey")]


def p_net(d, wi, th, hi=None):
    n = d["net"]
    if not n:
        return [Ln("no active interface", "grey")]
    up = n["state"] == "up" and n["carrier"] == 1
    sp = "%dMb/s %s" % (n["speed"], n["duplex"] or "") if n["speed"] else "speed N/A"
    L = [Ln(n["if"], "white").add(" %s " % th.g["dot"], "green" if up else "red")
         .add(("UP" if up else n["state"].upper()), "green" if up else "red").gap().add(sp, "text")]
    L.append(Ln("IP", "grey").gap().add(n["ip"] or "N/A", "white"))
    L.append(Ln("GW", "grey").gap().add(n["gw"] or "N/A", "text"))
    L.append(Ln(th.g["down"] + " ", "green").add(rate(n["rx_rate"]), "white").gap()
             .add(th.g["up"] + " ", "pink").add(rate(n["tx_rate"]), "white").section())
    L.append(Ln("RX ", "grey").add(E.human(n["rx_bytes"]), "text").gap().add("%s pkts" % n["rx_packets"], "grey"))
    L.append(Ln("TX ", "grey").add(E.human(n["tx_bytes"]), "text").gap().add("%s pkts" % n["tx_packets"], "grey"))
    if n["rx_errors"] or n["tx_errors"]:
        L.append(Ln("errors", "grey").gap().add("rx %d tx %d" % (n["rx_errors"], n["tx_errors"]), "red"))
    if n["ip6"]:
        L.append(Ln("IPv6", "grey").gap().add(n["ip6"], "text"))
    if n["others"]:
        L.append(Ln("also up", "grey").gap().add(" ".join(n["others"]), "text"))
    if n["driver"]:
        L.append(Ln("driver", "grey").gap().add(n["driver"], "text"))
    hist = d.get("hist") or {}
    spare = (hi or 0) - len(L) - 4
    if spare >= 4 and len(hist.get("rx") or []) >= 2:
        h1 = spare // 2
        for key, arrow, st, h in (("rx", th.g["down"], "green", h1), ("tx", th.g["up"], "pink", spare - h1)):
            vals = [v for v in hist[key] if v is not None]
            peak = max(vals + [0])
            L.append(Ln(arrow + " history", st).gap().add("peak " + rate(peak), "text").section())
            L += chart(vals, wi, h, th, None, st)
    return L


def compass(deg):
    if deg is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


def wx_kind(code, day=True):
    if code is None:
        return "cloud"
    if code == 0:
        return "sun" if day else "moon"
    if code in (1, 2):
        return "partly" if day else "moon"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloud"


def icon(kind, th):
    """2 columns: the drawn icon when the ERIS font is loaded, else a 2-letter code."""
    if th.icons:
        cp = ICON_CP[kind]
        return Ln(chr(cp) + chr(cp + 1), ICON_STY[kind])
    return Ln(ICON_TXT[kind][:2], ICON_STY[kind])


def p_weather(d, wi, th):
    w = d["weather"]
    if not w:
        return [Ln("loading...", "grey")]
    if not w.get("ok"):
        return [Ln("weather unavailable", "amber"), Ln(th.clean(w.get("error", "")), "grey")]
    u = w["units"]
    loc = w["loc"]
    deg = th.g["deg"]

    def t(v, full=True):
        return "N/A" if v is None else ("%.0f%s%s" % (v, deg, u) if full else "%.0f%s" % (v, deg or "'"))

    def tc(v):
        if v is None:
            return "grey"
        c = v if u == "C" else (v - 32) / 1.8
        return "cyan" if c < 8 else "green" if c < 20 else "yellow" if c < 30 else "red"

    kind = wx_kind(w["code"], w.get("is_day", 1))
    L = [Ln().cat(icon(kind, th)).add(" ").add(t(w["temp"]), "yellow").add("  " + WMO.get(w["code"], "code %s" % w["code"]), "text")
         .gap().add(th.clean("%s, %s" % (loc["name"], loc["country"])), "white").add(" (%s)" % loc["src"], "grey")]
    L.append(Ln("feels ", "grey").add(t(w["feels"]), "text").gap().add("hum ", "grey")
             .add("N/A" if w["hum"] is None else "%d%%" % w["hum"], "text").gap().add("wind ", "grey")
             .add("N/A" if w["wind"] is None else "%.0f %s %s" % (w["wind"], "mph" if u == "F" else "km/h", compass(w["wdir"])), "text"))
    L.append(Ln(th.g["up"] + " ", "yellow").add(w.get("sunrise") or "--:--", "text").gap()
             .add(th.g["down"] + " ", "amber").add(w.get("sunset") or "--:--", "text").gap()
             .add("updated ", "grey").add(time.strftime("%H:%M", time.localtime(w["time"])), "text")
             .add(" (stale)" if w.get("stale") else "", "amber"))
    # next hours: one column per hour, as many as fit (4..24)
    hours = w.get("hourly") or []
    if hours and wi >= 16:
        n = max(4, min(len(hours), wi // 5))
        cw = wi // n
        hs = hours[:n]
        row_h, row_i, row_t, row_s, row_p = Ln().section(), Ln(), Ln(), Ln(), Ln()
        temps = [h["temp"] for h in hs if h["temp"] is not None]
        lo, hi = (min(temps), max(temps)) if temps else (0, 1)
        for k, h in enumerate(hs):
            lab = "now" if k == 0 else h["hour"]
            row_h.add(lab.center(cw), "grey" if k else "white")
            ic = icon(wx_kind(h["code"], h.get("is_day", 1)), th)
            pad = cw - ic.n
            row_i.add(" " * (pad // 2)).cat(ic).add(" " * (pad - pad // 2))
            row_t.add(t(h["temp"], False).center(cw), tc(h["temp"]))
            if th.spark and h["temp"] is not None:
                lvl = SPARK[int(round((h["temp"] - lo) / ((hi - lo) or 1) * 7))]
                row_s.add((lvl * (cw - 1)).center(cw), tc(h["temp"]))
            pp = h.get("pop")
            row_p.add(("%d%%" % pp if pp else "").center(cw), "blue")
        L += [row_h, row_i, row_t]
        if th.spark:
            L.append(row_s)
        if any(h.get("pop") for h in hs):
            L.append(row_p)
    # next days: name, icon, min, range bar over the week, max, rain chance
    days = w.get("daily") or []
    if days:
        lows = [x["tmin"] for x in days if x["tmin"] is not None]
        highs = [x["tmax"] for x in days if x["tmax"] is not None]
        wlo, whi = (min(lows), max(highs)) if lows and highs else (0, 1)
        for k, x in enumerate(days):
            name = "Today" if k == 0 else x["day"]
            left = Ln(name.ljust(6), "white" if k == 0 else "text").cat(icon(wx_kind(x["code"]), th)).add(" ")
            left.add(t(x["tmin"], False).rjust(4), tc(x["tmin"])).add(" ")
            right = Ln(" ").add(t(x["tmax"], False).ljust(4), tc(x["tmax"]))
            pop = x.get("pop")
            right.add((" %3d%%" % pop) if pop is not None else "", "blue" if pop and pop >= 30 else "grey")
            bw = max(4, wi - left.n - right.n)
            a = int(round(((x["tmin"] if x["tmin"] is not None else wlo) - wlo) / ((whi - wlo) or 1) * (bw - 1)))
            b = int(round(((x["tmax"] if x["tmax"] is not None else whi) - wlo) / ((whi - wlo) or 1) * (bw - 1)))
            track = Ln(th.g["track"] * a, "grey").add(th.bar * (b - a + 1), tc(x["tmax"])).add(th.g["track"] * (bw - b - 1), "grey")
            ln = Ln().cat(left).cat(track).cat(right)
            L.append(ln.section() if k == 0 else ln)
    return L


def p_sys(d, wi, th):
    s = d["sys"]
    return [Ln("Host", "grey").gap().add(s["host"], "blue"),
            Ln("OS", "grey").gap().add(th.clean(s["os"]), "white"),
            Ln("Kernel", "grey").gap().add(s["kernel"], "text"),
            Ln("Uptime", "grey").gap().add(s["uptime"], "green").section(),
            Ln("Time", "grey").gap().add(s["now"], "white"),
            Ln("Procs", "grey").gap().add(str(s["procs"]), "white"),
            Ln("Disk I/O", "grey").gap().add("R " + rate(s["io"][0]), "text").gap().add("W " + rate(s["io"][1]), "text").section(),
            Ln("Refresh", "grey").gap().add(s["refreshed"], "text").add(" every %ss" % s["every"], "grey")]


def p_svc(d, wi, th):
    sv = d["svc"]
    if sv is None:
        return [Ln("checking...", "grey")]
    if not sv:
        return [Ln("no known services", "grey")]
    need = max(len(x["name"]) for x in sv) + 2
    per = max(1, (wi + 1) // (need + 1))
    cw = (wi - (per - 1)) // per          # columns share the whole width
    L = []
    for i in range(0, len(sv), per):
        ln = Ln()
        for j, x in enumerate(sv[i:i + per]):
            st = x["state"]
            col = "green" if st == "active" else "red" if st == "failed" else "amber" if st in ("activating", "reloading") else "grey"
            ln.add(th.g["dot"] + " ", col).add(x["name"][:cw - 2].ljust(cw - 2), "text" if st == "active" else col)
            if j < per - 1:
                ln.add(" ")
        L.append(ln)
    return L


def storage_lines(d, wi, th):
    st = d["storage"]
    if not st:
        return [Ln("scanning disks...", "grey")]
    g = th.g
    names = [p["name"] for dk in st["phys"] + st["virt"] for p in dk["parts"]] + [dk["name"] for dk in st["phys"] + st["virt"]]
    nw = min(10, max([len(n) for n in names] + [4]))
    show_fs = wi >= 58
    show_free = wi >= 50
    show_bar = wi >= 44
    # columns: tree+name+sp | fs(6) | size(7) | bar(bw+1) | use%(5) | used(7) | free(7) | sp mount
    fixed = nw + 3 + (6 if show_fs else 0) + 7 + 5 + 7 + (7 if show_free else 0) + 1
    bw = max(4, min(24, wi - fixed - 1 - 16)) if show_bar else 0
    hdr = Ln(" " * (nw + 3), "grey")
    if show_fs:
        hdr.add("FS    ", "grey")
    hdr.add("   SIZE", "grey").add(" ")
    if bw:
        hdr.add("USE".ljust(bw + 1), "grey")
    hdr.add(" USE%   USED", "grey")
    if show_free:
        hdr.add("   FREE", "grey")
    hdr.add(" MOUNT", "grey")
    L = [hdr]

    def disk_line(dk):
        left = Ln(dk["name"].ljust(nw), "cyan").add(" ")
        kc = {"HDD": "amber", "SSD": "green", "NVMe": "purple", "USB": "blue", "MMC": "dcyan"}.get(dk["kind"], "text")
        left.add(dk["kind"].ljust(4), kc).add(" %6s " % E.human(dk["size"]), "white").add(th.clean(dk["model"]), "text")
        right = Ln()
        if dk["link"]:
            right.add(dk["link"] + "  ", "grey")
        right.add(ftemp(dk["temp"], th), tcol(dk["temp"], 45, 55)).add("  ")
        if dk["health"]:
            right.add(dk["health"], "green" if dk["health"] == "PASSED" else "red")
        else:
            right.add(dk["note"] or "N/A", "amber" if dk["note"] == "SMART err" else "grey")
        return lr(left, right, wi)

    def part_line(p, last):
        ind = " " * max(0, (p["depth"] - 1))
        ln = Ln(ind + (g["end"] if last else g["tee"]) + " ", "grey").add(p["name"][:nw].ljust(nw - len(ind)), "text").add(" ")
        if show_fs:
            ln.add((p["fs"] or "-")[:5].ljust(6), "dcyan")
        ln.add("%6s " % E.human(p["size"]), "white")
        if bw:
            ln.cat(bar(p["pct"], bw, th, (75, 90)) if p["pct"] is not None else Ln(" " * bw)).add(" ")
        pc = p["pct"]
        ln.add(" N/A " if pc is None else "%4d%%" % pc, "grey" if pc is None else ("green" if pc < 75 else "amber" if pc < 90 else "red"))
        ln.add(" %6s" % E.human(p["used"]), "text" if p["used"] is not None else "grey")
        if show_free:
            ln.add(" %6s" % E.human(p["free"]), "text" if p["free"] is not None else "grey")
        mnt = " ".join(p["mounts"]) if p["mounts"] else "(extended)" if p["fs"] == "ext." else "(not mounted)"
        ln.add(" ")
        room = wi - ln.n
        if room > 0:
            ln.add(mnt if len(mnt) <= room else mnt[:max(1, room - 1)] + g["ell"], "white" if p["mounts"] else "grey")
        return ln

    for i, dk in enumerate(st["phys"]):
        dl = disk_line(dk)
        L.append(dl.section() if i else dl)
        for j, p in enumerate(dk["parts"]):
            L.append(part_line(p, j == len(dk["parts"]) - 1))
    if st["virt"]:
        L.append(Ln(g["h"] * 2 + " virtual ", "grey").add(g["h"] * max(0, wi - 12), "grey").section())
        for dk in st["virt"]:
            p = dk["parts"][0] if dk["parts"] else None
            ln = Ln(dk["name"].ljust(nw), "dcyan").add(" %6s " % E.human(dk["size"]), "text")
            if p:
                ln.add((p["fs"] or "")[:6] + " ", "grey").add(" ".join(p["mounts"]) or "(not mounted)", "text")
            L.append(ln)
    if not st["phys"]:
        L.append(Ln("no physical disks found", "grey"))
    else:
        fs = [p for dk in st["phys"] for p in dk["parts"] if p["total"] and "[SWAP]" not in p["mounts"]]
        used = sum(p["used"] for p in fs)
        free = sum(p["free"] for p in fs)
        L.append(Ln(g["h"] * wi, "grey").section())
        L.append(Ln("%d disks " % len(st["phys"]), "white").add(E.human(sum(dk["size"] or 0 for dk in st["phys"])), "white")
                 .gap().add("%d filesystems: " % len(fs), "grey").add(E.human(used), "white")
                 .add(" used, ", "grey").add(E.human(free), "green").add(" free", "grey"))
        io = d.get("disk_io") or {}
        if io:
            ln = Ln("I/O", "grey")
            for name in sorted(io):
                r, w = io[name]
                ln.gap().add(name + " ", "cyan").add("R %s W %s" % (rate(r), rate(w)), "text")
            L.append(ln)
    return L


PANELS = {
    # key: (title, colour, min width, weight, builder)
    "cpu": ("CPU", "pink", 30, 3.0, p_cpu),
    "usage": ("AI USAGE", "clawd", 30, 2.6, p_usage),
    "mem": ("MEMORY", "purple", 28, 2.3, p_mem),
    "storage": ("STORAGE", "cyan", 46, 5.0, storage_lines),
    "net": ("NETWORK", "blue", 28, 2.4, p_net),
    "gpu": ("GPU", "green", 28, 2.2, p_gpu),
    "weather": ("WEATHER", "amber", 36, 3.4, p_weather),
    "sys": ("SYSTEM", "red", 28, 2.4, p_sys),
    "svc": ("SERVICES", "yellow", 24, 2.6, p_svc),
}
PRIO = ["cpu", "usage", "mem", "storage", "net", "gpu", "weather", "sys", "svc"]
CHARTED = ("cpu", "mem", "net")
GROUPS = [["cpu", "svc"], ["usage", "mem"], ["storage"], ["net", "gpu"], ["weather"], ["sys"]]
FLEX = ("gpu", "weather", "sys", "svc", "net", "mem")


def spread_rows(lines, hi):
    """Spare rows of a panel become blank lines before section starts (at most 2 each)."""
    spare = hi - len(lines)
    marks = [i for i, ln in enumerate(lines) if i and ln.brk]
    if spare <= 0 or not marks:
        return lines
    add = {i: 0 for i in marks}
    for _ in range(3):
        for i in marks:
            if spare <= 0:
                break
            add[i] += 1
            spare -= 1
    out = []
    for i, ln in enumerate(lines):
        out += [Ln()] * add.get(i, 0)
        out.append(ln)
    return out


def boxed(key, w, h, lines, th, tag=None):
    title, col, _, _, _ = PANELS[key]
    g = th.g
    wi = w - 2
    top = Ln(g["tl"] + g["h"], col).add(" %s " % title, "white")
    if tag:
        tail = Ln(" %s " % tag, "grey").add(g["h"] + g["tr"], col)
    else:
        tail = Ln(g["tr"], col)
    top.add(g["h"] * max(0, w - top.n - tail.n), col).cat(tail)
    out = [top.fit(w)]
    lines = spread_rows(lines, h - 2)
    for i in range(h - 2):
        body = lines[i].fit(wi, g["ell"]) if i < len(lines) else Ln(" " * wi)
        out.append(Ln(g["v"], col).cat(body).add(g["v"], col))
    out.append(Ln(g["bl"] + g["h"] * (w - 2) + g["br"], col))
    return out[:h]


def layout(W, Hp, enabled, nat_h):
    gap = 1
    groups = [[p for p in grp if p in enabled] for grp in GROUPS]
    groups = [grp for grp in groups if grp]

    def gmin(grp):
        return max(PANELS[p][2] for p in grp)

    vis = list(groups)
    while len(vis) > 1 and sum(gmin(x) for x in vis) + gap * (len(vis) - 1) > W:
        vis.remove(max(vis, key=lambda x: min(PRIO.index(p) for p in x)))
    if not vis:
        return []
    widths = [gmin(x) for x in vis]
    if sum(widths) + gap * (len(vis) - 1) > W:          # one group, still too wide: squeeze it
        widths = [W]
    extra = W - sum(widths) - gap * (len(vis) - 1)
    weights = [max(PANELS[p][3] for p in x) for x in vis]
    tw = sum(weights)
    adds = [int(extra * wt / tw) for wt in weights]
    adds[weights.index(max(weights))] += extra - sum(adds)
    widths = [a + b for a, b in zip(widths, adds)]
    dropped = [p for grp in groups if grp not in vis for p in grp]
    cols = []
    for grp, w in zip(vis, widths):
        col, used = [], 0
        for p in grp:
            nh = nat_h(p, w)
            if not col:
                h = min(nh, Hp)
                col.append([p, h])
                used += h
            elif used + nh <= Hp:
                col.append([p, nh])
                used += nh
            else:
                dropped.append(p)
        cols.append({"w": w, "panels": col, "used": used})
    for p in sorted(dropped, key=PRIO.index):
        for c in sorted(cols, key=lambda c: c["used"]):          # the emptiest column first
            if c["w"] < PANELS[p][2]:
                continue
            free = Hp - c["used"]
            nh = nat_h(p, c["w"])
            if free >= nh or (free >= 4 and p in FLEX):
                c["panels"].append([p, min(nh, free)])
                c["used"] += min(nh, free)
                break
    for c in cols:
        if c["panels"]:
            # spare rows go to a panel that can draw a history chart, else to the last one
            target = next((pp for pp in c["panels"] if pp[0] in CHARTED), c["panels"][-1])
            target[1] += Hp - c["used"]
            c["used"] = Hp
    return cols


def header_lines(d, W, th, logo):
    """Header rows; with the logo, also the free area to the right where the mascot walks.

    Returns (rows, stage): rows are Ln objects as wide as the left part, stage = (x, width) or None.
    """
    s, c, m, n = d["sys"], d["cpu"], d["mem"], d["net"]
    g = th.g
    root = next((p for dk in (d["storage"] or {}).get("phys", []) for p in dk["parts"] if "/" in p["mounts"]), None)
    gpu_t = next((x["temp"] for x in d["gpu"] if x["temp"] is not None), None)
    chips = [Ln("CPU ", "grey").add(pct_s(c["usage"]).strip(), "white").add(" " + ftemp(c["hottest"], th), tcol(c["hottest"], 60, 80)),
             Ln("RAM ", "grey").add(pct_s(m["pct"]).strip(), "white"),
             Ln("SWAP ", "grey").add(pct_s(m["swap_pct"]).strip(), "white"),
             Ln("/ ", "grey").add("N/A" if not root or root["pct"] is None else "%d%%" % root["pct"], "white")]
    if n:
        chips.append(Ln(g["down"], "green").add(rate(n["rx_rate"]) + " ", "white").add(g["up"], "pink").add(rate(n["tx_rate"]), "white"))
    if gpu_t is not None:
        chips.append(Ln("GPU ", "grey").add(ftemp(gpu_t, th), tcol(gpu_t, 65, 80)))
    cl = ((d.get("usage") or {}).get("claude") or {})
    five = next((w for lab, w in (cl.get("windows") or []) if lab.strip() == "5h"), None)
    if five and five.get("pct") is not None:
        chips.append(Ln("Claude 5h ", "clawd").add("%d%%" % five["pct"], "white"))
    w = d.get("weather")
    if w and w.get("ok"):
        chips.append(Ln("%.0f%s%s " % (w["temp"], g["deg"], w["units"]), "yellow").add(WMO.get(w["code"], ""), "text"))

    def join(parts):
        out = Ln()
        for i, p in enumerate(parts):
            if i:
                out.gap(1).add(g["v"], "grey").gap(1)
            out.cat(p)
        return out

    if not logo:
        left = Ln("E", "pink").add("R", "pink").add("I", "blue").add("S", "blue").add(" " + s["host"] + " ", "white")
        return [lr(left.cat(join(chips)), Ln(s["now"], "white"), W)], None
    art = LOGO_BOX if th.cs != "ascii" else LOGO_ASCII
    split = 6 if th.cs != "ascii" else 9
    info = [
        Ln("ERIS CONSOLE", "white").gap().add(s["host"], "blue").gap().add(th.clean(s["os"]), "text")
        .gap().add(s["kernel"], "grey"),
        Ln(s["now"], "white").gap().add("up ", "grey").add(s["uptime"], "green").gap().add("load ", "grey")
        .add("%.2f %.2f %.2f" % tuple(c["load"]), "white").gap().add("procs ", "grey").add(str(s["procs"]), "white")
        .gap().add("ip ", "grey").add((n or {}).get("ip") or "N/A", "white"),
        join(chips),
    ]
    aw = len(art[0]) + 2
    natural = max(x.n for x in info)
    spare = W - aw - natural
    # the mascot gets a stage when there is room; the text widens a little on big screens
    stage_w = 0
    if th.quad and spare >= 18:
        grow = min(spare // 4, 24)
        stage_w = spare - grow - 2
        iw = natural + grow
    else:
        iw = W - aw
    rows = []
    for i in range(3):
        ln = Ln(art[i][:split], "pink").add(art[i][split:], "blue").add("  ")
        rows.append(ln.cat(info[i].fit(iw, g["ell"])).fit(W - stage_w if stage_w else W))
    return rows, ((W - stage_w, stage_w) if stage_w else None)


def dash_height(rows, pct):
    if rows < 10:
        return 1 if rows >= 4 else 0
    return max(3, min(int(round(rows * pct / 100.0)), rows - 6))


def render_parts(d, W, H, th, tick):
    """(lines, stage): H strings; with a stage the first 3 lines stop at stage x (the mascot fills the rest)."""
    if H <= 0 or W <= 0:
        return [], None
    logo = H >= 12 and W >= 70
    head, stage = header_lines(d, W, th, logo)
    head = head[:H]
    Hp = H - len(head)
    rows = [ln if stage else ln.fit(W) for ln in head]
    if Hp >= 3:
        enabled = [p for p in PRIO if not (p == "weather" and d.get("weather_off")) and not (p == "gpu" and not d["gpu"])]
        cache = {}

        def content(p, w):
            if (p, w) not in cache:
                try:
                    cache[(p, w)] = PANELS[p][4](d, w - 2, th)
                except Exception as e:  # a broken panel must not take the dashboard down
                    cache[(p, w)] = [Ln("N/A (%s)" % type(e).__name__, "grey")]
            return cache[(p, w)]

        def nat_h(p, w):
            return len(content(p, w)) + 2

        cols = layout(W, Hp, enabled, nat_h)
        body = [Ln() for _ in range(Hp)]
        for ci, c in enumerate(cols):
            y = 0
            for p, h in c["panels"]:
                lines = content(p, c["w"])
                if p in CHARTED and h - 2 > len(lines) + 2:       # tall panel: spare rows become a history chart
                    try:
                        lines = PANELS[p][4](d, c["w"] - 2, th, hi=h - 2)
                    except Exception:
                        pass
                tag = None
                if p == "storage" and len(lines) > h - 2 >= 2:
                    per = h - 3
                    data = lines[1:]
                    pages = max(1, -(-len(data) // per))
                    pg = (tick // 3) % pages
                    lines = [lines[0]] + data[pg * per:(pg + 1) * per]
                    tag = "%d/%d" % (pg + 1, pages)
                elif p == "cpu":
                    tag = pct_s(d["cpu"]["usage"]).strip()
                elif p == "usage":
                    cl = ((d.get("usage") or {}).get("claude") or {})
                    tag = cl.get("plan") or None
                for i, bl in enumerate(boxed(p, c["w"], h, lines, th, tag)):
                    body[y + i].cat(bl)
                y += h
            if ci < len(cols) - 1:
                for b in body:
                    b.add(" ")
        rows += [b.fit(W, spread=False) for b in body]
    elif Hp > 0:
        rows += [Ln(" " * W) for _ in range(Hp)]
    return [r.render() for r in rows[:H]], stage


def render(d, W, H, th, tick):
    """List of exactly H strings, each exactly W columns wide (escape codes excluded)."""
    lines, stage = render_parts(d, W, H, th, tick)
    if stage:
        x, sw = stage
        for i in range(min(3, len(lines))):
            lines[i] = lines[i] + " " * sw
    return lines


# ---------------------------------------------------------------- the Claude mascot

class Mascot:
    """Clawd, the Claude Code mascot, walking on top of the panels (2x2 'pixels' per cell).

    Sprite drawn after Claude Code's own banner art ( ▐▛███▜▌ / ▝▜█████▛▘ / ▘▘ ▝▝ ), 18x6 sub-pixels.
    """

    LEGS = {"stand": (4, 6, 11, 13), "a": (5, 7, 10, 12), "b": (3, 5, 12, 14)}

    def __init__(self):
        import random
        self.rnd = random.Random()
        self.x = 0                 # sub-pixel x of the sprite's left edge
        self.dir = 1
        self.mode = "walk"
        self.mode_left = 40        # ticks
        self.tick = 0
        self.blink = 0
        self.next_blink = 15
        self.look = 0
        self.jump = 0
        self.wave = 0
        self.speed = 1             # sub-pixels per tick
        self.mood = "ok"

    def sprite(self):
        w, h = 18, 6
        bm = [[0] * w for _ in range(h)]
        lift = 1 if self.jump > 0 else 0
        y0 = 1 - lift                 # feet on the bottom sub-row = standing on the panels
        def put(x, y):
            if 0 <= x < w and 0 <= y < h:
                bm[y][x] = 1
        for x in range(3, 15):
            put(x, y0)
            put(x, y0 + 1)
            put(x, y0 + 3)
        for x in range(1, 17):
            put(x, y0 + 2)                      # arms
        if self.wave:
            for x in (15, 16):                  # right arm raised and waving
                bm[y0 + 2][x] = 0
            up = (self.wave // 2) % 2
            put(15, y0 + 1)
            put(16 if up else 15, y0 if up else y0 + 1)
            put(17 if up else 16, max(0, y0 - 1) if up else y0)
        if not self.blink:
            shift = self.look if self.look else (1 if self.mode in ("walk", "run") else 0) * self.dir
            for ex in (5 + shift, 12 + shift):
                bm[y0 + 1][ex] = 0
        legs = self.LEGS["stand"]
        if self.mode in ("walk", "run") and not self.jump:
            legs = (self.LEGS["stand"], self.LEGS["a"], self.LEGS["stand"], self.LEGS["b"])[(self.tick // (1 if self.mode == "run" else 2)) % 4]
        if self.jump:
            legs = (5, 6, 11, 12)               # tucked
        for x in legs:
            put(x, y0 + 4)
        if self.dir < 0:
            bm = [list(reversed(r)) for r in bm]
        return bm

    def behave(self, ctx):
        """Pick what Clawd does next; ctx has cpu (%), claude_5h (%), busy (claude/codex running)."""
        r = self.rnd
        cpu = ctx.get("cpu") or 0
        hot = (ctx.get("claude_5h") or 0) >= 90
        self.mood = "tired" if hot else "busy" if ctx.get("busy") else "ok"
        self.mode_left -= 1
        if self.mode_left > 0:
            return
        pick = r.random()
        if self.mood == "busy" or cpu > 70:
            self.mode, self.mode_left = "run", r.randint(20, 60)
        elif self.mood == "tired":
            self.mode, self.mode_left = ("idle", r.randint(15, 40)) if pick < 0.5 else ("walk", r.randint(20, 40))
        elif pick < 0.55:
            self.mode, self.mode_left = "walk", r.randint(25, 90)
        elif pick < 0.75:
            self.mode, self.mode_left = "idle", r.randint(8, 25)
            self.look = r.choice((-1, 0, 1))
        elif pick < 0.87:
            self.mode, self.mode_left = "idle", 12
            self.jump = 3
        elif pick < 0.96:
            self.mode, self.mode_left = "idle", 16
            self.wave = 16
        else:
            self.dir = -self.dir
            self.mode, self.mode_left = "walk", r.randint(20, 60)

    def step(self, stage_w, ctx):
        """Advance one tick (0.2 s) and return the 3 stage rows."""
        self.tick += 1
        self.behave(ctx)
        span = stage_w * 2 - 18
        if self.mode in ("walk", "run"):
            self.look = 0
            sp = 2 if self.mode == "run" else 1 if self.mood != "tired" or self.tick % 2 else 0
            self.x += sp * self.dir
            if self.x <= 0 or self.x >= span:
                self.x = max(0, min(self.x, span))
                self.dir = -self.dir
        self.x = max(0, min(self.x, max(0, span)))
        if self.jump:
            self.jump -= 1
        if self.wave:
            self.wave -= 1
        if self.blink:
            self.blink -= 1
        else:
            self.next_blink -= 1
            if self.next_blink <= 0:
                self.blink = 1
                self.next_blink = self.rnd.randint(12, 35)
        return self.rows(stage_w)

    def rows(self, stage_w):
        bm = self.sprite()
        W = stage_w * 2
        canvas = [[0] * W for _ in range(6)]
        for y in range(6):
            for x in range(18):
                if bm[y][x] and 0 <= self.x + x < W:
                    canvas[y][self.x + x] = 1
        out = []
        for cy in range(3):
            chars = "".join(QUAD[(canvas[2 * cy][2 * cx], canvas[2 * cy][2 * cx + 1],
                                  canvas[2 * cy + 1][2 * cx], canvas[2 * cy + 1][2 * cx + 1])]
                            for cx in range(stage_w))
            out.append(STY["clawd"] + chars + R)
        return out


# ================================================================ data snapshot

class Collector:
    def __init__(self, conf):
        self.conf = conf
        self.cpu = Cpu()
        self.gpu = Gpu()
        self.net = Net()
        self.io = DiskIO()
        self.storage = Storage()
        self.services = Services()
        self.weather = Weather(conf)
        self.usage = Usage()
        for t in (self.storage, self.services, self.weather, self.usage):
            t.start()
        osr = {}
        for line in (rd("/etc/os-release", "") or "").splitlines():
            k, _, v = line.partition("=")
            osr[k] = v.strip('"')
        deb = rd("/etc/debian_version")
        name = osr.get("NAME", "Linux").replace(" GNU/Linux", "")
        self.os_name = "%s %s%s" % (name, deb or osr.get("VERSION_ID", ""),
                                    " (%s)" % osr["VERSION_CODENAME"] if osr.get("VERSION_CODENAME") else "")
        self.phys_disks = set()
        import collections
        self.hist = {k: collections.deque(maxlen=720) for k in ("cpu", "mem", "rx", "tx")}

    def stop(self):
        for t in (self.storage, self.services, self.weather, self.usage):
            t.stop()

    def snapshot(self):
        def safe(fn, default=None):
            try:
                return fn()
            except Exception as e:
                sys.stderr.write("eris: sample error %r\n" % e)
                return default

        st = self.storage.snap
        if st:
            self.phys_disks = {dk["name"] for dk in st["phys"]}
        up = float((rd("/proc/uptime", "0") or "0").split()[0])
        io_total, io_per = safe(lambda: self.io.sample(self.phys_disks), ((None, None), {}))
        cpu = safe(self.cpu.sample) or {"model": "N/A", "cores": 0, "threads": 0, "usage": None, "per": [],
                                        "load": [0, 0, 0], "pkg": None, "hottest": None, "max_seen": None,
                                        "crit": None, "throttle": "N/A", "throttle_count": None, "cpufreq": False}
        mem = safe(mem_sample)
        net = safe(self.net.sample)
        self.hist["cpu"].append(cpu.get("usage"))
        self.hist["mem"].append((mem or {}).get("pct"))
        if net:
            self.hist["rx"].append(net.get("rx_rate"))
            self.hist["tx"].append(net.get("tx_rate"))
        return {
            "disk_io": io_per,
            "hist": {k: list(v) for k, v in self.hist.items()},
            "cpu": cpu,
            "mem": mem,
            "gpu": safe(self.gpu.sample, []),
            "net": net,
            "storage": st,
            "svc": self.services.snap,
            "weather": self.weather.snap,
            "weather_off": self.conf.get("WEATHER") != "on",
            "usage": self.usage.snap,
            "sys": {"host": socket.gethostname(), "os": self.os_name, "kernel": os.uname().release,
                    "uptime": fmt_dur(up), "now": time.strftime("%a %Y-%m-%d %H:%M:%S"),
                    "procs": sum(1 for p in os.listdir("/proc") if p.isdigit()),
                    "io": io_total,
                    "refreshed": time.strftime("%H:%M:%S"), "every": self.conf.get("REFRESH", "2")},
        }


# ================================================================ console driver

class Console:
    """Owns the top H rows of a VT; the rest is the user's scroll region."""

    def __init__(self, tty):
        self.tty = tty
        self.n = E.tty_index(tty)
        maj, mnr = E.tty_rdev(tty)
        self.tty_nr = (mnr & 0xFF) | (maj << 8) | ((mnr & ~0xFF) << 12)
        self.fd = E.open_tty(tty)
        self.vcsa = os.open("/dev/vcsa%d" % self.n, os.O_RDONLY)
        self.H = 0
        self.rows = self.cols = 0
        self.cy = self.cx = 0
        self.aside = False
        self.snap = None
        self.leader = None
        self.leader_scan = 0.0
        self.state_file = os.path.join(E.RUN_DIR, "dashboard-tty%d.state" % self.n)
        self.handoff = os.path.join(E.RUN_DIR, "handoff-tty%d" % self.n)   # written by prompt.bash's preexec hook

    def handed_off(self):
        """The shell already gave the whole screen to a full-screen program (within the last 3 s)."""
        try:
            return time.time() - os.stat(self.handoff).st_mtime < 3.0
        except OSError:
            return False

    HANGUP = (errno.EIO, errno.EBADF, errno.ENXIO, errno.EPERM)

    def _retry(self, fn):
        """getty/login call vhangup() on every (re)login: the old fd then fails with EIO. Reopen and retry."""
        for attempt in range(3):
            try:
                return fn()
            except OSError as e:
                if e.errno not in self.HANGUP or attempt == 2:
                    raise
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                time.sleep(0.1)
                self.fd = E.open_tty(self.tty)

    def write(self, s):
        data = s.encode("utf-8", "replace")
        self._retry(lambda: E.write_all(self.fd, data))

    def geom(self):
        return self._retry(lambda: E.console_geometry(self.fd, self.tty))

    def save_state(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            f.write("H=%d\nASIDE=%d\nPID=%d\n" % (self.H, int(self.aside), os.getpid()))
        os.replace(tmp, self.state_file)

    def top(self):
        if self.H <= 0:
            return b""
        return os.pread(self.vcsa, 2 * self.H * self.cols, 4)

    def frame_seq(self, lines):
        out = ["\x1b7\x0f\x1b(B\x1b[?6l\x1b[%d;%dr" % (self.H + 1, self.rows)]
        for i, ln in enumerate(lines[:self.H]):
            out.append("\x1b[%d;1H%s" % (i + 1, ln))
        out.append("\x1b[0m\x1b[?6h\x1b8")
        return "".join(out)

    def regeom(self, remove, newh, lines=None):
        """Remove `remove` dashboard rows at the top, then make room for `newh` new ones.

        Shell content is pushed down (never cleared) and the cursor follows it; one write.
        """
        rows, cols, cy, cx = self.geom()
        s = ["\x1b[?6l\x1b[r"]
        if remove > 0:
            remove = min(remove, rows - 1)
            s.append("\x1b[1;1H\x1b[%dM" % remove)
            cy = max(0, cy - remove)
        if newh > 0:
            d = min(newh, rows - 1 - cy)
            if d > 0:
                s.append("\x1b[1;1H\x1b[%dL" % d)
                cy += d
            s.append("\x1b[%d;%dr\x1b[?6h\x1b[%d;%dH" % (newh + 1, rows, cy - newh + 1, cx + 1))
        else:
            s.append("\x1b[%d;%dH" % (cy + 1, cx + 1))
        self.H, self.rows, self.cols, self.cy, self.cx = newh, rows, cols, cy, cx
        if lines is not None and newh > 0:
            s.append(self.frame_seq(lines))
        self.write("".join(s))
        self.snap = self.top()
        self.save_state()

    def draw(self, lines):
        if self.handed_off():
            return
        self.write(self.frame_seq(lines))
        self.snap = self.top()

    def draw_stage(self, x, rows):
        """Repaint only the mascot's area of the top rows (one small atomic write per tick)."""
        if self.handed_off() or self.aside or self.H < len(rows):
            return
        out = ["\x1b7\x0f\x1b(B\x1b[?6l"]
        for i, row in enumerate(rows):
            out.append("\x1b[%d;%dH%s" % (i + 1, x + 1, row))
        out.append("\x1b[0m\x1b[?6h\x1b8")
        self.write("".join(out))
        self.snap = self.top()

    def tampered(self):
        return self.H > 0 and self.top() != self.snap

    def cursor_in_dash(self):
        rows, cols, cy, cx = self.geom()
        return cy < self.H

    # ---- foreground job detection
    def _stat(self, pid):
        s = open("/proc/%d/stat" % pid).read()
        f = s[s.rfind(")") + 2:].split()
        return int(f[2]), int(f[3]), int(f[4]), int(f[5])   # pgrp, session, tty_nr, tpgid

    def tpgid(self):
        for _ in range(2):
            if self.leader:
                try:
                    pg, sid, tnr, tp = self._stat(self.leader)
                    if tnr == self.tty_nr:
                        return tp
                except (OSError, ValueError, IndexError):
                    pass
                self.leader = None
            now = time.monotonic()
            if now - self.leader_scan < 1.0:
                return -1
            self.leader_scan = now
            for p in os.listdir("/proc"):
                if not p.isdigit():
                    continue
                try:
                    pg, sid, tnr, tp = self._stat(int(p))
                except (OSError, ValueError, IndexError):
                    continue
                if tnr == self.tty_nr and sid == int(p):
                    self.leader = int(p)
                    break
        return -1

    def fullscreen_fg(self, apps):
        pg = self.tpgid()
        if pg <= 0:
            return False
        stack, seen = [pg], 0
        while stack and seen < 24:
            pid = stack.pop()
            seen += 1
            try:
                ppg, _, tnr, _ = self._stat(pid)
                comm = open("/proc/%d/comm" % pid).read().strip()
            except (OSError, ValueError, IndexError):
                continue
            if pid != pg and ppg != pg and tnr == self.tty_nr:
                continue          # a background job of the shell, not part of the foreground job
            if comm in apps:
                return True
            try:
                kids = open("/proc/%d/task/%d/children" % (pid, pid)).read().split()
            except OSError:
                kids = []
            stack.extend(int(k) for k in kids)
        return False

    def step_aside(self):
        """A full-screen program took the foreground: give it the whole screen and make it redraw."""
        self.aside = True
        self.save_state()
        if self.handed_off():
            return            # the shell's preexec hook already reset the screen before the program drew
        self.write("\x1b[?6l\x1b[r\x1b[0m\x1b[H\x1b[2J")
        pg = self.tpgid()
        if pg > 0:
            try:
                os.killpg(pg, signal.SIGWINCH)
            except OSError:
                pass

    def come_back(self, newh, lines):
        """The shell is in the foreground again: clear leftovers, prompt line to the region top."""
        rows, cols, cy, cx = self.geom()
        s = ["\x1b[?6l\x1b[r\x1b[0m"]
        for r in range(0, cy):
            s.append("\x1b[%d;1H\x1b[2K" % (r + 1))
        if cy > newh:
            s.append("\x1b[1;1H\x1b[%dM" % (cy - newh))
        elif cy < newh:
            s.append("\x1b[1;1H\x1b[%dL" % (newh - cy))
        cy = newh
        s.append("\x1b[%d;%dr\x1b[?6h\x1b[1;%dH" % (newh + 1, rows, cx + 1))
        self.H, self.rows, self.cols, self.cy, self.cx = newh, rows, cols, cy, cx
        self.aside = False
        try:
            os.unlink(self.handoff)
        except FileNotFoundError:
            pass
        s.append(self.frame_seq(lines))
        self.write("".join(s))
        self.snap = self.top()
        self.save_state()

    def leave(self):
        """Stop: delete the dashboard rows (shell content moves up), full-screen scrolling again."""
        if self.aside:
            self.write("\x1b[?6l\x1b[r")
        elif self.H > 0:
            self.regeom(self.H, 0)
        for p in (self.state_file, self.handoff):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ================================================================ main loop

class Dashboard:
    def __init__(self, tty, conf):
        self.conf = conf
        self.conf_mtime = self.mtime()
        self.con = Console(tty)
        self.col = Collector(conf)
        self.stop = False
        self.tick = 0
        self.theme = self.make_theme()
        self.data = None
        self.lines = None
        self.stage = None
        self.mascot = Mascot()
        self.apps = set(E.FULLSCREEN_DEFAULT.split()) | set(conf["FULLSCREEN_APPS"].split())

    @staticmethod
    def mtime():
        try:
            return os.stat(E.CONF).st_mtime_ns
        except OSError:
            return 0

    def make_theme(self):
        cs = E.choose_charset(self.conf, self.con.tty)
        cps = None
        if cs != "ascii":
            try:
                cps = E.tty_unicode_set(self.con.tty)
            except OSError:
                cps = None
        return Theme(cs, cps)

    def height(self, rows):
        return dash_height(rows, int(self.conf["DASH_HEIGHT"]))

    def refresh(self):
        self.data = self.col.snapshot()
        lines, self.stage = render_parts(self.data, self.con.cols, self.con.H, self.theme, self.tick)
        if self.stage:
            sw = self.stage[1]
            self.mascot.x = max(0, min(self.mascot.x, sw * 2 - 18))
            for i, row in enumerate(self.mascot.rows(sw)):
                lines[i] += row
        self.lines = lines
        self.tick += 1

    def animate(self):
        """One mascot step on the free part of the header (every tick, ~5 fps)."""
        if not self.stage or self.con.aside or not self.data:
            return
        u = self.data.get("usage") or {}
        five = next((w for lab, w in ((u.get("claude") or {}).get("windows") or []) if lab.strip() == "5h"), None)
        ctx = {"cpu": (self.data.get("cpu") or {}).get("usage"), "busy": u.get("busy"),
               "claude_5h": five.get("pct") if five else None}
        x, sw = self.stage
        self.con.draw_stage(x, self.mascot.step(sw, ctx))

    def reload_conf(self):
        m = self.mtime()
        if m == self.conf_mtime:
            return False
        self.conf_mtime = m
        new = E.load_conf()
        self.conf.clear()
        self.conf.update(new)
        self.apps = set(E.FULLSCREEN_DEFAULT.split()) | set(self.conf["FULLSCREEN_APPS"].split())
        self.theme = self.make_theme()
        self.col.weather.wake.set()          # new city / units / on-off: fetch now
        return True

    def run(self):
        con = self.con
        rows, cols, cy, cx = con.geom()
        con.rows, con.cols = rows, cols
        con.H = self.height(rows)
        time.sleep(0.3)                     # first CPU/net deltas
        self.refresh()
        if con.fullscreen_fg(self.apps):
            con.H = 0
            con.aside = True
            con.save_state()
        else:
            con.regeom(0, self.height(rows), self.lines)
        E.sd_notify("READY=1\nSTATUS=dashboard on %s" % con.tty)
        next_refresh = time.monotonic() + float(self.conf["REFRESH"])
        while not self.stop:
            time.sleep(TICK)
            if self.stop:
                break
            try:
                self.loop_once(next_refresh)
            except Exception as e:  # keep running; the next tick repaints
                sys.stderr.write("eris: loop error %r\n" % e)
                time.sleep(1)
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + float(self.conf["REFRESH"])

    def loop_once(self, next_refresh):
        con = self.con
        changed = self.reload_conf()
        rows, cols, cy, cx = con.geom()
        fs = con.fullscreen_fg(self.apps) or (con.handed_off() and not con.aside)
        if fs:
            if not con.aside:
                con.step_aside()
            return
        newh = self.height(rows)
        if con.aside:
            con.rows, con.cols, con.H = rows, cols, newh
            self.refresh()
            con.come_back(newh, self.lines)
            return
        if (rows, cols) != (con.rows, con.cols):
            # font/resolution change: the kernel reset the scroll region and kept part of the old text
            shift = max(0, con.cy - cy)
            remnant = max(0, min(con.H - shift, rows))
            con.rows, con.cols, con.H = rows, cols, newh
            self.theme = self.make_theme()
            self.refresh()
            con.regeom(remnant, newh, self.lines)
            return
        con.cy, con.cx = cy, cx
        if changed and newh != con.H:
            old = con.H
            con.H = newh
            self.refresh()
            con.regeom(old, newh, self.lines)
            return
        if time.monotonic() >= next_refresh or changed:
            self.refresh()
            if cy < con.H:
                con.regeom(0, con.H, self.lines)
            else:
                con.draw(self.lines)
            return
        if con.tampered():
            if cy < con.H:                    # e.g. a terminal reset or a new getty put the cursor on top
                con.regeom(0, con.H, self.lines)
            else:
                con.draw(self.lines)
            return
        self.animate()


def render_cli(size, charset):
    rows, cols = [int(x) for x in size.lower().split("x")]
    conf = E.load_conf()
    col = Collector(conf)
    col.snapshot()
    deadline = time.time() + 40
    while time.time() < deadline and (col.storage.snap is None or col.services.snap is None
                                      or col.storage.next_smart == 0
                                      or (conf["WEATHER"] == "on" and col.weather.snap is None)):
        time.sleep(0.5)
    d = col.snapshot()
    col.stop()
    th = Theme("utf8", extras=True) if charset == "eris" else Theme(charset)
    H = dash_height(rows, int(conf["DASH_HEIGHT"]))
    for ln in render(d, cols, H, th, 0):
        sys.stdout.write(ln + "\n")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--render"]:
        render_cli(args[1], args[2] if len(args) > 2 else "utf8")
        return 0
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    if "--tty" in args:
        tty = args[args.index("--tty") + 1]
    os.makedirs(E.RUN_DIR, exist_ok=True)
    n = E.tty_index(tty)
    lock = open(os.path.join(E.RUN_DIR, "dashboard-tty%d.lock" % n), "w")   # O_CLOEXEC: children never inherit it
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("eris: a dashboard already runs on %s\n" % tty)
        return 1
    pidf = os.path.join(E.RUN_DIR, "dashboard-tty%d.pid" % n)
    with open(pidf, "w") as f:
        f.write("%d\n" % os.getpid())
    dash = Dashboard(tty, conf)

    def on_signal(signum, frame):
        dash.stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        dash.run()
    finally:
        try:
            dash.con.leave()
        except OSError as e:
            sys.stderr.write("eris: cleanup error %r\n" % e)
        dash.col.stop()
        for p in (pidf,):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
ERIS_EOF_erisd_py
  put_file /usr/local/lib/eris/erisfont.py 0644 << 'ERIS_EOF_erisfont_py'
"""ERIS console font builder.

The Linux console can only show glyphs that exist in the loaded font, and the stock console
fonts lack what the dashboard draws with: quadrant blocks (the Claude mascot), eighth blocks
(sparklines), a real ➜ for the prompt and weather icons. This module takes a 256-glyph Lat15
font of the chosen face and size, keeps every glyph and mapping, and appends the extra glyphs
drawn at that cell size, giving a 512-glyph PSF2 font. Python standard library only.
"""
import gzip
import math
import os
import re
import struct

FONT_VERSION = 3          # bump when glyph drawings change: cached fonts are rebuilt

# 2-cell weather icons in the private use area: (left half, right half)
ICONS = {
    "sun": (0xE000, 0xE001), "moon": (0xE002, 0xE003), "partly": (0xE004, 0xE005),
    "cloud": (0xE006, 0xE007), "fog": (0xE008, 0xE009), "drizzle": (0xE00A, 0xE00B),
    "rain": (0xE00C, 0xE00D), "snow": (0xE00E, 0xE00F), "storm": (0xE010, 0xE011),
}
QUADRANTS = {  # code point -> (UL, UR, LL, LR)
    0x2598: (1, 0, 0, 0), 0x259D: (0, 1, 0, 0), 0x2596: (0, 0, 1, 0), 0x2597: (0, 0, 0, 1),
    0x2580: (1, 1, 0, 0), 0x2584: (0, 0, 1, 1), 0x258C: (1, 0, 1, 0), 0x2590: (0, 1, 0, 1),
    0x259A: (1, 0, 0, 1), 0x259E: (0, 1, 1, 0), 0x2599: (1, 0, 1, 1), 0x259B: (1, 1, 1, 0),
    0x259C: (1, 1, 0, 1), 0x259F: (0, 1, 1, 1), 0x2588: (1, 1, 1, 1),
}
EIGHTHS = {0x2581 + i: i + 1 for i in range(7)}       # ▁▂▃▄▅▆▇ : lower i/8 filled
EXTRA_CHARS = "".join(chr(c) for c in list(QUADRANTS) + list(EIGHTHS)) + "➜▔━" + \
    "".join(chr(a) + chr(b) for a, b in ICONS.values())


# ---------------------------------------------------------------- PSF read / write

def read_psf(path):
    """-> dict(width, height, glyphs=[bytes], maps=[[str]]) for PSF1 or PSF2, .gz or plain."""
    data = open(path, "rb").read()
    if path.endswith(".gz"):
        data = gzip.decompress(data)
    if data[:2] == b"\x36\x04":
        mode, h = data[2], data[3]
        n = 512 if mode & 1 else 256
        w, row = 8, 1
        glyphs = [data[4 + i * h:4 + (i + 1) * h] for i in range(n)]
        maps = [[] for _ in range(n)]
        if mode & 2:
            tab, i, g, seq = data[4 + n * h:], 0, 0, None
            while i + 1 < len(tab) and g < n:
                v = tab[i] | tab[i + 1] << 8
                i += 2
                if v == 0xFFFF:
                    if seq:
                        maps[g].append(seq)
                    g, seq = g + 1, None
                elif v == 0xFFFE:
                    if seq:
                        maps[g].append(seq)
                    seq = ""
                elif seq is not None:
                    seq += chr(v)
                else:
                    maps[g].append(chr(v))
        return {"width": w, "height": h, "glyphs": glyphs, "maps": maps}
    if data[:4] == b"\x72\xb5\x4a\x86":
        _, _, hs, flags, n, cs, h, w = struct.unpack("<8I", data[:32])
        glyphs = [data[hs + i * cs:hs + (i + 1) * cs] for i in range(n)]
        maps = [[] for _ in range(n)]
        if flags & 1:
            parts = data[hs + n * cs:].split(b"\xff")
            for g in range(min(n, len(parts))):
                singles, *seqs = parts[g].split(b"\xfe")
                maps[g] += list(singles.decode("utf-8", "replace"))
                maps[g] += [s.decode("utf-8", "replace") for s in seqs if s]
        return {"width": w, "height": h, "glyphs": glyphs, "maps": maps}
    raise ValueError("not a PSF font: %s" % path)


def write_psf2(path, font):
    w, h = font["width"], font["height"]
    row = (w + 7) // 8
    cs = row * h
    n = len(font["glyphs"])
    out = bytearray(struct.pack("<8I", 0x864AB572, 0, 32, 1, n, cs, h, w))
    for g in font["glyphs"]:
        out += bytes(g).ljust(cs, b"\0")[:cs]
    for m in font["maps"]:
        singles = [s for s in m if len(s) == 1]
        seqs = [s for s in m if len(s) > 1]
        out += "".join(singles).encode("utf-8")
        for s in seqs:
            out += b"\xfe" + s.encode("utf-8")
        out += b"\xff"
    tmp = path + ".tmp"
    with gzip.open(tmp, "wb") as f:
        f.write(bytes(out))
    os.replace(tmp, path)


# ---------------------------------------------------------------- tiny rasterizer

class Canvas:
    """w x h pixels; shapes are given in normalized coordinates and supersampled 4x4."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = [[0.0] * w for _ in range(h)]

    def fill(self, inside, value=1.0):
        s = 4
        for y in range(self.h):
            for x in range(self.w):
                hit = 0
                for sy in range(s):
                    for sx in range(s):
                        u = (x + (sx + 0.5) / s) / self.w
                        v = (y + (sy + 0.5) / s) / self.h
                        if inside(u, v):
                            hit += 1
                if hit:
                    cov = hit / float(s * s)
                    self.px[y][x] = max(self.px[y][x], cov) if value > 0 else min(self.px[y][x], 1.0 - cov)

    def bits(self, x0, x1, threshold=0.45):
        """Rows of the column range [x0, x1) packed MSB-first, one glyph."""
        cols = x1 - x0
        row = (cols + 7) // 8
        out = bytearray()
        for y in range(self.h):
            val = 0
            for x in range(cols):
                if self.px[y][x0 + x] >= threshold:
                    val |= 1 << (row * 8 - 1 - x)
            out += val.to_bytes(row, "big")
        return bytes(out)


def disc(cx, cy, r, aspect):
    return lambda u, v: ((u - cx) * aspect) ** 2 + (v - cy) ** 2 <= r * r


def rect(u0, v0, u1, v1):
    return lambda u, v: u0 <= u <= u1 and v0 <= v <= v1


def seg(u0, v0, u1, v1, t, aspect):
    """Thick line segment (t = half thickness in height units)."""
    dx, dy = (u1 - u0) * aspect, v1 - v0
    ll = dx * dx + dy * dy or 1e-9

    def f(u, v):
        px, py = (u - u0) * aspect, v - v0
        k = max(0.0, min(1.0, (px * dx + py * dy) / ll))
        return (px - k * dx) ** 2 + (py - k * dy) ** 2 <= t * t
    return f


def union(*fs):
    return lambda u, v: any(f(u, v) for f in fs)


def minus(a, b):
    return lambda u, v: a(u, v) and not b(u, v)


# ---------------------------------------------------------------- glyph drawings

def cloud_shape(aspect, dy=0.0, scale=1.0):
    def sc(x, y, r):
        return disc(0.5 + (x - 0.5) * scale, y * scale + dy + (1 - scale) * 0.5, r * scale, aspect)
    return union(sc(0.33, 0.58, 0.17), sc(0.53, 0.47, 0.22), sc(0.73, 0.60, 0.15),
                 rect(0.5 - 0.33 * scale, 0.58 * scale + dy + (1 - scale) * 0.5,
                      0.5 + 0.30 * scale, 0.75 * scale + dy + (1 - scale) * 0.5))


def draw_icon(name, w, h):
    """2-cell icon on a (2w x h) canvas -> (left glyph, right glyph)."""
    c = Canvas(2 * w, h)
    aspect = (2.0 * w) / h            # u is stretched by this to get square units
    t = max(0.55 / h, 0.045)          # stroke half-thickness ~ one pixel
    if name in ("sun", "partly"):
        cx, cy, r = (0.5, 0.5, 0.2) if name == "sun" else (0.36, 0.36, 0.15)
        rays = []
        for k in range(8):
            a = k * math.pi / 4
            r0, r1 = r * 1.45, r * 2.05
            rays.append(seg(cx + math.cos(a) * r0 / aspect, cy + math.sin(a) * r0,
                            cx + math.cos(a) * r1 / aspect, cy + math.sin(a) * r1, t, aspect))
        sun = union(disc(cx, cy, r, aspect), *rays)
        if name == "sun":
            c.fill(sun)
        else:
            cl = cloud_shape(aspect, dy=0.08, scale=0.82)
            gap = lambda u, v: any(cl(u + du, v + dv) for du in (-1.5 / (2 * w), 0, 1.5 / (2 * w))
                                   for dv in (-1.5 / h, 0, 1.5 / h))
            c.fill(minus(sun, gap))
            c.fill(cl)
    elif name == "moon":
        c.fill(minus(disc(0.5, 0.5, 0.3, aspect), disc(0.62, 0.38, 0.26, aspect)))
    elif name == "cloud":
        c.fill(cloud_shape(aspect))
    elif name == "fog":
        for i, (v, a, b) in enumerate(((0.32, 0.18, 0.78), (0.52, 0.26, 0.86), (0.72, 0.14, 0.74))):
            c.fill(seg(a, v, b, v, t * 1.3, aspect))
    else:
        cl = cloud_shape(aspect, dy=-0.16, scale=0.9)
        c.fill(cl)
        if name == "rain":
            for x in (0.34, 0.52, 0.70):
                c.fill(seg(x + 0.04, 0.70, x - 0.04, 0.92, t, aspect))
        elif name == "drizzle":
            for x, y in ((0.34, 0.74), (0.52, 0.86), (0.70, 0.74)):
                c.fill(disc(x, y, t * 1.6, aspect))
        elif name == "snow":
            for x, y in ((0.32, 0.76), (0.52, 0.88), (0.72, 0.76)):
                r = t * 2.4
                c.fill(union(seg(x - r / aspect, y, x + r / aspect, y, t * 0.8, aspect),
                             seg(x, y - r, x, y + r, t * 0.8, aspect)))
        elif name == "storm":
            c.fill(union(seg(0.56, 0.66, 0.44, 0.80, t * 1.2, aspect), seg(0.44, 0.80, 0.58, 0.80, t * 1.2, aspect),
                         seg(0.58, 0.80, 0.46, 0.96, t * 1.2, aspect)))
    return c.bits(0, w), c.bits(w, 2 * w)


def draw_quadrant(q, w, h):
    c = Canvas(w, h)
    lx, ty = (w + 1) // 2, (h + 1) // 2
    for y in range(h):
        for x in range(w):
            top, left = y < ty, x < lx
            on = q[0] if top and left else q[1] if top else q[2] if left else q[3]
            c.px[y][x] = 1.0 if on else 0.0
    return c.bits(0, w)


def draw_eighth(k, w, h):
    c = Canvas(w, h)
    rows = max(1, round(h * k / 8.0))
    for y in range(h - rows, h):
        for x in range(w):
            c.px[y][x] = 1.0
    return c.bits(0, w)


def draw_arrow(w, h):
    """➜ heavy round-tipped rightwards arrow."""
    c = Canvas(w, h)
    aspect = float(w) / h
    t = max(0.075, 1.3 / h)
    c.fill(union(seg(0.0, 0.56, 0.6, 0.56, t, aspect),
                 lambda u, v: 0.36 <= u <= 1.0 and abs(v - 0.56) <= (1.0 - u) * 0.95 * aspect))
    return c.bits(0, w)


def draw_heavy_h(w, h):
    """━ centered thick horizontal line (weather range bars)."""
    c = Canvas(w, h)
    th = max(2, round(h / 5.0))
    y0 = (h - th) // 2
    for y in range(y0, y0 + th):
        for x in range(w):
            c.px[y][x] = 1.0
    return c.bits(0, w)


def draw_upper_eighth(w, h):
    c = Canvas(w, h)
    for y in range(max(1, round(h / 8.0))):
        for x in range(w):
            c.px[y][x] = 1.0
    return c.bits(0, w)


# ---------------------------------------------------------------- build

def lat15_variant(path):
    """The 256-glyph Lat15 font of the same face and size, if installed (e.g. for Uni3-Terminus24x12)."""
    base = os.path.basename(path)
    m = re.match(r"^(?:cached_)?(?:Uni\d|Lat\d+|Lat15|[A-Za-z]+)-(.+?\.psfu?(?:\.gz)?)$", base)
    if m:
        cand = os.path.join("/usr/share/consolefonts", "Lat15-" + m.group(1))
        if os.path.isfile(cand):
            return cand
    return path


def build(base_path, out_path):
    """Write the ERIS font for base_path's face/size; returns a short description."""
    src = lat15_variant(base_path)
    font = read_psf(src)
    n0 = len(font["glyphs"])
    if n0 > 256:
        raise ValueError("%s has %d glyphs, no room for the ERIS glyphs" % (os.path.basename(src), n0))
    w, h = font["width"], font["height"]
    have = {cp for m in font["maps"] for cp in m if len(cp) == 1}
    glyphs, maps = list(font["glyphs"]), [list(m) for m in font["maps"]]

    def add(ch, bits, replace=False):
        if ch in have and not replace:
            return
        if ch in have:                       # drop the old mapping so ours wins
            for m in maps:
                if ch in m:
                    m.remove(ch)
        glyphs.append(bits)
        maps.append([ch])

    for cp, q in QUADRANTS.items():           # the stock ▀▄▌▐█ shapes vary per font: draw all exactly
        add(chr(cp), draw_quadrant(q, w, h), replace=True)
    for cp, k in EIGHTHS.items():
        add(chr(cp), draw_eighth(k, w, h), replace=True)
    add("➜", draw_arrow(w, h), replace=True)
    add("▔", draw_upper_eighth(w, h))
    add("━", draw_heavy_h(w, h), replace=True)
    for name, (a, b) in ICONS.items():
        left, right = draw_icon(name, w, h)
        add(chr(a), left, replace=True)
        add(chr(b), right, replace=True)
    if len(glyphs) > 512:
        raise ValueError("too many glyphs: %d" % len(glyphs))
    blank = bytes(((w + 7) // 8) * h)
    while len(glyphs) < 512:
        glyphs.append(blank)
        maps.append([])
    write_psf2(out_path, {"width": w, "height": h, "glyphs": glyphs, "maps": maps})
    return "%s + %d ERIS glyphs (%dx%d)" % (os.path.basename(src), len([m for m in maps[n0:] if m]), w, h)


def eris_font_path(base_path, cache_dir):
    name = re.sub(r"\.psfu?(\.gz)?$", "", os.path.basename(lat15_variant(base_path)))
    return os.path.join(cache_dir, "eris%d-%s.psf.gz" % (FONT_VERSION, name))


def ensure(base_path, cache_dir):
    """Path of the (cached) ERIS font for base_path, building it when needed."""
    os.makedirs(cache_dir, exist_ok=True)
    out = eris_font_path(base_path, cache_dir)
    if not os.path.isfile(out) or os.path.getmtime(out) < os.path.getmtime(lat15_variant(base_path)):
        build(base_path, out)
    return out


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1], sys.argv[2]))
ERIS_EOF_erisfont_py
  put_file /usr/local/bin/eris 0755 << 'ERIS_EOF_eris'
#!/usr/bin/python3
"""eris - control the ERIS Console.

  eris                      settings menu
  eris on | off | toggle    start / stop the dashboard now
  eris enable | disable     dashboard at boot (and start / stop it now)
  eris status               what is running and how the console is set up
  eris set KEY VALUE        change a setting (eris config lists them)
  eris get KEY | config
  eris font [list | set NAME|default | current]
  eris apply                re-apply the console look (font, palette, blanking, kernel messages)
  eris render [ROWSxCOLS] [utf8|safe|ascii]   print one dashboard frame here
  eris selftest             quick checks of the installed files
  eris version
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/usr/local/lib/eris")
import erislib as E  # noqa: E402

UNIT = "eris-dashboard.service"
LOOK_UNIT = "eris-console.service"
DAEMON = "/usr/local/lib/eris/erisd.py"
TTYOUT = sys.stdout.isatty()


def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if TTYOUT else s


PINK, PURPLE, BLUE, GREEN, AMBER, RED, CYAN, GREY, WHITE = "1;35", "35", "1;34", "1;32", "33", "1;31", "1;36", "1;30", "1;37"


def sh(*cmd, timeout=60):
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def need_root():
    if os.geteuid() != 0:
        sys.exit("eris: run as root")


def svc_state(unit=UNIT):
    return sh("systemctl", "is-active", unit).stdout.strip() or "unknown"


def svc_enabled(unit=UNIT):
    return sh("systemctl", "is-enabled", unit).stdout.strip() or "unknown"


# ---------------------------------------------------------------- dashboard service

def cmd_on():
    need_root()
    if svc_state() == "active":
        print("ERIS dashboard: already %s" % c(GREEN, "ON"))
        return 0
    r = sh("systemctl", "start", UNIT, timeout=60)
    if svc_state() == "active":
        print("ERIS dashboard: %s  (eris off to hide it)" % c(GREEN, "ON"))
        return 0
    print("ERIS dashboard: %s %s" % (c(RED, "failed to start"), r.stderr.strip()))
    print(sh("journalctl", "-u", UNIT, "-n", "8", "--no-pager", "-o", "cat").stdout)
    return 1


def cmd_off():
    need_root()
    sh("systemctl", "stop", UNIT, timeout=30)
    print("ERIS dashboard: %s" % c(GREY, "OFF"))
    return 0


def cmd_toggle():
    return cmd_off() if svc_state() == "active" else cmd_on()


def cmd_enable():
    need_root()
    sh("systemctl", "enable", UNIT)
    E.save_conf({"DASH_AT_BOOT": "yes"})
    print("ERIS dashboard: starts at boot")
    return cmd_on()


def cmd_disable():
    need_root()
    sh("systemctl", "disable", UNIT)
    E.save_conf({"DASH_AT_BOOT": "no"})
    print("ERIS dashboard: not started at boot")
    return cmd_off()


def daemon_info():
    r = sh("systemctl", "show", UNIT, "-p", "MainPID", "--value")
    pid = r.stdout.strip()
    if not pid or pid == "0":
        return None
    p = sh("ps", "-o", "pid=,pcpu=,rss=,etime=", "-p", pid).stdout.split()
    return {"pid": pid, "cpu": p[1], "rss_kb": p[2], "etime": p[3]} if len(p) == 4 else {"pid": pid}


def cmd_status():
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    st, en = svc_state(), svc_enabled()
    on = st == "active"
    print(c(PINK, "ERIS") + " Console %s" % E.VERSION)
    line = "%s  (%s at boot)" % (c(GREEN, "ON") if on else c(GREY, "OFF"), "started" if en == "enabled" else "not started")
    di = daemon_info() if on else None
    if di and "cpu" in di:
        line += "  pid %s, %s%% CPU, %d MB RSS, up %s" % (di["pid"], di["cpu"], int(di["rss_kb"]) // 1024, di["etime"])
    print("  dashboard : " + line)
    try:
        fd = E.open_tty(tty, write=False)
        rows, cols, _, _ = E.console_geometry(fd, tty)
        os.close(fd)
        w, h, n = E.font_info(tty)
        cs = E.choose_charset(conf, tty)
        print("  console   : %s %dx%d, font %dx%d (%d glyphs), charset %s" % (tty, cols, rows, w, h, n, cs))
        if on:
            sf = os.path.join(E.RUN_DIR, "dashboard-tty%d.state" % E.tty_index(tty))
            hh = re.search(r"H=(\d+)", open(sf).read()) if os.path.exists(sf) else None
            print("  split     : top %s rows dashboard, %s rows shell" % (hh.group(1) if hh else "?",
                                                                         rows - int(hh.group(1)) if hh else "?"))
    except OSError as e:
        print("  console   : %s not available (%s)" % (tty, e))
    pk = " ".join(E.printk())
    try:
        blank = int(open("/sys/module/kernel/parameters/consoleblank").read())
    except (OSError, ValueError):
        blank = None
    print("  look      : palette %s, blanking %s, kernel messages %s (printk %s), font %s" % (
        conf["PALETTE"], "never" if not blank else "%d min" % (blank // 60), conf["KERNEL_MSGS"], pk,
        conf["CONSOLE_FONT"] or "system (console-setup)"))
    print("  prompt    : scope %s" % conf["PROMPT_SCOPE"])
    print("  settings  : %s   (eris = menu, eris config = list)" % E.CONF)
    print("  look unit : %s %s" % (LOOK_UNIT, svc_state(LOOK_UNIT)))
    return 0


# ---------------------------------------------------------------- console look (layer A)

def ensure_originals():
    """Remember what the system looked like before ERIS touched it (for eris-uninstall)."""
    os.makedirs(E.STATE_DIR, exist_ok=True)

    def keep(name, value):
        p = os.path.join(E.STATE_DIR, name)
        if not os.path.exists(p) and value is not None:
            with open(p, "w") as f:
                f.write(value)

    try:
        keep("vt-default.orig", "".join("%s=%s\n" % kv for kv in E.read_vt_default().items()))
    except OSError:
        pass
    keep("printk.orig", " ".join(E.printk()))
    try:
        keep("consoleblank.orig", open("/sys/module/kernel/parameters/consoleblank").read().strip())
    except OSError:
        pass
    if os.path.exists(E.CONSOLE_SETUP):
        keep("console-setup.orig", open(E.CONSOLE_SETUP).read())
    keep("system-font.orig", E.system_font() or "")


def font_ok(tty, path):
    try:
        w, h, n = E.font_info(tty)
        fw, fh, fn, _ = E.psf_header(path)
        return (w, h, n) == (fw, fh, fn) and len(E.tty_unicode_set(tty)) >= 200
    except (OSError, ValueError):
        return False


def desired_font(conf):
    if conf["CONSOLE_FONT"]:
        return E.font_path(conf["CONSOLE_FONT"])
    return E.system_font()


def loaded_font(conf):
    """The file actually put on the console: the ERIS build of the chosen font (with the mascot,
    icon, sparkline and ➜ glyphs) unless ERIS_GLYPHS=off or it cannot be built."""
    base = desired_font(conf)
    if not base or conf.get("ERIS_GLYPHS", "on") != "on":
        return base
    try:
        import erisfont
        return erisfont.ensure(base, os.path.join(E.STATE_DIR, "fonts"))
    except (OSError, ValueError) as e:
        E.log("ERIS font for %s not built: %s" % (base, e))
        return base


def apply_font(conf, force=False):
    tty = conf["CONSOLE_TTY"]
    path = loaded_font(conf)
    if not path:
        return "no font configured"
    if force or not font_ok(tty, path):
        E.set_font(tty, path)
        return "loaded %s" % os.path.basename(path)
    return "kept %s" % os.path.basename(path)


def apply_palette(conf, redraw=True):
    tty = conf["CONSOLE_TTY"]
    if conf["PALETTE"] == "neon":
        E.write_vt_default(E.neon_vt_default())
    else:
        orig = os.path.join(E.STATE_DIR, "vt-default.orig")
        if os.path.exists(orig):
            E.write_vt_default(dict(l.strip().split("=", 1) for l in open(orig) if "=" in l))
    # default colours white on black, then the palette itself
    E.tty_write(tty, E.palette_seq(conf["PALETTE"]) + "\033[0m")
    # A 32-bit framebuffer bakes colours into pixels: text already on screen keeps the old palette
    # until it is drawn again. Loading the same font makes fbcon redraw the whole console.
    path = loaded_font(conf)
    if redraw and path:
        try:
            E.set_font(tty, path)
        except (RuntimeError, OSError):
            pass


def apply_console(mode="now", quiet=True):
    need_root()
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    if not os.path.exists(tty):
        return 0
    if mode == "login":
        try:
            if os.ttyname(0) != tty or os.environ.get("SSH_CONNECTION"):
                return 0
        except OSError:
            return 0
    if mode == "boot":
        deadline = time.time() + 30
        while not E.fbcon_ready() and time.time() < deadline:
            time.sleep(0.5)
    ensure_originals()
    notes = []
    try:
        apply_palette(conf, redraw=False)
        E.tty_write(tty, E.blank_seq(conf["BLANK_MINUTES"]))
        notes.append("palette %s, blank %s min" % (conf["PALETTE"], conf["BLANK_MINUTES"]))
    except OSError as e:
        notes.append("look: %s" % e)
    try:
        # after the palette: (re)loading the font also repaints the console in the new colours
        notes.append("font: " + apply_font(conf, force=(mode in ("boot", "now"))))
    except (RuntimeError, OSError) as e:
        notes.append("font: %s" % e)
    want = "1" if conf["KERNEL_MSGS"] == "hidden" else None
    if (want == "1") != (E.printk()[0] == "1") or (want == "1") != os.path.exists(E.SYSCTL_FILE):
        notes.append("printk " + E.set_kernel_msgs(conf["KERNEL_MSGS"]))
    try:
        arrow, err = E.write_prompt_env(tty, conf)
        notes.append("prompt glyphs %s %s" % (arrow, err))
    except OSError as e:
        notes.append("prompt env: %s" % e)
    E.log("apply %s: %s" % (mode, "; ".join(notes)))
    if not quiet:
        for n in notes:
            print("  " + n)
    return 0


# ---------------------------------------------------------------- fonts

def font_setup_values(name):
    m = re.match(r"^([A-Za-z0-9]+)-([A-Za-z]+?)(\d+)(?:x(\d+))?$", name)
    if not m:
        return None
    codeset, face, a, b = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    h, w = (a, int(b)) if b else (a, 8)
    return {"CODESET": codeset, "FONTFACE": face, "FONTSIZE": "%dx%d" % (w, h)}


def backup_file(path):
    if not os.path.exists(path):
        return None
    dest = os.path.join(E.BACKUP_ROOT, time.strftime("%Y%m%d-%H%M%S"), path.lstrip("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def persist_console_setup(name):
    """Make console-setup load the same font early at boot (ERIS re-applies it after the KMS takeover)."""
    orig = os.path.join(E.STATE_DIR, "console-setup.orig")
    bk = backup_file(E.CONSOLE_SETUP)
    if name is None:
        if os.path.exists(orig):
            shutil.copyfile(orig, E.CONSOLE_SETUP)
    else:
        vals = font_setup_values(name)
        if not vals:
            return "console-setup unchanged (unknown font name pattern)"
        lines = open(E.CONSOLE_SETUP).read().splitlines() if os.path.exists(E.CONSOLE_SETUP) else []
        seen = set()
        for i, line in enumerate(lines):
            m = re.match(r"^\s*([A-Z_]+)=", line)
            if m and m.group(1) in vals:
                lines[i] = '%s="%s"' % (m.group(1), vals[m.group(1)])
                seen.add(m.group(1))
            elif m and m.group(1) == "FONT":
                lines[i] = "# " + line + "   # disabled by ERIS (CODESET/FONTFACE/FONTSIZE are used)"
        for k, v in vals.items():
            if k not in seen:
                lines.append('%s="%s"' % (k, v))
        with open(E.CONSOLE_SETUP + ".tmp", "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(E.CONSOLE_SETUP + ".tmp", E.CONSOLE_SETUP)
    before = set(glob.glob("/etc/console-setup/cached_*.psf*"))
    r = sh("setupcon", "--save-only", timeout=90)
    track = os.path.join(E.STATE_DIR, "cached-created")
    created = set(open(track).read().split()) if os.path.exists(track) else set()
    created |= set(glob.glob("/etc/console-setup/cached_*.psf*")) - before
    loaded = E.system_font()
    ok = r.returncode == 0 and (name is None or (loaded and os.path.basename(loaded).startswith(name + ".")))
    if not ok and bk:
        shutil.copyfile(bk, E.CONSOLE_SETUP)
        sh("setupcon", "--save-only", timeout=90)
    # cached font copies that only ERIS made and nothing loads any more
    keep = {"/etc/console-setup/cached_%s" % os.path.basename(E.system_font() or "")}
    for f in sorted(created - keep):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass
    created &= keep
    with open(track, "w") as fh:
        fh.write("\n".join(sorted(created)) + "\n")
    if not ok:
        return "console-setup could not use %s (restored; ERIS still applies the font at boot): %s" % (name, r.stderr.strip()[:120])
    return "console-setup updated (backup %s)" % bk if bk else "console-setup updated"


def set_console_font(name):
    need_root()
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    if name in ("default", "system", ""):
        E.save_conf({"CONSOLE_FONT": ""})
        msg = persist_console_setup(None)
    else:
        if not E.font_path(name):
            raise ValueError("font %s not found in %s" % (name, E.FONT_DIR))
        E.save_conf({"CONSOLE_FONT": name})
        msg = persist_console_setup(name)
    path = loaded_font(E.load_conf())
    if path:
        E.set_font(tty, path)
    E.write_prompt_env(tty, E.load_conf())
    fd = E.open_tty(tty, write=False)
    rows, cols, _, _ = E.console_geometry(fd, tty)
    os.close(fd)
    w, h, n = E.font_info(tty)
    E.log("console font -> %s (%dx%d glyphs, %dx%d console); %s" % (name or "default", w, h, cols, rows, msg))
    return "font %dx%d -> console %dx%d; %s" % (w, h, cols, rows, msg)


def font_table(bold=False):
    res = E.fb_resolution()
    rows = []
    for f in E.terminus_fonts():
        if (f["face"] != "Terminus") and not bold:
            continue
        if bold and f["face"] == "Terminus":
            continue
        grid = "%dx%d" % (res[0] // f["w"], res[1] // f["h"]) if res else "?"
        rows.append((f, grid))
    return rows


def cmd_font(args):
    sub = args[0] if args else "list"
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    if sub == "current":
        w, h, n = E.font_info(tty)
        print("%s: font %dx%d, %d glyphs; setting CONSOLE_FONT=%s; system font %s" % (
            tty, w, h, n, conf["CONSOLE_FONT"] or "(system)", E.system_font()))
        return 0
    if sub == "list":
        for bold in (False, True):
            for f, grid in font_table(bold):
                print("%-28s %2dx%-3d glyph  -> %s console  (%s)" % (f["name"], f["w"], f["h"], grid, f["charset"]))
        modes = []
        for m in sorted(set(E.rd("/sys/class/drm/%s/modes" % d) or "" for d in os.listdir("/sys/class/drm")
                            if re.fullmatch(r"card\d+-.+", d) and E.rd("/sys/class/drm/%s/status" % d) == "connected")):
            modes += m.split()
        res = E.fb_resolution()
        print("display: %s now; modes offered by the monitor: %s" % ("%dx%d" % res if res else "?",
                                                                   " ".join(dict.fromkeys(modes)) or "?"))
        return 0
    if sub == "set" and len(args) > 1:
        print(set_console_font(args[1]))
        return 0
    print("usage: eris font [list | set NAME|default | current]")
    return 2


# ---------------------------------------------------------------- settings

SIDE_EFFECTS = {
    "PALETTE": lambda conf: apply_palette(conf),
    "BLANK_MINUTES": lambda conf: E.tty_write(conf["CONSOLE_TTY"], E.blank_seq(conf["BLANK_MINUTES"])),
    "KERNEL_MSGS": lambda conf: E.set_kernel_msgs(conf["KERNEL_MSGS"]),
    "DASH_AT_BOOT": lambda conf: sh("systemctl", "enable" if conf["DASH_AT_BOOT"] == "yes" else "disable", UNIT),
    "FULLSCREEN_APPS": lambda conf: E.write_prompt_env(conf["CONSOLE_TTY"], conf),
    "ERIS_GLYPHS": lambda conf: (apply_font(conf, force=True), E.write_prompt_env(conf["CONSOLE_TTY"], conf)),
}


def set_setting(key, value):
    need_root()
    if key == "CONSOLE_FONT":
        return set_console_font(value or "default")
    E.validate(key, value)
    E.save_conf({key: value})
    conf = E.load_conf()
    if key in SIDE_EFFECTS:
        SIDE_EFFECTS[key](conf)
    note = ""
    if key == "PROMPT_SCOPE":
        note = " (new shells; this one: source ~/.bashrc)"
    elif key in ("DASH_HEIGHT", "REFRESH", "WEATHER", "WEATHER_CITY", "WEATHER_UNITS", "CHARSET", "FULLSCREEN_APPS"):
        note = " (a running dashboard picks it up within a second)"
    E.log("set %s=%s" % (key, value))
    return "%s=%s%s" % (key, value, note)


def cmd_config():
    conf = E.load_conf()
    for k in E.SETTINGS:
        print('%-15s = "%s"   # %s' % (k, conf[k], E.SETTINGS[k][1]))
    return 0


# ---------------------------------------------------------------- menu

def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def menu():
    need_root()
    while True:
        conf = E.load_conf()
        tty = conf["CONSOLE_TTY"]
        on = svc_state() == "active"
        try:
            cs = E.choose_charset(conf, tty)
            w, h, n = E.font_info(tty)
            fd = E.open_tty(tty, write=False)
            rows, cols, _, _ = E.console_geometry(fd, tty)
            os.close(fd)
            fontdesc = "%s (%dx%d glyphs, console %dx%d)" % (conf["CONSOLE_FONT"] or "system", w, h, cols, rows)
        except OSError:
            cs, fontdesc = "?", conf["CONSOLE_FONT"] or "system"
        city = conf["WEATHER_CITY"] or "auto (by IP)"
        print()
        print(" " + c(PINK, "E") + c(PINK, "R") + c(BLUE, "I") + c(BLUE, "S") + c(WHITE, " Console settings") +
              "      dashboard: %s, %s at boot" % (c(GREEN, "ON") if on else c(GREY, "OFF"),
                                                  "starts" if conf["DASH_AT_BOOT"] == "yes" else "not started"))
        items = [
            ("1", "Dashboard height", "%s %%" % conf["DASH_HEIGHT"]),
            ("2", "Refresh interval", "%s s" % conf["REFRESH"]),
            ("3", "Weather", "%s  city: %s  units: %s" % (conf["WEATHER"], city, conf["WEATHER_UNITS"])),
            ("4", "Palette", conf["PALETTE"]),
            ("5", "Character set", "%s (now %s)" % (conf["CHARSET"], cs)),
            ("6", "Prompt scope", conf["PROMPT_SCOPE"]),
            ("7", "Dashboard at boot", conf["DASH_AT_BOOT"]),
            ("8", "Console font size", fontdesc),
            ("9", "Screen blanking", "never" if conf["BLANK_MINUTES"] == "0" else conf["BLANK_MINUTES"] + " min"),
            ("10", "Kernel messages on screen", "no (hidden)" if conf["KERNEL_MSGS"] == "hidden" else "yes"),
            ("11", "ERIS glyphs (Clawd, icons)", conf["ERIS_GLYPHS"]),
        ]
        for k, label, val in items:
            print("  %s %s %s" % (c(CYAN, "%2s)" % k), label.ljust(26, "."), c(AMBER, val)))
        print("  %s start now   %s stop now   %s quit" % (c(GREEN, " s)"), c(RED, "x)"), c(PURPLE, "q)")))
        ch = ask(" > ")
        if ch is None or ch.lower() in ("q", ""):
            return 0
        try:
            msg = menu_item(ch.lower(), conf)
        except (ValueError, RuntimeError, OSError) as e:
            msg = c(RED, "not changed: %s" % e)
        if msg:
            print("  " + msg)


def choose(prompt, options, cur):
    print("   " + "  ".join("%s %s" % (c(CYAN, "%d)" % (i + 1)), o + (" *" if o == cur else "")) for i, o in enumerate(options)))
    a = ask("   %s [1-%d]: " % (prompt, len(options)))
    if a and a.isdigit() and 1 <= int(a) <= len(options):
        return options[int(a) - 1]
    return None


def menu_item(ch, conf):
    if ch == "1":
        v = ask("   dashboard height in %% (15-70) [%s]: " % conf["DASH_HEIGHT"])
        return set_setting("DASH_HEIGHT", v) if v else None
    if ch == "2":
        v = ask("   refresh seconds (1-30) [%s]: " % conf["REFRESH"])
        return set_setting("REFRESH", v) if v else None
    if ch == "3":
        v = choose("weather", ["on", "off"], conf["WEATHER"])
        out = [set_setting("WEATHER", v)] if v else []
        city = ask("   city (empty = keep '%s', '-' = auto by IP): " % (conf["WEATHER_CITY"] or "auto"))
        if city:
            out.append(set_setting("WEATHER_CITY", "" if city == "-" else city))
        u = choose("units", ["C", "F"], conf["WEATHER_UNITS"])
        if u:
            out.append(set_setting("WEATHER_UNITS", u))
        return "; ".join(out) or None
    if ch == "4":
        v = choose("palette", ["neon", "default"], conf["PALETTE"])
        return set_setting("PALETTE", v) if v else None
    if ch == "5":
        v = choose("charset", ["auto", "utf8", "safe", "ascii"], conf["CHARSET"])
        return set_setting("CHARSET", v) if v else None
    if ch == "6":
        v = choose("prompt scope", ["tty1", "all"], conf["PROMPT_SCOPE"])
        return set_setting("PROMPT_SCOPE", v) if v else None
    if ch == "7":
        v = choose("dashboard at boot", ["yes", "no"], conf["DASH_AT_BOOT"])
        return set_setting("DASH_AT_BOOT", v) if v else None
    if ch == "8":
        return font_menu(conf)
    if ch == "9":
        v = choose("blank the screen after", ["never", "5", "10", "15", "30", "60"],
                   "never" if conf["BLANK_MINUTES"] == "0" else conf["BLANK_MINUTES"])
        return set_setting("BLANK_MINUTES", "0" if v == "never" else v) if v else None
    if ch == "10":
        v = choose("kernel messages on the console", ["hidden", "shown"], conf["KERNEL_MSGS"])
        return set_setting("KERNEL_MSGS", v) if v else None
    if ch == "11":
        v = choose("ERIS glyphs in the console font", ["on", "off"], conf["ERIS_GLYPHS"])
        return set_setting("ERIS_GLYPHS", v) if v else None
    if ch == "s":
        cmd_on()
        return None
    if ch == "x":
        cmd_off()
        return None
    return "unknown choice"


def font_menu(conf):
    bold = False
    while True:
        table = font_table(bold)
        res = E.fb_resolution()
        print("   Console font (screen %s). Terminus %s, best charset per size (Uni3 > Uni2 > Lat15):" % (
            "%dx%d" % res if res else "?", "Bold" if bold else "regular"))
        for i, (f, grid) in enumerate(table):
            print("   %s %-6s -> %-8s %s" % (c(CYAN, "%2d)" % (i + 1)), "%dx%d" % (f["w"], f["h"]), grid, f["name"]))
        print("   %s system default (console-setup)   %s %s   %s back" % (
            c(CYAN, " d)"), c(CYAN, "b)"), "regular" if bold else "bold", c(CYAN, "q)")))
        a = ask("   font: ")
        if not a or a == "q":
            return None
        if a == "b":
            bold = not bold
            continue
        if a == "d":
            return set_console_font("default")
        if a.isdigit() and 1 <= int(a) <= len(table):
            return set_console_font(table[int(a) - 1][0]["name"])
        print("   unknown choice")


# ---------------------------------------------------------------- misc commands

def cmd_reset_tty(args):
    conf = E.load_conf()
    tty = conf["CONSOLE_TTY"]
    sf = os.path.join(E.RUN_DIR, "dashboard-tty%d.state" % E.tty_index(tty))
    st = {}
    if os.path.exists(sf):
        for line in open(sf):
            k, _, v = line.strip().partition("=")
            st[k] = v
    if "--if-dirty" in args and not st:
        return 0
    fd = E.open_tty(tty)
    try:
        rows, cols, cy, cx = E.console_geometry(fd, tty)
        s = "\033[?6l\033[r"
        h = int(st.get("H", "0") or 0)
        if h and st.get("ASIDE") != "1":
            s += "\033[1;1H\033[%dM" % h
            cy = max(0, cy - h)
        s += "\033[0m\033[%d;%dH" % (cy + 1, cx + 1)
        E.write_all(fd, s)
    finally:
        os.close(fd)
    for p in (sf,):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    E.log("reset-tty %s (dashboard rows %s)" % (tty, st.get("H", "0")))
    return 0


def cmd_selftest():
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print("  %s %s %s" % (c(GREEN, "PASS") if ok else c(RED, "FAIL"), name, detail))

    print(c(PINK, "ERIS") + " selftest")
    for f in ("/usr/local/lib/eris/erislib.py", DAEMON, "/usr/local/bin/eris"):
        try:
            compile(open(f).read(), f, "exec")
            check("python syntax", True, f)
        except (OSError, SyntaxError) as e:
            check("python syntax", False, "%s: %s" % (f, e))
    for f in ("/etc/eris/prompt.bash", "/usr/local/sbin/eris-uninstall"):
        r = sh("bash", "-n", f)
        check("bash syntax", r.returncode == 0, f + (" " + r.stderr.strip() if r.returncode else ""))
    conf = E.load_conf()
    bad = [k for k in E.SETTINGS if not E.SETTINGS[k][2](conf[k])]
    check("settings valid", not bad, E.CONF + (" bad: %s" % bad if bad else ""))
    for u in (LOOK_UNIT, UNIT):
        r = sh("systemctl", "cat", u)
        check("unit installed", r.returncode == 0, u)
    check("look unit enabled", svc_enabled(LOOK_UNIT) == "enabled", LOOK_UNIT)
    # render the dashboard at many sizes / charsets: every row exactly as wide as the console
    sys.path.insert(0, "/usr/local/lib/eris")
    import erisd
    col = erisd.Collector(conf)
    deadline = time.time() + 20
    while time.time() < deadline and col.storage.snap is None:
        time.sleep(0.3)
    time.sleep(0.5)
    d = col.snapshot()
    col.stop()
    strip = re.compile(r"\x1b\[[0-9;]*m")
    worst = []
    for rows, cols in ((67, 240), (90, 320), (45, 160), (33, 120), (25, 80), (30, 100), (20, 60), (12, 40), (8, 30)):
        for cs in ("utf8", "safe", "ascii"):
            H = erisd.dash_height(rows, int(conf["DASH_HEIGHT"]))
            lines = erisd.render(d, cols, H, erisd.Theme(cs), 0)
            widths = {len(strip.sub("", ln)) for ln in lines}
            if len(lines) != H or widths - {cols}:
                worst.append("%dx%d/%s rows=%d widths=%s" % (cols, rows, cs, len(lines), sorted(widths)))
    check("dashboard layout (27 size/charset combinations)", not worst, "; ".join(worst[:3]))
    cpu = d["cpu"]
    temps = [p["temp"] for p in cpu["per"]] + [cpu["pkg"]]
    check("CPU temps sane", all(t is None or -30 < t < 150 for t in temps), str(temps))
    check("memory read", bool(d["mem"] and d["mem"]["total"] > 0))
    check("disks found", bool(d["storage"] and d["storage"]["phys"]),
          ", ".join(dk["name"] for dk in (d["storage"] or {}).get("phys", [])))
    tty = conf["CONSOLE_TTY"]
    if os.path.exists(tty):
        try:
            check("console font has a unicode map", len(E.tty_unicode_set(tty)) >= 200, tty)
        except OSError as e:
            check("console font has a unicode map", False, str(e))
    ok = all(results)
    print("  %s" % (c(GREEN, "all %d checks passed" % len(results)) if ok else c(RED, "%d of %d checks failed" % (results.count(False), len(results)))))
    return 0 if ok else 1


def main(argv):
    if not argv:
        if sys.stdin.isatty():
            return menu()
        return cmd_status()
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "on":
            return cmd_on()
        if cmd == "off":
            return cmd_off()
        if cmd == "toggle":
            return cmd_toggle()
        if cmd == "enable":
            return cmd_enable()
        if cmd == "disable":
            return cmd_disable()
        if cmd == "status":
            return cmd_status()
        if cmd in ("menu", "settings"):
            return menu()
        if cmd == "set" and len(args) >= 1:
            print(set_setting(args[0].upper(), " ".join(args[1:])))
            return 0
        if cmd == "get" and len(args) == 1:
            print(E.load_conf().get(args[0].upper(), ""))
            return 0
        if cmd == "config":
            return cmd_config()
        if cmd == "font":
            return cmd_font(args)
        if cmd == "apply":
            mode = "boot" if "--boot" in args else "login" if "--login" in args else "now"
            return apply_console(mode, quiet=(mode != "now"))
        if cmd == "reset-tty":
            return cmd_reset_tty(args)
        if cmd == "render":
            size = args[0] if args else None
            if not size:
                conf = E.load_conf()
                fd = E.open_tty(conf["CONSOLE_TTY"], write=False)
                r, cl, _, _ = E.console_geometry(fd, conf["CONSOLE_TTY"])
                os.close(fd)
                size = "%dx%d" % (r, cl)
            os.execv(sys.executable, [sys.executable, DAEMON, "--render", size] + args[1:2])
        if cmd == "selftest":
            return cmd_selftest()
        if cmd in ("version", "--version"):
            print(E.VERSION)
            return 0
    except ValueError as e:
        print("eris: %s" % e)
        return 2
    print(__doc__.strip())
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
ERIS_EOF_eris
  put_file /etc/eris/prompt.bash 0644 << 'ERIS_EOF_prompt_bash'
# ERIS Console prompt, sourced from ~/.bashrc (managed block). One line, no clock:
#   ERIS root@erisnas /path git:main ➜        exit code in red only after an error, e.g. ✘127
# Scope (PROMPT_SCOPE in /etc/eris/eris.conf): tty1 = physical console only, all = every interactive bash.
case $- in *i*) ;; *) return 0 ;; esac
[ -n "${BASH_VERSION-}" ] || return 0

__eris_scope=tty1
__eris_ctty=/dev/tty1
if [ -r /etc/eris/eris.conf ]; then
  while IFS='=' read -r __eris_k __eris_v; do
    __eris_v=${__eris_v%%#*}
    __eris_v=${__eris_v%"${__eris_v##*[![:space:]]}"}
    __eris_v=${__eris_v#\"}
    __eris_v=${__eris_v%\"}
    case $__eris_k in
      PROMPT_SCOPE) __eris_scope=$__eris_v ;;
      CONSOLE_TTY) __eris_ctty=$__eris_v ;;
    esac
  done < /etc/eris/eris.conf
  unset __eris_k __eris_v
fi
__eris_tty=$(tty 2>/dev/null)
if [ "$__eris_scope" != all ] && [ "$__eris_tty" != "$__eris_ctty" ]; then
  unset __eris_scope __eris_ctty __eris_tty
  return 0
fi

# Glyphs: the physical console only gets what its font can draw (ERIS writes /run/eris/prompt.env).
__eris_arrow='>'
__eris_err='x'
__eris_fsapps=''
__eris_envfile=''
if [ "$__eris_tty" = "$__eris_ctty" ] || [ "${TERM-}" = linux ]; then
  __eris_envfile=/run/eris/prompt.env
else
  case "${LC_ALL:-${LC_CTYPE:-${LANG-}}}" in
    *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) __eris_arrow='➜' __eris_err='✘' ;;
  esac
fi
if [ -r /run/eris/prompt.env ]; then
  while IFS='=' read -r __eris_k __eris_v; do
    __eris_v=${__eris_v#\'}
    __eris_v=${__eris_v%\'}
    case $__eris_k in
      ERIS_ARROW) [ -n "$__eris_envfile" ] && __eris_arrow=$__eris_v ;;
      ERIS_ERRMARK) [ -n "$__eris_envfile" ] && __eris_err=$__eris_v ;;
      ERIS_FULLSCREEN) __eris_fsapps=" $__eris_v " ;;
    esac
  done < /run/eris/prompt.env
  unset __eris_k __eris_v
fi
# Files of a dashboard running on this terminal (see erisd.py), used by the preexec hook below.
__eris_state=/run/eris/dashboard-${__eris_tty#/dev/}.state
__eris_handoff=/run/eris/handoff-${__eris_tty#/dev/}
unset __eris_scope __eris_ctty __eris_tty __eris_envfile

# Current git branch without running git (it may not be installed): read .git/HEAD upwards.
__eris_git() {
  __eris_br=
  local d=$PWD h='' g=''
  while [ -n "$d" ]; do
    if [ -f "$d/.git/HEAD" ]; then
      read -r h < "$d/.git/HEAD"
      break
    elif [ -f "$d/.git" ]; then
      read -r g < "$d/.git"
      g=${g#gitdir: }
      [ "${g#/}" = "$g" ] && g=$d/$g
      [ -f "$g/HEAD" ] && read -r h < "$g/HEAD"
      break
    fi
    [ "$d" = / ] && return 0
    d=${d%/*}
    [ -z "$d" ] && d=/
  done
  [ -n "$h" ] || return 0
  case $h in
    "ref: refs/heads/"*) h=${h#ref: refs/heads/} ;;
    *) h=${h:0:7} ;;
  esac
  h=${h//[^A-Za-z0-9._\/-]/_}   # nothing that PS1 could expand or that breaks the line
  __eris_br=${h:0:32}
}

__eris_prompt() {
  local ec=$? e='' g=''
  __eris_git
  [ -n "$__eris_br" ] && g=" \[\e[0;1;33m\]git:${__eris_br}"
  [ "$ec" -ne 0 ] && e=" \[\e[0;1;31m\]${__eris_err}${ec}"
  PS1="\[\e[0;1;35m\]ERIS \[\e[0;35m\]\u\[\e[0;1;30m\]@\[\e[0;1;34m\]\h \[\e[0;1;32m\]\w${g}${e} \[\e[0;1;36m\]${__eris_arrow}\[\e[0m\] "
  PS2="\[\e[0;1;30m\]${__eris_arrow}\[\e[0m\] "
  __eris_armed=1
  return "$ec"
}

# While the ERIS dashboard owns the top of this terminal, give a full-screen program (vim, htop, ...)
# the whole screen *before* it starts drawing: ncurses programs do not repaint on a same-size SIGWINCH.
# Only the first command of each line typed at the prompt is looked at; the dashboard comes back by itself.
__eris_preexec() {
  [ -n "${__eris_armed-}" ] || return 0
  __eris_armed=
  [ -n "${COMP_LINE-}" ] && return 0
  [ -e "$__eris_state" ] || return 0
  local w=${BASH_COMMAND%% *}
  case $w in
    sudo | env | nice | time | exec | command | nohup | ionice) w=${BASH_COMMAND#* } && w=${w%% *} ;;
  esac
  case "$__eris_fsapps" in
    *" ${w##*/} "*) ;;
    *) return 0 ;;
  esac
  : > "$__eris_handoff" 2> /dev/null
  printf '\033[?6l\033[r\033[0m\033[H\033[2J'
}

# Run first so $? is still the last command's status; drop the hook of the previous ERIS version.
if [ -z "$(trap -p DEBUG)" ]; then
  trap '__eris_preexec' DEBUG
fi
unset -f __eris_ps1 2>/dev/null
PROMPT_COMMAND=${PROMPT_COMMAND//__eris_ps1; /}
PROMPT_COMMAND=${PROMPT_COMMAND//__eris_ps1/}
case "${PROMPT_COMMAND-}" in
  *__eris_prompt*) ;;
  *) PROMPT_COMMAND="__eris_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac
ERIS_EOF_prompt_bash
  put_file /etc/systemd/system/eris-console.service 0644 << 'ERIS_EOF_eris_console_service'
[Unit]
Description=ERIS Console look on the physical console (font after KMS takeover, neon palette, blanking)
After=console-setup.service systemd-sysctl.service systemd-udevd.service
ConditionPathExists=/dev/tty1

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/eris apply --boot
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
ERIS_EOF_eris_console_service
  put_file /etc/systemd/system/eris-dashboard.service 0644 << 'ERIS_EOF_eris_dashboard_service'
[Unit]
Description=ERIS dashboard in the top part of the physical console (eris on / eris off)
After=eris-console.service getty@tty1.service systemd-user-sessions.service
ConditionPathExists=/dev/tty1

[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/bin/python3 /usr/local/lib/eris/erisd.py
ExecStopPost=/usr/local/bin/eris reset-tty --if-dirty
Restart=on-failure
RestartSec=3
TimeoutStartSec=40
TimeoutStopSec=8
Nice=10
IOSchedulingClass=idle
MemoryMax=128M

[Install]
WantedBy=multi-user.target
ERIS_EOF_eris_dashboard_service
  write_uninstaller /usr/local/sbin/eris-uninstall
}

[ "$(id -u)" -eq 0 ] || die "run as root"
SELFTEST=1
for a in "$@"; do
  case $a in
    --uninstall)
      if [ -x /usr/local/sbin/eris-uninstall ]; then exec /usr/local/sbin/eris-uninstall; fi
      tmp=$(mktemp)
      write_uninstaller "$tmp"
      bash "$tmp"; rc=$?; rm -f "$tmp"; exit $rc ;;
    --no-selftest) SELFTEST=0 ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
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
         /etc/systemd/system/eris-dashboard.service /etc/sysctl.d/90-eris-console.conf; do
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
say "files: /usr/local/bin/eris, /usr/local/lib/eris/, /etc/eris/prompt.bash, eris-console.service, eris-dashboard.service, /usr/local/sbin/eris-uninstall"

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
systemctl daemon-reload
systemctl enable eris-console.service > /dev/null 2>&1
systemctl restart eris-console.service || say "warning: eris-console.service failed (journalctl -u eris-console)"
if [ "$(sed -n 's/^DASH_AT_BOOT="\(.*\)"$/\1/p' /etc/eris/eris.conf)" = yes ]; then
  systemctl enable eris-dashboard.service > /dev/null 2>&1
else
  systemctl disable eris-dashboard.service > /dev/null 2>&1 || true
fi
[ "$was_on" = 1 ] && systemctl start eris-dashboard.service
say "look applied: $(tail -n 1 "$LOG" | sed 's/^.*\] //' | cut -c1-160)"

# 7. self-test
rc=0
if [ "$SELFTEST" = 1 ]; then
  /usr/local/bin/eris selftest || rc=$?
fi
say "done. Commands: eris (menu) | eris on | eris off | eris toggle | eris enable | eris disable | eris status"
say "remove: eris-uninstall   (or: bash $0 --uninstall)"
exit $rc
