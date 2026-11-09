"""Which user mods a replay needs, and whether they are installed.

A replay is only playable on the content it was recorded with. Custom games in
particular are often played on a community map or game mode that ships as a
*mod*, and a replay that references one cannot be watched without it.

The game records that dependency inside the ``.rec`` as ``<guid>:<name>`` pairs:

* the map / scenario, in the ``SDSC`` chunk right after ``map_size_*``;
* the game mode, in the settings block after ``reveal_fow_on_elimination``.

Base-game content uses an all-zero guid (``000…000:149419911``), so a *non-zero*
guid is exactly "this came from a mod". Installed mods carry the matching id in
their SGA header: 32 hex characters as UTF-16 at offset ``0x0C``. That id is
stable across mod versions — two subscriptions of the same mod (different
archives, different bytes) share it — so comparing guids answers "is the mod
installed?" without caring which version the player subscribed to.

Everything here is read-only and best-effort: a replay or archive that cannot be
parsed simply reports nothing rather than raising, because a failure to detect a
mod must never block an otherwise fine playback.
"""

from __future__ import annotations

import contextlib
import gzip
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_BASE_GAME_GUID = "0" * 32
_CONTENT_REF = re.compile(rb"([0-9a-f]{32}):([A-Za-z0-9_]{1,48})")
# Only the head of a replay holds the scenario/settings blocks; stopping there
# keeps a long match's multi-megabyte command stream out of the scan.
_SCAN_BYTES = 300_000
_SGA_GUID_AT = 0x0C
_SGA_GUID_LEN = 0x40  # 32 chars, UTF-16


@dataclass(frozen=True)
class ModRequirement:
    """A piece of non-base-game content a replay depends on."""

    guid: str
    name: str

    @property
    def kind(self) -> str:
        # The mode slot stores a numeric id; a map/scenario stores a name.
        return "game mode" if self.name.isdigit() else "map"


@dataclass(frozen=True)
class InstalledMod:
    guid: str
    subscription: str  # the mod browser's folder id, e.g. "311265"
    archive: Path


def _replay_head(path: Path) -> bytes:
    """The start of a replay, transparently un-gzipping the downloaded form."""
    data = Path(path).read_bytes()
    if data[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError, EOFError, gzip.BadGzipFile):
            data = gzip.decompress(data)
    return data[:_SCAN_BYTES]


def required_mods(path: Path) -> list[ModRequirement]:
    """Mod content this replay references, in file order and de-duplicated.

    An empty list means the replay is plain vanilla and plays on a clean install.
    """
    try:
        head = _replay_head(path)
    except OSError:
        return []
    found: list[ModRequirement] = []
    seen: set[tuple[str, str]] = set()
    for raw_guid, raw_name in _CONTENT_REF.findall(head):
        guid, name = raw_guid.decode(), raw_name.decode()
        if guid == _BASE_GAME_GUID or (guid, name) in seen:
            continue
        seen.add((guid, name))
        found.append(ModRequirement(guid=guid, name=name))
    return found


def _sga_guid(archive: Path) -> str | None:
    """The mod id stored in an SGA header, or None if this isn't a usable one."""
    try:
        with archive.open("rb") as handle:
            header = handle.read(_SGA_GUID_AT + _SGA_GUID_LEN)
    except OSError:
        return None
    if not header.startswith(b"_ARCHIVE"):
        return None
    raw = header[_SGA_GUID_AT:_SGA_GUID_AT + _SGA_GUID_LEN]
    with contextlib.suppress(UnicodeDecodeError):
        guid = raw.decode("utf-16-le").rstrip("\x00").strip().lower()
        if len(guid) == 32 and all(c in "0123456789abcdef" for c in guid):
            return guid
    return None


def installed_mods(cfg: Config) -> list[InstalledMod]:
    """Every subscribed/local mod archive currently in the user's mods folder."""
    root = cfg.mods_dir
    if not root.exists():
        return []
    mods: list[InstalledMod] = []
    with contextlib.suppress(OSError):
        for archive in sorted(root.rglob("*.sga")):
            guid = _sga_guid(archive)
            if guid:
                mods.append(
                    InstalledMod(
                        guid=guid, subscription=archive.parent.name, archive=archive
                    )
                )
    return mods


def missing_mods(path: Path, cfg: Config) -> list[ModRequirement]:
    """Required content the user has *not* installed — what a playback would lack."""
    needed = required_mods(path)
    if not needed:
        return []
    have = {mod.guid for mod in installed_mods(cfg)}
    return [req for req in needed if req.guid not in have]


def needs_mods(path: Path) -> bool:
    """True if the replay depends on any mod at all (installed or not).

    This is the signal for *how to launch*: a modded replay must keep the user's
    mods in place, where a vanilla one is safest with them temporarily disabled.
    """
    return bool(required_mods(path))
