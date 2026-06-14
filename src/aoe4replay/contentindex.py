"""Content index: which (size, SHA-1) files we already have in the restic store.

Lets a gap file be sourced from a sibling build already in restic (a local
restore) instead of being re-downloaded from Steam. The index maps a file's
content identity to where a copy lives in the repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Location:
    snapshot: str       # restic snapshot short id holding a copy
    stored_path: str    # the file's path inside the snapshot (restic "ls" form)


class ContentIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._by_content: dict[str, Location] = {}

    @staticmethod
    def _key(size: int, sha1: str) -> str:
        return f"{size}|{sha1.lower()}"

    def load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            size, sha1, snapshot, stored = parts
            try:
                key = self._key(int(size), sha1)
            except ValueError:
                continue  # tolerate a partially written / corrupt line, don't crash
            self._by_content[key] = Location(snapshot, stored)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for key, loc in sorted(self._by_content.items()):
            size, sha1 = key.split("|", 1)
            lines.append(f"{size}\t{sha1}\t{loc.snapshot}\t{loc.stored_path}")
        # Atomic write: a crash or concurrent read never sees a half-written index.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, size: int, sha1: str, snapshot: str, stored_path: str) -> None:
        self._by_content.setdefault(self._key(size, sha1), Location(snapshot, stored_path))

    def lookup(self, size: int, sha1: str) -> Location | None:
        return self._by_content.get(self._key(size, sha1))

    def __len__(self) -> int:
        return len(self._by_content)
