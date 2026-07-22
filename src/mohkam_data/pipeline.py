from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from . import config
from .scraper import (
    RateLimitedError,
    UnverifiedEmptyPageError,
    classify_unverified_empty_html,
    configured_court_shards,
    ensure_dirs,
    extract_judgment_sections,
    is_verified_no_results_html,
    login,
    parse_result_blocks,
    record_key,
    request_with_retries,
    search_results_url,
    sleep_jitter,
)


DB_LOCK = threading.RLock()
OUTPUT_LOCK = threading.Lock()
KEYS_LOCK = threading.Lock()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(config.PIPELINE_DB_FILE, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            serial TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'pipeline',
            created_at TEXT NOT NULL,
            PRIMARY KEY (serial, entity_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shards (
            year INTEGER NOT NULL,
            parent INTEGER NOT NULL,
            court_id TEXT NOT NULL DEFAULT '',
            last_indexed_page INTEGER NOT NULL DEFAULT 0,
            records_found INTEGER NOT NULL DEFAULT 0,
            details_queued INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            needs_review INTEGER NOT NULL DEFAULT 0,
            completion_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (year, parent, court_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS details (
            serial TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            date TEXT,
            year INTEGER NOT NULL,
            parent INTEGER NOT NULL,
            court_id TEXT NOT NULL DEFAULT '',
            source_page INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (serial, entity_type)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_details_status ON details(status, attempts, updated_at)")
    conn.commit()
    reset_stale_processing(conn)
    return conn


def reset_stale_processing(conn: sqlite3.Connection) -> None:
    with DB_LOCK:
        conn.execute(
            "UPDATE details SET status='pending', updated_at=? WHERE status='processing'",
            (utc_now(),),
        )
        conn.commit()


def bootstrap_seen_keys(conn: sqlite3.Connection) -> None:
    if not config.BOOTSTRAP_SEEN_KEYS or not config.KEYS_FILE.exists():
        return
    row = conn.execute("SELECT COUNT(*) AS count FROM seen").fetchone()
    if row and int(row["count"]) > 0:
        return
    inserted = 0
    with config.KEYS_FILE.open("r", encoding="utf-8", errors="ignore") as handle, DB_LOCK:
        for line in handle:
            parts = line.strip().split("\t", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO seen(serial, entity_type, source, created_at) VALUES (?, ?, 'legacy_keys', ?)",
                (parts[0], parts[1], utc_now()),
            )
            inserted += 1
            if inserted % 5000 == 0:
                conn.commit()
        conn.commit()
    print(f"[pipeline] bootstrapped_seen_keys={inserted}", flush=True)


def shard_key(court_id: str | None = None) -> str:
    return court_id or ""


def load_shard(conn: sqlite3.Connection, year: int, parent: int, court_id: str | None = None) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM shards WHERE year=? AND parent=? AND court_id=?",
        (year, parent, shard_key(court_id)),
    ).fetchone()


def ensure_shard(conn: sqlite3.Connection, year: int, parent: int, court_id: str | None = None) -> None:
    with DB_LOCK:
        conn.execute(
            """
            INSERT OR IGNORE INTO shards
                (year, parent, court_id, last_indexed_page, records_found, details_queued, completed, needs_review, updated_at)
            VALUES (?, ?, ?, 0, 0, 0, 0, 0, ?)
            """,
            (year, parent, shard_key(court_id), utc_now()),
        )
        conn.commit()


def update_shard_progress(
    conn: sqlite3.Connection,
    year: int,
    parent: int,
    page: int,
    records_found: int,
    details_queued: int,
    court_id: str | None = None,
) -> None:
    with DB_LOCK:
        conn.execute(
            """
            UPDATE shards SET
                last_indexed_page=?,
                records_found=records_found+?,
                details_queued=details_queued+?,
                updated_at=?
            WHERE year=? AND parent=? AND court_id=?
            """,
            (page, records_found, details_queued, utc_now(), year, parent, shard_key(court_id)),
        )
        conn.commit()


def complete_shard(
    conn: sqlite3.Connection,
    year: int,
    parent: int,
    reason: str,
    needs_review: bool = False,
    court_id: str | None = None,
) -> None:
    with DB_LOCK:
        conn.execute(
            """
            UPDATE shards SET completed=?, needs_review=?, completion_reason=?, updated_at=?
            WHERE year=? AND parent=? AND court_id=?
            """,
            (0 if needs_review else 1, 1 if needs_review else 0, reason, utc_now(), year, parent, shard_key(court_id)),
        )
        conn.commit()


def is_seen(conn: sqlite3.Connection, serial: str, entity_type: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen WHERE serial=? AND entity_type=?", (serial, entity_type)).fetchone()
    return row is not None


def enqueue_detail(conn: sqlite3.Connection, record: dict[str, Any], year: int, parent: int, page: int, court_id: str | None = None) -> bool:
    serial = str(record.get("serial") or "")
    entity_type = str(record.get("entity_type") or "")
    if not serial or not entity_type or is_seen(conn, serial, entity_type):
        return False
    with DB_LOCK:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO details
                (serial, entity_type, url, title, date, year, parent, court_id, source_page, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                serial,
                entity_type,
                record.get("url") or "",
                record.get("title"),
                record.get("date"),
                year,
                parent,
                shard_key(court_id),
                page,
                utc_now(),
                utc_now(),
            ),
        )
        conn.commit()
    return cursor.rowcount > 0


def classify_search_html(html_text: str, records: list[dict[str, Any]]) -> tuple[str, str | None]:
    if records:
        return "records", None
    if is_verified_no_results_html(html_text):
        return "empty", "verified_no_results"
    return "bad", classify_unverified_empty_html(html_text)


def fetch_search_page(session: requests.Session, year: int, parent: int, page: int, court_id: str | None = None) -> tuple[list[dict[str, Any]], str]:
    response = request_with_retries(session, "GET", search_results_url(page, year, parent, court_id))
    return parse_result_blocks(response.text), response.text


def index_shard(year: int, parent: int, court_id: str | None = None) -> None:
    conn = connect_db()
    ensure_shard(conn, year, parent, court_id)
    shard = load_shard(conn, year, parent, court_id)
    if shard and (bool(shard["completed"]) or bool(shard["needs_review"])):
        return
    page = int(shard["last_indexed_page"] if shard else 0) + 1
    session = login()
    bad_page_attempts = 0

    while config.YEAR_COMPLETION_PAGE_CAP <= 0 or page <= config.YEAR_COMPLETION_PAGE_CAP:
        try:
            sleep_jitter()
            records, html_text = fetch_search_page(session, year, parent, page, court_id)
        except RateLimitedError as exc:
            print(f"[index-pause] year={year} parent={parent} page={page} rate_limited={exc.status_code}", flush=True)
            time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            session = login()
            continue
        except requests.RequestException as exc:
            print(f"[index-pause] year={year} parent={parent} page={page} network={exc}", flush=True)
            time.sleep(config.NETWORK_PAUSE_SECONDS)
            session = login()
            continue

        status, reason = classify_search_html(html_text, records)
        if status == "bad":
            bad_page_attempts += 1
            if bad_page_attempts >= config.UNVERIFIED_EMPTY_PAGE_MAX_RETRIES:
                raise UnverifiedEmptyPageError(reason or "bad_search_page", f"Bad search page year={year} parent={parent} page={page} reason={reason}")
            sleep_for = config.UNVERIFIED_EMPTY_PAGE_PAUSE_SECONDS * bad_page_attempts
            print(
                f"[index-bad-page] year={year} parent={parent} court={court_id or '-'} page={page} "
                f"reason={reason} attempt={bad_page_attempts}/{config.UNVERIFIED_EMPTY_PAGE_MAX_RETRIES} sleep={sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
            if config.RELOGIN_ON_UNVERIFIED_EMPTY:
                session = login()
            continue

        bad_page_attempts = 0
        if status == "empty":
            shard = load_shard(conn, year, parent, court_id)
            found = int(shard["records_found"] if shard else 0)
            if page > 1 and found >= 19000:
                complete_shard(conn, year, parent, "ambiguous_no_records_after_large_window", True, court_id)
                print(f"[index-review] year={year} parent={parent} page={page} found={found}", flush=True)
                return
            complete_shard(conn, year, parent, "no_records", False, court_id)
            print(f"[index-complete] year={year} parent={parent} court={court_id or '-'} reason=no_records page={page}", flush=True)
            return

        queued = 0
        for record in records:
            if enqueue_detail(conn, record, year, parent, page, court_id):
                queued += 1
        update_shard_progress(conn, year, parent, page, len(records), queued, court_id)
        print(
            f"[index-page] year={year} parent={parent} court={court_id or '-'} page={page} "
            f"found={len(records)} queued={queued}",
            flush=True,
        )
        page += 1
        if config.PAGE_DELAY_SECONDS > 0:
            time.sleep(config.PAGE_DELAY_SECONDS)

    complete_shard(conn, year, parent, "page_cap", False, court_id)
    print(f"[index-complete] year={year} parent={parent} court={court_id or '-'} reason=page_cap", flush=True)


def claim_detail(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with DB_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM details
            WHERE status IN ('pending', 'retry') AND attempts < ?
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            (config.DETAIL_MAX_ATTEMPTS,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE details SET status='processing', attempts=attempts+1, updated_at=?
            WHERE serial=? AND entity_type=?
            """,
            (utc_now(), row["serial"], row["entity_type"]),
        )
        conn.commit()
    return row


def mark_detail(conn: sqlite3.Connection, row: sqlite3.Row, status: str, error: str | None = None) -> None:
    with DB_LOCK:
        conn.execute(
            "UPDATE details SET status=?, last_error=?, updated_at=? WHERE serial=? AND entity_type=?",
            (status, error, utc_now(), row["serial"], row["entity_type"]),
        )
        conn.commit()


def append_output(record: dict[str, Any]) -> None:
    key = record_key(record)
    with OUTPUT_LOCK:
        with config.OUTPUT_FILE.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    if all(key):
        with KEYS_LOCK:
            with config.KEYS_FILE.open("a", encoding="utf-8") as keys:
                keys.write(f"{key[0]}\t{key[1]}\n")


def mark_seen(conn: sqlite3.Connection, serial: str, entity_type: str) -> None:
    with DB_LOCK:
        conn.execute(
            "INSERT OR IGNORE INTO seen(serial, entity_type, source, created_at) VALUES (?, ?, 'pipeline', ?)",
            (serial, entity_type, utc_now()),
        )
        conn.commit()


def is_valid_detail_html(html_text: str) -> bool:
    if "page-word" in html_text:
        return True
    return any(marker in html_text for marker in ("dec-text-tab", "dec-princ-text-tab", "dec-reasons-tab", "dec-file-tab"))


def detail_worker(worker_id: int, index_done: threading.Event) -> None:
    conn = connect_db()
    session: requests.Session | None = None
    first_login = True
    idle_rounds = 0
    while True:
        row = claim_detail(conn)
        if row is None:
            pending = pending_detail_count(conn)
            if index_done.is_set() and pending == 0:
                print(f"[detail-worker] id={worker_id} done", flush=True)
                return
            idle_rounds += 1
            time.sleep(min(config.DETAIL_IDLE_SLEEP_SECONDS * idle_rounds, 30))
            continue
        idle_rounds = 0
        try:
            if session is None:
                if first_login and config.DETAIL_WORKER_LOGIN_STAGGER_SECONDS > 0:
                    time.sleep(config.DETAIL_WORKER_LOGIN_STAGGER_SECONDS * (worker_id - 1))
                    first_login = False
                session = login()
            sleep_jitter()
            response = request_with_retries(session, "GET", row["url"])
            if not is_valid_detail_html(response.text):
                reason = classify_unverified_empty_html(response.text)
                raise UnverifiedEmptyPageError(reason, f"Bad detail page reason={reason}")
            record = {
                "serial": row["serial"],
                "entity_type": row["entity_type"],
                "title": row["title"],
                "date": row["date"],
                "url": row["url"],
                "_year": row["year"],
                "_parent": row["parent"],
                "_court_id": row["court_id"] or None,
                "_page": row["source_page"],
                **extract_judgment_sections(response.text),
            }
            append_output(record)
            mark_seen(conn, row["serial"], row["entity_type"])
            mark_detail(conn, row, "saved")
            print(f"[detail-saved] worker={worker_id} serial={row['serial']} entity={row['entity_type']}", flush=True)
        except RateLimitedError as exc:
            print(f"[detail-pause] worker={worker_id} rate_limited={exc.status_code}", flush=True)
            mark_detail(conn, row, "retry", f"rate_limited_{exc.status_code}")
            time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            session = None
        except (requests.RequestException, UnverifiedEmptyPageError) as exc:
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= config.DETAIL_MAX_ATTEMPTS else "retry"
            mark_detail(conn, row, status, str(exc))
            sleep_for = config.DETAIL_RETRY_BASE_SECONDS * attempts
            print(f"[detail-retry] worker={worker_id} serial={row['serial']} status={status} error={exc} sleep={sleep_for:.1f}s", flush=True)
            time.sleep(sleep_for)
            session = None
        except Exception as exc:  # noqa: BLE001
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= config.DETAIL_MAX_ATTEMPTS else "retry"
            mark_detail(conn, row, status, str(exc))
            print(f"[detail-error] worker={worker_id} serial={row['serial']} status={status} error={exc}", flush=True)


def pending_detail_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM details WHERE status IN ('pending', 'retry', 'processing')").fetchone()
    return int(row["count"] if row else 0)


def detail_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM details GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def build_shards() -> list[tuple[int, int, str | None]]:
    courts = configured_court_shards()
    if courts:
        return [(year, config.PARENT, court) for year in range(config.START_YEAR, config.END_YEAR - 1, -1) for court in courts]
    return [(year, parent, None) for year in range(config.START_YEAR, config.END_YEAR - 1, -1) for parent in config.PARENT_SHARDS]


def run_pipeline() -> int:
    ensure_dirs()
    conn = connect_db()
    bootstrap_seen_keys(conn)
    index_done = threading.Event()
    shards = build_shards()
    print(
        f"[pipeline] start years={config.START_YEAR}..{config.END_YEAR} shards={len(shards)} "
        f"index_workers={config.INDEX_WORKERS} detail_workers={config.DETAIL_WORKERS} "
        f"global_request_limit={config.GLOBAL_REQUEST_LIMIT}",
        flush=True,
    )
    detail_threads = [
        threading.Thread(target=detail_worker, args=(worker_id, index_done), daemon=False)
        for worker_id in range(1, max(1, config.DETAIL_WORKERS) + 1)
    ]
    for thread in detail_threads:
        thread.start()

    try:
        with ThreadPoolExecutor(max_workers=max(1, config.INDEX_WORKERS)) as executor:
            future_map = {executor.submit(index_shard, year, parent, court_id): (year, parent, court_id) for year, parent, court_id in shards}
            for future in as_completed(future_map):
                year, parent, court_id = future_map[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[pipeline-index-error] year={year} parent={parent} court={court_id or '-'} error={exc}", flush=True)
    finally:
        index_done.set()

    for thread in detail_threads:
        thread.join()
    counts = detail_status_counts(conn)
    print(f"[pipeline] complete detail_status={counts}", flush=True)
    return 0
