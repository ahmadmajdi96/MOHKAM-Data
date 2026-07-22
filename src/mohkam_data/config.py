from __future__ import annotations

import os
from pathlib import Path


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

START_YEAR = 2026
END_YEAR = 1850
DB = 2
COUNTRY = 1
PARENT = -1

TARGET_HOST_UTILIZATION = 0.90
MAX_DETAIL_CONCURRENCY = 64
MIN_DETAIL_CONCURRENCY = 4
PAGE_DELAY_SECONDS = 0.075
MIN_REQUEST_DELAY_SECONDS = 0.10
MAX_REQUEST_DELAY_SECONDS = 0.30

MAX_RETRIES = 5
BACKOFF_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
RATE_LIMIT_PAUSE_SECONDS = 600
NETWORK_PAUSE_SECONDS = 180
YEAR_COMPLETION_PAGE_CAP = 2000

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
