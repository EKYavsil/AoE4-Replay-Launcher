"""High-level orchestration tying the pieces together.

``watch`` and ``add`` flow through here: resolve the build for a replay, ensure
its delta is stored in restic (downloading only what the seed lacks), restore it,
compose a launch build from seed + delta, and optionally launch the game.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import (
    aoe4world,
    buildcache,
    buildmap,
    compose,
    deltaseed,
    depot,
    launch,
    manifest,
    replay,
    resticrepo,
)
from .config import Config
from .contentindex import ContentIndex
from .depot import (  # noqa: F401 - re-exported for the panel to catch
    DownloadCancelled,
    SteamAuthError,
)
from .hashcache import HashCache


def _force_rmtree(path: Path) -> None:
    """Remove a tree, tolerating read-only files, long paths, and locked files.

    Never raises: a leftover that cannot be removed (e.g. still open by a running
    game) is left in place rather than crashing the run.
    """
    if not path.exists():
        return

    def _onexc(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
            return
        except OSError:
            pass
        # Last resort on Windows: restic can restore intermediate path dirs
        # (e.g. C/Users) with the live system's restrictive ACL, which then
        # denies even chmod. We own them, so reset the ACL to inherited and
        # retry — otherwise they pile up as undeletable empty shells.
        if os.name == "nt":
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                subprocess.run(
                    ["icacls", str(p), "/reset", "/q"], capture_output=True, check=False
                )
                os.chmod(p, stat.S_IWRITE)
                func(p)

    try:
        shutil.rmtree(path, onexc=_onexc)
    except OSError:
        # Retry via an extended-length path for trees deeper than MAX_PATH.
        with contextlib.suppress(OSError):
            shutil.rmtree("\\\\?\\" + str(path.resolve()), onexc=_onexc)


def _clean_stale_workdirs(cfg: Config) -> None:
    """Wipe the transient work area so no raw game files survive between runs.

    launch_work holds ephemeral download/restore dirs; the composed-build cache
    (``builds/`` + ``saved_builds.json``) is intentionally preserved — it is
    managed by :mod:`aoe4replay.buildcache`, not wiped here.
    """
    work = _launch_work(cfg)
    if not work.exists():
        return
    keep = {"builds", "saved_builds.json", "saved_builds.json.tmp"}
    for child in work.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            _force_rmtree(child)
        else:
            with contextlib.suppress(OSError):
                child.unlink()


def clean_workspace(cfg: Config) -> None:
    """Public entry point for sweeping transient work dirs (panel calls this at
    startup so leftover restore/download scratch dirs can't accumulate)."""
    _clean_stale_workdirs(cfg)


def _work_dir(cfg: Config) -> Path:
    return cfg.project_root / ".aoe4-work"


def _launch_work(cfg: Config) -> Path:
    return cfg.project_root / "launch_work"


def _build_map_path(cfg: Config) -> Path:
    return cfg.project_root / "data" / "aoe4-build-map.json"


# Raw URL of the maintained build map; a scheduled GitHub Action keeps it current
# with newly released builds (anonymous Steam app_info — manifest ids only).
BUILD_MAP_URL = (
    "https://raw.githubusercontent.com/EKYavsil/aoe4-replay-launcher"
    "/main/data/aoe4-build-map.json"
)

# Serialises the build-map read-modify-write across the panel's threads (the
# background sync vs. an inferred build written during a play), so one writer
# can't clobber the other's update. Each writer re-reads the map under the lock.
_MAP_LOCK = threading.Lock()


def sync_build_map(cfg: Config, url: str = BUILD_MAP_URL, timeout: int = 5) -> bool:
    """Merge newly released builds from the maintained build map into the local one.

    Best-effort and additive: any network/parse error leaves the local map
    untouched, and remote entries only add to (never shrink) what's on disk. This
    is what lets a panel that's been closed across several patches learn the builds
    it missed. Only the small JSON is fetched — never any game files.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "aoe4-replay-launcher"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            remote = json.loads(response.read())
    except Exception:  # noqa: BLE001 - offline / transient failures are non-fatal
        return False
    if not isinstance(remote, list) or not all(
        isinstance(e, dict) and e.get("buildId") and e.get("depots") for e in remote
    ):
        return False

    # Hold the lock across the read-merge-write so a build inferred during a play
    # can't be lost to (or lose) this sync; the network fetch above stays unlocked.
    with _MAP_LOCK:
        path = _build_map_path(cfg)
        if path.exists():
            try:
                local = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return False  # local map exists but is unreadable — never overwrite/lose it
            if isinstance(local, dict):
                local = [local]
            if not isinstance(local, list):
                return False
        else:
            local = []

        by_id = {e["buildId"]: e for e in local if isinstance(e, dict) and e.get("buildId")}
        # Merge per build id: remote is authoritative for every key it carries, but a
        # local-only enrichment (harvested `version`, `replayVersionOverride`) survives
        # when the remote entry doesn't include that key.
        for entry in remote:
            bid = entry["buildId"]
            by_id[bid] = {**by_id[bid], **entry} if bid in by_id else entry
        merged = sorted(by_id.values(), key=lambda e: e.get("validFrom") or "")

        if json.dumps(merged, sort_keys=True) == json.dumps(local, sort_keys=True):
            return False  # nothing new
        # Atomic write: a concurrent playback worker must never read a half-written map.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, indent=4), encoding="utf-8")
        os.replace(tmp, path)
        return True


def _manifest_history_path(cfg: Config) -> Path:
    return cfg.project_root / "data" / "aoe4-manifest-history.json"


def _content_index_path(cfg: Config) -> Path:
    return _work_dir(cfg) / "content-index.tsv"


def _index_stored(
    cfg: Config,
    index: ContentIndex,
    build_id: str,
    source_dir: Path,
    rec_by_path: dict[str, manifest.ManifestRecord],
    paths: list[str],
) -> None:
    """Record the files just stored under ``source_dir`` so other builds reuse them."""
    snap_id = resticrepo.snapshot_id_for_path(cfg, build_id, source_dir)
    if not snap_id:
        return
    prefix = resticrepo.stored_form(str(source_dir))
    for path in paths:
        record = rec_by_path.get(path)
        if record:
            index.add(record.size, record.sha1, snap_id, f"{prefix}/{path}")


def harvest_versions(cfg: Config) -> int:
    """Record each stored build's RelicCardinal.exe version in the build map.

    Maintainer one-off: for every build already in the restic store, restore just
    its RelicCardinal.exe (no re-download), read the exe build number — which is
    exactly the version a replay embeds — and write it into the matching build
    map entry (joined by buildId). This makes replays resolve by version, which
    is locale-independent and immune to day/month ambiguity. Returns the count
    of newly harvested versions.
    """
    map_path = _build_map_path(cfg)
    by_id = {b.build_id: b for b in buildmap.load_build_map(map_path)}
    resticrepo.ensure_repo(cfg)
    updated = 0
    seen: set[str] = set()
    for snap in resticrepo.list_builds(cfg):
        bid = snap.build_id
        if not bid or bid in seen:
            continue
        build = by_id.get(bid)
        if build is None or build.version:  # not in the map, or already known
            seen.add(bid)  # nothing to harvest for this id — don't revisit it
            continue
        tmp = Path(tempfile.mkdtemp(prefix="aoe4ver_"))
        try:
            resticrepo.restore_paths(cfg, snap.short_id, ["**/RelicCardinal.exe"], tmp)
            exe = next(iter(tmp.rglob("RelicCardinal.exe")), None)
            version = launch._exe_build_number(exe) if exe else None
        finally:
            _force_rmtree(tmp)
        if not version:
            # Don't mark seen: a *later* snapshot with the same id may have the exe.
            print(f"  {bid}: no RelicCardinal.exe in this snapshot (trying any others)")
            continue
        seen.add(bid)
        by_id[bid] = replace(build, version=version)
        updated += 1
        print(f"  {bid}: version {version}")
    if updated:
        buildmap.save_build_map(list(by_id.values()), map_path)
        print(f"Harvested {updated} version(s) into {map_path.name}.")
    else:
        print("No new versions to harvest.")
    return updated


def resolve_build(cfg: Config, stamp: datetime) -> buildmap.Build:
    """Find the build for a replay timestamp, inferring + persisting if unknown."""
    map_path = _build_map_path(cfg)
    builds = buildmap.load_build_map(map_path)
    try:
        return buildmap.build_for_timestamp(stamp, builds)
    except LookupError:
        history_path = _manifest_history_path(cfg)
        if not history_path.exists():
            raise
        inferred = buildmap.infer_build(stamp, buildmap.load_manifest_history(history_path))
        if inferred is None:
            raise
        # Persist under the lock, re-reading the map so a concurrent sync's entries
        # aren't lost (and a build it added meanwhile is used instead of inferring).
        with _MAP_LOCK:
            current = buildmap.load_build_map(map_path)
            with contextlib.suppress(LookupError):
                return buildmap.build_for_timestamp(stamp, current)
            print(f"Build map did not cover this replay; inferred build {inferred.name}.")
            buildmap.save_build_map([*current, inferred], map_path)
        return inferred


def _build_for_replay(
    cfg: Config, replay_path: Path, stamp: replay.ReplayStamp | None
) -> buildmap.Build:
    """Resolve the build for a replay.

    Primary key is the replay's embedded exe version (locale-independent and
    exact, so day/month ambiguity can never mis-resolve it). Only when that
    version has not been harvested do we fall back to the timestamp — preferring
    aoe4world's exact UTC over the replay's local time — and even then we refuse,
    loudly, to play a build whose known version contradicts the replay.
    """
    version = replay.read_version(replay_path)
    builds = buildmap.load_build_map(_build_map_path(cfg))

    by_version = buildmap.build_for_version(version, builds) if version else None
    if by_version is not None:
        print(f"Replay version {version} -> build {by_version.name} (exact match, no date used).")
        return by_version

    # Version unknown/unharvested: resolve by date. Prefer aoe4world's exact UTC —
    # it works even when the replay's local timestamp couldn't be parsed at all, so
    # try it before giving up (a non-Gregorian locale must not block resolution).
    when = stamp.value if stamp else None
    game_id = aoe4world.game_id_from_name(Path(replay_path).name)
    if game_id:
        utc = aoe4world.game_started_at_utc(game_id)
        if utc:
            local = f"local was {stamp.value}" if stamp else "local timestamp unreadable"
            print(f"aoe4world game {game_id}: UTC start {utc} ({local})")
            when = utc
    if when is None:
        raise RuntimeError(
            f"Couldn't resolve this replay: its exe version ({version}) isn't in the "
            f"build map yet, its timestamp couldn't be read, and aoe4world has no start "
            f"time for it. Please report this replay so it can be handled."
        )
    build = resolve_build(cfg, when)

    # Never silently launch the wrong build: if the date landed on a build whose
    # version we know and it disagrees with the replay, stop and explain.
    detail = f"timestamp '{stamp.text}'" if stamp else f"date {when:%Y-%m-%d %H:%M}"
    if version and build.version and build.version != version:
        raise RuntimeError(
            f"Refusing to launch a mismatched build. This replay's version is {version}, "
            f"but its {detail} resolved to {build.name} (version {build.version}). "
            f"Please report this replay so it can be handled correctly."
        )
    if version and not build.version:
        print(
            f"Replay version {version}: resolved {build.name} by date "
            f"(no harvested version for this build yet)."
        )
    return build


# How each manifest file will be sourced for the launch build.
ReuseMap = dict[str, list[tuple[str, manifest.ManifestRecord]]]


def _plan_sources(
    cfg: Config, records: list[manifest.ManifestRecord]
) -> tuple[ReuseMap, list[str], int]:
    """Decide where every manifest file comes from, trusting nothing by path.

    A file is taken from the live install only if its SHA-1 matches, otherwise
    from the content index if the exact (size, SHA-1) is stored, otherwise it is
    downloaded. Returns (reuse-by-snapshot, paths-to-download, seed-file-count).
    """
    hc = HashCache(_work_dir(cfg) / "seed-hash-cache.tsv")
    hc.load()
    index = ContentIndex(_content_index_path(cfg))
    index.load()

    reuse: ReuseMap = {}
    to_download: list[str] = []
    from_seed = 0
    for record in records:
        if record.chunks == 0:
            continue
        seed_file = cfg.steam_install / record.path
        if (
            seed_file.is_file()
            and seed_file.stat().st_size == record.size
            and hc.sha1(seed_file) == record.sha1
        ):
            from_seed += 1
            continue
        loc = index.lookup(record.size, record.sha1)
        if loc:
            reuse.setdefault(loc.snapshot, []).append((loc.stored_path, record))
        else:
            to_download.append(record.path)
    hc.save()
    return reuse, to_download, from_seed


def _delta_seed(
    cfg: Config,
    build: buildmap.Build,
    to_download: list[str],
    target_dir: Path,
    cancel: threading.Event | None = None,
) -> int:
    """Set ``target_dir`` up so DepotDownloader reuses the live game's chunks and
    downloads only the genuine delta. Best-effort: any problem returns 0 and a
    plain full download follows. See :mod:`aoe4replay.deltaseed`."""
    try:
        version = launch.installed_game_version(cfg)
        if not version:
            return 0
        live = buildmap.build_for_version(version, buildmap.load_build_map(_build_map_path(cfg)))
        if live is None or not live.depots:
            print("Chunk delta: live build's version not in the map; downloading full files.")
            return 0
        depot_id = int(build.depots[0].id)
        live_manifest_id = int(live.depots[0].manifest)
        live_dir = _work_dir(cfg) / f"manifests_{live.path_id}"
        depot.fetch_manifest(cfg, live, live_dir)  # ensure the live binary manifest exists
        live_binary = live_dir / ".DepotDownloader" / f"{depot_id}_{live_manifest_id}.manifest"
        if not live_binary.is_file():
            print("Chunk delta: live binary manifest not found; downloading full files.")
            return 0
        seeded = deltaseed.prepare_seed(
            cfg.steam_install, depot_id, live_manifest_id, live_binary, to_download,
            target_dir, cancel=cancel,
        )
        print(
            f"Chunk delta: previous build {live.name} (manifest {live_manifest_id}); "
            f"seeded {seeded}/{len(to_download)} live files as chunk sources."
        )
        return seeded
    except DownloadCancelled:
        raise  # a user cancel is not an optimisation failure — let it propagate
    except SteamAuthError:
        raise  # an expired/dead Steam login is real, not an optimisation failure —
        # surface it so the panel clears the stale login and prompts a reconnect,
        # rather than falling through to a full download that hits the same wall.
    except Exception as exc:  # noqa: BLE001 - chunk reuse is an optimisation, never fatal
        print(f"Chunk delta seed skipped ({exc}); downloading full files.")
        return 0


def _download_and_store(
    cfg: Config,
    build: buildmap.Build,
    records: list[manifest.ManifestRecord],
    to_download: list[str],
    target_dir: Path,
    report: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Download files into ``target_dir`` and persist+index them in restic."""
    # Seeding copies the live files on disk first (no network yet), so report a
    # distinct "seed" phase — showing "downloading 0%" during a disk copy is wrong.
    if report:
        report("seed", None)
    _delta_seed(cfg, build, to_download, target_dir, cancel=cancel)
    dl_progress = (lambda p: report("download", p)) if report else None
    depot.download_files(cfg, build, target_dir, to_download, progress=dl_progress, cancel=cancel)
    # The download is finished; verify + restic store can't be cancelled, so move
    # off the "download" phase (this hides the Cancel button — see _play_progress).
    if report:
        report("store", None)
    # Chunk reuse must never store a wrong byte: verify before restic sees anything.
    deltaseed.verify_downloads(records, to_download, target_dir)
    deltaseed.cleanup(target_dir)
    print(f"Storing {len(to_download)} downloaded file(s) in restic...")
    resticrepo.backup_delta(cfg, build.build_id, target_dir)
    index = ContentIndex(_content_index_path(cfg))
    index.load()
    _index_stored(cfg, index, build.build_id, target_dir, {r.path: r for r in records}, to_download)
    index.save()


def acquire_build(
    cfg: Config, build: buildmap.Build, records: list[manifest.ManifestRecord]
) -> None:
    """Ensure every file this build needs is in the live install or the store.

    Verifies by content (not has_build), so a corrupt/incomplete cached build is
    repaired: only files present in neither the seed nor the index are fetched.
    """
    resticrepo.ensure_repo(cfg)
    print("Checking the build against the live install and the store...")
    _reuse, to_download, from_seed = _plan_sources(cfg, records)
    print(f"{from_seed} from live install, to download: {len(to_download)}.")
    if to_download:
        run = uuid.uuid4().hex[:8]
        delta_dir = _prepare_dir(_launch_work(cfg) / f"acquire_{build.path_id}_{run}")
        try:
            _download_and_store(cfg, build, records, to_download, delta_dir)
        finally:
            _force_rmtree(delta_dir)  # never leave a partial download behind on failure


def _prepare_dir(path: Path) -> Path:
    _force_rmtree(path)
    path.mkdir(parents=True)
    return path


def add_build(cfg: Config, replay_path: Path) -> buildmap.Build:
    """Resolve + acquire the build for a replay, without launching."""
    stamp = replay.read_timestamp_optional(replay_path)  # version-first; date is a fallback
    build = _build_for_replay(cfg, replay_path, stamp)
    label = stamp.text if stamp else f"version {replay.read_version(replay_path)}"
    print(f"Replay {label} -> build {build.name} ({build.build_id})")

    manifests_dir = _work_dir(cfg) / f"manifests_{build.path_id}"
    manifest_path = depot.fetch_manifest(cfg, build, manifests_dir)
    records = manifest.parse_manifest(manifest_path)
    if not records:
        raise RuntimeError(f"No file records parsed from manifest: {manifest_path}")

    acquire_build(cfg, build, records)
    return build


def reindex(cfg: Config) -> int:
    """Rebuild the content index by hashing the actual stored content.

    Hashing real bytes (not the manifest) makes the index complete and accurate
    regardless of build-map tags, manifest availability, or any manifest/content
    mismatch. A snapshot whose source folder is still on disk is hashed in place;
    otherwise it is restored to a temp dir, hashed, then removed. Rebuilt from
    scratch each run so stale entries cannot linger.
    """
    resticrepo.ensure_repo(cfg)
    index_path = _content_index_path(cfg)
    # Build a fresh index in memory and let save() replace the file atomically at
    # the very end. (Don't unlink up front: if the pass crashes, the previous
    # index must survive rather than leave the store invisible until a manual rerun.)
    index = ContentIndex(index_path)
    hc = HashCache(_work_dir(cfg) / "content-hash-cache.tsv")
    hc.load()
    work = _launch_work(cfg)

    for snap in resticrepo.all_snapshots(cfg):
        paths = snap.get("paths") or []
        if not paths:
            continue
        short = snap["short_id"]
        prefix = resticrepo.stored_form(paths[0])
        source = Path(paths[0])

        restored: Path | None = None
        if source.is_dir():
            root = source
        else:
            restored = _prepare_dir(work / f"reindex_{short}")
            root = resticrepo.restore_snapshot(cfg, short, restored, paths[0])
            if root is None:
                _force_rmtree(restored)
                print(f"  {short}: empty, skipped")
                continue

        count = 0
        for file in root.rglob("*"):
            if not file.is_file() or ".DepotDownloader" in file.parts:
                continue
            if file.name == "_filelist.txt":
                continue
            rel = file.relative_to(root).as_posix()
            index.add(file.stat().st_size, hc.sha1(file), short, f"{prefix}/{rel}")
            count += 1
        print(f"  {short}: indexed {count} files")
        if restored is not None:
            _force_rmtree(restored)

    hc.save()
    index.save()
    print(f"Content index now holds {len(index)} unique files.")
    return len(index)


def ingest_cache(cfg: Config, cache_dir: Path) -> int:
    """Import every ``delta_*`` folder under ``cache_dir`` into the restic store.

    The source cache is read only; existing snapshots are skipped. Returns the
    number of builds newly stored.
    """
    cache_dir = Path(cache_dir)
    deltas = sorted(p for p in cache_dir.iterdir() if p.is_dir() and p.name.startswith("delta_"))
    if not deltas:
        print(f"No delta_* folders found under {cache_dir}.")
        return 0

    resticrepo.ensure_repo(cfg)
    stored = 0
    newly_stored: list[tuple[Path, str]] = []
    for delta in deltas:
        build_id = delta.name[len("delta_") :]
        if resticrepo.has_build(cfg, build_id):
            print(f"  skip {build_id} (already stored)")
            continue
        print(f"  storing {build_id} from {delta}")
        resticrepo.backup_delta(cfg, build_id, delta)
        stored += 1
        newly_stored.append((delta, build_id))
    print(f"Ingest complete. Newly stored builds: {stored}")
    if newly_stored:
        _index_ingested(cfg, newly_stored)
    return stored


def _index_ingested(cfg: Config, items: list[tuple[Path, str]]) -> None:
    """Add just-ingested builds to the content index so ``_plan_sources`` can reuse
    them (otherwise their files are re-downloaded).

    Indexes from each build's on-disk ``delta_*`` directory directly — no restic
    restore and no full ``reindex`` — so it never unlinks/rebuilds the existing
    index (it only loads, adds, and saves), and a problem with one build can't
    blank the whole index."""
    print("Indexing the ingested builds so they can be reused (no re-download)...")
    snaps_by_id = {s.build_id: s for s in resticrepo.list_builds(cfg)}
    index = ContentIndex(_content_index_path(cfg))
    index.load()
    hc = HashCache(_work_dir(cfg) / "content-hash-cache.tsv")
    hc.load()
    for delta, build_id in items:
        snap = snaps_by_id.get(build_id)
        if snap is None or not snap.paths:
            continue  # empty delta -> no snapshot was created -> nothing to index
        prefix = resticrepo.stored_form(snap.paths[0])
        for file in delta.rglob("*"):
            if not file.is_file() or ".DepotDownloader" in file.parts:
                continue
            if file.name == "_filelist.txt":
                continue
            rel = file.relative_to(delta).as_posix()
            index.add(file.stat().st_size, hc.sha1(file), snap.short_id, f"{prefix}/{rel}")
    hc.save()
    index.save()


def _is_current_build(cfg: Config, replay_path: Path) -> bool:
    """True if the replay was recorded on the currently installed build, so it can
    play on the live install with no download, build map or reconstruction."""
    replay_version = replay.read_version(replay_path)
    installed = launch.installed_game_version(cfg)
    return replay_version is not None and installed is not None and replay_version == installed


def replay_build_is_saved_locally(cfg: Config, replay_path: Path) -> bool:
    """True if the replay resolves (by exe version, no network) to a build the user
    has *saved* and whose composed dir is present — so it plays offline with no
    Steam connection. Conservative: only the version path (never a date/aoe4world
    lookup), so an offline check can't block on the network."""
    version = replay.read_version(replay_path)
    if not version:
        return False
    build = buildmap.build_for_version(version, buildmap.load_build_map(_build_map_path(cfg)))
    if build is None:
        return False
    return buildcache.is_saved(cfg, build.build_id) and buildcache.build_dir(
        cfg, build.build_id
    ).is_dir()


def steam_login(cfg: Config, username: str, password: str, on_2fa, cancel=None) -> None:
    """Sign in to Steam from the UI (no console), caching the credential. The
    password is supplied directly; Steam Guard (phone push / authenticator /
    email) is surfaced through ``on_2fa`` so the UI can guide the user."""
    build = resolve_build(cfg, datetime.now())
    depot.steam_login(cfg, build, username, password, on_2fa, cancel)


def steam_login_qr(cfg: Config, on_qr, on_approved=None, cancel=None) -> str | None:
    """Sign in to Steam by QR from the UI (no password). ``on_qr`` receives the
    QR module matrix to display; returns the signed-in account name."""
    build = resolve_build(cfg, datetime.now())
    return depot.steam_login_qr(cfg, build, on_qr, on_approved, cancel)


# Build ids composed in this process. A panel session reuses them across replays
# without re-verifying; a fresh process (CLI one-shot) starts empty.
_SESSION_BUILT: set[str] = set()


def _build_is_ready(
    cfg: Config,
    build_id: str,
    target_dir: Path,
    records: list[manifest.ManifestRecord],
    report: Callable[[str, float | None], None],
) -> bool:
    """True if the cached composed build at ``target_dir`` can be reused as-is.

    Trusted without checks if it was composed in this session; a *saved* build
    from an earlier session is reused only after a cheap size check, and on a
    mismatch the user is told it is being rebuilt.
    """
    if not target_dir.is_dir():
        return False
    if build_id in _SESSION_BUILT:
        return True
    if buildcache.is_saved(cfg, build_id):
        if buildcache.verify_sizes(records, target_dir):
            _SESSION_BUILT.add(build_id)  # verified once this session -> trust further reuse
            return True
        print(f"Saved build {build_id} is out of date; rebuilding it.")
        report("rebuild", None)
    return False


def _reconstruct_build(
    cfg: Config,
    build: buildmap.Build,
    records: list[manifest.ManifestRecord],
    target_dir: Path,
    report: Callable[[str, float | None], None],
    cancel: threading.Event | None = None,
) -> None:
    """Download/restore/compose the build into ``target_dir`` (wiping any old one)."""
    _clean_stale_workdirs(cfg)
    work = _launch_work(cfg)
    run = uuid.uuid4().hex[:8]
    resticrepo.ensure_repo(cfg)

    # Decide where every manifest file comes from, trusting nothing by path: the
    # live install (SHA-1 verified), the content index, or a download.
    print("Planning the launch build (live install / store / download)...")
    reuse_by_snap, to_download, from_seed = _plan_sources(cfg, records)
    reused = sum(len(v) for v in reuse_by_snap.values())
    print(
        f"{from_seed} from live install, {reused} from the store, "
        f"{len(to_download)} to download."
    )

    extra_dirs: list[Path] = []
    if to_download:
        print(f"Downloading {len(to_download)} file(s) not in the store...")
        download_dir = _prepare_dir(work / f"download_{build.path_id}_{run}")
        try:
            _download_and_store(
                cfg, build, records, to_download, download_dir, report=report, cancel=cancel
            )
        except BaseException:
            # On a cancel (or any download error) don't leave a partial download dir.
            _force_rmtree(download_dir)
            raise
        extra_dirs.append(download_dir)

    # Restore + compose form the "build" phase shown in the panel.
    report("build", 0.0)
    if reuse_by_snap:
        print(f"Restoring {reused} file(s) from the store (no download)...")
        reuse_dir = _prepare_dir(work / f"reuse_{build.path_id}_{run}")
        snaps = list(reuse_by_snap.items())
        total_snaps = len(snaps)
        for i, (snap, items) in enumerate(snaps):
            raw = _prepare_dir(work / f"reuseraw_{snap}_{run}")
            # The restore is the slow part of the build phase; stream it as 0-85%
            # so the panel isn't stuck at 0 until it suddenly jumps to 100.
            restored = resticrepo.restore_paths(
                cfg, snap, [sp for sp, _ in items], raw,
                progress=lambda p, i=i, n=total_snaps: report(
                    "build", (i + p / 100.0) / n * 85.0
                ),
            )
            for stored_path, record in items:
                src = restored.get(stored_path) or (raw / stored_path.lstrip("/"))
                # A restic restore can fail (or extract nothing) and still return
                # cleanly; if a missing file slipped through, compose would silently
                # fall back to the live install's wrong same-name file. Refuse loudly.
                if not src.is_file() or src.stat().st_size != record.size:
                    raise RuntimeError(
                        f"Restore from the store failed for {record.path!r} "
                        f"(missing or wrong size). The store snapshot may be "
                        f"incomplete; refusing to assemble a build from wrong files."
                    )
                target = reuse_dir / record.path
                target.parent.mkdir(parents=True, exist_ok=True)
                compose.link_or_copy(src, target)
            _force_rmtree(raw)
        extra_dirs.append(reuse_dir)

    _prepare_dir(target_dir)  # wipe any stale/partial cached build, then compose fresh
    print("Composing launch build...")
    base = 85.0 if reuse_by_snap else 0.0
    count = compose.compose_launch_build(
        cfg, records, extra_dirs, target_dir,
        progress=lambda p: report("build", base + p * (100.0 - base) / 100.0),
    )
    print(f"Materialised {count} files into {target_dir}")

    # The download/restore sources are no longer needed; drop them (the composed
    # build holds independent hard links).
    for extra in extra_dirs:
        _force_rmtree(extra)


def _runasdate_when(stamp: replay.ReplayStamp | None, build: buildmap.Build) -> datetime:
    """The date to fake for RunAsDate. The game only checks that the date is inside
    the build's validity window, so clamp to it: a replay timestamp that parsed to
    a wrong year (a non-Gregorian locale) would otherwise be used verbatim and the
    old build would reject it."""
    when = stamp.value if stamp else (build.valid_from or datetime.now())
    if build.valid_from and when < build.valid_from:
        when = build.valid_from
    if build.valid_to and when > build.valid_to:
        when = build.valid_to
    return when


def watch_replay(
    cfg: Config,
    replay_path: Path,
    no_launch: bool = False,
    progress: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
) -> str | None:
    """Full flow: reconstruct the matching build and play the replay.

    ``progress`` (if given) reports the current phase and percent for the panel:
    ``("download", 0-100)`` while files are fetched, ``("build", 0-100)`` while
    the launch build is composed, and a ``None`` percent clears it.

    Returns the reconstructed build id that was launched (so the panel can offer
    to keep it), or ``None`` for the current-build fast path and ``--no-launch``.
    """

    def _report(stage: str, pct: float | None) -> None:
        if progress:
            progress(stage, pct)
    if not no_launch and launch.is_game_running():
        raise RuntimeError(
            "Age of Empires IV (RelicCardinal.exe) is already running. "
            "Close it before launching a replay build."
        )
    if not no_launch:
        # fail fast (before any download) if Steam isn't open and signed in
        launch.ensure_steam_running(cfg)
    # Resolve by exe version first (locale-independent); the timestamp is only a
    # fallback, so a replay whose locale formats the date in an unparseable (e.g.
    # non-Gregorian) calendar must not block resolution — read it optionally.
    stamp = replay.read_timestamp_optional(replay_path)

    # Fast path: a replay recorded on the build that is currently installed needs
    # no download or reconstruction — play it straight on the live install (no
    # RunAsDate either, since a current build has no expired date check).
    if _is_current_build(cfg, replay_path):
        print("Replay matches the installed build; playing it directly (no reconstruction).")
        playback = replay.copy_to_playback(replay_path, cfg.playback_dir)
        if no_launch:
            print(f"Playback replay: {playback.path}")
            return
        try:
            # Launch through Steam (not a direct spawn): a directly spawned game runs
            # without a full Steam session and crashes mid-match on long replays.
            launch.launch_replay_via_steam(cfg, playback.name)
        finally:
            if playback.delete_after_launch and playback.path.is_file():
                playback.path.unlink()
        return

    build = _build_for_replay(cfg, replay_path, stamp)
    label = stamp.text if stamp else f"version {replay.read_version(replay_path)}"
    print(f"Replay {label} -> build {build.name} ({build.build_id})")

    manifests_dir = _work_dir(cfg) / f"manifests_{build.path_id}"
    manifest_path = depot.fetch_manifest(cfg, build, manifests_dir)
    records = manifest.parse_manifest(manifest_path)
    if not records:
        raise RuntimeError(f"No file records parsed from manifest: {manifest_path}")

    # Reuse the composed build if it's cached this session or a verified saved one;
    # otherwise build it into the stable per-build cache directory.
    target_dir = buildcache.build_dir(cfg, build.build_id)
    if _build_is_ready(cfg, build.build_id, target_dir, records, _report):
        print(f"Reusing cached build at {target_dir} (no rebuild).")
    else:
        _reconstruct_build(cfg, build, records, target_dir, _report, cancel=cancel)
        _SESSION_BUILT.add(build.build_id)
    _report("build", None)  # clear the build progress before the game launches

    playback = replay.copy_to_playback(replay_path, cfg.playback_dir, build.replay_version_override)

    if no_launch:
        print(f"--no-launch set. Build left at: {target_dir}")
        print(f"Playback replay: {playback.path}")
        return

    try:
        launch.launch_replay(cfg, target_dir, playback.name, _runasdate_when(stamp, build))
    finally:
        # Keep the composed build — it's the cache, reused next time and cleaned by
        # buildcache. Only the temporary playback copy is dropped here.
        if playback.delete_after_launch and playback.path.is_file():
            playback.path.unlink()
    return build.build_id
