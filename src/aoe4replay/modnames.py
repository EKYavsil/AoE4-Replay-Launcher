"""What a mod's guid is actually called, so a refusal can name it.

A replay identifies its mods by guid plus a server-assigned pack number, and
neither spells the mod's title. The guid cannot be decoded either: every one
observed is a version-4 (random) UUID, and no hash of the title, the publisher
id or any combination of them reproduces it. The title is simply not in the
replay — it has to be asked for.

Relic's community match API has it, on the host the launcher already downloads
replays from. A match record's compressed ``options`` blob lists the lobby's mod
packs with their titles, publisher ids and guids, and asking for the replay's own
match by id takes **one request and works whatever the match's age**. A replay
whose match id cannot be read falls back to scanning its players' recent
histories, which costs two requests and reaches back only about ten custom games
per player.

Answers are remembered for the life of the process, so clicking the same blocked
replay twice — or the panel and the service both asking about it — costs one
request rather than two. Nothing is written to disk: one request is cheap enough
not to need it, and a stored title would go stale where a fresh one cannot. One
guid can even carry two titles, where a mod was republished under a new name, so
a remembered answer is a guess in a way a just-fetched one is not.

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
import urllib.parse
import zlib
from dataclasses import dataclass

from . import nethttp

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

# A title arrives from a third party and is shown to the user verbatim; cap it
# and strip anything that would break out of its line.
_MAX_TITLE = 60
_CONTROL = re.compile(r"[\r\n\t\x00-\x1f]")


@dataclass(frozen=True)
class ModName:
    """A mod's title as the match API reported it."""

    guid: str
    name: str
    pub_id: str

    @property
    def page_url(self) -> str | None:
        return _MOD_PAGE.format(pub_id=self.pub_id) if self.pub_id else None


# guid -> title, for the life of the process. See the module docstring for why
# this is deliberately not persisted.
_memo: dict[str, ModName] = {}


def forget() -> None:
    """Drop what this process has learned, so the next lookup asks again."""
    _memo.clear()


def _dicts(value: object) -> list[dict]:
    """The dict entries of something the server said was a list.

    An undocumented endpoint can answer with a different shape — a string where
    a list belonged, or a list with nulls in it — and a lookup that only ever
    produces a label must not raise on that.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clean_title(text: object) -> str:
    return _CONTROL.sub(" ", str(text or "")).strip()[:_MAX_TITLE]


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
    for pack in _dicts(packs):
        guid = guid_from_arc_id(pack.get("arcID"))
        title = _clean_title(pack.get("name"))
        if not guid or not title:
            continue
        pub_id = _CONTROL.sub("", str(pack.get("pubID") or "")).strip()
        found.append(
            ModName(guid=guid, name=title, pub_id=pub_id if pub_id.isdigit() else "")
        )
    return found


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


def describe_entries(
    requirements, names: dict[str, ModName]
) -> list[tuple[str, str | None]]:
    """``(text, page url)`` per missing requirement, for a surface that can click.

    One entry per requirement and one place it is named — the url rides along so
    the name itself becomes the link, rather than being printed once as text and
    again as an address. ``None`` where there is no page: an unresolved
    requirement falls back to its identifiers, which lead nowhere.
    """
    entries: list[tuple[str, str | None]] = []
    for req in requirements:
        entry = names.get(req.guid)
        if entry is None:
            entries.append((req.label, None))
        else:
            entries.append((entry.name, entry.page_url))
    return entries


def page_urls(requirements, names: dict[str, ModName]) -> list[str]:
    """The mod pages behind these requirements, in order and de-duplicated."""
    urls: list[str] = []
    for _text, url in describe_entries(requirements, names):
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
            "Subscribe to it in the game's Mods browser, then try the replay again."
            f" Its name there may differ from the identifiers shown {anchor}."
        )
    pages = page_urls(requirements, names)
    if len(pages) == 1:
        return (
            "Subscribe to it in the game's Mods browser, or on its mod page; both"
            " work while the game is running. Then try the replay again."
        )
    if pages:
        # No position word: a surface may list the pages above the sentence or
        # below it, and "their mod pages" is true either way.
        return (
            "Subscribe to them in the game's Mods browser, or on their mod pages;"
            " both work while the game is running. Then try the replay again."
        )
    return "Subscribe to it in the game's Mods browser, then try the replay again."


# --- asking the match API -----------------------------------------------------


def _get_json(url: str, timeout: float) -> dict | None:
    status, body = nethttp.get(url, timeout=timeout, headers={"User-Agent": _UA})
    if status != 200 or not body:
        return None
    with contextlib.suppress(ValueError):
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    return None


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
    for match in _dicts(data.get("matchHistory")):
        options = decode_options(match.get("options"))
        if not options:
            continue
        for entry in mod_packs(options):
            found[entry.guid] = entry
    return found


def _profile_ids(steam_ids: list[str], timeout: float) -> list[int]:
    """Relic profile ids for these Steam ids — one request for all of them."""
    names = json.dumps([f"/steam/{s}" for s in steam_ids])
    data = _get_json(_PERSONAL_STAT.format(names=urllib.parse.quote(names)), timeout)
    if not data:
        return []
    ids: list[int] = []
    for group in _dicts(data.get("statGroups")):
        for member in _dicts(group.get("members")):
            with contextlib.suppress(TypeError, ValueError):
                profile_id = int(member.get("profile_id") or 0)
                if profile_id and profile_id not in ids:
                    ids.append(profile_id)
    return ids


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
    for match in _dicts(data.get("matchHistoryStats")):
        options = decode_options(match.get("options"))
        if not options:
            continue
        here = match_id is not None and match.get("id") == match_id
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
    guids: list[str],
    steam_ids: list[str],
    *,
    match_id: int | None = None,
    allow_network: bool = True,
    timeout: float = 6.0,
) -> dict[str, ModName]:
    """Titles for these guids, from what this process knows or the match API.

    Never raises and never blocks on a verdict: whatever it cannot answer is
    simply absent from the result, and the caller falls back to the identifiers
    the replay itself provided. ``allow_network`` off answers only from what is
    already known, which is how a UI thread can ask without waiting.
    """
    wanted = [g for g in dict.fromkeys(guids) if g]
    if not wanted:
        return {}
    known = {g: _memo[g] for g in wanted if g in _memo}
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
    # Everything the response happened to carry is remembered, not only what was
    # asked for: it cost nothing extra and may answer the next replay for free.
    _memo.update(fetched)
    known.update({g: fetched[g] for g in missing if g in fetched})
    return known
