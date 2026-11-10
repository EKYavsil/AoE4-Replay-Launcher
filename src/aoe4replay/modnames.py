"""What a mod's guid is actually called, so a refusal can name it.

A replay identifies its mods by guid plus a server-assigned pack number, and
neither spells the mod's title. The guid cannot be decoded either: every one
observed is a version-4 (random) UUID, and no hash of the title, the publisher
id or any combination of them reproduces it. The title is simply not in the
replay — it has to come from somewhere else.

Three places have it, and they are tried cheapest first:

1. **The archives on disk.** A mod's own ``.sga`` carries its title in
   ``en.ucs`` line 1. Free and offline, and it covers mods that were once
   installed as well, since the disabled-mods backups are scanned too.
2. **What we learned before**, in a small tsv beside the other caches. Anything
   resolved once is never looked up again.
3. **Relic's community match API**, which the launcher already uses to fetch
   replays, and only when the first two tiers came up empty. A match record's
   compressed ``options`` blob lists the lobby's mod packs with their titles,
   publisher ids and guids. Asking for the replay's own match by id takes one
   request and works whatever the match's age; a replay whose id cannot be read
   falls back to scanning its players' recent histories, which costs two and
   reaches back only about ten custom games per player.

Nothing here is allowed to matter: a lookup that fails, times out or returns
nonsense leaves the caller showing the raw identifiers, exactly as it would
without this module. The refusal to launch is decided elsewhere and never waits
on an answer from here.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import struct
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import nethttp
from .config import Config

_UA = "aoe4-replay-launcher"
_COMMUNITY = "https://aoe-api.worldsedgelink.com/community/leaderboard"
# The same host the launcher already downloads replays from (see aoe4world.py),
# so this adds a method rather than a dependency. Undocumented either way: if it
# changes shape, the titles quietly stop appearing and nothing else breaks.
_PERSONAL_STAT = _COMMUNITY + "/getPersonalStat?title=age4&profile_names={names}"
_MATCH_HISTORY = _COMMUNITY + "/getRecentMatchHistory?title=age4&profile_ids={ids}"
# One match, addressed by its own id — no age limit and no player lookup first.
_MATCH_BY_ID = _COMMUNITY + "/getMatchHistory?title=age4&matchIDs=[{match_id}]"
_MOD_PAGE = "https://www.ageofempires.com/mods/details/{pub_id}/"

_CACHE_NAME = "mod-names.tsv"
_WORK_DIR = ".aoe4-work"
_DISABLED_MODS_DIR = ".aoe4-disabled-mods"
# A title arrives from a third party and is shown to the user verbatim; cap it
# and strip anything that would break out of its line.
_MAX_TITLE = 60
_CONTROL = re.compile(r"[\r\n\t\x00-\x1f]")

# --- SGA archive reading ------------------------------------------------------
# Offsets verified against installed v10/v11 archives. replaymods._sga_guid only
# needs the 76-byte header; pulling a file out needs the table of contents.
_SGA_MAGIC = b"_ARCHIVE"
_SGA_TOC_OFFSET_AT = 0x8C      # u64
_SGA_TOC_SIZE_AT = 0x94        # u32
_SGA_DATA_BASE_AT = 0x98       # u32
_TOC_FILES_AT = 16             # <IH> = (files_offset, file_count)
_TOC_NAMES_AT = 24             # <II> = (names_offset, names_size)
# name_off, parent, data_off, comp, uncomp, storage. The struct itself packs to
# 25 bytes, but entries are laid out on a 30-byte stride, so it is read with
# unpack_from and the stride is advanced separately.
_FILE_ENTRY = struct.Struct("<IIQIIB")
_FILE_ENTRY_SIZE = 30
_STORAGE_PLAIN = 1


@dataclass(frozen=True)
class ModName:
    """A mod's title as some source reported it."""

    guid: str
    name: str
    pub_id: str
    source: str  # "archive" | "match-api" | "cache"
    seen: int

    @property
    def page_url(self) -> str | None:
        return _MOD_PAGE.format(pub_id=self.pub_id) if self.pub_id else None


def _clean_title(text: object) -> str:
    return _CONTROL.sub(" ", str(text or "")).strip()[:_MAX_TITLE]


def _sga_file(archive: Path, wanted: str) -> bytes | None:
    """One file's bytes out of an SGA, by name, or None if it isn't there.

    Best-effort by design: a truncated, foreign or future-format archive simply
    yields nothing rather than raising into a caller that only wanted a label.
    """
    try:
        blob = Path(archive).read_bytes()
    except OSError:
        return None
    if not blob.startswith(_SGA_MAGIC) or len(blob) < _SGA_DATA_BASE_AT + 4:
        return None
    try:
        toc_offset = struct.unpack_from("<Q", blob, _SGA_TOC_OFFSET_AT)[0]
        toc_size = struct.unpack_from("<I", blob, _SGA_TOC_SIZE_AT)[0]
        data_base = struct.unpack_from("<I", blob, _SGA_DATA_BASE_AT)[0]
        toc = blob[toc_offset:toc_offset + toc_size]
        files_offset, file_count = struct.unpack_from("<IH", toc, _TOC_FILES_AT)
        names_offset, names_size = struct.unpack_from("<II", toc, _TOC_NAMES_AT)
        names = toc[names_offset:names_offset + names_size]
        for index in range(file_count):
            start = files_offset + index * _FILE_ENTRY_SIZE
            if start + _FILE_ENTRY.size > len(toc):
                return None
            name_off, _parent, data_off, packed, _unpacked, storage = _FILE_ENTRY.unpack_from(
                toc, start
            )
            end = names.find(b"\x00", name_off)
            name = names[name_off:end if end >= 0 else None].decode("utf-8", "replace")
            if name.lower() != wanted:
                continue
            raw = blob[data_base + data_off:data_base + data_off + packed]
            return raw if storage == _STORAGE_PLAIN else zlib.decompress(raw)
    except (struct.error, ValueError, zlib.error, IndexError):
        return None
    return None


def archive_display_name(archive: Path) -> str | None:
    """A mod's own title, from ``en.ucs`` line 1 (``"1\\tThe Berber Dynasties"``).

    Falls back to any other localisation file, so a mod shipped without English
    still gets a title rather than none.
    """
    blob = _sga_file(archive, "en.ucs")
    if blob is None:
        for candidate in ("tr.ucs", "de.ucs", "es.ucs", "fr.ucs"):
            blob = _sga_file(archive, candidate)
            if blob is not None:
                break
    if not blob:
        return None
    with contextlib.suppress(UnicodeDecodeError, ValueError):
        text = blob.decode("utf-16-le", "replace").lstrip("﻿")
        first = text.splitlines()[0] if text.splitlines() else ""
        # "<index>\t<title>" — take what follows the tab, not the index.
        title = _clean_title(first.split("\t", 1)[1] if "\t" in first else first)
        return title or None
    return None


# --- pure conversions ---------------------------------------------------------


def guid_from_arc_id(arc_id: object) -> str | None:
    """The 32-hex mod guid behind a match record's base64 ``arcID``.

    The bytes are a Microsoft GUID: the first three groups are little-endian and
    the last eight bytes are in order. Pure arithmetic, no network — this is not
    the part that needs the API.
    """
    try:
        raw = base64.b64decode(str(arc_id), validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != 16:
        return None
    return (raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]).hex()


def decode_options(raw: object) -> dict | None:
    """A match record's ``options`` blob: base64 -> zlib -> JSON.

    The decompressed buffer has trailing bytes after the closing brace, so
    ``json.loads`` fails on it with "Extra data"; the object is read off the
    front and the remainder ignored.
    """
    try:
        text = zlib.decompress(base64.b64decode(str(raw))).decode("utf-8", "replace")
        parsed, _end = json.JSONDecoder().raw_decode(text)
    except (ValueError, TypeError, zlib.error):
        return None
    return parsed if isinstance(parsed, dict) else None


def mod_packs(options: dict) -> list[ModName]:
    """Every mod pack a decoded lobby mentions, as titles keyed by guid."""
    helper = options.get("winConditionHelper")
    helper = helper if isinstance(helper, dict) else {}
    pack_options = helper.get("modPackOptions")
    packs = (pack_options or {}).get("modPacks") if isinstance(pack_options, dict) else None
    found: list[ModName] = []
    now = int(time.time())
    for pack in packs or []:
        if not isinstance(pack, dict):
            continue
        guid = guid_from_arc_id(pack.get("arcID"))
        title = _clean_title(pack.get("name"))
        if not guid or not title:
            continue
        pub_id = _CONTROL.sub("", str(pack.get("pubID") or "")).strip()
        found.append(
            ModName(
                guid=guid,
                name=title,
                pub_id=pub_id if pub_id.isdigit() else "",
                source="match-api",
                seen=now,
            )
        )
    return found


# --- the learned table --------------------------------------------------------


def cache_path(cfg: Config) -> Path:
    return cfg.project_root / _WORK_DIR / _CACHE_NAME


class NameCache:
    """guid -> title, persisted as a tab-separated file.

    One guid can legitimately carry more than one title: two subscriptions of
    the same mod (57415 and 70982, for instance) share a guid while naming
    themselves differently. The newest sighting wins — a title has to be chosen,
    and the most recent one is the most likely to match what the mod browser
    shows today.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, ModName] = {}
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            return
        with contextlib.suppress(OSError):
            for line in self.path.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) != 5:
                    continue  # tolerate a partially written / corrupt line
                guid, name, pub_id, source, seen = parts
                try:
                    stamp = int(seen)
                except ValueError:
                    continue
                self._entries[guid] = ModName(guid, name, pub_id, source, stamp)

    def save(self) -> None:
        if not self._dirty:
            return
        lines = [
            "\t".join((e.guid, e.name, e.pub_id, e.source, str(e.seen)))
            for e in sorted(self._entries.values(), key=lambda e: e.guid)
        ]
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._dirty = False

    def get(self, guid: str) -> ModName | None:
        return self._entries.get(guid)

    def put(self, entry: ModName) -> None:
        current = self._entries.get(entry.guid)
        if current is not None:
            if entry.seen < current.seen:
                return  # an older sighting never displaces a newer title
            if (entry.name, entry.pub_id) == (current.name, current.pub_id):
                return  # nothing new to record, so nothing to rewrite
        self._entries[entry.guid] = entry
        self._dirty = True

    def __len__(self) -> int:
        return len(self._entries)


# --- how a missing requirement is worded --------------------------------------
#
# Both surfaces format from here, so the console line and the dialog block can
# never drift apart in what they claim. A requirement whose title is unknown
# falls back to its own identifiers, which is exactly the behaviour there was
# before any of this existed.


def describe_inline(requirements, names: dict[str, ModName]) -> str:
    """One line naming each missing mod, for a console message."""
    parts = []
    for req in requirements:
        entry = names.get(req.guid)
        if entry is None:
            parts.append(req.label)
        elif entry.page_url:
            parts.append(f"{entry.name} ({entry.page_url})")
        else:
            parts.append(entry.name)
    return ", ".join(parts)


def describe_block(
    requirements, names: dict[str, ModName], *, indent: str = "    ", show_urls: bool = True
) -> str:
    """One indented block per missing mod, for a dialog with room to breathe.

    ``show_urls`` off leaves the page address out, for a surface that offers it
    as something to click instead of something to read.
    """
    lines = []
    for req in requirements:
        entry = names.get(req.guid)
        if entry is None:
            lines.append(f"{indent}{req.label}")
            continue
        lines.append(f"{indent}{entry.name}")
        if show_urls and entry.page_url:
            lines.append(f"{indent}{entry.page_url}")
    return "\n".join(lines)


def page_urls(requirements, names: dict[str, ModName]) -> list[str]:
    """The mod pages behind these requirements, in order and de-duplicated."""
    urls = []
    for req in requirements:
        entry = names.get(req.guid)
        url = entry.page_url if entry is not None else None
        if url and url not in urls:
            urls.append(url)
    return urls


def all_named(requirements, names: dict[str, ModName]) -> bool:
    return all(req.guid in names for req in requirements)


def subscribe_hint(requirements, names: dict[str, ModName], *, anchor: str = "above") -> str:
    """How to get the missing content, worded for what can actually be offered.

    Three different things are true depending on what was resolved, and saying
    the wrong one is worse than saying nothing: a mod page can only be mentioned
    when there is one, and the "it may be titled differently" warning only makes
    sense while an identifier is on screen instead of a title.
    """
    if not all_named(requirements, names):
        return (
            "Subscribe to it from the game's Mods browser, then try again. Its name"
            f" there may differ from the identifiers shown {anchor}."
        )
    pages = page_urls(requirements, names)
    if len(pages) == 1:
        return (
            "You can subscribe to it from the game's Mods browser, or from its mod"
            " page — subscribing there works while the game is running."
        )
    if pages:
        return (
            "You can subscribe to them from the game's Mods browser, or from the mod"
            f" pages {anchor} — subscribing there works while the game is running."
        )
    return "Subscribe to it from the game's Mods browser, then try again."


# --- the three tiers ----------------------------------------------------------


def _archive_roots(cfg: Config) -> list[Path]:
    mods = cfg.mods_dir
    return [mods, mods.parent / _DISABLED_MODS_DIR]


def harvest_local(cfg: Config) -> int:
    """Read every mod archive on disk into the cache. Returns how many it learned.

    Costs nothing and asks nothing of the network, and it covers mods that are
    no longer installed, since a disabled-mods backup is an archive like any
    other. Meant to run at startup next to the other housekeeping.
    """
    from . import replaymods  # local: replaymods owns the guid header read

    cache = NameCache(cache_path(cfg))
    cache.load()
    learned = 0
    now = int(time.time())
    for root in _archive_roots(cfg):
        if not root.exists():
            continue
        with contextlib.suppress(OSError):
            for archive in sorted(root.rglob("*.sga")):
                guid = replaymods._sga_guid(archive)
                if not guid or cache.get(guid) is not None:
                    continue
                title = archive_display_name(archive)
                if not title:
                    continue
                cache.put(
                    ModName(
                        guid=guid,
                        name=title,
                        pub_id=archive.parent.name if archive.parent.name.isdigit() else "",
                        source="archive",
                        seen=now,
                    )
                )
                learned += 1
    cache.save()
    return learned


def _get_json(url: str, timeout: float) -> dict | None:
    status, body = nethttp.get(url, timeout=timeout, headers={"User-Agent": _UA})
    if status != 200 or not body:
        return None
    with contextlib.suppress(ValueError):
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    return None


def _profile_ids(steam_ids: list[str], timeout: float) -> list[int]:
    """Relic profile ids for these Steam ids — one request for all of them."""
    names = json.dumps([f"/steam/{s}" for s in steam_ids])
    data = _get_json(_PERSONAL_STAT.format(names=urllib.parse.quote(names)), timeout)
    if not data:
        return []
    ids: list[int] = []
    for group in data.get("statGroups") or []:
        for member in (group or {}).get("members") or []:
            with contextlib.suppress(TypeError, ValueError):
                profile_id = int((member or {}).get("profile_id") or 0)
                if profile_id and profile_id not in ids:
                    ids.append(profile_id)
    return ids


def resolve_by_match(match_id: int, *, timeout: float = 6.0) -> dict[str, ModName]:
    """Titles for one match's mod packs, addressed by the match's own id.

    The best answer available, and the cheapest: one request, no player lookup
    first, and no age limit — a match stays reachable by id however long ago it
    was played, where a player's recent history holds only about ten per type.

    It is also the authoritative naming. A guid that different lobbies title
    differently (a republished or forked mod keeps its guid) is resolved here the
    way *this* replay's own lobby titled it, so there is nothing to disambiguate.
    """
    data = _get_json(_MATCH_BY_ID.format(match_id=int(match_id)), timeout)
    if not data:
        return {}
    found: dict[str, ModName] = {}
    for match in data.get("matchHistory") or []:
        options = decode_options((match or {}).get("options"))
        if not options:
            continue
        for entry in mod_packs(options):
            found[entry.guid] = entry
    return found


def resolve_online(
    steam_ids: list[str], *, match_id: int | None = None, timeout: float = 6.0
) -> dict[str, ModName]:
    """Titles for every mod pack these players' recent matches mention.

    Two requests, both batched over all players. The result is not filtered to
    the guids that were asked for: every pair the response happens to contain is
    returned, so one lookup can answer later ones for free.

    One guid can be titled differently by different lobbies — a republished or
    forked mod keeps the guid and changes its name — so the title is chosen
    rather than simply taken:

    * a pack listed by ``match_id``, the replay's own match, wins outright; it is
      by definition what that replay was recorded with;
    * otherwise the title the most lobbies agree on wins, which beats taking
      whichever happened to be seen last (one guid here is named by 23 matches
      one way and a single stray match another).

    The server returns only about ten matches per match type, so a mod is found
    when the match that used it is still among a participant's recent custom
    games. An older replay simply resolves to nothing.
    """
    if not steam_ids:
        return {}
    profile_ids = _profile_ids(steam_ids, timeout)
    if not profile_ids:
        return {}
    ids = urllib.parse.quote(json.dumps(profile_ids))
    data = _get_json(_MATCH_HISTORY.format(ids=ids), timeout)
    if not data:
        return {}
    exact: dict[str, ModName] = {}
    tally: dict[str, dict[tuple[str, str], tuple[int, ModName]]] = {}
    for match in data.get("matchHistoryStats") or []:
        options = decode_options((match or {}).get("options"))
        if not options:
            continue
        here = match_id is not None and (match or {}).get("id") == match_id
        for entry in mod_packs(options):
            if here:
                exact[entry.guid] = entry
                continue
            votes = tally.setdefault(entry.guid, {})
            key = (entry.name, entry.pub_id)
            count, _ = votes.get(key, (0, entry))
            votes[key] = (count + 1, entry)
    found = {
        guid: max(votes.values(), key=lambda pair: pair[0])[1]
        for guid, votes in tally.items()
    }
    found.update(exact)  # the replay's own lobby overrides any popularity count
    return found


def lookup(
    cfg: Config,
    guids: list[str],
    steam_ids: list[str],
    *,
    match_id: int | None = None,
    allow_network: bool = True,
    timeout: float = 6.0,
) -> dict[str, ModName]:
    """Titles for these guids, from the cache and — if needed — the match API.

    Never raises and never blocks on a verdict: whatever it cannot answer is
    simply absent from the result, and the caller falls back to the identifiers
    the replay itself provided.
    """
    wanted = [g for g in dict.fromkeys(guids) if g]
    if not wanted:
        return {}
    cache = NameCache(cache_path(cfg))
    with contextlib.suppress(Exception):
        cache.load()
    known = {g: entry for g in wanted if (entry := cache.get(g)) is not None}
    missing = [g for g in wanted if g not in known]
    if not missing or not allow_network:
        return known

    # The match's own id answers in one request, for a match of any age, so it is
    # tried first. Only a replay whose id could not be read — or a match the
    # server no longer serves — falls back to scanning the players' histories,
    # which costs two requests and reaches back only about ten custom games.
    fetched: dict[str, ModName] = {}
    if match_id is not None:
        with contextlib.suppress(Exception):
            fetched = resolve_by_match(match_id, timeout=timeout)
    if steam_ids and any(guid not in fetched for guid in missing):
        with contextlib.suppress(Exception):
            # The by-match answer is authoritative, so it stays on top.
            fetched = {
                **resolve_online(steam_ids, match_id=match_id, timeout=timeout),
                **fetched,
            }
    if fetched:
        with contextlib.suppress(Exception):
            for entry in fetched.values():
                cache.put(entry)
            cache.save()
    known.update({g: fetched[g] for g in missing if g in fetched})
    return known
