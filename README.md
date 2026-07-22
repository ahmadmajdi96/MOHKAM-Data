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
MOHKAM_YEAR_WORKERS=6
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=48
MOHKAM_GLOBAL_REQUEST_LIMIT=128
MOHKAM_MAX_DETAIL_CONCURRENCY=128
MOHKAM_MIN_REQUEST_DELAY_SECONDS=0
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.05
MOHKAM_PAGE_DELAY_SECONDS=0
```

`MOHKAM_GLOBAL_REQUEST_LIMIT` is the most important safety valve. If you see repeated `Max retries exceeded`, `ConnectTimeoutError`, or `Read timed out`, lower it first:

```env
MOHKAM_GLOBAL_REQUEST_LIMIT=64
MOHKAM_YEAR_WORKERS=4
MOHKAM_DETAIL_CONCURRENCY_PER_YEAR=32
MOHKAM_MAX_REQUEST_DELAY_SECONDS=0.10
```

If the site starts returning many `403` or `429`, reduce `MOHKAM_YEAR_WORKERS` first, then reduce `MOHKAM_GLOBAL_REQUEST_LIMIT`.

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
