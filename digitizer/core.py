"""
StitchForge digitizing core.

Turns a binary layer mask into embroidery stitches: satin columns wherever a
stroke is narrow enough to carry one, tatami fill everywhere else, with
underlay beneath both. Extracted from a working, sewn-and-verified pipeline.
"""
import numpy as np
import cv2
from skimage.morphology import skeletonize
from shapely.geometry import Polygon, MultiPolygon, LineString, Point
from shapely.ops import unary_union
from shapely import affinity
import pystitch

# --- tunables (see params.py / configure) ---------------------------------
ROW_SPACING = 0.35
MAX_STITCH = 3.5
MIN_STITCH = 0.5
UNDERLAY_SPACING = 2.5
TRIM_DIST = 2.0
LOCK = 1.0
SATIN_SPACING = 0.175
MAX_SATIN = 8.0
PULL_COMP = 0.12
UNDERLAY_RUN = 2.5
MIN_BRANCH_MM = 1.2
BLOB_RATIO = 1.3
MIN_FILL_AREA = 0.20


def configure(p):
    """Apply a Params object to the module-level tunables."""
    g = globals()
    for k in ('ROW_SPACING', 'MAX_STITCH', 'MIN_STITCH', 'UNDERLAY_SPACING',
              'TRIM_DIST', 'SATIN_SPACING', 'MAX_SATIN', 'PULL_COMP',
              'MIN_FILL_AREA'):
        if hasattr(p, k.lower()):
            g[k] = getattr(p, k.lower())


def clean(mask, k=7, min_area_px=400):
    m = (mask * 255).astype(np.uint8)
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ker)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
    n, lab, stats, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    out = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area_px:
            out[lab == i] = 255
    return out

def mask_to_polys(m, scale, simplify_mm=0.12):
    """scale = mm per pixel. Returns list of shapely Polygons (with holes)."""
    cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    hier = hier[0]
    polys = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1:            # this is a hole, handled by its parent
            continue
        if len(c) < 4:
            continue
        shell = c[:, 0, :] * scale
        holes = []
        j = hier[i][2]
        while j != -1:
            if len(cnts[j]) >= 4:
                h = cnts[j][:, 0, :] * scale
                if Polygon(h).area > 0.4:      # ignore pinhole artefacts (<0.4 mm^2)
                    holes.append(h)
            j = hier[j][0]
        try:
            p = Polygon(shell, holes)
            if not p.is_valid:
                p = p.buffer(0)
            p = p.simplify(simplify_mm, preserve_topology=True)
            if p.is_empty:
                continue
            if isinstance(p, Polygon):
                p = MultiPolygon([p])
            for q in p.geoms:
                if q.area > 0.6:
                    polys.append(q)
        except Exception:
            pass
    return polys

def scan_segments(poly, angle_deg, spacing, phase=0.0):
    """Parallel scanlines clipped to poly, returned in original coordinates."""
    rp = affinity.rotate(poly, -angle_deg, origin=(0, 0), use_radians=False)
    minx, miny, maxx, maxy = rp.bounds
    segs = []
    y = miny + spacing * 0.5 + phase
    row = 0
    while y < maxy:
        line = LineString([(minx - 5, y), (maxx + 5, y)])
        inter = rp.intersection(line)
        parts = []
        if inter.is_empty:
            pass
        elif inter.geom_type == 'LineString':
            parts = [inter]
        elif inter.geom_type in ('MultiLineString', 'GeometryCollection'):
            parts = [g for g in inter.geoms if g.geom_type == 'LineString']
        for pt in parts:
            if pt.length < 0.35:
                continue
            (x1, y1), (x2, y2) = list(pt.coords)[0], list(pt.coords)[-1]
            segs.append((row, (x1, y1), (x2, y2)))
        y += spacing
        row += 1
    # rotate segment endpoints back
    out = []
    for row, a, b in segs:
        ra = affinity.rotate(Point(a), angle_deg, origin=(0, 0))
        rb = affinity.rotate(Point(b), angle_deg, origin=(0, 0))
        out.append((row, (ra.x, ra.y), (rb.x, rb.y)))
    return out

def order_segments(segs, start=None):
    """Greedy nearest-neighbour chaining; returns ordered [(p0,p1,row), ...]."""
    if not segs:
        return []
    pts = np.array([[s[1], s[2]] for s in segs], dtype=float)   # (n,2,2)
    rows = np.array([s[0] for s in segs])
    n = len(segs)
    used = np.zeros(n, bool)
    # start at the topmost-leftmost segment
    if start is None:
        i0 = int(np.lexsort((pts[:, 0, 0], rows))[0])
    else:
        i0 = int(np.argmin(np.linalg.norm(pts[:, 0, :] - np.array(start), axis=1)))
    order = []
    cur = pts[i0, 0]
    i = i0
    flip = False
    for _ in range(n):
        used[i] = True
        a, b = (pts[i, 1], pts[i, 0]) if flip else (pts[i, 0], pts[i, 1])
        order.append((tuple(a), tuple(b), int(rows[i])))
        cur = b
        rem = np.where(~used)[0]
        if rem.size == 0:
            break
        d0 = np.linalg.norm(pts[rem, 0, :] - cur, axis=1)
        d1 = np.linalg.norm(pts[rem, 1, :] - cur, axis=1)
        k0, k1 = int(np.argmin(d0)), int(np.argmin(d1))
        if d0[k0] <= d1[k1]:
            i, flip = int(rem[k0]), False
        else:
            i, flip = int(rem[k1]), True
    return order

class Sewer:
    def __init__(self):
        self.pattern = pystitch.EmbPattern()
        self.pos = None
        self.prev = None          # point before self.pos, for tie-off direction
        self.count = 0
        self.jumps = 0
        self.trims = 0
        self.pending_tie_in = False

    def _st(self, p):
        self.pattern.add_stitch_absolute(pystitch.STITCH, p[0] * 10.0, p[1] * 10.0)
        self.count += 1
        self.prev = self.pos
        self.pos = p

    def _unit_back(self):
        if self.prev is None or self.pos is None:
            return None
        v = np.array(self.prev, float) - np.array(self.pos, float)
        n = np.linalg.norm(v)
        return None if n < 1e-6 else v / n

    def tie_off(self):
        """Lock stitches before leaving an area, so the thread can't pull out."""
        u = self._unit_back()
        if u is None:
            return
        home = np.array(self.pos, float)
        for _ in range(2):
            q = home + u * LOCK
            self._st((q[0], q[1]))
            self._st((home[0], home[1]))

    def tie_in(self, toward):
        u = np.array(toward, float) - np.array(self.pos, float)
        n = np.linalg.norm(u)
        if n < 1e-6:
            return
        u /= n
        home = np.array(self.pos, float)
        for _ in range(2):
            q = home + u * min(LOCK, n)
            self._st((q[0], q[1]))
            self._st((home[0], home[1]))

    def move_to(self, p, poly=None):
        """Travel to p: walk inside the shape if possible, else jump (+trim)."""
        if self.pos is None:
            self.pattern.add_stitch_absolute(pystitch.JUMP, p[0] * 10.0, p[1] * 10.0)
            self._st(p)
            self.pending_tie_in = True
            return
        d = np.hypot(p[0] - self.pos[0], p[1] - self.pos[1])
        if d < 0.05:
            return
        inside = False
        if poly is not None and d < 45:
            inside = poly.buffer(0.35).covers(LineString([self.pos, p]))
        if inside:
            self.run_to(p, 2.6)
        else:
            self.tie_off()
            if d > TRIM_DIST:
                self.pattern.trim()
                self.trims += 1
            self.pattern.add_stitch_absolute(pystitch.JUMP, p[0] * 10.0, p[1] * 10.0)
            self.jumps += 1
            self._st(p)
            self.pending_tie_in = True

    def run_to(self, p, maxlen):
        if self.pending_tie_in:
            self.pending_tie_in = False
            self.tie_in(p)
        a = np.array(self.pos, float)
        b = np.array(p, float)
        d = np.linalg.norm(b - a)
        if d < MIN_STITCH:
            self._st(p)
            return
        n = max(1, int(np.ceil(d / maxlen)))
        for i in range(1, n + 1):
            q = a + (b - a) * (i / n)
            self._st((q[0], q[1]))

    def color_break(self, thread):
        self.tie_off()
        self.pattern.add_thread(thread)
        self.pattern.color_change()
        self.pos = None
        self.prev = None

def sew_fill(sewer, poly, angle, spacing, maxlen, stagger=True, start=None, travel=None):
    segs = scan_segments(poly, angle, spacing)
    order = order_segments(segs, start)
    tp = travel if travel is not None else poly
    for a, b, row in order:
        sewer.move_to(a, tp)
        if stagger:
            # tatami stagger: shift the first stitch of each row so end-points
            # don't line up into visible ridges
            off = [0.0, 0.5, 0.25, 0.75][row % 4] * maxlen
            v = np.array(b) - np.array(a)
            L = np.linalg.norm(v)
            if L > off > 0.05 and L > MIN_STITCH:
                u = v / L
                sewer.run_to(tuple(np.array(a) + u * off), maxlen)
        sewer.run_to(b, maxlen)
    return order[-1][1] if order else start

def sew_edge_run(sewer, poly, inset=0.7, maxlen=2.5, travel=None):
    """Edge-walk underlay just inside the outline."""
    inner = poly.buffer(-inset)
    if inner.is_empty:
        return
    geoms = inner.geoms if inner.geom_type == 'MultiPolygon' else [inner]
    for g in geoms:
        if g.area < 2.0:
            continue
        rings = [g.exterior] + list(g.interiors)
        for ring in rings:
            cs = list(ring.simplify(0.25).coords)
            if len(cs) < 3:
                continue
            sewer.move_to(cs[0], travel if travel is not None else poly)
            for c in cs[1:]:
                sewer.run_to(c, maxlen)


# --------------------------------------------- skeleton / satin columns
NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def neighbours(pt, S):
    return [(pt[0] + dy, pt[1] + dx) for dy, dx in NB if (pt[0] + dy, pt[1] + dx) in S]


def extract_branches(mask):
    return extract_branches_from(skeletonize(mask > 0).astype(np.uint8) * 255)


def extract_branches_from(sk_img):
    """Return list of pixel paths [(y,x), ...] covering an existing skeleton."""
    S = set(map(tuple, np.argwhere(sk_img > 0)))
    if not S:
        return []
    deg = {p: len(neighbours(p, S)) for p in S}
    nodes = {p for p in S if deg[p] != 2}
    branches = []
    seen = set()

    def walk(start, first):
        path = [start, first]
        seen.add(frozenset((start, first)))
        prev, cur = start, first
        while deg.get(cur, 0) == 2:
            nxt = [q for q in neighbours(cur, S) if q != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if frozenset((prev, cur)) in seen:
                break
            seen.add(frozenset((prev, cur)))
            path.append(cur)
        return path

    for p in nodes:
        for q in neighbours(p, S):
            if frozenset((p, q)) not in seen:
                branches.append(walk(p, q))
    # any leftover pixels form closed loops with no endpoints or junctions
    left = S - {p for b in branches for p in b}
    while left:
        start = next(iter(left))
        nb = neighbours(start, S)
        if not nb:
            left.discard(start)
            continue
        b = walk(start, nb[0])
        branches.append(b)
        left -= set(b)
    return branches


def resample(path_mm, step):
    """Even arc-length resampling of a polyline."""
    p = np.asarray(path_mm, float)
    if len(p) < 2:
        return p
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] < 1e-9:
        return p[:1]
    n = max(2, int(round(s[-1] / step)) + 1)
    t = np.linspace(0, s[-1], n)
    return np.column_stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])])


def smooth(p, k=9):
    if len(p) < k or k < 3:
        return p
    ker = np.ones(k) / k
    out = p.copy()
    out[:, 0] = np.convolve(p[:, 0], ker, mode='same')
    out[:, 1] = np.convolve(p[:, 1], ker, mode='same')
    out[:2], out[-2:] = p[:2], p[-2:]        # don't drag the tips inward
    return out


def _ray(mask, py, px, ny, nx, max_px):
    """March from (py,px) along (ny,nx) until leaving the mask; return px distance."""
    H, W = mask.shape
    t = np.arange(0.0, max_px, 0.75)
    ys = (py + ny * t).astype(int)
    xs = (px + nx * t).astype(int)
    ok = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
    inside = np.zeros(len(t), bool)
    inside[ok] = mask[ys[ok], xs[ok]] > 0
    bad = np.where(~inside)[0]
    return t[bad[0] - 1] if len(bad) and bad[0] > 0 else (t[-1] if inside.all() else 0.0)


def best_crossing(mask, py, px, ny, nx, max_px):
    """Rotate the crossing direction to find the true perpendicular: the
    angle that cuts the stroke on its SHORTEST chord, not a diagonal one."""
    best = None
    for ang in np.deg2rad(np.arange(-40, 41, 5)):
        ca, sa = np.cos(ang), np.sin(ang)
        ay, ax = ny * ca - nx * sa, ny * sa + nx * ca
        a = _ray(mask, py, px, ay, ax, max_px)
        b = _ray(mask, py, px, -ay, -ax, max_px)
        if a + b < 0.5:
            continue
        if best is None or a + b < best[0]:
            best = (a + b, a, b, ay, ax)
    return best


def prune_spurs(comp, scale, rounds=4):
    """Drop skeleton forks at stroke ends. A branch that dead-ends and is
    shorter than ~1.5x the local stroke width is a terminal artefact, not a
    real limb, and satin-ing it makes a fan of crossed stitches."""
    dt = cv2.distanceTransform(comp // 255, cv2.DIST_L2, 5)
    sk = skeletonize(comp > 0)
    S = set(map(tuple, np.argwhere(sk)))
    for _ in range(rounds):
        if not S:
            break
        deg = {p: len(neighbours(p, S)) for p in S}
        tips = [p for p in S if deg[p] == 1]
        drop = set()
        for t in tips:
            path, prev, cur = [t], t, None
            nb = neighbours(t, S)
            if not nb:
                continue
            cur = nb[0]
            while deg.get(cur, 0) == 2:
                path.append(cur)
                nxt = [q for q in neighbours(cur, S) if q != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
            if deg.get(cur, 0) < 3:
                continue                      # whole component, not a spur
            length = len(path) * scale
            width = 2 * np.mean([dt[p] for p in path]) * scale
            if length < 1.5 * max(width, 1.0):
                drop |= set(path)
        if not drop:
            break
        S -= drop
    # second pass: short junction-to-junction branches inside wide areas make
    # crossing columns; drop them and let the surviving spine carry the satin
    for _ in range(2):
        img = np.zeros_like(comp)
        for y, x in S:
            img[y, x] = 255
        drop = set()
        for b in extract_branches_from(img):
            if len(b) < 3:
                continue
            width = 2 * np.mean([dt[p] for p in b]) * scale
            if len(b) * scale < 1.0 * max(width, 1.0):
                drop |= set(b[1:-1])
        if not drop:
            break
        S -= drop
    out = np.zeros_like(comp)
    for y, x in S:
        out[y, x] = 255
    return S, out


# ------------------------------------------------------------ column building
def extend_tips(mask, mm, scale, step=SATIN_SPACING, limit_mm=6.0):
    """Pruning stops the centreline short of the stroke ends; walk each free
    end forward along its own tangent so the satin caps the tip."""
    H, W = mask.shape

    def inside(p):
        y, x = int(p[0] / scale), int(p[1] / scale)
        return 0 <= y < H and 0 <= x < W and mask[y, x] > 0

    def grow(anchor, direction):
        add, p = [], np.array(anchor, float)
        for _ in range(int(limit_mm / step)):
            p = p + direction * step
            if not inside(p):
                break
            add.append(tuple(p))
        return add

    k = min(5, len(mm) - 1)
    d0 = mm[0] - mm[k]
    d0 /= max(np.linalg.norm(d0), 1e-9)
    d1 = mm[-1] - mm[-1 - k]
    d1 /= max(np.linalg.norm(d1), 1e-9)
    head = grow(mm[0], d0)[::-1]
    tail = grow(mm[-1], d1)
    return np.array(head + [tuple(q) for q in mm] + tail)


def satin_column(mask, path_px, scale, max_half_px):
    """-> list of (left_pt_mm, right_pt_mm, centre_mm) sampled along the column."""
    pts = np.asarray(path_px, float)
    if len(pts) < 2:
        return []
    mm = pts * scale
    mm = resample(mm, SATIN_SPACING * 0.25)
    mm = smooth(mm, 9)
    if len(mm) < 2:
        return []
    mm = extend_tips(mask, mm, scale)
    d = np.gradient(mm, axis=0)
    L = np.linalg.norm(d, axis=1)
    L[L < 1e-9] = 1e-9
    d /= L[:, None]
    nrm = np.column_stack([-d[:, 1], d[:, 0]])       # perpendicular
    out = []
    for (cy, cx), (ny, nx) in zip(mm, nrm):
        py, px = cy / scale, cx / scale
        best = best_crossing(mask, py, px, ny, nx, max_half_px)
        if best is None:
            continue
        _, a, b, ny, nx = best
        a = a * scale + PULL_COMP
        b = b * scale + PULL_COMP
        if a + b < 0.3 or a + b > MAX_SATIN:
            out.append(None)      # too wide for satin here -> break the column
            continue
        # emit as (x, y) for the sewer; skeleton space is (y, x)
        left = (cx + nx * a, cy + ny * a)
        right = (cx - nx * b, cy - ny * b)
        out.append((left, right, (cx, cy)))

    # Space the stitches by how far the OUTER edge travels, not the centreline.
    # On a curve the outer edge covers more ground; pacing off the centre is
    # what opens gaps on the outside of a bend.
    segments, run = [], []
    for item in out + [None]:
        if item is None:
            if len(run) >= 3:
                segments.append(run)
            run = []
        else:
            run.append(item)
    picked = []
    for seg in segments:
        sel = [seg[0]]
        for cand in seg[1:]:
            dl = np.hypot(cand[0][0] - sel[-1][0][0], cand[0][1] - sel[-1][0][1])
            dr = np.hypot(cand[1][0] - sel[-1][1][0], cand[1][1] - sel[-1][1][1])
            if max(dl, dr) >= SATIN_SPACING:
                sel.append(cand)
        if sel[-1] is not seg[-1]:
            sel.append(seg[-1])
        if len(sel) >= 3:
            picked.append(sel)
    return picked


def blob_column(poly_mask_pts_mm):
    """For round/blocky pieces: throw satin across the short axis (PCA)."""
    P = np.asarray(poly_mask_pts_mm, float)
    c = P.mean(0)
    u, s, vt = np.linalg.svd(P - c, full_matrices=False)
    major, minor = vt[0], vt[1]
    proj = (P - c) @ major
    lo, hi = proj.min(), proj.max()
    rows = []
    t = lo
    while t <= hi:
        sel = P[np.abs(proj - t) < SATIN_SPACING]
        if len(sel) >= 2:
            m = (sel - c) @ minor
            f = lambda v: (float(v[1]), float(v[0]))   # (y,x) -> (x,y)
            rows.append((f(c + major * t + minor * m.min()),
                         f(c + major * t + minor * m.max()), f(c + major * t)))
        t += SATIN_SPACING
    return rows



def build_columns(comp, scale, max_half_px):
    dt = cv2.distanceTransform(comp // 255, cv2.DIST_L2, 5)
    maxw_mm = 2 * dt.max() * scale
    _, pruned = prune_spurs(comp, scale)
    branches = extract_branches_from(pruned)
    keep = [b for b in branches if len(b) * scale > MIN_BRANCH_MM]
    if not keep:
        keep = [b for b in branches if len(b) >= 2]
    sk_len = sum(len(b) for b in keep) * scale
    if sk_len < BLOB_RATIO * maxw_mm:
        return []                      # blob: the patch fill handles it
    cols = []
    for b in keep:
        cols.extend(satin_column(comp, b, scale, max_half_px))
    return cols


def order_columns(cols, start=None):
    """Nearest-neighbour over columns, sewing each in whichever direction
    starts closest so the needle never doubles back across the design."""
    used = [False] * len(cols)
    cur, out = start, []
    for _ in range(len(cols)):
        best, bd, flip = -1, 1e18, False
        for i, c in enumerate(cols):
            if used[i]:
                continue
            a, z = np.array(c[0][2]), np.array(c[-1][2])
            if cur is None:
                da = db = 0.0
            else:
                da = float(np.linalg.norm(a - np.array(cur)))
                db = float(np.linalg.norm(z - np.array(cur)))
            if min(da, db) < bd:
                bd, best, flip = min(da, db), i, db < da
        if best < 0:
            break
        used[best] = True
        c = cols[best][::-1] if flip else cols[best]
        out.append(c)
        cur = c[-1][2]
    return out



def order_by_nearest(items, keyfn, start=None):
    used = [False] * len(items)
    cur, out = start, []
    for _ in range(len(items)):
        best, bd = -1, 1e18
        for i, it in enumerate(items):
            if used[i]:
                continue
            k = keyfn(it)
            d = 0.0 if cur is None else float(np.hypot(k[0] - cur[0], k[1] - cur[1]))
            if d < bd:
                bd, best = d, i
        if best < 0:
            break
        used[best] = True
        out.append(items[best])
        cur = keyfn(items[best])
    return out



def sew_column(s, col, travel):
    """centre run -> zigzag underlay -> satin, travelling inside the shape."""
    s.move_to(tuple(col[0][2]), travel)
    step = max(1, int(round(UNDERLAY_RUN / SATIN_SPACING)))
    for _, _, c3 in col[::step]:
        s.run_to(tuple(c3), UNDERLAY_RUN)

    zstep = max(1, int(round(2.0 / SATIN_SPACING)))
    zi = list(range(0, len(col), zstep))
    if zi and zi[-1] != len(col) - 1:
        zi.append(len(col) - 1)
    z = 0
    for i in zi[::-1]:
        l, r, c3 = col[i]
        mid = np.array(c3, float)
        edge = np.array(l if z == 0 else r, float)
        p = mid + (edge - mid) * 0.72
        s.run_to((p[0], p[1]), 4.0)
        z ^= 1

    s.move_to(tuple(col[0][2]), travel)
    side = 0
    for left, right, _ in col:
        s.run_to(tuple(left if side == 0 else right), MAX_SATIN + 1)
        side ^= 1


