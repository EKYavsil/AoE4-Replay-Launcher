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
_UA = "aoe4-replay-launcher"

_GAME_ID_RE = re.compile(r"AgeIV_Replay_(\d+)", re.IGNORECASE)

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


def game_id_from_name(name: str) -> int | None:
    """Extract the aoe4world game id from a replay filename.

    Matches ``AgeIV_Replay_<id>`` or, for panel downloads named like
    ``nick1_nick2_25-12-01_<id>.rec``, the trailing run of 7+ digits (the game
    id; short date fields never reach that length)."""
    match = _GAME_ID_RE.search(name)
    if match:
        return int(match.group(1))
    runs = re.findall(r"\d{7,}", name)
    return int(runs[-1]) if runs else None


class Aoe4WorldError(Exception):
    """A request to aoe4world failed (network, rate limit, server, bad JSON) — as
    opposed to a request that succeeded and simply returned no results."""


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
                "aoe4world is rate-limiting requests; wait a moment and try again"
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


def are_opponents(game: dict, id1: int, id2: int) -> bool:
    """True if the two profiles played on opposite teams in this game."""
    team_of = _team_index(game)
    if id1 in team_of and id2 in team_of:
        return team_of[id1] != team_of[id2]
    players = game.get("players")
    if isinstance(players, list):
        results = {p.get("profile_id"): p.get("result") for p in players}
        r1, r2 = results.get(id1), results.get(id2)
        if r1 and r2:
            return {r1, r2} == {"win", "loss"}
    return False


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

    return {
        "game_id": _game_id(game),
        "name1": _player_field(game, id1, "name") or str(id1),
        "name2": _player_field(game, id2, "name") or str(id2),
        "civ1": _civ_abbrev(_player_field(game, id1, "civilization")),
        "civ2": _civ_abbrev(_player_field(game, id2, "civilization")),
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
    profile_id: int, page: int = 1, since: str | None = None
) -> tuple[list[dict], int | None]:
    """One page (50) of a player's games plus the total game count.

    The page already contains full per-game info (players, civs, map, result),
    so no extra call per game is needed. Pages are fetched on demand only.
    ``since`` (a ``YYYY-MM-DD`` date) filters server-side by ``started_at``,
    shrinking the total — and thus the number of pages — for a date range.
    """
    url = f"{_API}/players/{profile_id}/games?page={page}"
    if since:
        url += f"&since={since}"
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


def h2h_games(
    id1: int, id2: int, max_pages: int = 10, limit: int = 50, since: str | None = None
) -> list[dict]:
    """All head-to-head games (opposite teams), newest first.

    ``since`` (a ``YYYY-MM-DD`` date) filters server-side by ``started_at``.
    """
    raw: list[dict] = []
    page = 1
    since_q = f"&since={since}" if since else ""
    while page <= max_pages:
        url = (
            f"{_API}/players/{id1}/games"
            f"?opponent_profile_id={id2}&limit={limit}&page={page}{since_q}"
        )
        # First page strict so a failure is reported; later pages best-effort.
        items = _extract_items(_get_json(url, strict=(page == 1)))
        if not items:
            break
        raw.extend(items)
        if len(items) < limit:
            break
        page += 1

    seen: set = set()
    uniq: list[dict] = []
    for game in raw:
        key = _game_id(game) or (game.get("kind"), game.get("started_at"))
        if key not in seen:
            seen.add(key)
            uniq.append(game)

    uniq.sort(
        key=lambda g: _parse_iso(g.get("started_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return [g for g in uniq if are_opponents(g, id1, id2)]


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


def download_replay(match_id: int, profile_ids: list[int], dest: Path) -> bool:
    """Download a match replay to ``dest``, trying each participant's perspective.

    A match's replay may only be reachable from one player's profile, so every
    unique id in ``profile_ids`` is tried in turn (the displayed players first,
    then the remaining team members).

    - Returns True if a replay was saved.
    - Returns False only if every id gave a definitive "no replay" answer (the
      replay has been deleted).
    - Raises on request failures. A rate-limit (HTTP 429) stops immediately
      rather than hammering the remaining perspectives (which would make it worse).
    """
    dest = Path(dest)
    error: Exception | None = None
    tried: set[int] = set()
    for pid in profile_ids:
        if pid is None or pid in tried:
            continue
        tried.add(pid)
        url = replay_url(match_id, pid)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):  # this id genuinely has no replay
                continue
            if exc.code == 429:  # rate limited — stop now, don't try more perspectives
                raise RuntimeError(
                    "The Age of Empires replay server is rate-limiting replay "
                    "downloads (HTTP 429). Wait a minute and try again."
                ) from exc
            error = exc  # 5xx etc. — a real error, but a sibling id may still work
            continue
        except Exception as exc:  # noqa: BLE001 - network/timeout, treat as error
            error = exc
            continue
        if _is_replay(data):
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write atomically: an interrupted download must never leave a good
            # existing replay half-overwritten with a partial file.
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return True
        # HTTP 200 but no AOE4_RE signature -> this id has no real replay
    if error is not None:
        raise RuntimeError(f"Could not reach the replay server: {error}")
    return False
