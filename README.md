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
