# MOHKAM Data

Dockerized Qistas judicial-judgment scraper with durable local storage, per-year auto-advance, checkpointing, compact dedupe keys, adaptive concurrency, and automatic retry/backoff.

## Important

This repository intentionally does **not** hardcode credentials. Do not publish credentials in a public GitHub repository. Put them in a local `.env` file or inject them as Docker secrets/environment variables.

## Run

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f mohkam-scraper
```

## High-Speed Mode

The service runs multiple year shards in parallel and fetches details with high thread concurrency. For a Core i7 14th gen / 32 GB RAM machine, start with:

```env
MOHKAM_TARGET_HOST_UTILIZATION=0.90
MOHKAM_COURT_SHARDS=none
MOHKAM_MAX_COURT_SHARDS=0
MOHKAM_PARENT_SHARDS=1,2,3,6
MOHKAM_ALLOW_UNSAFE_SPEED=false
MOHKAM_YEAR_WORKERS=3
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=24
MOHKAM_GLOBAL_REQUEST_LIMIT=48
MOHKAM_MAX_DETAIL_CONCURRENCY=128
MOHKAM_MIN_REQUEST_DELAY_SECONDS=0
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.10
MOHKAM_PAGE_DELAY_SECONDS=0
MOHKAM_YEAR_START_STAGGER_SECONDS=0
MOHKAM_LOGIN_RETRY_PAUSE_SECONDS=60
MOHKAM_EMPTY_PAGE_CONFIRMATIONS=1
MOHKAM_EMPTY_PAGE_RETRY_SECONDS=0
MOHKAM_UNVERIFIED_EMPTY_PAGE_PAUSE_SECONDS=2
MOHKAM_UNVERIFIED_EMPTY_PAGE_MAX_RETRIES=5
MOHKAM_RELOGIN_ON_UNVERIFIED_EMPTY=true
MOHKAM_MAX_RETRIES=5
MOHKAM_CONNECT_TIMEOUT_SECONDS=4
MOHKAM_READ_TIMEOUT_SECONDS=20
```

To use more host resources after the safe profile is confirmed working, use this stable-high parent-shard profile. The page-search side should stay modest; most of the extra resource use should come from detail-page concurrency.

```env
MOHKAM_TARGET_HOST_UTILIZATION=0.90
MOHKAM_COURT_SHARDS=none
MOHKAM_PARENT_SHARDS=1,2,3,6
MOHKAM_ALLOW_UNSAFE_SPEED=true
MOHKAM_YEAR_WORKERS=2
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=32
MOHKAM_GLOBAL_REQUEST_LIMIT=32
MOHKAM_MAX_DETAIL_CONCURRENCY=128
MOHKAM_MIN_REQUEST_DELAY_SECONDS=0.02
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.12
MOHKAM_PAGE_DELAY_SECONDS=0
MOHKAM_YEAR_START_STAGGER_SECONDS=3
MOHKAM_EMPTY_PAGE_CONFIRMATIONS=1
MOHKAM_EMPTY_PAGE_RETRY_SECONDS=0
MOHKAM_UNVERIFIED_EMPTY_PAGE_PAUSE_SECONDS=5
MOHKAM_UNVERIFIED_EMPTY_PAGE_MAX_RETRIES=3
MOHKAM_RELOGIN_ON_UNVERIFIED_EMPTY=true
MOHKAM_MAX_RETRIES=7
MOHKAM_CONNECT_TIMEOUT_SECONDS=6
MOHKAM_READ_TIMEOUT_SECONDS=25
```

If logs show `reason=layout_shell_video`, Qistas is returning its generic layout page instead of search results. Lower `MOHKAM_YEAR_WORKERS` first, then lower `MOHKAM_GLOBAL_REQUEST_LIMIT`.

`MOHKAM_GLOBAL_REQUEST_LIMIT` is the most important safety valve. If you see repeated `Max retries exceeded`, `ConnectTimeoutError`, or `Read timed out`, lower it first:

```env
MOHKAM_GLOBAL_REQUEST_LIMIT=16
MOHKAM_YEAR_WORKERS=1
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=12
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.25
MOHKAM_YEAR_START_STAGGER_SECONDS=5
```

If the site starts returning many `403` or `429`, reduce `MOHKAM_YEAR_WORKERS` first, then reduce `MOHKAM_GLOBAL_REQUEST_LIMIT`.

The scraper runs each year across parent shards (`pc`) by default. This keeps the main run fast and avoids exploding the job into hundreds of court shards.

Court sharding (`advCId`) is available only as a targeted fallback for years that hit a saturated result window. Do not enable it for the normal full run unless you explicitly want the slower exhaustive mode:

```env
MOHKAM_COURT_SHARDS=auto
MOHKAM_MAX_COURT_SHARDS=2
```

If a shard reaches a saturated result window and then receives an empty page, it is marked `needs_review` with `completion_reason=ambiguous_no_records_after_large_window` instead of being silently completed. The scraper does not save HTML pages for this condition.

If the error happens before any data is retrieved, it is usually a login/startup connection burst. Start with the safe profile above, confirm records are being written, then increase `MOHKAM_YEAR_WORKERS` and `MOHKAM_GLOBAL_REQUEST_LIMIT` gradually.

By default the scraper clamps unsafe `.env` speed values to prevent startup connection storms. To intentionally bypass those caps, set `MOHKAM_ALLOW_UNSAFE_SPEED=true`, but only after the safe profile writes data successfully.

Data is written locally to:

```text
./data/qistas_all.jsonl
./data/state/
./data/logs/
./data/qistas_seen_keys.tsv
./data/qistas_failed_details.jsonl
```

## Monitor

```bash
docker compose run --rm monitor
```

## Raw HTML Archive Service

The raw HTML service is separate from the JSON scraper and uses `docker-compose.raw.yml`. It saves compressed search-result HTML and, by default, compressed detail-page HTML, then tracks everything in a SQLite manifest.

```bash
docker compose -f docker-compose.raw.yml up -d --build
docker compose -f docker-compose.raw.yml logs -f mohkam-raw-html
```

Raw files are written locally under:

```text
./data/raw_html/search/year=2017/parent=1/court=none/page=0000001.html.gz
./data/raw_html/details/bucket=459/6361459_2.html.gz
./data/state/raw_html_state.sqlite3
./data/logs/raw_html_failed.jsonl
```

Monitor the raw archive:

```bash
docker compose -f docker-compose.raw.yml run --rm raw-monitor
```

Useful raw settings:

```env
MOHKAM_RAW_FETCH_DETAILS=true
MOHKAM_RAW_SEARCH_PAGE_CAP=0
MOHKAM_RAW_DETAIL_WORKERS=24
MOHKAM_RAW_COMPRESSLEVEL=5
MOHKAM_RAW_EMPTY_PAUSE_SECONDS=8
MOHKAM_RAW_EMPTY_MAX_RETRIES=3
MOHKAM_RAW_DETAIL_MAX_RETRIES=3
```

For a tiny smoke test, set `MOHKAM_RAW_SEARCH_PAGE_CAP=1`; for the full archive, keep it `0`.

## Stop / Resume

```bash
docker compose stop mohkam-scraper
docker compose up -d mohkam-scraper
```

The service resumes from checkpoint files and continues to the next year automatically.
