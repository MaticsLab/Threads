"""Digitize an SVG document, the way Ink/Stitch digitizes Inkscape files.

Elements are sewn in document order and grouped into colour blocks whenever
the colour changes, exactly like Ink/Stitch:

* a path carrying inkstitch:satin_column="True" is stitched as a satin
  column with the rail/rung engine (rails split at rungs, synced zigzag);
* a filled shape becomes a fill region (even-odd of its subpaths, holes
  kept) sewn with the requested fill method - tatami, contour or circular -
  honouring inkstitch:angle and inkstitch:row_spacing_mm when present;
* a stroked path becomes a running stitch (inkstitch:running_stitch_length_mm,
  inkstitch:bean_stitch_repeats honoured), or a zigzag at the stroke width
  when the stroke is wider than a thread.

Geometry, transforms and units are resolved with svgelements; sizes come
from the document's width/height + viewBox (96 dpi CSS px -> mm), with an
optional target width override.
"""
import io
import math
import re
from xml.etree import ElementTree as ET

import numpy as np
from shapely.geometry import Polygon
from svgelements import Path as SvgPath, Matrix, Color

from digitizer import core
from . import fills
from .lettering import satin_zigzag, center_run, flatten_path, resample_polyline

PIXELS_PER_MM = 96 / 25.4
INKSTITCH_NS = 'http://inkstitch.org/namespace'
SVG_NS = 'http://www.w3.org/2000/svg'

UNIT_PX = {'': 1.0, 'px': 1.0, 'mm': 96 / 25.4, 'cm': 96 / 2.54,
           'in': 96.0, 'pt': 96 / 72, 'pc': 16.0, 'q': 96 / 101.6}

SKIP_TAGS = {'defs', 'metadata', 'namedview', 'style', 'title', 'desc',
             'symbol', 'clipPath', 'mask', 'marker', 'pattern', 'script'}


class SvgError(ValueError):
    pass


def _length_px(s):
    if s is None:
        return None
    m = re.match(r'^\s*([0-9.eE+-]+)\s*([a-z%]*)\s*$', s)
    if not m or m.group(2) == '%':
        return None
    return float(m.group(1)) * UNIT_PX.get(m.group(2), 1.0)


def _style_of(el, inherited):
    """Merge inherited presentation with this element's attrs + style."""
    st = dict(inherited)
    for k in ('fill', 'stroke', 'stroke-width', 'display', 'opacity'):
        v = el.get(k)
        if v is not None:
            st[k] = v
    for part in (el.get('style') or '').split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            st[k.strip()] = v.strip()
    return st


def _color(v):
    try:
        c = Color(v)
        if c.value is None:
            return None
        return (c.red, c.green, c.blue)
    except Exception:
        return None


def _shape_to_path(el):
    tag = el.tag.split('}')[-1]
    if tag == 'path':
        d = el.get('d')
        return SvgPath(d) if d else None
    f = lambda k, dflt='0': float(el.get(k) or dflt)
    try:
        if tag == 'rect':
            x, y, w, h = f('x'), f('y'), f('width'), f('height')
            if w <= 0 or h <= 0:
                return None
            rx = el.get('rx')
            d = 'M%f,%f H%f V%f H%f Z' % (x, y, x + w, y + h, x)
            if rx:
                r = min(float(rx), w / 2, h / 2)
                d = ('M%f,%f H%f A%f,%f 0 0 1 %f,%f V%f A%f,%f 0 0 1 %f,%f '
                     'H%f A%f,%f 0 0 1 %f,%f V%f A%f,%f 0 0 1 %f,%f Z') % (
                    x + r, y, x + w - r, r, r, x + w, y + r, y + h - r,
                    r, r, x + w - r, y + h, x + r, r, r, x, y + h - r,
                    y + r, r, r, x + r, y)
            return SvgPath(d)
        if tag == 'circle':
            cx, cy, r = f('cx'), f('cy'), f('r')
            if r <= 0:
                return None
            return SvgPath('M%f,%f a%f,%f 0 1 0 %f,0 a%f,%f 0 1 0 %f,0 Z'
                           % (cx - r, cy, r, r, 2 * r, r, r, -2 * r))
        if tag == 'ellipse':
            cx, cy, rx, ry = f('cx'), f('cy'), f('rx'), f('ry')
            if rx <= 0 or ry <= 0:
                return None
            return SvgPath('M%f,%f a%f,%f 0 1 0 %f,0 a%f,%f 0 1 0 %f,0 Z'
                           % (cx - rx, cy, rx, ry, 2 * rx, rx, ry, -2 * rx))
        if tag == 'line':
            return SvgPath('M%f,%f L%f,%f' % (f('x1'), f('y1'), f('x2'), f('y2')))
        if tag in ('polyline', 'polygon'):
            pts = re.findall(r'[0-9.eE+-]+', el.get('points') or '')
            if len(pts) < 4:
                return None
            d = 'M' + ' L'.join('%s,%s' % (pts[i], pts[i + 1])
                                for i in range(0, len(pts) - 1, 2))
            if tag == 'polygon':
                d += ' Z'
            return SvgPath(d)
    except Exception:
        return None
    return None


def _collect(root):
    """Walk the tree in document order -> [(subpaths_px, style, attrs)]."""
    out = []

    def walk(el, matrix, style):
        tag = el.tag.split('}')[-1]
        if tag in SKIP_TAGS:
            return
        st = _style_of(el, style)
        if st.get('display') == 'none':
            return
        tr = el.get('transform')
        if tr:
            m = Matrix(tr)
            matrix = m * matrix if matrix else m
        p = _shape_to_path(el)
        if p is not None:
            if matrix:
                p *= matrix
            subs = flatten_path(p)
            if subs:
                out.append((subs, st, dict(el.attrib)))
        for child in el:
            walk(child, matrix, st)

    walk(root, None, {})
    return out


def _fill_polygon(subs_mm):
    """Even-odd combination of the subpath rings, holes preserved."""
    rings = []
    for s in subs_mm:
        if len(s) >= 3:
            try:
                q = Polygon(s)
                if not q.is_valid:
                    q = q.buffer(0)
                if not q.is_empty and q.area > 0.05:
                    rings.append(q)
            except Exception:
                pass
    if not rings:
        return None
    g = rings[0]
    for q in rings[1:]:
        g = g.symmetric_difference(q)
    g = g.buffer(0).simplify(0.05, preserve_topology=True)
    return None if g.is_empty else g


def _ink(attrs, key, default=None):
    return attrs.get('{%s}%s' % (INKSTITCH_NS, key), default)


def _sew_stroke(s, subs_mm, sw_mm, attrs, max_satin):
    run_len = float(_ink(attrs, 'running_stitch_length_mm') or 2.5)
    repeats = int(float(_ink(attrs, 'bean_stitch_repeats') or 0))
    zz_spacing = float(_ink(attrs, 'zigzag_spacing_mm') or 0.5)
    for sub in subs_mm:
        if sw_mm >= 0.8 and sw_mm <= max_satin:
            # zigzag at the stroke width
            pts = resample_polyline(sub, zz_spacing)
            if len(pts) < 2:
                continue
            p = np.asarray(pts, float)
            d = np.gradient(p, axis=0)
            L = np.linalg.norm(d, axis=1)
            L[L < 1e-9] = 1e-9
            n = np.column_stack([-d[:, 1] / L, d[:, 0] / L]) * (sw_mm / 2)
            s.move_to(tuple(p[0]), None)
            side = 1.0
            for q, nq in zip(p, n):
                s.run_to((q[0] + nq[0] * side, q[1] + nq[1] * side), max_satin + 1)
                side = -side
        else:
            pts = resample_polyline(sub, run_len)
            if len(pts) < 2:
                continue
            s.move_to(pts[0], None)
            for a, b in zip(pts, pts[1:]):
                s.run_to(b, run_len + 0.1)
                for _ in range(repeats):
                    s.run_to(a, run_len + 0.1)
                    s.run_to(b, run_len + 0.1)


def digitize_svg(data, width_mm=None, fill_method='tatami', fill_angle=65.0,
                 row_spacing=0.35, max_stitch=3.5, max_satin=8.0,
                 heavy_underlay=True):
    """SVG bytes/str -> (pystitch pattern via Sewer, layers, info)."""
    import pystitch

    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SvgError('not a valid SVG file: %s' % e)

    # document scale: user units -> mm
    vb = root.get('viewBox')
    if vb:
        parts = [float(v) for v in re.split(r'[ ,]+', vb.strip())]
        vw = parts[2]
    else:
        vw = _length_px(root.get('width')) or 100.0
    w_px = _length_px(root.get('width'))
    doc_scale = (w_px / vw) if (w_px and vb and vw) else 1.0
    unit2mm = doc_scale / PIXELS_PER_MM
    natural_w_mm = vw * doc_scale / PIXELS_PER_MM
    if width_mm:
        unit2mm *= width_mm / max(natural_w_mm, 1e-6)

    elements = _collect(root)
    if not elements:
        raise SvgError('no drawable shapes found in the SVG')

    s = core.Sewer()
    layers, block_colors = [], []
    prev_rgb = None
    n_satin = n_fill = n_stroke = 0

    def start_block(rgb):
        nonlocal prev_rgb
        if rgb == prev_rgb:
            return
        th = pystitch.EmbThread()
        th.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        th.description = 'Colour %d' % (len(block_colors) + 1)
        if prev_rgb is None:
            s.pattern.add_thread(th)
        else:
            s.color_break(th)
        block_colors.append(rgb)
        prev_rgb = rgb

    for subs, st, attrs in elements:
        subs_mm = [[(x * unit2mm, y * unit2mm) for x, y in sub] for sub in subs]
        fill_c = _color(st.get('fill', '#000000')) if st.get('fill', '#000000').lower() != 'none' else None
        stroke_c = _color(st.get('stroke', 'none')) if st.get('stroke', 'none').lower() != 'none' else None

        if str(_ink(attrs, 'satin_column', '')).lower() == 'true' and len(subs_mm) >= 2:
            rgb = stroke_c or fill_c or (0, 0, 0)
            start_block(rgb)
            spacing = float(_ink(attrs, 'zigzag_spacing_mm') or 0.35)
            zz = satin_zigzag(subs_mm, spacing)
            if zz:
                under = center_run(subs_mm)
                s.move_to(under[0] if under else zz[0], None)
                for pnt in under:
                    s.run_to(pnt, 2.5)
                for pnt in reversed(under):
                    s.run_to(pnt, 2.5)
                for pnt in zz:
                    s._st(pnt)
                n_satin += 1
            continue

        if fill_c is not None:
            g = _fill_polygon(subs_mm)
            if g is not None:
                start_block(fill_c)
                angle = float(_ink(attrs, 'angle') or fill_angle)
                spacing = float(_ink(attrs, 'row_spacing_mm') or row_spacing)
                geoms = g.geoms if g.geom_type == 'MultiPolygon' else [g]
                for q in geoms:
                    if q.area < 0.4:
                        continue
                    if q.area >= 3.0:
                        core.sew_edge_run(s, q, travel=q)
                        if heavy_underlay:
                            core.sew_fill(s, q, angle + 45, 2.5, 3.0,
                                          stagger=False, start=s.pos, travel=q)
                    fills.sew_area(s, q, fill_method, angle, spacing,
                                   max_stitch, travel=q)
                    n_fill += 1

        if stroke_c is not None:
            start_block(stroke_c)
            sw_mm = (_length_px(st.get('stroke-width', '1')) or 1.0) * unit2mm
            _sew_stroke(s, subs_mm, sw_mm, attrs, max_satin)
            n_stroke += 1

    if s.count == 0:
        raise SvgError('the SVG contained no stitchable geometry')
    s.tie_off()
    s.pattern.end()
    s.pattern.move_center_to_origin()

    for i, rgb in enumerate(block_colors):
        layers.append({'name': 'Colour %d' % (i + 1),
                       'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb})
    info = {'elements': len(elements), 'satin_columns': n_satin,
            'fills': n_fill, 'strokes': n_stroke,
            'natural_width_mm': round(natural_w_mm, 1)}
    return s.pattern, layers, info
