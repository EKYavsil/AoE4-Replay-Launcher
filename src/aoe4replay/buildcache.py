"""Cache of composed launch builds, so a replay doesn't rebuild every time.

A composed build lives in ``launch_work/builds/<build_id>/``. It is reused for
the whole panel session (any replay on the same build opens instantly) and is
removed by cleanup, *unless* the user marks the build "saved": saved ids are
listed in ``launch_work/saved_builds.json`` and survive across sessions.

The list is the single source of truth for "what survives cleanup". It is
written atomically and, if it ever becomes unreadable, cleanup refuses to run
(it never risks deleting a saved build). Nothing here can lose data — the actual
downloaded deltas live in restic; a build dir is only an extracted, rebuildable
cache of them.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from . import launch
from .config import Config
from .manifest import ManifestRecord


def _safe(build_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", build_id)


def builds_root(cfg: Config) -> Path:
    return cfg.project_root / "launch_work" / "builds"


def build_dir(cfg: Config, build_id: str) -> Path:
    safe = _safe(build_id)
    # "." / ".." / "" would resolve to the builds root or its parent — a build id
    # (from a build map) must never address anything but its own child directory.
    if safe in ("", ".", ".."):
        raise ValueError(f"Unsafe build id for a cache directory: {build_id!r}")
    return builds_root(cfg) / safe


def _saved_path(cfg: Config) -> Path:
    return cfg.project_root / "launch_work" / "saved_builds.json"


# --- the saved-id list (atomic, corruption-tolerant) ---------------------------

def load_saved(cfg: Config) -> set[str] | None:
    """Saved build ids, or ``None`` if the list exists but can't be read.

    ``None`` means "unknown" — callers must treat it conservatively (e.g. cleanup
    skips, never deleting a build that might be saved)."""
    path = _saved_path(cfg)
    if not path.exists():
        return set()
    try:
        # utf-8-sig tolerates a stray BOM (e.g. a file touched by PowerShell): a BOM
        # otherwise makes json.loads fail -> the list reads as "unreadable" -> cleanup
        # refuses to run and unsaved builds pile up forever.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return {str(x) for x in data}


def save_saved(cfg: Config, ids: set[str]) -> None:
    path = _saved_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_saved(cfg: Config, build_id: str) -> bool:
    saved = load_saved(cfg)
    return saved is not None and build_id in saved


def mark_saved(cfg: Config, build_id: str) -> None:
    saved = load_saved(cfg)
    if saved is None:
        # The list exists but is unreadable: refuse to write, or we'd overwrite it
        # with a single entry and silently drop every previously saved build.
        print("Build cache: saved list unreadable; not recording this save.")
        return
    saved.add(build_id)
    save_saved(cfg, saved)


def unmark_saved(cfg: Config, build_id: str) -> None:
    saved = load_saved(cfg)
    if saved and build_id in saved:
        saved.discard(build_id)
        save_saved(cfg, saved)


# --- verification, sizing, cleanup ---------------------------------------------

def verify_sizes(records: list[ManifestRecord], target_dir: Path) -> bool:
    """Cheap integrity check: every manifest file is present at the right size.

    No hashing — just ``stat`` — so it stays fast even on a slow disk. Catches the
    realistic ways a saved build goes bad (missing/truncated files, a game update
    that changed a file's size); a same-size content change is not caught, which
    is rare enough to accept for the speed."""
    for record in records:
        if record.chunks == 0:
            continue
        path = target_dir / record.path
        try:
            if path.stat().st_size != record.size:
                return False
        except OSError:
            return False
    return True


def delta_size(target_dir: Path) -> int:
    """Disk actually consumed by a build: files unique to it (one hard link),
    excluding files hard-linked to the live install (which cost no extra space)."""
    total = 0
    for path in target_dir.rglob("*"):
        with contextlib.suppress(OSError):
            st = path.stat()
            if stat.S_ISREG(st.st_mode) and st.st_nlink <= 1:
                total += st.st_size
    return total


def _force_rmtree(path: Path) -> None:
    def _on_error(func, p, _exc):
        # Retry the original op (os.rmdir for dirs, os.unlink for files) after
        # clearing the read-only bit; not os.unlink, which fails on directories.
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
            return
        except OSError:
            pass
        # Last resort on Windows: a dir restic restored with the live system's
        # restrictive ACL (e.g. C/Users) denies even chmod. We own it, so reset
        # the ACL to inherited and retry instead of leaving an empty shell.
        if os.name == "nt":
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                subprocess.run(
                    ["icacls", str(p), "/reset", "/q"], capture_output=True, check=False
                )
                os.chmod(p, stat.S_IWRITE)
                func(p)

    shutil.rmtree(path, onexc=lambda f, p, e: _on_error(f, p, e))


def delete_build(cfg: Config, build_id: str) -> None:
    """Remove a saved build: drop its saved mark and delete its composed dir.

    The restic delta is untouched, so a later replay just rebuilds it locally
    (no re-download)."""
    unmark_saved(cfg, build_id)
    target = build_dir(cfg, build_id)
    # Defence in depth: only ever delete a strict child of the builds root.
    root = builds_root(cfg).resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return
    if resolved == root or root not in resolved.parents:
        return
    if target.exists():
        _force_rmtree(target)


def cleanup(cfg: Config) -> None:
    """Delete non-saved build dirs (session leftovers / crash leftovers).

    Refuses to run if the saved list is unreadable (never risks a saved build),
    and skips everything while a game is running (a build dir may be in use)."""
    root = builds_root(cfg)
    if not root.exists():
        return
    saved = load_saved(cfg)
    if saved is None:
        print("Build cache: saved list unreadable; skipping cleanup to be safe.")
        return
    if launch.is_game_running():
        return
    keep = {_safe(s) for s in saved}
    for entry in root.iterdir():
        if entry.is_dir() and entry.name not in keep:
            with contextlib.suppress(OSError):
                _force_rmtree(entry)
