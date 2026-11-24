#!/usr/bin/python3 -I
"""Home-screen icons (apple-touch-icon, 180x180 PNG) for the apps built here, drawn with plain
geometry so nothing needs installing. Writes each app's app/static/icon.png.
    python3 ~/server/setup/icons/make-icons.py"""
import math
import os
import struct
import zlib

S = 180
ROOT = os.path.expanduser("~/server")


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def render(bg, shapes):
    """shapes: functions (x, y) -> coverage 0..1 with a color; painted in order over bg."""
    rows = []
    for y in range(S):
        row = bytearray([0])
        for x in range(S):
            r, g, b = bg
            for color, cover in shapes:
                a = cover(x + 0.5, y + 0.5)
                if a > 0:
                    r, g, b = (round(c0 + (c1 - c0) * a) for c0, c1 in zip((r, g, b), color))
            row += bytes((r, g, b))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def stroke(points, width):
    """A polyline with round joins, antialiased."""
    segs = list(zip(points, points[1:]))
    return lambda x, y: max(0.0, min(1.0, width / 2 - min(seg_dist(x, y, *a, *b) for a, b in segs) + 0.5))


def ring(cx, cy, r, width):
    return lambda x, y: max(0.0, min(1.0, width / 2 - abs(math.hypot(x - cx, y - cy) - r) + 0.5))


def box(x0, y0, x1, y1, radius=6):
    def cover(x, y):
        qx = abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - radius)
        qy = abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - radius)
        d = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
        return max(0.0, min(1.0, 0.5 - d))
    return cover


ICONS = {
    # Pulso: a heartbeat line, green on the terminal black.
    "pulso": ("#0b0e14", [("#3fb950", stroke([(18, 98), (58, 98), (72, 58), (92, 128), (108, 74), (118, 98), (162, 98)], 11))]),
    # Finanzas: three rising bars, white on its teal.
    "finanzas": ("#0f766e", [("#ffffff", box(38, 104, 64, 142)), ("#ffffff", box(77, 78, 103, 142)),
                             ("#ffffff", box(116, 46, 142, 142))]),
    # Recordatorios: a clock, white on its violet.
    "recordatorios": ("#6d28d9", [("#ffffff", ring(90, 90, 52, 11)), ("#ffffff", stroke([(90, 90), (90, 56)], 10)),
                                  ("#ffffff", stroke([(90, 90), (114, 104)], 10))]),
    # Lista: three ticked lines, white on its orange.
    "lista": ("#c2410c", [("#ffffff", stroke([(40, 58), (52, 70), (70, 48)], 10)), ("#ffffff", box(86, 53, 144, 65, 5)),
                          ("#ffffff", stroke([(40, 98), (52, 110), (70, 88)], 10)), ("#ffffff", box(86, 93, 144, 105, 5)),
                          ("#ffffff", box(40, 129, 66, 141, 5)), ("#ffffff", box(86, 129, 144, 141, 5))]),
    # Papeles: a sheet with its lines, white on its blue.
    "papeles": ("#1d4ed8", [("#ffffff", box(48, 34, 132, 146, 10)), ("#1d4ed8", box(62, 62, 118, 70, 3)),
                            ("#1d4ed8", box(62, 86, 118, 94, 3)), ("#1d4ed8", box(62, 110, 100, 118, 3))]),
    # Hoy: a sun rising over the horizon, white on its sky blue.
    "hoy": ("#0369a1", [("#ffffff", lambda x, y: 0.0 if y > 112 else max(0.0, min(1.0, 30 - math.hypot(x - 90, y - 112) + 0.5))),
                        ("#ffffff", stroke([(30, 124), (150, 124)], 10)),
                        ("#ffffff", stroke([(90, 50), (90, 66)], 9)), ("#ffffff", stroke([(46, 68), (57, 79)], 9)),
                        ("#ffffff", stroke([(134, 68), (123, 79)], 9))]),
    # Mensajes: a chat bubble with three dots, white on Telegram's blue.
    "mensajes": ("#2481cc", [("#ffffff", box(32, 42, 148, 120, 26)), ("#ffffff", stroke([(58, 112), (46, 144), (86, 118)], 12)),
                             ("#2481cc", ring(62, 81, 4, 9)), ("#2481cc", ring(90, 81, 4, 9)), ("#2481cc", ring(118, 81, 4, 9))]),
}

for app, (bg, shapes) in ICONS.items():
    out = os.path.join(ROOT, app, "app", "static", "icon.png")
    with open(out, "wb") as f:
        f.write(render(hex_rgb(bg), [(hex_rgb(c), fn) for c, fn in shapes]))
    print(out)
