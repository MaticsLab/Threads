"""Stitch plan SVG output, ported from Ink/Stitch (lib/svg/rendering.py).

Two render modes:

* line mode - one polyline per thread run, the classic stitch plan view.
* realistic mode - every stitch drawn as a rounded thread-shaped path lit by
  the feSpecularLighting filter Ink/Stitch uses for its realistic preview.

Ink/Stitch works in CSS pixels (96/25.4 px per mm); the same scale is kept
here so the ported filter constants behave identically.
"""
import math

import pystitch

PIXELS_PER_MM = 96 / 25.4

# The stitch vector path looks like this:
#  _______
# (_______)
#
# It's 0.32mm high, which is the approximate thickness of common machine
# embroidery threads. 1.398 pixels = 0.37mm
STITCH_HEIGHT = 1.398

# Starts at the upper right corner of the stitch shape, proceeds
# counter-clockwise, %s is the stitch length. The zero-width "whiskers" pad
# the filter region so the gaussian blur doesn't artefact at the endcaps.
STITCH_PATH = (
    "M0,0"
    "l0.55,-0.1,-0.55,0.1"
    "c0.613,0,0.613,1.4,0,1.4"
    "l0.55,0.1,-0.55,-0.1"
    "h-%s"
    "l-0.55,0.1,0.55,-0.1"
    "c-0.613,0,-0.613,-1.4,0,-1.4"
    "l-0.55,-0.1,0.55,0.1"
    "z")

REALISTIC_FILTER = """
<filter id="realistic-stitch-filter" x="0" y="0" width="1" height="1"
        style="color-interpolation-filters:sRGB">
  <feGaussianBlur stdDeviation="0.9" edgeMode="none" in="SourceAlpha"/>
  <feSpecularLighting result="result2" surfaceScale="4.29"
                      specularConstant="0.65" specularExponent="1.6">
    <feDistantLight azimuth="154" elevation="112"/>
  </feSpecularLighting>
  <feComposite in2="SourceAlpha" operator="atop"/>
  <feComposite in2="SourceGraphic" operator="arithmetic" result="result3"
               k1="0" k2="0.8" k3="1.2" k4="0"/>
</filter>
"""


def _blocks(pattern):
    """Split a pystitch pattern into color blocks of point lists.

    Returns [(hex_color, [ [ (x_px, y_px), ... ], ... ]), ...] with a new
    point list started at every trim or jump, mirroring Ink/Stitch's
    color_block_to_point_lists.
    """
    k = PIXELS_PER_MM / 10.0          # pattern units are 0.1 mm
    threads = pattern.threadlist
    blocks = [[[]]]
    for x, y, cmd in pattern.stitches:
        c = cmd & 0xFF
        if c == pystitch.COLOR_CHANGE:
            blocks.append([[]])
        elif c in (pystitch.TRIM, pystitch.JUMP):
            if blocks[-1][-1]:
                blocks[-1].append([])
        elif c == pystitch.STITCH:
            blocks[-1][-1].append((x * k, y * k))
    out = []
    for i, runs in enumerate(blocks):
        runs = [r for r in runs if len(r) > 1]
        if not runs:
            continue
        color = '#%06X' % (threads[i].color & 0xFFFFFF) if i < len(threads) else '#000000'
        out.append((color, runs))
    return out


def _bounds(blocks, margin_mm=2.0):
    xs, ys = [], []
    for _, runs in blocks:
        for r in runs:
            xs.extend(p[0] for p in r)
            ys.extend(p[1] for p in r)
    if not xs:
        return 0, 0, 10, 10
    m = margin_mm * PIXELS_PER_MM
    return min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m


def realistic_stitch(start, end):
    """One stitch as a thread-shaped path string (ported from Ink/Stitch)."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    center = ((end[0] + start[0]) / 2.0, (end[1] + start[1]) / 2.0)
    angle = math.degrees(math.atan2(dy, dx))
    length = max(0, length - 0.2 * PIXELS_PER_MM)

    rcx = -length / 2.0
    rcy = STITCH_HEIGHT / 2.0
    transform = 'translate(%.3f,%.3f) rotate(%.2f,%.3f,%.3f)' % (
        center[0] - rcx, center[1] - rcy, angle, rcx, rcy)
    return STITCH_PATH % round(length, 3), transform


def _darker(hex_color, factor=0.75):
    """Realistic stitches use a darker base so the specular light reads."""
    v = int(hex_color.lstrip('#'), 16)
    r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
    return '#%02X%02X%02X' % (int(r * factor), int(g * factor), int(b * factor))


def render(pattern, realistic=False, jumps=False):
    """Render a pystitch pattern to a standalone SVG string (px = CSS px)."""
    blocks = _blocks(pattern)
    x0, y0, x1, y1 = _bounds(blocks)
    w, h = x1 - x0, y1 - y0
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" '
             'viewBox="%.2f %.2f %.2f %.2f" width="%.1fmm" height="%.1fmm">'
             % (x0, y0, w, h, w / PIXELS_PER_MM, h / PIXELS_PER_MM)]

    if realistic:
        parts.append('<defs>%s</defs>' % REALISTIC_FILTER)
        for color, runs in blocks:
            fill = _darker(color)
            parts.append('<g fill="%s" stroke="none">' % fill)
            for run in runs:
                prev = run[0]
                for pt in run[1:]:
                    d, tr = realistic_stitch(prev, pt)
                    parts.append(
                        '<path d="%s" transform="%s" '
                        'style="filter:url(#realistic-stitch-filter)"/>' % (d, tr))
                    prev = pt
            parts.append('</g>')
    else:
        last_end = None
        for color, runs in blocks:
            parts.append('<g fill="none" stroke="%s" stroke-width="%.3f" '
                         'stroke-linejoin="round" stroke-linecap="round">'
                         % (color, 0.4 * PIXELS_PER_MM))
            for run in runs:
                if jumps and last_end is not None:
                    parts.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" '
                                 'stroke-dasharray="2 2" stroke-opacity="0.4" '
                                 'stroke-width="%.3f"/>'
                                 % (last_end[0], last_end[1], run[0][0], run[0][1],
                                    0.15 * PIXELS_PER_MM))
                d = 'M' + ' L'.join('%.2f,%.2f' % p for p in run)
                parts.append('<path d="%s"/>' % d)
                last_end = run[-1]
            parts.append('</g>')

    parts.append('</svg>')
    return '\n'.join(parts)


def stitch_json(pattern):
    """Stitch blocks as plain data for the front-end stitch player
    (feature ported from embTools' stitch player, drawn client-side)."""
    k = PIXELS_PER_MM / 10.0
    blocks = []
    threads = pattern.threadlist
    ci = 0
    cur = {'color': '#%06X' % (threads[0].color & 0xFFFFFF) if threads else '#000000',
           'runs': [[]]}
    blocks.append(cur)
    for x, y, cmd in pattern.stitches:
        c = cmd & 0xFF
        if c == pystitch.COLOR_CHANGE:
            ci += 1
            color = '#%06X' % (threads[ci].color & 0xFFFFFF) if ci < len(threads) else '#000000'
            cur = {'color': color, 'runs': [[]]}
            blocks.append(cur)
        elif c in (pystitch.TRIM, pystitch.JUMP):
            if cur['runs'][-1]:
                cur['runs'].append([])
        elif c == pystitch.STITCH:
            cur['runs'][-1].append([round(x * k, 2), round(y * k, 2)])
    for b in blocks:
        b['runs'] = [r for r in b['runs'] if len(r) > 1]
    return [b for b in blocks if b['runs']]
