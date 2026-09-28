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


# properties that cascade from groups / <use> down to shapes
INHERITED = ('fill', 'stroke', 'stroke-width', 'fill-rule', 'visibility',
             'fill-opacity', 'stroke-opacity', 'color')
PRESENTATION = INHERITED + ('display', 'opacity')


def _parse_css(root):
    """Rules from every <style> block -> [(specificity, order, selector, decls)].
    Handles the simple selectors design tools emit: .class, #id, tag,
    tag.class, and comma lists (Illustrator's .cls-1{fill:#...})."""
    rules = []
    order = 0
    for el in root.iter():
        if el.tag.split('}')[-1] != 'style' or not el.text:
            continue
        css = re.sub(r'/\*.*?\*/', '', el.text, flags=re.S)
        for sels, body in re.findall(r'([^{}]+)\{([^}]*)\}', css):
            decls = {}
            for part in body.split(';'):
                if ':' in part:
                    k, v = part.split(':', 1)
                    decls[k.strip().lower()] = v.strip().replace('!important', '').strip()
            for sel in sels.split(','):
                sel = sel.strip()
                if not sel or sel.startswith('@') or ' ' in sel or '>' in sel or ':' in sel:
                    continue
                spec = sel.count('#') * 100 + sel.count('.') * 10 + (0 if sel[0] in '.#*' else 1)
                rules.append((spec, order, sel, decls))
                order += 1
    rules.sort(key=lambda r: (r[0], r[1]))
    return rules


def _css_match(sel, tag, classes, el_id):
    m = re.fullmatch(r'([a-zA-Z][\w-]*|\*)?((?:[.#][\w-]+)*)', sel)
    if not m:
        return False
    if m.group(1) and m.group(1) not in ('*', tag):
        return False
    for kind, name in re.findall(r'([.#])([\w-]+)', m.group(2) or ''):
        if kind == '.' and name not in classes:
            return False
        if kind == '#' and name != el_id:
            return False
    return True


def _style_of(el, inherited, css=()):
    """Cascade: inherited < presentation attributes < <style> rules < style=""."""
    st = {k: v for k, v in inherited.items() if k in INHERITED}
    if inherited.get('_hidden'):
        st['_hidden'] = True
    for k in PRESENTATION:
        v = el.get(k)
        if v is not None:
            st[k] = v
    if css:
        tag = el.tag.split('}')[-1]
        classes = set((el.get('class') or '').split())
        el_id = el.get('id')
        for _spec, _o, sel, decls in css:
            if _css_match(sel, tag, classes, el_id):
                st.update(decls)
    for part in (el.get('style') or '').split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            st[k.strip().lower()] = v.strip()
    try:
        if float(st.get('opacity', 1)) <= 0.001:
            st['_hidden'] = True
    except ValueError:
        pass
    st.pop('opacity', None)
    return st


def _color(v, gradients=None, current=None):
    if v is None:
        return None
    v = v.strip()
    if v.lower() == 'currentcolor':
        v = current or '#000000'
    m = re.match(r'url\(\s*["\']?#([^)"\']+)["\']?\s*\)', v)
    if m:
        # gradients/patterns: sew the gradient's average colour
        return (gradients or {}).get(m.group(1))
    try:
        c = Color(v)
        if c.value is None:
            return None
        return (c.red, c.green, c.blue)
    except Exception:
        return None


def _gradients(root, ids):
    """Gradient id -> average stop colour (following xlink:href chains)."""
    out = {}

    def stops(g, depth=0):
        cols = []
        for st in g:
            if st.tag.split('}')[-1] != 'stop':
                continue
            sty = _style_of(st, {})
            c = _color(sty.get('stop-color', st.get('stop-color', '#000000')))
            if c:
                cols.append(c)
        if not cols and depth < 5:
            href = g.get('href') or g.get('{http://www.w3.org/1999/xlink}href')
            if href and href.startswith('#') and href[1:] in ids:
                return stops(ids[href[1:]], depth + 1)
        return cols

    for el in root.iter():
        if el.tag.split('}')[-1] in ('linearGradient', 'radialGradient') and el.get('id'):
            cols = stops(el)
            if cols:
                out[el.get('id')] = tuple(int(round(sum(c[i] for c in cols) / len(cols)))
                                          for i in range(3))
    return out
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


XLINK = '{http://www.w3.org/1999/xlink}href'


def _href(el):
    h = el.get('href') or el.get(XLINK) or ''
    return h[1:] if h.startswith('#') else None


def _clip_ref(v):
    m = re.match(r'url\(\s*["\']?#([^)"\']+)["\']?\s*\)', v or '')
    return m.group(1) if m else None


def _collect(root, step=0.6):
    """Walk the tree in document order.

    Returns (elements, notes): elements are (subpaths, style, attrs, clips)
    with subpaths in root user units and clips a list of clip regions (each
    a list of subpaths) the element must be cut to. <use> references are
    expanded (glyph-per-<use> exports from Canva, cairo, PDF converters),
    <style> classes and gradients resolve to colours, hidden and zero-opacity
    shapes are dropped. notes counts what can't be sewn (text, images)."""
    ids = {el.get('id'): el for el in root.iter() if el.get('id')}
    css = _parse_css(root)
    grads = _gradients(root, ids)
    out = []
    notes = {'text': 0, 'image': 0}
    clip_cache = {}

    def clip_region(cid, matrix, depth):
        cp = ids.get(cid)
        if cp is None or cp.tag.split('}')[-1] != 'clipPath' or depth > 4:
            return None
        m = matrix
        tr = cp.get('transform')
        if tr:
            mm = Matrix(tr)
            m = mm * m if m else mm
        subs = []

        def grab(e, mat, d):
            t = e.tag.split('}')[-1]
            tr2 = e.get('transform')
            if tr2:
                m2 = Matrix(tr2)
                mat = m2 * mat if mat else m2
            if t == 'use' and d < 8:
                tgt = ids.get(_href(e))
                if tgt is not None:
                    x, y = float(e.get('x') or 0), float(e.get('y') or 0)
                    mu = Matrix('translate(%f,%f)' % (x, y))
                    grab(tgt, mu * mat if mat else mu, d + 1)
                return
            p = _shape_to_path(e)
            if p is not None:
                if mat:
                    p *= mat
                    p.reify()
                subs.extend(flatten_path(p, step))
            for ch in e:
                grab(ch, mat, d + 1)

        for ch in cp:
            grab(ch, m, 0)
        return subs or None

    def walk(el, matrix, style, clips, depth=0, via_use=False):
        if depth > 60:
            return
        tag = el.tag.split('}')[-1]
        if tag in SKIP_TAGS and not (via_use and tag == 'symbol'):
            return
        if tag in ('text', 'foreignObject'):
            notes['text'] += 1
            return
        if tag == 'image':
            notes['image'] += 1
            return
        st = _style_of(el, style, css)
        if st.get('display') == 'none':
            return
        tr = el.get('transform')
        if tr:
            m = Matrix(tr)
            matrix = m * matrix if matrix else m
        cid = _clip_ref(st.get('clip-path') or el.get('clip-path'))
        if cid:
            reg = clip_region(cid, matrix, depth)
            if reg:
                clips = clips + [reg]
        if tag == 'use':
            tgt = ids.get(_href(el))
            if tgt is not None and depth < 50:
                x, y = float(el.get('x') or 0), float(el.get('y') or 0)
                mu = Matrix('translate(%f,%f)' % (x, y))
                walk(tgt, mu * matrix if matrix else mu, st, clips, depth + 1, via_use=True)
            return
        if tag == 'svg' and el is not root:
            # nested <svg>: place its content at x/y
            x, y = float(el.get('x') or 0), float(el.get('y') or 0)
            if x or y:
                mu = Matrix('translate(%f,%f)' % (x, y))
                matrix = mu * matrix if matrix else mu
        p = _shape_to_path(el)
        if p is not None and not st.get('_hidden') and st.get('visibility') not in ('hidden', 'collapse'):
            if matrix:
                # svgelements applies a translate-only matrix lazily; reify
                # so the segments (not just the d string) carry the offset
                p *= matrix
                p.reify()
            subs = flatten_path(p, step)
            if subs:
                st = dict(st)
                st['_fill_rgb'] = _fill_rgb(st, grads)
                st['_stroke_rgb'] = _stroke_rgb(st, grads)
                # scale stroke width by the transform
                if matrix:
                    try:
                        sx = math.hypot(matrix.a, matrix.b)
                        sy = math.hypot(matrix.c, matrix.d)
                        st['_stroke_scale'] = math.sqrt(abs(sx * sy)) or 1.0
                    except Exception:
                        pass
                out.append((subs, st, dict(el.attrib), clips))
        for child in el:
            walk(child, matrix, st, clips, depth + 1, via_use)

    walk(root, None, {}, [])
    return out, notes


def _opaque(v):
    try:
        return float(v) > 0.001
    except (TypeError, ValueError):
        return True


def _fill_rgb(st, grads):
    v = st.get('fill', '#000000')
    if str(v).strip().lower() == 'none' or not _opaque(st.get('fill-opacity', 1)):
        return None
    return _color(v, grads, st.get('color'))


def _stroke_rgb(st, grads):
    v = st.get('stroke', 'none')
    if str(v).strip().lower() == 'none' or not _opaque(st.get('stroke-opacity', 1)):
        return None
    return _color(v, grads, st.get('color'))


def _rings(subs_mm):
    rings = []
    for sub in subs_mm:
        if len(sub) < 3:
            continue
        try:
            q = Polygon(sub)
        except Exception:
            continue
        if q.is_empty:
            continue
        rings.append(sub)
    return rings


def _fill_polygon(subs_mm, rule='nonzero'):
    """The filled region of a path's subpaths under its fill-rule.

    Every face of the subpaths' arrangement is kept when its winding
    number is non-zero (nonzero, the SVG default: overlapping outlines
    merge, counters wound the other way stay holes) or odd (evenodd)."""
    from shapely import contains_xy
    from shapely.geometry import LineString, MultiPolygon
    from shapely.ops import unary_union, polygonize

    rings = _rings(subs_mm)
    if not rings:
        return None
    if len(rings) == 1:
        g = Polygon(rings[0])
        g = g if g.is_valid else g.buffer(0)
    else:
        lines = unary_union([LineString(list(r) + [r[0]]) for r in rings])
        faces = list(polygonize(lines))
        if not faces:
            return None
        pts = [f.representative_point() for f in faces]
        xs = np.array([p.x for p in pts]); ys = np.array([p.y for p in pts])
        wind = np.zeros(len(faces), int)
        for r in rings:
            poly = Polygon(r)
            if not poly.is_valid:
                poly = poly.buffer(0)
                if poly.is_empty:
                    continue
            a = np.asarray(r, float)
            signed = 0.5 * np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1])
            inside = contains_xy(poly, xs, ys)
            wind += np.where(inside, 1 if signed >= 0 else -1, 0)
        if rule == 'evenodd':
            count = np.zeros(len(faces), int)
            for r in rings:
                poly = Polygon(r)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                count += contains_xy(poly, xs, ys).astype(int)
            keep = count % 2 == 1
        else:
            keep = wind != 0
        kept = [f for f, k in zip(faces, keep) if k]
        if not kept:
            return None
        g = unary_union(kept)
    g = g.buffer(0).simplify(0.05, preserve_topology=True)
    if g.is_empty:
        return None
    if g.geom_type == 'GeometryCollection':
        polys = [q for q in g.geoms if q.geom_type in ('Polygon', 'MultiPolygon')]
        g = unary_union(polys) if polys else None
    return g


def _clip_geom(clips_mm):
    """Intersection of every clip region (each nonzero-filled)."""
    g = None
    for reg in clips_mm:
        c = _fill_polygon(reg, 'nonzero')
        if c is None:
            return 'empty'
        g = c if g is None else g.intersection(c)
        if g.is_empty:
            return 'empty'
    return g


def _ink(attrs, key, default=None):
    return attrs.get('{%s}%s' % (INKSTITCH_NS, key), default)


def _sew_stroke(s, subs_mm, sw_mm, attrs, max_satin):
    run_len = float(_ink(attrs, 'running_stitch_length_mm') or 2.5)
    repeats = int(float(_ink(attrs, 'bean_stitch_repeats') or 0))
    zz_spacing = float(_ink(attrs, 'zigzag_spacing_mm') or 0.5)
    for sub in subs_mm:
        if sw_mm >= core.MIN_SATIN and sw_mm <= max_satin:
            # zigzag at the stroke width (+ pull compensation)
            pts = resample_polyline(sub, zz_spacing)
            if len(pts) < 2:
                continue
            p = np.asarray(pts, float)
            d = np.gradient(p, axis=0)
            L = np.linalg.norm(d, axis=1)
            L[L < 1e-9] = 1e-9
            n = np.column_stack([-d[:, 1] / L, d[:, 0] / L]) * (sw_mm / 2 + core.comp_side())
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


def _stroke_pieces(subs_mm, st, attrs, clip, stroke_c, unit2mm, max_satin,
                   fill_method, fill_angle, row_spacing, max_stitch):
    """One element's stroke as (footprint, sew_fn) pieces: zigzag/running
    stitch, or a fill band when the stroke is wider than the satin cap."""
    from shapely.geometry import LineString, LinearRing
    from shapely.ops import unary_union
    sw_mm = ((_length_px(st.get('stroke-width', '1')) or 1.0)
             * st.get('_stroke_scale', 1.0) * unit2mm)
    lines = subs_mm
    if clip is not None:
        lines = []
        for sub in subs_mm:
            if len(sub) < 2:
                continue
            part = LineString(sub).intersection(clip)
            for ln in getattr(part, 'geoms', [part]):
                if ln.geom_type == 'LineString' and len(ln.coords) > 1:
                    lines.append(list(ln.coords))
    lines = [ln for ln in lines if len(ln) >= 2]
    if not lines:
        return []
    if sw_mm > max_satin:
        # too wide for a zigzag: sew the stroke's outline as a fill
        parts = []
        for sub in lines:
            closed = len(sub) > 2 and math.dist(sub[0], sub[-1]) < 1e-3
            ln = LinearRing(sub) if closed else LineString(sub)
            parts.append(ln.buffer(sw_mm / 2, join_style=2 if closed else 1,
                                   mitre_limit=4.0))
        band = unary_union(parts).buffer(0)
        if clip is not None:
            band = band.intersection(clip).buffer(0)
        if band.is_empty:
            return []
        polys = [q for q in (band.geoms if band.geom_type == 'MultiPolygon' else [band])
                 if q.geom_type == 'Polygon' and q.area >= 0.4]

        def under(s, polys=polys):
            for q in polys:
                if q.area >= 3.0:
                    core.fill_underlay(s, q, fill_angle, travel=q)

        def top(s, polys=polys):
            for q in polys:
                fills.sew_area(s, q, fill_method, fill_angle, row_spacing,
                               max_stitch, travel=q)
        return [(band, under, top)] if polys else []

    foot = unary_union([LineString(ln).buffer(max(sw_mm, 0.6) / 2) for ln in lines])
    return [(foot, None, lambda s, lines=lines: _sew_stroke(s, lines, sw_mm, attrs, max_satin))]


def digitize_svg(data, width_mm=None, fill_method='tatami', fill_angle=65.0,
                 row_spacing=0.35, max_stitch=3.5, max_satin=8.0,
                 heavy_underlay=True, scale=None, center=True, underlay=None,
                 pull_comp=None, min_satin=None, knockdown=False):
    """SVG bytes/str -> (pystitch pattern via Sewer, layers, info).

    Coordinates are page coordinates: the viewBox's top-left corner is
    (0, 0), so several SVGs exported from the same artboard line up. With
    center=False the pattern keeps them (0.1 mm units) instead of being
    moved to the origin. `scale` multiplies the natural size (used to sew a
    second SVG at the same scale as the first); width_mm overrides it.
    info['scale'] is the multiplier actually used.
    """
    import pystitch

    core.set_tunables(underlay=underlay or ('auto' if heavy_underlay else 'light'),
                      pull_comp=pull_comp, min_satin=min_satin)
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SvgError('not a valid SVG file: %s' % e)

    # document scale: user units -> mm
    vb = root.get('viewBox')
    vbx = vby = 0.0
    vh = None
    parts = None
    if vb:
        try:
            parts = [float(v) for v in re.split(r'[ ,]+', vb.strip())]
        except ValueError:
            parts = None
    if parts and len(parts) == 4 and parts[2] > 0:
        vbx, vby, vw, vh = parts
    else:
        vb = None
        vw = _length_px(root.get('width')) or 100.0
        vh = _length_px(root.get('height'))
    w_px = _length_px(root.get('width'))
    doc_scale = (w_px / vw) if (w_px and vb and vw) else 1.0
    unit2mm = doc_scale / PIXELS_PER_MM
    natural_w_mm = vw * doc_scale / PIXELS_PER_MM
    mult = 1.0
    if width_mm:
        mult = width_mm / max(natural_w_mm, 1e-6)
    elif scale:
        mult = float(scale)
    unit2mm *= mult

    # flatten curves to ~0.15 mm whatever the document's unit size
    step = max(1e-4, 0.15 / max(unit2mm, 1e-9))
    elements, notes = _collect(root, step)
    warnings = []
    if notes['text']:
        warnings.append('%d text element%s skipped — convert text to outlines/paths '
                        'in your design tool before exporting the SVG.'
                        % (notes['text'], '' if notes['text'] == 1 else 's'))
    if notes['image']:
        warnings.append('%d embedded picture%s skipped — upload the picture itself as a '
                        'PNG/JPG to digitize it.' % (notes['image'], '' if notes['image'] == 1 else 's'))
    if not elements:
        if notes['text'] or notes['image']:
            raise SvgError('this SVG only contains %s, no vector shapes to stitch — '
                           'convert text to outlines, or upload the picture as a PNG/JPG'
                           % ('text' if notes['text'] and not notes['image'] else
                              'an embedded picture' if not notes['text'] else 'text and pictures'))
        raise SvgError('no drawable shapes found in the SVG')

    n_satin = n_fill = n_stroke = 0
    pieces = []                       # (rgb, footprint, sew_fn) in document order

    to_mm = lambda subs: [[((x - vbx) * unit2mm, (y - vby) * unit2mm) for x, y in sub]
                          for sub in subs]
    for subs, st, attrs, clips in elements:
        subs_mm = to_mm(subs)
        fill_c = st.get('_fill_rgb')
        stroke_c = st.get('_stroke_rgb')
        clip = _clip_geom([to_mm(c) for c in clips]) if clips else None
        if clip == 'empty':
            continue

        if str(_ink(attrs, 'satin_column', '')).lower() == 'true' and len(subs_mm) >= 2:
            rgb = stroke_c or fill_c or (0, 0, 0)
            spacing = float(_ink(attrs, 'zigzag_spacing_mm') or 0.35)
            zz = satin_zigzag(subs_mm, spacing)
            if zz:
                under = center_run(subs_mm)
                width = float(np.mean([math.dist(a, b) for a, b in zip(zz, zz[1:])])) if len(zz) > 1 else 0.0
                from shapely.geometry import MultiPoint
                foot = MultiPoint(zz).convex_hull
                if width < core.MIN_SATIN and under:
                    # too narrow to hold satin: bean stitch along the centre
                    pieces.append((rgb, foot, (None, lambda s, u=under: core.bean_run(s, u, 2.0))))
                    n_satin += 1
                    continue
                zz = core.widen_zigzag(zz)
                mode = core.underlay_mode()

                def satin_under(s, zz=zz, under=under, mode=mode):
                    if mode == 'none':
                        return
                    s.move_to(under[0] if under else zz[0], None)
                    for pnt in under:
                        s.run_to(pnt, 2.5)
                    if mode != 'light':
                        for pnt in reversed(under):
                            s.run_to(pnt, 2.5)

                def satin_top(s, zz=zz):
                    s.move_to(zz[0], None)
                    for pnt in zz:
                        s._st(pnt)
                pieces.append((rgb, foot, (satin_under, satin_top)))
                n_satin += 1
            continue

        stroke_args = (subs_mm, st, attrs, clip, stroke_c, unit2mm, max_satin,
                       fill_method, fill_angle, row_spacing, max_stitch)
        stroke_first = str(st.get('paint-order', '')).split()[:1] == ['stroke']
        if stroke_first and stroke_c is not None:
            sp = _stroke_pieces(*stroke_args)
            pieces += [(stroke_c, g, (u, t)) for g, u, t in sp]
            n_stroke += bool(sp)
            stroke_c = None
        if fill_c is not None:
            g = _fill_polygon(subs_mm, 'evenodd' if st.get('fill-rule') == 'evenodd' else 'nonzero')
            if g is not None and clip is not None:
                g = g.intersection(clip).buffer(0)
                if g.is_empty:
                    g = None
            if g is not None and g.geom_type not in ('Polygon', 'MultiPolygon'):
                polys = [q for q in getattr(g, 'geoms', []) if q.geom_type == 'Polygon']
                from shapely.geometry import MultiPolygon
                g = MultiPolygon(polys) if polys else None
            if g is not None:
                angle = float(_ink(attrs, 'angle') or fill_angle)
                spacing = float(_ink(attrs, 'row_spacing_mm') or row_spacing)
                geoms = [q for q in (g.geoms if g.geom_type == 'MultiPolygon' else [g])
                         if q.area >= 0.4]

                def fill_under(s, geoms=geoms, angle=angle):
                    for q in geoms:
                        if q.area >= 3.0:
                            core.fill_underlay(s, q, angle, travel=q)

                def fill_top(s, geoms=geoms, angle=angle, spacing=spacing):
                    for q in geoms:
                        fills.sew_area(s, q, fill_method, angle, spacing,
                                       max_stitch, travel=q)
                if geoms:
                    pieces.append((fill_c, g, (fill_under, fill_top)))
                    n_fill += len(geoms)

        if stroke_c is not None:
            sp = _stroke_pieces(*stroke_args)
            pieces += [(stroke_c, g, (u, t)) for g, u, t in sp]
            n_stroke += bool(sp)

    s = core.Sewer()
    layers, block_colors = [], []

    def start_block(rgb, name=None):
        th = pystitch.EmbThread()
        th.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        th.description = name or 'Colour %d' % (len(block_colors) + 1)
        if not block_colors:
            s.pattern.add_thread(th)
        else:
            s.color_break(th)
        block_colors.append((rgb, th.description))

    if knockdown and pieces:
        from shapely.ops import unary_union
        start_block(pieces[0][0], 'Knockdown')
        core.sew_knockdown(s, unary_union([g for _r, g, _f in pieces]))

    for rgb, fns in core.plan_blocks(pieces):
        start_block(rgb)
        # the block's underlay first, then its top stitching; while the top
        # is pending, travel may run across the block's other pieces
        for under, _top in fns:
            if under:
                under(s)
        s.region_clear()
        for _r, g, _f in pieces:
            if _r == rgb:
                s.region_add(g)
        for _under, top in fns:
            top(s)
        s.region_clear()

    if s.count == 0:
        raise SvgError('the SVG contained no stitchable geometry')
    s.tie_off()
    s.pattern.end()
    if center:
        s.pattern.move_center_to_origin()

    for rgb, name in block_colors:
        layers.append({'name': name, 'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb})
    info = {'elements': len(elements), 'satin_columns': n_satin,
            'fills': n_fill, 'strokes': n_stroke,
            'natural_width_mm': round(natural_w_mm, 1),
            'page_w_mm': round(vw * unit2mm, 2),
            'page_h_mm': round(vh * unit2mm, 2) if vh else None,
            'scale': mult, 'warnings': warnings}
    return s.pattern, layers, info
