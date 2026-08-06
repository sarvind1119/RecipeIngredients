"""Database access for the Officers' Mess recipe scaling system.

One SQLite file, `mess.db`, holds everything. That single file *is* the recipe
database - back it up by taking a copy (or via /admin/backup, which uses the
online backup API so the copy is consistent).
"""

import os
import sqlite3
from contextlib import contextmanager

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
        # Multi-step writers use transaction(); a leftover open transaction on
        # an exceptional path is discarded so half-applied state never lands.
        if exception is not None:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
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
    """Run a write. Does not commit.

    Single-statement callers must call commit() afterwards. Multi-step writers
    (recipe replace, requisition freeze) must use transaction() so a failure
    mid-loop cannot leave a half-applied dish or Store indent visible to other
    Mess PCs under WAL.
    """
    db = get_db()
    cur = db.execute(sql, args)
    last_id = cur.lastrowid
    cur.close()
    return last_id


def commit():
    get_db().commit()


def rollback():
    get_db().rollback()


@contextmanager
def transaction():
    """Commit on success, roll back on any exception.

    While the block runs, execute() does not auto-commit (it never does);
    all statements share one SQLite transaction on the request connection.
    """
    try:
        yield get_db()
        get_db().commit()
    except Exception:
        get_db().rollback()
        raise


def _ingredient_id_by_name(conn, name):
    row = conn.execute(
        "SELECT id FROM ingredients WHERE name = ?", (name,)
    ).fetchone()
    return row["id"] if row else None


def get_or_create_ingredient(name, default_unit, conn=None):
    """Resolve an ingredient name to an id, creating the master row if new.

    Names are compared case-insensitively (COLLATE NOCASE on the column), so
    "Onion" and "onion" resolve to the same master ingredient. The first
    spelling entered is the one kept.

    `conn` lets callers outside a Flask request - the document importer's CLI -
    reuse this resolution instead of writing their own, which would be the one
    place a second spelling of "Onion" could creep into the master list.

    Concurrent staff can introduce the same new name at once; the UNIQUE
    constraint is the source of truth, and IntegrityError falls back to SELECT.
    """
    name = name.strip()
    if conn is None:
        conn = get_db()

    existing = _ingredient_id_by_name(conn, name)
    if existing is not None:
        return existing

    # SAVEPOINT so a UNIQUE conflict with a concurrent first-insert only rolls
    # back this statement group — not the ambient recipe/requisition transaction.
    # Without it, IntegrityError leaves the connection mid-transaction on an old
    # snapshot and a plain re-SELECT can still miss the peer's committed row.
    conn.execute("SAVEPOINT sp_get_or_create_ingredient")
    try:
        cur = conn.execute(
            "INSERT INTO ingredients (name, default_unit) VALUES (?, ?)",
            (name, default_unit),
        )
        conn.execute("RELEASE SAVEPOINT sp_get_or_create_ingredient")
        # Caller owns the commit: web paths use transaction()/commit(); the CLI
        # passes conn and commits once after the whole load.
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT sp_get_or_create_ingredient")
        conn.execute("RELEASE SAVEPOINT sp_get_or_create_ingredient")
        existing = _ingredient_id_by_name(conn, name)
        if existing is not None:
            return existing
        raise
