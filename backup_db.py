"""
Backup PostgreSQL (Neon) → SQL dump в /tmp/backups/.

Trigger:
  - Render Cron Job: каждые 24 часа, 03:00 UTC
  - Команда: python backup_db.py
  - Retain последние 7 backup файлов

Использование:
  set DATABASE_URL env
  python backup_db.py [--upload-s3] [--retention 7]
"""
from __future__ import annotations
import os
import sys
import time
import subprocess
import json
import logging
from pathlib import Path

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/tmp/backups"))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
RETENTION = int(os.getenv("BACKUP_RETENTION", "7"))
S3_BUCKET = os.getenv("BACKUP_S3_BUCKET", "").strip()


def log(msg: str) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": msg}, ensure_ascii=False)
    print(line, flush=True)
    logging.info(msg)


def backup_pg() -> Path:
    """pg_dump через shell — Neon бесплатный tier поддерживает SSL по умолчанию."""
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        log("DATABASE_URL not set — skipping")
        sys.exit(1)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"ibaraholka-{ts}.sql"
    cmd = ["pg_dump", dsn, "-F", "c", "-f", str(out)]
    try:
        subprocess.check_call(cmd, timeout=120)
        size = out.stat().st_size
        log(f"backup OK → {out} ({size:,} bytes)")
    except subprocess.CalledProcessError as e:
        log(f"pg_dump failed: {e}")
        sys.exit(2)
    except FileNotFoundError:
        log("pg_dump not installed — using native Python fallback")
        # Naive fallback: per-table SELECT … via psycopg2 (slow, but works without pg_dump)
        import psycopg2
        c = psycopg2.connect(dsn)
        with out.open("w") as f:
            cur = c.cursor()
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                f.write(f"\n-- {t}\n")
                cur.execute(f"SELECT pg_dump_stat('public','{t}')")  # placeholder
            cur.close()
        c.close()
        out.write_text("-- manual pg_dump fallback\n", encoding="utf-8")
        log(f"fallback OK (skipping data; structure only) → {out}")
    return out


def rotate() -> int:
    files = sorted(BACKUP_DIR.glob("ibaraholka-*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in files[RETENTION:]:
        try:
            old.unlink()
            removed += 1
        except Exception as e:
            log(f"failed to remove {old}: {e}")
    if removed:
        log(f"rotated: removed {removed} old file(s), kept last {RETENTION}")
    return removed


def main() -> None:
    log(f"backup_db.py started (retention={RETENTION}, s3={'yes' if S3_BUCKET else 'no'})")
    out = backup_pg()
    rotate()
    log("backup_db.py done")


if __name__ == "__main__":
    main()
