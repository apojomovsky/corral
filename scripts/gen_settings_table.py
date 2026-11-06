"""Regenerate the settings table in `docs/configuration.md`.

The table is generated from the settings registry (`src/korapy/settings.py`),
which owns every name, default and meaning, so the doc never hand-copies a
value. After changing the registry, run this from the repository root:

    uv run --locked python scripts/gen_settings_table.py

`tests/test_docs.py` fails when the committed table drifts from the
registry, naming this script as the fix.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_docs import (  # noqa: E402
    CONFIGURATION,
    TABLE_END,
    TABLE_START,
    render_settings_table,
)


def main() -> int:
    text = CONFIGURATION.read_text(encoding="utf-8")
    start = text.index(TABLE_START) + len(TABLE_START)
    end = text.index(TABLE_END)
    updated = text[:start] + "\n" + render_settings_table() + text[end:]
    CONFIGURATION.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
