"""StitchForge digitizing package.

Puts the vendored third_party directory (pystitch) on sys.path before any
submodule imports it.
"""
import os as _os
import sys as _sys

_tp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'third_party')
if _tp not in _sys.path:
    _sys.path.insert(0, _tp)
