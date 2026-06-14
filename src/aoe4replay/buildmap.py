"""Resolving a replay timestamp to a Steam build.

The build map (``data/aoe4-build-map.json``) maps date ranges to depot manifest
ids. When a replay falls outside every known range, an entry can be inferred
from the manifest history.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_DEFAULT_DEPOT = "1466861"


@dataclass(frozen=True)
class Depot:
    id: str
    manifest: str


@dataclass(frozen=True)
class Build:
    name: str
    build_id: str
    valid_from: datetime | None
    valid_to: datetime | None
    depots: tuple[Depot, ...]
    install_dir: str | None = None
    replay_version_override: int = 0
    # RelicCardinal.exe build number (== the replay's embedded version). Harvested
    # from the exe; the primary, locale-independent key for resolving a replay.
    version: int = 0

    @property
    def path_id(self) -> str:
        """Filesystem-safe identifier used for cache/restore directories."""
        return re.sub(r"[^A-Za-z0-9_.-]", "_", self.build_id)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _build_from_dict(entry: dict) -> Build:
    depots = tuple(
        Depot(id=str(d["id"]), manifest=str(d["manifest"])) for d in entry.get("depots", [])
    )
    return Build(
        name=str(entry.get("name", entry.get("buildId", "unknown"))),
        build_id=str(entry["buildId"]),
        valid_from=_parse_dt(entry.get("validFrom")),
        valid_to=_parse_dt(entry.get("validTo")),
        depots=depots,
        install_dir=entry.get("installDir"),
        replay_version_override=int(entry.get("replayVersionOverride") or 0),
        version=int(entry.get("version") or 0),
    )


def load_build_map(path: Path) -> list[Build]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [_build_from_dict(e) for e in data]


def build_for_timestamp(stamp: datetime, builds: list[Build]) -> Build:
    for build in builds:
        start = build.valid_from or datetime.min
        end = build.valid_to or datetime.max
        if start <= stamp < end:
            return build
    raise LookupError(f"No build map entry covers replay timestamp {stamp:%Y-%m-%d %H:%M}.")


def build_for_version(version: int, builds: list[Build]) -> Build | None:
    """The build whose harvested exe version matches the replay version, if any.

    This is the primary, locale-independent resolver: it never touches dates, so
    it cannot mis-resolve a replay through day/month ambiguity. Returns ``None``
    when the version has not been harvested yet (callers fall back to the date).
    """
    if not version:
        return None
    matches = [b for b in builds if b.version == version]
    return matches[0] if len(matches) == 1 else None


def build_to_entry(build: Build) -> dict:
    """Serialise a Build back to its build-map JSON shape."""
    entry: dict = {
        "name": build.name,
        "validFrom": build.valid_from.isoformat() if build.valid_from else None,
        "validTo": build.valid_to.isoformat() if build.valid_to else None,
        "buildId": build.build_id,
        "installDir": build.install_dir,
        "depots": [{"id": d.id, "manifest": d.manifest} for d in build.depots],
    }
    if build.version:
        entry["version"] = build.version
    if build.replay_version_override:
        entry["replayVersionOverride"] = build.replay_version_override
    return entry


def save_build_map(builds: list[Build], path: Path) -> None:
    ordered = sorted(builds, key=lambda b: b.valid_from or datetime.min)
    entries = [build_to_entry(b) for b in ordered]
    # Atomic write: a concurrent reader / a crash mid-write never sees a corrupt
    # (half-written) build map.
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=4), encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class HistoryEntry:
    valid_from: datetime
    manifest: str


def load_manifest_history(path: Path) -> list[HistoryEntry]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    entries = [
        HistoryEntry(valid_from=datetime.fromisoformat(e["validFrom"]), manifest=str(e["manifest"]))
        for e in json.loads(raw)
        if e.get("validFrom") and e.get("manifest")
    ]
    return sorted(entries, key=lambda e: e.valid_from)


def infer_build(
    stamp: datetime, history: list[HistoryEntry], depot_id: str = _DEFAULT_DEPOT
) -> Build | None:
    """Infer a build entry for ``stamp`` from the manifest history.

    Picks the latest history entry effective at or before ``stamp``; the build's
    ``valid_to`` is the next entry's ``valid_from`` (or open-ended).
    """
    selected_index = -1
    for i, entry in enumerate(history):
        if stamp >= entry.valid_from:
            selected_index = i
        else:
            break
    if selected_index < 0:
        return None

    selected = history[selected_index]
    valid_to = (
        history[selected_index + 1].valid_from if selected_index + 1 < len(history) else None
    )
    build_id = f"{selected.valid_from:%Y-%m-%d}-live"
    return Build(
        name=build_id,
        build_id=build_id,
        valid_from=selected.valid_from,
        valid_to=valid_to,
        depots=(Depot(id=depot_id, manifest=selected.manifest),),
    )
