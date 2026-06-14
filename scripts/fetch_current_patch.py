"""Record the current build's version in the build map, from aoe4world.

Run by the GitHub Action right after the Steam manifest update. aoe4world's
per-game ``patch`` is the RelicCardinal.exe build number — exactly the version a
replay embeds — so the most common patch among recent ranked games is the live
build's version. We write it onto the latest build map entry, with guards that
make it safe to run unattended:

* **mode, not first** — the most common patch among many recent games filters out
  PUP/beta/custom outliers and the brief mix right after a build drops;
* **forward-only** — only act when the patch is higher than everything recorded,
  which also means we wait until players have actually moved to a new build
  (Steam's manifest leads aoe4world, so the latest entry is already the new build);
* **never overwrite** — an existing, differing version is left untouched for a
  human to check, never silently changed;
* **fail-safe** — any network/parse problem is swallowed so the date/manifest
  update is never blocked.

Usage:
    python scripts/fetch_current_patch.py <build-map.json>
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

_API = "https://aoe4world.com/api/v0/games?per_page=50"
_UA = {"User-Agent": "aoe4-replay-launcher (build-map version sync)"}
_SANE_RANGE = (1, 1_000_000)  # a plausible build number, to reject junk


def current_patch() -> int | None:
    """Most common ``patch`` among recent games, or None if unavailable."""
    req = urllib.request.Request(_API, headers=_UA)  # noqa: S310 (trusted host)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        data = json.loads(resp.read())
    games = data.get("games", []) if isinstance(data, dict) else data
    patches = [
        g["patch"]
        for g in games
        if isinstance(g, dict) and isinstance(g.get("patch"), int)
        and _SANE_RANGE[0] <= g["patch"] <= _SANE_RANGE[1]
    ]
    if not patches:
        return None
    return Counter(patches).most_common(1)[0][0]


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: fetch_current_patch.py <build-map.json>")
    map_path = Path(sys.argv[1])

    try:
        patch = current_patch()
    except Exception as exc:  # noqa: BLE001 - never block the manifest update
        print(f"aoe4world unreachable ({exc}); skipping version sync.")
        return 0
    if not patch:
        print("No usable patch in recent games; skipping.")
        return 0

    entries = json.loads(map_path.read_text(encoding="utf-8-sig"))
    if isinstance(entries, dict):
        entries = [entries]
    max_known = max((e.get("version") or 0) for e in entries) if entries else 0

    if patch <= max_known:
        print(f"Current patch {patch} already covered (max known {max_known}); nothing to do.")
        return 0

    entries.sort(key=lambda e: e.get("validFrom") or "")
    latest = entries[-1]  # Steam leads aoe4world, so the live build is the last entry
    if latest.get("version"):
        print(
            f"Latest build {latest.get('buildId')} already has version "
            f"{latest['version']} != {patch}; leaving it for manual review."
        )
        return 0

    latest["version"] = patch
    map_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
    print(f"Set {latest.get('buildId')} version = {patch} (from aoe4world).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
