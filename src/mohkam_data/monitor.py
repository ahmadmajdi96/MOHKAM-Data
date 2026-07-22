from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("rb") as handle:
        for _ in handle:
            count += 1
    return count


def scalar(conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def main() -> int:
    output_lines = count_lines(config.OUTPUT_FILE)
    key_lines = count_lines(config.KEYS_FILE)
    print(f"output={config.OUTPUT_FILE}")
    print(f"records={output_lines}")
    print(f"keys={key_lines}")

    if not config.PIPELINE_DB_FILE.exists():
        print(f"pipeline_db={config.PIPELINE_DB_FILE} missing")
        return 0

    conn = sqlite3.connect(config.PIPELINE_DB_FILE)
    print(f"pipeline_db={config.PIPELINE_DB_FILE}")
    print(f"seen={scalar(conn, 'SELECT COUNT(*) FROM seen')}")
    print(
        "shards "
        f"total={scalar(conn, 'SELECT COUNT(*) FROM shards')} "
        f"complete={scalar(conn, 'SELECT COUNT(*) FROM shards WHERE completed=1')} "
        f"review={scalar(conn, 'SELECT COUNT(*) FROM shards WHERE needs_review=1')}"
    )
    status_rows = conn.execute("SELECT status, COUNT(*) FROM details GROUP BY status ORDER BY status").fetchall()
    if status_rows:
        print("details " + " ".join(f"{status}={count}" for status, count in status_rows))
    else:
        print("details none")

    rows = conn.execute(
        """
        SELECT year, parent, court_id, last_indexed_page, records_found, details_queued,
               completed, needs_review, completion_reason, updated_at
        FROM shards
        ORDER BY updated_at DESC
        LIMIT 12
        """
    ).fetchall()
    for row in rows:
        year, parent, court_id, page, found, queued, completed, review, reason, updated_at = row
        print(
            f"shard year={year} parent={parent} court={court_id or '-'} page={page} "
            f"found={found} queued={queued} completed={bool(completed)} "
            f"review={bool(review)} reason={reason or ''} updated={updated_at}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
