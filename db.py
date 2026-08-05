"""Database access for the Officers' Mess recipe scaling system.

One SQLite file, `mess.db`, holds everything. That single file *is* the recipe
database - back it up by taking a copy (or via /admin/backup, which uses the
online backup API so the copy is consistent).
"""

import os
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mess.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def connect():
    """Open a connection with the pragmas this deployment needs.

    Both pragmas matter and neither is the SQLite default:

    * journal_mode=WAL - the app is served over the LAN to several PCs. With the
      default rollback journal, two staff submitting requisitions at the same
      moment produce "database is locked". WAL persists in the database file
      once set, but re-issuing it is cheap and keeps a freshly copied mess.db
      correct too.
    * foreign_keys=ON - SQLite silently *ignores* foreign key constraints
      unless this is set per connection. Without it the ON DELETE clauses in
      schema.sql are decorative.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    """Return the request-scoped connection, opening it on first use."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exception=None):  # noqa: ARG001 - Flask passes the exception
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)


def init_db():
    """Create tables if absent and seed the first admin. Safe to re-run."""
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        schema = fh.read()

    conn = connect()
    try:
        conn.executescript(schema)
        conn.commit()
        seeded = _seed_admin(conn)
    finally:
        conn.close()
    return seeded


def _seed_admin(conn):
    """Create the bootstrap admin only when there are no users at all."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return False

    conn.execute(
        """INSERT INTO users (username, full_name, password_hash, role,
                              must_change_password)
           VALUES (?, ?, ?, 'admin', 1)""",
        (
            DEFAULT_ADMIN_USERNAME,
            "Mess Administrator",
            generate_password_hash(DEFAULT_ADMIN_PASSWORD),
        ),
    )
    conn.commit()
    return True


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """Run a write and commit. Returns lastrowid."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


def get_or_create_ingredient(name, default_unit, conn=None):
    """Resolve an ingredient name to an id, creating the master row if new.

    Names are compared case-insensitively (COLLATE NOCASE on the column), so
    "Onion" and "onion" resolve to the same master ingredient. The first
    spelling entered is the one kept.

    `conn` lets callers outside a Flask request - the document importer's CLI -
    reuse this resolution instead of writing their own, which would be the one
    place a second spelling of "Onion" could creep into the master list.
    """
    name = name.strip()

    if conn is None:
        row = query("SELECT id FROM ingredients WHERE name = ?", (name,), one=True)
        if row:
            return row["id"]
        return execute(
            "INSERT INTO ingredients (name, default_unit) VALUES (?, ?)",
            (name, default_unit),
        )

    row = conn.execute("SELECT id FROM ingredients WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO ingredients (name, default_unit) VALUES (?, ?)",
        (name, default_unit),
    )
    return cur.lastrowid
