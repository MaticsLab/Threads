"""Fill methods beyond tatami, after Ink/Stitch's fill family.

* contour fill - rows follow the shape's outline, stepping inward ring by
  ring (Ink/Stitch "contour fill", inner-to-outer strategy);
* circular fill - concentric circles about the shape's bounding-box centre,
  clipped to the shape (Ink/Stitch "circular fill").

Both return polylines in mm; sew_area() dispatches by method name and sews
through the shared Sewer so travel/tie behaviour matches the tatami path.
"""
import numpy as np
from shapely.geometry import Point, Polygon

from digitizer import core


def _resample(coords, step):
    pts = core.resample(np.asarray(coords, float), step)
    return [tuple(p) for p in pts]


def _geoms(g):
    if g.is_empty:
        return []
    if g.geom_type == 'Polygon':
        return [g]
    if g.geom_type in ('MultiPolygon', 'GeometryCollection'):
        return [q for q in g.geoms if q.geom_type == 'Polygon']
    return []


def _lines(g):
    if g.is_empty:
        return []
    if g.geom_type == 'LineString':
        return [g]
    if g.geom_type in ('MultiLineString', 'GeometryCollection'):
        return [q for q in g.geoms if q.geom_type == 'LineString']
    return []


def order_polylines(lines, start=None):
    """Greedy nearest-neighbour over polylines, flipping when the far end
    is closer — same idea as core.order_segments but for whole polylines."""
    used = [False] * len(lines)
    cur, out = start, []
    for _ in range(len(lines)):
        best, bd, flip = -1, 1e18, False
        for i, ln in enumerate(lines):
            if used[i]:
                continue
            a, z = np.asarray(ln[0]), np.asarray(ln[-1])
            if cur is None:
                da = db = 0.0
            else:
                da = float(np.linalg.norm(a - np.asarray(cur)))
                db = float(np.linalg.norm(z - np.asarray(cur)))
            if min(da, db) < bd:
                bd, best, flip = min(da, db), i, db < da
        if best < 0:
            break
        used[best] = True
        ln = lines[best][::-1] if flip else lines[best]
        out.append(ln)
        cur = ln[-1]
    return out


def contour_rings(poly, spacing, step=1.8):
    """Inward offset rings of the shape, outermost first."""
    rings = []
    off = spacing * 0.55
    while True:
        inner = poly.buffer(-off)
        gs = _geoms(inner)
        if not gs:
            break
        for g in gs:
            if g.area < 0.05:
                continue
            for ring in [g.exterior] + list(g.interiors):
                cs = list(ring.coords)
                if len(cs) >= 4:
                    rings.append(_resample(cs, step))
        off += spacing
    return rings


def circular_rings(poly, spacing, step=1.8):
    """Concentric circles about the bounding-box centre, clipped to poly."""
    minx, miny, maxx, maxy = poly.bounds
    c = ((minx + maxx) / 2, (miny + maxy) / 2)
    rmax = float(np.hypot(maxx - minx, maxy - miny) / 2) + spacing
    lines = []
    r = spacing * 0.55
    centre = Point(c)
    while r <= rmax:
        circle = centre.buffer(r, quad_segs=max(12, int(r * 8)))
        for ln in _lines(circle.exterior.intersection(poly)):
            cs = list(ln.coords)
            if len(cs) >= 2 and ln.length > 0.3:
                lines.append(_resample(cs, step))
        r += spacing
    return lines


def sew_polylines(sewer, lines, travel, max_stitch):
    for ln in order_polylines(lines, sewer.pos):
        sewer.move_to(ln[0], travel)
        for p in ln[1:]:
            sewer.run_to(p, max_stitch)


def sew_area(sewer, poly, method, angle, spacing, max_stitch,
             stagger=True, travel=None):
    """Top fill dispatch: tatami (scanline), contour, or circular."""
    if method == 'contour':
        rings = contour_rings(poly, spacing, step=min(1.8, max_stitch))
        if rings:
            sew_polylines(sewer, rings, travel, max_stitch)
            return
    elif method == 'circular':
        lines = circular_rings(poly, spacing, step=min(1.8, max_stitch))
        if lines:
            sew_polylines(sewer, lines, travel, max_stitch)
            return
    core.sew_fill(sewer, poly, angle, spacing, max_stitch,
                  stagger=stagger, start=sewer.pos, travel=travel)
