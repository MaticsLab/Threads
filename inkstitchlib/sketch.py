"""Sketch (redwork) digitizing for photographs and line art.

A photograph never digitizes well as fills: the colours blur into each
other and the fills read as mud. A sketch is what a photo *can* become on
fabric — its edges, drawn in one thread as a bean stitch (each stitch sewn
forward, back, forward, so the line reads as a line):

1. grey + bilateral smoothing (keeps edges, drops grain);
2. Canny edges at the requested detail level;
3. specks dropped, edges skeletonised to 1 px, split into branches;
4. branches smoothed, resampled to the run length, sewn nearest-first.
"""
import numpy as np
import cv2
from skimage.morphology import skeletonize

from digitizer import core, segment
from . import fills


class SketchError(ValueError):
    pass


# Canny low thresholds per detail level 1 (bold outline) .. 5 (fine)
LEVELS = [120, 95, 70, 50, 35]


def sketch(path, width_mm=90.0, detail=3, color='#1A3B69', bean=True, run_len=2.0):
    """-> (pystitch pattern, layers, info)."""
    import pystitch
    rgba = segment.load(path)
    h, w = rgba.shape[:2]
    scale = width_mm / max(1, w)
    detail = int(max(1, min(5, detail)))
    run_len = max(1.0, min(4.0, float(run_len)))

    gray = cv2.cvtColor(rgba[:, :, :3].astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gray[rgba[:, :, 3] < 128] = 255                     # transparent -> paper
    gray = cv2.bilateralFilter(gray, 7, 60, 7)
    lo = LEVELS[detail - 1]
    edges = cv2.Canny(gray, lo, int(lo * 2.5), L2gradient=True)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))

    # drop edge fragments shorter than ~2 mm
    n, lab, st, _ = cv2.connectedComponentsWithStats((edges > 0).astype(np.uint8), 8)
    min_px = max(8, int(round(2.0 / scale)) * 3)
    keep = np.zeros_like(edges)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= min_px:
            keep[lab == i] = 255
    sk = skeletonize(keep > 0).astype(np.uint8) * 255

    lines = []
    for b in core.extract_branches_from(sk):
        pts = np.asarray(b, float)[:, ::-1] * scale        # (y, x) px -> (x, y) mm
        if len(pts) < 2:
            continue
        if len(pts) >= 5:
            pts = core.smooth(pts, 5)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()
        if seg < 1.5:
            continue
        pts = core.resample(pts, run_len)
        if len(pts) >= 2:
            lines.append([tuple(p) for p in pts])
    if not lines:
        raise SketchError('no edges found — try a higher detail level or a sharper picture')

    hexv = str(color).lstrip('#')
    if len(hexv) != 6:
        hexv = '1A3B69'
    v = int(hexv, 16)
    rgb = ((v >> 16) & 255, (v >> 8) & 255, v & 255)

    s = core.Sewer()
    th = pystitch.EmbThread()
    th.color = v
    th.description = 'Sketch'
    s.pattern.add_thread(th)
    for ln in fills.order_polylines(lines, s.pos):
        if bean:
            core.bean_run(s, ln, run_len + 0.1)
        else:
            s.move_to(ln[0], None)
            for q in ln[1:]:
                s.run_to(q, run_len + 0.1)
    s.tie_off()
    s.pattern.end()
    s.pattern.move_center_to_origin()
    layers = [{'name': 'Sketch', 'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb}]
    info = {'lines': len(lines), 'detail': detail, 'bean': bool(bean),
            'length_mm': round(sum(np.linalg.norm(np.diff(np.asarray(l), axis=0), axis=1).sum()
                                   for l in lines), 1)}
    return s.pattern, layers, info
