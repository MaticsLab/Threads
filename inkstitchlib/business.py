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


def get_note(kind):
    with _lock, _db() as con:
        row = con.execute('SELECT text FROM note WHERE kind=?', (kind,)).fetchone()
    return row['text'] if row else ''


def set_note(kind, text):
    with _lock, _db() as con:
        con.execute('INSERT INTO note (kind, text) VALUES (?, ?) '
                    'ON CONFLICT(kind) DO UPDATE SET text=excluded.text',
                    (kind, str(text)[:100000]))
