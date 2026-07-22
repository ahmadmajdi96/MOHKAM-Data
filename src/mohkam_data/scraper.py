from __future__ import annotations

import html
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import psutil
import requests

from . import config


class RateLimitedError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def ensure_dirs() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)


def adaptive_concurrency() -> int:
    cpu_count = psutil.cpu_count(logical=True) or 4
    calculated = max(config.MIN_DETAIL_CONCURRENCY, int(cpu_count * config.TARGET_HOST_UTILIZATION * 8))
    return min(config.MAX_DETAIL_CONCURRENCY, calculated)


def sleep_jitter() -> None:
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

    login_page = session.get(config.LOGIN_URL, timeout=30)
    login_page.raise_for_status()
    token_match = re.search(
        r'<input[^>]+name="_token"[^>]+value="([^"]+)"',
        login_page.text,
        re.IGNORECASE,
    )
    if not token_match:
        raise RuntimeError("Could not find CSRF token on login page.")

    response = session.post(
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
    response.raise_for_status()
    if "login" in response.url.lower():
        raise RuntimeError(f"Login failed; landed on {response.url}")
    return session


def request_with_retries(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    last_error: Exception | None = None
    timeout = kwargs.pop("timeout", (config.CONNECT_TIMEOUT_SECONDS, config.READ_TIMEOUT_SECONDS))
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
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


def search_results_url(page_number: int, year: int) -> str:
    params = {
        "main-search-word": "",
        "c": str(config.COUNTRY),
        "pc": str(config.PARENT),
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
    with config.KEYS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{key[0]}\t{key[1]}\n")


def state_path(year: int) -> Path:
    return config.STATE_DIR / f"qistas_state_{year}.json"


def load_state(year: int) -> dict[str, Any]:
    path = state_path(year)
    if not path.exists():
        return {"last_completed_page": 0, "written_records": 0, "skipped_duplicates": 0, "completed": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_completed_page": 0, "written_records": 0, "skipped_duplicates": 0, "completed": False}


def save_state(year: int, state: dict[str, Any]) -> None:
    path = state_path(year)
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


def scrape_year(year: int) -> None:
    ensure_dirs()
    state = load_state(year)
    if state.get("completed"):
        return

    session = login()
    existing_keys = load_keys()
    print(f"[dedupe] loaded {len(existing_keys)} keys", flush=True)
    page = int(state.get("last_completed_page", 0)) + 1

    while page <= config.YEAR_COMPLETION_PAGE_CAP:
        try:
            response = request_with_retries(session, "GET", search_results_url(page, year))
            page_records = parse_result_blocks(response.text)
        except RateLimitedError as exc:
            print(f"[pause] year={year} page={page} rate_limited={exc.status_code}", flush=True)
            time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
            continue
        except requests.RequestException as exc:
            print(f"[pause] year={year} page={page} network={exc}", flush=True)
            time.sleep(config.NETWORK_PAUSE_SECONDS)
            continue

        if not page_records:
            state.update({"completed": True, "completion_reason": "no_records", "last_checked_page": page})
            save_state(year, state)
            print(f"[complete] year={year} reason=no_records page={page}", flush=True)
            return

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
                        print(f"[pause] year={year} page={page} detail_rate_limited={exc.status_code}", flush=True)
                        time.sleep(config.RATE_LIMIT_PAUSE_SECONDS)
                        detail = None
                    except Exception as exc:  # noqa: BLE001
                        append_failed_detail(year, page, record, exc)
                        detail = None
                    if detail:
                        enriched.append(detail)

        written_this_page = 0
        with config.OUTPUT_FILE.open("a", encoding="utf-8") as output:
            for record in enriched:
                record["_year"] = year
                record["_page"] = page
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                key = record_key(record)
                if all(key):
                    existing_keys.add(key)
                    append_key(key)
                written_this_page += 1
                sleep_jitter()

        state.update(
            {
                "last_completed_page": page,
                "written_records": int(state.get("written_records", 0)) + written_this_page,
                "completed": False,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "filters": {"db": config.DB, "country": config.COUNTRY, "parent": config.PARENT, "year_from": year, "year_to": year},
            }
        )
        save_state(year, state)
        print(f"[page] year={year} page={page} written={written_this_page} skipped={before - len(page_records)}", flush=True)
        page += 1
        if config.PAGE_DELAY_SECONDS > 0:
            time.sleep(config.PAGE_DELAY_SECONDS)

    state.update({"completed": True, "completion_reason": "page_cap", "last_checked_page": page})
    save_state(year, state)
    print(f"[complete] year={year} reason=page_cap", flush=True)
