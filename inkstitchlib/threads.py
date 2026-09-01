"""Thread palette matching against Ink/Stitch's GIMP palette files.

The .gpl files in palettes/ are taken from inkstitch/inkstitch (GPL-3.0).
Each design colour is matched to the nearest thread in the chosen palette so
the worksheet and UI can name real threads, the way Ink/Stitch's "Apply
Palette" extension does.
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PALETTE_DIR = os.path.join(_ROOT, 'palettes')
# user-added thread brands (Business tab -> palette manager) live alongside
# the business database so they survive restarts
CUSTOM_DIR = os.path.join(
    os.environ.get('STITCHFORGE_DATA', os.path.join(_ROOT, 'data')), 'palettes')

_cache = {}


def _safe_name(name):
    name = re.sub(r'[^\w &+.\'()-]', '', str(name)).strip()
    if not name:
        raise ValueError('palette needs a name')
    return name[:80]


def available():
    """-> [{'name', 'custom'}] sorted, built-in first by name."""
    out = []
    for fn in sorted(os.listdir(PALETTE_DIR)):
        if fn.endswith('.gpl'):
            out.append({'name': fn[:-4].replace('InkStitch ', ''), 'custom': False})
    if os.path.isdir(CUSTOM_DIR):
        for fn in sorted(os.listdir(CUSTOM_DIR)):
            if fn.endswith('.gpl'):
                out.append({'name': fn[:-4], 'custom': True})
    return out


def _path_for(name):
    for cand in (os.path.join(CUSTOM_DIR, '%s.gpl' % name),
                 os.path.join(PALETTE_DIR, 'InkStitch %s.gpl' % name),
                 os.path.join(PALETTE_DIR, '%s.gpl' % name)):
        if os.path.exists(cand):
            return cand
    return None


def save_custom(name, thread_list):
    """thread_list: [{'name','number','hex'}]. Writes a GIMP .gpl file."""
    name = _safe_name(name)
    rows = []
    for t in thread_list:
        hexv = str(t.get('hex', '')).lstrip('#')
        if not re.fullmatch(r'[0-9a-fA-F]{6}', hexv):
            continue
        v = int(hexv, 16)
        tname = re.sub(r'\s+', ' ', str(t.get('name', '') or 'Unnamed')).strip()[:48]
        num = re.sub(r'\s+', '', str(t.get('number', '') or ''))[:16]
        rows.append('%3d %3d %3d  %28s  %s'
                    % ((v >> 16) & 255, (v >> 8) & 255, v & 255, tname, num))
    if not rows:
        raise ValueError('no valid colours — each thread needs a 6-digit hex value')
    os.makedirs(CUSTOM_DIR, exist_ok=True)
    with open(os.path.join(CUSTOM_DIR, '%s.gpl' % name), 'w', encoding='utf-8') as f:
        f.write('GIMP Palette\nName: %s\nColumns: 4\n# custom thread brand\n' % name)
        f.write('\n'.join(rows) + '\n')
    _cache.pop(name, None)
    return name


def delete_custom(name):
    p = os.path.join(CUSTOM_DIR, '%s.gpl' % _safe_name(name))
    if not os.path.exists(p):
        return False
    os.remove(p)
    _cache.pop(name, None)
    return True


def load(name):
    """-> [(r, g, b, thread_name, catalog_number), ...]"""
    if name in _cache:
        return _cache[name]
    path = _path_for(name)
    if path is None:
        raise FileNotFoundError('no palette named %r' % name)
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
    try:
        load(palette_name)
    except FileNotFoundError:
        palette_name = 'Madeira Rayon'
    out = []
    for L in layers:
        t = nearest(L['rgb'], palette_name)
        out.append({'layer': L['name'], 'hex': L['hex'],
                    'thread_name': t['name'], 'thread_number': t['number'],
                    'thread_hex': t['hex'], 'palette': palette_name})
    return out
