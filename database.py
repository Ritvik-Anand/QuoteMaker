import os
import sqlite3
import secrets
from werkzeug.security import generate_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")          # set on Vercel → PostgreSQL
DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotation_maker.db")
)


# ── Unified cursor ─────────────────────────────────────────────────────────────

class _Cursor:
    """Always returns plain dicts regardless of backend."""
    def __init__(self, raw):
        self._r = raw

    def fetchone(self):
        row = self._r.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._r.fetchall()]

    def __iter__(self):
        for row in self._r:
            yield dict(row)


# ── Connection wrapper ─────────────────────────────────────────────────────────

class DB:
    """Thin wrapper that normalises SQLite and PostgreSQL into one API."""

    def __init__(self):
        if DATABASE_URL:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(DATABASE_URL,
                                          cursor_factory=psycopg2.extras.RealDictCursor,
                                          sslmode="require")
            self._pg = True
        else:
            os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._pg = False

    def _sql(self, sql):
        return sql.replace("?", "%s") if self._pg else sql

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(self._sql(sql), params)
        return _Cursor(cur)

    def executemany(self, sql, params_list):
        cur = self._conn.cursor()
        cur.executemany(self._sql(sql), list(params_list))

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row id."""
        cur = self._conn.cursor()
        if self._pg:
            cur.execute(self._sql(sql) + " RETURNING id", params)
            return cur.fetchone()["id"]
        cur.execute(sql, params)
        return cur.lastrowid

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db() -> DB:
    return DB()


# ── Schema ─────────────────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin    INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS suppliers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS catalogs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    item_count  INTEGER DEFAULT 0,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    catalog_id  INTEGER REFERENCES catalogs(id) ON DELETE CASCADE,
    code        TEXT,
    description TEXT NOT NULL,
    unit        TEXT DEFAULT 'Nos',
    base_price  REAL NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number   TEXT NOT NULL UNIQUE,
    client_name    TEXT NOT NULL,
    client_address TEXT,
    date           TEXT NOT NULL,
    gst_rate       REAL DEFAULT 18.0,
    notes          TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotation_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id     INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    item_id          INTEGER REFERENCES items(id) ON DELETE SET NULL,
    description      TEXT NOT NULL,
    code             TEXT,
    unit             TEXT DEFAULT 'Nos',
    quantity         REAL NOT NULL DEFAULT 1,
    base_price       REAL NOT NULL,
    adjustment_type  TEXT DEFAULT 'none',
    adjustment_value REAL DEFAULT 0,
    final_price      REAL NOT NULL,
    sort_order       INTEGER DEFAULT 0
);
"""

_PG_TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        id            SERIAL PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_admin      INTEGER DEFAULT 0,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS suppliers (
        id         SERIAL PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS catalogs (
        id          SERIAL PRIMARY KEY,
        supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        item_count  INTEGER DEFAULT 0,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS items (
        id          SERIAL PRIMARY KEY,
        supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
        catalog_id  INTEGER REFERENCES catalogs(id) ON DELETE CASCADE,
        code        TEXT,
        description TEXT NOT NULL,
        unit        TEXT DEFAULT 'Nos',
        base_price  REAL NOT NULL,
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS quotations (
        id             SERIAL PRIMARY KEY,
        quote_number   TEXT NOT NULL UNIQUE,
        client_name    TEXT NOT NULL,
        client_address TEXT,
        date           TEXT NOT NULL,
        gst_rate       REAL DEFAULT 18.0,
        notes          TEXT,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS quotation_items (
        id               SERIAL PRIMARY KEY,
        quotation_id     INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
        item_id          INTEGER REFERENCES items(id) ON DELETE SET NULL,
        description      TEXT NOT NULL,
        code             TEXT,
        unit             TEXT DEFAULT 'Nos',
        quantity         REAL NOT NULL DEFAULT 1,
        base_price       REAL NOT NULL,
        adjustment_type  TEXT DEFAULT 'none',
        adjustment_value REAL DEFAULT 0,
        final_price      REAL NOT NULL,
        sort_order       INTEGER DEFAULT 0
    )""",
]


def init_db():
    conn = get_db()
    if conn._pg:
        for stmt in _PG_TABLES:
            conn.execute(stmt)
        # Migration: add catalog_id if missing
        exists = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='items' AND column_name='catalog_id'"
        ).fetchone()
        if not exists:
            conn.execute(
                "ALTER TABLE items ADD COLUMN catalog_id INTEGER REFERENCES catalogs(id) ON DELETE CASCADE"
            )
    else:
        for stmt in _SQLITE_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn._conn.execute(stmt)
        # Migration: add catalog_id if missing
        cols = {r[1] for r in conn._conn.execute("PRAGMA table_info(items)")}
        if "catalog_id" not in cols:
            conn._conn.execute(
                "ALTER TABLE items ADD COLUMN catalog_id INTEGER REFERENCES catalogs(id) ON DELETE CASCADE"
            )
    conn.commit()
    conn.close()
    _seed_admin()


def _seed_admin():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    conn.close()
    if count > 0:
        return

    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        password = secrets.token_urlsafe(10)
        print(f"\n{'='*50}")
        print(f"  First-run setup: admin account created")
        print(f"  Username : {username}")
        print(f"  Password : {password}")
        print(f"  Change this after logging in!")
        print(f"{'='*50}\n")

    conn = get_db()
    conn.insert(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,?)",
        (username, generate_password_hash(password, method="pbkdf2:sha256"), 1)
    )
    conn.commit()
    conn.close()
