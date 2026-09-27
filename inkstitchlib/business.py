"""Client/vendor database and notes, ported from embTools (database.cpp,
mainwindow.cpp).

embTools keeps Client and Vendor tables (name, email, phone, mobile, address,
billing address, business name, website) with add/update/delete and sorting,
plus three persisted text panes (notes, quote log, to-do). Same schema here,
in SQLite via the stdlib, stored under STITCHFORGE_DATA (defaults to ./data).
"""
import os
import sqlite3
import threading

DATA_DIR = os.environ.get(
    'STITCHFORGE_DATA',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))

FIELDS = ['name', 'email', 'phone', 'mobile', 'address',
          'billing_address', 'business_name', 'website']
KINDS = ('client', 'vendor')
NOTE_KINDS = ('notes', 'quotes', 'todo')

_lock = threading.Lock()


def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(os.path.join(DATA_DIR, 'business.sqlite3'))
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS contact (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK(kind IN ('client','vendor')),
        name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', mobile TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '', billing_address TEXT NOT NULL DEFAULT '',
        business_name TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '')''')
    con.execute('''CREATE TABLE IF NOT EXISTS note (
        kind TEXT PRIMARY KEY CHECK(kind IN ('notes','quotes','todo')),
        text TEXT NOT NULL DEFAULT '')''')
    con.execute('''CREATE TABLE IF NOT EXISTS theme (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        colors TEXT NOT NULL DEFAULT '[]')''')
    con.execute('''CREATE TABLE IF NOT EXISTS design (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER,
        name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        notes TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        stitches INTEGER NOT NULL DEFAULT 0,
        width_mm REAL NOT NULL DEFAULT 0,
        height_mm REAL NOT NULL DEFAULT 0,
        colors TEXT NOT NULL DEFAULT '[]',
        created TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT '')''')
    con.execute('''CREATE TABLE IF NOT EXISTS hoop (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        w_mm REAL NOT NULL, h_mm REAL NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS wtheme (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        config TEXT NOT NULL DEFAULT '{}')''')
    return con


def list_contacts(kind, sort='name'):
    order = 'business_name' if sort == 'business' else 'name'
    with _lock, _db() as con:
        rows = con.execute(
            'SELECT * FROM contact WHERE kind=? ORDER BY %s COLLATE NOCASE ASC' % order,
            (kind,)).fetchall()
    return [dict(r) for r in rows]


def add_contact(kind, data):
    vals = [str(data.get(f, '') or '')[:500] for f in FIELDS]
    with _lock, _db() as con:
        cur = con.execute(
            'INSERT INTO contact (kind, %s) VALUES (?%s)'
            % (', '.join(FIELDS), ', ?' * len(FIELDS)),
            [kind] + vals)
        return cur.lastrowid


def update_contact(kind, cid, data):
    sets = ', '.join('%s=?' % f for f in FIELDS)
    vals = [str(data.get(f, '') or '')[:500] for f in FIELDS]
    with _lock, _db() as con:
        cur = con.execute(
            'UPDATE contact SET %s WHERE id=? AND kind=?' % sets,
            vals + [cid, kind])
        return cur.rowcount > 0


def delete_contact(kind, cid):
    with _lock, _db() as con:
        cur = con.execute('DELETE FROM contact WHERE id=? AND kind=?', (cid, kind))
        if kind == 'client' and cur.rowcount:
            # a client's designs stay in the library, unassigned
            con.execute('UPDATE design SET client_id=NULL WHERE client_id=?', (cid,))
        return cur.rowcount > 0


def get_contact(kind, cid):
    with _lock, _db() as con:
        r = con.execute('SELECT * FROM contact WHERE id=? AND kind=?', (cid, kind)).fetchone()
    return dict(r) if r else None


def client_summaries():
    """Clients with their design count and last design activity."""
    with _lock, _db() as con:
        rows = con.execute(
            '''SELECT c.*, COUNT(d.id) AS design_count, MAX(d.updated) AS last_design
               FROM contact c LEFT JOIN design d ON d.client_id = c.id
               WHERE c.kind='client' GROUP BY c.id
               ORDER BY c.name COLLATE NOCASE ASC''').fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------- design library (per client)
# Saved designs outlive the in-memory/tmp jobs: each one keeps its machine
# file, metadata and preview under DATA_DIR/designs/<id>/ and is restored
# into a working job when opened.
DESIGN_STATUSES = ('draft', 'approved', 'in_production', 'done')
DESIGN_FILES = ('design.dst', 'meta.json', 'preview.png')


def design_dir(did):
    return os.path.join(DATA_DIR, 'designs', str(int(did)))


def _design_row(r):
    import json
    d = dict(r)
    try:
        d['colors'] = json.loads(d['colors'])
    except Exception:
        d['colors'] = []
    return d


def list_designs(client_id=None, unassigned=False):
    q = 'SELECT * FROM design'
    args = ()
    if unassigned:
        q += ' WHERE client_id IS NULL'
    elif client_id is not None:
        q += ' WHERE client_id=?'
        args = (client_id,)
    q += ' ORDER BY updated DESC, id DESC'
    with _lock, _db() as con:
        rows = con.execute(q, args).fetchall()
    return [_design_row(r) for r in rows]


def get_design(did):
    with _lock, _db() as con:
        r = con.execute('SELECT * FROM design WHERE id=?', (did,)).fetchone()
    return _design_row(r) if r else None


def _now():
    import datetime
    return datetime.datetime.now().isoformat(timespec='seconds')


def _clean_client(con, client_id):
    if client_id in (None, '', 0, '0'):
        return None
    cid = int(client_id)
    r = con.execute("SELECT id FROM contact WHERE id=? AND kind='client'", (cid,)).fetchone()
    if not r:
        raise ValueError('unknown client')
    return cid


def save_design(src_dir, name, client_id=None, did=None, summary=None):
    """Copy a job's files into the library (new design, or overwrite `did`)."""
    import json
    import shutil
    summary = summary or {}
    name = str(name or 'Untitled design').strip()[:120] or 'Untitled design'
    now = _now()
    vals = (name, summary.get('kind', ''), int(summary.get('stitches', 0)),
            float(summary.get('width_mm', 0)), float(summary.get('height_mm', 0)),
            json.dumps(summary.get('colors', [])[:64]))
    with _lock, _db() as con:
        cid = _clean_client(con, client_id)
        if did:
            cur = con.execute(
                '''UPDATE design SET name=?, kind=?, stitches=?, width_mm=?, height_mm=?,
                   colors=?, client_id=?, updated=? WHERE id=?''', vals + (cid, now, did))
            if cur.rowcount == 0:
                raise KeyError('unknown design')
        else:
            did = con.execute(
                '''INSERT INTO design (name, kind, stitches, width_mm, height_mm, colors,
                   client_id, created, updated) VALUES (?,?,?,?,?,?,?,?,?)''',
                vals + (cid, now, now)).lastrowid
    out = design_dir(did)
    os.makedirs(out, exist_ok=True)
    for fn in DESIGN_FILES:
        p = os.path.join(src_dir, fn)
        if os.path.exists(p):
            shutil.copyfile(p, os.path.join(out, fn))
    return did


def update_design(did, data):
    sets, args = [], []
    with _lock, _db() as con:
        if 'name' in data:
            sets.append('name=?')
            args.append(str(data['name'] or '').strip()[:120] or 'Untitled design')
        if 'status' in data:
            if data['status'] not in DESIGN_STATUSES:
                raise ValueError('status must be one of %s' % ', '.join(DESIGN_STATUSES))
            sets.append('status=?'); args.append(data['status'])
        if 'notes' in data:
            sets.append('notes=?'); args.append(str(data['notes'] or '')[:5000])
        if 'client_id' in data:
            sets.append('client_id=?'); args.append(_clean_client(con, data['client_id']))
        if not sets:
            return get_design_unlocked(con, did) is not None
        sets.append('updated=?'); args.append(_now())
        cur = con.execute('UPDATE design SET %s WHERE id=?' % ', '.join(sets), args + [did])
        return cur.rowcount > 0


def get_design_unlocked(con, did):
    return con.execute('SELECT id FROM design WHERE id=?', (did,)).fetchone()


def delete_design(did):
    import shutil
    with _lock, _db() as con:
        cur = con.execute('DELETE FROM design WHERE id=?', (did,))
    shutil.rmtree(design_dir(did), ignore_errors=True)
    return cur.rowcount > 0


# ------------------------------------------------ design themes (colourways)
def list_themes():
    import json
    with _lock, _db() as con:
        rows = con.execute('SELECT * FROM theme ORDER BY id DESC').fetchall()
    out = []
    for r in rows:
        try:
            colors = json.loads(r['colors'])
        except Exception:
            colors = []
        out.append({'id': r['id'], 'name': r['name'], 'colors': colors})
    return out


def add_theme(name, colors):
    import json
    clean = []
    for c in colors[:64]:
        hexv = str(c.get('hex', '')).lstrip('#')
        if len(hexv) != 6:
            continue
        clean.append({'hex': '#' + hexv.upper(),
                      'name': str(c.get('name', '') or '')[:64],
                      'number': str(c.get('number', '') or '')[:16],
                      'palette': str(c.get('palette', '') or '')[:80]})
    if not clean:
        raise ValueError('a theme needs at least one colour')
    with _lock, _db() as con:
        cur = con.execute('INSERT INTO theme (name, colors) VALUES (?, ?)',
                          (str(name or 'Theme').strip()[:64] or 'Theme',
                           json.dumps(clean)))
        return cur.lastrowid


def delete_theme(tid):
    with _lock, _db() as con:
        cur = con.execute('DELETE FROM theme WHERE id=?', (tid,))
        return cur.rowcount > 0


# --------------------------------- worksheet appearance themes (Design panel)
WT_FONTS = ('Helvetica', 'Times', 'Courier')


def _wt_clean(config):
    import re
    c = config or {}
    accent = str(c.get('accent', '#12161C'))
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', accent):
        accent = '#12161C'
    return {
        'accent': accent.upper(),
        'font': c.get('font') if c.get('font') in WT_FONTS else 'Helvetica',
        'show_logo': bool(c.get('show_logo', True)),
        'logo_pos': c.get('logo_pos') if c.get('logo_pos') in ('left', 'right') else 'right',
        'logo_h_mm': max(5.0, min(25.0, float(c.get('logo_h_mm', 12) or 12))),
        'footer': str(c.get('footer', '') or '')[:120],
    }


def _wt_logo(wid):
    return os.path.join(DATA_DIR, 'wthemes', 'logo_%d' % wid)


def list_wthemes():
    import json
    with _lock, _db() as con:
        rows = con.execute('SELECT * FROM wtheme ORDER BY id DESC').fetchall()
    out = []
    for r in rows:
        try:
            cfg = _wt_clean(json.loads(r['config']))
        except Exception:
            cfg = _wt_clean({})
        out.append({'id': r['id'], 'name': r['name'], 'config': cfg,
                    'has_logo': os.path.exists(_wt_logo(r['id']))})
    return out


def save_wtheme(name, config, wid=None, logo_bytes=None):
    import json
    name = str(name or 'Worksheet theme').strip()[:64] or 'Worksheet theme'
    cfg = json.dumps(_wt_clean(config))
    with _lock, _db() as con:
        if wid:
            cur = con.execute('UPDATE wtheme SET name=?, config=? WHERE id=?',
                              (name, cfg, wid))
            if cur.rowcount == 0:
                return None
        else:
            wid = con.execute('INSERT INTO wtheme (name, config) VALUES (?, ?)',
                              (name, cfg)).lastrowid
    if logo_bytes:
        os.makedirs(os.path.join(DATA_DIR, 'wthemes'), exist_ok=True)
        with open(_wt_logo(wid), 'wb') as f:
            f.write(logo_bytes)
    return wid


def get_wtheme(wid):
    for t in list_wthemes():
        if t['id'] == wid:
            t['logo_path'] = _wt_logo(wid) if t['has_logo'] else None
            return t
    return None


def delete_wtheme(wid):
    with _lock, _db() as con:
        cur = con.execute('DELETE FROM wtheme WHERE id=?', (wid,))
    if os.path.exists(_wt_logo(wid)):
        os.remove(_wt_logo(wid))
    return cur.rowcount > 0


def get_note(kind):
    with _lock, _db() as con:
        row = con.execute('SELECT text FROM note WHERE kind=?', (kind,)).fetchone()
    return row['text'] if row else ''


def set_note(kind, text):
    with _lock, _db() as con:
        con.execute('INSERT INTO note (kind, text) VALUES (?, ?) '
                    'ON CONFLICT(kind) DO UPDATE SET text=excluded.text',
                    (kind, str(text)[:100000]))


# ------------------------------------------------------------ hoops
# Built-in hoop sizes by machine family (sewing field, mm), plus the
# custom ones a studio saves. The UI sizes a design to the chosen hoop.
BUILTIN_HOOPS = [
    ('4" × 4"', 100, 100), ('5" × 7"', 130, 180), ('6" × 10"', 160, 260),
    ('8" × 8"', 200, 200), ('8" × 12"', 200, 300), ('9.5" × 14"', 240, 360),
    ('Brother 4" × 9.25"', 100, 235), ('Brother 5" × 12"', 130, 300),
    ('Brother 10.6" × 16"', 272, 408), ('Janome 5.5" × 5.5"', 140, 140),
    ('Janome 7.9" × 11" (SQ23)', 230, 230), ('Janome 9.1" × 11.8" (GR)', 230, 300),
    ('Ricoma 2.5" × 2.5" (cap)', 65, 65), ('Ricoma 4.7" (12 cm)', 120, 120),
    ('Ricoma 5.9" (15 cm)', 150, 150), ('Ricoma 7.9" (20 cm)', 200, 200),
    ('Ricoma 11.8" × 15.7" (30 × 40)', 300, 400), ('Tajima 4.7" (12 cm)', 120, 120),
    ('Tajima 7.1" (18 cm)', 180, 180), ('Tajima 11.8" × 13.8" (30 × 35)', 300, 350),
    ('Cap frame 2.6" × 5.5"', 67, 140), ('Left chest 4" × 4"', 100, 100),
]


def list_hoops():
    out = [{'id': 0, 'name': n, 'w_mm': w, 'h_mm': h, 'custom': False}
           for n, w, h in BUILTIN_HOOPS]
    with _lock, _db() as con:
        rows = con.execute('SELECT * FROM hoop ORDER BY name COLLATE NOCASE').fetchall()
    out += [{'id': r['id'], 'name': r['name'], 'w_mm': r['w_mm'], 'h_mm': r['h_mm'],
             'custom': True} for r in rows]
    return out


def add_hoop(name, w_mm, h_mm):
    w, h = float(w_mm), float(h_mm)
    if not (20 <= w <= 1000 and 20 <= h <= 1000):
        raise ValueError('hoop sides must be between 20 and 1000 mm')
    name = str(name or '').strip()[:60] or '%g × %g mm' % (w, h)
    with _lock, _db() as con:
        return con.execute('INSERT INTO hoop (name, w_mm, h_mm) VALUES (?, ?, ?)',
                           (name, w, h)).lastrowid


def delete_hoop(hid):
    with _lock, _db() as con:
        return con.execute('DELETE FROM hoop WHERE id=?', (hid,)).rowcount > 0
