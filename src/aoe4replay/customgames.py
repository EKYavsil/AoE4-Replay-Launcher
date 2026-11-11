"""Custom-lobby games for one profile, from whichever source will answer.

aoe4world indexes ranked and quick-match games only — a profile with 883 games
there had none of its nine custom ones — so a custom lobby has to come from
Relic. Two of their services expose it, and both are capped the same way: at most
ten custom matches per profile, with ``page``, ``recordCount`` and
``sortDirection`` ignored outright. Verified across six profiles, the two return
*identical* sets, so the cap is the server's and not a property of either.

**Relic's community match API is the primary source.** One request returns the
whole page, and it carries things the official one does not:

* the real map name, where the official API says only ``Generated_Map``;
* the replay's download urls, so availability is a fact rather than a flag;
* the rating change, the lobby name and the match's mod packs;
* civilisations as ids, which the companion ``races`` table names — including
  the newest ones the official API still reports as ``Unknown``.

**The official match API is kept as a fallback**, because the community one is
undocumented: it is a method on a host the launcher already downloads replays
from, and it could change shape without notice. Falling back costs one request
per match instead of one per page, and loses the map name.

Both are read-only and best-effort; a profile that cannot be answered yields an
empty list rather than raising.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

from . import nethttp

_UA = "aoe4-replay-launcher"
_TIMEOUT = 20.0

# --- community (primary) ------------------------------------------------------
_COMMUNITY = "https://aoe-api.worldsedgelink.com/community/leaderboard"
_RECENT_HISTORY = _COMMUNITY + "/getRecentMatchHistory?title=age4&profile_ids=[{profile_id}]"
_LEADERBOARDS = _COMMUNITY + "/getAvailableLeaderboards?title=age4"
# matchtype 0 is "Custom" — named as such by getAvailableLeaderboards itself.
_CUSTOM_MATCH_TYPE = 0

# --- official (fallback) ------------------------------------------------------
_OFFICIAL = "https://api.ageofempires.com/api/GameStats/AgeIV/"
# The same matchtype, as a string, in the official API's own spelling.
_OFFICIAL_CUSTOM = "0"
# The API formats its timestamps US-style and in UTC, e.g. "7/14/2026 7:28:59 AM".
_API_TIME = "%m/%d/%Y %I:%M:%S %p"

# What either server will return no matter what is asked of it.
MAX_MATCHES = 10
# What a row shows where nothing could be resolved: an absent opponent, or a civ
# reported as "Unknown". The same three letters the civ abbreviations use.
UNKNOWN = "UNK"

# The community API identifies civilisations by id and names them in its own
# ``races`` table, whose ``name`` is an internal spelling. These are the twenty-
# five it lists today, mapped onto the icon keys; a civ added later falls back to
# its first three letters, which is already right for most of them.
_RACE_ABBREV = {
    "abbasid": "ABB",
    "abbasid_ha_01": "AYY",
    "byzantine": "BYZ",
    "byzantine_ha_mac": "MAC",
    "chinese": "CHI",
    "chinese_ha_01": "ZXL",
    "english": "ENG",
    "french": "FRE",
    "french_ha_01": "JDA",
    "hre": "HRE",
    "hre_ha_01": "OOD",
    "japanese": "JPN",
    "japanese_ha_sen": "SEN",
    "jin_dynasty": "JIN",
    "lancaster": "LAN",
    "malian": "MAL",
    "mongol": "MON",
    "mongol_ha_gol": "GOL",
    "ottoman": "OTT",
    "rus": "RUS",
    "scots": "SCO",
    "sultanate": "DEL",
    "sultanate_ha_tug": "TUG",
    "templar": "TMP",
    "vikings": "VIK",
}
_races_cache: dict[int, str] | None = None


def _get_json(url: str) -> dict | None:
    status, body = nethttp.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
    if status != 200 or not body:
        return None
    with contextlib.suppress(ValueError):
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    return None


def _post_json(endpoint: str, payload: dict) -> dict:
    request = urllib.request.Request(
        _OFFICIAL + endpoint,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        body = response.read()
    parsed = json.loads(body) if body else {}
    return parsed if isinstance(parsed, dict) else {}


def _civ_abbrev_for(race_id: object) -> str:
    """The icon key for a community civilisation id, via the ``races`` table.

    Fetched once per run and remembered: the table is static between patches, and
    a tab opened repeatedly should not keep asking for it.
    """
    global _races_cache
    if _races_cache is None:
        table: dict[int, str] = {}
        data = _get_json(_LEADERBOARDS) or {}
        races = data.get("races")
        races = races if isinstance(races, list) else []
        for race in races:
            if not isinstance(race, dict):
                continue
            name = str(race.get("name") or "").strip().lower()
            try:
                key = int(race.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if key and name:
                table[key] = _RACE_ABBREV.get(name) or name[:3].upper()
        # Only a table that actually arrived is remembered. Caching an empty one
        # would turn a single failed request into civ icons missing for the rest
        # of the run, with nothing to trigger a retry.
        if table:
            _races_cache = table
        else:
            return ""
    try:
        return _races_cache.get(int(race_id or 0), "")
    except (TypeError, ValueError):
        return ""


def _parse_time(text: object) -> datetime | None:
    try:
        return datetime.strptime(str(text), _API_TIME)
    except (TypeError, ValueError):
        return None


def _utc(stamp: object) -> datetime | None:
    """A unix timestamp as the naive UTC datetime the row renderer expects."""
    try:
        return datetime.fromtimestamp(int(stamp), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _row(
    *,
    game_id: int,
    mine: dict | None,
    theirs: dict | None,
    lobby: list[dict],
    map_name: str,
    duration: int,
    started_at: datetime | None,
    won: bool | None,
    replay: bool,
) -> dict:
    """One row for the Custom tab, in the shape the shared renderer wants.

    Both sources funnel through here, so a fallback row is indistinguishable from
    a primary one except for the fields the official API cannot supply.
    """
    opponent_id = (theirs or {}).get("_pid") or 0
    winner = None
    if won is True:
        winner = (mine or {}).get("_pid") or 0
    elif won is False:
        winner = opponent_id
    return {
        "game_id": game_id,
        "name1": (mine or {}).get("name") or UNKNOWN,
        "name2": (theirs or {}).get("name") or UNKNOWN,
        "civ1": (mine or {}).get("civ") or UNKNOWN,
        "civ2": (theirs or {}).get("civ") or UNKNOWN,
        # No rosters: the shared renderer reads these only to offer its hover over
        # the two names, which a custom row does not want. The lobby column has
        # its own hover and reads _lobby instead.
        "team1": [],
        "team2": [],
        "map": map_name,
        "kind": "custom",
        "duration": duration,
        "started_at": started_at,
        "winner": winner,
        "_id1": (mine or {}).get("_pid") or 0,
        "_id2": opponent_id,
        "_all_ids": [p["_pid"] for p in lobby if p.get("_pid")],
        "_lobby": lobby,
        "_replay_available": replay,
        # Draw "?"-style markers rather than a silent gap where a civ icon or an
        # opponent is missing.
        "_mark_unknown_civ": True,
    }


# --- primary: the community match API ----------------------------------------


def _community_games(profile_id: int) -> list[dict] | None:
    """This profile's custom games in one request, or None if unanswerable.

    None and ``[]`` mean different things: None is "ask the other service", while
    an empty list is this service saying the profile has no custom games.
    """
    from . import modnames  # local import: modnames owns the options blob

    data = _get_json(_RECENT_HISTORY.format(profile_id=int(profile_id)))
    if not data or "matchHistoryStats" not in data:
        return None
    aliases: dict[int, str] = {}
    for profile in modnames._dicts(data.get("profiles")):
        with contextlib.suppress(TypeError, ValueError):
            aliases[int(profile.get("profile_id") or 0)] = str(profile.get("alias") or "")

    rows: list[dict] = []
    for match in modnames._dicts(data.get("matchHistoryStats")):
        if match.get("matchtype_id") != _CUSTOM_MATCH_TYPE:
            continue
        options = modnames.decode_options(match.get("options")) or {}
        members = modnames._dicts(match.get("matchhistorymember"))
        lobby = []
        for member in members:
            with contextlib.suppress(TypeError, ValueError):
                pid = int(member.get("profile_id") or 0)
                lobby.append(
                    {
                        "_pid": pid,
                        "name": aliases.get(pid) or str(pid),
                        "civ": _civ_abbrev_for(member.get("civilization_id")),
                        "_won": member.get("outcome") == 1,
                    }
                )
        mine = next((p for p in lobby if p["_pid"] == int(profile_id)), None)
        others = [p for p in lobby if p is not mine]
        replay_ids = {u.get("profile_id") for u in modnames._dicts(match.get("matchurls"))}
        started = _utc(match.get("startgametime"))
        finished = _utc(match.get("completiontime"))
        duration = int((finished - started).total_seconds()) if started and finished else 0
        rows.append(
            _row(
                game_id=int(match.get("id") or 0),
                mine=mine,
                theirs=others[0] if others else None,
                lobby=lobby,
                # localizedMapName is the display spelling ("Lipany"); mapName is
                # the id ("lipany"). Either beats the official API's silence.
                map_name=str(options.get("localizedMapName") or options.get("mapName") or ""),
                duration=duration,
                started_at=started,
                won=mine["_won"] if mine is not None else None,
                replay=bool(replay_ids) if replay_ids else True,
            )
        )
    return [row for row in rows if row["game_id"]]


# --- fallback: the official match API ----------------------------------------


def match_list(profile_id: int) -> list[dict]:
    """The profile's custom matches, newest first, as the official API returns them."""
    data = _post_json(
        "GetMatchList",
        {
            "game": "age4",
            "gamertag": "unknown user",
            "matchType": _OFFICIAL_CUSTOM,
            "page": 1,
            "playerNumber": 0,
            "profileId": int(profile_id),
            "recordCount": MAX_MATCHES,
            "sortColumn": "dateTime",
            "sortDirection": "DESC",
            "undefined": None,
        },
    )
    matches = data.get("matchList")
    return [m for m in matches if isinstance(m, dict)] if isinstance(matches, list) else []


def match_detail(profile_id: int, match_id: str) -> dict:
    """One match's summary and full player list, from the official API."""
    return _post_json("GetMatchDetail", {"profileId": int(profile_id), "matchId": str(match_id)})


def _official_players(detail: dict) -> list[dict]:
    """Participants, normalised. ``userId`` holds the real profile id here; the
    sibling ``profileId`` field is 0 for a custom game and must not be used."""
    from . import aoe4world  # local import: aoe4world owns the civ-name table

    raw = detail.get("playerList")
    if not isinstance(raw, list):
        return []
    players = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("userId") or 0)
        except (TypeError, ValueError):
            pid = 0
        name = str(entry.get("civName") or "").strip().lower()
        players.append(
            {
                "_pid": pid,
                "name": str(entry.get("userName") or (pid or UNKNOWN)),
                "civ": aoe4world._civ_abbrev(name.replace("'", "").replace(" ", "_")),
                "_won": str(entry.get("winLoss") or "").lower() == "win",
                "_replay": bool(entry.get("matchReplayAvailable")),
            }
        )
    return players


def _official_games(profile_id: int) -> list[dict]:
    """The same page, at one request per match and without the map name."""
    rows = []
    for listed in match_list(profile_id)[:MAX_MATCHES]:
        try:
            detail = match_detail(profile_id, listed.get("matchId"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Being throttled is not a per-match hiccup to absorb: swallowing
                # it would hand back ten rows of blanks that read as real data.
                # Stop and let the caller say why — the same call aoe4world makes
                # when a rate limit interrupts a multi-request walk.
                raise
            detail = {}
        except (urllib.error.URLError, OSError, ValueError):
            detail = {}
        try:
            game_id = int(listed.get("matchId") or 0)
        except (TypeError, ValueError):
            continue
        if not game_id:
            continue
        lobby = _official_players(detail)
        mine = next((p for p in lobby if p["_pid"] == int(profile_id)), None)
        others = [p for p in lobby if p is not mine]
        summary = detail.get("matchSummary")
        summary = summary if isinstance(summary, dict) else {}
        minutes = summary.get("matchLength") or listed.get("matchLength") or 0
        try:
            duration = int(float(minutes) * 60)
        except (TypeError, ValueError):
            duration = 0
        # The list entry carries the queried player's own result even for a match
        # against the AI, where the detail lists no opponent to infer one from.
        own = str(listed.get("winLoss") or "").lower()
        rows.append(
            _row(
                game_id=game_id,
                mine=mine,
                theirs=others[0] if others else None,
                lobby=[{k: p[k] for k in ("_pid", "name", "civ")} for p in lobby],
                map_name="",  # this API never names a custom game's map
                duration=duration,
                started_at=_parse_time(listed.get("dateTime"))
                or _parse_time(summary.get("dateTime")),
                won=True if own == "win" else False if own == "loss" else None,
                replay=any(p["_replay"] for p in lobby) if lobby else True,
            )
        )
    return rows


# --- what the panel calls -----------------------------------------------------


def custom_games(profile_id: int) -> list[dict]:
    """Display-ready rows for this profile's custom games, newest first.

    At most :data:`MAX_MATCHES` come back — the server's ceiling, not a page
    size. The community API answers in one request and with more detail; the
    official one is tried only when that cannot be read at all, since it needs a
    request per match and cannot name the map.

    Neither server's order is dependable — the official one returns months out of
    sequence and ignores ``sortColumn`` — so the rows are sorted here. A row
    whose timestamp could not be read sorts last rather than breaking the
    comparison.
    """
    rows: list[dict] | None = None
    with contextlib.suppress(Exception):
        rows = _community_games(profile_id)
    if rows is None:
        rows = _official_games(profile_id)
    rows.sort(key=lambda row: row["started_at"] or datetime.min, reverse=True)
    return rows
