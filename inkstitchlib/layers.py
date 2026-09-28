"""AI layer extraction: image -> editable vector layers -> stitches on demand.

vectorize() looks at the artwork and builds SVG-style path layers the way a
digitizer would organise a logo:

* colours are separated first;
* within a colour, every connected region becomes a shape — so a ring of
  linked arms is ONE object, and each letter is its own object;
* similar round shapes (heads, dots) are grouped into a single layer;
* each large shape gets its own layer, small left-overs share a "details"
  layer.

The result is data, not stitches: the client edits layer order, colours and
stitch parameters, and only stitch() turns the arrangement into a pattern
(and therefore a DST/PES/... on export).
"""
import numpy as np
import cv2
from shapely.geometry import Polygon, MultiPolygon, LineString

from digitizer import segment, core
from . import fills, patterns

DEFAULT_PARAMS = {'stitch': 'auto', 'fill_method': 'tatami',
                  'angle': 'auto', 'density': 0.4, 'border_mm': 1.0}


class LayerError(ValueError):
    pass


def _poly_data(p, nd=2):
    return {'shell': [[round(x, nd), round(y, nd)] for x, y in p.exterior.coords],
            'holes': [[[round(x, nd), round(y, nd)] for x, y in ring.coords]
                      for ring in p.interiors]}


def vectorize(path, n_colors=4, width_mm=90.0):
    """-> (layers, width_mm, height_mm); layer polys in mm."""
    rgba = segment.load(path)
    qlayers = segment.quantize(rgba, n_colors)
    scale = width_mm / rgba.shape[1]
    height_mm = rgba.shape[0] * scale

    out = []
    lid = 0

    def emit(comps, name, color_hex, rgb):
        nonlocal lid
        polys = [p for c in comps for p in c['polys']]
        if not polys:
            return
        lid += 1
        out.append({'id': lid, 'name': name, 'color': color_hex, 'rgb': list(rgb),
                    'visible': True, 'params': dict(DEFAULT_PARAMS),
                    'polys': [_poly_data(p) for p in polys]})

    for L in qlayers:
        k = max(3, int(round(0.2 / scale)) | 1)
        m = core.clean(L['mask'], k=k,
                       min_area_px=max(40, int(round(0.3 / (scale * scale)))))
        n, lab = cv2.connectedComponents((m > 0).astype(np.uint8))
        comps = []
        for c in range(1, n):
            cm = ((lab == c).astype(np.uint8)) * 255
            polys = core.mask_to_polys(cm, scale, simplify_mm=0.15)
            if not polys:
                continue
            area = sum(p.area for p in polys)
            per = sum(p.exterior.length for p in polys)
            circ = 4 * np.pi * area / max(per * per, 1e-9)
            comps.append({'polys': polys, 'area': area, 'circ': circ})

        rounds = [c for c in comps if c['circ'] > 0.72]
        bigs = [c for c in comps if c['circ'] <= 0.72 and c['area'] >= 25.0]
        smalls = [c for c in comps if c['circ'] <= 0.72 and c['area'] < 25.0]

        base = L['name']
        for i, c in enumerate(sorted(bigs, key=lambda c: -c['area'])):
            emit([c], '%s · shape %d' % (base, i + 1), L['hex'], L['rgb'])
        if rounds:
            emit(rounds, '%s · round ×%d' % (base, len(rounds)), L['hex'], L['rgb'])
        if smalls:
            emit(smalls, '%s · details' % base, L['hex'], L['rgb'])

    if not out:
        raise LayerError('no shapes found once the background was removed')
    return out, width_mm, height_mm


def _poly_from_data(d):
    try:
        shell = d.get('shell') or []
        if len(shell) < 3:
            return None
        p = Polygon(shell, [h for h in (d.get('holes') or []) if len(h) >= 3])
        if not p.is_valid:
            p = p.buffer(0)
        return None if p.is_empty else p
    except Exception:
        return None


DECOR_TYPES = ('estitch', 'triangle', 'cross', 'motif')     # decorative runs
LINE_TYPES = ('run', 'bean', 'satin') + DECOR_TYPES
STITCH_TYPES = ('auto', 'fill', 'outline', 'run', 'bean', 'satin', 'applique', 'puff') + DECOR_TYPES
FILL_METHODS = ('tatami', 'contour', 'circular', 'walk', 'satin') + tuple(patterns.PATTERN_FILLS)


def _normals(path):
    p = np.asarray(path, float)
    d = np.gradient(p, axis=0)
    n = np.linalg.norm(d, axis=1)
    n[n < 1e-9] = 1e-9
    return np.column_stack([-d[:, 1] / n, d[:, 0] / n])


def decor_points(path, width, kind):
    """A decorative run along a resampled path -> stitch points.

    estitch  - a comb: along the line, out to one side and back at every step
    triangle - open triangles: point, apex above the next mid-point, point
    cross    - X's: a zigzag out and back on the opposite diagonal
    motif    - a chain of diamonds, one per step
    """
    p = np.asarray(path, float)
    if len(p) < 2:
        return [tuple(q) for q in p]
    nrm = _normals(p)
    half = width / 2.0
    out = []

    def mid_normal(i):
        nm = nrm[i] + nrm[i + 1]
        nm /= max(np.linalg.norm(nm), 1e-9)
        return (p[i] + p[i + 1]) / 2.0, nm

    if kind == 'estitch':
        for i in range(len(p)):
            out += [tuple(p[i]), tuple(p[i] + nrm[i] * width), tuple(p[i])]
    elif kind == 'triangle':
        for i in range(len(p) - 1):
            mid, nm = mid_normal(i)
            out += [tuple(p[i]), tuple(mid + nm * width)]
        out.append(tuple(p[-1]))
    elif kind == 'cross':
        fwd = [tuple(p[i] + nrm[i] * half * (1 if i % 2 == 0 else -1)) for i in range(len(p))]
        back = [tuple(p[i] + nrm[i] * half * (-1 if i % 2 == 0 else 1)) for i in range(len(p) - 1, -1, -1)]
        out = fwd + back
    elif kind == 'motif':
        for i in range(len(p) - 1):
            mid, nm = mid_normal(i)
            out += [tuple(p[i]), tuple(mid + nm * half), tuple(p[i + 1]), tuple(mid - nm * half), tuple(p[i + 1])]
    else:
        out = [tuple(q) for q in p]
    return out


UNDERLAY_KINDS = ('center', 'contour', 'zigzag')


def _underlay_list(v):
    """Explicit underlays [{type, len_mm}] (None: the underlay mode decides)."""
    if not isinstance(v, list):
        return None
    out = []
    for u in v[:4]:
        if not isinstance(u, dict) or u.get('type') not in UNDERLAY_KINDS:
            continue
        try:
            ln = max(0.8, min(6.0, float(u.get('len_mm') or 1.5)))
        except (TypeError, ValueError):
            ln = 1.5
        out.append({'type': u['type'], 'len': ln})
    return out


def _sew_underlays(s, it, g, ang, travel, satin_path=None):
    """The object's own underlay list: centre run, contour (edge walk) or
    zigzag, in order, before its top stitching."""
    for u in it['underlays']:
        if u['type'] == 'contour':
            core.sew_edge_run(s, g, inset=0.7, maxlen=u['len'], travel=travel)
        elif u['type'] == 'center':
            if satin_path is not None:
                s.move_to(satin_path[0], travel)
                for p in satin_path:
                    s.run_to(p, u['len'])
            else:
                core.sew_fill(s, g, ang + 90, core.UNDERLAY_SPACING, u['len'], stagger=False,
                              start=s.pos, travel=travel)
        else:   # zigzag
            if satin_path is not None:
                zz, _m = pen_zigzag(satin_path, min(it['width'] * 0.7, 6.0), 2.0)
                if zz:
                    s.move_to(zz[0], travel)
                    for p in zz:
                        s.run_to(p, 6.0)
            else:
                for a in (ang + 45, ang - 45):
                    core.sew_fill(s, g, a, core.UNDERLAY_SPACING, u['len'], stagger=False,
                                  start=s.pos, travel=travel)


def pen_zigzag(path, width, spacing):
    from . import pen
    return pen.center_zigzag(path, width, spacing)


def _read_layer(L, max_satin):
    prm = {**DEFAULT_PARAMS, **(L.get('params') or {})}
    density = max(0.25, min(3.0, float(prm.get('density') or 0.4)))
    method = prm.get('fill_method')
    if method not in FILL_METHODS:
        method = 'tatami'
    stype = prm.get('stitch')
    if stype not in STITCH_TYPES:
        stype = 'auto'
    border = max(0.5, min(3.0, float(prm.get('border_mm') or 1.0)))
    hexv = str(L.get('color', '#1A3B69')).lstrip('#')
    if len(hexv) != 6:
        hexv = '1A3B69'
    v = int(hexv, 16)
    rgb = ((v >> 16) & 255, (v >> 8) & 255, v & 255)
    ang = prm.get('angle')
    ang = None if ang in (None, '', 'auto') else float(ang)
    width = max(0.6, min(12.0, float(prm.get('width_mm') or 3.0)))
    run_len = max(0.8, min(6.0, float(prm.get('run_len_mm') or 2.5)))

    def num(key, lo, hi, default):
        try:
            v = prm.get(key)
            return default if v in (None, '') else max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default

    def flag(key, default):
        v = prm.get(key)
        return default if v is None else bool(v)

    umode = prm.get('underlay')
    umode = umode if umode in core.UNDERLAY_MODES else None      # None: the design setting
    split = flag('split', True)
    info = {'density': density, 'method': method, 'type': stype, 'border': border,
            'rgb': rgb, 'angle': ang, 'name': (L.get('name') or 'Layer')[:48],
            'max_satin': max_satin, 'width': width, 'run_len': run_len,
            # per-object refinements (the reference panel's Fill Settings /
            # Density Control / Underlays / Split Satin)
            'stitch_len': num('stitch_len_mm', 1.0, 7.0, 3.5),
            'pull_comp': num('pull_comp_mm', 0.0, 1.0, None),
            'hand': num('hand', 0.0, 1.0, 0.0),
            'underpath': flag('underpath', True),
            'row_short': flag('row_short', True),
            'density_trigger': num('density_trigger', 0.2, 1.0, 0.5),
            'underlay': umode,
            'split': split,
            'split_max': num('split_max_mm', 3.0, 12.0, 7.0),
            'stagger': flag('stagger', True),
            'cycles': int(num('cycles', 2, 8, 4)),
            'amount': num('amount_mm', 0.0, 1.0, 0.3),
            # gradient fill: row spacing grades from density to gradient_to
            'gradient': flag('gradient', False),
            'gradient_to': num('gradient_to_mm', 0.3, 4.0, 1.5),
            'gradient_flip': flag('gradient_flip', False),
            # stitch options
            'tie_on': flag('tie_on', True), 'tie_off': flag('tie_off', True),
            'trim_after': flag('trim_after', False),
            'speed': prm.get('speed') if prm.get('speed') in ('slow', 'fast') else '',
            'short_frac': num('short_frac', 0.0, 1.0, 0.45),
            'underlays': _underlay_list(prm.get('underlays')),
            'id': str(L.get('id') or '')}
    if split:
        # split satin lets a column run wider than one stitch can span
        info['max_satin'] = max(max_satin, min(12.0, info['split_max'] * 1.6))
    return info


def stitch(layers_in, max_satin=8.0, settings=None):
    """Sew the arranged layers, one uniform treatment per object.

    Objects are planned into colour blocks first (core.plan_blocks): a
    layer's objects join an earlier block of the same colour whenever
    nothing sewn in between would cover them, so a design with the same
    thread in several layers still sews with the fewest colour stops.
    Each block sews all of its underlay, then all of its top stitching.
    Appliqué layers keep their own block and sew placement line → stop →
    tack-down → stop → satin border, for every piece in the layer at once.

    settings: {underlay, pull_comp, min_satin, knockdown, cap_mode}.
    """
    import pystitch
    from shapely.ops import unary_union

    settings = settings or {}
    STITCH_COUNTS.clear()
    core.set_tunables(underlay=settings.get('underlay'),
                      pull_comp=settings.get('pull_comp'),
                      min_satin=settings.get('min_satin'))

    pieces = []                                   # (key, geom, item)
    for L in layers_in:
        if not L.get('visible', True):
            continue
        info = _read_layer(L, max_satin)
        polys = [q for q in (_poly_from_data(d) for d in L.get('polys') or []) if q]
        flat = []
        for p in polys:
            flat.extend(p.geoms if isinstance(p, MultiPolygon) else [p])
        flat = [q for q in flat if q.area > 0.3]
        # open paths (drawn shapes): running / bean / satin along the line
        lines = []
        for ln in L.get('lines') or []:
            pts = [(float(p[0]), float(p[1])) for p in (ln.get('points') or []) if len(p) >= 2]
            if len(pts) >= 2 and LineString(pts).length > 0.5:
                lines.append(pts)
        if not flat and not lines:
            continue
        key = info['rgb'] if info['type'] != 'applique' else (info['rgb'], 'applique', id(L))
        for g in flat:
            pieces.append((key, g, dict(info, g=g)))
        ltype = info['type'] if info['type'] in LINE_TYPES else 'run'
        for pts in lines:
            foot = LineString(pts).buffer(max(0.6, info['width'] if ltype == 'satin' else 0.6) / 2)
            pieces.append((info['rgb'], foot, dict(info, g=foot, line=pts, type=ltype)))
    if not pieces:
        raise LayerError('nothing to stitch — every layer is hidden or empty')

    s = core.Sewer()
    block_layers = []

    def start_block(rgb, name):
        th = pystitch.EmbThread()
        th.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        th.description = name
        if not block_layers:
            s.pattern.add_thread(th)
        else:
            s.color_break(th)
        block_layers.append({'name': name, 'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb})

    everything = unary_union([g for _k, g, _it in pieces])
    centre_x = everything.centroid.x
    keyfn = lambda it: (it['g'].centroid.x, it['g'].centroid.y)

    def order(items):
        if settings.get('cap_mode'):
            return core.cap_order(items, keyfn, centre_x)
        return core.order_by_nearest(items, keyfn, s.pos)

    if settings.get('knockdown'):
        start_block(pieces[0][2]['rgb'], 'Knockdown')
        core.sew_knockdown(s, everything)

    for key, items in core.plan_blocks(pieces):
        rgb = items[0]['rgb']
        start_block(rgb, items[0]['name'])
        if items[0]['type'] == 'applique':
            _sew_applique_block(s, order(items))
            continue
        for phase in (core.UNDER, core.TOP):
            if phase == core.TOP:
                s.region_clear()
                for it in items:
                    s.region_add(it['g'])
            for it in order(items):
                _sew_one(s, it, phase)
                if phase == core.TOP:
                    s.region_subtract(it['g'])
        s.region_clear()

    if s.count == 0:
        raise LayerError('nothing to stitch — every layer is hidden or empty')
    s.tie_off()
    s.pattern.end()
    xs = [q[0] for q in s.pattern.stitches]
    ys = [q[1] for q in s.pattern.stitches]
    s.pattern.extras['origin_mm'] = [round(float(min(xs) + max(xs)) / 20.0, 3),
                                     round(float(min(ys) + max(ys)) / 20.0, 3)]
    s.pattern.move_center_to_origin()
    for BL in block_layers:
        BL['rgb'] = tuple(BL['rgb'])
    return s.pattern, block_layers


def _sew_line(s, it, phase):
    """An open path: running stitch, bean stitch, or centre-line satin."""
    from . import pen
    pts, stype = it['line'], it['type']
    if stype in DECOR_TYPES:
        if phase == core.UNDER:
            return
        path = pen._resample(pts, it['run_len'])
        if len(path) < 2:
            return
        dp = decor_points(path, it['width'], stype)
        s.move_to(dp[0], None)
        for q in dp[1:]:
            s.run_to(q, max(it['width'], it['run_len']) + 0.6)
        return
    if stype in ('run', 'bean'):
        if phase == core.UNDER:
            return
        path = pen._resample(pts, it['run_len'])
        if stype == 'bean':
            core.bean_run(s, path, it['run_len'] + 0.1)
        else:
            s.move_to(path[0], None)
            for p in path[1:]:
                s.run_to(p, it['run_len'] + 0.1)
        return
    zz, mids = pen.center_zigzag(pts, it['width'], max(0.25, min(3.0, it['density'])))
    if len(zz) < 4:
        return
    under = pen._resample(mids, 2.5)
    if it.get('underlays') is not None and phase != core.TOP and it['width'] >= core.MIN_SATIN:
        _sew_underlays(s, it, LineString(pts).buffer(it['width'] / 2), 0.0, None, satin_path=under)
    if it['width'] < core.MIN_SATIN:
        if phase != core.UNDER:
            core.bean_run(s, under, 2.0)
        return
    umode = core.underlay_mode()
    if phase != core.TOP and umode != 'none':
        s.move_to(under[0], None)
        for p in under:
            s.run_to(p, 2.5)
        if umode != 'light' and it['width'] >= 2.0:
            for p in reversed(under):
                s.run_to(p, 2.5)
    if phase != core.UNDER:
        zz = core.shorten_zigzag(core.widen_zigzag(zz), mids)
        s.move_to(zz[0], None)
        for i, p in enumerate(zz):
            core.satin_to(s, p, i)


_OBJ_TUNABLES = ('PULL_COMP', 'UNDERLAY', 'ROW_SHORT', 'DENSITY_TRIGGER',
                 'SPLIT_SATIN', 'SPLIT_STAGGER', 'SPLIT_CYCLES', 'SPLIT_AMOUNT',
                 'TIE_IN', 'TIE_OFF', 'SHORT_FRAC')


def _hand_jitter(s, n0, amount, seed):
    """Hand-stitch look: nudge every needle point sewn since n0 by up to
    `amount` mm, the same way each time for the same object."""
    import pystitch
    rng = np.random.default_rng(seed)
    st = s.pattern.stitches
    for k in range(n0, len(st)):
        if (int(st[k][2]) & 0xFF) == pystitch.STITCH:
            st[k][0] += rng.uniform(-amount, amount) * 10.0
            st[k][1] += rng.uniform(-amount, amount) * 10.0


def _sew_one(s, it, phase):
    """Sew one object with its own refinements applied to the engine for
    the duration, then put the design-wide settings back."""
    saved = {k: getattr(core, k) for k in _OBJ_TUNABLES}
    core.set_tunables(pull_comp=it.get('pull_comp'),
                      underlay='none' if it.get('underlays') is not None else it.get('underlay'),
                      row_short=it.get('row_short'), density_trigger=it.get('density_trigger'),
                      split_satin=(it.get('split_max') if it.get('split') else 0.0),
                      split_stagger=it.get('stagger'), split_cycles=it.get('cycles'),
                      split_amount=it.get('amount'),
                      tie_in=it.get('tie_on'), tie_off=it.get('tie_off'),
                      short_frac=it.get('short_frac'))
    n0 = len(s.pattern.stitches)
    if it.get('speed') and phase != core.UNDER and s.pos is not None:
        import pystitch
        cmd = pystitch.SLOW if it['speed'] == 'slow' else pystitch.FAST
        s.pattern.add_stitch_absolute(cmd, s.pos[0] * 10.0, s.pos[1] * 10.0)
    try:
        _sew_one_inner(s, it, phase)
    finally:
        core.set_tunables(**{k.lower(): v for k, v in saved.items()})
    if it.get('hand') and phase != core.UNDER:
        _hand_jitter(s, n0, it['hand'] * 0.45, seed=int(abs(hash(it['name'])) % 100000 + n0))
    if it.get('trim_after') and phase != core.UNDER:
        s.cut_thread()
    n1 = sum(1 for k in range(n0, len(s.pattern.stitches)) if (int(s.pattern.stitches[k][2]) & 0xFF) == 0)
    if it.get('id'):
        STITCH_COUNTS[it['id']] = STITCH_COUNTS.get(it['id'], 0) + n1


STITCH_COUNTS = {}          # layer id -> stitches, for the last stitch() call


def _sew_one_inner(s, it, phase):
    if it.get('line') is not None:
        return _sew_line(s, it, phase)
    g, stype = it['g'], it['type']
    density, border, method = it['density'], it['border'], it['method']
    travel = g if it.get('underpath', True) else None
    if phase == core.TOP and travel is None:
        s.region_subtract(g)                  # no underpath: never travel across it
    # one uniform direction per object: its own principal axis unless the
    # layer sets an explicit angle
    ang = (core.principal_angle(g) + 90.0) if it['angle'] is None else it['angle']
    if it.get('underlays') is not None and phase != core.TOP and stype not in ('run', 'bean', 'applique'):
        _sew_underlays(s, it, g, ang, travel)
    if stype == 'run':
        if phase != core.UNDER:
            core.sew_edge_run(s, g, inset=0.35, travel=travel)
        return
    if stype == 'outline':
        ring = core.satin_ring(g, border, density, start=s.pos)
        if ring:
            core.sew_ring(s, ring, travel, phase=phase)
        return
    if stype == 'puff':
        # 3D foam: no underlay (it would crush the foam), tight satin with
        # extra width so the foam edge is buried, then a perforating run
        # around the outline so the surplus foam tears away clean
        if phase == core.UNDER:
            return
        rows = core.blob_rows(g, min(density, 0.30), it['max_satin'], start=s.pos)
        if rows is not None:
            for a, b, _row in rows:
                a, b = core.widen(a, b, core.comp_side() + 0.3)
                s.move_to(a, g)
                s.run_to(b, it['max_satin'] + 1)
        else:
            fills.sew_area(s, g, method, ang, min(density, 0.30), it['stitch_len'], travel=travel)
        core.outline_run(s, g, step=1.0, travel=travel)
        return
    mw = core.poly_max_width(g)
    if stype in ('run', 'bean') or stype in DECOR_TYPES:
        # an outline-only shape: run every ring of the outline as a line
        for ring in [g.exterior] + list(g.interiors):
            coords = [tuple(c) for c in ring.simplify(0.2).coords]
            if len(coords) >= 3:
                _sew_line(s, dict(it, line=coords, type=stype), phase)
        return
    if stype == 'satin' or (method == 'satin' and stype in ('auto', 'fill')):
        if mw <= it['max_satin'] and g.area >= 1.5:
            core.sew_blob(s, g, density, it['max_satin'], min(border, mw * 0.3),
                          travel=travel, heavy_underlay=True, phase=phase)
            return
        method = 'tatami'                      # too wide to satin across
    if stype == 'auto' and mw <= it['max_satin'] and g.area >= 1.5:
        core.sew_blob(s, g, density, it['max_satin'], min(border, mw * 0.3),
                      travel=travel, heavy_underlay=True, phase=phase)
        return
    if phase != core.TOP:
        core.fill_underlay(s, g, ang, travel=travel)
    if phase != core.UNDER:
        sp0, sp1 = density, None
        if it.get('gradient'):
            sp0, sp1 = (it['gradient_to'], density) if it.get('gradient_flip') else (density, it['gradient_to'])
        fills.sew_area(s, g, method, ang, sp0, it['stitch_len'], travel=travel, spacing_end=sp1)
        if stype == 'auto' and g.area >= 4.0:
            ring = core.satin_ring(g, border, density, start=s.pos)
            if ring:
                core.sew_ring(s, ring, travel, phase=core.TOP)


def _sew_applique_block(s, items):
    """Placement lines for every piece → stop (lay the fabric) → tack-down
    zigzag → stop (trim the fabric) → satin border with its underlay."""
    for it in items:
        core.outline_run(s, it['g'], step=2.5, travel=None)
    s.stop()
    for it in items:
        ring = core.satin_ring(it['g'], max(0.8, it['border'] * 0.6), 1.2, start=s.pos)
        if ring:
            core.sew_ring(s, ring, None, phase=core.TOP)
        else:
            core.outline_run(s, it['g'], offset=-0.4, step=1.5)
    s.stop()
    for it in items:
        ring = core.satin_ring(it['g'], it['border'], it['density'], start=s.pos)
        if ring:
            core.sew_ring(s, ring, None, phase=core.BOTH)
        else:
            core.outline_run(s, it['g'], step=1.5)
