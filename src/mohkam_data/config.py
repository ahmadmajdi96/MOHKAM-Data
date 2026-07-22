from __future__ import annotations

import os
from pathlib import Path


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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int_list(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return tuple(int(part.strip()) for part in value.split(",") if part.strip())


def env_str_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def cap_unless_unsafe(value: int, safe_max: int) -> int:
    if ALLOW_UNSAFE_SPEED:
        return value
    return min(value, safe_max)


def auto_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 4


BASE_URL = "https://research.qistas.com"
LOGIN_URL = f"{BASE_URL}/ar/login/"
AUTH_URL = f"{BASE_URL}/ar/authinticate/"
SEARCH_RESULTS_URL = f"{BASE_URL}/ar/search/results"

DATA_DIR = Path("/app/data")
OUTPUT_FILE = DATA_DIR / "qistas_all.jsonl"
KEYS_FILE = DATA_DIR / "qistas_seen_keys.tsv"
FAILED_DETAILS_FILE = DATA_DIR / "qistas_failed_details.jsonl"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"

START_YEAR = env_int("MOHKAM_START_YEAR", 2026)
END_YEAR = env_int("MOHKAM_END_YEAR", 1850)
DB = 2
COUNTRY = 1
PARENT = -1
PARENT_SHARDS = env_int_list("MOHKAM_PARENT_SHARDS", (1, 2, 3, 6))
COURT_SHARDS_RAW = os.environ.get("MOHKAM_COURT_SHARDS", "auto").strip()
COURT_SHARDS_AUTO = COURT_SHARDS_RAW.lower() == "auto"
COURT_SHARDS = (
    ()
    if COURT_SHARDS_AUTO or COURT_SHARDS_RAW.lower() in {"", "0", "false", "none", "off"}
    else env_str_list("MOHKAM_COURT_SHARDS", ())
)
MAX_COURT_SHARDS = env_int("MOHKAM_MAX_COURT_SHARDS", 0)

HOST_CPU_COUNT = auto_cpu_count()
TARGET_HOST_UTILIZATION = env_float("MOHKAM_TARGET_HOST_UTILIZATION", 0.90)
ALLOW_UNSAFE_SPEED = env_bool("MOHKAM_ALLOW_UNSAFE_SPEED", False)
YEAR_WORKERS = cap_unless_unsafe(env_int("MOHKAM_YEAR_WORKERS", max(1, min(3, int(HOST_CPU_COUNT * TARGET_HOST_UTILIZATION / 2)))), 3)
DETAIL_CONCURRENCY_PER_YEAR = cap_unless_unsafe(env_int("MOHKAM_DETAIL_CONCURRENCY_PER_YEAR", max(8, min(24, int(HOST_CPU_COUNT * TARGET_HOST_UTILIZATION * 2)))), 24)
GLOBAL_REQUEST_LIMIT = cap_unless_unsafe(env_int("MOHKAM_GLOBAL_REQUEST_LIMIT", max(12, min(48, int(HOST_CPU_COUNT * TARGET_HOST_UTILIZATION * 4)))), 48)
MAX_DETAIL_CONCURRENCY = cap_unless_unsafe(env_int("MOHKAM_MAX_DETAIL_CONCURRENCY", 128), 128)
MIN_DETAIL_CONCURRENCY = env_int("MOHKAM_MIN_DETAIL_CONCURRENCY", 8)
PAGE_DELAY_SECONDS = env_float("MOHKAM_PAGE_DELAY_SECONDS", 0.0)
MIN_REQUEST_DELAY_SECONDS = env_float("MOHKAM_MIN_REQUEST_DELAY_SECONDS", 0.0)
MAX_REQUEST_DELAY_SECONDS = env_float("MOHKAM_MAX_REQUEST_DELAY_SECONDS", 0.10)
YEAR_START_STAGGER_SECONDS = env_float("MOHKAM_YEAR_START_STAGGER_SECONDS", 8.0)

MAX_RETRIES = env_int("MOHKAM_MAX_RETRIES", 5)
BACKOFF_SECONDS = env_float("MOHKAM_BACKOFF_SECONDS", 2.0)
CONNECT_TIMEOUT_SECONDS = env_float("MOHKAM_CONNECT_TIMEOUT_SECONDS", 4)
READ_TIMEOUT_SECONDS = env_float("MOHKAM_READ_TIMEOUT_SECONDS", 20)
RATE_LIMIT_PAUSE_SECONDS = env_int("MOHKAM_RATE_LIMIT_PAUSE_SECONDS", 600)
NETWORK_PAUSE_SECONDS = env_int("MOHKAM_NETWORK_PAUSE_SECONDS", 120)
LOGIN_RETRY_PAUSE_SECONDS = env_int("MOHKAM_LOGIN_RETRY_PAUSE_SECONDS", 60)
YEAR_COMPLETION_PAGE_CAP = env_int("MOHKAM_YEAR_COMPLETION_PAGE_CAP", 0)
EMPTY_PAGE_CONFIRMATIONS = env_int("MOHKAM_EMPTY_PAGE_CONFIRMATIONS", 2)
EMPTY_PAGE_RETRY_SECONDS = env_float("MOHKAM_EMPTY_PAGE_RETRY_SECONDS", 5.0)

USERNAME = os.environ.get("QISTAS_USERNAME", "")
PASSWORD = os.environ.get("QISTAS_PASSWORD", "")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

JUDGMENT_SECTION_LABELS = {
    "decision_text": "نص القرار",
    "principle": "المبدأ",
    "violation_decision": "قرار المخالفة",
    "appeal_reasons": "أسباب الطعن",
    "response_to_reasons": "الرد على الأسباب",
    "procedural_history": "التاريخ الإجرائي",
    "case_file": "ملف الحكم",
}
