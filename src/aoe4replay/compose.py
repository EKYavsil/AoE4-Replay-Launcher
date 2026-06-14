"""Composing a launch build from the seed install + a restored delta.

Every manifest file is materialised into the launch directory, hardlinked from
the delta when present, otherwise from the live Steam install (seed). Hardlinks
are attempted first and fall back to a copy across volumes.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .manifest import ManifestRecord


def link_or_copy(source: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def find_missing(
    records: list[ManifestRecord],
    trusted_dirs: list[Path],
    seed: Path,
    sha1_of: Callable[[Path], str] | None = None,
) -> list[str]:
    """Manifest files this build needs that are not already correctly available.

    Files in ``trusted_dirs`` (the restored delta and the per-build supplement)
    are this build's own files and are trusted by path. The seed is a *different*
    build, so a seed file only counts if its size and SHA-1 match the manifest
    (verified via ``sha1_of`` when provided). Everything else is "missing" and
    must be fetched from Steam — this covers both files removed from the live
    install and files that still exist there but have since changed content.
    """
    trusted = [Path(d) for d in trusted_dirs]
    seed = Path(seed)
    missing = []
    for record in records:
        if record.chunks == 0:
            continue
        if any((root / record.path).is_file() for root in trusted):
            continue
        seed_file = seed / record.path
        if (
            seed_file.is_file()
            and seed_file.stat().st_size == record.size
            and (sha1_of is None or sha1_of(seed_file) == record.sha1)
        ):
            continue
        missing.append(record.path)
    return missing


def compose_launch_build(
    cfg: Config,
    records: list[ManifestRecord],
    source_dirs: list[Path],
    launch_dir: Path,
    progress: Callable[[float], None] | None = None,
) -> int:
    """Materialise a full launch tree into ``launch_dir``.

    Each manifest file is taken from the first of ``source_dirs`` (priority
    order, e.g. restored delta then supplement) that has it, falling back to the
    live Steam install (seed). Returns the count materialised; raises if a file
    is found in none of them. ``progress`` (if given) is called with the percent
    of files materialised, on each whole-percent change.
    """
    roots = [Path(d) for d in source_dirs] + [cfg.steam_install]
    launch_dir = Path(launch_dir)
    launch_dir.mkdir(parents=True, exist_ok=True)

    total = sum(1 for r in records if r.chunks != 0)
    materialised = 0
    last_pct = -1
    for record in records:
        if record.chunks == 0:  # directory / empty placeholder
            continue
        rel = record.path  # forward-slash separated; pathlib handles it on Windows
        source = next((root / rel for root in roots if (root / rel).is_file()), None)
        if source is None:
            raise FileNotFoundError(
                f"No source for {rel!r}: not in delta/supplement or seed ({cfg.steam_install})."
            )
        target = launch_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        link_or_copy(source, target)
        materialised += 1
        if progress and total:
            pct = materialised / total * 100.0
            if int(pct) != last_pct:  # throttle to ~100 updates
                last_pct = int(pct)
                progress(pct)
    return materialised
