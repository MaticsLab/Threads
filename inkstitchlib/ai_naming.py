"""Semantic layer names from a vision model.

The heuristic layer names from vectorize() describe shape traits ("round ×8",
"shape 1"). When an Anthropic API key is configured on the server, this module
shows Claude the original artwork next to a numbered map of the extracted
layers and asks it to name each layer by what it depicts ("Heads", "Arms
ring", "Letter S"). Without a key the app keeps the heuristic names.

Configuration: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the server
environment — Railway service variables, or a Cloudflare Worker secret passed
into the container.
"""
import base64
import io
import json
import os
import re

from PIL import Image, ImageDraw

MODEL = 'claude-opus-5'
MAX_SIDE = 1024


def available():
    return bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))


def _b64_png(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return base64.standard_b64encode(buf.getvalue()).decode()


def _image_block(im):
    return {'type': 'image',
            'source': {'type': 'base64', 'media_type': 'image/png',
                       'data': _b64_png(im)}}


def _layer_map_image(layers):
    """Render the extracted layers with a number badge on each, so the model
    can tie layer ids to what it sees in the artwork."""
    xs, ys = [], []
    for L in layers:
        for poly in L.get('polys', []):
            for x, y in poly.get('shell', []):
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ValueError('layers carry no geometry')
    minx, miny = min(xs), min(ys)
    w_mm = max(max(xs) - minx, 1.0)
    h_mm = max(max(ys) - miny, 1.0)
    s = MAX_SIDE / max(w_mm, h_mm)
    pad = 12
    im = Image.new('RGB', (int(w_mm * s) + 2 * pad, int(h_mm * s) + 2 * pad),
                   (255, 255, 255))
    d = ImageDraw.Draw(im)
    T = lambda p: ((p[0] - minx) * s + pad, (p[1] - miny) * s + pad)

    for L in layers:
        color = L.get('color', '#888888')
        for poly in L.get('polys', []):
            shell = [T(p) for p in poly.get('shell', [])]
            if len(shell) >= 3:
                d.polygon(shell, fill=color)
            for hole in poly.get('holes', []):
                hp = [T(p) for p in hole]
                if len(hp) >= 3:
                    d.polygon(hp, fill=(255, 255, 255))
    # badges on top of everything
    for L in layers:
        polys = L.get('polys', [])
        if not polys:
            continue
        big = max(polys, key=lambda p: len(p.get('shell', [])))
        sh = big.get('shell', [])
        if not sh:
            continue
        cx = sum(p[0] for p in sh) / len(sh)
        cy = sum(p[1] for p in sh) / len(sh)
        bx, by = T((cx, cy))
        r = 14
        d.ellipse([bx - r, by - r, bx + r, by + r], fill=(17, 19, 26),
                  outline=(255, 255, 255), width=2)
        label = str(L['id'])
        d.text((bx - 4 * len(label), by - 7), label, fill=(255, 255, 255))
    return im


def name_layers(image_path, layers):
    """-> {layer_id: name}. Raises RuntimeError with a readable message on
    any upstream problem; callers keep the heuristic names in that case."""
    import anthropic

    original = Image.open(image_path).convert('RGB')
    original.thumbnail((MAX_SIDE, MAX_SIDE))
    layer_map = _layer_map_image(layers)

    listing = '\n'.join(
        '%d. currently "%s", thread colour %s, %d shape%s' %
        (L['id'], L.get('name', ''), L.get('color', ''), len(L.get('polys', [])),
         's' if len(L.get('polys', [])) != 1 else '')
        for L in layers)

    prompt = (
        'The first image is artwork being digitized for machine embroidery. '
        'The second image shows the same artwork separated into numbered '
        'vector layers (one number badge per layer).\n\n'
        'Layers:\n%s\n\n'
        'Name each layer with a short, specific name (1-3 words) describing '
        'what it depicts — e.g. "Heads", "Arms ring", "Letter S", "Outer dots", '
        '"Background star". Reply with ONLY a JSON object mapping each layer '
        'number to its name, like {"1": "Heads", "2": "Letter S"}.' % listing)

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={'effort': 'low'},
            messages=[{'role': 'user', 'content': [
                _image_block(original),
                _image_block(layer_map),
                {'type': 'text', 'text': prompt},
            ]}])
    except anthropic.AuthenticationError:
        raise RuntimeError('the ANTHROPIC_API_KEY on the server is invalid')
    except anthropic.RateLimitError:
        raise RuntimeError('the naming model is rate-limited right now — try again shortly')
    except anthropic.APIStatusError as e:
        raise RuntimeError('naming failed upstream (%s)' % e.status_code)
    except anthropic.APIConnectionError:
        raise RuntimeError('could not reach the naming model (network)')

    if response.stop_reason == 'refusal':
        raise RuntimeError('the naming model declined this image')
    text = ''.join(b.text for b in response.content if b.type == 'text')
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise RuntimeError('the naming model returned no JSON')
    try:
        raw = json.loads(m.group(0))
    except Exception:
        raise RuntimeError('the naming model returned unparseable JSON')

    ids = {L['id'] for L in layers}
    out = {}
    for k, v in raw.items():
        try:
            lid = int(k)
        except (TypeError, ValueError):
            continue
        if lid in ids and str(v).strip():
            out[lid] = re.sub(r'\s+', ' ', str(v)).strip()[:40]
    if not out:
        raise RuntimeError('the naming model named no layers')
    return out
