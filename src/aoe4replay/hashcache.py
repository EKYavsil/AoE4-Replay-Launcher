"""SHA-1 cache keyed by path + size + mtime.

Used when deciding which manifest files are missing from the seed and must be
downloaded as delta, so unchanged files are not re-hashed every run.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = Path(cache_path)
        # key -> (size, mtime_ns, sha1)
        self._entries: dict[str, tuple[int, int, str]] = {}

    def load(self) -> None:
        if not self.cache_path.exists():
            return
        for line in self.cache_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            try:
                self._entries[parts[0]] = (int(parts[1]), int(parts[2]), parts[3])
            except ValueError:
                continue  # tolerate a partially written / corrupt line, don't crash

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{key}\t{size}\t{mtime}\t{sha1}"
            for key, (size, mtime, sha1) in sorted(self._entries.items())
        ]
        # Atomic write: a crash or concurrent read never sees a half-written cache.
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, self.cache_path)

    def sha1(self, path: Path) -> str:
        """Return the SHA-1 for ``path``, recomputing only if size/mtime changed."""
        resolved = Path(path).resolve()
        key = str(resolved)
        st = resolved.stat()
        cached = self._entries.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        digest = _hash_file(resolved)
        self._entries[key] = (st.st_size, st.st_mtime_ns, digest)
        return digest
