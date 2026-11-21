"""Make DepotDownloader fetch only changed *chunks*, not whole changed files.

DepotDownloader already does Steam-style chunk-level delta updates, but only when
it finds a *previous install* in the target directory:

* ``.DepotDownloader/depot.config`` — names the installed manifest per depot,
* ``.DepotDownloader/<depot>_<manifest>.manifest`` — that manifest's binary form,
* the old files themselves on disk.

It then matches the new manifest's chunks against the old manifest by ChunkID,
copies the bytes it can from the local files, and downloads only the missing
chunks. Our download directory is normally empty, so none of that fires and whole
changed files come down the wire (~10 GB for a far build).

This module fabricates that previous-install state from the *live* game install:
it seeds the same-path live files, drops in the live build's binary manifest, and
writes a ``depot.config`` pointing at the live build's real manifest id. The live
game is the chunk source; only the genuine delta is downloaded (~2 GB).

Everything here is best-effort and read-only against the live install: a normal
copy is used (never a hardlink, so DepotDownloader can never write through to the
live game), paths are traversal-checked, and a failure just falls back to a full
download. Correctness is guaranteed downstream by ``verify_downloads`` (size +
SHA-1 against the target manifest) before anything is stored.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import zlib
from collections.abc import Callable
from pathlib import Path

from .depot import DownloadCancelled
from .manifest import ManifestRecord

_CONFIG_DIR = ".DepotDownloader"

#: Read/write unit for the copy and hash loops. Large enough that the syscall
#: overhead stays invisible next to the disk, small enough that a cancel is
#: noticed and the progress line moves several times a second even on one
#: multi-gigabyte archive.
_BLOCK = 4 << 20


# --- depot.config (DepotConfigStore: protobuf map<depot,manifest> + raw deflate) ---

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def depot_config_bytes(depot_id: int, manifest_id: int) -> bytes:
    """Serialise ``{depot_id: manifest_id}`` exactly as DepotDownloader's
    ``DepotConfigStore`` does: a protobuf ``map<uint,ulong>`` (field 1, entries of
    {1:key, 2:value}) wrapped in raw DEFLATE (.NET ``DeflateStream``)."""
    entry = b"\x08" + _varint(depot_id) + b"\x10" + _varint(manifest_id)
    message = b"\x0a" + _varint(len(entry)) + entry  # field 1, length-delimited entry
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)  # raw deflate, no zlib header
    return compressor.compress(message) + compressor.flush()


# --- seeding -------------------------------------------------------------------

def _is_safe_relpath(relpath: str) -> bool:
    """Reject anything that wouldn't stay under the join root.

    Covers absolute paths, Windows drive-relative (``C:x``) and root-relative
    (``/x``) paths — which ``is_absolute()`` alone misses on Windows — and any
    ``..`` traversal component.
    """
    p = Path(relpath)
    return not (p.is_absolute() or p.drive or p.root) and ".." not in p.parts


def _copy_block_wise(
    src: Path,
    dst: Path,
    cancel: threading.Event | None,
    on_block: Callable[[int], None] | None,
) -> None:
    """``shutil.copy2`` split into blocks, so one huge archive is not an opaque
    multi-minute stall: progress moves inside the file and a cancel is honoured
    inside it too, instead of only between files."""
    with src.open("rb") as reader, dst.open("wb") as writer:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Download cancelled.")
            block = reader.read(_BLOCK)
            if not block:
                break
            writer.write(block)
            if on_block is not None:
                on_block(len(block))
    shutil.copystat(src, dst)  # same metadata copy2 would have made


def prepare_seed(
    steam_install: Path,
    depot_id: int,
    live_manifest_id: int,
    live_binary_manifest: Path,
    to_download: list[str],
    target_dir: Path,
    cancel: threading.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Fabricate the previous-install state in ``target_dir``. Returns the number
    of live files seeded (chunk sources); 0 means a plain full download follows.

    A set ``cancel`` event aborts the (potentially large) live-file copy with
    ``DownloadCancelled`` so the Cancel button works during the seed phase too.

    This copy is the longest stretch of the whole flow with nothing to show for
    itself — 10-18 GB for a build a couple of months old — so it is measured:
    ``on_progress(copied_bytes, total_bytes)`` is called as it runs, including
    once up front with ``(0, total)`` so the size is on screen immediately.
    """
    config = target_dir / _CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)

    shutil.copy2(live_binary_manifest, config / live_binary_manifest.name)
    sha = Path(f"{live_binary_manifest}.sha")
    if sha.is_file():
        shutil.copy2(sha, config / sha.name)
    (config / "depot.config").write_bytes(depot_config_bytes(depot_id, live_manifest_id))

    # Decide what is copyable first (a cheap stat pass) so the work can be
    # reported as a fraction of a known total rather than an open-ended count.
    plan: list[tuple[Path, Path, int]] = []
    for relpath in to_download:
        if not _is_safe_relpath(relpath):
            continue
        src = steam_install / relpath
        if not src.is_file():
            continue  # new file with no live counterpart -> downloaded in full
        try:
            size = src.stat().st_size
        except OSError:
            continue
        plan.append((src, target_dir / relpath, size))

    total = sum(size for _, _, size in plan)
    copied = 0

    def _on_block(size: int) -> None:
        nonlocal copied
        copied += size
        if on_progress is not None:
            on_progress(copied, total)

    if on_progress is not None:
        on_progress(0, total)  # show the size before the first block lands

    seeded = 0
    for src, dst, _size in plan:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Download cancelled.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # A normal copy, never a hardlink: the live install is only ever read.
        _copy_block_wise(src, dst, cancel, _on_block if on_progress else None)
        seeded += 1
    return seeded


# --- verification + cleanup ----------------------------------------------------

def _sha1(path: Path, on_block: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha1()  # noqa: S324 - matching Steam's manifest checksums, not security
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_BLOCK), b""):
            h.update(block)
            if on_block is not None:
                on_block(len(block))
    return h.hexdigest()


def verify_downloads(
    records: list[ManifestRecord],
    to_download: list[str],
    target_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Check every downloaded file's size and SHA-1 against the target manifest.

    Chunk reuse must never put a wrong byte into the store, so this runs before
    restic and raises on the first mismatch (DepotDownloader's exit code alone is
    not trusted).

    Hashing the whole delta is minutes of pure disk read, so it is measured the
    same way the copy is, through ``on_progress(hashed_bytes, total_bytes)``.
    There is no cancel hook on purpose — the download is already paid for at
    this point, and throwing it away is the worst possible moment to let a
    stray click do so.
    """
    by_path = {r.path: r for r in records}
    total = sum(by_path[p].size for p in to_download if p in by_path)
    hashed = 0

    def _on_block(size: int) -> None:
        nonlocal hashed
        hashed += size
        if on_progress is not None:
            on_progress(hashed, total)

    if on_progress is not None:
        on_progress(0, total)
    for relpath in to_download:
        record = by_path[relpath]
        path = target_dir / relpath
        if not path.is_file():
            raise RuntimeError(f"Downloaded file is missing: {relpath}")
        size = path.stat().st_size
        if size != record.size:
            raise RuntimeError(
                f"Size mismatch for {relpath}: got {size}, manifest {record.size}."
            )
        if _sha1(path, _on_block if on_progress else None) != record.sha1:
            raise RuntimeError(
                f"SHA-1 mismatch for {relpath}; the download is corrupt, refusing to store."
            )


def cleanup(target_dir: Path) -> None:
    """Drop DepotDownloader's working files so only real game files reach restic."""
    shutil.rmtree(target_dir / _CONFIG_DIR, ignore_errors=True)
    (target_dir / "_filelist.txt").unlink(missing_ok=True)
