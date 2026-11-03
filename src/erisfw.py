"""ERIS firewall: a short readable settings file turned into the nftables table inet eris_guard.

  /etc/eris/firewall.conf   the settings: eris fw ..., or edit the file and run eris fw reload
  /etc/eris/guard.nft       made from them: the confirmed rules, loaded at boot by eris-guard.service
  /run/eris/fw-pending/     a change that is live but not confirmed yet; if nobody confirms it, the
                            eris-fw-rollback unit (sleep, then /usr/local/lib/eris/fw-rollback) loads guard.nft again

Only the table inet eris_guard is ever changed: Docker's iptables-nft tables are not touched.
Localhost and SSH from the LAN are always allowed, whatever the settings say.
Used by /usr/local/bin/eris. Python 3 standard library only.
"""
import difflib
import fcntl
import glob
import ipaddress
import os
import re
import select
import shutil
import subprocess
import sys
import time

import erislib as E

FW_CONF = "/etc/eris/firewall.conf"
GUARD = "/etc/eris/guard.nft"
APPLIED = os.path.join(E.STATE_DIR, "firewall.applied")
PENDING = os.path.join(E.RUN_DIR, "fw-pending")
LOCK = os.path.join(E.RUN_DIR, "fw.lock")
LOADED = os.path.join(E.RUN_DIR, "fw-loaded")
LAST_ROLLBACK = os.path.join(E.RUN_DIR, "fw-last-rollback")
AUTO_ON_AT = os.path.join(E.RUN_DIR, "fw-auto-on")
ROLLBACK = "/usr/local/lib/eris/fw-rollback"
XUI_DB = "/etc/x-ui/x-ui.db"
NFT = "/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else (shutil.which("nft") or "nft")
SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
UNIT = "eris-guard.service"
ROLLBACK_UNIT = "eris-fw-rollback"
AUTO_ON_UNIT = "eris-fw-auto-on"
CONFIRM_SECONDS = 60
DEFAULT_LIMIT = 300
KEEP_AUTO_BACKUPS = 30
FLAGS = {"force": False, "open_ssh": False}
TTYOUT = sys.stdout.isatty()

PINK, GREEN, AMBER, RED, CYAN, GREY, WHITE = "1;35", "1;32", "33", "1;31", "1;36", "1;30", "1;37"

HELP = """eris fw - the ERIS firewall (nftables table inet eris_guard, settings in /etc/eris/firewall.conf)

  eris fw                            status: open ports, LAN networks, custom rules (numbered), drops
  eris fw open PORT[/udp] [limit N]  open a port to the Internet (default: at most 300 parallel connections)
  eris fw close PORT[/udp]           close it again
  eris fw allow ADDR[/PREFIX]        full access for an address or network
  eris fw deny ADDR[/PREFIX]         block an address or network (SSH from the LAN still works)
  eris fw remove N                   remove entry N of the status list
  eris fw limit PORT[/udp] N         parallel connection limit of an open port (0 = no limit)
  eris fw set limit-per total|address | docker lan-only|open | ping on|off
  eris fw reload                     load /etc/eris/firewall.conf after editing it
  eris fw confirm                    keep a change (without it, it is rolled back after 60 s)
  eris fw off [MINUTES] | on         switch the firewall off (for a while) / on again
  eris fw backup | restore [N]       save the settings now / bring back a saved version
  eris fw setup | import             first set-up on a server / take over an existing guard.nft
  eris fw nft                        the live nftables table

Every change is checked with nft -c, backed up in /var/backups/eris/fw-*, and rolled back after
60 s unless it is confirmed (Enter, or eris fw confirm). Localhost and SSH from the LAN always work.
Opening SSH to the Internet needs --open-ssh (or typing it in a terminal); other risky changes --force."""


def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if TTYOUT else s


def sh(*cmd, timeout=60, stdin=None):
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".eris-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def interactive():
    return sys.stdin.isatty()


def choose(prompt, options, cur):
    print("   " + "  ".join("%s %s" % (c(CYAN, "%d)" % (i + 1)), o + (" *" if o == cur else "")) for i, o in enumerate(options)))
    a = ask("   %s [1-%d]: " % (prompt, len(options)))
    if a and a.isdigit() and 1 <= int(a) <= len(options):
        return options[int(a) - 1]
    return None


def clock(ts):
    return time.strftime("%H:%M" if time.strftime("%F", time.localtime(ts)) == time.strftime("%F") else "%b %d %H:%M",
                         time.localtime(ts))


class Lock:
    """The lock file the rollback script also takes (flock): a confirmation and a rollback never overlap."""

    def __enter__(self):
        os.makedirs(E.RUN_DIR, exist_ok=True)
        self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


# ---------------------------------------------------------------- settings file

# option: (values, first = default)
OPTIONS = {
    "limit-per": ("total", "address"),
    "docker": ("lan-only", "open"),
    "ping": ("on", "off"),
}
GROUP = {"lan": 0, "open": 1, "allow": 2, "deny": 2, "option": 3}
SECTIONS = ("# Networks with full access", "# Open to the Internet", "# Custom rules", "# Options")

CONF_HEAD = """\
# ERIS firewall: the nftables table inet eris_guard, loaded at boot by eris-guard.service.
# Change it with `eris fw ...` (or the Firewall item of the `eris` menu), or edit this file and
# run `eris fw reload`. New rules are checked with nft -c, backed up in /var/backups/eris/fw-*,
# and rolled back after 60 seconds unless you confirm them (Enter, or: eris fw confirm).
# Always allowed, whatever this file says: localhost, SSH from the LAN, and replies to
# connections this server opens itself. Docker's own iptables tables are never touched.
#
#   lan ADDR[/PREFIX]                full access: LAN and Docker networks (IPv4 or IPv6)
#   open PORT[/tcp|/udp] [limit N]   reachable from the Internet, at most N parallel connections
#   allow ADDR[/PREFIX]              full access for one Internet address or network
#   deny ADDR[/PREFIX]               drop everything from it (SSH from the LAN still works)
#   limit-per total|address          total: N is shared by all addresses; address: N for each one
#   docker lan-only|open             Docker-published ports only from the lan networks, or unfiltered
#   ping on|off                      answer IPv4 ping (at most 5 per second)
"""


def parse_net(s):
    try:
        return ipaddress.ip_network(s, strict=False)
    except ValueError:
        raise ValueError("not an IP address or network: %s" % s)


def fmt_net(n):
    return str(n.network_address) if n.prefixlen == n.max_prefixlen else str(n)


def parse_port(s):
    m = re.fullmatch(r"(\d{1,5})(?:/(tcp|udp))?", s.lower())
    if not m or not 1 <= int(m.group(1)) <= 65535:
        raise ValueError("not a port (1-65535, optionally /tcp or /udp): %s" % s)
    return int(m.group(1)), m.group(2)


def parse_limit(s):
    if not s.isdigit() or int(s) > 1000000:
        raise ValueError("the limit is a number of parallel connections, 0 = no limit: %s" % s)
    return int(s) or None


def open_line(port, proto, limit):
    return "open %d/%s%s" % (port, proto, " limit %d" % limit if limit else "")


def parse(text):
    """-> (cfg, errors); cfg = {"lines": [...], "items": [...], "opts": {...}}; items know their line."""
    lines = (text or "").splitlines()
    items, errors = [], []
    opts = {k: v[0] for k, v in OPTIONS.items()}
    for i, raw in enumerate(lines):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        w = body.split()
        key = w[0].lower()
        try:
            if key in ("lan", "allow", "deny"):
                if len(w) != 2:
                    raise ValueError("expected: %s ADDR[/PREFIX]" % key)
                items.append({"kind": key, "net": parse_net(w[1]), "line": i})
            elif key == "open":
                if len(w) not in (2, 4) or (len(w) == 4 and w[2].lower() != "limit"):
                    raise ValueError("expected: open PORT[/tcp|/udp] [limit N]")
                port, proto = parse_port(w[1])
                items.append({"kind": "open", "port": port, "proto": proto or "tcp",
                              "limit": parse_limit(w[3]) if len(w) == 4 else None, "line": i})
            elif key in OPTIONS:
                if len(w) != 2 or w[1].lower() not in OPTIONS[key]:
                    raise ValueError("expected: %s %s" % (key, "|".join(OPTIONS[key])))
                opts[key] = w[1].lower()
                items.append({"kind": "option", "name": key, "value": opts[key], "line": i})
            else:
                raise ValueError("unknown word %s (lan, open, allow, deny, %s)" % (w[0], ", ".join(OPTIONS)))
        except ValueError as e:
            errors.append("line %d: %s   <- %s" % (i + 1, body, e))
    seen = {}
    for it in items:
        it["group"] = GROUP[it["kind"]]
        if it["kind"] == "option":
            key = ("option", it["name"])
        elif it["kind"] == "open":
            key = ("open", it["port"], it["proto"])
        else:
            key = ("net", it["net"])
        if key in seen:
            errors.append("line %d: %s is already on line %d" % (it["line"] + 1, lines[it["line"]].split("#")[0].strip(),
                                                                 seen[key] + 1))
        else:
            seen[key] = it["line"]
    return {"lines": lines, "items": items, "opts": opts}, errors


def load(text):
    cfg, errors = parse(text)
    if errors:
        raise ValueError("the settings have errors, nothing changed:\n  " + "\n  ".join(errors))
    return cfg


def entries(cfg, *kinds):
    return [it for it in cfg["items"] if it["kind"] in kinds]


def nets(cfg, kind):
    return [it["net"] for it in entries(cfg, kind)]


def numbered(cfg):
    """The order of the status list (eris fw remove N): open ports, LAN networks, custom rules."""
    return entries(cfg, "open") + entries(cfg, "lan") + entries(cfg, "allow", "deny")


def conf_text(lan, opens, custom=(), opts=None):
    o = {k: v[0] for k, v in OPTIONS.items()}
    o.update(opts or {})
    out = [CONF_HEAD.rstrip("\n"), "", SECTIONS[0] + " (LAN, Docker): SSH from these always works"]
    out += ["lan %s" % fmt_net(n) for n in lan]
    out += ["", SECTIONS[1] + " (limit = parallel connections, see limit-per)"]
    out += [open_line(p, pr, lim) for p, pr, lim in opens]
    out += ["", SECTIONS[2] + ": allow = full access, deny = blocked"]
    out += ["%s %s" % (k, fmt_net(n)) for k, n in custom]
    out += ["", SECTIONS[3]]
    out += ["%s %s" % (k, o[k]) for k in OPTIONS]
    return "\n".join(out) + "\n"


def text_of(cfg):
    return "\n".join(cfg["lines"]) + "\n"


def insert_line(cfg, kind, text):
    """After the last line of the same kind, else under its section comment, else after the kinds before it."""
    group, lines = GROUP[kind], cfg["lines"]
    same = [it["line"] for it in cfg["items"] if it["group"] == group]
    marks = [i for i, line in enumerate(lines) if line.strip().lower().startswith(SECTIONS[group].lower())]
    lower = [it["line"] for it in cfg["items"] if it["group"] < group]
    pos = max(same) + 1 if same else marks[0] + 1 if marks else max(lower) + 1 if lower else len(lines)
    lines.insert(pos, text)


def replace_line(cfg, idx, text):
    raw = cfg["lines"][idx]
    cfg["lines"][idx] = text + ("   " + raw[raw.index("#"):] if "#" in raw else "")


def delete_lines(cfg, idxs):
    for i in sorted(set(idxs), reverse=True):
        del cfg["lines"][i]


# ---------------------------------------------------------------- this host

PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                                             "fc00::/7", "fe80::/10")]
LOCAL_ONLY = {("udp", 68), ("udp", 5353), ("udp", 5355), ("udp", 1900), ("udp", 3702), ("tcp", 5355), ("tcp", 5357)}
RISKY_PORTS = {
    2053: "the default x-ui panel port", 54321: "the old default x-ui panel port", 8000: "the default Marzban panel port",
    9090: "Cockpit (server admin)", 10000: "Webmin (server admin)", 9000: "Portainer (Docker admin)",
    9443: "Portainer (Docker admin)", 62789: "the Xray API", 111: "rpcbind (NFS)", 139: "NetBIOS (Samba)",
    445: "SMB file sharing (Samba)", 2049: "NFS file sharing", 3306: "MySQL / MariaDB", 5432: "PostgreSQL",
    6379: "Redis", 27017: "MongoDB", 5900: "VNC", 3389: "remote desktop (RDP)",
}
RISKY_PROCS = {
    "smbd": "Samba file sharing", "nmbd": "NetBIOS (Samba)", "rpcbind": "rpcbind (NFS)", "rpc.mountd": "NFS",
    "cockpit-ws": "Cockpit (server admin)", "miniserv.pl": "Webmin (server admin)", "portainer": "Portainer",
    "mysqld": "MySQL", "mariadbd": "MariaDB", "postgres": "PostgreSQL", "redis-server": "Redis", "mongod": "MongoDB",
}


def within(n, pool):
    return any(n.version == p.version and n.subnet_of(p) for p in pool)


def addr_in(a, pool):
    return any(a.version == p.version and a in p for p in pool)


def private(n):
    return within(n, PRIVATE)


def collapse(ns, version):
    return list(ipaddress.collapse_addresses([n for n in ns if n.version == version]))


def to_addr(host):
    a = ipaddress.ip_address(host.strip("[]").split("%")[0])
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def listeners():
    """{(proto, port): {"names": {process, ...}, "public": listens on more than localhost}}"""
    out = {}
    for line in sh("ss", "-Hlntup").stdout.splitlines():
        f = line.split(None, 6)
        if len(f) < 6 or f[0] not in ("tcp", "udp"):
            continue
        host, _, port = f[4].rpartition(":")
        if not port.isdigit():
            continue
        host = host.strip("[]").split("%")[0]
        e = out.setdefault((f[0], int(port)), {"names": set(), "public": False})
        e["names"] |= set(re.findall(r'\("([^"]+)"', f[6] if len(f) > 6 else ""))
        if not (host.startswith("127.") or host == "::1"):
            e["public"] = True
    return out


def host_env():
    lis = listeners()
    ssh = {22} | {port for (proto, port), e in lis.items() if proto == "tcp" and "sshd" in e["names"]}
    connected = []
    for line in sh("ip", "-o", "addr", "show").stdout.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+(inet6?)\s+([0-9a-fA-F.:]+/\d+)", line)
        if m and m.group(1) != "lo":
            try:
                connected.append((m.group(1), ipaddress.ip_interface(m.group(3)).network))
            except ValueError:
                pass
    return {"ssh_ports": sorted(ssh), "connected": connected, "listeners": lis}


def ssh_clients(env):
    """(address, local port) of the SSH sessions open right now."""
    out = set()
    for line in sh("ss", "-Htn", "state", "established").stdout.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        lp = f[2].rpartition(":")[2]
        if lp.isdigit() and int(lp) in env["ssh_ports"]:
            try:
                out.add((to_addr(f[3].rpartition(":")[0]), int(lp)))
            except ValueError:
                pass
    sc = os.environ.get("SSH_CONNECTION", "").split()
    if len(sc) == 4 and sc[3].isdigit():
        try:
            out.add((to_addr(sc[0]), int(sc[3])))
        except ValueError:
            pass
    return sorted(out, key=str)


def connections():
    """{local TCP port: (established connections, distinct addresses)}"""
    per = {}
    for line in sh("ss", "-Htn", "state", "established").stdout.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        lp = f[2].rpartition(":")[2]
        if lp.isdigit():
            e = per.setdefault(int(lp), [0, set()])
            e[0] += 1
            e[1].add(f[3].rpartition(":")[0])
    return {p: (n, len(a)) for p, (n, a) in per.items()}


def xui_ports():
    """{"web": panel port, "sub": subscription port} of an installed x-ui, else {}."""
    if not os.path.exists(XUI_DB):
        return {}
    try:
        import sqlite3
    except ImportError:
        return {}
    try:
        db = sqlite3.connect("file:%s?mode=ro" % XUI_DB, uri=True, timeout=2)
        try:
            rows = dict(db.execute("select key, value from settings where key in ('webPort', 'subPort')"))
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return {k: int(rows[v]) for k, v in (("web", "webPort"), ("sub", "subPort")) if str(rows.get(v, "")).isdigit()}


def omv_ports():
    ports = set()
    for f in glob.glob("/etc/nginx/sites-enabled/*openmediavault*"):
        ports |= {int(p) for p in re.findall(r"^\s*listen\s+(?:\S*:)?(\d+)", read(f, "") or "", re.M)}
    return ports


def port_notes(port, proto, env, xui):
    """[(level, text)] for opening port/proto: level ssh | risky (needs an explicit yes) | warn."""
    out = []
    l = env["listeners"].get((proto, port))
    names = sorted(l["names"]) if l else []
    if proto == "tcp" and port in env["ssh_ports"]:
        out.append(("ssh", "%d/tcp is SSH: anyone on the Internet could try to log in" % port))
    elif proto == "tcp" and xui.get("web") == port:
        out.append(("risky", "%d/tcp is the x-ui management panel" % port))
    elif proto == "tcp" and port in omv_ports():
        out.append(("risky", "%d/tcp is the openmediavault admin web interface" % port))
    else:
        bad = [RISKY_PROCS[n] for n in names if n in RISKY_PROCS]
        if bad:
            out.append(("risky", "%d/%s is %s" % (port, proto, bad[0])))
        elif port in RISKY_PORTS and xui.get("sub") != port:
            out.append(("risky", "%d is usually %s" % (port, RISKY_PORTS[port])))
    if not l:
        out.append(("warn", "nothing listens on %d/%s on this server right now" % (port, proto)))
    elif not l["public"]:
        out.append(("warn", "%d/%s listens only on localhost (%s): opening it changes nothing" % (port, proto, ", ".join(names))))
    if "docker-proxy" in names:
        out.append(("warn", "%d/%s is a Docker-published port: IPv4 from the Internet goes through the forward chain, "
                            "which 'docker lan-only' still keeps closed" % (port, proto)))
    return out


def service_name(port, proto, env, xui):
    if proto == "tcp" and xui.get("sub") == port:
        return "x-ui subscription", None
    if proto == "tcp" and xui.get("web") == port:
        return "x-ui PANEL", RED
    l = env["listeners"].get((proto, port))
    if not l:
        return "nothing listens", AMBER
    names = sorted(l["names"])
    if "docker-proxy" in names:
        return "Docker", None
    return ", ".join(names) or "?", None


# ---------------------------------------------------------------- rules

def fam(n):
    return "ip" if n.version == 4 else "ip6"


def fmt_set(vals):
    vals = [str(v) for v in vals]
    return vals[0] if len(vals) == 1 else "{ %s }" % ", ".join(vals)


def covered(ns, pool):
    return all(within(n, pool) for n in ns)


def ssh_safe(cfg, env):
    """Where SSH must always work from: the lan networks and the private networks this host is on."""
    pool = nets(cfg, "lan") + [n for _, n in env["connected"] if private(n)]
    return collapse(pool, 4), collapse(pool, 6)


def public_rules(opens, per):
    """Sets and input rules for the open ports; the largest group with one limit uses the set public_<proto>."""
    sets, rules = [], []
    for proto in ("tcp", "udp"):
        groups = {}
        for o in opens:
            if o["proto"] == proto:
                groups.setdefault(o["limit"], []).append(o["port"])
        if not groups:
            continue
        main = max(groups, key=lambda k: len(groups[k]))
        for limit, ports in groups.items():
            if limit == main:
                sets.append("set public_%s { type inet_service; elements = { %s } }" % (proto, ", ".join(map(str, ports))))
                match = "%s dport @public_%s" % (proto, proto)
            else:
                match = "%s dport %s" % (proto, fmt_set(ports))
            if limit and per == "address":
                for v, key, typ in ((4, "ip saddr", "ipv4_addr"), (6, "ip6 saddr", "ipv6_addr")):
                    name = "each_%s%d_v%d" % (proto, limit, v)
                    sets.append("set %s { type %s; size 65535; flags dynamic; }" % (name, typ))
                    rules.append("%s ct state new add @%s { %s ct count over %d } drop" % (match, name, key, limit))
            elif limit:
                rules.append("%s ct state new ct count over %d drop" % (match, limit))
            rules.append("%s accept" % match)
    return sets, rules


def generate(cfg, env):
    """The nft script for table inet eris_guard; nft -f swaps the whole table in one step."""
    o = cfg["opts"]
    lan = nets(cfg, "lan")
    lan4, lan6 = collapse(lan, 4), collapse(lan, 6)
    allow, deny = nets(cfg, "allow"), nets(cfg, "deny")
    safe4, safe6 = ssh_safe(cfg, env)
    pub_sets, pub_rules = public_rules(entries(cfg, "open"), o["limit-per"])
    sets = ["set lan4 { type ipv4_addr; flags interval; %s}" % (
        "elements = { %s } " % ", ".join(map(fmt_net, lan4)) if lan4 else "")] + pub_sets

    inp = ["type filter hook input priority filter - 5; policy drop;", 'iif "lo" accept']
    # the lan accept below already covers SSH, unless something is denied or a LAN network is missing there
    if deny or not covered(safe4, lan4) or not covered(safe6, lan6):
        inp.append("# SSH from the LAN always works, even from a denied address")
        ssh = fmt_set(env["ssh_ports"])
        if safe4:
            inp.append("tcp dport %s ip saddr %s accept" % (ssh, fmt_set(map(fmt_net, safe4))))
        if safe6:
            inp.append("tcp dport %s ip6 saddr %s accept" % (ssh, fmt_set(map(fmt_net, safe6))))
    for n in deny:
        inp.append('%s saddr %s counter drop comment "deny %s"' % (fam(n), fmt_net(n), fmt_net(n)))
    inp += ["ct state established,related accept", "ct state invalid drop", "ip saddr @lan4 accept"]
    if lan6:
        inp.append("ip6 saddr %s accept" % fmt_set(map(fmt_net, lan6)))
    for n in allow:
        inp.append('%s saddr %s counter accept comment "allow %s"' % (fam(n), fmt_net(n), fmt_net(n)))
    icmp = (["echo-request"] if o["ping"] == "on" else []) + ["destination-unreachable", "time-exceeded"]
    inp += ["meta l4proto ipv6-icmp accept",
            "icmp type { %s } limit rate 5/second accept" % ", ".join(icmp),
            "udp dport 68 accept"]
    if pub_rules:
        inp.append("# open ports; a limit counts parallel connections %s" % (
            "of each address" if o["limit-per"] == "address" else "of all addresses together"))
    inp += pub_rules + ["counter drop"]

    fwd = ["type filter hook forward priority filter - 5; policy accept;"]
    for n in deny:
        fwd.append('%s saddr %s counter drop comment "deny %s"' % (fam(n), fmt_net(n), fmt_net(n)))
    if o["docker"] == "lan-only":
        fwd.append("# Docker-published ports stay LAN-only even if the router forwards them")
        for n in allow:
            fwd.append('ct status dnat %s saddr %s accept comment "allow %s"' % (fam(n), fmt_net(n), fmt_net(n)))
        fwd.append("ct status dnat ip saddr != @lan4 ct state new counter drop")

    out = ["# Made by `eris fw` from %s: do not edit, change that file and run `eris fw reload`." % FW_CONF,
           "# Own table only: Docker's iptables-nft tables are not touched.",
           "table inet eris_guard", "delete table inet eris_guard", "table inet eris_guard {"]
    out += ["    " + s for s in sets]
    out += ["", "    chain input {"] + ["        " + r for r in inp] + ["    }"]
    out += ["", "    chain forward {"] + ["        " + r for r in fwd] + ["    }", "}"]
    return "\n".join(out) + "\n"


def reachable(addr, port, proto, cfg, env):
    """Would a new connection from addr to port/proto get in with these settings? (the same order as generate)"""
    if addr.is_loopback:
        return True
    s4, s6 = ssh_safe(cfg, env)
    if proto == "tcp" and port in env["ssh_ports"] and addr_in(addr, s4 + s6):
        return True
    if addr_in(addr, nets(cfg, "deny")):
        return False
    if addr_in(addr, nets(cfg, "lan") + nets(cfg, "allow")):
        return True
    return any(o["port"] == port and o["proto"] == proto for o in entries(cfg, "open"))


def nft_check(rules):
    tmp = os.path.join(E.RUN_DIR, "fw-check.nft")
    write(tmp, rules, 0o600)
    r = sh(NFT, "-c", "-f", tmp)
    remove(tmp)
    if r.returncode != 0:
        raise ValueError("nft -c rejected the new rules, nothing changed:\n%s" % (r.stderr.strip() or r.stdout.strip()))


def live_listing():
    r = sh(NFT, "list", "table", "inet", "eris_guard")
    return r.stdout if r.returncode == 0 else None


def table_loaded():
    return sh(NFT, "list", "table", "inet", "eris_guard").returncode == 0


def listing_of(rules):
    """nft list table of a ruleset loaded into a private, empty network namespace (never the real one)."""
    r = sh("unshare", "--net", "sh", "-c", "%s -f - && %s list table inet eris_guard" % (NFT, NFT), stdin=rules)
    return r.stdout if r.returncode == 0 else None


def normalize(listing):
    """Without counter values and the addresses that per-address limits collect, to compare rule sets."""
    out, dyn, skip = [], False, False
    for line in (listing or "").splitlines():
        s = line.strip()
        if skip:
            skip = not s.endswith("}")
            continue
        if s.startswith("set "):
            dyn = False
        if s.startswith("flags") and "dynamic" in s:
            dyn = True
        if dyn and s.startswith("elements = {"):
            skip = not s.endswith("}")
            continue
        out.append(re.sub(r"counter packets \d+ bytes \d+", "counter", line))
    return "\n".join(out) + "\n"


def outside_changes(env):
    """What the next load would undo that was not made by eris fw: [note, ...]"""
    out = []
    live = live_listing()
    expect = read(os.path.join(PENDING, "guard.nft")) or read(GUARD)
    lst = listing_of(expect) if live is not None and expect else None
    if lst is not None:
        diff = ["%s %s" % (l[0], l[1:].strip()) for l in difflib.unified_diff(
            normalize(lst).splitlines(), normalize(live).splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        if diff:
            out.append("the live table was changed outside eris fw (nft by hand?) and would be replaced: " +
                       " | ".join(diff)[:400])
    applied, guard = read(APPLIED), read(GUARD)
    if applied is not None and guard is not None and not os.path.isdir(PENDING):
        cfg, errors = parse(applied)
        if not errors and generate(cfg, env) != guard:
            out.append("%s is not what eris fw made of the confirmed settings (edited by hand, or the networks "
                       "changed); it would be made again from %s" % (GUARD, FW_CONF))
    return out


def live_counters(listing):
    """{"input": (packets, bytes) of the final drop, "docker": (...) of the Docker drop, "rules": {comment: (...)}}"""
    out = {"input": None, "docker": None, "rules": {}}
    chain = None
    for line in (listing or "").splitlines():
        s = line.strip()
        m = re.match(r"chain (\S+) \{", s)
        if m:
            chain = m.group(1)
            continue
        m = re.search(r'counter packets (\d+) bytes (\d+) (?:drop|accept)(?: comment "([^"]*)")?$', s)
        if not m:
            continue
        val = (int(m.group(1)), int(m.group(2)))
        if m.group(3):
            old = out["rules"].get(m.group(3), (0, 0))
            out["rules"][m.group(3)] = (old[0] + val[0], old[1] + val[1])
        elif chain == "input" and s.startswith("counter packets"):
            out["input"] = val
        elif chain == "forward" and "ct status dnat" in s:
            out["docker"] = val
    return out


# ---------------------------------------------------------------- state

def svc_active():
    return sh(SYSTEMCTL, "is-active", UNIT).stdout.strip() == "active"


def pending():
    if not os.path.isdir(PENDING):
        return None
    p = {k: E.rd(os.path.join(PENDING, k), "") for k in ("reason", "deadline", "backup", "new_session", "setup")}
    p["left"] = max(0, int(float(p["deadline"] or 0) - time.time()))
    return p


def effective_text():
    """The settings commands build on: the unconfirmed change if there is one, else firewall.conf."""
    p = read(os.path.join(PENDING, "firewall.conf"))
    return p if p is not None else read(FW_CONF)


def running_text():
    """The settings the live table was made from."""
    for path in (os.path.join(PENDING, "firewall.conf"), APPLIED, FW_CONF):
        t = read(path)
        if t is not None:
            return t
    return None


def set_up():
    """firewall.conf exists, or the first set-up is live and waiting for its confirmation"""
    return os.path.isfile(FW_CONF) or os.path.isfile(os.path.join(PENDING, "firewall.conf"))


def need_setup(conf_file=False):
    if not set_up():
        if os.path.isfile(GUARD):
            raise ValueError("%s is not managed by eris fw yet; take it over (same rules) with: eris fw import" % GUARD)
        raise ValueError("the firewall is not set up on this server: eris fw setup")
    if conf_file and not os.path.isfile(FW_CONF):
        raise ValueError("the first set-up is not confirmed yet: eris fw confirm")


def loaded_since():
    ts = []
    t = E.rd(LOADED, "")
    if t.isdigit():
        ts.append(int(t))
    v = sh(SYSTEMCTL, "show", UNIT, "--timestamp=unix", "-p", "ActiveEnterTimestamp", "--value").stdout.strip()
    if v.startswith("@") and v[1:].isdigit():
        ts.append(int(v[1:]))
    return clock(max(ts)) if ts else "?"


def stop_units(name):
    sh(SYSTEMCTL, "stop", name + ".service")
    sh(SYSTEMCTL, "reset-failed", name + ".service")


def run_later(unit, seconds, description, *cmd):
    """A transient service that sleeps, then runs cmd. Not a .timer: a systemctl daemon-reload restarts the
    count of an OnActiveSec timer (systemd 257), a sleeping process just keeps going."""
    stop_units(unit)
    return sh("systemd-run", "--unit=" + unit, "--collect", "--quiet", "--description=" + description,
              "/bin/sh", "-c", 'sleep %d; exec "$0" "$@"' % seconds, *cmd)


def arm_rollback(seconds):
    if not os.access(ROLLBACK, os.X_OK):
        raise RuntimeError("%s is missing (reinstall ERIS Console); nothing changed" % ROLLBACK)
    r = run_later(ROLLBACK_UNIT, seconds, "ERIS firewall: undo the change nobody confirmed", "/bin/bash", ROLLBACK)
    if r.returncode != 0:
        raise RuntimeError("could not arm the rollback timer, nothing changed: %s" % r.stderr.strip())


def rollback_now():
    r = sh("/bin/bash", ROLLBACK, timeout=120)
    stop_units(ROLLBACK_UNIT)
    print("  %s %s" % (c(AMBER, "rolled back:"), E.rd(LAST_ROLLBACK) or r.stderr.strip() or "done"))
    return 1


# ---------------------------------------------------------------- backups

BK_RE = re.compile(r"^fw-\d{8}-\d{6}")


def backup(reason, tag=None):
    """A copy of the confirmed state; tagged ones (manual, import, setup) are never pruned."""
    base = os.path.join(E.BACKUP_ROOT, "fw-%s%s" % (time.strftime("%Y%m%d-%H%M%S"), "-" + tag if tag else ""))
    d, n = base, 2
    while os.path.exists(d):
        d, n = "%s-%d" % (base, n), n + 1
    os.makedirs(d)
    src = APPLIED if os.path.isfile(APPLIED) else FW_CONF
    if os.path.isfile(src):
        shutil.copy2(src, os.path.join(d, "firewall.conf"))
    if os.path.isfile(FW_CONF) and os.path.isfile(APPLIED) and read(FW_CONF) != read(APPLIED):
        shutil.copy2(FW_CONF, os.path.join(d, "firewall.conf.edited"))
    if os.path.isfile(GUARD):
        shutil.copy2(GUARD, os.path.join(d, "guard.nft"))
    live = live_listing()
    write(os.path.join(d, "live.nft"), live if live is not None else "# table inet eris_guard was not loaded\n")
    write(os.path.join(d, "info"), "time=%s\nreason=%s\nstate=%s\n" % (
        time.strftime("%F %T"), reason, "on" if live is not None else "off"))
    if not tag:
        auto = [b for b in backups() if re.fullmatch(r"fw-\d{8}-\d{6}(-\d+)?", b["name"])]
        for b in auto[KEEP_AUTO_BACKUPS:]:
            shutil.rmtree(b["dir"], ignore_errors=True)
    return d


def backups():
    out = []
    try:
        names = os.listdir(E.BACKUP_ROOT)
    except FileNotFoundError:
        return out
    for name in names:
        d = os.path.join(E.BACKUP_ROOT, name)
        if BK_RE.match(name) and os.path.isdir(d):
            info = dict(l.split("=", 1) for l in (read(os.path.join(d, "info"), "") or "").splitlines() if "=" in l)
            out.append({"dir": d, "name": name, "time": info.get("time", name), "reason": info.get("reason", ""),
                        "conf": read(os.path.join(d, "firewall.conf"))})
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def brief(text):
    cfg = parse(text)[0]
    return "%d open, %d lan, %d custom" % (len(entries(cfg, "open")), len(entries(cfg, "lan")),
                                         len(entries(cfg, "allow", "deny")))


# ---------------------------------------------------------------- applying a change

def describe(it):
    k = it["kind"]
    if k == "open":
        return "open %d/%s, %s" % (it["port"], it["proto"], "limit %d" % it["limit"] if it["limit"] else "no limit")
    return "%s %s%s" % (k, fmt_net(it["net"]), {"lan": " (full access)", "allow": " (full access)", "deny": " (blocked)"}[k])


def item_key(it):
    if it["kind"] == "open":
        return ("open", it["port"], it["proto"], it["limit"])
    return (it["kind"], it["net"])


def review(old, new, env, outside=()):
    """Print what changes and every warning; risky changes need an explicit yes (or --force / --open-ssh).
    outside: changes made beside eris fw that this load replaces. Returns (go on, a NEW SSH session must confirm)."""
    ok = {item_key(i): i for i in entries(old, "lan", "open", "allow", "deny")}
    nk = {item_key(i): i for i in entries(new, "lan", "open", "allow", "deny")}
    changes = ["%s %s" % (c(RED, "-"), describe(i)) for k, i in ok.items() if k not in nk]
    changes += ["%s %s" % (c(GREEN, "+"), describe(i)) for k, i in nk.items() if k not in ok]
    changes += ["%s %s %s -> %s" % (c(AMBER, "~"), k, old["opts"][k], v) for k, v in new["opts"].items() if old["opts"][k] != v]
    for line in changes or ["no change in the settings"]:
        print("  " + line)
    consents, warns = [], []
    xui = xui_ports()
    old_open = {(o["port"], o["proto"]) for o in entries(old, "open")}
    for o in entries(new, "open"):
        if (o["port"], o["proto"]) not in old_open:
            for level, msg in port_notes(o["port"], o["proto"], env, xui):
                (consents if level in ("ssh", "risky") else warns).append((level, msg))
    old_nets = set(nets(old, "lan") + nets(old, "allow"))
    lan = nets(new, "lan")
    for kind in ("lan", "allow"):
        for n in nets(new, kind):
            if n in old_nets:
                continue
            if not private(n) and n.prefixlen < (16 if n.version == 4 else 32):
                consents.append(("risky", "%s %s gives full access, SSH included, to %d addresses" % (kind, fmt_net(n), n.num_addresses)))
            elif kind == "lan" and not private(n):
                warns.append(("warn", "lan %s is not a private network: it gets full access" % fmt_net(n)))
            elif kind == "allow" and within(n, lan):
                warns.append(("warn", "allow %s: it is inside a lan network, so it has full access anyway" % fmt_net(n)))
    s4, s6 = ssh_safe(new, env)
    for n in set(nets(new, "deny")) - set(nets(old, "deny")):
        if within(n, s4 + s6) or any(within(s, [n]) for s in s4 + s6):
            warns.append(("warn", "deny %s covers LAN addresses: everything from them is blocked except SSH" % fmt_net(n)))
    if new["opts"]["docker"] == "open" and old["opts"]["docker"] != "open":
        consents.append(("risky", "docker open: every Docker-published port the router forwards is reachable from the Internet"))
    consents += [("risky", note) for note in outside]
    lockout = []
    for a, port in ssh_clients(env):
        if not reachable(a, port, "tcp", new, env):
            consents.append(("risky", "NEW SSH logins from %s (connected now, not in the LAN) would be blocked" % a))
            lockout.append(a)
    for level, msg in warns:
        print("  %s %s" % (c(AMBER, "warning:"), msg))
    for level, msg in consents:
        print("  %s %s" % (c(RED, "careful:"), msg))
    if consents:
        need_ssh = any(level == "ssh" for level, _ in consents)
        if interactive():
            word = "open ssh" if need_ssh else "yes"
            a = ask("  type '%s' to do it anyway (anything else = stop): " % word)
            if (a or "").lower() != word:
                print("  not changed")
                return False, False
        else:
            missing = []
            if need_ssh and not FLAGS["open_ssh"]:
                missing.append("--open-ssh")
            if any(level == "risky" for level, _ in consents) and not FLAGS["force"]:
                missing.append("--force")
            if missing:
                print("  not changed: to do it anyway without a terminal, add %s" % " ".join(missing))
                return False, False
    return True, bool(lockout)


def apply_text(text, reason, setup=False):
    """Check new settings, then make them live with the rollback timer armed, and wait for a confirmation."""
    new = load(text)
    env = host_env()
    old_text = running_text()
    old = parse(old_text)[0] if old_text else parse("")[0]
    outside = outside_changes(env)
    go, new_session = review(old, new, env, outside)
    if not go:
        return 1
    rules = generate(new, env)
    nft_check(rules)
    print("  %s nft -c accepts the new rules" % c(GREEN, "ok"))
    if not setup and not table_loaded():
        write(FW_CONF, text)
        E.log("saved while off: %s" % reason, tag="fw")
        print("  the firewall is OFF: saved in %s, it takes effect with: eris fw on" % FW_CONF)
        return 0
    failed = None
    with Lock():
        p = pending()
        if not p and not setup and rules == read(GUARD) and not outside:
            write(FW_CONF, text)
            write(APPLIED, text)
            print("  the rules stay the same (settings saved)")
            return 0
        bk = p["backup"] if p else backup("before " + reason, "setup" if setup else None)
        if not p:
            print("  backup: %s" % bk)
        tmp = PENDING + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        write(os.path.join(tmp, "firewall.conf"), text)
        write(os.path.join(tmp, "guard.nft"), rules)
        write(os.path.join(tmp, "reason"), "%s%s\n" % (p["reason"] + "; " if p else "", reason))
        write(os.path.join(tmp, "backup"), bk + "\n")
        write(os.path.join(tmp, "deadline"), "%d\n" % (time.time() + CONFIRM_SECONDS))
        if new_session:
            write(os.path.join(tmp, "new_session"), "1\n")
        if setup or (p and p["setup"]):
            write(os.path.join(tmp, "setup"), "1\n")
        arm_rollback(CONFIRM_SECONDS)              # the timer first, then the new rules
        shutil.rmtree(PENDING, ignore_errors=True)
        os.rename(tmp, PENDING)
        r = sh(NFT, "-f", os.path.join(PENDING, "guard.nft"))
        if r.returncode != 0:
            failed = r.stderr.strip() or "nft -f failed"
        else:
            write(LOADED, "%d\n" % time.time())
            remove(LAST_ROLLBACK)
    if failed:
        rollback_now()
        raise RuntimeError("loading failed, the previous rules are back: %s" % failed)
    E.log("live, waiting for a confirmation: %s" % reason, tag="fw")
    print("  %s the new rules are live; without a confirmation the previous ones come back in %d s" % (
        c(GREEN, "loaded:"), CONFIRM_SECONDS))
    return wait_confirm()


def wait_confirm():
    p = pending()
    if not p:
        return 0
    if p["new_session"]:
        print("  %s this change can block new SSH logins. Keep it only from a NEW SSH session: eris fw confirm" % c(RED, "!"))
        print("    (within %d s; Enter here proves nothing, this session stays up either way)" % p["left"])
        return 0
    if not interactive():
        print("  keep it with: eris fw confirm   (within %d s, otherwise the previous rules come back)" % p["left"])
        return 0
    print("  check now that what you need still works (SSH, the website, ...)")
    sys.stdout.write("  %s within %d s to keep the new rules (n = undo now): " % (c(WHITE, "press Enter"), p["left"]))
    sys.stdout.flush()
    try:
        ready = select.select([sys.stdin], [], [], p["left"])[0]
        line = sys.stdin.readline() if ready else None
    except KeyboardInterrupt:
        print("\n  not confirmed yet: eris fw confirm within %d s, otherwise the previous rules come back" % (pending() or p)["left"])
        return 0
    if line is None:
        print("\n  no answer in time")
        return rollback_now()
    if line.strip().lower() in ("", "y", "yes", "keep"):
        return confirm()
    return rollback_now()


def confirm():
    with Lock():
        p = pending()
        if not p:
            rb = E.rd(LAST_ROLLBACK)
            print("  nothing to confirm%s" % ("; the last change was rolled back: " + rb if rb else ""))
            return 1
        stop_units(ROLLBACK_UNIT)
        text = read(os.path.join(PENDING, "firewall.conf"))
        rules = read(os.path.join(PENDING, "guard.nft"))
        cur = read(FW_CONF)
        if cur is not None and cur not in (text, read(APPLIED)):
            keep = "%s.edited-%s" % (FW_CONF, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(FW_CONF, keep)
            print("  your newer edits of %s are kept in %s" % (FW_CONF, keep))
        write(GUARD, rules)
        write(FW_CONF, text)
        write(APPLIED, text)
        shutil.rmtree(PENDING, ignore_errors=True)
    if p["setup"]:
        sh(SYSTEMCTL, "daemon-reload")
        sh(SYSTEMCTL, "enable", UNIT)
        if not svc_active():
            sh(SYSTEMCTL, "start", UNIT)
    remove(LAST_ROLLBACK)
    E.log("confirmed: %s" % p["reason"], tag="fw")
    print("  %s %s; saved in %s (loaded at boot)" % (c(GREEN, "kept:"), p["reason"], GUARD))
    return 0


def edit(fn, reason):
    """Change the settings with fn(cfg) (it edits cfg["lines"]) and apply them."""
    need_setup()
    cfg = load(effective_text())
    fn(cfg)
    return apply_text(text_of(cfg), reason)


# ---------------------------------------------------------------- commands

def cmd_open(args):
    if not args:
        raise ValueError("usage: eris fw open PORT[/tcp|/udp] [limit N]")
    port, proto = parse_port(args[0])
    proto = proto or "tcp"
    rest = args[1:]
    if rest and rest[0].lower() == "limit":
        rest = rest[1:]
    if len(rest) > 1:
        raise ValueError("usage: eris fw open PORT[/tcp|/udp] [limit N]")
    limit = parse_limit(rest[0]) if rest else DEFAULT_LIMIT

    def fn(cfg):
        for o in entries(cfg, "open"):
            if (o["port"], o["proto"]) == (port, proto):
                raise ValueError("%d/%s is open already (%s); change the limit with: eris fw limit %d/%s N" % (
                    port, proto, "limit %d" % o["limit"] if o["limit"] else "no limit", port, proto))
        insert_line(cfg, "open", open_line(port, proto, limit))
    return edit(fn, "open %d/%s" % (port, proto))


def cmd_close(args):
    if len(args) != 1:
        raise ValueError("usage: eris fw close PORT[/tcp|/udp]")
    port, proto = parse_port(args[0])

    def fn(cfg):
        hit = [o for o in entries(cfg, "open") if o["port"] == port and proto in (None, o["proto"])]
        if not hit:
            raise ValueError("%s is not open (eris fw shows the open ports)" % args[0])
        delete_lines(cfg, [o["line"] for o in hit])
    return edit(fn, "close %s" % args[0])


def cmd_net(kind, args):
    if len(args) != 1:
        raise ValueError("usage: eris fw %s ADDR[/PREFIX]" % kind)
    n = parse_net(args[0])
    shown = fmt_net(n)
    if shown != args[0]:
        print("  (as a network: %s)" % shown)

    def fn(cfg):
        for it in entries(cfg, "lan", "allow", "deny"):
            if it["net"] == n:
                k = numbered(cfg).index(it) + 1
                raise ValueError("%s is entry %d already (%s); remove it first: eris fw remove %d" % (
                    shown, k, describe(it), k))
        insert_line(cfg, kind, "%s %s" % (kind, shown))
    return edit(fn, "%s %s" % (kind, shown))


def cmd_remove(args):
    if len(args) != 1 or not args[0].isdigit():
        raise ValueError("usage: eris fw remove N   (N from the list of eris fw)")
    k = int(args[0])
    what = []

    def fn(cfg):
        items = numbered(cfg)
        if not 1 <= k <= len(items):
            raise ValueError("there is no entry %d (1-%d, see eris fw)" % (k, len(items)))
        what.append(describe(items[k - 1]))
        delete_lines(cfg, [items[k - 1]["line"]])
    need_setup()
    cfg = load(effective_text())
    fn(cfg)
    return apply_text(text_of(cfg), "remove %d (%s)" % (k, what[0]))


def cmd_limit(args):
    if len(args) != 2:
        raise ValueError("usage: eris fw limit PORT[/tcp|/udp] N   (N parallel connections, 0 = no limit)")
    port, proto = parse_port(args[0])
    limit = parse_limit(args[1])

    def fn(cfg):
        hit = [o for o in entries(cfg, "open") if o["port"] == port and proto in (None, o["proto"])]
        if not hit:
            raise ValueError("%s is not open: eris fw open %s limit %s" % (args[0], args[0], args[1]))
        for o in hit:
            replace_line(cfg, o["line"], open_line(o["port"], o["proto"], limit))
    return edit(fn, "limit %s %s" % (args[0], limit or "none"))


def cmd_set(args):
    if len(args) != 2 or args[0].lower() not in OPTIONS or args[1].lower() not in OPTIONS[args[0].lower()]:
        raise ValueError("usage: eris fw set " + " | ".join("%s %s" % (k, "|".join(v)) for k, v in OPTIONS.items()))
    name, value = args[0].lower(), args[1].lower()

    def fn(cfg):
        hit = [it for it in entries(cfg, "option") if it["name"] == name]
        if hit:
            replace_line(cfg, hit[0]["line"], "%s %s" % (name, value))
        else:
            insert_line(cfg, "option", "%s %s" % (name, value))
    return edit(fn, "%s %s" % (name, value))


def cmd_reload():
    need_setup(conf_file=True)
    text = read(FW_CONF)
    env = host_env()
    rules = generate(load(text), env)
    if not pending() and rules == read(GUARD) and not outside_changes(env):
        nft_check(rules)
        if not table_loaded():
            print("  %s has no changes; the firewall is OFF (eris fw on)" % FW_CONF)
            return 0
        r = sh(NFT, "-f", GUARD)
        if r.returncode != 0:
            raise RuntimeError("nft -f %s failed: %s" % (GUARD, r.stderr.strip()))
        write(LOADED, "%d\n" % time.time())
        if read(APPLIED) != text:
            write(APPLIED, text)
        E.log("reload: no change in the rules, %s loaded again" % GUARD, tag="fw")
        print("  no change in the rules; %s loaded again" % GUARD)
        return 0
    return apply_text(text, "reload of firewall.conf")


def cmd_off(args):
    need_setup(conf_file=True)
    minutes = 0
    if args:
        if not args[0].isdigit() or int(args[0]) > 1440:
            raise ValueError("usage: eris fw off [MINUTES]   (0-1440; 0 = until eris fw on or the next boot)")
        minutes = int(args[0])
    if pending():
        print("  dropping the unconfirmed change first")
        rollback_now()
    stop_units(AUTO_ON_UNIT)
    remove(AUTO_ON_AT)
    sh(SYSTEMCTL, "stop", UNIT)
    if table_loaded():
        sh(NFT, "delete", "table", "inet", "eris_guard")
    if table_loaded():
        raise RuntimeError("could not remove the table inet eris_guard")
    note = "at the latest at the next boot"
    if minutes:
        r = run_later(AUTO_ON_UNIT, minutes * 60, "ERIS firewall: on again after eris fw off", SYSTEMCTL, "start", UNIT)
        if r.returncode == 0:
            at = int(time.time() + minutes * 60)
            write(AUTO_ON_AT, "%d\n" % at)
            note = "by itself at %s" % clock(at)
        else:
            note = "no automatic switch-on: %s" % r.stderr.strip()
    E.log("off (%s)" % note, tag="fw")
    print("%s firewall %s" % (c(PINK, "ERIS"), c(RED, "OFF")))
    print("  %s every port the router forwards to this server can be reached from the Internet now" % c(RED, "!"))
    lis = sorted(k for k, e in listeners().items() if e["public"] and k not in LOCAL_ONLY and k[1] < 32768)
    if lis:
        print("    listening on all addresses: " + ", ".join("%d/%s" % (p, pr) for pr, p in sorted(lis, key=lambda k: k[1])))
    print("  back on: eris fw on   (%s)" % note)
    return 0


def cmd_on():
    need_setup(conf_file=True)
    stop_units(AUTO_ON_UNIT)
    remove(AUTO_ON_AT)
    if not os.path.isfile(GUARD):
        return apply_text(read(FW_CONF), "switch on", setup=True)
    if table_loaded():
        print("%s firewall: already %s" % (c(PINK, "ERIS"), c(GREEN, "ON")))
    else:
        r = sh(NFT, "-c", "-f", GUARD)
        if r.returncode != 0:
            raise ValueError("%s does not pass nft -c, the firewall stays off:\n%s" % (GUARD, r.stderr.strip()))
        r = sh(NFT, "-f", GUARD) if svc_active() else sh(SYSTEMCTL, "start", UNIT)
        if not table_loaded():
            raise RuntimeError("the firewall did not come on: %s" % (r.stderr.strip() or "see journalctl -u " + UNIT))
        write(LOADED, "%d\n" % time.time())
        E.log("on", tag="fw")
        print("%s firewall %s (the confirmed rules of %s)" % (c(PINK, "ERIS"), c(GREEN, "ON"), GUARD))
    text = read(FW_CONF)
    cfg, errors = parse(text)
    if not errors and generate(cfg, host_env()) != read(GUARD):
        print("  %s has changes that are not loaded yet; loading them now:" % FW_CONF)
        return apply_text(text, "settings changed while off")
    return 0


def cmd_backup():
    need_setup()
    d = backup("manual backup", "manual")
    E.log("backup %s" % d, tag="fw")
    print("  backup: %s  (eris fw restore lists them)" % d)
    return 0


def cmd_restore(args):
    need_setup()
    bks = [b for b in backups() if b["conf"] is not None]
    if not bks:
        raise ValueError("no firewall backups in %s/fw-* yet" % E.BACKUP_ROOT)
    if not args:
        for i, b in enumerate(bks[:20]):
            print("  %s %s  %-34s %s" % (c(CYAN, "%2d)" % (i + 1)), b["time"], b["reason"][:34], brief(b["conf"])))
        if not interactive():
            print("usage: eris fw restore N|latest|DIR")
            return 2
        a = ask("  restore which one [1-%d, empty = none]: " % min(20, len(bks)))
        if not a:
            return 0
        args = [a]
    sel = args[0]
    if sel == "latest":
        b = bks[0]
    elif sel.isdigit() and 1 <= int(sel) <= len(bks):
        b = bks[int(sel) - 1]
    else:
        b = next((x for x in bks if x["dir"] == sel.rstrip("/") or x["name"] == os.path.basename(sel.rstrip("/"))), None)
        if not b:
            raise ValueError("no such backup: %s (eris fw restore lists them)" % sel)
    print("  restoring %s (%s)" % (b["dir"], b["reason"]))
    return apply_text(b["conf"], "restore %s" % b["name"])


def default_lan(env):
    lan = [n for _, n in env["connected"] if private(n) and n.version == 4]
    if os.path.exists("/sys/class/net/docker0"):
        lan.append(ipaddress.ip_network("172.16.0.0/12"))
    return collapse(lan, 4) + [ipaddress.ip_network("fe80::/10"), ipaddress.ip_network("fc00::/7")]


def cmd_setup():
    if set_up():
        print("%s firewall: set up already (%s); see: eris fw" % (c(PINK, "ERIS"), FW_CONF if os.path.isfile(FW_CONF)
                                                                  else "not confirmed yet: eris fw confirm"))
        return 0
    if os.path.isfile(GUARD):
        return cmd_import()
    env = host_env()
    lan = default_lan(env)
    print("%s firewall set-up: full access for %s" % (c(PINK, "ERIS"), ", ".join(map(fmt_net, lan))))
    xui = xui_ports()
    cands = []
    for (proto, port), l in sorted(env["listeners"].items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if (not l["public"] or (proto, port) in LOCAL_ONLY or port >= 32768 or
                any(level in ("ssh", "risky") for level, _ in port_notes(port, proto, env, xui))):
            continue
        cands.append((port, proto, ", ".join(sorted(l["names"]))))
    opens, custom = [], []
    if cands and interactive():
        print("  services listening on all addresses (SSH, admin panels and file sharing are left out):")
        for i, (port, proto, names) in enumerate(cands):
            print("  %s %d/%s  %s" % (c(CYAN, "%2d)" % (i + 1)), port, proto, names))
        for w in (ask("  numbers to open to the Internet, e.g. 1 3 (empty = none): ") or "").replace(",", " ").split():
            if w.isdigit() and 1 <= int(w) <= len(cands):
                opens.append((cands[int(w) - 1][0], cands[int(w) - 1][1], DEFAULT_LIMIT))
    elif cands:
        print("  nothing is opened to the Internet; later: eris fw open PORT")
    for a, port in ssh_clients(env):
        if reachable(a, port, "tcp", load(conf_text(lan, opens, custom)), env):
            continue
        print("  %s you are connected over SSH from %s, which is not in the LAN" % (c(AMBER, "!"), a))
        if not interactive():
            raise ValueError("SSH from %s would be blocked; run eris fw setup in a terminal to choose what to do" % a)
        v = choose("keep SSH working", ["allow only %s" % a, "open SSH to the whole Internet", "stop"], None)
        if v and v.startswith("allow"):
            custom.append(("allow", ipaddress.ip_network(a)))
        elif v and v.startswith("open"):
            opens.append((port, "tcp", None))
        else:
            print("  not set up")
            return 1
    return apply_text(conf_text(lan, opens, custom), "first set-up", setup=True)


def import_text(old):
    def elems(pattern):
        m = re.search(pattern, old, re.S)
        return [e.strip() for e in m.group(1).split(",") if e.strip()] if m else []
    lan = elems(r"set lan4\s*\{[^}]*?elements\s*=\s*\{([^}]*)\}") + elems(r"ip6 saddr\s*\{([^}]*)\}\s*accept")
    ports = elems(r"set public_tcp\s*\{[^}]*?elements\s*=\s*\{([^}]*)\}")
    m = re.search(r"@public_tcp ct state new ct count over (\d+) drop", old)
    if "table inet eris_guard" not in old or not lan:
        raise ValueError("%s is not in a format eris fw can take over (move it away, then: eris fw setup)" % GUARD)
    opts = {"ping": "on" if "echo-request" in old else "off",
            "docker": "lan-only" if "ct status dnat ip saddr != @lan4" in old else "open"}
    return conf_text([parse_net(x) for x in lan], [(int(p), "tcp", int(m.group(1)) if m else None) for p in ports],
                     (), opts)


def cmd_import():
    if os.path.isfile(FW_CONF):
        raise ValueError("%s exists already; eris fw reload loads it" % FW_CONF)
    old = read(GUARD)
    if old is None:
        raise ValueError("there is no %s to take over: eris fw setup" % GUARD)
    text = import_text(old)
    rules = generate(load(text), host_env())
    before, after = listing_of(old), listing_of(rules)
    if before is None or after is None:
        raise RuntimeError("could not compare the rules in a private network namespace (unshare + nft)")
    if normalize(before) != normalize(after):
        write(FW_CONF + ".imported", text)
        for line in difflib.unified_diff(normalize(before).splitlines(), normalize(after).splitlines(),
                                         "rules of guard.nft now", "rules made from the import", lineterm=""):
            print("  " + line)
        raise ValueError("the imported settings would not give exactly the same rules, nothing changed; see %s.imported"
                         % FW_CONF)
    bk = backup("before taking over guard.nft", "import")
    write(FW_CONF, text)
    write(APPLIED, text)
    write(GUARD, rules)
    E.log("import: %s -> %s (same rules), backup %s" % (GUARD, FW_CONF, bk), tag="fw")
    print("%s firewall: settings taken over from %s into %s" % (c(PINK, "ERIS"), GUARD, FW_CONF))
    print("  %s the rules made from them are exactly the same (nft list table, compared in a private network namespace)"
          % c(GREEN, "same rules:"))
    print("  backup of the old files: %s" % bk)
    print("  the live table was not touched; see it all with: eris fw")
    return 0


# ---------------------------------------------------------------- status

def lan_note(n, env):
    if n == ipaddress.ip_network("fe80::/10"):
        return "IPv6 link-local"
    if n == ipaddress.ip_network("fc00::/7"):
        return "IPv6 private (ULA)"
    names = []
    for name, cn in env["connected"]:
        if cn.version == n.version and cn.subnet_of(n) and name not in names:
            names.append(name)
    docker = [x for x in names if x == "docker0" or x.startswith("br-")]
    other = [x for x in names if x not in docker]
    parts = (["on " + ", ".join(other)] if other else []) + (["Docker: " + " ".join(docker)] if docker else [])
    return ("; ".join(parts))[:60]


def summary():
    if not set_up():
        return "not managed yet (eris fw import)" if os.path.isfile(GUARD) else "not set up"
    cfg = parse(effective_text())[0]
    st = "ON" if table_loaded() else "OFF"
    if pending():
        st += ", change PENDING"
    return "%s, %d open ports, %d custom rules" % (st, len(entries(cfg, "open")), len(entries(cfg, "allow", "deny")))


def cmd_status():
    if not set_up():
        if os.path.isfile(GUARD):
            print("%s firewall: %s is not managed by eris fw yet; take it over (same rules): eris fw import" % (c(PINK, "ERIS"), GUARD))
        else:
            print("%s firewall: not set up on this server (optional): eris fw setup" % c(PINK, "ERIS"))
        return 0
    env = host_env()
    p = pending()
    cfg, errors = parse(effective_text())
    if errors:
        print("%s %s has errors (eris fw reload refuses it until they are fixed):\n  %s" % (
            c(RED, "!"), FW_CONF, "\n  ".join(errors)))
        cfg = parse(running_text())[0]
    live = live_listing()
    on = live is not None
    boot = sh(SYSTEMCTL, "is-enabled", UNIT).stdout.strip() == "enabled"
    print("%s firewall  %s   table inet eris_guard, %s at boot   settings: %s" % (
        c(PINK, "ERIS"), c(GREEN, "ON") if on else c(RED, "OFF"), "loaded" if boot else "NOT loaded", FW_CONF))
    if p:
        print("  %s %s: live, not confirmed; the previous rules come back in %d s unless: eris fw confirm" % (
            c(AMBER, "PENDING"), p["reason"], p["left"]))
    if not on:
        at = E.rd(AUTO_ON_AT, "")
        print("  %s every port the router forwards here is reachable from the Internet; eris fw on%s" % (
            c(RED, "!"), "  (by itself at %s)" % clock(int(at)) if at.isdigit() and int(at) > time.time() else ""))
    rb = E.rd(LAST_ROLLBACK)
    if rb:
        print("  %s %s" % (c(AMBER, "last change rolled back:"), rb))
    nums = {id(it): k + 1 for k, it in enumerate(numbered(cfg))}
    xui = xui_ports()
    conns = connections()
    cn = live_counters(live)
    per = cfg["opts"]["limit-per"]

    print("  " + c(WHITE, "Open to the Internet"))
    if not entries(cfg, "open"):
        print("       none   (eris fw open PORT)")
    for it in entries(cfg, "open"):
        lim = ("limit %d %s" % (it["limit"], "each" if per == "address" else "shared")) if it["limit"] else "no limit"
        svc, col = service_name(it["port"], it["proto"], env, xui)
        now = conns.get(it["port"]) if it["proto"] == "tcp" else None
        print("  %s %-10s %-18s %s %s" % (c(CYAN, "%3d)" % nums[id(it)]), "%d/%s" % (it["port"], it["proto"]), lim,
                                         c(col, "%-20s" % svc) if col else "%-20s" % svc,
                                         "now %d connection%s from %d address%s" % (
                                             now[0], "s"[:now[0] != 1], now[1], "es"[:2 * (now[1] != 1)]) if now else ""))
    print("  " + c(WHITE, "Full access (LAN, Docker)"))
    for it in entries(cfg, "lan"):
        print("  %s %-29s %s" % (c(CYAN, "%3d)" % nums[id(it)]), fmt_net(it["net"]), c(GREY, lan_note(it["net"], env))))
    print("  " + c(WHITE, "Custom rules"))
    if not entries(cfg, "allow", "deny"):
        print("       none   (eris fw allow ADDR | eris fw deny ADDR)")
    for it in entries(cfg, "allow", "deny"):
        hits = cn["rules"].get("%s %s" % (it["kind"], fmt_net(it["net"])))
        print("  %s %-6s %-22s %s %s" % (c(CYAN, "%3d)" % nums[id(it)]), it["kind"], fmt_net(it["net"]),
                                        c(RED, "blocked    ") if it["kind"] == "deny" else c(GREEN, "full access"),
                                        "%d packets" % hits[0] if hits else ""))
    print("  always allowed: localhost, SSH (%s) from the LAN, replies to this server's own connections" % (
        ", ".join(map(str, env["ssh_ports"]))))
    print("  options: limit-per %s (%s), docker %s, ping %s" % (
        per, "a limit is for each address" if per == "address" else "a limit is shared by all addresses",
        cfg["opts"]["docker"], cfg["opts"]["ping"]))
    if on:
        d, f = cn["input"] or (0, 0), cn["docker"]
        print("  dropped since %s: %d packets (%s) from outside the LAN%s" % (
            loaded_since(), d[0], E.human(d[1]), ", %d new connections to Docker ports" % f[0] if f else ""))
    for note in outside_changes(env):
        print("  %s %s" % (c(AMBER, "!"), note))
    if not errors and not p and read(FW_CONF) != read(APPLIED) and generate(cfg, env) != read(GUARD):
        print("  %s %s has edits that are not loaded yet: eris fw reload" % (c(AMBER, "!"), FW_CONF))
    return 0


# ---------------------------------------------------------------- menu

def pick_port(cfg, prompt):
    opens = entries(cfg, "open")
    if not opens:
        print("   no open ports")
        return None
    for i, o in enumerate(opens):
        print("   %s %d/%s  %s" % (c(CYAN, "%d)" % (i + 1)), o["port"], o["proto"],
                                 "limit %d" % o["limit"] if o["limit"] else "no limit"))
    a = ask("   %s [1-%d]: " % (prompt, len(opens)))
    return opens[int(a) - 1] if a and a.isdigit() and 1 <= int(a) <= len(opens) else None


def menu():
    while True:
        print()
        print(" " + c(PINK, "ERIS") + c(WHITE, " Firewall") + "   " + summary())
        if not set_up():
            items = [("1", "Take over the existing %s (same rules)" % GUARD if os.path.isfile(GUARD)
                      else "Set up the firewall on this server")]
        else:
            on = table_loaded()
            items = [("1", "Show the rules"), ("2", "Open a port to the Internet"), ("3", "Close a port"),
                     ("4", "Allow an address or network (full access)"), ("5", "Block an address or network"),
                     ("6", "Remove an entry by its number"), ("7", "Connection limit of a port"),
                     ("8", "Options (limit per address, Docker ports, ping)"),
                     ("9", "Reload %s after editing it" % FW_CONF),
                     ("10", "Turn the firewall OFF for a while" if on else "Turn the firewall ON"),
                     ("11", "Back up the settings now"), ("12", "Restore a backup")]
            if pending():
                items.insert(0, ("c", "Keep (confirm) the pending change"))
        for k, label in items:
            print("  %s %s" % (c(CYAN, "%2s)" % k), label))
        print("  %s back" % c(CYAN, " q)"))
        ch = ask(" > ")
        if ch is None or ch.lower() in ("q", ""):
            return 0
        try:
            menu_item(ch.lower())
        except (ValueError, RuntimeError, OSError) as e:
            print("  " + c(RED, "not changed: %s" % e))


def menu_item(ch):
    if not set_up():
        if ch == "1":
            return cmd_import() if os.path.isfile(GUARD) else cmd_setup()
        print("  unknown choice")
        return None
    cfg = parse(effective_text())[0]
    if ch == "c":
        return confirm()
    if ch == "1":
        return cmd_status()
    if ch == "2":
        port = ask("   port to open, e.g. 8443 or 8443/udp: ")
        if not port:
            return None
        lim = ask("   at most how many parallel connections [%d, 0 = no limit]: " % DEFAULT_LIMIT)
        return cmd_open([port] + (["limit", lim] if lim else []))
    if ch == "3":
        o = pick_port(cfg, "close which one")
        return cmd_close(["%d/%s" % (o["port"], o["proto"])]) if o else None
    if ch == "7":
        o = pick_port(cfg, "limit of which one")
        if not o:
            return None
        lim = ask("   parallel connections for %d/%s [now %s, 0 = no limit]: " % (o["port"], o["proto"], o["limit"] or "none"))
        return cmd_limit(["%d/%s" % (o["port"], o["proto"]), lim]) if lim else None
    if ch in ("4", "5"):
        a = ask("   address or network, e.g. 1.2.3.4 or 1.2.3.0/24: ")
        return cmd_net("allow" if ch == "4" else "deny", [a]) if a else None
    if ch == "6":
        cmd_status()
        a = ask("   number to remove: ")
        return cmd_remove([a]) if a else None
    if ch == "8":
        name = choose("option", list(OPTIONS), None)
        v = choose(name, list(OPTIONS[name]), cfg["opts"][name]) if name else None
        return cmd_set([name, v]) if v else None
    if ch == "9":
        return cmd_reload()
    if ch == "10":
        if table_loaded():
            m = ask("   back on by itself after how many minutes [30, 0 = only by hand or at boot]: ")
            return cmd_off([m or "30"]) if m is not None else None
        return cmd_on()
    if ch == "11":
        return cmd_backup()
    if ch == "12":
        return cmd_restore([])
    print("  unknown choice")
    return None


def main(args):
    if os.geteuid() != 0:
        print("eris fw: run as root")
        return 1
    FLAGS["force"] = "--force" in args
    FLAGS["open_ssh"] = "--open-ssh" in args
    args = [a for a in args if a not in ("--force", "--open-ssh")]
    sub = args[0].lower() if args else "status"
    rest = args[1:]
    cmds = {
        "status": lambda: cmd_status(), "show": lambda: cmd_status(), "list": lambda: cmd_status(),
        "open": lambda: cmd_open(rest), "close": lambda: cmd_close(rest),
        "allow": lambda: cmd_net("allow", rest), "deny": lambda: cmd_net("deny", rest),
        "remove": lambda: cmd_remove(rest), "limit": lambda: cmd_limit(rest), "set": lambda: cmd_set(rest),
        "reload": lambda: cmd_reload(), "confirm": lambda: confirm(),
        "off": lambda: cmd_off(rest), "on": lambda: cmd_on(),
        "backup": lambda: cmd_backup(), "restore": lambda: cmd_restore(rest),
        "setup": lambda: cmd_setup(), "import": lambda: cmd_import(), "menu": lambda: menu(),
        "nft": lambda: print(live_listing() or "the table inet eris_guard is not loaded") or 0,
    }
    if sub not in cmds:
        print(HELP)
        return 0 if sub in ("help", "-h", "--help") else 2
    try:
        return cmds[sub]()
    except (ValueError, RuntimeError, OSError) as e:
        print("eris fw: %s" % e)
        return 2
