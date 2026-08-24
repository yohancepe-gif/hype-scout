"""Generate the app icons. Stdlib only - no Pillow, no image files to check in
that nobody can regenerate.

Draws the thing the app is actually about: a hype curve that accelerates, peaks,
and rolls over. Rendered at 2x and downsampled, which is enough anti-aliasing
for an icon at these sizes.

Run:  python3 scripts/make_icons.py
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "icons")

BG_TOP = (0x1a, 0x24, 0x31)
BG_BOT = (0x0b, 0x0f, 0x14)
LINE = (0x3f, 0xcf, 0x8e)
GLOW = (0x7a, 0xa2, 0xf7)

PEAK_AT = 0.40          # where the curve tops out, left to right
SHARPNESS = 3.2         # how sudden the run-up is
X0, X1 = 0.11, 0.92     # curve inset
BASE_Y = 0.80           # baseline
AMP = 0.56              # peak height above baseline
STROKE = 0.052          # stroke width as a fraction of icon size


def hype(t):
    """Asymmetric pulse: slow build, sharp run, slower decay. Peaks at 1.0."""
    if t <= 0:
        return 0.0
    r = t / PEAK_AT
    return (r ** SHARPNESS) * math.exp(SHARPNESS * (1 - r))


def curve_y(x):
    """Icon-space y for a given icon-space x, or None outside the curve."""
    if not (X0 <= x <= X1):
        return None
    return BASE_Y - AMP * hype((x - X0) / (X1 - X0))


def blend(dst, src, a):
    return tuple(int(round(d + (s - d) * a)) for d, s in zip(dst, src))


def render(size):
    """Return rows of (r,g,b,a) at `size`, supersampled 2x."""
    ss = size * 2
    half = STROKE * ss / 2.0
    rows = []
    for py in range(ss):
        row = []
        yv = py / ss
        for px in range(ss):
            xv = px / ss
            # vertical gradient background
            col = blend(BG_TOP, BG_BOT, yv)

            cy = curve_y(xv)
            if cy is not None:
                ypix = yv * ss
                cypix = cy * ss
                # translucent fill under the curve
                if ypix > cypix and yv < BASE_Y + 0.02:
                    col = blend(col, LINE, 0.16)
                # the stroke itself, with a soft edge
                d = abs(ypix - cypix)
                if d < half + 1.5:
                    a = 1.0 if d <= half else max(0.0, 1 - (d - half) / 1.5)
                    # shade the stroke from blue at the left to green at the peak
                    mix = min(1.0, max(0.0, (xv - X0) / (PEAK_AT - X0 + 1e-6)))
                    col = blend(col, blend(GLOW, LINE, mix), a)
            row.append(col)
        rows.append(row)

    # downsample 2x2
    out = []
    for y in range(size):
        r0, r1 = rows[2 * y], rows[2 * y + 1]
        row = []
        for x in range(size):
            px = [r0[2 * x], r0[2 * x + 1], r1[2 * x], r1[2 * x + 1]]
            row.append(tuple(sum(c[i] for c in px) // 4 for i in range(3)) + (255,))
        out.append(row)
    return out


def write_png(path, rows):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    hdr = struct.pack(">IIBBBBB", len(rows[0]), len(rows), 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", hdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    os.makedirs(OUT, exist_ok=True)
    # 180 = apple-touch-icon (Safari "Add to Dock" and iOS home screen)
    # 192/512 = web app manifest
    for size in (180, 192, 512):
        n = write_png(os.path.join(OUT, "icon-%d.png" % size), render(size))
        print("  icons/icon-%d.png  %d bytes" % (size, n))


if __name__ == "__main__":
    main()
