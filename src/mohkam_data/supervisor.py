from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .scraper import ensure_dirs, load_state, scrape_year


def run_year_until_complete(year: int, worker_index: int) -> int:
    if config.YEAR_START_STAGGER_SECONDS > 0 and worker_index > 0:
        sleep_for = config.YEAR_START_STAGGER_SECONDS * worker_index
        print(f"[supervisor] year={year} startup_stagger={sleep_for:.1f}s", flush=True)
        time.sleep(sleep_for)

    while True:
        state = load_state(year)
        if state.get("completed"):
            print(f"[supervisor] year={year} complete; worker done", flush=True)
            return year
        try:
            print(f"[supervisor] year={year} resume_page={int(state.get('last_completed_page', 0)) + 1}", flush=True)
            scrape_year(year)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[supervisor] year={year} crashed={exc}; retrying in {config.LOGIN_RETRY_PAUSE_SECONDS}s", file=sys.stderr, flush=True)
            time.sleep(config.LOGIN_RETRY_PAUSE_SECONDS)


def main() -> int:
    ensure_dirs()
    years = [year for year in range(config.START_YEAR, config.END_YEAR - 1, -1) if not load_state(year).get("completed")]
    print(
        f"[supervisor] start years={config.START_YEAR}..{config.END_YEAR} "
        f"target_host_utilization={config.TARGET_HOST_UTILIZATION} "
        f"year_workers={config.YEAR_WORKERS} "
        f"detail_concurrency_per_year={config.DETAIL_CONCURRENCY_PER_YEAR} "
        f"global_request_limit={config.GLOBAL_REQUEST_LIMIT} "
        f"pending_years={len(years)}",
        flush=True,
    )

    if not years:
        print("[supervisor] all years complete", flush=True)
        return 0

    with ThreadPoolExecutor(max_workers=max(1, config.YEAR_WORKERS)) as executor:
        future_map = {
            executor.submit(run_year_until_complete, year, index): year
            for index, year in enumerate(years)
        }
        for future in as_completed(future_map):
            year = future_map[future]
            try:
                future.result()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[supervisor] year={year} worker failed permanently={exc}", file=sys.stderr, flush=True)
                return 1

    print("[supervisor] all years complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
