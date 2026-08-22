"""Features ported from the Ink/Stitch project into StitchForge.

stitch_svg  - stitch plan SVG + realistic preview (from inkstitch lib/svg/rendering.py)
worksheet   - print worksheet PDF (from inkstitch print templates) with the
              quote sheet ported from inkstitch/embTools-1
lettering   - text stitching with Ink/Stitch lettering fonts (inkstitch/embroidery-fonts)
threads     - thread palette matching against Ink/Stitch .gpl palettes

Ink/Stitch and embTools are GPL-3.0; see LICENSE at the repository root.
"""
import os as _os
import sys as _sys

_tp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'third_party')
if _tp not in _sys.path:
    _sys.path.insert(0, _tp)
