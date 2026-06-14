"""Append the newly released build to the build map from Steam app_info output.

Run by the GitHub Action: ``steamcmd +login anonymous +app_info_print 1466860``
writes the (public) product info; this parses depot 1466861's current public
manifest and adds it to the build map if it is not already known. No Steam
account or ownership is required — the public manifest GID is anonymous metadata,
and only the manifest id is recorded (no game files are downloaded).

Builds stay live for days to weeks while the Action runs every few hours, so the
current public branch is always captured before Steam rolls to the next build.

Usage:
    python scripts/update_build_map.py <appinfo.txt> <build-map.json>

Writes the build map only when a genuinely new manifest appears; the Action
commits based on the resulting git diff.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

DEPOT = "1466861"  # AoE4's main content depot (the only one used for playback)

# depot 1466861's manifests block body (holds the public branch's gid)
_MANIFESTS = re.compile(r'"' + DEPOT + r'"\s*\{.*?"manifests"\s*\{(.*?)\}\s*\}', re.S)


def parse_appinfo(text: str) -> tuple[str, int | None]:
    """Return (public_manifest_gid, timeupdated) for DEPOT from app_info output."""
    manifests = _MANIFESTS.search(text)
    if not manifests:
        raise SystemExit(f"Could not find depot {DEPOT} manifests in app_info output.")
    gid = re.search(r'"public"\s*\{\s*"gid"\s*"(\d+)"', manifests.group(1))
    if not gid:
        raise SystemExit(f"No public manifest found for depot {DEPOT}.")
    branch = re.search(r'"branches"\s*\{.*?"public"\s*\{([^}]*)\}', text, re.S)
    stamp = re.search(r'"timeupdated"\s*"(\d+)"', branch.group(1)) if branch else None
    return gid.group(1), int(stamp.group(1)) if stamp else None


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: update_build_map.py <appinfo.txt> <build-map.json>")
    appinfo_path, map_path = Path(sys.argv[1]), Path(sys.argv[2])
    text = appinfo_path.read_text(encoding="utf-8", errors="replace")
    manifest, timeupdated = parse_appinfo(text)

    entries = json.loads(map_path.read_text(encoding="utf-8-sig"))
    if isinstance(entries, dict):
        entries = [entries]
    if any(d.get("manifest") == manifest for e in entries for d in e.get("depots", [])):
        print(f"No change: manifest {manifest} already in the build map.")
        return 0

    when = (
        datetime.fromtimestamp(timeupdated, tz=UTC).replace(tzinfo=None)
        if timeupdated
        else datetime.now(tz=UTC).replace(tzinfo=None)
    )
    name = f"{when:%Y-%m-%d}-live"
    if any(e.get("name") == name for e in entries):  # >1 patch in a day
        name = f"{name}-{manifest[:6]}"

    entries.append(
        {
            "name": name,
            "validFrom": when.isoformat(),
            "validTo": None,
            "buildId": name,
            "installDir": None,
            "depots": [{"id": DEPOT, "manifest": manifest}],
        }
    )
    entries.sort(key=lambda e: e.get("validFrom") or "")
    for i in range(len(entries) - 1):  # close any open entry that is no longer the latest
        if entries[i].get("validTo") is None:
            entries[i]["validTo"] = entries[i + 1]["validFrom"]
    entries[-1]["validTo"] = None

    map_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
    print(f"Added build {name} (manifest {manifest}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
