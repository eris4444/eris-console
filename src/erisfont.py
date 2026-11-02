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
