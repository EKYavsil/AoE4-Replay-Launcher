"""aoe4world / Relic API helpers.

Two uses:
- Build resolution: a replay's embedded timestamp is the recorder's *local* time
  with no timezone, while build boundaries are UTC; near a release this picks the
  wrong build. aoe4world downloads are named ``AgeIV_Replay_<gameid>`` and the API
  exposes the match's UTC ``started_at`` — the unambiguous time to resolve against.
- Replay panel: validate profiles, list head-to-head games, find which matches
  have a downloadable replay (Relic ``GetMatchReplay``), and download them.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_API = "https://aoe4world.com/api/v0"
# Relic's official replay endpoint; availability depends on the perspective
# profile_id, so a match is probed against both players before giving up.
_RELIC_REPLAY = (
    "https://api.ageofempires.com/api/GameStats/AgeIV/GetMatchReplay/"
    "?matchId={match_id}&profileId={profile_id}"
)
# WorldsEdge community endpoint (primary source): one request returns signed
# Azure-blob URLs for every available perspective, so the replay itself downloads
# straight from blob storage and almost nothing touches the rate-limited Relic host
# above. The signed URLs are short-lived (~6 min), so the list is fetched and the
# blob downloaded back-to-back in a single operation.
_WORLDSEDGE_REPLAYS = (
    "https://aoe-api.worldsedgelink.com/community/leaderboard/getReplayFiles"
    "?matchIDs=[{match_id}]&title=age4"
)
_WE_SUCCESS = 0          # result.code: the replay list was returned
_WE_NOT_FOUND = 2        # result.code: the match has no replay (deleted / never existed)
_WE_REPLAY_DATATYPE = 0  # replayFiles[].datatype: the real replay (others are aux files)
_UA = "aoe4-replay-launcher"

# Shown for any HTTP 429. Users read this as an app bug, so say plainly it isn't:
# the server (not the launcher) is throttling their network for too many requests.
_RATE_LIMIT_MSG = (
    "This isn't a problem with the launcher — the server temporarily limited your "
    "network for making too many requests (HTTP 429). Please wait a few minutes and "
    "try again."
)

_GAME_ID_RE = re.compile(r"AgeIV_Replay_(\d+)", re.IGNORECASE)
# WorldsEdge blob downloads are named ``M_<matchId>_<64-hex-hash>``. The hash holds
# long digit runs, so the generic trailing-digits fallback would grab a hash
# fragment instead of the id — match the match id up front.
_WE_BLOB_RE = re.compile(r"^M_(\d+)_[0-9a-f]{16,}", re.IGNORECASE)

# aoe4world civilization id -> short tag shown in match rows.
_CIV_ABBREV = {
    "abbasid_dynasty": "ABB",
    "ayyubids": "AYY",
    "byzantines": "BYZ",
    "chinese": "CHI",
    "delhi_sultanate": "DEL",
    "english": "ENG",
    "french": "FRE",
    "holy_roman_empire": "HRE",
    "house_of_lancaster": "LAN",
    "japanese": "JPN",
    "jeanne_darc": "JDA",
    "knights_templar": "TMP",
    "malians": "MAL",
    "mongols": "MON",
    "order_of_the_dragon": "OOD",
    "ottomans": "OTT",
    "rus": "RUS",
    "zhu_xis_legacy": "ZXL",
}


def _civ_abbrev(civ: object) -> str:
    if not civ:
        return ""
    key = str(civ).strip().lower()
    return _CIV_ABBREV.get(key) or key[:3].upper()


def _team_rosters(game: dict) -> list[list[dict]]:
    """Each team as a list of ``{"name", "civ", "_pid"}`` for its players, taken
    straight from the already-fetched game object — the games list carries every
    player, so building a full team roster needs no extra request."""
    rosters: list[list[dict]] = []
    for team in game.get("teams") or []:
        roster: list[dict] = []
        for slot in team if isinstance(team, list) else []:
            player = slot.get("player") if isinstance(slot, dict) else None
            if not isinstance(player, dict):
                player = slot if isinstance(slot, dict) else {}
            pid = player.get("profile_id")
            if pid:
                roster.append(
                    {
                        "name": player.get("name") or str(pid),
                        "civ": _civ_abbrev(player.get("civilization")),
                        "_pid": pid,
                    }
                )
        if roster:
            rosters.append(roster)
    return rosters


def game_id_from_name(name: str) -> int | None:
    """Extract the aoe4world game id from a replay filename.

    Matches ``AgeIV_Replay_<id>``; the WorldsEdge blob name ``M_<id>_<hash>``; or,
    for panel downloads like ``nick1_nick2_25-12-01_<id>.rec``, the trailing run of
    7+ digits (the game id; short date fields never reach that length)."""
    match = _GAME_ID_RE.search(name)
    if match:
        return int(match.group(1))
    blob = _WE_BLOB_RE.search(name)  # M_<matchId>_<hash> (WorldsEdge download)
    if blob:
        return int(blob.group(1))
    runs = re.findall(r"\d{7,}", name)
    return int(runs[-1]) if runs else None


class Aoe4WorldError(Exception):
    """A request to aoe4world failed (network, rate limit, server, bad JSON) — as
    opposed to a request that succeeded and simply returned no results."""


class _SourceUnavailable(Exception):
    """A replay source failed for a reason *other than* a definitive 'no replay'
    (rate limit, network, or its listed files couldn't be fetched). Signals the
    caller to try the other source; carries a user-facing message for when every
    source is exhausted."""


def _get_json(url: str, retries: int = 3, timeout: int = 15, strict: bool = False) -> Any | None:
    """GET and parse JSON, with exponential backoff.

    A clean HTTP 404 returns ``None`` (genuinely "not found"). A real failure
    (timeout, 429, 5xx, network, bad JSON) returns ``None`` when ``strict`` is
    False — build resolution treats it as a soft miss — or raises
    :class:`Aoe4WorldError` when ``strict`` is True, so the GUI can tell the user
    the request failed instead of reporting "no results".
    """
    detail = "couldn't reach aoe4world"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # not found is a real (empty) answer, not a failure
            detail = (
                _RATE_LIMIT_MSG
                if exc.code == 429
                else f"aoe4world returned an error (HTTP {exc.code})"
            )
        except Exception:  # noqa: BLE001 - network/timeout/parse: treat as a soft failure
            detail = "couldn't reach aoe4world (check your connection)"
        if attempt + 1 < retries:
            time.sleep(1.2 * (2**attempt))
    if strict:
        raise Aoe4WorldError(detail)
    return None


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def game_started_at_utc(game_id: int) -> datetime | None:
    """The match's UTC start time as a naive UTC datetime, or None on any failure."""
    data = _get_json(f"{_API}/games/{game_id}")
    if not isinstance(data, dict):
        return None
    dt = _parse_iso(data.get("started_at"))
    if dt is None:
        return None
    # An offset like +03:00 must be converted to UTC before dropping the tzinfo,
    # otherwise the wall-clock time (e.g. 20:49+03:00) would be kept as if UTC.
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None)


# --------------------------------------------------------------------------
# Panel: profiles, head-to-head games, replay availability / download
# --------------------------------------------------------------------------


def _players_of(data: Any) -> list[dict]:
    if isinstance(data, dict):
        players = data.get("players")
        return players if isinstance(players, list) else []
    return data if isinstance(data, list) else []


def _player_summary(player: dict) -> dict:
    leaderboards = player.get("leaderboards") or {}
    solo = leaderboards.get("rm_solo") or leaderboards.get("rm_1v1") or {}
    rank_num = solo.get("rank")
    profile_id = player.get("profile_id")
    return {
        "profile_id": profile_id,
        "name": player.get("name") or str(profile_id),
        "country": (player.get("country") or "").upper(),
        "last_game": (player.get("last_game_at") or "")[:10],
        "rank": solo.get("rank_level") or "unranked",
        "rank_num": rank_num if isinstance(rank_num, int) else None,
    }


def search_players(query: str, limit: int = 25, max_pages: int = 4) -> list[dict]:
    """Search players by name (case-insensitive exact), most recently active first.

    Mirrors the discord search flow: aoe4world's ``exact=true`` is case-sensitive
    (so it misses differently-cased same-name accounts), and the fuzzy endpoint
    ranks only some on the first page. Both are combined, then filtered to names
    equal to the query ignoring case (dropping near/partial results).
    """
    q = urllib.parse.quote_plus(query)
    target = query.strip().lower()

    # First request is strict (a failure here is reported to the user); extra
    # pages are best-effort so one flaky page doesn't sink an otherwise-good search.
    collected = list(_players_of(_get_json(f"{_API}/players/search?query={q}&exact=true",
                                           strict=True)))
    page = 1
    while page <= max_pages:
        batch = _players_of(_get_json(f"{_API}/players/search?query={q}&page={page}"))
        if not batch:
            break
        collected += batch
        if len(batch) < 50:  # last page (default per_page)
            break
        page += 1

    seen: set = set()
    uniq: list[dict] = []
    for player in collected:
        if (player.get("name") or "").strip().lower() != target:
            continue
        pid = player.get("profile_id")
        if pid and pid not in seen:
            seen.add(pid)
            uniq.append(player)
    # most recently active accounts first (ISO timestamps sort chronologically)
    uniq.sort(key=lambda p: p.get("last_game_at") or "", reverse=True)
    return [_player_summary(p) for p in uniq[:limit]]


def validate_profile(profile_id: int) -> dict | None:
    """Return {profile_id, name} if the aoe4world profile exists, else None.

    Raises :class:`Aoe4WorldError` if the request itself fails, so the caller can
    distinguish "profile doesn't exist" (None) from "couldn't check".
    """
    data = _get_json(f"{_API}/players/{profile_id}", strict=True)
    if not isinstance(data, dict) or not data:
        return None
    if isinstance(data.get("error"), str) or isinstance(data.get("message"), str):
        return None
    candidate = data.get("player") or data.get("data") or data
    if not isinstance(candidate, dict):
        return None
    pid = candidate.get("profile_id")
    name = candidate.get("name")
    if not ((isinstance(pid, int) and pid > 0) or isinstance(name, str)):
        return None
    return {
        "profile_id": int(pid) if isinstance(pid, int) else int(profile_id),
        "name": name.strip() if isinstance(name, str) and name.strip() else str(profile_id),
    }


def _extract_items(data: Any) -> list[dict]:
    """Pull the games list out of the various aoe4world response shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("games", "data", "matches", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for inner in ("data", "items", "results"):
                    if isinstance(value.get(inner), list):
                        return value[inner]
    return []


def _slot_player(slot: object) -> dict | None:
    """A team slot is either ``{"player": {...}}`` (players/games endpoint) or the
    player object directly (single game endpoint). Return the player dict."""
    if not isinstance(slot, dict):
        return None
    inner = slot.get("player")
    return inner if isinstance(inner, dict) else slot


def _team_index(game: dict) -> dict[int, int]:
    """Map profile_id -> team index from the teams[] schema."""
    out: dict[int, int] = {}
    teams = game.get("teams") or []
    if isinstance(teams, list):
        for idx, team in enumerate(teams):
            if isinstance(team, list):
                for slot in team:
                    player = _slot_player(slot)
                    pid = player.get("profile_id") if isinstance(player, dict) else None
                    if isinstance(pid, int):
                        out[pid] = idx
    return out


def _player_field(game: dict, profile_id: int, field: str) -> Any | None:
    teams = game.get("teams") or []
    if isinstance(teams, list):
        for team in teams:
            if isinstance(team, list):
                for slot in team:
                    player = _slot_player(slot)
                    if isinstance(player, dict) and player.get("profile_id") == profile_id:
                        return player.get(field)
    players = game.get("players")
    if isinstance(players, list):
        for player in players:
            if isinstance(player, dict) and player.get("profile_id") == profile_id:
                return player.get(field)
    return None


def _game_id(game: dict) -> int | None:
    for key in ("game_id", "id", "match_id"):
        value = game.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def match_summary(game: dict, id1: int, id2: int) -> dict:
    """A flat, display-ready summary of one head-to-head game."""
    map_field = game.get("map")
    if isinstance(map_field, dict):
        map_name = map_field.get("name") or map_field.get("id") or "?"
    elif isinstance(map_field, str):
        map_name = map_field
    else:
        map_name = game.get("map_id") or "?"

    started = _parse_iso(game.get("started_at"))
    # Convert to UTC before dropping the tz (the value is displayed labelled "UTC");
    # previously the offset was discarded without converting, showing a wrong hour.
    if started is not None and started.tzinfo is not None:
        started = started.astimezone(UTC)
    win1 = _player_field(game, id1, "result") == "win"
    win2 = _player_field(game, id2, "result") == "win"
    winner = id1 if win1 and not win2 else id2 if win2 and not win1 else None

    # Full team rosters for the hover tooltip (team games only). FFA has no real
    # teams, so both sides expose every participant. 1v1 rosters are size 1, which
    # the UI treats as "nothing extra to show".
    rosters = _team_rosters(game)
    if "ffa" in (game.get("kind") or ""):
        everyone = [p for r in rosters for p in r]
        team1 = team2 = everyone
    else:
        team1 = next((r for r in rosters if any(p["_pid"] == id1 for p in r)), [])
        team2 = next((r for r in rosters if any(p["_pid"] == id2 for p in r)), [])

    return {
        "game_id": _game_id(game),
        "name1": _player_field(game, id1, "name") or str(id1),
        "name2": _player_field(game, id2, "name") or str(id2),
        "civ1": _civ_abbrev(_player_field(game, id1, "civilization")),
        "civ2": _civ_abbrev(_player_field(game, id2, "civilization")),
        "team1": team1,
        "team2": team2,
        "map": map_name,
        "kind": game.get("kind") or "?",
        "duration": game.get("duration"),
        "started_at": started.replace(tzinfo=None) if started else None,
        "winner": winner,
    }


def _player_ids(game: dict) -> list[int]:
    """Profile ids of the (up to two) players in a game, teams[] then players[]."""
    ids: list[int] = []
    for team in game.get("teams") or []:
        if isinstance(team, list):
            for slot in team:
                player = _slot_player(slot)
                pid = player.get("profile_id") if isinstance(player, dict) else None
                if isinstance(pid, int) and pid not in ids:
                    ids.append(pid)
    if len(ids) < 2 and isinstance(game.get("players"), list):
        for player in game["players"]:
            pid = player.get("profile_id") if isinstance(player, dict) else None
            if isinstance(pid, int) and pid not in ids:
                ids.append(pid)
    return ids


def _first_pid(team: object) -> int | None:
    if isinstance(team, list):
        for slot in team:
            player = _slot_player(slot)
            pid = player.get("profile_id") if isinstance(player, dict) else None
            if isinstance(pid, int):
                return pid
    return None


def _opponent_of(game: dict, profile_id: int) -> int | None:
    """A player on a team opposite ``profile_id`` (their opponent for display)."""
    team_of = _team_index(game)
    my_team = team_of.get(profile_id)
    if my_team is not None:
        for pid, team in team_of.items():
            if team != my_team:
                return pid
    return next((pid for pid in _player_ids(game) if pid != profile_id), None)


def player_games(
    profile_id: int,
    page: int = 1,
    since: str | None = None,
    leaderboard: str | None = None,
    opponent: int | None = None,
) -> tuple[list[dict], int | None]:
    """One page (50) of a player's games plus the total game count.

    The page already contains full per-game info (players, civs, map, result),
    so no extra call per game is needed. Pages are fetched on demand only.
    ``since`` (a ``YYYY-MM-DD`` date), ``leaderboard`` (e.g. ``rm_1v1``, ``qm_ffa``)
    and ``opponent`` (a profile id — restricts to head-to-head games vs that player)
    all filter server-side, so the page count / total stays correct for any of them.
    """
    url = f"{_API}/players/{profile_id}/games?page={page}"
    if since:
        url += f"&since={since}"
    if leaderboard:
        url += f"&leaderboard={leaderboard}"
    if opponent:
        url += f"&opponent_profile_id={opponent}"
    data = _get_json(url, strict=True)
    games = _extract_items(data)
    total = data.get("total_count") if isinstance(data, dict) else None
    return games, (total if isinstance(total, int) else None)


def _opponent_ids(game: dict) -> list[int]:
    """One player from each of the first two teams (opposite sides), so a summary
    of an arbitrary game shows real opponents and a correct winner — even in team
    games, where the first two players might be teammates."""
    teams = game.get("teams")
    if isinstance(teams, list) and len(teams) >= 2:
        a, b = _first_pid(teams[0]), _first_pid(teams[1])
        if a is not None and b is not None:
            return [a, b]
    return _player_ids(game)[:2]


def game_summary(game_id: int, ids: tuple[int, int] | None = None) -> dict | None:
    """Fetch one game and summarise it (for display of a downloaded replay).

    ``ids`` (the profiles the user searched + downloaded for) anchors the summary
    on the searched player vs their chosen opponent — so a team/FFA replay shows
    who the user looked up, not two arbitrary participants. Falls back to two team
    representatives when ``ids`` is absent (e.g. a manually-imported replay). No
    extra request either way — the single game fetch already carries every player.
    """
    data = _get_json(f"{_API}/games/{game_id}")
    if not isinstance(data, dict):
        return None
    pair = list(ids) if ids and len(ids) >= 2 else _opponent_ids(data)
    if len(pair) < 2:
        return None
    summary = match_summary(data, pair[0], pair[1])
    summary["_id1"], summary["_id2"] = pair[0], pair[1]
    return summary


def replay_url(match_id: int, profile_id: int) -> str:
    return _RELIC_REPLAY.format(match_id=match_id, profile_id=profile_id)


def _is_replay(data: bytes) -> bool:
    """True only if the payload actually contains the ``AOE4_RE`` replay
    signature (decompressing gzip first). Checking the signature — not merely the
    gzip magic — stops a gzip-compressed error body from being saved as a replay."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return False
    return b"AOE4_RE" in data


def _save_replay(data: bytes, dest: Path) -> None:
    """Write replay bytes to ``dest`` atomically. The bytes may be gzip or raw —
    every reader (version, timestamp, playback copy) decompresses transparently —
    so they are stored as received. The temp-then-rename keeps an interrupted write
    from half-overwriting a good existing replay with a partial file."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _download_via_worldsedge(match_id: int, dest: Path) -> bool:
    """Primary source. One metadata request lists signed Azure-blob URLs for every
    available perspective; the replay then downloads straight from blob storage, so
    only that single small request touches an API host (a different one from the
    legacy Relic endpoint, with its own rate budget).

    - Returns True if a replay was saved.
    - Returns False if the match definitively has no replay (``NOT_FOUND``) — a
      delete shows on every source, so this is never second-guessed by the caller.
    - Raises :class:`_SourceUnavailable` if the list can't be fetched or none of the
      listed blobs download, so the caller can fall back to the legacy endpoint.
    """
    url = _WORLDSEDGE_REPLAYS.format(match_id=match_id)
    print(f"[replay {match_id}] primary: WorldsEdge list -> {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise _SourceUnavailable(_RATE_LIMIT_MSG) from exc
        raise _SourceUnavailable(f"replay list request failed (HTTP {exc.code})") from exc
    except Exception as exc:  # noqa: BLE001 - network/timeout -> let the caller fall back
        raise _SourceUnavailable("couldn't reach the replay list service") from exc
    try:
        meta = json.loads(payload)
    except ValueError as exc:
        raise _SourceUnavailable("replay list response was not valid JSON") from exc

    code = (meta.get("result") or {}).get("code") if isinstance(meta, dict) else None
    files = meta.get("replayFiles") if isinstance(meta, dict) else None
    if code == _WE_NOT_FOUND:
        print(f"[replay {match_id}] WorldsEdge: no replay available (deleted).")
        return False  # definitive: the match has no replay (deleted / never existed)
    if code != _WE_SUCCESS or not isinstance(files, list) or not files:
        raise _SourceUnavailable(f"unexpected replay list response (code {code})")

    # datatype 0 is the real replay (one entry per perspective that kept one); other
    # datatypes are auxiliary files. Try each real one in listed order, first valid
    # wins. The list only contains perspectives that actually have a replay, so this
    # usually succeeds on the first URL — no blind probing of empty perspectives.
    real = [
        f for f in files
        if isinstance(f, dict) and f.get("datatype") == _WE_REPLAY_DATATYPE and f.get("url")
    ]
    if not real:
        raise _SourceUnavailable("replay list had no downloadable replay")
    print(f"[replay {match_id}] WorldsEdge: {len(real)} perspective(s) available.")
    last_error: Exception | None = None
    for entry in real:
        blob = entry["url"]
        pid = entry.get("profile_id")
        print(f"[replay {match_id}] GET blob (profile {pid}) -> {blob.split('?', 1)[0]}")
        try:
            req = urllib.request.Request(blob, headers={"User-Agent": _UA})  # noqa: S310
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                data = resp.read()
        except Exception as exc:  # noqa: BLE001 - one blob (expired/hiccup) -> try the next
            last_error = exc
            print(f"[replay {match_id}] blob failed ({exc}); trying next perspective…")
            continue
        if _is_replay(data):
            _save_replay(data, dest)
            print(f"[replay {match_id}] SAVED via WorldsEdge / Azure blob ({len(data)} bytes).")
            return True
        # 200 but no AOE4_RE signature (unexpected for a listed file) -> try the next
    detail = f" ({last_error})" if last_error else ""
    raise _SourceUnavailable(f"listed replays could not be downloaded{detail}")


def _download_via_relic(match_id: int, profile_ids: list[int], dest: Path) -> bool:
    """Legacy fallback. Probe each participant's perspective on the Age of Empires
    API — one request per perspective, all on that (rate-limited) host — since a
    match's replay may only be reachable from one player's profile.

    - Returns True if a replay was saved.
    - Returns False if every perspective gave a definitive "no replay" answer.
    - Raises :class:`_SourceUnavailable` on a non-deleted failure (rate limit / net).
    """
    error: Exception | None = None
    tried: set[int] = set()
    for pid in profile_ids:
        if pid is None or pid in tried:
            continue
        tried.add(pid)
        url = replay_url(match_id, pid)
        print(f"[replay {match_id}] fallback: Relic GET -> {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):  # this id genuinely has no replay
                continue
            if exc.code == 429:  # rate limited -> stop, don't hammer the rest
                raise _SourceUnavailable(_RATE_LIMIT_MSG) from exc
            error = exc  # 5xx etc. -> a real error, but a sibling id may still work
            continue
        except Exception as exc:  # noqa: BLE001 - network/timeout, treat as error
            error = exc
            continue
        if _is_replay(data):
            _save_replay(data, dest)
            print(f"[replay {match_id}] SAVED via Relic (profile {pid}).")
            return True
        # HTTP 200 but no AOE4_RE signature -> this id has no real replay
    if error is not None:
        raise _SourceUnavailable(f"Could not reach the replay server: {error}")
    return False


def download_replay(match_id: int, profile_ids: list[int], dest: Path) -> bool:
    """Download a match replay to ``dest``.

    Tries the WorldsEdge community source first (one metadata call, then a direct
    Azure-blob download — almost nothing touches the rate-limited Relic host); if
    that source is unavailable, falls back to probing each perspective on the legacy
    Relic endpoint.

    - Returns True if a replay was saved.
    - Returns False if the match genuinely has no replay (deleted). A delete is
      reflected by both sources, so a definitive "no replay" is not second-guessed:
      when the primary reports it, the fallback is not consulted.
    - Raises :class:`RuntimeError` only when every source is unavailable.
    """
    dest = Path(dest)
    try:
        return _download_via_worldsedge(match_id, dest)
    except _SourceUnavailable as primary_exc:
        # Primary is unavailable (not a delete) -> probe perspectives on the legacy API.
        print(f"[replay {match_id}] WorldsEdge unavailable ({primary_exc}); falling back to Relic.")
        try:
            return _download_via_relic(match_id, profile_ids, dest)
        except _SourceUnavailable as fallback_exc:
            # Every source is exhausted (both rate-limited, or offline). Prefer the
            # actionable rate-limit note if either source hit it.
            reasons = (str(primary_exc), str(fallback_exc))
            message = _RATE_LIMIT_MSG if _RATE_LIMIT_MSG in reasons else str(fallback_exc)
            raise RuntimeError(message) from fallback_exc
