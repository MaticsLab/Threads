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
from . import fills

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


STITCH_TYPES = ('auto', 'fill', 'outline', 'run', 'bean', 'satin', 'applique', 'puff')
FILL_METHODS = ('tatami', 'contour', 'circular', 'walk', 'satin')
LINE_TYPES = ('run', 'bean', 'satin')


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
    return {'density': density, 'method': method, 'type': stype, 'border': border,
            'rgb': rgb, 'angle': ang, 'name': (L.get('name') or 'Layer')[:48],
            'max_satin': max_satin, 'width': width, 'run_len': run_len}


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
    s.pattern.move_center_to_origin()
    for BL in block_layers:
        BL['rgb'] = tuple(BL['rgb'])
    return s.pattern, block_layers


def _sew_line(s, it, phase):
    """An open path: running stitch, bean stitch, or centre-line satin."""
    from . import pen
    pts, stype = it['line'], it['type']
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
        zz = core.widen_zigzag(zz)
        s.move_to(zz[0], None)
        for p in zz:
            s._st(p)


def _sew_one(s, it, phase):
    if it.get('line') is not None:
        return _sew_line(s, it, phase)
    g, stype = it['g'], it['type']
    density, border, method = it['density'], it['border'], it['method']
    # one uniform direction per object: its own principal axis unless the
    # layer sets an explicit angle
    ang = (core.principal_angle(g) + 90.0) if it['angle'] is None else it['angle']
    if stype == 'run':
        if phase != core.UNDER:
            core.sew_edge_run(s, g, inset=0.35, travel=g)
        return
    if stype == 'outline':
        ring = core.satin_ring(g, border, density, start=s.pos)
        if ring:
            core.sew_ring(s, ring, g, phase=phase)
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
            fills.sew_area(s, g, method, ang, min(density, 0.30), 3.5, travel=g)
        core.outline_run(s, g, step=1.0, travel=g)
        return
    mw = core.poly_max_width(g)
    if stype in ('run', 'bean'):
        # an outline-only shape: run the outline
        if phase != core.UNDER:
            core.outline_run(s, g, step=it['run_len'], travel=g) if stype == 'run' else \
                core.bean_run(s, list(g.exterior.simplify(0.2).coords), it['run_len'], g)
        return
    if stype == 'satin' or (method == 'satin' and stype in ('auto', 'fill')):
        if mw <= it['max_satin'] and g.area >= 1.5:
            core.sew_blob(s, g, density, it['max_satin'], min(border, mw * 0.3),
                          travel=g, heavy_underlay=True, phase=phase)
            return
        method = 'tatami'                      # too wide to satin across
    if stype == 'auto' and mw <= it['max_satin'] and g.area >= 1.5:
        core.sew_blob(s, g, density, it['max_satin'], min(border, mw * 0.3),
                      travel=g, heavy_underlay=True, phase=phase)
        return
    if phase != core.TOP:
        core.fill_underlay(s, g, ang, travel=g)
    if phase != core.UNDER:
        fills.sew_area(s, g, method, ang, density, 3.5, travel=g)
        if stype == 'auto' and g.area >= 4.0:
            ring = core.satin_ring(g, border, density, start=s.pos)
            if ring:
                core.sew_ring(s, ring, g, phase=core.TOP)


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
