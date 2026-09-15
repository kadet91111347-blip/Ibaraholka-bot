"""
DB adapter: provides sqlite3-like API on top of psycopg2 (PostgreSQL).
All existing main.py code keeps using get_db() / conn.execute / conn.row_factory.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# When DATABASE_URL empty -> fallback to local sqlite for dev / Render free
USE_POSTGRES = bool(DATABASE_URL)


class _PostgresUnavailable(Exception):
    """Raised when Postgres is configured but unreachable; triggers sqlite fallback."""


class _PostgresRow(dict):
    """Row that allows both dict access and attribute access (sqlite3.Row-like)."""
    def __getitem__(self, k):
        return dict.__getitem__(self, k)
    def keys(self):
        return dict.keys(self)


class _PostgresCursor:
    """Cursor compatible with sqlite3 for our patterns."""
    def __init__(self, cursor):
        self._cursor = cursor
        self._description = cursor.description
        self._rowcount = cursor.rowcount

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self):
        return self._rowcount

    def execute(self, sql, params=None):
        # Convert ? placeholders to %s for psycopg
        sql = _convert_placeholders(sql)
        if params is None:
            self._cursor.execute(sql)
        else:
            if isinstance(params, (list, tuple)):
                self._cursor.execute(sql, params)
            else:
                self._cursor.execute(sql, (params,))
        return self

    def executemany(self, sql, seq):
        sql = _convert_placeholders(sql)
        self._cursor.executemany(sql, seq)
        return self

    def executescript(self, script):
        # sqlite3 allows multi-statement. For psycopg we split and execute each.
        for stmt in _split_statements(script):
            if stmt.strip():
                self._cursor.execute(_convert_placeholders(stmt))
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d.name for d in self._description]
        result = _PostgresRow(zip(cols, row))
        return result

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = [d.name for d in self._description]
        return [_PostgresRow(zip(cols, r)) for r in rows]

    def fetchmany(self, size=1):
        rows = self._cursor.fetchmany(size)
        cols = [d.name for d in self._description] if self._description else []
        return [_PostgresRow(zip(cols, r)) for r in rows]

    def close(self):
        try:
            self._cursor.close()
        except Exception:
            pass


class _PostgresConnection:
    """Connection wrapper compatible with sqlite3.Connection usage in main.py."""
    def __init__(self):
        # Parse URL to handle sslmode correctly
        try:
            self._conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            self._conn.autocommit = True
            self._cursor = _PostgresCursor(self._conn.cursor(cursor_factory=RealDictCursor))
        except Exception as e:
            print(f"[db_adapter] PG connection failed: {e}; falling back to sqlite", flush=True)
            raise _PostgresUnavailable(str(e))

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, val):
        pass  # No-op

    def execute(self, sql, params=None):
        self._cursor.execute(sql, params)
        return self._cursor

    def executemany(self, sql, seq):
        self._cursor.executemany(sql, seq)
        return self._cursor

    def executescript(self, script):
        self._cursor.executescript(script)
        return self

    def commit(self):
        try:
            self._conn.commit()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def cursor(self):
        return self._cursor


# ---- sqlite3 fallback ----
class _SqliteCursor:
    """Passthrough cursor with same execute/executescript API."""
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def execute(self, sql, params=None):
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._cursor.executemany(sql, seq)
        return self

    def executescript(self, script):
        self._cursor.executescript(script)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=1):
        return self._cursor.fetchmany(size)

    def close(self):
        self._cursor.close()


class _SqliteConnection:
    def __init__(self, db_file):
        import sqlite3 as _sql
        self._sqlite3 = _sql
        self._conn = _sql.connect(db_file, timeout=30, isolation_level=None)
        self._conn.row_factory = _sql.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")

    @property
    def row_factory(self):
        return self._sqlite3.Row

    @row_factory.setter
    def row_factory(self, val):
        self._conn.row_factory = val

    def execute(self, sql, params=None):
        if params is None:
            return _SqliteCursor(self._conn.execute(sql))
        return _SqliteCursor(self._conn.execute(sql, params))

    def executemany(self, sql, seq):
        self._conn.executemany(sql, seq)

    def executescript(self, script):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        return _SqliteCursor(self._conn.cursor())


# ---- placeholder conversion ----
def _convert_placeholders(sql):
    """Convert sqlite ? -> postgres %s. Only inside string."""
    # Avoid converting inside string literals (we keep simple approach - works for our schema)
    return sql.replace('?', '%s')


def _split_statements(script):
    """Split multi-statement script into individual statements."""
    out = []
    cur = []
    in_str = False
    ch = None
    for line in script.split('\n'):
        for c in line:
            cur.append(c)
            if c in ("'", '"'):
                if not in_str:
                    in_str = True
                    ch = c
                elif ch == c:
                    in_str = False
                    ch = None
        cur.append('\n')
        if not in_str and ';' in line:
            stmt = ''.join(cur).strip()
            if stmt and stmt != ';':
                out.append(stmt)
            cur = []
    rest = ''.join(cur).strip()
    if rest and rest != ';':
        out.append(rest)
    return out


# ---- public API ----
DB_FILE = os.getenv("DB_FILE", "/tmp/ibaraholka.db")


def get_db_connection():
    """Return DB connection (PostgreSQL if DATABASE_URL set AND reachable, else sqlite).

    If Postgres is configured but the password / network is wrong, we silently
    fall back to sqlite so the app keeps starting and we can debug later.
    """
    if USE_POSTGRES:
        try:
            return _PostgresConnection()
        except _PostgresUnavailable:
            print("[db_adapter] Falling back to sqlite (Postgres unavailable)", flush=True)
    return _SqliteConnection(DB_FILE)


@contextmanager
def db_cursor(commit=True):
    """Context manager: `with db_cursor() as conn:` (sqlite3-style)."""
    conn = get_db_connection()
    try:
        yield conn
        if commit:
            conn.commit()
    finally:
        conn.close()


def migrate_sqlite_to_pg(source_db_file):
    """One-time migration helper: copy rows from sqlite to postgres."""
    import sqlite3
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    src = sqlite3.connect(source_db_file)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(DATABASE_URL)
    dst.autocommit = True
    tables = ['listings', 'payments', 'scammers', 'sales_scripts',
              'conversations', 'ai_responses', 'variant_stats',
              'user_profiles', 'learned_patterns', 'withdrawals']
    for t in tables:
        try:
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            if not rows:
                print(f"  {t}: empty")
                continue
            cols = [d[0] for d in src.execute(f"SELECT * FROM {t}").description]
            placeholders = ','.join(['%s'] * len(cols))
            col_str = ','.join(cols)
            # Identify timestamp columns (microseconds in sqlite) and convert to seconds
            ts_cols = {'created', 'expires_at', 'updated_at', 'paid_at', 'created_at'}
            for r in rows:
                vals = []
                for c in cols:
                    v = r[c]
                    if c in ts_cols and isinstance(v, int) and v > 10**12:
                        v = v // 1000  # microseconds -> seconds
                    vals.append(v)
                try:
                    cur = dst.cursor()
                    cur.execute(f"INSERT INTO {t} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", vals)
                except Exception as e:
                    print(f"  skip {t}: {e}")
            dst.commit()
            print(f"  {t}: {len(rows)} rows")
        except Exception as e:
            print(f"  {t}: {e}")
    src.close()
    dst.close()
    print("Migration done")


PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    user_name TEXT,
    user_username TEXT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    price BIGINT DEFAULT 0,
    cat TEXT NOT NULL,
    type TEXT DEFAULT 'sell',
    contact TEXT NOT NULL,
    photo TEXT DEFAULT '',
    tier TEXT DEFAULT 'free',
    city TEXT DEFAULT 'Москва',
    status TEXT DEFAULT 'pending',
    created BIGINT NOT NULL,
    expires_at BIGINT,
    channel_message_id BIGINT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_tier ON listings(tier);
CREATE INDEX IF NOT EXISTS idx_cat ON listings(cat);
CREATE INDEX IF NOT EXISTS idx_user ON listings(user_id);
CREATE INDEX IF NOT EXISTS idx_created ON listings(created);
"""

