from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .raw_html_archiver import RAW_DIR, RAW_STATE_DB


def human_size(bytes_count: int) -> str:
    value = float(bytes_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def scalar(conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def main() -> int:
    if not RAW_STATE_DB.exists():
        print(f"raw state not found: {RAW_STATE_DB}")
        return 0

    conn = sqlite3.connect(RAW_STATE_DB)
    search_saved = scalar(conn, "SELECT COUNT(*) FROM search_pages WHERE status IN ('saved', 'empty')")
    search_bad = scalar(conn, "SELECT COUNT(*) FROM search_pages WHERE status='bad'")
    search_bytes = scalar(conn, "SELECT COALESCE(SUM(bytes), 0) FROM search_pages")
    detail_saved = scalar(conn, "SELECT COUNT(*) FROM detail_pages WHERE status='saved'")
    detail_pending = scalar(conn, "SELECT COUNT(*) FROM detail_pages WHERE status='pending'")
    detail_failed = scalar(conn, "SELECT COUNT(*) FROM detail_pages WHERE status IN ('failed', 'bad')")
    detail_bytes = scalar(conn, "SELECT COALESCE(SUM(bytes), 0) FROM detail_pages")
    completed_shards = scalar(conn, "SELECT COUNT(*) FROM shards WHERE completed=1")
    active_shards = scalar(conn, "SELECT COUNT(*) FROM shards WHERE completed=0")
    disk_files = sum(1 for path in RAW_DIR.rglob("*.html.gz") if path.is_file()) if RAW_DIR.exists() else 0

    print(f"raw_dir={RAW_DIR}")
    print(f"shards completed={completed_shards} active={active_shards}")
    print(f"search_pages saved_or_empty={search_saved} bad={search_bad} bytes={human_size(search_bytes)}")
    print(f"detail_pages saved={detail_saved} pending={detail_pending} failed_or_bad={detail_failed} bytes={human_size(detail_bytes)}")
    print(f"html_gz_files={disk_files} total_bytes={human_size(search_bytes + detail_bytes)}")

    rows = conn.execute(
        """
        SELECT year, parent, court_id, last_completed_page, completed, completion_reason, updated_at
        FROM shards
        ORDER BY year DESC, parent ASC
        LIMIT 20
        """
    ).fetchall()
    for year, parent, court_id, page, completed, reason, updated_at in rows:
        print(
            f"shard year={year} parent={parent} court={court_id or '-'} "
            f"page={page} completed={bool(completed)} reason={reason or ''} updated_at={updated_at}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
