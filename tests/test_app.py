"""End-to-end tests over the HTTP API (run with: pytest)."""
import io
import os
import sys

import pytest
from PIL import Image, ImageDraw

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
    assert 'Madeira Rayon' in r.json()
