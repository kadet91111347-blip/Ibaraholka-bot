"""Database adapter — psycopg2 (Postgres) with sqlite3 fallback (local dev).

db_cursor() yields a unified cursor-like object that supports:
  .execute(sql, params) — returns self
  .executemany(sql, seq) — returns self
  .executescript(sql) — multi-statement (used in init_db)
  .fetchone() / .fetchall() — get rows
  .rowcount / .description — introspection
  .commit() / .rollback() — explicit commit
"""
import os
import threading
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL) and os.getenv("DEMO_MODE", "0").strip() != "1"


# ---------- Unified cursor wrapper ----------
class _CursorAdapter:
    """Wraps a native cursor (psycopg2 or sqlite3) with uniform dict-like rows
    on Postgres and executescript support on both."""

    def __init__(self, native_cursor, native_conn=None):
        self._c = native_cursor
        self._conn = native_conn

    def execute(self, sql, params=None):
        if params is None:
            self._c.execute(sql)
        else:
            self._c.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._c.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def description(self):
        return self._c.description

    @property
    def lastrowid(self):
        return getattr(self._c, "lastrowid", None)

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass

    def commit(self):
        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:
                pass

    def rollback(self):
        if self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def executescript(self, sql_script):
        """Run multi-statement script. SQLite native supports this; for psycopg2
        we split on ';' and run each non-empty statement."""
        backend = type(self._c).__module__.split(".")[0]
        if backend == "sqlite3":
            return self._c.executescript(sql_script)
        # psycopg2 path: split on ';'
        for stmt in sql_script.split(";"):
            s = stmt.strip()
            # strip SQL line comments
            cleaned_lines = []
            for line in s.split("\n"):
                line_stripped = line.strip()
                if line_stripped.startswith("--"):
                    continue
                cleaned_lines.append(line)
            cleaned = "\n".join(cleaned_lines).strip()
            if cleaned:
                self._c.execute(cleaned)
        return self


# ---------- Postgres (psycopg2) ----------
try:
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.pool as _pool

        _pool_lock = threading.Lock()
        _pool_obj = None

        def _get_pool():
            global _pool_obj
            with _pool_lock:
                if _pool_obj is None:
                    _pool_obj = _pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=10,
                        dsn=DATABASE_URL,
                    )
                return _pool_obj

        @contextmanager
        def db_cursor():
            """`with db_cursor() as cur: cur.execute(...).fetchone()`"""
            pool = _get_pool()
            conn = pool.getconn()
            try:
                try:
                    conn.rollback()  # clear aborted state
                except Exception:
                    pass
                cur = _CursorAdapter(conn.cursor(), native_conn=conn)
                try:
                    yield cur
                    conn.commit()
                finally:
                    cur.close()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                pool.putconn(conn)

        def safe_execute(sql, params=None):
            """One-shot fresh connection for init_db ALTERs (not from pool)."""
            try:
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()
                cur.execute(sql, params or ())
                conn.commit()
                cur.close()
                conn.close()
                return True
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                return False

    else:
        raise ImportError("SQLite path")
except ImportError:
    # ---------- SQLite ----------
    import sqlite3
    DB_PATH = os.getenv("DB_PATH", "ibaraholka.db")

    @contextmanager
    def db_cursor():
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = _CursorAdapter(conn.cursor(), native_conn=conn)
            try:
                yield cur
                conn.commit()
            finally:
                cur.close()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def safe_execute(sql, params=None):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(sql, params or ())
            conn.commit()
            conn.close()
            return True
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return False


def get_db_connection():
    """Legacy compat — returns a fresh DB connection (psycopg2 or sqlite3)."""
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    import sqlite3
    return sqlite3.connect(DB_PATH)


def migrate_sqlite_to_pg(*args, **kwargs):
    return None
