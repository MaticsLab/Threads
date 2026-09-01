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
