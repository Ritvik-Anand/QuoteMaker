import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "quotation_maker.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS catalogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            item_count INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
            catalog_id INTEGER REFERENCES catalogs(id) ON DELETE CASCADE,
            code TEXT,
            description TEXT NOT NULL,
            unit TEXT DEFAULT 'Nos',
            base_price REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_number TEXT NOT NULL UNIQUE,
            client_name TEXT NOT NULL,
            client_address TEXT,
            date TEXT NOT NULL,
            gst_rate REAL DEFAULT 18.0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quotation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
            item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
            description TEXT NOT NULL,
            code TEXT,
            unit TEXT DEFAULT 'Nos',
            quantity REAL NOT NULL DEFAULT 1,
            base_price REAL NOT NULL,
            adjustment_type TEXT DEFAULT 'none',
            adjustment_value REAL DEFAULT 0,
            final_price REAL NOT NULL,
            sort_order INTEGER DEFAULT 0
        );
    """)
    conn.commit()

    # Migrations for existing databases
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "catalog_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN catalog_id INTEGER REFERENCES catalogs(id) ON DELETE CASCADE")
        conn.commit()

    conn.close()
