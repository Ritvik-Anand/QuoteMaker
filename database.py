import sqlite3
import os
import secrets
from werkzeug.security import generate_password_hash

# Allow DATABASE_PATH env var so cloud deployments can point to a persistent volume
DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(__file__), "quotation_maker.db")
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # better concurrent access
    return conn


def init_db():
    # Ensure directory exists (for cloud volume mounts)
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

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

    _seed_admin()


def _seed_admin():
    """Create the first admin account if no users exist yet."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
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
    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,1)",
        (username, generate_password_hash(password, method="pbkdf2:sha256"))
    )
    conn.commit()
    conn.close()
