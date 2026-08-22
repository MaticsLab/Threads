"""Stitch density map, after Ink/Stitch's "density map" extension.

Every needle penetration is counted against its neighbourhood (points within
~0.5 mm); the design is drawn in light grey with each penetration coloured
green / yellow / red by how crowded it is. Too-dense areas are where needles
break and fabric puckers.
"""
import numpy as np
import cv2
from PIL import Image, ImageDraw

import pystitch

# thresholds: neighbours within the window (Ink/Stitch flags ~ >=6 yellow,
# >=12 red for 0.5mm radius)
YELLOW_AT = 6
RED_AT = 12
MM_PER_CELL = 0.25          # histogram resolution
WINDOW_MM = 1.0             # neighbourhood window


def render(pattern, out_path, px_per_mm=8):
    pts = []
    prev = None
    for x, y, c in pattern.stitches:
        k = c & 0xFF
        if k == pystitch.STITCH:
            pts.append((x / 10.0, y / 10.0))
            prev = (x, y)
        elif k == pystitch.JUMP:
            prev = (x, y)
    if not pts:
        raise ValueError('no stitches to map')
    P = np.asarray(pts)
    minx, miny = P.min(0) - 2
    maxx, maxy = P.max(0) + 2

    # neighbour counts on a coarse grid
    gw = max(2, int((maxx - minx) / MM_PER_CELL))
    gh = max(2, int((maxy - miny) / MM_PER_CELL))
    xi = np.clip(((P[:, 0] - minx) / MM_PER_CELL).astype(int), 0, gw - 1)
    yi = np.clip(((P[:, 1] - miny) / MM_PER_CELL).astype(int), 0, gh - 1)
    acc = np.zeros((gh, gw), np.float32)
    np.add.at(acc, (yi, xi), 1)
    k = max(1, int(round(WINDOW_MM / MM_PER_CELL)) | 1)
    neigh = cv2.boxFilter(acc, -1, (k, k), normalize=False)
    counts = neigh[yi, xi]

    # draw: light grey threads underneath, coloured penetrations on top
    W = max(10, int((maxx - minx) * px_per_mm))
    H = max(10, int((maxy - miny) * px_per_mm))
    img = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    T = lambda x, y: ((x - minx) * px_per_mm, (y - miny) * px_per_mm)

    prev = None
    for x, y, c in pattern.stitches:
        k2 = c & 0xFF
        if k2 == pystitch.STITCH:
            if prev is not None:
                d.line([T(*prev), T(x / 10.0, y / 10.0)], fill=(208, 210, 214), width=2)
            prev = (x / 10.0, y / 10.0)
        elif k2 == pystitch.JUMP:
            prev = (x / 10.0, y / 10.0)
        else:
            prev = None

    r = max(1, px_per_mm // 5)
    for (x, y), n in zip(pts, counts):
        if n >= RED_AT:
            col = (200, 32, 32)
        elif n >= YELLOW_AT:
            col = (222, 168, 24)
        else:
            col = (46, 148, 82)
        cx, cy = T(x, y)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)

    img.save(out_path)
    stats = {'penetrations': len(pts),
             'red': int((counts >= RED_AT).sum()),
             'yellow': int(((counts >= YELLOW_AT) & (counts < RED_AT)).sum())}
    return out_path, stats
