from __future__ import annotations

import sys
import time

from . import config
from .scraper import ensure_dirs, load_state, scrape_year


def main() -> int:
    ensure_dirs()
    print(
        f"[supervisor] start years={config.START_YEAR}..{config.END_YEAR} "
        f"target_host_utilization={config.TARGET_HOST_UTILIZATION}",
        flush=True,
    )
    for year in range(config.START_YEAR, config.END_YEAR - 1, -1):
        while True:
            state = load_state(year)
            if state.get("completed"):
                print(f"[supervisor] year={year} complete; advancing", flush=True)
                break
            try:
                print(f"[supervisor] year={year} resume_page={int(state.get('last_completed_page', 0)) + 1}", flush=True)
                scrape_year(year)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[supervisor] year={year} crashed={exc}; retrying in 60s", file=sys.stderr, flush=True)
                time.sleep(60)
    print("[supervisor] all years complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
