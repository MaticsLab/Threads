"""StitchForge — upload an image (or type text), get machine-ready embroidery.

Digitizing pipeline from the original StitchForge core, with the Ink/Stitch
family of projects integrated:

* pystitch (vendored)      all format I/O: DST plus PES/EXP/JEF/VP3/XXX/...
* inkstitch                stitch plan SVG + realistic preview, print
                           worksheet PDF, thread palettes
* embroidery-fonts         lettering with pre-digitized satin fonts
* embTools                 quote sheet, stitch player, unit conversion
* svg.panzoom.js           pan/zoom for the previews (served from static/)
"""
import base64
import json
import os
import sys
import tempfile
import traceback
import uuid

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(APP_DIR, 'third_party'))

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import numpy as np
import pystitch

from digitizer import segment, engine, render, core
from inkstitchlib import stitch_svg, threads, lettering, worksheet, svginput, business
from inkstitchlib import density as density_map

JOBS = os.path.join(tempfile.gettempdir(), 'stitchforge_jobs')
os.makedirs(JOBS, exist_ok=True)

app = FastAPI(title='StitchForge')

# big SVG/JSON payloads (stitch plans, realistic SVG, stitch lists) compress
# 4-6x; makes remote deploys and CDN caching worthwhile
app.add_middleware(GZipMiddleware, minimum_size=1024)

# job artifacts never change once written (a new digitize gets a new job id),
# so browsers and any CDN in front (e.g. Cloudflare) may cache them hard
CACHED_PREFIXES = ('/static/', '/api/plan/', '/api/density/', '/api/stitches/',
                   '/api/fonts/')


@app.middleware('http')
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if (request.method == 'GET' and response.status_code == 200
            and request.url.path.startswith(CACHED_PREFIXES)):
        response.headers.setdefault('Cache-Control', 'public, max-age=86400')
    return response


PATTERNS = {}          # job id -> live pystitch.EmbPattern

EXPORT_FORMATS = {
    'dst': pystitch.write_dst, 'pes': pystitch.write_pes,
    'exp': pystitch.write_exp, 'jef': pystitch.write_jef,
    'vp3': pystitch.write_vp3, 'xxx': pystitch.write_xxx,
    'u01': pystitch.write_u01, 'pec': pystitch.write_pec,
    'tbf': pystitch.write_tbf, 'csv': pystitch.write_csv,
    'json': pystitch.write_json, 'txt': pystitch.write_txt,
    'gcode': pystitch.write_gcode, 'png': pystitch.write_png,
}


def _native(o):
    """NumPy scalars aren't JSON-serialisable; convert on the way out."""
    if isinstance(o, dict):
        return {k: _native(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_native(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    return o


def _b64(path):
    with open(path, 'rb') as f:
        return 'data:image/png;base64,' + base64.b64encode(f.read()).decode()


def _job_dir(job, must_exist=True):
    d = os.path.join(JOBS, job)
    if must_exist and not os.path.isdir(d):
        raise HTTPException(404, 'unknown job')
    return d


def _load_pattern(job):
    """Live pattern if we have it, else re-read the DST (threads from meta)."""
    if job in PATTERNS:
        return PATTERNS[job]
    d = _job_dir(job)
    dst = os.path.join(d, 'design.dst')
    if not os.path.exists(dst):
        raise HTTPException(404, 'no digitized design for this job')
    pat = pystitch.read(dst)
    meta = _load_meta(job)
    pat.threadlist.clear()
    for L in meta.get('layers', []):
        t = pystitch.EmbThread()
        t.color = int(L['hex'].lstrip('#'), 16)
        t.description = L.get('name', '')
        pat.add_thread(t)
    PATTERNS[job] = pat
    return pat


def _load_meta(job):
    p = os.path.join(_job_dir(job), 'meta.json')
    if not os.path.exists(p):
        raise HTTPException(404, 'job has not been digitized yet')
    with open(p) as f:
        return json.load(f)


def _store(job, pat, layers, report, settings, kind='image'):
    d = _job_dir(job, must_exist=False)
    os.makedirs(d, exist_ok=True)
    PATTERNS[job] = pat
    pystitch.write_dst(pat, os.path.join(d, 'design.dst'))
    png = render.preview(pat, [tuple(L['rgb']) for L in layers], os.path.join(d, 'preview.png'))
    with open(os.path.join(d, 'plan.svg'), 'w') as f:
        f.write(stitch_svg.render(pat, realistic=False))
    meta = {'kind': kind, 'report': report, 'settings': settings,
            'layers': [{'name': L['name'], 'hex': L['hex'], 'rgb': list(L['rgb'])}
                       for L in layers]}
    with open(os.path.join(d, 'meta.json'), 'w') as f:
        json.dump(_native(meta), f)
    return png


def basic_report(pat):
    """Size/count report for patterns without source masks (lettering)."""
    pts = [(x, y) for x, y, c in pat.stitches if (c & 0xFF) in (pystitch.STITCH, pystitch.JUMP)]
    a = np.array(pts) / 10.0 if pts else np.zeros((1, 2))
    w = float(a[:, 0].max() - a[:, 0].min())
    h = float(a[:, 1].max() - a[:, 1].min())
    lens, prev = [], None
    trims = jumps = 0
    for x, y, c in pat.stitches:
        k = c & 0xFF
        if k == pystitch.STITCH:
            if prev is not None:
                lens.append(float(np.hypot(x - prev[0], y - prev[1]) / 10))
            prev = (x, y)
        elif k == pystitch.JUMP:
            jumps += 1
            prev = (x, y)
        elif k == pystitch.TRIM:
            trims += 1
            prev = None
        else:
            prev = None
    n_st = sum(1 for q in pat.stitches if (q[2] & 0xFF) == pystitch.STITCH)
    return {'width_mm': round(w, 2), 'height_mm': round(h, 2),
            'width_in': round(w / 25.4, 3), 'stitches': n_st,
            'colour_changes': pat.count_color_changes(),
            'travels': jumps, 'trimmed': trims, 'floats': max(0, jumps - trims),
            'min_stitch_mm': round(min(lens), 2) if lens else 0.0,
            'max_stitch_mm': round(max(lens), 2) if lens else 0.0,
            'runtime_min': round(n_st / 700.0, 1)}


@app.get('/', response_class=HTMLResponse)
def index():
    with open(os.path.join(APP_DIR, 'static', 'index.html')) as f:
        return f.read()


# ------------------------------------------------------------------- image
@app.post('/api/analyze')
async def analyze(image: UploadFile = File(...), colors: int = Form(3)):
    """Separate the image into colour layers and report stroke widths."""
    job = uuid.uuid4().hex[:12]
    d = _job_dir(job, must_exist=False)
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, 'src' + os.path.splitext(image.filename or '.png')[1])
    with open(src, 'wb') as f:
        f.write(await image.read())
    try:
        rgba = segment.load(src)
        layers = segment.quantize(rgba, colors)
    except Exception as e:
        raise HTTPException(400, str(e))

    out = []
    for L in layers:
        scale = 100.0 / rgba.shape[1]
        mx, med = segment.stroke_stats(L['mask'], scale)
        out.append({'name': L['name'], 'hex': L['hex'], 'order': L['order'],
                    'area_px': L['area_px'],
                    'max_stroke_at_100mm': round(mx, 2),
                    'typ_stroke_at_100mm': round(med, 2)})
    return _native({'job': job, 'layers': out})


@app.post('/api/digitize')
async def digitize(job: str = Form(...), colors: int = Form(3),
                   width_mm: float = Form(100.0),
                   hoop_w: float = Form(100.0), hoop_h: float = Form(100.0),
                   density: float = Form(0.35), max_satin: float = Form(8.0),
                   heavy_underlay: bool = Form(True),
                   autotune: bool = Form(True),
                   fill_method: str = Form('tatami'),
                   fill_angle: float = Form(65.0),
                   palette: str = Form('Madeira Rayon')):
    d = _job_dir(job)
    src = None
    for fn in os.listdir(d):
        if fn.startswith('src'):
            src = os.path.join(d, fn)
    if not src:
        raise HTTPException(404, 'upload not found — please re-upload the image')

    try:
        rgba = segment.load(src)
        layers = segment.quantize(rgba, colors)
        if fill_method not in ('tatami', 'contour', 'circular'):
            raise HTTPException(400, 'fill_method must be tatami, contour or circular')
        p = engine.Params(target_width_mm=width_mm, hoop_w_mm=hoop_w,
                          hoop_h_mm=hoop_h, row_spacing=density,
                          satin_spacing=round(density / 2, 4),
                          max_satin=max_satin, heavy_underlay=heavy_underlay,
                          fill_method=fill_method, fill_angle=fill_angle)
        pat, rep, stats, log = engine.digitize(
            layers, p, max_passes=4 if autotune else 2)
        settings = {'density': p.row_spacing, 'max_satin': p.max_satin,
                    'min_fill_area': p.min_fill_area}
        png = _store(job, pat, layers, rep, settings)
        thread_matches = threads.match_layers(layers, palette)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f'digitizing failed: {e}')

    warnings = []
    if not rep['fits_hoop']:
        warnings.append('Design is %.1f x %.1f mm and will not fit the %.0f x %.0f mm '
                        'hoop. Reduce the width.' % (rep['width_mm'], rep['height_mm'],
                                                     hoop_w, hoop_h))
    elif rep['hoop_clearance_mm'] < 2:
        warnings.append('Only %.2f mm of clearance to the hoop edge. Trace the outline '
                        'before stitching.' % rep['hoop_clearance_mm'])
    if rep['max_stitch_mm'] > 10:
        warnings.append('Longest stitch is %.1f mm — long satin snags. Lower the satin '
                        'cap.' % rep['max_stitch_mm'])
    if rep['floats'] > 0:
        warnings.append('%d travel(s) left untrimmed; they will show as short floats.'
                        % rep['floats'])
    if rep['coverage_min'] < 95:
        warnings.append('Lowest layer coverage is %.1f%% — fabric may show through.'
                        % rep['coverage_min'])
    for L in rep['layers']:
        if L['worst_gap_mm2'] > 1.5:
            warnings.append('%s has a %.2f mm2 gap — check that layer in the preview.'
                            % (L['name'], L['worst_gap_mm2']))

    return _native({'job': job, 'kind': 'image', 'report': rep, 'layer_stats': stats,
                    'threads': thread_matches,
                    'passes': [{'pass': e['pass'], 'actions': e['actions'],
                                'width_mm': e['report']['width_mm'],
                                'coverage_min': e['report']['coverage_min'],
                                'stitches': e['report']['stitches']} for e in log],
                    'warnings': warnings, 'preview': _b64(png),
                    'settings': {'density': p.row_spacing, 'max_satin': p.max_satin,
                                 'min_fill_area': p.min_fill_area}})


# --------------------------------------------------------------- lettering
@app.get('/api/fonts')
def fonts():
    return lettering.available_fonts()


@app.get('/api/fonts/{font_id}/preview.png')
def font_preview(font_id: str):
    if '/' in font_id or '..' in font_id:
        raise HTTPException(404, 'no such font')
    p = os.path.join(lettering.FONT_DIR, font_id, 'preview.png')
    if not os.path.exists(p):
        raise HTTPException(404, 'no preview for this font')
    return FileResponse(p, media_type='image/png')


@app.post('/api/lettering')
async def letter(text: str = Form(...), font: str = Form(...),
                 height_mm: float = Form(0.0),
                 letter_spacing_mm: float = Form(0.0),
                 color: str = Form('#1A3B69'),
                 palette: str = Form('Madeira Rayon')):
    text = text.strip('\n')
    if not text.strip():
        raise HTTPException(400, 'please type some text to stitch')
    if font not in {f['id'] for f in lettering.available_fonts()}:
        raise HTTPException(404, 'unknown font')
    try:
        s = core.Sewer()
        rgb_int = int(color.lstrip('#'), 16)
        th = pystitch.EmbThread()
        th.color = rgb_int
        th.description = 'Lettering'
        s.pattern.add_thread(th)
        info = lettering.stitch_text(s, font, text,
                                     height_mm=height_mm or None,
                                     letter_spacing_mm=letter_spacing_mm)
        s.tie_off()
        s.pattern.end()
        pat = s.pattern
        pat.move_center_to_origin()
    except lettering.LetteringError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f'lettering failed: {e}')

    job = uuid.uuid4().hex[:12]
    rgb = ((rgb_int >> 16) & 255, (rgb_int >> 8) & 255, rgb_int & 255)
    layers = [{'name': 'Lettering', 'hex': '#%06X' % rgb_int, 'rgb': rgb}]
    rep = basic_report(pat)
    png = _store(job, pat, layers, rep, {'font': font, 'text': text}, kind='lettering')

    warnings = []
    if info['missing_chars']:
        warnings.append('Not in this font, replaced by the default glyph: %s'
                        % ' '.join(info['missing_chars']))
    if height_mm and abs(info['height_mm'] - height_mm) > 0.5:
        warnings.append('This font only scales %.1f–%.1f mm; sewing at %.1f mm.'
                        % (lettering.get_font(font).size * lettering.get_font(font).min_scale,
                           lettering.get_font(font).size * lettering.get_font(font).max_scale,
                           info['height_mm']))

    return _native({'job': job, 'kind': 'lettering', 'report': rep,
                    'lettering': info, 'warnings': warnings,
                    'threads': threads.match_layers(layers, palette),
                    'preview': _b64(png)})


# ------------------------------------------------------------------ import
IMPORT_EXTS = {'.dst', '.pes', '.jef', '.exp', '.vp3', '.xxx', '.u01', '.pec',
               '.tbf', '.hus', '.pcs', '.sew', '.shv', '.jpx', '.dsb', '.dsz',
               '.emd', '.new', '.max', '.mit', '.phb', '.phc', '.stx', '.tap',
               '.txt', '.gt', '.fxy', '.zxy', '.zhs', '.10o', '.100', '.bro',
               '.dat', '.exy', '.inb', '.ksm', '.pcd', '.pcm', '.pcq', '.spx',
               '.stc', '.gcode', '.json', '.csv'}

FALLBACK_COLORS = [(26, 59, 105), (168, 32, 26), (240, 180, 40), (29, 122, 76),
                   (90, 60, 140), (20, 20, 24), (200, 120, 160), (70, 140, 180)]


@app.post('/api/import')
async def import_file(design: UploadFile = File(...),
                      width_mm: float = Form(0.0),
                      fill_method: str = Form('tatami'),
                      fill_angle: float = Form(65.0),
                      density: float = Form(0.35),
                      palette: str = Form('Madeira Rayon')):
    """Ink/Stitch style input: read any embroidery file, or digitize an SVG."""
    name = design.filename or 'design'
    ext = os.path.splitext(name)[1].lower()
    data = await design.read()
    job = uuid.uuid4().hex[:12]
    d = _job_dir(job, must_exist=False)
    os.makedirs(d, exist_ok=True)

    info = {}
    if ext == '.svg':
        try:
            pat, layers, info = svginput.digitize_svg(
                data, width_mm=width_mm or None, fill_method=fill_method,
                fill_angle=fill_angle, row_spacing=density)
        except svginput.SvgError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(500, f'SVG digitizing failed: {e}')
        kind = 'svg'
    elif ext in IMPORT_EXTS:
        src = os.path.join(d, 'import' + ext)
        with open(src, 'wb') as f:
            f.write(data)
        try:
            pat = pystitch.read(src)
            if pat is None or not pat.stitches:
                raise ValueError('no stitches found in the file')
        except Exception as e:
            raise HTTPException(400, f'could not read {ext} file: {e}')
        n_blocks = pat.count_color_changes() + 1
        layers = []
        for i in range(n_blocks):
            th = pat.threadlist[i] if i < len(pat.threadlist) else None
            if th is not None and th.color is not None:
                v = th.color & 0xFFFFFF
                rgb = ((v >> 16) & 255, (v >> 8) & 255, v & 255)
            else:
                rgb = FALLBACK_COLORS[i % len(FALLBACK_COLORS)]
            layers.append({'name': (th.description if th is not None and th.description
                                    else 'Colour %d' % (i + 1)),
                           'hex': '#%02X%02X%02X' % rgb, 'rgb': rgb})
        pat.threadlist.clear()
        for L in layers:
            th = pystitch.EmbThread()
            th.color = int(L['hex'][1:], 16)
            th.description = L['name']
            pat.add_thread(th)
        kind = 'import'
        info = {'source_format': ext.lstrip('.'), 'filename': name}
    else:
        raise HTTPException(400, 'unsupported file type %r — upload an SVG or a '
                                 'machine embroidery file' % ext)

    rep = basic_report(pat)
    png = _store(job, pat, layers, rep, info, kind=kind)
    return _native({'job': job, 'kind': kind, 'report': rep, 'import_info': info,
                    'threads': threads.match_layers(layers, palette),
                    'warnings': [], 'preview': _b64(png)})


# ----------------------------------------------------------------- exports
ZIP_FORMATS = ('dst', 'pes', 'jef', 'exp', 'vp3', 'xxx')


@app.get('/api/download/{job}')
def download(job: str, fmt: str = 'dst'):
    fmt = fmt.lower()
    if fmt == 'zip':
        return _zip_export(job)
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(400, 'format must be one of: zip, ' + ', '.join(sorted(EXPORT_FORMATS)))
    d = _job_dir(job)
    out = os.path.join(d, 'design.' + fmt)
    if fmt == 'dst' and os.path.exists(out):
        return FileResponse(out, filename='design.dst', media_type='application/octet-stream')
    pat = _load_pattern(job)
    try:
        EXPORT_FORMATS[fmt](pat, out)
    except Exception as e:
        raise HTTPException(500, 'export to %s failed: %s' % (fmt, e))
    return FileResponse(out, filename='design.' + fmt, media_type='application/octet-stream')


@app.get('/api/plan/{job}.svg')
def plan_svg(job: str, realistic: bool = False):
    d = _job_dir(job)
    name = 'realistic.svg' if realistic else 'plan.svg'
    p = os.path.join(d, name)
    if not os.path.exists(p):
        pat = _load_pattern(job)
        with open(p, 'w') as f:
            f.write(stitch_svg.render(pat, realistic=realistic))
    return FileResponse(p, media_type='image/svg+xml')


def _zip_export(job):
    """Ink/Stitch style batch export: every major format + worksheet in one zip."""
    import zipfile
    d = _job_dir(job)
    pat = _load_pattern(job)
    meta = _load_meta(job)
    layers = meta['layers']
    try:
        matches = threads.match_layers(
            [{**L, 'rgb': tuple(L['rgb'])} for L in layers], 'Madeira Rayon')
    except Exception:
        matches = None
    out = os.path.join(d, 'design.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for fmt in ZIP_FORMATS:
            p = os.path.join(d, 'design.' + fmt)
            try:
                EXPORT_FORMATS[fmt](pat, p)
                z.write(p, 'design.' + fmt)
            except Exception:
                pass
        z.writestr('threadlist.txt',
                   threads.threadlist_txt('design', meta['report'], layers, matches))
        plan = os.path.join(d, 'plan.svg')
        if os.path.exists(plan):
            z.write(plan, 'stitch_plan.svg')
        try:
            pdf = os.path.join(d, 'worksheet.pdf')
            worksheet.build(pdf, pat, meta['report'], layers, thread_matches=matches,
                            preview_png=os.path.join(d, 'preview.png'),
                            design_name='design')
            z.write(pdf, 'worksheet.pdf')
        except Exception:
            pass
    return FileResponse(out, filename='design.zip', media_type='application/zip')


@app.get('/api/stitches/{job}')
def stitches(job: str):
    """Stitch blocks for the front-end stitch player (embTools port)."""
    pat = _load_pattern(job)
    return JSONResponse(stitch_svg.stitch_json(pat))


@app.get('/api/density/{job}.png')
def density_png(job: str):
    """Stitch density heat map (Ink/Stitch density map port)."""
    d = _job_dir(job)
    out = os.path.join(d, 'density.png')
    if not os.path.exists(out):
        pat = _load_pattern(job)
        density_map.render(pat, out)
    return FileResponse(out, media_type='image/png')


@app.get('/api/threadlist/{job}.txt')
def threadlist(job: str, name: str = 'design', palette: str = 'Madeira Rayon'):
    meta = _load_meta(job)
    layers = meta['layers']
    try:
        matches = threads.match_layers(
            [{**L, 'rgb': tuple(L['rgb'])} for L in layers], palette)
    except Exception:
        matches = None
    return Response(threads.threadlist_txt(name, meta['report'], layers, matches),
                    media_type='text/plain')


@app.get('/api/palettes')
def palettes():
    return threads.available()


# --------------------------------------------- business database (embTools)
@app.get('/api/business/{kind}')
def business_list(kind: str, sort: str = 'name'):
    if kind not in business.KINDS:
        raise HTTPException(404, 'kind must be client or vendor')
    return business.list_contacts(kind, sort)


@app.post('/api/business/{kind}')
async def business_add(kind: str, data: dict):
    if kind not in business.KINDS:
        raise HTTPException(404, 'kind must be client or vendor')
    if not str(data.get('name', '')).strip():
        raise HTTPException(400, 'name is required')
    return {'id': business.add_contact(kind, data)}


@app.put('/api/business/{kind}/{cid}')
async def business_update(kind: str, cid: int, data: dict):
    if kind not in business.KINDS:
        raise HTTPException(404, 'kind must be client or vendor')
    if not business.update_contact(kind, cid, data):
        raise HTTPException(404, 'no such contact')
    return {'ok': True}


@app.delete('/api/business/{kind}/{cid}')
def business_delete(kind: str, cid: int):
    if kind not in business.KINDS:
        raise HTTPException(404, 'kind must be client or vendor')
    if not business.delete_contact(kind, cid):
        raise HTTPException(404, 'no such contact')
    return {'ok': True}


@app.get('/api/notes/{kind}')
def notes_get(kind: str):
    if kind not in business.NOTE_KINDS:
        raise HTTPException(404, 'kind must be notes, quotes or todo')
    return {'kind': kind, 'text': business.get_note(kind)}


@app.put('/api/notes/{kind}')
async def notes_set(kind: str, data: dict):
    if kind not in business.NOTE_KINDS:
        raise HTTPException(404, 'kind must be notes, quotes or todo')
    business.set_note(kind, data.get('text', ''))
    return {'ok': True}


@app.get('/api/worksheet/{job}.pdf')
def worksheet_pdf(job: str, name: str = 'design', palette: str = 'Madeira Rayon',
                  client: str = '',
                  setup: float = 0.0, price_per_1000: float = 0.0,
                  garment_qty: int = 0, garment_base: float = 0.0,
                  markup_pct: float = 0.0, discount_pct: float = 0.0):
    d = _job_dir(job)
    pat = _load_pattern(job)
    meta = _load_meta(job)
    layers = meta['layers']
    for L in layers:
        L.setdefault('rgb', [int(L['hex'][1:3], 16), int(L['hex'][3:5], 16), int(L['hex'][5:7], 16)])
        L['rgb'] = tuple(L['rgb'])
    try:
        matches = threads.match_layers(layers, palette)
    except Exception:
        matches = None
    quote_params = None
    if setup or price_per_1000 or garment_qty:
        quote_params = {'setup': setup, 'price_per_1000': price_per_1000,
                        'garment_qty': garment_qty, 'garment_base': garment_base,
                        'markup_pct': markup_pct, 'discount_pct': discount_pct}
    out = os.path.join(d, 'worksheet.pdf')
    worksheet.build(out, pat, meta['report'], layers, thread_matches=matches,
                    preview_png=os.path.join(d, 'preview.png'),
                    design_name=name or 'design', quote_params=quote_params,
                    client=client)
    return FileResponse(out, filename='%s-worksheet.pdf' % (name or 'design'),
                        media_type='application/pdf')


app.mount('/static', StaticFiles(directory=os.path.join(APP_DIR, 'static')),
          name='static')
