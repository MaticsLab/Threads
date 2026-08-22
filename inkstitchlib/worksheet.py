"""Print worksheet PDF, ported from Ink/Stitch's print templates.

Ink/Stitch renders its worksheet as HTML in a browser and the user prints it
to PDF; here the same layout is drawn straight to PDF with reportlab:

* page 1 - client/overview page (print_overview.html): design preview, job
  details, colour palette with matched thread names, and a quote block ported
  from embTools' quote sheet (quotesheet.cpp: digitizing = price-per-1000 x
  stitches; total = setup + marked-up product + digitizing - discount);
* page 2 - operator detailed view (operator_detailedview.html): one row per
  colour block with swatch, thread, stitch count, estimated time, stops and
  trims, and a notes line.
"""
import datetime
import math
import os

import pystitch
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen.canvas import Canvas

PAGE_W, PAGE_H = A4
MARGIN = 15 * mm
INK = HexColor('#12161c')
MUTED = HexColor('#6b7480')
LINE = HexColor('#c9ced4')


def block_stats(pattern):
    """Per colour block: stitches, trims, stops (jumps left untrimmed)."""
    blocks = [{'stitches': 0, 'trims': 0, 'stops': 0}]
    prev_cmd = None
    for x, y, cmd in pattern.stitches:
        c = cmd & 0xFF
        if c == pystitch.COLOR_CHANGE:
            blocks.append({'stitches': 0, 'trims': 0, 'stops': 0})
        elif c == pystitch.STITCH:
            blocks[-1]['stitches'] += 1
        elif c == pystitch.TRIM:
            blocks[-1]['trims'] += 1
        elif c == pystitch.JUMP and prev_cmd != pystitch.JUMP:
            blocks[-1]['stops'] += 1
        prev_cmd = c
    return blocks


def _est_time(stitches, spm=700):
    minutes = stitches / spm
    return '%d:%02d min' % (int(minutes), int(round(minutes % 1 * 60)))


def _text(c, x, y, s, size=9, bold=False, color=INK, align='left'):
    c.setFillColor(color)
    font = 'Helvetica-Bold' if bold else 'Helvetica'
    c.setFont(font, size)
    if align == 'right':
        c.drawRightString(x, y, str(s))
    elif align == 'center':
        c.drawCentredString(x, y, str(s))
    else:
        c.drawString(x, y, str(s))


def _header(c, title, subtitle):
    _text(c, MARGIN, PAGE_H - MARGIN, title, 16, bold=True)
    _text(c, MARGIN, PAGE_H - MARGIN - 14, subtitle, 9, color=MUTED)
    _text(c, PAGE_W - MARGIN, PAGE_H - MARGIN, datetime.date.today().isoformat(),
          9, color=MUTED, align='right')
    c.setStrokeColor(LINE)
    c.setLineWidth(0.7)
    c.line(MARGIN, PAGE_H - MARGIN - 22, PAGE_W - MARGIN, PAGE_H - MARGIN - 22)


def _footer(c):
    c.setStrokeColor(LINE)
    c.line(MARGIN, MARGIN, PAGE_W - MARGIN, MARGIN)
    _text(c, MARGIN, MARGIN - 10,
          'StitchForge worksheet — layout after the Ink/Stitch print worksheet (inkstitch.org)',
          7, color=MUTED)
    _text(c, PAGE_W - MARGIN, MARGIN - 10, 'page %d' % c.getPageNumber(),
          7, color=MUTED, align='right')


def _kv_rows(c, x, y, rows, key_w=42 * mm, lh=13):
    for k, v in rows:
        _text(c, x, y, k, 9, color=MUTED)
        _text(c, x + key_w, y, v, 9)
        y -= lh
    return y


def quote(stitches, setup=0.0, price_per_1000=0.0, garment_qty=0, garment_base=0.0,
          markup_pct=0.0, discount_pct=0.0):
    """The embTools quote sheet calculation (quotesheet.cpp)."""
    product = garment_qty * garment_base
    marked_up = (markup_pct / 100.0) * product + product
    discount = (discount_pct / 100.0) * product
    digitizing = (price_per_1000 / 1000.0) * stitches
    total = setup + marked_up + (digitizing - discount)
    return {'product': product, 'marked_up': marked_up, 'discount': discount,
            'digitizing': digitizing, 'setup': setup, 'total': total}


def build(path, pattern, report, layers, thread_matches=None, preview_png=None,
          design_name='design', quote_params=None, spm=700):
    """Write the worksheet PDF to `path`.

    report: engine.qa_report dict (or the lighter lettering report).
    layers: [{'name','hex', ...}] in sew order.
    thread_matches: optional threads.match_layers output, same order.
    """
    c = Canvas(path, pagesize=A4)
    c.setTitle('%s — embroidery worksheet' % design_name)

    # ---------------------------------------------------- page 1: overview
    _header(c, design_name, 'Embroidery worksheet — client overview')

    y_top = PAGE_H - MARGIN - 36
    # preview, left column
    img_w = 95 * mm
    img_h = 95 * mm
    if preview_png and os.path.exists(preview_png):
        from reportlab.lib.utils import ImageReader
        img = ImageReader(preview_png)
        iw, ih = img.getSize()
        s = min(img_w / iw, img_h / ih)
        c.drawImage(img, MARGIN, y_top - ih * s, iw * s, ih * s,
                    preserveAspectRatio=True, anchor='nw')
        img_h = ih * s

    # job details, right column
    x2 = MARGIN + 103 * mm
    stitches = report.get('stitches', 0)
    rows = [
        ('Design box size', '%.1f × %.1f mm  (%.2f × %.2f in)' % (
            report.get('width_mm', 0), report.get('height_mm', 0),
            report.get('width_mm', 0) / 25.4, report.get('height_mm', 0) / 25.4)),
        ('Total stitch count', '{:,}'.format(stitches)),
        ('Unique colours', str(len(layers))),
        ('Colour blocks', str(max(1, report.get('colour_changes', 0) + 1))),
        ('Total stops / trims', '%s / %s' % (report.get('travels', '—'), report.get('trimmed', '—'))),
        ('Estimated time @%d spm' % spm, _est_time(stitches, spm)),
    ]
    if report.get('hoop_clearance_mm') is not None:
        rows.append(('Hoop clearance', '%.1f mm' % report['hoop_clearance_mm']))
    y = _kv_rows(c, x2, y_top - 10, rows)

    # client fields, like the editable spans on the HTML worksheet
    y -= 6
    for label in ('Client', 'Purchase order', 'Fabric / garment'):
        _text(c, x2, y, label, 9, color=MUTED)
        c.setStrokeColor(LINE)
        c.line(x2 + 32 * mm, y - 1, PAGE_W - MARGIN, y - 1)
        y -= 15

    # colour palette
    y = min(y, y_top - img_h) - 24
    _text(c, MARGIN, y, 'THREAD SEQUENCE', 9, bold=True)
    y -= 6
    c.setStrokeColor(LINE)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= 16
    for i, L in enumerate(layers):
        c.setFillColor(HexColor(L['hex']))
        c.setStrokeColor(LINE)
        c.rect(MARGIN, y - 3, 8 * mm, 5 * mm, fill=1)
        _text(c, MARGIN + 11 * mm, y, '#%d   %s   %s' % (i + 1, L.get('name', ''), L['hex']), 9)
        if thread_matches and i < len(thread_matches):
            t = thread_matches[i]
            _text(c, MARGIN + 90 * mm, y,
                  '%s  %s %s' % (t['palette'], t['thread_name'],
                                 ('#' + t['thread_number']) if t['thread_number'] else ''),
                  9, color=MUTED)
        y -= 12 * mm / 3.2

    # quote block (embTools port)
    if quote_params:
        q = quote(stitches, **quote_params)
        y -= 14
        _text(c, MARGIN, y, 'QUOTE', 9, bold=True)
        y -= 6
        c.setStrokeColor(LINE)
        c.line(MARGIN, y, PAGE_W - MARGIN, y)
        y -= 14
        qrows = [
            ('Setup fee', '$%.2f' % q['setup']),
            ('Digitizing (%s st @ $%.2f/1000)' % ('{:,}'.format(stitches),
                                                  quote_params.get('price_per_1000', 0)),
             '$%.2f' % q['digitizing']),
        ]
        if q['product']:
            qrows.append(('Garments (%d × $%.2f, %+.0f%% markup)'
                          % (quote_params.get('garment_qty', 0),
                             quote_params.get('garment_base', 0),
                             quote_params.get('markup_pct', 0)),
                          '$%.2f' % q['marked_up']))
        if q['discount']:
            qrows.append(('Discount (%.0f%%)' % quote_params.get('discount_pct', 0),
                          '-$%.2f' % q['discount']))
        for k, v in qrows:
            _text(c, MARGIN, y, k, 9, color=MUTED)
            _text(c, MARGIN + 120 * mm, y, v, 9, align='right')
            y -= 13
        _text(c, MARGIN, y, 'Total', 10, bold=True)
        _text(c, MARGIN + 120 * mm, y, '$%.2f' % q['total'], 10, bold=True, align='right')

    _footer(c)
    c.showPage()

    # ------------------------------------------- page 2: operator detailed
    _header(c, design_name, 'Embroidery worksheet — operator detailed view')
    blocks = block_stats(pattern)
    y = PAGE_H - MARGIN - 40

    _text(c, MARGIN, y, '#', 8, bold=True, color=MUTED)
    _text(c, MARGIN + 14 * mm, y, 'COLOUR', 8, bold=True, color=MUTED)
    _text(c, MARGIN + 74 * mm, y, 'STITCHES', 8, bold=True, color=MUTED)
    _text(c, MARGIN + 98 * mm, y, 'TIME', 8, bold=True, color=MUTED)
    _text(c, MARGIN + 118 * mm, y, 'STOPS/TRIMS', 8, bold=True, color=MUTED)
    _text(c, MARGIN + 146 * mm, y, 'NOTES', 8, bold=True, color=MUTED)
    y -= 5
    c.setStrokeColor(LINE)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= 16

    for i, b in enumerate(blocks):
        if y < MARGIN + 20:
            _footer(c)
            c.showPage()
            _header(c, design_name, 'Operator detailed view (continued)')
            y = PAGE_H - MARGIN - 40
        L = layers[i] if i < len(layers) else {'hex': '#888888', 'name': 'Colour %d' % (i + 1)}
        col = HexColor(L['hex'])
        c.setFillColor(col)
        c.setStrokeColor(LINE)
        c.rect(MARGIN, y - 4, 10 * mm, 7 * mm, fill=1)
        # readable index on the swatch, like Ink/Stitch's font_color logic
        lum = (col.red * 0.2126 + col.green * 0.7152 + col.blue * 0.0722)
        c.setFillColor(white if lum < 0.5 else black)
        c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(MARGIN + 5 * mm, y - 1, str(i + 1))

        name = L.get('name', '')
        if thread_matches and i < len(thread_matches):
            t = thread_matches[i]
            name += '  ·  %s %s' % (t['thread_name'],
                                    ('#' + t['thread_number']) if t['thread_number'] else '')
        _text(c, MARGIN + 14 * mm, y, name[:44], 9)
        _text(c, MARGIN + 14 * mm, y - 9, L['hex'], 7, color=MUTED)
        _text(c, MARGIN + 74 * mm, y, '{:,}'.format(b['stitches']), 9)
        _text(c, MARGIN + 98 * mm, y, _est_time(b['stitches'], spm), 9)
        _text(c, MARGIN + 118 * mm, y, '%d / %d' % (b['stops'], b['trims']), 9)
        c.setStrokeColor(LINE)
        c.line(MARGIN + 146 * mm, y - 2, PAGE_W - MARGIN, y - 2)
        y -= 13 * mm / 1.6

    _footer(c)
    c.showPage()
    c.save()
    return path
