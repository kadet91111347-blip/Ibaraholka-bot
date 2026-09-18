"""
DB adapter: provides sqlite3-like API on top of psycopg2 OR pg8000 (PostgreSQL).
All existing main.py code keeps using get_db() / conn.execute / conn.row_factory.
"""

import os
import threading
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# === Backend selection ===
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import ThreadedConnectionPool
    _BACKEND = "psycopg2"
except ImportError:
    try:
        import pg8000
        from pg8000 import Connection
        _BACKEND = "pg8000"
    except ImportError:
        _BACKEND = None


_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()


class _PostgresUnavailable(Exception):
    """Raised when Postgres is configured but unreachable; triggers sqlite fallback."""
    pass


def _dict_row_factory(cursor):
    """Returns dict-like rows for both psycopg2 (extras) and pg8000."""
    if _BACKEND == "psycopg2":
        return cursor
    # pg8000: build dict from description + fetched rows
    desc = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(desc, row)) for row in rows]


def _init_pool(minconn=1, maxconn=10):
    """Create pool once. Returns existing pool if already initialized."""
    global _PG_POOL
    if _PG_POOL is not None:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is not None:
            return _PG_POOL
        try:
            if _BACKEND == "psycopg2":
                _PG_POOL = ThreadedConnectionPool(
                    minconn=minconn,
                    maxconn=maxconn,
                    dsn=DATABASE_URL,
                    connect_timeout=10,
                )
            elif _BACKEND == "pg8000":
                # Simple manual pool for pg8000
                _PG_POOL = {"min": minconn, "max": maxconn, "free": [], "used": set(), "lock": threading.Lock()}
            else:
                raise _PostgresUnavailable("No PostgreSQL driver installed (need psycopg2-binary or pg8000)")
            print(f"[db_adapter] PG pool initialized (backend={_BACKEND}, min={minconn}, max={maxconn})", flush=True)
            return _PG_POOL
        except Exception as e:
            print(f"[db_adapter] PG pool init failed: {e}", flush=True)
            raise _PostgresUnavailable(str(e))


def _pg8000_connect():
    """Open a new pg8000 connection from DATABASE_URL."""
    import pg8000
    # DATABASE_URL format: postgresql://user:pass@host:port/dbname?sslmode=require
    from urllib.parse import urlparse
    p = urlparse(DATABASE_URL)
    ssl_context = None
    if 'sslmode=require' in DATABASE_URL or p.scheme == 'postgres':
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    return pg8000.connect(
        host=p.hostname,
        port=p.port or 5432,
        user=p.username,
        password=p.password,
        database=p.path.lstrip('/'),
        ssl_context=ssl_context,
    )


@contextmanager
def get_db_connection():
    """Context manager that yields a connection with row_factory=RealDictCursor-like."""
    if not USE_POSTGRES:
        import sqlite3
        conn = sqlite3.connect(os.getenv("DB_PATH", "ibaraholka.db"))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
        return

    if _PG_POOL is None:
        _init_pool()

    if _BACKEND == "psycopg2":
        from psycopg2.extras import RealDictCursor
        raw = _PG_POOL.getconn()
        try:
            raw.cursor_factory = RealDictCursor
            yield raw
        finally:
            _PG_POOL.putconn(raw)
    else:
        # pg8000 manual pool
        with _PG_POOL["lock"]:
            if _PG_POOL["free"]:
                conn = _PG_POOL["free"].pop()
            else:
                if len(_PG_POOL["used"]) >= _PG_POOL["max"]:
                    raise _PostgresUnavailable("Pool exhausted")
                conn = _pg8000_connect()
                _PG_POOL["used"].add(id(conn))
        try:
            yield _Pg8000DictConn(conn)
        finally:
            with _PG_POOL["lock"]:
                _PG_POOL["free"].append(conn)
                _PG_POOL["used"].discard(id(conn))


class _Pg8000DictConn:
    """Wrap pg8000 connection to provide RealDictCursor-like API."""

    def __init__(self, conn):
        self._conn = conn
        self._tx_active = False

    def cursor(self):
        c = self._conn.cursor()
        return _Pg8000DictCursor(c)

    def execute(self, sql, params=None):
        c = self.cursor()
        return c.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class _Pg8000DictCursor:
    """Wrap pg8000 cursor to provide dict-like rows (mimics psycopg2.extras.RealDictCursor)."""

    def __init__(self, cursor):
        self._c = cursor
        self._row_factory = None  # we build dicts ourselves via fetch methods

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def description(self):
        return self._c.description

    def execute(self, sql, params=None):
        if params is None:
            return self._c.execute(sql)
        return self._c.execute(sql, params)

    def executemany(self, sql, seq):
        return self._c.executemany(sql, seq)

    def fetchone(self):
        row = self._c.fetchone()
        if row is None:
            return None
        desc = [d[0] for d in self._c.description]
        return dict(zip(desc, row))

    def fetchall(self):
        rows = self._c.fetchall()
        if not rows:
            return []
        desc = [d[0] for d in self._c.description]
        return [dict(zip(desc, row)) for row in rows]

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


def db_cursor():
    """Returns (conn, cursor) pair — for places where main.py uses db_cursor(ctx) pattern."""
    return get_db_connection()


def migrate_sqlite_to_pg(*args, **kwargs):
    """Stub: migration already done in main.py at startup. No-op here."""
    return None
