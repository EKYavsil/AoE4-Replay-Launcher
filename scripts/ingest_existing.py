"""One-time import of an existing AOE4ReplayBuilds delta_* cache into restic.

Reads the old cache READ-ONLY and writes snapshots into the new restic repo;
the source cache is never modified. Thin wrapper around `aoe4replay ingest`.
Implemented in Phase 5.
"""

from __future__ import annotations

raise SystemExit("Not implemented yet (Phase 5). Use: aoe4replay ingest <cache-dir>")
