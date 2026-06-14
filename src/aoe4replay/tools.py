"""Locating and auto-downloading the external binaries this tool drives.

restic and DepotDownloader are fetched from their GitHub releases. RunAsDate is
fetched as the complete, unmodified x64 package from NirSoft. All tools are
downloaded on demand into ``tools/`` (gitignored).
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .config import Config

_UA = {"User-Agent": "aoe4-replay-launcher"}
_RUNASDATE_URL = "https://www.nirsoft.net/utils/runasdate-x64.zip"
_RUNASDATE_FILES = ("RunAsDate.exe", "RunAsDate.chm", "readme.txt")
# Pinned so the Steam Guard / QR console strings we parse in depot.py stay stable;
# bump deliberately and re-verify those strings against the new binary.
_DEPOTDOWNLOADER_TAG = "DepotDownloader_3.4.0"
# Pinned so a fresh install gets the exact restic we test against, rather than
# whatever "latest" happens to be (a new release could change the --json output
# or console strings we parse). Bump deliberately and re-verify against it.
_RESTIC_TAG = "v0.19.0"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)  # noqa: S310 (trusted GitHub URLs)
    # Generous socket timeout: a stalled connection fails instead of hanging the
    # first-run setup forever, while a slow-but-progressing download is not cut
    # (the timeout is per socket operation, not a total deadline).
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        return resp.read()


def _asset_url(repo: str, name_contains: str, tag: str | None = None) -> str:
    where = f"releases/tags/{tag}" if tag else "releases/latest"
    data = json.loads(_http_get(f"https://api.github.com/repos/{repo}/{where}"))
    for asset in data.get("assets", []):
        if name_contains in asset["name"]:
            return asset["browser_download_url"]
    raise RuntimeError(f"No release asset matching {name_contains!r} found for {repo}.")


def _download_and_extract_exe(url: str, exe_glob: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp names so two instances racing the same first-run download don't
    # collide; write the exe via a temp + atomic replace so an interrupted write
    # never leaves a truncated file that ensure_*() would later treat as installed.
    token = uuid.uuid4().hex
    zip_path = dest.parent / f"_dl_{token}.zip"
    zip_path.write_bytes(_http_get(url))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.namelist() if Path(m).match(exe_glob)]
            if not members:
                raise RuntimeError(f"No {exe_glob} inside {url}")
            tmp_exe = dest.with_name(f"{dest.name}.{token}.part")
            with zf.open(members[0]) as src:
                tmp_exe.write_bytes(src.read())
            os.replace(tmp_exe, dest)
    finally:
        zip_path.unlink(missing_ok=True)
    return dest


def _download_runasdate(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    zip_path = dest.parent / f"_runasdate_{token}.zip"
    zip_path.write_bytes(_http_get(_RUNASDATE_URL))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = {info.filename: info for info in zf.infolist()}
            missing = [name for name in _RUNASDATE_FILES if name not in members]
            unsafe = [name for name in members if Path(name).name != name]
            if missing or unsafe:
                raise RuntimeError("The RunAsDate download has an unexpected package layout.")

            for name in _RUNASDATE_FILES:
                target = dest if name == "RunAsDate.exe" else dest.parent / name
                tmp = target.with_name(f"{target.name}.{token}.part")
                with zf.open(members[name]) as src:
                    tmp.write_bytes(src.read())
                os.replace(tmp, target)  # atomic: never leave a truncated tool file
    finally:
        zip_path.unlink(missing_ok=True)
    return dest


def ensure_restic(cfg: Config) -> Path:
    if cfg.restic.is_file():
        return cfg.restic
    url = _asset_url("restic/restic", "windows_amd64.zip", tag=_RESTIC_TAG)
    return _download_and_extract_exe(url, "restic_*_windows_amd64.exe", cfg.restic)


def ensure_depotdownloader(cfg: Config) -> Path:
    if cfg.depotdownloader.is_file():
        return cfg.depotdownloader
    url = _asset_url("SteamRE/DepotDownloader", "windows-x64.zip", tag=_DEPOTDOWNLOADER_TAG)
    return _download_and_extract_exe(url, "DepotDownloader.exe", cfg.depotdownloader)


def ensure_runasdate(cfg: Config) -> Path:
    if cfg.runasdate.is_file():
        return cfg.runasdate
    return _download_runasdate(cfg.runasdate)
