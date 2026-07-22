from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from . import config
from .scraper import (
    RateLimitedError,
    classify_unverified_empty_html,
    ensure_dirs,
    is_verified_no_results_html,
    login,
    parse_result_blocks,
    request_with_retries,
    search_results_url,
)

RAW_DIR = config.DATA_DIR / "raw_html"
RAW_SEARCH_DIR = RAW_DIR / "search"
RAW_DETAIL_DIR = RAW_DIR / "details"
RAW_STATE_DB = config.STATE_DIR / "raw_html_state.sqlite3"
RAW_FAILED_FILE = config.LOG_DIR / "raw_html_failed.jsonl"

DB_LOCK = threading.Lock()
FAILED_LOCK = threading.Lock()


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


RAW_FETCH_DETAILS = env_bool("MOHKAM_RAW_FETCH_DETAILS", True)
RAW_SEARCH_PAGE_CAP = env_int("MOHKAM_RAW_SEARCH_PAGE_CAP", 0)
RAW_DETAIL_WORKERS = env_int("MOHKAM_RAW_DETAIL_WORKERS", max(8, min(24, config.DETAIL_CONCURRENCY_PER_YEAR)))
RAW_COMPRESSLEVEL = env_int("MOHKAM_RAW_COMPRESSLEVEL", 5)
RAW_REQUEST_MIN_DELAY_SECONDS = env_float("MOHKAM_RAW_REQUEST_MIN_DELAY_SECONDS", config.MIN_REQUEST_DELAY_SECONDS)
RAW_REQUEST_MAX_DELAY_SECONDS = env_float("MOHKAM_RAW_REQUEST_MAX_DELAY_SECONDS", config.MAX_REQUEST_DELAY_SECONDS)
RAW_EMPTY_PAUSE_SECONDS = env_float("MOHKAM_RAW_EMPTY_PAUSE_SECONDS", config.UNVERIFIED_EMPTY_PAGE_PAUSE_SECONDS)
RAW_EMPTY_MAX_RETRIES = env_int("MOHKAM_RAW_EMPTY_MAX_RETRIES", config.UNVERIFIED_EMPTY_PAGE_MAX_RETRIES)
RAW_DETAIL_MAX_RETRIES = env_int("MOHKAM_RAW_DETAIL_MAX_RETRIES", 3)


def ensure_raw_dirs() -> None:
    ensure_dirs()
    RAW_SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DETAIL_DIR.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_raw_dirs()
    conn = sqlite3.connect(RAW_STATE_DB, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shards (
            year INTEGER NOT NULL,
            parent INTEGER NOT NULL,
            court_id TEXT NOT NULL DEFAULT '',
            last_completed_page INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            completion_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (year, parent, court_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_pages (
            year INTEGER NOT NULL,
            parent INTEGER NOT NULL,
            court_id TEXT NOT NULL DEFAULT '',
            page INTEGER NOT NULL,
            status TEXT NOT NULL,
            path TEXT,
            sha256 TEXT,
            bytes INTEGER NOT NULL DEFAULT 0,
            records_count INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (year, parent, court_id, page)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detail_pages (
            serial TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            status TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            date TEXT,
            year INTEGER,
            parent INTEGER,
            court_id TEXT NOT NULL DEFAULT '',
            source_page INTEGER,
            path TEXT,
            sha256 TEXT,
            bytes INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (serial, entity_type)
        )
        """
    )
    conn.commit()
    return conn


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sleep_jitter() -> None:
    if RAW_REQUEST_MAX_DELAY_SECONDS <= 0:
        return
    time.sleep(random.uniform(RAW_REQUEST_MIN_DELAY_SECONDS, RAW_REQUEST_MAX_DELAY_SECONDS))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_gzip_atomic(path: Path, html_text: str) -> tuple[int, str]:
    payload = html_text.encode("utf-8")
    digest = sha256_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    with gzip.open(tmp, "wb", compresslevel=RAW_COMPRESSLEVEL) as handle:
        handle.write(payload)
    tmp.replace(path)
    return len(payload), digest


def search_html_path(year: int, parent: int, page: int, court_id: str | None = None) -> Path:
    court_part = f"court={court_id}" if court_id else "court=none"
    return RAW_SEARCH_DIR / f"year={year}" / f"parent={parent}" / court_part / f"page={page:07d}.html.gz"


def detail_html_path(serial: str, entity_type: str) -> Path:
    bucket = serial[-3:].rjust(3, "0")
    return RAW_DETAIL_DIR / f"bucket={bucket}" / f"{serial}_{entity_type}.html.gz"


def append_failed(payload: dict[str, Any]) -> None:
    payload = {**payload, "updated_at": utc_now()}
    with FAILED_LOCK:
        with RAW_FAILED_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def get_shard_state(conn: sqlite3.Connection, year: int, parent: int, court_id: str | None = None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT last_completed_page, completed, completion_reason FROM shards WHERE year=? AND parent=? AND court_id=?",
        (year, parent, court_id or ""),
    ).fetchone()
    if not row:
        return {"last_completed_page": 0, "completed": False, "completion_reason": None}
    return {"last_completed_page": int(row[0]), "completed": bool(row[1]), "completion_reason": row[2]}


def update_shard(conn: sqlite3.Connection, year: int, parent: int, page: int, completed: bool, reason: str | None, court_id: str | None = None) -> None:
    with DB_LOCK:
        conn.execute(
            """
            INSERT INTO shards (year, parent, court_id, last_completed_page, completed, completion_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, parent, court_id) DO UPDATE SET
                last_completed_page=excluded.last_completed_page,
                completed=excluded.completed,
                completion_reason=excluded.completion_reason,
                updated_at=excluded.updated_at
            """,
            (year, parent, court_id or "", page, int(completed), reason, utc_now()),
        )
        conn.commit()


def record_search_page(
    conn: sqlite3.Connection,
    year: int,
    parent: int,
    page: int,
    status: str,
    path: Path | None,
    digest: str | None,
    bytes_count: int,
    records_count: int,
    reason: str | None,
    court_id: str | None = None,
) -> None:
    with DB_LOCK:
        conn.execute(
            """
            INSERT INTO search_pages
                (year, parent, court_id, page, status, path, sha256, bytes, records_count, reason, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, parent, court_id, page) DO UPDATE SET
                status=excluded.status,
                path=excluded.path,
                sha256=excluded.sha256,
                bytes=excluded.bytes,
                records_count=excluded.records_count,
                reason=excluded.reason,
                fetched_at=excluded.fetched_at
            """,
            (
                year,
                parent,
                court_id or "",
                page,
                status,
                str(path) if path else None,
                digest,
                bytes_count,
                records_count,
                reason,
                utc_now(),
            ),
        )
        conn.commit()


def upsert_detail_pending(conn: sqlite3.Connection, record: dict[str, Any], year: int, parent: int, page: int, court_id: str | None = None) -> None:
    serial = str(record.get("serial") or "")
    entity_type = str(record.get("entity_type") or "")
    if not serial or not entity_type:
        return
    with DB_LOCK:
        conn.execute(
            """
            INSERT INTO detail_pages
                (serial, entity_type, status, url, title, date, year, parent, court_id, source_page, fetched_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial, entity_type) DO NOTHING
            """,
            (
                serial,
                entity_type,
                record.get("url") or "",
                record.get("title") or "",
                record.get("date") or "",
                year,
                parent,
                court_id or "",
                page,
                utc_now(),
            ),
        )
        conn.commit()


def mark_detail_saved(
    conn: sqlite3.Connection,
    serial: str,
    entity_type: str,
    status: str,
    path: Path | None,
    digest: str | None,
    bytes_count: int,
    reason: str | None,
) -> None:
    with DB_LOCK:
        conn.execute(
            """
            UPDATE detail_pages SET
                status=?, path=?, sha256=?, bytes=?, reason=?, attempts=attempts+1, fetched_at=?
            WHERE serial=? AND entity_type=?
            """,
            (status, str(path) if path else None, digest, bytes_count, reason, utc_now(), serial, entity_type),
        )
        conn.commit()


def classify_search_html(html_text: str, records: list[dict[str, Any]]) -> tuple[str, str | None]:
    if records:
        return "saved", None
    if is_verified_no_results_html(html_text):
        return "empty", "verified_no_results"
    return "bad", classify_unverified_empty_html(html_text)


def is_valid_detail_html(html_text: str) -> bool:
    if "page-word" in html_text:
        return True
    markers = ("dec-text-tab", "dec-princ-text-tab", "dec-reasons-tab", "dec-file-tab")
    return any(marker in html_text for marker in markers)


def fetch_and_save_detail(conn: sqlite3.Connection, base_session: requests.Session, row: sqlite3.Row | tuple[Any, ...]) -> str:
    serial, entity_type, url = str(row[0]), str(row[1]), str(row[2])
    session = requests.Session()
    session.headers.update(base_session.headers)
    session.cookies.update(base_session.cookies)
    for attempt in range(1, RAW_DETAIL_MAX_RETRIES + 1):
        try:
            sleep_jitter()
            response = request_with_retries(session, "GET", url)
            html_text = response.text
            if not is_valid_detail_html(html_text):
                reason = classify_unverified_empty_html(html_text)
                if attempt >= RAW_DETAIL_MAX_RETRIES:
                    mark_detail_saved(conn, serial, entity_type, "bad", None, None, 0, reason)
                    append_failed({"kind": "detail", "serial": serial, "entity_type": entity_type, "url": url, "reason": reason})
                    return "bad"
                time.sleep(RAW_EMPTY_PAUSE_SECONDS * attempt)
                session = login()
                continue
            path = detail_html_path(serial, entity_type)
            bytes_count, digest = write_gzip_atomic(path, html_text)
            mark_detail_saved(conn, serial, entity_type, "saved", path, digest, bytes_count, None)
            return "saved"
        except RateLimitedError as exc:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt >= RAW_DETAIL_MAX_RETRIES:
                mark_detail_saved(conn, serial, entity_type, "failed", None, None, 0, exc.__class__.__name__)
                append_failed({"kind": "detail", "serial": serial, "entity_type": entity_type, "url": url, "error": str(exc)})
                return "failed"
            time.sleep(config.BACKOFF_SECONDS * attempt)
    return "failed"


def fetch_pending_details(conn: sqlite3.Connection, session: requests.Session, limit: int) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT serial, entity_type, url FROM detail_pages
        WHERE status IN ('pending', 'failed', 'bad') AND attempts < ?
        ORDER BY fetched_at ASC
        LIMIT ?
        """,
        (RAW_DETAIL_MAX_RETRIES, limit),
    ).fetchall()
    if not rows:
        return 0, 0

    saved = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, RAW_DETAIL_WORKERS)) as executor:
        futures = [executor.submit(fetch_and_save_detail, conn, session, row) for row in rows]
        for future in as_completed(futures):
            try:
                status = future.result()
                if status == "saved":
                    saved += 1
                else:
                    failed += 1
            except RateLimitedError as exc:
                failed += 1
                print(f"[raw-detail-pause] rate_limited={exc.status_code} sleep={config.RATE_LIMIT_PAUSE_SECONDS}s", flush=True)
                time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                append_failed({"kind": "detail_worker", "error": str(exc)})
    return saved, failed


def scrape_raw_shard(year: int, parent: int, court_id: str | None = None) -> None:
    conn = connect_db()
    state = get_shard_state(conn, year, parent, court_id)
    if state["completed"]:
        return

    session = login()
    page = int(state["last_completed_page"]) + 1
    empty_retries = 0

    while RAW_SEARCH_PAGE_CAP <= 0 or page <= RAW_SEARCH_PAGE_CAP:
        try:
            sleep_jitter()
            response = request_with_retries(session, "GET", search_results_url(page, year, parent, court_id))
            html_text = response.text
            records = parse_result_blocks(html_text)
            status, reason = classify_search_html(html_text, records)
            if status == "bad":
                empty_retries += 1
                if empty_retries >= RAW_EMPTY_MAX_RETRIES:
                    record_search_page(conn, year, parent, page, "bad", None, None, 0, 0, reason, court_id)
                    raise RuntimeError(f"Bad search page reason={reason} year={year} parent={parent} page={page}")
                print(
                    f"[raw-bad-page] year={year} parent={parent} court={court_id or '-'} page={page} "
                    f"reason={reason} attempt={empty_retries}/{RAW_EMPTY_MAX_RETRIES}",
                    flush=True,
                )
                time.sleep(RAW_EMPTY_PAUSE_SECONDS * empty_retries)
                session = login()
                continue

            path = search_html_path(year, parent, page, court_id)
            bytes_count, digest = write_gzip_atomic(path, html_text)
            record_search_page(conn, year, parent, page, status, path, digest, bytes_count, len(records), reason, court_id)
            empty_retries = 0

            if status == "empty":
                update_shard(conn, year, parent, page - 1, True, "no_records", court_id)
                print(f"[raw-complete] year={year} parent={parent} court={court_id or '-'} reason=no_records page={page}", flush=True)
                return

            for record in records:
                upsert_detail_pending(conn, record, year, parent, page, court_id)

            detail_saved = 0
            detail_failed = 0
            if RAW_FETCH_DETAILS:
                detail_saved, detail_failed = fetch_pending_details(conn, session, max(len(records), RAW_DETAIL_WORKERS))

            update_shard(conn, year, parent, page, False, None, court_id)
            print(
                f"[raw-page] year={year} parent={parent} court={court_id or '-'} page={page} "
                f"search_records={len(records)} detail_done={detail_saved} detail_failed={detail_failed}",
                flush=True,
            )
            page += 1
            if config.PAGE_DELAY_SECONDS > 0:
                time.sleep(config.PAGE_DELAY_SECONDS)
        except RateLimitedError as exc:
            print(f"[raw-pause] year={year} parent={parent} page={page} rate_limited={exc.status_code}", flush=True)
            time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            session = login()
        except requests.RequestException as exc:
            print(f"[raw-pause] year={year} parent={parent} page={page} network={exc}", flush=True)
            time.sleep(config.NETWORK_PAUSE_SECONDS)
            session = login()

    update_shard(conn, year, parent, page - 1, True, "page_cap", court_id)
    print(f"[raw-complete] year={year} parent={parent} court={court_id or '-'} reason=page_cap", flush=True)


def pending_shards() -> list[tuple[int, int, str | None]]:
    conn = connect_db()
    shards: list[tuple[int, int, str | None]] = []
    for year in range(config.START_YEAR, config.END_YEAR - 1, -1):
        for parent in config.PARENT_SHARDS:
            state = get_shard_state(conn, year, parent)
            if not state["completed"]:
                shards.append((year, parent, None))
    return shards


def main() -> int:
    ensure_raw_dirs()
    shards = pending_shards()
    print(
        f"[raw-supervisor] start years={config.START_YEAR}..{config.END_YEAR} "
        f"parents={','.join(str(parent) for parent in config.PARENT_SHARDS)} "
        f"year_workers={config.YEAR_WORKERS} detail_workers={RAW_DETAIL_WORKERS} "
        f"fetch_details={RAW_FETCH_DETAILS} pending_shards={len(shards)}",
        flush=True,
    )
    if not shards:
        print("[raw-supervisor] all shards complete", flush=True)
        return 0

    with ThreadPoolExecutor(max_workers=max(1, config.YEAR_WORKERS)) as executor:
        future_map = {executor.submit(scrape_raw_shard, year, parent, court_id): (year, parent, court_id) for year, parent, court_id in shards}
        for future in as_completed(future_map):
            year, parent, court_id = future_map[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[raw-supervisor] year={year} parent={parent} court={court_id or '-'} failed={exc}", flush=True)
                return 1
    print("[raw-supervisor] all shards complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
