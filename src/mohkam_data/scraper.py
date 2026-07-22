from __future__ import annotations

import html
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import psutil
import requests

from . import config


OUTPUT_LOCK = threading.Lock()
KEYS_LOCK = threading.Lock()
FAILED_DETAILS_LOCK = threading.Lock()
REQUEST_SEMAPHORE = threading.BoundedSemaphore(config.GLOBAL_REQUEST_LIMIT)


class RateLimitedError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class UnverifiedEmptyPageError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def ensure_dirs() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)


def adaptive_concurrency() -> int:
    calculated = max(config.MIN_DETAIL_CONCURRENCY, config.DETAIL_CONCURRENCY_PER_YEAR)
    return min(config.MAX_DETAIL_CONCURRENCY, calculated)


def sleep_jitter() -> None:
    if config.MAX_REQUEST_DELAY_SECONDS <= 0:
        return
    time.sleep(random.uniform(config.MIN_REQUEST_DELAY_SECONDS, config.MAX_REQUEST_DELAY_SECONDS))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def login() -> requests.Session:
    if not config.USERNAME or not config.PASSWORD:
        raise RuntimeError("Set QISTAS_USERNAME and QISTAS_PASSWORD.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Referer": config.LOGIN_URL,
            "Origin": config.BASE_URL,
        }
    )

    login_page = request_with_retries(session, "GET", config.LOGIN_URL, timeout=30)
    token_match = re.search(
        r'<input[^>]+name="_token"[^>]+value="([^"]+)"',
        login_page.text,
        re.IGNORECASE,
    )
    if not token_match:
        raise RuntimeError("Could not find CSRF token on login page.")

    response = request_with_retries(
        session,
        "POST",
        config.AUTH_URL,
        data={
            "_token": token_match.group(1),
            "returnURL": "",
            "username": config.USERNAME,
            "userpassword": config.PASSWORD,
            "remember-me": "1",
        },
        timeout=30,
        allow_redirects=True,
    )
    if "login" in response.url.lower():
        raise RuntimeError(f"Login failed; landed on {response.url}")
    return session


def request_with_retries(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    last_error: Exception | None = None
    timeout = kwargs.pop("timeout", (config.CONNECT_TIMEOUT_SECONDS, config.READ_TIMEOUT_SECONDS))
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            with REQUEST_SEMAPHORE:
                response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in (403, 429):
                raise RateLimitedError(response.status_code)
            response.raise_for_status()
            return response
        except RateLimitedError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= config.MAX_RETRIES:
                break
            sleep_for = config.BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"[retry] {method} {url} attempt={attempt}/{config.MAX_RETRIES} error={exc} sleep={sleep_for:.1f}s", file=sys.stderr)
            time.sleep(sleep_for)
    assert last_error is not None
    raise last_error


def search_results_url(page_number: int, year: int, parent: int = config.PARENT, court_id: str | None = None) -> str:
    params = {
        "main-search-word": "",
        "c": str(config.COUNTRY),
        "pc": str(parent),
        "search-type": "1",
        "page-number": str(page_number),
        "from-mainsearch": "-1",
        "from-filter": "0",
        "scmode": "2",
        "vmode": "1",
        "slang": "1",
        "db": str(config.DB),
        "geo": "-1",
        "yearFrom": str(year),
        "yearTo": str(year),
        "cdate": "0",
    }
    if court_id and court_id != "-1":
        params["advCId"] = court_id
    return f"{config.SEARCH_RESULTS_URL}?{urlencode(params)}"


def parse_result_blocks(html_text: str) -> list[dict[str, Any]]:
    blocks = re.split(r'<div class="ResultsItem dec-item">', html_text, flags=re.IGNORECASE)[1:]
    records: list[dict[str, Any]] = []
    for block in blocks:
        href_match = re.search(r'<a[^>]+href="(/ar/decs/info/[^"]+)"[^>]*>\s*(.*?)\s*</a>', block, re.IGNORECASE | re.DOTALL)
        if not href_match:
            continue
        serial_match = re.search(r"/ar/decs/info/(\d+)/(\d+)", href_match.group(1))
        date_match = re.search(r'dir:rtl;font-size: 13px;">\s*([^<]+)\s*<', block, re.IGNORECASE)
        title = normalize_text(re.sub(r"<[^>]+>", " ", href_match.group(2)))
        records.append(
            {
                "serial": serial_match.group(1) if serial_match else None,
                "entity_type": serial_match.group(2) if serial_match else None,
                "title": title,
                "date": date_match.group(1).strip() if date_match else None,
                "url": urljoin(config.BASE_URL, href_match.group(1)),
            }
        )
    return records


def extract_tab_section(html_text: str, section_id: str) -> str:
    match = re.search(rf'<div[^>]+id="{re.escape(section_id)}"[^>]*>(.*?)</div>\s*</div>', html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    section_html = match.group(1)
    words = re.findall(r'<span class="page-word"[^>]*>(.*?)</span>', section_html, re.IGNORECASE | re.DOTALL)
    if words:
        return normalize_text(" ".join(re.sub(r"<[^>]+>", "", word) for word in words))
    section_html = re.sub(r"<script.*?</script>", " ", section_html, flags=re.IGNORECASE | re.DOTALL)
    section_html = re.sub(r"<style.*?</style>", " ", section_html, flags=re.IGNORECASE | re.DOTALL)
    return normalize_text(re.sub(r"<[^>]+>", " ", section_html))


def extract_detail_text(html_text: str) -> str:
    words = re.findall(r'<span class="page-word"[^>]*>(.*?)</span>', html_text, re.IGNORECASE | re.DOTALL)
    if words:
        return normalize_text(" ".join(re.sub(r"<[^>]+>", "", word) for word in words))
    return normalize_text(re.sub(r"<[^>]+>", " ", html_text))


def extract_judgment_sections(html_text: str) -> dict[str, str]:
    sections = {
        "decision_text": extract_tab_section(html_text, "dec-text-tab"),
        "principle": extract_tab_section(html_text, "dec-princ-text-tab"),
        "violation_decision": extract_tab_section(html_text, "dec-opp-tab"),
        "appeal_reasons": extract_tab_section(html_text, "dec-reasons-tab"),
        "response_to_reasons": extract_tab_section(html_text, "dec-reasonsrep-tab"),
        "procedural_history": extract_tab_section(html_text, "ex-history"),
        "case_file": extract_tab_section(html_text, "dec-file-tab"),
    }
    parts = []
    for key, label in config.JUDGMENT_SECTION_LABELS.items():
        if sections.get(key):
            parts.append(f"{label}\n{sections[key]}")
    sections["content"] = "\n\n".join(parts) or extract_detail_text(html_text)
    return sections


def record_key(record: dict[str, Any]) -> tuple[str, str]:
    return (str(record.get("serial") or ""), str(record.get("entity_type") or ""))


def load_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    with KEYS_LOCK:
        if config.KEYS_FILE.exists():
            with config.KEYS_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    parts = line.strip().split("\t", 1)
                    if len(parts) == 2 and all(parts):
                        keys.add((parts[0], parts[1]))
    return keys


def append_key(key: tuple[str, str]) -> None:
    if not all(key):
        return
    with KEYS_LOCK:
        with config.KEYS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{key[0]}\t{key[1]}\n")


def shard_suffix(year: int, parent: int = config.PARENT, court_id: str | None = None) -> str:
    if court_id and court_id != "-1":
        safe_court_id = re.sub(r"[^0-9A-Za-z_-]+", "_", court_id)
        return f"{year}_court_{safe_court_id}"
    if parent != config.PARENT:
        return f"{year}_parent_{parent}"
    return f"{year}"


def state_path(year: int, parent: int = config.PARENT, court_id: str | None = None) -> Path:
    suffix = shard_suffix(year, parent, court_id)
    return config.STATE_DIR / f"qistas_state_{suffix}.json"


def load_state(year: int, parent: int = config.PARENT, court_id: str | None = None) -> dict[str, Any]:
    path = state_path(year, parent, court_id)
    if not path.exists():
        return {"last_completed_page": 0, "written_records": 0, "skipped_duplicates": 0, "completed": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_completed_page": 0, "written_records": 0, "skipped_duplicates": 0, "completed": False}
    if (
        state.get("completed") is True
        and state.get("completion_reason") == "page_cap"
        and config.YEAR_COMPLETION_PAGE_CAP <= 0
    ):
        state["completed"] = False
        state["completion_reason"] = "page_cap_reopened"
    if (
        state.get("completed") is True
        and state.get("completion_reason") in {"no_records", "no_records_after_large_window"}
        and int(state.get("last_checked_page", 0)) > 1
        and int(state.get("written_records", 0)) >= 19000
    ):
        state["completed"] = False
        state["needs_review"] = True
        state["completion_reason"] = "ambiguous_no_records_reopened"
    return state


def save_state(year: int, parent: int, court_id: str | None, state: dict[str, Any]) -> None:
    path = state_path(year, parent, court_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_failed_detail(year: int, page: int, record: dict[str, Any], error: Exception) -> None:
    payload = {
        "year": year,
        "page": page,
        "serial": record.get("serial"),
        "entity_type": record.get("entity_type"),
        "url": record.get("url"),
        "error_type": error.__class__.__name__,
        "error": str(error),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with FAILED_DETAILS_LOCK:
        with config.FAILED_DETAILS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def clone_session(session: requests.Session) -> requests.Session:
    child = requests.Session()
    child.headers.update(session.headers)
    child.cookies.update(session.cookies)
    return child


def fetch_detail(session: requests.Session, record: dict[str, Any], rank: int) -> dict[str, Any] | None:
    sleep_jitter()
    try:
        response = request_with_retries(clone_session(session), "GET", record["url"])
        return {**record, "rank": rank, **extract_judgment_sections(response.text)}
    except RateLimitedError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {**record, "rank": rank, "content": "", "detail_error": str(exc)}


def fetch_page_records(session: requests.Session, page: int, year: int, parent: int, court_id: str | None = None) -> list[dict[str, Any]]:
    response = request_with_retries(session, "GET", search_results_url(page, year, parent, court_id))
    return parse_result_blocks(response.text)


def is_verified_no_results_html(html_text: str) -> bool:
    normalized = normalize_text(re.sub(r"<[^>]+>", " ", html_text))
    return "لا توجد نتائج لعوامل البحث المختارة" in normalized


def classify_unverified_empty_html(html_text: str) -> str:
    normalized = normalize_text(re.sub(r"<[^>]+>", " ", html_text)).lower()
    if "your browser does not support the video tag" in normalized or "browser does not support the video tag" in normalized:
        return "layout_shell_video"
    if "userpassword" in html_text or "name=\"username\"" in html_text or "/ar/authinticate" in html_text:
        return "login_page"
    if "الرجاء الانتظار" in normalized or "please wait" in normalized:
        return "wait_page"
    return "unrecognized_empty_page"


def raise_unverified_empty_page(year: int, page: int, parent: int, court_id: str | None, html_text: str, retry: bool = False) -> None:
    reason = classify_unverified_empty_html(html_text)
    phase = "retry" if retry else "initial"
    raise UnverifiedEmptyPageError(
        reason,
        f"Unverified empty {phase} page reason={reason} year={year} parent={parent} court={court_id or '-'} page={page}",
    )


def fetch_page(session: requests.Session, page: int, year: int, parent: int, court_id: str | None = None) -> tuple[list[dict[str, Any]], str]:
    response = request_with_retries(session, "GET", search_results_url(page, year, parent, court_id))
    return parse_result_blocks(response.text), response.text


def confirm_empty_page(
    session: requests.Session,
    page: int,
    year: int,
    parent: int,
    court_id: str | None,
    html_text: str,
) -> list[dict[str, Any]]:
    if not is_verified_no_results_html(html_text):
        raise_unverified_empty_page(year, page, parent, court_id, html_text)

    for attempt in range(1, config.EMPTY_PAGE_CONFIRMATIONS + 1):
        if config.EMPTY_PAGE_RETRY_SECONDS > 0:
            time.sleep(config.EMPTY_PAGE_RETRY_SECONDS)
        records, retry_html = fetch_page(session, page, year, parent, court_id)
        print(
            f"[empty-check] year={year} parent={parent} court={court_id or '-'} page={page} "
            f"attempt={attempt}/{config.EMPTY_PAGE_CONFIRMATIONS} records={len(records)}",
            flush=True,
        )
        if records:
            return records
        if not is_verified_no_results_html(retry_html):
            raise_unverified_empty_page(year, page, parent, court_id, retry_html, retry=True)
    return []


def discover_court_shards(session: requests.Session) -> tuple[str, ...]:
    response = request_with_retries(session, "GET", f"{config.BASE_URL}/ar/search?c={config.COUNTRY}&pc={config.PARENT}&db={config.DB}")
    select_match = re.search(
        r'<select[^>]+id="courtNamesQuickSearch"[^>]*>(.*?)</select>',
        response.text,
        re.IGNORECASE | re.DOTALL,
    )
    if not select_match:
        raise RuntimeError("Could not discover court shards from courtNamesQuickSearch.")

    court_ids: list[str] = []
    seen: set[str] = set()
    for value in re.findall(r'<option[^>]+value="([^"]+)"', select_match.group(1), re.IGNORECASE):
        if value == "-1" or value in seen:
            continue
        seen.add(value)
        court_ids.append(value)

    if config.MAX_COURT_SHARDS > 0:
        court_ids = court_ids[: config.MAX_COURT_SHARDS]
    if not court_ids:
        raise RuntimeError("Court shard discovery returned no courts.")
    return tuple(court_ids)


def configured_court_shards() -> tuple[str, ...]:
    if config.COURT_SHARDS_AUTO:
        session = login()
        return discover_court_shards(session)
    return config.COURT_SHARDS


def scrape_year(year: int, parent: int = config.PARENT, court_id: str | None = None) -> None:
    ensure_dirs()
    state = load_state(year, parent, court_id)
    if state.get("completed"):
        return

    session = login()
    existing_keys = load_keys()
    print(f"[dedupe] loaded {len(existing_keys)} keys", flush=True)
    page = int(state.get("last_completed_page", 0)) + 1
    unverified_empty_retries = 0

    while config.YEAR_COMPLETION_PAGE_CAP <= 0 or page <= config.YEAR_COMPLETION_PAGE_CAP:
        try:
            page_records, page_html = fetch_page(session, page, year, parent, court_id)
        except RateLimitedError as exc:
            print(f"[pause] year={year} parent={parent} court={court_id or '-'} page={page} rate_limited={exc.status_code}", flush=True)
            time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            continue
        except requests.RequestException as exc:
            print(f"[pause] year={year} parent={parent} court={court_id or '-'} page={page} network={exc}", flush=True)
            time.sleep(config.NETWORK_PAUSE_SECONDS)
            continue

        if not page_records:
            try:
                page_records = confirm_empty_page(session, page, year, parent, court_id, page_html)
            except UnverifiedEmptyPageError as exc:
                unverified_empty_retries += 1
                if unverified_empty_retries >= config.UNVERIFIED_EMPTY_PAGE_MAX_RETRIES:
                    raise RuntimeError(
                        f"Too many unverified empty pages year={year} parent={parent} "
                        f"court={court_id or '-'} page={page} reason={exc.reason} retries={unverified_empty_retries}"
                    ) from exc
                sleep_for = config.UNVERIFIED_EMPTY_PAGE_PAUSE_SECONDS * unverified_empty_retries
                print(
                    f"[bad-page] year={year} parent={parent} court={court_id or '-'} page={page} "
                    f"attempt={unverified_empty_retries}/{config.UNVERIFIED_EMPTY_PAGE_MAX_RETRIES} "
                    f"reason={exc.reason} sleep={sleep_for:.1f}s relogin={config.RELOGIN_ON_UNVERIFIED_EMPTY}",
                    flush=True,
                )
                if sleep_for > 0:
                    time.sleep(sleep_for)
                if config.RELOGIN_ON_UNVERIFIED_EMPTY:
                    session = login()
                continue

        if not page_records:
            completion_reason = "no_records"
            if page > 1 and int(state.get("written_records", 0)) >= 19000:
                completion_reason = "ambiguous_no_records_after_large_window"
                state.update(
                    {
                        "completed": False,
                        "needs_review": True,
                        "completion_reason": completion_reason,
                        "last_checked_page": page,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    }
                )
                save_state(year, parent, court_id, state)
                raise RuntimeError(
                    f"Ambiguous empty page after saturated window year={year} parent={parent} "
                    f"court={court_id or '-'} page={page}; add finer shards before marking complete."
                )
            state.update({"completed": True, "completion_reason": completion_reason, "last_checked_page": page})
            save_state(year, parent, court_id, state)
            print(f"[complete] year={year} parent={parent} court={court_id or '-'} reason={completion_reason} page={page}", flush=True)
            return

        unverified_empty_retries = 0
        before = len(page_records)
        page_records = [record for record in page_records if record_key(record) not in existing_keys]
        state["skipped_duplicates"] = int(state.get("skipped_duplicates", 0)) + (before - len(page_records))

        enriched: list[dict[str, Any]] = []
        if page_records:
            with ThreadPoolExecutor(max_workers=adaptive_concurrency()) as executor:
                future_map = {
                    executor.submit(fetch_detail, session, record, index + 1): record
                    for index, record in enumerate(page_records)
                }
                for future in as_completed(future_map):
                    record = future_map[future]
                    try:
                        detail = future.result()
                    except RateLimitedError as exc:
                        print(
                            f"[pause] year={year} parent={parent} court={court_id or '-'} page={page} "
                            f"detail_rate_limited={exc.status_code}",
                            flush=True,
                        )
                        time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
                        detail = None
                    except Exception as exc:  # noqa: BLE001
                        append_failed_detail(year, page, record, exc)
                        detail = None
                    if detail:
                        enriched.append(detail)

        written_this_page = 0
        with OUTPUT_LOCK:
            with config.OUTPUT_FILE.open("a", encoding="utf-8") as output:
                for record in enriched:
                    record["_year"] = year
                    record["_parent"] = parent
                    record["_court_id"] = court_id
                    record["_page"] = page
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    key = record_key(record)
                    if all(key):
                        existing_keys.add(key)
                        append_key(key)
                    written_this_page += 1

        state.update(
            {
                "last_completed_page": page,
                "written_records": int(state.get("written_records", 0)) + written_this_page,
                "completed": False,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "filters": {
                    "db": config.DB,
                    "country": config.COUNTRY,
                    "parent": parent,
                    "court_id": court_id,
                    "year_from": year,
                    "year_to": year,
                },
            }
        )
        save_state(year, parent, court_id, state)
        print(
            f"[page] year={year} parent={parent} court={court_id or '-'} page={page} "
            f"written={written_this_page} skipped={before - len(page_records)}",
            flush=True,
        )
        page += 1
        if config.PAGE_DELAY_SECONDS > 0:
            time.sleep(config.PAGE_DELAY_SECONDS)

    state.update({"completed": True, "completion_reason": "page_cap", "last_checked_page": page})
    save_state(year, parent, court_id, state)
    print(f"[complete] year={year} parent={parent} court={court_id or '-'} reason=page_cap", flush=True)
