"""Thread palette matching against Ink/Stitch's GIMP palette files.

The .gpl files in palettes/ are taken from inkstitch/inkstitch (GPL-3.0).
Each design colour is matched to the nearest thread in the chosen palette so
the worksheet and UI can name real threads, the way Ink/Stitch's "Apply
Palette" extension does.
"""
import os
import re

PALETTE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'palettes')

_cache = {}


def available():
    out = []
    for fn in sorted(os.listdir(PALETTE_DIR)):
        if fn.endswith('.gpl'):
            out.append(fn[:-4].replace('InkStitch ', ''))
    return out


def load(name):
    """-> [(r, g, b, thread_name, catalog_number), ...]"""
    if name in _cache:
        return _cache[name]
    path = os.path.join(PALETTE_DIR, 'InkStitch %s.gpl' % name)
    if not os.path.exists(path):
        path = os.path.join(PALETTE_DIR, '%s.gpl' % name)
    threads = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(('#', 'GIMP', 'Name:', 'Columns:')):
                continue
            m = re.match(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$', line)
            if not m:
                continue
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            rest = m.group(4).strip()
            num = ''
            nm = re.match(r'^(.*?)\s+(\S+)$', rest)
            if nm and any(ch.isdigit() for ch in nm.group(2)):
                rest, num = nm.group(1).strip(), nm.group(2)
            threads.append((r, g, b, rest or 'Unnamed', num))
    _cache[name] = threads
    return threads


def nearest(rgb, palette_name):
    """Nearest thread by weighted RGB distance (red-mean approximation)."""
    threads = load(palette_name)
    r0, g0, b0 = rgb
    best, bd = None, 1e18
    for r, g, b, name, num in threads:
        rm = (r + r0) / 2
        d = (2 + rm / 256) * (r - r0) ** 2 + 4 * (g - g0) ** 2 + (2 + (255 - rm) / 256) * (b - b0) ** 2
        if d < bd:
            bd, best = d, {'name': name, 'number': num,
                           'hex': '#%02X%02X%02X' % (r, g, b), 'rgb': (r, g, b)}
    return best


def threadlist_txt(design_name, report, layers, matches=None):
    """Plain-text thread list, after Ink/Stitch's threadlist export."""
    lines = ['Design: %s' % design_name,
             'Size: %.1f x %.1f mm' % (report.get('width_mm', 0), report.get('height_mm', 0)),
             'Stitches: %s   Colour changes: %s' % (report.get('stitches', 0),
                                                    report.get('colour_changes', 0)),
             '', 'Thread order:']
    for i, L in enumerate(layers):
        row = '%2d  %-12s %s' % (i + 1, L.get('name', ''), L['hex'])
        if matches and i < len(matches):
            t = matches[i]
            row += '   %s %s %s' % (t['palette'], t['thread_name'],
                                    ('#' + t['thread_number']) if t['thread_number'] else '')
        lines.append(row)
    return '\n'.join(lines) + '\n'


def match_layers(layers, palette_name):
    """Attach the nearest thread of the palette to each colour layer dict."""
    out = []
    for L in layers:
        t = nearest(L['rgb'], palette_name)
        out.append({'layer': L['name'], 'hex': L['hex'],
                    'thread_name': t['name'], 'thread_number': t['number'],
                    'thread_hex': t['hex'], 'palette': palette_name})
    return out
