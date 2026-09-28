"""Render a stitch pattern to a PNG preview with a thread look.

Every stitch is drawn as a strand: a dark edge under it, a body whose
brightness follows the stitch direction against a top-left light (this is
what gives satin its sheen and makes columns of different direction read
differently), and a needle mark at each penetration. A thin highlight then
runs along each stitch run. The image is drawn at 2× and downsampled, so
the strands come out smooth at any size.
"""
import numpy as np
import pystitch
from PIL import Image, ImageDraw

LIGHT_ANG = -np.pi * 0.75          # light from the top-left
THREAD_MM = 0.42                   # rendered strand width


def _shade(col, f):
    return tuple(max(0, min(255, int(round(c * f)))) for c in col)


def preview(pat, colors, out_path, px_wide=1000, bg=None, shade=True, ss=2):
    """bg=None renders on a transparent background so the preview sits on
    whatever the page puts behind it (the canvas grid, a dark thumbnail).
    `shade` is kept for callers; strands are always drawn shaded now."""
    pts = [(s[0], s[1]) for s in pat.stitches]
    if not pts:
        pts = [(0, 0)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    S = px_wide * ss / max(maxx - minx, maxy - miny, 1)
    pad = int(px_wide * ss * 0.03)
    W = int((maxx - minx) * S) + 2 * pad
    H = int((maxy - miny) * S) + 2 * pad
    W, H = max(W, 10 * ss), max(H, 10 * ss)
    img = (Image.new('RGBA', (W, H), (0, 0, 0, 0)) if bg is None
           else Image.new('RGB', (W, H), bg))
    d = ImageDraw.Draw(img)

    def T(x, y):
        return ((x - minx) * S + pad, (y - miny) * S + pad)

    tw = max(2.0, THREAD_MM * S * 10)             # strand width, px (S is per 0.1 mm)
    edge_w = int(round(tw + 2 * ss))
    body_w = int(round(tw))
    hi_w = max(1, int(round(tw * 0.28)))
    dot_r = max(0.9, tw * 0.26)
    n_st = sum(1 for s in pat.stitches if (s[2] & 0xFF) == pystitch.STITCH)
    detailed = n_st <= 220000                      # per-stitch strands; polylines beyond

    ci = 0
    prev = None
    run = []                                       # current run, image coords

    def flush(col):
        if len(run) >= 2:
            hx, hy = -tw * 0.16, -tw * 0.22
            d.line([(x + hx, y + hy) for x, y in run], fill=_shade(col, 1.38),
                   width=hi_w, joint='curve')
        run.clear()

    for x, y, c in pat.stitches:
        k = c & 0xFF
        col = colors[min(ci, len(colors) - 1)]
        if k == pystitch.COLOR_CHANGE:
            flush(col)
            ci += 1
            prev = None
            continue
        if k == pystitch.STITCH:
            p = T(x, y)
            if prev is not None:
                if detailed:
                    ang = np.arctan2(p[1] - prev[1], p[0] - prev[0])
                    sheen = abs(np.sin(ang - LIGHT_ANG))
                    body = _shade(col, 0.80 + 0.34 * sheen)
                    d.line([prev, p], fill=_shade(col, 0.50), width=edge_w)
                    d.line([prev, p], fill=body, width=body_w)
                    d.ellipse([p[0] - dot_r, p[1] - dot_r, p[0] + dot_r, p[1] + dot_r],
                              fill=_shade(col, 0.36))
                run.append(p)
            else:
                flush(col)
                run.append(p)
            prev = p
        elif k == pystitch.JUMP:
            flush(col)
            prev = T(x, y)
        else:
            flush(col)
            prev = None
    if not detailed:
        # big designs: one shaded polyline per run is plenty at preview size
        ci, prev, poly = 0, None, []
        for x, y, c in pat.stitches:
            k = c & 0xFF
            col = colors[min(ci, len(colors) - 1)]
            if k == pystitch.STITCH:
                poly.append(T(x, y))
                continue
            if len(poly) >= 2:
                d.line(poly, fill=_shade(col, 0.5), width=edge_w, joint='curve')
                d.line(poly, fill=col, width=body_w, joint='curve')
            poly = []
            if k == pystitch.COLOR_CHANGE:
                ci += 1
        if len(poly) >= 2:
            col = colors[min(ci, len(colors) - 1)]
            d.line(poly, fill=_shade(col, 0.5), width=edge_w, joint='curve')
            d.line(poly, fill=col, width=body_w, joint='curve')
    else:
        flush(colors[min(ci, len(colors) - 1)])
    if ss > 1:
        img = img.resize((W // ss, H // ss), Image.LANCZOS)
    img.save(out_path)
    return out_path
