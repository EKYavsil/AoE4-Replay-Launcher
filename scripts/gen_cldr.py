"""Regenerate the shipped CLDR month-name / AM-PM table from babel.

Run after a babel/CLDR upgrade:  python scripts/gen_cldr.py
Requires the dev extra (babel). The runtime itself does not need babel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aoe4replay.replay import _CLDR_JSON, build_cldr_tables_via_babel  # noqa: E402

months, meridiem = build_cldr_tables_via_babel()
_CLDR_JSON.write_text(
    json.dumps({"months": months, "meridiem": meridiem}, ensure_ascii=False, indent=0),
    encoding="utf-8",
)
print(f"Wrote {_CLDR_JSON} ({len(months)} month names, {len(meridiem)} AM/PM markers)")
