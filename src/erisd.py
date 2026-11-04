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


def ls(path):
    """os.listdir, or [] when the folder is not there (containers, other kernels)."""
    try:
        return os.listdir(path)
    except OSError:
        return []


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
        for d in sorted(ls("/sys/devices/system/cpu")):
            if re.fullmatch(r"cpu\d+", d) and os.path.exists("/sys/devices/system/cpu/%s/topology" % d):
                t = "/sys/devices/system/cpu/%s/topology/" % d
                self.core_of[int(d[3:])] = rint(t + "core_id")
                cores.add((rint(t + "physical_package_id"), rint(t + "core_id")))
        self.threads = len(self.core_of) or os.cpu_count() or 1
        self.cores = len(cores) or self.threads
        for h in sorted(ls("/sys/class/hwmon")):
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
            for h in ls("/sys/class/hwmon"):
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
            for c in ls("/sys/class/drm"):
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
            for n in sorted(ls("/sys/class/net")):
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
        others = [n for n in sorted(ls("/sys/class/net")) if n not in ("lo", ifname)
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
        for h in ls("/sys/class/hwmon"):
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


class TempHistory:
    """CPU and disk temperatures once a minute for the last 24 hours, kept across restarts."""
    PATH = os.path.join(E.STATE_DIR, "temps.json")
    STEP, KEEP = 60, 1440

    def __init__(self):
        self.points = []          # [epoch, hottest CPU core, {disk: temp}]
        self.next = 0
        self.saved = time.time()
        try:
            with open(self.PATH) as f:
                cut = time.time() - self.STEP * self.KEEP
                self.points = [p for p in json.load(f) if isinstance(p, list) and len(p) == 3 and p[0] >= cut]
        except (OSError, ValueError, TypeError):
            self.points = []

    def add(self, cpu, disks):
        now = time.time()
        if now < self.next:
            return
        self.next = now + self.STEP
        self.points.append([int(now), cpu, {k: v for k, v in disks.items() if v is not None}])
        del self.points[:-self.KEEP]
        if now - self.saved >= 300:
            self.save()

    def save(self):
        self.saved = time.time()
        try:
            os.makedirs(E.STATE_DIR, exist_ok=True)
            tmp = self.PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.points, f, separators=(",", ":"))
            os.replace(tmp, self.PATH)
        except OSError:
            pass

    def series(self):
        disks = {}
        for _, _, dd in self.points:
            for k in dd:
                disks.setdefault(k, [])
        for _, _, dd in self.points:
            for k in disks:
                disks[k].append(dd.get(k))
        return {"cpu": [p[1] for p in self.points], "disks": disks,
                "span": (self.points[-1][0] - self.points[0][0]) if len(self.points) > 1 else 0}


def mz_env(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class MarzbanStat(Worker):
    """Users, today's traffic and nodes of the Marzban panel on this server (its admin from .env)."""
    ENV = "/opt/marzban/.env"

    def __init__(self, conf):
        Worker.__init__(self, "marzban")
        self.conf = conf
        self.token = None
        import ssl
        import urllib.request
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))

    def req(self, base, path, form=None):
        import urllib.parse
        import urllib.request
        headers = {"Accept": "application/json"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.token and form is None:
            headers["Authorization"] = "Bearer " + self.token
        with self.opener.open(urllib.request.Request(base + path, data=body, headers=headers), timeout=15) as r:
            return json.loads(r.read() or b"null")

    def step(self):
        if self.conf.get("MARZBAN") == "off" or not os.path.isfile(self.ENV):
            self.snap = None
            return 60
        import urllib.error
        e = mz_env(self.ENV)
        base = "%s://127.0.0.1:%s" % ("https" if e.get("UVICORN_SSL_CERTFILE") else "http", e.get("UVICORN_PORT", "8000"))
        try:
            for attempt in (1, 2):
                if not self.token:
                    self.token = self.req(base, "/api/admin/token", form={
                        "username": e.get("SUDO_USERNAME", ""), "password": e.get("SUDO_PASSWORD", "")})["access_token"]
                try:
                    sysinfo = self.req(base, "/api/system")
                    break
                except urllib.error.HTTPError as ex:
                    if ex.code != 401 or attempt == 2:
                        raise
                    self.token = None
            nodes = self.req(base, "/api/nodes") or []
            core = self.req(base, "/api/core") or {}
            # Marzban's own online_users counts the last 24 hours; "connected now" = traffic in the last
            # 2 minutes (online_at is set whenever a user's traffic is recorded, every few seconds)
            now_names = []
            for u in (self.req(base, "/api/users") or {}).get("users", []):
                t = iso_epoch((u.get("online_at") or "") + "Z") if u.get("online_at") else None
                if t is not None and time.time() - t <= 120:
                    now_names.append(u.get("username") or "?")
            # today since local midnight; Marzban keeps UTC
            midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(midnight))
            usage = self.req(base, "/api/nodes/usage?start=" + start) or {}
            up = sum(u.get("uplink") or 0 for u in usage.get("usages", []))
            down = sum(u.get("downlink") or 0 for u in usage.get("usages", []))
            self.snap = {"ok": True, "version": sysinfo.get("version"), "xray": core.get("version"),
                         "core_on": core.get("started"), "total": sysinfo.get("total_user", 0),
                         "active": sysinfo.get("users_active", 0), "online": sysinfo.get("online_users", 0),
                         "now": sorted(now_names),
                         "limited": sysinfo.get("users_limited", 0), "expired": sysinfo.get("users_expired", 0),
                         "rx_rate": sysinfo.get("incoming_bandwidth_speed"), "tx_rate": sysinfo.get("outgoing_bandwidth_speed"),
                         "today_up": up, "today_down": down,
                         "nodes": [{"name": n.get("name"), "status": n.get("status"), "xray": n.get("xray_version"),
                                    "local": n.get("address") in ("127.0.0.1", "localhost", "::1"),
                                    "message": n.get("message")} for n in nodes],
                         "at": time.time()}
        except Exception as ex:  # the panel restarting, a changed password, ...
            self.token = None
            msg = "HTTP %s" % ex.code if hasattr(ex, "code") else type(ex).__name__
            self.snap = {"ok": False, "error": "no answer from %s (%s)" % (base, msg), "at": time.time(),
                         "nodes": (self.snap or {}).get("nodes", [])}
        return 30


def read_backup_state():
    try:
        with open(os.path.join(E.STATE_DIR, "backup-state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def alerts(d, conf):
    """[(level, text)]: 2 = something is wrong now, 1 = look at it soon."""
    out = []
    lim_c, lim_d = int(conf.get("ALERT_CPU_TEMP", "80")), int(conf.get("ALERT_DISK_TEMP", "55"))
    c = d.get("cpu") or {}
    if c.get("hottest") is not None and c["hottest"] >= lim_c:
        out.append((2, "CPU %.0fC" % c["hottest"]))
    if c.get("throttle") == "THROTTLING":
        out.append((1, "CPU throttling"))
    for dk in (d.get("storage") or {}).get("phys", []):
        t = dk.get("temp")
        if t is not None and t >= lim_d:
            out.append((2, "%s %.0fC" % (dk["name"], t)))
        if dk.get("health") == "FAILED":
            out.append((2, "%s SMART FAILED" % dk["name"]))
        elif dk.get("note") == "SMART err":
            out.append((1, "%s SMART unreadable" % dk["name"]))
        for p in dk.get("parts", []):
            mnt = next((m for m in p.get("mounts") or [] if m.startswith("/")), None)
            if mnt and p.get("pct") is not None and p["pct"] >= 90:
                out.append((2 if p["pct"] >= 95 else 1, "%s %d%% full" % (mnt, p["pct"])))
    for s in d.get("svc") or []:
        if s.get("state") == "failed":
            out.append((2, "%s failed" % s["name"]))
    m = d.get("mem") or {}
    if (m.get("pct") or 0) >= 95 and (m.get("swap_pct") or 0) >= 80:
        out.append((1, "memory full"))
    mz = d.get("mz")
    if mz:
        if not mz.get("ok"):
            out.append((1, "Marzban not answering"))
        for n in mz.get("nodes", []):
            if n.get("status") not in ("connected", "disabled", "connecting"):
                out.append((2, "node %s %s" % (n.get("name"), n.get("status"))))
    if conf.get("BACKUP_DIR"):
        b = d.get("backup") or {}
        parts = {k: v for k, v in (b.get("parts") or {}).items()
                 if isinstance(v, dict) and k in E.parts_setting(conf.get("BACKUP_PARTS", "all"))}
        failed = False
        if conf.get("BACKUP_FULL", "on") == "on":
            last = b.get("last_ok_epoch")
            failed = b.get("last_error_epoch", 0) > (last or 0)
            if failed:
                out.append((2, "backup failed"))
        else:   # only parts: the age of the newest part backup
            last = max([p.get("last_ok_epoch") or 0 for p in parts.values()] or [0]) or None
        if failed:
            pass
        elif not last:
            out.append((1, "no backup yet"))
        elif time.time() - last > 36 * 3600:
            out.append((1, "backup %dd old" % ((time.time() - last) // 86400 or 1)))
        for name, p in sorted(parts.items()):
            if p.get("last_error_epoch", 0) > (p.get("last_ok_epoch") or 0):
                out.append((2, "backup %s failed" % name))
        if not os.path.isdir(conf["BACKUP_DIR"]):
            out.append((2, "backup folder missing"))
    return sorted(out, key=lambda x: -x[0])


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
    "alert": "\x1b[0;1;37;41m",     # white on red: the alert bar's label
    "warn": "\x1b[0;30;43m",        # black on amber: the label when only warnings are left
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


def p_temps(d, wi, th, hi=None):
    """Now, 24 h min/max and (tall panels) a chart for the CPU and every disk with a sensor."""
    th_ = d.get("temps") or {}
    now_cpu = (d.get("cpu") or {}).get("hottest")
    now_disk = {dk["name"]: dk.get("temp") for dk in (d.get("storage") or {}).get("phys", [])}
    hist_disks = dict(th_.get("disks") or {})
    for k, v in now_disk.items():
        if v is not None:
            hist_disks.setdefault(k, [])            # a disk with a sensor shows before its history starts
    series = [("CPU", th_.get("cpu") or [], 60, 80, "pink")]
    series += [(k, v, 45, 55, "cyan") for k, v in sorted(hist_disks.items())]
    L = []
    nw = max(len(s[0]) for s in series) + 1
    for name, vals, warn, crit, col in series:
        v = [x for x in vals if x is not None]
        now = now_cpu if name == "CPU" else now_disk.get(name)
        if now is None and v:
            now = v[-1]
        ln = Ln(name.ljust(nw), col).add(ftemp(now, th), tcol(now, warn, crit))
        if v:
            ln.gap().add("min ", "grey").add(ftemp(min(v), th), "text").gap().add("max ", "grey")
            ln.add(ftemp(max(v), th), tcol(max(v), warn, crit))
        L.append(ln)
    span = th_.get("span") or 0
    L.append(Ln("history", "grey").gap().add(fmt_dur(span) if span else "starts now", "text")
             .gap().add("1 min/col", "grey").section())
    spare = (hi or 0) - len(L)
    charts = [s for s in series if len([x for x in s[1] if x is not None]) >= 2]
    if spare >= 4 and charts:
        per = max(3, spare // len(charts))
        for name, vals, warn, crit, col in charts:
            if spare < 4:
                break
            h = min(per, spare) - 1
            lo, top = 20.0, max(crit + 10.0, max(x for x in vals if x is not None) + 5.0)
            shifted = [None if x is None else max(0.0, x - lo) for x in vals]
            L.append(Ln(name + " ", col).add("%s..%s" % (ftemp(lo, th), ftemp(top, th)), "grey").section())
            L += chart(shifted, wi, h, th, top - lo)
            spare -= h + 1
    return L


def p_marzban(d, wi, th):
    m = d.get("mz")
    g = th.g
    if not m:
        return [Ln("checking...", "grey")]
    if not m.get("ok"):
        L = [Ln("N/A", "red"), Ln(th.clean(m.get("error", "")), "grey")]
    else:
        now = m.get("now") or []
        conn = Ln("connected now ", "grey").add(str(len(now)), "green" if now else "text")
        if now:
            conn.add("  " + ", ".join(th.clean(n) for n in now), "white")
        L = [conn,
             Ln("Xray ", "grey").add(m.get("xray") or "?", "white").gap().add("panel ", "grey")
             .add(th.clean(m.get("version") or "?"), "text").section(),
             Ln("users ", "grey").add(str(m["active"]), "green").add("/%d active" % m["total"], "text").gap()
             .add("24h ", "grey").add(str(m["online"]), "white" if m["online"] else "text"),
             Ln("today ", "grey").add(g["down"] + E.human(m["today_down"]), "green").add("  ")
             .add(g["up"] + E.human(m["today_up"]), "pink").section()]
        if m.get("rx_rate") is not None:
            L.append(Ln("now   ", "grey").add(g["down"] + rate(m["rx_rate"]), "text").add("  ")
                     .add(g["up"] + rate(m["tx_rate"]), "text"))
        if m.get("expired") or m.get("limited"):
            L.append(Ln("expired ", "grey").add(str(m.get("expired", 0)), "amber").gap()
                     .add("limited ", "grey").add(str(m.get("limited", 0)), "amber"))
    nodes = m.get("nodes") or []
    if nodes:
        nw = max(len(n["name"] or "") for n in nodes)
        for i, n in enumerate(nodes):
            st = n.get("status") or "?"
            col = "green" if st == "connected" else "amber" if st in ("connecting", "disabled") else "red"
            ln = Ln(g["dot"] + " ", col).add(th.clean(n["name"] or "?").ljust(nw), "text").gap().add(st, col)
            if n.get("xray"):
                ln.gap().add(n["xray"], "grey")
            L.append(ln.section() if i == 0 else ln)
    elif m.get("ok"):
        L.append(Ln("no nodes", "grey").section())
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
    "temps": ("TEMPERATURES", "amber", 28, 2.4, p_temps),
    "marzban": ("MARZBAN", "purple", 30, 2.6, p_marzban),
}
PRIO = ["cpu", "usage", "mem", "storage", "marzban", "net", "temps", "gpu", "weather", "sys", "svc"]
CHARTED = ("cpu", "mem", "net", "temps")
GROUPS = [["cpu", "svc"], ["usage", "mem"], ["storage", "temps"], ["marzban", "net", "gpu"], ["weather"], ["sys"]]
FLEX = ("gpu", "weather", "sys", "svc", "net", "mem", "temps", "marzban")


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


def alert_bar(al, W, th, tick):
    """One row: ' ALERT 3 ' label, then the messages (red = now, amber = soon); long lists rotate."""
    red = any(lv == 2 for lv, _ in al)
    head = Ln(" %s %d " % ("ALERT" if red else "WARNING", len(al)), "alert" if red else "warn").add(" ")
    sep = " %s " % th.g["sep"]
    start = (tick // 3) % len(al)
    order = al[start:] + al[:start] if sum(len(t) + len(sep) for _, t in al) > W - head.n else al
    out = Ln().cat(head)
    for i, (lv, text) in enumerate(order):
        if i:
            out.add(sep, "grey")
        out.add(th.clean(text), "red" if lv == 2 else "amber")
    return out.fit(W, th.g["ell"], spread=False)


def dash_height(rows, pct, full=False):
    """Dashboard rows; full screen leaves the shell its last 2 rows (a scroll region needs 2)."""
    if rows < 10:
        return 1 if rows >= 4 else 0
    if full:
        return rows - 2
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
    al = d.get("alerts") or []
    if al and Hp >= 5:
        rows.append(alert_bar(al, W, th, tick))
        Hp -= 1
    if Hp >= 3:
        enabled = [p for p in PRIO if not (p == "weather" and d.get("weather_off")) and not (p == "gpu" and not d["gpu"])
                   and not (p == "marzban" and not d.get("mz"))]
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
        self.marzban = MarzbanStat(conf)
        for t in (self.storage, self.services, self.weather, self.usage, self.marzban):
            t.start()
        self.temps = TempHistory()
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
        for t in (self.storage, self.services, self.weather, self.usage, self.marzban):
            t.stop()
        self.temps.save()

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
        if cpu.get("hottest") is not None or st:
            self.temps.add(cpu.get("hottest"), {dk["name"]: dk.get("temp") for dk in (st or {}).get("phys", [])})
        d = {
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
            "temps": self.temps.series(),
            "mz": self.marzban.snap,
            "backup": read_backup_state(),
        }
        d["alerts"] = safe(lambda: alerts(d, self.conf), []) if self.conf.get("ALERTS", "on") == "on" else []
        return d


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
        return dash_height(rows, int(self.conf["DASH_HEIGHT"]), self.conf.get("DASH_MODE") == "full")

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
                                      or (conf["WEATHER"] == "on" and col.weather.snap is None)
                                      or (conf["MARZBAN"] != "off" and os.path.isfile(MarzbanStat.ENV)
                                          and col.marzban.snap is None)):
        time.sleep(0.5)
    d = col.snapshot()
    col.stop()
    th = Theme("utf8", extras=True) if charset == "eris" else Theme(charset)
    H = dash_height(rows, int(conf["DASH_HEIGHT"]), conf.get("DASH_MODE") == "full")
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
