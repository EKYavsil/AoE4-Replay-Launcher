"""Fetching and parsing Steam depot manifests via DepotDownloader.

A manifest lists every file in a build with its size, chunk count and SHA-1.
This module owns parsing; fetching lives in :mod:`aoe4replay.depot`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# DepotDownloader manifest line: Size  Chunks  SHA1(40 hex)  Flags(hex)  Name
_RECORD_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+([0-9a-fA-F]{40})\s+([0-9a-fA-F]+)\s+(.+)$"
)


@dataclass(frozen=True)
class ManifestRecord:
    path: str  # relative path, forward-slash separated
    size: int
    chunks: int
    sha1: str


def _is_safe_relpath(rel: str) -> bool:
    """Reject a manifest path that wouldn't stay under the launch root: absolute,
    Windows drive-/root-relative, or containing a ``..`` traversal component. A
    corrupt or tampered manifest must never let ``compose`` write outside the
    build directory."""
    p = Path(rel)
    return bool(rel) and not (p.is_absolute() or p.drive or p.root) and ".." not in p.parts


def parse_manifest(path: Path) -> list[ManifestRecord]:
    """Parse a DepotDownloader ``manifest_*.txt`` file into records.

    Records are de-duplicated by path (first occurrence wins) and sorted by path.
    Unsafe paths (absolute / drive / ``..``) are dropped at this single chokepoint
    so no later filesystem join can escape the build directory.
    """
    by_path: dict[str, ManifestRecord] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RECORD_RE.match(line)
        if not m:
            continue
        rel = m.group(5).strip().replace("\\", "/")
        if not _is_safe_relpath(rel):
            continue
        if rel not in by_path:
            by_path[rel] = ManifestRecord(
                path=rel,
                size=int(m.group(1)),
                chunks=int(m.group(2)),
                sha1=m.group(3).lower(),
            )
    return [by_path[k] for k in sorted(by_path)]
