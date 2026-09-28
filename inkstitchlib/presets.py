"""Fabric and machine presets.

A fabric preset is the set of stitch settings a digitizer reaches for on
that material: density, how much underlay, how much pull compensation, and
whether the pile needs a knockdown stitch first. A machine preset carries
the machine's limits (satin width, jump-before-trim distance) and its
native file format. The UI applies a preset to the ordinary controls, so
every value stays visible and editable.
"""

FABRICS = [
    {'id': 'twill', 'name': 'Cotton twill / denim', 'density': 0.35,
     'underlay': 'auto', 'pull_comp': 0.20, 'knockdown': False, 'max_satin': 8.0,
     'cap_mode': False, 'note': 'Stable woven — standard settings.'},
    {'id': 'pique', 'name': 'Piqué / polo knit', 'density': 0.38,
     'underlay': 'auto', 'pull_comp': 0.30, 'knockdown': False, 'max_satin': 7.5,
     'cap_mode': False, 'note': 'Knit stretches: more pull compensation, slightly open density.'},
    {'id': 'jersey', 'name': 'Jersey / performance knit', 'density': 0.40,
     'underlay': 'heavy', 'pull_comp': 0.40, 'knockdown': False, 'max_satin': 6.5,
     'cap_mode': False, 'note': 'Thin, stretchy: heavy underlay holds the shape, short satin.'},
    {'id': 'fleece', 'name': 'Fleece / sweatshirt', 'density': 0.35,
     'underlay': 'heavy', 'pull_comp': 0.30, 'knockdown': True, 'max_satin': 7.0,
     'cap_mode': False, 'note': 'Pile: knockdown stitch first so the design does not sink.'},
    {'id': 'towel', 'name': 'Towel / terry', 'density': 0.33,
     'underlay': 'heavy', 'pull_comp': 0.30, 'knockdown': True, 'max_satin': 7.0,
     'cap_mode': False, 'note': 'Deep pile: knockdown + heavy underlay + tighter density.'},
    {'id': 'cap', 'name': 'Structured cap', 'density': 0.35,
     'underlay': 'auto', 'pull_comp': 0.25, 'knockdown': False, 'max_satin': 7.0,
     'cap_mode': True, 'note': 'Sews centre-out and bottom-up so the crown stays flat.'},
    {'id': 'canvas', 'name': 'Canvas / bags', 'density': 0.33,
     'underlay': 'light', 'pull_comp': 0.15, 'knockdown': False, 'max_satin': 8.0,
     'cap_mode': False, 'note': 'Firm: light underlay is enough.'},
    {'id': 'leather', 'name': 'Leather / vinyl', 'density': 0.40,
     'underlay': 'light', 'pull_comp': 0.10, 'knockdown': False, 'max_satin': 7.0,
     'cap_mode': False, 'note': 'Every needle hole is permanent: open density, little underlay.'},
]

MACHINES = [
    {'id': 'tajima', 'name': 'Tajima', 'max_satin': 8.0, 'trim_dist': 3.0,
     'format': 'dst', 'max_stitch_mm': 12.1},
    {'id': 'brother_home', 'name': 'Brother / Baby Lock (single-needle)', 'max_satin': 7.0,
     'trim_dist': 3.0, 'format': 'pes', 'max_stitch_mm': 7.0},
    {'id': 'brother_pr', 'name': 'Brother PR / Baby Lock multi-needle', 'max_satin': 8.0,
     'trim_dist': 2.5, 'format': 'pes', 'max_stitch_mm': 12.7},
    {'id': 'janome', 'name': 'Janome', 'max_satin': 7.0, 'trim_dist': 3.0,
     'format': 'jef', 'max_stitch_mm': 12.7},
    {'id': 'ricoma', 'name': 'Ricoma', 'max_satin': 8.0, 'trim_dist': 2.5,
     'format': 'dst', 'max_stitch_mm': 12.7},
    {'id': 'melco', 'name': 'Melco / Bernina', 'max_satin': 8.0, 'trim_dist': 2.5,
     'format': 'exp', 'max_stitch_mm': 12.7},
    {'id': 'husqvarna', 'name': 'Husqvarna Viking / Pfaff', 'max_satin': 7.0,
     'trim_dist': 3.0, 'format': 'vp3', 'max_stitch_mm': 12.7},
    {'id': 'barudan', 'name': 'Barudan', 'max_satin': 8.0, 'trim_dist': 2.5,
     'format': 'u01', 'max_stitch_mm': 12.7},
]


def all_presets():
    return {'fabrics': FABRICS, 'machines': MACHINES}


def clean_underlay(v, default='auto'):
    return v if v in ('auto', 'light', 'heavy', 'none') else default


def clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))
