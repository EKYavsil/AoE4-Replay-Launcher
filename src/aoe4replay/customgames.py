"""Custom-lobby games for one profile, from the official match API.

aoe4world indexes ranked and quick-match games only, so a custom lobby is
invisible there — which is why this goes to the official endpoint instead.

Two hard server-side limits shape everything below, both measured rather than
documented:

* **At most ten matches per profile.** ``page`` returns an empty list beyond the
  first, and ``recordCount`` and ``sortDirection`` are ignored outright, so there
  is no pagination and no way to reach further back than the server offers.
* **The list carries only the queried player's own civilisation** and no opponent
  at all (``profileId`` 0, ``userName`` null). Each match therefore has to be
  detailed individually to learn who else was in the lobby, which is where the
  real player list, civilisations and results come from.

``mapType`` is always ``Generated_Map`` for a custom game: the API never names
the map, so neither does the UI. Replay-only data (the real map, any mods) is not
available until the replay itself is downloaded.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from . import aoe4world

_BASE = "https://api.ageofempires.com/api/GameStats/AgeIV/"
_UA = "aoe4-replay-launcher"
# matchType 0 selects custom games. The value is not documented; it was found by
# probing, and 1/2 return the matchmaking histories instead.
_MATCH_TYPE_CUSTOM = "0"
# What the server will return no matter what is asked of it (verified: page 2+ is
# empty, recordCount is ignored). Exposed so the UI can explain the ceiling.
MAX_MATCHES = 10
_TIMEOUT = 20.0
# The API formats its timestamps US-style and in UTC, e.g. "7/14/2026 7:28:59 AM".
_API_TIME = "%m/%d/%Y %I:%M:%S %p"
# What a row shows where the API gave nothing: an absent opponent, or a civ it
# reports as "Unknown" (a new or modded one). The same three letters the civ
# abbreviations already use, so the row and the hover agree.
UNKNOWN = "UNK"


def _post(endpoint: str, payload: dict) -> dict:
    request = urllib.request.Request(
        _BASE + endpoint,
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


def _parse_time(text: object) -> datetime | None:
    try:
        return datetime.strptime(str(text), _API_TIME)
    except (TypeError, ValueError):
        return None


def match_list(profile_id: int) -> list[dict]:
    """The profile's custom matches, newest first, as the server returns them."""
    data = _post(
        "GetMatchList",
        {
            "game": "age4",
            "gamertag": "unknown user",
            "matchType": _MATCH_TYPE_CUSTOM,
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
    """One match's summary and full player list."""
    return _post("GetMatchDetail", {"profileId": int(profile_id), "matchId": str(match_id)})


def _players(detail: dict) -> list[dict]:
    """Every participant, normalised. ``userId`` holds the real profile id here;
    the sibling ``profileId`` field is 0 for a custom game and must not be used."""
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
        players.append(
            {
                "_pid": pid,
                "name": str(entry.get("userName") or (pid or "?")),
                "civ": aoe4world._civ_abbrev(_civ_key(entry.get("civName"))),
                "replay": bool(entry.get("matchReplayAvailable")),
            }
        )
    return players


def _civ_key(name: object) -> str:
    """Map a display civ name onto the id spelling aoe4world's table uses.

    The official API says "Jeanne d'Arc" / "Zhu Xi's Legacy" where aoe4world says
    ``jeanne_darc`` / ``zhu_xis_legacy``; dropping apostrophes and underscoring
    spaces lines the two up, and civs missing from that table still fall back to
    their first three letters, which is what the icon keys use.
    """
    text = str(name or "").strip().lower().replace("'", "").replace("’", "")
    return text.replace(" ", "_")



def _summary(profile_id: int, listed: dict, detail: dict) -> dict | None:
    """One row for the Custom tab, in the same shape the shared renderer wants."""
    try:
        game_id = int(listed.get("matchId") or 0)
    except (TypeError, ValueError):
        return None
    if not game_id:
        return None

    players = _players(detail)
    mine = next((p for p in players if p["_pid"] == int(profile_id)), None)
    others = [p for p in players if p is not mine]
    # A match against the AI lists only the human players, so there may be no
    # opponent to show at all; the row still carries everything else.
    theirs = others[0] if others else None

    # The queried player's own result comes from the *list* entry, which carries it
    # even for a match against the AI — where the detail lists no opponent at all,
    # so there is no second player to infer a loss from.
    own_result = str(listed.get("winLoss") or "").lower()
    opponent_id = (theirs or {}).get("_pid") or 0
    winner = None
    if own_result == "win":
        winner = int(profile_id)
    elif own_result == "loss":
        winner = opponent_id

    summary_block = detail.get("matchSummary")
    summary_block = summary_block if isinstance(summary_block, dict) else {}
    started = _parse_time(listed.get("dateTime")) or _parse_time(summary_block.get("dateTime"))
    minutes = summary_block.get("matchLength") or listed.get("matchLength") or 0
    try:
        duration = int(float(minutes) * 60)
    except (TypeError, ValueError):
        duration = 0

    fallback_civ = aoe4world._civ_abbrev(_civ_key(listed.get("civilization")))
    return {
        "game_id": game_id,
        "name1": (mine or {}).get("name") or str(profile_id),
        # UNKNOWN rather than an empty string: a custom match against the AI lists
        # no opponent at all, and the row says so in the same words the civ slot
        # and the hover use, instead of a bare "?".
        "name2": (theirs or {}).get("name") or UNKNOWN,
        "civ1": (mine or {}).get("civ") or fallback_civ or UNKNOWN,
        "civ2": (theirs or {}).get("civ") or UNKNOWN,
        # No rosters: the shared renderer reads these only to offer its hover over
        # the two names, which a custom row does not want. Empty lists switch it off.
        "team1": [],
        "team2": [],
        # The whole lobby, for the row's own player-count column and its hover.
        # Separate from team1/team2 so it cannot re-enable the matchup hover.
        # AI players are absent from the API's list, so this counts humans.
        "_lobby": [
            {"name": p["name"], "civ": p["civ"], "_pid": p["_pid"]} for p in players
        ],
        "map": "",  # the API never names a custom game's map
        # Draw "?" where an unknown civilisation's icon would go, instead of a
        # silent gap: for a custom game the opponent may be missing entirely, and
        # a new or modded civ comes back as "Unknown".
        "_mark_unknown_civ": True,
        "kind": "custom",
        "duration": duration,
        "started_at": started,
        "winner": winner,
        "_id1": int(profile_id),
        "_id2": opponent_id,
        "_all_ids": [p["_pid"] for p in players if p["_pid"]],
        "_replay_available": any(p["replay"] for p in players) if players else True,
    }


def custom_games(profile_id: int) -> list[dict]:
    """Display-ready rows for this profile's custom games, newest first.

    At most :data:`MAX_MATCHES` come back — that is the server's ceiling, not a
    page size. One request lists the matches and one more details each of them;
    a match whose detail cannot be fetched is still listed, from the list data
    alone, rather than being dropped.

    The server's own order is arbitrary — it ignores ``sortColumn`` and
    ``sortDirection``, and returns months out of sequence — so the rows are
    sorted here, newest first. A row whose timestamp could not be parsed sorts
    last rather than breaking the comparison.
    """
    rows = []
    for listed in match_list(profile_id)[:MAX_MATCHES]:
        try:
            detail = match_detail(profile_id, listed.get("matchId"))
        except (urllib.error.URLError, OSError, ValueError):
            detail = {}
        row = _summary(profile_id, listed, detail)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row["started_at"] or datetime.min, reverse=True)
    return rows
