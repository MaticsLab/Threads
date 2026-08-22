"""Lettering with Ink/Stitch embroidery fonts (inkstitch/embroidery-fonts).

Each font ships a `font.json` (metrics, kerning) and an `ltr.svg` whose
`GlyphLayer-X` layers hold pre-digitized Ink/Stitch objects: satin columns
(a path whose subpaths are two rails plus zero or more rungs) and running
stitch paths.  This module implements the pieces of Ink/Stitch needed to sew
them without Inkscape:

* glyph extraction and layout (ported from lib/lettering/font.py, glyph.py):
  baseline guide, horiz_adv_x advances, kerning pairs, letter case, leading;
* rail/rung identification (ported from lib/elements/satin_column.py):
  rungs are the subpaths that intersect exactly two others, rails the rest,
  falling back to the two longest;
* satin stitching: rails are split into sections at each rung, both rails of
  a section are sampled evenly and the needle zigzags between them, with a
  centre-run underlay first;
* running stitch at the path's running_stitch_length_mm.
"""
import json
import math
import os
import re
from xml.etree import ElementTree as ET

import numpy as np
from shapely.geometry import LineString, MultiLineString

from svgelements import Path as SvgPath, Matrix, Line, Close, Move

PIXELS_PER_MM = 96 / 25.4
FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'fonts')

SVG_NS = 'http://www.w3.org/2000/svg'
INKSCAPE_NS = 'http://www.inkscape.org/namespaces/inkscape'
SODIPODI_NS = 'http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd'
INKSTITCH_NS = 'http://inkstitch.org/namespace'

_font_cache = {}


# ----------------------------------------------------------------- loading
def available_fonts():
    out = []
    for d in sorted(os.listdir(FONT_DIR)):
        fj = os.path.join(FONT_DIR, d, 'font.json')
        if not os.path.exists(fj):
            continue
        with open(fj, encoding='utf-8') as f:
            j = json.load(f)
        out.append({'id': d, 'name': j.get('name', d),
                    'description': j.get('description', ''),
                    'size_mm': j.get('size'),
                    'min_scale': j.get('min_scale', 1), 'max_scale': j.get('max_scale', 1),
                    'letter_case': j.get('letter_case', ''),
                    'glyphs': len(j.get('glyphs', [])) or None,
                    'preview': os.path.exists(os.path.join(FONT_DIR, d, 'preview.png'))})
    return out


def _flatten(path, step=0.6):
    """Flatten an svgelements Path into subpath polylines (font units)."""
    subpaths = []
    cur = []
    for seg in path:
        if isinstance(seg, Move):
            if len(cur) > 1:
                subpaths.append(cur)
            cur = [(seg.end.x, seg.end.y)] if seg.end is not None else []
            continue
        if seg.start is None or seg.end is None:
            continue
        if not cur:
            cur = [(seg.start.x, seg.start.y)]
        if isinstance(seg, (Line, Close)):
            cur.append((seg.end.x, seg.end.y))
        else:
            try:
                length = seg.length(error=1e-3)
            except Exception:
                length = 0
            n = max(2, int(math.ceil((length or 0) / step)))
            for i in range(1, n + 1):
                p = seg.point(i / n)
                cur.append((p.x, p.y))
    if len(cur) > 1:
        subpaths.append(cur)
    return subpaths


class Glyph:
    __slots__ = ('elements', 'min_x', 'width')

    def __init__(self, elements):
        self.elements = elements
        xs = [p[0] for el in elements for sub in el['subpaths'] for p in sub]
        self.min_x = min(xs) if xs else 0.0
        self.width = (max(xs) - self.min_x) if xs else 0.0


class Font:
    def __init__(self, font_id):
        d = os.path.join(FONT_DIR, font_id)
        with open(os.path.join(d, 'font.json'), encoding='utf-8') as f:
            self.meta = json.load(f)
        self.id = font_id
        self.name = self.meta.get('name', font_id)
        self.size = float(self.meta.get('size', 20))
        self.min_scale = float(self.meta.get('min_scale', 1))
        self.max_scale = float(self.meta.get('max_scale', 1))
        self.leading = float(self.meta.get('leading') or self.size * PIXELS_PER_MM * 1.5)
        self.letter_case = self.meta.get('letter_case', '')
        self.default_glyph = self.meta.get('default_glyph', ' ')
        self.kerning = self.meta.get('kerning_pairs', {})
        self.horiz_adv_x = self.meta.get('horiz_adv_x', {})
        self.horiz_adv_x_default = self.meta.get('horiz_adv_x_default')
        self.word_spacing = self.meta.get('horiz_adv_x_space', 20)
        self.glyphs = {}
        self.baseline = 0.0
        self._parse_svg(os.path.join(d, 'ltr.svg'))

    def _parse_svg(self, path):
        tree = ET.parse(path)
        root = tree.getroot()

        # document height in user units, for the Inkscape Y-flip of guides
        vb = root.get('viewBox')
        if vb:
            doc_h = float(vb.split()[3])
        else:
            doc_h = float(re.sub(r'[a-z%]+$', '', root.get('height', '150')))

        for guide in root.iter('{%s}guide' % SODIPODI_NS):
            if guide.get('{%s}label' % INKSCAPE_NS) == 'baseline':
                pos = guide.get('position', '0,0').split(',')
                self.baseline = doc_h - float(pos[1])
                break

        for g in root.iter('{%s}g' % SVG_NS):
            label = g.get('{%s}label' % INKSCAPE_NS, '')
            if not label.startswith('GlyphLayer-'):
                continue
            char = label[len('GlyphLayer-'):]
            elements = self._parse_layer(g)
            if elements:
                self.glyphs[char] = Glyph(elements)

    def _parse_layer(self, layer):
        elements = []

        def walk(el, matrix):
            tr = el.get('transform')
            if tr:
                matrix = Matrix(tr) * matrix if matrix else Matrix(tr)
            tag = el.tag.split('}')[-1]
            if tag == 'path':
                d = el.get('d')
                if d:
                    p = SvgPath(d)
                    if matrix:
                        p *= matrix
                    subpaths = _flatten(p)
                    if subpaths:
                        elements.append({
                            'satin': el.get('{%s}satin_column' % INKSTITCH_NS, '').lower() == 'true',
                            'subpaths': subpaths,
                            'zigzag_spacing': float(el.get('{%s}zigzag_spacing_mm' % INKSTITCH_NS, 0.3) or 0.3),
                            'run_length': float(el.get('{%s}running_stitch_length_mm' % INKSTITCH_NS, 2.0) or 2.0),
                        })
            for child in el:
                walk(child, matrix)

        for child in layer:
            walk(child, Matrix(layer.get('transform')) if layer.get('transform') else None)
        return elements


def get_font(font_id):
    if font_id not in _font_cache:
        _font_cache[font_id] = Font(font_id)
    return _font_cache[font_id]


# ------------------------------------------------------------ satin engine
def _rail_indices(polylines):
    """Ink/Stitch's rail/rung rule (lib/elements/satin_column.py)."""
    paths = [LineString(p) for p in polylines if len(p) > 1]
    n = len(paths)
    if n <= 2:
        return list(range(n))
    counts = [sum(paths[i].intersects(paths[j]) for j in range(n) if i != j) for i in range(n)]
    if n == 3:
        possible = [i for i in range(n) if counts[i] == 1 and paths[i].length > 0.1]
    else:
        possible = [i for i in range(n) if counts[i] > 2 and paths[i].length > 0.1]
    if len(possible) == 2:
        return possible
    return sorted(range(n), key=lambda i: paths[i].length, reverse=True)[:2]


def _satin_zigzag(subpaths_mm, spacing_mm):
    """-> list of stitch points zigzagging between the two rails."""
    idx = _rail_indices(subpaths_mm)
    if len(idx) < 2:
        return []
    r1 = LineString(subpaths_mm[idx[0]])
    r2 = LineString(subpaths_mm[idx[1]])
    rungs = [LineString(p) for i, p in enumerate(subpaths_mm)
             if i not in idx and len(p) > 1]

    # rung crossings as arc positions along each rail
    cuts = []
    for rung in rungs:
        try:
            i1 = rung.intersection(r1)
            i2 = rung.intersection(r2)
        except Exception:
            continue
        if i1.is_empty or i2.is_empty:
            continue
        p1 = i1.geoms[0] if hasattr(i1, 'geoms') else i1
        p2 = i2.geoms[0] if hasattr(i2, 'geoms') else i2
        if p1.geom_type != 'Point':
            p1 = p1.representative_point()
        if p2.geom_type != 'Point':
            p2 = p2.representative_point()
        cuts.append((r1.project(p1), r2.project(p2)))
    cuts.sort()

    # rails drawn in opposite directions twist the column; detect and flip
    if cuts and len(cuts) >= 2:
        s2 = [c[1] for c in cuts]
        if all(s2[i] >= s2[i + 1] for i in range(len(s2) - 1)):
            r2 = LineString(list(r2.coords)[::-1])
            cuts = sorted((a, r2.length - b) for a, b in cuts)
    elif not cuts:
        # no rungs: align by endpoint proximity
        a0, b0 = np.array(r1.coords[0]), np.array(r1.coords[-1])
        c0, c1 = np.array(r2.coords[0]), np.array(r2.coords[-1])
        if np.linalg.norm(a0 - c0) + np.linalg.norm(b0 - c1) > \
           np.linalg.norm(a0 - c1) + np.linalg.norm(b0 - c0):
            r2 = LineString(list(r2.coords)[::-1])

    sections = []
    prev = (0.0, 0.0)
    for c in cuts:
        sections.append((prev, c))
        prev = c
    sections.append((prev, (r1.length, r2.length)))

    points = []
    side = 0
    for (a1, a2), (b1, b2) in sections:
        seg1, seg2 = max(b1 - a1, 0), max(b2 - a2, 0)
        length = max(seg1, seg2)
        if length < 1e-3:
            continue
        n = max(1, int(round(length / max(spacing_mm, 0.1))))
        for i in range(n + 1):
            t = i / n
            p1 = r1.interpolate(a1 + seg1 * t)
            p2 = r2.interpolate(a2 + seg2 * t)
            p = p1 if side == 0 else p2
            points.append((p.x, p.y))
            side ^= 1
    return points


def _center_run(subpaths_mm, step=2.0):
    """Centre-line points between the rails, for underlay."""
    idx = _rail_indices(subpaths_mm)
    if len(idx) < 2:
        return []
    r1 = LineString(subpaths_mm[idx[0]])
    r2 = LineString(subpaths_mm[idx[1]])
    n = max(2, int(round(max(r1.length, r2.length) / step)))
    out = []
    for i in range(n + 1):
        t = i / n
        p1, p2 = r1.interpolate(t, normalized=True), r2.interpolate(t, normalized=True)
        out.append(((p1.x + p2.x) / 2, (p1.y + p2.y) / 2))
    return out


def _resample(points, step):
    p = np.asarray(points, float)
    if len(p) < 2:
        return [tuple(q) for q in p]
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] < 1e-9:
        return [tuple(p[0])]
    n = max(1, int(round(s[-1] / step)))
    t = np.linspace(0, s[-1], n + 1)
    return list(zip(np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])))


# ---------------------------------------------------------------- layout
class LetteringError(ValueError):
    pass


def layout(font, text, height_mm=None, letter_spacing_mm=0.0):
    """Place glyphs -> [(element, translate(x, y) in font units)], plus scale."""
    if height_mm:
        scale = height_mm / font.size
    else:
        scale = 1.0
    scale = max(font.min_scale, min(font.max_scale, scale))

    if font.letter_case == 'upper':
        text = text.upper()
    elif font.letter_case == 'lower':
        text = text.lower()

    placed = []
    pen_y = 0.0
    ls_units = letter_spacing_mm * PIXELS_PER_MM / scale
    missing = set()
    for line in text.split('\n'):
        pen_x = 0.0
        prev_char = None
        for ch in line:
            if ch == ' ':
                pen_x += font.word_spacing
                prev_char = None
                continue
            glyph = font.glyphs.get(ch)
            if glyph is None and ch != font.default_glyph:
                glyph = font.glyphs.get(font.default_glyph)
                missing.add(ch)
            if glyph is None:
                continue
            kern = 0.0
            if prev_char is not None:
                kern = font.kerning.get('%s %s' % (prev_char, ch),
                                        font.kerning.get(prev_char + ch, 0.0))
            x = pen_x - kern + ls_units
            for el in glyph.elements:
                placed.append((el, x, pen_y))
            adv = font.horiz_adv_x.get(ch, font.horiz_adv_x_default)
            if adv is None:
                adv = glyph.width + glyph.min_x
            pen_x = x + adv
            prev_char = ch
        pen_y += font.leading
    if not placed:
        raise LetteringError('none of the requested characters exist in this font')
    return placed, scale, sorted(missing)


def stitch_text(sewer, font_id, text, height_mm=None, letter_spacing_mm=0.0):
    """Sew text into a digitizer.core.Sewer. Returns info dict."""
    font = get_font(font_id)
    placed, scale, missing = layout(font, text, height_mm, letter_spacing_mm)

    k = scale / PIXELS_PER_MM        # font units -> mm
    n_satin = n_run = 0
    for el, ox, oy in placed:
        subs_mm = [[((x + ox - 0) * k, (y + oy - font.baseline) * k) for x, y in sub]
                   for sub in el['subpaths']]
        if el['satin'] and len(subs_mm) >= 2:
            zz = _satin_zigzag(subs_mm, el['zigzag_spacing'])
            if not zz:
                continue
            under = _center_run(subs_mm)
            start = under[0] if under else zz[0]
            sewer.move_to(start, None)
            for p in under:
                sewer.run_to(p, 2.5)
            for p in reversed(under):
                sewer.run_to(p, 2.5)
            for p in zz:
                sewer._st(p)
            n_satin += 1
        else:
            for sub in subs_mm:
                pts = _resample(sub, el['run_length'])
                if len(pts) < 2:
                    continue
                sewer.move_to(pts[0], None)
                for p in pts[1:]:
                    sewer.run_to(p, el['run_length'] + 0.1)
                n_run += 1
    return {'font': font.name, 'scale': round(scale, 3),
            'height_mm': round(font.size * scale, 1),
            'satin_columns': n_satin, 'running_paths': n_run,
            'missing_chars': missing}


# public names, shared with the SVG digitizer (svginput.py)
satin_zigzag = _satin_zigzag
center_run = _center_run
flatten_path = _flatten
resample_polyline = _resample
