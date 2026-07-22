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
MOHKAM_COURT_SHARDS=auto
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
MOHKAM_YEAR_START_STAGGER_SECONDS=8
MOHKAM_LOGIN_RETRY_PAUSE_SECONDS=60
```

`MOHKAM_GLOBAL_REQUEST_LIMIT` is the most important safety valve. If you see repeated `Max retries exceeded`, `ConnectTimeoutError`, or `Read timed out`, lower it first:

```env
MOHKAM_GLOBAL_REQUEST_LIMIT=16
MOHKAM_YEAR_WORKERS=1
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=12
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.25
MOHKAM_YEAR_START_STAGGER_SECONDS=15
```

If the site starts returning many `403` or `429`, reduce `MOHKAM_YEAR_WORKERS` first, then reduce `MOHKAM_GLOBAL_REQUEST_LIMIT`.

The scraper runs each year across court shards (`advCId`) by default. This is important because broad year or parent-filter searches can return a verified `no_records` page after the result window is exhausted, even when the year still contains more records under finer court filters. `MOHKAM_COURT_SHARDS=auto` discovers the Jordan court list from Qistas at startup and creates separate durable checkpoints like `qistas_state_2017_court_4_1_0_12_1.json`.

If you want to disable court sharding and fall back to parent shards (`pc`), set:

```env
MOHKAM_COURT_SHARDS=none
MOHKAM_PARENT_SHARDS=1,2,3,6
```

For small smoke tests, limit auto-discovered courts:

```env
MOHKAM_COURT_SHARDS=auto
MOHKAM_MAX_COURT_SHARDS=2
```

If a shard reaches a saturated result window and then receives an empty page, it is marked `needs_review` with `completion_reason=ambiguous_no_records_after_large_window` instead of being silently completed.

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

## Stop / Resume

```bash
docker compose stop mohkam-scraper
docker compose up -d mohkam-scraper
```

The service resumes from checkpoint files and continues to the next year automatically.
