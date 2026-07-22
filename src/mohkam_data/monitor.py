from __future__ import annotations

import json
from pathlib import Path

from . import config


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("rb") as handle:
        for _ in handle:
            count += 1
    return count


def main() -> int:
    output_lines = count_lines(config.OUTPUT_FILE)
    states = []
    if config.STATE_DIR.exists():
        for path in sorted(config.STATE_DIR.glob("qistas_state_*.json"), reverse=True):
            try:
                states.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
            except json.JSONDecodeError:
                continue
    print(f"output={config.OUTPUT_FILE}")
    print(f"records={output_lines}")
    for name, state in states[:10]:
        print(
            f"{name}: page={state.get('last_completed_page')} "
            f"written={state.get('written_records')} completed={state.get('completed')} "
            f"reason={state.get('completion_reason', '')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
