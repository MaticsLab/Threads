"""End-to-end tests over the HTTP API (run with: pytest)."""
import io
import os
import sys
import tempfile

import pytest
from PIL import Image, ImageDraw

os.environ.setdefault('STITCHFORGE_DATA',
                      os.path.join(tempfile.mkdtemp(), 'sf_test_data'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def _test_png():
    im = Image.new('RGBA', (400, 260), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([30, 30, 220, 220], fill=(26, 59, 105, 255))
    d.ellipse([80, 80, 170, 170], fill=(0, 0, 0, 0))
    d.rounded_rectangle([250, 60, 380, 120], 18, fill=(168, 32, 26, 255))
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return buf.getvalue()


@pytest.fixture(scope='module')
def image_job():
    r = client.post('/api/analyze',
                    files={'image': ('t.png', _test_png(), 'image/png')},
                    data={'colors': 2})
    assert r.status_code == 200
    job = r.json()['job']
    r = client.post('/api/digitize', data={
        'job': job, 'colors': 2, 'width_mm': 60, 'hoop_w': 100, 'hoop_h': 100,
        'density': 0.4, 'max_satin': 8, 'heavy_underlay': False, 'autotune': False})
    assert r.status_code == 200, r.text
    return job, r.json()


def test_digitize_report(image_job):
    job, d = image_job
    rep = d['report']
    assert 55 <= rep['width_mm'] <= 65
    assert rep['stitches'] > 200
    assert len(d['threads']) == 2
    assert d['threads'][0]['thread_name']


def test_exports(image_job):
    job, _ = image_job
    for fmt in ('dst', 'pes', 'jef', 'exp', 'vp3', 'csv'):
        r = client.get('/api/download/%s?fmt=%s' % (job, fmt))
        assert r.status_code == 200 and len(r.content) > 100, fmt
    assert client.get('/api/download/%s?fmt=nope' % job).status_code == 400


def test_svg_and_stitch_json(image_job):
    job, _ = image_job
    r = client.get('/api/plan/%s.svg' % job)
    assert r.status_code == 200 and b'<svg' in r.content
    r = client.get('/api/plan/%s.svg?realistic=true' % job)
    assert r.status_code == 200 and b'realistic-stitch-filter' in r.content
    r = client.get('/api/stitches/%s' % job)
    assert r.status_code == 200 and len(r.json()) >= 1


def test_worksheet_pdf(image_job):
    job, _ = image_job
    r = client.get('/api/worksheet/%s.pdf?name=Test&setup=10&price_per_1000=1.5' % job)
    assert r.status_code == 200
    assert r.content[:4] == b'%PDF'


def test_fonts_listing():
    r = client.get('/api/fonts')
    assert r.status_code == 200
    ids = {f['id'] for f in r.json()}
    assert 'emilio_20' in ids and 'sacramarif' in ids


def test_lettering():
    r = client.post('/api/lettering', data={
        'text': 'Abc', 'font': 'geneva_simple', 'height_mm': 15, 'color': '#8B1A1A'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['report']['stitches'] > 100
    assert d['lettering']['satin_columns'] > 0
    r = client.get('/api/download/%s?fmt=dst' % d['job'])
    assert r.status_code == 200


def test_lettering_bad_input():
    assert client.post('/api/lettering', data={
        'text': '   ', 'font': 'geneva_simple'}).status_code == 400
    assert client.post('/api/lettering', data={
        'text': 'hi', 'font': 'no_such_font'}).status_code == 404


def test_palettes():
    r = client.get('/api/palettes')
    names = [p['name'] for p in r.json()]
    assert 'Madeira Rayon' in names
    assert 'BAI Matte' in names          # seeded from the product colour card
    assert len(names) > 70               # the full Ink/Stitch palette set
    r = client.get('/api/palettes/BAI Matte')
    rows = r.json()
    assert r.status_code == 200 and len(rows) == 20
    assert any(t['number'] == '8382' for t in rows)


def test_custom_palette_crud_and_match(image_job):
    job, _ = image_job
    r = client.post('/api/palettes', json={'name': 'Test Brand', 'threads': [
        {'name': 'Navy', 'number': 'T1', 'hex': '#1A3B69'},
        {'name': 'Flame', 'number': 'T2', 'hex': '#A8201A'}]})
    assert r.status_code == 200 and r.json()['colors'] == 2
    assert any(p['name'] == 'Test Brand' and p['custom']
               for p in client.get('/api/palettes').json())
    m = client.get('/api/match/%s?palette=Test%%20Brand' % job).json()
    assert all(t['palette'] == 'Test Brand' for t in m)
    assert {t['thread_number'] for t in m} <= {'T1', 'T2'}
    assert client.delete('/api/palettes/Test Brand').status_code == 200
    assert client.delete('/api/palettes/Madeira Rayon').status_code == 404
    assert client.post('/api/palettes', json={'name': 'x', 'threads': []}).status_code == 400


def test_fill_methods():
    for method in ('contour', 'circular'):
        r = client.post('/api/analyze',
                        files={'image': ('t.png', _test_png(), 'image/png')},
                        data={'colors': 2})
        job = r.json()['job']
        r = client.post('/api/digitize', data={
            'job': job, 'colors': 2, 'width_mm': 50, 'hoop_w': 100, 'hoop_h': 100,
            'density': 0.4, 'max_satin': 8, 'heavy_underlay': False,
            'autotune': False, 'fill_method': method})
        assert r.status_code == 200, (method, r.text)
        assert r.json()['report']['stitches'] > 200


TEST_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm"
     viewBox="0 0 60 40">
  <rect x="4" y="4" width="24" height="16" fill="#1a3b69"/>
  <circle cx="45" cy="12" r="8" fill="#a8201a"/>
  <path d="M4,30 C20,38 40,22 56,32" fill="none" stroke="#1d7a4c" stroke-width="0.5"/>
</svg>'''


def test_svg_import():
    r = client.post('/api/import',
                    files={'design': ('art.svg', TEST_SVG.encode(), 'image/svg+xml')},
                    data={'width_mm': 0})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['kind'] == 'svg'
    assert d['import_info']['fills'] >= 2
    assert d['import_info']['strokes'] >= 1
    # content spans x=4..56 of the 60mm document -> ~52mm stitched extent
    assert 48 <= d['report']['width_mm'] <= 56
    assert d['report']['stitches'] > 300
    assert client.get('/api/download/%s?fmt=dst' % d['job']).status_code == 200


def test_embroidery_file_import(image_job):
    job, _ = image_job
    dst = client.get('/api/download/%s?fmt=dst' % job).content
    r = client.post('/api/import',
                    files={'design': ('old.dst', dst, 'application/octet-stream')})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['kind'] == 'import'
    assert d['report']['stitches'] > 200
    assert client.get('/api/download/%s?fmt=pes' % d['job']).status_code == 200


def test_density_and_zip_and_threadlist(image_job):
    job, _ = image_job
    r = client.get('/api/density/%s.png' % job)
    assert r.status_code == 200 and r.content[:8].startswith(b'\x89PNG')
    r = client.get('/api/download/%s?fmt=zip' % job)
    assert r.status_code == 200 and r.content[:2] == b'PK'
    r = client.get('/api/threadlist/%s.txt' % job)
    assert r.status_code == 200 and b'Thread order' in r.content


def test_business_crud():
    r = client.post('/api/business/client', json={'name': 'Acme Embroidery',
                                                  'business_name': 'Acme'})
    assert r.status_code == 200
    cid = r.json()['id']
    rows = client.get('/api/business/client').json()
    assert any(row['id'] == cid for row in rows)
    assert client.put('/api/business/client/%d' % cid,
                      json={'name': 'Acme 2'}).status_code == 200
    assert client.delete('/api/business/client/%d' % cid).status_code == 200
    assert client.post('/api/business/client', json={'name': '  '}).status_code == 400
    assert client.get('/api/business/nope').status_code == 404


def test_notes():
    assert client.put('/api/notes/todo', json={'text': 'hoop the caps'}).status_code == 200
    assert client.get('/api/notes/todo').json()['text'] == 'hoop the caps'
    assert client.get('/api/notes/nope').status_code == 404
