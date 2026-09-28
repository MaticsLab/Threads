"""Patterned tatami fills.

A tatami fill is rows of running stitch; where the needle goes down in each
row decides the texture. Plain tatami staggers the needle points so no
ridge shows. A patterned fill instead places them on the rows where a
tiled motif crosses — columns, waves, diamonds, hearts — so the motif
reads in the finished fill the way it does in commercial "decorative
fills".

Every pattern is a function f(y, x0, x1, row) -> sorted needle-point x
positions inside [x0, x1], in the fill's own frame (x along the rows, y
across them, both in mm). core.sew_fill() rotates each row into that
frame, asks the pattern where to put the needle, and sews.
"""
import math

import numpy as np


def _tri(u):
    """Triangle wave of period 1 in -1..1."""
    u = u - math.floor(u)
    return 4.0 * abs(u - 0.5) - 1.0


def _cols(period, shift):
    def f(y, x0, x1, row):
        off = shift(y, row)
        k0 = int(math.floor((x0 - off) / period))
        k1 = int(math.ceil((x1 - off) / period))
        return [k * period + off for k in range(k0, k1 + 1) if x0 <= k * period + off <= x1]
    return f


def _two(period, amp, tile):
    """Two crossing zigzag families: k*period ± amp*tri(y/tile) -> diamonds."""
    def f(y, x0, x1, row):
        t = amp * _tri(y / tile)
        out = []
        for off in (t, -t):
            k0 = int(math.floor((x0 - off) / period))
            k1 = int(math.ceil((x1 - off) / period))
            out += [k * period + off for k in range(k0, k1 + 1) if x0 <= k * period + off <= x1]
        return sorted(out)
    return f


def _heart_tile(size, n=720):
    """The classic heart curve, scaled into a size×size tile, as a closed
    polyline (x, y) with y down."""
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    x = 16 * np.sin(t) ** 3
    y = -(13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t))
    x = (x - x.min()) / (x.max() - x.min())
    y = (y - y.min()) / (y.max() - y.min())
    pad = 0.08
    pts = np.column_stack([(pad + x * (1 - 2 * pad)) * size, (pad + y * (1 - 2 * pad)) * size])
    return pts


def _crossings(poly, y):
    """x positions where the closed polyline crosses the horizontal line y."""
    xs = []
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 <= y < y2) or (y2 <= y < y1):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    return xs


def _motif(size, tile_pts, stagger=True):
    """A motif tiled every `size` mm in both directions (alternate bands
    shifted half a tile so the motifs pack), needle points where the row
    crosses its outline. Crossings are cached per 0.05 mm of y."""
    cache = {}

    def f(y, x0, x1, row):
        band = int(math.floor(y / size))
        yy = y - band * size
        key = int(round(yy / 0.05))
        if key not in cache:
            cache[key] = sorted(_crossings(tile_pts, yy))
        xs = cache[key]
        if not xs:
            return []
        off = size / 2.0 if (stagger and band % 2) else 0.0
        k0 = int(math.floor((x0 - off) / size)) - 1
        k1 = int(math.ceil((x1 - off) / size)) + 1
        out = [k * size + off + x for k in range(k0, k1 + 1) for x in xs]
        return [x for x in sorted(out) if x0 <= x <= x1]
    return f


# name -> (label, factory(stitch_len) -> f)
PATTERN_FILLS = {
    'columns': ('Columns', lambda L: _cols(L, lambda y, row: 0.0)),
    'offset_columns': ('Offset Columns', lambda L: _cols(L, lambda y, row: (row % 2) * L / 2.0)),
    'waves': ('Waves', lambda L: _cols(L, lambda y, row: (L / 3.0) * math.sin(2 * math.pi * y / 8.0))),
    'triangle': ('Triangle', lambda L: _cols(L, lambda y, row: (L / 3.0) * _tri(y / 8.0))),
    'checks': ('Checks', lambda L: _cols(4.0, lambda y, row: (int(math.floor(y / 4.0)) % 2) * 2.0)),
    'diamonds_sm': ('Diamonds (sm)', lambda L: _two(2.5, 1.25, 2.5)),
    'diamonds_md': ('Diamonds (md)', lambda L: _two(4.0, 2.0, 4.0)),
    'diamonds_lg': ('Diamonds (lg)', lambda L: _two(6.0, 3.0, 6.0)),
    'hearts_sm': ('Hearts (sm)', lambda L: _motif(4.0, _heart_tile(4.0))),
    'hearts_md': ('Hearts (md)', lambda L: _motif(6.0, _heart_tile(6.0))),
    'hearts_lg': ('Hearts (lg)', lambda L: _motif(8.0, _heart_tile(8.0))),
}

FILL_LABELS = {'tatami': 'Tatami', 'satin': 'Satin', 'walk': 'Walk', 'contour': 'Contour',
               'circular': 'Circular', **{k: v[0] for k, v in PATTERN_FILLS.items()}}


def make(name, stitch_len):
    """-> the needle-point function for a pattern fill, or None."""
    if name not in PATTERN_FILLS:
        return None
    return PATTERN_FILLS[name][1](max(1.5, min(6.0, float(stitch_len))))
