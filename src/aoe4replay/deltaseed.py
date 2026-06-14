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
from pathlib import Path

from .depot import DownloadCancelled
from .manifest import ManifestRecord

_CONFIG_DIR = ".DepotDownloader"


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


def prepare_seed(
    steam_install: Path,
    depot_id: int,
    live_manifest_id: int,
    live_binary_manifest: Path,
    to_download: list[str],
    target_dir: Path,
    cancel: threading.Event | None = None,
) -> int:
    """Fabricate the previous-install state in ``target_dir``. Returns the number
    of live files seeded (chunk sources); 0 means a plain full download follows.

    A set ``cancel`` event aborts the (potentially large) live-file copy with
    ``DownloadCancelled`` so the Cancel button works during the seed phase too."""
    config = target_dir / _CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)

    shutil.copy2(live_binary_manifest, config / live_binary_manifest.name)
    sha = Path(f"{live_binary_manifest}.sha")
    if sha.is_file():
        shutil.copy2(sha, config / sha.name)
    (config / "depot.config").write_bytes(depot_config_bytes(depot_id, live_manifest_id))

    seeded = 0
    for relpath in to_download:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Download cancelled.")
        if not _is_safe_relpath(relpath):
            continue
        src = steam_install / relpath
        if not src.is_file():
            continue  # new file with no live counterpart -> downloaded in full
        dst = target_dir / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)  # normal copy: the live install is only ever read
        seeded += 1
    return seeded


# --- verification + cleanup ----------------------------------------------------

def _sha1(path: Path) -> str:
    h = hashlib.sha1()  # noqa: S324 - matching Steam's manifest checksums, not security
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_downloads(
    records: list[ManifestRecord], to_download: list[str], target_dir: Path
) -> None:
    """Check every downloaded file's size and SHA-1 against the target manifest.

    Chunk reuse must never put a wrong byte into the store, so this runs before
    restic and raises on the first mismatch (DepotDownloader's exit code alone is
    not trusted)."""
    by_path = {r.path: r for r in records}
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
        if _sha1(path) != record.sha1:
            raise RuntimeError(
                f"SHA-1 mismatch for {relpath}; the download is corrupt, refusing to store."
            )


def cleanup(target_dir: Path) -> None:
    """Drop DepotDownloader's working files so only real game files reach restic."""
    shutil.rmtree(target_dir / _CONFIG_DIR, ignore_errors=True)
    (target_dir / "_filelist.txt").unlink(missing_ok=True)
