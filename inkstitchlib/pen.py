"""Manual digitizing: build a pattern from shapes traced with the pen tool.

The UI sends shapes drawn over a dimmed reference image. Three stitch modes,
mirroring how manual punching works:

* pairs  - the classic satin column: points clicked alternately on the two
           rails (L, R, L, R, ...); the needle zigzags between the rails,
           subdivided to the requested density, over a centre-run underlay;
* center - a satin bead along a clicked centreline at a fixed width;
* run    - a running stitch along the clicked path.

Points arrive in mm. Consecutive shapes with the same colour share a colour
block, like Ink/Stitch does with adjacent same-colour elements.
"""
import numpy as np

from digitizer import core


class PenError(ValueError):
    pass


def _resample(points, step):
    p = np.asarray(points, float)
    if len(p) < 2:
        return [tuple(q) for q in p]
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] < 1e-9:
        return [tuple(p[0])]
    n = max(1, int(round(s[-1] / max(step, 0.1))))
    t = np.linspace(0, s[-1], n + 1)
    return list(zip(np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])))


def pair_zigzag(points, spacing):
    """Points alternate left/right rail -> satin stitch points."""
    if len(points) < 4:
        return [], []
    pts = points[: len(points) - (len(points) % 2)]
    L = np.asarray(pts[0::2], float)
    R = np.asarray(pts[1::2], float)
    out, mids = [], []
    side = 0
    for i in range(len(L) - 1):
        span = max(np.linalg.norm(L[i + 1] - L[i]), np.linalg.norm(R[i + 1] - R[i]))
        n = max(1, int(round(span / max(spacing, 0.1))))
        last = (i == len(L) - 2)
        for k in range(n + (1 if last else 0)):
            t = k / n
            a = L[i] + (L[i + 1] - L[i]) * t
            b = R[i] + (R[i + 1] - R[i]) * t
            out.append(tuple(a if side == 0 else b))
            mids.append(tuple((a + b) / 2))
            side ^= 1
    return out, mids


def center_zigzag(points, width, spacing):
    """Satin bead along a centreline at fixed width."""
    pts = _resample(points, max(spacing, 0.1))
    if len(pts) < 2:
        return [], []
    p = np.asarray(pts, float)
    d = np.gradient(p, axis=0)
    n = np.linalg.norm(d, axis=1)
    n[n < 1e-9] = 1e-9
    nrm = np.column_stack([-d[:, 1] / n, d[:, 0] / n]) * (width / 2)
    out, mids = [], []
    side = 1.0
    for q, nq in zip(p, nrm):
        out.append((q[0] + nq[0] * side, q[1] + nq[1] * side))
        mids.append((q[0], q[1]))
        side = -side
    return out, mids


def build(shapes):
    """-> (pattern via Sewer, layers). Shapes: [{mode,color,points,width_mm,
    spacing_mm,run_len_mm}], points in mm."""
    import pystitch

    s = core.Sewer()
    block_colors = []
    prev_rgb = None
    drawn = 0

    def start_block(rgb):
        nonlocal prev_rgb
        if rgb == prev_rgb:
            return
        th = pystitch.EmbThread()
        th.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        th.description = 'Pen %d' % (len(block_colors) + 1)
        if prev_rgb is None:
            s.pattern.add_thread(th)
        else:
            s.color_break(th)
        block_colors.append(rgb)
        prev_rgb = rgb

    for shape in shapes:
        mode = shape.get('mode', 'pairs')
        pts = [(float(p[0]), float(p[1])) for p in shape.get('points', [])]
        hexv = str(shape.get('color', '#1A3B69')).lstrip('#')
        if len(hexv) != 6:
            hexv = '1A3B69'
        v = int(hexv, 16)
        rgb = ((v >> 16) & 255, (v >> 8) & 255, v & 255)
        spacing = max(0.25, min(3.0, float(shape.get('spacing_mm', 0.4) or 0.4)))
        width = max(0.6, min(12.0, float(shape.get('width_mm', 3.0) or 3.0)))
        run_len = max(0.8, min(6.0, float(shape.get('run_len_mm', 2.5) or 2.5)))

        if mode == 'run':
            if len(pts) < 2:
                continue
            start_block(rgb)
            path = _resample(pts, run_len)
            s.move_to(path[0], None)
            for p in path[1:]:
                s.run_to(p, run_len + 0.1)
            drawn += 1
            continue

        zz, mids = (pair_zigzag(pts, spacing) if mode == 'pairs'
                    else center_zigzag(pts, width, spacing))
        if len(zz) < 4:
            continue
        start_block(rgb)
        under = _resample(mids, 2.5)
        s.move_to(under[0], None)
        for p in under:
            s.run_to(p, 2.5)
        for p in reversed(under):
            s.run_to(p, 2.5)
        for p in zz:
            s._st(p)
        drawn += 1

    if drawn == 0 or s.count == 0:
        raise PenError('no stitchable shapes — a satin column needs at least '
                       '4 points (2 per rail), a run needs 2')
    s.tie_off()
    s.pattern.end()
    s.pattern.move_center_to_origin()

    layers = [{'name': 'Pen %d' % (i + 1),
               'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb}
              for i, rgb in enumerate(block_colors)]
    return s.pattern, layers
