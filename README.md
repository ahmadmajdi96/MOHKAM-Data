# MOHKAM Data

Dockerized Qistas judicial-judgment scraper tailored for large-scale `أحكام قضائية` collection.

## Architecture

The main service is now a SQLite-backed pipeline, not a page-by-page blocking scraper:

- **Index workers** fetch search-result pages and enqueue detail URLs.
- **Detail workers** independently fetch judgment detail pages and extract all known tabs.
- **SQLite checkpointing** tracks shards, queued details, retries, saved records, and seen keys.
- **JSONL output** is written directly to `./data/qistas_all.jsonl`.
- **Resume-safe state** lives in `./data/state/qistas_pipeline.sqlite3`.

This keeps search pagination stable while still using more resources on detail extraction, where parallelism is safer.

## Important

This repository intentionally does **not** hardcode credentials. Put credentials in a local `.env` file.

## Run

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f mohkam-scraper
```

## Recommended Profile

Start with this profile for a Core i7 / 32 GB machine:

```env
MOHKAM_PARENT_SHARDS=1,2,3,6
MOHKAM_COURT_SHARDS=none
MOHKAM_ALLOW_UNSAFE_SPEED=true
MOHKAM_INDEX_WORKERS=2
MOHKAM_DETAIL_WORKERS=24
MOHKAM_GLOBAL_REQUEST_LIMIT=24
MOHKAM_DETAIL_WORKER_LOGIN_STAGGER_SECONDS=0.25
MOHKAM_MIN_REQUEST_DELAY_SECONDS=0.05
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.20
MOHKAM_PAGE_DELAY_SECONDS=0.1
MOHKAM_CONNECT_TIMEOUT_SECONDS=10
MOHKAM_READ_TIMEOUT_SECONDS=45
MOHKAM_MAX_RETRIES=9
```

If logs show many timeouts or `reason=layout_shell_video`, lower these first:

```env
MOHKAM_INDEX_WORKERS=1
MOHKAM_GLOBAL_REQUEST_LIMIT=16
MOHKAM_DETAIL_WORKERS=16
```

If the run is stable and you want more speed, raise detail workers first:

```env
MOHKAM_DETAIL_WORKERS=32
MOHKAM_GLOBAL_REQUEST_LIMIT=32
```

Avoid raising index workers too high; Qistas search pages are the fragile part.

## Output

```text
./data/qistas_all.jsonl
./data/qistas_seen_keys.tsv
./data/state/qistas_pipeline.sqlite3
./data/logs/qistas_failed_details.jsonl
```

Each output record includes the extracted tabs when present:

```text
decision_text
principle
violation_decision
appeal_reasons
response_to_reasons
procedural_history
case_file
content
```

## Monitor

```bash
docker compose run --rm monitor
```

The monitor reports output counts, seen keys, shard progress, and detail queue statuses.

## Stop / Resume

```bash
docker compose stop mohkam-scraper
docker compose up -d mohkam-scraper
```

The service resumes from SQLite checkpoints and continues queued detail jobs automatically.
