from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .scraper import configured_court_shards, ensure_dirs, load_state, scrape_year


Shard = tuple[int, int, str | None]


def run_shard_until_complete(year: int, parent: int, court_id: str | None, worker_index: int) -> Shard:
    if config.YEAR_START_STAGGER_SECONDS > 0 and worker_index > 0:
        sleep_for = config.YEAR_START_STAGGER_SECONDS * worker_index
        print(f"[supervisor] year={year} parent={parent} court={court_id or '-'} startup_stagger={sleep_for:.1f}s", flush=True)
        time.sleep(sleep_for)

    while True:
        state = load_state(year, parent, court_id)
        if state.get("completed"):
            print(f"[supervisor] year={year} parent={parent} court={court_id or '-'} complete; worker done", flush=True)
            return (year, parent, court_id)
        if state.get("needs_review"):
            print(f"[supervisor] year={year} parent={parent} court={court_id or '-'} needs_review; worker done", flush=True)
            return (year, parent, court_id)
        try:
            print(
                f"[supervisor] year={year} parent={parent} court={court_id or '-'} "
                f"resume_page={int(state.get('last_completed_page', 0)) + 1}",
                flush=True,
            )
            scrape_year(year, parent, court_id)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                f"[supervisor] year={year} parent={parent} court={court_id or '-'} "
                f"crashed={exc}; retrying in {config.LOGIN_RETRY_PAUSE_SECONDS}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(config.LOGIN_RETRY_PAUSE_SECONDS)


def state_done_or_review(year: int, parent: int, court_id: str | None = None) -> tuple[bool, bool]:
    state = load_state(year, parent, court_id)
    return bool(state.get("completed")), bool(state.get("needs_review"))


def build_shards() -> tuple[list[Shard], int]:
    court_shards = configured_court_shards()
    pending: list[Shard] = []
    review_count = 0
    if court_shards:
        print(f"[supervisor] using court shards count={len(court_shards)}", flush=True)
        for year in range(config.START_YEAR, config.END_YEAR - 1, -1):
            for court_id in court_shards:
                completed, needs_review = state_done_or_review(year, config.PARENT, court_id)
                if needs_review:
                    review_count += 1
                elif not completed:
                    pending.append((year, config.PARENT, court_id))
        return pending, review_count

    print(
        f"[supervisor] using parent shards count={len(config.PARENT_SHARDS)} "
        f"parents={','.join(str(parent) for parent in config.PARENT_SHARDS)}",
        flush=True,
    )
    for year in range(config.START_YEAR, config.END_YEAR - 1, -1):
        for parent in config.PARENT_SHARDS:
            completed, needs_review = state_done_or_review(year, parent)
            if needs_review:
                review_count += 1
            elif not completed:
                pending.append((year, parent, None))
    return pending, review_count


def main() -> int:
    ensure_dirs()
    shards, review_count = build_shards()
    print(
        f"[supervisor] start years={config.START_YEAR}..{config.END_YEAR} "
        f"court_shards={'auto' if config.COURT_SHARDS_AUTO else len(config.COURT_SHARDS)} "
        f"parent_shards={','.join(str(parent) for parent in config.PARENT_SHARDS)} "
        f"target_host_utilization={config.TARGET_HOST_UTILIZATION} "
        f"year_workers={config.YEAR_WORKERS} "
        f"detail_concurrency_per_year={config.DETAIL_CONCURRENCY_PER_YEAR} "
        f"global_request_limit={config.GLOBAL_REQUEST_LIMIT} "
        f"pending_shards={len(shards)} "
        f"review_shards={review_count}",
        flush=True,
    )

    if not shards:
        if review_count:
            print(f"[supervisor] no pending shards; review_shards={review_count}", flush=True)
        else:
            print("[supervisor] all shards complete", flush=True)
        return 0

    with ThreadPoolExecutor(max_workers=max(1, config.YEAR_WORKERS)) as executor:
        future_map = {
            executor.submit(run_shard_until_complete, year, parent, court_id, index): (year, parent, court_id)
            for index, (year, parent, court_id) in enumerate(shards)
        }
        for future in as_completed(future_map):
            year, parent, court_id = future_map[future]
            try:
                future.result()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[supervisor] year={year} parent={parent} court={court_id or '-'} "
                    f"worker failed permanently={exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1

    _, final_review_count = build_shards()
    if final_review_count:
        print(f"[supervisor] pending shards complete; review_shards={final_review_count}", flush=True)
    else:
        print("[supervisor] all shards complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
