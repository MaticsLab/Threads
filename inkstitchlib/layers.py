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
from shapely.geometry import Polygon, MultiPolygon

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


def stitch(layers_in, max_satin=8.0):
    """Sew the arranged layers, in order, one uniform treatment per object."""
    import pystitch

    s = core.Sewer()
    block_colors = []
    block_layers = []
    prev_rgb = None
    drawn = 0

    def start_block(rgb, name):
        nonlocal prev_rgb
        if rgb == prev_rgb:
            return
        th = pystitch.EmbThread()
        th.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        th.description = name[:48]
        if prev_rgb is None:
            s.pattern.add_thread(th)
        else:
            s.color_break(th)
        block_colors.append(rgb)
        block_layers.append({'name': name[:48],
                             'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb})
        prev_rgb = rgb

    for L in layers_in:
        if not L.get('visible', True):
            continue
        prm = {**DEFAULT_PARAMS, **(L.get('params') or {})}
        density = max(0.25, min(3.0, float(prm.get('density') or 0.4)))
        method = prm.get('fill_method')
        if method not in ('tatami', 'contour', 'circular'):
            method = 'tatami'
        stype = prm.get('stitch')
        if stype not in ('auto', 'fill', 'outline', 'run'):
            stype = 'auto'
        border = max(0.5, min(3.0, float(prm.get('border_mm') or 1.0)))
        hexv = str(L.get('color', '#1A3B69')).lstrip('#')
        if len(hexv) != 6:
            hexv = '1A3B69'
        v = int(hexv, 16)
        rgb = ((v >> 16) & 255, (v >> 8) & 255, v & 255)

        polys = [q for q in (_poly_from_data(d) for d in L.get('polys') or []) if q]
        flat = []
        for p in polys:
            flat.extend(p.geoms if isinstance(p, MultiPolygon) else [p])
        flat = [q for q in flat if q.area > 0.3]
        if not flat:
            continue
        start_block(rgb, L.get('name') or 'Layer')

        for g in core.order_by_nearest(flat, lambda q: (q.centroid.x, q.centroid.y), s.pos):
            # one uniform direction per object: its own principal axis unless
            # the layer sets an explicit angle
            ang = prm.get('angle')
            ang = (core.principal_angle(g) + 90.0) if (ang in (None, '', 'auto')) else float(ang)
            if stype == 'run':
                core.sew_edge_run(s, g, inset=0.35, travel=g)
                drawn += 1
                continue
            if stype == 'outline':
                ring = core.satin_ring(g, border, density)
                if ring:
                    core.sew_ring(s, ring, g)
                    drawn += 1
                continue
            mw = core.poly_max_width(g)
            if stype == 'auto' and mw <= max_satin and g.area >= 1.5:
                core.sew_blob(s, g, density, max_satin, min(border, mw * 0.3),
                              travel=g, heavy_underlay=True)
            else:
                core.sew_edge_run(s, g, travel=g)
                fills.sew_area(s, g, method, ang, density, 3.5, travel=g)
                if stype == 'auto' and g.area >= 4.0:
                    ring = core.satin_ring(g, border, density)
                    if ring:
                        core.sew_ring(s, ring, g)
            drawn += 1

    if drawn == 0 or s.count == 0:
        raise LayerError('nothing to stitch — every layer is hidden or empty')
    s.tie_off()
    s.pattern.end()
    s.pattern.move_center_to_origin()
    for BL in block_layers:
        BL['rgb'] = tuple(BL['rgb'])
    return s.pattern, block_layers
